"""Every check in the suite, one command:

    PYTHONPATH=.pylibs python3 -m tests     # with the vendored dependencies
    python3 -m tests                        # with installed ones

Each file runs in its own process, exactly as it does alone, so no file's
module state can leak into the next. A check prints PASS, FAIL or SKIP. SKIPs
are counted and listed by name at the end, so a skip is always visible and
cannot quietly become permanent. Exits non-zero on any FAIL, or if a file dies
without reporting.
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATUS = re.compile(r"\s(PASS|FAIL|SKIP)(?=\s|$)")


def main():
    files = sorted(glob.glob(os.path.join(HERE, "test_*.py")))
    total = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    fails, skips, died = [], [], []
    started = time.perf_counter()
    for path in files:
        mod = "tests." + os.path.splitext(os.path.basename(path))[0]
        t0 = time.perf_counter()
        r = subprocess.run([sys.executable, "-m", mod], cwd=ROOT,
                           capture_output=True, text=True)
        n = {"PASS": 0, "FAIL": 0, "SKIP": 0}
        for line in r.stdout.splitlines():
            m = STATUS.search(line)
            if not m:
                continue
            n[m.group(1)] += 1
            if m.group(1) == "FAIL":
                fails.append("%s: %s" % (mod[6:], line.strip()))
            elif m.group(1) == "SKIP":
                skips.append("%s: %s" % (mod[6:], line.strip()))
        if r.returncode != 0 and n["FAIL"] == 0:
            died.append("%s exited %d: %s" % (mod[6:], r.returncode,
                                              (r.stderr or r.stdout).strip()[-300:]))
        for k in total:
            total[k] += n[k]
        print("%-14s %4d pass  %3d fail  %3d skip   %5.1f s"
              % (mod[11:], n["PASS"], n["FAIL"], n["SKIP"], time.perf_counter() - t0))
    wall = time.perf_counter() - started
    print("-" * 52)
    print("%-14s %4d pass  %3d fail  %3d skip   %5.1f s   (%d files)"
          % ("all", total["PASS"], total["FAIL"], total["SKIP"], wall, len(files)))
    for s in skips:
        print("SKIPPED  " + s)
    for f in fails:
        print("FAILED   " + f)
    for d in died:
        print("DIED     " + d)
    return 1 if (fails or died) else 0


if __name__ == "__main__":
    sys.exit(main())
