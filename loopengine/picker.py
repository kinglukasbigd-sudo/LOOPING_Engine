"""The operating system's own file dialog, opened by the server.

A browser `<input type="file">` hands back a File object and a bare filename,
never a real path — browsers withhold it deliberately, and 127.0.0.1 is no
exception. The engine loads by path from disk. Routing a file that is already
on the server's own disk up through the browser would mean an upload endpoint
and a large binary transfer over the control channel to end up back where it
started.

So the dialog runs here instead and returns the path the existing load call
already takes. Zero bytes move.

Backends, in order of preference. zenity and kdialog are subprocesses, so no
GUI toolkit is ever imported into the server process; tkinter is the fallback
and needs its own thread with its own root.
"""
from __future__ import annotations

import os
import shutil
import subprocess

AUDIO_EXTS = ("wav", "wave", "flac", "ogg", "oga", "opus", "mp3",
              "m4a", "aac", "aif", "aiff", "aifc", "caf", "w64", "wv")


def _globs():
    out = []
    for e in AUDIO_EXTS:
        out.append("*." + e)
        out.append("*." + e.upper())
    return out


def backend():
    """-> (name, reason). name is None when no dialog is possible here."""
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return None, ("No X11 or Wayland display is attached to this machine, "
                      "so no file dialog can open. Use the LOAD panel, which "
                      "browses by path.")
    for exe in ("zenity", "kdialog"):
        if shutil.which(exe):
            return exe, ""
    try:
        import tkinter  # noqa: F401
        return "tkinter", ""
    except ImportError:
        pass
    return None, ("There is a display but no zenity, no kdialog and no "
                  "tkinter, so no file dialog can open. Install zenity, or "
                  "use the LOAD panel, which browses by path.")


def _zenity(multiple, start_dir, timeout):
    cmd = ["zenity", "--file-selection", "--title=LOOP ENGINE — load audio"]
    if start_dir:
        cmd.append("--filename=%s" % (start_dir.rstrip("/") + "/"))
    cmd.append("--file-filter=Audio | " + " ".join(_globs()))
    cmd.append("--file-filter=All files | *")
    if multiple:
        cmd += ["--multiple", "--separator=\n"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    # 0 selected, 1 cancelled or closed, 5 timed out
    if p.returncode != 0:
        return [], True, ""
    return [l for l in p.stdout.strip().split("\n") if l], False, ""


def _kdialog(multiple, start_dir, timeout):
    filt = " ".join(_globs()) + "|Audio files"
    cmd = ["kdialog", "--getopenfilename", start_dir or os.path.expanduser("~"),
           filt]
    if multiple:
        cmd += ["--multiple", "--separate-output"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        return [], True, ""
    return [l for l in p.stdout.strip().split("\n") if l], False, ""


def _tkinter(multiple, start_dir, timeout):
    """Runs on whatever thread calls it, which must not be the audio thread
    and must not be an HTTP handler thread holding a request open."""
    import tkinter
    from tkinter import filedialog
    root = tkinter.Tk()
    root.withdraw()
    try:
        types = [("Audio", " ".join(_globs())), ("All files", "*")]
        if multiple:
            got = filedialog.askopenfilenames(
                parent=root, title="LOOP ENGINE — load audio",
                initialdir=start_dir or os.path.expanduser("~"),
                filetypes=types)
            paths = list(got)
        else:
            got = filedialog.askopenfilename(
                parent=root, title="LOOP ENGINE — load audio",
                initialdir=start_dir or os.path.expanduser("~"),
                filetypes=types)
            paths = [got] if got else []
    finally:
        try:
            root.destroy()
        except Exception:
            pass
    return paths, (not paths), ""


def pick(multiple=False, start_dir=None, timeout=300):
    """Block until the user chooses or cancels.

    -> (paths, cancelled, error). Cancel is a normal outcome, not an error:
    it comes back as ([], True, "").
    """
    name, reason = backend()
    if name is None:
        return [], False, reason
    fn = {"zenity": _zenity, "kdialog": _kdialog, "tkinter": _tkinter}[name]
    try:
        return fn(multiple, start_dir, timeout)
    except subprocess.TimeoutExpired:
        return [], True, ""
    except Exception as e:
        return [], False, "The %s dialog failed: %s" % (name, e)
