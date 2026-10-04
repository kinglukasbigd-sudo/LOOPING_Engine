"""The browser build: its DSP against numpy's, and the shape of what it ships.

    PYTHONPATH=.pylibs python3 -m tests.test_web

public/ is the panel driving an engine written in JavaScript instead of one
written in Python, with the page about it one level in at /artifact. The panel is the same file in both, so what has to be
checked is the new engine: that its kernels produce the samples numpy
produces, and that the build actually assembles.

The kernel checks run under node from tests/web_check.js and are relayed here,
the same way the view's checks are. Without node on PATH that relay FAILS
rather than skipping: an untested engine must not read as a pass.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np

from loopengine import dsp

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FAILS = []


def check(name, ok, detail=""):
    print("%-70s %s%s" % (name, "PASS" if ok else "FAIL",
                          ("  " + detail) if detail else ""))
    if not ok:
        FAILS.append(name)


def _reference(path):
    """Blocks rendered by the engine that is already trusted."""
    rng = np.random.default_rng(7)
    frames, ch = 9000, 2
    buf = (rng.standard_normal((frames, ch)) * 0.3).astype(np.float32)
    cases = [
        dict(what="a loop at the file's own rate", ls=100, L=4096,
             phase=100.0, step=1.0, n=128, xf=0),
        dict(what="a 44.1k file on a 48k graph", ls=0, L=1000,
             phase=37.5, step=0.91875, n=128, xf=240),
        dict(what="reverse at 1.3x", ls=500, L=777,
             phase=1234.25, step=-1.3, n=128, xf=0),
        dict(what="a region shorter than a block", ls=64, L=200,
             phase=70.0, step=2.7, n=128, xf=48),
    ]
    out = {"buf": buf.T.tolist(), "frames": frames, "cases": []}
    for c in cases:
        w = dsp.Work(c["n"], ch)
        y, rel = dsp.loop_gather(buf, c["ls"], c["L"], c["phase"], c["step"],
                                 c["n"], w)
        y = y.copy()
        if c["xf"]:
            y = dsp.seam_blend(buf, y, rel, c["ls"], c["L"], c["xf"], frames, w)
        out["cases"].append({**c, "y": y.T.tolist()})

    out["pans"] = [[p, *dsp.pan_gains(p)]
                   for p in (-1.0, -0.5, 0.0, 0.33, 1.0)]
    w = dsp.Work(96, ch)
    y = dsp.shot_gather(buf, 0, 8800.5, 1.7, 96, w)   # runs off the end
    out["shot"] = {"phase": 8800.5, "step": 1.7, "n": 96, "y": y.T.tolist()}

    with open(path, "w") as f:
        json.dump(out, f)


def t_kernels_match_numpy():
    node = shutil.which("node")
    if node is None:
        check("web dsp: kernels match numpy (node)", False, "node is not on PATH")
        return
    with tempfile.TemporaryDirectory() as tmp:
        ref = os.path.join(tmp, "ref.json")
        _reference(ref)
        r = subprocess.run(
            [node, os.path.join(HERE, "web_check.js"),
             os.path.join(ROOT, "web", "worklet.js"), ref],
            capture_output=True, text=True)
        sys.stdout.write(r.stdout)
        if r.returncode and not r.stdout.strip():
            check("web dsp: the node relay ran", False,
                  (r.stderr or "").strip().splitlines()[-1:] and
                  r.stderr.strip().splitlines()[-1] or "no output")
        elif r.returncode:
            FAILS.append("web dsp kernels")


def t_the_build_assembles():
    """web/build.py produces something a static host can serve as it stands."""
    r = subprocess.run([sys.executable, os.path.join(ROOT, "web", "build.py")],
                       capture_output=True, text=True, cwd=ROOT)
    out = os.path.join(ROOT, "public")
    check("web build: it runs", r.returncode == 0,
          (r.stderr or "").strip().splitlines()[-1] if r.returncode else "")
    want = ["index.html", "app.js", "view.js", "app.css", "tokens.css",
            "host.js", "worklet.js", "analyze.js"]
    missing = [n for n in want if not os.path.exists(os.path.join(out, n))]
    check("web build: every file the page asks for is there", not missing,
          ", ".join(missing))
    nfonts = len(os.listdir(os.path.join(out, "fonts"))) \
        if os.path.isdir(os.path.join(out, "fonts")) else 0
    check("web build: the fonts travel with it", nfonts >= 12, "%d files" % nfonts)


def t_the_panel_is_not_forked():
    """One panel, two engines. A copy that drifts is two panels."""
    for name in ("app.js", "view.js", "app.css", "tokens.css"):
        a = open(os.path.join(ROOT, "loopengine", "ui", name), "rb").read()
        b_path = os.path.join(ROOT, "public", name)
        b = open(b_path, "rb").read() if os.path.exists(b_path) else b""
        check("web build: %s is the panel's own file, byte for byte" % name, a == b)


def t_the_tool_is_the_address():
    """The root is the instrument, and the page about it is reachable from
    inside the instrument rather than in front of it."""
    root = open(os.path.join(ROOT, "public", "index.html")).read()
    ok = 'id="app"' in root
    check("web build: the root page is the panel, not a description", ok,
          "" if ok else "the root is not the panel")
    art = os.path.join(ROOT, "public", "artifact", "index.html")
    check("web build: the page about it is served at /artifact", os.path.exists(art))
    if os.path.exists(art):
        check("web build: that page links back to the instrument",
              'href="../"' in open(art).read())
    host = open(os.path.join(ROOT, "web", "host.js")).read()
    check("web build: the panel links to it from the help sheet `?` opens",
          'artifact/' in host and "#helpsheet" in host)
    app = open(os.path.join(ROOT, "loopengine", "ui", "app.js")).read()
    ok = "artifact" not in app
    check("web build: no existing key or button was given a new meaning", ok,
          "" if ok else "app.js mentions the page")


def t_the_page_loads_the_engine_before_the_panel():
    """host.js installs the engine behind window.WebSocket; app.js opens one
    on its first pass. The order is the whole trick, so it is checked."""
    html = open(os.path.join(ROOT, "public", "index.html")).read()
    ih, ia = html.find("host.js"), html.find("app.js")
    check("web build: host.js is loaded before app.js", 0 <= ih < ia,
          "host at %d, app at %d" % (ih, ia))
    check("web build: no token is left in the page", "{{TOKEN}}" not in html)


def t_the_ops_the_panel_sends_are_all_handled():
    """Every op app.js can send is either answered on the main thread or
    named in the worklet's table. A missing one is a control that does
    nothing, which is worse than one that is not there."""
    app = open(os.path.join(ROOT, "loopengine", "ui", "app.js")).read()
    sent = set(re.findall(r"op: '([a-z.]+)'", app))
    host = open(os.path.join(ROOT, "web", "host.js")).read()
    wk = open(os.path.join(ROOT, "web", "worklet.js")).read()
    known = set(re.findall(r"case '([a-z.]+)'", host + wk))
    missing = sorted(sent - known)
    check("web engine: every op the panel sends is handled", not missing,
          ", ".join(missing))


import re  # noqa: E402  (used by the check above)

if __name__ == "__main__":
    for fn in (t_kernels_match_numpy,
               t_the_build_assembles,
               t_the_tool_is_the_address,
               t_the_panel_is_not_forked,
               t_the_page_loads_the_engine_before_the_panel,
               t_the_ops_the_panel_sends_are_all_handled):
        try:
            fn()
        except Exception as exc:
            check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    print("\n%d checks failed" % len(FAILS) if FAILS else "\nall checks passed")
    sys.exit(1 if FAILS else 0)
