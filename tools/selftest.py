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
from handwriting_helper.generate import _word_ends
from handwriting_helper.layout import (
    DEFAULT_LINE_PITCH_RATIO, LayoutError, layout, parse_page)
from handwriting_helper.strokes import (
    Document, Line, estimate_x_height, paragraphs, reflow, simplify, split_pen_flags)
from handwriting_helper import styles
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


def test_paragraphs():
    section("paragraphs")
    flowed = reflow("one\ntwo\n\nthree\nfour\n")
    check("a paragraph is joined onto one line", flowed == "one two\n\nthree four", repr(flowed))
    check("runs of blank lines collapse", reflow("a\n\n\n\nb") == "a\n\nb")
    check("paragraphs become word lists with a gap between",
          paragraphs("one\ntwo\n\nthree") == [["one", "two"], None, ["three"]])
    check("--keep-breaks keeps every input line",
          paragraphs("one\ntwo\n\nthree", keep_breaks=True)
          == [["one"], ["two"], None, ["three"]])
    check("leading and trailing blank lines are dropped",
          paragraphs("\n\nhi\n\n\n", keep_breaks=True) == [["hi"]])


def _points(spec):
    '''
    Synthetic draw: (attended_position, pen_up) pairs, one point per pair, x advancing by one.
    Attended position 0 is the start token, so character i of the text is position i+1.
    '''
    return [(float(i), 0.0, pen, att) for i, (att, pen) in enumerate(spec)]


def test_segmentation():
    section("word segmentation")
    # "ab cd": a=1 b=2 space=3 c=4 d=5
    pts = _points([(1, 0), (2, 0), (2, 1), (3, 0), (4, 0), (5, 0), (5, 1)])
    check("each word ends at its last pen-up", _word_ends(pts, "ab cd") == [2, 6],
          _word_ends(pts, "ab cd"))

    # "I wo": the pen lifts after "I" while attention is still on it, then "w" starts
    # before the next lift. The cut belongs at the first lift, not after the "w".
    pts = _points([(1, 0), (1, 1), (2, 0), (3, 0), (3, 1), (4, 0), (4, 1)])
    check("a cut never takes the start of the next word",
          _word_ends(pts, "I wo")[0] == 1, _word_ends(pts, "I wo"))

    # Attention dropping back to the start token is noise, not the model going back.
    pts = _points([(1, 0), (0, 0), (2, 1), (0, 0), (4, 0), (5, 1)])
    check("start-token blips are ignored", _word_ends(pts, "ab cd") == [2, 5],
          _word_ends(pts, "ab cd"))

    pts = _points([(1, 0), (2, 1), (3, 0)])
    check("in a draw stopped early, a word only counts once the next has begun",
          _word_ends(pts, "ab cd", finished=False) == [],
          _word_ends(pts, "ab cd", finished=False))
    pts = _points([(1, 0), (2, 1), (4, 0)])
    check("a last word the draw was stopped in is left out",
          _word_ends(pts, "ab cd", finished=False) == [1],
          _word_ends(pts, "ab cd", finished=False))
    # The model ran on past the end instead of stopping: the scribble is not kept.
    pts = _points([(1, 0), (2, 1), (4, 0), (5, 0), (6, 1), (6, 0), (6, 0), (6, 1)])
    check("a run-on after the last word is cut off",
          _word_ends(pts, "ab cd", finished=False) == [1, 4],
          _word_ends(pts, "ab cd", finished=False))

    check("pen flags split strokes",
          split_pen_flags([(0, 0, 0), (1, 0, 1), (2, 0, 0), (3, 0, 0)])
          == [[(0, 0), (1, 0)], [(2, 0), (3, 0)]])


