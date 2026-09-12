"""WebSocket frame codec. All four length paths plus reassembly.

    PYTHONPATH=.pylibs python3 -m tests.test_protocol
"""
from __future__ import annotations

import io
import struct
import sys

from loopengine.server import (encode_frame, read_frame, OP_TEXT, OP_CONT,
                               OP_BIN, OP_CLOSE, OP_PING)

FAILS = []


def check(name, ok, detail=""):
    print("%-46s %s%s" % (name, "PASS" if ok else "FAIL",
                          ("  " + detail) if detail else ""))
    if not ok:
        FAILS.append(name)


def client_frame(op, payload, fin=True):
    """Encode the way a browser does: masked, with the right length path."""
    m = b"\x5a\x3c\x71\x0e"
    p = bytes(b ^ m[i & 3] for i, b in enumerate(payload))
    n = len(p)
    b0 = (0x80 if fin else 0) | op
    if n < 126:
        hdr = struct.pack("!BB", b0, 0x80 | n)
    elif n < 65536:
        hdr = struct.pack("!BBH", b0, 0x80 | 126, n)
    else:
        hdr = struct.pack("!BBQ", b0, 0x80 | 127, n)
    return hdr + m + p


def one(raw):
    return read_frame(io.BytesIO(raw))


if __name__ == "__main__":
    op, d = one(client_frame(OP_TEXT, b'{"op":"panic"}'))
    check("7-bit length, unmasked on the way out",
          op == OP_TEXT and d == b'{"op":"panic"}')

    op, d = one(client_frame(OP_TEXT, b"x" * 300))
    check("16-bit length path", op == OP_TEXT and len(d) == 300)

    op, d = one(client_frame(OP_BIN, b"y" * 70000))
    check("64-bit length path", op == OP_BIN and len(d) == 70000)

    op, d = one(client_frame(OP_TEXT, b'{"op":"tra', fin=False)
                + client_frame(OP_CONT, b'ck.play"}'))
    check("fragmented message is reassembled",
          op == OP_TEXT and d == b'{"op":"track.play"}', d.decode())

    op, d = one(client_frame(OP_TEXT, b'{"op":"a', fin=False)
                + client_frame(OP_PING, b"hb", fin=True)
                + client_frame(OP_CONT, b'"}'))
    check("a ping between fragments does not corrupt the message",
          op == OP_TEXT and d == b'{"op":"a"}', d.decode())

    op, d = one(client_frame(OP_TEXT, b"a", fin=False)
                + client_frame(OP_TEXT, b"b"))
    check("a second non-continuation frame hangs the connection up",
          op == OP_CLOSE)

    op, d = one(b"")
    check("truncated stream returns end-of-stream", op is None)

    check("server frames go out unmasked with FIN set",
          encode_frame(OP_TEXT, b"hi") == b"\x81\x02hi",
          encode_frame(OP_TEXT, b"hi").hex())
    check("server 16-bit length path",
          encode_frame(OP_BIN, b"z" * 300)[:4] == b"\x82\x7e\x01\x2c")

    print("\n%d checks failed" % len(FAILS) if FAILS else "\nall checks passed")
    sys.exit(1 if FAILS else 0)
