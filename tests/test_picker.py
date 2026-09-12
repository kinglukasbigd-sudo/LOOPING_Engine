"""Native dialog: backend choice, cancel, and the capability grant.

    PYTHONPATH=.pylibs python3 -m tests.test_picker
"""
from __future__ import annotations

import os
import sys
import types

from loopengine import picker
from loopengine.app import App

FAILS = []


def check(name, ok, detail=""):
    print("%-52s %s%s" % (name, "PASS" if ok else "FAIL",
                          ("  " + detail) if detail else ""))
    if not ok:
        FAILS.append(name)


class FakeEngine:
    def __init__(self):
        self.tracks = [types.SimpleNamespace(src=None) for _ in range(8)]
        self.last_error = ""
        self.posted = []

    def post(self, op, **kw):
        self.posted.append((op, kw))


def make_app(tmp_root):
    return App(FakeEngine(), roots=[tmp_root], inbox=os.path.join(tmp_root, ".inbox"))


def t_backend_reports_a_reason_when_headless():
    d, w = os.environ.pop("DISPLAY", None), os.environ.pop("WAYLAND_DISPLAY", None)
    try:
        name, reason = picker.backend()
        check("headless reports no backend and says why",
              name is None and "display" in reason.lower(), reason[:44])
        check("the headless reason points at the working fallback",
              "LOAD panel" in reason)
    finally:
        if d is not None:
            os.environ["DISPLAY"] = d
        if w is not None:
            os.environ["WAYLAND_DISPLAY"] = w


def t_backend_found_here():
    name, reason = picker.backend()
    check("a backend is available on this machine", name is not None,
          name or reason[:40])


def t_cancel_is_not_an_error(monkey_rc=1):
    """Exit code 1 from zenity means the user closed it. That is an outcome."""
    import subprocess
    real = subprocess.run

    def fake(cmd, **kw):
        return types.SimpleNamespace(returncode=monkey_rc, stdout="", stderr="")
    subprocess.run = fake
    try:
        paths, cancelled, err = picker.pick()
    finally:
        subprocess.run = real
    check("cancel returns cancelled with no error",
          paths == [] and cancelled is True and err == "",
          "paths=%r cancelled=%r err=%r" % (paths, cancelled, err))


def t_grant_lets_a_picked_path_past_the_root_check(tmp="/tmp"):
    app = make_app(os.path.join(tmp, "loopengine-root-test"))
    outside = "/usr/share/sounds/alsa/Front_Center.wav"
    check("a path outside --root is refused before it is picked",
          not app._inside_roots(outside))
    app._grant(outside)
    check("the same path is allowed once the dialog returned it",
          app._inside_roots(outside))
    other = "/etc/passwd"
    check("granting one path does not open the rest of the disk",
          not app._inside_roots(other))


def t_grant_set_is_bounded():
    app = make_app("/tmp/loopengine-root-test")
    for i in range(App.GRANT_MAX + 20):
        app._grant("/tmp/pick-%d.wav" % i)
    check("the grant set stays bounded",
          len(app._granted) == App.GRANT_MAX, "%d entries" % len(app._granted))
    check("the oldest grant is evicted first",
          not app._inside_roots("/tmp/pick-0.wav")
          and app._inside_roots("/tmp/pick-%d.wav" % (App.GRANT_MAX + 19)))


def t_unavailable_backend_answers_over_the_socket():
    """A machine with no dialog must still answer, not leave the slot waiting."""
    app = make_app("/tmp/loopengine-root-test")
    sent = []
    app.hub = types.SimpleNamespace(text=lambda o: sent.append(o))
    real = picker.backend
    picker.backend = lambda: (None, "no display here")
    try:
        req, info = app.pick_async(3, False)
    finally:
        picker.backend = real
    check("an impossible dialog still replies", len(sent) == 1)
    check("the reply names the slot and carries the reason",
          sent and sent[0]["track"] == 3 and sent[0]["error"] == "no display here"
          and sent[0]["cancelled"] is False,
          str(sent[0]) if sent else "")
    check("it is reported unavailable to the caller", info["available"] is False)


if __name__ == "__main__":
    for fn in (t_backend_reports_a_reason_when_headless, t_backend_found_here,
               t_cancel_is_not_an_error, t_grant_lets_a_picked_path_past_the_root_check,
               t_grant_set_is_bounded, t_unavailable_backend_answers_over_the_socket):
        try:
            fn()
        except Exception as exc:
            check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    print("\n%d checks failed" % len(FAILS) if FAILS else "\nall checks passed")
    sys.exit(1 if FAILS else 0)
