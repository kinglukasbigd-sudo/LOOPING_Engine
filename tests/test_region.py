"""Loop region: defaults, degenerate cases, and quantised application.

    PYTHONPATH=.pylibs python3 -m tests.test_region
"""
from __future__ import annotations

import math
import sys

import numpy as np

from loopengine.engine import Engine
from loopengine.track import MIN_LOOP
from loopengine.transport import QUANTA

SR = 48000
FAILS = []


def check(name, ok, detail=""):
    print("%-54s %s%s" % (name, "PASS" if ok else "FAIL",
                          ("  " + detail) if detail else ""))
    if not ok:
        FAILS.append(name)


def loaded(frames=SR, engine=None, sr=SR):
    e = engine or Engine(samplerate=SR, blocksize=256, offline=True)
    ramp = np.linspace(-1, 1, frames, dtype=np.float32)
    buf = np.stack([ramp, ramp], axis=1)
    e.post("track.load", i=0, buf=buf, sr=sr, name="ramp", path="",
           bpm=0.0, conf=0.0, slices=np.zeros(0, dtype=np.int64))
    e.render_offline(256)
    return e, e.tracks[0]


# --------------------------------------------------------------------------
def t_default_is_the_whole_file():
    e, t = loaded(frames=12345)
    check("a freshly loaded file loops end to end",
          t.loop_start == 0 and t.loop_end == 12345,
          "%d -> %d of %d" % (t.loop_start, t.loop_end, t.frames))


def t_detection_never_moves_a_loop_point():
    """Onsets and bpm may be drawn as markers; they must not set the region."""
    from loopengine.demo import kick, SR as DSR
    e = Engine(samplerate=SR, blocksize=256, offline=True)
    buf = kick().astype(np.float32)
    from loopengine import dsp
    pts, _ = dsp.slice_points(buf, DSR, 16)
    bpm, conf = dsp.estimate_bpm(buf, DSR)
    e.post("track.load", i=0, buf=buf, sr=DSR, name="kick", path="",
           bpm=bpm, conf=conf, slices=pts)
    e.render_offline(256)
    t = e.tracks[0]
    check("analysis is carried but does not touch the region",
          t.loop_start == 0 and t.loop_end == t.frames
          and t.bpm > 0 and t.slices.size > 0,
          "bpm %.1f, %d slices, region %d->%d"
          % (t.bpm, t.slices.size, t.loop_start, t.loop_end))


def t_reversed_and_empty_regions():
    e, t = loaded()
    t.set_loop(5000, 5000)
    check("an empty region becomes the minimum, not a division by zero",
          t.loop_end - t.loop_start == MIN_LOOP,
          "%d..%d" % (t.loop_start, t.loop_end))
    t.set_loop(9000, 1000)
    check("a reversed region is repaired rather than refused",
          t.loop_end > t.loop_start and t.loop_end - t.loop_start >= MIN_LOOP,
          "%d..%d" % (t.loop_start, t.loop_end))


def t_sub_block_region_is_allowed_and_renders():
    e, t = loaded()
    e.post("track.gain", i=0, v=1.0)
    e.post("master.gain", v=1.0)
    e.post("track.play", i=0)
    e.render_offline(2048)
    t.set_loop(1000, 1000 + 100)        # 100 frames, well under a 256 block
    y = e.render_offline(4096)
    check("a region shorter than one block still renders",
          t.loop_end - t.loop_start == 100 and np.isfinite(y).all()
          and float(np.abs(y).max()) > 0,
          "L=%d peak %.3f" % (t.loop_len, np.abs(y).max()))
    t.set_loop(1000, 1000 + 4)
    check("a region below the floor is widened to it",
          t.loop_len == MIN_LOOP, "L=%d" % t.loop_len)


def t_region_shorter_than_the_crossfade():
    e, t = loaded()
    e.post("track.xfade", i=0, v=50.0)          # 2400 frames at 48k
    e.post("track.gain", i=0, v=1.0)
    e.post("track.play", i=0)
    e.render_offline(1024)
    t.set_loop(2000, 2000 + 300)                # region far shorter than fade
    y = e.render_offline(4096)
    check("a region shorter than the crossfade stays finite",
          bool(np.isfinite(y).all()), "peak %.3f" % np.abs(y).max())


def t_playhead_outside_is_wrapped_not_snapped():
    e, t = loaded()
    t.phase = 9000.0
    # 450, not 500: with a length that divides the offset exactly the wrap
    # lands on the start and the test cannot tell wrapping from snapping.
    t.set_loop(1000, 1450)                       # phase is far outside
    inside = t.loop_start <= t.phase < t.loop_end
    wrapped = 1000 + ((9000 - 1000) % 450)
    check("a playhead outside the new region is wrapped into it",
          inside and abs(t.phase - wrapped) < 1e-6,
          "phase %.1f, expected %.1f" % (t.phase, wrapped))
    check("it is not snapped to the region start",
          abs(t.phase - t.loop_start) > 1e-6, "phase %.1f" % t.phase)


def t_region_clamps_to_the_file():
    e, t = loaded(frames=5000)
    t.set_loop(-500, 99999)
    check("a region past both ends is clamped to the file",
          t.loop_start == 0 and t.loop_end == 5000,
          "%d..%d of %d" % (t.loop_start, t.loop_end, t.frames))
    t.set_loop(4990, 99999)
    check("a region starting near the end keeps the minimum length",
          t.loop_len >= MIN_LOOP and t.loop_end <= 5000,
          "%d..%d" % (t.loop_start, t.loop_end))


