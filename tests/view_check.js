// Exercises loopengine/ui/view.js — the file the panel loads — under node.
// One line per check: "PASS<TAB>name<TAB>detail" or "FAIL<TAB>name<TAB>detail".
// Run by tests/test_zoom.py; runnable alone:  node tests/view_check.js
'use strict';
const path = require('path');
const V = require(path.join(__dirname, '..', 'loopengine', 'ui', 'view.js'));

let fails = 0;
function check(name, ok, detail) {
  if (!ok) fails++;
  console.log([ok ? 'PASS' : 'FAIL', name, detail === undefined ? '' : String(detail)].join('\t'));
}
const MIN4 = 48000 * 240;           // the brief's four-minute file: 11,520,000 frames

// ── anchor invariance ─────────────────────────────────────────────────────
{
  let cases = 0, held = 0, worst = 0, escaped = 0;
  for (const frames of [185806, MIN4, 1000]) {
    for (const W of [800, 1079]) {
      const starts = [
        V.whole(frames),
        V.clamp({ vs: frames * 0.3, ve: frames * 0.35 }, frames, W),
        V.clamp({ vs: frames * 0.61, ve: frames * 0.61 + V.minSpan(W) * 3 }, frames, W),
      ];
      for (const v0 of starts) {
        for (const x of [0, 1, W / 3, W / 2, W - 1, W]) {
          for (const f of [0.5, 0.8, 1.25, 2, 0.1, 10, 1e-6, 1e6]) {
            const before = v0.vs + (x / W) * (v0.ve - v0.vs);
            const v1 = V.zoomAt(v0, frames, W, x, f);
            const after = v1.vs + (x / W) * (v1.ve - v1.vs);
            cases++;
            if (v1.vs < 0 || v1.ve > frames || !(v1.ve > v1.vs)) escaped++;
            const want = Math.min(frames, Math.max(Math.min(frames, V.minSpan(W)), (v0.ve - v0.vs) * f));
            const forced = Math.abs((v1.ve - v1.vs) - want) > 1e-6 || v1.vs <= 0 || v1.ve >= frames;
            if (forced) { held++; continue; }
            worst = Math.max(worst, Math.abs(after - before));
          }
        }
      }
    }
  }
  check('the sample under the pointer stays under the pointer', worst < 1e-6,
        `worst drift ${worst.toExponential(1)} samples across ${cases - held} free zooms`);
  check('every zoom stays inside the file', escaped === 0,
        `${escaped} of ${cases} escaped; ${held} were held by an edge or a limit`);
}

// ── limits and clamping ───────────────────────────────────────────────────
{
  const frames = MIN4, W = 1079;
  let v = V.whole(frames);
  for (let i = 0; i < 200; i++) v = V.zoomAt(v, frames, W, W * 0.37, 0.5);
  check('zooming in stops at one sample per 4 px', Math.abs((v.ve - v.vs) - W / 4) < 1e-9,
        `${(v.ve - v.vs).toFixed(2)} samples on ${W} px`);
  for (let i = 0; i < 200; i++) v = V.zoomAt(v, frames, W, W * 0.9, 2);
  check('zooming out stops at the whole file', v.vs === 0 && v.ve === frames, `${v.vs}..${v.ve}`);
  const z = V.clamp({ vs: 5000, ve: 53000 }, frames, W);
  const zl = V.pan(z, frames, W, 1e9), zr = V.pan(z, frames, W, -1e9);
  check('panning past the start holds at sample 0 without shrinking',
        zl.vs === 0 && Math.abs((zl.ve - zl.vs) - 48000) < 1e-9, `${zl.vs}..${zl.ve}`);
  check('panning past the end holds at the last sample without shrinking',
        zr.ve === frames && Math.abs((zr.ve - zr.vs) - 48000) < 1e-6, `${zr.vs}..${zr.ve}`);
  const s0 = V.zoomAt(V.clamp({ vs: 10, ve: 480010 }, frames, W), frames, W, W, 50);
  const s1 = V.zoomAt(V.clamp({ vs: frames - 480010, ve: frames - 10 }, frames, W), frames, W, 0, 50);
  check("zooming out at the file's start stays inside it", s0.vs >= 0 && s0.ve <= frames, `${s0.vs}..${s0.ve}`);
  check("zooming out at the file's end stays inside it", s1.vs >= 0 && s1.ve <= frames, `${s1.vs}..${s1.ve}`);
  const tiny = V.zoomAt(V.whole(100), 100, W, W / 2, 0.01);
  check('a file shorter than max zoom stays whole', tiny.vs === 0 && tiny.ve === 100, `${tiny.vs}..${tiny.ve}`);
  const bad = V.clamp({ vs: NaN, ve: -5 }, frames, W);
  check('a corrupt view is repaired, not propagated', bad.vs >= 0 && bad.ve > bad.vs && bad.ve <= frames,
        `${bad.vs}..${bad.ve}`);
}

