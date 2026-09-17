/* LOOP ENGINE — the waveform panel's VIEW: what you are looking at.

   Pure arithmetic: no DOM, no socket. A view is a window [vs, ve) in source
   samples; a loop is what plays. They are never the same thing — zooming
   changes nothing audible, and nothing in this file reaches the engine.

   Loaded as a classic script by the panel (window.View) and by
   tests/view_check.js under node, so the suite exercises the file that ships
   rather than a copy of its arithmetic. */
(function (root) {
  'use strict';

  /* Past one sample per 4 px there is nothing more to see. */
  const PX_PER_SAMPLE_MAX = 4;

  /* The shortest region the engine keeps — Track.set_loop widens anything
     narrower. The panel applies it too, so a sweep paints the length that is
     about to exist rather than one the engine is about to replace. */
  const MIN_LOOP = 64;

  function minSpan(W) {
    return Math.max(1, (W > 0 ? W : 1) / PX_PER_SAMPLE_MAX);
  }

  function whole(frames) { return { vs: 0, ve: Math.max(0, frames) }; }

  function isWhole(v, frames) { return v.vs <= 0 && v.ve >= frames; }

  /* Inside the file, no narrower than max zoom, no wider than the file.
     Shifting rather than shrinking at the edges keeps a pan a pan. */
  function clamp(v, frames, W) {
    if (!(frames > 0)) return { vs: 0, ve: 0 };
    const lo = Math.min(frames, minSpan(W));
    let span = v.ve - v.vs;
    if (!(span >= lo)) span = lo;           // also repairs NaN and inverted views
    if (span > frames) span = frames;
    let vs = v.vs;
    if (!(vs >= 0)) vs = 0;
    if (vs + span > frames) vs = frames - span;
    return { vs, ve: vs + span };
  }

  /* Zoom by `factor` (below 1 is in) keeping the sample under pixel x under
     pixel x. That holds exactly unless the file's start or end, or a zoom
     limit, forces the window over — then the view stays inside the file and
     the anchor is what gives. */
  function zoomAt(v, frames, W, x, factor) {
    if (!(frames > 0)) return whole(frames);
    if (!(factor > 0)) return clamp(v, frames, W);
    const span = v.ve - v.vs;
    const u = W > 0 ? Math.min(1, Math.max(0, x / W)) : 0.5;
    const anchor = v.vs + u * span;
    const ns = Math.min(frames, Math.max(Math.min(frames, minSpan(W)), span * factor));
    const vs = anchor - u * ns;
    return clamp({ vs, ve: vs + ns }, frames, W);
  }

  /* Drag the material with the pointer: moving right by dx px brings
     earlier samples into view. */
  function pan(v, frames, W, dx) {
    const span = v.ve - v.vs;
    const d = W > 0 ? -dx / W * span : 0;
    return clamp({ vs: v.vs + d, ve: v.ve + d }, frames, W);
  }

  /* Shift the window by whole samples rather than by pixels of the panel: the
     overview strip is the whole file, so a hand moving dx of its width moves
     the view dx of the file. Panning the waveform moves the material with the
     hand; dragging the lit span moves the window with the hand, which is the
     opposite sign and a different scale — hence its own function rather than
     an inverted call to pan(). */
  function panFrames(v, frames, W, df) {
    const d = Number.isFinite(df) ? df : 0;
    return clamp({ vs: v.vs + d, ve: v.ve + d }, frames, W);
  }

  /* The two ends of a sweep, in order, as a region the engine will accept
     unchanged: right-to-left gives exactly what left-to-right gives, and
     nothing shorter than MIN_LOOP survives. Same rule, same order, as
     Track.set_loop — a region the engine would rewrite is a region the panel
     painted wrongly. */
  function sweep(a, b, frames) {
    const n = Number.isFinite(frames) ? Math.max(0, Math.floor(frames)) : 0;
    if (!Number.isFinite(a) || !Number.isFinite(b) || n <= MIN_LOOP) return [0, n];
    const ls = Math.max(0, Math.min(Math.round(Math.min(a, b)), n - MIN_LOOP));
    return [ls, Math.max(ls + MIN_LOOP, Math.min(Math.round(Math.max(a, b)), n))];
  }

  /* The loop, centred, with `margin` of its length either side — the fastest
     route from "roughly there" to tuning the points. */
  function fitLoop(ls, le, frames, W, margin) {
    const m = margin === undefined ? 0.05 : margin;
    const len = Math.max(1, le - ls);
    const span = Math.max(len * (1 + 2 * m), Math.min(frames, minSpan(W)));
    const c = (ls + le) / 2;
    return clamp({ vs: c - span / 2, ve: c + span / 2 }, frames, W);
  }

  /* Pixel -> source sample. Always a whole sample inside the file: this is
     the number a drag hands to the engine, and a pixel must never leak into
     engine state as though it were a sample. */
  function xToFrame(x, W, v, frames) {
    const u = W > 0 ? x / W : 0;
    const f = Math.round(v.vs + u * (v.ve - v.vs));
    return Math.max(0, Math.min(frames, f));
  }

  function frameToX(f, W, v) {
    const span = v.ve - v.vs;
    return span > 0 ? (f - v.vs) / span * W : 0;
  }

  /* One pixel of the VIEW, not of the file — so the arrow keys get finer as
     you zoom in. */
  function nudgeStep(v, W) {
    return Math.max(1, Math.round((v.ve - v.vs) / (W > 0 ? W : 1)));
  }

  /* ── sources of ink ──────────────────────────────────────────────────
     envelope: n buckets of min/max over [start, end). Bucket k covers
               [start + floor(k*span/n), start + floor((k+1)*span/n)) — the
               edges peaks_range uses on the server and, with end = n*hop,
               exactly the edges of the whole-file peaks() taken at load.
     samples:  one value per frame from start. */
  function envelopeSource(data, start, end) {
    return { kind: 'env', data, start, end, n: data.length / 2 };
  }
  function samplesSource(data, start) {
    return { kind: 'samples', data, start, end: start + data.length, n: data.length };
  }
  /* The load-time envelope. peaks() rounds its hop down, so it covers n*hop
     frames; the last few — fewer than one hop — are not in it. */
  function overviewSource(data, frames) {
    const n = data ? data.length / 2 : 0;
    if (!(n > 0) || !(frames > 0)) return null;
    const hop = Math.max(1, Math.floor(frames / n));
    return envelopeSource(data, 0, n * hop);
  }
  function bucketSize(src) { return src ? (src.end - src.start) / src.n : Infinity; }

  /* The bucket holding whole frame f: the largest k with floor(k*span/n) <= f-start. */
  function bucketOf(src, f) {
    const span = src.end - src.start;
    let k = Math.ceil((f - src.start + 1) * src.n / span) - 1;
    if (k < 0) k = 0;
    if (k > src.n - 1) k = src.n - 1;
    return k;
  }

  function reduce(s, f0, f1, out, x) {
    let lo = Infinity, hi = -Infinity;
    if (s.kind === 'samples') {
      for (let f = f0; f <= f1; f++) {
        const y = s.data[f - s.start];
        if (y < lo) lo = y;
        if (y > hi) hi = y;
      }
    } else {
      const k1 = bucketOf(s, f1);
      for (let k = bucketOf(s, f0); k <= k1; k++) {
        if (s.data[2 * k] < lo) lo = s.data[2 * k];
        if (s.data[2 * k + 1] > hi) hi = s.data[2 * k + 1];
      }
    }
    out[2 * x] = lo;
    out[2 * x + 1] = hi;
  }

  /* min/max for each of W columns tiling view v. A column takes EVERY bucket
     that overlaps it: skipping one loses whatever transient was in it. (The
     plot used to take one bucket per pixel, so 2,048 buckets on a ~1,080 px
     panel drew about half of them.) The finest source that covers the whole
     column wins; a column no source fully covers takes whatever part one
     does, so the file's last sliver never leaves a blank column. A column
     holding no whole sample, or no data at all, stays NaN. */
  function columns(v, W, sources) {
    const out = new Float32Array(Math.max(0, W) * 2).fill(NaN);
    const span = v.ve - v.vs;
    if (!(W > 0) || !(span > 0)) return out;
    const spp = span / W;
    const srcs = sources.filter(Boolean).sort((a, b) => bucketSize(a) - bucketSize(b));
    for (let x = 0; x < W; x++) {
      const a = v.vs + x * spp;
      const f0 = Math.ceil(a), f1 = Math.ceil(a + spp) - 1;
      if (f1 < f0) continue;
      let done = false;
      for (const s of srcs) {
        if (f0 >= s.start && f1 < s.end) { reduce(s, f0, f1, out, x); done = true; break; }
      }
      if (done) continue;
      for (const s of srcs) {
        const g0 = Math.max(f0, s.start), g1 = Math.min(f1, s.end - 1);
        if (g1 >= g0) { reduce(s, g0, g1, out, x); break; }
      }
    }
    return out;
  }

  /* Below one sample per pixel an envelope is meaningless — each column holds
     at most one sample — so draw the samples. [x, value] pairs for every
     sample in view plus one either side, so the line runs off both edges
     rather than stopping short. Null unless the samples cover the view. */
  function sampleLine(v, W, src) {
    if (!src || src.kind !== 'samples') return null;
    if (v.vs < src.start || v.ve > src.end) return null;
    const f0 = Math.max(src.start, Math.floor(v.vs) - 1);
    const f1 = Math.min(src.end - 1, Math.ceil(v.ve) + 1);
    const pts = new Float32Array(Math.max(0, f1 - f0 + 1) * 2);
    for (let f = f0, j = 0; f <= f1; f++, j += 2) {
      pts[j] = frameToX(f, W, v);
      pts[j + 1] = src.data[f - src.start];
    }
    return pts;
  }

  /* One view per thing shown — track 3, key W — so zooming into a track,
     focusing a key and coming back finds the track as you left it. A view
     belongs to the file it was set on: load a different file into the slot
     and it starts from the whole file, not a window into audio that is gone. */
  function store() {
    const m = new Map();
    return {
      get(id, sig, frames) {
        const e = m.get(id);
        if (e && e.sig === sig) return e.v;
        const v = whole(frames);
        m.set(id, { sig, v });
        return v;
      },
      peek(id, sig) {
        const e = m.get(id);
        return e && e.sig === sig ? e.v : null;
      },
      set(id, sig, v) { m.set(id, { sig, v }); },
      /* For SAVE: every view, with the file signature it belongs to. */
      entries() {
        const out = {};
        for (const [id, e] of m) out[id] = { sig: e.sig, vs: e.v.vs, ve: e.v.ve };
        return out;
      },
      /* For OPEN: the views a session saved. A loaded session replaces the
         whole set, so the old views go; an entry that is not a real window
         is dropped rather than trusted — the file is plain JSON. */
      restore(obj) {
        m.clear();
        for (const id of Object.keys(obj || {})) {
          const e = obj[id];
          if (e && typeof e.sig === 'string' && Number.isFinite(e.vs)
              && Number.isFinite(e.ve) && e.ve > e.vs && e.vs >= 0) {
            m.set(id, { sig: e.sig, v: { vs: e.vs, ve: e.ve } });
          }
        }
      },
    };
  }

  /* At most one request in flight. Anything asked for meanwhile replaces
     whatever was already waiting, so a fast scroll that settles twenty times
     sends the one in flight and the last one — never twenty. A reply that
     never comes releases the slot after timeoutMs, so a dropped message
     cannot wedge zooming for the rest of the session. */
  function coalescer(timeoutMs) {
    let seq = 0, inflight = null, waiting = null;
    const api = {
      ask(desc, now) {
        if (inflight && now - inflight.at < timeoutMs) { waiting = desc; return null; }
        seq = (seq % 0x7fffffff) + 1;
        inflight = Object.assign({}, desc, { req: seq, at: now });
        waiting = null;
        return inflight;
      },
      answer(req, now) {
        if (!inflight || req !== inflight.req) return { accepted: null, next: null };
        const accepted = inflight;
        inflight = null;
        let next = null;
        if (waiting) { const w = waiting; waiting = null; next = api.ask(w, now); }
        return { accepted, next };
      },
      tick(now) {
        if (inflight && now - inflight.at >= timeoutMs) {
          inflight = null;
          if (waiting) { const w = waiting; waiting = null; return api.ask(w, now); }
        }
        return null;
      },
      busy() { return !!inflight; },
    };
    return api;
  }

  /* A take's length, hh:mm:ss: always eight characters, so no digit moves as
     it grows. Whole seconds; past 99 hours it holds at 99:59:59. */
  function clock(seconds) {
    const v = Number(seconds);
    const s = Number.isFinite(v) ? Math.max(0, Math.min(359999, Math.floor(v))) : 0;
    const two = (n) => (n < 10 ? '0' : '') + n;
    return two(Math.floor(s / 3600)) + ':' + two(Math.floor(s / 60) % 60) + ':' + two(s % 60);
  }

  const View = {
    PX_PER_SAMPLE_MAX, MIN_LOOP, minSpan, whole, isWhole, clamp, zoomAt, pan,
    panFrames, sweep, fitLoop,
    xToFrame, frameToX, nudgeStep, envelopeSource, samplesSource,
    overviewSource, bucketSize, bucketOf, columns, sampleLine, store, coalescer,
    clock,
  };
  if (typeof module === 'object' && module.exports) module.exports = View;
  else root.View = View;
})(typeof window !== 'undefined' ? window : this);
