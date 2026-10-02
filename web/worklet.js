/* The audio thread, in the browser.

   This is loopengine/dsp.py, track.py, voice.py and transport.py, rewritten
   to run inside an AudioWorkletProcessor. The Python reads as vectorised
   numpy because a per-sample Python loop would be a hundred times too slow;
   here the opposite is true, so the same arithmetic is written as plain
   scalar loops over the block. The numbers are the same numbers: Catmull-Rom
   through four wrapped taps, an equal-power seam blend, a linear gain ramp
   across the block, phase kept in float64 and wrapped by modulo.

   Nothing here allocates per block. Every buffer it needs is made once, when
   a file arrives or the block size changes.

   It owns the clock, the eight tracks and the sixteen voices. The main
   thread owns everything else and talks to it only through messages. */

const MIN_LOOP = 64;
const CUES = 8;
const STOP_FLOOR = 1e-4;
const QUANTA = [0.0, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0];

/* ── scratch ───────────────────────────────────────────────────────────
   One of these per voice and per track, sized to the block. */
class Work {
  constructor(n) {
    this.n = n;
    this.rel = new Float64Array(n);
    this.yl = new Float32Array(n);
    this.yr = new Float32Array(n);
  }
}

function catmull(p0, p1, p2, p3, t) {
  const a = 0.5 * (p3 - p0) + 1.5 * (p1 - p2);
  const b = p0 - 2.5 * p1 + 2.0 * p2 - 0.5 * p3;
  const c = 0.5 * (p2 - p0);
  return ((a * t + b) * t + c) * t + p1;
}

/* `n` interpolated frames from `phase`, wrapped inside [ls, ls+L).
   `step` may be negative (reverse) or fractional (varispeed, or a file whose
   rate is not the graph's). The neighbour taps wrap inside the loop too, so
   the interpolator never reads across the seam into unrelated material.
   Writes w.yl/w.yr and leaves the wrapped positions in w.rel for the seam. */
function loopGather(L_ch, R_ch, ls, L, phase, step, n, w) {
  const rel = w.rel, yl = w.yl, yr = w.yr;
  const mono = R_ch === null;
  for (let k = 0; k < n; k++) {
    let r = (phase - ls) + k * step;
    r = r % L;
    if (r < 0) r += L;                    // JS % keeps the sign; numpy's does not
    rel[k] = r;
    const i0 = Math.floor(r);
    const t = r - i0;
    const im = ls + ((i0 - 1 + L) % L);
    const i1 = ls + i0;
    const i2 = ls + ((i0 + 1) % L);
    const i3 = ls + ((i0 + 2) % L);
    yl[k] = catmull(L_ch[im], L_ch[i1], L_ch[i2], L_ch[i3], t);
    yr[k] = mono ? yl[k]
                 : catmull(R_ch[im], R_ch[i1], R_ch[i2], R_ch[i3], t);
  }
}

/* Equal-power crossfade over the loop seam. For the first `xf` samples after
   the wrap we mix in the material that would have played had the loop kept
   running. The loop length is preserved — this is a post-fade, not a short
   loop. Linear is fine over five milliseconds. */
function seamBlend(L_ch, R_ch, ls, L, xf, frames, n, w) {
  if (xf <= 0) return;
  const rel = w.rel, yl = w.yl, yr = w.yr;
  const mono = R_ch === null;
  for (let k = 0; k < n; k++) {
    const r = rel[k];
    if (r >= xf) continue;
    let tail = (ls + L + r) | 0;
    if (tail < 0) tail = 0; else if (tail > frames - 1) tail = frames - 1;
    const ph = (r / xf) * (Math.PI / 2);
    const s = Math.sin(ph), c = Math.cos(ph);
    yl[k] = yl[k] * s + L_ch[tail] * c;
    yr[k] = yr[k] * s + (mono ? L_ch[tail] : R_ch[tail]) * c;
  }
}

/* Non-looping gather, clamped at the buffer edges. */
function shotGather(L_ch, R_ch, phase, step, n, w) {
  const yl = w.yl, yr = w.yr;
  const mono = R_ch === null;
  const hi = L_ch.length - 1;
  const cl = (i) => (i < 0 ? 0 : i > hi ? hi : i);
  for (let k = 0; k < n; k++) {
    const pos = phase + k * step;
    const i0 = Math.floor(pos);
    const t = pos - i0;
    const a = cl(i0 - 1), b = cl(i0), c = cl(i0 + 1), d = cl(i0 + 2);
    yl[k] = catmull(L_ch[a], L_ch[b], L_ch[c], L_ch[d], t);
    yr[k] = mono ? yl[k] : catmull(R_ch[a], R_ch[b], R_ch[c], R_ch[d], t);
  }
}

/* Constant-power pan, pan in [-1, 1]. */
function panGains(pan) {
  const a = (pan + 1.0) * 0.25 * Math.PI;
  return [Math.cos(a), Math.sin(a)];
}

/* ── transport ─────────────────────────────────────────────────────────
   Sample-counted, never wall-clock. */
class Transport {
  constructor(sr, bpm = 124.0) {
    this.sr = sr;
    this.bpm = bpm;
    this.beatsPerBar = 4;
    this.pos = 0;
    this.playing = false;
    this.quantumI = 5;            // BAR
  }
  get spb() { return 60.0 / this.bpm * this.sr; }
  get quantumBeats() { return QUANTA[this.quantumI]; }
  barsBeats() {
    const b = this.pos / this.spb;
    const bar = Math.floor(b / this.beatsPerBar);
    return [bar + 1, b - bar * this.beatsPerBar + 1.0];
  }
  /* 0 when we sit on a quantum edge, else frames until the next one. A
     stopped clock has no edges: pos does not advance, so a queue waiting on
     a boundary would wait for ever and then fire on restart over whatever
     the player edited meanwhile. So every moment is an edge when stopped. */
  framesToBoundary() {
    if (!this.playing) return 0;
    const q = this.quantumBeats;
    if (q <= 0.0) return 0;
    const qs = this.spb * q;
    const r = this.pos % qs;
    if (r < 1.0 || (qs - r) < 1.0) return 0;
    return Math.max(1, Math.ceil(qs - r));
  }
  advance(n) { if (this.playing) this.pos += n; }
  setBpm(b) { this.bpm = Math.max(40.0, Math.min(220.0, b)); }
}