// ── pixels never leak into engine state ───────────────────────────────────
{
  const frames = MIN4, W = 1080;
  const v = { vs: 1000000, ve: 1000270 };          // 270 samples on 1,080 px
  const a = V.xToFrame(100, W, v, frames), b = V.xToFrame(500, W, v, frames);
  const oldMap = Math.round(100 / W * frames);
  check('a region dragged while zoomed lands on source samples', a === 1000025 && b === 1000125,
        `100 px -> ${a}, 500 px -> ${b}`);
  check('not on the pixel coordinates, not on the whole-file mapping', a !== 100 && b !== 500 && a !== oldMap,
        `the whole-file mapping would have sent ${oldMap}`);
  let frac = 0, outside = 0, drift = 0;
  for (let x = -20; x <= W + 20; x += 0.37) {
    const f = V.xToFrame(x, W, v, frames);
    if (!Number.isInteger(f)) frac++;
    if (f < 0 || f > frames) outside++;
    if (x >= 0 && x <= W && Math.abs(V.frameToX(f, W, v) - x) > (W / 270) / 2 + 1e-9) drift++;
  }
  check('every pixel maps to a whole sample inside the file', frac === 0 && outside === 0,
        `${frac} fractional, ${outside} outside`);
  check('and back to within half a sample of that pixel', drift === 0, `${drift} off`);
}

// ── nudge ─────────────────────────────────────────────────────────────────
{
  const W = 1080;
  const a = V.nudgeStep(V.whole(MIN4), W), b = V.nudgeStep({ vs: 0, ve: 48000 }, W),
        c = V.nudgeStep({ vs: 0, ve: 270 }, W);
  check('arrow nudge is one pixel of the view, so it gets finer as you zoom', a === 10667 && b === 44 && c === 1,
        `whole file ${a} samples, one second ${b}, max zoom ${c}`);
}

// ── fit ───────────────────────────────────────────────────────────────────
{
  const W = 1080, frames = MIN4;
  const f = V.fitLoop(2000000, 2096000, frames, W);
  check('fit-loop shows the loop with a margin either side',
        f.vs < 2000000 && f.ve > 2096000 && Math.abs((f.ve - f.vs) - 105600) < 1e-6, `${f.vs}..${f.ve}`);
  const g = V.fitLoop(100, 5000, frames, W);
  check("fit-loop at the file's start stays inside it with the loop in view", g.vs === 0 && g.ve >= 5000,
        `${g.vs}..${g.ve}`);
  const h = V.fitLoop(3000000, 3000010, frames, W);
  check('fit-loop on a loop shorter than max zoom centres it at max zoom',
        Math.abs((h.ve - h.vs) - W / 4) < 1e-9 && h.vs < 3000000 && h.ve > 3000010, `${h.vs}..${h.ve}`);
  const w = V.fitLoop(0, frames, frames, W);
  check('fit-loop on a whole-file loop is the whole file', w.vs === 0 && w.ve === frames);
}

