#!/usr/bin/env python3
"""Assemble site/app/ — the panel, running against the browser engine.

The panel's files are copied, not forked. web/ holds only what the browser
build adds: the engine itself (worklet.js), the analysis (analyze.js) and the
shim that puts them where the socket used to be (host.js). host.js is loaded
before app.js and replaces window.WebSocket, so app.js is byte-identical to
the one the Python build serves and there is exactly one panel to maintain.
"""
import pathlib
import shutil

ROOT = pathlib.Path(__file__).resolve().parent.parent
UI = ROOT / "loopengine" / "ui"
WEB = ROOT / "web"
OUT = ROOT / "site" / "app"


def build():
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    for name in ("app.js", "view.js", "app.css", "tokens.css"):
        shutil.copy2(UI / name, OUT / name)
    shutil.copytree(UI / "fonts", OUT / "fonts")
    for name in ("worklet.js", "analyze.js", "host.js"):
        shutil.copy2(WEB / name, OUT / name)

    html = (UI / "index.html").read_text()
    # There is no server to authenticate to, so the token the Python build
    # stamps in is empty and every ?t= in app.js resolves to nothing.
    html = html.replace("{{TOKEN}}", "")
    # host.js first: it installs the engine behind window.WebSocket, and app.js
    # opens that socket on its first line of work.
    html = html.replace('<script src="view.js"></script>',
                        '<script src="host.js"></script>\n<script src="view.js"></script>')
    (OUT / "index.html").write_text(html)

    n = sum(1 for _ in OUT.rglob("*") if _.is_file())
    kb = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file()) // 1024
    print(f"site/app: {n} files, {kb} KB")


if __name__ == "__main__":
    build()
