#!/usr/bin/env python3
"""Optional bounded module-level parallel run of the unittest suite.

The reference command stays `python3 -m unittest discover -s tests -p 'test_*.py'`.
Each module runs in its own interpreter; modules in SERIAL run one at a time afterwards.
"""
import argparse
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
MAX_JOBS = 4
# Pseudo-terminal repaint timing and loopback HTTP servers: kept out of the pool so load cannot skew them.
SERIAL = {"test_status_repaint", "test_lmstudio", "test_routing_evals"}


def run(module):
    started = time.monotonic()
    proc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(TESTS), "-p", module + ".py"],
                          cwd=ROOT, capture_output=True, text=True)
    output = proc.stdout + proc.stderr
    ran = re.search(r"^Ran (\d+) tests?", output, re.M)
    return module, proc.returncode, int(ran.group(1)) if ran else 0, time.monotonic() - started, output


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-j", "--jobs", type=int, default=2, help=f"parallel modules, 1-{MAX_JOBS} (default 2)")
    parser.add_argument("modules", nargs="*", help="module names such as test_goal (default: all)")
    args = parser.parse_args()
    if not 1 <= args.jobs <= MAX_JOBS:
        parser.error(f"--jobs must be between 1 and {MAX_JOBS}")
    found = sorted(p.stem for p in TESTS.glob("test_*.py"))
    missing_serial = SERIAL - set(found)
    if missing_serial:
        parser.error("serial modules missing (review SERIAL after renaming): " + ", ".join(sorted(missing_serial)))
    wanted = [m.removesuffix(".py") for m in args.modules] or found
    unknown = sorted(set(wanted) - set(found))
    if unknown:
        parser.error("unknown modules: " + ", ".join(unknown))
    started = time.monotonic()
    results = []

    def report(result):
        module, code, ran, seconds, output = result
        results.append(result)
        # A module that runs nothing is a failure: an import error or bad pattern must not pass silently.
        ok = code == 0 and ran > 0
        print(f"{'ok  ' if ok else 'FAIL'} {module} ({ran} tests, {seconds:.1f}s)", flush=True)
        if not ok:
            print(output, flush=True)
        return ok

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        passed = [report(r) for r in pool.map(run, [m for m in wanted if m not in SERIAL])]
    passed += [report(run(m)) for m in wanted if m in SERIAL]
    total = sum(r[2] for r in results)
    failed = passed.count(False)
    print(f"Ran {total} tests in {len(results)} modules in {time.monotonic() - started:.1f}s with -j{args.jobs}: "
          + ("OK" if not failed else f"{failed} module(s) FAILED"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
