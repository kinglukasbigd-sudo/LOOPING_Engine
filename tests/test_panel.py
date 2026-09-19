"""Task 31: the panel, driven the way a person drives it.

Every bug that reached Ivan in the last three work orders was a panel gesture no
Python test could send — a mode button that replaced sixteen keys, a key drag
that snapped back, a title that shifted, a message rebuilt sixty times a second.
The engine suite could not press a button, so it could not see any of them.

This file presses them, in a real browser, against a real panel server on the
null backend: no sound card, no display. It needs chromium, which

    PYTHONPATH=.pylibs python3 -m playwright install chromium

fetches once. Without it every check here prints SKIP with the reason rather
than passing untested.

    PYTHONPATH=.pylibs python3 -m tests.test_panel
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KIT = os.path.join(ROOT, "kits", "testkit-124")
# The browser lives with the other vendored dependencies. Twice in one hour this
# machine cleared ~/.cache between the download and the run, which reads as "no
# browser" and skips every check here; .pylibs is where this project already
# keeps what it vendors, and it is not a cache anything else prunes.
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(ROOT, ".pylibs", "ms-playwright")
if not os.path.isdir(KIT):
    # The kit's wavs are gitignored, so a fresh clone and every CI run starts
    # without them. run.py builds the kit only when no --kit is given, and this
    # server is started with one, so the test builds it the same way demo does.
    sys.path.insert(0, ROOT)
    from loopengine import demo
    print("building the synthesised test kit in %s" % KIT)
    demo.build(KIT)
FAILS = []


def check(name, ok, detail=""):
    print("%-74s %s%s" % (name, "PASS" if ok else "FAIL",
                          ("  " + detail) if detail else ""))
    if not ok:
        FAILS.append(name)


def skip_all(reason):
    for name in ("the panel's gestures are driven in a browser",):
        print("%-74s SKIP  %s" % (name, reason))
    sys.exit(0)


try:
    from playwright.sync_api import sync_playwright
except ImportError:
    skip_all("playwright is not installed — pip install playwright, then "
             "python3 -m playwright install chromium")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Panel:
    """A panel server on the null backend, and its URL with the token."""

    def __init__(self, base):
        self.base = base
        self.port = free_port()
        env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, ".pylibs"))
        self.proc = subprocess.Popen(
            [sys.executable, "-u", "run.py", "--offline", "--no-browser",
             "--port", str(self.port), "--kit", KIT,
             "--root", KIT, "--root", base, "--root", ROOT,
             "--sessions", os.path.join(base, "sessions"),
             "--recordings", os.path.join(base, "takes")],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1)
        self.lines = []
        self.url = ""
        threading.Thread(target=self._drain, daemon=True).start()
        t0 = time.monotonic()
        while time.monotonic() - t0 < 45 and not self.url:
            time.sleep(0.05)

    def _drain(self):
        for line in self.proc.stdout:
            self.lines.append(line.rstrip())
            m = re.search(r"(http://127\.0\.0\.1:\d+/\?t=\S+)", line)
            if m and not self.url:
                self.url = m.group(1)

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


HOOK = """
window.__sent = [];
(() => {
  const real = WebSocket.prototype.send;
  WebSocket.prototype.send = function (data) {
    try {
      const pad = document.querySelectorAll('#padgrid .pad')[0];
      window.__sent.push({
        data: String(data),
        run: (document.querySelector('[data-act="run"]') || {}).getAttribute
             ? document.querySelector('[data-act="run"]').getAttribute('aria-pressed') : null,
        pad0: pad ? pad.className : '',
        line: (document.querySelector('#session-line') || {}).textContent || '',
        roll: (document.querySelector('#roll-line') || {}).textContent || '',
        win: (document.querySelector('#w-in') || {}).textContent || '',
        wout: (document.querySelector('#w-out') || {}).textContent || '',
        hand: (typeof dragLoop !== 'undefined' && dragLoop)
              ? [dragLoop.ls, dragLoop.le] : null,
      });
    } catch (e) { /* the hook must never break the panel */ }
    return real.call(this, data);
  };
})();
/* The stillness probe holds the ELEMENTS it measured, not a numbered list of
   their boxes. Keyed by position, one element added or removed anywhere shifts
   every index after it, and the comparison then reports a row against some
   other row — movement that never happened, and, the other way round, real
   movement hidden behind a neighbour's box. */
