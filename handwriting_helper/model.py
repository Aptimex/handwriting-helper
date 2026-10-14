'''
calligrapher.ai's handwriting model, ported from the page's JavaScript to numpy.

The page's network is hand-written vanilla JS over Float32Arrays, and this is a line-for-line
port of it, so the browser is not needed to run it. The single-letter names in comments are
the page's own (`A` the encoder, `F` one recurrent step, `k` attention, `U` sampling), which
is what to grep for in website/Calligrapher.ai.htm when comparing the two.

The JS stores every intermediate vector in a Float32Array but does its arithmetic in doubles,
and so does this: sums and transcendental functions are evaluated in float64 and rounded to
float32 wherever the page would store the result. Column sums, which the page accumulates
straight into a Float32Array, are accumulated in float32 in the same order. Math.random is
replaced by the same mulberry32 the browser backend installs, called in the same order, so a
seed gives the same draw on either backend to within the odd last-bit difference between
V8's and libm's exp/log -- which matters only when two mixture components are nearly tied.
'''

import math
import struct
from pathlib import Path
from decimal import ROUND_HALF_UP, Decimal

import numpy as np

F32 = np.float32
F64 = np.float64

# s(t, X): the residual connections are scaled by sqrt(1/2).
_X = math.sqrt(0.5)

# The page's character table `H`. 0 is padding, 1 unknown, 2 and 3 start and end of text.
VOCABULARY = {
    "": 0, "\x01": 1, "\x02": 2, "\x03": 3,
    " ": 8, "!": 72, '"': 4, "#": 56, "&": 84, "'": 16, "(": 66, ")": 67, "*": 80,
    "+": 82, ",": 37, "-": 40, ".": 7, "/": 77,
    "0": 62, "1": 59, "2": 63, "3": 69, "4": 68, "5": 61, "6": 71, "7": 70, "8": 76,
    "9": 60, ":": 74, ";": 73, "?": 51,
    "A": 9, "B": 47, "C": 57, "D": 52, "E": 42, "F": 53, "G": 45, "H": 41, "I": 23,
    "J": 64, "K": 58, "L": 48, "M": 5, "N": 38, "O": 36, "P": 46, "Q": 75, "R": 55,
    "S": 18, "T": 31, "U": 65, "V": 39, "W": 54, "X": 79, "Y": 50, "Z": 78,
    "[": 81, "]": 83,
    "a": 14, "b": 32, "c": 20, "d": 27, "e": 19, "f": 35, "g": 33, "h": 30, "i": 13,
    "j": 43, "k": 28, "l": 26, "m": 12, "n": 15, "o": 25, "p": 29, "q": 49, "r": 6,
    "s": 17, "t": 21, "u": 11, "v": 34, "w": 24, "x": 44, "y": 22, "z": 10,
}

# The page reads the bias from an <input type="range" min="0.15" max="2.5" step="0.02">,
# which clamps and snaps whatever it is given.
BIAS_MIN, BIAS_MAX, BIAS_STEP = Decimal("0.15"), Decimal("2.5"), Decimal("0.02")

# Weights stored sparse in d.bin, kept as matrices; the page multiplies them in CSR form.
_SPARSE = ("y", "w", "r", "l")

STYLE_SIZE = 64
N_MIXTURES = 20
N_WINDOWS = 10


class ModelError(RuntimeError):
    pass


def snap_bias(bias):
    '''The value the page's bias slider would hold after being set to `bias`.'''
    value = min(max(Decimal(repr(float(bias))), BIAS_MIN), BIAS_MAX)
    steps = ((value - BIAS_MIN) / BIAS_STEP).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return float(min(BIAS_MIN + steps * BIAS_STEP, BIAS_MAX))


class Mulberry32:
    '''The seeded Math.random the browser backend installs in the page.'''

    def __init__(self, seed):
        self.state = seed & 0xFFFFFFFF

    def __call__(self):
        s = self.state = (self.state + 0x6D2B79F5) & 0xFFFFFFFF
        t = ((s ^ (s >> 15)) * (s | 1)) & 0xFFFFFFFF
        t ^= (t + ((t ^ (t >> 7)) * (t | 61))) & 0xFFFFFFFF
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296


def load_weights(path):
    '''
    Parse d.bin into {name: float32 array}. Each record is: name length (u8), name, a
    "compressed" flag (u8), value count (u32), float32 values, then if compressed one byte
    per value giving the gap to the previous non-zero entry, then rank (u8) and shape (u16s).
    '''
    data = memoryview(Path(path).read_bytes())
    weights, pos = {}, 0
    while pos < len(data):
        n = data[pos]
        name = bytes(data[pos + 1:pos + 1 + n]).decode("latin-1")
        pos += 1 + n
        compressed = data[pos]
        count, = struct.unpack_from("<I", data, pos + 1)
        pos += 5
        values = np.frombuffer(data, "<f4", count, pos).astype(F32)
        pos += 4 * count
        if compressed:
            # The page reads these with getUint16 into a Uint8Array but advances one byte,
            # so each gap is simply the byte at that position.
            gaps = np.frombuffer(data, np.uint8, count, pos).astype(np.int64)
            pos += count
        rank = data[pos]
        shape = struct.unpack_from("<{}H".format(rank), data, pos + 1)
        pos += 1 + 2 * rank
        if compressed or name in _SPARSE:
            dense = np.zeros(int(np.prod(shape)), F32)
            dense[np.cumsum(gaps)] = values
            values = dense
        weights[name] = values.reshape(shape) if rank > 1 else values
    return weights


