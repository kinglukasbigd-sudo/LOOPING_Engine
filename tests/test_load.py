"""Staged loading: playable before analysed, and analysis stays display-only.

    PYTHONPATH=.pylibs python3 -m tests.test_load
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np

from loopengine import dsp
from loopengine.demo import write_wav
from loopengine.engine import Engine
from loopengine.loader import Loader

SR = 48000
FAILS = []


def check(name, ok, detail=""):
    print("%-58s %s%s" % (name, "PASS" if ok else "FAIL",
                          ("  " + detail) if detail else ""))
    if not ok:
        FAILS.append(name)


def long_file(seconds=120, path="/tmp/le-long-test.wav"):
    n = SR * seconds
    t = (np.sin(2 * np.pi * 220 * np.arange(n) / SR) * 0.3
         + np.random.default_rng(1).standard_normal(n) * 0.05).astype(np.float32)
    write_wav(path, np.stack([t, t], axis=1), SR)
    return path


def t_playable_before_analysed():
    path = long_file()
    e = Engine(samplerate=SR, blocksize=256, offline=True).start()
    stages, t0 = [], time.perf_counter()
    L = Loader(e, on_done=lambda i, info: stages.append(
        (info.get("stage"), (time.perf_counter() - t0) * 1000)))
    L.load_async(0, path, 16)
    playable_ms = None
    while len(stages) < 3 and time.perf_counter() - t0 < 40:
        e.render_offline(256)
        if playable_ms is None and e.tracks[0].src is not None:
            playable_ms = (time.perf_counter() - t0) * 1000
        time.sleep(0.005)
    # the third callback fires when the loader POSTS track.analysis; the
    # engine applies it on the next callback, so drain before asserting
    e.render_offline(2048)
    t = e.tracks[0]
    analysed_ms = dict(stages).get("analysed", 1e9)
    e.stop()
    os.remove(path)

    check("the stages arrive in order",
          [s for s, _ in stages] == ["playable", "waveform", "analysed"],
          str([s for s, _ in stages]))
    check("the track is playable well before analysis finishes",
          playable_ms is not None and playable_ms < analysed_ms * 0.5,
          "playable %.0f ms, analysed %.0f ms" % (playable_ms or -1, analysed_ms))
    check("it arrives looping the whole file",
          (t.loop_start, t.loop_end) == (0, t.frames),
          "%s of %d" % ((t.loop_start, t.loop_end), t.frames))
    check("analysis filled in afterwards without moving the loop",
          t.bpm > 0 and t.slices.size > 0
          and (t.loop_start, t.loop_end) == (0, t.frames),
          "bpm %.1f, %d slices" % (t.bpm, t.slices.size))
    check("and the analysing flag clears", t.analysing is False)


def t_analysis_op_touches_nothing_it_should_not():
    e = Engine(samplerate=SR, blocksize=256, offline=True)
    buf = np.stack([np.linspace(-1, 1, SR, dtype=np.float32)] * 2, axis=1)
    e.post("track.load", i=0, buf=buf, sr=SR, name="x", path="", bpm=0.0,
           conf=0.0, slices=np.zeros(0, dtype=np.int64), analysing=True)
    e.render_offline(256)
    t = e.tracks[0]
    t.set_loop(500, 9000)
    t.gain = 0.33
    e.post("track.analysis", i=0, bpm=128.0, conf=0.9,
           slices=np.arange(16, dtype=np.int64) * 1000)
    e.render_offline(256)
    check("a hand-set region survives the analysis update",
          (t.loop_start, t.loop_end) == (500, 9000),
          str((t.loop_start, t.loop_end)))
    check("so do the playback settings", abs(t.gain - 0.33) < 1e-9)
    check("and the display fields are filled",
          t.bpm == 128.0 and t.slices.size == 16 and t.analysing is False)


def _tone(seconds, seed=2):
    n = SR * seconds
    t = (np.sin(2 * np.pi * 220 * np.arange(n) / SR) * 0.3
         + np.random.default_rng(seed).standard_normal(n) * 0.05).astype(np.float32)
    return np.stack([t, t], axis=1)


def _ms(fn, *a):
    t0 = time.perf_counter()
    out = fn(*a)
    return (time.perf_counter() - t0) * 1000.0, out


def t_long_file_analysis_is_bounded():
    """Unbounded, a four-minute track cost 7.6 s and blocked playback.

    What went wrong was quadratic cost in envelope length, so what this has to
    catch is a return to quadratic — not a particular number of milliseconds.
    An absolute wall-clock bar cannot do that honestly: `slice_points` on this
    machine measures 802 ms idle and 1815 ms with the panel running, and the
    thing competing for the CPU is this application. It flaked against a
    1500 ms bar for exactly that reason.

    So compare a one-minute file with a four-minute one in the same run. Both
    measurements take whatever contention is going, and the RATIO does not.
    Quadratic is 16x, linear is 4x. Generous absolute bounds stay as a
    backstop, far enough out that load cannot reach them.
    """
    short, big = _tone(60), _tone(240)

    bpm_short_ms, _ = _ms(dsp.estimate_bpm, short, SR)
    bpm_ms, _ = _ms(dsp.estimate_bpm, big, SR)
    sl_short_ms, _ = _ms(dsp.slice_points, short, SR, 16)
    sl_ms, (pts, _junk) = _ms(dsp.slice_points, big, SR, 16)

    # tempo caps its window at 30 s, so four times the file is the same work
    check("tempo cost stops growing once past the analysis window",
          bpm_ms < bpm_short_ms * 2.0 + 1.0,
          "%.0f ms for 4 min vs %.0f ms for 1 min" % (bpm_ms, bpm_short_ms))
    check("slicing grows with length, not with length squared",
          sl_ms < sl_short_ms * 8.0 + 1.0,
          "%.1fx for 4x the file (quadratic would be 16x)"
          % (sl_ms / max(sl_short_ms, 0.001)))
    check("and neither is anywhere near the old cost",
          bpm_ms < 2000 and sl_ms < 4000,
          "bpm %.0f ms (was 4695), slices %.0f ms (was 2697)" % (bpm_ms, sl_ms))
    check("slices still span the whole file, not just the window",
          pts[-1] > SR * 240 * 0.5,
          "last slice at %d of %d" % (pts[-1], SR * 240))

    env, hop = dsp.onset_envelope(big, SR)
    check("the onset envelope is bounded by widening the hop",
          env.size <= 16384 and hop > 256,
          "%d bins at hop %d" % (env.size, hop))


if __name__ == "__main__":
    for fn in (t_playable_before_analysed, t_analysis_op_touches_nothing_it_should_not,
               t_long_file_analysis_is_bounded):
        try:
            fn()
        except Exception as exc:
            check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    print("\n%d checks failed" % len(FAILS) if FAILS else "\nall checks passed")
    sys.exit(1 if FAILS else 0)
