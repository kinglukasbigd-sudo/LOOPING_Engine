"""Key slots: own audio, own region, surviving any change to the tracks.

    PYTHONPATH=.pylibs python3 -m tests.test_keys
"""
from __future__ import annotations

import sys

import numpy as np

from loopengine.engine import Engine
from loopengine.track import MIN_LOOP

SR = 48000
FAILS = []


def check(name, ok, detail=""):
    print("%-60s %s%s" % (name, "PASS" if ok else "FAIL",
                          ("  " + detail) if detail else ""))
    if not ok:
        FAILS.append(name)


def tone(f, n=SR, amp=0.5):
    t = np.arange(n) / SR
    y = (np.sin(2 * np.pi * f * t) * amp).astype(np.float32)
    return np.stack([y, y], axis=1)


def engine(**kw):
    return Engine(samplerate=SR, blocksize=256, offline=True, **kw)


def load(e, i, buf, name):
    e.post("track.load", i=i, buf=buf, sr=SR, name=name, path="/tmp/" + name,
           bpm=0.0, conf=0.0, slices=np.zeros(0, dtype=np.int64))
    e.render_offline(256)


def dominant_hz(y):
    spec = np.abs(np.fft.rfft(y[:, 0] * np.hanning(y.shape[0])))
    return float(np.fft.rfftfreq(y.shape[0], 1 / SR)[int(np.argmax(spec))])


# --------------------------------------------------------------------------
def t_a_key_keeps_its_audio_across_a_track_change():
    """The whole request. Assign from one file, replace the file, press the key."""
    e = engine()
    load(e, 0, tone(300.0), "first.wav")
    e.post("track.loop", i=0, ls=1000, le=21000)
    e.render_offline(256)
    e.post("pad.take", i=0, track=0)                 # snapshot buffer + region
    e.post("pad.assign", i=0, mode="ONE", gain=1.0)
    e.post("master.gain", v=1.0)
    e.render_offline(256)
    p = e.pads[0]
    check("the key took the track's audio and region",
          p.loaded and (p.loop_start, p.loop_end) == (1000, 21000)
          and p.name == "first.wav",
          "%s %s" % (p.name, (p.loop_start, p.loop_end)))

    load(e, 0, tone(900.0), "second.wav")            # the track is replaced
    e.post("track.loop", i=0, ls=5, le=500)
    e.render_offline(512)
    check("the key is untouched by the track being replaced",
          p.name == "first.wav" and (p.loop_start, p.loop_end) == (1000, 21000),
          "%s %s" % (p.name, (p.loop_start, p.loop_end)))

    e.post("pad.trigger", i=0)
    y = e.render_offline(8192)
    hz = dominant_hz(y)
    check("and pressing it still plays the ORIGINAL sound",
          abs(hz - 300.0) < 8.0, "%.0f Hz (first=300, second=900)" % hz)


def t_the_snapshot_is_not_a_live_link():
    e = engine()
    load(e, 0, tone(300.0), "a.wav")
    e.post("track.loop", i=0, ls=1000, le=21000)
    e.render_offline(256)
    e.post("pad.take", i=0, track=0)
    e.render_offline(256)
    e.post("track.loop", i=0, ls=30000, le=40000)    # edit the track after
    e.render_offline(256)
    p = e.pads[0]
    check("editing the track's region does not reach into the key",
          (p.loop_start, p.loop_end) == (1000, 21000),
          str((p.loop_start, p.loop_end)))


def t_a_key_region_is_editable_in_its_own_right():
    e = engine()
    load(e, 0, tone(300.0), "a.wav")
    e.post("pad.take", i=0, track=0, ls=0, le=10000)
    e.render_offline(256)
    e.post("pad.loop", i=0, ls=2000, le=6000)
    e.render_offline(256)
    p = e.pads[0]
    check("a key's own loop can be set", (p.loop_start, p.loop_end) == (2000, 6000),
          str((p.loop_start, p.loop_end)))
    e.post("pad.loop", i=0, ls=900, le=100)          # reversed
    e.render_offline(256)
    check("and it carries the same guards as a track",
          p.loop_end - p.loop_start >= MIN_LOOP and p.loop_end > p.loop_start,
          str((p.loop_start, p.loop_end)))


