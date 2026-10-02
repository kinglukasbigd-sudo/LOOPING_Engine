/* The browser engine's kernels, against blocks numpy rendered.

   Run from tests/test_web.py, which writes the reference to a temp file and
   passes its path. Prints one PASS or FAIL line per case, in the same shape
   the Python checks print, so the suite counts them without knowing node ran.

   The tolerance is 1e-6: float32 has about seven decimal digits, numpy
   accumulates the Catmull-Rom in float32 and this does it in float64 before
   storing, so the two differ in the last bit and nowhere else. A real port
   error — a wrong tap, an off-by-one wrap, a sign — moves the answer by
   whole percentages, never by an ULP. */
const fs = require('fs');

const WORKLET = process.argv[2];
const REF = process.argv[3];
const TOL = 1e-6;

const src = fs.readFileSync(WORKLET, 'utf8');
// just the kernels: everything above the transport
const head = src.slice(0, src.indexOf('/* ── transport'));
const mod = new Function(head + '\nreturn {loopGather, seamBlend, shotGather, panGains, Work};')();

const ref = JSON.parse(fs.readFileSync(REF, 'utf8'));
const L = Float32Array.from(ref.buf[0]), R = Float32Array.from(ref.buf[1]);
let fails = 0;

function check(name, ok, detail) {
  console.log(name.padEnd(70) + ' ' + (ok ? 'PASS' : 'FAIL') + (detail ? '  ' + detail : ''));
  if (!ok) fails++;
}

ref.cases.forEach((c) => {
  const w = new mod.Work(c.n);
  mod.loopGather(L, R, c.ls, c.L, c.phase, c.step, c.n, w);
  if (c.xf) mod.seamBlend(L, R, c.ls, c.L, c.xf, ref.frames, c.n, w);
  let d = 0;
  for (let k = 0; k < c.n; k++)
    d = Math.max(d, Math.abs(w.yl[k] - c.y[0][k]), Math.abs(w.yr[k] - c.y[1][k]));
  check(`web dsp: ${c.what} matches numpy`, d < TOL, `max|diff| ${d.toExponential(2)}`);
});

// the pan law, which the mixer leans on at every block
let pd = 0;
for (const [pan, gl, gr] of ref.pans) {
  const [a, b] = mod.panGains(pan);
  pd = Math.max(pd, Math.abs(a - gl), Math.abs(b - gr));
}
check('web dsp: constant-power pan matches numpy', pd < TOL, `max|diff| ${pd.toExponential(2)}`);

// a one-shot gather, clamped at the edges rather than wrapped
const sw = new mod.Work(ref.shot.n);
mod.shotGather(L, R, ref.shot.phase, ref.shot.step, ref.shot.n, sw);
let sd = 0;
for (let k = 0; k < ref.shot.n; k++)
  sd = Math.max(sd, Math.abs(sw.yl[k] - ref.shot.y[0][k]), Math.abs(sw.yr[k] - ref.shot.y[1][k]));
check('web dsp: a key one-shot matches numpy', sd < TOL, `max|diff| ${sd.toExponential(2)}`);

process.exit(fails ? 1 : 0);
