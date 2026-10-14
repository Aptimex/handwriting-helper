'''
The canonical intermediate representation.

A Line holds its strokes in *model units*: the pen offsets the neural net itself emits,
summed from a start at (0, 0). x=0 is the line's starting edge, y=0 is roughly its baseline,
and y grows downward (SVG convention). Nothing in this space depends on the line's length --
a 4-character line and a 100-character line come back the same size, which is what makes
stacking them look right.

See MODEL_* below for the constants that tie model units to millimetres.
'''

import json
import math
import re
from dataclasses import dataclass, field, asdict

# The site estimates a line's width as 8.2 model units per character. Only a fallback: the
# real figure runs from about 7 to 8.5 by style, and is looked up per style (styles.json).
MODEL_CHAR_ADVANCE = 8.2

# x-height letters only, with the connectors that cursive actually draws. Used as the
# calibration line: its measured height is what --x-height-mm is scaled against.
CALIBRATION_TEXT = "nouncesomuxnecoumsanueco"

# Sampling steps allowed per character before a draw is abandoned as stuck. Real text takes
# 20-27; the site allows 40.
STEPS_PER_CHAR_BUDGET = 50

# The longest single draw. The site's 50-character input cap is a UI limit, not a model one:
# 180-character draws sample cleanly in every style tried. This bounds the overdraw on very
# wide lines rather than anything the model needs.
MAX_DRAW_CHARS = 160

STROKES_FORMAT_VERSION = 2


def normalize_text(text):
    '''Exactly what the site does to the input before encoding it: Z.trim().replace(/\\s+/g," ")'''
    return re.sub(r"\s+", " ", text.strip())


@dataclass
class Line:
    '''One generated line of handwriting, in model units.'''
    text: str
    strokes: list = field(default_factory=list)   # [[(x, y), ...], ...]
    style: int = None
    bias: float = None
    seed: int = None                              # the draw's seed
    truncated: bool = False                       # ran out of steps before its last word
    blank: bool = False

    @property
    def points(self):
        for stroke in self.strokes:
            for p in stroke:
                yield p

    def bbox(self):
        '''(xmin, ymin, xmax, ymax) in model units, or None for a blank line.'''
        pts = list(self.points)
        if not pts:
            return None
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return min(xs), min(ys), max(xs), max(ys)

    def height(self):
        '''Ink height in model units.'''
        box = self.bbox()
        return 0.0 if box is None else box[3] - box[1]

    def width(self):
        box = self.bbox()
        return 0.0 if box is None else box[2] - box[0]

    def point_count(self):
        return sum(len(s) for s in self.strokes)


@dataclass
class Document:
    '''A generated document: the lines, plus the settings that produced them.'''
    lines: list = field(default_factory=list)
    style: int = None
    bias: float = None
    seed: int = None
    x_height_units: float = None       # measured; the anchor for converting to millimetres
    char_advance_units: float = None   # measured; how wide a character runs, for page fitting
    source: str = "calligrapher.ai (offline copy)"
    version: int = STROKES_FORMAT_VERSION

    def point_count(self):
        return sum(l.point_count() for l in self.lines)

    def stroke_count(self):
        return sum(len(l.strokes) for l in self.lines)

    def to_json(self, indent=1):
        d = {
            "version": self.version,
            "source": self.source,
            "units": "model",
            "style": self.style,
            "bias": self.bias,
            "seed": self.seed,
            "x_height_units": self.x_height_units,
            "char_advance_units": self.char_advance_units,
            "lines": [asdict(l) for l in self.lines],
        }
        return json.dumps(d, indent=indent)

    @classmethod
    def from_json(cls, text):
        d = json.loads(text)
        version = d.get("version")
        if version != STROKES_FORMAT_VERSION:
            raise ValueError(
                "unsupported strokes file version {!r} (this build writes version {})".format(
                    version, STROKES_FORMAT_VERSION))
        lines = []
        for ld in d["lines"]:
            ld = dict(ld)
            ld["strokes"] = [[tuple(p) for p in s] for s in ld.get("strokes", [])]
            lines.append(Line(**ld))
        return cls(lines=lines, style=d.get("style"), bias=d.get("bias"),
                   seed=d.get("seed"), x_height_units=d.get("x_height_units"),
                   char_advance_units=d.get("char_advance_units"),
                   source=d.get("source", ""), version=version)


def split_pen_flags(points):
    '''
    [(x, y, pen_up, ...), ...] -> [[(x, y), ...], ...]. A point with pen_up set is the last
    point of its stroke. Single-point strokes are dropped: they leave no ink.
    '''
    strokes, current = [], []
    for p in points:
        current.append((p[0], p[1]))
        if p[2] == 1:
            if len(current) >= 2:
                strokes.append(current)
            current = []
    if len(current) >= 2:
        strokes.append(current)
    return strokes


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

