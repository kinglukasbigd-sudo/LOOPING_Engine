# LOOP ENGINE

Multi-track loop mangler. Load stems or whole tracks, set loop points, launch
them in time with each other, and hit slices back on a 4×4 pad grid.

- numpy does the maths, PortAudio does the clock, a local page is the panel.
- 8 loop tracks + 16 pads over a 16-voice pool, all sounding at once.
- Loop launches, stops, reverses and pad hits land on a quantum boundary,
  sample-accurate. The block is split at the boundary; nothing waits for the
  next callback.
- Files are never resampled. A 44.1k loop on a 48k device plays through the
  same Catmull-Rom interpolator that does varispeed.

---

## Install

```bash
pip install -r requirements.txt
```

If your system Python is externally managed (PEP 668), vendor them next to
`run.py` instead — it puts `.pylibs/` on the path itself:

```bash
pip install --target .pylibs -r requirements.txt
```

PortAudio is a system library. pip does not ship it on Linux:

```bash
sudo apt install libportaudio2        # Debian / Ubuntu
```

`sudo dnf install portaudio` on Fedora, `sudo pacman -S portaudio` on Arch.
macOS and Windows wheels bundle it already.

MP3 and M4A: `soundfile` reads MP3 when libsndfile is 1.1 or newer. If yours is
older, install `ffmpeg` and the loader falls back to it automatically.

## Run

```bash
python run.py
```

The panel opens at `http://127.0.0.1:7331`. On first run it synthesises a test
kit (`kits/testkit-124`, 2 bars at 124 BPM in Fm) and loads it, so the thing
makes noise before you have pointed it at your own files. `--empty` skips that.

```
--device N|name    output device        --blocksize 256   frames per callback
--samplerate N     default: the device  --kit DIR         folder to load
--root DIR         folder the browser may read (repeatable)
--list-devices     what PortAudio sees  --offline         run with no sound card
```

No sound card, or PortAudio missing? `--offline` runs the callback on a wall
clock. The panel, meters, scope and quantiser all behave exactly as they will
once a device is there — you just can't hear it.

## Keys — left hand, one hand

```
 1 2 3 4        pads  1– 4
 Q W E R        pads  5– 8
 A S D F        pads  9–12
 Z X C V        pads 13–16

 SHIFT + 1234   mute track 1–4        SPACE   transport run / stop
 SHIFT + QWER   mute track 5–8        5 / 6   loop ÷2 / ×2
 SHIFT + ASDF   launch track 1–4      7 / 8   nudge loop ∓1 beat
 SHIFT + ZXCV   launch track 5–8      T G B   tap / quantum / reverse
                                      TAB L   next track / load
                                      ESC     panic, everything stops
```

SHIFT is the FUNC key: hold it and the pad grid becomes the track grid. Double
click a strip to launch it; drag on the waveform to set the loop; double click
the waveform to loop the whole file.

## Looping a vocal out of a stereo mix

Each track has three sources: `STEREO`, `CTR` and `SIDE`.

- `CTR` keeps the mid channel across 180 Hz – 8 kHz, where a lead vocal sits,
  and the sides everywhere else, so the loop keeps its bottom and its air.
- `SIDE` is `(L−R)/2` — the wide material, with most centred vocals gone.

This is channel arithmetic, not source separation. It works on a mix where the
vocal is dead centre and falls apart when it is not. Both variants are computed
once on a worker thread and swapped in as a pointer.

## Layout

```
loopengine/
  dsp.py         gather kernels, onsets, bpm, peaks, centre extraction
  track.py       one looping track
  voice.py       pad voices + the pool
  transport.py   sample-counted clock and quantiser
  engine.py      the callback, the command queue, telemetry
  loader.py      decode (libsndfile, then ffmpeg) + analysis off-thread
  server.py      stdlib HTTP + a hand-rolled WebSocket, no framework
  app.py         wiring and the command router
  demo.py        the synthesised test kit
  offline.py     render a session to a WAV with no sound card
  ui/            tokens.css is the design system; nothing else holds a value
```

### Threading

| thread  | does                                        | may block |
|---------|---------------------------------------------|-----------|
| audio   | drains commands, renders, writes telemetry  | never     |
| control | appends commands from the WebSocket         | yes       |
| loader  | decode + analyse, hands over finished arrays| yes       |

`deque.append` / `popleft` are atomic under CPython, so the command path takes
no lock. The callback preallocates its scratch and gathers through `np.take(…,
out=…)`; a few small temporaries per block remain, which is why the default
blocksize is 256 rather than 64. If you get xruns, raise `--blocksize`.

