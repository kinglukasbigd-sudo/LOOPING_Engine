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

/* Control round trip: input -> socket -> audio thread -> telemetry -> here.
   The engine stamps `probe` inside the callback itself, so an id coming back
   proves the audio thread saw it — not just that the socket delivered it.
   Timing send() would measure nothing: it returns before anything happens. */
const probe = { id: 0, sentAt: 0, ms: null, hist: [] };
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
      console.log(
`LOOP ENGINE — press-to-speaker path (${L.backend} backend, ${L.samplerate} Hz)
  queue   ${L.queue_p50_ms.toFixed(2)} ms median, ${L.queue_p95_ms.toFixed(2)} p95   post() -> picked up by the callback
  block   ${L.block_ms.toFixed(2)} ms              ${L.blocksize} frames
  output  ${L.output_ms.toFixed(2)} ms              callback -> speaker
  ------
  total   ${L.press_to_speaker_ms.toFixed(2)} ms with the quantiser off
  quantum ${L.quantum_ms.toFixed(0)} ms            musical wait, not latency

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
      S = JSON.parse(ev.data);
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
    } else if (kind === 0x11) {                            // peaks
      const t = v.getUint8(1), n = v.getUint16(2);
      const raw = new Int8Array(ev.data, 4, n * 2);
      const f = new Float32Array(n * 2);
      for (let i = 0; i < n * 2; i++) f[i] = raw[i] / 127;
      peaks[t] = f;
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
function liveLoop(i, t) {
  if (dragLoop && dragLoop.i === i) return [dragLoop.ls, dragLoop.le];
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
  focus = Math.max(0, Math.min(i, (S ? S.tracks.length : 8) - 1));
  const rowH = parseInt(css.getPropertyValue('--row-h'), 10);
  const bar = $('#focusbar');
  if (bar) bar.style.transform = `translateY(${focus * rowH}px)`;
  $$('#strips .strip').forEach((el, k) => el.classList.toggle('focused', k === focus));
}

/* ── pads ───────────────────────────────────────────────────────────── */
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
    b.addEventListener('mousedown', () => triggerPad(i));
    b.addEventListener('mouseup', () => releasePad(i));
    b.addEventListener('mouseleave', () => releasePad(i));
    host.appendChild(b);
  }
}

/* ── render pass ────────────────────────────────────────────────────── */
function renderState() {
  if (!S) return;
  setText($('#h-device'), S.device);
  setText($('#h-rate'), S.sr + ' Hz');
  setText($('#h-block'), S.blocksize);
  setText($('#h-lat'), fx(S.latency_ms, 1) + ' ms');
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
  setText(pend, S.pending
    ? `${S.pending} queued — fires on the ${S.quantum.toLowerCase()}`
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
    setText(el.querySelector('.c-name'), t.loaded ? t.name : 'empty');
    setText(el.querySelector('.c-bpm'), t.loaded ? fx(t.bpm, 1) : '—');
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
  const t = S.tracks[focus];
  const m = (S.meta && (S.meta[focus] || S.meta[String(focus)])) || {};
  const queued = settled(`${focus}.queued`, t.queued);
  const [ls, le] = liveLoop(focus, t);
  setText($('#i-title'), 'TRACK ' + (focus + 1));
  setText($('#w-title'), 'TRACK ' + (focus + 1) + (t.loaded ? ' — ' + t.name : ''));
  $$('#w-modes .seg-btn').forEach(b =>
    b.setAttribute('aria-pressed', b.dataset.mode === t.mode));

  $('#w-empty').hidden = t.loaded;
  if (!t.loaded) {
    $('#w-empty').innerHTML =
      `Track ${focus + 1} is empty. Press <kbd>L</kbd> to pick a file, ` +
      `or drop one on this panel.`;
  }
  setText($('#w-info'), t.loaded
    ? `${t.sr} Hz · ${t.ch} ch · ${secs(t.frames, t.sr)} · ${t.slices} slices` +
      (m.slice_method ? ` (${m.slice_method})` : '')
    : 'no file');

  const err = $('#i-error');
  if (m.error) { err.hidden = false; err.textContent = m.error; }
  else if (S.error) { err.hidden = false; err.textContent = S.error; }
  else err.hidden = true;

  const pan = t.pan === 0 ? 'C'
    : (t.pan < 0 ? 'L' : 'R') + fx(Math.abs(t.pan) * 100, 0);
  const rows = [
    ['file', t.loaded ? t.name : 'empty'],
    ['path', t.loaded ? t.path.replace(/^.*\/([^/]+\/[^/]+)$/, '…/$1') : '—'],
    ['format', t.loaded ? `${t.sr} Hz · ${t.ch} ch` : '—'],
    ['length', t.loaded ? `${t.frames.toLocaleString('en-US')} fr · ${secs(t.frames, t.sr)}` : '—'],
    ['bpm', t.loaded ? `${fx(t.bpm, 2)} · ${fx(t.conf * 100, 0)}% sure` : '—'],
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
  const t = S.tracks[focus];
  if (!t.loaded) return;
  const pk = peaks[focus];
  const mid = H / 2;

  // beat grid
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

  const [lsF, leF] = liveLoop(focus, t);
  const x0 = Math.round(lsF / t.frames * W);
  const x1 = Math.round(leF / t.frames * W);

  // waveform: dim outside the loop, full inside
  if (pk) {
    const n = pk.length / 2;
    for (let x = 0; x < W; x++) {
      const k = Math.min(n - 1, Math.floor(x / W * n));
      const lo = pk[k * 2], hi = pk[k * 2 + 1];
      const inLoop = x >= x0 && x < x1;
      g.globalAlpha = inLoop ? WAVE_INK : DIM2;
      g.fillStyle = C.fg;
      const yTop = mid - hi * mid * 0.92;
      const yBot = mid - lo * mid * 0.92;
      g.fillRect(x, yTop, 1, Math.max(1, yBot - yTop));
    }
    g.globalAlpha = 1;
  }

  // slice ticks
  const m = (S.meta && (S.meta[focus] || S.meta[String(focus)])) || {};
  if (m.slices) {
    g.fillStyle = C.fg;
    g.globalAlpha = DIM1;
    for (const s of m.slices) {
      const x = Math.round(s / t.frames * W);
      g.fillRect(x, 0, 1, 7 * r);
      g.fillRect(x, H - 7 * r, 1, 7 * r);
    }
    g.globalAlpha = 1;
  }

  // loop edges + handles
  g.fillStyle = C.fg;
  g.fillRect(x0, 0, 1, H);
  g.fillRect(x1 - 1, 0, 1, H);
  g.fillRect(x0, 0, 5 * r, 3 * r);
  g.fillRect(x1 - 5 * r, H - 3 * r, 5 * r, 3 * r);

  // playhead — ACCENT
  const xp = Math.round(t.phase / t.frames * W);
  g.fillStyle = C.accent;
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
    const t = S.tracks[focus];
    return Math.max(0, Math.min(t.frames,
      Math.round((e.clientX - b.left) / b.width * t.frames)));
  };
  cv.addEventListener('mousedown', (e) => {
    if (!S || !S.tracks[focus].loaded) return;
    const t = S.tracks[focus];
    const b = cv.getBoundingClientRect();
    const grab = 8 / b.width * t.frames;
    const f = xToFrame(e);
    if (Math.abs(f - t.ls) < grab) drag = { edge: 'in', le: t.le };
    else if (Math.abs(f - t.le) < grab) drag = { edge: 'out', ls: t.ls };
    else drag = { edge: 'new', anchor: f };
    e.preventDefault();
  });
  window.addEventListener('mousemove', (e) => {
    if (!drag || !S) return;
    const f = xToFrame(e);
    let ls, le;
    if (drag.edge === 'in') { ls = Math.min(f, drag.le - 64); le = drag.le; }
    else if (drag.edge === 'out') { ls = drag.ls; le = Math.max(f, drag.ls + 64); }
    else { ls = Math.min(drag.anchor, f); le = Math.max(drag.anchor, f); }
    dragLoop = { i: focus, ls, le };        // paint first
    send({ op: 'track.loop', i: focus, ls, le });
  });
  window.addEventListener('mouseup', () => {
    drag = null;
    // hand back to the engine once it has caught up, never mid-drag
    if (dragLoop) {
      const held = dragLoop;
      setTimeout(() => { if (dragLoop === held) dragLoop = null; }, 200);
    }
  });
  cv.addEventListener('dblclick', () => {
    if (S) send({ op: 'track.loop', i: focus, ls: 0, le: S.tracks[focus].frames });
  });
})();

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
  unload:  () => send({ op: 'unload', i: focus }),
  mappads: () => send({ op: 'pads.map', track: focus, mode: padMode }),
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
    case 'KeyL': e.preventDefault(); openBrowser(); break;
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