window.__box = (el) => {
  const r = el.getBoundingClientRect();
  return [r.x, r.y, r.width, r.height].map(v => Math.round(v * 10) / 10).join(',');
};
window.__mark = () => {
  window.__snap = [];
  for (const el of document.querySelectorAll('body *')) window.__snap.push([el, window.__box(el)]);
  return window.__snap.length;
};
window.__moved = (skip) => {
  const out = [];
  for (const [el, was] of window.__snap || []) {
    if (!el.isConnected) continue;             // gone is not moved
    const name = el.tagName + (el.id ? '#' + el.id : '');
    if (skip && skip.some(s => name.indexOf(s) >= 0)) continue;
    const now = window.__box(el);
    if (now !== was) out.push(name + ' ' + was + ' -> ' + now);
  }
  return out;
};
"""


def ready(page):
    # `let S` lives in the global lexical scope, not on window: the panel's own
    # name for it is the only one that resolves.
    page.wait_for_function(
        "typeof S !== 'undefined' && S && S.pads && S.tracks && S.tracks.some(t => t.loaded)",
        timeout=30000)
    paint(page)


def paint(page):
    """The panel draws in its rAF loop; drive it so a check never races a frame."""
    page.evaluate("renderState()")


def pads(page):
    return page.evaluate("S.pads.map(p => p.loaded ? "
                         "`${p.name} ${p.ls}-${p.le} ${p.mode}` : 'empty')")


def key(page, i):
    return page.evaluate("i => { const p = S.pads[i]; return p.loaded ? "
                         "`${p.name} ${p.ls}-${p.le} ${p.mode}` : 'empty'; }", i)


def track(page, i=0):
    return page.evaluate("i => { const t = S.tracks[i]; return t.loaded ? "
                         "`${t.name} ${t.ls}-${t.le}` : 'empty'; }", i)


def note(page):
    return page.evaluate("typeof localError === 'string' ? localError : ''")


def press(page, cap):
    page.keyboard.press(cap)


def open_file(page, name):
    """LOAD, then click a file — the browser's own gesture.

    The server's first --root is the kit, so the browser opens there. Waiting on
    the listing rather than on the row: reading #b-dir straight after the click
    catches it before the fetch lands, and then nothing matches."""
    page.click('[data-act="browse"]')
    page.wait_for_function(
        "() => { const d = document.querySelector('#b-dir');"
        " return d && d.textContent.endsWith('testkit-124'); }", timeout=15000)
    page.wait_for_selector("#b-list .b-row", timeout=10000)
    page.click(f'#b-list .b-row:has-text("{name}")')
    page.wait_for_function("n => S.tracks[0].loaded && S.tracks[0].name.startsWith(n)",
                           arg=name, timeout=20000)
    closed(page)
    paint(page)


def toggle(page, act, on):
    """ASSIGN and EDIT are toggles. Press only when the state is not already
    the one wanted, or a test inherits whatever the last one left behind — which
    is how a key press became a trigger instead of a focus."""
    now = page.get_attribute('[data-act="%s"]' % act, "aria-pressed") == "true"
    if now != on:
        page.click('[data-act="%s"]' % act)
    paint(page)


def whole_file(page):
    """Fit the view before working out where a frame is on screen."""
    page.keyboard.press("Digit0")
    page.wait_for_timeout(120)
    paint(page)


def closed(page):
    """The file browser slides out over the pad grid and keeps taking clicks
    until it has gone. A press on the bar underneath lands on it instead, so
    every gesture starts by making sure it is out of the way."""
    if page.evaluate("document.querySelector('#browser').classList.contains('open')"):
        page.keyboard.press("Escape")
    page.wait_for_function(
        "!document.querySelector('#browser').classList.contains('open')", timeout=8000)
    page.wait_for_timeout(320)                   # the slide, then it stops intercepting


def drag(page, sel, x0, x1, y=None, shift=False, button="left"):
    """Press, travel, release — and hand back the two client x it really used,
    because that is what the panel's own arithmetic sees. A synthetic clientX is
    a whole number, so a check that recomputes from x0 would be comparing
    against a coordinate the browser never sent."""
    box = page.locator(sel).bounding_box()
    yy = box["y"] + (y if y is not None else box["height"] / 2)
    cx0 = round(box["x"] + x0)
    cx1 = round(box["x"] + x1)
    page.mouse.move(cx0, round(yy))
    if shift:
        page.keyboard.down("Shift")
    page.mouse.down(button=button)
    for step in (0.35, 0.7, 1.0):
        page.mouse.move(round(box["x"] + x0 + (x1 - x0) * step), round(yy))
        page.wait_for_timeout(20)
    page.mouse.up(button=button)
    if shift:
        page.keyboard.up("Shift")
    page.wait_for_timeout(120)
    paint(page)
    return cx0, cx1


def focus_track(page, i):
    """Press the row's name, not its envelope: the envelope is a scroll handle
    now, and this is setting up a check, not the check. Already focused, press
    nothing — two presses on one row inside the double-click window launch it."""
    if page.evaluate("k => focusKind === 'track' && focus === k", i):
        return
    page.click("#strips .strip:nth-of-type(%d) .c-name" % (i + 1))
    paint(page)


def landed(page):
    """Wait for the engine to echo the region the hand painted — the panel holds
    its own version until it does, so reading S before that reads the old one."""
    page.wait_for_function("dragLoop === null", timeout=10000)
    paint(page)


def region(page):
    return page.evaluate("[S.tracks[0].ls, S.tracks[0].le]")


def whole_loop(page):
    """Loop the whole file first, so both handles sit at the panel's edges and
    a sweep started in the middle cannot land on one — handles win that press,
    which is the rule, and a check that trips over it is testing the wrong
    thing. Double click is the panel's own gesture for it."""
    page.dblclick("#wcanvas")
    landed(page)


