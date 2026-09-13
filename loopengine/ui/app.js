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
  }
  return k.charAt(0).toUpperCase() + k.slice(1);
}

let ws = null, S = null, focus = 0, padMode = null;
let focusKind = 'track';   // 'track' | 'key' — what the waveform panel edits
let focusKey = 0;          // which key, when focusKind is 'key'
let assignArmed = false;   // next key pressed takes the focused track's region
let editKeys = false;      // clicking a pad focuses it instead of firing it
const padPeaks = {};       // pad index -> its own envelope
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
  const [ls, le] = liveLoop(focus, t);
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
  for (let i = 0; i < n; i++) {
    const el = document.createElement('div');
    el.className = 'strip empty-row';
    el.dataset.i = i;
    el.innerHTML = `
      <span class="c-n">${i + 1}</span>
      <span class="c-name">—</span>
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
     and how long is left, so a long quantum reads as patience not a hang. */
  setText(pend, S.pending
    ? `${S.pending} queued — fires in ${(S.pending_ms / 1000).toFixed(1)} s`
      + ` (${S.quantum.toLowerCase()})`
    : 'nothing queued');
  pend.classList.toggle('armed', S.pending > 0);
  setText($('#master-db'), dB(S.master));
  if (document.activeElement !== $('#master')) $('#master').value = S.master;

  if ($$('#strips .strip').length !== S.tracks.length) buildStrips(S.tracks.length);
  if ($$('#padgrid .pad').length !== S.pads.length) buildPads(S.pads.length);

  S.tracks.forEach((t, i) => {
    const el = $$('#strips .strip')[i];
    if (!el) return;
    el.classList.toggle('empty-row', !t.loaded);
    el.classList.toggle('playing', t.playing);
    setText(el.querySelector('.c-name'),
            i === awaitingPick ? 'waiting for the file dialog'
                               : (t.loaded ? t.name : 'empty'));
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
    const queued = settled(`${i}.queued`, t.queued);
    setText(q, queued ? queued.slice(0, 3) : (t.playing ? '▮' : ''));
    q.classList.toggle('armed', !!queued);
    if (lastFired[i] !== undefined && t.fired !== lastFired[i]) {
      el.classList.remove('fired');
      void el.offsetWidth;
      el.classList.add('fired');
    }
    lastFired[i] = t.fired;
    drawTrackMeter(el.querySelector('.c-meter canvas'), i, t);
  });

  S.pads.forEach((p, i) => {
    const el = $$('#padgrid .pad')[i];
    if (!el) return;
    const mapped = p.track >= 0;
    el.classList.toggle('unmapped', !mapped);
    el.classList.toggle('on', !!S.pads_on[i]);
    el.classList.toggle('armed', !!(S.pads_pending && S.pads_pending[i]));
    setText(el.querySelector('.p-label'), mapped
      ? `${p.label}  T${p.track + 1}/S${String(p.slice + 1).padStart(2, '0')}`
      : 'unassigned');
    setText(el.querySelector('.p-mode'), mapped ? p.mode : '—');
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
  const queued = t.kind === 'track'
    ? settled(`${focus}.queued`, t.queued) : null;
  const [ls, le] = liveLoop(t.i, t);
  setText($('#i-title'), t.title);
  setText($('#w-title'), t.title + (t.loaded ? ' — ' + t.name : ''));
  $$('#w-modes .seg-btn').forEach(b =>
    b.setAttribute('aria-pressed', b.dataset.mode === t.mode));

  $('#w-empty').hidden = t.loaded;
  if (!t.loaded) {
    $('#w-empty').innerHTML = t.kind === 'key'
      ? `Key ${PAD_CAPS[focusKey]} holds nothing yet. Set a loop on a track, `
        + `then hold <kbd>CTRL</kbd> and press this key to give it that loop.`
      : `Track ${focus + 1} is empty. Press <kbd>L</kbd> to pick a file, `
        + `or drop one on this panel.`;
  }
  const [rls, rle] = liveLoop(focus, t);
  setText($('#w-in'), t.loaded ? rls.toLocaleString('en-US') : '—');
  setText($('#w-out'), t.loaded ? rle.toLocaleString('en-US') : '—');
  $$('.rgn').forEach((el, k) =>
    el.classList.toggle('aimed', t.loaded && handleFocus === (k ? 'out' : 'in')));
  setText($('#w-info'), !t.loaded ? 'no file'
    : `${t.sr} Hz · ${t.ch} ch · ${secs(t.frames, t.sr)} · `
      + (t.analysing ? 'analysing' :
         `${t.slices} slices` + (m.slice_method ? ` (${m.slice_method})` : '')));

  const err = $('#i-error');
  if (localError) { err.hidden = false; setText(err, localError); }
  else if (m.error) { err.hidden = false; err.textContent = m.error; }
  else if (S.error) { err.hidden = false; err.textContent = S.error; }
  else err.hidden = true;

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
  // Built once. Rewriting innerHTML at 60fps re-lays out the whole column and
  // makes every value twitch; from here on only the text nodes change.
  const host = $('#i-rows');
  if (host.childElementCount !== rows.length * 2) {
    host.innerHTML = rows.map(([k]) => `<dt>${k}</dt><dd></dd>`).join('');
  }
  const dds = host.querySelectorAll('dd');
  rows.forEach(([, v], k) => {
    const str = String(v);
    if (dds[k].textContent !== str) dds[k].textContent = str;
  });
}

/* ── canvases ───────────────────────────────────────────────────────── */
/* The peak plot is the expensive part and it only changes when the file, the
   source mode or the canvas size changes. Plot it once into an offscreen
   canvas and blit that each frame; a drag then moves the region and the
   handles only, and never re-walks the peak array. */
const waveCache = { key: '', dim: null, hot: null };

function waveKey(t, W, H) {
  return [t.kind, t.i, t.mode, t.frames, W, H,
          (t.peaks || []).length].join(':');
}

function plot(W, H, colour, pk) {
  const cv = document.createElement('canvas');
  cv.width = W; cv.height = H;                  // transparent ground: ink only
  const g = cv.getContext('2d');
  if (pk) {
    const n = pk.length / 2, mid = H / 2;
    g.fillStyle = colour;
    for (let x = 0; x < W; x++) {
      const k = Math.min(n - 1, Math.floor(x / W * n));
      const yTop = mid - pk[k * 2 + 1] * mid * 0.92;
      const yBot = mid - pk[k * 2] * mid * 0.92;
      g.fillRect(x, yTop, 1, Math.max(1, yBot - yTop));
    }
  }
  return cv;
}

/* Two layers, both plotted only when the key changes: the excluded ends in
   ink and the chosen region in accent. Clipping one over the other tints the
   samples themselves. Compositing a colour over the region instead would fill
   the whole rectangle, because the ground beneath it is opaque. */
function buildWaveCache(t, W, H) {
  const key = waveKey(t, W, H);
  if (waveCache.key !== key) {
    waveCache.dim = plot(W, H, C.fg, t.peaks);
    waveCache.hot = plot(W, H, C.accent, t.peaks);
    waveCache.key = key;
  }
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
  if (!S) return;
  const t = subject();
  if (!t || !t.loaded) return;
  const [lsF, leF] = liveLoop(t.i, t);
  const x0 = Math.round(lsF / t.frames * W);
  const x1 = Math.round(leF / t.frames * W);

  // beat grid, behind everything
  const bf = beatFrames(t);
  if (bf > 0 && t.frames > 0) {
    const nb = Math.floor(t.frames / bf);
    for (let b = 0; b <= nb; b++) {
      const x = Math.round(b * bf / t.frames * W);
      g.fillStyle = C.n2;
      g.globalAlpha = (b % 4 === 0) ? 1 : DIM1;
      g.fillRect(x, 0, 1, H);
      g.globalAlpha = 1;
    }
  }

  // excluded ends: the ink layer, dimmed
  const cache = buildWaveCache(t, W, H);
  g.globalAlpha = DIM2;
  g.drawImage(cache.dim, 0, 0);
  g.globalAlpha = 1;

  // the chosen region: the accent layer, clipped to it — the samples
  // themselves change colour, nothing is filled behind them
  if (x1 > x0) {
    g.save();
    g.beginPath();
    g.rect(x0, 0, x1 - x0, H);
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
      const x = Math.round(sl / t.frames * W);
      g.fillRect(x, 0, 1, 7 * r);
      g.fillRect(x, H - 7 * r, 1, 7 * r);
    }
    g.globalAlpha = 1;
  }

  // the two handles
  const hw = Math.max(3, Math.round(4 * r));
  for (const [x, edge] of [[x0, 'in'], [x1, 'out']]) {
    const lit = (grabbed === edge) || (handleFocus === edge);
    g.fillStyle = C.accent;
    g.fillRect(edge === 'in' ? x : x - 1, 0, 1, H);
    g.fillRect(edge === 'in' ? x : x - hw, 0, hw, hw * (lit ? 3 : 2));
    g.fillRect(edge === 'in' ? x : x - hw, H - hw * (lit ? 3 : 2), hw,
               hw * (lit ? 3 : 2));
  }

  /* Playhead in ink, not accent: the region already carries accent here, and
     the moving thing has to stay legible on top of it. */
  const xp = Math.round(t.phase / t.frames * W);
  g.fillStyle = C.bg;
  g.fillRect(xp - Math.round(r), 0, Math.round(r) * 3, H);
  g.fillStyle = C.fg;
  g.fillRect(xp, 0, Math.max(1, Math.round(r)), H);

  g.font = `${T1 * r}px ${FONT_TEXT}`;
  g.fillStyle = C.fg;
  g.globalAlpha = DIM1;
  g.fillText(`in ${lsF}`, x0 + 4 * r, H - 6 * r);
  const outLabel = `out ${leF}`;
  g.fillText(outLabel, Math.max(0, x1 - g.measureText(outLabel).width - 4 * r), 14 * r);
  g.globalAlpha = 1;
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
  drawScope();
  drawMasterMeter();
  requestAnimationFrame(frame);
}

/* ── waveform interaction ───────────────────────────────────────────── */
(function wireWave() {
  const cv = $('#wcanvas');
  let drag = null;
  const xToFrame = (e) => {
    const b = cv.getBoundingClientRect();
    const t = subject();
    if (!t) return 0;
    return Math.max(0, Math.min(t.frames,
      Math.round((e.clientX - b.left) / b.width * t.frames)));
  };
  const commit = (ls, le) => {
    paintRegion(subject().i, ls, le);           // paint first, always
    sendRegion(ls, le);
  };

  cv.addEventListener('mousemove', (e) => {
    const t = subject();
    if (drag || !t || !t.loaded) return;
    const b = cv.getBoundingClientRect();
    const grab = 8 / b.width * t.frames;
    const f = xToFrame(e);
    const [ls, le] = liveLoop(t.i, t);
    cv.style.cursor = (Math.abs(f - ls) < grab || Math.abs(f - le) < grab)
      ? 'col-resize' : 'crosshair';
  });

  cv.addEventListener('mousedown', (e) => {
    const t = subject();
    if (!t || !t.loaded) return;
    const b = cv.getBoundingClientRect();
    const grab = 8 / b.width * t.frames;
    const f = xToFrame(e);
    const [ls, le] = liveLoop(t.i, t);
    if (Math.abs(f - ls) < grab) { drag = { edge: 'in', le }; handleFocus = 'in'; }
    else if (Math.abs(f - le) < grab) { drag = { edge: 'out', ls }; handleFocus = 'out'; }
    else { drag = { edge: 'new', anchor: f }; handleFocus = 'out'; }
    grabbed = drag.edge;                        // lights the handle this frame
    e.preventDefault();
  });

  window.addEventListener('mousemove', (e) => {
    if (!drag || !S) return;
    const f = xToFrame(e);
    if (drag.edge === 'in') commit(Math.min(f, drag.le - 64), drag.le);
    else if (drag.edge === 'out') commit(drag.ls, Math.max(f, drag.ls + 64));
    else commit(Math.min(drag.anchor, f), Math.max(drag.anchor, f));
  });

  window.addEventListener('mouseup', () => {
    drag = null;
    grabbed = null;
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
        const t = S && S.tracks[held.i];
        if (t && t.ls === held.ls && t.le === held.le) {
          dragLoop = null; clearInterval(settle);
        }
      }, 120);
      setTimeout(() => { if (dragLoop === held) dragLoop = null; }, grace);
    }
  });

  cv.addEventListener('dblclick', () => {
    const t = subject();
    if (t && t.loaded) commit(0, t.frames);
  });
})();

/* Arrows nudge the focused handle. Fine is one pixel of the plot so a press
   is always visible; coarse is a beat of the file's own tempo. */
function nudgeHandle(dir, coarse) {
  const t = subject();
  if (!t || !t.loaded) return;
  const W = $('#wcanvas').getBoundingClientRect().width || 1;
  const step = coarse ? Math.round(beatFrames(t))
                      : Math.max(1, Math.round(t.frames / W));
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
    case 'Space': e.preventDefault(); send({ op: 'transport.toggle' }); break;
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
      if (browserOpen()) closeBrowser(); else send({ op: 'panic' });
      break;
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
  $('#b-target').textContent = focus + 1;
  $('#browser').classList.add('open');        // paint first
  $('#browser').setAttribute('aria-hidden', 'false');
  browseTo($('#b-dir').dataset.dir || '');
}
function closeBrowser() {
  $('#browser').classList.remove('open');
  $('#browser').setAttribute('aria-hidden', 'true');
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
