'''
Stroke generation, by running the offline copy of calligrapher.ai's model in headless Chromium.

Everything the page needs is already in website/ -- d.bin is the complete 2.65 MB model and
the network is hand-written vanilla JS, so there is no ML framework involved.

The page is loaded only for its model. Its script declares everything with top-level `var`,
so the encoder (`A`), the sampling step (`L`), the weights (`$`) and the vocabulary (`H`) are
all globals, and this module runs its own sampling loop on them inside the page instead of
pressing the Draw button. That sidesteps three things about the page's own draw handler:

  * it scales glyphs by the character count, v = min(105/n, 11), so a long line comes out
    several times smaller than a short one. Sampling directly yields the model's own offsets,
    which are length-independent by construction;
  * its input box is capped at 50 characters. That is a UI limit, not a model one: single
    draws of 180 characters still sample cleanly, which is longer than any page line;
  * it only stops when the model says it has finished or a step budget runs out. The loop
    here also stops once the pen passes the line width, since nothing beyond it is kept, and
    when the model stalls -- attention stuck on one letter while the pen runs on writing
    "enenenen", this model's characteristic failure.

The loop also records the model's *attention*: which character it is writing at each step.
That is what makes line filling exact. See Generator.flow().
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
    STEPS_PER_CHAR_BUDGET, estimate_x_height, normalize_text, split_pen_flags,
)
from . import styles

WEBSITE_DIR = Path(__file__).resolve().parent.parent / "website"
PAGE_NAME = "Calligrapher.ai.htm"
MODEL_BIN = "d.bin"

# A line is given this many times the words that are expected to fit, then cut back to the
# ones that do. Sampling stops once the pen passes the line width, so the surplus costs
# almost nothing; asking for too few means a redraw.
OVERDRAW = 1.5

# How far past the line width, in characters, sampling continues before it stops. Enough for
# the next word to begin, which is what proves the last fitting word is finished.
OVERRUN_CHARS = 4

# The model's characteristic failure is a stall: attention sticks on one letter and the pen
# runs on to the right writing "enenenen". No real character is anywhere near this many
# advances wide, so travelling this far without attention moving on is a stall.
STALL_CHARS = 5

# Weight given to each new line's measured advance when updating the running estimate.
ADVANCE_SMOOTHING = 0.5

# Redraws allowed when a line came out short because too few words were drawn.
MAX_REFILLS = 2

# Shading applied when hard-splitting a word that is too wide for a line on its own.
SPLIT_SAFETY = 0.9

# Mixed-case text for measuring a style's character advance, the figure that sizes each draw.
ADVANCE_TEXT = "The quick brown fox jumps over the lazy dog"

# Draws medianed together when measuring a style. One sample carries ~5-8% spread.
MEASURE_SAMPLES = 5

LOAD_TIMEOUT_MS = 120_000

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

# Wraps the page's attention function `k` so every step also leaves behind the position the
# model is attending to. `k` computes a mixture-of-logistics window over the character grid
# but keeps the weights local, so they are recomputed here from the same inputs: weights `a`
# (softmax), widths `beta` (softplus), and the positions `e.k` it has just advanced. The
# argmax over the grid is the character being written; grid position 0 is the start token, so
# character i of the text is position i+1.
_INSTALL_ATTENTION = """
() => {
  if (window.__hwAttention) return;
  const original = k;
  const sig = z => 1 / (1 + Math.exp(-z));
  k = (r, e) => {
    const result = original(r, e);
    const [al, bl] = b(u(m(r, $.h), $.n), 3);
    const a = d(al), beta = n(bl);
    const width = K(e.u) / 10;
    let best = 0, bestPhi = -1;
    for (let pos = 0; pos < width; pos++) {
      let phi = 0;
      for (let j = 0; j < 10; j++) {
        phi += a[j] * (sig((pos + 0.5 - e.k[j]) / beta[j]) - sig((pos - 0.5 - e.k[j]) / beta[j]));
      }
      if (phi > bestPhi) { bestPhi = phi; best = pos; }
    }
    window.__hwAttended = best;
    return result;
  };
  window.__hwAttention = true;
}
"""

# One draw, mirroring the page's own draw handler minus the canvas. Returns
# [[x, y, penUp, attendedPosition], ...] in model units, with the pen starting at (0, 0) and y
# growing downward, and why it stopped: "finished", "width" (passed maxX), "stalled" (moved
# stallX right without attention moving on) or "budget".
_SAMPLE = """
({text, style, bias, seed, budget, maxX, stallX}) => {
  window.__hwSetSeed(seed);
  document.getElementById('bias-slider').value = String(bias);   // the sampler reads this
  const enc = Y([2, ...text.split('').map(ch => ch in H ? H[ch] : 1), 3]);
  er = A(enc);
  const cols = K(enc) + 1;
  const grid = Y(10 * cols);
  for (let i = 0; i < 10; i++) for (let j = 0; j < cols; j++) grid[i * cols + j] = j - 0.5;
  const styleVec = u(m(O($.g, 64 * style, 64 * (style + 1)), $.k), $.R);
  let state = {a: $.d, b: $.o, c: $.e, d: $.m, e: $.x, f: $.a, w: $.T,
               k: Y(10), u: grid, z: styleVec};
  let prev = Y([0, 0, 1]);
  let x = 0, y = 0, reach = 0, reachX = 0, reason = "budget";
  const out = [];
  for (let i = 0; i < budget; i++) {
    const [p, stop, next] = L(prev, state);
    state = next;
    if (window.__hwAttended > reach) { reach = window.__hwAttended; reachX = x; }
    // Only believe "finished" once attention has reached the last character.
    if (stop > 0.5 && reach >= text.length) { reason = "finished"; break; }
    prev = p;
    x += p[0]; y -= p[1];
    out.push([x, y, p[2], window.__hwAttended]);
    if (maxX !== null && x > maxX) { reason = "width"; break; }
    if (stallX !== null && x - reachX > stallX) { reason = "stalled"; break; }
  }
  return {points: out, reason};
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


class Draw:
    '''One sampled run of text: points in model units plus where each word's ink ends.'''

    def __init__(self, text, points, reason, seed):
        self.text = text
        self.points = points            # [(x, y, pen_up, attended_position), ...]
        self.reason = reason            # "finished", "width", "stalled" or "budget"
        self.seed = seed
        self.word_ends = _word_ends(points, text, self.finished)

    @property
    def finished(self):
        return self.reason == "finished"

    @property
    def words(self):
        return self.text.split(" ")

    def right_edge(self, n_words):
        '''How far right the ink of the first `n_words` words reaches.'''
        end = self.word_ends[n_words - 1]
        return max(p[0] for p in self.points[:end + 1])

    def strokes(self, n_words=None):
        '''The strokes for the first `n_words` words, or the whole draw.'''
        points = self.points
        if n_words is not None:
            points = points[:self.word_ends[n_words - 1] + 1]
        return split_pen_flags(points)


