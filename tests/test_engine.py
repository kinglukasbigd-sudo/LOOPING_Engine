"""Checks that matter: seam continuity, quantiser accuracy, rate conversion.

    PYTHONPATH=.pylibs python3 -m tests.test_engine
"""
from __future__ import annotations

import math
import sys

import numpy as np

from loopengine import dsp
from loopengine.engine import Engine
from loopengine.transport import QUANTA

SR = 48000
FAILS = []


def check(name, ok, detail=""):
    print("%-46s %s%s" % (name, "PASS" if ok else "FAIL",
                          ("  " + detail) if detail else ""))
    if not ok:
        FAILS.append(name)


def sine(f, n, sr=SR, ch=2):
    t = np.arange(n) / sr
    y = np.sin(2 * np.pi * f * t).astype(np.float32)
    return np.stack([y] * ch, axis=1)


# --------------------------------------------------------------------------
def t_seam_continuity():
    """A loop whose length is a whole number of cycles must not click."""
    f, n = 200.0, SR
    buf = sine(f, n)
    e = Engine(samplerate=SR, blocksize=256, offline=True)
    cycle = SR / f                                  # 240 samples
    ls, le = 0, int(cycle * 100)                    # exactly 100 cycles
    e.post("track.load", i=0, buf=buf, sr=SR, name="sine", path="",
           bpm=0.0, conf=0.0, slices=np.zeros(0, dtype=np.int64))
    e.post("track.loop", i=0, ls=ls, le=le)
    e.post("track.gain", i=0, v=1.0)
    e.post("master.gain", v=1.0)
    e.post("track.play", i=0)
    e.render_offline(4096)                          # settle the gain ramp
    y = e.render_offline(int(cycle * 250))[:, 0]
    d = np.abs(np.diff(y))
    expected = 2 * math.pi * f / SR                 # max slope of the sine
    check("loop seam has no step discontinuity",
          d.max() < expected * 1.6,
          "max step %.5f vs %.5f/sample" % (d.max(), expected))


def t_seam_crossfade_rescues_bad_loop():
    """A loop cut mid-cycle clicks; the crossfade must take the edge off."""
    buf = sine(200.0, SR)
    out = {}
    for xf_ms in (0.0, 8.0):
        e = Engine(samplerate=SR, blocksize=256, offline=True)
        e.post("track.load", i=0, buf=buf, sr=SR, name="s", path="", bpm=0.0,
               conf=0.0, slices=np.zeros(0, dtype=np.int64))
        e.post("track.loop", i=0, ls=1000, le=1000 + 24010)   # deliberate ugly cut
        e.post("track.xfade", i=0, v=xf_ms)
        e.post("track.gain", i=0, v=1.0)
        e.post("master.gain", v=1.0)
        e.post("track.play", i=0)
        e.render_offline(8192)
        y = e.render_offline(24010 * 3)[:, 0]
        out[xf_ms] = float(np.abs(np.diff(y)).max())
    check("crossfade reduces the seam step",
          out[8.0] < out[0.0] * 0.6,
          "0ms %.4f -> 8ms %.4f" % (out[0.0], out[8.0]))


def t_quantise_lands_on_the_bar():
    """A queued start must begin within one sample of the bar line."""
    bpm = 124.0
    spb = 60.0 / bpm * SR
    bar = spb * 4
    buf = np.ones((SR, 2), dtype=np.float32) * 0.5   # DC, so onset is obvious
    e = Engine(samplerate=SR, blocksize=256, offline=True)
    e.post("track.load", i=0, buf=buf, sr=SR, name="dc", path="", bpm=0.0,
           conf=0.0, slices=np.zeros(0, dtype=np.int64))
    e.post("track.gain", i=0, v=1.0)
    e.post("track.xfade", i=0, v=0.0)
    e.post("master.gain", v=1.0)
    e.post("transport.bpm", v=bpm)
    e.post("transport.quantum", v=QUANTA.index(4.0))   # BAR
    e.post("transport.start")
    e.render_offline(1000)                             # roll for a bit
    start_pos = e.transport.pos
    e.post("track.play", i=0)
    y = e.render_offline(int(bar * 2))[:, 0]
    # first non-zero sample, not first *audible* one — every start gets a
    # 6 ms anti-click fade, so a level threshold would measure the fade.
    onset = int(np.argmax(np.abs(y) > 1e-7))
    want = int(math.ceil(start_pos / bar) * bar) - start_pos
    check("queued start lands on the bar line",
          abs(onset - want) <= 2,
          "onset +%d, bar line +%d" % (onset, want))


