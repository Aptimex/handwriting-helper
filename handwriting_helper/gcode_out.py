'''
G-code output.

Written straight from the stroke points rather than by re-reading an SVG. The strokes already
carry explicit pen-up/pen-down boundaries, so there is nothing to infer: every stroke is one
pen-down move, and the gaps between them are the lifts. That is the whole reason this project
exists, and routing it through an SVG converter would throw it away and guess it back.

Nothing here emits heater, fan or extruder commands. If a machine needs them, they belong in
a profile's preamble.
'''

import math
import tomllib
from pathlib import Path

from .strokes import simplify

PROFILE_DIR = Path(__file__).resolve().parent.parent / "profiles"

DEFAULTS = {
    "name": "generic",
    "pen_down_z": 0.0,       # Z where the pen touches the paper
    "pen_up_z": 2.0,         # Z for travel between strokes
    "safe_z": 10.0,          # Z to finish at, clear of the work
    "feed_draw": 1800.0,     # mm/min while drawing
    "feed_travel": 6000.0,   # mm/min while travelling
    "feed_z": 1200.0,        # mm/min for pen up/down moves
    "paper_anchor": "top-left",  # which part of the bed the paper is laid against (ANCHORS)
    "bed": None,             # [x, y] machine limits; None disables the check
    "flip_y": True,          # page y grows downward, machine y grows away from Y0
    "pen_offset_x": 0.0,     # where the pen tip is relative to the nozzle, in mm: + is toward
    "pen_offset_y": 0.0,     # X max (right) and Y max (the back of the bed)
    "pen_dwell_ms": 0,       # pause after each pen up/down, for a pen that needs to settle
    "preamble": ["G21", "G90"],
    "postamble": [],
    "park": None,            # [x, y] to move to at the end
}

# Where the paper is laid on the bed, flush against that corner or edge of it, or centred.
# "top" is the back of the bed (high Y), "bottom" the front (Y0), as you look at the machine.
ANCHORS = ("top-left", "top-center", "top-right",
           "center-left", "center", "center-right",
           "bottom-left", "bottom-center", "bottom-right")

_NUMERIC = ("pen_down_z", "pen_up_z", "safe_z", "feed_draw", "feed_travel", "feed_z",
            "pen_dwell_ms", "pen_offset_x", "pen_offset_y")


class ProfileError(ValueError):
    pass


class BedLimitError(ValueError):
    pass


def load_profile(spec):
    '''
    Load a printer profile. `spec` is a path to a .toml file, or the stem of one in profiles/.
    Returns a dict with every key in DEFAULTS present.
    '''
    profile = dict(DEFAULTS)
    if spec is None:
        return profile

    path = Path(spec)
    if not path.is_file():
        candidate = PROFILE_DIR / "{}.toml".format(spec)
        if candidate.is_file():
            path = candidate
        else:
            raise ProfileError("no such profile: {} (looked in {})".format(spec, PROFILE_DIR))

    with open(path, "rb") as fh:
        data = tomllib.load(fh)

    unknown = set(data) - set(DEFAULTS)
    if unknown:
        raise ProfileError("{}: unknown setting(s) {}".format(
            path, ", ".join(sorted(unknown))))
    profile.update(data)
    profile.setdefault("name", path.stem)

    for key in _NUMERIC:
        profile[key] = float(profile[key])
    if profile["paper_anchor"] not in ANCHORS:
        raise ProfileError("{}: paper_anchor must be one of {} (got {!r})".format(
            path, ", ".join(ANCHORS), profile["paper_anchor"]))
    for key in ("bed", "park"):
        if profile[key] is not None:
            values = list(profile[key])
            if len(values) != 2:
                raise ProfileError("{}: {} must be two numbers".format(path, key))
            profile[key] = [float(v) for v in values]
    if profile["pen_up_z"] <= profile["pen_down_z"]:
        raise ProfileError("{}: pen_up_z must be above pen_down_z".format(path))
    return profile


def reachable_area(profile):
    '''
    The largest page the pen can reach, (width, height) in mm: the whole bed, less the strip
    along each side that a pen mounted off the nozzle cannot get to.
    '''
    if not profile["bed"]:
        raise ProfileError("profile {!r} declares no bed, so there is nothing to fit the page "
                           "to".format(profile["name"]))
    width = profile["bed"][0] - abs(profile["pen_offset_x"])
    height = profile["bed"][1] - abs(profile["pen_offset_y"])
    if width <= 0 or height <= 0:
        raise ProfileError("profile {!r} offsets the pen further than its bed is wide".format(
            profile["name"]))
    return width, height


