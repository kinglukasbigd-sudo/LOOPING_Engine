#!/usr/bin/env python3
"""Assemble public/ — the whole site, which is mostly the instrument itself.

    python3 web/build.py

The address is the tool. https://loop-engine-dj.web.app opens the panel and
starts an engine; the page describing it sits at /artifact, reachable from the
panel's own help sheet, because somebody who has the instrument in front of
them does not need to be sold it.

The panel's files are copied, not forked. web/ holds only what the browser
build adds: the engine (worklet.js), the analysis (analyze.js) and the shim
that puts them where the socket used to be (host.js). host.js loads before
app.js and replaces window.WebSocket, so app.js is byte-identical to the one
the Python build serves and there is exactly one panel to maintain.
"""
import pathlib
import shutil

ROOT = pathlib.Path(__file__).resolve().parent.parent
UI = ROOT / "loopengine" / "ui"
WEB = ROOT / "web"
OUT = ROOT / "public"


def build():
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    # -- the instrument, at the root ---------------------------------------
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
    # opens that socket on its first pass.
    html = html.replace('<script src="view.js"></script>',
                        '<script src="host.js"></script>\n<script src="view.js"></script>')
    (OUT / "index.html").write_text(html)

    # -- the page about it, one level in -----------------------------------
    art = OUT / "artifact"
    art.mkdir()
    shutil.copy2(WEB / "artifact" / "index.html", art / "index.html")
    shutil.copy2(WEB / "artifact" / "panel.png", art / "panel.png")

    n = sum(1 for f in OUT.rglob("*") if f.is_file())
    kb = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file()) // 1024
    print(f"public: {n} files, {kb} KB")


if __name__ == "__main__":
    build()
