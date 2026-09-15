"""Recording the master output: the callback fills a ring, a thread writes it.

The callback must not touch the filesystem or allocate, so all it does is copy
each block into a ring allocated with the recorder and advance a count, the way
capture() and the scope ring already work. A writer thread drains the ring to a
WAV file at the engine's own rate, with no resampling, well away from it.

What is written is what the device was handed: after the master gain, the soft
clip and the hard clip at ±1. The file is 24-bit PCM. Float would keep nothing a
listener heard that 24 bits drops, and it reaches WAV's 4 GiB ceiling in under
three hours of stereo at 48 kHz, where 24-bit lasts nearly four. A take that does
reach it carries on in a second file, sample-continuous with the first
(-part2.wav), so nothing is lost at the seam.

Frames are counted, not timed. Elapsed is frames taken over the rate — the length
of the take on disk, not a wall clock that drifts from it. If the writer falls a
whole ring behind, a disk that stops answering for longer than the ring holds,
the frames it could not keep become silence of the same length, counted, with
where they fell. A take is never shortened, and never spliced unannounced.

One feeder, one drainer. Only the callback advances `fed`; only the writer
advances `drained`. Each reads the other's count whole under the GIL, so neither
needs a lock and the callback never waits on one. Where a take ends is the
callback's call too: asked to stop, it names the last frame it copied on its
next block, so a block half-copied at that moment is never cut off.
"""
from __future__ import annotations

import os
import threading
import time

import numpy as np
import soundfile as sf

IDLE, ARMED, RECORDING, STOPPING = "idle", "armed", "recording", "stopping"

# libsndfile's command to rewrite a header over what has been written so far.
# soundfile does not name it; this is its value in sndfile.h.
SFC_UPDATE_HEADER_NOW = 0x1060

BYTES_PER_SAMPLE = {"PCM_16": 2, "PCM_24": 3, "PCM_32": 4, "FLOAT": 4, "DOUBLE": 8}
WAV_DATA_MAX = 4_000_000_000        # under 2**32, with room for the chunks around the data


def part_path(path, n):
    """take.wav, take-part2.wav, take-part3.wav …"""
    if n <= 1:
        return path
    stem, ext = os.path.splitext(path)
    return "%s-part%d%s" % (stem, n, ext)