def reachable_anchor(profile):
    '''
    The paper_anchor that lays a reachable_area() page over the part of the bed the pen can
    reach. That part touches the bed's edges on the side the pen is offset toward, so the
    anchor moves to that side; along an axis with no offset it is left as the profile has it.
    '''
    vertical, horizontal = _split_anchor(profile["paper_anchor"])
    dx, dy = profile["pen_offset_x"], profile["pen_offset_y"]
    if dx:
        horizontal = "right" if dx > 0 else "left"
    if dy:
        vertical = "top" if dy > 0 else "bottom"
    if vertical == horizontal == "center":
        return "center"
    return "{}-{}".format(vertical, horizontal)


def _split_anchor(anchor):
    '''"top-left" -> ("top", "left"); "center" -> ("center", "center").'''
    if anchor == "center":
        return "center", "center"
    vertical, _, horizontal = anchor.partition("-")
    return vertical, horizontal


def paper_corner(width, height, profile):
    '''
    Where a sheet of `width` x `height` mm laid at the profile's paper_anchor has its corner
    nearest X0 Y0, in machine coordinates. Without a bed there is nothing to lay it against,
    so that corner is on X0 Y0.
    '''
    bed = profile["bed"]
    if not bed:
        return 0.0, 0.0
    vertical, horizontal = _split_anchor(profile["paper_anchor"])
    x = {"left": 0.0, "center": (bed[0] - width) / 2.0, "right": bed[0] - width}[horizontal]
    y = {"bottom": 0.0, "center": (bed[1] - height) / 2.0, "top": bed[1] - height}[vertical]
    return x, y


def _flatten_bezier(p0, c1, c2, p3, tolerance):
    '''Subdivide a cubic until it is flat to `tolerance`, returning the points after p0.'''
    # Distance of the control points from the chord bounds the flatness error closely enough.
    ax, ay = p0
    bx, by = p3
    chord = math.hypot(bx - ax, by - ay)
    if chord < 1e-9:
        error = max(math.hypot(c1[0] - ax, c1[1] - ay), math.hypot(c2[0] - ax, c2[1] - ay))
    else:
        def dist(p):
            return abs((by - ay) * (p[0] - ax) - (bx - ax) * (p[1] - ay)) / chord
        error = max(dist(c1), dist(c2))

    steps = max(1, min(64, int(math.ceil(math.sqrt(error / max(tolerance, 1e-6)) * 4))))
    out = []
    for i in range(1, steps + 1):
        t = i / steps
        u = 1.0 - t
        out.append((
            u * u * u * ax + 3 * u * u * t * c1[0] + 3 * u * t * t * c2[0] + t * t * t * bx,
            u * u * u * ay + 3 * u * u * t * c1[1] + 3 * u * t * t * c2[1] + t * t * t * by,
        ))
    return out


def flatten(beziers, tolerance=0.05):
    '''Bezier control polys -> a polyline, for a machine that only moves in straight lines.'''
    if not beziers:
        return []
    points = [beziers[0][0]]
    for p0, c1, c2, p3 in beziers:
        points.extend(_flatten_bezier(p0, c1, c2, p3, tolerance))
    return points


def to_machine(page, profile):
    '''
    Page millimetres -> machine millimetres.

    The page has its origin at the top-left with y growing downward; a printer bed has its
    origin at the front-left with y growing away from it. The paper is laid on the bed at the
    profile's paper_anchor, and with flip_y the right way up: its top toward the back.

    Machine coordinates are where the nozzle goes, so a pen mounted off the nozzle is reached
    by moving the nozzle the other way by the pen's offset.
    '''
    return _place(page.strokes, page, profile)


def _place(strokes, page, profile):
    ox, oy = paper_corner(page.width_mm, page.height_mm, profile)
    ox -= profile["pen_offset_x"]
    oy -= profile["pen_offset_y"]
    if profile["flip_y"]:
        return [[(ox + x, oy + (page.height_mm - y)) for x, y in stroke]
                for stroke in strokes]
    return [[(ox + x, oy + y) for x, y in stroke] for stroke in strokes]


class Section:
    '''
    One bed-sized band of a page, in machine (nozzle) coordinates.

    shift_y is how far the paper has to be moved along Y from its usual place (laid at the
    profile's paper_anchor) for this band to be in reach: negative is toward Y0.
    '''

    def __init__(self, strokes, shift_y=0.0):
        self.strokes = strokes
        self.shift_y = shift_y


def _y_range(strokes):
    ys = [p[1] for s in strokes for p in s]
    return min(ys), max(ys)


