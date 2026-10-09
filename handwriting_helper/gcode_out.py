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
    "origin": [0.0, 0.0],    # where the page's bottom-left corner sits on the bed
    "bed": None,             # [x, y] machine limits; None disables the check
    "flip_y": True,          # page y grows downward, machine y grows away from the origin
    "pen_dwell_ms": 0,       # pause after each pen up/down, for a pen that needs to settle
    "preamble": ["G21", "G90"],
    "postamble": [],
    "park": None,            # [x, y] to move to at the end
}

_NUMERIC = ("pen_down_z", "pen_up_z", "safe_z", "feed_draw", "feed_travel", "feed_z",
            "pen_dwell_ms")


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
    for key in ("origin", "bed", "park"):
        if profile[key] is not None:
            values = list(profile[key])
            if len(values) != 2:
                raise ProfileError("{}: {} must be two numbers".format(path, key))
            profile[key] = [float(v) for v in values]
    if profile["pen_up_z"] <= profile["pen_down_z"]:
        raise ProfileError("{}: pen_up_z must be above pen_down_z".format(path))
    return profile


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
    origin at the front-left with y growing away from it. With flip_y the page is placed so
    its bottom-left corner lands on `origin`, which is what you get by laying the paper on the
    bed the right way up.
    '''
    ox, oy = profile["origin"]
    if profile["flip_y"]:
        return [[(ox + x, oy + (page.height_mm - y)) for x, y in stroke]
                for stroke in page.strokes]
    return [[(ox + x, oy + y) for x, y in stroke] for stroke in page.strokes]


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
        raise BedLimitError(
            "the drawing would run off the bed: it occupies X {:.1f}..{:.1f}, "
            "Y {:.1f}..{:.1f} mm but the bed is {:g} x {:g} mm. Use a smaller --page (or "
            "--page fit to crop to the ink), reduce --x-height-mm, or move the profile's "
            "origin.".format(
                box[0], box[2], box[1], box[3], bed[0], bed[1]))


def _fmt(v):
    return "{:.3f}".format(v).rstrip("0").rstrip(".")


def write_gcode(page, path, profile, smooth=False, fit_error=1.0, simplify_tol=0.0,
                flatten_tol=0.05, header_comment=None):
    '''
    Write a laid-out Page as G-code. Returns a stats dict.

    Raises BedLimitError rather than emitting moves that would drive into the bed's limits.
    '''
    from .svg_out import fit_beziers

    strokes = to_machine(page, profile)

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
    emit("; page: {:g} x {:g} mm, x-height {:.2f} mm".format(
        page.width_mm, page.height_mm, page.x_height_mm))
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
