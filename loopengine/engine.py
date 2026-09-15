"""The audio callback and everything it owns.

Threading contract
------------------
  audio thread   : drains `cmds`, renders, writes telemetry. Never blocks,
                   never opens a file, never allocates a large array.
  control thread : appends to `cmds`. deque.append/popleft are atomic under
                   CPython, so no lock is needed on the hot path.
  loader thread  : decodes and analyses, then hands finished ndarrays over
                   through a command. Buffer swaps are pointer assignments.
"""
from __future__ import annotations

import collections
import gc
import threading
import time
import weakref

import numpy as np

from .track import Track, MIN_LOOP
from .transport import Transport, QUANTA, QUANTUM_LABELS
from .voice import VoicePool, ONESHOT, LOOP

SCOPE_N = 4096
LAT_N = 512          # rolling window of command pickup times

# Ops that wait for the next quantum. Everything else lands now.
QUANTIZED = {
    "track.play", "track.stop", "track.toggle", "track.rev", "track.retrig",
    "pad.trigger.q",
    # A region change on a running track lands on the boundary too. Moving the
    # points under a moving playhead is what clicks; landing them where the
    # material is already coherent, with the existing equal-power crossfade
    # over the new seam, is what does not. The panel paints the drag locally,
    # so the hand still gets an answer on the same frame.
    "track.loop", "track.loop.scale", "track.loop.nudge", "track.loop.slice",
}

# Of those, the ones that are a MOMENT rather than a state. An edit describes
# how a thing should be and can land whenever; a launch describes when a thing
# should happen, and without a running clock there is no when.
#
# This is the other half of "a stopped clock has no edges". That invariant is
# right for an edit — it stopped a queue stranding and reverting a later one.
# Applied to a launch it inverted the meaning of the transport: a start queued
# at 4BAR fired at the instant of pressing STOP, and since tracks are not gated
# on the transport, STOP started a track and it was audible.
#
# So stopping the clock cancels everything that was waiting for it. The queue
# is a promise about a future moment on the grid; stopping the grid means that
# moment never comes. The alternative — some queued work surviving a stop and
# some not — is a rule nobody can hold at speed in the dark, and it is the
# stranded-command hazard again with a different name.
LAUNCH = {
    "track.play", "track.stop", "track.toggle", "track.retrig", "pad.trigger.q",
}

# The quantised queue holds at most one entry per (op, index) — collapse sees
# to that. Nine track ops over eight tracks and one key op over sixteen keys is
# 88, so this never fills; if it ever did, the command applies at once and is
# counted rather than dropped.
QUEUE_CAP = 256

QUEUED_LABEL = {
    "track.play": "START", "track.stop": "STOP", "track.toggle": "TOGGLE",
    "track.rev": "REV", "track.retrig": "RETRIG", "track.loop": "LOOP",
    "track.loop.scale": "LOOP", "track.loop.nudge": "LOOP",
    "track.loop.slice": "LOOP",
}


def _gc_watch(engine):
    """A gc callback counting collections that run on this engine's audio
    thread, and the longest. Holds the engine only weakly."""
    ref = weakref.ref(engine)

    def hook(phase, info):
        e = ref()
        if e is None or threading.get_ident() != e._audio_ident:
            return
        if phase == "start":
            e._gc_t0 = time.perf_counter()
        else:
            e.gc_audio += 1
            ms = (time.perf_counter() - e._gc_t0) * 1000.0
            if ms > e.gc_audio_max_ms:
                e.gc_audio_max_ms = ms
    return hook


