"""What the audio graph is actually doing below us.

The engine folds a file's sample rate into the playback step so nothing is
ever resampled on load. That property is undone at the last hop if the stream
is opened at a rate the graph does not run at — the server silently inserts a
resampler and every sample goes through an interpolator the engine never chose.

A user cannot see that. This module finds the rate the hardware is really
running at so the panel can say so.

Linux only, and best-effort by design: an unknown native rate reports None and
the panel simply does not claim anything.
"""
from __future__ import annotations

import glob
import os
import re
import subprocess

PREFERRED = 48000


def alsa_native_rate():
    """The rate of a RUNNING playback substream, straight from the kernel."""
    for status in sorted(glob.glob("/proc/asound/card*/pcm*p/sub*/status")):
        try:
            if "RUNNING" not in open(status).read():
                continue
            hw = os.path.join(os.path.dirname(status), "hw_params")
            m = re.search(r"^rate:\s*(\d+)", open(hw).read(), re.M)
            if m:
                return int(m.group(1))
        except OSError:
            continue
    return None


def pipewire_sink_rate():
    """The graph's sink rate, if pipewire is running. Empty if it is not."""
    try:
        out = subprocess.run(["pw-top", "-b", "-n", "2"], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:
        return None
    best = None
    for ln in out.splitlines():
        if "alsa_output" not in ln:
            continue
        m = re.match(r"^R\s+\d+\s+\d+\s+(\d+)\s", ln)
        if m:
            best = int(m.group(1))
    return best


def native_output_rate():
    """-> (rate, how). rate is None when nothing could be established."""
    r = pipewire_sink_rate()
    if r:
        return r, "pipewire sink"
    r = alsa_native_rate()
    if r:
        return r, "alsa substream"
    return None, "unknown"


def choose_rate(device_default, supports):
    """Prefer the rate the graph runs at; fall back only when refused.

    `supports(rate) -> bool` is passed in so this stays testable without a
    sound card.
    """
    native, _ = native_output_rate()
    for candidate in (native, PREFERRED, device_default):
        if candidate and supports(int(candidate)):
            return int(candidate)
    return int(device_default)
