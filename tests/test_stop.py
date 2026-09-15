"""Task 26: SPACE stops the sound, and no stop clicks.

    PYTHONPATH=.pylibs python3 -m tests.test_stop

"A click" is measured, not heard: the largest sample-to-sample step from the
moment a change lands, against the largest step while the audio was simply
playing. Setting a track's playing flag False used to make that ratio 26.
"""
from __future__ import annotations

import sys

import numpy as np

from loopengine.engine import Engine

SR, B = 48000, 256
FAILS = []
CLICK = 1.5        # a fade may steepen a sine a little; a cut is 26x


def check(name, ok, detail=""):
    print("%-68s %s%s" % (name, "PASS" if ok else "FAIL",
                          ("  " + detail) if detail else ""))
    if not ok:
        FAILS.append(name)


def sine(f, secs=2.0, amp=0.5):
    t = np.arange(int(SR * secs)) / SR
    y = (np.sin(2 * np.pi * f * t) * amp).astype(np.float32)
    return np.stack([y, y], axis=1)


def rig(load=((0, 220.0),), play=(0,)):
    e = Engine(samplerate=SR, blocksize=B, offline=True)
    for i, f in load:
        e.post("track.load", i=i, buf=sine(f), sr=SR, name="s%d.wav" % i, path="",
               bpm=0.0, conf=0.0, slices=np.zeros(0, dtype=np.int64))
    e.post("transport.quantum", v=0)
    e.post("transport.start")
    for i in play:
        e.post("track.play", i=i)
    e.render_offline(B * 20)
    return e


def across(e, *posts, blocks=16):
    """Render, apply the posts, render on. Returns (click ratio, ms until
    silent, the output after the change)."""
    before = e.render_offline(B * 2)
    for op, kw in posts:
        e.post(op, **kw)
    after = e.render_offline(B * blocks)
    x = np.concatenate([before[:, 0], after[:, 0]])
    d = np.abs(np.diff(x))
    sounding = float(np.abs(np.diff(before[:, 0])).max()) or 1e-12
    ratio = float(d[before.shape[0] - 1:].max()) / sounding
    loud = np.nonzero(np.abs(after[:, 0]) > 1e-6)[0]
    ms = (loud[-1] + 1) / SR * 1000.0 if loud.size else 0.0
    return ratio, ms, after


def t_space_silences_the_tracks_without_a_click():
    e = rig(load=((0, 220.0), (1, 330.0)), play=(0, 1))
    ratio, ms, _ = across(e, ("transport.stop", {}))
    check("SPACE stops the tracks, not just the clock",
          not e.tracks[0].playing and not e.tracks[1].playing
          and not e.transport.playing)
    check("without a click", ratio <= CLICK,
          "largest step %.2fx the sounding one (a cut was 26x)" % ratio)
    check("and it is silent inside 40 ms", ms <= 40.0, "%.1f ms" % ms)
    check("nothing is left half-stopped",
          not any(t.stopping for t in e.tracks))


def t_one_track_stopping_fades_too():
    e = rig()
    ratio, ms, _ = across(e, ("track.stop", {"i": 0}))
    check("a single track's stop is a fade, not a cut", ratio <= CLICK,
          "%.2fx" % ratio)
    check("and lands in silence", ms <= 40.0 and not e.tracks[0].playing, "%.1f ms" % ms)


def t_toggling_back_mid_fade_carries_on():
    e = rig()
    before = e.render_offline(B * 2)
    e.post("track.toggle", i=0)
    mid = e.render_offline(B)                  # partway down the fade
    was_fading = e.tracks[0].stopping and e.tracks[0].playing
    e.post("track.toggle", i=0)
    after = e.render_offline(B * 12)
    x = np.concatenate([before[:, 0], mid[:, 0], after[:, 0]])
    sounding = float(np.abs(np.diff(before[:, 0])).max())
    ratio = float(np.abs(np.diff(x))[before.shape[0] - 1:].max()) / sounding
    check("a toggle catches the track mid-fade", was_fading)
    check("toggling again carries on instead of stopping or jumping",
          e.tracks[0].playing and not e.tracks[0].stopping
          and float(np.abs(after[-B:, 0]).max()) > 0.05)
    check("and neither turn clicks", ratio <= CLICK, "%.2fx" % ratio)


def t_keys_fade_in_every_mode():
    for mode in ("ONE", "GATE", "LOOP"):
        e = rig(play=())                       # only the key sounds
        e.post("pad.take", i=0, track=0)
        e.post("pad.assign", i=0, mode=mode)
        e.render_offline(B)
        e.post("pad.trigger", i=0)
        e.render_offline(B * 10)
        sounding = float(np.abs(e.render_offline(B)).max())
        ratio, ms, _ = across(e, ("transport.stop", {}))
        check("a %s key is sounding before the stop" % mode, sounding > 0.01,
              "peak %.2f" % sounding)
        check("SPACE fades a %s key without a click" % mode,
              ratio <= CLICK and ms <= 40.0 and not e.voices.pad_active(0),
              "%.2fx, silent after %.1f ms" % (ratio, ms))


def t_escape_is_still_the_immediate_cut():
    e = rig()
    ratio, ms, after = across(e, ("panic", {}))
    check("ESC still cuts in the same block — the one control allowed to click",
          float(np.abs(after[:, 0]).max()) == 0.0 and ratio > 10.0,
          "%.1fx, silent from the first sample" % ratio)


def t_starting_the_clock_brings_nothing_back():
    e = rig(load=((0, 220.0), (1, 330.0)), play=(0, 1))
    e.post("transport.stop")
    e.render_offline(B * 16)
    e.post("transport.start")
    out = e.render_offline(B * 8)
    check("starting the clock again does not resurrect stopped tracks",
          e.transport.playing and not any(t.playing for t in e.tracks)
          and float(np.abs(out).max()) == 0.0)


def t_space_still_discards_a_queued_launch():
    """Work order 5's rule must survive the new stop."""
    e = rig(load=((0, 220.0), (1, 330.0)), play=(0,))
    e.post("transport.quantum", v=7)           # 4BAR
    e.render_offline(B)
    e.post("track.play", i=1)
    e.render_offline(B * 2)
    check("the launch is waiting on a 4BAR boundary",
          len(e._pending) == 1 and not e.tracks[1].playing)
    ratio, ms, after = across(e, ("transport.stop", {}), blocks=24)
    check("SPACE discards it: track 2 never starts",
          not e.tracks[1].playing and len(e._pending) == 0)
    check("while track 1 fades out without a click",
          not e.tracks[0].playing and ratio <= CLICK and ms <= 40.0,
          "%.2fx, silent after %.1f ms" % (ratio, ms))
    check("and the queue does not strand to fire on the next start",
          (e.post("transport.start"), e.render_offline(SR * 2), not e.tracks[1].playing)[2])


if __name__ == "__main__":
    for fn in (t_space_silences_the_tracks_without_a_click,
               t_one_track_stopping_fades_too,
               t_toggling_back_mid_fade_carries_on,
               t_keys_fade_in_every_mode,
               t_escape_is_still_the_immediate_cut,
               t_starting_the_clock_brings_nothing_back,
               t_space_still_discards_a_queued_launch):
        try:
            fn()
        except Exception as exc:
            check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    print("\n%d checks failed" % len(FAILS) if FAILS else "\nall checks passed")
    sys.exit(1 if FAILS else 0)
