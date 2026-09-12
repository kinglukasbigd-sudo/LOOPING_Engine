"""Synthesised test kit — 2 bars at 124 BPM, Fm.

This is generated with numpy, not sampled from anything. It exists so the
engine makes noise the first time you run it, before you have pointed it at
your own files. Written as 16-bit WAV through the stdlib so it needs nothing.
"""
from __future__ import annotations

import os
import wave

import numpy as np

BPM = 124.0
SR = 48000
BARS = 2
BEATS = BARS * 4
DUR = BEATS * 60.0 / BPM


def _t(n):
    return np.arange(n, dtype=np.float64) / SR


def _lowpass(x, cutoff, sr=SR, order=2.0):
    """Zero-phase brick-ish lowpass in the frequency domain. Offline only."""
    n = x.shape[0]
    f = np.fft.rfftfreq(n, 1.0 / sr)
    h = 1.0 / (1.0 + (f / max(1.0, cutoff)) ** (2 * order))
    return np.fft.irfft(np.fft.rfft(x) * h, n=n)


def _highpass(x, cutoff, sr=SR, order=2.0):
    n = x.shape[0]
    f = np.fft.rfftfreq(n, 1.0 / sr)
    h = 1.0 - 1.0 / (1.0 + (f / max(1.0, cutoff)) ** (2 * order))
    return np.fft.irfft(np.fft.rfft(x) * h, n=n)


def _place(dst, src, at):
    a = int(at)
    b = min(dst.shape[0], a + src.shape[0])
    if a < dst.shape[0]:
        dst[a:b] += src[:b - a]


def _beat(i):
    return int(i * 60.0 / BPM * SR)


def kick():
    n = int(DUR * SR)
    out = np.zeros(n)
    hit_n = int(0.42 * SR)
    t = _t(hit_n)
    pitch = 45.0 + 95.0 * np.exp(-t / 0.028)
    body = np.sin(2 * np.pi * np.cumsum(pitch) / SR) * np.exp(-t / 0.16)
    click = np.exp(-t / 0.0022) * 0.55
    hit = (body + click)
    for b in (0, 1, 2, 3, 4, 5, 6, 7):
        _place(out, hit * (1.0 if b % 2 == 0 else 0.62), _beat(b))
    _place(out, hit * 0.4, _beat(3.75))
    _place(out, hit * 0.4, _beat(7.5))
    return np.stack([out, out], axis=1)


def hats():
    n = int(DUR * SR)
    out = np.zeros(n)
    rng = np.random.default_rng(1404)
    closed_n = int(0.06 * SR)
    open_n = int(0.30 * SR)
    tc, to = _t(closed_n), _t(open_n)
    closed = _highpass(rng.standard_normal(closed_n), 7000) * np.exp(-tc / 0.014)
    opn = _highpass(rng.standard_normal(open_n), 6000) * np.exp(-to / 0.11)
    for s in range(BEATS * 4):
        pos = _beat(s / 4.0)
        if s % 8 == 6:
            _place(out, opn * 0.5, pos)
        else:
            _place(out, closed * (0.9 if s % 4 == 0 else 0.45), pos)
    return np.stack([out * 0.85, out], axis=1)


def bass():
    n = int(DUR * SR)
    out = np.zeros(n)
    prog = [(0, 3.0, 43.65), (3, 1.0, 51.91), (4, 2.0, 43.65),
            (6, 1.0, 65.41), (7, 1.0, 58.27)]
    for start, length, f in prog:
        ln = int(length * 60.0 / BPM * SR)
        t = _t(ln)
        ph = 2 * np.pi * f * t
        saw = 2.0 * (ph / (2 * np.pi) % 1.0) - 1.0
        sub = np.sin(ph)
        env = np.minimum(1.0, t / 0.006) * np.exp(-t / (length * 0.55))
        # filter sweep: crossfade a dark and a bright pass with the decay
        sweep = np.exp(-t / 0.09)
        dark, bright = _lowpass(saw, 320.0), _lowpass(saw, 1600.0)
        v = (dark * (1 - sweep) + bright * sweep) * 0.6 + sub * 0.8
        _place(out, v * env, _beat(start))
    return np.stack([out, out], axis=1)


def keys():
    n = int(DUR * SR)
    l = np.zeros(n)
    r = np.zeros(n)
    t = _t(n)
    # Fm9 — F3 Ab3 C4 Eb4 G4
    for k, f in enumerate((174.61, 207.65, 261.63, 311.13, 392.00)):
        det = 1.0 + (k - 2) * 0.0016
        env = np.minimum(1.0, t / 0.45) * (0.55 + 0.45 * np.sin(2 * np.pi * 0.5 * t))
        v = (np.sin(2 * np.pi * f * det * t) * 0.6 +
             np.sin(2 * np.pi * f * det * 2 * t) * 0.16 +
             np.sin(2 * np.pi * f * det * 3 * t) * 0.07) * env / 5.0
        pan = (k - 2) / 4.0
        l += v * np.cos((pan + 1) * np.pi / 4)
        r += v * np.sin((pan + 1) * np.pi / 4)
    fade = np.minimum(1.0, np.minimum(t / 0.2, (DUR - t) / 0.2))
    return np.stack([_lowpass(l, 3200) * fade, _lowpass(r, 3200) * fade], axis=1)


def stabs():
    """Short chord hits on an off-grid pattern. Made to be sliced onto pads."""
    n = int(DUR * SR)
    out = np.zeros(n)
    hit_n = int(0.34 * SR)
    t = _t(hit_n)
    voicings = [(349.23, 415.30, 523.25), (311.13, 392.00, 466.16),
                (349.23, 440.00, 523.25), (261.63, 349.23, 415.30)]
    for k, pos in enumerate([0, 1.5, 2.75, 4, 5.5, 6.25, 7, 7.75]):
        chord = np.zeros(hit_n)
        for f in voicings[k % len(voicings)]:
            ph = 2 * np.pi * f * t
            chord += 2.0 * (ph / (2 * np.pi) % 1.0) - 1.0
        env = np.exp(-t / (0.05 + 0.05 * (k % 3)))
        _place(out, _lowpass(chord / 3.0, 2600) * env * 0.8, _beat(pos))
    return np.stack([out, out * 0.92], axis=1)


KIT = [
    ("tk_kick_124.wav", kick),
    ("tk_hats_124.wav", hats),
    ("tk_bass_Fm_124.wav", bass),
    ("tk_keys_Fm9_124.wav", keys),
    ("tk_stab_124.wav", stabs),
]


def write_wav(path, data, sr=SR):
    peak = float(np.abs(data).max()) or 1.0
    pcm = np.clip(data / peak * 0.89, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(pcm.shape[1])
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return path


def build(outdir: str, force: bool = False):
    os.makedirs(outdir, exist_ok=True)
    paths = []
    for name, fn in KIT:
        p = os.path.join(outdir, name)
        if force or not os.path.isfile(p):
            write_wav(p, fn())
        paths.append(p)
    return paths


if __name__ == "__main__":
    import sys
    d = sys.argv[1] if len(sys.argv) > 1 else "kits/testkit-124"
    for p in build(d, force=True):
        print(p)