def _word_ends(points, text, finished=True):
    '''
    For each word, the index of the point where the pen leaves it for good: the last pen-up
    before attention reaches the next word's first letter.

    Cutting there rather than at the first pen-up after the word is what stops a line ending
    in "I w" -- a pen-up that happens while attention is still on a short word can come after
    the next word has already begun.

    The last word of the text has no next word, so it ends where the model stopped, or, if
    it ran on instead of stopping, at the first pen-up after attention passed its last
    letter. For a draw that was stopped early, a word only counts once the next word has
    begun: attention can move on while the pen is still finishing a stroke, and a word cut
    off there would be kept without, say, the cross of its t.

    Attention is read as monotone, and steps where it falls back to the start token are
    ignored: that happens when the window is diffuse, and is noise rather than the model
    going back.
    '''
    words = text.split(" ")
    starts, i = [], 0
    for word in words:
        starts.append(i)
        i += len(word) + 1
    final_last_char = starts[-1] + len(words[-1]) - 1

    ends, last_lift, position, past_end_lift = [], None, 1, None
    for index, (_, _, pen_up, attended) in enumerate(points):
        if attended > 0:
            position = max(position, int(attended))
        char = position - 1
        while len(ends) + 1 < len(starts) and char >= starts[len(ends) + 1]:
            ends.append(last_lift if last_lift is not None else index)
        if pen_up == 1:
            last_lift = index
            if char > final_last_char and past_end_lift is None:
                past_end_lift = index

    if points and len(ends) == len(starts) - 1:
        if finished and position - 1 >= starts[-1]:
            ends.append(len(points) - 1)
        elif past_end_lift is not None:
            ends.append(past_end_lift)
    return ends