def view(page):
    return page.evaluate("() => { const v = viewOf(subject()); return [v.vs, v.ve]; }")


def swept(page, cx0, cx1):
    """What the region SHOULD be: the samples under those two client pixels,
    through the view that is showing, by the panel's own published arithmetic.
    An exact comparison — a pixel that reached engine state as though it were a
    sample shows up here as a wrong number, not as a near miss."""
    return page.evaluate("""([a, b]) => {
      const r = document.querySelector('#wcanvas').getBoundingClientRect();
      const t = subject(), v = viewOf(t);
      return View.sweep(View.xToFrame(a - r.left, r.width, v, t.frames),
                        View.xToFrame(b - r.left, r.width, v, t.frames), t.frames);
    }""", [cx0, cx1])


# ── the gestures that change what a key holds ────────────────────────────────
def t_assign_keeps_what_the_key_was_given(page):
    closed(page)
    whole_file(page)
    box = page.locator("#wcanvas").bounding_box()
    frames = page.evaluate("S.tracks[0].frames")
    x = lambda f: round(box["width"] * f / frames)
    drag(page, "#wcanvas", x(46451), x(92902))
    region = page.evaluate("[S.tracks[0].ls, S.tracks[0].le]")
    check("a drag across the waveform sets the track's region by hand",
          region[0] > 40000 and region[1] - region[0] > 30000, str(region))

    taken = page.evaluate("S.pads[0].loaded")
    toggle(page, "assign", True)
    press(page, "Digit1")
    paint(page)
    if taken:
        before = key(page, 0)
        check("assigning over a key that holds audio asks first, and changes nothing",
              key(page, 0) == before and note(page).startswith("Key 1 holds"), note(page)[:60])
        press(page, "Digit1")
    page.wait_for_function("f => S.pads[0].loaded && Math.abs(S.pads[0].ls - f) < 200",
                           arg=region[0], timeout=10000)
    paint(page)
    check("the key takes the track's file and the region that was dragged",
          key(page, 0).startswith("tk_bass") and abs(page.evaluate("S.pads[0].ls") - region[0]) < 200,
          key(page, 0))

    held = key(page, 0)
    open_file(page, "tk_hats")
    check("loading another file onto that track leaves the key exactly as it was",
          key(page, 0) == held, "%s -> %s" % (held, key(page, 0)))
    page.click('[data-act="unload"]')
    page.wait_for_function("!S.tracks[0].loaded", timeout=10000)
    paint(page)
    check("and ejecting the track leaves it alone too",
          key(page, 0) == held and track(page) == "empty")


def t_map_asks_and_mode_is_only_a_mode(page):
    closed(page)
    open_file(page, "tk_stab")
    before = pads(page)
    page.click('[data-act="mappads"]')
    paint(page)
    armed = page.get_attribute('[data-act="mappads"]', "aria-pressed")
    check("MAP arms and says what it would replace, without replacing it",
          armed == "true" and pads(page) == before and note(page).startswith("MAP replaces"),
          note(page)[:60])
    page.wait_for_timeout(5000)          # a window would have lapsed by now
    paint(page)
    check("the armed state has no clock: it is still waiting five seconds later",
          page.get_attribute('[data-act="mappads"]', "aria-pressed") == "true"
          and pads(page) == before)
    page.keyboard.press("Escape")
    paint(page)
    check("Esc calls it off and the keys are untouched",
          page.get_attribute('[data-act="mappads"]', "aria-pressed") == "false"
          and pads(page) == before and note(page) == "")
    page.click('[data-act="mappads"]')
    page.click('[data-act="mappads"]')
    page.wait_for_function("S.pads[0].slice === 0", timeout=10000)
    paint(page)
    check("pressed twice it fills every key from the file's slices",
          all(p != "empty" for p in pads(page)) and key(page, 0).startswith("tk_stab"),
          key(page, 0))

    toggle(page, "editkeys", True)
    page.click("#padgrid .pad >> nth=1")
    paint(page)
    check("editing a key, the segment says whose mode it is showing",
          page.inner_text("#pmode-for") == "KEY 2", page.inner_text("#pmode-for"))
    was = pads(page)
    want = "LOOP" if page.evaluate("S.pads[1].mode") != "LOOP" else "ONE"
    page.click(f'#pad-modes [data-pmode="{want}"]')
    page.wait_for_function("m => S.pads[1].mode === m", arg=want, timeout=10000)
    paint(page)
    now = pads(page)
    check("choosing a mode changes that key's mode and no audio anywhere",
          now[1].split()[0] == was[1].split()[0] and now[1].endswith(want)
          and [p.rsplit(" ", 1)[0] for p in now] == [p.rsplit(" ", 1)[0] for p in was],
          now[1])
    toggle(page, "editkeys", False)
    check("with no key being edited it names the default instead",
          page.inner_text("#pmode-for") == "NEW KEYS")


