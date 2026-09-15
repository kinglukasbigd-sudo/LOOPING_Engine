"""Sessions: the set as a file, and the grants that come back with it.

A session is plain, versioned JSON, because someone will hand-edit it. It holds
the tracks with their files, loop regions, gain, pan, speed, reverse, mute,
solo and source mode; the keys with their own file references, source mode and
regions; tempo, quantum and master level; and the panel's zoom for each track
and key.

Files outside --root were readable only because the OS dialog granted them, for
that run. Re-prompting for up to 24 paths on every load would be unusable, so
the grants are kept in the session — but a path in a JSON file is only a claim.
On load every file is re-validated: still there, still readable, still the same
size and the same bytes at both ends. And a path outside the roots is honoured
only if it is covered by a signature this installation made when it saved the
grant, so a hand-edited or foreign session cannot grant itself the disk.
Editing a region or a gain by hand is fine. Editing a granted path is not.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time

KIND = "loopengine_session"
VERSION = 1
EDGE = 1 << 20          # bytes hashed at each end of a file


class SessionError(Exception):
    """A session that cannot be read or used, with a sentence a person can act on."""


def default_name():
    return time.strftime("set-%Y%m%d-%H%M%S.json")


def fingerprint(path):
    """Size, modification time and a hash of the first and last MiB.

    The hash is what "still the same file" means: a copy that kept the bytes
    but not the timestamp is the same file; a re-export with the same name is
    not. Hashing the ends rather than the whole keeps a 100 MB file cheap."""
    st = os.stat(path)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read(EDGE))
        if st.st_size > 2 * EDGE:
            f.seek(-EDGE, os.SEEK_END)
            h.update(f.read(EDGE))
        elif st.st_size > EDGE:
            h.update(f.read())
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns,
            "edges_sha256": h.hexdigest()}


def check_file(entry):
    """None if the file is still the one the session saved, else why not."""
    p = entry.get("path") if isinstance(entry, dict) else None
    if not isinstance(p, str) or not p:
        return "has no path"
    if not os.path.exists(p):
        return "is not there any more — moved or deleted"
    if not os.path.isfile(p):
        return "is no longer a file"
    try:
        fp = fingerprint(p)
    except PermissionError:
        return "is not readable"
    except OSError as e:
        return "is not readable (%s)" % (e.strerror or e)
    if fp["size"] != entry.get("size") or fp["edges_sha256"] != entry.get("edges_sha256"):
        return "has changed since the session was saved"
    return None


def install_key(path):
    """The per-installation secret that signs grants. Made once, owner-only."""
    try:
        with open(path, "rb") as f:
            k = f.read()
        if len(k) >= 32:
            return k
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
    k = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(k)
    return k


def _grant_message(entries):
    rows = sorted([e.get("path"), e.get("size"), e.get("edges_sha256")]
                  for e in entries)
    return json.dumps(rows, separators=(",", ":")).encode("utf-8")


def sign_grants(key, entries):
    return hmac.new(key, _grant_message(entries), hashlib.sha256).hexdigest()


def grants_verify(key, entries, sig):
    return isinstance(sig, str) and hmac.compare_digest(sign_grants(key, entries), sig)


def write(doc, path):
    """Atomically: a crash mid-save leaves the previous session, not half of one."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".session-", suffix=".json", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read(path):
    name = os.path.basename(path)
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        raise SessionError("There is no session at %s." % path)
    except json.JSONDecodeError as e:
        raise SessionError("%s is not valid JSON — line %d, column %d: %s."
                           % (name, e.lineno, e.colno, e.msg))
    except OSError as e:
        raise SessionError("%s could not be read — %s." % (name, e.strerror or e))
    if not isinstance(doc, dict) or KIND not in doc:
        raise SessionError("%s is not a LOOP ENGINE session." % name)
    if doc[KIND] != VERSION:
        raise SessionError("%s is session format %r; this build reads format %d."
                           % (name, doc[KIND], VERSION))
    return doc


def listing(folder):
    """Sessions in a folder, newest first, for the panel's OPEN list."""
    rows = []
    try:
        names = os.listdir(folder)
    except OSError:
        return rows
    for n in names:
        if not n.endswith(".json") or n.startswith("."):
            continue
        p = os.path.join(folder, n)
        try:
            st = os.stat(p)
            doc = read(p)
            tracks, keys = doc.get("tracks"), doc.get("keys")     # hand-edited: any type
            rows.append({"name": n, "saved": str(doc.get("saved", "")),
                         "tracks": len(tracks) if isinstance(tracks, list) else 0,
                         "keys": len(keys) if isinstance(keys, list) else 0,
                         "size": st.st_size, "mtime_ns": st.st_mtime_ns, "error": ""})
        except (OSError, SessionError) as e:
            mt = 0
            try:
                mt = os.stat(p).st_mtime_ns
            except OSError:
                pass
            rows.append({"name": n, "saved": "", "tracks": 0, "keys": 0,
                         "size": 0, "mtime_ns": mt, "error": str(e)})
    # Newest first. Two saves inside one timestamp tick are a tie a filesystem
    # will happily produce, so the name breaks it rather than directory order.
    rows.sort(key=lambda r: (r["mtime_ns"], r["name"]), reverse=True)
    return rows