# --------------------------------------------------------------------------
def t_region_change_waits_for_the_boundary():
    bpm = 120.0
    spb = 60.0 / bpm * SR
    bar = spb * 4
    e, t = loaded()
    e.post("track.gain", i=0, v=1.0)
    e.post("transport.bpm", v=bpm)
    e.post("transport.quantum", v=QUANTA.index(4.0))     # BAR
    e.post("transport.start")
    e.post("track.play", i=0)
    e.render_offline(int(bar) + 700)                      # roll past the first
    before = (e.tracks[0].loop_start, e.tracks[0].loop_end)
    start_pos = e.transport.pos
    e.post("track.loop", i=0, ls=1000, le=2000)
    e.render_offline(64)
    check("a region change on a running track does not land at once",
          (e.tracks[0].loop_start, e.tracks[0].loop_end) == before,
          "still %s" % (before,))
    to_bar = int(math.ceil(start_pos / bar) * bar) - start_pos
    e.render_offline(to_bar + 512)
    check("it lands on the next bar",
          (e.tracks[0].loop_start, e.tracks[0].loop_end) == (1000, 2000),
          "%s after %d frames" % ((e.tracks[0].loop_start,
                                   e.tracks[0].loop_end), to_bar))


def t_region_change_is_immediate_when_stopped():
    e, t = loaded()
    e.post("transport.quantum", v=QUANTA.index(4.0))
    e.post("track.loop", i=0, ls=300, le=900)
    e.render_offline(256)
    check("a stopped track takes the region at once",
          (t.loop_start, t.loop_end) == (300, 900),
          "%d..%d" % (t.loop_start, t.loop_end))


def t_region_change_is_immediate_when_quantum_off():
    e, t = loaded()
    e.post("track.gain", i=0, v=1.0)
    e.post("transport.quantum", v=0)
    e.post("transport.start")
    e.post("track.play", i=0)
    e.render_offline(512)
    e.post("track.loop", i=0, ls=700, le=1700)
    e.render_offline(256)
    check("quantum OFF applies the region in the same block",
          (t.loop_start, t.loop_end) == (700, 1700),
          "%d..%d" % (t.loop_start, t.loop_end))


def t_live_change_does_not_step():
    """The seam after a region change must not jump harder than the material."""
    f = 200.0
    n = SR
    tone = np.sin(2 * np.pi * f * np.arange(n) / SR).astype(np.float32)
    e = Engine(samplerate=SR, blocksize=256, offline=True)
    e.post("track.load", i=0, buf=np.stack([tone, tone], axis=1), sr=SR,
           name="sine", path="", bpm=0.0, conf=0.0,
           slices=np.zeros(0, dtype=np.int64))
    e.post("track.gain", i=0, v=1.0)
    e.post("master.gain", v=1.0)
    e.post("track.xfade", i=0, v=8.0)
    e.post("transport.bpm", v=120.0)
    e.post("transport.quantum", v=QUANTA.index(4.0))
    e.post("transport.start")
    e.post("track.play", i=0)
    e.render_offline(8192)
    cycle = SR / f
    e.post("track.loop", i=0, ls=0, le=int(cycle * 60))
    y = e.render_offline(SR // 2)[:, 0]
    step = float(np.abs(np.diff(y)).max())
    expected = 2 * math.pi * f / SR
    check("a region change landing mid-playback does not step",
          step < expected * 2.0,
          "max step %.5f vs %.5f/sample" % (step, expected))


def t_offline_matches_the_live_path():
    """offline.py and the null backend must render the same samples."""
    def run():
        e = Engine(samplerate=SR, blocksize=256, offline=True)
        tone = np.sin(2 * np.pi * 300 * np.arange(SR) / SR).astype(np.float32)
        e.post("track.load", i=0, buf=np.stack([tone, tone], axis=1), sr=SR,
               name="s", path="", bpm=0.0, conf=0.0,
               slices=np.zeros(0, dtype=np.int64))
        e.post("track.gain", i=0, v=1.0)
        e.post("master.gain", v=1.0)
        e.post("transport.bpm", v=120.0)
        e.post("transport.quantum", v=QUANTA.index(4.0))
        e.post("transport.start")
        e.post("track.play", i=0)
        e.render_offline(4096)
        e.post("track.loop", i=0, ls=500, le=9500)
        # a bar at 120 bpm is 96000 frames — render past it so the queued
        # region change actually fires inside the window being compared
        return (e.render_offline(SR * 3),
                (e.tracks[0].loop_start, e.tracks[0].loop_end))

    a, ra = run()
    b, rb = run()
    check("the same session renders sample-identical twice",
          np.array_equal(a, b) and ra == rb,
          "region %s, max diff %.2e" % (ra, float(np.abs(a - b).max())))


if __name__ == "__main__":
    for fn in (t_default_is_the_whole_file, t_detection_never_moves_a_loop_point,
               t_reversed_and_empty_regions, t_sub_block_region_is_allowed_and_renders,
               t_region_shorter_than_the_crossfade,
               t_playhead_outside_is_wrapped_not_snapped, t_region_clamps_to_the_file,
               t_region_change_waits_for_the_boundary,
               t_region_change_is_immediate_when_stopped,
               t_region_change_is_immediate_when_quantum_off,
               t_live_change_does_not_step, t_offline_matches_the_live_path):
        try:
            fn()
        except Exception as exc:
            check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    print("\n%d checks failed" % len(FAILS) if FAILS else "\nall checks passed")
    sys.exit(1 if FAILS else 0)
