"""Hot cues and beat jump: aiming points, and a window that slides.

    PYTHONPATH=.pylibs python3 -m tests.test_cues
"""
from __future__ import annotations

import sys

import numpy as np

from loopengine.engine import Engine
from loopengine.track import CUES

SR = 48000
BS = 256
FAILS = []


def check(name, ok, detail=""):
    print("%-60s %s%s" % (name, "PASS" if ok else "FAIL",
                          ("  " + detail) if detail else ""))
    if not ok:
        FAILS.append(name)


def rig(frames=SR * 4, loop=(0, SR)):
    e = Engine(samplerate=SR, blocksize=BS, offline=True)
    ramp = np.linspace(-1, 1, frames, dtype=np.float32)
    e.post("track.load", i=0, buf=np.stack([ramp, ramp], axis=1), sr=SR,
           name="ramp.wav", path="", bpm=120.0, conf=1.0,
           slices=np.zeros(0, dtype=np.int64))
    e.post("transport.quantum", v=0)
    e.post("track.loop", i=0, ls=loop[0], le=loop[1])
    e.render_offline(BS)
    return e, e.tracks[0]


# --------------------------------------------------------------------------
def t_set_jump_and_clear():
    e, t = rig()
    check("a fresh track has eight empty cues",
          t.cues.shape[0] == CUES and all(c < 0 for c in t.cues))
    e.post("track.play", i=0)
    e.render_offline(BS * 10)
    where = int(t.phase)
    e.post("track.cue.set", i=0, c=2)
    e.render_offline(BS)
    check("an empty cue takes the playhead's place",
          abs(int(t.cues[2]) - where) <= BS, "%d vs %d" % (t.cues[2], where))
    was = (t.loop_start, t.loop_end)
    e.render_offline(BS * 10)
    e.post("track.cue.jump", i=0, c=2)
    e.render_offline(BS)
    check("a set cue is jumped to, and moves no loop point",
          abs(t.phase - int(t.cues[2])) < BS * 2 and (t.loop_start, t.loop_end) == was,
          "phase %.0f cue %d" % (t.phase, t.cues[2]))
    e.post("track.cue.clear", i=0, c=2)
    e.render_offline(BS)
    check("and clearing it empties that one and no other",
          t.cues[2] == -1 and all(t.cues[c] == -1 for c in range(CUES)))
    e.post("track.cue.jump", i=0, c=2)
    e.render_offline(BS)
    check("jumping to an empty cue does nothing at all",
          (t.loop_start, t.loop_end) == was)


def t_a_cue_outside_the_region_slides_it():
    e, t = rig(loop=(0, SR))
    e.post("track.cues", i=0, cues=[SR * 2] + [-1] * 7)
    e.render_offline(BS)
    L = t.loop_len
    e.post("track.cue.jump", i=0, c=0)
    e.render_offline(BS)
    check("the window moves to start on the cue and keeps its length",
          t.loop_start == SR * 2 and t.loop_len == L,
          "%d..%d (%d)" % (t.loop_start, t.loop_end, t.loop_len))
    check("and playback starts at its head", t.phase == float(SR * 2))

    e2, t2 = rig(loop=(SR, SR * 2))
    e2.post("track.cues", i=0, cues=[SR + 5000] + [-1] * 7)
    e2.render_offline(BS)
    was = (t2.loop_start, t2.loop_end)
    e2.post("track.cue.jump", i=0, c=0)
    e2.render_offline(BS)
    check("a cue inside the region moves the playhead and not the region",
          (t2.loop_start, t2.loop_end) == was and t2.phase == float(SR + 5000))


def t_a_jump_waits_for_the_quantum():
    e, t = rig(loop=(0, SR))
    e.post("track.cues", i=0, cues=[SR * 2] + [-1] * 7)
    e.post("transport.quantum", v=3)            # BEAT
    e.post("transport.start")
    e.post("track.play", i=0)
    e.render_offline(BS * 4)
    was = t.loop_start
    e.post("track.cue.jump", i=0, c=0)
    e.render_offline(BS)
    check("the jump is queued, and says so",
          e.tracks[0].queued == "CUE" and t.loop_start == was,
          "%s at %d" % (t.queued, t.loop_start))
    e.render_offline(int(e.transport.spb) + BS)
    check("and lands on the beat", t.loop_start == SR * 2 and t.queued is None,
          "%d" % t.loop_start)

    e2, t2 = rig(loop=(0, SR))
    e2.post("track.cues", i=0, cues=[SR * 2] + [-1] * 7)
    e2.post("transport.quantum", v=0)           # OFF
    e2.post("transport.start")
    e2.post("track.play", i=0)
    e2.render_offline(BS)
    e2.post("track.cue.jump", i=0, c=0)
    e2.render_offline(BS)
    check("with the quantum off it lands at once", t2.loop_start == SR * 2)


def t_a_beat_jump_slides_the_window():
    e, t = rig(loop=(SR, SR + 24000))
    bf = t.beat_frames(e.transport.bpm)         # the file says 120 bpm
    L = t.loop_len
    e.post("track.loop.nudge", i=0, v=4)        # a bar
    e.render_offline(BS)
    check("a bar forward moves four beats and keeps the length",
          t.loop_len == L and abs(t.loop_start - (SR + 4 * bf)) <= 1,
          "%d, wanted %d" % (t.loop_start, SR + 4 * bf))
    e.post("track.loop.nudge", i=0, v=-4)
    e.render_offline(BS)
    check("and back again lands where it started",
          t.loop_start == SR and t.loop_len == L, "%d" % t.loop_start)


def t_cues_do_not_outlive_the_file():
    e, t = rig()
    e.post("track.cues", i=0, cues=[100, 200, -1, -1, -1, -1, -1, -1])
    e.render_offline(BS)
    check("a session's set goes on", int(t.cues[0]) == 100 and int(t.cues[1]) == 200)
    ramp = np.linspace(-1, 1, SR, dtype=np.float32)
    e.post("track.load", i=0, buf=np.stack([ramp, ramp], axis=1), sr=SR,
           name="other.wav", path="", bpm=0.0, conf=0.0,
           slices=np.zeros(0, dtype=np.int64))
    e.render_offline(BS)
    check("but another file on that track arrives with none",
          all(c < 0 for c in t.cues))
    e.post("track.cues", i=0, cues=[SR * 9, -1, -1, -1, -1, -1, -1, -1])
    e.render_offline(BS)
    check("and a cue past the end of the file is no cue", t.cues[0] == -1)


if __name__ == "__main__":
    for fn in (t_set_jump_and_clear,
               t_a_cue_outside_the_region_slides_it,
               t_a_jump_waits_for_the_quantum,
               t_a_beat_jump_slides_the_window,
               t_cues_do_not_outlive_the_file):
        try:
            fn()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    print("\n%d checks failed" % len(FAILS) if FAILS else "\nall checks passed")
    sys.exit(1 if FAILS else 0)