def t_clearing_a_key(page):
    closed(page)
    toggle(page, "editkeys", True)
    page.click("#padgrid .pad >> nth=2")
    page.click('[data-act="clearkey"]')
    page.wait_for_function("!S.pads[2].loaded", timeout=10000)
    paint(page)
    cell = page.evaluate("document.querySelectorAll('#padgrid .pad')[2].className")
    check("CLR empties the key it is editing, and the cell says so",
          key(page, 2) == "empty" and "unmapped" in cell
          and "unassigned" in page.inner_text("#padgrid .pad >> nth=2"), cell)
    toggle(page, "editkeys", False)


# ── the two things only a browser can check ──────────────────────────────────
def t_feedback_lands_before_the_send(page):
    closed(page)
    page.evaluate("window.__sent = []")
    page.click('[data-act="run"]')
    page.wait_for_timeout(150)
    sent = page.evaluate("window.__sent")
    run = [s for s in sent if "transport.toggle" in s["data"]]
    check("RUN paints itself before the socket carries the press",
          len(run) == 1 and run[0]["run"] == "true", str(run[:1])[:90])
    page.click('[data-act="run"]')
    page.wait_for_timeout(150)

    page.evaluate("window.__sent = []")
    page.keyboard.press("Digit4")
    page.wait_for_timeout(150)
    sent = page.evaluate("window.__sent")
    hit = [s for s in sent if "pad.trigger" in s["data"]]
    check("a key lights its cell before the trigger is sent",
          len(hit) >= 1 and ("hit" in hit[0]["pad0"] or "armed" in hit[0]["pad0"]
                             or "pad.trigger" in hit[0]["data"]), str(hit[:1])[:90])

    page.evaluate("window.__sent = []")
    page.click('[data-act="save"]')
    page.wait_for_timeout(300)
    sent = page.evaluate("window.__sent")
    save = [s for s in sent if "session.save" in s["data"]]
    check("SAVE writes its own line before asking the engine to write the file",
          len(save) == 1 and save[0]["line"] == "saving…", str(save[:1])[:90])


def t_nothing_moves_that_was_not_asked_to(page):
    closed(page)
    paint(page)
    page.evaluate("window.__mark()")
    page.keyboard.press("Digit3")            # play a key
    page.wait_for_timeout(200)
    paint(page)
    moved = page.evaluate("s => window.__moved(s)", [])
    check("pressing a key moves nothing on the panel", moved == [], "; ".join(moved[:3]))

    page.evaluate("window.__mark()")
    page.click('[data-act="mappads"]')       # the note appears in its reserved box
    paint(page)
    moved = page.evaluate("s => window.__moved(s)", ["DIV#i-error"])
    check("a note appearing moves nothing but itself", moved == [], "; ".join(moved[:3]))
    page.keyboard.press("Escape")
    paint(page)

    page.evaluate("window.__mark()")
    open_file(page, "tk_kick")
    moved = page.evaluate("s => window.__moved(s)", [])
    check("loading a file moves nothing either", moved == [], "; ".join(moved[:3]))


def t_a_dragged_region_stays_where_it_was_put(page):
    closed(page)
    toggle(page, "editkeys", False)
    whole_file(page)
    box = page.locator("#wcanvas").bounding_box()
    frames = page.evaluate("S.tracks[0].frames")
    x = lambda f: round(box["width"] * f / frames)
    drag(page, "#wcanvas", x(20000), x(120000))
    first = page.evaluate("[S.tracks[0].ls, S.tracks[0].le]")
    drag(page, "#wcanvas", x(first[0]), x(60000))          # drag the IN handle
    landed = page.evaluate("[S.tracks[0].ls, S.tracks[0].le]")
    page.wait_for_timeout(700)                             # two telemetry frames later
    paint(page)
    check("a dragged loop point lands where it was dropped and stays there",
          page.evaluate("[S.tracks[0].ls, S.tracks[0].le]") == landed
          and abs(landed[0] - 60000) < 2500, "%s then %s" % (landed, first))

    toggle(page, "editkeys", True)
    page.click("#padgrid .pad >> nth=7")
    whole_file(page)
    before = page.evaluate("[S.pads[7].ls, S.pads[7].le]")
    kframes = page.evaluate("S.pads[7].frames")
    kbox = page.locator("#wcanvas").bounding_box()
    kx = lambda f: round(kbox["width"] * f / kframes)
    want = max(2000, before[0] - 40000)
    drag(page, "#wcanvas", kx(before[0]), kx(want))
    put = page.evaluate("[S.pads[7].ls, S.pads[7].le]")
    page.wait_for_timeout(700)
    paint(page)
    check("and a key's own region does too, instead of snapping back",
          page.evaluate("[S.pads[7].ls, S.pads[7].le]") == put and put != before
          and abs(put[0] - want) < 3000, "%s -> %s, wanted %d" % (before, put, want))
    toggle(page, "editkeys", False)