/* ── track ─────────────────────────────────────────────────────────────
   All positions are in source samples, phase in float64. The file's own rate
   is folded into the step, so nothing is resampled on load: a 44.1k loop on
   a 48k graph plays at step 0.91875 through the varispeed interpolator. */
class Track {
  constructor(index, blocksize) {
    this.index = index;
    this.w = new Work(blocksize);
    this.name = ''; this.path = '';
    this.sr = 48000; this.frames = 0; this.channels = 0;
    this.L = null; this.R = null;            // the playing buffer
    this.srcL = null; this.srcR = null;      // STEREO, always kept
    this.variants = {};                      // mode -> [L, R]
    this.mode = 'STEREO';
    this.loopStart = 0; this.loopEnd = 0; this.phase = 0.0;
    this.playing = false; this.reverse = false;
    this.mute = false; this.solo = false;
    this.gain = 0.65; this.pan = 0.0; this.speed = 1.0;
    this.xfade = 0; this.nslices = 0;
    this.bpm = 0.0; this.bpmConf = 0.0;
    this.peak = 0.0; this.rms = 0.0;
    this._g = 0.0; this.stopping = false;
    const [gl, gr] = panGains(0.0); this._gl = gl; this._gr = gr;
    this.fired = 0; this.queued = null; this.analysing = false;
    this.cues = new Int32Array(CUES).fill(-1);
  }

  load(L, R, sr, name, path, bpm, conf, nslices, xfadeMs = 6.0, analysing = false) {
    this.srcL = L; this.srcR = R;
    this.variants = { STEREO: [L, R] };
    this.L = L; this.R = R;
    this.stopping = false; this.mode = 'STEREO';
    this.sr = sr; this.frames = L.length; this.channels = R ? 2 : 1;
    this.name = name; this.path = path;
    this.bpm = bpm; this.bpmConf = conf; this.nslices = nslices;
    this.analysing = analysing;
    this.loopStart = 0; this.loopEnd = this.frames; this.phase = 0.0;
    this.xfade = Math.floor(xfadeMs * 0.001 * sr);
    this.cues.fill(-1);
  }

  clear() {
    this.playing = false; this.cues.fill(-1);
    this.L = this.R = this.srcL = this.srcR = null;
    this.variants = {}; this.stopping = false;
    this.name = ''; this.path = ''; this.frames = 0;
    this.loopStart = this.loopEnd = 0;
    this.peak = this.rms = 0.0; this.queued = null;
  }

  get loopLen() { return Math.max(0, this.loopEnd - this.loopStart); }

  render(outL, outR, n, engineSr, anySolo) {
    const target = (this.stopping || this.mute || (anySolo && !this.solo))
      ? 0.0 : this.gain;
    if (this.L === null || !this.playing) {
      // keep the smoother running so an unmute mid-fade still lands clean
      this._g += (0.0 - this._g) * 0.35;
      this.peak *= 0.55; this.rms *= 0.55;
      return;
    }
    const L = this.loopLen;
    if (L < MIN_LOOP) {
      if (this.stopping) { this.playing = this.stopping = false; }
      return;
    }
    let step = this.speed * (this.sr / engineSr);
    if (this.reverse) step = -step;

    const w = this.w;
    loopGather(this.L, this.R, this.loopStart, L, this.phase, step, n, w);
    const xf = Math.min(this.xfade, (L / 4) | 0,
                        Math.max(0, this.frames - this.loopEnd));
    if (xf > 0 && !this.reverse)
      seamBlend(this.L, this.R, this.loopStart, L, xf, this.frames, n, w);

    // Linear gain ramp across the block: about 6 ms to target, no clicks.
    const g0 = this._g;
    this._g = g0 + (target - g0) * Math.min(1.0, n / (0.006 * engineSr));
    const dg = (this._g - g0) / n;
    const yl = w.yl, yr = w.yr;
    let pk = 0.0, sum = 0.0;
    for (let k = 0; k < n; k++) {
      const g = g0 + dg * k;
      const a = yl[k] * g, b = yr[k] * g;
      outL[k] += a * this._gl;
      outR[k] += b * this._gr;
      const aa = a < 0 ? -a : a, ab = b < 0 ? -b : b;
      if (aa > pk) pk = aa;
      if (ab > pk) pk = ab;
      sum += a * a + b * b;
    }
    this.peak = pk > this.peak ? pk : this.peak * 0.72;
    this.rms = n ? Math.sqrt(sum / (n * 2)) : 0.0;

    let p = (this.phase - this.loopStart + step * n) % L;
    if (p < 0) p += L;
    this.phase = this.loopStart + p;

    /* A stop is a fade, not a cut. Dropping `playing` mid-waveform stepped
       the output by many times the largest step in the audio itself — a
       click on every stop. So a stopping track keeps rendering toward zero
       through the same smoother mute uses, and halts once it lands. */
    if (this.stopping && this._g < STOP_FLOOR) {
      this.playing = this.stopping = false; this._g = 0.0;
    }
  }

  setPan(pan) {
    this.pan = Math.max(-1.0, Math.min(1.0, pan));
    const [gl, gr] = panGains(this.pan); this._gl = gl; this._gr = gr;
  }