class Pad:
    """A key slot. Holds its OWN audio and its own region.

    A pad used to be a live link into a track — `track` plus `slice` — so
    loading a new file on that track destroyed what the key was pointing at.
    A key now snapshots the buffer reference and the region at the moment it
    is assigned, and keeps both until it is reassigned. Changing the track
    underneath it changes nothing here.

    Ownership is shared by reference: the decoded array stays alive while any
    track or any key still refers to it, and is freed when the last one lets
    go. Engine.audio_bytes() counts each distinct buffer once, however many
    holders it has.

    `track` and `slice` survive only as provenance for the label — nothing
    reads them at trigger time.
    """

    __slots__ = ("track", "slice", "mode", "gain", "pan", "speed", "quantize",
                 "label", "buf", "sr", "channels", "frames", "loop_start",
                 "loop_end", "name", "path", "reverse", "source")

    def __init__(self):
        self.track = -1
        self.slice = -1
        self.source = "STEREO"      # the track's source mode when taken
        self.mode = ONESHOT
        self.gain = 1.0
        self.pan = 0.0
        self.speed = 1.0
        self.quantize = False
        self.label = ""
        self.buf = None          # the pinned audio, not a track lookup
        self.sr = 48000
        self.channels = 0
        self.frames = 0
        self.loop_start = 0
        self.loop_end = 0
        self.name = ""
        self.path = ""
        self.reverse = False

    @property
    def loaded(self):
        return self.buf is not None and self.loop_end > self.loop_start

    def assign(self, buf, sr, name, path, ls, le, track=-1, slice_i=-1):
        self.buf = buf
        self.sr = int(sr)
        self.channels = buf.shape[1]
        self.frames = buf.shape[0]
        self.name = name
        self.path = path
        self.track = track
        self.slice = slice_i
        self.set_loop(ls, le)

    def clear(self):
        self.buf = None          # last reference here; the array may now go
        self.frames = 0
        self.loop_start = self.loop_end = 0
        self.name = ""
        self.path = ""
        self.label = ""
        self.track = -1
        self.slice = -1
        self.source = "STEREO"

    def set_loop(self, ls, le):
        """Same guards as a track: a key's region is edited the same way."""
        if self.buf is None:
            return
        ls, le = int(ls), int(le)
        if le <= ls:
            le = ls + MIN_LOOP
        ls = max(0, min(ls, self.frames - MIN_LOOP))
        le = max(ls + MIN_LOOP, min(le, self.frames))
        if le - ls < MIN_LOOP:
            ls = max(0, le - MIN_LOOP)
        self.loop_start, self.loop_end = ls, le

    def snapshot(self, i):
        return {"i": i, "track": self.track, "slice": self.slice,
                "mode": self.mode, "gain": round(self.gain, 3),
                "pan": round(self.pan, 3), "q": self.quantize,
                "label": self.label, "loaded": self.loaded,
                "name": self.name, "sr": self.sr, "ch": self.channels,
                "frames": self.frames, "ls": self.loop_start,
                "le": self.loop_end, "rev": self.reverse,
                "source": self.source,
                "speed": round(self.speed, 4),
                "mb": round((self.buf.nbytes / 1048576.0) if self.buf is not None else 0.0, 1)}