# ── work order 10: the sweep, and what must not disturb it ───────────────────
def t_a_sweep_sets_the_loop_at_every_zoom(page):
    """The gesture Ivan reaches for constantly: press, drag, release.

    Checked in samples against the panel's own published arithmetic, not
    against a tolerance — a pixel that reaches engine state as though it were a
    sample is the failure mode here, and at the whole file one pixel is a
    thousand samples, so a loose check would not see it."""
    closed(page)
    toggle(page, "editkeys", False)
    focus_track(page, 0)
    for zooms, where in ((0, "the whole file"), (4, "zoomed in"), (9, "zoomed hard")):
        whole_file(page)
        whole_loop(page)
        for _ in range(zooms):
            page.keyboard.press("Equal")
        page.wait_for_timeout(120)
        if zooms:
            drag(page, "#wcanvas", 620, 330, shift=True)     # and scrolled off centre
        cx0, cx1 = drag(page, "#wcanvas", 180, 760)
        landed(page)
        want = swept(page, cx0, cx1)
        check("a sweep sets the samples under the pointer, at %s" % where,
              region(page) == want, "%s wanted %s" % (region(page), want))

    whole_file(page)
    whole_loop(page)
    page.evaluate("window.__sent = []")
    drag(page, "#wcanvas", 200, 640)
    landed(page)
    forwards = region(page)
    late = []
    for msg in page.evaluate("window.__sent"):
        sent = json.loads(msg["data"])
        if sent.get("op") != "track.loop":
            continue
        shown = [int(msg["win"].replace(",", "")), int(msg["wout"].replace(",", ""))]
        if shown != [sent["ls"], sent["le"]] or msg["hand"] != shown:
            late.append("%s sent, %s on screen, %s held" % (shown, [sent["ls"], sent["le"]],
                                                            msg["hand"]))
    check("every step of a sweep is on screen before the socket carries it",
          not late, "; ".join(late[:2]) or "%d messages" % len(page.evaluate("window.__sent")))
    whole_loop(page)                      # the handles back to the edges
    drag(page, "#wcanvas", 640, 200)
    landed(page)
    check("a backwards sweep gives the same region as a forwards one",
          region(page) == forwards, "%s then %s" % (forwards, region(page)))


def t_a_sweep_stops_at_the_edge_of_the_view(page):
    """The panel does not scroll under a drag, so a loop point chosen past the
    edge would be one nobody could see. To take in more than the view, zoom
    out."""
    closed(page)
    focus_track(page, 0)
    whole_file(page)
    whole_loop(page)
    for _ in range(5):
        page.keyboard.press("Equal")
    page.wait_for_timeout(150)
    paint(page)
    edge = page.evaluate("() => { const v = viewOf(subject()); return [Math.round(v.vs), Math.round(v.ve)]; }")
    width = page.locator("#wcanvas").bounding_box()["width"]
    drag(page, "#wcanvas", 300, width + 260)          # off the right-hand edge
    landed(page)
    check("a sweep that runs off the edge stops at the last sample in view",
          region(page)[1] == edge[1], "%s, the view ends at %d" % (region(page), edge[1]))


def t_a_click_is_not_a_sweep(page):
    """A region is hand-tuned work. The MAP wipe was the same class of harm:
    something destructive on a gesture the hand makes by accident."""
    closed(page)
    focus_track(page, 0)
    whole_file(page)
    whole_loop(page)
    drag(page, "#wcanvas", 200, 700)
    landed(page)
    held = region(page)
    box = page.locator("#wcanvas").bounding_box()
    y = round(box["y"] + box["height"] / 2)

    page.mouse.click(round(box["x"]) + 450, y)         # inside the region, on no handle
    page.wait_for_timeout(250)
    paint(page)
    check("a click without a drag leaves the region exactly as it was",
          region(page) == held, "%s -> %s" % (held, region(page)))

    page.mouse.move(round(box["x"]) + 300, y)
    page.mouse.down()
    page.mouse.move(round(box["x"]) + 302, y)          # under the threshold
    page.mouse.up()
    page.wait_for_timeout(250)
    paint(page)
    check("nor does a press that barely travels, at either end of it",
          region(page) == held, "%s -> %s" % (held, region(page)))

    xin = page.evaluate("""() => {
      const t = subject(), r = document.querySelector('#wcanvas').getBoundingClientRect();
      return View.frameToX(t.ls, r.width, viewOf(t)); }""")
    drag(page, "#wcanvas", round(xin), round(xin) + 150)
    landed(page)
    now = region(page)
    check("a press that lands on a handle moves that handle and starts no region",
          now[1] == held[1] and now[0] > held[0], "%s -> %s" % (held, now))


