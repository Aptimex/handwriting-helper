#!/usr/bin/env python3
'''
Render a G-code file back to SVG, so the toolpath can be checked before a machine runs it.

handwrite.py already writes NAME-preview.svg next to every G-code file it writes; this renders
one on its own, for a G-code file that was edited by hand, or with different options. See
handwriting_helper/preview.py for what is drawn. For a file whose header does not record the
bed and the paper's place, --profile supplies them.

    tools/gcode_preview.py output/letter.gcode -o preview.svg
'''

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from handwriting_helper.preview import write_preview  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("gcode")
    ap.add_argument("--outfile", "-o", default=None,
                    help="default: alongside the input, as NAME-preview.svg")
    ap.add_argument("--pen-z", type=float, default=None,
                    help="Z at or below which the pen is considered down")
    ap.add_argument("--no-travel", action="store_true", help="hide the travel moves")
    ap.add_argument("--no-frame", action="store_true",
                    help="hide the paper and bed outlines, and frame just the drawing")
    ap.add_argument("--profile", metavar="NAME",
                    help="take the bed, and the paper's usual place, from this profile when "
                         "the file's header does not record them")
    args = ap.parse_args(argv)

    profile = None
    if args.profile and not args.no_frame:
        from handwriting_helper.gcode_out import ProfileError, load_profile
        try:
            profile = load_profile(args.profile)
        except ProfileError as exc:
            raise SystemExit("profile error: {}".format(exc))

    source = Path(args.gcode)
    target = (Path(args.outfile) if args.outfile
              else source.with_name(source.stem + "-preview.svg"))
    try:
        stats = write_preview(source, target, pen_threshold=args.pen_z,
                              travel=not args.no_travel, frame=not args.no_frame,
                              profile=profile)
    except ValueError as exc:
        raise SystemExit(str(exc))
    if not args.no_frame and not stats["framed"]:
        print("note: no paper or bed recorded in the file; pass --profile to draw them",
              file=sys.stderr)
    print("{}: {} strokes, {} travel moves, Z values {}".format(
        target, stats["strokes"], stats["travel"],
        ", ".join("{:g}".format(z) for z in stats["z_values"])), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
