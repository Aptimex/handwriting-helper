#!/usr/bin/env python3
'''
Self-test for the pipeline.

By default it runs only the parts that need no browser, using the sample files in input/.
Pass --online to also exercise generation, which downloads nothing but does start headless
Chromium and takes a few seconds.

    tools/selftest.py
    tools/selftest.py --online
'''

import argparse
import math
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from handwriting_helper.gcode_out import (
    BedLimitError, ProfileError, flatten, load_profile, to_machine, write_gcode)
from handwriting_helper.layout import (
    DEFAULT_LINE_PITCH_RATIO, LayoutError, layout, parse_page)
from handwriting_helper.strokes import (
    Document, MAX_DRAW_CHARS, estimate_x_height, json2points, reflow, simplify,
    split_chunks, wrap_text)
from handwriting_helper.svg_out import fit_beziers, write_svg

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print("  ok    {}".format(name))
    else:
        print("  FAIL  {} {}".format(name, detail))
        FAILURES.append(name)


def section(title):
    print("\n{}".format(title))


def test_wrapping():
    section("text wrapping")
    lines = wrap_text("a " * 60)
    check("every line is within the requested width",
          all(len(l) <= MAX_DRAW_CHARS for l in lines),
          max((len(l) for l in lines), default=0))
    check("blank lines are preserved", wrap_text("one\n\ntwo") == ["one", "", "two"])
    check("an over-long word is hard split",
          all(len(l) <= 10 for l in wrap_text("x" * 35, width=10)))
    check("trailing blank lines are dropped", wrap_text("hi\n\n\n") == ["hi"])
    check("a line may exceed the model's draw limit",
          len(wrap_text("word " * 40, width=90)[0]) > MAX_DRAW_CHARS)


def test_chunking():
    section("chunking and reflow")
    text = "the quick brown fox jumps over the lazy dog and then keeps on running"
    chunks = split_chunks(text)
    check("every chunk fits the model's input limit",
          all(len(c) <= MAX_DRAW_CHARS for c in chunks),
          [len(c) for c in chunks])
    check("chunks rejoin to the original text", " ".join(chunks) == text)
    check("a short line is a single chunk", split_chunks("short") == ["short"])
    check("an over-long word is hard split",
          all(len(c) <= MAX_DRAW_CHARS for c in split_chunks("x" * 130)))

    flowed = reflow("one\ntwo\n\nthree\nfour\n")
    check("a paragraph is joined onto one line", flowed == "one two\n\nthree four", repr(flowed))
    check("a paragraph break survives reflow",
          wrap_text(flowed, width=40) == ["one two", "", "three four"])
    check("runs of blank lines collapse", reflow("a\n\n\n\nb") == "a\n\nb")


def test_simplify():
    section("simplification")
    stroke = [(0, 0), (1, 0.01), (2, -0.01), (3, 0), (3, 5)]
    out = simplify(stroke, 0.1)
    check("collinear points are dropped", len(out) == 3, out)
    check("endpoints are kept", out[0] == stroke[0] and out[-1] == stroke[-1])
    check("a tolerance of 0 is a no-op", simplify(stroke, 0) == stroke)


def test_parse():
    section("legacy point-array parsing")
    sample = ROOT / "input" / "sample-points.json"
    if not sample.is_file():
        check("input/sample-points.json exists", False)
        return
    segments = json2points(sample.read_text())
    check("the sample parses into strokes", len(segments) > 5, len(segments))
    check("no single-point strokes survive", all(len(s) >= 2 for s in segments))
    check("array form parses the same as the object form",
          json2points([[0, 0, 0], [1, 1, 1]]) == json2points('[{"0":0,"1":0,"2":0},'
                                                             '{"0":1,"1":1,"2":1}]'))


def _sample_document():
    path = ROOT / "input" / "sample.strokes.json"
    if not path.is_file():
        return None
    return Document.from_json(path.read_text())


