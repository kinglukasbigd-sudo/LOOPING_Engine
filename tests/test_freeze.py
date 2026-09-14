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


def t_stopping_cannot_start_a_track():
    """The other half of the stopped-clock rule, and the worse half.

    "A stopped clock has no edges" made a queued LAUNCH fire at the instant of
    pressing stop. Tracks are not gated on the transport, so the track did not
    merely arm — it started, and it was audible with the transport stopped.
    Pressing STOP made sound.
    """
    e, t = loaded()
    e.post("transport.quantum", v=QUANTA.index(16.0))    # 4BAR, a long wait
    e.post("transport.start")
    e.render_offline(4096)
    e.post("track.play", i=0)
    e.render_offline(512)
    check("the launch is genuinely waiting while the clock runs",
          len(e._pending) == 1 and not t.playing,
          "pending=%d playing=%s" % (len(e._pending), t.playing))

    e.post("transport.stop")
    out = e.render_offline(2048)
    check("stopping does not start the track",
          not t.playing, "track.playing=%s" % t.playing)
    check("and the queue is empty rather than stranded",
          len(e._pending) == 0, "pending=%d" % len(e._pending))
    check("nothing is audible after a stop",
          float(np.abs(out).max()) == 0.0, "peak %.4f" % float(np.abs(out).max()))


def t_a_cancelled_launch_does_not_come_back():
    """Cancelled means gone, not deferred — the stranding hazard again."""
    e, t = loaded()
    e.post("transport.quantum", v=BAR)
    e.post("transport.start")
    e.render_offline(4096)
    e.post("track.play", i=0)
    e.render_offline(512)
    e.post("transport.stop")
    e.render_offline(512)
    e.post("transport.start")
    e.render_offline(SR * 3)                    # well past several bars
    check("restarting does not resurrect the cancelled launch",
          not t.playing and len(e._pending) == 0,
          "playing=%s pending=%d" % (t.playing, len(e._pending)))


def t_a_cancelled_launch_says_so_rather_than_pretending_it_landed():
    """A silent revert is worse than a freeze; so is a false confirmation."""
    e, t = loaded()
    e.post("transport.quantum", v=BAR)
    e.post("transport.start")
    e.render_offline(4096)
    e.post("track.play", i=0)
    e.render_offline(512)
    fired_before = t.fired
    check("the row shows what it is waiting to do", t.queued == "START", str(t.queued))
    e.post("transport.stop")
    e.render_offline(512)
    check("the label clears when the queue is cancelled",
          t.queued is None, str(t.queued))
    check("and the row does not flash as though it landed",
          t.fired == fired_before, "fired %d -> %d" % (fired_before, t.fired))
    check("the panel reports an empty queue",
          e.snapshot()["pending"] == 0 and e.snapshot()["pending_ms"] == 0)


def t_a_stop_keeps_the_edits_and_drops_only_the_moments():
    """Task 14 must survive Task 22: an edit still lands, a launch does not."""
    e, t = loaded()
    e.post("transport.quantum", v=BAR)
    e.post("transport.start")
    e.render_offline(4096)
    e.post("track.loop", i=0, ls=100, le=5000)      # a state
    e.post("track.play", i=0)                       # a moment
    e.render_offline(512)
    check("both are waiting", len(e._pending) == 2, "pending=%d" % len(e._pending))
    e.post("transport.stop")
    e.render_offline(512)
    check("the edit lands, because an edit describes how a thing should be",
          (t.loop_start, t.loop_end) == (100, 5000),
          str((t.loop_start, t.loop_end)))
    check("the launch does not, because there is no moment to land on",
          not t.playing, "playing=%s" % t.playing)
    check("and nothing is left in the queue either way",
          len(e._pending) == 0, "pending=%d" % len(e._pending))


def t_a_queued_key_is_cancelled_by_a_stop_too():
    """Keys queue on the same grid and lose it the same way."""
    e, t = loaded()
    e.post("transport.quantum", v=BAR)
    e.post("transport.start")
    e.render_offline(4096)
    e.post("pad.take", i=0, track=0)
    e.render_offline(256)
    e.post("pad.trigger.q", i=0)
    e.render_offline(512)
    check("the key is armed while the clock runs",
          e.snapshot()["pads_pending"][0] is True)
    e.post("transport.stop")
    e.render_offline(512)
    check("and disarmed by the stop, not fired by it",
          e.snapshot()["pads_pending"][0] is False
          and e.snapshot()["pads_on"][0] is False)


def t_the_wait_the_panel_shows_is_an_honest_upper_bound():
    """What the drag overlay's deadline is built on.

    The overlay keeps showing the hand's region until the engine echoes it,
    with a deadline of max(4s, pending_ms + 2s). A fixed 4s expired mid-wait
    at 4BAR and snapped the readout back to a number the engine was about to
    replace. That fix is only sound if pending_ms never UNDER-reports the real
    wait, so assert the property the UI leans on, at the longest quantum.
    """
    e, t = loaded()
    e.post("transport.quantum", v=QUANTA.index(16.0))
    e.post("transport.start")
    e.render_offline(4096)
    e.post("track.loop", i=0, ls=100, le=5000)
    e.render_offline(256)
    claimed_ms = e.snapshot()["pending_ms"]
    bar4_ms = 60.0 / e.transport.bpm * 16.0 * 1000.0
    check("a 4BAR wait is reported, not rounded away",
          claimed_ms > 4000.0,
          "%.0f ms — a fixed 4 s deadline expires inside it" % claimed_ms)
    check("and it never claims more than the quantum itself",
          claimed_ms <= bar4_ms + 1, "%.0f of %.0f ms" % (claimed_ms, bar4_ms))
    e.render_offline(int(claimed_ms / 1000.0 * SR) + 4096)
    check("the change lands within the wait it reported",
          (t.loop_start, t.loop_end) == (100, 5000),
          str((t.loop_start, t.loop_end)))


if __name__ == "__main__":
    for fn in (t_a_stopped_clock_has_no_boundaries, t_queue_never_strands_on_stop,
               t_a_stale_command_cannot_revert_a_later_edit,
               t_a_drag_does_not_pile_up, t_pending_reports_its_own_wait,
               t_quantum_off_and_never_started_still_apply_at_once,
               t_a_tiny_region_can_still_be_escaped,
               t_stopping_cannot_start_a_track,
               t_a_cancelled_launch_does_not_come_back,
               t_a_cancelled_launch_says_so_rather_than_pretending_it_landed,
               t_a_stop_keeps_the_edits_and_drops_only_the_moments,
               t_a_queued_key_is_cancelled_by_a_stop_too,
               t_the_wait_the_panel_shows_is_an_honest_upper_bound):
        try:
            fn()
        except Exception as exc:
            check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    print("\n%d checks failed" % len(FAILS) if FAILS else "\nall checks passed")
    sys.exit(1 if FAILS else 0)
