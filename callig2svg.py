#!/usr/bin/env python3
'''
Convert a hand-extracted calligrapher.ai point array into a single-stroke SVG.

This is the original entry point, kept working for JSON files captured the old way (open
calligrapher.ai, run JSON.stringify(tr) in the browser console, paste into a file). It now
runs on top of handwriting_helper, so it produces the same per-stroke, millimetre-measured
SVG as the rest of the tool.

For new work use handwrite.py instead, which generates the strokes itself and can write
G-code directly:

    ./handwrite.py "some text" -o output/out.gcode

This program is free software: you can redistribute it and/or modify it under the terms of
the GNU General Public License as published by the Free Software Foundation, either version
3 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with this program.
If not, see <https://www.gnu.org/licenses/>.
'''

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from handwriting_helper.layout import layout
from handwriting_helper.strokes import (
    Document, Line, json2points, moveToOrigin, segments_to_strokes,
)
from handwriting_helper.svg_out import write_svg


def main(args):
    raw = Path(args.json).read_text(encoding="utf-8").strip()
    if not raw:
        raise SystemExit("{} is empty".format(args.json))

    segments = json2points(raw)
    if not segments:
        raise SystemExit(
            "{} contained no strokes. It should be the array from JSON.stringify(tr); if it "
            "starts and ends with a quote, strip the quotes (and un-escape \\\" if your "
            "browser added them).".format(args.json))

    if not args.whitespace:
        segments = moveToOrigin(segments)

    # These points are in the site's canvas pixels, so the scale is whatever the browser
    # window happened to be. Keep 1 unit = 1 mm, matching the old unitless behaviour.
    document = Document(lines=[Line(text="", strokes=segments_to_strokes(segments))])
    page = layout(document, page="fit", margin_mm=0.0, mm_per_unit=1.0, warn=False)

    # These points are canvas pixels, so pick a pen width in proportion to the drawing
    # rather than a fixed millimetre value that would render as a hairline.
    pen = max(0.4, page.height_mm * 0.012)
    count = write_svg(page, args.outfile, smooth=args.smooth, fit_error=args.fit_error,
                      pen_width_mm=pen)
    print("wrote {} ({} paths)".format(args.outfile, count))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Convert points from the output of calligrapher.ai to single-stroke SVG "
                    "paths for use with plotters and engravers. Get the input JSON by running "
                    "JSON.stringify(tr) in your browser's developer console. See handwrite.py "
                    "for the automated version of this whole workflow.")
    parser.add_argument("json", help="path to a JSON file containing the array of points "
                                     "obtained from JSON.stringify(tr)")
    parser.add_argument("--smooth", "-s", action="store_true",
                        help="smooth out paths using cubic bezier approximations")
    parser.add_argument("--fit-error", type=float, default=1.0, metavar="F",
                        help="curve fitting tolerance for --smooth (default: %(default)s)")
    parser.add_argument("--whitespace", "-w", action="store_true",
                        help="keep any whitespace (offset from the top-left corner) present "
                             "in the original point array")
    parser.add_argument("--outfile", "-o", default="./output/output.svg",
                        help="path/filename to save output (default: %(default)s)")
    main(parser.parse_args())
