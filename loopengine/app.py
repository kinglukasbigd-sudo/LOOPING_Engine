"""Wiring: engine + loader + server + telemetry pump."""
from __future__ import annotations

import collections
import os
import struct
import threading
import time

import numpy as np

from . import picker
from .loader import Loader, AUDIO_EXT
from .server import Hub, new_token, OP_BIN, BIN_SCOPE, BIN_PEAKS
from .track import MODES

MAX_UPLOAD = 200 * 1024 * 1024


class App:
    def __init__(self, engine, roots=None, inbox=".inbox", fps=30):
        self.engine = engine
        self.token = new_token()
        self.hub = Hub()
        self.fps = fps
        self.roots = [os.path.realpath(os.path.expanduser(r))
                      for r in (roots or ["~"])]
        self.inbox = os.path.realpath(inbox)
        self.loader = Loader(engine, on_done=self._loaded,
                             on_error=self._load_error)
        self.peaks = {}     # track index -> ndarray (buckets, 2)
        # Paths this server itself returned from a user-driven OS dialog.
        # The dialog exists to reach folders outside --root, so its results
        # cannot be judged by the root check; choosing a file in the OS
        # picker IS the grant. Only paths we produced are trusted, never
        # paths a caller supplies, and the set is bounded.
        self._granted = collections.OrderedDict()
        self._pick_seq = 0
        self.meta = {}      # track index -> dict of analysis results
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

    def start_pump(self):
        threading.Thread(target=self.pump, daemon=True).start()
        return self

    def on_client_join(self, client):
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
        return any(rp == r or rp.startswith(r + os.sep)
                   for r in self.roots + [self.inbox])

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
    def handle(self, msg):
        op = msg.get("op", "")
        if op == "load":
            i, path = int(msg["i"]), msg["path"]
            if not self._inside_roots(path):
                return self._load_error(i, "%s is outside the folders this "
                                           "run is allowed to read." % path)
            self.meta.setdefault(i, {})["error"] = ""
            self.loader.load_async(i, path, int(msg.get("slices", 16)))
        elif op == "pick":
            self.pick_async(int(msg.get("i", 0)), bool(msg.get("multiple")),
                            msg.get("dir"))
        elif op == "load_kit":
            self.load_kit(msg["dir"])
        elif op == "unload":
            i = int(msg["i"])
            self.engine.post("track.clear", i=i)
            self.peaks.pop(i, None)
            self.meta.pop(i, None)
        elif op == "mode":
            if msg.get("mode") in MODES:
                self.loader.variant_async(int(msg["i"]), msg["mode"])
        elif op == "reslice":
            self.loader.reslice_async(int(msg["i"]), int(msg.get("n", 16)))
        elif op == "pads.map":
            self.map_pads(int(msg["track"]), msg.get("mode", "ONE"))
        elif op == "tap":
            self.engine.transport.tap(time.monotonic())
        elif op.startswith(("track.", "transport.", "pad.", "master.", "clip.")) \
                or op in ("panic", "probe"):
            kw = {k: v for k, v in msg.items() if k != "op"}
            self.engine.post(op, **kw)

    # -- convenience -------------------------------------------------------
    def load_kit(self, d):
        d = os.path.realpath(os.path.expanduser(d))
        if not os.path.isdir(d):
            self.engine.last_error = "No folder at %s." % d
            return []
        files = [os.path.join(d, n) for n in sorted(os.listdir(d))
                 if os.path.splitext(n)[1].lower() in AUDIO_EXT]
        for i, p in enumerate(files[:len(self.engine.tracks)]):
            self.loader.load_async(i, p, 16)
        return files

    def map_pads(self, track_i, mode="ONE"):
        t = self.engine.tracks[track_i]
        n = max(1, int(t.slices.size))
        for k in range(len(self.engine.pads)):
            self.engine.post("pad.assign", i=k, track=track_i,
                             slice=k % n, mode=mode,
                             quantize=(mode == "LOOP"),
                             label="%s/%02d" % (t.name.split(".")[0][:9], k + 1))
