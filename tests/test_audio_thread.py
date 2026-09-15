"""Task 28: what the audio thread builds, and what lands on it.

    PYTHONPATH=.pylibs python3 -m tests.test_audio_thread
"""
from __future__ import annotations

import gc
import sys
import threading

import numpy as np

from loopengine.engine import QUEUE_CAP, Engine

SR, B = 48000, 256
FAILS = []


def check(name, ok, detail=""):
    print("%-72s %s%s" % (name, "PASS" if ok else "FAIL",
                          ("  " + detail) if detail else ""))
    if not ok:
        FAILS.append(name)


def loaded(n=1):
    e = Engine(samplerate=SR, blocksize=B, offline=True)
    ramp = np.linspace(-1, 1, SR * 4, dtype=np.float32)
    for i in range(n):
        e.post("track.load", i=i, buf=np.stack([ramp, ramp], axis=1), sr=SR,
               name="r%d" % i, path="", bpm=0.0, conf=0.0,
               slices=np.zeros(0, dtype=np.int64))
    e.render_offline(B)
    return e


def at_bar(e):
    e.post("transport.quantum", v=5)
    e.post("transport.start")
    e.render_offline(B * 4)


def t_the_queue_is_never_rebuilt():
    e = loaded(3)
    at_bar(e)
    ops, kws = e._q_op, e._q_kw
    peak = 0
    for k in range(2000):
        e.post("track.loop", i=k % 3, ls=100 + k, le=50000 + k)
        if k % 50 == 0:
            e.post("track.play", i=k % 3)
        if k % 7 == 0:
            e.render_offline(B)
            peak = max(peak, e._q_n)
    e.render_offline(B)
    peak = max(peak, e._q_n)
    e.post("transport.stop")
    e.render_offline(B)
    check("2,000 edits and launches go through the same two slot arrays",
          e._q_op is ops and e._q_kw is kws and len(ops) == QUEUE_CAP
          and len(kws) == QUEUE_CAP and e._q_n == 0)
    check("a drag holds one entry per (op, track) at most", peak <= 6, "peak %d waiting" % peak)
    last = {0: 1998, 1: 1999, 2: 1997}
    check("the stop fires the last edit for each track",
          all((e.tracks[i].loop_start, e.tracks[i].loop_end) == (100 + k, 50000 + k)
              for i, k in last.items()),
          str([(e.tracks[i].loop_start, e.tracks[i].loop_end) for i in range(3)]))
    check("and discards the launches queued among them",
          not any(e.tracks[i].playing for i in range(3)))


def t_collapse_keeps_arrival_order():
    e = loaded(1)
    at_bar(e)
    e.post("track.loop", i=0, ls=10, le=20000)
    e.post("track.rev", i=0)
    e.post("track.loop", i=0, ls=30, le=40000)
    e.render_offline(B)
    q = e._pending
    check("a later edit to the same thing replaces the earlier and goes to the back",
          [op for op, _ in q] == ["track.rev", "track.loop"] and q[1][1]["ls"] == 30,
          str([op for op, _ in q]))


def t_a_full_queue_applies_and_says_so():
    e = loaded(1)
    at_bar(e)
    for i in range(QUEUE_CAP + 44):
        e.post("track.loop", i=i, ls=10, le=20000)
    e.render_offline(B)
    check("past capacity a command applies at once and is counted, never dropped",
          e._q_n == QUEUE_CAP and e.queue_overflow == 44
          and e.snapshot()["queue_overflow"] == 44,
          "%d waiting, %d applied at once" % (e._q_n, e.queue_overflow))


def t_solo_still_silences_the_rest():
    e = Engine(samplerate=SR, blocksize=B, offline=True)
    t = np.arange(SR) / SR
    for i, f in ((0, 220.0), (1, 330.0)):
        y = (np.sin(2 * np.pi * f * t) * 0.5).astype(np.float32)
        e.post("track.load", i=i, buf=np.stack([y, y], 1), sr=SR, name="s%d" % i,
               path="", bpm=0.0, conf=0.0, slices=np.zeros(0, dtype=np.int64))
    e.post("transport.quantum", v=0)
    e.post("track.play", i=0)
    e.post("track.play", i=1)
    e.post("track.solo", i=0, v=True)
    e.render_offline(B * 20)
    check("with a track soloed the scan still silences the others",
          e.tracks[1]._g < 1e-3 and e.tracks[0]._g > 0.1,
          "soloed gain %.2f, other %.4f" % (e.tracks[0]._g, e.tracks[1]._g))


def t_a_collection_on_the_audio_thread_is_counted():
    e = loaded(1)
    check("the engine knows which thread runs its callback",
          e._audio_ident == threading.get_ident())
    e.watch_gc()
    try:
        before = e.gc_audio
        gc.collect()
        snap = e.snapshot()
    finally:
        e.unwatch_gc()
    counted = e.gc_audio
    check("a collection that runs there is counted, with its length",
          counted >= before + 1 and e.gc_audio_max_ms > 0.0 and snap["gc_audio"] >= before + 1,
          "longest %.3f ms" % e.gc_audio_max_ms)
    gc.collect()
    check("once unwatched, nothing more is counted", e.gc_audio == counted)
    e.watch_gc()
    try:
        th = threading.Thread(target=gc.collect)
        th.start()
        th.join()
    finally:
        e.unwatch_gc()
    check("a collection on another thread is not blamed on the audio thread",
          e.gc_audio == counted)


if __name__ == "__main__":
    for fn in (t_the_queue_is_never_rebuilt,
               t_collapse_keeps_arrival_order,
               t_a_full_queue_applies_and_says_so,
               t_solo_still_silences_the_rest,
               t_a_collection_on_the_audio_thread_is_counted):
        try:
            fn()
        except Exception as exc:
            check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    print("\n%d checks failed" % len(FAILS) if FAILS else "\nall checks passed")
    sys.exit(1 if FAILS else 0)
