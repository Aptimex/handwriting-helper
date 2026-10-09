'''
Stroke generation by driving the offline copy of calligrapher.ai in headless Chromium.

This replaces the old manual steps: open the site, click Draw, then read the `tr` global out
of the DevTools console by hand.

Everything the page needs is already in website/ -- d.bin is the complete 2.65 MB model and
the network is hand-written vanilla JS, so there is no ML framework involved and a line takes
well under a second to sample.

Three details of the page's own code drive the shape of this module:

  * The Draw button listens for *mousedown*, not click, and its listener is only attached
    once d.bin has finished loading (which also removes #loading-indicator).

  * `tr` is a live reference to the array the sampler pushes into, so it is complete once it
    stops growing. The sampler stops at `40 * charCount` steps, so reaching that budget means
    the line was cut off mid-word rather than finished.

  * The glyph scale is derived from the character count:
        v = min(105/n, 11) * canvasWidth/1240
    so a 50-character line is drawn roughly five times smaller than a 10-character one. The
    page leaves that scale in the global `t`, which is what lets us undo it and return
    length-independent coordinates. See strokes.py for the resulting space.
'''

import http.server
import random
import socketserver
import statistics
import sys
import threading
from pathlib import Path

from .strokes import (
    CALIBRATION_TEXT, Document, Line, MAX_DRAW_CHARS, MODEL_CHAR_ADVANCE,
    MODEL_WORD_GAP_RATIO, STEPS_PER_CHAR_BUDGET, estimate_x_height, json2points,
    normalize_text, segments_to_strokes, split_chunks,
)

# Number of calibration samples to median together. One sample carries ~5-8% spread; three
# brings that down to a few percent, which is well inside what anyone can see in ink.
CALIBRATION_SAMPLES = 3

# How far a seam may shift the baseline, as a fraction of the x-height. Each chunk is joined
# carrying the previous one's baseline slope forward, so the line drifts the way a single
# draw would; the clamp stops a chunk that happened to fit a steep slope from tilting the
# rest of the line.
SEAM_DRIFT_CLAMP = 0.35

# Shading applied when fitting characters to a page width, to absorb the spread between the
# calibration line's advance and real mixed-case text.
FIT_SAFETY = 0.95

WEBSITE_DIR = Path(__file__).resolve().parent.parent / "website"
PAGE_NAME = "Calligrapher.ai.htm"
MODEL_BIN = "d.bin"

# The canvas is CSS-sized (width:100vw, height:calc(100vh - 70px)), so the viewport decides
# the scale. 1240 wide makes the page's own canvasWidth/1240 term exactly 1.0.
VIEWPORT = {"width": 1240, "height": 800}

# The speed slider is steps-per-animation-frame only; it has no effect on the geometry, so
# it is pinned to its maximum to make generation as fast as possible.
FAST_SPEED = 9.51

LOAD_TIMEOUT_MS = 120_000
DRAW_TIMEOUT_MS = 60_000
STABLE_MS = 250

# Installed before the page's own script so the sampler's Gumbel-max pick and Box-Muller draw
# become reproducible. mulberry32.
_SEED_SCRIPT = """
(() => {
  let s = 0;
  window.__hwSetSeed = (n) => { s = (n >>> 0); };
  Math.random = function () {
    s = (s + 0x6D2B79F5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
})();
"""

_SET_CONTROLS = """
({style, bias, speed, width}) => {
  const set = (id, v) => {
    const el = document.getElementById(id);
    el.value = String(v);
    el.dispatchEvent(new Event('input', {bubbles: true}));
  };
  set('bias-slider', bias);
  set('speed-slider', speed);
  set('width-slider', width);

  const sel = document.getElementById('select-style');
  if (style === null) {
    sel.value = '-';
  } else {
    const v = String(style);
    if (![...sel.options].some(o => o.value === v)) {
      const o = document.createElement('option');
      o.value = v; o.text = '#' + v;
      sel.add(o);
    }
    sel.value = v;
  }
}
"""

# Waits inside the page rather than round-tripping: one requestAnimationFrame per check, and
# done once tr has stopped growing. Returns the final length, or a negative length on timeout.
_AWAIT_DRAW = """
async ({stableMs, timeoutMs}) => {
  const t0 = performance.now();
  let last = -1, lastChange = t0;
  for (;;) {
    await new Promise(r => requestAnimationFrame(r));
    const n = window.tr.length;
    const now = performance.now();
    if (n !== last) { last = n; lastChange = now; }
    else if (n > 1 && now - lastChange > stableMs) return n;
    if (now - t0 > timeoutMs) return -n;
  }
}
"""