def test_layout():
    section("layout")
    document = _sample_document()
    if document is None:
        check("input/sample.strokes.json exists", False)
        return

    page = layout(document, page="a4", margin_mm=20, x_height_mm=3.0, warn=False)
    check("the page is A4", (page.width_mm, page.height_mm) == (210.0, 297.0))
    check("x-height is honoured", abs(page.x_height_mm - 3.0) < 1e-9)
    check("the default line pitch follows the x-height",
          abs(page.line_pitch_mm - 3.0 * DEFAULT_LINE_PITCH_RATIO) < 1e-6,
          page.line_pitch_mm)

    box = page.bbox()
    check("ink stays inside the page", 0 < box[0] and box[2] < 210 and box[3] < 297, box)

    doubled = layout(document, page="a4", x_height_mm=6.0, warn=False)
    check("doubling the x-height doubles the ink",
          abs(_width(doubled) / _width(page) - 2.0) < 0.01)

    fitted = layout(document, page="fit", margin_mm=5, warn=False)
    fbox = fitted.bbox()
    check("'fit' crops to the ink plus the margin",
          abs(fbox[0] - 5) < 0.6 and abs(fitted.width_mm - fbox[2] - 5) < 0.6, fbox)

    check("a4 and letter are known", parse_page("letter") == (215.9, 279.4))
    check("WxH parses", parse_page("120x80") == (120.0, 80.0))
    _raises("an unknown page size is rejected", LayoutError, parse_page, "a9")
    _raises("a bad alignment is rejected", LayoutError,
            layout, document, align="middle", warn=False)


def _width(page):
    box = page.bbox()
    return box[2] - box[0]


def test_curves():
    section("curve fitting and flattening")
    stroke = [(math.cos(i / 8.0) * 10, math.sin(i / 8.0) * 10) for i in range(40)]
    beziers = fit_beziers(stroke, 0.5)
    check("a curve is fitted", len(beziers) >= 1, len(beziers))
    points = flatten(beziers, 0.05)
    check("flattening returns a polyline", len(points) > len(beziers))
    worst = max(min(math.dist(p, q) for q in points) for p in stroke)
    check("the flattened path tracks the original", worst < 1.0, "max gap {:.3f}".format(worst))


def test_profiles():
    section("profiles")
    default = load_profile(None)
    check("defaults load", default["pen_up_z"] > default["pen_down_z"])
    ender = load_profile("ender3")
    check("a profile is found by name", ender["name"] == "ender3")
    check("the bed is read", ender["bed"] == [220.0, 220.0], ender["bed"])
    _raises("an unknown profile is rejected", ProfileError, load_profile, "no-such-printer")

    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / "bad.toml"
        bad.write_text('pen_down_z = 5.0\npen_up_z = 1.0\n')
        _raises("an inverted pen height is rejected", ProfileError, load_profile, str(bad))
        typo = Path(tmp) / "typo.toml"
        typo.write_text('pen_downz = 5.0\n')
        _raises("an unknown setting is rejected", ProfileError, load_profile, str(typo))


def test_output():
    section("output")
    document = _sample_document()
    if document is None:
        return
    page = layout(document, page="fit", margin_mm=5, warn=False)
    profile = load_profile("ender3")

    with tempfile.TemporaryDirectory() as tmp:
        svg = Path(tmp) / "out.svg"
        count = write_svg(page, svg, pen_width_mm=0.4)
        body = svg.read_text()
        check("one path per stroke", count == len(page.strokes), (count, len(page.strokes)))
        check("the SVG is measured in mm", 'width="{}mm"'.format(
            "{:.4f}".format(page.width_mm).rstrip("0").rstrip(".")) in body)
        check("the viewBox matches the dimensions",
              'viewBox="0 0 ' in body and body.count("<path") == count)

        gcode = Path(tmp) / "out.gcode"
        stats = write_gcode(page, gcode, profile)
        text = gcode.read_text()
        check("every stroke is one pen-down move",
              text.count("G1 Z{:g} F".format(profile["pen_down_z"])) == stats["strokes"])
        check("the pen is lifted as often as it is lowered",
              text.count("G1 Z{:g} F".format(profile["pen_up_z"])) == stats["strokes"])
        check("no heater or extruder commands", not any(
            c in text for c in ("M104", "M109", "M140", "M190", "G1 E", "M83")))
        check("a runtime estimate is written", "estimated runtime" in text)

        machine = to_machine(page, profile)
        page_top = min(p[1] for s in page.strokes for p in s)
        machine_of_top = max(p[1] for s in machine for p in s)
        check("the Y axis is flipped for the machine",
              abs(machine_of_top - (profile["origin"][1] + page.height_mm - page_top)) < 1e-6)

        smoothed = Path(tmp) / "smooth.gcode"
        small = write_gcode(page, smoothed, profile, smooth=True, simplify_tol=0.05)
        check("smoothing plus simplification cuts the point count",
              small["points"] < stats["points"] * 0.75,
              "{} -> {}".format(stats["points"], small["points"]))

        big = layout(document, page="a4", margin_mm=20, x_height_mm=3.0, warn=False)
        _raises("a page that overruns the bed is refused", BedLimitError,
                write_gcode, big, Path(tmp) / "big.gcode", profile)


