"""Task 30: the master output, recorded off the audio thread.

    PYTHONPATH=.pylibs python3 -m tests.test_record
"""
from __future__ import annotations

import gc
import os
import shutil
import sys
import tempfile
import threading
import time
import tracemalloc
import types

import numpy as np
import soundfile as sf

from loopengine.app import App
from loopengine.engine import Engine
from loopengine.recorder import ARMED, IDLE, RECORDING, Recorder

SR, B = 48000, 256
Q24 = 1.0 / 8388607          # one 24-bit step
FAILS = []


def check(name, ok, detail=""):
    print("%-72s %s%s" % (name, "PASS" if ok else "FAIL",
                          ("  " + detail) if detail else ""))
    if not ok:
        FAILS.append(name)


def signal(n, seed=0):
    k = np.arange(n, dtype=np.float64)
    return np.stack([np.sin(k * 0.013 + seed) * 0.5,
                     np.cos(k * 0.007 + seed) * 0.25], axis=1).astype(np.float32)


def feed_all(rec, x, pace=0.0):
    for off in range(0, x.shape[0], B):
        rec.feed(x[off:off + B])
        if pace:
            time.sleep(pace)


def stop(rec):
    """What the app does, with this thread standing in for the callback."""
    rec.request_stop()
    rec.feed(np.zeros((B, 2), dtype=np.float32))     # the next block names the end
    return rec.finish(timeout=60)


def take(paths):
    return np.concatenate([sf.read(p, dtype="float32", always_2d=True)[0] for p in paths])


def worst(a, b):
    return float(np.abs(a - b).max()) if a.shape == b.shape and a.size else float("inf")


def playing_engine(seconds, seed):
    e = Engine(samplerate=SR, blocksize=B, offline=True)
    e.post("track.load", i=0, buf=signal(SR * seconds, seed), sr=SR, name="sig", path="",
           bpm=0.0, conf=0.0, slices=np.zeros(0, dtype=np.int64))
    e.post("transport.quantum", v=0)
    e.post("track.play", i=0)
    return e


def t_the_file_is_what_was_fed():
    d = tempfile.mkdtemp(prefix="le-rec-")
    try:
        x = signal(SR * 3 + 101)
        rec = Recorder(SR, ring_seconds=1.0, drain_every=0.005)
        p = os.path.join(d, "a.wav")
        rec.arm(p)
        rec.begin()
        feed_all(rec, x, pace=0.0003)          # three seconds through a one-second ring
        s = stop(rec)
        y = take(s["files"])
        info = sf.info(p)
        check("every frame fed is in the file, within a 24-bit step, through a ring a third as long",
              y.shape == x.shape and worst(y, x) <= 2 * Q24 and s["dropped"] == 0,
              "%d of %d frames, worst %.1e" % (y.shape[0], x.shape[0], worst(y, x)))
        check("WAV at the engine's rate — 48000 Hz, stereo, 24-bit — nothing resampled",
              info.samplerate == SR and info.channels == 2 and info.format == "WAV"
              and info.subtype == "PCM_24", "%d Hz %s %s" % (info.samplerate, info.format, info.subtype))
        check("its length is the frames over the rate",
              s["frames"] == x.shape[0] and abs(s["seconds"] - x.shape[0] / SR) < 1e-3,
              "%.3f s" % s["seconds"])

        full = np.zeros((4096, 2), dtype=np.float32)
        full[100] = (1.0, -1.0)
        full[200] = (-1.0, 1.0)
        rec.arm(os.path.join(d, "full.wav"))
        rec.begin()
        feed_all(rec, full)
        z = take(stop(rec)["files"])
        check("a full-scale sample is written full-scale, never wrapped round",
              worst(z[[100, 200]], full[[100, 200]]) <= 2 * Q24, str(z[[100, 200]].tolist()))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def t_a_long_take_carries_on_in_the_next_file():
    d = tempfile.mkdtemp(prefix="le-rec-")
    try:
        rec = Recorder(SR, ring_seconds=1.0, drain_every=0.005, part_frames=50000)
        x = signal(123457, seed=1)
        rec.arm(os.path.join(d, "b.wav"))
        rec.begin()
        feed_all(rec, x, pace=0.0002)
        s = stop(rec)
        y = take(s["files"])
        check("past a file's limit the take carries on in the next, no frame lost or doubled at a seam",
              [os.path.basename(f) for f in s["files"]] == ["b.wav", "b-part2.wav", "b-part3.wav"]
              and y.shape == x.shape and worst(y, x) <= 2 * Q24,
              "%s, worst %.1e" % ([os.path.basename(f) for f in s["files"]], worst(y, x)))
        check("and every part is a whole WAV that opens by itself",
              [sf.info(f).frames for f in s["files"]] == [50000, 50000, 23457])
    finally:
        shutil.rmtree(d, ignore_errors=True)


