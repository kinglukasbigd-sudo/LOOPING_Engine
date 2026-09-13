"""Local control surface: stdlib HTTP + a hand-rolled WebSocket.

No web framework. The socket work is ~150 lines and keeps the dependency list
at numpy / sounddevice / soundfile, which is the point.

Bound to 127.0.0.1 and gated on a per-run token so a page you happen to have
open elsewhere can't drive your sound system.
"""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import posixpath
import secrets
import struct
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")

BIN_SCOPE = 0x10
BIN_PEAKS = 0x11
BIN_PEAKS_PAD = 0x12

OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


# ---------------------------------------------------------------------------
# frame codec
# ---------------------------------------------------------------------------
def encode_frame(op: int, payload: bytes) -> bytes:
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", 0x80 | op, n)
    elif n < 65536:
        head = struct.pack("!BBH", 0x80 | op, 126, n)
    else:
        head = struct.pack("!BBQ", 0x80 | op, 127, n)
    return head + payload


def _read_one(rfile):
    """-> (fin, opcode, payload). (None, None, None) at end of stream."""
    hdr = rfile.read(2)
    if len(hdr) < 2:
        return None, None, None
    b0, b1 = hdr[0], hdr[1]
    fin = bool(b0 & 0x80)
    op = b0 & 0x0F
    masked = bool(b1 & 0x80)
    ln = b1 & 0x7F
    if ln == 126:
        ln = struct.unpack("!H", rfile.read(2))[0]
    elif ln == 127:
        ln = struct.unpack("!Q", rfile.read(8))[0]
    if ln > 32 * 1024 * 1024:
        return True, OP_CLOSE, b""
    mask = rfile.read(4) if masked else None
    data = rfile.read(ln) if ln else b""
    if mask:
        d = bytearray(data)
        for i in range(len(d)):
            d[i] ^= mask[i & 3]
        data = bytes(d)
    return fin, op, data


def read_frame(rfile):
    """One complete message, reassembling continuation frames.

    Browsers do not fragment the small JSON commands this panel sends, but a
    reader that silently hands a half message to json.loads is a bug waiting
    for the one client that does.
    """
    fin, op, data = _read_one(rfile)
    if op is None:
        return None, None
    if op in (OP_CLOSE, OP_PING, OP_PONG):
        return op, data
    buf = [data]
    while not fin:
        fin, cont_op, more = _read_one(rfile)
        if cont_op is None:
            return None, None
        if cont_op in (OP_PING, OP_PONG):      # control frames may interleave
            fin = False
            continue
        if cont_op == OP_CLOSE:
            return OP_CLOSE, b""
        if cont_op != OP_CONT:
            return OP_CLOSE, b""               # protocol violation, hang up
        buf.append(more)
        if sum(len(b) for b in buf) > 32 * 1024 * 1024:
            return OP_CLOSE, b""
    return op, b"".join(buf)


# ---------------------------------------------------------------------------
# hub
# ---------------------------------------------------------------------------
class Client:
    def __init__(self, conn):
        self.conn = conn
        self.lock = threading.Lock()
        self.alive = True

    def send(self, op, payload):
        if not self.alive:
            return False
        try:
            with self.lock:
                self.conn.sendall(encode_frame(op, payload))
            return True
        except OSError:
            self.alive = False
            return False


class Hub:
    def __init__(self):
        self.clients = []
        self.lock = threading.Lock()

    def add(self, c):
        with self.lock:
            self.clients.append(c)

    def drop(self, c):
        with self.lock:
            if c in self.clients:
                self.clients.remove(c)

    def broadcast(self, op, payload):
        with self.lock:
            targets = list(self.clients)
        for c in targets:
            if not c.send(op, payload):
                self.drop(c)

    def text(self, obj):
        self.broadcast(OP_TEXT, json.dumps(obj, separators=(",", ":")).encode())

    def peaks(self, track_i, arr: np.ndarray, kind=BIN_PEAKS):
        q = np.clip(arr * 127.0, -127, 127).astype(np.int8)
        head = struct.pack("!BBH", kind, track_i & 0xFF, q.shape[0])
        self.broadcast(OP_BIN, head + q.tobytes())

    def pad_peaks(self, pad_i, arr: np.ndarray):
        """A key keeps its own envelope: the track it came from may be gone."""
        self.peaks(pad_i, arr, kind=BIN_PEAKS_PAD)


