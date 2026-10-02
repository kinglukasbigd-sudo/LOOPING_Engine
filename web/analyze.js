/* Offline analysis. Runs in a worker, never on the audio thread.

   This is the second half of loopengine/dsp.py: the envelope the waveform is
   drawn from, the spectral flux the slice points and the tempo come out of,
   and the mid/side shaping behind the CTR and SIDE buttons. numpy does these
   with whole-array calls; here they are written out, which is longer but the
   same arithmetic.

   There is no FFT in a browser, so there is one below: iterative radix-2
   Cooley-Tukey, real input, which is all any of this needs. */

/* ── FFT ───────────────────────────────────────────────────────────────
   In-place complex transform of (re, im), length a power of two. */
function fft(re, im) {
  const n = re.length;
  for (let i = 1, j = 0; i < n; i++) {           // bit reversal
    let bit = n >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) {
      let t = re[i]; re[i] = re[j]; re[j] = t;
      t = im[i]; im[i] = im[j]; im[j] = t;
    }
  }
  for (let len = 2; len <= n; len <<= 1) {
    const ang = -2 * Math.PI / len;
    const wr = Math.cos(ang), wi = Math.sin(ang);
    for (let i = 0; i < n; i += len) {
      let cr = 1, ci = 0;
      for (let k = 0; k < len / 2; k++) {
        const ur = re[i + k], ui = im[i + k];
        const vr = re[i + k + len / 2] * cr - im[i + k + len / 2] * ci;
        const vi = re[i + k + len / 2] * ci + im[i + k + len / 2] * cr;
        re[i + k] = ur + vr; im[i + k] = ui + vi;
        re[i + k + len / 2] = ur - vr; im[i + k + len / 2] = ui - vi;
        const ncr = cr * wr - ci * wi;
        ci = cr * wi + ci * wr; cr = ncr;
      }
    }
  }
}

/* Magnitudes of the first n/2+1 bins of a real signal. */
function rfftMag(x, out) {
  const n = x.length;
  const re = new Float64Array(n), im = new Float64Array(n);
  re.set(x);
  fft(re, im);
  const half = (n >> 1) + 1;
  for (let k = 0; k < half; k++) out[k] = Math.hypot(re[k], im[k]);
  return out;
}

/* The mix the envelope is drawn from: the channel mean, or the only channel. */
function mono(L, R, start, end) {
  const n = end - start;
  const m = new Float32Array(n);
  if (R) for (let k = 0; k < n; k++) m[k] = (L[start + k] + R[start + k]) * 0.5;
  else m.set(L.subarray(start, end));
  return m;
}

