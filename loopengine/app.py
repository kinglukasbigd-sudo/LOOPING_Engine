"""Wiring: engine + loader + server + telemetry pump."""
from __future__ import annotations

import collections
import math
import os
import struct
import threading
import time

import numpy as np

from . import picker
from . import session
from .loader import AUDIO_EXT, DecodeError, Loader, decode
from .recorder import (ARMED as REC_ARMED, IDLE as REC_IDLE,
                       RECORDING as REC_RECORDING, Recorder, part_path)
from .server import (Hub, new_token, OP_BIN, BIN_SCOPE, BIN_PEAKS,
                     BIN_PEAKS_PAD, BIN_PEAKS_RANGE)
from . import dsp
from .track import MODES
from .voice import GATE, LOOP, ONESHOT

MAX_UPLOAD = 200 * 1024 * 1024


KEY_CAPS = "1234QWERASDFZXCV"   # key slot -> the cap on the keyboard
KEY_MODES = (ONESHOT, GATE, LOOP)


def _finite(v, default=None):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


class App:
    def __init__(self, engine, roots=None, inbox=".inbox", fps=30,
                 sessions_dir=None, session_key=None, recordings_dir=None):
        self.engine = engine
        self.token = new_token()
        self.hub = Hub()
        self.fps = fps
        self.roots = [os.path.realpath(os.path.expanduser(r))
                      for r in (roots or ["~"])]
        self.inbox = os.path.realpath(inbox)
        self.loader = Loader(engine, on_done=self._loaded,
                             on_error=self._load_error)
        self.peaks = {}      # track index -> ndarray (buckets, 2)
        self.pad_peaks = {}  # pad index -> the envelope that key holds
        # Paths this server itself returned from a user-driven OS dialog.
        # The dialog exists to reach folders outside --root, so its results
        # cannot be judged by the root check; choosing a file in the OS
        # picker IS the grant. Only paths we produced are trusted, never
        # paths a caller supplies, and the set is bounded.
        self._granted = collections.OrderedDict()
        self._pick_seq = 0
        self.meta = {}      # track index -> dict of analysis results
        home = os.path.expanduser("~/.loopengine")
        self.sessions_dir = os.path.realpath(os.path.expanduser(
            sessions_dir or os.path.join(home, "sessions")))
        self.session_key_path = session_key or os.path.join(home, "session.key")
        self.session = {"name": "", "state": "", "error": "", "missing": {}}
        # The saved rows behind the missing marks. A save made while a file is
        # away writes them back, so the reference does not leave with the drive.
        self._kept = {}
        self.recordings_dir = os.path.realpath(os.path.expanduser(
            recordings_dir or os.path.join(home, "recordings")))
        self.record_last = None      # the last finished take, for a panel opened later
        self.record_error = ""
        # Has the device asked for sound lately? The callback counts blocks and
        # this thread watches that count move. Nothing here runs on the audio
        # thread, and nothing here restarts anything.
        self._blocks_seen = -1
        self._blocks_at = time.monotonic()
        self._take_stalled = False
        self._stop = threading.Event()

    # -- telemetry ---------------------------------------------------------
    def pump(self):
        period = 1.0 / self.fps
        nxt = time.monotonic()
        while not self._stop.is_set():
            nxt += period
            try:
                snap = self.engine.snapshot()
                snap["op"] = "state"
                snap["meta"] = self.meta
                snap["session"] = self.session
                snap["record"] = self.record_state()
                snap["audio"] = self.device_health()
                self.hub.text(snap)
                sc = self.engine.scope(512)
                self.hub.broadcast(
                    OP_BIN, struct.pack("!BBH", BIN_SCOPE, 0, sc.shape[0] // 2)
                    + sc.tobytes())
            except Exception as e:
                self.engine.last_error = "telemetry: %s" % e
            sleep = nxt - time.monotonic()
            if sleep > 0:
                time.sleep(sleep)
            else:
                nxt = time.monotonic()

    # A block is 5.3 ms at 256 frames and 48 kHz, so a quarter second is fifty
    # of them. Anything that quiet means the device has stopped asking, and the
    # room has gone silent.
    STALL_S = 0.25

    def device_health(self):
        """Whether the callback is still being called, judged from off the
        audio thread by watching the block count move.

        Detection, not recovery. Bringing a stream back mid-set is its own risk
        — a click, a gap, a take that ends up short — so this says what happened
        and leaves the decision to the person in front of it.
        """
        now = time.monotonic()
        blocks = self.engine.blocks
        if blocks != self._blocks_seen:
            self._blocks_seen = blocks
            self._blocks_at = now
        quiet = now - self._blocks_at
        running = self.engine.stream is not None
        stalled = bool(running and quiet > self.STALL_S)
        if stalled and self.engine.recorder is not None \
                and self.engine.recorder.state in (REC_RECORDING, REC_ARMED):
            self._take_stalled = True            # the take stopped growing with it
        return {"blocks": blocks, "quiet_s": round(quiet, 2),
                "stalled": stalled, "running": running}

    def start_pump(self):
        threading.Thread(target=self.pump, daemon=True).start()
        return self

    def on_client_join(self, client):
        for i, arr in self.pad_peaks.items():
            q = np.clip(arr * 127.0, -127, 127).astype(np.int8)
            client.send(OP_BIN, struct.pack("!BBH", BIN_PEAKS_PAD, i & 0xFF,
                                            q.shape[0]) + q.tobytes())
        for i, arr in self.peaks.items():
            q = np.clip(arr * 127.0, -127, 127).astype(np.int8)
            client.send(OP_BIN,
                        struct.pack("!BBH", BIN_PEAKS, i & 0xFF, q.shape[0])
                        + q.tobytes())

    # -- loader callbacks --------------------------------------------------
    def _loaded(self, i, info):
        arr = info.pop("peaks", None)
        m = self.meta.setdefault(i, {})
        m.update(info)
        m["error"] = ""
        if arr is not None:
            self.peaks[i] = arr
            self.hub.peaks(i, arr)

    def _load_error(self, i, msg):
        self.meta.setdefault(i, {})["error"] = msg
        self.engine.last_error = msg

    # -- file system -------------------------------------------------------
    GRANT_MAX = 64

    def _pinned(self):
        """Paths a track is currently holding. These must outlive eviction.

        Evicting a grant for a file that is loaded and on screen breaks the
        next thing that touches it — a reslice, a source-mode switch, a
        reload — with a refusal the user cannot act on, because from where
        they sit the file is plainly right there.
        """
        out = set()
        for t in self.engine.tracks:
            if getattr(t, "src", None) is not None and getattr(t, "path", ""):
                out.add(os.path.realpath(t.path))
        return out

    def _grant(self, p):
        rp = os.path.realpath(p)
        self._granted[rp] = True
        if len(self._granted) <= self.GRANT_MAX:
            return
        pinned = self._pinned()
        for k in list(self._granted):                  # oldest first
            if len(self._granted) <= self.GRANT_MAX:
                break
            if k not in pinned and k != rp:
                del self._granted[k]

    def _inside_roots(self, p):
        rp = os.path.realpath(p)
        if rp in self._granted:
            return True
        # Takes are readable because this run wrote them: a recording offered to
        # a track has to be loadable whatever --root says, or REC writes files
        # the panel that made them is not allowed to open.
        return any(rp == r or rp.startswith(r + os.sep)
                   for r in self.roots + [self.inbox, self.recordings_dir])

    # -- native dialog -----------------------------------------------------
    def pick_backend(self):
        name, reason = picker.backend()
        return {"backend": name, "reason": reason, "available": name is not None}

    def pick_async(self, track_i: int, multiple: bool, start_dir=None):
        """Return a request id at once; the dialog runs on its own thread.

        The HTTP request must not be held open waiting for a human, so the
        result comes back over the WebSocket that is already there.
        """
        self._pick_seq += 1
        req = self._pick_seq
        info = self.pick_backend()
        if not info["available"]:
            self.hub.text({"op": "picked", "req": req, "track": track_i,
                           "paths": [], "cancelled": False,
                           "error": info["reason"]})
            return req, info
        threading.Thread(target=self._pick, daemon=True,
                         args=(req, track_i, multiple, start_dir)).start()
        return req, info

    def _pick(self, req, track_i, multiple, start_dir):
        paths, cancelled, err = picker.pick(
            multiple=multiple, start_dir=start_dir or self.roots[0])
        for p in paths:
            self._grant(p)          # granted before the client can ask to load
        self.hub.text({"op": "picked", "req": req, "track": track_i,
                       "paths": paths, "cancelled": cancelled, "error": err})

    def browse(self, d):
        d = os.path.realpath(os.path.expanduser(d or self.roots[0]))
        if not self._inside_roots(d) or not os.path.isdir(d):
            d = self.roots[0]
        rows = []
        try:
            names = sorted(os.listdir(d), key=str.lower)
        except OSError as e:
            return {"dir": d, "up": os.path.dirname(d), "entries": [],
                    "error": "Can't read %s — %s." % (d, e.strerror)}
        for n in names:
            if n.startswith("."):
                continue
            p = os.path.join(d, n)
            try:
                isdir = os.path.isdir(p)
                size = 0 if isdir else os.path.getsize(p)
            except OSError:
                continue
            ext = os.path.splitext(n)[1].lower()
            if isdir or ext in AUDIO_EXT:
                rows.append({"name": n, "path": p, "dir": isdir,
                             "size": size, "ext": ext})
        return {"dir": d, "up": os.path.dirname(d) or d, "entries": rows,
                "error": ""}

    def stash(self, name, data):
        if len(data) > MAX_UPLOAD:
            raise ValueError("That file is %d MB. The drop limit is %d MB — "
                             "load it by path instead."
                             % (len(data) // 1048576, MAX_UPLOAD // 1048576))
        os.makedirs(self.inbox, exist_ok=True)
        safe = os.path.basename(name).replace(os.sep, "_") or "dropped.wav"
        p = os.path.join(self.inbox, safe)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def devices(self):
        import sounddevice as sd
        out = []
        for i, d in enumerate(sd.query_devices()):
            if d["max_output_channels"] >= 2:
                out.append({"i": i, "name": d["name"],
                            "sr": int(d["default_samplerate"]),
                            "api": sd.query_hostapis(d["hostapi"])["name"]})
        return {"devices": out, "current": self.engine.device_name}

    # -- command router ----------------------------------------------------
    def handle(self, msg, client=None):
        op = msg.get("op", "")
        if op == "peaks.range":
            return self._peaks_range(msg, client)
        if op == "session.save":
            return self._session_save(msg)
        if op == "session.load":
            return self._session_load_async(msg)
        if op == "record":
            return self.record_toggle()
        if op == "load":
            i, path = int(msg["i"]), msg["path"]
            if not self._inside_roots(path):
                return self._load_error(i, "%s is outside the folders this "
                                           "run is allowed to read." % path)
            self.meta.setdefault(i, {})["error"] = ""
            self._forget_missing("track:%d" % i)
            self.loader.load_async(i, path, int(msg.get("slices", 16)))
        elif op == "pick":
            self.pick_async(int(msg.get("i", 0)), bool(msg.get("multiple")),
                            msg.get("dir"))
        elif op == "load_kit":
            self.load_kit(msg["dir"])
        elif op == "unload":
            i = int(msg["i"])
            self.engine.post("track.clear", i=i)
            self._forget_missing("track:%d" % i)
            self.peaks.pop(i, None)
            self.meta.pop(i, None)
        elif op == "mode":
            if msg.get("mode") in MODES:
                self.loader.variant_async(int(msg["i"]), msg["mode"])
        elif op == "reslice":
            self.loader.reslice_async(int(msg["i"]), int(msg.get("n", 16)))
        elif op == "pad.take":
            # the key's envelope is the source track's, captured now so it
            # survives that track being replaced
            t = int(msg.get("track", -1))
            i = int(msg.get("i", 0)) % len(self.engine.pads)
            if self.engine.pads[i].loaded and not msg.get("replace"):
                # Hand-tuned audio goes only when its owner says so. The panel
                # asks and its second press carries `replace`.
                self.engine.last_error = (
                    "Key %s holds %s. Press it again to replace it, or clear it first."
                    % (KEY_CAPS[i], self.engine.pads[i].name or "audio"))
                return
            self._forget_missing("key:%d" % i)
            if t in self.peaks:
                self.pad_peaks[i] = self.peaks[t]
                self.hub.pad_peaks(i, self.peaks[t])
            self.engine.post("pad.take", **{k: v for k, v in msg.items() if k != "op"})
        elif op == "pad.assign":
            # Settings only, and only values the engine can use: it applies
            # these as given, so a null mode from a panel that has not painted
            # yet would stick to the key and travel into its session.
            kw = {"i": int(msg.get("i", 0)) % len(self.engine.pads)}
            if msg.get("mode") in KEY_MODES:
                kw["mode"] = msg["mode"]
            for f, lo, hi in (("gain", 0.0, 1.4), ("pan", -1.0, 1.0), ("speed", 0.25, 4.0)):
                v = _finite(msg.get(f)) if f in msg else None
                if v is not None:
                    kw[f] = max(lo, min(hi, v))
            for f in ("quantize", "reverse"):
                if f in msg:
                    kw[f] = bool(msg[f])
            if "label" in msg:
                kw["label"] = str(msg["label"])[:32]
            self.engine.post("pad.assign", **kw)
        elif op == "pad.clear":
            self.pad_peaks.pop(int(msg.get("i", 0)), None)
            self._forget_missing("key:%d" % int(msg.get("i", 0)))
            self.engine.post("pad.clear", **{k: v for k, v in msg.items() if k != "op"})
        elif op == "pads.map":
            # MAP replaces every key with a file's slices — up to 16 hand-tuned
            # keys in one press. The panel asks first and its second press
            # carries `confirm`; this refuses anything else, whatever sent it.
            taken = [KEY_CAPS[i] for i, p in enumerate(self.engine.pads) if p.loaded]
            if taken and not msg.get("confirm"):
                self.engine.last_error = (
                    "MAP would replace %d assigned key%s (%s) with this file's "
                    "slices. Press MAP again to go ahead."
                    % (len(taken), "" if len(taken) == 1 else "s", ", ".join(taken)))
                return
            self.map_pads(int(msg["track"]), msg.get("mode", "ONE"))
        elif op == "tap":
            self.engine.transport.tap(time.monotonic())
        elif op.startswith(("track.", "transport.", "pad.", "master.", "clip.")) \
                or op in ("panic", "probe"):
            kw = {k: v for k, v in msg.items() if k != "op"}
            self.engine.post(op, **kw)

    # Re-bucketing is bounded per request; the panel coalesces, so one panel
    # holds at most one of these at a time.
    RANGE_BUCKETS_MAX = 8192

    def _peaks_range(self, msg, client):
        """The envelope of one panel's zoomed view, from the buffer held here.

        A read, not a command: the engine never sees it, and the view it
        describes lives only in the panel that asked. So the reply goes to
        that panel alone — broadcasting it would hand every other open panel
        peaks for a window it is not looking at.

        Header (network order): kind 0x13, subject (0 track / 1 key), index,
        flags (1 = raw samples, 0 = min/max pairs), then u32 req, start, end,
        frames, count. Values follow as little-endian int16 — int8 staircases
        a sample line visibly once you are zoomed far enough to see one.
        """
        if client is None:
            return
        try:
            key = msg.get("kind") == "key"
            i = int(msg.get("i", -1))
            start, end = int(msg.get("start", 0)), int(msg.get("end", 0))
            buckets = int(msg.get("buckets", 0))
            req = int(msg.get("req", 0)) & 0xFFFFFFFF
        except (TypeError, ValueError, AttributeError):
            return
        slots = self.engine.pads if key else self.engine.tracks
        if not 0 <= i < len(slots):
            return
        buf = slots[i].buf                  # a track's is its active source mode
        if buf is None:
            return
        frames = buf.shape[0]
        buckets = max(16, min(buckets, self.RANGE_BUCKETS_MAX))
        start = max(0, min(start, frames))
        end = max(start, min(end, frames))
        what, data = dsp.peaks_range(buf, start, end, buckets)
        q = np.clip(np.rint(data * 32767.0), -32767, 32767).astype("<i2")
        head = struct.pack("!BBBBIIIII", BIN_PEAKS_RANGE, 1 if key else 0, i & 0xFF,
                           1 if what == "samples" else 0, req, start, end,
                           frames, data.shape[0])
        client.send(OP_BIN, head + q.tobytes())

    # -- sessions ------------------------------------------------------------
    def _inside_roots_only(self, p):
        """Inside --root or the inbox, grants aside: reachable without the dialog."""
        rp = os.path.realpath(p)
        return any(rp == r or rp.startswith(r + os.sep)
                   for r in self.roots + [self.inbox])

    def _forget_missing(self, slot):
        """A slot filled or cleared by hand lets its old reference go.

        Copied and swapped, never changed in place: the telemetry thread may be
        serialising the old dict this moment, and a dict must not change size
        under its iterator."""
        if slot in self.session["missing"]:
            m = dict(self.session["missing"])
            del m[slot]
            self.session["missing"] = m
        if slot in self._kept:
            k = dict(self._kept)
            del k[slot]
            self._kept = k

    def session_list(self):
        return {"dir": self.sessions_dir,
                "sessions": session.listing(self.sessions_dir)}

    def _session_save(self, msg):
        views = msg.get("views")
        try:
            path, _doc, unsaved, kept = self.save_session(
                views if isinstance(views, dict) else {}, msg.get("name"))
            reply = {"op": "session.saved", "name": os.path.basename(path),
                     "unsaved": unsaved, "kept": kept, "error": ""}
            self.session.update(name=reply["name"], state="saved", error="")
        except Exception as e:                       # a reply always lands
            reply = {"op": "session.saved", "name": "", "unsaved": [], "kept": [],
                     "error": "The session was not saved — %s."
                              % (getattr(e, "strerror", None) or e)}
            self.session.update(state="failed", error=reply["error"])
        self.hub.text(reply)

    def save_session(self, views, name=None):
        """Write the set to a file in the sessions folder. -> (path, doc, unsaved).

        Engine state is read from this thread: plain attributes, and a session
        is a picture of a moment. A track or key with no file behind it — audio
        that only ever existed in memory — cannot be saved, and is named in
        `unsaved` rather than silently left out. A slot the loaded session
        could not fill, and that is still empty, is written back as it was read
        and named in `kept`: saving while a drive is unplugged must not lose
        what was on it."""
        e = self.engine
        files, ids, unsaved = [], {}, []

        def file_ref(path):
            rp = os.path.realpath(path)
            if rp not in ids:
                ids[rp] = "f%d" % len(files)
                entry = {"id": ids[rp], "path": rp}
                entry.update(session.fingerprint(rp))
                entry["granted"] = not self._inside_roots_only(rp)
                files.append(entry)
            return ids[rp]

        def view_of(kind, i, name, frames):
            v = (views or {}).get("%s:%d" % (kind, i))
            if not isinstance(v, dict) or v.get("sig") != "%s|%d" % (name, frames):
                return None
            try:
                return {"vs": float(v["vs"]), "ve": float(v["ve"])}
            except (KeyError, TypeError, ValueError):
                return None

        tracks = []
        for t in e.tracks:
            if t.src is None:
                continue
            if not t.path or not os.path.isfile(t.path):
                unsaved.append("track %d (%s) has no file on disk"
                               % (t.index + 1, t.name or "unnamed"))
                continue
            tracks.append({
                "slot": t.index, "file": file_ref(t.path), "name": t.name,
                "frames": int(t.frames),
                "loop": [int(t.loop_start), int(t.loop_end)],
                "gain": round(float(t.gain), 4), "pan": round(float(t.pan), 4),
                "speed": round(float(t.speed), 4), "reverse": bool(t.reverse),
                "mode": t.mode, "mute": bool(t.mute), "solo": bool(t.solo),
                "view": view_of("track", t.index, t.name, t.frames)})
        keys = []
        for i, p in enumerate(e.pads):
            if not p.loaded:
                continue
            if not p.path or not os.path.isfile(p.path):
                unsaved.append("key %s (%s) has no file on disk"
                               % (KEY_CAPS[i], p.name or "unnamed"))
                continue
            keys.append({
                "slot": i, "file": file_ref(p.path), "name": p.name,
                "frames": int(p.frames), "source": p.source,
                "loop": [int(p.loop_start), int(p.loop_end)], "mode": p.mode,
                "gain": round(float(p.gain), 4), "pan": round(float(p.pan), 4),
                "speed": round(float(p.speed), 4), "reverse": bool(p.reverse),
                "quantize": bool(p.quantize), "label": p.label,
                "view": view_of("key", i, p.name, p.frames)})
        kept, kept_ids = [], {}
        for slot, k in sorted(self._kept.items(),
                              key=lambda kv: (kv[0][0] != "t", int(kv[0].split(":")[1]))):
            kind, i = slot.split(":")[0], int(slot.split(":")[1])
            if (e.tracks[i].src is not None) if kind == "track" else e.pads[i].loaded:
                continue
            f = k["file"]
            # its own entry with the fingerprint it was saved with, never merged
            # into a live file of the same path whose fingerprint may be why it failed
            sig = repr((f["path"], f.get("size"), f.get("edges_sha256"), f["granted"]))
            if sig not in kept_ids:
                kept_ids[sig] = "f%d" % len(files)
                files.append(dict(f, id=kept_ids[sig]))
            (tracks if kind == "track" else keys).append(
                dict(k["row"], slot=i, file=kept_ids[sig]))
            kept.append("%s (%s)" % ("track %d" % (i + 1) if kind == "track"
                                     else "key %s" % KEY_CAPS[i],
                                     os.path.basename(f["path"])))
        tracks.sort(key=lambda row: row["slot"])
        keys.sort(key=lambda row: row["slot"])
        granted = [f for f in files if f["granted"]]
        signed = ""
        if granted:
            signed = session.sign_grants(session.install_key(self.session_key_path), granted)
        doc = {session.KIND: session.VERSION,
               "saved": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "engine": {"samplerate": e.sr, "bpm": round(float(e.transport.bpm), 4),
                          "quantum": int(e.transport.quantum_i),
                          "master_gain": round(float(e.master_gain), 4)},
               "files": files, "tracks": tracks, "keys": keys,
               "grants": {"signed": signed}}
        fname = os.path.basename(str(name)) if name else session.default_name()
        if not fname.endswith(".json"):
            fname += ".json"
        path = os.path.join(self.sessions_dir, fname)
        session.write(doc, path)
        return path, doc, unsaved, kept

    def _session_load_async(self, msg):
        name = os.path.basename(str(msg.get("name", "")))
        self.session.update(name=name, state="loading", error="")
        threading.Thread(target=self._session_load_thread,
                         args=(os.path.join(self.sessions_dir, name),),
                         daemon=True).start()

    def _session_load_thread(self, path):
        try:
            result = self.load_session(path, analyse=False)
        except Exception as x:                       # never left reading "loading"
            msg = "%s did not load — %s." % (os.path.basename(path), x)
            self.session.update(state="failed", error=msg)
            result = {"name": os.path.basename(path), "state": "failed", "error": msg,
                      "missing": {}, "views": {}, "_analyse": []}
        todo = result.pop("_analyse", [])
        self.hub.text(dict(result, op="session.loaded"))
        self._analyse(todo)

    def load_session(self, path, analyse=True):
        """Put a saved set back. Synchronous; call it off the audio thread.

        Every refusal happens before the engine is touched. Read and
        version-check the file; validate every file it names on disk, and
        every grant; decode what survives; add up what the set will hold
        against the memory ceiling. Only then are the current tracks and keys
        cleared and the session's posted — so a session that cannot fit, or
        cannot be read, leaves the set you had exactly as it was."""
        name = os.path.basename(path)
        MiB = 1048576

        def failed(msg):
            # the set on deck is untouched, and so are the marks on its empty slots
            self.session.update(name=name, state="failed", error=msg)
            return {"name": name, "state": "failed", "error": msg,
                    "missing": {}, "views": {}, "_analyse": []}

        try:
            doc = session.read(path)
        except session.SessionError as x:
            return failed(str(x))
        e = self.engine

        # Someone will hand-edit this file. Every value's type is checked here,
        # so nothing after the current set is cleared can stop a load halfway.
        def rows_of(key):
            v = doc.get(key)
            return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []

        def num(row, key, default, lo, hi):
            try:
                v = float(row.get(key, default))
            except (TypeError, ValueError):
                return default
            return max(lo, min(hi, v)) if math.isfinite(v) else default

        files = {f["id"]: f for f in rows_of("files")
                 if isinstance(f.get("id"), str) and isinstance(f.get("path"), str)}
        granted = [f for f in files.values() if f.get("granted")]
        grants = doc.get("grants")
        grants_ok = bool(granted) and session.grants_verify(
            session.install_key(self.session_key_path), granted,
            grants.get("signed") if isinstance(grants, dict) else None)

        why = {}
        for fid, f in files.items():
            reason = session.check_file(f)
            if reason is None and not self._inside_roots_only(f["path"]) \
                    and not (f.get("granted") and grants_ok):
                reason = ("is outside the folders this run may read, and the "
                          "session's grant for it does not check out — pick it "
                          "again with the dialog")
            why[fid] = reason
        decoded = {}
        for fid, reason in why.items():
            if reason is None:
                try:
                    decoded[fid] = decode(files[fid]["path"])
                except Exception as x:              # DecodeError, or a file libsndfile chokes on
                    why[fid] = "could not be decoded — %s" % x

        variants = {}

        def buf(fid, mode):
            if (fid, mode) not in variants:
                src, sr = decoded[fid]
                variants[(fid, mode)] = src if mode == "STEREO" else \
                    dsp.center_extract(src, sr, mode.lower())
            return variants[(fid, mode)]

        def slot_of(row, n):
            try:
                i = int(row.get("slot", -1))
            except (TypeError, ValueError):
                return -1
            return i if 0 <= i < n else -1

        def region(row, frames):
            try:
                ls, le = int(row["loop"][0]), int(row["loop"][1])
            except (KeyError, IndexError, TypeError, ValueError):
                return 0, frames
            return max(0, min(ls, frames)), max(0, min(le, frames))

        missing, kept, tracks, keys, seen = {}, {}, [], [], set()

        def usable(kind, row, n):
            """-> (slot, file id) to load, or None. A row that cannot load leaves
            a missing mark and, when it names a file, the row itself, kept for
            the next save. A grant goes back into a save only if this machine's
            signature over it checked out: re-signing one that did not would
            turn a hand-edited path into a trusted one."""
            i = slot_of(row, n)
            if i < 0 or (kind, i) in seen:
                return None
            seen.add((kind, i))
            fid = row.get("file")
            f = files.get(fid) if isinstance(fid, str) else None
            reason = "is not in the session's file list" if f is None else why[fid]
            if reason is None:
                return i, fid
            slot = "%s:%d" % (kind, i)
            missing[slot] = {"name": os.path.basename(f["path"]) if f
                             else str(row.get("name") or ""), "reason": reason}
            if f:
                entry = {k: f[k] for k in ("path", "size", "mtime_ns", "edges_sha256") if k in f}
                entry["granted"] = bool(f.get("granted")) and grants_ok
                kept[slot] = {"row": dict(row), "file": entry}
            return None

        for row in rows_of("tracks"):
            got = usable("track", row, len(e.tracks))
            if got:
                i, fid = got
                mode = row.get("mode") if row.get("mode") in MODES else "STEREO"
                tracks.append((i, row, fid, buf(fid, "STEREO"),
                               buf(fid, mode) if mode != "STEREO" else None, mode))
        for row in rows_of("keys"):
            got = usable("key", row, len(e.pads))
            if got:
                i, fid = got
                source = row.get("source") if row.get("source") in MODES else "STEREO"
                keys.append((i, row, fid, buf(fid, source), source))

        if e.memory_limit > 0:
            by_tracks = {}
            for (_, _, _, src, var, _) in tracks:
                by_tracks[id(src)] = src.nbytes
                if var is not None:
                    by_tracks[id(var)] = var.nbytes
            total = dict(by_tracks)
            extra = []
            for (i, _, fid, b, _) in keys:
                if id(b) not in total:
                    extra.append((KEY_CAPS[i], os.path.basename(files[fid]["path"]), b.nbytes))
                total[id(b)] = b.nbytes
            need = sum(total.values())
            if need > e.memory_limit:
                if extra:
                    return failed(
                        "Nothing was loaded: this session holds %d MB of audio and "
                        "the ceiling is %d MB. These keys hold files no track in it "
                        "shows: %s. Clear them from the session, or restart with "
                        "--memory-mb." % (need // MiB, e.memory_limit // MiB,
                                          ", ".join("key %s (%s, %d MB)" % (c, n, b // MiB)
                                                    for c, n, b in extra)))
                return failed(
                    "Nothing was loaded: this session's tracks alone hold %d MB of "
                    "audio and the ceiling is %d MB. Restart with --memory-mb."
                    % (sum(by_tracks.values()) // MiB, e.memory_limit // MiB))

        # Past every refusal. Now the current set goes, and the session arrives.
        e.post("transport.stop")
        for i in range(len(e.tracks)):
            e.post("track.clear", i=i)
        for i in range(len(e.pads)):
            e.post("pad.clear", i=i)
        self.peaks.clear()
        self.pad_peaks.clear()
        self.meta.clear()
        todo = []
        for (i, row, fid, src, var, mode) in tracks:
            f, sr = files[fid], decoded[fid][1]
            nm = os.path.basename(f["path"])
            ls, le = region(row, src.shape[0])
            e.post("track.load", i=i, buf=src, sr=sr, name=nm, path=f["path"],
                   bpm=0.0, conf=0.0, slices=np.zeros(0, dtype=np.int64),
                   analysing=True)
            if var is not None:
                e.post("track.variant", i=i, mode=mode, buf=var)
            e.post("track.loop", i=i, ls=ls, le=le)
            e.post("track.gain", i=i, v=num(row, "gain", 0.65, 0.0, 1.4))
            e.post("track.pan", i=i, v=num(row, "pan", 0.0, -1.0, 1.0))
            e.post("track.speed", i=i, v=num(row, "speed", 1.0, 0.25, 4.0))
            e.post("track.rev", i=i, v=bool(row.get("reverse", False)))
            e.post("track.mute", i=i, v=bool(row.get("mute", False)))
            e.post("track.solo", i=i, v=bool(row.get("solo", False)))
            if not self._inside_roots_only(f["path"]):
                self._grant(f["path"])
            self.meta[i] = {"frames": src.shape[0], "sr": sr, "channels": src.shape[1],
                            "name": nm, "stage": "playable", "error": ""}
            pk = dsp.peaks(var if var is not None else src, 2048)
            self.peaks[i] = pk
            self.hub.peaks(i, pk)
            todo.append((i, src, sr))
        for (i, row, fid, b, source) in keys:
            f, sr = files[fid], decoded[fid][1]
            ls, le = region(row, b.shape[0])
            e.post("pad.load", i=i, buf=b, sr=sr, name=os.path.basename(f["path"]),
                   path=f["path"], ls=ls, le=le, source=source,
                   label=str(row.get("label") or ""))
            e.post("pad.assign", i=i,               # the engine sets these as given
                   mode=row.get("mode") if row.get("mode") in KEY_MODES else ONESHOT,
                   gain=num(row, "gain", 1.0, 0.0, 1.4), pan=num(row, "pan", 0.0, -1.0, 1.0),
                   speed=num(row, "speed", 1.0, 0.25, 4.0),
                   quantize=bool(row.get("quantize", False)),
                   reverse=bool(row.get("reverse", False)))
            if not self._inside_roots_only(f["path"]):
                self._grant(f["path"])
            pk = dsp.peaks(b, 2048)
            self.pad_peaks[i] = pk
            self.hub.pad_peaks(i, pk)
        eng = doc.get("engine") if isinstance(doc.get("engine"), dict) else {}
        bpm = num(eng, "bpm", 0.0, 0.0, 1000.0)
        if bpm > 0:
            e.post("transport.bpm", v=bpm)
        quantum = num(eng, "quantum", -1.0, 0.0, 64.0)
        if quantum >= 0:
            e.post("transport.quantum", v=int(quantum))
        if "master_gain" in eng:
            e.post("master.gain", v=num(eng, "master_gain", float(e.master_gain), 0.0, 1.2))

        views = {}
        loaded = {("track", i): src.shape[0] for (i, _, _, src, _, _) in tracks}
        loaded.update({("key", i): b.shape[0] for (i, _, _, b, _) in keys})
        names = {("track", i): os.path.basename(files[fid]["path"]) for (i, _, fid, _, _, _) in tracks}
        names.update({("key", i): os.path.basename(files[fid]["path"]) for (i, _, fid, _, _) in keys})
        for kind, rows in (("track", rows_of("tracks")), ("key", rows_of("keys"))):
            for row in rows:
                i = slot_of(row, len(e.tracks) if kind == "track" else len(e.pads))
                v = row.get("view")
                if (kind, i) not in loaded or not isinstance(v, dict):
                    continue
                try:
                    vs, ve = float(v["vs"]), float(v["ve"])
                except (KeyError, TypeError, ValueError):
                    continue
                if math.isfinite(vs) and math.isfinite(ve):   # NaN is not JSON a browser reads
                    views["%s:%d" % (kind, i)] = {
                        "sig": "%s|%d" % (names[(kind, i)], loaded[(kind, i)]),
                        "vs": vs, "ve": ve}
        self._kept = kept
        self.session.update(name=name, state="loaded", error="", missing=missing)
        result = {"name": name, "state": "loaded", "error": "", "missing": missing,
                  "views": views, "_analyse": todo}
        if analyse:
            self._analyse(result.pop("_analyse"))
        return result

    def _analyse(self, todo):
        """Tempo and slices after the set is already playable — display only."""
        for (i, src, sr) in todo:
            try:
                bpm, conf = dsp.estimate_bpm(src, sr)
                pts, method = dsp.slice_points(src, sr, 16)
            except Exception as x:
                self.meta.setdefault(i, {})["error"] = "analysis failed: %s" % x
                continue
            self.engine.post("track.analysis", i=i, bpm=bpm, conf=conf, slices=pts)
            self.meta.setdefault(i, {}).update(
                bpm=bpm, conf=conf, slices=pts.tolist(), slice_method=method,
                stage="analysed")

    # -- recording -------------------------------------------------------------
    def record_state(self):
        """The take, for telemetry: its state, length and file, and the last one."""
        r = self.engine.recorder
        out = r.report() if r is not None else {
            "state": REC_IDLE, "elapsed_s": 0.0, "name": "", "parts": 0,
            "dropped": 0, "lag_ms": 0.0, "error": "", "free_s": None}
        out["error"] = out["error"] or self.record_error
        out["last"] = self.record_last
        return out

    def record_toggle(self):
        """REC. Idle: arm a take, which starts with the clock, or at once if the
        clock is already running. Armed or recording: end it. The file is
        opened here and written by the recorder's own thread; the audio thread
        only ever copies into its ring."""
        e = self.engine
        r = e.recorder
        if r is None:
            r = e.recorder = Recorder(e.sr, on_end=self._take_ended)
        if r.state == REC_IDLE:
            self.record_error = ""
            self._take_stalled = False
            try:
                r.arm(self._take_path())
            except Exception as x:
                self.record_error = ("Not recording — nothing could be written in %s: %s."
                                     % (self.recordings_dir,
                                        getattr(x, "strerror", None) or x))
                # the rail points at the inspector, so the reason has to reach it
                self.hub.text({"op": "record.done", "disarmed": False, "files": [],
                               "error": self.record_error})
                return
            if e.transport.playing:
                r.begin(e.xruns)
        elif r.state in (REC_ARMED, REC_RECORDING):
            r.request_stop()

    def _take_path(self):
        os.makedirs(self.recordings_dir, exist_ok=True)
        stem = os.path.join(self.recordings_dir, time.strftime("rec-%Y%m%d-%H%M%S"))
        k = 1
        while True:
            p = stem + ("" if k == 1 else "-%d" % k) + ".wav"
            if not os.path.exists(p) and not os.path.exists(part_path(p, 2)):
                return p
            k += 1

    def _take_ended(self, s):
        """On the recorder's writer thread, once the file is closed."""
        if s["frames"] == 0 and not s["error"]:
            self.hub.text({"op": "record.done", "disarmed": True})
            return
        r = self.engine.recorder
        xr = max(0, self.engine.xruns - (r.xruns_at_begin if r is not None else 0))
        self.record_last = {"name": os.path.basename(s["path"]), "seconds": s["seconds"],
                            "dropped": s["dropped"], "error": s["error"]}
        self.hub.text(dict(s, op="record.done", disarmed=False, xruns=xr,
                           stalled=self._take_stalled, sr=self.engine.sr,
                           dir=self.recordings_dir))

    # -- convenience -------------------------------------------------------
    def load_kit(self, d):
        d = os.path.realpath(os.path.expanduser(d))
        if not os.path.isdir(d):
            self.engine.last_error = "No folder at %s." % d
            return []
        files = [os.path.join(d, n) for n in sorted(os.listdir(d))
                 if os.path.splitext(n)[1].lower() in AUDIO_EXT]
        for i, p in enumerate(files[:len(self.engine.tracks)]):
            self._forget_missing("track:%d" % i)
            self.loader.load_async(i, p, 16)
        return files

    def fill_keys_from_slices(self, track_i, mode="ONE"):
        """The demo kit's convenience at launch — and only then.

        It fills the keys from a track's slices when no key holds anything. A
        session restored into this run, or any key assigned before it, must
        never be wiped by something that runs at startup. -> did it fill?
        """
        if any(p.loaded for p in self.engine.pads):
            return False
        self.map_pads(track_i, mode)
        return True

    def map_pads(self, track_i, mode="ONE"):
        """Fill the keys from a track's slices — each one SNAPSHOTTING the
        audio, so the keys survive the track being replaced."""
        t = self.engine.tracks[track_i]
        pts = t.slices
        n = max(1, int(pts.size))
        for k in range(len(self.engine.pads)):
            if pts.size:
                j = k % n
                ls = int(pts[j])
                le = int(pts[j + 1]) if j + 1 < pts.size else t.frames
            else:
                ls, le = t.loop_start, t.loop_end
            self.handle({"op": "pad.take", "i": k, "track": track_i, "replace": True,
                         "ls": ls, "le": le, "slice": k % n,
                         "label": "%s/%02d" % (t.name.split(".")[0][:9], k + 1)})
            self.engine.post("pad.assign", i=k, mode=mode,
                             quantize=(mode == "LOOP"))