def t_quantum_off_is_immediate():
    buf = np.ones((SR, 2), dtype=np.float32) * 0.5
    e = Engine(samplerate=SR, blocksize=256, offline=True)
    e.post("track.load", i=0, buf=buf, sr=SR, name="dc", path="", bpm=0.0,
           conf=0.0, slices=np.zeros(0, dtype=np.int64))
    e.post("track.gain", i=0, v=1.0)
    e.post("master.gain", v=1.0)
    e.post("transport.quantum", v=0)
    e.post("transport.start")
    e.render_offline(512)
    e.post("track.play", i=0)
    y = e.render_offline(512)[:, 0]
    check("quantum OFF fires inside the same block", float(y.max()) > 0.02,
          "peak %.3f" % y.max())


def t_rate_conversion():
    """A 44.1k loop on a 48k engine must keep its own pitch and length."""
    f, sr_file = 441.0, 44100
    buf = sine(f, sr_file, sr=sr_file)          # exactly 1.0 s, 441 cycles
    e = Engine(samplerate=48000, blocksize=256, offline=True)
    e.post("track.load", i=0, buf=buf, sr=sr_file, name="s", path="",
           bpm=0.0, conf=0.0, slices=np.zeros(0, dtype=np.int64))
    e.post("track.gain", i=0, v=1.0)
    e.post("master.gain", v=1.0)
    e.post("track.play", i=0)
    e.render_offline(4096)
    y = e.render_offline(48000)[:, 0]
    spec = np.abs(np.fft.rfft(y * np.hanning(y.size)))
    peak_hz = np.fft.rfftfreq(y.size, 1 / 48000)[int(np.argmax(spec))]
    check("44.1k file keeps its pitch on a 48k device",
          abs(peak_hz - f) < 2.0, "peak %.1f Hz, want %.1f Hz" % (peak_hz, f))


def t_reverse():
    n = 20000
    ramp = np.linspace(0, 1, n, dtype=np.float32)
    buf = np.stack([ramp, ramp], axis=1)
    e = Engine(samplerate=SR, blocksize=256, offline=True)
    e.post("track.load", i=0, buf=buf, sr=SR, name="ramp", path="", bpm=0.0,
           conf=0.0, slices=np.zeros(0, dtype=np.int64))
    e.post("track.gain", i=0, v=1.0)
    e.post("master.gain", v=1.0)
    e.post("track.xfade", i=0, v=0.0)
    e.post("track.play", i=0)
    e.post("track.rev", i=0)
    e.render_offline(4096)
    y = e.render_offline(4096)[:, 0]
    check("reverse plays the buffer backwards",
          np.polyfit(np.arange(y.size), y, 1)[0] < 0,
          "slope %.2e" % np.polyfit(np.arange(y.size), y, 1)[0])


