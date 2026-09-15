# LOOP ENGINE

Multi-track loop mangler. Load stems or whole tracks, set loop points, launch
them in time with each other, and hit slices back on a 4×4 pad grid.

- numpy does the maths, PortAudio does the clock, a local page is the panel.
- 8 loop tracks + 16 keys over a 16-voice pool, all sounding at once.
- A key holds its own audio and its own region, and keeps both when the track
  it came from is replaced. Each key shows the file it holds, and lights the
  span of that file across its own bottom rule.
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

PortAudio is a system library. pip does not ship it on Linux, so check before
installing — many desktops already have it as a dependency of something else:

```bash
python3 -c "import sounddevice; print(sounddevice.get_portaudio_version()[1])"
sudo apt install libportaudio2        # Debian / Ubuntu, only if that failed
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
                                      9 / 0   fit loop / whole file
                                      - / =   zoom out / in
                                      TAB L   next track / load
                                      ESC     panic, cut everything now
```

SHIFT is the FUNC key: hold it and the pad grid becomes the track grid. Double
click a strip to launch it.

On the waveform: drag a handle to move that loop point, SHIFT-drag to draw a
new loop, drag anywhere else to pan, scroll to zoom around the pointer, and
double click to loop the whole file. Zooming changes nothing you hear and
sends nothing to the engine. The arrow keys nudge the aimed point by one pixel
of what you are looking at, so they get finer as you zoom in — at full zoom,
one sample. While zoomed, the track's row lights the part of the file in view
on its bottom rule, and if the loop runs past an edge of the view, that edge
of the panel lights.

`9` and `0` sit beside `-` and `=` so fitting and zooming are one run of four
keys next to the loop keys `5`–`8`. `F` and `L`, the obvious letters, were
already a pad and the file browser.

## Looping a vocal out of a stereo mix

Each track has three sources: `STEREO`, `CTR` and `SIDE`.

- `CTR` keeps the mid channel across 180 Hz – 8 kHz, where a lead vocal sits,
  and the sides everywhere else, so the loop keeps its bottom and its air.
- `SIDE` is `(L−R)/2` — the wide material, with most centred vocals gone.

This is channel arithmetic, not source separation. It works on a mix where the
vocal is dead centre and falls apart when it is not. Both variants are computed
once on a worker thread and swapped in as a pointer.

## Sessions

SAVE, in the rail, writes the whole set to a file: every track's file, loop
region, gain, pan, speed, reverse, mute, solo and source mode; every key's own
file, source mode, region and settings; tempo, quantum and master level; and
the waveform panel's zoom for each track and each key. OPEN lists the saved
sets where the pad grid is, and puts one back. A loaded set starts stopped.

Sessions are plain JSON in `~/.loopengine/sessions/` (`--sessions DIR` to choose
another folder), versioned from the first release, so a file from a newer build,
or one a hand edit has left unreadable, is refused with the reason rather than
half-read. A value of the wrong type — a gain typed as a word — falls back to its
default instead of stopping the load halfway.

A session never simply trusts what it says. On load every file it names is
checked again — still there, still readable, still the same size and the same
bytes at both ends — and a track or key whose file fails is left empty and
named, in its row or on its key, with the reason in the inspector. Files outside
`--root` were readable only because the file dialog granted them for that run.
The session keeps those grants, signed with a key made once for this
installation (`~/.loopengine/session.key`, readable only by you), so a granted
path that has been edited — even to point at an identical copy — or a session
from another machine is refused for those files and asks for the dialog again.
Editing a gain or a region by hand is fine.

Saving a set with missing slots keeps them. The track or key is written back
exactly as it was read, with the fingerprint it was saved with, so a session
saved while a drive is unplugged still loads those files once the drive is back.
A kept grant goes back signed only if its signature checked out when it was
read; one that did not is written back unsigned, and stays refused.

