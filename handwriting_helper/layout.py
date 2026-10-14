'''
Turn model-unit lines into millimetre pages.

Lines arrive already fitted to the width (generate.Generator.flow measures the ink while
breaking them), so this module only has to scale, stack and paginate:

  * Lines are stacked by *baseline*, not by bounding box. Cropping each line to its own ink
    throws away the information that makes a paragraph look like a paragraph: ascenders,
    descenders and a common left edge.

  * The scale comes from the style's measured x-height (styles.json) rather than a constant,
    because the model's styles differ in size by nearly 4x.

  * Page height is a fixed number of baselines, so a long text simply continues onto the
    next page.
'''

import sys


# width, height in mm
PAGE_SIZES = {
    "a3": (297.0, 420.0),
    "a4": (210.0, 297.0),
    "a5": (148.0, 210.0),
    "a6": (105.0, 148.0),
    "letter": (215.9, 279.4),
    "legal": (215.9, 355.6),
}

# Line pitch as a multiple of x-height. Ruled paper runs about 8 mm for a 2.5 mm x-height,
# which is where this sits; going much tighter starts to put descenders into the ascenders
# of the line below.
DEFAULT_LINE_PITCH_RATIO = 3.1

# Room left for ascenders above the first baseline and descenders below the last, as
# multiples of x-height.
ASCENDER_ROOM = 2.0
DESCENDER_ROOM = 1.5


class LayoutError(ValueError):
    pass


def parse_page(spec):
    '''
    "a4", "letter", "120x80" (mm), or a (width, height) pair -> (width, height) in mm.

    "fit" (the reachable area of a printer's bed) is not handled here: it depends on the
    printer profile, so the caller resolves it to a size first.
    '''
    if isinstance(spec, (tuple, list)):
        w, h = (float(v) for v in spec)
        if w <= 0 or h <= 0:
            raise LayoutError("page dimensions must be positive: {!r}".format(spec))
        return (w, h)
    key = str(spec).strip().lower()
    if key in PAGE_SIZES:
        return PAGE_SIZES[key]
    if "x" in key:
        try:
            w, h = (float(v) for v in key.split("x", 1))
        except ValueError:
            raise LayoutError("bad page size {!r}".format(spec))
        if w <= 0 or h <= 0:
            raise LayoutError("page dimensions must be positive: {!r}".format(spec))
        return (w, h)
    raise LayoutError("unknown page size {!r}; try one of {}, or WxH in mm".format(
        spec, ", ".join(sorted(PAGE_SIZES))))


class Page:
    '''
    Laid-out ink in millimetres, y growing downward from the top-left corner.

    `lines` keeps the strokes grouped by text line, top to bottom, so a page can later be cut
    between lines (G-code for a page taller than the bed). `strokes` is the same ink, flat.
    '''

    def __init__(self, width_mm, height_mm, strokes, mm_per_unit, x_height_mm, line_pitch_mm,
                 lines=None):
        self.width_mm = width_mm
        self.height_mm = height_mm
        self.strokes = strokes
        self.lines = lines if lines is not None else [strokes]
        self.mm_per_unit = mm_per_unit
        self.x_height_mm = x_height_mm
        self.line_pitch_mm = line_pitch_mm

    def bbox(self):
        pts = [p for s in self.strokes for p in s]
        if not pts:
            return None
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return min(xs), min(ys), max(xs), max(ys)

    def point_count(self):
        return sum(len(s) for s in self.strokes)


def resolve_scale(x_height_units, x_height_mm=3.0, mm_per_unit=None):
    '''(mm_per_unit, x_height_mm) from either a target x-height or an explicit scale.'''
    if not x_height_units:
        raise LayoutError("the document has no x-height, so it cannot be scaled")
    if mm_per_unit is None:
        mm_per_unit = x_height_mm / x_height_units
    else:
        x_height_mm = mm_per_unit * x_height_units
    if mm_per_unit <= 0:
        raise LayoutError("scale must be positive")
    return mm_per_unit, x_height_mm


def text_width_mm(page, margin_mm):
    '''Width between the side margins.'''
    size = parse_page(page)
    ml, _, mr, _ = _margins(margin_mm)
    width = size[0] - ml - mr
    if width <= 0:
        raise LayoutError("margins leave no room on a {:g} mm wide page".format(size[0]))
    return width


