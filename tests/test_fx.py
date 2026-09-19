"""Hold FX on the master bus: the echo.

    PYTHONPATH=.pylibs python3 -m tests.test_fx
"""
from __future__ import annotations

import sys
import time

import numpy as np

from loopengine.engine import Engine

SR = 48000
BS = 256
FAILS = []


def check(name, ok, detail=""):
    print("%-60s %s%s" % (name, "PASS" if ok else "FAIL",
                          ("  " + detail) if detail else ""))
    if not ok:
        FAILS.append(name)


CLICK_AT = SR // 2


def clicker(frames=SR * 4, amp=0.5):
    """Silence with one click in it: every repeat is then countable. Not at
    frame 0 — a track's gain ramps up over its first block, which would swallow
    it."""
    y = np.zeros((frames, 2), dtype=np.float32)
    y[CLICK_AT] = amp
    return y


def rig(bpm=120.0):
    e = Engine(samplerate=SR, blocksize=BS, offline=True)
    e.transport.set_bpm(bpm)
    e.post("track.load", i=0, buf=clicker(), sr=SR, name="click.wav", path="",
           bpm=0.0, conf=0.0, slices=np.zeros(0, dtype=np.int64))
    e.post("transport.quantum", v=0)
    e.post("track.gain", i=0, v=1.0)
    e.post("master.gain", v=1.0)
    e.render_offline(BS)
    return e


def hits(y, thresh=0.01):
    """Frame index of every sample that stands out of the silence."""
    m = np.abs(y[:, 0])
    return [int(i) for i in np.nonzero(m > thresh)[0]]


# --------------------------------------------------------------------------
def t_a_held_echo_repeats_on_the_quarter_beat():
    e = rig()
    d = int(e.transport.spb * 0.25)
    e.post("master.echo", on=True)
    e.post("track.play", i=0)
    y = e.render_offline(BS * 400)
    at = hits(y)
    check("the click and its repeats land a quarter beat apart",
          len(at) >= 3 and at[0] == CLICK_AT
          and abs(at[1] - at[0] - d) <= 1 and abs(at[2] - at[1] - d) <= 1,
          "%s (d=%d)" % (at[:4], d))
    lv = [float(abs(y[i, 0])) for i in at[:3]]
    check("and each one is quieter than the last",
          lv[1] < lv[0] * 0.6 and 0.3 < lv[2] / lv[1] < 0.6,
          "%.3f %.3f %.3f" % tuple(lv))


def t_release_stops_the_input_and_lets_the_tail_ring():
    e = rig()
    d = int(e.transport.spb * 0.25)
    e.post("master.echo", on=True)
    e.post("track.play", i=0)
    e.render_offline(BS * 100)             # past the click: it is in the line
    e.post("master.echo", on=False)
    y = e.render_offline(BS * 400)
    at = hits(y)
    check("what was already in there still comes out, twice over",
          len(at) >= 2 and abs(at[1] - at[0] - d) <= 1, str(at[:3]))
    check("and it dies away rather than going on for ever",
          float(np.abs(y[-BS * 20:]).max()) < 0.01,
          "%.4f at the end" % float(np.abs(y[-BS * 20:]).max()))


def t_off_is_off():
    plain = rig()
    plain.post("track.play", i=0)
    a = plain.render_offline(BS * 300)
    e = rig()
    e.post("master.echo", on=True)
    e.post("master.echo", on=False)
    e.post("track.play", i=0)
    b = e.render_offline(BS * 300)
    check("an echo asked for and let go in the same breath changes nothing",
          np.array_equal(a, b), "max diff %.3g" % float(np.abs(a - b).max()))


def t_panic_takes_the_tail_with_it():
    e = rig()
    e.post("master.echo", on=True)
    e.post("track.play", i=0)
    e.render_offline(BS * 20)
    e.post("panic")
    y = e.render_offline(BS * 400)
    check("ESC silences the repeats too, not just the tracks",
          float(np.abs(y).max()) < 1e-4 and not e.echo_on,
          "%.5f" % float(np.abs(y).max()))


def t_five_minutes_of_it():
    """The callback's own time, over five minutes of audio with the echo in.

    Offline, so there is no device to underrun: what this measures is the work
    per block against the 5.33 ms a block has to be ready in.
    """
    e = rig()
    e.post("master.echo", on=True)
    e.post("transport.start")
    for i in range(8):
        e.post("track.play", i=i % 8)
    e.render_offline(BS * 10)
    blocks = int(SR * 300 / BS)
    out = np.zeros((BS, 2), dtype=np.float32)
    ms = np.zeros(blocks, dtype=np.float64)
    for k in range(blocks):
        t0 = time.perf_counter()
        e._callback(out, BS, None, None)
        ms[k] = (time.perf_counter() - t0) * 1000.0
    budget = BS / SR * 1000.0
    p95 = float(np.percentile(ms, 95))
    check("five minutes of blocks stay well inside the block's own time",
          p95 < budget * 0.5 and e.xruns == 0,
          "mean %.3f ms, p95 %.3f, max %.3f, budget %.2f, xruns %d"
          % (float(ms.mean()), p95, float(ms.max()), budget, e.xruns))
    check("and the echo is still the same one ring it started with",
          e._echo.shape[0] == SR * 2 and e._echo_w < e._echo.shape[0])


if __name__ == "__main__":
    for fn in (t_a_held_echo_repeats_on_the_quarter_beat,
               t_release_stops_the_input_and_lets_the_tail_ring,
               t_off_is_off,
               t_panic_takes_the_tail_with_it,
               t_five_minutes_of_it):
        try:
            fn()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    print("\n%d checks failed" % len(FAILS) if FAILS else "\nall checks passed")
    sys.exit(1 if FAILS else 0)