A session is added up against the memory ceiling before anything is touched. If
it will not fit, nothing loads, the set you had stays exactly as it was, and the
error names the keys holding files no track in the session shows. Audio that
only ever existed in memory has no file to point at; saving names it rather than
dropping it quietly.

## Layout

```
loopengine/
  dsp.py         gather kernels, onsets, bpm, peaks, centre extraction
  track.py       one looping track
  voice.py       pad voices + the pool
  transport.py   sample-counted clock and quantiser
  engine.py      the callback, the command queue, telemetry
  session.py     sets saved and put back; files and grants re-validated
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
PYTHONPATH=.pylibs python3 -m tests     # with the vendored dependencies
python3 -m tests                        # with installed ones
```

295 checks in 13 files, about 15 seconds on the machine they were written on.
Each file runs in its own process and prints PASS, FAIL or SKIP for every check,
and the summary names every SKIP, so a skipped check cannot quietly become a
permanent one. No sound card is needed — everything runs through the offline
engine. `test_zoom` does need `node` on PATH: the panel's zoom arithmetic is
tested in the browser's own file, `loopengine/ui/view.js`, and without node
those checks fail rather than pass untested.

A single file still runs alone, for example `python3 -m tests.test_freeze`.

CI runs the same command on every push: GitHub Actions, ubuntu-24.04,
Python 3.12.3, node 22, the pinned requirements, no display and no sound
card. There the one check that needs a desktop — whether a file-dialog
program exists — prints SKIP with its reason, and the summary names it.

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
distribution. Measured over 400 presses arriving at arbitrary moments, at the
default 48000 Hz / 256 frames (5.333 ms per block):

```
p0 0.064   p25 1.706   p50 2.723   p75 3.942
p90 4.837  p95 5.131   p99 5.260   p100 5.753      mean 2.775 ms
```

Mean lands at half a block, which is what uniform arrival against a fixed
callback gives, and the tail now sits just over **one** block period. It used
to reach nearly two — that was the resampler:

| | 44100 (resampler in path) | 48000 (matched) |
|---|---|---|
| p99 | 10.126 | **5.260** |
| max | 10.665 | **5.753** |
| mean | 3.060 | 2.775 |

### Two totals, both labelled

A single press-to-speaker figure is misleading, because the one worth quoting
is built on the tail and gets repeated as if it were an average. Both are
published, in `/api/latency`, the console report and the header:

| | queue | + block | + output | total |
|---|---|---|---|---|
| at the **mean** queue wait | 2.775 | 5.333 | 10.667 | **18.78 ms** |
| at the **p95** queue wait | 5.131 | 5.333 | 10.667 | **21.13 ms** |

The header shows the p95 one and says so in the label: `PATH p95`.

### Block size

pipewire negotiates its graph quantum to whatever the client asks for, so
there is no fixed quantum to match. At 48000, 30 s per size, 400 presses at
arbitrary phases:

| block | block ms | xruns | p50 | p95 | p99 | callback CPU | pw quantum |
|---|---|---|---|---|---|---|---|
| 256 | 5.333 | 0 | 2.629 | 5.090 | 5.272 | 15.4% | 256 |
| 512 | 10.667 | 0 | 5.724 | 10.220 | 10.620 | 8.3% | 512 |
| 1024 | 21.333 | 0 | 10.696 | 20.261 | 21.180 | 5.2% | 1024 |

The queue wait is uniform over the callback period at every size — mean about
half a period, p99 about one — so the whole path scales linearly:
press-to-callback worst case runs 10.6, 21.3, 42.5 ms.

There is no tail left to buy off. Zero xruns at every size, and the p99 that
used to sit near two block periods was the resampler, not the block size.
Larger blocks buy CPU and cost responsiveness with nothing to offset it, so
256 stays the default: 4× the responsiveness of 1024 for 3× the CPU, and at
15.4% CPU is not the constraint.

### The output stage is measured, not reported