def layout(document, page="a4", margin_mm=20.0, x_height_mm=3.0, line_pitch_mm=None,
           align="left", mm_per_unit=None, warn=True):
    '''
    Lay a document out on as many pages as it needs. Returns a list of Page.

    document       a Document of lines in model units
    page           "a4" / "letter" / "WxH" / (width, height) in mm
    margin_mm      single value, or (left, top, right, bottom)
    x_height_mm    target height of an x-height letter
    line_pitch_mm  baseline-to-baseline distance; defaults to DEFAULT_LINE_PITCH_RATIO times
                   the x-height
    align          "left", "center" or "right"
    mm_per_unit    bypass x_height_mm and set the scale directly
    '''
    if align not in ("left", "center", "right"):
        raise LayoutError("align must be left, center or right (got {!r})".format(align))

    mm_per_unit, x_height_mm = resolve_scale(document.x_height_units, x_height_mm,
                                             mm_per_unit)
    if line_pitch_mm is None:
        line_pitch_mm = DEFAULT_LINE_PITCH_RATIO * x_height_mm

    size = parse_page(page)
    ml, mt, mr, mb = _margins(margin_mm)

    # Scale every line about its own baseline; None marks a blank line.
    rows = []
    for line in document.lines:
        if line.blank or not line.strokes:
            rows.append(None)
            continue
        rows.append([[(x * mm_per_unit, y * mm_per_unit) for x, y in stroke]
                     for stroke in line.strokes])
    if not any(rows):
        raise LayoutError("nothing to lay out: the document has no ink")

    width, height = size
    text_width = width - ml - mr
    if text_width <= 0:
        raise LayoutError("margins leave no room on a {:g} mm wide page".format(width))
    first_baseline = mt + ASCENDER_ROOM * x_height_mm
    last_baseline = height - mb - DESCENDER_ROOM * x_height_mm
    per_page = int((last_baseline - first_baseline) // line_pitch_mm) + 1
    if per_page < 1:
        raise LayoutError("a {:g} x {:g} mm page with these margins has no room for a line "
                          "at a {:g} mm x-height".format(width, height, x_height_mm))

    pages = []
    for chunk in _paginate(rows, per_page):
        placed = _stack(chunk, line_pitch_mm)
        if warn:
            _warn_collisions(placed)
        lines = []
        for strokes in placed:
            xs = [p[0] for s in strokes for p in s]
            line_width = max(xs) - min(xs)
            if align == "left":
                dx = ml            # x=0 is the pen's start position, so left edges line up
            elif align == "center":
                dx = ml + (text_width - line_width) / 2.0 - min(xs)
            else:
                dx = ml + text_width - line_width - min(xs)
            lines.append([[(x + dx, y + first_baseline) for x, y in s] for s in strokes])
        out = [s for line in lines for s in line]
        pages.append(Page(width, height, out, mm_per_unit, x_height_mm, line_pitch_mm, lines))
    if warn:
        _warn_overflow(pages, ml, mr)
    return pages


def _paginate(rows, per_page):
    '''Split rows into pages of `per_page`, dropping blank lines at the top of a page.'''
    pages, current = [], []
    for row in rows:
        if row is None and not current:
            continue
        current.append(row)
        if len(current) == per_page:
            pages.append(current)
            current = []
    if any(r is not None for r in current):
        pages.append(current)
    return pages


def _stack(rows, pitch):
    '''Move each row down to its baseline. Blank rows take a pitch and draw nothing.'''
    placed = []
    for k, row in enumerate(rows):
        if row is not None:
            placed.append([[(x, y + k * pitch) for x, y in s] for s in row])
    return placed


def _margins(margin_mm):
    if isinstance(margin_mm, (int, float)):
        return (float(margin_mm),) * 4
    values = list(margin_mm)
    if len(values) == 2:
        return values[0], values[1], values[0], values[1]
    if len(values) == 4:
        return tuple(float(v) for v in values)
    raise LayoutError("margin must be one, two or four numbers")


# Collisions are tested per column rather than by comparing whole-line extents: a
# descender on one line and an ascender on the next overlap vertically on nearly every
# page of real handwriting without ever touching, because they sit at different x.
_COLLISION_BIN_MM = 1.5
_COLLISION_TOLERANCE_MM = 0.3


def _warn_collisions(placed):
    '''Warn when ink from one line actually runs into ink from the next.'''
    def columns(strokes):
        out = {}
        for stroke in strokes:
            for x, y in stroke:
                key = int(x // _COLLISION_BIN_MM)
                lo, hi = out.get(key, (y, y))
                out[key] = (min(lo, y), max(hi, y))
        return out

    worst = 0.0
    for above, below in zip(placed, placed[1:]):
        top_cols = columns(above)
        bottom_cols = columns(below)
        for key, (_, bottom_of_above) in top_cols.items():
            # A glyph is wider than one bin, so check the neighbouring columns too.
            for neighbour in (key - 1, key, key + 1):
                if neighbour in bottom_cols:
                    worst = max(worst, bottom_of_above - bottom_cols[neighbour][0])
    if worst > _COLLISION_TOLERANCE_MM:
        print("warning: adjacent lines collide by up to {:.1f} mm; raise --line-pitch "
              "or lower --x-height-mm".format(worst), file=sys.stderr)


# Lines are anchored on the pen's starting x, not their ink bounds, so a leading flourish can
# poke a fraction of a millimetre past the margin. Only complain about real overflow, which
# now only happens when saved strokes are re-laid onto a narrower page than they were set for.
_OVERFLOW_TOLERANCE_MM = 1.0


def _warn_overflow(pages, ml, mr):
    over = 0.0
    for page in pages:
        box = page.bbox()
        if box is not None:
            over = max(over, box[2] - (page.width_mm - mr), ml - box[0])
    if over > _OVERFLOW_TOLERANCE_MM:
        print("warning: text overruns the side margins by {:.1f} mm. Generate again if this is a problem.".format(over),
              file=sys.stderr)
