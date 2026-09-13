"""Rate choice and the resampling disclosure.

    PYTHONPATH=.pylibs python3 -m tests.test_rate
"""
from __future__ import annotations

import sys

import numpy as np

from loopengine import graph
from loopengine.engine import Engine

SR = 48000
FAILS = []


def check(name, ok, detail=""):
    print("%-56s %s%s" % (name, "PASS" if ok else "FAIL",
                          ("  " + detail) if detail else ""))
    if not ok:
        FAILS.append(name)


def t_prefers_the_graph_rate():
    """The device advertised 44100 while the sink ran 48000; following the
    device put a resampler in the path."""
    real = graph.native_output_rate
    graph.native_output_rate = lambda: (48000, "test")
    try:
        got = graph.choose_rate(44100, supports=lambda r: True)
        check("the graph's rate wins over the device default", got == 48000,
              "chose %d" % got)
        got = graph.choose_rate(44100, supports=lambda r: r != 48000)
        check("a refused graph rate falls back rather than failing",
              got in (graph.PREFERRED, 44100) and got != 48000, "chose %d" % got)
    finally:
        graph.native_output_rate = real


def t_unknown_graph_falls_back_to_the_device():
    real = graph.native_output_rate
    graph.native_output_rate = lambda: (None, "unknown")
    try:
        got = graph.choose_rate(44100, supports=lambda r: r == 44100)
        check("an unknown graph rate uses what the device offers", got == 44100,
              "chose %d" % got)
    finally:
        graph.native_output_rate = real


def t_offline_claims_nothing():
    e = Engine(samplerate=SR, blocksize=256, offline=True)
    snap = e.snapshot()
    check("the null backend does not claim a native rate",
          snap["native_rate"] is None and snap["resampling"] is False,
          "native=%s resampling=%s" % (snap["native_rate"], snap["resampling"]))


def t_resampling_flag_is_reported():
    e = Engine(samplerate=SR, blocksize=256, offline=True)
    e.native_rate, e.native_how = 48000, "test"
    check("matched rates report no resampling", e.snapshot()["resampling"] is False)
    e.sr = 44100
    check("a mismatch is reported so the panel can disclose it",
          e.snapshot()["resampling"] is True
          and e.snapshot()["native_rate"] == 48000)


def t_44k1_file_on_a_48k_engine_keeps_its_pitch():
    """The never-resample property, exercised in the direction 48000 creates:
    files at 44100 now fold their ratio into the playback step."""
    f, sr_file = 441.0, 44100
    t = np.arange(sr_file) / sr_file
    tone = np.sin(2 * np.pi * f * t).astype(np.float32)
    e = Engine(samplerate=48000, blocksize=256, offline=True)
    e.post("track.load", i=0, buf=np.stack([tone, tone], axis=1), sr=sr_file,
           name="s", path="", bpm=0.0, conf=0.0, slices=np.zeros(0, dtype=np.int64))
    e.post("track.gain", i=0, v=1.0)
    e.post("master.gain", v=1.0)
    e.post("track.play", i=0)
    e.render_offline(4096)
    y = e.render_offline(48000)[:, 0]
    spec = np.abs(np.fft.rfft(y * np.hanning(y.size)))
    peak = np.fft.rfftfreq(y.size, 1 / 48000)[int(np.argmax(spec))]
    check("a 44.1k file keeps its pitch on a 48k engine",
          abs(peak - f) < 2.0, "peak %.1f Hz, want %.1f" % (peak, f))

    step = 1.0 * (sr_file / 48000.0)
    check("its playback step is the rate ratio, not a resample",
          abs(step - 0.91875) < 1e-9, "step %.6f" % step)


def t_native_probe_is_safe_when_nothing_is_there():
    check("a native-rate probe never raises",
          isinstance(graph.native_output_rate(), tuple))


if __name__ == "__main__":
    for fn in (t_prefers_the_graph_rate, t_unknown_graph_falls_back_to_the_device,
               t_offline_claims_nothing, t_resampling_flag_is_reported,
               t_44k1_file_on_a_48k_engine_keeps_its_pitch,
               t_native_probe_is_safe_when_nothing_is_there):
        try:
            fn()
        except Exception as exc:
            check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    print("\n%d checks failed" % len(FAILS) if FAILS else "\nall checks passed")
    sys.exit(1 if FAILS else 0)