PortAudio's stream latency is not a measurement. Opening the stream at several
sizes shows it tracking `blocksize / rate`:

| block | rate | block_ms | reported | ratio |
|---|---|---|---|---|
| 128 | 44100 | 2.9025 | 8.7075 | 3.000 |
| 256 | 44100 | 5.8050 | 5.8050 | **1.000** |
| 256 | 48000 | 5.3333 | 10.6667 | 2.000 |

It is `max(one block, the device's advertised figure rounded up to whole
blocks)`. `--measure-output` measures it instead: a 5 ms chirp emitted at a
known output frame, recorded on a duplex stream, located by cross-correlation.

```
48000/256   round trip 21.3333 ms, 7 of 7 repeats, spread 0.0000 ms
            one way    10.6667 ms   backend said 10.6667 — agrees exactly
44100/256   round trip 18.3370 ms, 6 repeats, spread 0.0230 ms
            one way     9.1690 ms   backend said  5.8050 — understates by 3.364
```

At the default the backend's arithmetic happens to be right; at 44100 it was
not. Both caveats travel in the output rather than being buried here: the
deltas do not jitter across repeats, so the path is **digital — a graph loop,
not a DAC**, excluding analog conversion, cable and air; and one direction is
half the round trip, which assumes the stages are symmetric.

Without a measurement the field stays `output_ms_reported` and the header
value carries a trailing `?`. A labelled unknown beats a confident wrong
number.

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

## Measured on hardware

The engine ran for a long time against a wall-clock backend with no sound card,
which meant every DSP claim rested on the offline renderer. It has now been run
through PortAudio on a real device (pipewire default, 44100 Hz), with the
callback output teed to disk so the exact signal the device consumed could be
analysed.

**Rate conversion under load.** A 48 kHz loop of 20000 samples played on a
44100 Hz device is 18375 output samples — a ratio of 0.91875, so the
interpolator is working on every sample. The seam recurred at exactly that
period across a 12.5 s capture.

**The crossfade, on real output, at both rates.** A click is a step far larger
than the material's own slope, so the seam is judged against the step
distribution of the same recording away from the seam. At 48000 with a 48000
source the playback step is exactly 1.0, so the interpolator contributes
nothing and what remains is the crossfade and the material:

| | seam step | p99 elsewhere | ratio |
|---|---|---|---|
| 44100, resampler in path — off | 0.13571 | 0.00897 | 15.14× |
| 44100, resampler in path — 10 ms | 0.02919 | 0.00865 | 3.37× |
| **48000, no resampler — off** | 0.13085 | 0.00827 | 15.83× |
| **48000, no resampler — 10 ms** | **0.02682** | 0.00795 | **3.37×** |

Reduction 4.65× at 44100, 4.88× at 48000. **The resampler was not a
significant contributor**: the residue fell about 8% and the ratio against the
material is identical. The earlier figure was already measuring the crossfade
alone, so what is left is genuinely the crossfade's, not interpolation error.
That makes the listening test decisive about the crossfade itself.

**Channel shaping, on a source that actually has width.** `CTR`/`SIDE` only
mean anything on stereo material; of the demo kit only the keys part has real
width (side/mid 0.283). Soloed, at 48000:

| | correlation | side/mid |
|---|---|---|
| STEREO | +0.851 | 0.283 |
| CTR | +0.978 | 0.112 |
| SIDE | −1.000 | mid fully nulled |

**The one xrun was the measuring instrument.** A 28 s capture reported 1 xrun,
which looked like a real finding. Isolating it across four conditions, three
repeats each:

| condition | xruns | blocks |
|---|---|---|
| baseline, pre-decoded, no capture | 0 | 7542 |
| decode inside the window | 0 | 7607 |
| capture appending to a list | 0 | 7543 |
| **both together** | **3** | 7605 |

