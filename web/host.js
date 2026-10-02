/* The main thread. Everything the audio thread must not do.

   LOOP ENGINE's panel talks to the engine over a WebSocket: JSON commands up,
   a state snapshot and three kinds of binary frame down. That is the whole
   contract, so this file keeps the contract and throws away the socket. It
   installs a WebSocket that is not a socket, hands the panel the same frames
   the Python server sends, and routes commands to an AudioWorklet instead of
   to a machine on the other end of a wire.

   The panel is not modified. It does not know which engine it is driving.

   Here: files, decoding, analysis, the envelopes the waveform is drawn from,
   sessions, and the one thing a browser adds — audio that may not start until
   the user has touched the page. */

(function () {
  'use strict';

  const MODES = ['STEREO', 'CTR', 'SIDE'];
  const files = new Map();        // "path" -> File, what the picker chose
  const buffers = new Map();      // track/key -> {L, R, sr} kept for analysis
  let ctx = null, node = null, worker = null;
  let listeners = {};             // the fake socket's handlers
  let lastState = null;
  let jobId = 1;
  const pending = new Map();      // worker job id -> what to do with the answer
  let picking = null;             // the track an open file dialog is aiming at
  const sliceCache = new Map();   // track -> the slice edges analysis found
  let taps = [];                  // tap tempo, timed on this thread

  /* ── frames ──────────────────────────────────────────────────────────
     The three binary shapes app.js unpacks, built here exactly as the Python
     server builds them. Peaks go down as signed bytes, a zoomed range as
     signed 16-bit little-endian. */
  function peakFrame(kind, i, env) {
    const n = env.length >> 1;
    const b = new ArrayBuffer(4 + n * 2);
    const v = new DataView(b);
    v.setUint8(0, kind); v.setUint8(1, i); v.setUint16(2, n);
    const out = new Int8Array(b, 4, n * 2);
    for (let k = 0; k < n * 2; k++) {
      let x = Math.round(env[k] * 127);
      out[k] = x > 127 ? 127 : x < -127 ? -127 : x;
    }
    return b;
  }

  function rangeFrame(m, data) {
    const count = m.samples ? data.length : data.length >> 1;
    const vals = data.length;
    const b = new ArrayBuffer(24 + vals * 2);
    const v = new DataView(b);
    v.setUint8(0, 0x13); v.setUint8(1, 0); v.setUint8(2, 0);
    v.setUint8(3, m.samples ? 1 : 0);
    v.setUint32(4, m.req); v.setUint32(8, m.start); v.setUint32(12, m.end);
    v.setUint32(16, m.frames); v.setUint32(20, count);
    for (let k = 0; k < vals; k++) {
      let x = Math.round(data[k] * 32767);
      v.setInt16(24 + 2 * k, x > 32767 ? 32767 : x < -32767 ? -32767 : x, true);
    }
    return b;
  }

  const toPanel = (o) => {
    if (listeners.onmessage)
      listeners.onmessage({ data: typeof o === 'string' ? o : JSON.stringify(o) });
  };
  const toPanelBin = (b) => { if (listeners.onmessage) listeners.onmessage({ data: b }); };

  /* ── audio ───────────────────────────────────────────────────────────
     A browser will not start a sound card until the user has touched the
     page, and refusing to say so is how a looper looks broken. The context is
     built at once so everything downstream exists; the first gesture resumes
     it, and until then the header says what it is waiting for. */
  let suspendedNote = '';
  async function startAudio() {
    ctx = new AudioContext({ latencyHint: 'interactive' });
    await ctx.audioWorklet.addModule('worklet.js');
    node = new AudioWorkletNode(ctx, 'loop-engine', {
      numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [2],
      processorOptions: { tracks: 8, voices: 16, banks: 4, memoryMb: 1024 },
    });
    node.connect(ctx.destination);
    node.port.onmessage = (e) => {
      const m = e.data;
      if (m.type === 'state') {
        lastState = m.s;
        if (suspendedNote) m.s.device = suspendedNote;
        toPanel(m.s);
      } else if (m.type === 'scope') {
        const src = new Int8Array(m.data);
        const b = new ArrayBuffer(4 + src.length);
        new DataView(b).setUint8(0, 0x10);
        new Int8Array(b, 4).set(src);
        toPanelBin(b);
      }
    };

    worker = new Worker('analyze.js');
    worker.onmessage = (e) => {
      const m = e.data;
      const job = pending.get(m.id);
      if (!job) return;
      if (m.job === 'peaks') {
        toPanelBin(peakFrame(job.which === 'key' ? 0x12 : 0x11, job.i,
                             new Float32Array(m.env)));
      } else if (m.job === 'analysis') {
        pending.delete(m.id);
        const pts = new Int32Array(m.slices);
        sliceCache.set(job.i, pts);
        post({ op: 'track.analysis', i: job.i, bpm: m.bpm, conf: m.conf, slices: pts });
      } else if (m.job === 'slices') {
        pending.delete(m.id);
        const pts2 = new Int32Array(m.slices);
        sliceCache.set(job.i, pts2);
        post({ op: 'track.slices', i: job.i, slices: pts2 });
      } else if (m.job === 'range') {
        pending.delete(m.id);
        toPanelBin(rangeFrame(m, new Float32Array(m.data)));
      } else if (m.job === 'variant') {
        pending.delete(m.id);
        post({ op: 'track.variant', i: job.i, mode: m.mode,
               L: new Float32Array(m.L), R: m.R ? new Float32Array(m.R) : null });
      }
    };

    if (ctx.state !== 'running') {
      suspendedNote = 'Web Audio — press any key or click to start the sound card';
      const wake = async () => {
        try { await ctx.resume(); } catch (e) { /* the next gesture tries again */ }
        if (ctx.state === 'running') {
          suspendedNote = '';
          window.removeEventListener('pointerdown', wake, true);
          window.removeEventListener('keydown', wake, true);
        }
      };
      window.addEventListener('pointerdown', wake, true);
      window.addEventListener('keydown', wake, true);
    }
    if (listeners.onopen) listeners.onopen();
  }

  /* Commands to the audio thread. Typed arrays go by transfer where they are
     ours to give away — a decoded file is never touched here again. */
  function post(o, transfer) {
    if (node) node.port.postMessage(o, transfer || []);
  }

  /* ── files ───────────────────────────────────────────────────────────
     A browser has no paths. The picker hands back File objects and the panel
     wants a string it can show and send back, so the name is the path and the
     File lives in a map beside it. Two files with one name get a suffix,
     because the panel keys slots by that string. */
  function remember(file) {
    let path = file.name;
    if (files.has(path) && files.get(path) !== file) {
      let k = 2;
      while (files.has(`${file.name} (${k})`)) k += 1;
      path = `${file.name} (${k})`;
    }
    files.set(path, file);
    return path;
  }

  async function decode(file) {
    const bytes = await file.arrayBuffer();
    const audio = await ctx.decodeAudioData(bytes);
    const L = audio.getChannelData(0);
    const R = audio.numberOfChannels > 1 ? audio.getChannelData(1) : null;
    // decodeAudioData resamples to the context rate, so this IS the file's
    // rate as far as everything downstream is concerned
    return { L: new Float32Array(L), R: R ? new Float32Array(R) : null,
             sr: audio.sampleRate, frames: audio.length };
  }

  async function loadTrack(i, path) {
    const file = files.get(path);
    if (!file) { loadError(i, `${path} is not one of the files you opened.`); return; }
    let a;
    try {
      a = await decode(file);
    } catch (e) {
      loadError(i, `${path} is not audio this browser can decode. ` +
                   `WAV, FLAC, MP3, M4A and OGG all work; some AIFFs do not.`);
      return;
    }
    buffers.set('track:' + i, a);
    /* The buffer goes to the audio thread playable, with the tempo and the
       slices still unknown — exactly as the Python loader does it, so a long
       file is audible in the time it takes to decode rather than to analyse. */
    post({ op: 'track.load', i, L: a.L, R: a.R, sr: a.sr, name: path, path,
           bpm: 0, conf: 0, slices: new Int32Array(0), analysing: true });
    analyse(i, 'track', a);
  }

  function analyse(i, which, a) {
    const id = jobId++;
    pending.set(id, { i, which });
    // copies, because the worker transfers what it is given and the audio
    // thread is now the owner of the originals
    const L = a.L.slice(), R = a.R ? a.R.slice() : null;
    worker.postMessage({ job: 'load', id, i, which, L, R, sr: a.sr },
                       R ? [L.buffer, R.buffer] : [L.buffer]);
  }

  function loadError(i, why) {
    if (lastState) { lastState.error = why; toPanel(lastState); }
  }

  /* ── the ops this side owns ──────────────────────────────────────────
     Everything else is forwarded to the audio thread untouched. */
  function handle(msg) {
    const op = msg.op;
    switch (op) {
      case 'load':
        loadTrack(msg.i | 0, msg.path);
        return;
      case 'unload':
        buffers.delete('track:' + (msg.i | 0));
        post({ op: 'track.clear', i: msg.i | 0 });
        return;
      case 'mode': {
        const a = buffers.get('track:' + (msg.i | 0));
        if (!a || MODES.indexOf(msg.mode) < 0) return;
        if (msg.mode === 'STEREO') { post({ op: 'track.mode', i: msg.i | 0, mode: 'STEREO' }); return; }
        const id = jobId++;
        pending.set(id, { i: msg.i | 0 });
        const L = a.L.slice(), R = a.R ? a.R.slice() : null;
        worker.postMessage({ job: 'variant', id, i: msg.i | 0, mode: msg.mode,
                             L, R, sr: a.sr }, R ? [L.buffer, R.buffer] : [L.buffer]);
        return;
      }
      case 'reslice': {
        const a = buffers.get('track:' + (msg.i | 0));
        if (!a) return;
        const id = jobId++;
        pending.set(id, { i: msg.i | 0 });
        const L = a.L.slice(), R = a.R ? a.R.slice() : null;
        worker.postMessage({ job: 'slice', id, i: msg.i | 0, n: msg.n || 16,
                             L, R, sr: a.sr }, R ? [L.buffer, R.buffer] : [L.buffer]);
        return;
      }
      case 'peaks.range': {
        const a = buffers.get(msg.kind + ':' + (msg.i | 0));
        if (!a) return;
        const id = jobId++;
        pending.set(id, { i: msg.i | 0 });
        const L = a.L.slice(), R = a.R ? a.R.slice() : null;
        worker.postMessage({ job: 'range', id, req: msg.req, kind: msg.kind,
                             i: msg.i | 0, start: msg.start, end: msg.end,
                             buckets: msg.buckets || 2048, L, R, sr: a.sr },
                           R ? [L.buffer, R.buffer] : [L.buffer]);
        return;
      }
      case 'pad.take': {
        /* The key's envelope is the source track's, captured now so it
           survives that track being replaced. */
        const t = msg.track | 0;
        const a = buffers.get('track:' + t);
        const slot = lastState ? (lastState.bank * 16 + ((msg.i | 0) % 16)) : (msg.i | 0);
        const held = lastState && lastState.pads[(msg.i | 0) % 16];
        if (held && held.loaded && !msg.replace) {
          /* Hand-tuned audio goes only when its owner says so. The panel asks
             and its second press carries `replace`. */
          loadError(0, `That key holds ${held.name || 'audio'}. Press it again ` +
                       `to replace it, or clear it first.`);
          return;
        }
        if (a) {
          buffers.set('key:' + slot, a);
          const id = jobId++;
          pending.set(id, { i: (msg.i | 0) % 16, which: 'key' });
          const L = a.L.slice(), R = a.R ? a.R.slice() : null;
          worker.postMessage({ job: 'range', id, req: 0, kind: 'key', i: 0,
                               start: 0, end: a.L.length, buckets: 2048,
                               L, R, sr: a.sr }, R ? [L.buffer, R.buffer] : [L.buffer]);
        }
        post(msg);
        return;
      }
      case 'pads.map': {
        mapPads(msg);
        return;
      }
      case 'tap':
        /* Tap tempo wants a wall clock, and the audio thread has none — it
           counts samples, which is the whole point of it. So the taps are
           timed here and only the tempo they imply crosses over. Two taps
           give a tempo; a gap over two seconds starts again; the result is
           folded into 70..180 so tapping half time still lands. */
        taps = taps.filter((t) => performance.now() - t <= 2000);
        taps.push(performance.now());
        if (taps.length > 5) taps.shift();
        if (taps.length >= 2) {
          const span = (taps[taps.length - 1] - taps[0]) / 1000;
          if (span > 0) {
            let bpm = 60.0 * (taps.length - 1) / span;
            while (bpm < 70) bpm *= 2;
            while (bpm > 180) bpm /= 2;
            post({ op: 'transport.bpm', v: bpm });
          }
        }
        return;
      case 'session.save': saveSession(msg); return;
      case 'session.load': loadSession(msg); return;
      case 'record': toPanel({ op: 'record.done', error:
        'Recording the master is not in the web build yet.' }); return;
      case 'audio.device': return;      // the browser picks the device, not us
      default:
        post(msg);
    }
  }

  /* MAP replaces every key in the showing bank with a file's slices — up to
     sixteen hand-tuned keys in one press. The panel asks first and its second
     press carries `confirm`; this refuses anything else, whatever sent it. */
  function mapPads(msg) {
    const i = msg.track | 0;
    const a = buffers.get('track:' + i);
    if (!a || !lastState) return;
    const taken = lastState.pads.filter((p) => p.loaded).length;
    if (taken && !msg.confirm) {
      loadError(0, `MAP would replace ${taken} assigned key${taken === 1 ? '' : 's'} ` +
                   `with this file's slices. Press MAP again to go ahead.`);
      return;
    }
    const t = lastState.tracks[i];
    if (!t || !t.loaded) return;
    const n = 16;
    const slices = sliceEdges(i, t, n);
    for (let k = 0; k < n; k++) {
      buffers.set('key:' + (lastState.bank * 16 + k), a);
      post({ op: 'pad.take', i: k, track: i, ls: slices[k][0], le: slices[k][1],
             slice: k, label: `${(t.name || '').split('.')[0].slice(0, 9)} ${k + 1}` });
      post({ op: 'pad.assign', i: k, mode: msg.mode || 'ONE' });
    }
    const id = jobId++;
    pending.set(id, { i: 0, which: 'key' });
    const L = a.L.slice(), R = a.R ? a.R.slice() : null;
    worker.postMessage({ job: 'range', id, req: 0, kind: 'key', i: 0, start: 0,
                         end: a.L.length, buckets: 2048, L, R, sr: a.sr },
                       R ? [L.buffer, R.buffer] : [L.buffer]);
  }

  /* The slice edges the engine holds, mirrored here so MAP can cut without
     asking the audio thread for them. Equal division when there are none. */
  function sliceEdges(i, t, n) {
    const pts = sliceCache.get(i);
    const out = [];
    if (pts && pts.length) {
      for (let k = 0; k < n; k++) {
        const a = pts[k % pts.length];
        const b = (k + 1 < pts.length) ? pts[k + 1] : t.frames;
        out.push([a, b]);
      }
    } else {
      const step = Math.max(64, Math.floor(t.frames / n));
      for (let k = 0; k < n; k++) out.push([k * step, Math.min(t.frames, (k + 1) * step)]);
    }
    return out;
  }

  /* ── sessions ────────────────────────────────────────────────────────
     The arrangement, in this browser's own storage. Audio is not in it: a
     page cannot re-open a file it was handed once, so a session names the
     files and says which are missing when it comes back. Everything else —
     regions, gains, modes, cues, the keys, the zooms — is restored exactly. */
  const KEY = 'loop-engine/session';

  function saveSession(msg) {
    if (!lastState) return;
    const s = {
      v: 2,
      bpm: lastState.bpm, quantum_i: lastState.quantum_i,
      master: lastState.master, bank: lastState.bank,
      views: msg.views || {},
      tracks: lastState.tracks.map((t) => ({
        path: t.path, name: t.name, ls: t.ls, le: t.le, gain: t.gain,
        pan: t.pan, speed: t.speed, rev: t.rev, mute: t.mute, solo: t.solo,
        mode: t.mode, xfade: t.xfade, cues: t.cues, playing: t.playing,
      })),
      pads: lastState.pads.map((p) => ({
        i: p.i, label: p.label, mode: p.mode, gain: p.gain, pan: p.pan,
        speed: p.speed, q: p.q, rev: p.rev, ls: p.ls, le: p.le,
        name: p.name, path: p.path, track: p.track, slice: p.slice,
      })),
    };
    try {
      localStorage.setItem(KEY, JSON.stringify(s));
    } catch (e) {
      toPanel({ op: 'session.saved', error:
        'This browser refused to store the session — private windows often do.' });
      return;
    }
    const away = s.tracks.filter((t) => t.path && !files.has(t.path)).map((t) => t.name);
    toPanel({ op: 'session.saved', unsaved: [],
              kept: away.length ? [`the audio for ${away.join(', ')}`] : [] });
  }

  async function loadSession() {
    let s;
    try { s = JSON.parse(localStorage.getItem(KEY) || 'null'); } catch (e) { s = null; }
    if (!s) {
      toPanel({ op: 'session.loaded', error: 'Nothing is saved in this browser yet.' });
      return;
    }
    post({ op: 'transport.bpm', v: s.bpm });
    post({ op: 'transport.quantum', v: s.quantum_i });
    post({ op: 'master.gain', v: s.master });
    const missing = {};
    for (let i = 0; i < s.tracks.length; i++) {
      const t = s.tracks[i];
      if (!t.path) { post({ op: 'track.clear', i }); continue; }
      if (!files.has(t.path)) {
        missing[i] = { name: t.name, reason: 'is not open in this tab — pick it again' };
        continue;
      }
      await loadTrack(i, t.path);
      post({ op: 'track.loop', i, ls: t.ls, le: t.le });
      post({ op: 'track.gain', i, v: t.gain });
      post({ op: 'track.pan', i, v: t.pan });
      post({ op: 'track.speed', i, v: t.speed });
      post({ op: 'track.rev', i, v: t.rev });
      post({ op: 'track.mute', i, v: t.mute });
      post({ op: 'track.solo', i, v: t.solo });
      post({ op: 'track.xfade', i, v: t.xfade / 48.0 });
      post({ op: 'track.cues', i, cues: t.cues });
      if (t.mode !== 'STEREO') handle({ op: 'mode', i, mode: t.mode });
      if (t.playing) post({ op: 'track.play', i });
    }
    for (const p of (s.pads || [])) {
      if (p.name && p.track >= 0 && !missing[p.track])
        post({ op: 'pad.take', i: p.i, track: p.track, ls: p.ls, le: p.le, slice: p.slice });
      post({ op: 'pad.assign', i: p.i, mode: p.mode, gain: p.gain, pan: p.pan,
             speed: p.speed, quantize: p.q, reverse: p.rev, label: p.label });
    }
    toPanel({ op: 'session.loaded', views: s.views || {}, missing });
  }

  /* ── the file dialog ─────────────────────────────────────────────────
     The panel asks over HTTP and the answer comes back on the socket, because
     in the Python build a human is standing at an OS window. A browser's file
     input behaves the same way, so the same two-step is kept. */
  function openDialog(multiple) {
    const input = document.createElement('input');
    input.type = 'file';
    input.multiple = !!multiple;
    input.accept = 'audio/*,.wav,.flac,.mp3,.m4a,.ogg,.aif,.aiff';
    input.style.display = 'none';
    document.body.appendChild(input);
    input.addEventListener('change', () => {
      const paths = Array.from(input.files).map(remember);
      input.remove();
      toPanel({ op: 'picked', track: picking, paths, cancelled: !paths.length });
      picking = null;
    });
    /* A cancelled dialog fires nothing in most browsers, so the window
       regaining focus is the only signal that the human is done. */
    window.addEventListener('focus', function once() {
      window.removeEventListener('focus', once);
      setTimeout(() => {
        if (picking !== null && input.isConnected && !input.files.length) {
          input.remove();
          toPanel({ op: 'picked', track: picking, paths: [], cancelled: true });
          picking = null;
        }
      }, 800);
    });
    input.click();
  }

  /* Files dropped anywhere on the panel land on the focused track and then on
     empty ones, which is what the picker does with a multiple selection. */
  function wireDrop() {
    const stop = (e) => { e.preventDefault(); e.stopPropagation(); };
    window.addEventListener('dragover', stop);
    window.addEventListener('drop', (e) => {
      stop(e);
      const dropped = Array.from(e.dataTransfer.files || []);
      if (!dropped.length) return;
      let aim = 0;
      if (lastState) {
        const empty = lastState.tracks.findIndex((t) => !t.loaded);
        aim = empty < 0 ? 0 : empty;
      }
      toPanel({ op: 'picked', track: aim, paths: dropped.map(remember) });
    });
  }

  /* ── the socket that is not a socket ─────────────────────────────────
     app.js builds one of these, sets three handlers and calls send(). Nothing
     else about a WebSocket is used, so nothing else is implemented. Messages
     are delivered asynchronously, as a real one would. */
  class LocalSocket {
    constructor() {
      this.readyState = 0;
      listeners = this;
      startAudio().then(() => {
        this.readyState = 1;
        if (this.onopen) this.onopen();
      }).catch((err) => {
        this.readyState = 3;
        document.body.classList.add('offline');
        const el = document.querySelector('#h-conn');
        if (el) el.textContent = 'NO AUDIO';
        console.error('LOOP ENGINE could not start Web Audio:', err);
      });
    }
    send(text) {
      let msg;
      try { msg = JSON.parse(text); } catch (e) { return; }
      handle(msg);
    }
    close() { this.readyState = 3; }
  }

  window.WebSocket = LocalSocket;

  /* The panel's two HTTP calls. One opens a file dialog, the other asks for a
     latency report it prints to the console; both are answered here so the
     panel's own code path is unchanged. */
  const realFetch = window.fetch.bind(window);
  window.fetch = async (url, init) => {
    const u = String(url);
    if (u.indexOf('/api/pick') === 0) {
      let body = {};
      try { body = JSON.parse((init && init.body) || '{}'); } catch (e) { /* defaults */ }
      picking = body.track | 0;
      openDialog(!!body.multiple);
      return new Response(JSON.stringify({ available: true }),
                          { headers: { 'Content-Type': 'application/json' } });
    }
    if (u.indexOf('/api/devices') === 0) {
      /* A page cannot choose an output device, and pretending otherwise would
         give the panel a control that silently does nothing. One entry, named
         for what it is, so AUDIO OUT tells the truth instead of sitting
         empty. */
      const sr = ctx ? ctx.sampleRate : 48000;
      return new Response(JSON.stringify({
        devices: [{ index: 0, name: 'the system output (chosen by the browser)',
                    rate: sr, channels: 2, default: true, current: true }],
      }), { headers: { 'Content-Type': 'application/json' } });
    }
    if (u.indexOf('/api/latency') === 0) {
      const bs = 128, sr = ctx ? ctx.sampleRate : 48000;
      const base = ctx ? (ctx.baseLatency || 0) * 1000 : 0;
      const out = ctx ? (ctx.outputLatency || 0) * 1000 : 0;
      const block = bs / sr * 1000;
      return new Response(JSON.stringify({
        backend: 'Web Audio', samplerate: sr, blocksize: bs,
        queue_mean_ms: 0, queue_p50_ms: 0, queue_p95_ms: 0, queue_p99_ms: 0,
        block_ms: block, output_ms: base + out,
        output_ms_source: 'AudioContext baseLatency + outputLatency',
        output_ms_reported: base + out, output_ms_measured: null,
        press_to_speaker_mean_ms: block + base + out,
        press_to_speaker_p95_ms: block + base + out,
        quantum_ms: lastState ? lastState.pending_ms : 0,
      }), { headers: { 'Content-Type': 'application/json' } });
    }
    return realFetch(url, init);
  };

  /* The audio device list the panel shows. A browser does not let a page
     choose an output without permission it has no reason to ask for here, so
     this says so plainly rather than offering a control that does nothing. */
  window.__loopEngineDevices = () => ([
    { index: 0, name: 'the system output', default: true, rate: ctx ? ctx.sampleRate : 48000 },
  ]);

  wireDrop();
})();
