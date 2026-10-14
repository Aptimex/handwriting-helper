# handwriting-helper

Turn text into G-code that a 3D printer can plot in a way that mimics the strokes of human handwriting.

```bash
./handwrite.py --file letter.txt --profile generic-256    # -> output/letter.gcode and output/letter.svg
```

![example output](docs/example.svg)

The handwriting comes from [calligrapher.ai](https://www.calligrapher.ai/), a recurrent neural net trained on the IAM On-Line Handwriting Database. A complete offline copy lives in `website/`, and this tool runs its network with a numpy port of the page's JavaScript, so everything runs locally.

> [!WARNING]
> This program outputs raw G-code. If you pick the wrong settings, this G-code may cause the printer to attempt movement outside its physical limits, causing damage. Use at your own risk.

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

pip install -r requirements.txt
```

The original site can still be used as the "backend" instead of the numpy port, with `--backend browser`, but it needs Playwright and a headless Chromium (`pip install playwright && playwright install chromium`, about 120 MB). Both backends produce effectively identical output, and the numpy one runs faster.

Requires python 3.11 or newer. If you already cloned without `--recurse-submodules`, run `git submodule update --init` (the `fitCurves` submodule is needed for `--smooth`).

## Use

```
# a single line -> output/hello.gcode and output/hello.svg
./handwrite.py "Hello world" --name hello --profile ender3

# a whole text file on A5 paper, split into as many pages as needed -> output/letter.*
./handwrite.py --file letter.txt --profile ender3 --page a5

# the same, but assuming the paper fills the entire bed
./handwrite.py --file letter.txt --profile ender3 --page fit

# only the SVG, if you'd rather keep your own G-code workflow, into another folder
./handwrite.py --file letter.txt --format svg --out-dir ~/plots
```

Every run writes a G-code file and an SVG of the same page into `--out-dir` (default `output/` in the current directory, created if needed). They are named after the input file without its extension, or `--name`, or `handwriting` for text given on the command line. Existing files are overwritten; with `--no-overwrite` the whole set is written as `letter (1).gcode`, `letter (1).svg`, ... instead.

A text that runs longer than one page is split into numbered files — `letter-p1.gcode`, `letter-p2.gcode`, and so on. A page taller than the bed's Y travel is further cut, between lines, into parts lettered `letter-p1a.gcode`, `letter-p1b.gcode`, ... (see [Printer profiles](#printer-profiles)) so you can manually move the paper and fill an entire physical page. SVGs are never cut.

Generating strokes is the slow, random part. You can save the raw strokes output and re-render them them later into SVG or G-code, with a different profile, smoothing, simplification, etc:

```
./handwrite.py --file letter.txt --save-strokes output/letter.strokes.json
./handwrite.py --strokes output/letter.strokes.json --smooth --name letter-smooth
```

However, line breaks are chosen during the stroke generation step against the page width at the requested size. So re-laying saved strokes onto a narrower page or at a larger `--x-height-mm` won't re-break them; you get a warning if they overrun the margins, and should generate again instead.

Run `./handwrite.py --help` for the full list. The main options:

| Option | What it does |
|---|---|
| `--style N` | One of 80 handwriting styles (the website exposes only 9). `random` picks one and prints it. |
| `--bias F` | Legibility, 0.15 (loose and scrawly) to 2.5 (neat). |
| `--seed N` | Makes the output reproducible — the same seed gives byte-identical strokes. Without one, a seed is chosen and printed, so a run you like can be repeated. |
| `--x-height-mm` | Physical size, measured on letters like `n`. 3 mm is normal handwriting. |
| `--wrap N` \| `fit` | Line length. `fit` (the default) fills the width between the margins; a number asks for lines of about N characters. |
| `--reflow` | Treat the input as pre-wrapped: join single line breaks and reflow each paragraph, so only blank lines separate paragraphs. By default every line break in the input is kept. |
| `--page`, `--margin`, `--align`, `--line-pitch` | Page layout; Letter with 10 mm margins by default. `--page fit` makes the page the area of the `--profile`'s bed the pen can reach, so any sheet that covers it works; margins are measured from the edges of that area. |
| `--smooth` | Fits cubic beziers to the strokes. Looks better at large sizes. |
| `--simplify-tol 0.05` | Drops redundant points. Roughly halves the file for no visible change. |
| `--dry-run-z 5` | Raises both pen heights, so you can watch the motion without touching paper. |
| `--out-dir`, `--name`, `--format`, `--no-overwrite` | Where the files go, what they are called, whether to write `both` (default), `gcode` or `svg`, and whether to keep existing files. |

## Text reflow

Most of the difficulty in laying out generated handwriting is that you don't know how wide a line of it will be until it has been drawn: the 80 styles' letter widths differ, and so does every sample of the same style. Predicting a character count and hoping it fits leaves you choosing between lines that overrun the margin and lines that stop well short of it.

To solve this, each line is drawn with more words than should fit, and then cut back as soon as a stroke crosses the margin:

1. Draw the next ~1.5 lines' worth of words as one sequence.
2. Find where each word's ink ends. The model's attention says which character it is writing at every step, so each point of ink belongs to a known word. A word ends at the last pen lift before the model starts on the next word.
3. Keep the longest run of whole words whose ink stays inside the line width. The words after it are used to start the next line.

Sampling stops as soon as the pen passes the line width, so the extra words don't actually burn time getting generated.

A word too wide for a line on its own is split across lines. Every line break in the input is kept, and a line too long for the page is wrapped. For input that arrives already hard-wrapped, use `--reflow`: it joins each paragraph onto one line first, otherwise every original line break would leave a short orphan behind; blank lines still separate paragraphs.

Pages are then simple: every line is the same pitch apart, so a page holds a fixed number of lines and the rest continue on the next.

## Printer profiles

G-code is written from a profile. Two are included, both commented: `profiles/ender3.toml` for a 220 × 220 mm bed, and `profiles/generic-256.toml` for the 256 × 256 mm beds most current machines use. Copy one and edit it. `--profile` takes either a name from `profiles/` or a path to a `.toml` file.

The number that matters is `pen_down_z`. Home the machine, jog Z down until the pen reliably marks the paper while moving, and use that value. Then sanity check the result by "writing" 5mm above the bed before committing to it:

```
./handwrite.py "test" --name test --format gcode --profile generic-256 --dry-run-z 5
tools/gcode_preview.py output/test.gcode -o output/test.preview.svg
```

`tools/gcode_preview.py` renders a G-code file back to SVG — drawing moves in black, travel moves in red. It is the cheapest way to catch an inverted axis or a pen that never lifts.

No profile ever emits heater, fan or extruder commands. If your machine needs these for some reason, put them in the profile's `preamble`.

`handwrite.py` refuses to write G-code that would run off the bed, if the profile declares one. A page taller than the bed — A4 or Letter on a 220 or 256 mm bed — is cut between lines into lettered parts, `-p1a`, `-p1b`, …, each of which fits. The lines already in reach with the paper in its usual place make up one part that needs no paper move; for each of the others, the command line and the top of its G-code file say how far to slide the paper along Y first, in whole millimetres, for example:

```
; paper: slide the paper 19 mm toward Y0 along Y, so the page corner that normally sits on the origin is at X18 Y-1
```

A page too *wide* for the bed cannot be cut this way and is still refused: use a narrower `--page`, or `--page fit`.

`--page fit` sizes the page to the bed instead: the bed less the profile's origin, so 238 × 238 mm on `generic-256` and 200 × 200 mm on `ender3`. Lines wrap to that width, longer texts continue onto `-p2`, `-p3`, …, and nothing ever needs cutting. Lay any sheet that covers the area with its corner on the origin. It needs a profile that declares a `bed`, even for SVG output, where it shows exactly what one bed load looks like.

## How it works

```
text
  └─ split into paragraphs of words
      └─ the model in website/d.bin is loaded once
          └─ each line: draw extra words, cut back to the ones whose ink fits the width
              └─ strokes in model units, baseline-relative and length-independent
                  ├─ layout: scaled to millimetres, stacked on a baseline grid, paginated
                  ├─ SVG:    one <path> per stroke, dimensions in mm
                  └─ G-code: one pen-down move per stroke, from a printer profile
```

The page is used only for its model. `handwriting_helper/model.py` is a line-for-line numpy port of the page's network, and runs its own sampling loop instead of the page's Draw button (the browser backend in `generate.py` does the same inside the page, on its globals). That matters because the page's own draw handler **scales glyphs by the character count** (`v = min(105/n, 11)`), so a 50-character line comes out about five times smaller than a 10-character one. Sampling directly yields the model's own pen offsets, in which a letter is the same size whatever line it is in.

The 80 style vectors differ in size by more than 4×, so `--x-height-mm` needs to know the chosen style's size. That depends on the style alone — bias moves it by under 5% — so it is measured once per style and shipped in `handwriting_helper/styles.json`. `tools/build_style_table.py` rebuilds it (a few minutes) if the model ever changes.

Measuring an x-height needs a little care, and `strokes.estimate_x_height` explains why. Removing a straight baseline fit is not enough, because real drift is not linear: the leftover wander grows with line length, and a whole-line measurement makes a 45-character sample read about 28% taller than a 9-character one even though the letters are the same size. Measuring in fixed-width windows instead holds to within 4% across those lengths.

### Limitations

- The model's vocabulary is 77 printable characters; anything else is drawn as garbage and warned about. Digits in particular are the model's weakest output.
- Every line is sampled separately, so a stray flourish or a misformed first letter is possible. Re-run with a different `--seed`, or edit the saved strokes file.
- The model now and then leaves out punctuation, most often a full stop at the very end of a paragraph.

## The offline site copy

`website/` is a snapshot of calligrapher.ai taken from the [Web Archive](https://web.archive.org/web/20240924102434/https://www.calligrapher.ai/), with Google Analytics removed and `d.bin` — the 2.65 MB model — saved locally. The network itself is hand-written vanilla JavaScript, not TensorFlow.js, which is why it loads in about a second and needs no ML dependencies.

`handwrite.py` serves that directory on a random local port and intercepts the `d.bin` request, so the copy needs no edits. To use it by hand instead, see `website/README.md`.

## Tests

```
tools/selftest.py            # paragraphs, word segmentation, layout and pagination, output
tools/selftest.py --online   # also generation: reproducibility, scale, long draws, line filling
```

## Alternatives

- If you don't mind the uniform look of a font, skip all of this and generate G-code from an [engraving font](https://www.evilmadscientist.com/2019/hershey-text-v30/) in Inkscape.
- [otuva/handwriting-synthesis](https://github.com/otuva/handwriting-synthesis) is a TensorFlow 2 fork of the original model with pretrained weights. It is archived, and its output differs from calligrapher.ai's.
- [utrost/VHS](https://github.com/utrost/VHS) builds plotter handwriting from a library of hand-drawn glyphs rather than a neural net, and has its own page layout engine.
- [vpype](https://github.com/abey79/vpype) with [vpype-gcode](https://github.com/plottertools/vpype-gcode), or [svg2gcode](https://github.com/sameer/svg2gcode), if you want to take the SVG output through a general-purpose plotter toolchain instead.

## License

AGPL-3.0-or-later. The `fitCurves` submodule is MIT.

## TODO
- Handle or warn about pen offset vs nozzle.
