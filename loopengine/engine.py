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
import threading
import time

import numpy as np

from .track import Track
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


class Pad:
    __slots__ = ("track", "slice", "mode", "gain", "pan", "speed", "quantize",
                 "label")

    def __init__(self):
        self.track = -1
        self.slice = -1
        self.mode = ONESHOT
        self.gain = 1.0
        self.pan = 0.0
        self.speed = 1.0
        self.quantize = False
        self.label = ""

    def snapshot(self, i):
        return {"i": i, "track": self.track, "slice": self.slice,
                "mode": self.mode, "gain": round(self.gain, 3),
                "pan": round(self.pan, 3), "q": self.quantize,
                "label": self.label}


class Engine:
    def __init__(self, samplerate=48000, blocksize=256, device=None,
                 n_tracks=8, n_voices=16, n_pads=16, offline=False):
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
        else:
            import sounddevice as sd
            self.sd = sd
            info = sd.query_devices(device, "output") if device is not None \
                else sd.query_devices(sd.default.device[1], "output")
            self.device_name = info["name"]
            if samplerate is None:
                samplerate = int(info["default_samplerate"])
            self.sr = int(samplerate)

        self.transport = Transport(self.sr, 124.0)
        self.tracks = [Track(i, blocksize) for i in range(n_tracks)]
        self.pads = [Pad() for _ in range(n_pads)]
        self.voices = VoicePool(n_voices, blocksize)

        self.master_gain = 0.70
        self._mg = 0.70
        self.cmds = collections.deque()
        self._pending = []
        self.pending_labels = []

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
        self.last_error = ""
        self.stream = None

    # ==================================================================
    # lifecycle
    # ==================================================================
    def start(self):
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
        if status:
            self.xruns += 1
        self.blocks += 1

        self._drain()

        mix = self._mix[:frames]
        mix[:] = 0.0

        off = 0
        guard = 0
        while off < frames and guard < 64:
            guard += 1
            n = frames - off
            if self._pending:
                d = self.transport.frames_to_boundary()
                if d == 0:
                    self._fire()
                    d = self.transport.frames_to_boundary()
                if self._pending and d > 0:
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
        any_solo = any(t.solo for t in self.tracks)
        for t in self.tracks:
            t.render(out, n, self.sr, any_solo)
        for v in self.voices.voices:
            if v.active:
                v.render(out, n, self.sr)

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
                self._pending.append((op, kw))
                self._label_pending(op, kw)
            else:
                self._apply(op, kw)

    def _label_pending(self, op, kw):
        i = kw.get("i")
        if op.startswith("track.") and isinstance(i, int) and 0 <= i < len(self.tracks):
            self.tracks[i].queued = {
                "track.play": "START", "track.stop": "STOP",
                "track.toggle": "TOGGLE", "track.rev": "REV",
                "track.retrig": "RETRIG", "track.loop": "LOOP",
                "track.loop.scale": "LOOP", "track.loop.nudge": "LOOP",
                "track.loop.slice": "LOOP"}.get(op, op)
        self.pending_labels = [o for o, _ in self._pending]

    def _fire(self):
        pend, self._pending = self._pending, []
        for op, kw in pend:
            self._apply(op, kw)
        for t in self.tracks:
            if t.queued is not None:
                t.queued = None
                t.fired += 1
        self.pending_labels = []

    def _t(self, kw):
        i = kw.get("i", -1)
        if isinstance(i, int) and 0 <= i < len(self.tracks):
            return self.tracks[i]
        return None

    def _apply(self, op, kw):
        tr = self.transport

        if op == "track.load":
            t = self._t(kw)
            if t:
                t.load(kw["buf"], kw["sr"], kw["name"], kw["path"], kw["bpm"],
                       kw["conf"], kw["slices"], kw.get("xfade_ms", 6.0))
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
                t.playing = True
                if kw.get("retrig", True):
                    t.phase = float(t.loop_start)
        elif op == "track.stop":
            t = self._t(kw)
            if t:
                t.playing = False
        elif op == "track.toggle":
            t = self._t(kw)
            if t and t.src is not None:
                t.playing = not t.playing
                if t.playing:
                    t.phase = float(t.loop_start)
        elif op == "track.retrig":
            t = self._t(kw)
            if t:
                t.phase = float(t.loop_start)
        elif op == "track.rev":
            t = self._t(kw)
            if t:
                t.reverse = not t.reverse
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
        elif op == "track.slices":
            t = self._t(kw)
            if t:
                t.slices = kw["slices"]

        elif op == "master.gain":
            self.master_gain = max(0.0, min(1.2, float(kw["v"])))
        elif op == "transport.start":
            tr.playing = True
        elif op == "transport.stop":
            tr.playing = False
        elif op == "transport.toggle":
            tr.playing = not tr.playing
        elif op == "transport.rewind":
            tr.pos = 0
            for t in self.tracks:
                t.phase = float(t.loop_start)
        elif op == "transport.bpm":
            tr.set_bpm(kw["v"])
        elif op == "transport.quantum":
            tr.quantum_i = int(kw["v"]) % len(QUANTA)

        elif op == "pad.assign":
            p = self.pads[int(kw["i"]) % len(self.pads)]
            for f in ("track", "slice", "mode", "gain", "pan", "speed",
                      "quantize", "label"):
                if f in kw:
                    setattr(p, f, kw[f])
        elif op in ("pad.trigger", "pad.trigger.q"):
            self._trigger_pad(int(kw["i"]))
        elif op == "pad.release":
            self.voices.release_pad(int(kw["i"]))
        elif op == "panic":
            self.voices.panic()
            for t in self.tracks:
                t.playing = False
        elif op == "probe":
            # touched by the audio thread itself, so an id coming back in the
            # next snapshot proves the whole loop, not just the socket
            self.probe_id = int(kw.get("id", 0))
        elif op == "clip.reset":
            self.clips = 0
            self.xruns = 0

    def _trigger_pad(self, pi):
        if not (0 <= pi < len(self.pads)):
            return
        p = self.pads[pi]
        if not (0 <= p.track < len(self.tracks)):
            return
        t = self.tracks[p.track]
        buf = t.buf
        if buf is None:
            return
        if p.mode == LOOP and self.voices.pad_active(pi):
            self.voices.kill_pad(pi)
            return
        if p.slice < 0 or t.slices.size == 0:
            s, e = t.loop_start, t.loop_end
        else:
            k = p.slice % t.slices.size
            s = int(t.slices[k])
            e = int(t.slices[k + 1]) if k + 1 < t.slices.size else t.frames
        v = self.voices.alloc()
        v.start_note(pi, buf, t.sr, s, e,
                     p.speed * (t.sr / float(self.sr)), p.gain, p.pan,
                     p.mode, self.sr)

    # ==================================================================
    # telemetry
    # ==================================================================
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

    def _pending_pads(self):
        return {kw.get("i") for op, kw in self._pending if op == "pad.trigger.q"}

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
            "pending": len(self._pending),
            "master": round(self.master_gain, 3),
            "mpeak": [round(float(self.master_peak[0]), 4),
                      round(float(self.master_peak[1]), 4)],
            "voices": self.voices.used(),
            "voices_max": len(self.voices.voices),
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