## Tests

```bash
python -m tests.test_engine
```

13 checks: seam continuity, crossfade, quantiser accuracy to within the
anti-click fade, rate conversion, reverse, pad polyphony, the limiter, bpm and
slice analysis, and finiteness under extreme speed and sub-crossfade loops.

```bash
python -m loopengine.offline kits/testkit-124 render.wav 8
```

Renders 8 bars through the same callback, no device needed.

## Latency

Web Audio collapses the path into `baseLatency + outputLatency` because the
graph and the sound card live in the same process. Here a press crosses a
socket and a thread boundary first, so the stages are separate and each is
measured where it happens:

| stage | what | figure |
|---|---|---|
| control | input event → `ws.send()` | ~0.5 ms, browser side |
| wire | `ws.send()` → `Engine.post()` | localhost socket |
| queue | `post()` → drained by the callback | **measured**, ~half a block |
| block | one callback period | `blocksize / samplerate` |
| output | callback → speaker | PortAudio's own figure |
| quantum | 0 … one launch quantum | musical, *not* latency |

`queue` is the only stage that moves with load, so it is kept as a rolling
distribution rather than a constant. Measured over 400 presses arriving at
arbitrary moments (48 kHz, 256 frames, 5.333 ms per block):

```
p0  0.027   p50 2.671   p90 4.919   p100 6.345   mean 2.715 ms
```

Mean lands at half a block, which is what uniform arrival against a fixed
callback should give; 9 of 400 crossed one block period, which is scheduler
jitter. `python -m tests.test_engine` asserts the mean stays inside 0.3–0.7 of
a block and the worst case inside two.

Read it three ways:

- **`GET /api/latency`** — the whole budget as JSON.
- **The panel's console**, once per connect — the same budget, formatted.
- **The header** — `OUT` is callback → speaker, `CTRL` is the panel's own loop
  (press → engine → telemetry → panel, so it carries the 30 Hz pump at ~20 ms,
  median of the last eight probes).

`CTRL` is the reason every control paints its own result locally: waiting 20 ms
for the engine to answer would be felt, so the press is drawn in under a
millisecond and the engine's state replaces it when it arrives.

A probe id is stamped **inside the audio callback**, not on receipt, so a value
coming back proves the audio thread saw it rather than that the socket
delivered it. Timing the send itself would measure nothing — it returns long
before anything happens, the same way `AudioBufferSourceNode.start()` returns
before a sample is heard.

## Craft contract

Four rules the panel holds to, and how each was checked.

**Motion.** Two things move: the focus marker (160 ms) and the browser taking
over the pad grid (280 ms). Both transition `transform` only, both on
`cubic-bezier(0.22, 0.61, 0.36, 1)`. Sampled live, the marker peaks in velocity
on its first frame and decays to zero by 164 ms with no overshoot. Interrupt it
mid-travel and it reverses from where it got to — measured turning round at
125.7 px of a 182 px move without continuing to the target first. Nothing in
the panel is linear or springy; `prefers-reduced-motion` takes both to 0 ms.

**Response.** Every press paints before anything is sent. A pad inverts, a mute
flips, the transport flips — all confirmed set at the instant `send()` runs,
not a telemetry tick later. A *quantised* press is the exception that proves
it: it cannot honestly show the change applied, so it shows `REV` in accent
straight away and lets the engine replace that with the real queued state. A
loop drag redraws from the pointer and hands back once the engine agrees.

**Dominance.** One element at 44 px (the transport position, and the only
accent), one at 26 px (tempo), everything else at 17 px and below. The
waveform is the largest area, so its ink sits at 0.84 to keep the playhead and
the loop rules above it.

**Stillness.** Nothing moves after load. 332 elements sampled through four
seconds of live telemetry and again across eight focus changes, a file load, a
decode error and the browser opening: zero moved, zero resized. Every value
that changes while running has a reserved box measured in `ch` of its own face.
Webfonts load `font-display: optional`, so the face either wins the first paint
or waits for the next run — it never swaps mid-session.

Depth is contrast, size and spacing only: a runtime sweep finds no shadow, no
filter, no backdrop-filter, and no translucent panel anywhere. Dimming is
opacity on leaf text against opaque ground, never a layer over content.

## Security

The server binds 127.0.0.1 and every `/api` call and the WebSocket carry a
token minted per run. The file browser refuses paths outside `--root`. Drops
larger than 200 MB are refused — load those by path.
