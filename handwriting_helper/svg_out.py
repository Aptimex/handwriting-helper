'''
SVG output.

One <path> per stroke, so a pen lift is explicit in the file rather than implied by a
discontinuity, and real dimensions -- width/height in mm plus a matching viewBox -- so
downstream tools don't have to be told what scale the drawing is.
'''

import sys
from pathlib import Path

from .strokes import simplify

# The fitCurves submodule has no package layout and does a bare `import bezier`, so its own
# directory has to be on sys.path. Resolved from this file rather than the working directory.
_FITCURVES_DIR = Path(__file__).resolve().parent.parent / "fitCurves"


def _fit_curve():
    if not (_FITCURVES_DIR / "fitCurves.py").is_file():
        raise RuntimeError(
            "--smooth needs the fitCurves submodule:\n"
            "    git submodule update --init")
    if str(_FITCURVES_DIR) not in sys.path:
        sys.path.append(str(_FITCURVES_DIR))
    from fitCurves import fitCurve
    return fitCurve


def fit_beziers(stroke, max_error):
    '''
    Fit cubic beziers to a stroke with Schneider's algorithm.

    Returns [(p0, c1, c2, p3), ...]. Straight line output is fine for the plotter -- G-code
    is linear anyway -- but smoothing measurably improves the look at larger sizes.
    '''
    import numpy as np

    if len(stroke) < 2:
        return []
    fitCurve = _fit_curve()
    points = np.array(stroke, dtype=float)
    return [tuple(tuple(float(v) for v in p) for p in bez)
            for bez in fitCurve(points, max_error)]


def _num(v):
    '''Trim trailing zeros; SVG files of a few thousand points get large otherwise.'''
    return "{:.4f}".format(v).rstrip("0").rstrip(".")


def stroke_to_d(stroke, smooth=False, fit_error=1.0):
    if len(stroke) < 2:
        return None
    if not smooth:
        parts = ["M{} {}".format(_num(stroke[0][0]), _num(stroke[0][1]))]
        parts += ["L{} {}".format(_num(x), _num(y)) for x, y in stroke[1:]]
        return "".join(parts)

    beziers = fit_beziers(stroke, fit_error)
    if not beziers:
        return None
    start = beziers[0][0]
    parts = ["M{} {}".format(_num(start[0]), _num(start[1]))]
    for _, c1, c2, end in beziers:
        parts.append("C{} {} {} {} {} {}".format(
            _num(c1[0]), _num(c1[1]), _num(c2[0]), _num(c2[1]),
            _num(end[0]), _num(end[1])))
    return "".join(parts)


def write_svg(page, path, smooth=False, fit_error=1.0, pen_width_mm=0.4,
              simplify_tol=0.0, background=False):
    '''Write a laid-out Page as an SVG measured in millimetres.'''
    paths = []
    for stroke in page.strokes:
        s = simplify(stroke, simplify_tol) if simplify_tol else stroke
        d = stroke_to_d(s, smooth=smooth, fit_error=fit_error)
        if d:
            paths.append(d)

    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<svg xmlns="http://www.w3.org/2000/svg" version="1.1"',
        '     width="{}mm" height="{}mm" viewBox="0 0 {} {}">'.format(
            _num(page.width_mm), _num(page.height_mm),
            _num(page.width_mm), _num(page.height_mm)),
    ]
    if background:
        out.append('  <rect width="100%" height="100%" fill="white"/>')
    out.append('  <g fill="none" stroke="black" stroke-width="{}" '
               'stroke-linecap="round" stroke-linejoin="round">'.format(_num(pen_width_mm)))
    out += ['    <path d="{}"/>'.format(d) for d in paths]
    out.append("  </g>")
    out.append("</svg>")

    target = Path(path)
    if target.parent != Path(""):
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(out) + "\n", encoding="utf-8")
    return len(paths)