def t_pads_overlap():
    buf = sine(440.0, SR)
    e = Engine(samplerate=SR, blocksize=256, offline=True, n_voices=8)
    slices = np.arange(16, dtype=np.int64) * (SR // 16)
    e.post("track.load", i=0, buf=buf, sr=SR, name="s", path="", bpm=0.0,
           conf=0.0, slices=slices)
    for k in range(4):
        e.post("pad.assign", i=k, track=0, slice=k, mode="ONE", gain=0.3)
    e.render_offline(512)
    for k in range(4):
        e.post("pad.trigger", i=k)
    e.render_offline(256)
    check("four pads sound at once", e.voices.used() == 4,
          "%d voices" % e.voices.used())
    e.post("panic")
    e.render_offline(256)
    check("panic stops every voice", e.voices.used() == 0)


def t_limiter():
    """Eight tracks at full tilt must not leave the rails."""
    buf = sine(300.0, SR) * 0.95
    e = Engine(samplerate=SR, blocksize=256, offline=True)
    for i in range(8):
        e.post("track.load", i=i, buf=buf, sr=SR, name="s", path="", bpm=0.0,
               conf=0.0, slices=np.zeros(0, dtype=np.int64))
        e.post("track.gain", i=i, v=1.4)
        e.post("track.play", i=i)
    e.post("master.gain", v=1.2)
    e.render_offline(8192)
    y = e.render_offline(8192)
    check("soft clip holds the output inside 0 dBFS",
          float(np.abs(y).max()) <= 1.0, "peak %.4f" % np.abs(y).max())


def t_analysis():
    from loopengine.demo import kick, SR as DSR
    k = kick().astype(np.float32)
    bpm, conf = dsp.estimate_bpm(k, DSR)
    check("bpm of the 2-bar 124 kit reads back as 124",
          abs(bpm - 124.0) < 0.5, "%.2f (conf %.2f)" % (bpm, conf))
    pts, method = dsp.slice_points(k, DSR, 16)
    check("kick slices onto transients", method == "transient" and pts.size >= 4,
          "%d points, %s" % (pts.size, method))
    st = dsp.center_extract(sine(500.0, 4800), 48000, "side")
    check("side extraction nulls a centred signal",
          float(np.abs(st).max()) < 1e-6, "residual %.2e" % np.abs(st).max())


def t_no_nans():
    from loopengine.demo import stabs
    buf = stabs().astype(np.float32)
    e = Engine(samplerate=SR, blocksize=256, offline=True)
    e.post("track.load", i=0, buf=buf, sr=SR, name="s", path="", bpm=124.0,
           conf=0.9, slices=np.arange(8, dtype=np.int64) * 20000)
    e.post("track.play", i=0)
    for v in (0.25, 4.0, 1.0):
        e.post("track.speed", i=0, v=v)
        e.render_offline(2048)
    e.post("track.loop", i=0, ls=100, le=170)     # shorter than the crossfade
    y = e.render_offline(4096)
    check("extreme speed and tiny loops stay finite",
          bool(np.isfinite(y).all()), "")


def t_latency_budget():
    """The command path must cost about half a block on average, not more.

    A press arrives at an arbitrary moment inside a callback period, so the
    wait to be picked up is uniform over that period: mean ~= half a block,
    worst case ~= one block plus scheduler jitter. If either drifts, something
    started blocking the audio thread.
    """
    import random
    import time as _t
    e = Engine(samplerate=SR, blocksize=256, offline=True).start()
    _t.sleep(0.25)
    block_ms = 256 / SR * 1000.0
    random.seed(7)
    for i in range(200):
        e.post("track.gain", i=0, v=0.5)
        _t.sleep(random.uniform(0.0, block_ms * 3 / 1000.0))
    lat = e.latency()
    v = e._lat[:e._lat_n].copy()
    out_live = lat["output_ms"]
    e.stop()
    after_stop = e.latency()["output_ms"]

    check("queue wait averages about half a callback period",
          block_ms * 0.3 < v.mean() < block_ms * 0.7,
          "mean %.3f ms of a %.3f ms block" % (v.mean(), block_ms))
    check("queue wait stays inside two callback periods",
          float(v.max()) < block_ms * 2.0,
          "max %.3f ms" % v.max())
    check("the reported path survives the stream closing",
          after_stop == out_live and out_live > 0,
          "%.3f ms live, %.3f ms after stop" % (out_live, after_stop))
    fixed = lat["block_ms"] + lat["output_ms"]
    check("the p95 total adds up",
          abs(lat["press_to_speaker_p95_ms"] - (lat["queue_p95_ms"] + fixed)) < 0.02,
          "%.2f ms" % lat["press_to_speaker_p95_ms"])
    check("the mean total adds up",
          abs(lat["press_to_speaker_mean_ms"] - (lat["queue_mean_ms"] + fixed)) < 0.02,
          "%.2f ms" % lat["press_to_speaker_mean_ms"])
    check("the two totals are published separately and differ",
          lat["press_to_speaker_p95_ms"] > lat["press_to_speaker_mean_ms"],
          "mean %.2f < p95 %.2f" % (lat["press_to_speaker_mean_ms"],
                                    lat["press_to_speaker_p95_ms"]))
    check("an unmeasured output stage says so rather than looking measured",
          lat["output_ms_measured"] is None
          and "NOT a measurement" in lat["output_ms_source"]
          and lat["output_ms"] == lat["output_ms_reported"],
          lat["output_ms_source"][:38])


def t_measured_output_overrides_the_reported_one():
    """Once a loopback figure exists it must win everywhere, and say so."""
    e = Engine(samplerate=SR, blocksize=256, offline=True).start()
    import time as _t
    _t.sleep(0.2)
    before = e.latency()
    e.output_ms_measured = 9.174
    after = e.latency()
    snap = e.snapshot()
    e.stop()
    check("the reported figure is kept under its own name",
          after["output_ms_reported"] == before["output_ms_reported"],
          "%.3f ms" % after["output_ms_reported"])
    check("the measured figure becomes authoritative",
          after["output_ms"] == 9.174 and "measured" in after["output_ms_source"],
          after["output_ms_source"])
    check("the totals move with it",
          abs(after["press_to_speaker_p95_ms"] - before["press_to_speaker_p95_ms"]
              - (9.174 - before["output_ms_reported"])) < 0.02)
    check("the panel is told which it is",
          snap["latency_measured"] is True and abs(snap["latency_ms"] - 9.174) < 0.01,
          "latency_ms %.2f measured=%s" % (snap["latency_ms"], snap["latency_measured"]))


def t_capture_is_preallocated_and_records():
    """The diagnostic must not cause the fault it is there to measure.

    Appending each block to a list allocates ~2 kB per callback on the audio
    thread. Measured on real hardware: 0 xruns from that alone and 0 from
    decode work alone, but 3 over 7605 blocks with both — the capture
    producing the underrun it was recording. And rebinding _callback after
    start() records nothing, because PortAudio holds the bound method it was
    constructed with.
    """
    e = Engine(samplerate=SR, blocksize=256, offline=True)
    buf = e.capture(0.5)
    check("capture allocates its whole buffer up front",
          isinstance(buf, np.ndarray) and buf.shape[0] >= SR // 2,
          "%s" % (buf.shape,))
    before = buf.__array_interface__["data"][0]
    e.post("track.load", i=0, buf=np.ones((SR, 2), dtype=np.float32) * 0.4,
           sr=SR, name="dc", path="", bpm=0.0, conf=0.0,
           slices=np.zeros(0, dtype=np.int64))
    e.post("track.gain", i=0, v=1.0)
    e.post("master.gain", v=1.0)
    e.post("track.play", i=0)
    e.render_offline(4096)
    got = e.end_capture()
    check("the callback wrote into that same buffer, not a new one",
          buf.__array_interface__["data"][0] == before)
    check("it recorded what the callback produced",
          got is not None and got.shape[0] == 4096
          and float(np.abs(got).max()) > 0.05,
          "%d frames, peak %.3f" % (got.shape[0], float(np.abs(got).max())))
    check("ending the capture releases it",
          e.end_capture() is None and e._cap_buf is None)


def t_xruns_are_timestamped_not_just_counted():
    """A count cannot tell warm-up from periodic from correlated."""
    e = Engine(samplerate=SR, blocksize=256, offline=True).start()
    import time as _t
    _t.sleep(0.2)
    check("a clean run reports no xruns at all",
          e.xrun_report() == [], "%d entries" % len(e.xrun_report()))

    class Flags:                       # what PortAudio hands the callback
        output_underflow = True
        output_overflow = False
        priming_output = False
        def __bool__(self): return True
    out = np.zeros((256, 2), dtype=np.float32)
    e._callback(out, 256, None, Flags())
    rep = e.xrun_report()
    e.stop()
    check("an xrun records when it happened and what it was",
          len(rep) == 1 and rep[0]["underflow"] is True
          and rep[0]["priming"] is False and rep[0]["t_s"] >= 0.0,
          str(rep[0]) if rep else "nothing recorded")
    check("and which block it landed on", rep and rep[0]["block"] >= 0)


def t_probe_reaches_the_audio_thread():
    """A probe id must come back only after the callback itself has seen it."""
    e = Engine(samplerate=SR, blocksize=256, offline=True)
    check("probe id starts unset", e.snapshot()["probe_id"] == 0)
    e.post("probe", id=4242)
    check("a posted probe is not acknowledged before the callback runs",
          e.snapshot()["probe_id"] == 0)
    e.render_offline(256)
    check("the callback acknowledges the probe",
          e.snapshot()["probe_id"] == 4242,
          "got %s" % e.snapshot()["probe_id"])


if __name__ == "__main__":
    for fn in (t_seam_continuity, t_seam_crossfade_rescues_bad_loop,
               t_quantise_lands_on_the_bar, t_quantum_off_is_immediate,
               t_rate_conversion, t_reverse, t_pads_overlap, t_limiter,
               t_analysis, t_no_nans, t_latency_budget,
               t_measured_output_overrides_the_reported_one,
               t_capture_is_preallocated_and_records,
               t_xruns_are_timestamped_not_just_counted,
               t_probe_reaches_the_audio_thread):
        try:
            fn()
        except Exception as exc:
            check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    print("\n%d checks failed" % len(FAILS) if FAILS else "\nall checks passed")
    sys.exit(1 if FAILS else 0)