Neither cause alone produces one; together they do, at the same point in each
repeat. The capture was copying every block into a growing list on the audio
thread — about 2 kB per callback — and with the extra memory pressure from
decoding in-window a garbage collection lands inside the callback. `capture()`
now writes into one buffer allocated up front, and the same worst case runs
clean: 0 xruns over 13051 blocks. Across every run without an appending
capture, roughly 56000 blocks, zero.

Two traps worth keeping written down. Wrapping `Engine._callback` *after*
`start()` records nothing at all — PortAudio holds the bound method it was
constructed with, so the capture silently returns zero frames. And the
hardware period here is 1024 frames at 48000 while the engine asks for 256 at
the device rate; that mismatch is real but produced no periodic underruns, so
the ALSA layer is absorbing it cleanly.

**What is still unverified: nobody has listened.** These are measurements, not
judgements. `max |diff|` is useless as a click metric on broadband material —
a hi-hat at 44.1 kHz legitimately swings full scale between adjacent samples,
and an earlier pass of this analysis produced meaningless numbers for exactly
that reason. The captures are the honest artefact; a person has to play them.

## Traps

Ten things that looked like they worked. Each cost real time, and each
produces a confident wrong answer rather than an error, which is why they are
written down rather than left in a commit message.

**A CSS edit that silently did not match.** An edit to `tokens.css` failed to
apply — the search string had drifted — leaving `--m-state` and `--ease`
undefined. The `transition` shorthand was then invalid, so every transition
ran at `0s`. Screenshots looked perfect, because 0 ms motion is invisible, not
broken. *Read computed values back from the live page after any token edit;
a diff that applied cleanly is not evidence.*

**Wrapping `_callback` after `start()` does nothing.** PortAudio holds the
bound method it was constructed with, so reassigning `engine._callback` on a
running stream records zero frames and reports no error. The first fix for the
capture problem below hit this and silently captured nothing. *`capture()` is
inside the callback now, so ordering cannot break it.*

**The capture produced the underrun it was recording.** Copying each block
into a growing list on the audio thread survived on its own, and so did decode
work elsewhere. The two together produced xruns:

| condition | xruns | blocks |
|---|---|---|
| baseline | 0 | 7542 |
| decode inside the window | 0 | 7607 |
| capture appending to a list | 0 | 7543 |
| both together | **3** | 7605 |

*`capture()` allocates one buffer up front. The same worst case then runs
clean: 0 over 13051 blocks.*

The explanation first written here — that the list dragged a garbage
collection into the callback — cannot be right as it stood. numpy arrays are
not tracked by the collector (`gc.is_tracked(np.zeros(3))` is False), so a list
of them is one tracked object however long it grows, and appending to it never
advances the collector's count. The table and the fix stand; the mechanism
behind those three xruns is not established. What work order 7 did establish
is the next entry.

**Allocating on the audio thread is not what starves it.** Work order 7 was
asked to remove the lists the quantised queue built inside the callback, on the
theory that they were how that dropout happened. Measured on the device before
changing anything, three muted four-minute tracks playing, 30 s a phase:

| load on another thread | xruns | blocks run | on the audio thread |
|---|---|---|---|
| none | 0 | 6000 | no collections |
| a loop drag at 120 Hz against a BAR quantum | 0 | 6000 | no collections |
| decoding a 44 MB file, back to back | 0 | 6016 | no collections |
| a set: a four-minute file loading every 3 s, telemetry at 30 Hz, the drag | 0 | 6000 | no collections |
| a tight pure-Python loop building containers | **367** | **909** | 7, one of them 10.36 ms |

The queue's lists never set a collection off: they come from CPython's free
lists and die in the block that made them, and a collection is triggered by net
growth in tracked objects, not by how many are made. What starves the callback
is another thread holding the interpreter lock — a CPU-bound Python loop, and
the full collections it sets off, 8–11 ms each wherever they run. A shorter GIL
switch interval made it worse: 903 xruns. The queue is preallocated anyway, so
the callback builds no container by construction, and every snapshot now counts
the collections that land on the audio thread. The same phases afterwards: 0, 0,
0, 0 — and 382 for the loop that no queue change can reach. *Nothing in the
engine or the panel runs a loop like that. Keep it that way.*

