"""One looping track. Eight of these run at once."""
from __future__ import annotations

import numpy as np

from . import dsp

MODES = ["STEREO", "CTR", "SIDE"]


class Track:
    """State + render. All positions are in *source* samples, float64 phase.

    The file's own sample rate is folded into the playback step, so no file is
    ever resampled on load — a 44.1k loop on a 48k device just plays at
    step 0.91875 through the same interpolator that does varispeed.
    """

    __slots__ = (
        "index", "blocksize", "name", "path", "sr", "frames", "channels",
        "src", "variants", "mode", "loop_start", "loop_end", "phase",
        "playing", "reverse", "mute", "solo", "gain", "pan", "speed",
        "xfade", "slices", "bpm", "bpm_conf", "peak", "rms", "_g", "_gl",
        "_gr", "_w", "fired", "queued",
    )

    def __init__(self, index: int, blocksize: int):
        self.index = index
        self.blocksize = blocksize
        self.name = ""
        self.path = ""
        self.sr = 48000
        self.frames = 0
        self.channels = 0
        self.src = None            # np.float32 (frames, channels)
        self.variants = {}         # mode -> ndarray, filled lazily off-thread
        self.mode = "STEREO"
        self.loop_start = 0
        self.loop_end = 0
        self.phase = 0.0
        self.playing = False
        self.reverse = False
        self.mute = False
        self.solo = False
        self.gain = 0.65
        self.pan = 0.0
        self.speed = 1.0           # 1.0 = file's own pitch
        self.xfade = 0             # samples, set from ms on load
        self.slices = np.zeros(0, dtype=np.int64)
        self.bpm = 0.0
        self.bpm_conf = 0.0
        self.peak = 0.0
        self.rms = 0.0
        self._g = 0.0              # smoothed gain, avoids zipper noise
        self._gl, self._gr = dsp.pan_gains(0.0)
        self._w = None
        self.fired = 0             # bumped when a queued change lands
        self.queued = None         # human-readable label of what's pending

    # -- loading -----------------------------------------------------------
    def load(self, buf: np.ndarray, sr: int, name: str, path: str,
             bpm: float, conf: float, slices: np.ndarray, xfade_ms: float = 6.0):
        self.src = buf
        self.variants = {"STEREO": buf}
        self.mode = "STEREO"
        self.sr = sr
        self.frames = buf.shape[0]
        self.channels = buf.shape[1]
        self.name = name
        self.path = path
        self.bpm = bpm
        self.bpm_conf = conf
        self.slices = slices
        self.loop_start = 0
        self.loop_end = self.frames
        self.phase = 0.0
        self.xfade = int(xfade_ms * 0.001 * sr)
        self._w = dsp.Work(self.blocksize, self.channels)

    def clear(self):
        self.playing = False
        self.src = None
        self.variants = {}
        self.name = ""
        self.path = ""
        self.frames = 0
        self.loop_start = self.loop_end = 0
        self.peak = self.rms = 0.0
        self.queued = None

    @property
    def buf(self):
        return self.variants.get(self.mode, self.src)

    @property
    def loop_len(self) -> int:
        return max(0, self.loop_end - self.loop_start)

    # -- render ------------------------------------------------------------
    def render(self, out: np.ndarray, n: int, engine_sr: int, any_solo: bool):
        target = 0.0 if (self.mute or (any_solo and not self.solo)) else self.gain
        buf = self.buf
        if buf is None or not self.playing:
            # keep the smoother running so an unmute mid-fade still lands clean
            self._g += (0.0 - self._g) * 0.35
            self.peak *= 0.55
            self.rms *= 0.55
            return

        L = self.loop_len
        if L < 64:
            return

        step = self.speed * (self.sr / float(engine_sr))
        if self.reverse:
            step = -step

        w = self._w
        y, rel = dsp.loop_gather(buf, self.loop_start, L, self.phase, step, n, w)
        xf = min(self.xfade, L // 4, max(0, self.frames - self.loop_end))
        if xf > 0 and not self.reverse:
            y = dsp.seam_blend(buf, y, rel, self.loop_start, L, xf, self.frames, w)

        # Linear gain ramp across the block. 6ms-ish to target, no clicks.
        g0 = self._g
        self._g = g0 + (target - g0) * min(1.0, n / (0.006 * engine_sr))
        ramp = w.k[:n] * (1.0 / n)
        g = (g0 + (self._g - g0) * ramp).astype(np.float32)[:, None]

        yg = y * g
        if self.channels == 1:
            out[:n, 0] += yg[:, 0] * self._gl
            out[:n, 1] += yg[:, 0] * self._gr
        else:
            out[:n, 0] += yg[:, 0] * self._gl
            out[:n, 1] += yg[:, 1] * self._gr

        pk = float(np.abs(yg).max()) if n else 0.0
        self.peak = pk if pk > self.peak else self.peak * 0.72
        self.rms = float(np.sqrt(np.mean(yg * yg))) if n else 0.0

        self.phase = self.loop_start + (
            (self.phase - self.loop_start + step * n) % L)

    # -- edits -------------------------------------------------------------
    def set_pan(self, pan: float):
        self.pan = max(-1.0, min(1.0, float(pan)))
        self._gl, self._gr = dsp.pan_gains(self.pan)

    def set_loop(self, start: int, end: int):
        start = max(0, min(int(start), self.frames - 64))
        end = max(start + 64, min(int(end), self.frames))
        self.loop_start, self.loop_end = start, end
        if not (start <= self.phase < end):
            self.phase = float(start)

    def scale_loop(self, factor: float):
        """Halve or double the loop, anchored at the start."""
        L = max(64, int(round(self.loop_len * factor)))
        self.set_loop(self.loop_start, self.loop_start + L)

    def nudge_loop(self, frames: int):
        """Slide the whole loop window without changing its length."""
        L = self.loop_len
        s = max(0, min(self.frames - L, self.loop_start + int(frames)))
        self.loop_start, self.loop_end = s, s + L

    def beat_frames(self, transport_bpm: float) -> float:
        """Length of one beat in this file's samples, at the file's own bpm."""
        bpm = self.bpm if self.bpm > 0 else transport_bpm
        return 60.0 / bpm * self.sr

    def snapshot(self):
        return {
            "i": self.index,
            "name": self.name,
            "path": self.path,
            "loaded": self.src is not None,
            "playing": self.playing,
            "mute": self.mute,
            "solo": self.solo,
            "rev": self.reverse,
            "gain": round(self.gain, 3),
            "pan": round(self.pan, 3),
            "speed": round(self.speed, 4),
            "mode": self.mode,
            "sr": self.sr,
            "ch": self.channels,
            "frames": self.frames,
            "ls": self.loop_start,
            "le": self.loop_end,
            "phase": int(self.phase),
            "bpm": self.bpm,
            "conf": round(self.bpm_conf, 2),
            "xfade": self.xfade,
            "slices": int(self.slices.size),
            "peak": round(min(1.5, self.peak), 4),
            "rms": round(min(1.5, self.rms), 4),
            "queued": self.queued,
            "fired": self.fired,
        }