_READ_RESULT = """
() => {
  const canvas = document.getElementById('canvas');
  return {
    tr: Array.from(window.tr, p => [p[0], p[1], p[2]]),
    scale: window.t,
    text: window.Z,
    canvasWidth: canvas.width.baseVal.value,
    canvasHeight: canvas.height.baseVal.value,
    style: (v => Number.isNaN(v) ? null : v)(
      parseInt(document.getElementById('select-style').value, 10)),
  };
}
"""


class GenerationError(RuntimeError):
    pass


def _quiet_handler(directory):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=directory, **kw)

        def log_message(self, *a):
            pass

    return Handler


class _LocalSite:
    '''Serves website/ on an ephemeral port so the page loads over http, not file://.'''

    def __init__(self, directory):
        self.directory = str(directory)
        self._server = None
        self._thread = None

    def __enter__(self):
        self._server = socketserver.ThreadingTCPServer(
            ("127.0.0.1", 0), _quiet_handler(self.directory))
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    @property
    def url(self):
        host, port = self._server.server_address[:2]
        return "http://{}:{}/{}".format(host, port, PAGE_NAME)

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _normalize_line(tr, scale, canvas_width, canvas_height, text):
    '''
    Undo the page's per-line pixel scale and starting offset.

    The page computes, for trimmed text of n characters:
        i = 8.2 * n * scale          (estimated ink width)
        f = max((canvasWidth - i)/2, 10)   (pen start x, centred)
        s = canvasHeight/2 + 20            (pen start y, the baseline)

    Subtracting (f, s) and dividing by `scale` therefore yields coordinates with the baseline
    at y=0 and the line's left starting edge at x=0, independent of line length.
    '''
    n = len(normalize_text(text))
    ink_width = MODEL_CHAR_ADVANCE * n * scale
    start_x = max((canvas_width - ink_width) / 2.0, 10.0)
    start_y = canvas_height / 2.0 + 20.0

    segments = json2points(tr)
    strokes = segments_to_strokes(segments)
    return [[((x - start_x) / scale, (y - start_y) / scale) for x, y in stroke]
            for stroke in strokes]


