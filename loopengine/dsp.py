"""Interpolating gather kernels and offline analysis. numpy only.

Everything here is vectorised over a block. The audio callback never touches a
Python loop over samples. Gathers write through `out=` into preallocated
workspace so the hot path allocates a handful of small temporaries, not MBs.
"""
from __future__ import annotations

import numpy as np

TWO_PI = 2.0 * np.pi


class Work:
    """Preallocated scratch for one voice at one channel count."""

    __slots__ = ("n", "ch", "k", "p", "y", "acc")

    def __init__(self, n: int, ch: int):
        self.n = n
        self.ch = ch
        self.k = np.arange(n, dtype=np.float64)
        self.p = [np.zeros((n, ch), dtype=np.float32) for _ in range(4)]
        self.y = np.zeros((n, ch), dtype=np.float32)
        self.acc = np.zeros((n, 2), dtype=np.float32)


def _catmull(p0, p1, p2, p3, t, out):
    """4-point Catmull-Rom. out = f(t) with t in [0,1) between p1 and p2."""
    a = 0.5 * (p3 - p0) + 1.5 * (p1 - p2)
    b = p0 - 2.5 * p1 + 2.0 * p2 - 0.5 * p3
    c = 0.5 * (p2 - p0)
    np.multiply(a, t, out=out)
    out += b
    out *= t
    out += c
    out *= t
    out += p1
    return out


def loop_gather(buf, ls: int, L: int, phase: float, step: float, n: int, w: Work):
    """`n` interpolated frames from `phase`, wrapped inside [ls, ls+L).

    `step` may be negative (reverse) or fractional (varispeed / SR conversion).
    Neighbour taps are wrapped inside the loop too, so the interpolator never
    reads across the seam into unrelated material.
    """
    rel = w.k[:n] * step + (phase - ls)
    np.mod(rel, L, out=rel)                       # [0, L)
    i0 = rel.astype(np.int64)
    t = (rel - i0).astype(np.float32)[:, None]

    p = w.p
    np.take(buf, ls + np.mod(i0 - 1, L), axis=0, out=p[0][:n], mode="clip")
    np.take(buf, ls + i0,                axis=0, out=p[1][:n], mode="clip")
    np.take(buf, ls + np.mod(i0 + 1, L), axis=0, out=p[2][:n], mode="clip")
    np.take(buf, ls + np.mod(i0 + 2, L), axis=0, out=p[3][:n], mode="clip")

    y = w.y[:n]
    _catmull(p[0][:n], p[1][:n], p[2][:n], p[3][:n], t, y)
    return y, rel


def seam_blend(buf, y, rel, ls: int, L: int, xf: int, n_frames: int, w: Work):
    """Equal-power crossfade over the loop seam.

    For the first `xf` samples after the wrap we mix in the material that
    *would* have played had the loop kept running (ls+L+rel). Loop length is
    preserved — this is a post-fade, not a shortened loop.
    """
    if xf <= 0:
        return y
    m = rel < xf
    cnt = int(m.sum())
    if cnt == 0:
        return y
    tail = (ls + L + rel[m]).astype(np.int64)
    np.clip(tail, 0, n_frames - 1, out=tail)
    cont = buf[tail]                              # linear is fine over 5ms
    ph = (rel[m] / xf).astype(np.float32)[:, None] * (np.pi / 2.0)
    y[m] = y[m] * np.sin(ph) + cont * np.cos(ph)
    return y


def shot_gather(buf, start: int, phase: float, step: float, n: int, w: Work):
    """Non-looping interpolated gather, clamped at the buffer edges."""
    pos = w.k[:n] * step + phase
    i0 = np.floor(pos).astype(np.int64)
    t = (pos - i0).astype(np.float32)[:, None]
    hi = buf.shape[0] - 1
    p = w.p
    np.take(buf, np.clip(i0 - 1, 0, hi), axis=0, out=p[0][:n], mode="clip")
    np.take(buf, np.clip(i0,     0, hi), axis=0, out=p[1][:n], mode="clip")
    np.take(buf, np.clip(i0 + 1, 0, hi), axis=0, out=p[2][:n], mode="clip")
    np.take(buf, np.clip(i0 + 2, 0, hi), axis=0, out=p[3][:n], mode="clip")
    y = w.y[:n]
    _catmull(p[0][:n], p[1][:n], p[2][:n], p[3][:n], t, y)
    return y


def pan_gains(pan: float):
    """Constant-power pan. pan in [-1, 1]."""
    a = (float(pan) + 1.0) * 0.25 * np.pi
    return float(np.cos(a)), float(np.sin(a))


# --------------------------------------------------------------------------
# Offline analysis. Runs on the loader thread, never in the callback.
# --------------------------------------------------------------------------

