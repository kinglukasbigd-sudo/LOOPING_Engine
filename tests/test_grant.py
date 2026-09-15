"""Grant lifetime: eviction, the offline boundary, and offline/live equality.

    PYTHONPATH=.pylibs python3 -m tests.test_grant
"""
from __future__ import annotations

import os
import sys
import types

import numpy as np

from loopengine import offline
from loopengine.app import App
from loopengine.engine import Engine

OUTSIDE = "/usr/share/sounds/alsa/Front_Center.wav"
ROOT = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FAILS = []


def check(name, ok, detail=""):
    print("%-56s %s%s" % (name, "PASS" if ok else "FAIL",
                          ("  " + detail) if detail else ""))
    if not ok:
        FAILS.append(name)


def app_with_engine(offline_engine=None):
    eng = offline_engine or Engine(samplerate=48000, blocksize=256, offline=True)
    a = App(eng, roots=[ROOT], inbox="/tmp/le-inbox")
    a.hub = types.SimpleNamespace(text=lambda o: None, peaks=lambda *a, **k: None)
    return a, eng


# --------------------------------------------------------------------------
def t_eviction_spares_a_loaded_path():
    """64 further grants must not un-grant a file a track is holding."""
    a, eng = app_with_engine()
    a._grant(OUTSIDE)
    a.handle({"op": "load", "i": 0, "path": OUTSIDE})
    import time
    for _ in range(60):
        eng.render_offline(256)
        if eng.tracks[0].src is not None:
            break
        time.sleep(0.05)
    check("the outside-root file loaded once granted",
          eng.tracks[0].src is not None and eng.tracks[0].path == OUTSIDE,
          eng.tracks[0].name or "not loaded")
    for i in range(App.GRANT_MAX + 40):
        a._grant("/tmp/filler-%d.wav" % i)
    check("the grant set is still bounded",
          len(a._granted) <= App.GRANT_MAX, "%d entries" % len(a._granted))
    check("the loaded path survived eviction", a._inside_roots(OUTSIDE))
    check("an unpinned old grant did not", not a._inside_roots("/tmp/filler-0.wav"))


def t_eviction_still_drops_unloaded_paths():
    a, eng = app_with_engine()
    a._grant(OUTSIDE)                       # granted but never loaded
    for i in range(App.GRANT_MAX + 10):
        a._grant("/tmp/other-%d.wav" % i)
    check("a granted-but-unloaded path is still evictable",
          not a._inside_roots(OUTSIDE))


def t_offline_refuses_outside_and_names_the_file():
    eng = Engine(samplerate=48000, blocksize=256, offline=True).start()
    try:
        offline.load_into(eng, [OUTSIDE], [ROOT])
        check("offline refuses a file outside --allow", False, "it loaded it")
    except offline.OutsideAllowed as e:
        check("offline refuses a file outside --allow", True)
        check("the refusal names the file and says how to fix it",
              OUTSIDE in str(e) and "--allow" in str(e), str(e)[:52])
    finally:
        eng.stop()


def t_offline_accepts_it_when_allowed():
    eng = Engine(samplerate=48000, blocksize=256, offline=True).start()
    offline.load_into(eng, [OUTSIDE], [ROOT, os.path.dirname(OUTSIDE)])
    eng.render_offline(512)
    ok = eng.tracks[0].src is not None
    eng.stop()
    check("offline loads it once --allow covers it", ok)


def t_offline_matches_live_sample_for_sample():
    """The claim every DSP result rests on: the renderer is the live path.

    Same outside-root file, same commands. One session goes through App with
    a dialog grant, the other through the offline renderer with --allow.
    """
    def via_app():
        eng = Engine(samplerate=48000, blocksize=256, offline=True)
        a, _ = app_with_engine(eng)
        a._grant(OUTSIDE)
        a.handle({"op": "load", "i": 0, "path": OUTSIDE})
        import time
        for _ in range(80):
            eng.render_offline(256)
            if eng.tracks[0].src is not None:
                break
            time.sleep(0.05)
        return eng

    def via_offline():
        eng = Engine(samplerate=48000, blocksize=256, offline=True)
        offline.load_into(eng, [OUTSIDE], [os.path.dirname(OUTSIDE)])
        eng.render_offline(256)
        return eng

    a_eng, b_eng = via_app(), via_offline()
    if a_eng.tracks[0].src is None or b_eng.tracks[0].src is None:
        check("both paths loaded the file", False)
        return
    check("both paths loaded the same file",
          a_eng.tracks[0].frames == b_eng.tracks[0].frames
          and a_eng.tracks[0].sr == b_eng.tracks[0].sr,
          "%d vs %d frames" % (a_eng.tracks[0].frames, b_eng.tracks[0].frames))

    outs = []
    for eng in (a_eng, b_eng):
        eng.post("track.gain", i=0, v=0.9)
        eng.post("master.gain", v=1.0)
        eng.post("track.xfade", i=0, v=6.0)
        eng.post("transport.bpm", v=120.0)
        eng.post("transport.quantum", v=0)
        eng.post("transport.start")
        eng.post("track.play", i=0)
        eng.render_offline(4096)
        eng.post("track.loop", i=0, ls=1000, le=20000)
        outs.append(eng.render_offline(48000).copy())

    same = np.array_equal(outs[0], outs[1])
    check("offline renders the live path sample for sample",
          same, "max diff %.3e" % float(np.abs(outs[0] - outs[1]).max()))
    check("and it was not silence",
          float(np.abs(outs[0]).max()) > 0.01,
          "peak %.4f" % float(np.abs(outs[0]).max()))


def t_cli_file_mode_enforces_allow():
    """The boundary must be reachable from the command line, not only the API.

    Directory mode treats the directory argument as the grant, deliberately.
    Explicit --file arguments carry no implicit grant, so the refusal is
    exercisable without constructing an Engine by hand.
    """
    import subprocess
    root = ROOT
    env = dict(os.environ, PYTHONPATH=os.path.join(root, ".pylibs"))
    r = subprocess.run([sys.executable, "-m", "loopengine.offline",
                        "--file", OUTSIDE, "/tmp/le-cli-test.wav", "1"],
                       cwd=root, env=env, capture_output=True, text=True, timeout=120)
    check("the CLI refuses --file outside --allow", r.returncode == 2,
          "exit %d" % r.returncode)
    check("and the refusal names the file on stderr",
          OUTSIDE in r.stderr and "--allow" in r.stderr, r.stderr.strip()[:56])

    r2 = subprocess.run([sys.executable, "-m", "loopengine.offline",
                         "--file", OUTSIDE, "--allow", os.path.dirname(OUTSIDE),
                         "kits/testkit-124", "/tmp/le-cli-test.wav", "1"],
                        cwd=root, env=env, capture_output=True, text=True, timeout=180)
    check("and renders it once --allow covers it",
          r2.returncode == 0 and "Front_Center" in r2.stdout,
          (r2.stdout or r2.stderr).strip()[:56])


if __name__ == "__main__":
    if not os.path.isfile(OUTSIDE):
        print("skipped: %s not present on this machine" % OUTSIDE)
        sys.exit(0)
    for fn in (t_eviction_spares_a_loaded_path, t_eviction_still_drops_unloaded_paths,
               t_offline_refuses_outside_and_names_the_file,
               t_offline_accepts_it_when_allowed,
               t_offline_matches_live_sample_for_sample,
               t_cli_file_mode_enforces_allow):
        try:
            fn()
        except Exception as exc:
            check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    print("\n%d checks failed" % len(FAILS) if FAILS else "\nall checks passed")
    sys.exit(1 if FAILS else 0)
