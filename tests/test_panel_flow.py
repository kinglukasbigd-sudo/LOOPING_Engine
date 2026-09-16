"""Work order 8: the panel's own message flow.

Every check here drives the app with the JSON the browser sends — never the
engine API — because that is the gap the key-wiping bug lived in. The engine
was right, Task 19's test drove the engine, and the panel sent something else:
choosing ONE / GATE / LOOP sent `pads.map`, which replaces all 16 keys with
the focused track's slices.

    PYTHONPATH=.pylibs python3 -m tests.test_panel_flow
"""
from __future__ import annotations

import io
import os
import re
import shutil
import sys
import tempfile
import time
import types

import numpy as np
import soundfile as sf

from loopengine.app import App
from loopengine.engine import Engine

SR, B = 48000, 256
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []


def check(name, ok, detail=""):
    print("%-74s %s%s" % (name, "PASS" if ok else "FAIL",
                          ("  " + detail) if detail else ""))
    if not ok:
        FAILS.append(name)


def tone(path, secs, f):
    t = np.arange(int(SR * secs)) / SR
    y = (np.sin(2 * np.pi * f * t) * 0.4).astype(np.float32)
    sf.write(path, np.stack([y, y], axis=1), SR, subtype="PCM_16")
    return os.path.realpath(path)


def hz(x, sr=SR):
    """The loudest frequency in a rendered block."""
    m = x.mean(axis=1)
    if float(np.abs(m).max()) < 1e-4:
        return 0.0
    X = np.abs(np.fft.rfft(m * np.hanning(m.size)))
    return float(np.fft.rfftfreq(m.size, 1.0 / sr)[int(np.argmax(X))])


