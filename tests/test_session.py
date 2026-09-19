"""Task 29: a set saved, the machine restarted, the set put back.

    PYTHONPATH=.pylibs python3 -m tests.test_session
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import types

import numpy as np
import soundfile as sf

from loopengine import dsp
from loopengine.app import App
from loopengine.engine import Engine

SR = 48000
FAILS = []


def check(name, ok, detail=""):
    print("%-72s %s%s" % (name, "PASS" if ok else "FAIL",
                          ("  " + detail) if detail else ""))
    if not ok:
        FAILS.append(name)


def wav(path, secs, f):
    t = np.arange(int(SR * secs)) / SR
    y = np.stack([np.sin(2 * np.pi * f * t) * 0.4,
                  np.sin(2 * np.pi * f * 1.01 * t) * 0.3], axis=1).astype(np.float32)
    sf.write(path, y, SR, subtype="PCM_16")
    return os.path.realpath(path)


class Rig:
    def __init__(self, base, memory_mb=1024):
        self.base = base
        self.e = Engine(samplerate=SR, blocksize=256, offline=True, memory_mb=memory_mb)
        self.a = App(self.e, roots=[os.path.join(base, "music")],
                     inbox=os.path.join(base, ".inbox"),
                     sessions_dir=os.path.join(base, "sessions"),
                     session_key=os.path.join(base, "cfg", "session.key"))
        self.a.hub = types.SimpleNamespace(text=lambda o: None, peaks=lambda *x, **k: None,
                                           pad_peaks=lambda *x, **k: None,
                                           broadcast=lambda *x: None)

    def settle(self, n=6):
        for _ in range(n):
            self.e.render_offline(256)

    def load(self, i, path):
        self.a.loader._work(i, path, 16)
        self.settle()

    def post(self, op, **kw):
        self.e.post(op, **kw)
        self.settle()


def base_dir():
    d = tempfile.mkdtemp(prefix="le-session-")
    os.makedirs(os.path.join(d, "music"))
    return d


def build_set(r, music):
    a = wav(os.path.join(music, "a.wav"), 1.0, 220.0)
    b = wav(os.path.join(music, "b.wav"), 0.75, 330.0)
    c = wav(os.path.join(music, "c.wav"), 0.5, 440.0)
    d = wav(os.path.join(music, "d.wav"), 0.6, 550.0)
    r.load(0, a)
    r.load(1, b)
    r.load(2, c)
    r.a.loader._variant(0, "CTR")
    r.settle()
    r.post("track.loop", i=0, ls=4000, le=40000)
    r.post("track.gain", i=0, v=0.5)
    r.post("track.pan", i=0, v=-0.3)
    r.post("track.speed", i=0, v=1.25)
    r.post("track.rev", i=0)
    r.post("track.solo", i=1, v=True)
    r.a.handle({"op": "pad.take", "i": 4, "track": 0, "ls": 1000, "le": 20000})
    r.settle()
    r.post("pad.assign", i=4, mode="LOOP", gain=0.8, quantize=True, label="vox")
    r.a.handle({"op": "pad.take", "i": 5, "track": 2, "ls": 100, "le": 12000})
    r.settle()
    r.load(2, d)                       # the track moves on; key W keeps c.wav
    r.post("transport.bpm", v=131.5)
    r.post("transport.quantum", v=3)
    r.post("master.gain", v=0.9)
    views = {"track:0": {"sig": "a.wav|48000", "vs": 1000.0, "ve": 5000.0},
             "key:5": {"sig": "c.wav|24000", "vs": 10.0, "ve": 400.0},
             "track:1": {"sig": "stale.wav|1", "vs": 1.0, "ve": 2.0}}
    return (a, b, c, d), views


TRACK_FIELDS = ("name", "path", "loop_start", "loop_end", "gain", "pan", "speed",
                "reverse", "mode", "mute", "solo", "frames")
KEY_FIELDS = ("name", "path", "source", "loop_start", "loop_end", "mode", "gain",
              "pan", "speed", "reverse", "quantize", "label", "frames")


def state(e):
    tr = {t.index: {f: getattr(t, f) for f in TRACK_FIELDS} for t in e.tracks if t.src is not None}
    ks = {i: {f: getattr(p, f) for f in KEY_FIELDS} for i, p in enumerate(e.pads) if p.loaded}
    return tr, ks, (round(e.transport.bpm, 4), e.transport.quantum_i, round(e.master_gain, 4))


def t_round_trip():
    base = base_dir()
    try:
        r = Rig(base)
        (a, b, c, d), views = build_set(r, os.path.join(base, "music"))
        before = state(r.e)
        path, doc, unsaved, _ = r.a.save_session(views)
        check("save writes a versioned, human-readable file",
              os.path.isfile(path) and json.load(open(path))["loopengine_session"] == 2
              and "\n  " in open(path).read(), os.path.basename(path))
        check("a file used by a track and a key is listed once",
              len(doc["files"]) == 4 and not unsaved,
              "%d files: %s" % (len(doc["files"]), [os.path.basename(f["path"]) for f in doc["files"]]))
        check("a view whose file changed under it is not saved",
              doc["tracks"][1]["view"] is None)

        r2 = Rig(base)                      # a restart: nothing carried but the file
        res = r2.a.load_session(path, analyse=False)
        r2.settle(8)
        after = state(r2.e)
        diffs = []
        for kind, x, y in (("track", before[0], after[0]), ("key", before[1], after[1])):
            for slot in sorted(set(x) | set(y)):
                for f in set(x.get(slot, {})) | set(y.get(slot, {})):
                    u, v = x.get(slot, {}).get(f), y.get(slot, {}).get(f)
                    same = abs(u - v) < 1e-3 if isinstance(u, float) and isinstance(v, float) else u == v
                    if not same:
                        diffs.append("%s %d %s: %r -> %r" % (kind, slot, f, u, v))
        check("every track and key field comes back the same",
              not diffs and len(after[0]) == 3 and len(after[1]) == 2,
              "; ".join(diffs[:3]) or "%d tracks, %d keys compared field by field"
              % (len(after[0]), len(after[1])))
        check("tempo, quantum and master come back", before[2] == after[2], str(after[2]))
        ctr = dsp.center_extract(*sf.read(a, dtype="float32", always_2d=True), "ctr")
        check("the CTR key holds the CTR variant of its file, not the stereo source",
              r2.e.pads[4].source == "CTR" and np.allclose(r2.e.pads[4].buf, ctr, atol=1e-6))
        check("key W holds c.wav though no track shows it",
              r2.e.pads[5].name == "c.wav" and r2.e.tracks[2].name == "d.wav")
        check("the views come back for the files they were set on",
              res["views"].get("track:0") == views["track:0"]
              and res["views"].get("key:5") == views["key:5"]
              and "track:1" not in res["views"], str(sorted(res["views"])))
        check("and nothing is reported missing", res["state"] == "loaded" and not res["missing"])
    finally:
        shutil.rmtree(base, ignore_errors=True)


def t_moved_and_changed_files():
    base = base_dir()
    try:
        r = Rig(base)
        music = os.path.join(base, "music")
        (a, b, c, d), views = build_set(r, music)
        path, _, _, _ = r.a.save_session({})
        os.rename(b, b + ".moved")
        wav(c, 0.5, 880.0)                 # same name, different audio
        r2 = Rig(base)
        res = r2.a.load_session(path, analyse=False)
        r2.settle(8)
        m = res["missing"]
        check("a moved file leaves its track empty and says so by name",
              r2.e.tracks[1].src is None and m.get("track:1", {}).get("name") == "b.wav"
              and "moved or deleted" in m["track:1"]["reason"], str(m.get("track:1")))
        check("a file changed under the same name is refused, not trusted",
              not r2.e.pads[5].loaded and "changed" in m.get("key:5", {}).get("reason", ""),
              str(m.get("key:5")))
        check("everything that still checks out loads around them",
              r2.e.tracks[0].name == "a.wav" and r2.e.tracks[2].name == "d.wav"
              and r2.e.pads[4].loaded)
        check("the missing slots are in the telemetry the panel draws",
              set(r2.a.session["missing"]) == {"track:1", "key:5"})
        broken = os.path.join(os.path.dirname(path), "broken.json")
        with open(broken, "w") as fh:
            fh.write("{ not json")
        bad = r2.a.load_session(broken, analyse=False)
        check("a load that fails leaves the set on deck as it was, missing marks and all",
              bad["state"] == "failed" and r2.e.tracks[0].name == "a.wav"
              and set(r2.a.session["missing"]) == {"track:1", "key:5"},
              str(sorted(r2.a.session["missing"])))
    finally:
        shutil.rmtree(base, ignore_errors=True)


def t_the_ceiling_refuses_before_touching_anything():
    base = base_dir()
    try:
        r = Rig(base)
        music = os.path.join(base, "music")
        big = [wav(os.path.join(music, "big%d.wav" % k), 4.0, 200.0 + k) for k in range(3)]
        r.load(0, big[0])
        r.load(1, big[1])
        r.a.handle({"op": "pad.take", "i": 8, "track": 1})
        r.settle()
        r.load(1, big[2])                  # key A now pins big1 on its own
        path, _, _, _ = r.a.save_session({})
        small = wav(os.path.join(music, "small.wav"), 0.25, 300.0)
        r2 = Rig(base, memory_mb=3)        # room for two of those buffers, not three
        r2.load(3, small)
        res = r2.a.load_session(path, analyse=False)
        r2.settle()
        check("a session over the ceiling fails loudly, naming the key responsible",
              res["state"] == "failed" and "key A" in res["error"] and "big1.wav" in res["error"],
              res["error"][:110])
        check("and nothing was loaded or cleared: the set you had is untouched",
              r2.e.tracks[3].name == "small.wav" and r2.e.tracks[0].src is None
              and not any(p.loaded for p in r2.e.pads))
    finally:
        shutil.rmtree(base, ignore_errors=True)


def t_grants_come_back_only_if_this_machine_signed_them():
    base = base_dir()
    try:
        outside = os.path.join(base, "elsewhere")
        os.makedirs(outside)
        o = wav(os.path.join(outside, "o.wav"), 0.5, 250.0)
        shutil.copyfile(o, os.path.join(outside, "twin.wav"))
        r = Rig(base)
        r.a._grant(o)                       # what the OS dialog does
        r.load(0, o)
        path, doc, _, _ = r.a.save_session({})
        check("a file reached through the dialog is saved as a signed grant",
              doc["files"][0]["granted"] and len(doc["grants"]["signed"]) == 64)
        r2 = Rig(base)
        check("a fresh run cannot read it", not r2.a._inside_roots(o))
        res = r2.a.load_session(path, analyse=False)
        r2.settle()
        check("the signed grant is honoured: the file loads and is granted for this run",
              r2.e.tracks[0].name == "o.wav" and r2.a._inside_roots(o) and not res["missing"])

        doc2 = json.load(open(path))
        doc2["tracks"][0]["gain"] = 0.3
        p2 = os.path.join(os.path.dirname(path), "hand-edited.json")
        json.dump(doc2, open(p2, "w"), indent=2)
        r3 = Rig(base)
        r3.a.load_session(p2, analyse=False)
        r3.settle()
        check("editing a gain by hand is fine",
              r3.e.tracks[0].src is not None and abs(r3.e.tracks[0].gain - 0.3) < 1e-6)

        doc3 = json.load(open(path))
        doc3["files"][0]["path"] = os.path.realpath(os.path.join(outside, "twin.wav"))
        p3 = os.path.join(os.path.dirname(path), "path-edited.json")
        json.dump(doc3, open(p3, "w"), indent=2)
        r4 = Rig(base)
        res4 = r4.a.load_session(p3, analyse=False)
        r4.settle()
        check("editing a granted path is not — even to a byte-identical twin",
              r4.e.tracks[0].src is None and "grant" in res4["missing"].get("track:0", {}).get("reason", "")
              and not r4.a._inside_roots(os.path.join(outside, "twin.wav")),
              res4["missing"].get("track:0", {}).get("reason", "")[:70])
    finally:
        shutil.rmtree(base, ignore_errors=True)


def t_what_cannot_be_saved_or_read_says_so():
    base = base_dir()
    try:
        r = Rig(base)
        y = np.zeros((4800, 2), dtype=np.float32)
        r.post("track.load", i=6, buf=y, sr=SR, name="in-memory", path="", bpm=0.0,
               conf=0.0, slices=np.zeros(0, dtype=np.int64))
        path, doc, unsaved, _ = r.a.save_session({})
        check("audio with no file behind it is named, not silently dropped",
              unsaved == ["track 7 (in-memory) has no file on disk"] and not doc["tracks"], str(unsaved))
        future = json.load(open(path))
        future["loopengine_session"] = 99
        fp = os.path.join(os.path.dirname(path), "future.json")
        json.dump(future, open(fp, "w"))
        later = os.stat(path).st_mtime_ns + 5 * 10 ** 9     # no relying on the clock's tick
        os.utime(fp, ns=(later, later))
        res = r.a.load_session(fp, analyse=False)
        check("a session from a newer format is refused with the reason",
              res["state"] == "failed" and "format 99" in res["error"], res["error"])
        listed = r.a.session_list()["sessions"]
        check("the OPEN list shows sessions newest first and marks the unreadable",
              [x["name"] for x in listed][:1] == ["future.json"] and listed[0]["error"],
              str([(x["name"], bool(x["error"])) for x in listed]))
    finally:
        shutil.rmtree(base, ignore_errors=True)


def t_a_save_while_files_are_away_keeps_them():
    base = base_dir()
    try:
        r = Rig(base)
        music = os.path.join(base, "music")
        (a, b, c, d), views = build_set(r, music)
        path, _, _, _ = r.a.save_session({})
        os.rename(b, b + ".away")                     # the drive is unplugged
        r2 = Rig(base)
        r2.a.load_session(path, analyse=False)
        r2.settle(8)
        p2, doc2, _, kept = r2.a.save_session({}, "while-away.json")
        row = [t for t in doc2["tracks"] if t["slot"] == 1]
        check("a save made while a file is away keeps its track, as it was saved",
              kept == ["track 2 (b.wav)"] and len(row) == 1 and row[0]["solo"] is True, str(kept))
        os.rename(b + ".away", b)                     # and plugged back in
        r3 = Rig(base)
        res3 = r3.a.load_session(p2, analyse=False)
        r3.settle(8)
        check("once the file is back, that later session loads it",
              r3.e.tracks[1].name == "b.wav" and r3.e.tracks[1].solo and not res3["missing"],
              str(res3["missing"]))
        r2.load(1, d)
        _, doc4, _, kept4 = r2.a.save_session({}, "filled.json")
        check("a slot filled by hand since lets the old reference go",
              not kept4 and [t["name"] for t in doc4["tracks"] if t["slot"] == 1] == ["d.wav"])
    finally:
        shutil.rmtree(base, ignore_errors=True)


def t_a_kept_grant_is_resigned_only_if_it_checked_out():
    base = base_dir()
    try:
        outside = os.path.join(base, "elsewhere")
        os.makedirs(outside)
        o = wav(os.path.join(outside, "o.wav"), 0.5, 250.0)
        shutil.copyfile(o, os.path.join(outside, "twin.wav"))
        twin = os.path.realpath(os.path.join(outside, "twin.wav"))
        r = Rig(base)
        r.a._grant(o)
        r.load(0, o)
        path, _, _, _ = r.a.save_session({})

        os.rename(o, o + ".away")
        r2 = Rig(base)
        r2.a.load_session(path, analyse=False)
        r2.settle()
        p2, doc2, _, kept2 = r2.a.save_session({}, "grant-away.json")
        os.rename(o + ".away", o)
        r3 = Rig(base)
        res3 = r3.a.load_session(p2, analyse=False)
        r3.settle()
        check("a genuine grant whose file was away is kept, re-signed, and loads when back",
              kept2 == ["track 1 (o.wav)"] and doc2["files"][0]["granted"] is True
              and r3.e.tracks[0].name == "o.wav" and not res3["missing"], str(res3["missing"]))

        doc3 = json.load(open(path))
        doc3["files"][0]["path"] = twin
        p3 = os.path.join(os.path.dirname(path), "path-edited.json")
        json.dump(doc3, open(p3, "w"), indent=2)
        r4 = Rig(base)
        r4.a.load_session(p3, analyse=False)
        r4.settle()
        p5, doc5, _, kept5 = r4.a.save_session({}, "laundered.json")
        r5 = Rig(base)
        res5 = r5.a.load_session(p5, analyse=False)
        r5.settle()
        reason = res5["missing"].get("track:0", {}).get("reason", "")
        check("a hand-edited grant carried through a save is not re-signed: still refused",
              kept5 == ["track 1 (twin.wav)"] and doc5["files"][0]["granted"] is False
              and not doc5["grants"]["signed"] and r5.e.tracks[0].src is None
              and not r5.a._inside_roots(twin) and "grant" in reason, reason[:70])
    finally:
        shutil.rmtree(base, ignore_errors=True)


def t_a_hand_edit_with_the_wrong_types_never_stops_halfway():
    base = base_dir()
    try:
        r = Rig(base)
        build_set(r, os.path.join(base, "music"))
        path, _, _, _ = r.a.save_session({})
        doc = json.load(open(path))
        doc["tracks"][0]["gain"] = None
        doc["tracks"][0]["speed"] = "fast"
        doc["tracks"][0]["loop"] = "all of it"
        doc["tracks"][1]["file"] = ["f1"]
        doc["tracks"].insert(0, 7)
        doc["keys"][0]["mode"] = "SIDEWAYS"
        doc["keys"][0]["gain"] = [1]
        doc["engine"]["quantum"] = "three"
        doc["engine"]["bpm"] = None
        p2 = os.path.join(os.path.dirname(path), "typos.json")
        json.dump(doc, open(p2, "w"), indent=2)
        r2 = Rig(base)
        res = r2.a.load_session(p2, analyse=False)
        r2.settle(8)
        t0, k4 = r2.e.tracks[0], r2.e.pads[4]
        check("a hand-edit with the wrong types loads what it can, and never stops halfway",
              res["state"] == "loaded" and t0.name == "a.wav" and t0.speed == 1.0
              and t0.loop_end == t0.frames and r2.e.tracks[2].name == "d.wav"
              and k4.loaded and k4.mode == "ONE" and "track:1" in res["missing"],
              "%s %s" % (res["state"], (res.get("error") or "")[:60]))
    finally:
        shutil.rmtree(base, ignore_errors=True)


def t_banks_survive_a_session_and_an_old_one_lands_in_bank_A():
    base = base_dir()
    try:
        r = Rig(base)
        _, views = build_set(r, os.path.join(base, "music"))
        r.post("pads.bank", b=2)
        r.a.handle({"op": "pad.take", "i": 1, "track": 0, "ls": 500, "le": 9000})
        r.settle()
        r.post("pads.bank", b=0)
        path, doc, unsaved, _ = r.a.save_session(views)
        slots = sorted(k["slot"] for k in doc["keys"])
        check("a key assigned on bank C is saved by its slot",
              slots == [4, 5, 33] and not unsaved, str(slots))

        r2 = Rig(base)
        res = r2.a.load_session(path, analyse=False)
        r2.settle()
        check("and comes back on bank C, with bank A's keys where they were",
              res["state"] == "loaded" and r2.e.pads[33].loaded
              and (r2.e.pads[33].loop_start, r2.e.pads[33].loop_end) == (500, 9000)
              and r2.e.pads[4].loaded and not r2.e.pads[32].loaded,
              "%s %s" % (res["state"], (res.get("error") or "")[:60]))

        # A format 1 file: sixteen key slots, and nothing else different.
        old = json.load(open(path))
        old["loopengine_session"] = 1
        old["keys"] = [k for k in old["keys"] if k["slot"] < 16]
        op = os.path.join(os.path.dirname(path), "old.json")
        json.dump(old, open(op, "w"))
        r3 = Rig(base)
        res3 = r3.a.load_session(op, analyse=False)
        r3.settle()
        check("a session from the format before banks still loads",
              res3["state"] == "loaded" and not res3.get("error"),
              "%s %s" % (res3["state"], (res3.get("error") or "")[:60]))
        check("and its keys land in bank A, with the other banks empty",
              r3.e.pads[4].loaded and r3.e.pads[5].loaded
              and not any(q.loaded for q in r3.e.pads[r3.e.bank_size:])
              and r3.e.bank == 0)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def t_cues_round_trip():
    base = base_dir()
    try:
        r = Rig(base)
        _, views = build_set(r, os.path.join(base, "music"))
        r.post("track.cues", i=0, cues=[100, 2000, -1, -1, -1, -1, -1, 9999])
        path, doc, unsaved, _ = r.a.save_session(views)
        row = [t for t in doc["tracks"] if t["slot"] == 0][0]
        check("a track's cues are written with it",
              row["cues"] == [100, 2000, -1, -1, -1, -1, -1, 9999], str(row["cues"]))
        r2 = Rig(base)
        r2.a.load_session(path, analyse=False)
        r2.settle()
        check("and come back on the track they were set on",
              [int(c) for c in r2.e.tracks[0].cues] == [100, 2000, -1, -1, -1, -1, -1, 9999]
              and all(c < 0 for c in r2.e.tracks[1].cues),
              str([int(c) for c in r2.e.tracks[0].cues]))
        # a session from before cues existed
        old = json.load(open(path))
        for t in old["tracks"]:
            t.pop("cues", None)
        op = os.path.join(os.path.dirname(path), "nocues.json")
        json.dump(old, open(op, "w"))
        r3 = Rig(base)
        res = r3.a.load_session(op, analyse=False)
        r3.settle()
        check("a session with no cues in it loads with none",
              res["state"] == "loaded" and all(c < 0 for c in r3.e.tracks[0].cues),
              res["state"])
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    for fn in (t_round_trip,
               t_moved_and_changed_files,
               t_the_ceiling_refuses_before_touching_anything,
               t_grants_come_back_only_if_this_machine_signed_them,
               t_what_cannot_be_saved_or_read_says_so,
               t_a_save_while_files_are_away_keeps_them,
               t_a_kept_grant_is_resigned_only_if_it_checked_out,
               t_a_hand_edit_with_the_wrong_types_never_stops_halfway,
               t_banks_survive_a_session_and_an_old_one_lands_in_bank_A,
               t_cues_round_trip):
        try:
            fn()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    print("\n%d checks failed" % len(FAILS) if FAILS else "\nall checks passed")
    sys.exit(1 if FAILS else 0)