def t_ejecting_the_track_does_not_silence_the_key():
    e = engine()
    load(e, 0, tone(300.0), "a.wav")
    e.post("pad.take", i=0, track=0, ls=0, le=20000)
    e.post("pad.assign", i=0, mode="ONE", gain=1.0)
    e.post("master.gain", v=1.0)
    e.render_offline(256)
    e.post("track.clear", i=0)                        # eject
    e.render_offline(512)
    check("the track is empty", e.tracks[0].src is None)
    check("the key still holds its audio", e.pads[0].loaded)
    e.post("pad.trigger", i=0)
    y = e.render_offline(8192)
    check("and still makes a sound",
          float(np.abs(y).max()) > 0.05, "peak %.3f" % np.abs(y).max())


def t_buffers_are_shared_not_copied():
    e = engine()
    buf = tone(300.0)
    load(e, 0, buf, "a.wav")
    one = e.audio_bytes()
    for k in range(8):
        e.post("pad.take", i=k, track=0, ls=0, le=10000 + k)
    e.render_offline(256)
    many = e.audio_bytes()
    check("eight keys on one file cost what one file costs",
          many == one, "%d vs %d bytes" % (many, one))
    check("and they really are the same array",
          all(e.pads[k].buf is e.tracks[0].buf for k in range(8)))


def t_memory_has_a_stated_ceiling():
    """Growth comes from keys outliving the track that loaded their audio.

    Taking from a loaded track is free — the track already holds the array.
    The total only climbs when the track moves on and the key keeps the old
    buffer alive, so that is where the ceiling is enforced.
    """
    one_mb = (SR * 3 * 2 * 4) / 1048576.0        # ~1.1 MB per 3-second file
    e = engine(memory_mb=6)
    load(e, 0, tone(200.0, SR * 3), "f0.wav")
    e.post("pad.take", i=0, track=0, ls=0, le=1000)
    e.render_offline(256)
    check("taking from a loaded track is free and allowed",
          e.pads[0].loaded and e.last_error == "",
          "refused: %s" % e.last_error)
    before = e.audio_bytes()

    # each new file on the same track leaves the previous one pinned by a key
    refused_at = None
    for k in range(1, 12):
        load(e, 0, tone(200.0 + 40 * k, SR * 3), "f%d.wav" % k)
        if e.last_error:
            refused_at = k
            break
        e.post("pad.take", i=k % len(e.pads), track=0, ls=0, le=1000)
        e.render_offline(256)
    check("the total climbs as keys outlive their tracks",
          e.audio_bytes() > before, "%.1f -> %.1f MB"
          % (before / 1048576.0, e.audio_bytes() / 1048576.0))
    check("and a load past the ceiling is refused rather than swallowed",
          refused_at is not None, "never refused in 11 loads")
    if refused_at:
        check("the refusal names the ceiling and the keys holding memory",
              "ceiling" in e.last_error and "--memory-mb" in e.last_error
              and "keys" in e.last_error,
              e.last_error[:64])
        check("and the usage is reported to the panel",
              e.snapshot()["audio_mb"] > 0
              and e.snapshot()["audio_limit_mb"] == 6,
              "%s of %s MB" % (e.snapshot()["audio_mb"],
                               e.snapshot()["audio_limit_mb"]))


def t_clearing_a_key_releases_its_hold():
    e = engine()
    load(e, 0, tone(300.0), "a.wav")
    e.post("pad.take", i=0, track=0, ls=0, le=10000)
    e.render_offline(256)
    e.post("track.clear", i=0)
    e.render_offline(256)
    held = e.audio_bytes()
    check("with the track ejected the key is the only holder", held > 0)
    e.post("pad.clear", i=0)
    e.render_offline(256)
    check("clearing the key releases the last reference",
          e.audio_bytes() == 0 and not e.pads[0].loaded,
          "%d bytes still held" % e.audio_bytes())


