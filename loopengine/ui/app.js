/* LOOP ENGINE — panel.
   Colours are read out of tokens.css at runtime. Nothing here invents one. */
'use strict';

const TOKEN = document.body.dataset.token;
const css = getComputedStyle(document.documentElement);
const C = {};
for (const k of ['bg', 'n1', 'n2', 'fg', 'accent'])
  C[k] = css.getPropertyValue('--' + k).trim();
const DIM1 = parseFloat(css.getPropertyValue('--dim1')) || 0.55;
const WAVE_INK = parseFloat(
  getComputedStyle(document.querySelector('#wcanvas')).getPropertyValue('--wave-ink')) || 0.84;
const DIM2 = parseFloat(css.getPropertyValue('--dim2')) || 0.30;
const FONT_TEXT = css.getPropertyValue('--font-text').trim();
const T1 = parseInt(css.getPropertyValue('--t1'), 10) || 10;

const PAD_CODES = ['Digit1', 'Digit2', 'Digit3', 'Digit4',
                   'KeyQ', 'KeyW', 'KeyE', 'KeyR',
                   'KeyA', 'KeyS', 'KeyD', 'KeyF',
                   'KeyZ', 'KeyX', 'KeyC', 'KeyV'];
const PAD_CAPS = ['1', '2', '3', '4', 'Q', 'W', 'E', 'R',
                  'A', 'S', 'D', 'F', 'Z', 'X', 'C', 'V'];
const QUANTUM_LABELS = ['OFF', '1/16', '1/8', 'BEAT', '1/2', 'BAR', '2BAR', '4BAR'];

/* Physical key positions, not characters — the 4x4 block is a shape, not a
   set of letters. Some remote-desktop and virtual keyboards send no `code`;
   codeOf() rebuilds one from `key` so the panel still drives. */
const SHIFTED_DIGITS = { '!': 1, '@': 2, '#': 3, '$': 4, '%': 5,
                         '^': 6, '&': 7, '*': 8, '(': 9, ')': 0 };
function codeOf(e) {
  if (e.code) return e.code;
  const k = e.key;
  if (!k) return '';
  if (k === ' ') return 'Space';
  if (k.length === 1) {
    if (/[a-z]/i.test(k)) return 'Key' + k.toUpperCase();
    if (/[0-9]/.test(k)) return 'Digit' + k;
    if (k in SHIFTED_DIGITS) return 'Digit' + SHIFTED_DIGITS[k];
    if (k === '=' || k === '+') return 'Equal';
    if (k === '-' || k === '_') return 'Minus';
  }
  return k.charAt(0).toUpperCase() + k.slice(1);
}

let ws = null, S = null, focus = 0, padMode = null;
/* Help is a MODE, not a layer. Nothing floats over the grid; each cell that
   has room swaps its own label for an explanation, in its own box. The short
   form has to fit the box it lands in — every target is a reserved cell with
   nowrap and overflow hidden, so a long string clips rather than reflowing —
   and the long form goes in the inspector, which already scrolls.

   One table. Labels and help cannot drift apart because the label is only
   ever restored from here. */
const HELP = {
  pos:     ['BAR AND BEAT',      'Where the master clock is. Counts from 1. Turns orange while running.'],
  tempo:   ['BEATS PER MINUTE',  'The master tempo. TAP it four times to set it by hand.'],
  quantum: ['WHEN LAUNCHES LAND','Changes wait for this boundary before taking effect. OFF applies at once.'],
  master:  ['OUTPUT LEVEL',      'Final level before the limiter. The ladder below is what is leaving.'],
  num:     ['N',                 'Track number. Hold SHIFT and press its key to mute or launch it.'],
  source:  ['FILE ON THIS TRACK','The loaded file. Drop one here, or press L to browse.'],
  wave:    ['THIS FILE',         'Orange loops. Scroll zooms at the pointer, drag moves, SHIFT-drag draws a loop.'],
  bpm:     ['DETECTED TEMPO',    'Measured from the file. Shown only — it never moves a loop point.'],
  loop:    ['LOOP LENGTH',       'How long the looping part is, in beats of this file.'],
  level:   ['OUTPUT',            'How loud this track is right now. Fills solid if it clips.'],
  gain:    ['VOLUME',            'Drag to set this track level.'],
  pan:     ['LEFT / RIGHT',      'Drag to place this track in the stereo field.'],
  msr:     ['MUTE SOLO REVERSE', 'Mute, solo, and play backwards. Reverse waits for the quantum.'],
  q:       ['WAITING',           'Shows what this track is waiting to do at the next boundary.'],
  pads:    ['KEYS',              'Each key holds its own sound and loop, and keeps them when you change the track.'],
  session: ['SESSION',           'SAVE writes every track, key, region and zoom to a file. OPEN puts a set back.'],
};

/* Reserved, not derived: the inspector holds this many rows whether it is
   showing track detail (13) or the help table (13). An empty row keeps its
   box so a shorter list cannot shorten the column. */
const INSPECTOR_ROWS = 16;
let helpMode = false;
let focusKind = 'track';   // 'track' | 'key' — what the waveform panel edits
let focusKey = 0;          // which key, when focusKind is 'key'
let assignArmed = false;   // next key pressed takes the focused track's region
let editKeys = false;      // clicking a pad focuses it instead of firing it
const padPeaks = {};       // pad index -> its own envelope
/* What the waveform panel is looking at: one view per track and per key,
   kept in this page and never sent to the engine. See view.js. */
const views = View.store();
const ranges = new Map();  // subject -> accurate peaks for its zoomed view
const rangeQ = View.coalescer(2000);
let rangeTimer = 0;
let awaitingPick = null;   // track index whose slot is waiting on the dialog
let localError = '';       // picker trouble, shown where engine errors go

/* Control round trip: input -> socket -> audio thread -> telemetry -> here.
   The engine stamps `probe` inside the callback itself, so an id coming back
   proves the audio thread saw it — not just that the socket delivered it.
   Timing send() would measure nothing: it returns before anything happens. */
const probe = { id: 0, sentAt: 0, ms: null, hist: [] };
let pathP95 = null;   // press-to-speaker at p95, refreshed with the report
function sendProbe() {
  if (!ws || ws.readyState !== 1) return;
  probe.id = (probe.id % 30000) + 1;
  probe.sentAt = performance.now();
  send({ op: 'probe', id: probe.id });
}

/* Local prediction. The engine is 33ms of telemetry away at best, so a press
   paints its own result immediately and holds it until the engine agrees or
   the window lapses. Only ever used for changes that land at once — a
   quantised launch must NOT be predicted, because the whole point is that it
   has not happened yet. */
const OPT = new Map();
function predict(key, v, ms = 500) {
  OPT.set(key, { v, until: performance.now() + ms });
}
function settled(key, fromEngine) {
  const o = OPT.get(key);
  if (o === undefined) return fromEngine;
  if (o.v === fromEngine || performance.now() > o.until) {
    OPT.delete(key);
    return fromEngine;
  }
  return o.v;
}
let connected = false, lastFired = [];
const peaks = {};                 // track -> Float32Array(n*2) min/max
let scope = new Int8Array(0);
const meterHold = [];             // per-track peak hold

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
/* Writing textContent dirties the node even when the string is identical, and
   this runs 60 times a second across ~60 elements. */
function setText(el, v) {
  const str = String(v);
  if (el && el.textContent !== str) el.textContent = str;
}

/* ── link ───────────────────────────────────────────────────────────── */
function connect() {
  ws = new WebSocket(`ws://${location.host}/ws?t=${encodeURIComponent(TOKEN)}`);
  ws.binaryType = 'arraybuffer';
  ws.onopen = async () => {
    connected = true;
    document.body.classList.remove('offline');
    sendProbe();
    // One-time path report, once the engine is actually there
    try {
      const r = await fetch(`/api/latency?t=${encodeURIComponent(TOKEN)}`);
      const L = await r.json();
      pathP95 = L.press_to_speaker_p95_ms;
      console.log(
`LOOP ENGINE — press-to-speaker path (${L.backend} backend, ${L.samplerate} Hz)
  queue   mean ${L.queue_mean_ms.toFixed(2)}  p50 ${L.queue_p50_ms.toFixed(2)}  p95 ${L.queue_p95_ms.toFixed(2)}  p99 ${L.queue_p99_ms.toFixed(2)} ms
          post() -> picked up by the callback; the only stage that moves
  block   ${L.block_ms.toFixed(2)} ms   ${L.blocksize} frames
  output  ${L.output_ms.toFixed(2)} ms   ${L.output_ms_source}
          backend reported ${L.output_ms_reported.toFixed(2)} ms${L.output_ms_measured === null ? '' : `, loopback measured ${L.output_ms_measured.toFixed(2)} ms`}
  ------
  total   ${L.press_to_speaker_mean_ms.toFixed(2)} ms  at the MEAN queue wait
  total   ${L.press_to_speaker_p95_ms.toFixed(2)} ms  at the p95 queue wait  <- the header shows this one
  quantum ${L.quantum_ms.toFixed(0)} ms   musical wait, not latency

CTRL in the header is a different path: press -> engine -> telemetry -> panel,
so it carries the 30 Hz pump and is roughly 20 ms. That is why every control
paints its own result locally instead of waiting for the engine to answer.`);
    } catch (e) { /* the panel works without the report */ }
  };
  ws.onclose = () => {
    connected = false;
    document.body.classList.add('offline');
    $('#h-conn').textContent = 'DOWN';
    setTimeout(connect, 1000);
  };
  ws.onmessage = (ev) => {
    if (typeof ev.data === 'string') {
      const msg = JSON.parse(ev.data);
      if (msg.op === 'picked') { onPicked(msg); return; }
      if (msg.op === 'session.saved') { onSessionSaved(msg); return; }
      if (msg.op === 'session.loaded') { onSessionLoaded(msg); return; }
      S = msg;
      if (S.probe_id === probe.id && probe.sentAt) {
        // A probe is answered by the next telemetry frame, so one reading
        // carries up to a whole pump period of jitter. The median of the last
        // eight is the honest single figure, and it does not flicker.
        probe.hist.push(performance.now() - probe.sentAt);
        if (probe.hist.length > 8) probe.hist.shift();
        const sorted = [...probe.hist].sort((a, b) => a - b);
        probe.ms = sorted[sorted.length >> 1];
        probe.sentAt = 0;
      }
      return;
    }
    const v = new DataView(ev.data);
    const kind = v.getUint8(0);
    if (kind === 0x10) {                                   // scope
      scope = new Int8Array(ev.data, 4);
    } else if (kind === 0x11 || kind === 0x12) {           // peaks
      const t = v.getUint8(1), n = v.getUint16(2);
      const raw = new Int8Array(ev.data, 4, n * 2);
      const f = new Float32Array(n * 2);
      for (let i = 0; i < n * 2; i++) f[i] = raw[i] / 127;
      if (kind === 0x11) peaks[t] = f; else padPeaks[t] = f;
    } else if (kind === 0x13) {                            // peaks for a zoomed view
      const samples = (v.getUint8(3) & 1) === 1;
      const req = v.getUint32(4), start = v.getUint32(8), end = v.getUint32(12);
      const frames = v.getUint32(16), count = v.getUint32(20);
      const vals = count * (samples ? 1 : 2);
      const data = new Float32Array(vals);
      for (let k = 0; k < vals; k++) data[k] = v.getInt16(24 + 2 * k, true) / 32767;
      const r = rangeQ.answer(req, performance.now());
      // a reply for a file that has since been replaced is dropped
      if (r.accepted && frames === r.accepted.frames) {
        ranges.set(r.accepted.kind + ':' + r.accepted.i, {
          ink: r.accepted.ink,
          src: samples ? View.samplesSource(data, start)
                       : View.envelopeSource(data, start, end),
        });
      }
      if (r.next) sendRange(r.next);
    }
  };
}
function send(o) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }

