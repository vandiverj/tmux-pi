#!/usr/bin/env python3
"""Optional bounded module-level parallel run of the unittest suite.

The reference command stays `python3 -m unittest discover -s tests -p 'test_*.py'`.
Each module runs in its own interpreter; modules in SERIAL run one at a time afterwards.
"""
import argparse
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import run_bounded  # noqa: E402

ROOT = TESTS.parent
MAX_JOBS = 4
MODULE_TIMEOUT = run_bounded.DEFAULT_TIMEOUT  # seconds one module may run before its process group is stopped
# Pseudo-terminal repaint timing and loopback HTTP servers: kept out of the pool so load cannot skew them.
SERIAL = {"test_status_repaint", "test_lmstudio", "test_routing_evals"}


def default_jobs():
    """Half the cores (1 to MAX_JOBS) when the machine is quiet, else 2: process starts are the cost, so a busy
    machine gets fewer at once."""
    cores = os.cpu_count() or 2
    try:
        busy = os.getloadavg()[0] >= cores / 2
    except OSError:
        busy = True
    return 2 if busy else max(1, min(MAX_JOBS, cores // 2))


def run(module, timeout=MODULE_TIMEOUT, log=None):
    """Run one module under the bound and log its seconds and the load, in run_bounded's format."""
    record, output = run_bounded.run_bounded(
        [sys.executable, "-m", "unittest", "discover", "-s", str(TESTS), "-p", module + ".py"],
        timeout=timeout, log=log, cwd=ROOT, files=[module], echo=False)
    ran = re.search(r"^Ran (\d+) tests?", output, re.M)
    if record["timed_out"]:
        output += f"\n{module} hit the {timeout:g}s bound and its process group was stopped\n"
    return module, 124 if record["timed_out"] else record["rc"], int(ran.group(1)) if ran else 0, record["seconds"], output


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-j", "--jobs", type=int, default=None,
                        help=f"parallel modules, 1-{MAX_JOBS} (default: by core count when the load is low, else 2)")
    parser.add_argument("--module-timeout", type=float, default=MODULE_TIMEOUT, help="seconds per module")
    parser.add_argument("--log", help="JSON lines file for per-module seconds (default: run_bounded's)")
    parser.add_argument("modules", nargs="*", help="module names such as test_goal (default: all)")
    args = parser.parse_args()
    if args.jobs is None:
        args.jobs = default_jobs()
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

    def bounded(module):
        return run(module, args.module_timeout, args.log)

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        passed = [report(r) for r in pool.map(bounded, [m for m in wanted if m not in SERIAL])]
    passed += [report(bounded(m)) for m in wanted if m in SERIAL]
    total = sum(r[2] for r in results)
    failed = passed.count(False)
    print(f"Ran {total} tests in {len(results)} modules in {time.monotonic() - started:.1f}s with -j{args.jobs}: "
          + ("OK" if not failed else f"{failed} module(s) FAILED"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