class Generator:
    '''
    A loaded browser page, ready to draw lines.

    Held open across a document so the 2.65 MB model is loaded once rather than per line, and
    so calibration and drawing share the same page state. Use it as a context manager:

        with Generator(style=7, seed=1) as gen:
            gen.calibrate()
            line = gen.line("some text")
    '''

    def __init__(self, style=None, bias=0.75, seed=None, tries=3, stroke_width=0.76,
                 word_gap=None, headless=True, viewport=None):
        self.style = style
        self.bias = bias
        self.seed = seed
        self.tries = tries
        self.stroke_width = stroke_width
        self.word_gap = word_gap
        self.headless = headless
        self.viewport = viewport or VIEWPORT
        self.x_height_units = None
        self.char_advance_units = None
        self.n_styles = None
        self._page = None
        self._stack = None

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:                              # pragma: no cover
            raise GenerationError(
                "playwright is not installed. Install it with:\n"
                "    pip install playwright && playwright install chromium") from exc

        page_file = WEBSITE_DIR / PAGE_NAME
        model_file = WEBSITE_DIR / MODEL_BIN
        for f in (page_file, model_file):
            if not f.is_file():
                raise GenerationError(
                    "missing {} -- the offline site copy is incomplete".format(f))
        model_bytes = model_file.read_bytes()

        self._site = _LocalSite(WEBSITE_DIR).__enter__()
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        page = self._browser.new_page(viewport=self.viewport)
        page.add_init_script(_SEED_SCRIPT)

        # The page hard-codes http://127.0.0.1:8000/d.bin. Fulfil it from disk so the offline
        # copy needs no edits and no particular port has to be free.
        page.route("**/" + MODEL_BIN, lambda route: route.fulfill(
            status=200,
            body=model_bytes,
            headers={"Content-Type": "application/octet-stream",
                     "Access-Control-Allow-Origin": "*"}))

        page.goto(self._site.url, wait_until="domcontentloaded")
        # The draw handler is only attached once the model has finished loading, which is also
        # when the loading indicator is removed.
        page.wait_for_selector("#loading-indicator", state="detached",
                               timeout=LOAD_TIMEOUT_MS)
        self._page = page

        self.n_styles = int(page.evaluate("() => window.$.g.length / 64"))
        if self.style is None:
            self.style = random.Random(self.seed).randrange(self.n_styles)
        elif not (0 <= self.style < self.n_styles):
            raise GenerationError(
                "style {} out of range; this model has {} styles (0-{})".format(
                    self.style, self.n_styles, self.n_styles - 1))
        return self

    def __exit__(self, *exc):
        try:
            self._browser.close()
        finally:
            self._pw.stop()
            self._site.__exit__(*exc)
        self._page = None

    def vocabulary(self):
        '''The model's character set; anything outside it is drawn as garbage.'''
        return set(self._page.evaluate("() => Object.keys(window.H)"))

    def calibrate(self):
        '''
        Measure this style's letter size, in model units.

        Two numbers come out of it. The x-height anchors --x-height-mm: without it that option
        would mean wildly different things from style to style, since the 80 style vectors
        differ in size by nearly 3x. The character advance lets the page decide how many
        characters fit on a line; measured advances run from 7.5 to 9.8 units depending on the
        style, against the 8.2 the site assumes for everything.

        Costs one sub-second draw per sample.
        '''
        heights, advances = [], []
        for i in range(CALIBRATION_SAMPLES):
            cal_seed = (self.seed if self.seed is not None else 0) + 900_000 + i
            self._page.evaluate("(s) => window.__hwSetSeed(s)", cal_seed)
            line = self._draw(CALIBRATION_TEXT, cal_seed)
            value = estimate_x_height(line)
            if value:
                heights.append(value)
                advances.append(line.width() / len(CALIBRATION_TEXT))
        self.x_height_units = statistics.median(heights) if heights else None
        self.char_advance_units = statistics.median(advances) if advances else None
        return self.x_height_units

    def chars_for_width(self, width_mm, x_height_mm):
        '''
        How many characters fit in `width_mm`, at the measured scale.

        The measured advance predicts real text to within about 6%, so the result is shaded
        down slightly: a line that falls short of the margin looks fine, one that runs past it
        does not.
        '''
        if not self.x_height_units:
            return MAX_DRAW_CHARS
        mm_per_unit = x_height_mm / self.x_height_units
        advance = self.char_advance_units or MODEL_CHAR_ADVANCE
        return max(1, int(FIT_SAFETY * width_mm / (advance * mm_per_unit)))

    def line(self, text, index=0):
        '''
        Draw one line of any length.

        Lines over the site's 50-character input limit are drawn in chunks and joined at word
        boundaries. The model lifts the pen between words anyway, so a seam falls where a pen
        lift belongs; joining individual words instead reads as pasted-together, because a
        word drawn alone has no neighbours to lead into.
        '''
        if not normalize_text(text):
            return Line(text="", blank=True, style=self.style, bias=self.bias)

        chunks = split_chunks(text)
        drawn = []
        for position, chunk in enumerate(chunks):
            line = None
            for attempt in range(max(1, self.tries)):
                chunk_seed = None
                if self.seed is not None:
                    chunk_seed = (self.seed + index * 1_000 + position
                                  + attempt * 100_000)
                    self._page.evaluate("(s) => window.__hwSetSeed(s)", chunk_seed)
                line = self._draw(chunk, chunk_seed)
                if not line.truncated:
                    break
            drawn.append(line)

        if len(drawn) == 1:
            drawn[0].text = normalize_text(text)
            return drawn[0]

        joined = Line(
            text=normalize_text(text),
            strokes=self._join(drawn),
            chunks=[l.text for l in drawn],
            style=self.style,
            bias=self.bias,
            seed=drawn[0].seed,
            scale_px_per_unit=drawn[0].scale_px_per_unit,
            truncated=any(l.truncated for l in drawn),
        )
        return joined

    def seam_gap(self):
        '''The gap left at a chunk seam, scaled to this style unless one was given.'''
        if self.word_gap is not None:
            return self.word_gap
        return MODEL_WORD_GAP_RATIO * (self.char_advance_units or MODEL_CHAR_ADVANCE)

    def _join(self, lines):
        '''Butt chunks together on a shared, continuously drifting baseline.'''
        clamp = SEAM_DRIFT_CLAMP * (self.x_height_units or MODEL_CHAR_ADVANCE)
        gap = self.seam_gap()
        strokes = []
        cursor = 0.0
        dy = 0.0
        for line in lines:
            box = line.bbox()
            if box is None:
                continue
            dx = cursor - box[0]
            strokes.extend([[(x + dx, y + dy) for x, y in s] for s in line.strokes])
            cursor = box[2] + dx + gap
            # Carry this chunk's baseline slope forward so the seam does not step.
            dy += max(-clamp, min(clamp, _baseline_slope(line) * (box[2] - box[0])))
        return strokes

    def document(self, texts, progress=None):
        '''Draw every line, returning a Document in model units.'''
        document = Document(style=self.style, bias=self.bias, seed=self.seed,
                            x_height_units=self.x_height_units,
                            char_advance_units=self.char_advance_units)
        for index, text in enumerate(texts):
            line = self.line(text, index=index)
            document.lines.append(line)
            if progress:
                progress(index, len(texts), line)
        return document

    def _draw(self, text, line_seed):
        return _draw_one(self._page, text, self.style, self.bias, self.stroke_width,
                         line_seed)