/* ── formatting ─────────────────────────────────────────────────────── */
const pad3 = (n) => String(n).padStart(3, '0');
const fx = (n, d) => (n === undefined || n === null) ? '—' : Number(n).toFixed(d);
function dB(g) { return g <= 0.0001 ? '-inf' : (20 * Math.log10(g)).toFixed(1); }
function secs(f, sr) { return sr ? (f / sr).toFixed(2) + ' s' : '—'; }
function bytes(n) {
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n / 1024).toFixed(0) + ' K';
  return (n / 1048576).toFixed(1) + ' M';
}
/* A loop drag redraws from the pointer, not from the next telemetry tick.
   `dragLoop` holds the local truth until the engine echoes the same numbers. */
let dragLoop = null;
let grabbed = null;        // 'in' | 'out' | 'new' while the pointer is down
let handleFocus = 'in';    // which handle the arrow keys move

/* Setting dragLoop is enough for the canvas, which redraws next frame, but the
   readouts are DOM text written by the render loop — so a frame would pass
   before the digits moved. Write them here instead: the whole visible result
   of the gesture lands before the socket call, not a frame after it. */
function paintRegion(i, ls, le) {
  dragLoop = { i, ls, le, kind: focusKind };
  setText($('#w-in'), ls.toLocaleString('en-US'));
  setText($('#w-out'), le.toLocaleString('en-US'));
  setText($('#w-len'), (le - ls).toLocaleString('en-US'));
  // the key's own rule is feedback too, and it is a frame away otherwise
  if (focusKind === 'key' && S && S.pads[i]) {
    const el = $$('#padgrid .pad')[i];
    if (el) paintKeySpan(el, i, ls, le, S.pads[i].frames);
  }
}

/* A key's region, carried on the rule the key already has. Not a bar and not
   a readout: the cell's own bottom hairline lights over the part of the file
   this key holds, so sixteen keys report sixteen regions in sixteen pixels of
   ink. Two custom properties, on a strip that is out of flow — it cannot move
   anything, and the write is skipped entirely when the numbers have not
   changed. */
const spanCache = [];
function paintKeySpan(el, i, ls, le, frames) {
  let a = 0, b = 0;
  if (frames > 0 && le > ls) {
    a = Math.max(0, Math.min(1, ls / frames));
    b = Math.max(a, Math.min(1, le / frames));
  }
  const k = a.toFixed(5) + ':' + b.toFixed(5);
  if (spanCache[i] === k) return;
  spanCache[i] = k;
  el.style.setProperty('--rs', (a * 100).toFixed(3) + '%');
  el.style.setProperty('--re', ((1 - b) * 100).toFixed(3) + '%');
}

/* One place that knows whether an edit goes to a track or a key. */
function sendRegion(ls, le) {
  if (focusKind === 'key') send({ op: 'pad.loop', i: focusKey, ls, le });
  else send({ op: 'track.loop', i: focus, ls, le });
}

function assignToKey(k) {
  const t = S && S.tracks[focus];
  if (!t || !t.loaded) {
    localError = 'Nothing to assign — track ' + (focus + 1) + ' is empty.';
    return;
  }
  const [ls, le] = liveLoop(focus, Object.assign({}, t, { kind: 'track' }));
  const el = $$('#padgrid .pad')[k];
  if (el) el.classList.add('hit');            // paint first
  setTimeout(() => el && el.classList.remove('hit'), 160);
  send({ op: 'pad.take', i: k, track: focus, ls, le });
  assignArmed = false;
  $('[data-act="assign"]').setAttribute('aria-pressed', false);
}
/* The waveform panel edits a track or a key. Everything downstream reads
   this rather than reaching into S.tracks, so a key is a first-class subject
   and not a special case bolted onto the track path. */
function subject() {
  if (focusKind === 'key' && S && S.pads[focusKey]) {
    const p = S.pads[focusKey];
    return {
      kind: 'key', i: focusKey, loaded: p.loaded, name: p.name,
      sr: p.sr, ch: p.ch, frames: p.frames, ls: p.ls, le: p.le,
      bpm: 0, conf: 0, mode: 'STEREO', speed: p.speed, gain: p.gain,
      pan: p.pan, rev: p.rev, peak: 0, phase: 0, slices: 0,
      playing: !!(S.pads_on && S.pads_on[focusKey]), queued: null,
      analysing: false, xfade: 0, path: p.name,
      title: 'KEY ' + (PAD_CAPS[focusKey] || focusKey + 1),
      peaks: padPeaks[focusKey],
    };
  }
  const t = S ? S.tracks[focus] : null;
  if (!t) return null;
  return Object.assign({}, t, {
    kind: 'track', i: focus, peaks: peaks[focus],
    title: 'TRACK ' + (focus + 1),
  });
}

function liveLoop(i, t) {
  if (dragLoop && dragLoop.i === i && dragLoop.kind === (t.kind || focusKind))
    return [dragLoop.ls, dragLoop.le];
  return [t.ls, t.le];
}

/* ── the view ───────────────────────────────────────────────────────────
   Wires view.js to whatever the panel shows. A view is keyed to the slot and
   the file in it; the accurate peaks are keyed to that AND the source mode. */
function subjectId(t) { return t.kind + ':' + t.i; }
function fileSig(t) { return t.name + '|' + t.frames; }
function inkId(t) { return subjectId(t) + '|' + fileSig(t) + '|' + t.mode; }
function cssW() { return $('#wcanvas').clientWidth || 1; }

function viewOf(t) {
  return View.clamp(views.get(subjectId(t), fileSig(t), t.frames), t.frames, cssW());
}

/* The whole visible result of a zoom or pan is this store write: the next
   frame plots from data already in the page. Accurate peaks are asked for
   once the view settles — never per wheel tick. */
function setView(t, v) {
  views.set(subjectId(t), fileSig(t), View.clamp(v, t.frames, cssW()));
  scheduleRange();
}

function scheduleRange() {
  clearTimeout(rangeTimer);
  rangeTimer = setTimeout(askRange, 120);
}

/* Only when the load-time envelope cannot resolve the view. Wider than one of
   its buckets per column, reducing it per column is already exact to its
   buckets, and asking would cost the server a pass over millions of frames
   for nothing. */
function askRange() {
  const t = subject();
  if (!t || !t.loaded || !t.peaks) return;
  const Wd = $('#wcanvas').width || 1;              // device px: a bucket per column
  const v = viewOf(t);
  const ov = View.overviewSource(t.peaks, t.frames);
  if (!ov || (v.ve - v.vs) / Wd >= View.bucketSize(ov)) return;
  const start = Math.floor(v.vs), end = Math.ceil(v.ve);
  const have = ranges.get(subjectId(t));
  if (have && have.ink === inkId(t) && have.src.start === start && have.src.end === end) return;
  const d = rangeQ.ask({ kind: t.kind, i: t.i, start, end, buckets: Wd,
                         frames: t.frames, ink: inkId(t) }, performance.now());
  if (d) sendRange(d);
}

function sendRange(d) {
  send({ op: 'peaks.range', kind: d.kind, i: d.i, start: d.start, end: d.end,
         buckets: d.buckets, req: d.req });
}

function rangeFor(t) {
  const e = ranges.get(subjectId(t));
  return e && e.ink === inkId(t) ? e.src : null;
}

function zoomKey(factor) {
  const t = subject();
  if (!t || !t.loaded) return;
  const W = cssW();
  setView(t, View.zoomAt(viewOf(t), t.frames, W, W / 2, factor));
}
function fitLoopKey() {
  const t = subject();
  if (!t || !t.loaded) return;
  const [ls, le] = liveLoop(t.i, t);
  setView(t, View.fitLoop(ls, le, t.frames, cssW()));
}
function fitFileKey() {
  const t = subject();
  if (t && t.loaded) setView(t, View.whole(t.frames));
}

/* The strip's envelope is the whole file — the overview. While the panel is
   zoomed into this track, the strip's bottom rule lights across the span in
   view. Written only when that span changes, like the key regions. */