def test_online(args):
    section("generation (headless browser)")
    from handwriting_helper.generate import generate

    first = generate(["repeatable"], style=7, seed=4242, calibrate=False)
    second = generate(["repeatable"], style=7, seed=4242, calibrate=False)
    check("a seed makes generation reproducible",
          first.lines[0].strokes == second.lines[0].strokes)

    # The site scales glyphs by min(105/n, 11), so a short line is drawn much larger than a
    # long one. Undoing that is the whole point of the normalisation in generate.py, and this
    # is the check that it works. Both lines are x-height letters only so the measurements
    # are comparable; a mix of ascenders and descenders would swamp the signal. Each is
    # measured over several seeds, because a single sample of this model carries a spread of
    # its own that is larger than the effect being tested.
    import statistics

    short, long = "noun once", "noun once moxe suna comun ecoun samu nexo con"
    samples = {short: [], long: []}
    calibrated = None
    for seed in (11, 12, 13):
        doc = generate([short, long], style=7, seed=seed, calibrate=(seed == 11))
        calibrated = calibrated or doc
        for line in doc.lines:
            samples[line.text].append(estimate_x_height(line))
    check("the x-height was measured",
          bool(calibrated.x_height_units), calibrated.x_height_units)

    medians = {k: statistics.median(v) for k, v in samples.items()}
    ratio = max(medians.values()) / min(medians.values())
    check("a {}- and a {}-character line come out the same size".format(
              len(short), len(long)),
          ratio < 1.2,
          "{:.2f} vs {:.2f} model units (ratio {:.2f})".format(
              medians[short], medians[long], ratio))

    doc = calibrated
    check("no line was truncated", not any(l.truncated for l in doc.lines))
    check("a style is recorded", doc.style == 7)
    check("the character advance was measured",
          bool(doc.char_advance_units), doc.char_advance_units)


def test_online_chunking():
    section("chunked lines (headless browser)")
    from handwriting_helper.generate import Generator

    short = "sending a note to a printer one word"                       # 36 chars, one draw
    long = short + " at a time and carrying on well past the limit"      # 81 chars, two draws
    with Generator(style=23, seed=31) as gen:
        gen.calibrate()
        a = gen.line(short)
        b = gen.line(long, index=1)
        fitted = gen.chars_for_width(170.0, 3.0)

    check("a line past the input limit is drawn in chunks",
          b.chunks and len(b.chunks) == 2, b.chunks)
    check("the chunks rejoin to the original text",
          " ".join(b.chunks) == long)
    check("a short line needs no chunking", a.chunks is None)

    per_char = (b.width() / len(b.text)) / (a.width() / len(a.text))
    check("a joined line has the same character density as a single draw",
          0.85 < per_char < 1.15, "ratio {:.2f}".format(per_char))

    check("the page width maps to a plausible line length",
          30 < fitted < 90, "{} characters across 170 mm".format(fitted))


def _raises(name, exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc:
        check(name, True)
    except Exception as other:                       # noqa: BLE001
        check(name, False, "raised {!r} instead".format(other))
    else:
        check(name, False, "nothing was raised")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--online", action="store_true",
                    help="also test generation, which starts headless Chromium")
    args = ap.parse_args(argv)

    test_wrapping()
    test_chunking()
    test_simplify()
    test_parse()
    test_layout()
    test_curves()
    test_profiles()
    test_output()
    if args.online:
        test_online(args)
        test_online_chunking()

    print()
    if FAILURES:
        print("{} check(s) failed: {}".format(len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