// ── the plot never skips a bucket ─────────────────────────────────────────
{
  let lost = 0, oldLost = 0, tried = 0;
  for (const [frames, n] of [[MIN4, 2048], [185806, 2064]]) {
    const W = 1079;
    for (let k = 0; k < n; k += 5) {
      const data = new Float32Array(n * 2);
      // 0.5, not 0.9: a Float32Array holds 0.9 as 0.8999999762, so a check
      // for >= 0.9 found no transient anywhere and blamed the plot
      data[2 * k] = -0.5; data[2 * k + 1] = 0.5;       // one transient, in bucket k
      const col = V.columns(V.whole(frames), W, [V.overviewSource(data, frames)]);
      let seen = false, oldSeen = false;
      for (let x = 0; x < W; x++) {
        if (col[2 * x + 1] >= 0.5) seen = true;
        if (data[2 * Math.min(n - 1, Math.floor(x / W * n)) + 1] >= 0.5) oldSeen = true;
      }
      tried++;
      if (!seen) lost++;
      if (!oldSeen) oldLost++;
    }
  }
  check('the overview never skips a bucket', lost === 0,
        `${lost} of ${tried} single-bucket transients lost; the old one-bucket-per-pixel plot lost ${oldLost}`);
}
{
  const frames = 185806, n = 2064, W = 1079;           // hop 90: covers 185,760 frames
  const data = new Float32Array(n * 2).fill(0.1);
  const col = V.columns(V.whole(frames), W, [V.overviewSource(data, frames)]);
  let blank = 0;
  for (let x = 0; x < W; x++) if (Number.isNaN(col[2 * x])) blank++;
  check("the file's last sliver, which the load-time envelope omits, leaves no blank column", blank === 0,
        `${blank} blank`);
}
{
  const frames = MIN4, W = 1000;
  const ov = new Float32Array(2048 * 2);
  for (let i = 0; i < 2048; i++) { ov[2 * i] = -0.2; ov[2 * i + 1] = 0.2; }
  const rd = new Float32Array(W * 2);
  for (let i = 0; i < W; i++) { rd[2 * i] = -0.5; rd[2 * i + 1] = 0.5; }
  const rs = V.envelopeSource(rd, 5000000, 5100000);
  const col = V.columns({ vs: 5000000, ve: 5100000 }, W, [V.overviewSource(ov, frames), rs]);
  let acc = 0;
  for (let x = 0; x < W; x++) if (col[2 * x + 1] === 0.5) acc++;
  check('accurate peaks replace the stretched overview wherever they cover the view', acc === W,
        `${acc} of ${W} columns`);
  const col2 = V.columns({ vs: 5050000, ve: 5150000 }, W, [V.overviewSource(ov, frames), rs]);
  let a2 = 0, o2 = 0, b2 = 0;
  for (let x = 0; x < W; x++) {
    const hi = col2[2 * x + 1];
    if (Number.isNaN(hi)) b2++; else if (hi === 0.5) a2++; else o2++;
  }
  check('panned past the accurate data, the rest draws from the overview and nothing goes blank',
        b2 === 0 && a2 > 0 && o2 > 0, `${a2} accurate, ${o2} from the overview, ${b2} blank`);
}

// ── the sample line ───────────────────────────────────────────────────────
{
  const W = 1080;
  const data = new Float32Array(270);
  for (let i = 0; i < 270; i++) data[i] = Math.sin(i / 10);
  const s = V.samplesSource(data, 1000000);
  const pts = V.sampleLine({ vs: 1000000, ve: 1000270 }, W, s);
  check('below one sample per pixel the samples themselves are drawn',
        pts && pts.length / 2 === 270 && pts[0] === 0 && Math.abs(pts[2] - 4) < 1e-9,
        pts ? `${pts.length / 2} points, 4 px apart` : 'none');
  check('and never from samples that do not cover the view',
        V.sampleLine({ vs: 999000, ve: 999270 }, W, s) === null);
}

// ── view state per subject ────────────────────────────────────────────────
{
  const st = V.store();
  const vt = V.clamp({ vs: 2e6, ve: 2.1e6 }, MIN4, 1080);
  st.set('track:0', 'bass.wav|11520000', vt);
  const k = st.get('key:5', 'stab.wav|185806', 185806);
  const back = st.get('track:0', 'bass.wav|11520000', MIN4);
  check('a key starts from its own whole file', k.vs === 0 && k.ve === 185806);
  check('the track is as you left it after focusing a key and coming back',
        back.vs === vt.vs && back.ve === vt.ve, `${back.vs}..${back.ve}`);
  const re = st.get('track:0', 'keys.wav|11520000', MIN4);
  check('a different file in the same slot starts from the whole file, even at the same length',
        re.vs === 0 && re.ve === MIN4, `${re.vs}..${re.ve}`);
}

