"""Render a session to a WAV with no sound card.

    python -m loopengine.offline kits/testkit-124 out.wav 8

Same callback, same quantiser, same interpolators as the live path — this is
how you check a change without putting your ears on the line.
"""
from __future__ import annotations

import os
import sys

import numpy as np

from .demo import write_wav
from .engine import Engine
from .loader import decode
from . import dsp


def build_session(kit_dir, sr=48000, blocksize=256):
    eng = Engine(samplerate=sr, blocksize=blocksize, offline=True).start()
    files = sorted(f for f in os.listdir(kit_dir)
                   if f.lower().endswith((".wav", ".flac", ".mp3", ".ogg",
                                          ".aif", ".aiff")))
    for i, name in enumerate(files[:len(eng.tracks)]):
        path = os.path.join(kit_dir, name)
        buf, fsr = decode(path)
        bpm, conf = dsp.estimate_bpm(buf, fsr)
        pts, _ = dsp.slice_points(buf, fsr, 16)
        eng.post("track.load", i=i, buf=buf, sr=fsr, name=name, path=path,
                 bpm=bpm, conf=conf, slices=pts)
    return eng, files


def main(argv=None):
    argv = argv or sys.argv[1:]
    kit = argv[0] if argv else "kits/testkit-124"
    out = argv[1] if len(argv) > 1 else "render.wav"
    bars = float(argv[2]) if len(argv) > 2 else 8.0

    eng, files = build_session(kit)
    eng.stop()                       # kill the wall clock; we pull manually
    eng.stream = None

    sr = eng.sr
    eng.render_offline(2048)         # drain the load commands
    eng.post("transport.bpm", v=124.0)
    eng.post("transport.start")
    for i in range(min(4, len(files))):
        eng.post("track.play", i=i)
    eng.post("pad.assign", i=0, track=min(4, len(files) - 1), slice=2,
             mode="ONE", gain=1.0)

    spb = 60.0 / 124.0 * sr
    total = int(bars * 4 * spb)
    chunks = []
    fired = False
    done = 0
    while done < total:
        n = min(4096, total - done)
        chunks.append(eng.render_offline(n).copy())
        done += n
        if not fired and done > total * 0.5:
            eng.post("track.rev", i=1)          # quantised: lands on the bar
            eng.post("pad.trigger", i=0)
            eng.post("track.loop.scale", i=2, v=0.5)
            fired = True
    audio = np.concatenate(chunks)

    write_wav(out, audio, sr)
    pk = float(np.abs(audio).max())
    print("%s  %.2f s  %d Hz  peak %.3f (%.1f dBFS)"
          % (out, audio.shape[0] / sr, sr, pk,
             20 * np.log10(max(pk, 1e-9))))
    print("tracks: %s" % ", ".join(files[:4]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