# -- the page's elementwise helpers, rounded the way it rounds them ----------------------

def _f32(x):
    return np.asarray(x, F64).astype(F32)


def _sigmoid(x):                                    # o
    with np.errstate(over="ignore"):
        return _f32(1 / (1 + np.exp(-x.astype(F64))))


def _softplus(x):                                   # n
    with np.errstate(over="ignore"):
        return _f32(np.log(1 + np.exp(x.astype(F64))))


def _tanh(x):                                       # v, written as (e^2x - 1)/(e^2x + 1)
    with np.errstate(over="ignore", invalid="ignore"):
        e = np.exp(2 * x.astype(F64))
        return _f32((e - 1) / (e + 1))


def _softmax(x):                                    # d, with no max subtracted
    e = _f32(np.exp(x.astype(F64))).astype(F64)
    total = 0.0
    for v in e.tolist():
        total += v
    return _f32(e / total)


def _log(v):
    '''Math.log, which gives -Infinity for 0 where math.log raises.'''
    return math.log(v) if v > 0 else -math.inf


def _scale(x, factor):                              # s(x, scalar)
    return _f32(x.astype(F64) * factor)


class Model:
    '''The loaded network, ready to sample from.'''

    def __init__(self, path):
        try:
            w = load_weights(path)
        except (OSError, struct.error, ValueError) as exc:
            raise ModelError("could not read {}: {}".format(path, exc)) from exc
        self.n_styles = w["g"].size // STYLE_SIZE
        self.vocabulary = set(VOCABULARY)
        # Dense matrices are row-major (inputs x outputs) and used as x @ W. The sparse ones
        # are (outputs x inputs), so they are transposed to match.
        self.embed = w["s"].reshape(-1, 256)
        self.conv = w["b"].reshape(768, 256).astype(F64)
        self.conv_b = w["t"]
        self.enc = w["j"].reshape(512, 256).astype(F64)
        self.enc_b = w["E"]
        self.inp = w["i"].reshape(3, 256).astype(F64)
        self.inp_b = w["W"]
        self.style_w = w["k"].reshape(STYLE_SIZE, 256).astype(F64)
        self.style_b = w["R"]
        self.styles = w["g"].reshape(-1, STYLE_SIZE)
        self.lstm = [(w["y"].T.astype(F64), w["p"]),
                     (w["w"].T.astype(F64), w["q"]),
                     (w["r"].T.astype(F64), w["f"])]
        self.att = w["h"].reshape(256, 3 * N_WINDOWS).astype(F64)
        self.att_b = w["n"]
        self.mix = w["l"].T.astype(F64)
        self.mix_b = w["Q"]
        self.stop = w["c"].reshape(256, 1).astype(F64)
        self.stop_b = w["u"]
        self.out = w["z"].reshape(256, 6 * N_MIXTURES + 1).astype(F64)
        self.out_b = w["v"]
        # Initial cell states, hidden states and attention window.
        self.init_cells = [w["d"], w["o"], w["e"]]
        self.init_hidden = [w["m"], w["x"], w["a"]]
        self.init_window = w["T"]

    # -- the network ----------------------------------------------------------------------

    def encode(self, ids):                          # A
        emb = self.embed[[0, *ids, 0]]
        windows = np.concatenate([emb[:-2], emb[1:-1], emb[2:]], axis=1)
        conv = _tanh(_f32(windows.astype(F64) @ self.conv) + self.conv_b)
        feats = np.concatenate([emb[1:-1], conv], axis=1)
        return _f32(feats.astype(F64) @ self.enc) + self.enc_b

    def style_vector(self, style):
        return _f32(self.styles[style].astype(F64) @ self.style_w) + self.style_b

    def _lstm(self, layer, x, state):               # C
        weights, bias = self.lstm[layer]
        r = np.concatenate([x, state.hidden[layer]])
        i, j, f, o = np.split(_f32(r.astype(F64) @ weights) + bias, 4)
        cell = _sigmoid(f) * state.cells[layer] + _sigmoid(i) * _tanh(j)
        hidden = _sigmoid(o) * _tanh(cell)
        state.cells[layer], state.hidden[layer] = cell, hidden
        return hidden

    def _attend(self, x, state):                    # k
        al, bl, kl = np.split(_f32(x.astype(F64) @ self.att) + self.att_b, 3)
        beta, a = _softplus(bl), _softmax(al)
        state.kappa = state.kappa + _softplus(kl) / F32(15)
        edges = _sigmoid((state.grid - state.kappa[:, None]) / beta[:, None])
        phi = (a[:, None] * (edges[:, 1:] - edges[:, :-1])).sum(axis=0)
        state.window = (phi[:, None] * state.encoded).sum(axis=0)
        state.attended = self._attended(a, beta, state.kappa, state.grid.shape[1])
        return state.window

    @staticmethod
    def _attended(a, beta, kappa, width):
        '''
        Which grid position the window weighs most -- the character being written. The
        browser backend computes the same thing, in doubles, from a hook on `k`.
        '''
        pos = np.arange(width, dtype=F64)[None, :]
        a, beta, kappa = (v.astype(F64)[:, None] for v in (a, beta, kappa))
        sig = lambda z: 1 / (1 + np.exp(-z))                                    # noqa: E731
        with np.errstate(over="ignore"):
            phi = (a * (sig((pos + 0.5 - kappa) / beta)
                        - sig((pos - 0.5 - kappa) / beta))).sum(0)
        return int(np.argmax(phi))

    def _step(self, prev, state):                   # F
        t = _f32(prev.astype(F64) @ self.inp) + self.inp_b
        t = _scale(t + state.style, _X)
        t = _scale(t + self._lstm(0, t, state), _X)
        h = self._lstm(1, np.concatenate([t, state.window]), state)
        window = self._attend(h, state)
        f = _tanh(_f32(np.concatenate([h, window]).astype(F64) @ self.mix) + self.mix_b)
        t = _scale(t + f, _X)
        stop = _sigmoid(_f32(window.astype(F64) @ self.stop) + self.stop_b)
        t = _scale(t + self._lstm(2, t, state), _X)
        return _f32(t.astype(F64) @ self.out) + self.out_b, float(stop[0])

    @staticmethod
    def _sample_point(params, bias, rand):          # U
        pen_p = float(_sigmoid(params[120:121])[0])
        pen = 1 if rand() < pen_p else 0
        p = params[:120].reshape(N_MIXTURES, 6)
        logits, sigma, rho, mu = p[:, 0], p[:, 1:3], _tanh(p[:, 3]), p[:, 4:6]
        sigma = _f32(_softplus(sigma).astype(F64) / math.exp(bias))
        logits = _f32(np.log(_softmax(logits).astype(F64)))
        logits = _f32(logits.astype(F64) * (1 + bias))
        logits = np.where(logits.astype(F64) < math.log(0.02), logits - F32(100), logits)
        best, pick = -1e6, 0
        for i, v in enumerate(logits.tolist()):      # Gumbel-max
            score = v + -_log(-_log(rand()))
            if score > best:
                best, pick = score, i
        sx, sy = float(sigma[pick, 0]), float(sigma[pick, 1])
        r = float(rho[pick])
        cov = _f32([sx, r * sy, 0.0, sy * math.sqrt(1 - r * r)])
        normal = []
        for _ in range(2):                           # Box-Muller
            u1, u2 = 1 - rand(), 1 - rand()
            normal.append(math.sqrt(-2 * math.log(u1)) * math.cos(2 * math.pi * u2))
        z = _f32(normal).astype(F64)
        offset = _f32([z[0] * cov[0] + z[1] * cov[2], z[0] * cov[1] + z[1] * cov[3]])
        dx, dy = (mu[pick] + offset).tolist()
        return np.array([dx, dy, pen], F32)

    # -- one draw -------------------------------------------------------------------------

    def sample(self, text, style, bias, seed, budget, max_x=None, stall_x=None):
        '''
        Draw `text`. Returns ([(x, y, pen_up, attended_position), ...], reason) exactly as
        the browser backend's sampling loop does; see generate._SAMPLE.
        '''
        rand = Mulberry32(seed)
        bias = snap_bias(bias)
        ids = [2, *(VOCABULARY.get(ch, 1) for ch in text), 3]
        state = _State(self, self.encode(ids), self.style_vector(style))

        prev = np.array([0, 0, 1], F32)
        x = y = 0.0
        reach, reach_x, reason = 0, 0.0, "budget"
        points = []
        for _ in range(budget):
            params, stop = self._step(prev, state)
            p = self._sample_point(params, bias, rand)
            if state.attended > reach:
                reach, reach_x = state.attended, x
            # Only believe "finished" once attention has reached the last character.
            if stop > 0.5 and reach >= len(text):
                reason = "finished"
                break
            prev = p
            x += float(p[0])
            y -= float(p[1])
            points.append((x, y, int(p[2]), state.attended))
            if max_x is not None and x > max_x:
                reason = "width"
                break
            if stall_x is not None and x - reach_x > stall_x:
                reason = "stalled"
                break
        return points, reason


class _State:
    '''The page's per-draw state object: LSTM cells and outputs, attention window and grid.'''

    def __init__(self, model, encoded, style):
        self.cells = list(model.init_cells)
        self.hidden = list(model.init_hidden)
        self.window = model.init_window
        self.kappa = np.zeros(N_WINDOWS, F32)
        cols = len(encoded) + 1
        self.grid = np.tile(np.arange(cols, dtype=F32) - F32(0.5), (N_WINDOWS, 1))
        self.encoded = encoded
        self.style = style
        self.attended = 0
