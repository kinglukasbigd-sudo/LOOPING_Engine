"""Master clock. Sample-counted, never wall-clock."""
from __future__ import annotations

import math

# Quantum menu, in beats. 0 = fire immediately.
QUANTA = [0.0, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0]
QUANTUM_LABELS = ["OFF", "1/16", "1/8", "BEAT", "1/2", "BAR", "2BAR", "4BAR"]


class Transport:
    __slots__ = ("sr", "bpm", "beats_per_bar", "pos", "playing", "quantum_i",
                 "_taps")

    def __init__(self, sr: int, bpm: float = 124.0):
        self.sr = sr
        self.bpm = bpm
        self.beats_per_bar = 4
        self.pos = 0            # samples since transport start
        self.playing = False
        self.quantum_i = 5      # BAR
        self._taps = []

    # -- geometry ----------------------------------------------------------
    @property
    def spb(self) -> float:
        """Samples per beat."""
        return 60.0 / self.bpm * self.sr

    @property
    def quantum_beats(self) -> float:
        return QUANTA[self.quantum_i]

    def bars_beats(self):
        b = self.pos / self.spb
        bar = int(b // self.beats_per_bar)
        beat = b - bar * self.beats_per_bar
        return bar + 1, beat + 1.0

    def frames_to_boundary(self) -> int:
        """0 when we are sitting on a quantum edge, else frames until the next.

        With quantum OFF every sample is an edge, so queued work fires at the
        top of the block.

        A stopped clock has no edges either. `pos` does not advance while
        stopped, so a queue waiting on a boundary here would wait for ever:
        the change never lands, the panel shows it queued indefinitely, and
        worse, it fires later when the transport restarts and overwrites
        whatever the player edited in the meantime. Quantising against a clock
        that is not running is meaningless, so every moment is an edge.
        """
        return self.frames_to_grid(self.quantum_beats)

    def frames_to_grid(self, beats: float) -> int:
        """The same question for any division of the beat, which is what a
        loop roll waits on: it has its own grid, not the launch quantum's."""
        if not self.playing or beats <= 0.0:
            return 0
        qs = self.spb * beats
        r = self.pos % qs
        if r < 1.0 or (qs - r) < 1.0:
            return 0
        return max(1, int(math.ceil(qs - r)))

    def advance(self, n: int):
        if self.playing:
            self.pos += n

    # -- controls ----------------------------------------------------------
    def set_bpm(self, bpm: float):
        self.bpm = max(40.0, min(220.0, float(bpm)))

    def tap(self, now: float):
        """Tap tempo. Two taps give a tempo; a >2s gap starts over."""
        if self._taps and now - self._taps[-1] > 2.0:
            self._taps = []
        self._taps.append(now)
        if len(self._taps) > 5:
            self._taps.pop(0)
        if len(self._taps) >= 2:
            span = self._taps[-1] - self._taps[0]
            if span > 0:
                bpm = 60.0 * (len(self._taps) - 1) / span
                while bpm < 70:
                    bpm *= 2
                while bpm > 180:
                    bpm /= 2
                self.set_bpm(bpm)
        return self.bpm