class Generator:
    '''
    A loaded browser page, ready to draw.

    Held open across a document so the 2.65 MB model is loaded once. Use it as a context
    manager:

        with Generator(style=7, seed=1) as gen:
            lines = gen.flow("some words to set".split(), width_units=400)
    '''

    def __init__(self, style=None, bias=0.75, seed=None, tries=3, headless=True):
        self.style = style
        self.bias = bias
        self.seed = seed if seed is not None else random.randrange(2 ** 31)
        self.tries = tries
        self.headless = headless
        self.x_height_units = None
        self.char_advance_units = None
        self.n_styles = None
        self._page = None
        self._draws = 0

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
        page = self._browser.new_page()
        page.add_init_script(_SEED_SCRIPT)

        # The page hard-codes http://127.0.0.1:8000/d.bin. Fulfil it from disk so the offline
        # copy needs no edits and no particular port has to be free.
        page.route("**/" + MODEL_BIN, lambda route: route.fulfill(
            status=200,
            body=model_bytes,
            headers={"Content-Type": "application/octet-stream",
                     "Access-Control-Allow-Origin": "*"}))

        page.goto(self._site.url, wait_until="domcontentloaded")
        # The weights load in animation-frame chunks; the loading indicator is removed when
        # the last one is in.
        page.wait_for_selector("#loading-indicator", state="detached",
                               timeout=LOAD_TIMEOUT_MS)
        page.evaluate(_INSTALL_ATTENTION)
        self._page = page

        self.n_styles = int(page.evaluate("() => window.$.g.length / 64"))
        if self.style is None:
            self.style = random.Random(self.seed).randrange(self.n_styles)
        elif not (0 <= self.style < self.n_styles):
            raise GenerationError(
                "style {} out of range; this model has {} styles (0-{})".format(
                    self.style, self.n_styles, self.n_styles - 1))

        known = styles.lookup(self.style)
        if known:
            self.x_height_units, self.char_advance_units = known
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

    # -- sampling -----------------------------------------------------------------------

    def draw(self, text, seed=None, max_x=None, retry=True):
        '''
        Sample `text`, re-drawing up to `tries` times if the model stalls or runs out of
        steps (unless `retry` is off, for a caller that judges the draw itself).

        With `max_x`, sampling stops once the pen passes that x; nothing beyond it is wanted.

        Each call gets its own seed derived from the generator's, so a document is
        reproducible from its seed alone.
        '''
        text = normalize_text(text)
        advance = self.char_advance_units or MODEL_CHAR_ADVANCE
        result = None
        for attempt in range(max(1, self.tries) if retry else 1):
            if seed is None:
                draw_seed = (self.seed + 7919 * self._draws) % 2 ** 32
                self._draws += 1
            else:
                draw_seed = (seed + 1_000_003 * attempt) % 2 ** 32
            raw = self._page.evaluate(_SAMPLE, {
                "text": text, "style": self.style, "bias": self.bias, "seed": draw_seed,
                "budget": STEPS_PER_CHAR_BUDGET * max(1, len(text)), "maxX": max_x,
                "stallX": STALL_CHARS * advance})
            result = Draw(text, [tuple(p) for p in raw["points"]], raw["reason"], draw_seed)
            if result.reason in ("finished", "width"):
                break
        return result

    def measure(self, samples=MEASURE_SAMPLES):
        '''
        Measure this style's x-height and character advance, in model units. This is how
        tools/build_style_table.py fills styles.json.
        '''
        heights, advances = [], []
        for i in range(samples):
            line = Line(text=CALIBRATION_TEXT,
                        strokes=self.draw(CALIBRATION_TEXT, seed=900_000 + i).strokes())
            value = estimate_x_height(line)
            if value:
                heights.append(value)
            mixed = self.draw(ADVANCE_TEXT, seed=950_000 + i)
            if mixed.word_ends:
                advances.append(max(p[0] for p in mixed.points) / len(ADVANCE_TEXT))
        self.x_height_units = statistics.median(heights) if heights else None
        self.char_advance_units = statistics.median(advances) if advances else None
        return self.x_height_units, self.char_advance_units

    # -- line filling -------------------------------------------------------------------

    def flow(self, words, width_units, progress=None):
        '''
        Set a paragraph's words into lines no wider than `width_units`.

        Each line is drawn with somewhat more words than should fit, then cut back at the end
        of the last word whose ink stays inside the width; the rest carry over to the next
        line. Because the cut is placed by measuring real ink -- attention says which points
        belong to which word -- lines fill the width without ever overrunning it, whatever
        the style's letter widths, and no character-count estimate has to be right.

        The model writes strictly left to right, so a cut-down draw is a genuine sample of
        the shorter text, not an approximation of it.
        '''
        words = [w for w in words if w]
        advance = self.char_advance_units or MODEL_CHAR_ADVANCE
        lines = []
        while words:
            line, used, advance = self._fill_line(words, width_units, advance)
            lines.append(line)
            words = words[used:]
            if progress:
                progress(line)
        return lines

    def _fill_line(self, words, width, advance):
        '''Draw one line from the front of `words`. Returns (Line, words_used, advance).'''
        overdraw = OVERDRAW
        refills = failures = 0
        best = None
        while True:
            take = _take_words(words, overdraw * width / advance)
            draw = self.draw(" ".join(take), max_x=width + OVERRUN_CHARS * advance,
                             retry=False)
            if draw.reason in ("stalled", "budget") and failures < self.tries:
                # The words before the stall are fine, but the line would come up short.
                # Keep the best attempt in case every retry stalls too.
                failures += 1
                if draw.word_ends and (best is None
                                       or len(draw.word_ends) > len(best[0].word_ends)):
                    best = (draw, take)
                continue
            if best is not None and len(best[0].word_ends) > len(draw.word_ends):
                draw, take = best
            if not draw.word_ends:
                raise GenerationError("could not draw {!r}".format(" ".join(take)))

            fits = 0
            for n in range(1, len(draw.word_ends) + 1):
                if draw.right_edge(n) <= width:
                    fits = n
                else:
                    break

            if fits == 0:
                if len(take[0]) > 1:
                    # The first word alone is wider than the line: split it and try again.
                    words[:1] = _split_word(take[0], width / draw.right_edge(1))
                    continue
                fits = 1        # a single character wider than the line; nothing to be done

            # Learn the advance from the words that fitted, so the next draw asks for a
            # better word count. Smoothed, because one line's spacing is noisy.
            measured = draw.right_edge(fits) / len(" ".join(take[:fits]))
            advance = (1 - ADVANCE_SMOOTHING) * advance + ADVANCE_SMOOTHING * measured

            if (fits == len(take) and len(words) > len(take) and refills < MAX_REFILLS
                    and len(" ".join(take)) < MAX_DRAW_CHARS):
                # Every word fitted with room to spare: too few were drawn. Ask for more.
                refills += 1
                overdraw *= 1.3
                continue
            break

        line = Line(
            text=" ".join(take[:fits]),
            strokes=draw.strokes(fits),
            style=self.style,
            bias=self.bias,
            seed=draw.seed,
            truncated=(draw.reason in ("stalled", "budget") and fits == len(draw.word_ends)
                       and fits < len(take)),
        )
        return line, fits, advance

    def document(self, paragraphs, width_units, progress=None):
        '''
        Set every paragraph, returning a Document in model units. A paragraph is a list of
        words; None marks a blank line.
        '''
        if self.x_height_units is None:
            raise GenerationError(
                "style {} is not in {}; rebuild it with tools/build_style_table.py".format(
                    self.style, styles.TABLE.name))
        document = Document(style=self.style, bias=self.bias, seed=self.seed,
                            x_height_units=self.x_height_units,
                            char_advance_units=self.char_advance_units)
        for paragraph in paragraphs:
            if paragraph is None:
                document.lines.append(Line(text="", blank=True, style=self.style,
                                           bias=self.bias))
                continue
            document.lines.extend(self.flow(paragraph, width_units, progress=progress))
        return document