  /* Every degenerate case is defined here rather than trusted to the caller:
     a reversed or empty region becomes MIN_LOOP at the start; a short one is
     widened; one past the end is clamped with the start pulled back; and a
     playhead outside the new region is *wrapped* into it rather than snapped
     to its start, because wrapping is what render does on every pass and so
     stays continuous instead of jumping. */
  setLoop(start, end) {
    if (this.frames < MIN_LOOP) {
      this.loopStart = 0; this.loopEnd = this.frames; return;
    }
    start = Math.round(start); end = Math.round(end);
    if (end <= start) end = start + MIN_LOOP;
    start = Math.max(0, Math.min(start, this.frames - MIN_LOOP));
    end = Math.max(start + MIN_LOOP, Math.min(end, this.frames));
    if (end - start < MIN_LOOP) start = Math.max(0, end - MIN_LOOP);
    this.loopStart = start; this.loopEnd = end;
    const L = end - start;
    if (!(start <= this.phase && this.phase < end)) {
      let p = (this.phase - start) % L;
      if (p < 0) p += L;
      this.phase = start + p;
    }
  }

  scaleLoop(f) {
    const L = Math.max(MIN_LOOP, Math.round(this.loopLen * f));
    this.setLoop(this.loopStart, this.loopStart + L);
  }

  /* Slide the window without changing its length. */
  nudgeLoop(frames) {
    const L = this.loopLen;
    const s = Math.max(0, Math.min(this.frames - L, this.loopStart + Math.round(frames)));
    this.loopStart = s; this.loopEnd = s + L;
  }

  cueSet(c) {
    if (this.L === null || c < 0 || c >= CUES) return;
    this.cues[c] = Math.max(0, Math.min(this.frames - 1, Math.floor(this.phase)));
  }
  cueClear(c) { if (c >= 0 && c < CUES) this.cues[c] = -1; }
  /* Inside the region the playhead moves; outside it the region moves. A cue
     past the loop end is a cue into other material, and putting the playhead
     there would drop it where the loop is about to wrap away from. */
  cueJump(c) {
    if (this.L === null || c < 0 || c >= CUES) return;
    const p = this.cues[c];
    if (p < 0) return;
    if (this.loopStart <= p && p < this.loopEnd) this.phase = p;
    else { this.nudgeLoop(p - this.loopStart); this.phase = this.loopStart; }
  }

  beatFrames(transportBpm) {
    const bpm = this.bpm > 0 ? this.bpm : transportBpm;
    return 60.0 / bpm * this.sr;
  }

  snapshot() {
    return {
      i: this.index, name: this.name, cues: Array.from(this.cues),
      path: this.path, loaded: this.L !== null, playing: this.playing,
      mute: this.mute, solo: this.solo, rev: this.reverse,
      gain: r3(this.gain), pan: r3(this.pan), speed: r4(this.speed),
      mode: this.mode, sr: this.sr, ch: this.channels, frames: this.frames,
      ls: this.loopStart, le: this.loopEnd, phase: Math.floor(this.phase),
      bpm: this.bpm, conf: r2(this.bpmConf), xfade: this.xfade,
      slices: this.nslices, peak: r4(Math.min(1.5, this.peak)),
      rms: r4(Math.min(1.5, this.rms)), queued: this.queued,
      fired: this.fired, analysing: this.analysing,
    };
  }
}

const r2 = (x) => Math.round(x * 100) / 100;
const r3 = (x) => Math.round(x * 1000) / 1000;
const r4 = (x) => Math.round(x * 10000) / 10000;

/* ── voices ────────────────────────────────────────────────────────────
   A small polyphonic pool, so a retrigger overlaps its own tail. */
const ONESHOT = 'ONE', GATE = 'GATE', LOOP = 'LOOP';

class Voice {
  constructor(blocksize) {
    this.w = new Work(blocksize);
    this.active = false; this.pad = -1;
    this.L = null; this.R = null; this.sr = 48000;
    this.start = 0; this.end = 0; this.phase = 0.0; this.step = 1.0;
    this.gain = 1.0; this.gl = 0.7071; this.gr = 0.7071;
    this.mode = ONESHOT; this.held = false; this.stopping = false;
    this.relEnv = 1.0; this.relStep = 0.0; this.age = 0;
  }

  startNote(pad, L, R, sr, start, end, step, gain, pan, mode, engineSr) {
    this.pad = pad; this.L = L; this.R = R; this.sr = sr;
    this.start = start | 0; this.end = end | 0;
    this.phase = start; this.step = step; this.gain = gain;
    const [gl, gr] = panGains(pan); this.gl = gl; this.gr = gr;
    this.mode = mode; this.held = true; this.stopping = false;
    this.relEnv = 1.0;
    // 4 ms release: long enough to kill the click, short enough to feel hard.
    this.relStep = 1.0 / Math.max(1.0, 0.004 * engineSr);
    this.active = true; this.age = 0;
  }

  release() { this.held = false; }
  /* Stop through the release ramp whatever the mode. A one-shot normally
     ignores release and plays to its end; a transport stop means now. */
  fadeOut() { this.held = false; this.stopping = true; }

  render(outL, outR, n, engineSr) {
    if (!this.active) return 0.0;
    const L = this.end - this.start;
    if (L < 32) { this.active = false; return 0.0; }

    const w = this.w;
    let done = false;
    if (this.mode === LOOP) {
      loopGather(this.L, this.R, this.start, L, this.phase, this.step, n, w);
      let p = (this.phase - this.start + this.step * n) % L;
      if (p < 0) p += L;
      this.phase = this.start + p;
    } else {
      shotGather(this.L, this.R, this.phase, this.step, n, w);
      const endPhase = this.phase + this.step * n;
      done = endPhase >= this.end;
      if (done) {                       // zero whatever sits past the slice end
        const over = Math.max(0, Math.ceil((endPhase - this.end) / this.step)) | 0;
        if (over > 0 && over <= n)
          for (let k = n - over; k < n; k++) { w.yl[k] = 0; w.yr[k] = 0; }
      }
      this.phase = endPhase;
    }

    let e0 = 1.0, de = 0.0, ramped = false;
    if (!this.held && (this.mode !== ONESHOT || this.stopping)) {
      e0 = this.relEnv;
      this.relEnv = Math.max(0.0, e0 - this.relStep * n);
      de = (this.relEnv - e0) / n;
      ramped = true;
      if (this.relEnv <= 0.0) this.active = false;
    }

    const yl = w.yl, yr = w.yr, g = this.gain;
    let pk = 0.0;
    for (let k = 0; k < n; k++) {
      const env = ramped ? (e0 + de * k) : 1.0;
      const a = yl[k] * g * env, b = yr[k] * g * env;
      outL[k] += a * this.gl;
      outR[k] += b * this.gr;
      const aa = a < 0 ? -a : a, ab = b < 0 ? -b : b;
      if (aa > pk) pk = aa;
      if (ab > pk) pk = ab;
    }
    if (done && this.mode !== LOOP) this.active = false;
    this.age += n;
    return pk;
  }
}

