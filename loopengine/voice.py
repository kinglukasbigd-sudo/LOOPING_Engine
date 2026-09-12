"""Pad voices. A small polyphonic pool so a retrigger overlaps its own tail."""
from __future__ import annotations

import numpy as np

from . import dsp

ONESHOT, GATE, LOOP = "ONE", "GATE", "LOOP"


class Voice:
    __slots__ = ("blocksize", "active", "pad", "buf", "sr", "start", "end",
                 "phase", "step", "gain", "gl", "gr", "mode", "held",
                 "rel_env", "rel_step", "_w", "age")

    def __init__(self, blocksize: int):
        self.blocksize = blocksize
        self.active = False
        self.pad = -1
        self.buf = None
        self.sr = 48000
        self.start = 0
        self.end = 0
        self.phase = 0.0
        self.step = 1.0
        self.gain = 1.0
        self.gl = self.gr = 0.7071
        self.mode = ONESHOT
        self.held = False
        self.rel_env = 1.0
        self.rel_step = 0.0
        self._w = None
        self.age = 0

    def start_note(self, pad, buf, sr, start, end, step, gain, pan, mode,
                   engine_sr):
        self.pad = pad
        self.buf = buf
        self.sr = sr
        self.start = int(start)
        self.end = int(end)
        self.phase = float(start)
        self.step = step
        self.gain = gain
        self.gl, self.gr = dsp.pan_gains(pan)
        self.mode = mode
        self.held = True
        self.rel_env = 1.0
        # 4 ms release. Long enough to kill the click, short enough to feel hard.
        self.rel_step = 1.0 / max(1.0, 0.004 * engine_sr)
        self.active = True
        self.age = 0
        ch = buf.shape[1]
        if self._w is None or self._w.ch != ch:
            self._w = dsp.Work(self.blocksize, ch)

    def release(self):
        self.held = False

    def render(self, out: np.ndarray, n: int, engine_sr: int):
        if not self.active:
            return 0.0
        buf = self.buf
        L = self.end - self.start
        if L < 32:
            self.active = False
            return 0.0

        w = self._w
        if self.mode == LOOP:
            y, _ = dsp.loop_gather(buf, self.start, L, self.phase, self.step, n, w)
            self.phase = self.start + ((self.phase - self.start + self.step * n) % L)
            done = False
        else:
            y = dsp.shot_gather(buf, self.start, self.phase, self.step, n, w)
            end_phase = self.phase + self.step * n
            done = end_phase >= self.end
            if done:
                # zero the part past the slice end
                over = int(max(0, np.ceil((end_phase - self.end) / self.step)))
                if 0 < over <= n:
                    y[n - over:] = 0.0
            self.phase = end_phase

        env = 1.0
        if not self.held and self.mode in (GATE, LOOP):
            e0 = self.rel_env
            self.rel_env = max(0.0, e0 - self.rel_step * n)
            ramp = w.k[:n] * (1.0 / n)
            env = (e0 + (self.rel_env - e0) * ramp).astype(np.float32)[:, None]
            if self.rel_env <= 0.0:
                self.active = False

        yg = y * (self.gain * env if isinstance(env, float) else env * self.gain)
        if buf.shape[1] == 1:
            out[:n, 0] += yg[:, 0] * self.gl
            out[:n, 1] += yg[:, 0] * self.gr
        else:
            out[:n, 0] += yg[:, 0] * self.gl
            out[:n, 1] += yg[:, 1] * self.gr

        if done and self.mode != LOOP:
            self.active = False
        self.age += n
        return float(np.abs(yg).max()) if n else 0.0


class VoicePool:
    def __init__(self, count: int, blocksize: int):
        self.voices = [Voice(blocksize) for _ in range(count)]

    def alloc(self) -> Voice:
        for v in self.voices:
            if not v.active:
                return v
        # steal the oldest — a DJ hitting 17 pads at once gets the newest 16
        return max(self.voices, key=lambda v: v.age)

    def release_pad(self, pad: int):
        for v in self.voices:
            if v.active and v.pad == pad:
                v.release()

    def kill_pad(self, pad: int):
        for v in self.voices:
            if v.active and v.pad == pad:
                v.release()

    def panic(self):
        for v in self.voices:
            v.active = False
            v.held = False

    def pad_active(self, pad: int) -> bool:
        return any(v.active and v.pad == pad for v in self.voices)

    def used(self) -> int:
        return sum(1 for v in self.voices if v.active)