const miniView = [];
function paintMinimap(cell, i, t) {
  let k = '';
  if (cell && t.loaded && focusKind === 'track' && i === focus) {
    const v = views.peek('track:' + i, fileSig(t));
    if (v && !View.isWhole(v, t.frames)) {
      k = (v.vs / t.frames * 100).toFixed(3) + '%:' +
          ((1 - v.ve / t.frames) * 100).toFixed(3) + '%';
    }
  }
  if (!cell || miniView[i] === k) return;
  miniView[i] = k;
  cell.classList.toggle('zoomed', k !== '');
  if (k) {
    const [a, b] = k.split(':');
    cell.style.setProperty('--vs', a);
    cell.style.setProperty('--ve', b);
  }
}

function beatFrames(t) {
  const bpm = t.bpm > 0 ? t.bpm : (S ? S.bpm : 124);
  return 60 / bpm * t.sr;
}
function loopBeats(t) {
  const bf = beatFrames(t);
  if (bf <= 0) return 0;
  const [ls, le] = liveLoop(t.i, t);
  return (le - ls) / bf;
}

/* ── strips ─────────────────────────────────────────────────────────── */
function buildStrips(n) {
  const host = $('#strips');
  host.innerHTML = '';
  miniView.length = 0;         // fresh rows carry no view span yet
  for (let i = 0; i < n; i++) {
    const el = document.createElement('div');
    el.className = 'strip empty-row';
    el.dataset.i = i;
    el.innerHTML = `
      <span class="c-n">${i + 1}</span>
      <span class="c-name">—</span>
      <span class="c-wave"><canvas></canvas></span>
      <span class="c-bpm">—</span>
      <span class="c-len">—</span>
      <span class="c-meter"><canvas></canvas></span>
      <span class="c-gain"><input class="slider" type="range" min="0" max="1.4" step="0.005"></span>
      <span class="c-pan"><input class="slider" type="range" min="-1" max="1" step="0.02"></span>
      <span class="c-btns">
        <button class="btn" data-t="mute">M</button>
        <button class="btn" data-t="solo">S</button>
        <button class="btn" data-t="rev">R</button>
      </span>
      <span class="c-q"></span>`;
    el.addEventListener('mousedown', (e) => {
      if (e.target.closest('button, input')) return;
      setFocus(i);
      if (e.detail === 2) { markQueued(i, 'TOG'); send({ op: 'track.toggle', i }); }
    });
    for (const [t, op] of [['mute', 'track.mute'], ['solo', 'track.solo']]) {
      const b = el.querySelector(`[data-t="${t}"]`);
      b.onclick = () => {
        const v = b.getAttribute('aria-pressed') !== 'true';
        b.setAttribute('aria-pressed', v);       // paint first
        predict(`${i}.${t}`, v);
        send({ op, i, v });
      };
    }
    // reverse is quantised: show it queued at once, never show it applied
    el.querySelector('[data-t="rev"]').onclick = () => {
      markQueued(i, 'REV');
      send({ op: 'track.rev', i });
    };
    el.querySelector('.c-gain input').oninput = (e) =>
      send({ op: 'track.gain', i, v: +e.target.value });
    el.querySelector('.c-pan input').oninput = (e) =>
      send({ op: 'track.pan', i, v: +e.target.value });
    host.appendChild(el);
    meterHold[i] = 0;
  }
  const bar = document.createElement('div');
  bar.id = 'focusbar';
  host.appendChild(bar);
  setFocus(focus);
}

/* A quantised press cannot honestly show the change applied, so it shows the
   change accepted. The engine's own `queued` label replaces this the moment
   it arrives. */
function markQueued(i, label) {
  const el = $$('#strips .strip')[i];
  if (el) {
    const q = el.querySelector('.c-q');
    q.textContent = label;
    q.classList.add('armed');
  }
  predict(`${i}.queued`, label);
}

function setFocus(i) {
  focusKind = 'track';
  focus = Math.max(0, Math.min(i, (S ? S.tracks.length : 8) - 1));
  const rowH = parseInt(css.getPropertyValue('--row-h'), 10);
  const bar = $('#focusbar');
  if (bar) bar.style.transform = `translateY(${focus * rowH}px)`;
  $$('#strips .strip').forEach((el, k) => el.classList.toggle('focused', k === focus));
  paintKeyFocus();
}

/* ── pads ───────────────────────────────────────────────────────────── */
/* The dialog is a human at an OS window, so the HTTP call returns at once and
   the chosen path arrives later on the socket. The slot is marked waiting
   before the request goes out — feedback before logic, same as every control. */
