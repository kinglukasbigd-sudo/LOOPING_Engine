#!/usr/bin/env python3
"""Shortcut for `python -m loopengine`.

If a `.pylibs/` directory sits next to this file it goes on the path first, so
you can vendor numpy / sounddevice / soundfile locally on a machine whose
system Python is externally managed:

    pip install --target .pylibs -r requirements.txt
"""
import os
import sys

_vendor = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".pylibs")
if os.path.isdir(_vendor):
    sys.path.insert(0, _vendor)

from loopengine.__main__ import main

raise SystemExit(main())
