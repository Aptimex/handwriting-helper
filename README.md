# Handwriting Helper

Turn text into G-code that a 3D printer can plot in a way that mimics the strokes of human handwriting. Dynamically reflows text to fit arbitrary combinations of paper and bed sizes.

```bash
./handwrite.py --file example.txt --profile generic-256
```

![example output](docs/example.svg)

The handwriting comes from the model used by [calligrapher.ai](https://www.calligrapher.ai/), a recurrent neural net trained on the IAM On-Line Handwriting Database. A complete offline copy lives in `website/`, and this tool runs its model network with a numpy port of the page's JavaScript, so everything runs locally.

> [!WARNING]
> This program outputs raw G-code. If you pick the wrong settings, this G-code may cause your printer to attempt movement outside its physical limits, causing damage. Use at your own risk.

> [!CAUTION]
> **Home your printer yourself before running any G-code.** The G-code files use absolute machine coordinates which only work if the printer knows the current X, Y and Z coordinates. Failure to home first has different consequences depending on your printer/firmware, but in some cases will absolutely **crash the toolhead into the frame** and grind motor gears. You can add `G28` (or `G28 X Y`) to a profile's preamble to auto-home at the start if you know your pen mount won't interfere with the homing process.

## Install

```bash
git clone --recurse-submodules https://github.com/Aptimex/handwriting-helper
cd handwriting-helper

pip install -r requirements.txt
```

Requires python 3.11 or newer. If you already cloned without `--recurse-submodules`, run `git submodule update --init` (the `fitCurves` submodule is needed for `--smooth`).

## Use

Before generating any files you'll need to setup a config file for your printer/plotter. See the [Printer profiles](#printer-profiles) section. 

All output files will be saved to the `output` folder (relative to cwd). By default this will generally include an SVG file with the stroke data, a G-code file generated from that SVG, and a preview SVG file showing the expected relative placement of bed, paper, and pen strokes in the G-code file. File names are derived from the input filename, and output files overwrite existing files by default. 

```bash
# a single text line input
./handwrite.py "Hello world" --name hello --profile ender3

# a whole text file on A5 paper, split into as many pages as needed
./handwrite.py --file example.txt --profile ender3 --page a5

# the same, but assuming the selected paper fills the entire bed
./handwrite.py --file example.txt --profile ender3 --page fit

# only the SVG, if you'd rather keep your own G-code workflow, saved into another folder
./handwrite.py --file example.txt --format svg --out-dir ~/plots
```

Input text that runs longer than one physical page is split into numbered files — `letter-p1.gcode`, `letter-p2.gcode`, and so on. A physical page taller than the bed's Y travel is further cut, between lines, into parts lettered `letter-p1a.gcode`, `letter-p1b.gcode`, ..., each with its own `letter-p1a-preview.svg`, so you can manually shift the paper and fill an entire physical page. Only G-code files are cut this way, not SVGs. Such cut G-code files will have a comment line near the top describing how to shift the paper to get the correct alignment. There is no support for splitting pages that are horizontally too wide, text just won't write past the bed width.

Run `./handwrite.py --help` for the full list of options. The main ones:

| Option | What it does |
|---|---|
| `--style N` | One of 80 handwriting styles (the website exposes only 9). `random` is the default and the selection will be printed. |
| `--bias F` | Legibility, 0.15 (loose and scrawly) to 2.5 (neat). |
| `--seed N` | Makes the output reproducible — the same seed gives byte-identical strokes. Default is random and the selection will be printed. |
| `--x-height-mm` | Physical size, measured on letters like `n`. 3 mm is normal handwriting. |
| `--wrap N` \| `fit` | Line length. `fit` (the default) fills the page width between the margins; a number asks for lines of about N characters. |
| `--reflow` | Treat the input as pre-wrapped: join single line breaks and reflow each paragraph, so only blank lines separate paragraphs. By default every line break in the input is kept. |
| `--page`, `--margin`, `--align`, `--line-pitch` | Page layout; Letter with 10 mm margins by default. `--page fit` uses the dimensions of the bed from the `--profile`, so any sheet that covers the bed works. |
| `--paper-anchor` | Where the paper is laid on the bed: flush against a corner (`top-left`, the default, `top-right`, `bottom-left`, `bottom-right`), an edge (`top-center`, `center-left`, ...), or `center`. `top` is the back of the bed. |
| `--smooth` | Fits cubic beziers to the strokes. At large sizes, this makes text look much smoother and more natural. |
| `--simplify-tol 0.05` | Drops redundant points. Roughly halves the file for no visible change. |
| `--bambu [PATH]` | Also wrap each G-code file in a `NAME.gcode.3mf` file that can be easily sent to Bambu Lab printers. Requires you to create a template file first, see the [Bambu Lab printers](#bambu-lab-printers) section. |
| `--dry-run-z 5` | Raises both pen heights in the G-code so you can preview the motion without touching paper. |
| `--out-dir`, `--name`, `--format`, `--no-overwrite` | Where the files go, what they are called, whether to write `both` (default), `gcode` or `svg`, and whether to keep existing files. |

## Printer profiles

G-code is generated based on info in a printer profile. Two examples are included: `profiles/ender3.toml` for a 220 × 220 mm bed, and `profiles/generic-256.toml` for the 256 × 256 mm beds many modern printers use. Copy one and edit it. `--profile` takes either a name (sans file extension) from `profiles/` or a path to a `.toml` file. 

The first numbers you will need to customize are `pen_down_z` and `pen_up_z`. Set these values so that your pen will easily mark the paper at the `pen_down_z` height and will be off the paper at the `pen_up_z` height. If you have an adjustable pen holder with at least 4mm of springiness, here's how I recommend setting these: home the machine and raise the nozzle to exactly 5mm above the bed. Adjust your pen in the holder so it's touching the paper and secure it in place. Then you can set `pen_down_z = 1.0` and `pen_up_z = 6.0`. You may need to adjust these values if your spring doesn't easily allow 4mm of vertical travel.

Next, set `pen_offset_x` and `pen_offset_y` in the profile to the pen tip's position relative to the nozzle, in mm (+X if the pen is located to the right of the nozzle, +Y if it's behind it). Here's a visual diagram with `O` as the nozzle position:
```
     +Y

  -X  O  +X

     -Y
```

The G-code always controls the nozzle position, so the generator applies these offsets to movements so that the pen lands on the right spot on the page. An easy way to measure these offsets is to mount the pen and press it into the paper, then move the printhead/bed until the nozzle is directly above the start of the pen line. Then remove the paper and measure the X and Y distances between the start and end of the line. 

The nozzle still has to stay above the bed area while plotting, so the pen cannot reach a strip as wide as the offset along the opposite side of the bed (even if it technically could on your printer, the generator intentionally bounds the nozzle to the bed). Note that the G-code preview adds the offset back, so it shows where the pen (not the nozzle) will move relative to the bed.

Every generated G-code file comes with a `NAME-preview.svg`, rendered directly from the G-code file itself. It shows the G-code's moves (drawing moves in black, travel moves in red) over the bed (blue) and the paper (dashed amber) where the G-code expects it to be placed, so you can see where the writing lands on the sheet and how the sheet should sit on the bed. Note that both rectangles are drawn based on info saved into the G-code's comments, not the G-code itself, so it's just an aid rather than guaranteed truth. To render a preview of a G-code file on its own, for example after editing it by hand, run `tools/gcode_preview.py output/test.gcode`. It takes `--profile` to draw the bed for a file without one specified in its comments, and `--no-frame` to hide both rectangles.

By default the G-code generator assumes the paper is laid flush against the back-left corner of the bed. `--paper-anchor`, or `paper_anchor` in a profile, specifies a different corner, an edge, or the centre of the bed. "The bed" means the rectangle the profile's `bed` gives, starting from X0 Y0; if your nozzle can't actually reach the bed edge, line the paper up with the nozzle's reach instead.

The generator never adds heater, fan or extruder commands, just nozzle movements. If your machine needs these other commands for some reason, put them in the profile's `preamble`.

`handwrite.py` does its best to refuse to write G-code that would cause the nozzle to run off the bed, if the profile defines one. There is no support for exclusion zones within the bed size, if you need these you'll have to crop the entire bed area.

## Bambu Lab printers

Bambu Studio won't send a bare `.gcode` file to printers, it will only send `.gcode.3mf` files that match the format it generates. These are really just ZIP files that store the G-code inside at `Metadata/plate_1.gcode`. Since the slicer will also refuse to send a `.gcode.3mf` that contains metadata for a different printer model than the one you're trying to send to, no template file is included, you have to make your own as described here.

**Before using `--bambu`, make the template once:**

1. In Bambu Studio, select your printer and slice any small object (Right click -> Add Primitive -> Cube). The settings don't matter.
2. Export the sliced plate as a `.gcode.3mf` (File → Export → Export plate sliced file).
3. Save it as `template.gcode.3mf` in the repo root.

If you have multiple printer models to use, instead name each template appropriately and use `--bambu ./printer.gcode.3mf` during generation. 

The `--bambu` flag writes a `NAME.gcode.3mf` output file alongside each G-code file. It copies the template file and swaps its internal `plate_1` G-code file for the one being generated. Everything else in the template (the thumbnails, the slicer settings, the printer model) is copied over unchanged, so the printer will incorrectly show the template's original thumbnail, time estimate and filament use. Home your printer, then open the generated `NAME.gcode.3mf` file in Bambu Studio (it won't show any sliced preview) and send it to your printer as usual (Print Plate button). Any filament mapping it asks for doesn't matter, since nothing is extruded. **Remember to disable auto bed leveling unless your pen mount is compatible with that operation.**