def _take_words(words, char_budget):
    '''As many whole words as fit `char_budget` characters, but always at least one.'''
    char_budget = min(char_budget, MAX_DRAW_CHARS)
    take, length = [], -1
    for word in words:
        if take and length + 1 + len(word) > char_budget:
            break
        take.append(word)
        length += 1 + len(word)
    return take


def _split_word(word, fraction):
    '''Hard-split a word that is too wide for a line on its own.'''
    cut = max(1, min(len(word) - 1, int(len(word) * fraction * SPLIT_SAFETY)))
    return [word[:cut], word[cut:]]


def generate(paragraphs, width_units, style=None, bias=0.75, seed=None, tries=3,
             headless=True, progress=None):
    '''
    Generate handwriting for a list of paragraphs. A convenience wrapper around Generator.

    paragraphs   list of word lists; None produces a blank line. A plain string is taken as
                 one paragraph
    width_units  line width in model units
    style        0..79, or None to pick one at random for the whole document
    bias         legibility; the site's slider runs 0.15 (loose) to 2.5 (neat)
    seed         base seed for reproducible output; one is chosen and recorded if omitted
    tries        re-draw a line this many times if it runs out of steps
    progress     optional callback(Line)
    '''
    if isinstance(paragraphs, str):
        paragraphs = [normalize_text(paragraphs).split(" ")]
    with Generator(style=style, bias=bias, seed=seed, tries=tries,
                   headless=headless) as gen:
        warn_unsupported(gen, paragraphs)
        return gen.document(paragraphs, width_units, progress=progress)


def warn_unsupported(generator, paragraphs):
    '''The model's vocabulary lives in the page's `H` global; anything missing maps to id 1.'''
    vocab = generator.vocabulary()
    text = " ".join(" ".join(p) for p in paragraphs if p)
    bad = sorted({c for c in text if c not in vocab and c != " "})
    if bad:
        print("warning: characters not in the model's vocabulary will be drawn as garbage: "
              + " ".join(repr(c) for c in bad), file=sys.stderr)