// ── requests: debounce lives in the panel, coalescing here ───────────────
{
  const q = V.coalescer(2000);
  let sent = 0, t = 0;
  for (let i = 0; i < 20; i++) { if (q.ask({ view: i }, t)) sent++; t += 16; }
  check('twenty settles while one request is in flight send one request, not twenty', sent === 1,
        `${sent} sent`);
  const r = q.answer(1, t);
  check('when it answers, only the latest waiting view goes next', !!(r.accepted && r.next && r.next.view === 19),
        r.next ? `next asks for view ${r.next.view}` : 'nothing next');
  check('a late reply to an older request is ignored', q.answer(1, t + 5).accepted === null);
  const q2 = V.coalescer(2000);
  q2.ask({ view: 'a' }, 0);
  q2.ask({ view: 'b' }, 10);
  const rel = q2.tick(2500);
  check('a lost reply releases the slot after the timeout', !!(rel && rel.view === 'b'));
}

// ── views travel with a session ──────────────────────────────────────────
{
  const a = V.store();
  a.set('track:0', 'bass.wav|11520000', { vs: 2e6, ve: 2.1e6 });
  a.set('key:5', 'stab.wav|185806', { vs: 10, ve: 400 });
  const wire = JSON.parse(JSON.stringify(a.entries()));
  const b = V.store();
  b.set('track:3', 'old.wav|99', { vs: 1, ve: 2 });
  b.restore(wire);
  const t0 = b.get('track:0', 'bass.wav|11520000', 11520000);
  check('views saved with a session come back through JSON exactly',
        t0.vs === 2e6 && t0.ve === 2.1e6 && b.peek('key:5', 'stab.wav|185806').ve === 400,
        `${t0.vs}..${t0.ve}`);
  check('loading a session drops the views of the set it replaced',
        b.peek('track:3', 'old.wav|99') === null);
  const c = V.store();
  c.restore({ 'track:1': { sig: 'x|1', vs: 5, ve: 2 }, 'track:2': { sig: 'y|1', vs: 'a', ve: 9 },
              'track:3': { sig: 'z|1', vs: -4, ve: 9 }, 'track:4': { vs: 0, ve: 9 },
              'track:5': { sig: 'ok|100', vs: 0, ve: 50 } });
  check('a hand-edited view that is not a real window is dropped, not trusted',
        Object.keys(c.entries()).join() === 'track:5', Object.keys(c.entries()).join());
}

// ── a sweep of the pointer ────────────────────────────────────────────────
// Work order 10: plain drag across the waveform sets the loop. The failure
// mode is a pixel reaching engine state as though it were a sample, so every
// check here is in samples, at zoom levels a hand actually works at.
{
  const frames = 185806;
  const pairs = [[0, frames], [46495, 92817], [1000, 1000], [12345.4, 12345.6],
                 [frames - 10, frames + 500], [-200, 300], [frames, frames],
                 [0, 63], [frames - 63, frames], [90000, 89990]];
  let mirrored = true, short = 0, escaped = 0;
  const sent = [];
  for (const [a, b] of pairs) {
    const f = V.sweep(a, b, frames), r = V.sweep(b, a, frames);
    if (f[0] !== r[0] || f[1] !== r[1]) mirrored = false;
    if (f[1] - f[0] < V.MIN_LOOP) short++;
    if (f[0] < 0 || f[1] > frames || !Number.isInteger(f[0]) || !Number.isInteger(f[1])) escaped++;
    sent.push([f[0], f[1], frames]);
  }
  check('a backwards sweep gives the same region as a forwards one', mirrored,
        `${pairs.length} pairs both ways`);
  check('no sweep is shorter than the engine keeps', short === 0,
        `${short} of ${pairs.length} under ${V.MIN_LOOP} samples`);
  check('a sweep lands on whole samples inside the file', escaped === 0);
  const tail = V.sweep(frames - 5, frames + 400, frames);
  check('a sweep off the end of the file pulls its start back rather than shrink',
        tail[1] === frames && tail[1] - tail[0] === V.MIN_LOOP, `${tail[0]}..${tail[1]}`);
  check('a file shorter than the minimum loops end to end',
        V.sweep(10, 20, 40).join() === '0,40', V.sweep(10, 20, 40).join());
  // Handed to tests/test_zoom.py, which puts each one through the real
  // Track.set_loop: what the panel paints has to be what the engine keeps.
  console.log(['DATA', 'sweep', JSON.stringify(sent)].join('\t'));
}