To wrap a `.gcode` file manually, for example after editing it by hand, run `tools/bambu_wrap.py output/letter.gcode`. Use `--template` to specify a non-default `.gcode.3mf` template file location.

## Process Overview

```
text
  └─ split into paragraphs of words
      └─ the model in website/d.bin is loaded once
          └─ each line: draw extra words, cut back to the ones whose ink fits the width
              └─ strokes in model units, baseline-relative and length-independent
                  ├─ layout: scaled to millimeters, stacked on a baseline grid, paginated
                  ├─ SVG:    one <path> per stroke, dimensions in mm
                  └─ G-code: one pen-down move per stroke, from a printer profile
```

`handwriting_helper/model.py` is a line-for-line numpy port of the network used by the calligrapher.ai site, and runs its own sampling loop instead of the page's Draw button (the browser backend in `generate.py` does the same inside the page, on its globals). This allows the raw model output to be used, whereas the website applies several post-processing steps needed to render it nicely in a browser window. This project applies its own minimal post-processing to make each style roughly similar in size for uniform-ish output sizes (see `styles.json`).

### Text reflow

Most of the difficulty in laying out generated handwriting is that you don't know how wide a line of it will be until it has been drawn: the 80 styles' letter widths differ, and so does every sample of the same style. Predicting a character count and hoping it fits leaves you choosing between lines that overrun the margin and lines that stop well short of it.