def _draw_one(page, text, style, bias, stroke_width, line_seed):
    '''One draw: set the controls, press the button, wait for the sampler to stop.'''
    page.evaluate(_SET_CONTROLS, {"style": style, "bias": bias,
                                 "speed": FAST_SPEED, "width": stroke_width})
    page.fill("#text-input", text)
    page.evaluate("() => { window.tr = []; }")
    page.dispatch_event("#draw-button", "mousedown")

    length = page.evaluate(_AWAIT_DRAW, {"stableMs": STABLE_MS, "timeoutMs": DRAW_TIMEOUT_MS})
    if length < 0:
        raise GenerationError(
            "timed out sampling {!r} after {:.0f}s ({} points so far)".format(
                text, DRAW_TIMEOUT_MS / 1000, -length))

    result = page.evaluate(_READ_RESULT)
    scale = result["scale"]
    if not scale:
        raise GenerationError("the page reported no scale for {!r}".format(text))

    strokes = _normalize_line(result["tr"], scale, result["canvasWidth"],
                              result["canvasHeight"], text)

    n_chars = len(normalize_text(text))
    truncated = (len(result["tr"]) - 1) >= STEPS_PER_CHAR_BUDGET * n_chars

    return Line(
        text=normalize_text(text),
        strokes=strokes,
        style=result["style"],
        bias=bias,
        seed=line_seed,
        scale_px_per_unit=scale,
        truncated=truncated,
    )


def _baseline_slope(line):
    import numpy as np

    pts = list(line.points)
    if len(pts) < 10:
        return 0.0
    xs = np.array([p[0] for p in pts], dtype=float)
    ys = np.array([p[1] for p in pts], dtype=float)
    if xs.max() - xs.min() < 1e-9:
        return 0.0
    return float(np.polyfit(xs, ys, 1)[0])


def generate(texts, style=None, bias=0.75, seed=None, tries=3, stroke_width=0.76,
             calibrate=True, headless=True, progress=None, viewport=None,
             word_gap=None):
    '''
    Generate handwriting for a list of lines. A convenience wrapper around Generator.

    texts        list of strings; "" produces a blank line. Lines longer than the site's
                 50-character input limit are drawn in chunks and joined
    style        0..79, or None to pick one at random for the whole document
    bias         legibility; the site's slider runs 0.15 (loose) to 2.5 (neat)
    seed         base seed for reproducible output
    tries        re-draw a chunk this many times if it comes back truncated
    calibrate    measure this style's x-height so --x-height-mm means something
    progress     optional callback(index, total, Line)

    Note that `style` is resolved once rather than left as the page's "-" (random per draw).
    Per-line random styles would make every line a different size.
    '''
    with Generator(style=style, bias=bias, seed=seed, tries=tries,
                   stroke_width=stroke_width, word_gap=word_gap, headless=headless,
                   viewport=viewport) as gen:
        _warn_unsupported(gen, texts)
        if calibrate:
            gen.calibrate()
        return gen.document(texts, progress=progress)


def _warn_unsupported(generator, texts):
    '''The model's vocabulary lives in the page's `H` global; anything missing maps to id 1.'''
    vocab = generator.vocabulary()
    bad = sorted({c for t in texts for c in normalize_text(t) if c not in vocab})
    if bad:
        print("warning: characters not in the model's vocabulary will be drawn as garbage: "
              + " ".join(repr(c) for c in bad), file=sys.stderr)
