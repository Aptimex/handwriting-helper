# handwriting-helper

Turn text into G-code that a 3D printer can plot in something that looks like human handwriting — in one command, with nothing sent anywhere.

```
./handwrite.py --file letter.txt -o output/letter.gcode --profile ender3
```

![example output](docs/example.svg)

The handwriting comes from [calligrapher.ai](https://www.calligrapher.ai/), a recurrent neural net trained on the IAM On-Line Handwriting Database. A complete offline copy lives in `website/`, and this tool drives it in headless Chromium, so the model runs on your machine and no text leaves it.

## Why this exists

Plenty of programs turn an SVG into G-code, but not usefully for handwriting:

- calligrapher.ai's own SVG export outlines and fills each stroke, so the G-code traces around the edges of the letters instead of writing them.
- The [original model repo](https://github.com/sjvasquez/handwriting-synthesis) does emit stroke lines, but it is old enough to be difficult to run, and so are its forks.
- The site's JavaScript is obfuscated, so the stroke data it generates internally is not exposed through any UI.

So this reaches into the page for the raw stroke points, and keeps the explicit pen-up / pen-down information all the way through to the G-code. Nothing has to infer where a pen lift belongs, because it was never thrown away.

## Install

```
git clone --recurse-submodules https://github.com/Aptimex/handwriting-helper
cd handwriting-helper

python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
playwright install chromium          # one-off, ~120 MB
```

Python 3.11 or newer. If you already cloned without `--recurse-submodules`, run `git submodule update --init` (the `fitCurves` submodule is needed for `--smooth`).

If the repo lives on a VMware shared folder (`/mnt/hgfs/...`) or any other filesystem without symlink support, `python3 -m venv .venv` fails with `Operation not supported: 'lib' -> '.../lib64'`. Put the venv somewhere else:

```
python3 -m venv ~/.venvs/handwriting-helper
. ~/.venvs/handwriting-helper/bin/activate
```

## Use

```
# a single line, straight to G-code
./handwrite.py "Hello world" -o output/hello.gcode --profile ender3

# a whole letter, wrapped to the page and stacked automatically
./handwrite.py --file letter.txt -o output/letter.gcode --profile ender3 --page a5

# see it before you print it
./handwrite.py --file letter.txt -o output/letter.gcode --preview output/letter.svg

# SVG instead, if you'd rather keep your own G-code workflow
./handwrite.py --file letter.txt -o output/letter.svg
```

Generating is the slow, random part; layout is what you actually iterate on. Save the strokes once and re-lay them out as often as you like:

```
./handwrite.py --file letter.txt --save-strokes output/letter.strokes.json -o output/a.gcode
./handwrite.py --strokes output/letter.strokes.json --page a5 --x-height-mm 4 -o output/b.gcode
```

Run `./handwrite.py --help` for the full list. The options worth knowing:

| Option | What it does |
|---|---|
| `--style N` | One of 80 handwriting styles (the website exposes only 9). Fixed for the whole document; `random` picks one and reports it. |
| `--bias F` | Legibility, 0.15 (loose and scrawly) to 2.5 (neat). |
| `--seed N` | Makes the output reproducible — the same seed gives byte-identical strokes. |
| `--x-height-mm` | Physical size, measured on letters like `n`. 3 mm is normal handwriting. |
| `--wrap N` \| `fit` | Characters per line. `fit` (the default) fills the page width. |
| `--keep-breaks` | Honour the line breaks in your input instead of reflowing each paragraph. |
| `--page`, `--margin`, `--align`, `--line-pitch` | Page layout. `--page fit` crops to the ink. |
| `--smooth` | Fits cubic beziers to the strokes. Looks better at large sizes. |
| `--simplify-tol 0.05` | Drops redundant points. Roughly halves the file for no visible change. |
| `--dry-run-z 5` | Raises both pen heights, so you can watch the motion without touching paper. |

## Line length and the 50-character limit

The model's input box caps a single draw at 50 characters, but that no longer caps a line of output. Lines longer than the limit are split at word boundaries, drawn as separate chunks, and butted back together on a shared baseline. So `--wrap` takes any width, and by default the *page* decides where lines break: `--wrap fit` measures the chosen style and fits as many characters as the margins allow.

Seams land between words, which is where the model lifts the pen anyway, so a joined line reads as one line. Two details make that work:

- **The gap at a seam** is set to 0.72 of the style's measured character advance. That figure comes from generating texts whole and in halves and solving for the gap that makes the widths match. It is a ratio rather than a fixed number because the measured advance runs from 7.5 to 9.8 model units depending on the style. Override it with `--word-gap`.
- **The baseline drift carries across the seam**, so the line keeps wandering the way a single draw would instead of stepping back to level. The carry is clamped per seam so one badly-fitted chunk can't tilt the rest of the line.

Joining *individual words* rather than multi-word chunks was tried and is noticeably worse: a word drawn alone has no neighbours to lead into, letter sizes wobble more between words, and the result reads as pasted together. Chunks close to the 50-character limit keep almost all of the model's context, and there is only one seam per overflowing line rather than one per word. They are also far quicker — one draw per line instead of one per word.

Because line breaks are now a page decision, input that arrives already hard-wrapped is reflowed paragraph by paragraph first; otherwise every original line break would leave a short orphan behind. Blank lines still separate paragraphs. Use `--keep-breaks` for text where the line breaks are deliberate, such as an address block or a poem.

## Printer profiles

G-code is written from a profile. Two are included, both commented: `profiles/ender3.toml` for a 220 × 220 mm bed, and `profiles/generic-256.toml` for the 256 × 256 mm beds most current machines use. Copy one and edit it. `--profile` takes either a name from `profiles/` or a path to a `.toml` file.

The number that matters is `pen_down_z`. Home the machine, jog Z down until the pen just marks the paper, and use that value. Then check the result before committing to it:

```
./handwrite.py "test" -o output/test.gcode --profile generic-256 --dry-run-z 5
tools/gcode_preview.py output/test.gcode -o output/test.preview.svg
```

`tools/gcode_preview.py` renders a G-code file back to SVG — drawing moves in black, travel moves in red. It is the cheapest way to catch an inverted axis or a pen that never lifts.

No profile ever emits heater, fan or extruder commands. If your machine needs any, put them in the profile's `preamble`.

`handwrite.py` refuses to write G-code that would run off the bed, if the profile declares one. Note that an A4 page fits neither a 220 nor a 256 mm bed: use `--page a5`, `--page fit`, or a custom size such as `--page 200x200`.

## How it works

```
text
  └─ reflowed into paragraphs, then wrapped to the page width
      └─ headless Chromium runs website/ once, then draws each line
          │   lines over 50 characters are drawn in chunks and joined
          └─ strokes in model units, baseline-relative and length-independent
              ├─ layout: scaled to millimetres, stacked on a baseline grid, placed on a page
              ├─ SVG:    one <path> per stroke, dimensions in mm
              └─ G-code: one pen-down move per stroke, from a printer profile
```

A few things in the site's code shape all of this, and they are documented where they matter in `handwriting_helper/generate.py`:

- The Draw button listens for `mousedown`, not `click`, and only after the model has loaded.
- The sampler gives up after 40 steps per character. Hitting that means the line was cut off rather than finished, so `--tries` re-draws it.
- **Glyph scale is derived from the character count** (`v = min(105/n, 11) * canvasWidth/1240`), so a 50-character line is drawn about five times smaller than a 10-character one. The page leaves that scale in a global, which is what lets this tool undo it. Without that, stacked lines come out in mismatched sizes.
- The 80 style vectors differ in size by nearly 4×, so each run measures the chosen style rather than assuming a constant: the x-height anchors `--x-height-mm`, and the character advance is what `--wrap fit` counts with. `--no-calibrate` skips both.

Measuring the x-height needs a little care, and `strokes.estimate_x_height` explains why. Removing a straight baseline fit is not enough, because real drift is not linear: the leftover wander grows with line length, and a whole-line measurement makes a 45-character sample read about 28% taller than a 9-character one even though the letters are the same size. Measuring in fixed-width windows instead holds to within 4% across those lengths.

### Limitations

- The model's vocabulary is 77 printable characters; anything else is drawn as garbage and warned about. Digits in particular are the model's weakest output.
- Each chunk is sampled independently, so a stray flourish is possible. Re-run with a different `--seed`, or edit the saved strokes file.
- `--wrap fit` sizes lines from a measured average advance, so an unusually wide line can still overrun the margin by a millimetre or two. You get a warning when it does.

## The offline site copy

`website/` is a snapshot of calligrapher.ai taken from the [Web Archive](https://web.archive.org/web/20240924102434/https://www.calligrapher.ai/), with Google Analytics removed and `d.bin` — the 2.65 MB model — saved locally. The network itself is hand-written vanilla JavaScript, not TensorFlow.js, which is why it loads in about a second and needs no ML dependencies.

`handwrite.py` serves that directory on a random local port and intercepts the `d.bin` request, so the copy needs no edits. To use it by hand instead, see `website/README.md`.

## The old manual workflow

The original flow still works if you have point arrays captured that way:

1. Generate text on calligrapher.ai, open the browser console and run `JSON.stringify(tr)`.
2. Save the output to a file, stripping the surrounding quotes (and un-escaping `\"` if your browser added them).
3. `./callig2svg.py points.json -o output/output.svg`

`input/sample-points.json` is one such capture, kept as a test fixture. Those points are in the browser's canvas pixels, so the resulting SVG has no meaningful scale — which is one of the things `handwrite.py` fixes.

## Tests

```
tools/selftest.py            # wrapping, chunking, layout, curve fitting, profiles, output
tools/selftest.py --online   # also generation: reproducibility, scale consistency, seams
```

## Alternatives

- If you don't mind the uniform look of a font, skip all of this and generate G-code from an [engraving font](https://www.evilmadscientist.com/2019/hershey-text-v30/) in Inkscape.
- [otuva/handwriting-synthesis](https://github.com/otuva/handwriting-synthesis) is a TensorFlow 2 fork of the original model with pretrained weights, if you'd rather have a pure-Python generator than a browser. It is archived, and its output differs from calligrapher.ai's.
- [utrost/VHS](https://github.com/utrost/VHS) builds plotter handwriting from a library of hand-drawn glyphs rather than a neural net, and has its own page layout engine.
- [vpype](https://github.com/abey79/vpype) with [vpype-gcode](https://github.com/plottertools/vpype-gcode), or [svg2gcode](https://github.com/sameer/svg2gcode), if you want to take the SVG output through a general-purpose plotter toolchain instead.

## License

GPL-3.0-or-later. The `fitCurves` submodule is MIT.