def bed_sections(page, profile):
    '''
    Cut a page that is taller than the bed into bands that each fit its Y travel.

    Cuts only fall between text lines, never through one, and the page is cut into as few
    bands as fit. Where that number allows, the lines already in reach with the paper in its
    usual place form one band that needs no shift; every other band has the paper slid along
    Y by a whole number of millimetres so it can be measured. Returns a list of Section, top
    of the page first. A page that fits, or a profile without a bed, gives a single unshifted
    Section.
    '''
    bed = profile["bed"]
    lines = [_place(line, page, profile) for line in page.lines if line]
    lines = [line for line in lines if any(line)]
    everything = [s for line in lines for s in line]
    if not bed or not everything:
        return [Section(everything)]
    lo, hi = _y_range(everything)
    if lo >= 0 and hi <= bed[1]:
        return [Section(everything)]

    ranges = [_y_range(line) for line in lines]
    groups = _group(ranges, 0, len(lines), bed[1])
    in_reach = [i for i, (l, h) in enumerate(ranges) if l >= 0 and h <= bed[1]]
    if in_reach:
        # Machine y is monotonic down the page, so the lines in reach are one contiguous run.
        # Keeping them as a band that needs no paper move is only worth it when it costs no
        # extra part: text that fits the bed in one go is better written with one slide.
        start, stop = in_reach[0], in_reach[-1] + 1
        anchored = (_group(ranges, 0, start, bed[1]) + [list(range(start, stop))]
                    + _group(ranges, stop, len(lines), bed[1]))
        if len(anchored) <= len(groups):
            groups = anchored

    sections = []
    for group in groups:
        strokes = [s for i in group for s in lines[i]]
        lo, hi = _y_range(strokes)
        if hi > bed[1]:
            shift = -math.ceil(hi - bed[1])
        elif lo < 0:
            shift = math.ceil(-lo)
        else:
            shift = 0
        if shift:
            strokes = [[(x, y + shift) for x, y in s] for s in strokes]
        sections.append(Section(strokes, float(shift)))
    return sections


def _group(ranges, start, stop, bed_y):
    '''Greedily pack lines start..stop into runs that span no more than the bed.'''
    # One millimetre in hand, so a band's shift can be rounded up to a whole millimetre.
    limit = bed_y - 1.0
    groups, current, lo, hi = [], [], None, None
    for i in range(start, stop):
        l, h = ranges[i]
        if current and max(hi, h) - min(lo, l) > limit:
            groups.append(current)
            current = []
        if not current:
            lo, hi = l, h
        current.append(i)
        lo, hi = min(lo, l), max(hi, h)
    if current:
        groups.append(current)
    return groups


def describe_shift(section, page, profile):
    '''Paper placement instructions for a section, or None if the paper stays put.'''
    if not section.shift_y:
        return None
    _, oy = paper_corner(page.width_mm, page.height_mm, profile)
    bottom = oy + section.shift_y
    # Measure from the edge the paper is normally laid against, so the slide can be checked
    # with a ruler at that edge of the bed.
    if profile["paper_anchor"].startswith("bottom"):
        edge, gap = "front", bottom
    else:
        edge, gap = "back", profile["bed"][1] - (bottom + page.height_mm)
    if abs(gap) < 1e-6:
        where = "flush with the {} of the bed".format(edge)
    else:
        where = "{:g} mm {} the {} of the bed".format(
            abs(gap), "in from" if gap > 0 else "past", edge)
    return ("slide the paper {:g} mm {} along Y without moving it sideways, so its {} edge is "
            "{}".format(abs(section.shift_y),
                        "toward Y0" if section.shift_y < 0 else "away from Y0", edge, where))


def check_bed(strokes, profile):
    bed = profile["bed"]
    if not bed:
        return
    xs = [p[0] for s in strokes for p in s]
    ys = [p[1] for s in strokes for p in s]
    if not xs:
        return
    box = (min(xs), min(ys), max(xs), max(ys))
    if box[0] < 0 or box[1] < 0 or box[2] > bed[0] or box[3] > bed[1]:
        offset = ""
        if profile["pen_offset_x"] or profile["pen_offset_y"]:
            offset = (" The pen is offset X{:g} Y{:g} from the nozzle, so it cannot reach as far "
                      "along the opposite sides of the bed.".format(
                          profile["pen_offset_x"], profile["pen_offset_y"]))
        raise BedLimitError(
            "the drawing would run off the bed: the nozzle would have to cover X "
            "{:.1f}..{:.1f}, Y {:.1f}..{:.1f} mm but the bed is {:g} x {:g} mm.{} Use --page "
            "fit to size the page to the bed, a smaller --page, a smaller --x-height-mm, or "
            "another --paper-anchor.".format(
                box[0], box[2], box[1], box[3], bed[0], bed[1], offset))