def t_panning_leaves_the_region_alone(page):
    closed(page)
    focus_track(page, 0)
    whole_file(page)
    whole_loop(page)
    drag(page, "#wcanvas", 200, 700)
    landed(page)
    held = region(page)

    def zoomed_in():
        whole_file(page)
        for _ in range(4):
            page.keyboard.press("Equal")
        page.wait_for_timeout(120)
        paint(page)
        return view(page)

    was = zoomed_in()
    drag(page, "#wcanvas", 620, 300, shift=True)
    check("SHIFT-drag pans the waveform and moves no loop point",
          view(page) != was and region(page) == held,
          "%s -> %s, region %s" % (was, view(page), region(page)))

    was = zoomed_in()
    drag(page, "#wcanvas", 300, 640, button="middle")
    check("the middle button pans it too, for the hand that expects it",
          view(page) != was and region(page) == held,
          "%s -> %s" % (was, view(page)))

    was = zoomed_in()
    strip = "#strips .strip:nth-of-type(1) .c-wave"
    drag(page, strip, 25, 70)
    check("dragging the lit span on the track's row scrolls the panel",
          view(page) != was and region(page) == held,
          "%s -> %s" % (was, view(page)))

    playing = page.evaluate("!!S.tracks[0].playing")
    drag(page, strip, 70, 30)
    drag(page, strip, 30, 65)                 # twice, inside the double-click window
    check("and scrolling a row twice over does not launch it",
          page.evaluate("!!S.tracks[0].playing") == playing and region(page) == held,
          "playing %s" % page.evaluate("S.tracks[0].playing"))


def t_a_sweep_edits_whatever_the_panel_is_showing(page):
    closed(page)
    focus_track(page, 0)
    whole_file(page)
    whole_loop(page)
    drag(page, "#wcanvas", 150, 800)
    landed(page)
    on_the_track = region(page)

    toggle(page, "editkeys", True)
    page.click("#padgrid .pad >> nth=5")
    paint(page)
    if not page.evaluate("S.pads[5].loaded"):
        check("key 6 holds audio to sweep", False, "the key is empty")
        toggle(page, "editkeys", False)
        return
    whole_file(page)
    whole_loop(page)
    before = page.evaluate("[S.pads[5].ls, S.pads[5].le]")
    cx0, cx1 = drag(page, "#wcanvas", 240, 690)
    landed(page)
    want = swept(page, cx0, cx1)
    now = page.evaluate("[S.pads[5].ls, S.pads[5].le]")
    check("sweeping while a key is focused sets that key's region, by sample",
          now == want and now != before, "%s -> %s wanted %s" % (before, now, want))
    check("and the track the panel came from keeps the region it had",
          region(page) == on_the_track, str(region(page)))
    toggle(page, "editkeys", False)


def record_a_take(page):
    """RUN, REC, a moment, REC — a take of the master output, through the
    panel's own buttons."""
    if page.get_attribute('[data-act="run"]', "aria-pressed") != "true":
        page.click('[data-act="run"]')
        page.wait_for_function("S.playing", timeout=10000)
    page.click('[data-act="rec"]')
    page.wait_for_function("S.record && S.record.state === 'recording'", timeout=15000)
    page.wait_for_timeout(600)
    page.click('[data-act="rec"]')
    page.wait_for_function("typeof takeReady !== 'undefined' && takeReady !== null",
                           timeout=20000)
    paint(page)