async function openPicker(multiple) {
  if (awaitingPick !== null) return;
  const target = focus;
  awaitingPick = target;                       // paint first
  localError = '';
  paintAwaiting();
  // if the reply never arrives — socket dropped, dialog killed — the row must
  // not read "waiting" for the rest of the session
  clearTimeout(openPicker._t);
  openPicker._t = setTimeout(() => {
    if (awaitingPick === target) {
      awaitingPick = null;
      localError = 'The file dialog did not answer. Nothing was loaded — '
                 + 'press PICK again, or use LOAD to browse by path.';
      paintAwaiting();
    }
  }, 300000);
  try {
    const r = await fetch(`/api/pick?t=${encodeURIComponent(TOKEN)}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ track: target, multiple: !!multiple }),
    });
    const d = await r.json();
    if (!d.available) { awaitingPick = null; localError = d.reason; paintAwaiting(); }
  } catch (e) {
    awaitingPick = null;
    localError = 'Could not reach the engine to open a file dialog.';
    paintAwaiting();
  }
}

function applyHelp() {
  const sheet = $('#helpsheet');
  if (!sheet.childElementCount) {
    sheet.innerHTML = Object.keys(HELP).map(
      k => `<div class="hrow"><b>${HELP[k][0]}</b><span>${HELP[k][1]}</span></div>`
    ).join('');
  }
  sheet.classList.toggle('open', helpMode);
  sheet.setAttribute('aria-hidden', String(!helpMode));
  $$('[data-help]').forEach((el) => {
    const h = HELP[el.dataset.help];
    if (!h) return;
    if (helpMode) {
      if (el.dataset.label === undefined) el.dataset.label = el.textContent;
      setText(el, h[0]);
    } else if (el.dataset.label !== undefined) {
      setText(el, el.dataset.label);
    }
  });
  document.body.classList.toggle('helping', helpMode);
}

function paintKeyFocus() {
  $$('#padgrid .pad').forEach((el, k) =>
    el.classList.toggle('editing', editKeys && focusKind === 'key' && k === focusKey));
  $$('#strips .strip').forEach((el, k) =>
    el.classList.toggle('focused', focusKind === 'track' && k === focus));
}

function paintAwaiting() {
  $$('#strips .strip').forEach((el, k) => {
    const on = k === awaitingPick;
    el.classList.toggle('awaiting', on);
    if (on) setText(el.querySelector('.c-name'), 'waiting for the file dialog');
    else if (S && S.tracks[k]) {
      setText(el.querySelector('.c-name'),
              S.tracks[k].loaded ? S.tracks[k].name : 'empty');
    }
  });
}

/* Cancel is a normal outcome: the slot goes back to exactly what it showed. */
/* ── sessions ───────────────────────────────────────────────────────── */
/* The rail's session line follows the server's own state, so every open panel
   agrees. A press paints its line first as a prediction, and the reply clears
   the prediction the moment it lands. */
function sessionText(s) {
  if (!s || !s.state) return 'nothing saved yet';
  const n = Object.keys(s.missing || {}).length;
  if (s.state === 'loading') return 'loading ' + s.name;
  if (s.state === 'loaded') return 'loaded ' + s.name + (n ? ' — ' + n + ' missing' : '');
  if (s.state === 'saved') return 'saved ' + s.name + (n ? ' — ' + n + ' missing' : '');
  return 'failed — the reason is in the inspector';
}
function slotName(id) {
  const [kind, i] = id.split(':');
  return kind === 'key' ? 'key ' + (PAD_CAPS[+i] || +i + 1) : 'track ' + (+i + 1);
}
function onSessionSaved(msg) {
  OPT.delete('sessionLine');
  if (msg.error) { localError = msg.error; return; }
  const notes = [];
  if (msg.unsaved && msg.unsaved.length)
    notes.push('Saved, but not everything: ' + msg.unsaved.join('; ') + '.');
  /* a save made while a file is away keeps its slot, so it says it did */
  if (msg.kept && msg.kept.length)
    notes.push('Still missing, and kept in the session so they load once the files are back: '
               + msg.kept.join('; ') + '.');
  localError = notes.join(' ');
}
function onSessionLoaded(msg) {
  OPT.delete('sessionLine');
  if (msg.error) { localError = msg.error; return; }
  views.restore(msg.views);            // each track's and key's zoom, as saved
  ranges.clear();
  lastInk = '';
  const miss = Object.entries(msg.missing || {});
  localError = miss.length
    ? 'Loaded without ' + miss.map(([slot, m]) => `${slotName(slot)} — ${m.name} ${m.reason}`).join('; ') + '.'
    : '';
}

function onPicked(msg) {
  clearTimeout(openPicker._t);
  awaitingPick = null;
  paintAwaiting();
  if (msg.error) { localError = msg.error; return; }
  if (msg.cancelled || !msg.paths.length) return;
  fillSlots(msg.track, msg.paths);
}

/* The aimed slot always takes the first file; the rest go to empty slots after
   it. Running out of slots loads what fits and says so, rather than rejecting. */
function fillSlots(start, paths) {
  const n = S ? S.tracks.length : 8;
  const used = [];
  let slot = start;
  for (const path of paths) {
    if (slot === null) break;
    send({ op: 'load', i: slot, path });
    used.push(slot);
    slot = null;
    for (let i = 0; i < n; i++) {
      if (!S.tracks[i].loaded && !used.includes(i)) { slot = i; break; }
    }
  }
  if (used.length < paths.length) {
    localError = `Loaded ${used.length} of ${paths.length} files — `
               + `the other ${paths.length - used.length} had no free track. `
               + `Eject something and pick again.`;
  }
  return used.length;
}

function triggerPad(i) {
  const el = $$('#padgrid .pad')[i];
  const q = S && S.pads[i] && S.pads[i].q;
  if (el) {                       // paint first
    el.classList.add(q ? 'armed' : 'hit');
    if (!q) {
      clearTimeout(el._hit);
      el._hit = setTimeout(() => el.classList.remove('hit'), 140);
    }
  }
  send({ op: q ? 'pad.trigger.q' : 'pad.trigger', i });
}

function releasePad(i) {
  const el = $$('#padgrid .pad')[i];
  if (el) el.classList.remove('hit');
  send({ op: 'pad.release', i });
}

function buildPads(n) {
  const host = $('#padgrid');
  host.innerHTML = '';
  spanCache.length = 0;        // fresh cells carry no span yet
  for (let i = 0; i < n; i++) {
    const b = document.createElement('button');
    b.className = 'pad unmapped';
    b.dataset.i = i;
    b.innerHTML = `<span class="p-key">${PAD_CAPS[i] || i + 1}</span>
                   <span class="p-mode">—</span>
                   <span class="p-label">unassigned</span>`;
    b.addEventListener('mousedown', (e) => {
      if (assignArmed) return assignToKey(i);
      if (editKeys) { focusKind = 'key'; focusKey = i; paintKeyFocus(); return; }
      triggerPad(i);
    });
    b.addEventListener('mouseup', () => releasePad(i));
    b.addEventListener('mouseleave', () => releasePad(i));
    host.appendChild(b);
  }
}

/* ── render pass ────────────────────────────────────────────────────── */
function renderState() {
  if (!S) return;
  setText($('#h-device'), S.device);
  /* A resampler inserted by the sound server is invisible to the user and
     undoes the engine's never-resample property at the last hop. Say so. */
  setText($('#h-rate'), S.resampling
    ? `${S.sr}>${Math.round(S.native_rate / 1000)}k`
    : S.sr + ' Hz');
  $('#h-rate').parentElement.classList.toggle('resampling', !!S.resampling);
  setText($('#h-block'), S.blocksize);
  /* A trailing ? means the backend reported this rather than anything
     measuring it. The console report on connect spells out which. */
  setText($('#h-lat'), fx(S.latency_ms, 1) + ' ms' + (S.latency_measured ? '' : '?'));
  setText($('#h-path'), pathP95 === null ? '—' : fx(pathP95, 2) + ' ms');
  setText($('#h-ctrl'), probe.ms === null ? '—' : fx(probe.ms, 1) + ' ms');
  setText($('#h-cpu'), fx(S.cpu, 1) + ' %');
  setText($('#h-xrun'), S.xruns);
  setText($('#h-clip'), S.clips);
  setText($('#h-vox'), S.voices + '/' + S.voices_max);
  setText($('#h-conn'), connected ? 'UP' : 'DOWN');
  $('#h-link').classList.toggle('warn', !connected);
  document.querySelector('.stat:nth-child(6)').classList.toggle('warn', S.xruns > 0);
  document.querySelector('.stat:nth-child(7)').classList.toggle('warn', S.clips > 0);

  setText($('#pos'), pad3(S.bar) + '.' + Math.floor(S.beat));
  const rolling = settled('transport', S.playing);
  $('#pos').classList.toggle('rolling', rolling);
  $('[data-act="run"]').setAttribute('aria-pressed', rolling);
  setText($('#bpm'), fx(S.bpm, 2));
  if (document.activeElement !== $('#bpm-slider')) $('#bpm-slider').value = S.bpm;
  setText($('#quantum'), QUANTUM_LABELS[settled('quantum', S.quantum_i)]);
  const pend = $('#pending');
  /* A silent wait is indistinguishable from a crash. Say what is waiting
     and how long is left, so a long quantum reads as patience not a hang.
     One predicted depth, and every other queue mark on the panel derives
     from it, so they cannot disagree with each other for a frame. */
  const depth = settled('pending', S.pending);
  setText(pend, depth
    ? `${depth} queued — fires in ${(S.pending_ms / 1000).toFixed(1)} s`
      + ` (${S.quantum.toLowerCase()})`
    : 'nothing queued');
  pend.classList.toggle('armed', depth > 0);
  setText($('#session-line'), settled('sessionLine', sessionText(S.session)));
  setText($('#master-db'), dB(S.master));
  if (document.activeElement !== $('#master')) $('#master').value = S.master;

  if ($$('#strips .strip').length !== S.tracks.length) buildStrips(S.tracks.length);
  if ($$('#padgrid .pad').length !== S.pads.length) buildPads(S.pads.length);

  S.tracks.forEach((t, i) => {
    const el = $$('#strips .strip')[i];
    if (!el) return;
    el.classList.toggle('empty-row', !t.loaded);
    const playing = settled(`${i}.playing`, t.playing);
    el.classList.toggle('playing', playing);
    /* A slot a session could not fill says so, by file name, where the name goes. */
    const gone = !t.loaded && S.session && S.session.missing && S.session.missing['track:' + i];
    setText(el.querySelector('.c-name'),
            i === awaitingPick ? 'waiting for the file dialog'
                               : (t.loaded ? t.name : (gone ? 'missing — ' + gone.name : 'empty')));
    setText(el.querySelector('.c-bpm'),
            !t.loaded ? '—' : (t.analysing ? '···' : fx(t.bpm, 1)));
    setText(el.querySelector('.c-len'),
            t.loaded ? fx(loopBeats(t), 2) + ' b' : '—');
    const g = el.querySelector('.c-gain input');
    if (document.activeElement !== g) g.value = t.gain;
    const p = el.querySelector('.c-pan input');
    if (document.activeElement !== p) p.value = t.pan;
    el.querySelector('[data-t="mute"]').setAttribute(
      'aria-pressed', settled(`${i}.mute`, t.mute));
    el.querySelector('[data-t="solo"]').setAttribute(
      'aria-pressed', settled(`${i}.solo`, t.solo));
    el.querySelector('[data-t="rev"]').setAttribute('aria-pressed', t.rev);
    const q = el.querySelector('.c-q');
    const queued = depth === 0 ? null : settled(`${i}.queued`, t.queued);
    setText(q, queued ? queued.slice(0, 3) : (playing ? '▮' : ''));
    q.classList.toggle('armed', !!queued);
    if (lastFired[i] !== undefined && t.fired !== lastFired[i]) {
      el.classList.remove('fired');
      void el.offsetWidth;
      el.classList.add('fired');
    }
    lastFired[i] = t.fired;
    drawTrackMeter(el.querySelector('.c-meter canvas'), i, t);
    const [wls, wle] = liveLoop(i, Object.assign({}, t, { kind: 'track' }));
    drawMini(el.querySelector('.c-wave canvas'), 't' + i, peaks[i],
             t.frames, wls, wle, t.phase, t.playing);
    paintMinimap(el.querySelector('.c-wave'), i, t);
  });

  const keysSounding = settled('padsOn', S.pads_on.some(Boolean));
  S.pads.forEach((p, i) => {
    const el = $$('#padgrid .pad')[i];
    if (!el) return;
    const mapped = p.loaded;
    el.classList.toggle('unmapped', !mapped);
    el.classList.toggle('on', keysSounding && !!S.pads_on[i]);
    el.classList.toggle('armed',
      depth !== 0 && !!(S.pads_pending && S.pads_pending[i]));
    /* The file this key holds, not where it came from. A key keeps its
       audio when the track moves on, so "T5/S01" named a track that may
       since have been replaced — provenance that goes stale and reads as
       fact. The filename is the one thing that stays true. */
    const gone = !mapped && S.session && S.session.missing && S.session.missing['key:' + i];
    setText(el.querySelector('.p-label'),
            mapped ? p.name : (gone ? 'missing — ' + gone.name : 'unassigned'));
    setText(el.querySelector('.p-mode'), mapped ? p.mode : '—');
    const [pls, ple] = liveLoop(i, { kind: 'key', i, ls: p.ls, le: p.le });
    paintKeySpan(el, i, pls, ple, p.frames);
  });
  // the engine owns the pad mode; a reloaded panel adopts it rather than
  // stamping its own default over a running set
  if (padMode === null) {
    const mapped = S.pads.find(p => p.track >= 0);
    padMode = mapped ? mapped.mode : 'ONE';
  }
  $$('#pad-modes .seg-btn').forEach(b =>
    b.setAttribute('aria-pressed', b.dataset.pmode === padMode));

  renderInspector();
}

function renderInspector() {
  const t = subject();
  if (!t) return;
  const m = t.kind === 'track'
    ? ((S.meta && (S.meta[focus] || S.meta[String(focus)])) || {}) : {};
  /* Same predicted depth as the rows, so the inspector's STATE line cannot
     say "START queued" a beat after the rail says nothing is queued. */
  const queued = t.kind === 'track' && settled('pending', S.pending) !== 0
    ? settled(`${focus}.queued`, t.queued) : null;
  const [ls, le] = liveLoop(t.i, t);
  setText($('#i-title'), t.title);
  setText($('#w-title'), t.title + (t.loaded ? ' — ' + t.name : ''));
  $$('#w-modes .seg-btn').forEach(b =>
    b.setAttribute('aria-pressed', b.dataset.mode === t.mode));

  $('#w-empty').hidden = t.loaded;
  if (!t.loaded) {
    /* Written only when the sentence itself changes. Assigning innerHTML every
       pass destroyed and rebuilt the <kbd> caps sixty times a second to end up
       with identical text — the same churn as the inspector rows, invisible
       until the probe reported nodes appearing and disappearing. */
    const copy = t.kind === 'key'
      ? `Key ${PAD_CAPS[focusKey]} holds nothing yet. Set a loop on a track, `
        + `then hold <kbd>CTRL</kbd> and press this key to give it that loop.`
      : `Track ${focus + 1} is empty. Press <kbd>L</kbd> to pick a file, `
        + `or drop one on this panel.`;
    if ($('#w-empty')._copy !== copy) {
      $('#w-empty')._copy = copy;
      $('#w-empty').innerHTML = copy;
    }
  }
  /* `t.i`, not `focus`: for a key subject those are different numbers, and
     indexing the live drag by the track number meant the readouts and the big
     canvas found no match and fell back to the STORED region — so a key drag
     painted correctly and then reverted on the next telemetry tick. The edit
     still landed; you just could not see what you were doing. */
  const [rls, rle] = liveLoop(t.i, t);
  setText($('#w-in'), t.loaded ? rls.toLocaleString('en-US') : '—');
  setText($('#w-out'), t.loaded ? rle.toLocaleString('en-US') : '—');
  /* Length beside the points: hunting a short part, how long it is is the
     number you steer by, not where it starts. */
  setText($('#w-len'), t.loaded ? (rle - rls).toLocaleString('en-US') : '—');
  // IN and OUT can be aimed; LEN is a result, never a handle
  $$('.rgn').forEach((el, k) =>
    el.classList.toggle('aimed', k < 2 && t.loaded && handleFocus === (k ? 'out' : 'in')));
  setText($('#w-info'), !t.loaded ? 'no file'
    : `${t.sr} Hz · ${t.ch} ch · ${secs(t.frames, t.sr)} · `
      + (t.analysing ? 'analysing' :
         `${t.slices} slices` + (m.slice_method ? ` (${m.slice_method})` : '')));

  /* One note at a time, in the box kept for it. A new note starts at its first
     line, even if the last one had been scrolled. */
  const err = $('#i-error');
  const note = localError || m.error || S.error || '';
  if (err.textContent !== note) { err.textContent = note; err.scrollTop = 0; }
  err.hidden = !note;

  const pan = t.pan === 0 ? 'C'
    : (t.pan < 0 ? 'L' : 'R') + fx(Math.abs(t.pan) * 100, 0);
  const rows = [
    ['file', t.loaded ? t.name : 'empty'],
    ['holds', t.kind === 'key' ? 'its own copy — survives a track change'
                               : 'track audio'],
    ['format', t.loaded ? `${t.sr} Hz · ${t.ch} ch` : '—'],
    ['length', t.loaded ? `${t.frames.toLocaleString('en-US')} fr · ${secs(t.frames, t.sr)}` : '—'],
    ['bpm', !t.loaded ? '—'
            : t.analysing ? 'analysing — playable now'
            : `${fx(t.bpm, 2)} · ${fx(t.conf * 100, 0)}% sure`],
    ['source', t.mode],
    ['loop', t.loaded ? `${ls.toLocaleString('en-US')} → ${le.toLocaleString('en-US')}` : '—'],
    ['loop len', t.loaded ? `${fx(loopBeats(t), 3)} b · ${secs(le - ls, t.sr)}` : '—'],
    ['xfade', t.loaded ? fx(t.xfade / t.sr * 1000, 1) + ' ms' : '—'],
    ['speed', fx(t.speed, 3) + '×' + (t.rev ? '  reversed' : '')],
    ['gain', `${dB(t.gain)} dB · ${pan}`],
    ['peak', dB(t.peak) + ' dB'],
    ['state', queued ? queued + ' queued' : (t.playing ? 'running' : 'stopped')],
  ];
  /* One fixed set of rows, reused. Rebuilding innerHTML would destroy every
     dt and dd and create new ones — invisible on screen, but the elements
     that were there are gone, and anything measuring element identity is
     right to call that a reflow. So the boxes stay and only text changes,
     which is the same rule the rest of the panel follows. */
  const host = $('#i-rows');
  const source = rows;
  if (host.childElementCount !== INSPECTOR_ROWS * 2) {
    host.innerHTML = Array.from({ length: INSPECTOR_ROWS },
      () => '<dt></dt><dd></dd>').join('');
  }
  const dts = host.querySelectorAll('dt');
  const dds = host.querySelectorAll('dd');
  for (let k = 0; k < INSPECTOR_ROWS; k++) {
    const pair = source[k];
    setText(dts[k], pair ? pair[0] : '');
    setText(dds[k], pair ? String(pair[1]) : '');
  }
}

/* ── canvases ───────────────────────────────────────────────────────── */
/* The peak plot is the expensive part and it only changes when the file, the
   source mode or the canvas size changes. Plot it once into an offscreen
   canvas and blit that each frame; a drag then moves the region and the
   handles only, and never re-walks the peak array. */
const waveCache = { key: '', dim: null, hot: null, pk: null, rng: null };
/* One cache per small strip, keyed the same way as the big panel. Eight rows
   and sixteen pads redrawing peaks every frame would be sixty times the work
   of plotting them once; these blit. */
const miniCache = {};

function drawMini(cv, id, pk, frames, ls, le, phase, lit) {
  const [g, W, H] = fit(cv);
  g.fillStyle = C.bg; g.fillRect(0, 0, W, H);
  if (!pk || !frames) return;
  const key = [id, W, H, frames].join(':');
  let c = miniCache[id];
  // keyed on the peaks array itself: two files the same length made the same key
  if (!c || c.key !== key || c.pk !== pk) {
    c = miniCache[id] = { key, pk, dim: plot(W, H, C.fg, pk, frames),
                          hot: plot(W, H, C.accent, pk, frames) };
  }
  g.globalAlpha = DIM2;
  g.drawImage(c.dim, 0, 0);
  g.globalAlpha = 1;
  const x0 = Math.round(ls / frames * W);
  const x1 = Math.round(le / frames * W);
  if (x1 > x0) {
    g.save();
    g.beginPath(); g.rect(x0, 0, x1 - x0, H); g.clip();
    g.drawImage(c.hot, 0, 0);
    g.restore();
  }
  g.fillStyle = C.n2;
  g.fillRect(x0, 0, 1, H);
  g.fillRect(Math.max(x0 + 1, x1 - 1), 0, 1, H);
  if (lit && phase >= 0) {
    g.fillStyle = C.fg;
    g.fillRect(Math.round(phase / frames * W), 0, 1, H);
  }
}

/* Ink for W columns into a canvas (made, or reused), from min/max columns —
   View.columns, which never skips a bucket. Transparent ground. */
function inkColumns(cv, W, H, colour, col) {
  cv = cv || document.createElement('canvas');
  if (cv.width !== W || cv.height !== H) { cv.width = W; cv.height = H; }
  const g = cv.getContext('2d');
  g.clearRect(0, 0, W, H);
  const mid = H / 2;
  g.fillStyle = colour;
  for (let x = 0; x < W; x++) {
    const lo = col[2 * x], hi = col[2 * x + 1];
    if (Number.isNaN(lo)) continue;
    const yTop = mid - hi * mid * 0.92;
    g.fillRect(x, yTop, 1, Math.max(1, (mid - lo * mid * 0.92) - yTop));
  }
  return cv;
}

/* The samples themselves, once the view is below about one sample per pixel.
   At three pixels a sample each one also gets a point, so a transient's first
   sample is somewhere you can put a handle. */
function inkLine(cv, W, H, colour, pts, pxPerSample, r) {
  cv = cv || document.createElement('canvas');
  if (cv.width !== W || cv.height !== H) { cv.width = W; cv.height = H; }
  const g = cv.getContext('2d');
  g.clearRect(0, 0, W, H);
  const mid = H / 2;
  g.strokeStyle = colour;
  g.lineWidth = Math.max(1, Math.round(r));
  g.beginPath();
  for (let j = 0; j < pts.length; j += 2) {
    const y = mid - pts[j + 1] * mid * 0.92;
    if (j) g.lineTo(pts[j], y); else g.moveTo(pts[j], y);
  }
  g.stroke();
  if (pxPerSample >= 3 * r) {
    const d = Math.max(2, Math.round(2 * r));
    g.fillStyle = colour;
    for (let j = 0; j < pts.length; j += 2) {
      g.fillRect(Math.round(pts[j] - d / 2),
                 Math.round(mid - pts[j + 1] * mid * 0.92 - d / 2), d, d);
    }
  }
  return cv;
}

/* A strip's whole-file envelope. */
function plot(W, H, colour, pk, frames) {
  return inkColumns(null, W, H, colour,
    View.columns(View.whole(frames), W, [View.overviewSource(pk, frames)]));
}

/* Two layers, plotted only when what they show changes: the excluded ends in
   ink and the chosen region in accent. Clipping one over the other tints the
   samples themselves; compositing a colour over the region would fill the
   whole rectangle, because the ground beneath it is opaque.

   The layers belong to a VIEW now: they rebuild when the view moves — at most
   once a frame, from data already here — and a handle drag, which moves no
   view, still only blits. They are keyed on the peaks arrays themselves and
   the file's name, not on a length: every kit loop is 185,806 frames, so a
   key built from length alone could not tell one loaded file from the next. */
function buildWaveCache(t, W, H, v, rng, r) {
  const key = [inkId(t), W, H, v.vs, v.ve].join(':');
  if (waveCache.key === key && waveCache.pk === t.peaks && waveCache.rng === rng) {
    return waveCache;
  }
  const line = rng ? View.sampleLine(v, W, rng) : null;
  if (line) {
    const pps = W / (v.ve - v.vs);
    waveCache.dim = inkLine(waveCache.dim, W, H, C.fg, line, pps, r);
    waveCache.hot = inkLine(waveCache.hot, W, H, C.accent, line, pps, r);
  } else {
    const col = View.columns(v, W, [View.overviewSource(t.peaks, t.frames), rng]);
    waveCache.dim = inkColumns(waveCache.dim, W, H, C.fg, col);
    waveCache.hot = inkColumns(waveCache.hot, W, H, C.accent, col);
  }
  waveCache.key = key;
  waveCache.pk = t.peaks;
  waveCache.rng = rng;
  return waveCache;
}

function fit(cv) {
  const r = window.devicePixelRatio || 1;
  const w = Math.max(1, Math.round(cv.clientWidth * r));
  const h = Math.max(1, Math.round(cv.clientHeight * r));
  if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
  return [cv.getContext('2d'), cv.width, cv.height, r];
}

function drawTrackMeter(cv, i, t) {
  const [g, W, H] = fit(cv);
  g.fillStyle = C.n2; g.fillRect(0, 0, W, H);
  if (!t.loaded) return;
  const v = Math.min(1, t.peak);
  const seg = Math.max(2, Math.round(W / 28));
  const lit = Math.round(v * W);
  g.fillStyle = C.fg;
  for (let x = 0; x < lit; x += seg) g.fillRect(x, 0, seg - 1, H);
  meterHold[i] = Math.max(v, (meterHold[i] || 0) - 0.012);
  const hx = Math.round(meterHold[i] * W);
  g.fillRect(Math.min(hx, W - 2), 0, 2, H);
  if (v > 0.99) { g.fillStyle = C.fg; g.fillRect(0, 0, W, H); }
}

const MHOLD = [0, 0];
function drawMasterMeter() {
  const [g, W, H, r] = fit($('#mmeter'));
  g.fillStyle = C.bg; g.fillRect(0, 0, W, H);
  if (!S) return;
  const bw = Math.round(14 * r), gap = Math.round(3 * r);
  const norm = (p) => p <= 0 ? 0
    : Math.max(0, 1 + Math.log10(Math.max(p, 1e-4)) / 2.5);   // -50 dB floor
  g.font = `${T1 * r}px ${FONT_TEXT}`;
  g.textBaseline = 'middle';
  for (const d of [0, -3, -6, -12, -24, -48]) {
    const y = Math.round(H - norm(Math.pow(10, d / 20)) * H);
    g.fillStyle = C.n2;
    g.fillRect(0, y, bw * 2 + gap, 1);
    g.fillStyle = C.fg;
    g.globalAlpha = DIM2;
    g.fillText(d === 0 ? '0' : String(d), bw * 2 + gap * 2, Math.min(H - 5 * r, y));
    g.globalAlpha = 1;
  }
  S.mpeak.forEach((p, k) => {
    const x = k * (bw + gap);
    const h = Math.round(norm(p) * H);
    const seg = Math.round(3 * r);
    g.fillStyle = C.fg;
    for (let y = 0; y < h; y += seg) g.fillRect(x, H - y - seg + 1, bw, seg - 1);
    MHOLD[k] = Math.max(norm(p), MHOLD[k] - 0.004);
    g.fillRect(x, Math.max(0, H - Math.round(MHOLD[k] * H) - 2 * r), bw, 2 * r);
    if (p > 0.999) g.fillRect(x, 0, bw, Math.round(4 * r));
  });
}

function drawWave() {
  const cv = $('#wcanvas');
  const [g, W, H, r] = fit(cv);
  g.fillStyle = C.bg; g.fillRect(0, 0, W, H);
  const wrap = cv.parentElement;
  const t = S ? subject() : null;
  if (!t || !t.loaded) { lightEdges(wrap, false, false); return; }
  const v = viewOf(t);
  const span = v.ve - v.vs;
  const X = (f) => (f - v.vs) / span * W;           // source sample -> device px

  // a different subject, source mode or width may want peaks this view lacks
  const ink = inkId(t) + '@' + W;
  if (ink !== lastInk) { lastInk = ink; scheduleRange(); }

  const [lsF, leF] = liveLoop(t.i, t);
  const x0 = Math.round(X(lsF));
  const x1 = Math.round(X(leF));

  /* Beat grid, behind everything: only the beats in view, and only bars once
     beats crowd closer than a few pixels. Across a whole four-minute file every
     beat was a line two pixels from the last. */
  const bf = beatFrames(t);
  if (bf > 0) {
    const pxBeat = bf / span * W;
    const every = pxBeat >= 4 * r ? 1 : (pxBeat * 4 >= 4 * r ? 4 : 0);
    if (every) {
      g.fillStyle = C.n2;
      const last = Math.floor(v.ve / bf);
      for (let b = Math.ceil(Math.ceil(v.vs / bf) / every) * every; b <= last; b += every) {
        g.globalAlpha = (b % 4 === 0) ? 1 : DIM1;
        g.fillRect(Math.round(X(b * bf)), 0, 1, H);
      }
      g.globalAlpha = 1;
    }
  }

  // excluded ends: the ink layer, dimmed
  const cache = buildWaveCache(t, W, H, v, rangeFor(t), r);
  g.globalAlpha = DIM2;
  g.drawImage(cache.dim, 0, 0);
  g.globalAlpha = 1;

  // the chosen region: the accent layer, clipped to the part of it in view —
  // the samples themselves change colour, nothing is filled behind them
  const c0 = Math.max(0, x0), c1 = Math.min(W, x1);
  if (c1 > c0) {
    g.save();
    g.beginPath();
    g.rect(c0, 0, c1 - c0, H);
    g.clip();
    g.drawImage(cache.hot, 0, 0);
    g.restore();
  }

  // slice markers: aiming points only, they never move a loop point
  const m = t.kind === 'track'
    ? ((S.meta && (S.meta[focus] || S.meta[String(focus)])) || {}) : {};
  if (m.slices) {
    g.fillStyle = C.fg;
    g.globalAlpha = DIM1;
    for (const sl of m.slices) {
      if (sl < v.vs || sl > v.ve) continue;
      const x = Math.round(X(sl));
      g.fillRect(x, 0, 1, 7 * r);
      g.fillRect(x, H - 7 * r, 1, 7 * r);
    }
    g.globalAlpha = 1;
  }

  // the two handles, wherever they are in view
  const hw = Math.max(3, Math.round(4 * r));
  for (const [x, edge] of [[x0, 'in'], [x1, 'out']]) {
    if (x < -hw || x > W + hw) continue;
    const lit = (grabbed === edge) || (handleFocus === edge);
    g.fillStyle = C.accent;
    g.fillRect(edge === 'in' ? x : x - 1, 0, 1, H);
    g.fillRect(edge === 'in' ? x : x - hw, 0, hw, hw * (lit ? 3 : 2));
    g.fillRect(edge === 'in' ? x : x - hw, H - hw * (lit ? 3 : 2), hw,
               hw * (lit ? 3 : 2));
  }

  /* Playhead in ink, not accent: the region already carries accent here, and
     the moving thing has to stay legible on top of it. */
  if (t.phase >= v.vs && t.phase <= v.ve) {
    const xp = Math.round(X(t.phase));
    g.fillStyle = C.bg;
    g.fillRect(xp - Math.round(r), 0, Math.round(r) * 3, H);
    g.fillStyle = C.fg;
    g.fillRect(xp, 0, Math.max(1, Math.round(r)), H);
  }

  g.font = `${T1 * r}px ${FONT_TEXT}`;
  g.fillStyle = C.fg;
  g.globalAlpha = DIM1;
  if (x0 >= 0 && x0 < W) g.fillText(`in ${lsF}`, x0 + 4 * r, H - 6 * r);
  if (x1 > 0 && x1 <= W) {
    const outLabel = `out ${leF}`;
    g.fillText(outLabel, Math.max(0, x1 - g.measureText(outLabel).width - 4 * r), 14 * r);
  }
  g.globalAlpha = 1;

  // the loop runs off an edge of the view: that side's rule lights
  lightEdges(wrap, lsF < v.vs, leF > v.ve);
}

let lastInk = '';
function lightEdges(wrap, left, right) {
  wrap.classList.toggle('off-l', left);
  wrap.classList.toggle('off-r', right);
}

function drawScope() {
  const [g, W, H, r] = fit($('#scanvas'));
  g.fillStyle = C.bg; g.fillRect(0, 0, W, H);
  const n = scope.length / 2;
  // graticule
  g.fillStyle = C.n2;
  for (let k = 1; k < 8; k++) g.fillRect(Math.round(W * k / 8), 0, 1, H);
  g.fillRect(0, Math.round(H / 4), W, 1);
  g.fillRect(0, Math.round(3 * H / 4), W, 1);
  if (!n) return;

  let sLR = 0, sLL = 0, sRR = 0;
  for (const [ch, cy] of [[0, H / 4], [1, 3 * H / 4]]) {
    g.strokeStyle = C.fg;
    g.lineWidth = Math.max(1, r);
    g.beginPath();
    for (let i = 0; i < n; i++) {
      const v = scope[i * 2 + ch] / 127;
      const x = i / (n - 1) * W;
      const y = cy - v * (H / 4) * 0.92;
      i ? g.lineTo(x, y) : g.moveTo(x, y);
    }
    g.stroke();
  }
  for (let i = 0; i < n; i++) {
    const l = scope[i * 2] / 127, rr = scope[i * 2 + 1] / 127;
    sLR += l * rr; sLL += l * l; sRR += rr * rr;
  }
  const corr = (sLL > 0 && sRR > 0) ? sLR / Math.sqrt(sLL * sRR) : 0;
  setText($('#s-corr'), 'CORR ' + (corr >= 0 ? '+' : '') + corr.toFixed(2));

  // goniometer
  const [gg, GW, GH] = fit($('#gonio'));
  gg.fillStyle = C.bg; gg.fillRect(0, 0, GW, GH);
  gg.strokeStyle = C.n2; gg.lineWidth = 1;
  gg.beginPath();
  gg.moveTo(0, 0); gg.lineTo(GW, GH); gg.moveTo(GW, 0); gg.lineTo(0, GH);
  gg.stroke();
  gg.fillStyle = C.fg;
  gg.globalAlpha = DIM1;
  const cx = GW / 2, cy = GH / 2, sc = GW / 2 * 0.86;
  for (let i = 0; i < n; i++) {
    const l = scope[i * 2] / 127, rr = scope[i * 2 + 1] / 127;
    gg.fillRect(cx + (l - rr) * 0.7071 * sc, cy - (l + rr) * 0.7071 * sc, 1, 1);
  }
  gg.globalAlpha = 1;
}

function frame() {
  renderState();
  drawWave();
  const lost = rangeQ.tick(performance.now());   // a reply that never came
  if (lost) sendRange(lost);
  drawScope();
  drawMasterMeter();
  requestAnimationFrame(frame);
}

/* ── waveform interaction ───────────────────────────────────────────── */
/* Handles edit the loop; everything else moves the view. Drag the background
   to pan, scroll to zoom where you point, SHIFT-drag to draw a new loop —
   plain drag used to draw one, and panning took that gesture. Every pixel
   goes through the current view on its way to a sample, so a region set
   while zoomed lands on the samples under the pointer, never on pixels of
   the whole file. */
(function wireWave() {
  const cv = $('#wcanvas');
  let drag = null;
  const px = (e) => {
    const b = cv.getBoundingClientRect();
    return [e.clientX - b.left, b.width];
  };
  const frameAt = (e, t, v) => {
    const [x, W] = px(e);
    return View.xToFrame(x, W, v, t.frames);
  };
  /* Which handle, if either, is within 8 px. The closer one wins; on a region
     narrower than a pixel, the side the pointer is on. */
  const nearHandle = (e, t, v) => {
    const [x, W] = px(e);
    const [ls, le] = liveLoop(t.i, t);
    const xl = View.frameToX(ls, W, v), xr = View.frameToX(le, W, v);
    const dl = Math.abs(x - xl), dr = Math.abs(x - xr);
    if (Math.min(dl, dr) >= 8) return null;
    if (dl === dr) return x <= xl ? 'in' : 'out';
    return dl < dr ? 'in' : 'out';
  };
  const commit = (ls, le) => {
    paintRegion(subject().i, ls, le);           // paint first, always
    sendRegion(ls, le);
  };

  cv.addEventListener('mousemove', (e) => {
    const t = subject();
    if (drag || !t || !t.loaded) return;
    cv.style.cursor = nearHandle(e, t, viewOf(t)) ? 'col-resize'
      : (e.shiftKey ? 'crosshair' : 'grab');
  });

  cv.addEventListener('mousedown', (e) => {
    const t = subject();
    if (!t || !t.loaded) return;
    const v = viewOf(t);
    const edge = nearHandle(e, t, v);
    const [ls, le] = liveLoop(t.i, t);
    if (edge === 'in') { drag = { edge: 'in', le }; handleFocus = 'in'; }
    else if (edge === 'out') { drag = { edge: 'out', ls }; handleFocus = 'out'; }
    else if (e.shiftKey) { drag = { edge: 'new', anchor: frameAt(e, t, v) }; handleFocus = 'out'; }
    else { drag = { edge: 'pan', x: e.clientX, v }; cv.style.cursor = 'grabbing'; }
    if (drag.edge !== 'pan') grabbed = drag.edge;   // lights the handle this frame
    e.preventDefault();
  });

  window.addEventListener('mousemove', (e) => {
    if (!drag || !S) return;
    const t = subject();
    if (!t || !t.loaded) return;
    if (drag.edge === 'pan') {
      // from where the drag began, not from the last move: no drift
      setView(t, View.pan(drag.v, t.frames, px(e)[1], e.clientX - drag.x));
      return;
    }
    const f = frameAt(e, t, viewOf(t));
    if (drag.edge === 'in') commit(Math.min(f, drag.le - 64), drag.le);
    else if (drag.edge === 'out') commit(drag.ls, Math.max(f, drag.ls + 64));
    else commit(Math.min(drag.anchor, f), Math.max(drag.anchor, f));
  });

  window.addEventListener('mouseup', () => {
    const panned = drag && drag.edge === 'pan';
    drag = null;
    grabbed = null;
    if (panned) { cv.style.cursor = 'grab'; return; }
    if (dragLoop) {
      const held = dragLoop;
      // The engine may hold a region change until the next quantum, so keep
      // showing the hand's version until it reports the same numbers. The
      // deadline tracks the quantum: a fixed 4 s would expire mid-wait at
      // 4BAR and snap the readout back to a value the engine is about to
      // replace. It is still a deadline — a dropped socket must not leave
      // the panel showing a number nothing agrees with.
      const grace = Math.max(4000, (S ? S.pending_ms : 0) + 2000);
      const settle = setInterval(() => {
        if (dragLoop !== held) return clearInterval(settle);
        // a key's echo arrives in the pads, not in the track with its number
        const echo = S && (held.kind === 'key' ? S.pads[held.i] : S.tracks[held.i]);
        if (echo && echo.ls === held.ls && echo.le === held.le) {
          dragLoop = null; clearInterval(settle);
        }
      }, 120);
      setTimeout(() => { if (dragLoop === held) dragLoop = null; }, grace);
    }
  });

  /* The wheel zooms around the sample under the pointer; SHIFT, or a sideways
     swipe, pans. Nothing is sent: the next frame plots from what is already
     here, and accurate peaks follow once the view settles. */
  cv.addEventListener('wheel', (e) => {
    const t = subject();
    if (!t || !t.loaded) return;
    e.preventDefault();
    const [x, W] = px(e);
    const unit = e.deltaMode === 1 ? 16 : (e.deltaMode === 2 ? W : 1);
    const dx = e.deltaX * unit, dy = e.deltaY * unit;
    const v = viewOf(t);
    if (e.shiftKey || Math.abs(dx) > Math.abs(dy)) {
      setView(t, View.pan(v, t.frames, W, -(Math.abs(dx) > Math.abs(dy) ? dx : dy)));
    } else {
      setView(t, View.zoomAt(v, t.frames, W, x, Math.pow(2, dy / 240)));
    }
  }, { passive: false });

  cv.addEventListener('dblclick', () => {
    const t = subject();
    if (t && t.loaded) commit(0, t.frames);
  });
})();

/* Arrows nudge the focused handle. Fine is one pixel of the VIEW, so a press
   is always visible and gets finer as you zoom in; coarse is a beat of the
   file's own tempo. (It was one pixel of the whole file whatever the view.) */
function nudgeHandle(dir, coarse) {
  const t = subject();
  if (!t || !t.loaded) return;
  const step = coarse ? Math.round(beatFrames(t)) : View.nudgeStep(viewOf(t), cssW());
  let [ls, le] = liveLoop(t.i, t);
  if (handleFocus === 'in') ls = Math.min(ls + dir * step, le - 64);
  else le = Math.max(le + dir * step, ls + 64);
  paintRegion(t.i, ls, le);                      // paint first
  sendRegion(ls, le);
}

/* ── buttons ────────────────────────────────────────────────────────── */
const ACT = {
  run: () => {
    const v = !(S && settled('transport', S.playing));
    $('[data-act="run"]').setAttribute('aria-pressed', v);   // paint first
    predict('transport', v);
    /* A stop empties the queue either way — launches are cancelled, edits
       fire at once — so predict nought rather than let the rows go on
       promising a START that has already been called off. Writing the DOM
       here would not survive: the render pass runs on the next telemetry
       tick and rebuilds these from the snapshot, which is still the one from
       before the stop. Predicting is the only thing that outlives it, and
       this is a fact about the engine's rule, not a guess. */
    if (!v) predict('pending', 0);
    /* SPACE stops the sound now, not just the clock, so the rows and keys
       stop claiming to play on the same frame — the fades are 6 ms and 4 ms,
       well inside the half-second window these predictions hold for. */
    if (!v && S) {
      S.tracks.forEach((_, i) => predict(`${i}.playing`, false));
      predict('padsOn', false);
    }
    send({ op: 'transport.toggle' });
  },
  rtz:     () => send({ op: 'transport.rewind' }),
  panic:   () => send({ op: 'panic' }),
  tap:     () => send({ op: 'tap' }),
  quantum: () => {
    if (!S) return;
    const next = (S.quantum_i + 1) % QUANTUM_LABELS.length;
    $('#quantum').textContent = QUANTUM_LABELS[next];         // paint first
    predict('quantum', next);
    send({ op: 'transport.quantum', v: next });
  },
  reslice: () => send({ op: 'reslice', i: focus, n: 16 }),
  browse:  () => openBrowser(),
  save:    () => {
    predict('sessionLine', 'saving…', 5000);
    setText($('#session-line'), 'saving…');                 // paint first
    send({ op: 'session.save', views: views.entries() });
  },
  open:    () => openSessions(),
  pick:    () => openPicker(true),
  unload:  () => send({ op: 'unload', i: focus }),
  mappads: () => send({ op: 'pads.map', track: focus, mode: padMode }),
  assign: () => {
    assignArmed = !assignArmed;
    $('[data-act="assign"]').setAttribute('aria-pressed', assignArmed);
  },
  editkeys: () => {
    editKeys = !editKeys;
    $('[data-act="editkeys"]').setAttribute('aria-pressed', editKeys);
    if (!editKeys && focusKind === 'key') setFocus(focus);
    paintKeyFocus();
  },
  help: () => {
    helpMode = !helpMode;
    $('#help-btn').setAttribute('aria-pressed', helpMode);
    applyHelp();
  },
  clearkey: () => {
    if (focusKind !== 'key') { localError = 'No key is being edited.'; return; }
    send({ op: 'pad.clear', i: focusKey });
  },
  'b-up':  () => browseTo($('#b-dir').dataset.up || ''),
  'b-close': () => closeBrowser(),
};
document.addEventListener('mouseup', (e) => {
  const b = e.target.closest('button');
  if (b) b.blur();          // otherwise SPACE re-fires the last button clicked
});
document.addEventListener('click', (e) => {
  const b = e.target.closest('[data-act]');
  if (b && ACT[b.dataset.act]) ACT[b.dataset.act]();
  const m = e.target.closest('[data-mode]');
  if (m) send({ op: 'mode', i: focus, mode: m.dataset.mode });
  const pm = e.target.closest('[data-pmode]');
  if (pm) { padMode = pm.dataset.pmode; send({ op: 'pads.map', track: focus, mode: padMode }); }
});
$$('.head-stats .stat')[5].classList.add('clickable');
$$('.head-stats .stat')[6].classList.add('clickable');
$$('.head-stats .stat')[5].onclick = () => send({ op: 'clip.reset' });
$$('.head-stats .stat')[6].onclick = () => send({ op: 'clip.reset' });
$('#bpm-slider').oninput = (e) => send({ op: 'transport.bpm', v: +e.target.value });
$('#master').oninput = (e) => send({ op: 'master.gain', v: +e.target.value });

/* ── keys ───────────────────────────────────────────────────────────── */
const down = new Set();
window.addEventListener('keydown', (e) => {
  const tgt = e.target;
  if (tgt && tgt.matches && tgt.matches('input, textarea')) return;
  const code = codeOf(e);
  if (e.repeat) return;
  const pi = PAD_CODES.indexOf(code);

  if (e.shiftKey && pi >= 0) {                 // FUNC layer
    e.preventDefault();
    if (pi < 8) {
      const b = $$('#strips .strip')[pi];
      const btn = b && b.querySelector('[data-t="mute"]');
      const v = btn ? btn.getAttribute('aria-pressed') !== 'true' : true;
      if (btn) btn.setAttribute('aria-pressed', v);           // paint first
      predict(`${pi}.mute`, v);
      send({ op: 'track.mute', i: pi, v });
    } else {
      markQueued(pi - 8, 'TOG');
      send({ op: 'track.toggle', i: pi - 8 });
    }
    return;
  }
  if (pi >= 0) {
    e.preventDefault();
    if (e.ctrlKey || e.metaKey || assignArmed) return assignToKey(pi);
    if (editKeys) { focusKind = 'key'; focusKey = pi; paintKeyFocus(); return; }
    down.add(code);
    triggerPad(pi);
    return;
  }
  switch (code) {
    /* Through ACT.run, not straight to the socket: the key used to skip the
       paint-first path the RUN button had, so the most important key on the
       panel was the one control whose press drew nothing until the engine
       answered. */
    case 'Space': e.preventDefault(); ACT.run(); break;
    case 'Digit5': send({ op: 'track.loop.scale', i: focus, v: 0.5 }); break;
    case 'Digit6': send({ op: 'track.loop.scale', i: focus, v: 2.0 }); break;
    case 'Digit7': send({ op: 'track.loop.nudge', i: focus, v: -1 }); break;
    case 'Digit8': send({ op: 'track.loop.nudge', i: focus, v: 1 }); break;
    case 'KeyT': send({ op: 'tap' }); break;
    case 'KeyG': ACT.quantum(); break;
    case 'KeyB': markQueued(focus, 'REV'); send({ op: 'track.rev', i: focus }); break;
    case 'ArrowLeft':  e.preventDefault(); nudgeHandle(-1, e.shiftKey); break;
    case 'ArrowRight': e.preventDefault(); nudgeHandle(+1, e.shiftKey); break;
    case 'ArrowUp':    e.preventDefault(); handleFocus = 'in'; break;
    case 'ArrowDown':  e.preventDefault(); handleFocus = 'out'; break;
    case 'KeyL': e.preventDefault();
      if (e.shiftKey) openPicker(true); else openBrowser();
      break;
    case 'Tab': {
      e.preventDefault();
      const n = S ? S.tracks.length : 8;
      setFocus((focus + (e.shiftKey ? -1 : 1) + n) % n);
      break;
    }
    case 'Escape':
      if (helpMode) ACT.help();
      else if (browserOpen()) closeBrowser();
      else send({ op: 'panic' });
      break;
    /* The view. 9 and 0 sit beside - and =, so fit and zoom are one run of
       four keys next to the loop keys 5-8. F and L, the obvious letters,
       are already a pad and the file browser. None of these reach the engine. */
    case 'Digit9': fitLoopKey(); break;
    case 'Digit0': fitFileKey(); break;
    case 'Equal': case 'NumpadAdd': zoomKey(0.5); break;
    case 'Minus': case 'NumpadSubtract': zoomKey(2); break;
    case 'Slash': if (e.shiftKey) { e.preventDefault(); ACT.help(); } break;
  }
});
window.addEventListener('keyup', (e) => {
  const pi = PAD_CODES.indexOf(codeOf(e));
  if (pi >= 0 && down.has(codeOf(e))) { down.delete(codeOf(e)); releasePad(pi); }
});
window.addEventListener('blur', () => {
  down.forEach(c => releasePad(PAD_CODES.indexOf(c)));
  down.clear();
});

/* ── file browser (replaces the pad grid — no modal) ─────────────────── */
function browserOpen() { return $('#browser').classList.contains('open'); }
function openBrowser() {
  $('#b-title').innerHTML = 'LOAD → TRACK <b id="b-target"></b>';
  $('#b-target').textContent = focus + 1;
  $('[data-act="b-up"]').style.visibility = '';
  $('#browser').classList.add('open');        // paint first
  $('#browser').setAttribute('aria-hidden', 'false');
  browseTo($('#b-dir').dataset.dir || '');
}
function closeBrowser() {
  $('#browser').classList.remove('open');
  $('#browser').setAttribute('aria-hidden', 'true');
}

/* OPEN: the same panel, in the same place, listing saved sets instead of audio.
   Names from disk go in as text, never as markup. */
async function openSessions() {
  $('#b-title').textContent = 'OPEN SESSION';
  $('[data-act="b-up"]').style.visibility = 'hidden';     // keeps its box
  const list = $('#b-list');
  list.innerHTML = '<div class="b-row"><span class="k">—</span><span class="nm"></span></div>';
  list.querySelector('.nm').textContent = 'reading the sessions folder…';
  $('#browser').classList.add('open');                    // paint first
  $('#browser').setAttribute('aria-hidden', 'false');
  let d;
  try {
    d = await (await fetch(`/api/sessions?t=${encodeURIComponent(TOKEN)}`)).json();
  } catch (e) {
    list.querySelector('.nm').textContent = 'Could not reach the engine to list sessions.';
    return;
  }
  $('#b-dir').textContent = d.dir;
  if (!d.sessions.length) {
    list.querySelector('.nm').textContent = 'No sessions saved yet. SAVE writes one here.';
    return;
  }
  list.innerHTML = d.sessions.map(() => `
    <div class="b-row">
      <span class="k"></span><span class="nm"></span><span class="sz"></span><span class="to"></span>
    </div>`).join('');
  list.querySelectorAll('.b-row').forEach((row, k) => {
    const x = d.sessions[k];
    row.querySelector('.k').textContent = x.error ? 'ERR' : 'SET';
    row.querySelector('.nm').textContent = x.error ? x.name + ' — ' + x.error
      : x.name + (x.saved ? '   ' + x.saved.slice(0, 16).replace('T', ' ') : '');
    row.querySelector('.sz').textContent = x.error ? '' : `${x.tracks}T ${x.keys}K`;
    if (x.error) return;
    row.addEventListener('click', () => {
      predict('sessionLine', 'loading ' + x.name, 5000);
      setText($('#session-line'), 'loading ' + x.name);   // paint first
      send({ op: 'session.load', name: x.name });
      closeBrowser();
    });
  });
}

async function browseTo(dir) {
  const r = await fetch(`/api/browse?t=${encodeURIComponent(TOKEN)}&dir=${encodeURIComponent(dir)}`);
  const d = await r.json();
  const el = $('#b-dir');
  el.textContent = d.dir;
  el.dataset.dir = d.dir;
  el.dataset.up = d.up;
  const list = $('#b-list');
  if (d.error) { list.innerHTML = `<div class="b-row"><span></span><span class="nm">${d.error}</span></div>`; return; }
  if (!d.entries.length) {
    list.innerHTML = `<div class="b-row"><span class="k">—</span>
      <span class="nm">No folders and no audio files in here.</span></div>`;
    return;
  }
  list.innerHTML = d.entries.map((x, k) => `
    <div class="b-row" data-k="${k}">
      <span class="k">${x.dir ? 'DIR' : x.ext.slice(1).toUpperCase()}</span>
      <span class="nm">${x.name}</span>
      <span class="sz">${x.dir ? '' : bytes(x.size)}</span>
      <span class="to">${x.dir ? '' :
        Array.from({ length: S.tracks.length }, (_, i) =>
          `<button class="btn" data-load="${i}">${i + 1}</button>`).join('')}</span>
    </div>`).join('');
  list.querySelectorAll('.b-row').forEach((row, k) => {
    const x = d.entries[k];
    row.addEventListener('click', (ev) => {
      const lb = ev.target.closest('[data-load]');
      if (lb) { send({ op: 'load', i: +lb.dataset.load, path: x.path }); closeBrowser(); return; }
      if (x.dir) browseTo(x.path);
      else { send({ op: 'load', i: focus, path: x.path }); closeBrowser(); }
    });
  });
}

/* ── drop ───────────────────────────────────────────────────────────── */
['dragover', 'drop'].forEach(t =>
  window.addEventListener(t, e => { e.preventDefault(); e.stopPropagation(); }));
window.addEventListener('drop', async (e) => {
  const strip = e.target.closest('.strip');
  let target = strip ? +strip.dataset.i : focus;
  for (const f of e.dataTransfer.files) {
    const r = await fetch(`/api/upload?t=${encodeURIComponent(TOKEN)}`, {
      method: 'POST', headers: { 'X-Filename': f.name }, body: await f.arrayBuffer(),
    });
    const d = await r.json();
    if (d.path) send({ op: 'load', i: target, path: d.path });
    target = Math.min(target + 1, (S ? S.tracks.length : 8) - 1);
  }
});

/* ── go ─────────────────────────────────────────────────────────────── */
buildStrips(8);
buildPads(16);
connect();
setInterval(sendProbe, 1000);
requestAnimationFrame(frame);