class Engine:
    def __init__(self, samplerate=48000, blocksize=256, device=None,
                 n_tracks=8, n_voices=16, n_pads=16, offline=False,
                 memory_mb=1024):
        self.blocksize = blocksize
        self.device = device
        self.offline = offline

        if offline:
            # No sound card. The callback still runs at real-time pace so the
            # panel, the meters and the scope behave exactly as they will
            # once PortAudio is present.
            self.sd = None
            self.device_name = "null backend (no PortAudio)"
            self.sr = int(samplerate or 48000)
            self.native_rate, self.native_how = None, "offline"
        else:
            import sounddevice as sd
            from . import graph
            self.sd = sd
            info = sd.query_devices(device, "output") if device is not None \
                else sd.query_devices(sd.default.device[1], "output")
            self.device_name = info["name"]
            self.native_rate, self.native_how = graph.native_output_rate()
            if samplerate is None:
                # Prefer the rate the graph actually runs at. Opening at the
                # device's advertised default put a resampler in the path on
                # this machine — the device says 44100, the sink runs 48000 —
                # which quietly undoes the engine's never-resample property at
                # the last hop.
                def supports(rate):
                    try:
                        sd.check_output_settings(device=device, samplerate=rate,
                                                 channels=2, dtype="float32")
                        return True
                    except Exception:
                        return False
                samplerate = graph.choose_rate(info["default_samplerate"], supports)
            self.sr = int(samplerate)

        self.transport = Transport(self.sr, 124.0)
        self.tracks = [Track(i, blocksize) for i in range(n_tracks)]
        self.pads = [Pad() for _ in range(n_pads)]
        self.voices = VoicePool(n_voices, blocksize)

        self.memory_limit = memory_mb * 1048576 if memory_mb else 0
        self.master_gain = 0.70
        self._mg = 0.70
        self.cmds = collections.deque()
        # The quantised queue, preallocated: two fixed slot arrays and a count,
        # compacted in place, so draining, collapsing, cancelling and firing never
        # build a container on the audio thread. Measured, the lists this replaces
        # never set a collection off — they came from CPython's free lists and
        # died in the block that made them — so this is allocation-free by
        # construction rather than by the allocator's good grace.
        self._q_op = [None] * QUEUE_CAP
        self._q_kw = [None] * QUEUE_CAP
        self._q_n = 0
        self.queue_overflow = 0
        # Which thread runs the callback, and what the collector did there.
        self._audio_ident = None
        self.gc_audio = 0
        self.gc_audio_max_ms = 0.0
        self._gc_t0 = 0.0
        self._gc_hook = None

        # How long a command sat in the queue before the audio thread took it.
        # This is the only stage of the path the engine can see; the wire and
        # the device are measured either side of it.
        self._lat = np.zeros(LAT_N, dtype=np.float32)
        self._lat_i = 0
        self._lat_n = 0
        self.probe_id = 0     # bounced back through telemetry, see App.handle
        # PortAudio's figure, latched when the stream opens. It is block
        # arithmetic, not a measurement — see loopback.py — so it is named for
        # what it is and never silently presented as the truth.
        self.output_ms_reported = 0.0
        self.output_ms_measured = None     # set by a loopback run, if one ran
        self.output_measurement = None     # the full result, method and all

        self._mix = np.zeros((blocksize, 2), dtype=np.float32)
        self._k = np.arange(blocksize, dtype=np.float64)
        self._scope = np.zeros((SCOPE_N, 2), dtype=np.float32)
        self._scope_w = 0
        self.master_peak = np.zeros(2, dtype=np.float32)
        self.clips = 0
        self.xruns = 0
        self.blocks = 0
        # A counted xrun says nothing. One at the first callback is warm-up;
        # one every N seconds is a period mismatch; one next to a decode is
        # work landing on the audio thread. Record when and what.
        self._xrun_log = []
        self._t_start = None
        self._cap_buf = None      # diagnostics: see capture()
        self._cap_w = 0
        self.recorder = None      # the master recording: see recorder.py
        self.last_error = ""
        self.stream = None

    # ==================================================================
    # lifecycle
    # ==================================================================
    def start(self):
        self.watch_gc()
        self._t_start = time.perf_counter()
        if self.offline:
            self.stream = NullStream(self)
            self.stream.start()
            self.output_ms_reported = self.stream.latency * 1000.0
            return self
        self.stream = self.sd.OutputStream(
            samplerate=self.sr, blocksize=self.blocksize, device=self.device,
            channels=2, dtype="float32", callback=self._callback,
            latency="low",
        )
        self.stream.start()
        self.output_ms_reported = self.stream.latency * 1000.0
        return self

    def stop(self):
        self.unwatch_gc()
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None

    def render_offline(self, frames: int) -> np.ndarray:
        """Pull `frames` frames straight through the callback. No device.

        Used by the null backend and by `python -m loopengine.offline`.
        """
        out = np.zeros((frames, 2), dtype=np.float32)
        bs = self.blocksize
        for off in range(0, frames, bs):
            n = min(bs, frames - off)
            self._callback(out[off:off + n], n, None, None)
        return out

    def post(self, op: str, **kw):
        """Called from any non-audio thread. Stamped so the callback can say
        how long it waited to be picked up."""
        self.cmds.append((op, kw, time.perf_counter()))

    # ==================================================================
    # callback
    # ==================================================================
    def _callback(self, outdata, frames, time_info, status):
        if self._audio_ident is None:
            self._audio_ident = threading.get_ident()   # once; the gc watch keys on it
        if status:
            self.xruns += 1
            if len(self._xrun_log) < 512:
                # only allocates when one actually happens
                self._xrun_log.append((
                    (time.perf_counter() - self._t_start) if self._t_start else 0.0,
                    self.blocks,
                    bool(getattr(status, "output_underflow", False)),
                    bool(getattr(status, "output_overflow", False)),
                    bool(getattr(status, "priming_output", False)),
                ))
        self.blocks += 1

        self._drain()

        mix = self._mix[:frames]
        mix[:] = 0.0

        # A queue must never be unreachable. If the clock is not running there
        # are no boundaries to wait for, so drain it now rather than let it
        # strand and fire on the next start.
        if self._q_n and not self.transport.playing:
            self._fire()

        off = 0
        guard = 0
        while off < frames and guard < 64:
            guard += 1
            n = frames - off
            if self._q_n:
                d = self.transport.frames_to_boundary()
                if d == 0:
                    self._fire()
                    d = self.transport.frames_to_boundary()
                if self._q_n and d > 0:
                    n = min(n, d)
            self._render_span(mix[off:off + n], n)
            self.transport.advance(n)
            off += n
        if off < frames:
            self._render_span(mix[off:frames], frames - off)
            self.transport.advance(frames - off)

        # master gain, ramped
        g0 = self._mg
        self._mg = g0 + (self.master_gain - g0) * min(1.0, frames / (0.008 * self.sr))
        ramp = self._k[:frames] * (1.0 / frames)
        mix *= (g0 + (self._mg - g0) * ramp).astype(np.float32)[:, None]

        self.master_peak[0] = np.abs(mix[:, 0]).max()
        self.master_peak[1] = np.abs(mix[:, 1]).max()
        if self.master_peak.max() > 0.999:
            self.clips += 1

        # Padé soft clip. Transparent under 0.7, firm above it, never folds.
        x2 = mix * mix
        np.multiply(mix, 27.0 + x2, out=outdata[:frames])
        outdata[:frames] /= 27.0 + 9.0 * x2
        np.clip(outdata[:frames], -1.0, 1.0, out=outdata[:frames])

        # diagnostic capture, if one is running: one branch, no allocation
        cb = self._cap_buf
        if cb is not None:
            w = self._cap_w
            if w + frames <= cb.shape[0]:
                cb[w:w + frames] = outdata[:frames]
                self._cap_w = w + frames

        # the master recording, if there is one: a copy into its ring, no more
        rec = self.recorder
        if rec is not None:
            rec.feed(outdata[:frames])

        # scope ring
        take = min(frames, SCOPE_N)
        w = self._scope_w
        src = outdata[frames - take:frames]
        end = w + take
        if end <= SCOPE_N:
            self._scope[w:end] = src
        else:
            first = SCOPE_N - w
            self._scope[w:] = src[:first]
            self._scope[:end - SCOPE_N] = src[first:]
        self._scope_w = end % SCOPE_N

    def _render_span(self, out, n):
        if n <= 0:
            return
        # Index loops: a list iterator, or the generator any() needs, is a
        # collector-tracked object with no free list, built every span.
        tracks = self.tracks
        any_solo = False
        for k in range(len(tracks)):
            if tracks[k].solo:
                any_solo = True
                break
        for k in range(len(tracks)):
            tracks[k].render(out, n, self.sr, any_solo)
        voices = self.voices.voices
        for k in range(len(voices)):
            if voices[k].active:
                voices[k].render(out, n, self.sr)

    # ==================================================================
    # commands
    # ==================================================================
    def _drain(self):
        cmds = self.cmds
        now = time.perf_counter()
        while cmds:
            try:
                op, kw, posted = cmds.popleft()
            except IndexError:
                break
            self._lat[self._lat_i] = (now - posted) * 1000.0
            self._lat_i = (self._lat_i + 1) % LAT_N
            if self._lat_n < LAT_N:
                self._lat_n += 1
            if op in QUANTIZED and self.transport.playing and \
                    self.transport.quantum_beats > 0:
                self._enqueue(op, kw)
            else:
                self._apply(op, kw)

    @property
    def _pending(self):
        """The queue as (op, kw) pairs — a copy, for tests and the telemetry
        thread. The callback never reads this; it reads _q_n."""
        return [(self._q_op[k], self._q_kw[k]) for k in range(self._q_n)]

    @property
    def pending_labels(self):
        return [self._q_op[k] for k in range(self._q_n)]

    def _enqueue(self, op, kw):
        """Last edit wins. A drag emits a command per pointer move, so without
        this a two-second wait at BAR piles up a hundred redundant region
        changes that all fire at once. The earlier entry for the same (op,
        index) is removed by shifting the rest down and the new one goes to
        the back — the order edits arrived in is the order they apply."""
        ops, kws, n = self._q_op, self._q_kw, self._q_n
        i = kw.get("i")
        if i is not None:
            k = 0
            while k < n:
                if ops[k] == op and kws[k].get("i") == i:
                    j = k
                    while j < n - 1:
                        ops[j] = ops[j + 1]
                        kws[j] = kws[j + 1]
                        j += 1
                    n -= 1
                    ops[n] = None
                    kws[n] = None
                else:
                    k += 1
        if n >= QUEUE_CAP:
            self._q_n = n
            self.queue_overflow += 1
            self._apply(op, kw)
            return
        ops[n] = op
        kws[n] = kw
        self._q_n = n + 1
        self._label_pending(op, kw)

    def _label_pending(self, op, kw):
        i = kw.get("i")
        if op.startswith("track.") and isinstance(i, int) and 0 <= i < len(self.tracks):
            self.tracks[i].queued = QUEUED_LABEL.get(op, op)

    def _stop_sound(self):
        """SPACE stops what you hear, not just the clock.

        The label always said "run / stop"; the transport used to stop only
        the grid while every playing track and key went on sounding until ESC.
        Now tracks fade through their gain smoother (about 6 ms at 256 frames)
        and keys through their release ramp (4 ms) — one-shots included — so a
        stop never clicks. Starting the clock again does not bring them back:
        what was stopped stays stopped until it is launched. ESC is still the
        immediate cut.
        """
        for t in self.tracks:
            if t.playing:
                t.stopping = True
        self.voices.fade_all()

    def _cancel_launches(self):
        """Drop queued moments; keep queued edits.

        Called the instant the clock stops, from inside _drain, so it lands
        before the callback's drain-on-stop reaches the rest of the queue.
        Nothing is marked as having fired, because nothing did — the labels
        simply clear, which is the panel saying the queue is gone rather than
        pretending it landed.
        """
        ops, kws, n = self._q_op, self._q_kw, self._q_n
        w = 0
        for k in range(n):
            if ops[k] not in LAUNCH:
                ops[w] = ops[k]
                kws[w] = kws[k]
                w += 1
        if w == n:
            return
        for k in range(w, n):
            ops[k] = None
            kws[k] = None
        self._q_n = w
        self._relabel()

    def _relabel(self):
        """Rebuild the queued labels from the queue itself."""
        tracks = self.tracks
        for k in range(len(tracks)):
            tracks[k].queued = None
        ops, kws = self._q_op, self._q_kw
        for k in range(self._q_n):
            self._label_pending(ops[k], kws[k])

    def _fire(self):
        # Emptied before applying; nothing _apply does enqueues (transport ops,
        # the only ones that touch the queue, are never quantised themselves).
        ops, kws, n = self._q_op, self._q_kw, self._q_n
        self._q_n = 0
        for k in range(n):
            op = ops[k]
            kw = kws[k]
            ops[k] = None
            kws[k] = None
            self._apply(op, kw)
        tracks = self.tracks
        for k in range(len(tracks)):
            t = tracks[k]
            if t.queued is not None:
                t.queued = None
                t.fired += 1

    def _t(self, kw):
        i = kw.get("i", -1)
        if isinstance(i, int) and 0 <= i < len(self.tracks):
            return self.tracks[i]
        return None

    def _apply(self, op, kw):
        tr = self.transport

        if op == "track.load":
            t = self._t(kw)
            # The ceiling bites here, not at pad.take. Taking from a loaded
            # track costs nothing — the track already holds that array. Memory
            # grows when the TRACK moves on and a key keeps the old buffer
            # alive. So this is the moment to refuse, while the user still has
            # the old arrangement in front of them to clear.
            if t is not None and self.would_exceed(kw["buf"]):
                pinned = sorted({p.name for p in self.pads if p.loaded})
                self.last_error = (
                    "No room to load %s. %d MB of audio is held against a %d "
                    "MB ceiling, and these keys are pinning files the tracks "
                    "no longer show: %s. Clear a key, or restart with "
                    "--memory-mb." % (kw.get("name", "that file"),
                                      self.audio_bytes() // 1048576,
                                      self.memory_limit // 1048576,
                                      ", ".join(pinned) or "none"))
                return
            if t:
                t.load(kw["buf"], kw["sr"], kw["name"], kw["path"], kw["bpm"],
                       kw["conf"], kw["slices"], kw.get("xfade_ms", 6.0),
                       analysing=kw.get("analysing", False))
        elif op == "track.clear":
            t = self._t(kw)
            if t:
                t.clear()
        elif op == "track.variant":
            t = self._t(kw)
            if t:
                t.variants[kw["mode"]] = kw["buf"]
                t.mode = kw["mode"]
        elif op == "track.mode":
            t = self._t(kw)
            if t and kw["mode"] in t.variants:
                t.mode = kw["mode"]
        elif op == "track.play":
            t = self._t(kw)
            if t and t.src is not None:
                t.playing, t.stopping = True, False
                if kw.get("retrig", True):
                    t.phase = float(t.loop_start)
        elif op == "track.stop":
            t = self._t(kw)
            if t and t.playing:
                t.stopping = True            # fades, then halts in render()
        elif op == "track.toggle":
            t = self._t(kw)
            if t and t.src is not None:
                if t.stopping:               # caught mid-fade: carry on from here —
                    t.stopping = False       # jumping back to the loop start would click
                elif t.playing:
                    t.stopping = True        # fade out, then halt
                else:
                    t.playing = True
                    t.phase = float(t.loop_start)
        elif op == "track.retrig":
            t = self._t(kw)
            if t:
                t.phase = float(t.loop_start)
        elif op == "track.rev":
            t = self._t(kw)
            if t:
                # a toggle from a key press; a session sets it outright
                t.reverse = bool(kw["v"]) if "v" in kw else not t.reverse
        elif op == "track.mute":
            t = self._t(kw)
            if t:
                t.mute = bool(kw.get("v", not t.mute))
        elif op == "track.solo":
            t = self._t(kw)
            if t:
                t.solo = bool(kw.get("v", not t.solo))
        elif op == "track.gain":
            t = self._t(kw)
            if t:
                t.gain = max(0.0, min(1.4, float(kw["v"])))
        elif op == "track.pan":
            t = self._t(kw)
            if t:
                t.set_pan(kw["v"])
        elif op == "track.speed":
            t = self._t(kw)
            if t:
                t.speed = max(0.25, min(4.0, float(kw["v"])))
        elif op == "track.loop":
            t = self._t(kw)
            if t:
                t.set_loop(kw["ls"], kw["le"])
        elif op == "track.loop.scale":
            t = self._t(kw)
            if t:
                t.scale_loop(float(kw["v"]))
        elif op == "track.loop.nudge":
            t = self._t(kw)
            if t:
                t.nudge_loop(int(kw["v"] * t.beat_frames(tr.bpm)))
        elif op == "track.loop.slice":
            t = self._t(kw)
            if t and t.slices.size:
                k = int(kw["v"]) % t.slices.size
                s = int(t.slices[k])
                e = int(t.slices[k + 1]) if k + 1 < t.slices.size else t.frames
                t.set_loop(s, e)
        elif op == "track.xfade":
            t = self._t(kw)
            if t:
                t.xfade = int(max(0.0, min(120.0, kw["v"])) * 0.001 * t.sr)
        elif op == "track.analysis":
            # Second-stage results. Deliberately touches nothing but the
            # display fields: analysis never moves a loop point, and the
            # track has been playable since the buffer arrived.
            t = self._t(kw)
            if t:
                t.bpm = kw.get("bpm", t.bpm)
                t.bpm_conf = kw.get("conf", t.bpm_conf)
                if kw.get("slices") is not None:
                    t.slices = kw["slices"]
                t.analysing = False
        elif op == "track.slices":
            t = self._t(kw)
            if t:
                t.slices = kw["slices"]

        elif op == "master.gain":
            self.master_gain = max(0.0, min(1.2, float(kw["v"])))
        elif op == "transport.start":
            tr.playing = True
            if self.recorder is not None:
                self.recorder.begin(self.xruns)     # an armed take starts with the clock
        elif op == "transport.stop":
            tr.playing = False
            self._cancel_launches()
            self._stop_sound()
        elif op == "transport.toggle":
            tr.playing = not tr.playing
            if not tr.playing:
                self._cancel_launches()
                self._stop_sound()
            elif self.recorder is not None:
                self.recorder.begin(self.xruns)
        elif op == "transport.rewind":
            tr.pos = 0
            for t in self.tracks:
                t.phase = float(t.loop_start)
        elif op == "transport.bpm":
            tr.set_bpm(kw["v"])
        elif op == "transport.quantum":
            tr.quantum_i = int(kw["v"]) % len(QUANTA)

        elif op == "pad.assign":
            # Settings only. Audio comes through pad.take.
            p = self.pads[int(kw["i"]) % len(self.pads)]
            for f in ("mode", "gain", "pan", "speed", "quantize", "label",
                      "reverse"):
                if f in kw:
                    setattr(p, f, kw[f])
        elif op == "pad.take":
            # Snapshot a track's CURRENT buffer and region onto a key. A
            # snapshot, not a link: later edits to the track must not reach
            # back and change the key.
            p = self.pads[int(kw["i"]) % len(self.pads)]
            t = self._t({"i": kw.get("track", -1)})
            if t is None or t.src is None:
                self.last_error = "Nothing to assign — that track is empty."
            elif self.would_exceed(t.buf):
                self.last_error = (
                    "Not enough room to pin %s to a key. %d MB of audio is "
                    "already held and the ceiling is %d MB. Eject a track or "
                    "clear a key, or raise it with --memory-mb."
                    % (t.name, self.audio_bytes() // 1048576,
                       self.memory_limit // 1048576))
            else:
                ls = int(kw.get("ls", t.loop_start))
                le = int(kw.get("le", t.loop_end))
                p.assign(t.buf, t.sr, t.name, t.path, ls, le,
                         track=t.index, slice_i=int(kw.get("slice", -1)))
                p.label = kw.get("label") or "%s" % t.name.split(".")[0][:12]
                p.source = t.mode        # a CTR or SIDE key holds that variant
                self.last_error = ""
        elif op == "pad.load":
            # A key's own audio, put back by a session. Not taken from a track:
            # the track it came from may hold something else by now.
            p = self.pads[int(kw["i"]) % len(self.pads)]
            buf = kw["buf"]
            if self.would_exceed(buf):
                self.last_error = (
                    "No room to put %s back on a key: %d MB of audio is held "
                    "against a %d MB ceiling." % (kw.get("name", "that file"),
                                                  self.audio_bytes() // 1048576,
                                                  self.memory_limit // 1048576))
            else:
                p.assign(buf, int(kw["sr"]), kw.get("name", ""), kw.get("path", ""),
                         int(kw.get("ls", 0)), int(kw.get("le", buf.shape[0])))
                p.source = kw.get("source", "STEREO")
                p.label = kw.get("label") or kw.get("name", "").split(".")[0][:12]
                self.last_error = ""
        elif op == "pad.loop":
            p = self.pads[int(kw["i"]) % len(self.pads)]
            p.set_loop(kw["ls"], kw["le"])
        elif op == "pad.clear":
            self.voices.kill_pad(int(kw["i"]))
            self.pads[int(kw["i"]) % len(self.pads)].clear()
        elif op in ("pad.trigger", "pad.trigger.q"):
            self._trigger_pad(int(kw["i"]))
        elif op == "pad.release":
            self.voices.release_pad(int(kw["i"]))
        elif op == "panic":
            # ESC: the immediate cut. No fade, by design — it is the control
            # for when something has to be silent this block, clicks and all.
            self.voices.panic()
            for t in self.tracks:
                t.playing = t.stopping = False
        elif op == "probe":
            # touched by the audio thread itself, so an id coming back in the
            # next snapshot proves the whole loop, not just the socket
            self.probe_id = int(kw.get("id", 0))
        elif op == "clip.reset":
            self.clips = 0
            self.xruns = 0

    def _trigger_pad(self, pi):
        """Plays the key's own audio. No track is consulted."""
        if not (0 <= pi < len(self.pads)):
            return
        p = self.pads[pi]
        if not p.loaded:
            return
        if p.mode == LOOP and self.voices.pad_active(pi):
            self.voices.kill_pad(pi)
            return
        step = p.speed * (p.sr / float(self.sr))
        if p.reverse:
            step = -step
        v = self.voices.alloc()
        v.start_note(pi, p.buf, p.sr, p.loop_start, p.loop_end,
                     step, p.gain, p.pan, p.mode, self.sr)

    # ==================================================================
    # telemetry
    # ==================================================================
    def audio_bytes(self):
        """Decoded audio held alive, counting each distinct buffer once.

        A buffer shared by a track and three keys costs what one costs. This
        is why the ceiling is checked against unique arrays rather than a sum
        over holders.
        """
        seen = {}
        for t in self.tracks:
            for b in t.variants.values():
                if b is not None:
                    seen[id(b)] = b.nbytes
        for p in self.pads:
            if p.buf is not None:
                seen[id(p.buf)] = p.buf.nbytes
        return sum(seen.values())

    def would_exceed(self, buf):
        """True if pinning `buf` would cross the ceiling and it is not already
        held. Refusing is better than an OOM the user cannot interpret."""
        if self.memory_limit <= 0:
            return False
        held = {}
        for t in self.tracks:
            for b in t.variants.values():
                if b is not None:
                    held[id(b)] = b.nbytes
        for p in self.pads:
            if p.buf is not None:
                held[id(p.buf)] = p.buf.nbytes
        if id(buf) in held:
            return False
        return sum(held.values()) + buf.nbytes > self.memory_limit

    def latency(self):
        """The press-to-speaker budget, stage by stage, in ms.

        Web Audio collapses this into baseLatency + outputLatency because the
        graph and the sound card are in the same process. Here a press crosses
        a socket and a thread boundary first, so those stages are separate and
        each one is measured where it actually happens:

            control   input event -> ws.send()      (browser, see the panel)
            wire      ws.send()   -> Engine.post()  (localhost socket)
            queue     post()      -> drained by the callback   <- measured here
            block     one callback period                       blocksize / sr
            output    callback    -> speaker        PortAudio's own figure
            quantum   0 .. one launch quantum       musical, not latency

        `queue` is the only one that varies with load, which is why it is the
        one kept as a rolling distribution rather than a constant.
        """
        block_ms = self.blocksize / float(self.sr) * 1000.0
        st = self.stream
        reported = (st.latency * 1000.0) if st else self.output_ms_reported
        measured = self.output_ms_measured
        out_ms = measured if measured is not None else reported
        v = self._lat[:self._lat_n] if self._lat_n else np.zeros(1, np.float32)
        q = {k: float(np.percentile(v, k)) for k in (50, 95, 99)}
        mean = float(v.mean())
        fixed = block_ms + out_ms
        return {
            "samplerate": self.sr,
            "blocksize": self.blocksize,
            "block_ms": round(block_ms, 3),

            # Named so the two can never be confused for one another.
            "output_ms_reported": round(reported, 3),
            "output_ms_measured": (None if measured is None
                                   else round(measured, 3)),
            "output_ms_source": ("measured by loopback" if measured is not None
                                 else "backend-reported: block arithmetic, "
                                      "NOT a measurement"),
            "output_ms": round(out_ms, 3),        # whichever is authoritative

            "queue_mean_ms": round(mean, 3),
            "queue_p50_ms": round(q[50], 3),
            "queue_p95_ms": round(q[95], 3),
            "queue_p99_ms": round(q[99], 3),
            "queue_max_ms": round(float(v.max()), 3),
            "samples": int(self._lat_n),

            # Two totals, each labelled. The single figure that used to sit
            # here was built on p95 and printed beside the mean, which reads
            # as a mean and gets repeated as one.
            "press_to_speaker_mean_ms": round(mean + fixed, 2),
            "press_to_speaker_p95_ms": round(q[95] + fixed, 2),

            "quantum_ms": round(
                self.transport.spb * self.transport.quantum_beats
                / self.sr * 1000.0, 1),
            "backend": "null" if self.offline else "portaudio",
        }

    def watch_gc(self):
        """Count garbage collections that run on the audio thread, and the
        longest. A collection holds the interpreter lock for its whole length
        wherever it runs; the ones on the callback's own thread are the ones
        this engine could have set off itself."""
        if self._gc_hook is None:
            self._gc_hook = _gc_watch(self)
            gc.callbacks.append(self._gc_hook)

    def unwatch_gc(self):
        if self._gc_hook is not None:
            try:
                gc.callbacks.remove(self._gc_hook)
            except ValueError:
                pass
            self._gc_hook = None

    def capture(self, seconds):
        """Start recording what the device consumes, into one preallocated buffer.

        Two traps, both hit while building this. The obvious tee — copy each
        block into a list — allocates about 2 kB per callback on the audio
        thread; alone it survives, but combined with decode work elsewhere it
        drags a garbage collection into the callback and produces exactly the
        underrun the capture was meant to measure (0 xruns with either alone
        over ~15000 blocks, 3 with both). And wrapping `self._callback` after
        start() does nothing at all, because PortAudio holds the bound method
        it was constructed with — the capture silently records zero frames.

        So the buffer is allocated once here, and the callback itself writes
        into a slice of it. Works whether the stream is running or not.
        """
        n = int(seconds * self.sr) + self.blocksize
        self._cap_buf = np.zeros((n, 2), dtype=np.float32)
        self._cap_w = 0
        return self._cap_buf

    def end_capture(self):
        """-> the frames actually written, or None if no capture was running."""
        buf, w = self._cap_buf, self._cap_w
        self._cap_buf = None
        self._cap_w = 0
        return None if buf is None else buf[:w]

    def xrun_report(self):
        """-> list of dicts, one per recorded xrun."""
        return [{"t_s": round(t, 4), "block": b, "underflow": u,
                 "overflow": o, "priming": p}
                for (t, b, u, o, p) in self._xrun_log]

    def _pending_pads(self):
        out = set()                       # telemetry thread; allocation is fine here
        ops, kws = self._q_op, self._q_kw
        for k in range(self._q_n):
            kw = kws[k]
            if ops[k] == "pad.trigger.q" and kw is not None:
                out.add(kw.get("i"))
        return out

    def scope(self, points=512):
        """Most recent `points` frames of master out, oldest first, int8."""
        w = self._scope_w
        buf = np.concatenate([self._scope[w:], self._scope[:w]])
        take = buf[-points * 4:]
        if take.shape[0] < points:
            points = take.shape[0]
        if points <= 0:
            return np.zeros(0, dtype=np.int8)
        hop = take.shape[0] // points
        v = take[:hop * points].reshape(points, hop, 2)
        # keep the peak of each hop so transients survive decimation
        pk = np.where(np.abs(v.max(axis=1)) >= np.abs(v.min(axis=1)),
                      v.max(axis=1), v.min(axis=1))
        return np.clip(pk * 127.0, -127, 127).astype(np.int8)

    def snapshot(self):
        tr = self.transport
        bar, beat = tr.bars_beats()
        st = self.stream
        return {
            "sr": self.sr,
            "blocksize": self.blocksize,
            "device": self.device_name,
            "native_rate": self.native_rate,
            "resampling": bool(self.native_rate
                               and int(self.native_rate) != int(self.sr)),
            "latency_ms": round(
                self.output_ms_measured if self.output_ms_measured is not None
                else ((st.latency * 1000.0) if st else self.output_ms_reported), 2),
            "latency_measured": self.output_ms_measured is not None,
            "queue_ms": round(float(self._lat[:self._lat_n].max())
                              if self._lat_n else 0.0, 2),
            "probe_id": self.probe_id,
            "cpu": round((st.cpu_load * 100.0) if st else 0.0, 1),
            "xruns": self.xruns,
            "clips": self.clips,
            "playing": tr.playing,
            "bpm": round(tr.bpm, 2),
            "bar": bar,
            "beat": beat,
            "pos": tr.pos,
            "quantum": QUANTUM_LABELS[tr.quantum_i],
            "quantum_i": tr.quantum_i,
            "pending": self._q_n,
            "queue_overflow": self.queue_overflow,
            "gc_audio": self.gc_audio,
            "gc_audio_max_ms": round(self.gc_audio_max_ms, 3),
            "pending_ms": round(self.transport.frames_to_boundary()
                                / self.sr * 1000.0, 0) if self._q_n else 0,
            "master": round(self.master_gain, 3),
            "mpeak": [round(float(self.master_peak[0]), 4),
                      round(float(self.master_peak[1]), 4)],
            "voices": self.voices.used(),
            "voices_max": len(self.voices.voices),
            "audio_mb": round(self.audio_bytes() / 1048576.0, 1),
            "audio_limit_mb": round(self.memory_limit / 1048576.0, 0),
            "error": self.last_error,
            "tracks": [t.snapshot() for t in self.tracks],
            "pads": [p.snapshot(i) for i, p in enumerate(self.pads)],
            "pads_on": [self.voices.pad_active(i) for i in range(len(self.pads))],
            "pads_pending": [i in self._pending_pads()
                             for i in range(len(self.pads))],
        }


class NullStream:
    """A sound card that isn't there. Runs the callback on a wall clock."""

    def __init__(self, engine):
        self.engine = engine
        self.latency = engine.blocksize / float(engine.sr)
        self.cpu_load = 0.0
        self._stop = threading.Event()
        self._buf = np.zeros((engine.blocksize, 2), dtype=np.float32)
        self._t = None

    def start(self):
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        e = self.engine
        period = e.blocksize / float(e.sr)
        nxt = time.monotonic()
        while not self._stop.is_set():
            t0 = time.monotonic()
            e._callback(self._buf, e.blocksize, None, None)
            self.cpu_load = (time.monotonic() - t0) / period
            nxt += period
            d = nxt - time.monotonic()
            if d > 0:
                time.sleep(d)
            else:
                nxt = time.monotonic()

    def stop(self):
        self._stop.set()
        if self._t:
            self._t.join(timeout=1.0)

    def close(self):
        self.stop()
