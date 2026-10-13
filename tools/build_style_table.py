#!/usr/bin/env python3
'''
Measure every handwriting style's x-height and character advance, and write
handwriting_helper/styles.json.

Only needs re-running if the model in website/ changes. Takes a few minutes.

    tools/build_style_table.py
'''

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from handwriting_helper import styles                                    # noqa: E402
from handwriting_helper.generate import (                               # noqa: E402
    ADVANCE_TEXT, MEASURE_SAMPLES, Generator)
from handwriting_helper.strokes import CALIBRATION_TEXT                 # noqa: E402

BIAS = 0.75


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--samples", type=int, default=MEASURE_SAMPLES,
                    help="draws medianed per measurement (default: %(default)s)")
    ap.add_argument("-o", "--outfile", default=str(styles.TABLE))
    args = ap.parse_args(argv)

    table = {}
    with Generator(style=0, bias=BIAS, seed=0) as gen:
        for style in range(gen.n_styles):
            gen.style = style
            x_height, advance = gen.measure(samples=args.samples)
            table[str(style)] = {"x_height": round(x_height, 3), "advance": round(advance, 3)}
            print("style {:>2}: x-height {:6.2f}  advance {:5.2f}".format(
                style, x_height, advance), file=sys.stderr, flush=True)

    out = {
        "about": "Per-style size in model units, written by tools/build_style_table.py. "
                 "x_height is measured on {!r}, advance on {!r}; each is the median of {} "
                 "draws at bias {}.".format(CALIBRATION_TEXT, ADVANCE_TEXT, args.samples,
                                            BIAS),
        "styles": table,
    }
    Path(args.outfile).write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    print("wrote {}".format(args.outfile), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
