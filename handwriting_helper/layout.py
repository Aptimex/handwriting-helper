'''
Turn model-unit lines into millimetre page coordinates.

Two things here are worth knowing about:

  * Lines are stacked by *baseline*, not by bounding box. Cropping each line to its own ink
    (which is all the legacy moveToOrigin() does) throws away the information that makes a
    paragraph look like a paragraph: ascenders, descenders and a common left edge.

  * The scale comes from a measured x-height rather than a constant, because the model's
    styles differ in size by nearly 3x. See generate.calibrate_x_height().
'''

import sys

from .strokes import MODEL_X_HEIGHT

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


class LayoutError(ValueError):
    pass


def parse_page(spec):
    '''"a4", "letter", "120x80" (mm), or "fit" for a page cropped to the ink.'''
    if spec is None:
        return None
    key = str(spec).strip().lower()
    if key == "fit":
        return "fit"
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
    raise LayoutError("unknown page size {!r}; try one of {}, WxH in mm, or 'fit'".format(
        spec, ", ".join(sorted(PAGE_SIZES))))


class Page:
    '''Laid-out ink in millimetres, y growing downward from the top-left corner.'''

    def __init__(self, width_mm, height_mm, strokes, mm_per_unit, x_height_mm, line_pitch_mm):
        self.width_mm = width_mm
        self.height_mm = height_mm
        self.strokes = strokes
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


def layout(document, page="a4", margin_mm=20.0, x_height_mm=3.0, line_pitch_mm=None,
           align="left", mm_per_unit=None, warn=True):
    '''
    document       a Document of lines in model units
    page           "a4" / "letter" / "WxH" / "fit" / None (same as "fit")
    margin_mm      single value, or (left, top, right, bottom)
    x_height_mm    target height of an x-height letter
    line_pitch_mm  baseline-to-baseline distance; defaults to 2.7 * x_height_mm
    align          "left", "center" or "right"
    mm_per_unit    bypass x_height_mm and set the scale directly
    '''
    if align not in ("left", "center", "right"):
        raise LayoutError("align must be left, center or right (got {!r})".format(align))

    units = document.x_height_units or MODEL_X_HEIGHT
    if mm_per_unit is None:
        mm_per_unit = x_height_mm / units
    else:
        x_height_mm = mm_per_unit * units
    if mm_per_unit <= 0:
        raise LayoutError("scale must be positive")

    if line_pitch_mm is None:
        line_pitch_mm = DEFAULT_LINE_PITCH_RATIO * x_height_mm

    size = parse_page(page) if page is not None else "fit"
    ml, mt, mr, mb = _margins(margin_mm)

    # Place every line against a notional origin first, then decide where that origin goes.
    placed = []          # (baseline_mm, [[(x_mm, y_mm), ...], ...])
    baseline = 0.0
    for line in document.lines:
        if line.blank or not line.strokes:
            baseline += line_pitch_mm
            continue
        strokes = [[(x * mm_per_unit, baseline + y * mm_per_unit) for x, y in stroke]
                   for stroke in line.strokes]
        placed.append((baseline, strokes))
        baseline += line_pitch_mm

    if not placed:
        raise LayoutError("nothing to lay out: the document has no ink")

    if warn:
        _warn_collisions(placed)

    if size == "fit":
        strokes = [s for _, ss in placed for s in ss]
        xs = [p[0] for s in strokes for p in s]
        ys = [p[1] for s in strokes for p in s]
        width = (max(xs) - min(xs)) + ml + mr
        height = (max(ys) - min(ys)) + mt + mb
        dx, dy = ml - min(xs), mt - min(ys)
        out = [[(x + dx, y + dy) for x, y in s] for s in strokes]
        return Page(width, height, out, mm_per_unit, x_height_mm, line_pitch_mm)

    width, height = size
    text_width = width - ml - mr
    if text_width <= 0:
        raise LayoutError("margins leave no room on a {:g} mm wide page".format(width))

    # The first baseline sits one x-height's worth of ascender below the top margin.
    top_offset = mt + 2.0 * x_height_mm

    out = []
    for _, strokes in placed:
        xs = [p[0] for s in strokes for p in s]
        line_width = max(xs) - min(xs)
        if align == "left":
            dx = ml            # x=0 is the pen's start position, so left edges line up
        elif align == "center":
            dx = ml + (text_width - line_width) / 2.0 - min(xs)
        else:
            dx = ml + text_width - line_width - min(xs)
        out.extend([[(x + dx, y + top_offset) for x, y in s] for s in strokes])

    page_obj = Page(width, height, out, mm_per_unit, x_height_mm, line_pitch_mm)
    if warn:
        _warn_overflow(page_obj, ml, mt, mr, mb)
    return page_obj


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
    for (_, above), (_, below) in zip(placed, placed[1:]):
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
# poke a fraction of a millimetre past the margin. Only complain about real overflow.
_OVERFLOW_TOLERANCE_MM = 1.0


def _warn_overflow(page, ml, mt, mr, mb):
    box = page.bbox()
    if box is None:
        return
    x0, y0, x1, y1 = box
    over = max(x1 - (page.width_mm - mr), ml - x0)
    if over > _OVERFLOW_TOLERANCE_MM:
        print("warning: text overruns the side margins by {:.1f} mm".format(over),
              file=sys.stderr)
    below = y1 - (page.height_mm - mb)
    if below > _OVERFLOW_TOLERANCE_MM:
        print("warning: text runs {:.1f} mm past the bottom margin".format(below),
              file=sys.stderr)