{
  const frames = MIN4, W = 1079;
  let bad = 0, free = 0, forced = 0;
  for (const z of [1, 0.5, 0.05, 0.002, 1e-5]) {
    for (const off of [0, 0.25, 0.9]) {
      const span = Math.max(V.minSpan(W), frames * z);
      const v = V.clamp({ vs: frames * off, ve: frames * off + span }, frames, W);
      for (const [x0, x1] of [[10, 900], [900, 10], [0, W], [500, 503], [W, 0]]) {
        const lo = Math.min(x0, x1), hi = Math.max(x0, x1);
        const a = V.xToFrame(lo, W, v, frames), b = V.xToFrame(hi, W, v, frames);
        const f = V.sweep(V.xToFrame(x0, W, v, frames), V.xToFrame(x1, W, v, frames), frames);
        if ((f[1] - f[0]) > V.MIN_LOOP && f[0] > 0 && f[1] < frames) {
          free++;
          if (f[0] !== a || f[1] !== b) bad++;
        } else { forced++; }
      }
    }
  }
  check('a sweep hands over the samples under the two pixels, at every zoom',
        bad === 0, `${bad} wrong of ${free} free sweeps; ${forced} held by an edge`);
}

// ── dragging the overview span ────────────────────────────────────────────
{
  const frames = MIN4, W = 1079;
  const v = V.clamp({ vs: frames * 0.4, ve: frames * 0.4 + 480000 }, frames, W);
  const Wmini = 240;
  const moved = V.panFrames(v, frames, W, 30 / Wmini * frames);
  check('dragging the overview span moves the view by that fraction of the file',
        Math.abs(moved.vs - (v.vs + 30 / Wmini * frames)) < 1e-6
        && Math.abs((moved.ve - moved.vs) - (v.ve - v.vs)) < 1e-6,
        `${moved.vs.toFixed(1)}..${moved.ve.toFixed(1)}`);
  const left = V.panFrames(v, frames, W, -1e12), right = V.panFrames(v, frames, W, 1e12);
  check('and stops at either end of the file without shrinking the view',
        left.vs === 0 && right.ve === frames
        && Math.abs((left.ve - left.vs) - (v.ve - v.vs)) < 1e-6
        && Math.abs((right.ve - right.vs) - (v.ve - v.vs)) < 1e-6,
        `${left.vs}..${left.ve} | ${right.vs}..${right.ve}`);
  check('a drag that has not moved leaves the view exactly where it was',
        V.panFrames(v, frames, W, 0).vs === v.vs && V.panFrames(v, frames, W, NaN).ve === v.ve);
}

// ── a take's clock ───────────────────────────────────────────────────────
{
  const lengths = new Set();
  for (const s of [0, 0.4, 59.99, 60, 599, 3599.9, 3600, 36000, 359999, 400000, -5, NaN, 'x', undefined])
    lengths.add(V.clock(s).length);
  check('the take clock is always eight characters, so no digit moves as it grows',
        lengths.size === 1 && lengths.has(8), [...lengths].join());
  check('it reads hours, minutes and whole seconds',
        V.clock(0) === '00:00:00' && V.clock(59.99) === '00:00:59' && V.clock(3661) === '01:01:01',
        V.clock(3661));
  check('past 99 hours it holds at 99:59:59 rather than grow a digit', V.clock(400000) === '99:59:59');
  check('a missing or broken reading shows nought, never NaN',
        V.clock(NaN) === '00:00:00' && V.clock(undefined) === '00:00:00' && V.clock(-5) === '00:00:00');
}

process.exitCode = fails ? 1 : 0;