/* min/max envelope, (buckets, 2) interleaved, for the whole file. */
function peaks(L, R, buckets = 2048) {
  const n = L.length;
  if (n === 0) return new Float32Array(buckets * 2);
  const hop = Math.max(1, Math.floor(n / buckets));
  const count = Math.floor(n / hop);
  const out = new Float32Array(count * 2);
  for (let b = 0; b < count; b++) {
    let lo = Infinity, hi = -Infinity;
    const s = b * hop, e = s + hop;
    for (let k = s; k < e; k++) {
      const v = R ? (L[k] + R[k]) * 0.5 : L[k];
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
    out[b * 2] = lo; out[b * 2 + 1] = hi;
  }
  return out;
}

/* The envelope of [start, end) at exactly `buckets` buckets, for a zoomed
   view. The whole-file envelope stretched over a short span has buckets wider
   than the pixels showing them — a blocky outline of the wrong material,
   which looks like it works. Bucket k covers
   [start + k*span/buckets, start + (k+1)*span/buckets), so every sample lands
   in exactly one bucket and none is dropped.

   Once the span holds no more samples than there are buckets it returns the
   samples themselves: the min and max of one sample are that sample, and the
   line through the samples is the thing worth looking at. */
function peaksRange(L, R, start, end, buckets) {
  const n = L.length;
  start = Math.max(0, Math.min(start | 0, n));
  end = Math.max(start, Math.min(end | 0, n));
  buckets = Math.max(1, buckets | 0);
  const span = end - start;
  if (span === 0) return { kind: 'env', data: new Float32Array(0) };
  if (span <= buckets) return { kind: 'samples', data: mono(L, R, start, end) };
  const out = new Float32Array(buckets * 2);
  for (let k = 0; k < buckets; k++) {
    const s = start + Math.floor((k * span) / buckets);
    const e = start + Math.floor(((k + 1) * span) / buckets);
    let lo = Infinity, hi = -Infinity;
    for (let i = s; i < e; i++) {
      const v = R ? (L[i] + R[i]) * 0.5 : L[i];
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
    out[k * 2] = lo; out[k * 2 + 1] = hi;
  }
  return { kind: 'env', data: out };
}

/* Spectral flux: half-wave rectified magnitude rise. The hop widens for long
   files so the envelope length stays bounded — at a fixed hop a four-minute
   track produced a 45000-bin envelope, paid for twice because tempo and
   slicing each asked for it. */
function onsetEnvelope(L, R, sr, win = 1024, hop = 256, maxBins = 16384) {
  const m = mono(L, R, 0, L.length);
  const n = m.length;
  if (n < win * 2) return { env: new Float32Array(1), hop };
  let frames = 1 + Math.floor((n - win) / hop);
  if (frames > maxBins) {
    hop = Math.ceil((n - win) / maxBins);
    frames = 1 + Math.floor((n - win) / hop);
  }
  const w = new Float32Array(win);
  for (let k = 0; k < win; k++) w[k] = 0.5 - 0.5 * Math.cos(2 * Math.PI * k / (win - 1));
  const half = (win >> 1) + 1;
  let prev = new Float64Array(half), cur = new Float64Array(half);
  const buf = new Float32Array(win);
  const env = new Float32Array(frames);
  for (let f = 0; f < frames; f++) {
    const s = f * hop;
    for (let k = 0; k < win; k++) buf[k] = m[s + k] * w[k];
    rfftMag(buf, cur);
    if (f > 0) {
      let sum = 0;
      for (let k = 0; k < half; k++) { const d = cur[k] - prev[k]; if (d > 0) sum += d; }
      env[f - 1] = sum;
    }
    const t = prev; prev = cur; cur = t;
  }
  let mx = 0;
  for (let k = 0; k < env.length; k++) if (env[k] > mx) mx = env[k];
  if (mx > 0) for (let k = 0; k < env.length; k++) env[k] /= mx;
  return { env, hop };
}

/* Transient slice points, in samples. Falls back to equal division and says
   which it did. */
function slicePoints(L, R, sr, want = 16) {
  const n = L.length;
  const { env, hop } = onsetEnvelope(L, R, sr);
  if (env.length > 8) {
    const k = 9, h = k >> 1;
    const picked = [];
    const minGap = Math.floor(0.045 * sr / hop);
    const win = new Float64Array(k);
    for (let i = 0; i < env.length; i++) {
      for (let j = 0; j < k; j++) {
        const q = Math.min(env.length - 1, Math.max(0, i + j - h));
        win[j] = env[q];
      }
      win.sort();
      const thr = win[h] + 0.08;
      const before = env[(i - 1 + env.length) % env.length];
      const after = env[(i + 1) % env.length];
      if (env[i] > thr && env[i] >= before && env[i] >= after) {
        if (!picked.length || i - picked[picked.length - 1] >= minGap) picked.push(i);
      }
    }
    if (picked.length >= Math.max(4, want >> 2)) {
      let pts = picked.map((c) => c * hop);
      if (pts[0] > sr / 100) pts.unshift(0);
      if (pts.length > want) {
        // keep the `want` strongest onsets, then re-sort by time
        const strength = pts.map((p) => env[Math.min(env.length - 1, Math.floor(p / hop))]);
        const order = pts.map((_, i) => i).sort((a, b) => strength[b] - strength[a]).slice(0, want);
        order.sort((a, b) => a - b);
        pts = order.map((i) => pts[i]);
      }
      return { points: Int32Array.from(pts), method: 'transient' };
    }
  }
  const step = Math.max(1, Math.floor(n / want));
  const pts = new Int32Array(want);
  for (let i = 0; i < want; i++) pts[i] = i * step;
  return { points: pts, method: 'equal' };
}

/* Length first, autocorrelation second. A loop is almost always a whole
   number of bars, and tempo does not become more certain with more material,
   so only the first thirty seconds are ever analysed. */
function estimateBpm(L, R, sr, lo = 70.0, hi = 180.0, maxSeconds = 30.0) {
  const n = L.length;
  const dur = n / sr;
  if (dur <= 0.05) return { bpm: 0, conf: 0 };

  let best = null;
  for (const beats of [1, 2, 4, 8, 12, 16, 24, 32, 64]) {
    const bpm = 60.0 * beats / dur;
    if (bpm >= lo && bpm <= hi) {
      const err = Math.abs(bpm - Math.round(bpm));
      if (best === null || err < best[1]) best = [bpm, err];
    }
  }
  if (best !== null && best[1] < 0.12) return { bpm: r2(best[0]), conf: 0.92 };

  const take = dur > maxSeconds ? Math.floor(maxSeconds * sr) : n;
  const { env, hop } = onsetEnvelope(L.subarray(0, take), R ? R.subarray(0, take) : null, sr);
  if (env.length < 16) return best ? { bpm: r2(best[0]), conf: 0.4 } : { bpm: 0, conf: 0 };
  let mean = 0;
  for (let k = 0; k < env.length; k++) mean += env[k];
  mean /= env.length;
  const e = new Float64Array(env.length);
  for (let k = 0; k < env.length; k++) e[k] = env[k] - mean;

  const lagLo = Math.floor(60.0 / hi * sr / hop);
  const lagHi = Math.min(Math.floor(60.0 / lo * sr / hop), e.length - 1);
  if (lagHi <= lagLo) return best ? { bpm: r2(best[0]), conf: 0.4 } : { bpm: 0, conf: 0 };

  let bestLag = lagLo, peak = -Infinity, total = 0;
  for (let lag = 1; lag < e.length; lag++) {
    let ac = 0;
    for (let k = 0; k + lag < e.length; k++) ac += e[k] * e[k + lag];
    total += Math.abs(ac);
    if (lag >= lagLo && lag < lagHi && ac > peak) { peak = ac; bestLag = lag; }
  }
  const bpm = 60.0 * sr / (bestLag * hop);
  return { bpm: r2(bpm), conf: Math.min(1.0, peak / (total || 1) * 12.0) };
}

/* Cheap channel-domain source shaping. Not source separation — say so.

   'ctr'  mid (L+R)/2, band-limited to 180 Hz .. 8 kHz where a lead vocal
          sits, sides kept outside that band so the loop still has bottom
          and air.
   'side' (L-R)/2 — everything panned wide. Kills most centred vocals. */
function centerExtract(Lc, Rc, sr, mode) {
  if (!Rc) return [Lc, Rc];
  const n = Lc.length;
  if (mode === 'side') {
    const a = new Float32Array(n), b = new Float32Array(n);
    for (let k = 0; k < n; k++) {
      const s = (Lc[k] - Rc[k]) * 0.5;
      a[k] = s; b[k] = -s;
    }
    return [a, b];
  }
  // round up to a power of two so the FFT above can take it whole
  let N = 1; while (N < n) N <<= 1;
  const midRe = new Float64Array(N), midIm = new Float64Array(N);
  const sideRe = new Float64Array(N), sideIm = new Float64Array(N);
  for (let k = 0; k < n; k++) {
    midRe[k] = (Lc[k] + Rc[k]) * 0.5;
    sideRe[k] = (Lc[k] - Rc[k]) * 0.5;
  }
  fft(midRe, midIm); fft(sideRe, sideIm);
  // 1/6-octave raised-cosine edges, so we do not ring on the transients
  for (let k = 0; k < N; k++) {
    const f = (k <= N / 2 ? k : k - N) * sr / N;
    const af = Math.abs(f);
    let band = (af >= 180.0 && af <= 8000.0) ? 1.0 : 0.0;
    for (const [edge, width, rising] of [[180.0, 90.0, true], [8000.0, 2500.0, false]]) {
      if (Math.abs(af - edge) < width) {
        const x = (af - (edge - width)) / (2 * width);
        band = rising ? x : 1.0 - x;
      }
    }
    midRe[k] *= band; midIm[k] *= band;
    sideRe[k] *= (1.0 - band); sideIm[k] *= (1.0 - band);
  }
  // inverse via conjugation
  for (let k = 0; k < N; k++) { midIm[k] = -midIm[k]; sideIm[k] = -sideIm[k]; }
  fft(midRe, midIm); fft(sideRe, sideIm);
  const outL = new Float32Array(n), outR = new Float32Array(n);
  for (let k = 0; k < n; k++) {
    const m = midRe[k] / N, s = sideRe[k] / N;
    outL[k] = m + s; outR[k] = m - s;
  }
  return [outL, outR];
}

const r2 = (x) => Math.round(x * 100) / 100;

/* ── the worker's side of the wire ─────────────────────────────────────
   Buffers arrive and leave by transfer, so nothing here is ever copied. */
self.onmessage = (e) => {
  const m = e.data;
  const L = m.L, R = m.R || null;
  if (m.job === 'load') {
    // The envelope first and on its own: the panel can draw the file, and the
    // track is playable, before the tempo and the slices are known.
    const env = peaks(L, R, 2048);
    self.postMessage({ job: 'peaks', id: m.id, i: m.i, which: m.which, env: env.buffer },
                     [env.buffer]);
    const { bpm, conf } = estimateBpm(L, R, m.sr);
    const { points, method } = slicePoints(L, R, m.sr, 16);
    self.postMessage({ job: 'analysis', id: m.id, i: m.i, bpm, conf,
                       slices: points.buffer, method }, [points.buffer]);
  } else if (m.job === 'range') {
    const r = peaksRange(L, R, m.start, m.end, m.buckets);
    self.postMessage({ job: 'range', id: m.id, req: m.req, kind: m.kind, i: m.i,
                       start: m.start, end: m.end, frames: L.length,
                       samples: r.kind === 'samples', data: r.data.buffer },
                     [r.data.buffer]);
  } else if (m.job === 'variant') {
    const [a, b] = centerExtract(L, R, m.sr, m.mode);
    const xs = b ? [a.buffer, b.buffer] : [a.buffer];
    self.postMessage({ job: 'variant', id: m.id, i: m.i, mode: m.mode,
                       L: a.buffer, R: b ? b.buffer : null }, xs);
  } else if (m.job === 'slice') {
    const { points, method } = slicePoints(L, R, m.sr, m.n || 16);
    self.postMessage({ job: 'slices', id: m.id, i: m.i, slices: points.buffer, method },
                     [points.buffer]);
  }
};