# ---------------------------------------------------------------------------
# handler
# ---------------------------------------------------------------------------
def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "loopengine"

        def log_message(self, fmt, *args):
            pass

        # -- helpers -------------------------------------------------------
        def _q(self):
            return urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query)

        def _authed(self):
            return self._q().get("t", [""])[0] == app.token

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _bytes(self, body, ctype):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        # -- routes --------------------------------------------------------
        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/ws":
                return self._ws()
            if path.startswith("/api/"):
                if not self._authed():
                    return self._json({"error": "bad token"}, 403)
                return self._api_get(path)
            return self._static(path)

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            if not self._authed():
                return self._json({"error": "bad token"}, 403)
            if path == "/api/upload":
                return self._upload()
            if path == "/api/pick":
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                req, info = app.pick_async(int(body.get("track", 0)),
                                           bool(body.get("multiple")),
                                           body.get("dir"))
                return self._json({"req": req, **info})
            return self._json({"error": "no such endpoint"}, 404)

        def _static(self, path):
            if path in ("/", "/index.html"):
                with open(os.path.join(UI_DIR, "index.html"), "rb") as f:
                    html = f.read().decode()
                html = html.replace("{{TOKEN}}", app.token)
                return self._bytes(html.encode(), "text/html; charset=utf-8")
            name = posixpath.normpath(path).lstrip("/")
            full = os.path.join(UI_DIR, name)
            if not os.path.abspath(full).startswith(UI_DIR) or \
                    not os.path.isfile(full):
                return self._json({"error": "not found"}, 404)
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            with open(full, "rb") as f:
                return self._bytes(f.read(), ctype)

        def _api_get(self, path):
            q = self._q()
            if path == "/api/browse":
                return self._json(app.browse(q.get("dir", [""])[0]))
            if path == "/api/devices":
                return self._json(app.devices())
            if path == "/api/latency":
                return self._json(app.engine.latency())
            if path == "/api/pick":
                return self._json(app.pick_backend())
            return self._json({"error": "no such endpoint"}, 404)

        def _upload(self):
            n = int(self.headers.get("Content-Length", "0"))
            name = self.headers.get("X-Filename", "dropped.wav")
            data = self.rfile.read(n) if n else b""
            try:
                p = app.stash(name, data)
                return self._json({"path": p})
            except Exception as e:
                return self._json({"error": str(e)}, 400)

        # -- websocket -----------------------------------------------------
        def _ws(self):
            if not self._authed():
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            key = self.headers.get("Sec-WebSocket-Key")
            if not key or "websocket" not in \
                    (self.headers.get("Upgrade") or "").lower():
                self.send_response(400)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            accept = base64.b64encode(
                hashlib.sha1(key.encode() + WS_GUID).digest()).decode()
            self.wfile.write(
                ("HTTP/1.1 101 Switching Protocols\r\n"
                 "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                 "Sec-WebSocket-Accept: %s\r\n\r\n" % accept).encode())
            self.wfile.flush()

            client = Client(self.connection)
            app.hub.add(client)
            app.on_client_join(client)
            try:
                while client.alive:
                    op, data = read_frame(self.rfile)
                    if op is None or op == OP_CLOSE:
                        break
                    if op == OP_PING:
                        client.send(OP_PONG, data)
                    elif op == OP_TEXT:
                        try:
                            app.handle(json.loads(data.decode()))
                        except Exception as e:
                            app.engine.last_error = "%s: %s" % (
                                type(e).__name__, e)
            except (OSError, struct.error):
                pass
            finally:
                client.alive = False
                app.hub.drop(client)
                self.close_connection = True

    return Handler


# ---------------------------------------------------------------------------
class Server:
    def __init__(self, app, host="127.0.0.1", port=7331):
        self.app = app
        self.host = host
        self.httpd = ThreadingHTTPServer((host, port), make_handler(app))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]

    @property
    def url(self):
        return "http://%s:%d/?t=%s" % (self.host, self.port, self.app.token)

    def serve_forever(self):
        self.httpd.serve_forever()

    def start(self):
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self

    def shutdown(self):
        self.httpd.shutdown()


def new_token():
    return secrets.token_urlsafe(18)
