#!/usr/bin/env python3
'''
Check the numpy port of the model against the original page running in headless Chromium.

Draws the same texts with the same seeds on both backends, across several styles and biases,
and reports where (if anywhere) the strokes part ways. Also writes one SVG per style with
each browser draw above the matching numpy draw, for comparing by eye.

Needs Playwright and its Chromium, for the browser side.

    tools/compare_backends.py
    tools/compare_backends.py --styles 0 7 23 --out output/compare
'''

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from handwriting_helper import styles                                    # noqa: E402
from handwriting_helper.generate import Generator                       # noqa: E402

DEFAULT_STYLES = [0, 7, 15, 23, 38, 51, 64, 79]

# (text, seed, bias): ordinary prose, a long line, digits and punctuation, and both ends of
# the bias range.
CASES = [
    ("The quick brown fox jumps over the lazy dog.", 101, 0.75),
    ("Dear Sam, thanks for the letter -- it arrived on 14/10, right on time!", 202, 0.75),
    ("Loose and scrawly at a low bias", 303, 0.3),
    ("Neat and tidy at a high bias (2.0)", 404, 2.0),
    ("It was the best of times, it was the worst of times, it was the age of wisdom, "
     "it was the age of foolishness", 505, 0.75),
]

# Coordinates further apart than this, in model units, count as a divergence. A letter is
# roughly 10-20 units tall. Below it is the last-bit difference between V8's and libm's
# exp/log, which can nudge a point by ~1e-6 late in a long draw without changing its shape.
TOLERANCE = 1e-3

ROW_GAP = 30.0
PAD = 20.0


def compare(browser, numpy_):
    '''
    ("identical" | "close" | "diverged", first divergent index or None, largest coordinate
    gap before it). Pen-up flags, attention, length and stop reason must match exactly.
    '''
    first, gap = None, 0.0
    for i, (b, n) in enumerate(zip(browser.points, numpy_.points)):
        d = max(abs(b[0] - n[0]), abs(b[1] - n[1]))
        if d > TOLERANCE or b[2] != n[2] or b[3] != n[3]:
            first = i
            break
        gap = max(gap, d)
    if first is None and len(browser.points) != len(numpy_.points):
        first = min(len(browser.points), len(numpy_.points))
    if first is not None or browser.reason != numpy_.reason:
        return "diverged", first, gap
    return ("identical" if gap == 0 else "close"), None, gap


def _path(strokes, dx, dy):
    parts = []
    for stroke in strokes:
        parts.append("M" + " L".join("{:.2f},{:.2f}".format(x + dx, y + dy) for x, y in stroke))
    return '<path d="{}" fill="none" stroke="black" stroke-width="0.8" ' \
           'stroke-linecap="round" stroke-linejoin="round"/>'.format(" ".join(parts))


def write_svg(path, style, rows):
    '''rows: [(label, Draw), ...], stacked top to bottom.'''
    body, y, width = [], PAD, 0.0
    for label, draw in rows:
        pts = draw.points or [(0, 0, 1, 0)]
        top = min(p[1] for p in pts)
        bottom = max(p[1] for p in pts)
        left = min(p[0] for p in pts)
        right = max(p[0] for p in pts)
        body.append('<text x="{:.1f}" y="{:.1f}" font-family="sans-serif" font-size="7" '
                    'fill="#888">{}</text>'.format(PAD, y + 6, label))
        y += 12
        body.append(_path(draw.strokes(), PAD + 90 - left, y - top))
        y += (bottom - top) + ROW_GAP
        width = max(width, right - left + 90)
    w, h = width + 2 * PAD, y
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="{0:.0f}" height="{1:.0f}" '
        'viewBox="0 0 {0:.1f} {1:.1f}">\n<rect width="100%" height="100%" fill="white"/>\n'
        '<text x="{2}" y="{2}" font-family="sans-serif" font-size="9">style {3}: browser '
        'above, numpy below</text>\n{4}\n</svg>\n'.format(w, h, PAD / 2, style,
                                                          "\n".join(body)),
        encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--styles", type=int, nargs="+", default=DEFAULT_STYLES)
    ap.add_argument("--out", default=str(ROOT / "output" / "compare"),
                    help="directory for the side-by-side SVGs (default: %(default)s)")
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    timings = {"browser": 0.0, "numpy": 0.0}
    steps = 0
    counts = {"identical": 0, "close": 0, "diverged": 0}
    with Generator(style=0, seed=0, backend="browser") as web, \
            Generator(style=0, seed=0, backend="numpy") as num:
        for style in args.styles:
            rows = []
            for gen in (web, num):
                gen.style = style
                gen.x_height_units, gen.char_advance_units = styles.lookup(style)
            for text, seed, bias in CASES:
                draws = {}
                for name, gen in (("browser", web), ("numpy", num)):
                    gen.bias = bias
                    start = time.perf_counter()
                    draws[name] = gen.draw(text, seed=seed, retry=False)
                    timings[name] += time.perf_counter() - start
                b, n = draws["browser"], draws["numpy"]
                steps += len(b.points)
                outcome, first, gap = compare(b, n)
                counts[outcome] += 1
                if outcome == "identical":
                    verdict = "identical ({} points)".format(len(b.points))
                elif outcome == "close":
                    verdict = "close ({} points, largest gap {:.1e} units)".format(
                        len(b.points), gap)
                else:
                    verdict = "DIVERGE at point {} of {}/{} ({} vs {})".format(
                        first, len(b.points), len(n.points), b.reason, n.reason)
                print("style {:>2}  bias {:<4}  seed {:>3}  {:<28}  {}".format(
                    style, bias, seed, repr(text[:26]), verdict))
                label = "seed {}, bias {}".format(seed, bias)
                rows += [("browser  " + label, b), ("numpy    " + label, n)]
            target = out / "style-{:02d}.svg".format(style)
            write_svg(target, style, rows)

    print("\n{identical} identical, {close} close, {diverged} diverged; SVGs in {0}".format(
        out, **counts))
    print("time per sampling step: browser {:.2f} ms, numpy {:.2f} ms".format(
        1000 * timings["browser"] / max(1, steps), 1000 * timings["numpy"] / max(1, steps)))
    return 1 if counts["diverged"] else 0


if __name__ == "__main__":
    sys.exit(main())
