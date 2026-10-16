#!/usr/bin/env python3
'''
Wrap a G-code file in a .gcode.3mf, so it can be sent to a Bambu Lab printer.

handwrite.py --bambu already writes NAME.gcode.3mf next to every G-code file it writes; this
wraps one on its own, for a G-code file that was edited by hand. See handwriting_helper/bambu.py
for how, and for the template it starts from.

    tools/bambu_wrap.py output/letter.gcode
'''

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from handwriting_helper.bambu import TEMPLATE, TemplateError, wrap_gcode  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("gcode")
    ap.add_argument("--outfile", "-o", default=None,
                    help="default: alongside the input, as NAME.gcode.3mf")
    ap.add_argument("--template", metavar="PATH", default=None,
                    help="a .gcode.3mf sliced by Bambu Studio for the printer "
                         "(default: {})".format(TEMPLATE.name))
    args = ap.parse_args(argv)

    source = Path(args.gcode)
    target = (Path(args.outfile) if args.outfile
              else source.with_name(source.stem + ".gcode.3mf"))
    try:
        wrap_gcode(source, target, args.template)
    except (TemplateError, OSError) as exc:
        raise SystemExit(str(exc))
    print("wrote {}".format(target), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
