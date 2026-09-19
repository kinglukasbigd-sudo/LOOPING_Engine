"""One looping track. Eight of these run at once."""
from __future__ import annotations

import numpy as np

from . import dsp

MODES = ["STEREO", "CTR", "SIDE"]

# A loop shorter than this is not a loop, it is a click generator. Above it,
# regions shorter than one callback block are allowed and wrap several times
# per block — the modulo gather handles that, and it is a usable effect.
MIN_LOOP = 64
CUES = 8          # hot cues per track


# A stop halts once the smoothed gain is below this: -80 dB.
STOP_FLOOR = 1e-4


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
        "_gr", "_w", "fired", "queued", "analysing", "stopping",
        "roll_beats", "roll_armed", "roll", "roll_start", "roll_shadow",
        "_rfade", "_rxf", "_rtail", "cues",
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
        self.stopping = False      # fading to silence; halts when the fade lands
        self._gl, self._gr = dsp.pan_gains(0.0)
        self._w = None
        self.fired = 0             # bumped when a queued change lands
        self.queued = None         # human-readable label of what's pending
        self.analysing = False     # buffer is in and playable; bpm/slices pending
        # Loop Roll. Temporary state, never an edit: `roll` is the window
        # length in this file's frames while one is running, `roll_start` where
        # that window begins, and `roll_shadow` where the phase would have been
        # all along — which is where the release puts it back. The loop points
        # are not touched by any of it.
        # Eight hot cues, in source samples. -1 is an empty one. Aiming
        # points, like the slice marks: setting one never moves a loop point.
        self.cues = np.full(CUES, -1, dtype=np.int64)
        self.roll_beats = 0.0      # length asked for, in beats of the clock
        self.roll_armed = False    # waiting for its own grid line
        self.roll = 0
        self.roll_start = 0
        self.roll_shadow = 0.0
        self._rfade = 0            # frames of release tail still to fade
        self._rxf = 0
        self._rtail = 0.0

    # -- loading -----------------------------------------------------------
    def load(self, buf: np.ndarray, sr: int, name: str, path: str,
             bpm: float, conf: float, slices: np.ndarray, xfade_ms: float = 6.0,
             analysing: bool = False):
        self.src = buf
        self.variants = {"STEREO": buf}
        self.stopping = False
        self.mode = "STEREO"
        self.sr = sr
        self.frames = buf.shape[0]
        self.channels = buf.shape[1]
        self.name = name
        self.path = path
        self.bpm = bpm
        self.bpm_conf = conf
        self.slices = slices
        self.analysing = analysing
        self.loop_start = 0
        self.loop_end = self.frames
        self.phase = 0.0
        self.xfade = int(xfade_ms * 0.001 * sr)
        self.cues[:] = -1              # a new file has nobody's cues on it
        self.roll_cancel()
        self._w = dsp.Work(self.blocksize, self.channels)

    def clear(self):
        self.playing = False
        self.cues[:] = -1
        self.roll_cancel()
        self.src = None
        self.variants = {}
        self.stopping = False
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
        target = 0.0 if (self.stopping or self.mute or (any_solo and not self.solo)) \
            else self.gain
        buf = self.buf
        if buf is None or not self.playing:
            # keep the smoother running so an unmute mid-fade still lands clean
            self._g += (0.0 - self._g) * 0.35
            self.peak *= 0.55
            self.rms *= 0.55
            return

        L = self.loop_len
        if L < MIN_LOOP:
            if self.stopping:                # nothing to fade: just halt
                self.playing = self.stopping = False
            return

        step = self.speed * (self.sr / float(engine_sr))
        if self.reverse:
            step = -step

        w = self._w
        # A roll plays a short window of the material just heard. Same gather,
        # same seam crossfade — the window is the only thing that differs, and
        # the loop points behind it are untouched.
        if self.roll > 0:
            rs = self.roll_start
            RL = self.roll
        else:
            rs = self.loop_start
            RL = L
        y, rel = dsp.loop_gather(buf, rs, RL, self.phase, step, n, w)
        xf = min(self.xfade, RL // 4, max(0, self.frames - (rs + RL)))
        if xf > 0 and not self.reverse:
            y = dsp.seam_blend(buf, y, rel, rs, RL, xf, self.frames, w)
        if self._rfade > 0:
            y = self._roll_tail(buf, y, n, step)

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

        self.phase = rs + ((self.phase - rs + step * n) % RL)
        if self.roll > 0:
            # what the phase would be doing if nobody had pressed anything
            self.roll_shadow = self.loop_start + (
                (self.roll_shadow - self.loop_start + step * n) % L)

        # A stop is a fade, not a cut. Setting playing False mid-waveform stepped
        # the output 26x the largest step in the audio itself — a click on every
        # stop. So a stopping track keeps rendering toward a gain of zero through
        # the same smoother mute uses, and halts only once it has landed there.
        if self.stopping and self._g < STOP_FLOOR:
            self.playing = self.stopping = False
            self._g = 0.0
            self.roll_cancel()

    def _roll_tail(self, buf, y, n, step):
        """The roll's own continuation, fading out under the resumed stream.

        The same equal-power shape the loop seam uses, and the same reasoning:
        for the first few milliseconds after the jump, mix in the material that
        would have played had the roll kept running. Past the fade the output
        is the resumed stream and nothing else, sample for sample.
        """
        xf = self._rxf
        k = n if self._rfade > n else self._rfade
        w = self._w
        pos = w.k[:k] * step + self._rtail
        idx = pos.astype(np.int64)
        np.clip(idx, 0, self.frames - 1, out=idx)
        cont = buf[idx]
        ph = ((w.k[:k] + (xf - self._rfade)) * (1.0 / xf)).astype(np.float32)[:, None]
        ph *= np.pi / 2.0
        y[:k] = y[:k] * np.sin(ph) + cont * np.cos(ph)
        self._rtail += step * k
        self._rfade -= k
        return y

    # -- loop roll ---------------------------------------------------------
    def roll_arm(self, beats: float) -> bool:
        """Ask for a roll. It starts on the next line of its own grid."""
        if self.src is None or self.loop_len < MIN_LOOP:
            return False
        self.roll_beats = max(0.0, float(beats))
        self.roll_armed = self.roll_beats > 0.0
        return self.roll_armed

    def roll_begin(self, spb: float, engine_sr: int):
        """On the line: loop the roll length just played, and start a shadow
        phase that goes on exactly as if none of this were happening."""
        self.roll_armed = False
        L = self.loop_len
        if self.src is None or not self.playing or L < MIN_LOOP:
            self.roll_beats = 0.0
            return
        step = abs(self.speed * (self.sr / float(engine_sr)))
        rf = int(round(self.roll_beats * spb * step))
        rf = max(MIN_LOOP, min(rf, L))
        u = (self.phase - self.loop_start) % L
        self.roll_shadow = self.phase
        self.roll_start = int(self.loop_start + ((u - rf) % L))
        self.roll = rf

    def roll_release(self):
        """Back to where playback had got to, now, through the seam fade."""
        self.roll_armed = False
        self.roll_beats = 0.0
        if self.roll <= 0:
            return
        xf = min(self.xfade, self.roll // 4)
        if xf > 0 and not self.reverse:
            self._rxf = xf
            self._rfade = xf
            self._rtail = self.phase
        self.phase = self.roll_shadow
        self.roll = 0

    def roll_cancel(self):
        """Stop or panic. The roll simply is not, and leaves no tail behind."""
        if self.roll > 0:
            self.phase = self.roll_shadow
        self.roll = 0
        self.roll_armed = False
        self.roll_beats = 0.0
        self._rfade = 0

    # -- edits -------------------------------------------------------------
    def set_pan(self, pan: float):
        self.pan = max(-1.0, min(1.0, float(pan)))
        self._gl, self._gr = dsp.pan_gains(self.pan)

    def set_loop(self, start: int, end: int):
        """Set the region, defining every degenerate case rather than trusting
        the caller.

          reversed or empty (end <= start)  -> a MIN_LOOP region at start
          shorter than MIN_LOOP             -> widened to MIN_LOOP
          shorter than one block            -> kept; it wraps several times
                                               per block, which is a real use
          past the end of the file          -> clamped, start pulled back
          playhead outside the new region   -> wrapped into it, not snapped to
                                               the start: wrapping is what the
                                               render loop does on every pass,
                                               so it stays continuous instead
                                               of jumping

        The crossfade is not adjusted here; render() already takes the
        smallest of the requested fade, a quarter of the region, and whatever
        material exists past the end.
        """
        if self.frames < MIN_LOOP:
            self.loop_start, self.loop_end = 0, self.frames
            return
        start = int(start)
        end = int(end)
        if end <= start:
            end = start + MIN_LOOP
        start = max(0, min(start, self.frames - MIN_LOOP))
        end = max(start + MIN_LOOP, min(end, self.frames))
        if end - start < MIN_LOOP:                      # ran out of file
            start = max(0, end - MIN_LOOP)
        self.loop_start, self.loop_end = start, end

        L = end - start
        if self.roll > 0:
            # the roll keeps its window; what wraps is the playback it stands in for
            if not (start <= self.roll_shadow < end):
                self.roll_shadow = start + ((self.roll_shadow - start) % L)
        elif not (start <= self.phase < end):
            self.phase = start + ((self.phase - start) % L)

    def scale_loop(self, factor: float):
        """Halve or double the loop, anchored at the start."""
        L = max(MIN_LOOP, int(round(self.loop_len * factor)))
        self.set_loop(self.loop_start, self.loop_start + L)

    # -- hot cues ----------------------------------------------------------
    def cue_set(self, c: int):
        """Where the playhead is now. While rolling, where playback really is."""
        if self.src is None or not 0 <= c < CUES:
            return
        p = self.roll_shadow if self.roll > 0 else self.phase
        self.cues[c] = int(max(0, min(self.frames - 1, int(p))))

    def cue_clear(self, c: int):
        if 0 <= c < CUES:
            self.cues[c] = -1

    def cue_jump(self, c: int):
        """Inside the region, the playhead moves. Outside it, the region moves.

        A cue past the end of the loop is a cue into another part of the file,
        and jumping the playhead there would put it somewhere the loop is about
        to wrap away from. So the window slides to start on the cue and keeps
        its length — the same operation as a beat jump — and playback starts at
        its head.
        """
        if self.src is None or not 0 <= c < CUES:
            return
        p = int(self.cues[c])
        if p < 0:
            return
        if self.loop_start <= p < self.loop_end:
            self.phase = float(p)
        else:
            self.nudge_loop(p - self.loop_start)
            self.phase = float(self.loop_start)
        if self.roll > 0:
            self.roll_shadow = self.phase      # the roll ends where the cue is

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
            "roll": round(self.roll_beats, 4) if (self.roll or self.roll_armed) else 0,
            "cues": [int(x) for x in self.cues],
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
            "analysing": self.analysing,
        }
