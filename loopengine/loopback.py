"""Measure the output stage instead of trusting the backend's arithmetic.

PortAudio's reported stream latency is not a measurement. On this machine it
is `max(one block, the device's advertised low-latency figure rounded up to
whole blocks) / rate`, which at 256 frames / 44100 Hz collapses to exactly one
block period — i.e. it restates the block size and says nothing about the
device. Proven by opening the stream at several sizes and watching the
reported figure track blocksize/rate exactly.

So: emit a chirp at a known output frame, record simultaneously on a duplex
stream, and cross-correlate to find it. The frame delta is the round trip
through output and input together.

What this measures, and what it does not
----------------------------------------
If the delta lands on exact multiples of the hardware period with no spread
across repeats, the path is digital — a monitor or a graph loop — and the
number is GRAPH latency. It does not include the DAC's own analog conversion,
a cable, or air. An acoustic path shows jitter and a non-integer air delay
instead; `classify()` reports which one it saw rather than assuming.

One direction is reported as half the round trip. That assumes the input and
output stages are symmetric, which is the usual assumption for a duplex path
on one device but is an assumption, not a measurement, and is labelled so.
"""
from __future__ import annotations

import time

import numpy as np

DEFAULT_REPS = 9
WARMUP = 2


def _chirp(sr, ms=5.0):
    n = int(sr * ms / 1000.0)
    t = np.arange(n) / sr
    f0, f1 = 500.0, 8000.0
    sig = np.sin(2 * np.pi * (f0 * (f1 / f0) ** (t / t[-1])) * t)
    return (sig * np.hanning(n)).astype(np.float32)


def round_trip(samplerate=48000, blocksize=256, reps=DEFAULT_REPS, device=None):
    """-> dict. Raises RuntimeError if no duplex stream or nothing comes back."""
    import sounddevice as sd

    sr, bs = int(samplerate), int(blocksize)
    chirp = _chirp(sr)
    cl = chirp.size
    n = int(sr * 1.2)
    deltas, rms_seen, conf = [], [], []
    reported = None

    for r in range(reps):
        emit = int(sr * (0.35 + 0.03 * (r % 5)))
        played = np.zeros(n, dtype=np.float32)
        played[emit:emit + cl] = chirp
        rec = np.zeros((n, 2), dtype=np.float32)
        idx = {"n": 0}

        def cb(indata, outdata, frames, tinfo, status):
            i = idx["n"]
            j = min(i + frames, n)
            outdata[:] = 0
            if i < n:
                outdata[:j - i, 0] = played[i:j]
                outdata[:j - i, 1] = played[i:j]
                rec[i:j] = indata[:j - i]
            idx["n"] = i + frames

        try:
            s = sd.Stream(samplerate=sr, blocksize=bs, channels=(2, 2),
                          dtype="float32", callback=cb, latency="low",
                          device=device)
            s.start()
            deadline = time.monotonic() + 5.0
            while idx["n"] < n and time.monotonic() < deadline:
                time.sleep(0.02)
            s.stop()
            reported = s.latency
            s.close()
        except Exception as e:
            raise RuntimeError("no duplex stream on this device: %s" % e)

        mic = rec.mean(axis=1)
        rms_seen.append(float(np.sqrt((mic ** 2).mean())))
        xc = np.abs(np.correlate(mic - mic.mean(), chirp, mode="valid"))
        if r >= WARMUP:                       # the first opens are not settled
            deltas.append(int(np.argmax(xc)) - emit)
            conf.append(float(xc.max() / max(np.median(xc), 1e-12)))
        time.sleep(0.15)

    if max(rms_seen) < 1e-5:
        raise RuntimeError("the input is silent — no monitor source and no "
                           "live microphone, so no loopback is possible")

    d = np.array(deltas, dtype=float)
    med = np.median(d)
    keep = d[np.abs(d - med) < max(0.05 * abs(med), 32)]   # drop startup flyers
    if keep.size < 3:
        raise RuntimeError("repeats did not agree: %s frames" % sorted(d.astype(int)))

    rt_ms = float(keep.mean()) / sr * 1000.0
    spread = float(keep.max() - keep.min()) / sr * 1000.0
    return {
        "samplerate": sr,
        "blocksize": bs,
        "round_trip_ms": round(rt_ms, 4),
        "one_way_ms": round(rt_ms / 2.0, 4),
        "frames": sorted(int(x) for x in set(keep)),
        "reps_agreed": int(keep.size),
        "reps_run": int(d.size),
        "spread_ms": round(spread, 4),
        "confidence": round(float(np.mean(conf)), 1),
        "reported_in_ms": round(reported[0] * 1000.0, 4) if reported else None,
        "reported_out_ms": round(reported[1] * 1000.0, 4) if reported else None,
        "path": classify(keep, spread, sr),
        "assumes": "input and output stages are symmetric",
    }


def classify(frames, spread_ms, sr, hw_period=1024):
    """Digital paths land on exact period multiples and do not jitter."""
    exact = all(abs(f % hw_period) < 2 or abs(hw_period - f % hw_period) < 2
                for f in frames)
    if spread_ms < 0.2 and exact:
        return ("graph (digital monitor loop): no DAC, no cable, no air — "
                "excludes the analog conversion stage")
    if spread_ms < 0.2:
        return ("digital, but not aligned to the hardware period — treat the "
                "figure as graph latency")
    return ("acoustic or jittering: subtract 2.9 ms per metre of air before "
            "comparing with a digital figure")