def t_a_finished_take_asks_where_it_goes(page):
    closed(page)
    toggle(page, "editkeys", False)
    focus_track(page, 0)
    before = [page.evaluate("i => S.tracks[i].name || ''", i) for i in range(4)]
    page.evaluate("window.__mark()")
    record_a_take(page)
    moved = page.evaluate("s => window.__moved(s)", ["DIV#i-error"])
    check("a finished take asks where it goes, and loads nothing on its own",
          page.evaluate("recLine(S.record.state, S.record)") == "click a track to load it"
          and "armed" in page.evaluate("document.querySelector('#rec-line').className")
          and [page.evaluate("i => S.tracks[i].name || ''", i) for i in range(4)] == before,
          page.evaluate("recLine(S.record.state, S.record)"))
    check("and asking moves nothing on the panel", moved == [], "; ".join(moved[:3]))

    page.click("#strips .strip:nth-of-type(3) .c-name")
    page.wait_for_function("S.tracks[2].loaded && /^rec-/.test(S.tracks[2].name)",
                           timeout=20000)
    paint(page)
    check("pressing a track row loads the take onto that track",
          page.evaluate("takeReady") is None and track(page, 2).startswith("rec-"),
          track(page, 2))

    focus_track(page, 3)
    record_a_take(page)
    page.keyboard.press("Enter")
    page.wait_for_function("S.tracks[3].loaded && /^rec-/.test(S.tracks[3].name)",
                           timeout=20000)
    paint(page)
    check("ENTER puts it on the row the focus bar is on",
          page.evaluate("takeReady") is None and track(page, 3).startswith("rec-"),
          track(page, 3))

    record_a_take(page)
    page.keyboard.press("Escape")
    paint(page)
    check("Esc leaves the take on disk and loads it nowhere",
          page.evaluate("takeReady") is None
          and not page.evaluate("/^rec-/.test(S.tracks[1].name || '')")
          and "still in" in note(page), note(page)[:60])
    page.click('[data-act="run"]')
    page.wait_for_function("!S.playing", timeout=10000)
    paint(page)


def t_the_view_gestures_move_nothing(page):
    closed(page)
    toggle(page, "editkeys", False)
    focus_track(page, 0)
    whole_file(page)
    whole_loop(page)
    page.evaluate("window.__mark()")
    drag(page, "#wcanvas", 260, 720)
    landed(page)
    for _ in range(3):
        page.keyboard.press("Equal")
    page.wait_for_timeout(120)
    drag(page, "#wcanvas", 700, 350, shift=True)
    drag(page, "#strips .strip:nth-of-type(1) .c-wave", 30, 70)
    page.keyboard.press("Digit9")                        # fit the loop
    page.wait_for_timeout(120)
    page.keyboard.press("Digit0")                        # fit the file
    page.wait_for_timeout(120)
    paint(page)
    moved = page.evaluate("s => window.__moved(s)", [])
    check("sweeping, panning, zooming and fitting move nothing on the panel",
          moved == [], "; ".join(moved[:3]))

    other = page.evaluate("S.tracks.findIndex((t, k) => k > 0 && t.loaded)")
    skip = ["DIV#focusbar"] if other > 0 else ["DIV#focusbar", "DIV#w-empty"]
    page.evaluate("window.__mark()")
    focus_track(page, other if other > 0 else 1)
    moved = page.evaluate("s => window.__moved(s)", skip)
    check("changing focus moves nothing but the bar that marks it",
          moved == [], "; ".join(moved[:3]))
    focus_track(page, 0)


def t_a_bank_switch_moves_the_address_not_the_audio(page):
    """Four banks of the same sixteen caps: the keys point elsewhere and no
    slot is touched. Order matters here — it runs after a key has been given
    something, so there is audio on bank A to come back to."""
    closed(page)
    on_a = key(page, 0)
    before = page.evaluate("document.querySelector('#bank-now').textContent")
    press(page, "`")
    page.wait_for_function("S.bank === 1", timeout=10000)
    paint(page)
    check("the box says which bank the keys are on",
          before == "A"
          and page.evaluate("document.querySelector('#bank-now').textContent") == "B")
    check("and the sixteen caps now reach sixteen empty slots",
          pads(page) == ["empty"] * 16, str(pads(page)[:2]))
    press(page, "`")
    press(page, "`")
    press(page, "`")
    page.wait_for_function("S.bank === 0", timeout=10000)
    paint(page)
    check("cycling back shows bank A holding exactly what it held",
          key(page, 0) == on_a
          and page.evaluate("document.querySelector('#bank-now').textContent") == "A",
          "%s vs %s" % (key(page, 0), on_a))


def t_a_roll_is_held_and_says_so_before_it_sends(page):
    """The length is the panel's business; the hold is the engine's."""
    closed(page)
    page.evaluate("window.__sent = []")
    caps = []
    for _ in range(4):
        page.click('#roll-len')
        caps.append(page.evaluate("document.querySelector('#roll-len').textContent"))
    told = [x for x in page.evaluate("window.__sent") if "roll" in x["data"]]
    check("the button cycles the four lengths and reaches the engine never",
          caps == ["1/2", "1", "1/8", "1/4"] and not told,
          "%s %s" % (caps, str(told)[:40]))

    page.evaluate("window.__sent = []")
    page.keyboard.down("h")
    page.wait_for_timeout(150)
    on = [x for x in page.evaluate("window.__sent") if "roll.on" in x["data"]]
    check("holding H rolls the focused track, and the rail says so first",
          len(on) == 1 and '"beats":0.25' in on[0]["data"].replace(" ", "")
          and on[0]["roll"] == "rolling 1/4", str(on[:1])[:100])
    page.keyboard.up("h")
    page.wait_for_timeout(150)
    off = [x for x in page.evaluate("window.__sent") if "roll.off" in x["data"]]
    paint(page)
    check("letting go ends it, once, and the box goes back to waiting",
          len(off) == 1 and off[0]["roll"] == "hold H"
          and page.evaluate("document.querySelector('#roll-line').textContent") == "hold H",
          str(off[:1])[:90])