class VoicePool {
  constructor(count, blocksize) {
    this.voices = [];
    for (let k = 0; k < count; k++) this.voices.push(new Voice(blocksize));
  }
  alloc() {
    for (const v of this.voices) if (!v.active) return v;
    // steal the oldest — a DJ hitting 17 pads at once gets the newest 16
    let best = this.voices[0];
    for (const v of this.voices) if (v.age > best.age) best = v;
    return best;
  }
  releasePad(p) { for (const v of this.voices) if (v.active && v.pad === p) v.release(); }
  fadeAll() { for (const v of this.voices) if (v.active) v.fadeOut(); }
  panic() { for (const v of this.voices) { v.active = false; v.held = false; } }
  padActive(p) { return this.voices.some((v) => v.active && v.pad === p); }
  used() { return this.voices.reduce((a, v) => a + (v.active ? 1 : 0), 0); }
}

/* ── pads ──────────────────────────────────────────────────────────────
   A key slot holds its OWN audio and its own region, so loading a new file
   on a track cannot destroy what a key points at. */
class Pad {
  constructor() {
    this.L = null; this.R = null;
    this.sr = 48000; this.channels = 0; this.frames = 0;
    this.name = ''; this.path = ''; this.label = '';
    this.track = -1; this.slice = -1; this.source = 'STEREO';
    this.mode = ONESHOT; this.gain = 1.0; this.pan = 0.0;
    this.quantize = false; this.reverse = false; this.speed = 1.0;
    this.loopStart = 0; this.loopEnd = 0;
  }
  get loaded() { return this.L !== null && this.loopEnd > this.loopStart; }
  assign(L, R, sr, name, path, ls, le, track = -1, sliceI = -1) {
    this.L = L; this.R = R; this.sr = sr | 0;
    this.channels = R ? 2 : 1; this.frames = L.length;
    this.name = name; this.path = path;
    this.track = track; this.slice = sliceI;
    this.setLoop(ls, le);
  }
  clear() {
    this.L = this.R = null; this.frames = 0;
    this.loopStart = this.loopEnd = 0;
    this.name = ''; this.path = ''; this.label = '';
    this.track = -1; this.slice = -1; this.source = 'STEREO';
  }
  /* The same guards a track uses: a key's region is edited the same way. */
  setLoop(ls, le) {
    if (this.L === null) return;
    ls = Math.round(ls); le = Math.round(le);
    if (le <= ls) le = ls + MIN_LOOP;
    ls = Math.max(0, Math.min(ls, this.frames - MIN_LOOP));
    le = Math.max(ls + MIN_LOOP, Math.min(le, this.frames));
    if (le - ls < MIN_LOOP) ls = Math.max(0, le - MIN_LOOP);
    this.loopStart = ls; this.loopEnd = le;
  }
  snapshot(i) {
    return { i, track: this.track, slice: this.slice, mode: this.mode,
             gain: r3(this.gain), pan: r3(this.pan), q: this.quantize,
             label: this.label, loaded: this.loaded, name: this.name,
             sr: this.sr, ch: this.channels, frames: this.frames,
             ls: this.loopStart, le: this.loopEnd, rev: this.reverse,
             source: this.source, speed: r4(this.speed),
             mb: r1(this.L ? (this.L.length * (this.R ? 8 : 4)) / 1048576 : 0) };
  }
}
const r1 = (x) => Math.round(x * 10) / 10;

/* ── the engine ────────────────────────────────────────────────────────
   Owns the clock, the tracks, the pads and the voices, and nothing else.
   Commands arrive as messages and are drained at the top of every block, on
   this thread, in arrival order — which is why a bank switch and a key press
   can never cross. */

const SCOPE_N = 4096;
const QUEUE_CAP = 256;
const QUANTUM_LABELS = ['OFF', '1/16', '1/8', 'BEAT', '1/2', 'BAR', '2BAR', '4BAR'];

// Ops that wait for the next quantum. Everything else lands now.
const QUANTIZED = new Set([
  'track.play', 'track.stop', 'track.toggle', 'track.rev', 'track.retrig',
  'pad.trigger.q', 'track.cue.jump',
  // A region change on a running track lands on the boundary too: moving the
  // points under a moving playhead is what clicks. The panel paints the drag
  // locally, so the hand still gets an answer on the same frame.
  'track.loop', 'track.loop.scale', 'track.loop.nudge', 'track.loop.slice',
]);

/* Of those, the ones that are a MOMENT rather than a state. An edit says how
   a thing should be and can land whenever; a launch says when something
   happens, and with no running clock there is no when — so stopping the clock
   cancels these and keeps the edits. */
const LAUNCH = new Set([
  'track.play', 'track.stop', 'track.toggle', 'track.retrig', 'pad.trigger.q',
  'track.cue.jump',
]);

const QUEUED_LABEL = {
  'track.play': 'START', 'track.stop': 'STOP', 'track.toggle': 'TOGGLE',
  'track.rev': 'REV', 'track.retrig': 'RETRIG', 'track.loop': 'LOOP',
  'track.loop.scale': 'LOOP', 'track.loop.nudge': 'LOOP',
  'track.loop.slice': 'LOOP', 'track.cue.jump': 'CUE',
};