def clock(seconds):
    s = int(max(0, seconds))
    return "%d:%02d:%02d" % (s // 3600, s // 60 % 60, s % 60)


class Recorder:
    def __init__(self, samplerate, channels=2, ring_seconds=30.0, subtype="PCM_24",
                 chunk=16384, drain_every=0.05, header_every=2.0, ack_timeout=0.5,
                 part_frames=None, on_end=None):
        self.sr = int(samplerate)
        self.channels = int(channels)
        self.cap = max(1, int(ring_seconds * self.sr))
        self.ring = np.zeros((self.cap, self.channels), dtype=np.float32)
        self._wbuf = np.zeros((max(1, min(int(chunk), self.cap)), self.channels),
                              dtype=np.float32)
        self.subtype = subtype
        self.drain_every = drain_every
        self.header_every = header_every
        self.ack_timeout = ack_timeout
        self.part_frames = int(part_frames or WAV_DATA_MAX
                               // (BYTES_PER_SAMPLE[subtype] * self.channels))
        self.on_end = on_end        # called on the writer thread with summary()
        self.state = IDLE
        self._wake = threading.Event()
        self._thread = None
        self._file = None
        self._reset("")

    def _reset(self, path):
        self.path = path            # the take's first file
        self.paths = []             # every file it has written, in order
        self.fed = 0                # frames the callback has copied in; only it writes this
        self.drained = 0            # frames the writer has taken out; only it writes this
        self.written = 0            # frames in the files, silence for lost frames included
        self.final = -1             # where the callback says the take ends; -1 until it has
        self.dropped = 0
        self.gaps = []              # (frame in the take, frames lost)
        self.lag_max = 0            # the furthest the writer has been behind, in frames
        self.xruns_at_begin = 0
        self.error = ""
        self._part_written = 0
        self._stop_asked = 0.0

    # -- control: any thread but the audio one -----------------------------------
    def arm(self, path):
        """Open the take's file and start its writer. Nothing is taken until
        begin() — which the transport's start calls, or a press while it runs."""
        if self.state != IDLE:
            raise RuntimeError("a take is already %s" % self.state)
        self._reset(os.path.abspath(path))
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._open(1)
        self._wake.clear()
        self.state = ARMED
        self._thread = threading.Thread(target=self._write_loop, name="recorder",
                                        daemon=True)
        self._thread.start()

    def begin(self, xruns=0):
        """Armed to recording, from the next block copied. Also called on the
        audio thread, by the transport's start: a comparison and two attributes."""
        if self.state == ARMED:
            self.xruns_at_begin = xruns
            self.state = RECORDING

    def request_stop(self):
        """End the take, or call off an armed one. Returns at once."""
        if self.state in (ARMED, RECORDING):
            self._stop_asked = time.monotonic()
            self.state = STOPPING
            self._wake.set()

    def finish(self, timeout=None):
        """Wait for the writer to drain and close. -> summary(), or None if it
        is still writing when the timeout runs out."""
        t = self._thread
        if t is not None:
            t.join(timeout)
            if t.is_alive():
                return None
        self._thread = None
        return self.summary()

    def summary(self):
        return {"path": self.path, "files": list(self.paths), "frames": self.written,
                "seconds": round(self.written / float(self.sr), 3),
                "dropped": self.dropped, "gaps": list(self.gaps[:32]),
                "error": self.error}

    def report(self):
        """For telemetry, 30 times a second: small, and cheap to build."""
        st = self.state
        frames = self.fed if st in (RECORDING, STOPPING) else self.written
        out = {"state": st, "elapsed_s": round(frames / float(self.sr), 2),
               "name": os.path.basename(self.paths[-1]) if self.paths else "",
               "parts": len(self.paths), "dropped": self.dropped,
               "lag_ms": round(self.lag_max * 1000.0 / self.sr, 1),
               "error": self.error, "free_s": None}
        if st != IDLE and self.path:
            try:
                v = os.statvfs(os.path.dirname(self.path))
                out["free_s"] = int(v.f_bavail * v.f_frsize / float(
                    BYTES_PER_SAMPLE[self.subtype] * self.channels * self.sr))
            except OSError:
                pass
        return out

    # -- the audio thread ------------------------------------------------------------
    def feed(self, block):
        """Copy one block into the ring. Slices of arrays that already exist, an
        integer and a comparison: no container is built and no file is touched."""
        st = self.state
        if st != RECORDING:
            if st == STOPPING and self.final < 0:
                self.final = self.fed        # the take ends after the last block copied
            return
        n = block.shape[0]
        fed = self.fed
        pos = fed % self.cap
        end = pos + n
        if end <= self.cap:
            self.ring[pos:end] = block
        else:
            first = self.cap - pos
            self.ring[pos:] = block[:first]
            self.ring[:end - self.cap] = block[first:]
        self.fed = fed + n

    # -- the writer thread -----------------------------------------------------------
    def _write_loop(self):
        last_header = time.monotonic()
        try:
            while True:
                if self.state == STOPPING:
                    end = self.final
                    if end < 0 and time.monotonic() - self._stop_asked > self.ack_timeout:
                        end = self.fed           # no callback came to name it: no stream running
                    if end >= 0:
                        self._drain(end)
                        break
                    # The callback names the end within a block, so poll for it.
                    # Waiting on the wake event here could sleep a whole
                    # drain_every: the stop that set it has already been seen.
                    time.sleep(0.002)
                    continue
                self._drain(self.fed)
                now = time.monotonic()
                if now - last_header >= self.header_every:
                    self._update_header()
                    last_header = now
                if self._wake.wait(self.drain_every):
                    self._wake.clear()
        except Exception as e:                    # a full disk, a pulled drive
            self.error = ("The recording stopped writing at %s — %s. Everything before "
                          "that is in %s." % (clock(self.written / float(self.sr)),
                                              getattr(e, "strerror", None) or e,
                                              os.path.basename(self.paths[-1])
                                              if self.paths else "the file"))
        finally:
            try:
                self._close()
            except Exception as e:
                self.error = self.error or "The take's file could not be closed — %s." % e
            if self.written == 0 and not self.error:
                for p in self.paths:              # armed and never begun: no empty file left
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
                self.paths = []
            self.state = IDLE
            cb = self.on_end
            if cb is not None:
                try:
                    cb(self.summary())
                except Exception:
                    pass

    def _drain(self, upto):
        got = self.drained
        if self.fed - got > self.lag_max:
            self.lag_max = self.fed - got
        while got < upto:
            avail = upto - got
            if avail > self.cap:                  # overwritten before it could be read
                lost = avail - self.cap
                self._silence(lost)
                got += lost
                self.drained = got
                continue
            n = min(avail, self._wbuf.shape[0])
            w = self._wbuf[:n]
            pos = got % self.cap
            end = pos + n
            if end <= self.cap:
                w[:] = self.ring[pos:end]
            else:
                first = self.cap - pos
                w[:first] = self.ring[pos:]
                w[first:] = self.ring[:end - self.cap]
            torn = self.fed - self.cap - got      # the callback lapped the copy as it ran
            if torn > 0:
                torn = min(torn, n)
                self._gap(torn)
                w[:torn] = 0.0
            self._write(w)
            got += n
            self.drained = got

    def _gap(self, n):
        self.dropped += n
        if self.gaps and sum(self.gaps[-1]) == self.written:
            self.gaps[-1] = (self.gaps[-1][0], self.gaps[-1][1] + n)   # one gap, not many
        elif len(self.gaps) < 256:
            self.gaps.append((self.written, n))

    def _silence(self, n):
        self._gap(n)
        z = self._wbuf
        while n > 0:
            k = min(n, z.shape[0])
            z[:k] = 0.0
            self._write(z[:k])
            n -= k

    def _write(self, frames):
        while frames.shape[0]:
            room = self.part_frames - self._part_written
            if room <= 0:
                self._close()
                self._open(len(self.paths) + 1)
                room = self.part_frames
            k = min(room, frames.shape[0])
            self._file.write(frames[:k])
            self._part_written += k
            self.written += k
            frames = frames[k:]

    def _open(self, n):
        p = part_path(self.path, n)
        # "x": a take never writes over a file that is already there
        self._file = sf.SoundFile(p, mode="x", samplerate=self.sr,
                                  channels=self.channels, subtype=self.subtype,
                                  format="WAV")
        self.paths.append(p)
        self._part_written = 0

    def _close(self):
        f, self._file = self._file, None
        if f is not None:
            f.close()

    def _update_header(self):
        """So a crash mid-take leaves a file that opens, holding everything
        written up to the last update, a couple of seconds before."""
        f = self._file
        if f is not None and self._part_written:
            sf._snd.sf_command(f._file, SFC_UPDATE_HEADER_NOW, sf._ffi.NULL, 0)