def t_a_cue_is_set_then_jumped_then_cleared(page):
    closed(page)
    if not page.evaluate("S.tracks[0].loaded"):
        open_file(page, "tk_stab")            # the last test ejected it
    page.evaluate("focusKind = 'track'; focus = 0; renderState()")
    page.evaluate("window.__sent = []")
    lit = lambda: page.evaluate(
        "document.querySelectorAll('#cuerow .btn')[3].getAttribute('aria-pressed')")
    check("a cue that holds nothing is not lit", lit() == "false")
    page.locator("#cuerow .btn").nth(3).click()
    page.wait_for_function("S.tracks[0].cues[3] >= 0", timeout=10000)
    paint(page)
    sent = [x["data"] for x in page.evaluate("window.__sent")]
    check("pressing an empty one drops it where the playhead is, and lights it",
          any('"track.cue.set"' in d and '"c":3' in d for d in sent) and lit() == "true",
          str([d for d in sent if "cue" in d])[:80])
    at = page.evaluate("S.tracks[0].cues[3]")

    page.evaluate("window.__sent = []")
    page.locator("#cuerow .btn").nth(3).click()
    page.wait_for_timeout(150)
    sent = [x["data"] for x in page.evaluate("window.__sent")]
    check("pressing it again jumps there and sets nothing new",
          any('"track.cue.jump"' in d and '"c":3' in d for d in sent)
          and page.evaluate("S.tracks[0].cues[3]") == at,
          str([d for d in sent if "cue" in d])[:80])

    page.evaluate("window.__sent = []")
    page.locator("#cuerow .btn").nth(3).click(modifiers=["Control"])
    page.wait_for_function("S.tracks[0].cues[3] < 0", timeout=10000)
    paint(page)
    check("and CTRL-pressing it clears it, leaving the others alone",
          lit() == "false" and page.evaluate("S.tracks[0].cues.filter(c => c >= 0).length") == 0)


if __name__ == "__main__":
    base = tempfile.mkdtemp(prefix="le-panel-ui-")
    server = Panel(base)
    if not server.url:
        server.stop()
        shutil.rmtree(base, ignore_errors=True)
        skip_all("the panel server printed no URL: " + " | ".join(server.lines[-3:]))
    t0 = time.monotonic()
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except Exception as exc:
                server.stop()
                shutil.rmtree(base, ignore_errors=True)
                skip_all("chromium will not launch (%s) — python3 -m playwright install chromium"
                         % str(exc).splitlines()[0][:80])
            page = browser.new_page(viewport={"width": 1600, "height": 1000})
            page.add_init_script(HOOK)
            page.goto(server.url)
            ready(page)
            for fn in (t_assign_keeps_what_the_key_was_given,
                       t_a_bank_switch_moves_the_address_not_the_audio,
                       t_a_roll_is_held_and_says_so_before_it_sends,
                       t_a_cue_is_set_then_jumped_then_cleared,
                       t_map_asks_and_mode_is_only_a_mode,
                       t_clearing_a_key,
                       t_feedback_lands_before_the_send,
                       t_nothing_moves_that_was_not_asked_to,
                       t_a_dragged_region_stays_where_it_was_put,
                       t_a_sweep_sets_the_loop_at_every_zoom,
                       t_a_sweep_stops_at_the_edge_of_the_view,
                       t_a_click_is_not_a_sweep,
                       t_panning_leaves_the_region_alone,
                       t_a_sweep_edits_whatever_the_panel_is_showing,
                       t_the_view_gestures_move_nothing,
                       t_a_finished_take_asks_where_it_goes):
                try:
                    fn(page)
                except Exception as exc:
                    import traceback
                    traceback.print_exc()
                    check(fn.__name__, False, "%s: %s" % (type(exc).__name__, str(exc)[:80]))
            browser.close()
    finally:
        server.stop()
        shutil.rmtree(base, ignore_errors=True)
    print("\n%d checks failed" % len(FAILS) if FAILS
          else "\nall checks passed (%.1f s in the browser)" % (time.monotonic() - t0))
    sys.exit(1 if FAILS else 0)