def t_key_state_serialises():
    """Session save/load is the natural follow-on, so the state must be flat."""
    import json
    e = engine()
    load(e, 0, tone(300.0), "a.wav")
    e.post("pad.take", i=3, track=0, ls=111, le=2222)
    e.post("pad.assign", i=3, mode="LOOP", gain=0.7, pan=-0.3, speed=1.5,
           quantize=True, reverse=True)
    e.render_offline(256)
    snap = e.pads[3].snapshot(3)
    round_tripped = json.loads(json.dumps(snap))
    check("a key's whole state is JSON-serialisable",
          round_tripped == snap)
    for field in ("name", "ls", "le", "mode", "gain", "pan", "speed", "q", "rev"):
        if field not in snap:
            check("key state carries %s" % field, False)
            return
    check("and carries everything needed to restore it "
          "(path kept separately for reload)",
          snap["ls"] == 111 and snap["le"] == 2222 and snap["mode"] == "LOOP"
          and snap["rev"] is True, str(snap)[:58])


def t_a_key_reports_the_four_numbers_its_rule_is_drawn_from():
    """The pad no longer draws a waveform; it lights part of its own hairline.

    That mark is computed from exactly four fields of the key's snapshot —
    name, ls, le, frames — so those four have to describe the KEY and not the
    track it came from, and they have to stay inside the buffer. A stale
    `track`/`slice` pair used to be printed on the cell as provenance; it
    names a track that may since hold something else entirely, which is why
    the cell shows the filename instead.
    """
    e = engine()
    load(e, 0, tone(220.0, n=SR * 4), "bass.wav")
    e.post("pad.take", i=2, track=0, ls=SR, le=SR * 2)
    e.render_offline(256)
    before = e.pads[2].snapshot(2)

    # the track moves on; the key must not follow it
    load(e, 0, tone(660.0, n=SR * 3), "keys.wav")
    after = e.pads[2].snapshot(2)

    check("the key names its own file, not the track's",
          after["name"] == "bass.wav" and e.tracks[0].name == "keys.wav",
          "key=%s track=%s" % (after["name"], e.tracks[0].name))
    check("and its region and length are untouched by the load",
          (after["ls"], after["le"], after["frames"])
          == (before["ls"], before["le"], before["frames"]),
          str((after["ls"], after["le"], after["frames"])))

    span = (after["ls"] / after["frames"], after["le"] / after["frames"])
    check("the span normalises inside the cell, in order",
          0.0 <= span[0] < span[1] <= 1.0, "%.4f..%.4f" % span)
    check("and points at the quarter of the file the key actually holds",
          abs(span[0] - 0.25) < 1e-6 and abs(span[1] - 0.5) < 1e-6,
          "%.4f..%.4f" % span)


def t_an_unassigned_key_has_no_span_to_draw():
    """An empty key shows the plain hairline, so it must report frames 0 and
    not a stale region left over from whatever it held before."""
    e = engine()
    load(e, 0, tone(300.0), "a.wav")
    e.post("pad.take", i=5, track=0, ls=100, le=9000)
    e.render_offline(256)
    check("a loaded key has something to draw", e.pads[5].snapshot(5)["frames"] > 0)
    e.post("pad.clear", i=5)
    e.render_offline(256)
    snap = e.pads[5].snapshot(5)
    check("a cleared key reports nothing to draw",
          snap["frames"] == 0 and snap["ls"] == 0 and snap["le"] == 0
          and snap["loaded"] is False, str(snap)[:60])
    check("and stops naming a file it no longer holds",
          snap["name"] == "", repr(snap["name"]))


if __name__ == "__main__":
    for fn in (t_a_key_keeps_its_audio_across_a_track_change,
               t_the_snapshot_is_not_a_live_link,
               t_a_key_region_is_editable_in_its_own_right,
               t_ejecting_the_track_does_not_silence_the_key,
               t_buffers_are_shared_not_copied,
               t_memory_has_a_stated_ceiling,
               t_clearing_a_key_releases_its_hold,
               t_key_state_serialises,
               t_a_key_reports_the_four_numbers_its_rule_is_drawn_from,
               t_an_unassigned_key_has_no_span_to_draw):
        try:
            fn()
        except Exception as exc:
            check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    print("\n%d checks failed" % len(FAILS) if FAILS else "\nall checks passed")
    sys.exit(1 if FAILS else 0)