class Panel:
    """What the browser is: a socket that carries ops, and a state snapshot."""

    def __init__(self, base, memory_mb=1024):
        self.base = base
        self.e = Engine(samplerate=SR, blocksize=B, offline=True, memory_mb=memory_mb)
        self.a = App(self.e, roots=[base], inbox=os.path.join(base, ".inbox"),
                     sessions_dir=os.path.join(base, "sessions"),
                     session_key=os.path.join(base, "cfg", "session.key"),
                     recordings_dir=os.path.join(base, "takes"))
        self.sent = []
        self.a.hub = types.SimpleNamespace(text=self.sent.append, peaks=lambda *x, **k: None,
                                           pad_peaks=lambda *x, **k: None,
                                           broadcast=lambda *x: None)

    def send(self, op, **kw):
        self.a.handle(dict(kw, op=op))          # exactly what app.js does
        self.settle()
        return self

    def settle(self, n=6):
        for _ in range(n):
            self.e.render_offline(B)

    def wait(self, pred, secs=15.0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < secs:
            self.settle(2)
            if pred():
                return True
            time.sleep(0.01)
        return False

    def load(self, i, path):
        self.send("load", i=i, path=path)
        return self.wait(lambda: self.e.tracks[i].src is not None
                         and self.e.tracks[i].name == os.path.basename(path))

    def key(self, i):
        p = self.e.pads[i]
        return (p.name, p.loop_start, p.loop_end, id(p.buf), p.mode)

    def render(self, blocks=60):
        return self.e.render_offline(B * blocks)


def base_dir():
    d = tempfile.mkdtemp(prefix="le-panel-")
    os.makedirs(os.path.join(d, "music"))
    return d


def t_every_op_the_panel_sends_is_handled():
    src = io.open(os.path.join(ROOT, "loopengine", "ui", "app.js"), encoding="utf-8").read()
    ops = set()
    for line in src.splitlines():
        if "send({" in line or "send({ op" in line:
            ops.update(re.findall(r"op:\s*(?:[a-zA-Z]+\s*\?\s*)?'([a-z][a-z.]*)'", line))
            ops.update(re.findall(r":\s*'([a-z][a-z.]*)'\s*\}", line))
    handler = io.open(os.path.join(ROOT, "loopengine", "app.py"), encoding="utf-8").read()
    named = set(re.findall(r'op == "([a-z][a-z._]*)"', handler))
    prefixes = ("track.", "transport.", "pad.", "master.", "clip.")
    passthrough = {"panic", "probe"}
    missing = sorted(o for o in ops
                     if o not in named and o not in passthrough
                     and not o.startswith(prefixes))
    check("every op the panel sends is one the app routes",
          not missing and len(ops) > 20, "%d ops, missing: %s" % (len(ops), missing or "none"))
    check("neither confirmation runs on a clock: they wait for the press",
          "CONFIRM_MS" not in src
          and re.search(r"function cancelMap\(\)[\s\S]{0,300}?setTimeout", src) is None
          and re.search(r"function cancelAssign\(\)[\s\S]{0,300}?setTimeout", src) is None)
    check("the panel asks for a remap in exactly one place, and it carries confirm",
          src.count("'pads.map'") == 1 and re.search(r"op: 'pads\.map'[^}]*confirm: true", src)
          is not None, "%d senders" % src.count("'pads.map'"))


def t_the_segment_says_whose_mode_it_shows():
    html = io.open(os.path.join(ROOT, "loopengine", "ui", "index.html"), encoding="utf-8").read()
    css = io.open(os.path.join(ROOT, "loopengine", "ui", "app.css"), encoding="utf-8").read()
    js = io.open(os.path.join(ROOT, "loopengine", "ui", "app.js"), encoding="utf-8").read()
    check("the pads bar names whose mode the segment is showing, in a reserved box",
          'id="pmode-for"' in html and "NEW KEYS" in html
          and re.search(r"#pmode-for\s*\{[^}]*width: var\(--w-pmode\)", css) is not None)
    check("and the panel never shows one subject while editing another",
          "function padModeShown()" in js and js.count("padModeShown()") >= 3
          and "'KEY ' + (PAD_CAPS[k]" in js)


def t_each_key_keeps_its_own_mode():
    d = base_dir()
    try:
        bass = tone(os.path.join(d, "music", "bass.wav"), 1.0, 110.0)
        p = Panel(d)
        p.load(0, bass)
        p.send("pad.take", i=0, track=0, ls=0, le=20000)
        p.send("pad.assign", i=0, mode="LOOP")
        p.send("pad.take", i=1, track=0, ls=20000, le=40000)
        p.send("pad.assign", i=1, mode="GATE")
        check("two keys hold two different modes at once",
              (p.e.pads[0].mode, p.e.pads[1].mode) == ("LOOP", "GATE"),
              "%s %s" % (p.e.pads[0].mode, p.e.pads[1].mode))
        p.send("session.save")
        p.wait(lambda: any(m.get("op") == "session.saved" for m in p.sent))
        name = [m for m in p.sent if m.get("op") == "session.saved"][-1]["name"]
        r = Panel(d)
        r.send("session.load", name=name)
        r.wait(lambda: any(m.get("op") == "session.loaded" for m in r.sent))
        r.settle(8)
        check("and each comes back with its own mode after a restart",
              (r.e.pads[0].mode, r.e.pads[1].mode) == ("LOOP", "GATE"),
              "%s %s" % (r.e.pads[0].mode, r.e.pads[1].mode))
        r.send("pad.assign", i=0, mode="ONE")
        check("changing one key's mode leaves every other key alone",
              r.e.pads[0].mode == "ONE" and r.e.pads[1].mode == "GATE"
              and r.key(1)[:3] == ("bass.wav", 20000, 40000))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def t_loading_a_file_cannot_touch_a_key():
    d = base_dir()
    try:
        bass = tone(os.path.join(d, "music", "bass.wav"), 1.0, 110.0)
        hats = tone(os.path.join(d, "music", "hats.wav"), 1.0, 4000.0)
        p = Panel(d)
        check("the browser's load op fills the track", p.load(0, bass))
        p.send("track.loop", i=0, ls=4000, le=40000)
        p.send("pad.take", i=0, track=0, ls=4000, le=40000)     # ASSIGN then the key cap
        before = p.key(0)
        check("the assign gesture puts the track's file and region on the key",
              before[0] == "bass.wav" and before[1:3] == (4000, 40000), str(before[:3]))

        check("loading another file onto that track works", p.load(0, hats))
        after = p.key(0)
        check("and the key is untouched — same file, same region, same buffer",
              after == before, "%s -> %s" % (before[:3], after[:3]))
        check("every other key is untouched too",
              not any(q.loaded for q in p.e.pads[1:]))

        p.send("pad.trigger", i=0)
        f = hz(p.render(60))
        check("and pressing the key plays the bass it was given, not the hats on the track",
              abs(f - 110.0) < 6.0, "%.1f Hz, wanted 110 Hz (the track holds 4000 Hz)" % f)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def t_a_mode_is_only_a_mode():
    d = base_dir()
    try:
        bass = tone(os.path.join(d, "music", "bass.wav"), 1.0, 110.0)
        hats = tone(os.path.join(d, "music", "hats.wav"), 1.0, 4000.0)
        p = Panel(d)
        p.load(0, bass)
        p.send("pad.take", i=0, track=0, ls=4000, le=40000)
        p.load(0, hats)
        before = p.key(0)
        p.send("pad.assign", i=0, mode="GATE")          # what a mode button sends now
        after = p.key(0)
        check("choosing a mode changes the mode and nothing else",
              after[4] == "GATE" and after[:4] == before[:4],
              "%s -> %s" % (before[3:], after[3:]))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def t_map_asks_before_it_replaces_assigned_keys():
    d = base_dir()
    try:
        bass = tone(os.path.join(d, "music", "bass.wav"), 1.0, 110.0)
        hats = tone(os.path.join(d, "music", "hats.wav"), 1.0, 4000.0)
        p = Panel(d)
        p.load(0, hats)
        p.send("pads.map", track=0, mode="ONE")
        check("with every key empty, MAP fills them without asking",
              all(q.loaded for q in p.e.pads) and p.e.pads[0].name == "hats.wav")

        p2 = Panel(d)
        p2.load(0, bass)
        p2.send("pad.take", i=0, track=0, ls=4000, le=40000)
        p2.load(0, hats)
        before = [p2.key(i) for i in range(len(p2.e.pads))]
        p2.send("pads.map", track=0, mode="ONE")
        check("with a key assigned, MAP alone is refused and nothing changes",
              [p2.key(i) for i in range(len(p2.e.pads))] == before
              and "MAP would replace 1 assigned key" in p2.e.last_error,
              p2.e.last_error[:70])
        p2.send("pads.map", track=0, mode="ONE", confirm=True)
        check("and the second press, which carries confirm, does it",
              all(q.loaded for q in p2.e.pads) and p2.e.pads[0].name == "hats.wav"
              and p2.e.pads[0].slice == 0)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def t_assigning_over_a_key_asks():
    d = base_dir()
    try:
        bass = tone(os.path.join(d, "music", "bass.wav"), 1.0, 110.0)
        hats = tone(os.path.join(d, "music", "hats.wav"), 1.0, 4000.0)
        p = Panel(d)
        p.load(0, bass)
        p.send("pad.take", i=0, track=0, ls=4000, le=40000)
        before = p.key(0)
        p.load(0, hats)
        p.send("pad.take", i=0, track=0, ls=0, le=20000)
        check("assigning over a key that holds audio is refused the first time",
              p.key(0) == before and "Key 1 holds bass.wav" in p.e.last_error,
              p.e.last_error[:60])
        p.send("pad.take", i=0, track=0, ls=0, le=20000, replace=True)
        check("and the second press, which carries replace, takes the key",
              p.key(0)[0] == "hats.wav" and p.key(0)[1:3] == (0, 20000), str(p.key(0)[:3]))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def t_clearing_a_key():
    d = base_dir()
    try:
        bass = tone(os.path.join(d, "music", "bass.wav"), 1.0, 110.0)
        hats = tone(os.path.join(d, "music", "hats.wav"), 1.0, 4000.0)
        p = Panel(d)
        p.load(0, bass)
        p.send("pad.take", i=0, track=0, ls=4000, le=40000)
        p.load(0, hats)                       # only the key holds the bass now
        held = p.e.audio_bytes()
        p.send("pad.clear", i=0)
        check("clearing empties the key",
              not p.e.pads[0].loaded and p.e.pads[0].name == "" and p.e.pads[0].buf is None)
        freed = held - p.e.audio_bytes()
        check("and lets go of its audio, so the ceiling gets it back",
              freed >= SR * 2 * 4 * 0.9, "%d bytes freed of %d held" % (freed, held))
        p.send("pad.trigger", i=0)
        check("a cleared key makes no sound", hz(p.render(40)) == 0.0)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def t_keys_survive_save_restart_load_and_a_later_file_load():
    d = base_dir()
    try:
        bass = tone(os.path.join(d, "music", "bass.wav"), 1.0, 110.0)
        hats = tone(os.path.join(d, "music", "hats.wav"), 1.0, 4000.0)
        p = Panel(d)
        p.load(0, bass)
        p.send("pad.take", i=0, track=0, ls=4000, le=40000)
        p.send("pad.take", i=5, track=0, ls=1000, le=9000)
        p.send("pad.clear", i=5)                       # cleared on purpose
        p.send("pad.take", i=2, track=0, ls=2000, le=22000)
        p.load(0, hats)
        p.send("session.save")
        saved = p.wait(lambda: any(m.get("op") == "session.saved" for m in p.sent))
        name = [m for m in p.sent if m.get("op") == "session.saved"][-1]["name"]
        check("the panel's save op writes the set", saved and name.endswith(".json"), name)

        r = Panel(d)                                   # a restart
        r.send("session.load", name=name)
        r.wait(lambda: any(m.get("op") == "session.loaded" for m in r.sent))
        r.settle(8)
        check("after a restart the keys come back as they were",
              r.key(0)[0] == "bass.wav" and r.key(0)[1:3] == (4000, 40000)
              and r.key(2)[0] == "bass.wav" and r.key(2)[1:3] == (2000, 22000),
              "%s %s" % (r.key(0)[:3], r.key(2)[:3]))
        check("a key cleared before the save stays cleared", not r.e.pads[5].loaded)
        check("and the track comes back with the file it had", r.e.tracks[0].name == "hats.wav")

        before = [r.key(i) for i in range(len(r.e.pads))]
        third = tone(os.path.join(d, "music", "stab.wav"), 1.0, 700.0)
        check("loading a third file after a session load works", r.load(0, third))
        check("and the restored keys are still untouched",
              [r.key(i) for i in range(len(r.e.pads))] == before)
        check("the kit's launch convenience refuses to wipe a restored key",
              r.a.fill_keys_from_slices(0, "ONE") is False
              and [r.key(i) for i in range(len(r.e.pads))] == before)

        empty = Panel(d)
        empty.load(0, hats)
        filled = empty.a.fill_keys_from_slices(0, "ONE")
        empty.settle()                      # it posts ops; the engine applies them
        check("with nothing assigned, that same convenience still fills the keys",
              filled is True and empty.e.pads[0].loaded)
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    for fn in (t_every_op_the_panel_sends_is_handled,
               t_the_segment_says_whose_mode_it_shows,
               t_each_key_keeps_its_own_mode,
               t_loading_a_file_cannot_touch_a_key,
               t_a_mode_is_only_a_mode,
               t_map_asks_before_it_replaces_assigned_keys,
               t_assigning_over_a_key_asks,
               t_clearing_a_key,
               t_keys_survive_save_restart_load_and_a_later_file_load):
        try:
            fn()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    print("\n%d checks failed" % len(FAILS) if FAILS else "\nall checks passed")
    sys.exit(1 if FAILS else 0)