def test_simplify():
    section("simplification")
    stroke = [(0, 0), (1, 0.01), (2, -0.01), (3, 0), (3, 5)]
    out = simplify(stroke, 0.1)
    check("collinear points are dropped", len(out) == 3, out)
    check("endpoints are kept", out[0] == stroke[0] and out[-1] == stroke[-1])
    check("a tolerance of 0 is a no-op", simplify(stroke, 0) == stroke)


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
    _raises("a strokes file from an older version is rejected", ValueError,
            Document.from_json, '{"version": 1, "lines": []}')

    page, = layout(document, page="a4", margin_mm=20, x_height_mm=3.0, warn=False)
    check("the page is A4", (page.width_mm, page.height_mm) == (210.0, 297.0))
    check("x-height is honoured", abs(page.x_height_mm - 3.0) < 1e-9)
    check("the default line pitch follows the x-height",
          abs(page.line_pitch_mm - 3.0 * DEFAULT_LINE_PITCH_RATIO) < 1e-6,
          page.line_pitch_mm)

    box = page.bbox()
    check("ink stays inside the page", 0 < box[0] and box[2] < 210 and box[3] < 297, box)

    doubled, = layout(document, page="a4", x_height_mm=6.0, warn=False)
    check("doubling the x-height doubles the ink",
          abs(_width(doubled) / _width(page) - 2.0) < 0.01)

    fitted, = layout(document, page="fit", margin_mm=5, warn=False)
    fbox = fitted.bbox()
    check("'fit' crops to the ink plus the margin",
          abs(fbox[0] - 5) < 0.6 and abs(fitted.width_mm - fbox[2] - 5) < 0.6, fbox)

    # Pagination: 30 lines with a gap in the middle, on a page that holds far fewer.
    line = document.lines[1]
    blank = Line(text="", blank=True)
    long_doc = Document(lines=[line] * 15 + [blank] + [line] * 15,
                        x_height_units=document.x_height_units)
    pages = layout(long_doc, page="80x60", margin_mm=5, x_height_mm=2.0, warn=False)
    check("a long text runs onto several pages", len(pages) > 2, len(pages))
    inside = all(p.bbox()[1] > 0 and p.bbox()[3] < 60 for p in pages)
    check("every page's ink stays on that page", inside)
    total = sum(len(p.strokes) for p in pages)
    check("no line is lost between pages", total == 30 * len(line.strokes),
          "{} vs {}".format(total, 30 * len(line.strokes)))
    tops = [round(p.bbox()[1], 6) for p in pages]
    check("every page starts on its first baseline", len(set(tops)) == 1, tops)

    check("a4 and letter are known", parse_page("letter") == (215.9, 279.4))
    check("WxH parses", parse_page("120x80") == (120.0, 80.0))
    _raises("an unknown page size is rejected", LayoutError, parse_page, "a9")
    _raises("a bad alignment is rejected", LayoutError,
            layout, document, align="middle", warn=False)
    _raises("a page too short for one line is rejected", LayoutError,
            layout, document, page="80x10", margin_mm=4, warn=False)


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
    page, = layout(document, page="fit", margin_mm=5, warn=False)
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

        big, = layout(document, page="a4", margin_mm=20, x_height_mm=3.0, warn=False)
        _raises("a page that overruns the bed is refused", BedLimitError,
                write_gcode, big, Path(tmp) / "big.gcode", profile)


def test_style_table():
    section("style table")
    missing = [n for n in range(80) if styles.lookup(n) is None]
    check("every style has a measured size", not missing, missing)
    sizes = [styles.lookup(n)[0] for n in range(80) if styles.lookup(n)]
    check("styles really do differ in size", max(sizes) / min(sizes) > 2.5,
          "{:.1f}..{:.1f}".format(min(sizes), max(sizes)))


def test_online(args):
    section("generation (headless browser)")
    import statistics
    from handwriting_helper.generate import Generator, generate

    first = generate("repeatable", width_units=400, style=7, seed=4242)
    second = generate("repeatable", width_units=400, style=7, seed=4242)
    check("a seed makes generation reproducible",
          first.lines[0].strokes == second.lines[0].strokes)
    check("the style's size comes from the table",
          first.x_height_units == styles.lookup(7)[0])

    # Sampling straight from the model means nothing depends on line length -- the page's
    # own draw handler scales glyphs by min(105/n, 11). Both texts are x-height letters only
    # so the measurements are comparable, and each is measured over several seeds because a
    # single sample carries a spread of its own.
    short, long = "noun once", "noun once moxe suna comun ecoun samu nexo con" * 2
    with Generator(style=7, seed=11) as gen:
        heights = {t: statistics.median(
            estimate_x_height(Line(text=t, strokes=gen.draw(t).strokes()))
            for _ in range(3)) for t in (short, long)}
        long_draw = gen.draw("It was the best of times, it was the worst of times, it was "
                             "the age of wisdom, it was the age of foolishness, it was the "
                             "epoch of belief")
        words = "Call me Ishmael. Some years ago, never mind how long precisely, having " \
                "little or no money in my purse, and nothing particular to interest me on " \
                "shore, I thought I would sail about a little".split()
        width = 300.0
        lines = gen.flow(list(words), width)

    ratio = max(heights.values()) / min(heights.values())
    check("a {}- and a {}-character line come out the same size".format(
              len(short), len(long)), ratio < 1.2,
          "{:.2f} vs {:.2f} model units (ratio {:.2f})".format(
              heights[short], heights[long], ratio))

    check("a draw far past the site's 50-character input cap finishes",
          long_draw.finished and len(long_draw.word_ends) == len(long_draw.words),
          "{} of {} words".format(len(long_draw.word_ends), len(long_draw.words)))

    section("line filling (headless browser)")
    check("every word is set exactly once, in order",
          " ".join(l.text for l in lines) == " ".join(words))
    widths = [max(p[0] for p in l.points) for l in lines]
    check("no line runs past the width", max(widths) <= width,
          "widest {:.1f} of {:.0f}".format(max(widths), width))
    check("every line but the last fills most of the width",
          min(widths[:-1]) > 0.75 * width,
          " ".join("{:.0f}".format(w) for w in widths))
    check("no line ran out of steps", not any(l.truncated for l in lines))


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

    test_paragraphs()
    test_segmentation()
    test_simplify()
    test_layout()
    test_curves()
    test_profiles()
    test_output()
    test_style_table()
    if args.online:
        test_online(args)

    print()
    if FAILURES:
        print("{} check(s) failed: {}".format(len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
