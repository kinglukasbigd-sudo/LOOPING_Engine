"""Decode to float32 (frames, channels). libsndfile first, ffmpeg second.

No resampling happens here. The file keeps its own rate and the engine folds
the ratio into the playback step.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading

import numpy as np

from . import dsp

AUDIO_EXT = {".wav", ".wave", ".flac", ".ogg", ".oga", ".opus", ".mp3",
             ".m4a", ".aac", ".aif", ".aiff", ".aifc", ".caf", ".w64", ".wv"}


class DecodeError(Exception):
    pass


def _via_soundfile(path):
    try:
        import soundfile as sf
    except ImportError as e:
        raise DecodeError("python-soundfile is not installed") from e
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    return np.ascontiguousarray(data, dtype=np.float32), int(sr)


def _via_ffmpeg(path):
    exe = shutil.which("ffmpeg")
    if not exe:
        raise DecodeError("ffmpeg is not on PATH")
    probe = shutil.which("ffprobe")
    sr, ch = 44100, 2
    if probe:
        try:
            out = subprocess.run(
                [probe, "-v", "quiet", "-select_streams", "a:0", "-show_entries",
                 "stream=sample_rate,channels", "-of", "csv=p=0", path],
                capture_output=True, text=True, timeout=20).stdout.strip()
            parts = out.split(",")
            if len(parts) >= 2:
                sr, ch = int(parts[0]), int(parts[1])
        except Exception:
            pass
    proc = subprocess.run(
        [exe, "-v", "quiet", "-i", path, "-f", "f32le", "-acodec",
         "pcm_f32le", "-ar", str(sr), "-ac", str(ch), "-"],
        capture_output=True, timeout=600)
    if proc.returncode != 0 or not proc.stdout:
        raise DecodeError("ffmpeg could not decode this file")
    data = np.frombuffer(proc.stdout, dtype="<f4")
    usable = (data.size // ch) * ch
    return np.ascontiguousarray(data[:usable].reshape(-1, ch)), sr


def decode(path: str):
    """-> (buf, sr). Raises DecodeError with a sentence a person can act on."""
    if not os.path.isfile(path):
        raise DecodeError("There is no file at %s" % path)
    errors = []
    for fn in (_via_soundfile, _via_ffmpeg):
        try:
            buf, sr = fn(path)
            if buf.size == 0:
                errors.append("%s returned zero samples" % fn.__name__)
                continue
            peak = float(np.abs(buf).max())
            if peak > 1.0:
                buf = buf / peak
            return buf, sr
        except DecodeError as e:
            errors.append(str(e))
        except Exception as e:
            errors.append("%s: %s" % (type(e).__name__, e))
    raise DecodeError("Couldn't decode %s — %s." %
                      (os.path.basename(path), "; ".join(errors)))


class Loader:
    """Decode + analyse off the audio thread, then post the result in."""

    def __init__(self, engine, on_done=None, on_error=None):
        self.engine = engine
        self.on_done = on_done or (lambda *a: None)
        self.on_error = on_error or (lambda *a: None)

    def load_async(self, track_i: int, path: str, slices: int = 16):
        threading.Thread(target=self._work, args=(track_i, path, slices),
                         daemon=True).start()

    def _work(self, track_i, path, want_slices):
        try:
            buf, sr = decode(path)
        except DecodeError as e:
            self.on_error(track_i, str(e))
            return
        bpm, conf = dsp.estimate_bpm(buf, sr)
        pts, method = dsp.slice_points(buf, sr, want_slices)
        self.engine.post("track.load", i=track_i, buf=buf, sr=sr,
                         name=os.path.basename(path), path=path,
                         bpm=bpm, conf=conf, slices=pts)
        self.on_done(track_i, {
            "peaks": dsp.peaks(buf, 2048),
            "frames": buf.shape[0], "sr": sr, "channels": buf.shape[1],
            "bpm": bpm, "conf": conf, "slices": pts.tolist(),
            "slice_method": method, "name": os.path.basename(path),
        })

    def variant_async(self, track_i: int, mode: str):
        threading.Thread(target=self._variant, args=(track_i, mode),
                         daemon=True).start()

    def _variant(self, track_i, mode):
        t = self.engine.tracks[track_i]
        if t.src is None:
            return
        if mode in t.variants:
            self.engine.post("track.mode", i=track_i, mode=mode)
            return
        buf = dsp.center_extract(t.src, t.sr, mode.lower())
        self.engine.post("track.variant", i=track_i, mode=mode, buf=buf)
        self.on_done(track_i, {"peaks": dsp.peaks(buf, 2048), "mode": mode})

    def reslice_async(self, track_i: int, want: int):
        threading.Thread(target=self._reslice, args=(track_i, want),
                         daemon=True).start()

    def _reslice(self, track_i, want):
        t = self.engine.tracks[track_i]
        if t.src is None:
            return
        pts, method = dsp.slice_points(t.buf, t.sr, want)
        self.engine.post("track.slices", i=track_i, slices=pts)
        self.on_done(track_i, {"slices": pts.tolist(), "slice_method": method})
