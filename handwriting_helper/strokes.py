'''
The canonical intermediate representation.

A Line holds its strokes in *model units*: the coordinate space the neural net itself
works in, recovered by dividing out the per-line pixel scale that calligrapher.ai applies.
In model units x=0 is the line's left starting edge, y=0 is its baseline, and y grows
downward (SVG convention). Because the scale has been divided out, a 4-character line and a
50-character line come back the same size -- which is what makes stacking them look right.

See MODEL_* below for the constants that tie model units to millimetres.
'''

import json
import math
import re
from dataclasses import dataclass, field, asdict

# The site estimates a line's width as 8.2 * charCount * scale, so one character advances
# ~8.2 model units. Used to place the starting pen position, same as the site does.
MODEL_CHAR_ADVANCE = 8.2

# Fallback x-height in model units, used only when calibration is skipped. It is a poor
# substitute: the styles differ in size by nearly 4x (style 7 measures 3.2 units, style 44
# measures 12.8), so the real value is measured per run by Generator.calibrate(). This is the
# median over a sample of styles.
MODEL_X_HEIGHT = 4.8

# x-height letters only, with the connectors that cursive actually draws. Used as the
# calibration line: its measured height is what --x-height-mm is scaled against.
CALIBRATION_TEXT = "nouncesomuxnecoumsanueco"

# The site's sampler gives up after 40 steps per character; hitting that means the line
# was cut off rather than finished.
STEPS_PER_CHAR_BUDGET = 40

# The site's text input is capped at 50 characters. That caps one *draw*, not one line of
# output: a longer line is drawn as several chunks and joined (see generate.Generator.line).
MAX_DRAW_CHARS = 50

# Backwards-compatible alias.
MAX_LINE_CHARS = MAX_DRAW_CHARS

# Width of the gap left at a chunk seam, as a fraction of a character advance. Calibrated by
# generating texts whole and in two halves and solving for the gap that makes the widths
# match: the median came out at 5.9 units against the site's nominal 8.2-unit advance. It is
# a ratio rather than a constant because the measured advance runs from 7.5 to 9.8 units
# depending on the style. The spread is wide either way, because this model's word spacing is
# itself random -- a seam is never more wrong than an ordinary word gap.
MODEL_WORD_GAP_RATIO = 0.72

# Fallback seam width in model units, for when no advance has been measured.
MODEL_WORD_GAP = MODEL_WORD_GAP_RATIO * MODEL_CHAR_ADVANCE

STROKES_FORMAT_VERSION = 1


def normalize_text(text):
    '''Exactly what the site does to the input before encoding it: Z.trim().replace(/\\s+/g," ")'''
    return re.sub(r"\s+", " ", text.strip())


@dataclass
class Line:
    '''One generated line of handwriting, in model units.'''
    text: str
    strokes: list = field(default_factory=list)   # [[(x, y), ...], ...]
    chunks: list = None                           # draw texts, when the line needed several
    style: int = None
    bias: float = None
    seed: int = None
    scale_px_per_unit: float = None               # the site's `t` for this line
    truncated: bool = False
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
        '''Ink height in model units. The finished form of callig2svg.py's getHeight().'''
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


# ---------------------------------------------------------------------------
# Raw point-array parsing (the old manual workflow's input format)
# ---------------------------------------------------------------------------

def json2points(raw):
    '''
    Parse the JSON produced by `JSON.stringify(tr)` in the browser console.

    JSON.stringify on a Float32Array emits an object with string numeric keys rather than an
    array, which is why the values are read as point['0'] / ['1'] / ['2']. Plain arrays are
    accepted too, since that is what the automated generator produces.

    Returns [[{'x': float, 'y': float}, ...], ...] -- one list per continuous stroke, split
    on the pen-up flag. Single-point strokes are dropped; the very first point of a run is
    the seed position and carries flag 1, so this discards it as a side effect.
    '''
    if isinstance(raw, (str, bytes)):
        points = json.loads(raw)
    else:
        points = raw

    segments = []
    segment = []
    for point in points:
        if isinstance(point, dict):
            x, y, end = point["0"], point["1"], point["2"]
        else:
            x, y, end = point[0], point[1], point[2]
        segment.append({"x": float(x), "y": float(y)})
        if end == 1:
            if len(segment) >= 2:
                segments.append(segment)
            segment = []
    if len(segment) >= 2:
        segments.append(segment)
    return segments


def moveToOrigin(segments):
    '''
    Shift every point so the ink's top-left corner sits at (0, 0), removing surrounding
    whitespace. Kept for the legacy single-line path; the layout engine deliberately does
    NOT use this, because cropping to the ink bbox destroys baseline alignment between lines.
    '''
    if not segments:
        return segments
    minX = min(p["x"] for s in segments for p in s)
    minY = min(p["y"] for s in segments for p in s)
    return [[{"x": p["x"] - minX, "y": p["y"] - minY} for p in s] for s in segments]


def getHeight(segments):
    '''Distance between the highest and lowest points of a legacy segment list.'''
    if not segments:
        return 0.0
    ys = [p["y"] for s in segments for p in s]
    return max(ys) - min(ys)


def segments_to_strokes(segments):
    '''Legacy [{'x','y'}] segments -> [(x, y)] strokes.'''
    return [[(p["x"], p["y"]) for p in s] for s in segments]


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


def split_chunks(text, limit=MAX_DRAW_CHARS):
    '''
    Split one line into draws of at most `limit` characters, breaking between words.

    The seams land where the model lifts the pen anyway, which is why joining chunks reads as
    one line while joining individual words does not -- a word generated alone has no
    neighbours to lead into and comes out visibly pasted on.
    '''
    words = normalize_text(text).split(" ")
    chunks = []
    current = ""
    for word in words:
        while len(word) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(word[:limit])
            word = word[limit:]
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= limit:
            current += " " + word
        else:
            chunks.append(current)
            current = word
    if current:
        chunks.append(current)
    return chunks


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


def wrap_text(text, width=MAX_LINE_CHARS):
    '''
    Break text into lines of at most `width` characters, on word boundaries, hard-splitting
    any single word that is longer than the limit. Blank input lines are preserved as blank
    output lines so they become vertical gaps in the layout.

    `width` may exceed the 50-character draw limit; the generator splits each line into draws
    itself. Line length is a page decision, not a model one.
    '''
    out = []
    for raw_line in text.splitlines():
        stripped = normalize_text(raw_line)
        if not stripped:
            out.append("")
            continue
        current = ""
        for word in stripped.split(" "):
            while len(word) > width:
                if current:
                    out.append(current)
                    current = ""
                out.append(word[:width])
                word = word[width:]
            if not current:
                current = word
            elif len(current) + 1 + len(word) <= width:
                current += " " + word
            else:
                out.append(current)
                current = word
        if current:
            out.append(current)
    while out and out[-1] == "":
        out.pop()
    return out