def peaks(buf: np.ndarray, buckets: int = 2048) -> np.ndarray:
    """min/max envelope, shape (buckets, 2), float32 in [-1, 1]."""
    mono = buf.mean(axis=1) if buf.ndim > 1 and buf.shape[1] > 1 else buf[:, 0]
    n = mono.shape[0]
    if n == 0:
        return np.zeros((buckets, 2), dtype=np.float32)
    hop = max(1, n // buckets)
    usable = (n // hop) * hop
    v = mono[:usable].reshape(-1, hop)
    out = np.zeros((v.shape[0], 2), dtype=np.float32)
    out[:, 0] = v.min(axis=1)
    out[:, 1] = v.max(axis=1)
    return out


# Frames folded to mono per pass in peaks_range. A wide request re-buckets in
# groups of about this size, so it never allocates the whole span at once.
RANGE_CHUNK = 1 << 20


def _mono(x: np.ndarray) -> np.ndarray:
    """The same mix peaks() plots: the channel mean, or the only channel."""
    return x.mean(axis=1) if x.ndim > 1 and x.shape[1] > 1 else x[:, 0]


def peaks_range(buf: np.ndarray, start: int, end: int, buckets: int):
    """The envelope of [start, end) at exactly `buckets` buckets, for a zoomed view.

    peaks() is taken once per load over the whole file. Stretched over a short
    span its buckets are wider than the pixels showing them — a blocky outline
    of the wrong material, which looks like it works. This re-buckets from the
    buffer the server already holds, over the visible span only.

    Bucket k covers [start + k*span//buckets, start + (k+1)*span//buckets), so
    every sample lands in exactly one bucket and none is dropped. (peaks()
    rounds its hop down and discards up to hop-1 samples at the end of the
    file.) When the span divides evenly the result is exactly
    peaks(buf[start:end], buckets).

    Returns ("env", float32 (buckets, 2)) of min/max, or — once the span holds
    no more samples than there are buckets — ("samples", float32 (span,)): the
    min and max of one sample are that sample, and the line through the
    samples is the thing worth looking at.
    """
    n = buf.shape[0]
    start = max(0, min(int(start), n))
    end = max(start, min(int(end), n))
    buckets = max(1, int(buckets))
    span = end - start
    if span == 0:
        return "env", np.zeros((0, 2), dtype=np.float32)
    if span <= buckets:
        return "samples", np.ascontiguousarray(_mono(buf[start:end]), dtype=np.float32)

    # span > buckets, so consecutive edges differ by at least one frame
    edges = start + (np.arange(buckets + 1, dtype=np.int64) * span) // buckets
    out = np.empty((buckets, 2), dtype=np.float32)
    k = 0
    while k < buckets:
        s0 = int(edges[k])
        j = int(np.searchsorted(edges, s0 + RANGE_CHUNK, side="right")) - 1
        j = min(max(j, k + 1), buckets)       # one bucket wider than a chunk: take it whole
        mono = _mono(buf[s0:int(edges[j])])
        idx = edges[k:j] - s0
        out[k:j, 0] = np.minimum.reduceat(mono, idx)
        out[k:j, 1] = np.maximum.reduceat(mono, idx)
        k = j
    return "env", out


def onset_envelope(buf: np.ndarray, sr: int, win: int = 1024, hop: int = 256,
                   max_bins: int = 16384):
    """Spectral flux. Returns (env, hop) — half-wave rectified magnitude rise.

    The hop widens for long files so the envelope length is bounded. At a
    fixed hop a four-minute track produced a 45000-bin envelope and 1.8 s of
    work, paid twice because bpm and slicing each called this separately.
    """
    mono = buf.mean(axis=1) if buf.ndim > 1 and buf.shape[1] > 1 else buf[:, 0]
    n = mono.shape[0]
    if n < win * 2:
        return np.zeros(1, dtype=np.float32), hop
    frames = 1 + (n - win) // hop
    if frames > max_bins:
        hop = int(np.ceil((n - win) / float(max_bins)))
        frames = 1 + (n - win) // hop
    idx = np.arange(win)[None, :] + hop * np.arange(frames)[:, None]
    w = np.hanning(win).astype(np.float32)
    mag = np.abs(np.fft.rfft(mono[idx] * w, axis=1)).astype(np.float32)
    d = np.diff(mag, axis=0)
    np.maximum(d, 0.0, out=d)
    env = d.sum(axis=1)
    mx = float(env.max()) if env.size else 0.0
    if mx > 0:
        env /= mx
    return env, hop


def slice_points(buf: np.ndarray, sr: int, want: int = 16):
    """Transient slice points in samples. Falls back to equal division.

    Returns (points, method) where method is 'transient' or 'equal'.
    """
    n = buf.shape[0]
    env, hop = onset_envelope(buf, sr)
    if env.size > 8:
        k = 9
        pad = np.pad(env, (k // 2, k // 2), mode="edge")
        base = np.median(np.lib.stride_tricks.sliding_window_view(pad, k), axis=1)
        thr = base + 0.08
        cand = np.flatnonzero((env > thr) & (env >= np.roll(env, 1)) &
                              (env >= np.roll(env, -1)))
        min_gap = int(0.045 * sr / hop)
        picked = []
        for c in cand:
            if not picked or c - picked[-1] >= min_gap:
                picked.append(int(c))
        if len(picked) >= max(4, want // 4):
            pts = (np.array(picked, dtype=np.int64) * hop)
            if pts[0] > sr // 100:
                pts = np.concatenate([[0], pts])
            if len(pts) > want:
                # keep the `want` strongest onsets, then re-sort by time
                strength = env[np.clip(pts // hop, 0, env.size - 1)]
                keep = np.sort(np.argsort(strength)[-want:])
                pts = pts[keep]
            return pts.astype(np.int64), "transient"
    step = max(1, n // want)
    return (np.arange(want, dtype=np.int64) * step), "equal"


def estimate_bpm(buf: np.ndarray, sr: int, lo: float = 70.0, hi: float = 180.0,
                 max_seconds: float = 30.0):
    """Length-first, autocorrelation second. Returns (bpm, confidence 0..1).

    Only the first `max_seconds` are analysed when the fallback is needed.
    Tempo does not become more certain with more material, and the
    autocorrelation was quadratic in envelope length: a four-minute track cost
    4.7 s, nearly all of it in np.correlate over 45000 bins.
    """
    n = buf.shape[0]
    dur = n / float(sr)
    if dur <= 0.05:
        return 0.0, 0.0

    # A loop is almost always a whole number of bars. Try that first.
    best = None
    for beats in (1, 2, 4, 8, 12, 16, 24, 32, 64):
        bpm = 60.0 * beats / dur
        if lo <= bpm <= hi:
            err = abs(bpm - round(bpm))
            if best is None or err < best[1]:
                best = (bpm, err)
    if best is not None and best[1] < 0.12:
        return round(best[0], 2), 0.92

    window = buf[:int(max_seconds * sr)] if dur > max_seconds else buf
    env, hop = onset_envelope(window, sr)
    if env.size < 16:
        return (round(best[0], 2), 0.4) if best else (0.0, 0.0)
    env = env - env.mean()
    ac = np.correlate(env, env, mode="full")[env.size - 1:]
    lag_lo = int(60.0 / hi * sr / hop)
    lag_hi = min(int(60.0 / lo * sr / hop), ac.size - 1)
    if lag_hi <= lag_lo:
        return (round(best[0], 2), 0.4) if best else (0.0, 0.0)
    seg = ac[lag_lo:lag_hi]
    lag = int(np.argmax(seg)) + lag_lo
    peak = float(seg.max())
    total = float(np.abs(ac[1:]).sum()) or 1.0
    bpm = 60.0 * sr / (lag * hop)
    return round(bpm, 2), float(min(1.0, peak / total * 12.0))


def center_extract(buf: np.ndarray, sr: int, mode: str) -> np.ndarray:
    """Cheap channel-domain source shaping. Not source separation — say so.

    'ctr'  mid (L+R)/2, band-limited to 180 Hz .. 8 kHz where a lead vocal sits,
           sides kept outside that band so the loop still has bottom and air.
    'side' (L-R)/2 — everything panned wide. Kills most centred vocals.
    """
    if buf.ndim < 2 or buf.shape[1] < 2:
        return buf
    L = buf[:, 0].astype(np.float32)
    R = buf[:, 1].astype(np.float32)
    mid = (L + R) * 0.5
    side = (L - R) * 0.5
    if mode == "side":
        return np.stack([side, -side], axis=1).astype(np.float32)

    n = buf.shape[0]
    f = np.fft.rfftfreq(n, 1.0 / sr)
    band = ((f >= 180.0) & (f <= 8000.0)).astype(np.float32)
    # 1/6-octave raised-cosine edges so we don't ring on the transients
    for edge, width, rising in ((180.0, 90.0, True), (8000.0, 2500.0, False)):
        sel = np.abs(f - edge) < width
        x = (f[sel] - (edge - width)) / (2 * width)
        band[sel] = x if rising else (1.0 - x)
    mid_b = np.fft.irfft(np.fft.rfft(mid) * band, n=n).astype(np.float32)
    side_b = np.fft.irfft(np.fft.rfft(side) * (1.0 - band), n=n).astype(np.float32)
    out = np.stack([mid_b + side_b, mid_b - side_b], axis=1)
    return np.ascontiguousarray(out, dtype=np.float32)
