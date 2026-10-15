#!/usr/bin/env python3
'''
Render a G-code file back to SVG, so the toolpath can be checked before a machine runs it.

This is the round-trip check that catches the mistakes that are expensive to find on the bed:
an inverted Y axis, pen-up moves drawn as ink, strokes joined that should be separate. Travel
moves are drawn in red, drawing moves in black.

The paper and the bed are drawn as outlined rectangles behind the toolpath, so it is plain
where the writing lands on the sheet and whether the sheet sits where the file expects. Both
come from the header comments handwrite.py writes, as does the pen's offset from the nozzle,
which is added to every move so the toolpath shows where the pen goes rather than the nozzle; for an older file without them, --profile
supplies the bed and, with the page size in the header, the paper's usual place.

    tools/gcode_preview.py output/letter.gcode -o preview.svg
'''

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WORD = re.compile(r"([A-Za-z])\s*(-?\d*\.?\d+)")
NUM = r"(-?\d+(?:\.\d+)?)"
PAGE = re.compile(r"^;\s*page:\s*{0}\s*x\s*{0}\s*mm".format(NUM))
CORNER = re.compile(r"^;\s*paper corner:\s*X{0}\s+Y{0}".format(NUM))
BED = re.compile(r"^;\s*bed:\s*{0}\s*x\s*{0}\s*mm".format(NUM))
OFFSET = re.compile(r"^;\s*pen offset:\s*X{0}\s+Y{0}".format(NUM))

BED_FILL, BED_EDGE = "#e3ecf6", "#3b6ea5"
PAPER_FILL, PAPER_EDGE = "#fffdf2", "#c98a00"


def parse(path, pen_threshold=None, offset=(0.0, 0.0)):
    '''
    Returns (draw_strokes, travel_segments, z_values).

    The moves are the nozzle's; `offset`, the pen's position relative to it, is added so the
    result shows where the pen goes.

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
        moves.append((x + offset[0], y + offset[1], z))

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


def parse_frame(path):
    '''
    The paper, bed and pen offset recorded in the file's header: (page (w, h) or None, paper
    corner (x, y) or None, bed (w, h) or None, pen offset (x, y)).
    '''
    page = corner = bed = None
    offset = (0.0, 0.0)
    for raw in Path(path).read_text().splitlines():
        if not raw.startswith(";"):
            if raw.strip():
                break                       # the header is over at the first command
            continue
        for pattern, kind in ((PAGE, "page"), (CORNER, "corner"), (BED, "bed"),
                              (OFFSET, "offset")):
            m = pattern.match(raw)
            if m:
                value = (float(m.group(1)), float(m.group(2)))
                if kind == "page":
                    page = value
                elif kind == "corner":
                    corner = value
                elif kind == "bed":
                    bed = value
                else:
                    offset = value
    return page, corner, bed, offset


def _rect(x0, y0, x1, y1, tx, **attrs):
    (ax, ay), (bx, by) = tx((x0, y1)), tx((x1, y0))
    extra = " ".join('{}="{}"'.format(k.replace("_", "-"), v) for k, v in attrs.items())
    return '  <rect x="{:.3f}" y="{:.3f}" width="{:.3f}" height="{:.3f}" {}/>'.format(
        ax, ay, bx - ax, by - ay, extra)


def _label(x, y, text, color, tx, anchor="start"):
    sx, sy = tx((x, y))
    return ('  <text x="{:.3f}" y="{:.3f}" font-family="sans-serif" font-size="3.5" '
            'fill="{}" text-anchor="{}">{}</text>'.format(sx, sy, color, anchor, text))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("gcode")
    ap.add_argument("--outfile", "-o", default=None, help="default: alongside the input")
    ap.add_argument("--pen-z", type=float, default=None,
                    help="Z at or below which the pen is considered down")
    ap.add_argument("--no-travel", action="store_true", help="hide the travel moves")
    ap.add_argument("--no-frame", action="store_true",
                    help="hide the paper and bed outlines, and frame just the drawing")
    ap.add_argument("--profile", metavar="NAME",
                    help="take the bed, and the paper's usual place, from this profile when "
                         "the file's header does not record them")
    args = ap.parse_args(argv)

    page, corner, bed, offset = parse_frame(args.gcode)
    draw, travel, zs = parse(args.gcode, args.pen_z, offset)
    if args.no_frame:
        page = corner = bed = None
    if args.profile and not args.no_frame:
        from handwriting_helper.gcode_out import ProfileError, load_profile, paper_corner
        try:
            profile = load_profile(args.profile)
        except ProfileError as exc:
            raise SystemExit("profile error: {}".format(exc))
        bed = bed or (tuple(profile["bed"]) if profile["bed"] else None)
        if not corner and page:
            corner = paper_corner(page[0], page[1], profile)
    paper = None
    if page and corner:
        paper = (corner[0], corner[1], corner[0] + page[0], corner[1] + page[1])

    # Frame the drawing, paper and bed, not the travel: a park move to the far corner of a
    # machine would otherwise shrink the actual writing to a speck. Travel outside the frame
    # is clipped.
    pts = [p for s in draw for p in s] or [p for seg in travel for p in seg]
    if paper:
        pts += [paper[:2], paper[2:]]
    if bed:
        pts += [(0.0, 0.0), bed]
    x0 = min(p[0] for p in pts)
    y0 = min(p[1] for p in pts)
    x1 = max(p[0] for p in pts)
    y1 = max(p[1] for p in pts)
    pad = 5.0
    top = 6.0 if (paper or bed) else 0.0          # room for the labels above the frame
    w, h = (x1 - x0) + 2 * pad, (y1 - y0) + 2 * pad + top

    # Machine Y grows upward, SVG Y grows downward.
    def tx(p):
        return p[0] - x0 + pad, (y1 - p[1]) + pad + top

    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<svg xmlns="http://www.w3.org/2000/svg" width="{:.2f}mm" height="{:.2f}mm" '
           'viewBox="0 0 {:.2f} {:.2f}">'.format(w, h, w, h),
           '  <rect width="100%" height="100%" fill="white"/>']
    # Bed first, then the paper over it, translucent so the part of the sheet hanging off
    # the bed stays visibly different from the part on it, then the bed's edge again so it
    # stays crisp through the paper.
    if bed:
        out.append(_rect(0.0, 0.0, bed[0], bed[1], tx, fill=BED_FILL, stroke="none"))
        out.append(_label(0.0, y1 + 1.5, "bed {:g} x {:g} mm".format(*bed), BED_EDGE, tx))
    if paper:
        out.append(_rect(*paper, tx, fill=PAPER_FILL, fill_opacity="0.7", stroke=PAPER_EDGE,
                         stroke_width="0.5", stroke_dasharray="3 1.5"))
        caption = "paper {:g} x {:g} mm, corner at X{:g} Y{:g}".format(
            page[0], page[1], corner[0], corner[1])
        out.append(_label(paper[2], y1 + 1.5, caption, PAPER_EDGE, tx, anchor="end"))
    if bed:
        out.append(_rect(0.0, 0.0, bed[0], bed[1], tx, fill="none", stroke=BED_EDGE,
                         stroke_width="0.5"))
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
    if not args.no_frame and not (paper or bed):
        print("note: no paper or bed recorded in the file; pass --profile to draw them",
              file=sys.stderr)
    print("{}: {} strokes, {} travel moves, Z values {}".format(
        target, len(draw), len(travel), ", ".join("{:g}".format(z) for z in zs)),
        file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
