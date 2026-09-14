"""Task 24: zoom on the waveform panel — the server's half, and the view maths.

    PYTHONPATH=.pylibs python3 -m tests.test_zoom

The view lives in the browser (loopengine/ui/view.js). Its checks run under
node from tests/view_check.js, against the file the panel loads, and are
relayed here so one command covers both halves. Without node on PATH that
relay FAILS rather than skipping: an untested zoom must not read as a pass.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import types

import numpy as np

from loopengine import dsp
from loopengine.app import App
from loopengine.engine import Engine
from loopengine.server import (BIN_PEAKS_RANGE, OP_BIN, Hub, Server,
                               read_frame)

HERE = os.path.dirname(os.path.abspath(__file__))
SR = 48000
FAILS = []


def check(name, ok, detail=""):
    print("%-70s %s%s" % (name, "PASS" if ok else "FAIL",
                          ("  " + detail) if detail else ""))
    if not ok:
        FAILS.append(name)


def noise(n, seed=3):
    r = np.random.default_rng(seed)
    return (r.standard_normal((n, 2)) * 0.3).astype(np.float32)


def q16(x):
    return np.clip(np.rint(x * 32767.0), -32767, 32767).astype("<i2").ravel()


# -- dsp.peaks_range --------------------------------------------------------
def t_range_agrees_with_peaks():
    buf = noise(SR * 5)
    for s, b in ((0, 2048), (12345, 1000), (SR, 1100)):
        e = s + b * 97
        what, env = dsp.peaks_range(buf, s, e, b)
        ref = dsp.peaks(buf[s:e], b)
        check("peaks_range equals peaks() over the same span at %d buckets" % b,
              what == "env" and np.array_equal(env, ref), "%d..%d" % (s, e))
    n = (buf.shape[0] // 2048) * 2048
    check("and over a whole file that divides evenly",
          np.array_equal(dsp.peaks_range(buf[:n], 0, n, 2048)[1], dsp.peaks(buf[:n], 2048)))


def t_range_keeps_the_tail_peaks_drops():
    n = SR * 3 + 777
    buf = np.zeros((n, 2), dtype=np.float32)
    buf[-1] = 0.99                                  # a transient in the very last sample
    hop = n // 2048
    dropped = n - (n // hop) * hop
    check("peaks() never envelopes the last samples of a file",
          float(dsp.peaks(buf, 2048)[:, 1].max()) == 0.0,
          "hop %d: the last %d samples are discarded" % (hop, dropped))
    env = dsp.peaks_range(buf, 0, n, 2048)[1]
    check("peaks_range keeps them: a transient in the final sample shows",
          abs(float(env[-1, 1]) - 0.99) < 1e-6, "last bucket max %.2f" % env[-1, 1])


def t_range_is_exact_per_bucket_on_uneven_spans():
    buf = noise(SR * 4 + 13)
    mono = dsp._mono(buf)
    s, e, b = 777, buf.shape[0] - 3, 1103
    what, env = dsp.peaks_range(buf, s, e, b)
    edges = s + (np.arange(b + 1) * (e - s)) // b
    exact = all(env[k, 0] == mono[edges[k]:edges[k + 1]].min()
                and env[k, 1] == mono[edges[k]:edges[k + 1]].max() for k in range(b))
    check("an uneven span is exact bucket by bucket, covering every sample",
          what == "env" and exact and edges[0] == s and edges[-1] == e)


def t_range_chunking_cannot_change_the_answer():
    buf = noise(SR * 3)
    a = dsp.peaks_range(buf, 101, buf.shape[0] - 7, 999)[1]
    old = dsp.RANGE_CHUNK
    try:
        dsp.RANGE_CHUNK = 1000
        b = dsp.peaks_range(buf, 101, buf.shape[0] - 7, 999)[1]
    finally:
        dsp.RANGE_CHUNK = old
    check("folding to mono in small chunks gives the identical envelope",
          np.array_equal(a, b))


def t_range_hands_back_samples_below_one_per_bucket():
    buf = noise(SR)
    what, sm = dsp.peaks_range(buf, 5000, 5600, 1100)
    check("600 samples on 1,100 columns come back as the samples themselves",
          what == "samples" and np.array_equal(sm, dsp._mono(buf)[5000:5600]),
          "%s, %d values" % (what, sm.shape[0]))


def t_range_clamps():
    buf = noise(SR)
    a = dsp.peaks_range(buf, -500, 10 ** 12, 64)
    b = dsp.peaks_range(buf, 900, 100, 64)
    check("a span past both ends is clamped to the file",
          a[0] == "env" and a[1].shape == (64, 2))
    check("an inverted span is empty, not an error", b[1].shape[0] == 0)


# -- the op -------------------------------------------------------------------
class Panel:
    """Stands in for a websocket client: records what it is sent."""

    def __init__(self):
        self.frames = []

    def send(self, op, payload):
        self.frames.append((op, payload))
        return True


def parse(payload):
    k, subj, i, flags, req, start, end, frames, count = \
        struct.unpack("!BBBBIIIII", payload[:24])
    return dict(kind=k, subj=subj, i=i, flags=flags, req=req, start=start,
                end=end, frames=frames, count=count,
                vals=np.frombuffer(payload[24:], dtype="<i2"))


def rig(buf=None):
    eng = Engine(samplerate=SR, blocksize=256, offline=True)
    a = App(eng, roots=[HERE], inbox="/tmp/le-inbox")
    broadcasts = []
    a.hub = types.SimpleNamespace(text=lambda o: None, peaks=lambda *x, **k: None,
                                  pad_peaks=lambda *x, **k: None,
                                  broadcast=lambda *x: broadcasts.append(x))
    if buf is not None:
        eng.post("track.load", i=0, buf=buf, sr=SR, name="a.wav", path="/tmp/a.wav",
                 bpm=0.0, conf=0.0, slices=np.zeros(0, dtype=np.int64))
        eng.render_offline(256)
    return a, eng, broadcasts


def t_the_reply_goes_to_the_panel_that_asked():
    buf = noise(SR * 6)
    a, eng, broadcasts = rig(buf)
    asker, other = Panel(), Panel()
    before = (len(eng.cmds), len(eng._pending))
    a.handle({"op": "peaks.range", "kind": "track", "i": 0, "start": 48000,
              "end": 48000 + 1100 * 50, "buckets": 1100, "req": 77}, asker)
    check("one reply, to the panel that asked, broadcast to nobody",
          len(asker.frames) == 1 and not other.frames and not broadcasts,
          "%d replies, %d broadcasts" % (len(asker.frames), len(broadcasts)))
    if not asker.frames:
        return
    op, payload = asker.frames[0]
    h = parse(payload)
    check("the header names the request, the span and the file",
          op == OP_BIN and h["kind"] == BIN_PEAKS_RANGE and h["subj"] == 0
          and h["i"] == 0 and h["flags"] == 0 and h["req"] == 77
          and (h["start"], h["end"]) == (48000, 103000)
          and h["frames"] == buf.shape[0] and h["count"] == 1100,
          "req %(req)d span %(start)d..%(end)d frames %(frames)d count %(count)d" % h)
    check("and the values are peaks_range of that span at 16 bits",
          np.array_equal(h["vals"], q16(dsp.peaks_range(buf, 48000, 103000, 1100)[1])),
          "%d values" % h["vals"].shape[0])
    check("the engine never sees a view request",
          (len(eng.cmds), len(eng._pending)) == before)


def t_it_reads_the_buffer_that_is_actually_shown():
    buf = noise(SR * 2, seed=5)
    a, eng, _ = rig(buf)
    side = noise(SR * 2, seed=9) * 0.5
    eng.post("track.variant", i=0, mode="SIDE", buf=side)
    eng.render_offline(256)
    p = Panel()
    a.handle({"op": "peaks.range", "kind": "track", "i": 0, "start": 0,
              "end": 44000, "buckets": 1000, "req": 1}, p)
    check("a track in SIDE mode is enveloped from the SIDE buffer it plays",
          bool(p.frames) and np.array_equal(parse(p.frames[0][1])["vals"],
                                            q16(dsp.peaks_range(side, 0, 44000, 1000)[1])))
    eng.post("pad.take", i=3, track=0, ls=100, le=20000)
    eng.render_offline(256)
    eng.post("track.load", i=0, buf=noise(SR * 2, seed=11), sr=SR, name="b.wav",
             path="/tmp/b.wav", bpm=0.0, conf=0.0, slices=np.zeros(0, dtype=np.int64))
    eng.render_offline(256)
    k = Panel()
    a.handle({"op": "peaks.range", "kind": "key", "i": 3, "start": 0, "end": 300,
              "buckets": 1000, "req": 2}, k)
    h = parse(k.frames[0][1]) if k.frames else None
    kb = eng.pads[3].buf
    check("a key is enveloped from its own buffer after its track moved on",
          h is not None and h["subj"] == 1 and h["i"] == 3 and h["flags"] == 1
          and kb is not eng.tracks[0].buf
          and np.array_equal(h["vals"], q16(dsp._mono(kb[0:300]))),
          "raw samples: %s" % (h and h["flags"] == 1))


def t_bad_requests_neither_raise_nor_reply():
    a, eng, _ = rig(noise(SR))
    p = Panel()
    bad = [{"op": "peaks.range"},
           {"op": "peaks.range", "kind": "track", "i": "x"},
           {"op": "peaks.range", "kind": "track", "i": 99, "start": 0, "end": 10, "buckets": 10},
           {"op": "peaks.range", "kind": "key", "i": 0, "start": 0, "end": 10, "buckets": 10},
           {"op": "peaks.range", "kind": "track", "i": 1, "start": 0, "end": 10, "buckets": 10},
           {"op": "peaks.range", "kind": "track", "i": 0, "start": None, "end": [], "buckets": {}}]
    raised = 0
    for m in bad:
        try:
            a.handle(m, p)
        except Exception:
            raised += 1
    check("malformed, out-of-range or empty-slot requests neither raise nor reply",
          raised == 0 and not p.frames, "%d raised, %d replies" % (raised, len(p.frames)))
    try:
        a.handle({"op": "peaks.range", "kind": "track", "i": 0, "start": 0,
                  "end": 1000, "buckets": 64, "req": 1})
        ok = True
    except Exception:
        ok = False
    check("a request with no panel to answer is dropped quietly", ok)
    p = Panel()
    a.handle({"op": "peaks.range", "kind": "track", "i": 0, "start": 0, "end": SR,
              "buckets": 10 ** 9, "req": 5}, p)
    check("the bucket count is capped", bool(p.frames) and parse(p.frames[0][1])["count"] == 8192)


def _masked(obj):
    data, mask = json.dumps(obj).encode(), os.urandom(4)
    n = len(data)
    head = bytes([0x81]) + (bytes([0x80 | n]) if n < 126
                            else bytes([0x80 | 126]) + struct.pack("!H", n))
    return head + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(data))


def t_through_a_real_socket():
    """The op is only reachable if the server hands the client to handle()."""
    buf = noise(SR * 3)
    a, eng, _ = rig(buf)
    a.hub = Hub()
    srv = Server(a, port=0).start()
    got, status = None, b""
    try:
        s = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall(("GET /ws?t=%s HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\n"
                   "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                   "Sec-WebSocket-Version: 13\r\n\r\n" % (a.token, key)).encode())
        f = s.makefile("rb")
        status = f.readline()
        while f.readline() not in (b"\r\n", b""):
            pass
        s.sendall(_masked({"op": "peaks.range", "kind": "track", "i": 0, "start": 1000,
                           "end": 7400, "buckets": 64, "req": 4242}))
        deadline = time.time() + 5
        while time.time() < deadline:
            op, data = read_frame(f)
            if op is None:
                break
            if op == OP_BIN and data and data[0] == BIN_PEAKS_RANGE:
                got = parse(data)
                break
        s.close()
    finally:
        srv.shutdown()
    check("through the real server, the panel that asks gets its reply",
          got is not None and got["req"] == 4242 and got["count"] == 64,
          status.strip().decode(errors="replace"))


# -- the view, in the browser's own file ------------------------------------
def t_view_maths_under_node():
    node = shutil.which("node") or shutil.which("nodejs")
    if not node:
        check("the view maths runs under node", False,
              "node is not on PATH — the panel's zoom arithmetic is UNTESTED here")
        return
    r = subprocess.run([node, os.path.join(HERE, "view_check.js")],
                       capture_output=True, text=True, timeout=120)
    rows = [l.split("\t") for l in r.stdout.splitlines()
            if l.startswith(("PASS\t", "FAIL\t"))]
    if not rows:
        check("view_check.js reported its checks", False,
              (r.stderr or r.stdout).strip()[-160:])
        return
    for row in rows:
        check("view: " + row[1], row[0] == "PASS", row[2] if len(row) > 2 else "")


if __name__ == "__main__":
    for fn in (t_range_agrees_with_peaks,
               t_range_keeps_the_tail_peaks_drops,
               t_range_is_exact_per_bucket_on_uneven_spans,
               t_range_chunking_cannot_change_the_answer,
               t_range_hands_back_samples_below_one_per_bucket,
               t_range_clamps,
               t_the_reply_goes_to_the_panel_that_asked,
               t_it_reads_the_buffer_that_is_actually_shown,
               t_bad_requests_neither_raise_nor_reply,
               t_through_a_real_socket,
               t_view_maths_under_node):
        try:
            fn()
        except Exception as exc:
            check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    print("\n%d checks failed" % len(FAILS) if FAILS else "\nall checks passed")
    sys.exit(1 if FAILS else 0)
