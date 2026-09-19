"""Loop Roll: a way of playing, not an edit.

    PYTHONPATH=.pylibs python3 -m tests.test_roll
"""
from __future__ import annotations

import sys

import numpy as np

from loopengine.engine import Engine

SR = 48000
BS = 256
BPM = 125.0          # 23040 frames a beat: every roll line is a block line too
FAILS = []


def check(name, ok, detail=""):
    print("%-62s %s%s" % (name, "PASS" if ok else "FAIL",
                          ("  " + detail) if detail else ""))
    if not ok:
        FAILS.append(name)


def material(frames=SR * 2):
    """Nothing repeats, so playing the same half-beat twice is visible."""
    rng = np.random.default_rng(7)
    y = rng.standard_normal((frames, 2)).astype(np.float32) * 0.2
    return y


def rig(loop=(0, SR * 2)):
    e = Engine(samplerate=SR, blocksize=BS, offline=True)
    e.transport.set_bpm(BPM)
    e.post("track.load", i=0, buf=material(), sr=SR, name="r.wav", path="",
           bpm=0.0, conf=0.0, slices=np.zeros(0, dtype=np.int64))
    e.post("transport.quantum", v=0)         # OFF: nothing waits on a boundary
    e.post("track.loop", i=0, ls=loop[0], le=loop[1])
    e.post("track.gain", i=0, v=1.0)
    e.post("master.gain", v=1.0)
    e.post("transport.start")
    e.post("track.play", i=0)
    e.render_offline(BS)                     # everything lands
    return e


def take(beats=None, before=40, during=50, after=40):
    """Render the same passage with and without a roll in the middle."""
    e = rig()
    a = e.render_offline(BS * before)
    if beats:
        e.post("track.roll.on", i=0, beats=beats)
    b = e.render_offline(BS * during)
    if beats:
        e.post("track.roll.off", i=0)
    c = e.render_offline(BS * after)
    return e, np.concatenate([a, b, c])


# --------------------------------------------------------------------------
def t_release_continues_as_if_it_never_stopped():
    plain_e, plain = take(None)
    roll_e, rolled = take(0.5)
    rel = BS * 90
    xf = roll_e.tracks[0].xfade
    check("the roll is audible while it is held",
          not np.allclose(plain[BS * 46:rel], rolled[BS * 46:rel]))
    tail_p, tail_r = plain[rel + xf:], rolled[rel + xf:]
    same = np.array_equal(tail_p, tail_r)
    check("and past the release fade the two renders are sample-identical",
          same, "max diff %.3g over %d frames"
          % (float(np.abs(tail_p - tail_r).max()), tail_p.shape[0]))
    check("the fade itself is short and quiet, not a cut",
          float(np.abs(rolled[rel:rel + xf]).max()) < 1.0 and xf > 0,
          "xf=%d" % xf)


def t_a_roll_never_touches_the_loop():
    e = rig(loop=(1000, 1000 + SR))
    was = (e.tracks[0].loop_start, e.tracks[0].loop_end)
    e.post("track.roll.on", i=0, beats=0.25)
    e.render_offline(BS * 60)
    now = (e.tracks[0].loop_start, e.tracks[0].loop_end)
    check("the stored region is untouched while a roll runs",
          now == was and e.tracks[0].roll > 0, "%s -> %s" % (was, now))
    e.post("track.roll.off", i=0)
    e.render_offline(BS)
    check("and after the release", (e.tracks[0].loop_start,
                                    e.tracks[0].loop_end) == was)


def t_a_roll_waits_for_its_own_line():
    e = rig()
    e.render_offline(BS * 40)                       # pos 10240, line at 11520
    e.post("track.roll.on", i=0, beats=0.5)
    e.render_offline(BS)
    check("armed, and not yet rolling", e.tracks[0].roll_armed
          and e.tracks[0].roll == 0)
    e.render_offline(BS * 3)                        # pos 11264
    check("still waiting a block before the line", e.tracks[0].roll == 0)
    e.render_offline(BS)                            # pos 11520
    t = e.tracks[0]
    check("and it starts on the line, one half-beat long",
          t.roll > 0 and abs(t.roll - 11520) <= 1 and not t.roll_armed,
          "roll=%d at pos %d" % (t.roll, e.transport.pos))
    check("the window is the half-beat just played, not the one to come",
          t.roll_start + t.roll == 11520
          and t.roll_start <= t.phase < t.roll_start + t.roll,
          "%d + %d, phase %.1f" % (t.roll_start, t.roll, t.phase))


def t_stop_and_panic_end_it():
    e = rig()
    e.post("track.roll.on", i=0, beats=0.25)
    e.render_offline(BS * 40)
    check("rolling", e.tracks[0].roll > 0)
    e.post("panic")
    e.render_offline(BS)
    t = e.tracks[0]
    check("panic ends the roll and leaves the phase where playback was",
          t.roll == 0 and not t.roll_armed and t.phase == t.roll_shadow)

    e2 = rig()
    e2.post("track.roll.on", i=0, beats=0.25)
    e2.render_offline(BS * 40)
    e2.post("track.stop", i=0)
    out = e2.render_offline(BS * 40)
    check("stop fades out with no click and clears the roll",
          e2.tracks[0].roll == 0 and not e2.tracks[0].playing
          and float(np.abs(np.diff(out[:, 0])).max()) < 0.5,
          "biggest step %.3f" % float(np.abs(np.diff(out[:, 0])).max()))


if __name__ == "__main__":
    for fn in (t_release_continues_as_if_it_never_stopped,
               t_a_roll_never_touches_the_loop,
               t_a_roll_waits_for_its_own_line,
               t_stop_and_panic_end_it):
        try:
            fn()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    print("\n%d checks failed" % len(FAILS) if FAILS else "\nall checks passed")
    sys.exit(1 if FAILS else 0)