To solve this, each line is drawn with more words than should fit, and then cut back as soon as a stroke crosses the margin:

1. Draw the next ~1.5 lines' worth of words as one sequence.
2. Find where each word's ink ends. The model's attention says which character it is writing at every step, so each point of ink belongs to a known word. A word ends at the last pen lift before the model starts on the next word.
3. Keep the longest run of whole words whose ink stays inside the line width. Start the next line with the first dropped word.

Sampling stops as soon as the pen passes the line width, so the extra words don't actually burn time getting generated.

A word too wide for a line on its own is split across lines. Every line break in the input is kept, and a line too long for the page is wrapped. For input that already has single-newlines hard-wrapping paragraphs, use `--reflow` to join each paragraph onto one line before starting the layout process. Blank lines still separate paragraphs.

Pages are then simple: every line is the same pitch apart, so a page holds a fixed number of lines and the rest continue on the next.

### Limitations

- The model's vocabulary is 77 printable characters; anything else is drawn as garbage and warned about. Digits in particular are the model's weakest output.
- Malformed letters are pretty common in the model output, and some styles tend to be more legible than others. Re-run with a different `--seed` and you might get better results for a style.
- The model now and then leaves out punctuation and small strokes, like the dots on an `i` or the period at the very end of a paragraph.

## Tests

```
tools/selftest.py            # paragraphs, word segmentation, layout and pagination, output
tools/selftest.py --online   # also generation: reproducibility, scale, long draws, line filling
```

## Alternatives

- If you don't mind the uniform look of a font, an [engraving font](https://www.evilmadscientist.com/2019/hershey-text-v30/) is another good source of human-like strokes.
- [otuva/handwriting-synthesis](https://github.com/otuva/handwriting-synthesis) is an archived TensorFlow 2 fork of the original model with pretrained weights.
- [utrost/VHS](https://github.com/utrost/VHS) builds plotter handwriting from a library of hand-drawn glyphs rather than a neural net, and has its own page layout engine.
- [vpype](https://github.com/abey79/vpype) with [vpype-gcode](https://github.com/plottertools/vpype-gcode), or [svg2gcode](https://github.com/sameer/svg2gcode), if you want to take SVG output through a general-purpose plotter toolchain instead.

## License

AGPL-3.0-or-later. The `fitCurves` submodule is MIT.

## AI Disclaimer

The current code in this repo was almost entirely vibe-coded using Claude. I haven't audited the code, only the behavior on my own printers. I did hand-edit most of the documentation in this README though. The original code I manually wrote that this is all based on is available in the `original-code` tag and associated release.