# Width of the window used to measure x-height, in model units: about eight characters.
# Wide enough to hold several letters, narrow enough that the baseline is straight across it.
X_HEIGHT_WINDOW = 8 * MODEL_CHAR_ADVANCE


def estimate_x_height(line):
    '''
    Robust x-height of a line, in model units.

    Handwriting drifts off the horizontal, so the raw vertical spread of a line overstates its
    letter height. Removing a straight baseline fit is not enough either: real drift is not
    linear, so the leftover wander grows with line length, and measuring a whole line makes a
    45-character sample read about 28% taller than a 9-character one even though the letters
    are identical in size. So the line is measured in fixed-width windows -- de-trended and
    banded within each, then medianed -- which is length-independent by construction. Across
    9, 19 and 45-character samples that holds to within 4%.
    '''
    import numpy as np

    pts = sorted(line.points, key=lambda p: p[0])
    if len(pts) < 10:
        return None
    xs = np.array([p[0] for p in pts], dtype=float)
    ys = np.array([p[1] for p in pts], dtype=float)
    span = xs.max() - xs.min()
    if span < 1e-9:
        return None

    windows = max(1, int(round(span / X_HEIGHT_WINDOW)))
    edges = np.linspace(xs.min(), xs.max(), windows + 1)
    bands = []
    for lo, hi in zip(edges, edges[1:]):
        mask = (xs >= lo) & (xs <= hi)
        if mask.sum() < 30:
            continue
        wx, wy = xs[mask], ys[mask]
        residual = wy - np.polyval(np.polyfit(wx, wy, 1), wx)
        bands.append(np.percentile(residual, 98) - np.percentile(residual, 2))

    if not bands:
        # Too little ink to window; fall back to measuring the whole thing.
        residual = ys - np.polyval(np.polyfit(xs, ys, 1), xs)
        return float(np.percentile(residual, 98) - np.percentile(residual, 2))
    return float(np.median(bands))


def simplify(stroke, tolerance):
    '''
    Ramer-Douglas-Peucker. The model emits very dense points (~40 per character), which
    bloats the G-code and can starve the motion planner's lookahead; dropping points that
    sit within `tolerance` of the line they span is visually free.
    '''
    if tolerance <= 0 or len(stroke) < 3:
        return list(stroke)

    keep = [False] * len(stroke)
    keep[0] = keep[-1] = True
    stack = [(0, len(stroke) - 1)]
    while stack:
        lo, hi = stack.pop()
        if hi - lo < 2:
            continue
        ax, ay = stroke[lo]
        bx, by = stroke[hi]
        dx, dy = bx - ax, by - ay
        span = math.hypot(dx, dy)
        worst, worst_i = -1.0, None
        for i in range(lo + 1, hi):
            px, py = stroke[i]
            if span == 0:
                dist = math.hypot(px - ax, py - ay)
            else:
                dist = abs(dy * (px - ax) - dx * (py - ay)) / span
            if dist > worst:
                worst, worst_i = dist, i
        if worst > tolerance:
            keep[worst_i] = True
            stack.append((lo, worst_i))
            stack.append((worst_i, hi))
    return [p for p, k in zip(stroke, keep) if k]


def reflow(text):
    '''
    Join each paragraph onto one line, so the wrapper decides where lines break.

    Without this, text that arrives already hard-wrapped gets re-wrapped line by line, and
    every original line leaves a short orphan behind. Blank lines still separate paragraphs.
    '''
    paragraphs = []
    current = []
    for raw in text.splitlines():
        if raw.strip():
            current.append(raw.strip())
        elif current:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    # A blank line between paragraphs survives, so it still becomes a gap in the layout.
    return "\n\n".join(paragraphs)


def paragraphs(text, reflow_text=False):
    '''
    Split input text into paragraphs of words, with None for each blank line between them.

    By default every input line is its own paragraph, so a line break in the input is a line
    break on the page. With reflow_text, single line breaks are joined and only blank lines
    separate paragraphs -- for text that arrives already hard-wrapped, which would otherwise
    leave a short orphan behind at every original break. Lines that are too wide for the
    page are wrapped either way.
    '''
    source = reflow(text) if reflow_text else text
    out = []
    for raw in source.splitlines():
        words = normalize_text(raw)
        out.append(words.split(" ") if words else None)
    while out and out[0] is None:
        out.pop(0)
    while out and out[-1] is None:
        out.pop()
    return out