def t_a_disk_that_falls_behind_costs_silence_never_length():
    d = tempfile.mkdtemp(prefix="le-rec-")
    try:
        rec = Recorder(SR, ring_seconds=0.5, drain_every=3600.0)   # a writer that will not wake
        rec.arm(os.path.join(d, "c.wav"))
        rec.begin()
        time.sleep(0.05)
        x = signal(SR * 2, seed=2)
        feed_all(rec, x)                                          # four rings' worth
        s = stop(rec)
        y = take(s["files"])
        lost = x.shape[0] - rec.cap
        check("frames a stalled writer could not keep become silence of the same length, counted",
              s["dropped"] == lost and s["gaps"] == [(0, lost)] and y.shape == x.shape
              and not np.any(y[:lost]), "dropped %d, gaps %s" % (s["dropped"], s["gaps"]))
        check("so the take keeps its length, and what it could keep is intact",
              worst(y[lost:], x[lost:]) <= 2 * Q24)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def t_the_states():
    d = tempfile.mkdtemp(prefix="le-rec-")
    try:
        rec = Recorder(SR, ring_seconds=1.0, drain_every=0.005)
        x = signal(SR // 4)
        rec.feed(x[:B])
        check("idle, a block is ignored", rec.fed == 0 and rec.state == IDLE)
        p = os.path.join(d, "d.wav")
        rec.arm(p)
        feed_all(rec, x)
        check("armed, nothing is taken until the take begins", rec.state == ARMED and rec.fed == 0)
        rec.begin()
        feed_all(rec, x)
        check("begun, it records", rec.state == RECORDING and rec.fed == x.shape[0])
        stop(rec)
        check("stopped, the file is closed and the recorder idle",
              rec.state == IDLE and sf.info(p).frames == x.shape[0] and rec._thread is None)
        q = os.path.join(d, "never.wav")
        rec.arm(q)
        s = stop(rec)
        check("armed and stopped without a frame leaves no empty file behind",
              not os.path.exists(q) and s["frames"] == 0 and s["files"] == [])
        rec.arm(q)
        try:
            rec.arm(os.path.join(d, "twice.wav"))
            refused = False
        except RuntimeError:
            refused = True
        stop(rec)
        check("a second arm while a take is open is refused, not a second writer", refused)
        slow = Recorder(SR, ring_seconds=1.0, drain_every=3600.0)
        slow.arm(os.path.join(d, "prompt.wav"))
        slow.begin()
        feed_all(slow, x)
        slow.request_stop()
        time.sleep(0.05)                      # the writer sees the stop before the callback names the end
        slow.feed(x[:B])
        t0 = time.monotonic()
        ended = slow.finish(timeout=2.0)
        check("a stop lands within a block or two, however long the writer sleeps between drains",
              ended is not None and ended["frames"] == x.shape[0],
              "%.3f s" % (time.monotonic() - t0))
        with open(p, "rb") as fh:
            before = fh.read()
        try:
            rec.arm(p)
            refused = False
            stop(rec)
        except OSError:
            refused = True
        with open(p, "rb") as fh:
            after = fh.read()
        check("a take never writes over a file already there: arming on one is refused",
              refused and before == after and rec.state == IDLE)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def t_a_crash_mid_take_leaves_a_file_that_opens():
    d = tempfile.mkdtemp(prefix="le-rec-")
    try:
        rec = Recorder(SR, ring_seconds=1.0, drain_every=0.005, header_every=0.02)
        p = os.path.join(d, "live.wav")
        rec.arm(p)
        rec.begin()
        x = signal(SR, seed=3)
        feed_all(rec, x, pace=0.0001)
        t0 = time.monotonic()
        while rec.drained < x.shape[0] and time.monotonic() - t0 < 5:
            time.sleep(0.01)
        time.sleep(0.15)                          # one header update after the last write
        live = sf.info(p).frames                  # read while the take is still open
        stop(rec)
        check("while a take runs its header is kept current, so a crash leaves a file that opens",
              live == x.shape[0], "%d of %d frames readable mid-take" % (live, x.shape[0]))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def t_a_disk_that_fills_says_so():
    d = tempfile.mkdtemp(prefix="le-rec-")
    try:
        rec = Recorder(SR, ring_seconds=4.0, drain_every=0.005)
        rec.arm(os.path.join(d, "full.wav"))
        real = rec._file.write
        calls = [0]

        def filling(data):
            calls[0] += 1
            if calls[0] > 3:
                raise OSError(28, "No space left on device")
            return real(data)
        rec._file.write = filling
        rec.begin()
        x = signal(SR * 2, seed=4)
        feed_all(rec, x, pace=0.0002)
        t0 = time.monotonic()
        while rec.state != IDLE and time.monotonic() - t0 < 5:
            time.sleep(0.01)
        s = rec.finish(timeout=5)
        y = take(s["files"])
        check("a disk that fills mid-take stops the writer, and the reason names the file",
              rec.state == IDLE and "No space left" in s["error"] and "full.wav" in s["error"],
              s["error"][:80])
        check("what was written before it is a file that opens",
              0 < y.shape[0] == s["frames"] and worst(y, x[:y.shape[0]]) <= 2 * Q24,
              "%d frames" % y.shape[0])
        fed = rec.fed
        rec.feed(x[:B])
        check("and the callback carries on without it", rec.fed == fed)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def t_the_engine_records_what_it_plays():
    d = tempfile.mkdtemp(prefix="le-rec-")
    try:
        e = playing_engine(2, seed=5)
        e.render_offline(B * 8)
        rec = e.recorder = Recorder(SR, ring_seconds=2.0, drain_every=0.005)
        rec.arm(os.path.join(d, "take.wav"))
        e.render_offline(B * 8)
        check("armed with the clock stopped, the engine plays and the take waits", rec.fed == 0)
        e.post("transport.start")
        e.capture(2.0)
        e.render_offline(B * 300)
        heard = e.end_capture().copy()
        rec.request_stop()
        e.render_offline(B)                                       # the callback names the end
        s = rec.finish(timeout=30)
        got = take(s["files"])
        check("the take starts on the block the clock did and holds what the device was handed",
              got.shape == heard.shape and worst(got, heard) <= 2 * Q24
              and float(np.abs(heard).max()) > 0.1,
              "%d frames, worst %.1e" % (got.shape[0], worst(got, heard)))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def t_the_callback_builds_nothing_and_touches_no_file():
    d = tempfile.mkdtemp(prefix="le-rec-")
    try:
        e = playing_engine(60, seed=6)
        e.post("transport.start")
        out = np.zeros((B, 2), dtype=np.float32)
        for _ in range(200):
            e._callback(out, B, None, None)

        def tracked(n):
            gc.collect()
            gc.disable()
            try:
                c0 = gc.get_count()[0]
                for _ in range(n):
                    e._callback(out, B, None, None)
                return gc.get_count()[0] - c0
            finally:
                gc.enable()

        without = tracked(3000)
        rec = e.recorder = Recorder(SR, ring_seconds=60.0, drain_every=3600.0)
        rec.arm(os.path.join(d, "alloc.wav"))             # its writer waits: only the callback runs
        rec.begin()
        for _ in range(50):
            e._callback(out, B, None, None)
        with_rec = tracked(3000)
        tracemalloc.start()
        for _ in range(10):                   # so the counter's own integer is traced too
            e._callback(out, B, None, None)
        s1 = tracemalloc.take_snapshot()
        for _ in range(200):                  # tracing is slow; a leak per block shows 200-fold
            e._callback(out, B, None, None)
        s2 = tracemalloc.take_snapshot()
        tracemalloc.stop()
        mine = [st for st in s2.compare_to(s1, "filename")
                if st.traceback[0].filename.endswith(os.sep + "recorder.py")]
        grown = sum(st.size_diff for st in mine)
        check("recording adds no tracked object to the callback: the collector counts the same",
              with_rec == without, "%+d without, %+d with, over 3,000 blocks" % (without, with_rec))
        check("and holds on to nothing: recorder.py's memory over 200 traced blocks, net",
              grown == 0 and rec.fed == 3260 * B, "%+d bytes, %d blocks taken" % (grown, rec.fed // B))
        rec.request_stop()
        e._callback(out, B, None, None)
        rec.finish(timeout=60)

        who = set()
        real_write = sf.SoundFile.write

        def spy(self, data):
            who.add(threading.get_ident())
            return real_write(self, data)
        sf.SoundFile.write = spy
        try:
            rec2 = e.recorder = Recorder(SR, ring_seconds=5.0, drain_every=0.002)
            rec2.arm(os.path.join(d, "spy.wav"))
            rec2.begin()
            for k in range(600):
                e._callback(out, B, None, None)
                if k % 20 == 0:
                    time.sleep(0.003)
            rec2.request_stop()
            e._callback(out, B, None, None)
            s = rec2.finish(timeout=30)
        finally:
            sf.SoundFile.write = real_write
        me = threading.get_ident()
        check("every write to the file came from the writer, none from the thread running the callback",
              e._audio_ident == me and who and me not in who and s["frames"] == 600 * B,
              "%d writing thread(s), %d frames" % (len(who), s["frames"]))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def t_rec_from_the_panel():
    d = tempfile.mkdtemp(prefix="le-rec-")
    try:
        e = playing_engine(4, seed=7)
        takes = os.path.join(d, "takes")
        a = App(e, roots=[d], inbox=os.path.join(d, ".inbox"),
                sessions_dir=os.path.join(d, "sessions"),
                session_key=os.path.join(d, "cfg", "session.key"), recordings_dir=takes)
        sent = []
        a.hub = types.SimpleNamespace(text=sent.append, peaks=lambda *x, **k: None,
                                      pad_peaks=lambda *x, **k: None, broadcast=lambda *x: None)
        e.render_offline(B * 4)

        def done(n):
            t0 = time.monotonic()
            while time.monotonic() - t0 < 10:
                got = [m for m in sent if m.get("op") == "record.done"]
                if len(got) >= n:
                    return got[n - 1]
                time.sleep(0.01)
            return None

        a.handle({"op": "record"})
        e.render_offline(B * 20)
        check("REC with the clock stopped arms a take, and nothing is taken yet",
              a.record_state()["state"] == "armed" and e.recorder.fed == 0)
        e.post("transport.start")
        e.render_offline(B * 100)
        st = a.record_state()
        check("RUN starts it; elapsed is the frames taken over the rate",
              st["state"] == "recording" and st["elapsed_s"] == round(100 * B / SR, 2),
              "%s %.2f s" % (st["state"], st["elapsed_s"]))
        a.handle({"op": "record"})
        e.render_offline(B)
        m1 = done(1)
        check("REC again ends it: the take is in the recordings folder at its full length",
              m1 is not None and not m1["disarmed"] and m1["frames"] == 100 * B
              and os.path.dirname(m1["path"]) == os.path.realpath(takes)
              and sf.info(m1["path"]).frames == 100 * B,
              os.path.basename(m1["path"]) if m1 else "no reply")
        a.handle({"op": "record"})
        e.render_offline(B * 10)
        check("REC while the clock runs starts at once, under a name of its own",
              a.record_state()["state"] == "recording" and e.recorder.fed == 10 * B
              and e.recorder.path != m1["path"])
        a.handle({"op": "record"})
        e.render_offline(B)
        m2 = done(2)
        e.post("transport.stop")
        e.render_offline(B)
        a.handle({"op": "record"})
        a.handle({"op": "record"})
        e.render_offline(B)
        m3 = done(3)
        check("armed and pressed again, the take is called off and leaves no file",
              m2 is not None and m3 is not None and m3["disarmed"]
              and sorted(os.listdir(takes)) == sorted(os.path.basename(m["path"]) for m in (m1, m2)),
              str(sorted(os.listdir(takes))))
        check("the last take stays in the telemetry, for a panel opened later",
              a.record_state()["last"]["name"] == os.path.basename(m2["path"]))
        blocker = os.path.join(d, "a-file")
        with open(blocker, "w") as fh:
            fh.write("not a folder")
        a.recordings_dir = os.path.join(blocker, "takes")
        a.handle({"op": "record"})
        m4 = done(4)
        check("a REC that cannot open its file says why, in the reply and the telemetry",
              m4 is not None and "Not recording" in m4["error"]
              and a.record_state()["state"] == "idle" and a.record_state()["error"] == m4["error"],
              (m4 or {}).get("error", "no reply")[:70])
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    for fn in (t_the_file_is_what_was_fed,
               t_a_long_take_carries_on_in_the_next_file,
               t_a_disk_that_falls_behind_costs_silence_never_length,
               t_the_states,
               t_a_crash_mid_take_leaves_a_file_that_opens,
               t_a_disk_that_fills_says_so,
               t_the_engine_records_what_it_plays,
               t_the_callback_builds_nothing_and_touches_no_file,
               t_rec_from_the_panel):
        try:
            fn()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    print("\n%d checks failed" % len(FAILS) if FAILS else "\nall checks passed")
    sys.exit(1 if FAILS else 0)
