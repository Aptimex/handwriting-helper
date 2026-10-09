#!/usr/bin/env python3
'''
Render a G-code file back to SVG, so the toolpath can be checked before a machine runs it.

This is the round-trip check that catches the mistakes that are expensive to find on the bed:
an inverted Y axis, pen-up moves drawn as ink, strokes joined that should be separate. Travel
moves are drawn in red, drawing moves in black.

    tools/gcode_preview.py output/letter.gcode -o preview.svg
'''

import argparse
import re
import sys
from pathlib import Path

WORD = re.compile(r"([A-Za-z])\s*(-?\d*\.?\d+)")


def parse(path, pen_threshold=None):
    '''
    Returns (draw_strokes, travel_segments, z_values).

    A move counts as drawing when Z is at or below the threshold, which defaults to halfway
    between the two lowest Z values in the file. Note this is deliberately not the midpoint of
    the whole Z range: a safe_z far above the pen-up height would drag the midpoint up and
    make travel moves look like ink.
    '''
    moves = []          # (x, y, z)
    x = y = z = 0.0
    zs = set()
    for raw in Path(path).read_text().splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue
        words = dict((w.upper(), float(v)) for w, v in WORD.findall(line))
        code = line.split()[0].upper()
        if code not in ("G0", "G1", "G00", "G01"):
            continue
        x = words.get("X", x)
        y = words.get("Y", y)
        z = words.get("Z", z)
        zs.add(z)
        moves.append((x, y, z))

    if not moves:
        raise SystemExit("no G0/G1 moves found in {}".format(path))
    if pen_threshold is None:
        levels = sorted(zs)
        pen_threshold = ((levels[0] + levels[1]) / 2.0 if len(levels) > 1
                         else levels[0] + 0.001)

    draw, travel = [], []
    current = []
    prev = None
    for point in moves:
        down = point[2] <= pen_threshold
        if down and prev is not None:
            if not current:
                current = [prev[:2]]
            current.append(point[:2])
        else:
            if len(current) >= 2:
                draw.append(current)
            current = []
            if prev is not None:
                travel.append((prev[:2], point[:2]))
        prev = point
    if len(current) >= 2:
        draw.append(current)
    return draw, travel, sorted(zs)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("gcode")
    ap.add_argument("--outfile", "-o", default=None, help="default: alongside the input")
    ap.add_argument("--pen-z", type=float, default=None,
                    help="Z at or below which the pen is considered down")
    ap.add_argument("--no-travel", action="store_true", help="hide the travel moves")
    args = ap.parse_args(argv)

    draw, travel, zs = parse(args.gcode, args.pen_z)
    # Frame the drawing, not the travel: a park move to the far corner of the bed would
    # otherwise shrink the actual writing to a speck. Travel outside the frame is clipped.
    pts = [p for s in draw for p in s] or [p for seg in travel for p in seg]
    x0 = min(p[0] for p in pts)
    y0 = min(p[1] for p in pts)
    x1 = max(p[0] for p in pts)
    y1 = max(p[1] for p in pts)
    pad = 5.0
    w, h = (x1 - x0) + 2 * pad, (y1 - y0) + 2 * pad

    # Machine Y grows upward, SVG Y grows downward.
    def tx(p):
        return p[0] - x0 + pad, (y1 - p[1]) + pad

    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<svg xmlns="http://www.w3.org/2000/svg" width="{:.2f}mm" height="{:.2f}mm" '
           'viewBox="0 0 {:.2f} {:.2f}">'.format(w, h, w, h),
           '  <rect width="100%" height="100%" fill="white"/>']
    if travel and not args.no_travel:
        out.append('  <g stroke="#e04040" stroke-width="0.1" fill="none" opacity="0.6">')
        for a, b in travel:
            ax, ay = tx(a)
            bx, by = tx(b)
            out.append('    <path d="M{:.3f} {:.3f}L{:.3f} {:.3f}"/>'.format(ax, ay, bx, by))
        out.append("  </g>")
    out.append('  <g stroke="black" stroke-width="0.4" fill="none" '
               'stroke-linecap="round" stroke-linejoin="round">')
    for stroke in draw:
        d = "M{:.3f} {:.3f}".format(*tx(stroke[0]))
        d += "".join("L{:.3f} {:.3f}".format(*tx(p)) for p in stroke[1:])
        out.append('    <path d="{}"/>'.format(d))
    out.append("  </g>")
    out.append("</svg>")

    target = Path(args.outfile) if args.outfile else Path(args.gcode).with_suffix(".preview.svg")
    target.write_text("\n".join(out) + "\n", encoding="utf-8")
    print("{}: {} strokes, {} travel moves, Z values {}".format(
        target, len(draw), len(travel), ", ".join("{:g}".format(z) for z in zs)),
        file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