def _fmt(v):
    return "{:.3f}".format(v).rstrip("0").rstrip(".")


def write_gcode(page, path, profile, smooth=False, fit_error=1.0, simplify_tol=0.0,
                flatten_tol=0.05, header_comment=None, section=None):
    '''
    Write a laid-out Page as G-code. Returns a stats dict.

    `section`, from bed_sections(), writes just that band of the page instead of all of it.

    Raises BedLimitError rather than emitting moves that would drive into the bed's limits.
    '''
    from .svg_out import fit_beziers

    strokes = section.strokes if section is not None else to_machine(page, profile)

    prepared = []
    for stroke in strokes:
        if smooth:
            beziers = fit_beziers(stroke, fit_error)
            stroke = flatten(beziers, flatten_tol) or stroke
        if simplify_tol:
            stroke = simplify(stroke, simplify_tol)
        if len(stroke) >= 2:
            prepared.append(stroke)

    if not prepared:
        raise ValueError("nothing to write: no stroke survived preparation")

    check_bed(prepared, profile)

    up, down, safe = profile["pen_up_z"], profile["pen_down_z"], profile["safe_z"]
    f_draw, f_travel, f_z = profile["feed_draw"], profile["feed_travel"], profile["feed_z"]
    dwell = profile["pen_dwell_ms"]

    lines = []
    emit = lines.append

    def pen(z):
        emit("G1 Z{} F{}".format(_fmt(z), _fmt(f_z)))
        if dwell:
            emit("G4 P{}".format(_fmt(dwell)))

    draw_mm = 0.0
    travel_mm = 0.0

    emit("; handwriting-helper")
    if header_comment:
        for line in header_comment.splitlines():
            emit("; " + line)
    emit("; profile: {}".format(profile["name"]))
    if profile["bed"]:
        emit("; paper anchor: {}".format(profile["paper_anchor"]))
    emit("; page: {:g} x {:g} mm, x-height {:.2f} mm".format(
        page.width_mm, page.height_mm, page.x_height_mm))
    placement = describe_shift(section, page, profile) if section is not None else None
    if placement:
        emit("; paper: " + placement)
    # Read back by tools/gcode_preview.py to draw the paper and the bed. The paper spans
    # this corner to corner + (width, height) in machine coordinates, whichever way Y runs.
    ox, oy = paper_corner(page.width_mm, page.height_mm, profile)
    shift = section.shift_y if section is not None else 0.0
    emit("; paper corner: X{} Y{}".format(_fmt(ox), _fmt(oy + shift)))
    # The moves are the nozzle's; the preview adds this back to show where the ink lands.
    emit("; pen offset: X{} Y{}".format(
        _fmt(profile["pen_offset_x"]), _fmt(profile["pen_offset_y"])))
    if profile["bed"]:
        emit("; bed: {} x {} mm".format(_fmt(profile["bed"][0]), _fmt(profile["bed"][1])))
    emit("; strokes: {}, points: {}".format(
        len(prepared), sum(len(s) for s in prepared)))
    emit("; pen down Z{} / up Z{} -- check this against your pen before running".format(
        _fmt(down), _fmt(up)))
    for line in profile["preamble"]:
        emit(line)

    emit("G1 Z{} F{}".format(_fmt(safe), _fmt(f_z)))
    here = None
    for stroke in prepared:
        first = stroke[0]
        emit("G0 Z{}".format(_fmt(up)))
        emit("G0 X{} Y{} F{}".format(_fmt(first[0]), _fmt(first[1]), _fmt(f_travel)))
        if here is not None:
            travel_mm += math.dist(here, first)
        pen(down)
        emit("G1 F{}".format(_fmt(f_draw)))
        prev = first
        for x, y in stroke[1:]:
            emit("G1 X{} Y{}".format(_fmt(x), _fmt(y)))
            draw_mm += math.dist(prev, (x, y))
            prev = (x, y)
        pen(up)
        here = prev

    emit("G0 Z{}".format(_fmt(safe)))
    if profile["park"]:
        emit("G0 X{} Y{} F{}".format(
            _fmt(profile["park"][0]), _fmt(profile["park"][1]), _fmt(f_travel)))
    for line in profile["postamble"]:
        emit(line)

    minutes = draw_mm / f_draw + travel_mm / f_travel
    minutes += len(prepared) * 2 * abs(up - down) / f_z
    emit("; estimated runtime: {:.1f} min".format(minutes))

    target = Path(path)
    if target.parent != Path(""):
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {
        "strokes": len(prepared),
        "points": sum(len(s) for s in prepared),
        "draw_mm": draw_mm,
        "travel_mm": travel_mm,
        "minutes": minutes,
    }