**A fix that was right for one kind of work and wrong for the other.** "A
stopped clock has no edges" unstuck a queue that could strand and later revert
an edit. Applied to a *launch* it inverted the transport: a start queued at
4BAR fired at the instant of pressing STOP, and because tracks are not gated on
the transport, the track did not merely arm — it started, at peak 0.1595 with
the transport stopped. Pressing STOP made sound. *The queue holds two kinds of
work. An edit says how a thing should be and can land whenever; a launch says
when a thing should happen, and a stopped clock has no when. Stopping cancels
every launch and still lands every edit.*

**Painting the panel in the click handler does not survive the render pass.**
Clearing the queue rail on stop worked for one frame and was then rebuilt from
the telemetry snapshot taken *before* the stop — so a STOP was followed by half
a second of the panel still promising a START. *Feedback before the send has to
be a prediction the render pass consults, not a write it overwrites.*

**A cache keyed on a length.** The waveform panel and every strip cached their
plot under a key built from the track, its mode, its frame count and its number
of buckets. Every kit loop is 185,806 frames, so loading one over another made
the same key, and the first file's waveform went on being drawn over the second
— confirmed against the committed code: new peaks arrived, the key did not
change, the bitmap was the old file's. *Key a cache on the thing itself — the
peaks array, the file name — not on a number that happens to describe it.*

**An overview that skipped half the file.** The plot took one envelope bucket
per pixel, so 2,048 buckets on a ~1,080 px panel drew about half of them. With
one transient hidden in each of 823 buckets in turn, the overview missed 389.
It looked like a waveform because most of a waveform survives being thinned.
*Every column now takes every bucket that overlaps it.*

**A test that blamed correct code.** The check for that fix hid a 0.9 transient
in a bucket and looked for >= 0.9 in the plot. It found none, in the new plot
and the old one alike: a Float32Array holds 0.9 as 0.8999999762. *When the new
code and the old fail a test identically, suspect the test.*

**`pgrep` takes one pattern.** `pgrep -a pipewire wireplumber pulseaudio`
matches nothing and exits quietly, which produced a confident "pipewire is not
running" that was wrong and propagated into a later work order. pipewire was
running the whole time and was the thing inserting the resampler.

## The audio graph underneath

The ALSA hardware substream runs at **48000 Hz with 1024-frame periods**.
pipewire sits above it and negotiates its graph quantum to whatever the client
asks — `pw-top` shows our node at 256, 512 or 1024 to match each request — so
there is no fixed quantum to match and no requantising.

The rate is what matters. The device advertises 44100 while the sink runs
48000, so opening at the device default put a resampler in the path:

```
opened at 44100: node 44100 vs sink 48000  ->  RESAMPLER IN PATH
opened at 48000: node 48000 vs sink 48000  ->  no resampling
```

That resampler cost real accuracy — it was most of the latency tail (p99
10.126 → 5.260 ms) — and it quietly undid the engine's never-resample property
at the last hop. So the engine prefers the graph's rate over the device's
advertised default, and when it cannot match one it says so: the header reads
`44100>48k` underlined, and startup prints `RESAMPLED`.

Running at 48000 exercises never-resample in the other direction rather than
less: a 44.1 kHz file folds 0.91875 into its playback step, which is the
property working as designed.

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

**Dominance.** One element at 44 px (the transport position), one at 26 px
(tempo), everything else at 17 px and below. One accent carries exactly three
roles and nothing else may borrow it: the playhead, what is queued, and what is
chosen — the region, whether that is ink in a waveform or a lit span on a key's
own rule. The token used to say "max 2 elements per screen", which a panel with
eight rows that can each queue exceeded long ago; the discipline that actually
held is the role count. The
waveform is the largest area, so its ink sits at 0.84 to keep the playhead and
the loop rules above it.