class LoopEngine extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const o = (options && options.processorOptions) || {};
    this.sr = sampleRate;
    this.blocksize = 128;                  // the graph's render quantum
    this.nTracks = o.tracks || 8;
    this.bankSize = 16;
    this.nBanks = o.banks || 4;
    this.memoryLimit = (o.memoryMb || 1024) * 1048576;

    this.transport = new Transport(this.sr, 124.0);
    this.tracks = [];
    for (let k = 0; k < this.nTracks; k++) this.tracks.push(new Track(k, this.blocksize));
    this.pads = [];
    for (let k = 0; k < this.bankSize * this.nBanks; k++) this.pads.push(new Pad());
    this.voices = new VoicePool(o.voices || 16, this.blocksize);
    this.bank = 0;
    this.held = new Int32Array(this.bankSize).fill(-1);

    this.cmds = [];
    this.qOp = new Array(QUEUE_CAP).fill(null);
    this.qKw = new Array(QUEUE_CAP).fill(null);
    this.qN = 0;
    this.queueOverflow = 0;

    this.masterGain = 0.8; this._mg = 0.0;
    this.masterPeak = [0, 0];
    this.clips = 0; this.xruns = 0; this.blocks = 0;
    this.probeId = 0; this.lastError = '';
    this.slices = new Array(this.nTracks).fill(null).map(() => new Int32Array(0));

    this.mixL = new Float32Array(this.blocksize);
    this.mixR = new Float32Array(this.blocksize);
    this.scope = new Int8Array(SCOPE_N);
    this.scopeW = 0;
    this.cpu = 0;
    this.sinceReport = 0;
    this.reportEvery = Math.max(1, Math.round(this.sr / this.blocksize / 30));

    this.port.onmessage = (e) => { this.cmds.push(e.data); };
  }

  // -- addressing --------------------------------------------------------
  slotOf(i) { return this.bank * this.bankSize + (((i | 0) % this.bankSize) + this.bankSize) % this.bankSize; }
  bankSlots() {
    const b = this.bank * this.bankSize, out = [];
    for (let k = 0; k < this.bankSize; k++) out.push(b + k);
    return out;
  }
  padSlot(kw) {
    if (kw.sl === undefined || kw.sl === null) return this.slotOf(kw.i || 0);
    return ((kw.sl | 0) % this.pads.length + this.pads.length) % this.pads.length;
  }
  trackOf(kw) {
    const i = kw.i;
    return (Number.isInteger(i) && i >= 0 && i < this.tracks.length) ? this.tracks[i] : null;
  }

  audioBytes() {
    let n = 0;
    const seen = new Set();
    const add = (L, R) => {
      if (!L || seen.has(L)) return;
      seen.add(L); n += L.byteLength + (R ? R.byteLength : 0);
    };
    for (const t of this.tracks) { add(t.srcL, t.srcR); for (const k in t.variants) add(t.variants[k][0], t.variants[k][1]); }
    for (const p of this.pads) add(p.L, p.R);
    return n;
  }
  wouldExceed(L, R) {
    if (!this.memoryLimit) return false;
    return this.audioBytes() + L.byteLength + (R ? R.byteLength : 0) > this.memoryLimit;
  }

  // -- queue -------------------------------------------------------------
  drain() {
    while (this.cmds.length) {
      const m = this.cmds.shift();
      const op = m.op, kw = m;
      /* Here, not on the sending side: the bank changes on this thread too,
         so resolving in arrival order is the only way a press made after a
         switch cannot land on the bank before it. */
      if (op.slice(0, 4) === 'pad.' && kw.sl === undefined)
        kw.sl = this.slotOf(kw.i || 0);
      if (QUANTIZED.has(op) && this.transport.playing && this.transport.quantumBeats > 0)
        this.enqueue(op, kw);
      else this.apply(op, kw);
    }
  }

  /* Last edit wins. A drag emits a command per pointer move, so without this
     a two-second wait at BAR piles up a hundred redundant region changes that
     all fire at once. The earlier entry for the same (op, index) is removed by
     shifting the rest down; the new one goes to the back, so the order edits
     arrived in is the order they apply. */
  enqueue(op, kw) {
    const ops = this.qOp, kws = this.qKw;
    let n = this.qN;
    const i = kw.sl !== undefined ? kw.sl : kw.i;
    if (i !== undefined && i !== null) {
      let k = 0;
      while (k < n) {
        const ki = kws[k].sl !== undefined ? kws[k].sl : kws[k].i;
        if (ops[k] === op && ki === i) {
          for (let j = k; j < n - 1; j++) { ops[j] = ops[j + 1]; kws[j] = kws[j + 1]; }
          n -= 1; ops[n] = null; kws[n] = null;
        } else k += 1;
      }
    }
    if (n >= QUEUE_CAP) { this.qN = n; this.queueOverflow += 1; this.apply(op, kw); return; }
    ops[n] = op; kws[n] = kw; this.qN = n + 1;
    this.labelPending(op, kw);
  }

  labelPending(op, kw) {
    const i = kw.i;
    if (op.startsWith('track.') && Number.isInteger(i) && i >= 0 && i < this.tracks.length)
      this.tracks[i].queued = QUEUED_LABEL[op] || op;
  }

  relabel() {
    for (const t of this.tracks) t.queued = null;
    for (let k = 0; k < this.qN; k++) this.labelPending(this.qOp[k], this.qKw[k]);
  }

  /* Drop queued moments, keep queued edits. Nothing is marked as fired,
     because nothing did — the labels simply clear, which is the panel saying
     the queue is gone rather than pretending it landed. */
  cancelLaunches() {
    const ops = this.qOp, kws = this.qKw, n = this.qN;
    let w = 0;
    for (let k = 0; k < n; k++) {
      if (!LAUNCH.has(ops[k])) { ops[w] = ops[k]; kws[w] = kws[k]; w += 1; }
    }
    if (w === n) return;
    for (let k = w; k < n; k++) { ops[k] = null; kws[k] = null; }
    this.qN = w;
    this.relabel();
  }

  fire() {
    const ops = this.qOp, kws = this.qKw, n = this.qN;
    this.qN = 0;
    for (let k = 0; k < n; k++) {
      const op = ops[k], kw = kws[k];
      ops[k] = null; kws[k] = null;
      this.apply(op, kw);
    }
    for (const t of this.tracks) if (t.queued !== null) { t.queued = null; t.fired += 1; }
  }

  /* SPACE stops what you hear, not just the clock: tracks fade through their
     gain smoother and keys through their release ramp, so a stop never clicks.
     Starting the clock again does not bring them back. ESC is the hard cut. */
  stopSound() {
    for (const t of this.tracks) if (t.playing) t.stopping = true;
    this.voices.fadeAll();
  }

  // -- the op table ------------------------------------------------------
  apply(op, kw) {
    const tr = this.transport;
    let t = null, p = null, s = 0;
    switch (op) {
      case 'track.load':
        t = this.trackOf(kw);
        /* The ceiling bites here, not at pad.take: taking from a loaded track
           costs nothing, the track already holds that array. Memory grows when
           the TRACK moves on and a key keeps the old buffer alive. So this is
           the moment to refuse, while the old arrangement is still on screen. */
        if (t && this.wouldExceed(kw.L, kw.R)) {
          const pinned = [...new Set(this.pads.filter((q) => q.loaded).map((q) => q.name))].sort();
          this.lastError = `No room to load ${kw.name || 'that file'}. ` +
            `${Math.floor(this.audioBytes() / 1048576)} MB of audio is held against a ` +
            `${Math.floor(this.memoryLimit / 1048576)} MB ceiling, and these keys are pinning ` +
            `files the tracks no longer show: ${pinned.join(', ') || 'none'}. Clear a key.`;
          return;
        }
        if (t) {
          t.load(kw.L, kw.R, kw.sr, kw.name, kw.path, kw.bpm, kw.conf,
                 kw.slices ? kw.slices.length : 0, kw.xfade_ms || 6.0, !!kw.analysing);
          this.slices[t.index] = kw.slices || new Int32Array(0);
        }
        break;
      case 'track.clear':
        t = this.trackOf(kw);
        if (t) { t.clear(); this.slices[t.index] = new Int32Array(0); }
        break;
      case 'track.variant':
        t = this.trackOf(kw);
        if (t) { t.variants[kw.mode] = [kw.L, kw.R]; t.mode = kw.mode; t.L = kw.L; t.R = kw.R; }
        break;
      case 'track.mode':
        t = this.trackOf(kw);
        if (t && t.variants[kw.mode]) {
          t.mode = kw.mode; t.L = t.variants[kw.mode][0]; t.R = t.variants[kw.mode][1];
        }
        break;
      case 'track.play':
        t = this.trackOf(kw);
        if (t && t.L !== null) {
          t.playing = true; t.stopping = false;
          if (kw.retrig !== false) t.phase = t.loopStart;
        }
        break;
      case 'track.stop':
        t = this.trackOf(kw);
        if (t && t.playing) t.stopping = true;        // fades, then halts in render
        break;
      case 'track.toggle':
        t = this.trackOf(kw);
        if (t && t.L !== null) {
          // caught mid-fade: carry on from here — jumping back would click
          if (t.stopping) t.stopping = false;
          else if (t.playing) t.stopping = true;
          else { t.playing = true; t.phase = t.loopStart; }
        }
        break;
      case 'track.retrig':
        t = this.trackOf(kw); if (t) t.phase = t.loopStart; break;
      case 'track.rev':
        t = this.trackOf(kw);
        // a toggle from a key press; a session sets it outright
        if (t) t.reverse = ('v' in kw) ? !!kw.v : !t.reverse;
        break;
      case 'track.mute':
        t = this.trackOf(kw); if (t) t.mute = ('v' in kw) ? !!kw.v : !t.mute; break;
      case 'track.solo':
        t = this.trackOf(kw); if (t) t.solo = ('v' in kw) ? !!kw.v : !t.solo; break;
      case 'track.gain':
        t = this.trackOf(kw); if (t) t.gain = Math.max(0, Math.min(1.4, +kw.v)); break;
      case 'track.pan':
        t = this.trackOf(kw); if (t) t.setPan(+kw.v); break;
      case 'track.speed':
        t = this.trackOf(kw); if (t) t.speed = Math.max(0.25, Math.min(4.0, +kw.v)); break;
      case 'track.loop':
        t = this.trackOf(kw); if (t) t.setLoop(kw.ls, kw.le); break;
      case 'track.loop.scale':
        t = this.trackOf(kw); if (t) t.scaleLoop(+kw.v); break;
      case 'track.loop.nudge':
        t = this.trackOf(kw);
        if (t) t.nudgeLoop(Math.round(kw.v * t.beatFrames(tr.bpm)));
        break;
      case 'track.loop.slice': {
        t = this.trackOf(kw);
        const sl = t ? this.slices[t.index] : null;
        if (t && sl && sl.length) {
          const k = ((kw.v | 0) % sl.length + sl.length) % sl.length;
          const a = sl[k], b = (k + 1 < sl.length) ? sl[k + 1] : t.frames;
          t.setLoop(a, b);
        }
        break;
      }
      case 'track.xfade':
        t = this.trackOf(kw);
        if (t) t.xfade = Math.floor(Math.max(0, Math.min(120, kw.v)) * 0.001 * t.sr);
        break;
      case 'track.analysis':
        /* Second-stage results. Deliberately touches nothing but the display
           fields: analysis never moves a loop point, and the track has been
           playable since the buffer arrived. */
        t = this.trackOf(kw);
        if (t) {
          if (kw.bpm !== undefined) t.bpm = kw.bpm;
          if (kw.conf !== undefined) t.bpmConf = kw.conf;
          if (kw.slices) { this.slices[t.index] = kw.slices; t.nslices = kw.slices.length; }
          t.analysing = false;
        }
        break;
      case 'track.slices':
        t = this.trackOf(kw);
        if (t) { this.slices[t.index] = kw.slices; t.nslices = kw.slices.length; }
        break;

      case 'master.gain': this.masterGain = Math.max(0, Math.min(1.2, +kw.v)); break;
      case 'transport.start': tr.playing = true; break;
      case 'transport.stop':
        tr.playing = false; this.cancelLaunches(); this.stopSound(); break;
      case 'transport.toggle':
        tr.playing = !tr.playing;
        if (!tr.playing) { this.cancelLaunches(); this.stopSound(); }
        break;
      case 'transport.rewind':
        tr.pos = 0;
        for (const q of this.tracks) q.phase = q.loopStart;
        break;
      case 'transport.bpm': tr.setBpm(+kw.v); break;
      case 'transport.quantum': tr.quantumI = ((kw.v | 0) % QUANTA.length + QUANTA.length) % QUANTA.length; break;

      case 'pads.bank':
        // Address only. No slot is touched and no voice is stopped.
        this.bank = (((kw.b | 0) % this.nBanks) + this.nBanks) % this.nBanks;
        break;
      case 'pad.assign':
        // Settings only. Audio comes through pad.take.
        p = this.pads[this.padSlot(kw)];
        for (const f of ['mode', 'gain', 'pan', 'speed', 'quantize', 'label', 'reverse'])
          if (f in kw) p[f] = kw[f];
        break;
      case 'pad.take': {
        /* Snapshot a track's CURRENT buffer and region onto a key. A
           snapshot, not a link: later edits to the track must not reach back
           and change the key. */
        p = this.pads[this.padSlot(kw)];
        t = this.trackOf({ i: kw.track === undefined ? -1 : kw.track });
        if (t === null || t.L === null) {
          this.lastError = 'Nothing to assign — that track is empty.';
        } else if (this.wouldExceed(t.L, t.R)) {
          this.lastError = `Not enough room to pin ${t.name} to a key. ` +
            `${Math.floor(this.audioBytes() / 1048576)} MB of audio is already held ` +
            `and the ceiling is ${Math.floor(this.memoryLimit / 1048576)} MB.`;
        } else {
          const ls = kw.ls === undefined ? t.loopStart : kw.ls | 0;
          const le = kw.le === undefined ? t.loopEnd : kw.le | 0;
          p.assign(t.L, t.R, t.sr, t.name, t.path, ls, le, t.index,
                   kw.slice === undefined ? -1 : kw.slice | 0);
          p.label = kw.label || t.name.split('.')[0].slice(0, 12);
          p.source = t.mode;              // a CTR or SIDE key holds that variant
          this.lastError = '';
        }
        break;
      }
      case 'pad.load':
        /* A key's own audio, put back by a session. Not taken from a track:
           the track it came from may hold something else by now. */
        p = this.pads[this.padSlot(kw)];
        if (this.wouldExceed(kw.L, kw.R)) {
          this.lastError = `No room to put ${kw.name || 'that file'} back on a key.`;
        } else {
          p.assign(kw.L, kw.R, kw.sr | 0, kw.name || '', kw.path || '',
                   kw.ls | 0, kw.le === undefined ? kw.L.length : kw.le | 0);
          p.source = kw.source || 'STEREO';
          p.label = kw.label || (kw.name || '').split('.')[0].slice(0, 12);
          this.lastError = '';
        }
        break;
      case 'pad.loop':
        this.pads[this.padSlot(kw)].setLoop(kw.ls, kw.le); break;
      case 'pad.clear':
        s = this.padSlot(kw);
        this.voices.releasePad(s);
        this.pads[s].clear();
        break;
      case 'pad.trigger':
      case 'pad.trigger.q': {
        s = this.padSlot(kw);
        const pos = ((kw.i | 0) % this.bankSize + this.bankSize) % this.bankSize;
        /* The cap is being pressed again on another bank, so the note it is
           still holding gets its release now rather than never. */
        if (this.held[pos] >= 0 && this.held[pos] !== s) this.voices.releasePad(this.held[pos]);
        this.held[pos] = s;
        this.triggerPad(s);
        break;
      }
      case 'pad.release': {
        /* The slot the press went to, not the one this cap points at now: a
           bank switch while a key is down must not strand the voice. */
        const pos = ((kw.i | 0) % this.bankSize + this.bankSize) % this.bankSize;
        const h = this.held[pos];
        this.voices.releasePad(h < 0 ? this.padSlot(kw) : h);
        this.held[pos] = -1;
        break;
      }
      case 'track.cue.set': t = this.trackOf(kw); if (t) t.cueSet(kw.c | 0); break;
      case 'track.cue.clear': t = this.trackOf(kw); if (t) t.cueClear(kw.c | 0); break;
      case 'track.cue.jump': t = this.trackOf(kw); if (t) t.cueJump(kw.c | 0); break;
      case 'track.cues':
        t = this.trackOf(kw);
        if (t) {
          const cues = kw.cues || [];
          t.cues.fill(-1);
          for (let c = 0; c < Math.min(cues.length, CUES); c++) {
            const v = cues[c] | 0;
            t.cues[c] = (v >= 0 && v < t.frames) ? v : -1;
          }
        }
        break;
      case 'panic':
        /* ESC: the immediate cut. No fade, by design — it is the control for
           when something has to be silent this block, clicks and all. */
        this.voices.panic();
        for (const q of this.tracks) { q.playing = false; q.stopping = false; }
        break;
      case 'probe':
        // stamped on this thread, so an id coming back proves the whole loop
        this.probeId = kw.id | 0;
        break;
      case 'clip.reset': this.clips = 0; this.xruns = 0; break;
    }
  }

  /* Plays the key's own audio. No track is consulted. */
  triggerPad(pi) {
    if (!(pi >= 0 && pi < this.pads.length)) return;
    const p = this.pads[pi];
    if (!p.loaded) return;
    if (p.mode === LOOP && this.voices.padActive(pi)) { this.voices.releasePad(pi); return; }
    let step = p.speed * (p.sr / this.sr);
    if (p.reverse) step = -step;
    const v = this.voices.alloc();
    v.startNote(pi, p.L, p.R, p.sr, p.loopStart, p.loopEnd, step, p.gain, p.pan, p.mode, this.sr);
  }

  // ==================================================================
  // the block
  // ==================================================================
  process(inputs, outputs) {
    const t0 = currentTime;
    const out = outputs[0];
    const outL = out[0], outR = out.length > 1 ? out[1] : out[0];
    const frames = outL.length;
    this.blocks += 1;

    this.drain();

    const mixL = this.mixL, mixR = this.mixR;
    mixL.fill(0, 0, frames); mixR.fill(0, 0, frames);

    /* A queue must never be unreachable. With the clock stopped there are no
       boundaries to wait for, so drain it now rather than let it strand and
       fire on the next start. */
    if (this.qN && !this.transport.playing) this.fire();

    let off = 0, guard = 0;
    while (off < frames && guard < 64) {
      guard += 1;
      let n = frames - off;
      if (this.qN) {
        let d = this.transport.framesToBoundary();
        if (d === 0) { this.fire(); d = this.transport.framesToBoundary(); }
        if (this.qN && d > 0) n = Math.min(n, d);
      }
      this.renderSpan(mixL, mixR, off, n);
      this.transport.advance(n);
      off += n;
    }
    if (off < frames) {
      this.renderSpan(mixL, mixR, off, frames - off);
      this.transport.advance(frames - off);
    }

    // master gain, ramped, then a Padé soft clip: transparent under 0.7,
    // firm above it, never folds.
    const g0 = this._mg;
    this._mg = g0 + (this.masterGain - g0) * Math.min(1.0, frames / (0.008 * this.sr));
    const dg = (this._mg - g0) / frames;
    let pl = 0, pr = 0;
    for (let k = 0; k < frames; k++) {
      const g = g0 + dg * k;
      const a = mixL[k] * g, b = mixR[k] * g;
      const aa = a < 0 ? -a : a, ab = b < 0 ? -b : b;
      if (aa > pl) pl = aa;
      if (ab > pr) pr = ab;
      const a2 = a * a, b2 = b * b;
      let ca = a * (27.0 + a2) / (27.0 + 9.0 * a2);
      let cb = b * (27.0 + b2) / (27.0 + 9.0 * b2);
      if (ca > 1) ca = 1; else if (ca < -1) ca = -1;
      if (cb > 1) cb = 1; else if (cb < -1) cb = -1;
      outL[k] = ca;
      if (outR !== outL) outR[k] = cb;
      // scope ring, mono-summed so one trace shows the master
      this.scope[this.scopeW] = Math.max(-127, Math.min(127, ((ca + cb) * 0.5 * 127) | 0));
      this.scopeW = (this.scopeW + 1) % SCOPE_N;
    }
    this.masterPeak[0] = pl; this.masterPeak[1] = pr;
    if (Math.max(pl, pr) > 0.999) this.clips += 1;

    // telemetry, at about 30 Hz
    this.cpu = 0.9 * this.cpu + 0.1 * ((currentTime - t0) / (frames / this.sr));
    if (++this.sinceReport >= this.reportEvery) {
      this.sinceReport = 0;
      this.port.postMessage({ type: 'state', s: this.snapshot() });
      const sc = new Int8Array(SCOPE_N);
      sc.set(this.scope.subarray(this.scopeW));
      sc.set(this.scope.subarray(0, this.scopeW), SCOPE_N - this.scopeW);
      this.port.postMessage({ type: 'scope', data: sc.buffer }, [sc.buffer]);
    }
    return true;
  }

  renderSpan(mixL, mixR, off, n) {
    if (n <= 0) return;
    const tracks = this.tracks;
    let anySolo = false;
    for (let k = 0; k < tracks.length; k++) if (tracks[k].solo) { anySolo = true; break; }
    const sl = mixL.subarray(off, off + n), sr = mixR.subarray(off, off + n);
    for (let k = 0; k < tracks.length; k++) tracks[k].render(sl, sr, n, this.sr, anySolo);
    const voices = this.voices.voices;
    for (let k = 0; k < voices.length; k++)
      if (voices[k].active) voices[k].render(sl, sr, n, this.sr);
  }

  snapshot() {
    const tr = this.transport;
    const [bar, beat] = tr.barsBeats();
    const slots = this.bankSlots();
    return {
      sr: this.sr,
      blocksize: this.blocksize,
      device: 'Web Audio',
      native_rate: this.sr,
      resampling: false,
      latency_ms: r2(this.outputMs || (this.blocksize / this.sr * 1000)),
      latency_measured: false,
      queue_ms: 0,
      probe_id: this.probeId,
      cpu: r1(this.cpu * 100),
      xruns: this.xruns,
      clips: this.clips,
      playing: tr.playing,
      bpm: r2(tr.bpm),
      bar, beat,
      pos: tr.pos,
      quantum: QUANTUM_LABELS[tr.quantumI],
      quantum_i: tr.quantumI,
      pending: this.qN,
      queue_overflow: this.queueOverflow,
      gc_audio: 0,
      gc_audio_max_ms: 0,
      pending_ms: this.qN ? Math.round(tr.framesToBoundary() / this.sr * 1000) : 0,
      master: r3(this.masterGain),
      mpeak: [r4(this.masterPeak[0]), r4(this.masterPeak[1])],
      voices: this.voices.used(),
      voices_max: this.voices.voices.length,
      audio_mb: r1(this.audioBytes() / 1048576),
      audio_limit_mb: Math.round(this.memoryLimit / 1048576),
      error: this.lastError,
      tracks: this.tracks.map((t) => t.snapshot()),
      bank: this.bank,
      banks: this.nBanks,
      // the sixteen the caps address, by position: the panel paints keys, not
      // slots, and the slot each one stands for is the engine's business
      pads: slots.map((s, k) => this.pads[s].snapshot(k)),
      pads_on: slots.map((s) => this.voices.padActive(s)),
      pads_pending: slots.map((s) => {
        for (let k = 0; k < this.qN; k++)
          if (this.qOp[k] === 'pad.trigger.q' && this.qKw[k].sl === s) return true;
        return false;
      }),
    };
  }
}

registerProcessor('loop-engine', LoopEngine);
