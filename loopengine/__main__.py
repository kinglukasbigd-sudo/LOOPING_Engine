"""python -m loopengine"""
from __future__ import annotations

import argparse
import os
import sys
import time
import webbrowser

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="loopengine", add_help=True)
    ap.add_argument("--device", default=None,
                    help="output device index or name substring")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--samplerate", type=int, default=None,
                    help="default: the device's own rate")
    ap.add_argument("--blocksize", type=int, default=256,
                    help="frames per callback (128 is tighter, 512 is safer)")
    ap.add_argument("--port", type=int, default=7331)
    ap.add_argument("--tracks", type=int, default=8)
    ap.add_argument("--voices", type=int, default=16)
    ap.add_argument("--kit", default=None, help="folder to load into tracks")
    ap.add_argument("--root", action="append", default=None,
                    help="folder the file browser may read (repeatable)")
    ap.add_argument("--empty", action="store_true",
                    help="start with no audio loaded")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--offline", action="store_true",
                    help="run without a sound card — the panel, meters and "
                         "scope still work, you just can't hear it")
    args = ap.parse_args(argv)

    try:
        import numpy  # noqa: F401
    except ImportError:
        print("numpy is missing.  pip install -r requirements.txt",
              file=sys.stderr)
        return 2

    sd = None
    if not args.offline:
        try:
            import sounddevice as sd
        except ImportError:
            print("sounddevice is missing.  pip install -r requirements.txt",
                  file=sys.stderr)
            return 2
        except OSError as e:
            print("%s\n"
                  "PortAudio is a system library and pip does not ship it on "
                  "Linux.\n  Debian/Ubuntu:  sudo apt install libportaudio2\n"
                  "  Fedora:        sudo dnf install portaudio\n"
                  "  Arch:          sudo pacman -S portaudio\n"
                  "Or start silent with --offline." % e, file=sys.stderr)
            return 2

    if args.list_devices:
        if sd is None:
            print("--list-devices needs PortAudio.", file=sys.stderr)
            return 2
        print(sd.query_devices())
        return 0

    dev = args.device
    if dev is not None:
        try:
            dev = int(dev)
        except ValueError:
            pass

    from .engine import Engine
    from .app import App
    from .server import Server
    from . import demo

    try:
        engine = Engine(samplerate=args.samplerate, blocksize=args.blocksize,
                        device=dev, n_tracks=args.tracks,
                        n_voices=args.voices, offline=args.offline).start()
    except Exception as e:
        print("PortAudio would not open an output stream: %s\n"
              "Run with --list-devices, then pick one with --device."
              % e, file=sys.stderr)
        return 1

    roots = args.root or [HERE, os.path.expanduser("~")]
    app = App(engine, roots=roots,
              inbox=os.path.join(HERE, ".inbox")).start_pump()

    try:
        server = Server(app, port=args.port).start()
    except OSError as e:
        engine.stop()
        print("Port %d is taken (%s). Try --port %d."
              % (args.port, e.strerror, args.port + 1), file=sys.stderr)
        return 1

    kit = args.kit
    if kit is None and not args.empty:
        kit = os.path.join(HERE, "kits", "testkit-124")
        if not os.path.isdir(kit):
            print("Building the synthesised test kit in %s" % kit)
            demo.build(kit)
    if kit and not args.empty:
        app.load_kit(kit)
        time.sleep(0.6)                       # let the analysers finish
        engine.post("transport.bpm", v=124.0)
        app.map_pads(min(4, args.tracks - 1), "ONE")

    lat = engine.latency()
    print("LOOP ENGINE %s" % __import__("loopengine").__version__)
    print("  device     %s" % engine.device_name)
    print("  rate       %d Hz, %d frames/block" % (engine.sr, engine.blocksize))
    print("  path       block %.2f ms + output %.2f ms = %.2f ms to the speaker"
          % (lat["block_ms"], lat["output_ms"],
             lat["block_ms"] + lat["output_ms"]))
    print("             queue and wire are measured live — the panel shows CTRL")
    print("  panel      %s" % server.url)
    if kit and not args.empty:
        print("  loaded     %s" % kit)
    if args.offline:
        print("  silent    --offline: no PortAudio stream is open")
    print("  ctrl-c to stop")

    if not args.no_browser:
        webbrowser.open(server.url)

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print()
    finally:
        engine.stop()
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