**Stillness.** Nothing moves after load. The current sweep samples 499
elements, at 1366 px and at 1600 px, across every way of moving the waveform
view — wheel zoom at the pointer, shift-wheel and drag pans, fit loop, fit
file, the zoom keys, accurate peaks arriving, a handle dragged while zoomed,
the readouts growing to an eight-digit offset — plus focus moving between
tracks and keys whose names differ wildly in length, and help toggling on and
off: zero moved, zero resized, every label restored exactly after help. The
sweep before zoom covered a real file load, the transport starting and a key
region dragged down to a sliver, also at zero.

The probe earns its keep. It found the inspector's subject title sizing to its
content — the display face has no tabular figures, so TRACK 1 is 41.22px and
TRACK 8 is 43.30px, and moving the focus one row down shifted the title and
pushed the spacer beside it. It found the empty-state panel assigning
`innerHTML` on every pass, destroying and rebuilding its key caps sixty times a
second to arrive at identical text. Both were invisible by eye.

A baseline taken while `requestAnimationFrame` is throttled compares a stale
DOM to a settled one, so the probe forces a render and a layout flush before
sampling. That artifact has cost real time twice. Every value
that changes while running has a reserved box measured in `ch` of its own face.
Webfonts load `font-display: optional`, so the face either wins the first paint
or waits for the next run — it never swaps mid-session.

Depth is contrast, size and spacing only: a runtime sweep finds no shadow, no
filter, no backdrop-filter, and no translucent panel anywhere. Dimming is
opacity on leaf text against opaque ground, never a layer over content.

## The grant, and its lifetime

The OS dialog exists to reach folders outside `--root`, so its results cannot
be judged by the root check. Paths this server itself returned from a
user-driven dialog are granted individually — never paths a caller supplies —
bounded to 64. A sibling file in the same directory stays refused.

**Loaded paths are pinned.** Eviction walks oldest-first past any path a track
is currently holding. Evicting a grant for a file that is loaded and on screen
breaks the next reslice or source-mode switch with a refusal the user cannot
act on, because from where they sit the file is plainly right there.
Granted-but-unloaded paths are still evictable.

**`offline.py` carries the same boundary — and where it does not, it says so.**
It used to call `decode()` with no root check at all, which made the renderer
strictly *more* permissive than the server it reproduces. Now:

- **Directory mode** treats the directory argument as the grant for its
  contents. That is deliberate — naming the directory is the whole instruction
  the renderer was given — but it was previously implicit, which made the
  boundary unreachable from the command line and easy to mistake for
  enforcement that was not happening.
- **`--file` arguments carry no implicit grant.** Each must be covered by
  `--allow`, or the run exits 2 naming the file.

Either way a file that may not be read *raises*. Nothing is skipped into a
silent track, which was the actual risk. Pinned by
`tests/test_grant.py::t_offline_refuses_outside_and_names_the_file` and
`::t_cli_file_mode_enforces_allow`.

**Restart is not yet a question.** Nothing in the project saves or loads a
session, so no path can go stale across a restart. `tests/test_grant.py`
asserts that absence deliberately: add session save/load and the test fails,
forcing the grant lifetime to be decided rather than rediscovered.

The test that matters is offline/live equality, because every DSP result rests
on it. The same outside-root file is loaded once through the server with a
dialog grant and once through the renderer with `--allow`, run through a
region change, and compared: **max diff 0.000e+00**, peak 0.2886 — not silence
being compared against silence.

## Security

The server binds 127.0.0.1 and every `/api` call and the WebSocket carry a
token minted per run. The file browser refuses paths outside `--root`. Drops
larger than 200 MB are refused — load those by path.
