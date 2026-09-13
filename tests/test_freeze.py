"""The stuck loop: no queue may ever become unreachable.

    PYTHONPATH=.pylibs python3 -m tests.test_freeze
"""
from __future__ import annotations

import sys

import numpy as np

from loopengine.engine import Engine
from loopengine.transport import QUANTA, Transport

SR = 48000
BAR = QUANTA.index(4.0)
FAILS = []


def check(name, ok, detail=""):
    print("%-58s %s%s" % (name, "PASS" if ok else "FAIL",
                          ("  " + detail) if detail else ""))
    if not ok:
        FAILS.append(name)


def loaded():
    e = Engine(samplerate=SR, blocksize=256, offline=True)
    ramp = np.linspace(-1, 1, SR, dtype=np.float32)
    e.post("track.load", i=0, buf=np.stack([ramp, ramp], axis=1), sr=SR,
           name="r", path="", bpm=0.0, conf=0.0, slices=np.zeros(0, dtype=np.int64))
    e.render_offline(256)
    return e, e.tracks[0]


# --------------------------------------------------------------------------
def t_a_stopped_clock_has_no_boundaries():
    tr = Transport(SR, 120.0)
    tr.quantum_i = BAR
    tr.pos = 12345                       # deliberately not on an edge
    tr.playing = False
    check("a stopped transport reports every moment as an edge",
          tr.frames_to_boundary() == 0)
    tr.playing = True
    check("a running one still waits for the real edge",
          tr.frames_to_boundary() > 0, "%d frames" % tr.frames_to_boundary())


def t_queue_never_strands_on_stop():
    """The reported freeze. Queue while playing, stop before the bar."""
    e, t = loaded()
    e.post("transport.quantum", v=BAR)
    e.post("transport.start")
    e.render_offline(4096)
    e.post("track.loop", i=0, ls=100, le=5000)
    e.render_offline(512)
    check("the change is genuinely waiting while the clock runs",
          len(e._pending) == 1 and (t.loop_start, t.loop_end) == (0, SR),
          "pending=%d" % len(e._pending))
    e.post("transport.stop")
    e.render_offline(512)
    check("stopping drains it instead of stranding it",
          len(e._pending) == 0 and (t.loop_start, t.loop_end) == (100, 5000),
          "pending=%d region=%s" % (len(e._pending), (t.loop_start, t.loop_end)))
    check("and the queued label clears with it", t.queued is None, str(t.queued))


def t_a_stale_command_cannot_revert_a_later_edit():
    """The sting: the stranded op used to fire on restart and overwrite."""
    e, t = loaded()
    e.post("transport.quantum", v=BAR)
    e.post("transport.start")
    e.render_offline(4096)
    e.post("track.loop", i=0, ls=100, le=5000)     # queued
    e.render_offline(512)
    e.post("transport.stop")
    e.render_offline(512)
    e.post("track.loop", i=0, ls=9, le=9999)       # the player edits, stopped
    e.render_offline(512)
    check("an edit made while stopped applies at once",
          (t.loop_start, t.loop_end) == (9, 9999), str((t.loop_start, t.loop_end)))
    e.post("transport.start")
    e.render_offline(SR * 3)
    check("restarting does not resurrect the pre-stop command",
          (t.loop_start, t.loop_end) == (9, 9999),
          "region %s after restart" % ((t.loop_start, t.loop_end),))


def t_a_drag_does_not_pile_up():
    e, t = loaded()
    e.post("transport.quantum", v=BAR)
    e.post("transport.start")
    e.render_offline(4096)
    for k in range(120):                            # one per pointer move
        e.post("track.loop", i=0, ls=k, le=20000 + k)
    e.render_offline(256)
    check("a drag collapses to one pending change, last wins",
          len(e._pending) == 1, "%d pending" % len(e._pending))
    e.render_offline(SR * 3)
    check("and the one that lands is the last one",
          (t.loop_start, t.loop_end) == (119, 20119),
          str((t.loop_start, t.loop_end)))


def t_pending_reports_its_own_wait():
    """A silent wait is indistinguishable from a crash."""
    e, t = loaded()
    e.post("transport.quantum", v=BAR)
    e.post("transport.start")
    e.render_offline(4096)
    check("nothing queued reports no wait", e.snapshot()["pending_ms"] == 0)
    e.post("track.loop", i=0, ls=100, le=5000)
    e.render_offline(256)
    snap = e.snapshot()
    bar_ms = 60.0 / 120.0 * 4 * 1000.0
    check("a queued change reports how long is left",
          0 < snap["pending_ms"] <= bar_ms + 1,
          "%.0f ms of a %.0f ms bar" % (snap["pending_ms"], bar_ms))
    check("and says how many are waiting", snap["pending"] == 1)


def t_quantum_off_and_never_started_still_apply_at_once():
    """Both were already correct; pin them so the fix cannot regress them."""
    e, t = loaded()
    e.post("transport.quantum", v=BAR)          # never started
    e.post("track.loop", i=0, ls=7, le=777)
    e.render_offline(512)
    check("quantised op on a never-started transport applies at once",
          (t.loop_start, t.loop_end) == (7, 777), str((t.loop_start, t.loop_end)))
    e2, t2 = loaded()
    e2.post("transport.quantum", v=0)
    e2.post("transport.start")
    e2.render_offline(512)
    e2.post("track.loop", i=0, ls=11, le=1111)
    e2.render_offline(256)
    check("quantum OFF applies in the same block",
          (t2.loop_start, t2.loop_end) == (11, 1111),
          str((t2.loop_start, t2.loop_end)))


def t_a_tiny_region_can_still_be_escaped():
    """A region clamped to the floor must not be a state you cannot leave."""
    from loopengine.track import MIN_LOOP
    e, t = loaded()
    t.set_loop(1000, 1000 + MIN_LOOP)
    check("at the floor to begin with", t.loop_len == MIN_LOOP)
    t.scale_loop(2.0)
    check("scaling up escapes the floor", t.loop_len > MIN_LOOP,
          "L=%d" % t.loop_len)
    t.set_loop(1000, 1000 + MIN_LOOP)
    t.nudge_loop(5000)
    check("nudging still moves it at the floor",
          t.loop_start != 1000 and t.loop_len == MIN_LOOP,
          "start=%d L=%d" % (t.loop_start, t.loop_len))
    t.set_loop(0, t.frames)
    check("and it can always be reset to the whole file",
          (t.loop_start, t.loop_end) == (0, t.frames))


if __name__ == "__main__":
    for fn in (t_a_stopped_clock_has_no_boundaries, t_queue_never_strands_on_stop,
               t_a_stale_command_cannot_revert_a_later_edit,
               t_a_drag_does_not_pile_up, t_pending_reports_its_own_wait,
               t_quantum_off_and_never_started_still_apply_at_once,
               t_a_tiny_region_can_still_be_escaped):
        try:
            fn()
        except Exception as exc:
            check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    print("\n%d checks failed" % len(FAILS) if FAILS else "\nall checks passed")
    sys.exit(1 if FAILS else 0)
