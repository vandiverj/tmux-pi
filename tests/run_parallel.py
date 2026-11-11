#!/usr/bin/env python3
"""Run the unittest suite split across processes, with the same tests and the same verdict as the serial discovery.

    tests/run_parallel.py [-j N] [--only MODULE...] [--timeout S] [--serial]

The reference command stays `python3 -m unittest discover -s tests -p 'test_*.py'`; `--serial` runs exactly that.

Rules:
- Same tests. One discovery process lists every test id the serial discovery loads, and the ids each test file gives on its
  own (`discover -p FILE`). The per-file lists must add up to the full list exactly, or the run stops before any test runs.
  A unit is one module; a module whose last recorded time is over --split-seconds is split by class. Each child loads its
  file the way discovery does, keeps exactly the ids it was given, and reports the ids it loaded. Any difference is an error.
- Isolation. Each child gets this process's environment without SCRUB, plus its own fresh TMUX_TMPDIR under /tmp, which
  is removed afterwards. TMPDIR stays the caller's, as in the serial run (see private_env for the measured reason). A tmux server left on a socket there is stopped with `tmux -L NAME kill-server` in that
  directory and listed as a leftover. Nothing is added to the environment, so live opt-ins stay the caller's choice.
- Same verdict. Exit 0 only when every unit passed. A child that crashes, hits the timeout or writes no result is an
  error for its unit, never a pass. The output ends in unittest's format: `Ran N tests in S.SSSs`, a blank line, then
  `OK` or `FAILED (...)`.
- Longest first. Units start in order of their last recorded seconds (the --times file, untracked under .tmux-pi/). Units
  with no record start first, largest file first, so the first run goes by file size.
- SERIAL modules run one at a time after the pool has finished. Each entry states its measured reason. A failed module is
  never retried.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import run_bounded  # noqa: E402

ROOT = TESTS.parent
PATTERN = "test_*.py"
# Main's discovery runs under `env -u` of these keys: the caller's tmux, session and task identity, and its state dir.
SCRUB = ("TMUX", "TMUX_PANE", "TP_SESSION", "TP_TASK_ID", "TP_TASK_DIR", "TP_STATE_DIR")
DEFAULT_JOBS = 8  # leaves most of an 18-core Mac to the owner's own work; see default_jobs
MODULE_TIMEOUT = run_bounded.DEFAULT_TIMEOUT  # seconds one unit may run before its process group is stopped
SPLIT_SECONDS = 240.0  # a module whose last run took longer than this is split by class
DISCOVER_TIMEOUT = 300.0
TIMES = ROOT / ".tmux-pi" / "test-times-parallel.json"
TAIL = 60  # lines of a child's output shown when the child itself failed
# Modules that must not run alongside others: name -> the measured reason. Empty: no collision has been measured.
SERIAL = {}

# The discovery child. It loads the suite the serial way, then each test file alone, and writes both id lists.
LIST = r"""
import fnmatch, json, os, sys, unittest
start, pattern, out = sys.argv[1:4]
def flat(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from flat(test)
        else:
            yield test
full = [t.id() for t in flat(unittest.TestLoader().discover(start, pattern=pattern))]
names = sorted({name for _, _, files in os.walk(start) for name in files
                if name.endswith(".py") and fnmatch.fnmatch(name, pattern)})
groups = []
for name in names:
    ids = [t.id() for t in flat(unittest.TestLoader().discover(start, pattern=name))]
    if ids:
        groups.append([name[:-3], ids])
with open(out + ".tmp", "w") as handle:
    json.dump({"full": full, "groups": groups}, handle)
os.replace(out + ".tmp", out)
"""

# The unit child. It loads one file the way discovery does and runs only its ids, in discovery order, in one flat suite:
# class and module fixtures run as they do serially. It writes the result as JSON. A child that writes nothing has failed.
CHILD = r"""
import json, os, sys, time, unittest
start, pattern, spec, out = sys.argv[1:5]
with open(spec) as handle:
    wanted = set(json.load(handle))
def flat(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from flat(test)
        else:
            yield test
cases = [t for t in flat(unittest.TestLoader().discover(start, pattern=pattern)) if t.id() in wanted]
ids = [t.id() for t in cases]
class_seconds = {}
outcomes = {}  # id -> its worst outcome; a loaded test with none was never started (its class or module setup failed)
RANK = {"ok": 0, "skip": 1, "expected_failure": 1, "unexpected_success": 2, "fail": 3, "error": 4}
def outcome(test, value):
    if RANK[value] >= RANK.get(outcomes.get(test.id()), -1):
        outcomes[test.id()] = value
class Result(unittest.TextTestResult):
    mark = time.monotonic()
    def addSuccess(self, test):
        super().addSuccess(test); outcome(test, "ok")
    def addFailure(self, test, err):
        super().addFailure(test, err); outcome(test, "fail")
    def addError(self, test, err):
        super().addError(test, err); outcome(test, "error")
    def addSkip(self, test, reason):
        super().addSkip(test, reason); outcome(test, "skip")
    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err); outcome(test, "expected_failure")
    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test); outcome(test, "unexpected_success")
    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            outcome(test, "fail" if issubclass(err[0], test.failureException) else "error")
    def stopTest(self, test):
        super().stopTest(test)
        now = time.monotonic()
        key = test.id().rsplit(".", 1)[0]
        class_seconds[key] = class_seconds.get(key, 0.0) + now - Result.mark
        Result.mark = now
result = unittest.TextTestRunner(stream=sys.stderr, verbosity=1, resultclass=Result).run(unittest.TestSuite(cases))
rows = lambda pairs: [[test.id(), tb] for test, tb in pairs]
data = {"ids": ids, "ran": result.testsRun, "failures": rows(result.failures), "errors": rows(result.errors),
        "skipped": len(result.skipped), "expected_failures": len(result.expectedFailures),
        "unexpected_successes": [t.id() for t in result.unexpectedSuccesses], "class_seconds": class_seconds,
        "outcomes": outcomes}
with open(out + ".tmp", "w") as handle:
    json.dump(data, handle)
os.replace(out + ".tmp", out)
sys.exit(0 if result.wasSuccessful() else 1)
"""


class RunnerError(Exception):
    pass


def default_jobs(cores=None):
    """DEFAULT_JOBS, or half the cores when there are fewer than twice that (at least 1)."""
    cores = cores or os.cpu_count() or 2
    return max(1, min(DEFAULT_JOBS, cores // 2))


def max_jobs():
    return os.cpu_count() or 2


def scrubbed_env(env=None):
    return {k: v for k, v in (os.environ if env is None else env).items() if k not in SCRUB}


def private_env(env, directory):
    """env with its own fresh TMUX_TMPDIR (`directory`, a short path under /tmp: tmux socket paths are length-limited).
    TMPDIR stays the caller's, as in the serial discovery; tests make their own temp dirs inside it. Measured 2026-11-11,
    both private alternatives change the verdict:
    - a private TMPDIR under /tmp: the world-writable /tmp becomes an ancestor of each test's working directory and
      trust store, which the sensitive-read and trust checks treat differently (19 failures in test_sensitive_read,
      test_sensitive_trust, test_trust_guard and test_trust_record);
    - a private dir inside the caller's TMPDIR: the extra level pushes socket paths that tests build under TMPDIR past
      the 104-byte limit (test_process_end: 98 bytes serially, 111 with the level; test_trust_dialog's setUpClass)."""
    directory.mkdir()
    return dict(env, TMUX_TMPDIR=str(directory))


def release(env):
    """Stop each tmux server whose socket is in env's private TMUX_TMPDIR (always with -L, never the caller's server),
    remove that dir, and return the socket names found."""
    socket_dir = Path(env["TMUX_TMPDIR"]) / f"tmux-{os.getuid()}"
    try:
        names = sorted(p.name for p in socket_dir.iterdir())
    except OSError:
        names = []
    for name in names:
        try:
            subprocess.run(["tmux", "-L", name, "kill-server"], env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass
    shutil.rmtree(env["TMUX_TMPDIR"], ignore_errors=True)
    return names


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def discover(tests, base, env):
    """Full and per-file id lists from one discovery process. Raises RunnerError when they do not partition."""
    out = base / "discover.json"
    child_env = private_env(env, base / "discover")
    try:
        record, output = run_bounded.run_bounded([sys.executable, "-c", LIST, str(tests), PATTERN, str(out)],
                                                 timeout=DISCOVER_TIMEOUT, log=False, cwd=ROOT, echo=False,
                                                 env=child_env)
    finally:
        release(child_env)
    data = read_json(out)
    if record["timed_out"] or record["rc"] != 0 or data is None:
        raise RunnerError(f"discovery failed (exit {record['rc']}, timed out {record['timed_out']}):\n{output}")
    joined = [i for _, ids in data["groups"] for i in ids]
    if sorted(joined) != sorted(data["full"]):
        missing = sorted(set(data["full"]) - set(joined))[:20]
        extra = sorted(set(joined) - set(data["full"]))[:20]
        raise RunnerError("the per-file ids do not add up to the serial discovery's ids "
                          f"({len(joined)} vs {len(data['full'])}); missing {missing}, extra or repeated {extra}")
    return data["full"], [(module, ids) for module, ids in data["groups"]]


def plan(groups, times, sizes, split_seconds=SPLIT_SECONDS):
    """Units, longest first. groups: [(module, ids)] in discovery order. A module whose recorded seconds exceed
    split_seconds and that has more than one class becomes one unit per class (ids keep their order). A unit's estimate
    is its recorded seconds, else (a class) the module's seconds times its share of the ids, else None. Units with no
    estimate come first, largest file first, then the rest by estimate."""
    units = []
    for module, ids in groups:
        pattern = module.rsplit(".", 1)[-1] + ".py"
        seconds = times.get(module)
        classes = {}
        for test_id in ids:
            classes.setdefault(test_id.rsplit(".", 1)[0], []).append(test_id)
        if seconds is not None and seconds > split_seconds and len(classes) > 1:
            for name, class_ids in classes.items():
                estimate = times.get(name, seconds * len(class_ids) / len(ids))
                units.append({"name": name, "module": module, "pattern": pattern, "ids": class_ids,
                              "estimate": estimate, "size": sizes.get(module, 0)})
        else:
            units.append({"name": module, "module": module, "pattern": pattern, "ids": list(ids),
                          "estimate": seconds, "size": sizes.get(module, 0)})
    units.sort(key=lambda u: (u["estimate"] is not None, -(u["estimate"] or 0), -u["size"], u["name"]))
    return units


def run_unit(unit, base, index, env, timeout, log, tests=None):
    """Run one unit in its own process and private temp dirs. Returns a result dict; `problems` lists why the unit
    failed as a whole (crash, timeout, no result, other ids loaded)."""
    tests = tests or TESTS
    spec, out = base / f"{index}.ids.json", base / f"{index}.result.json"
    spec.write_text(json.dumps(unit["ids"]))
    child_env = private_env(env, base / str(index))
    try:
        record, output = run_bounded.run_bounded(
            [sys.executable, "-c", CHILD, str(tests), unit["pattern"], str(spec), str(out)],
            timeout=timeout, log=log, cwd=ROOT, files=[unit["name"]], echo=False, env=child_env)
    finally:
        leftovers = release(child_env)
    data = read_json(out)
    wrote = data is not None
    problems = []
    if record["timed_out"]:
        problems.append(f"hit the {timeout:g}s timeout; its process group was stopped")
    if data is None:
        if not record["timed_out"]:  # one error per unit: a timed-out unit has no result either
            problems.append(f"wrote no unittest result (exit {record['rc']})")
        data = {"ids": [], "ran": 0, "failures": [], "errors": [], "skipped": 0, "expected_failures": 0,
                "unexpected_successes": [], "class_seconds": {}, "outcomes": {}}
    else:
        if data["ids"] != unit["ids"]:
            missing = [i for i in unit["ids"] if i not in set(data["ids"])][:10]
            extra = [i for i in data["ids"] if i not in set(unit["ids"])][:10]
            problems.append(f"loaded other tests than discovery listed: missing {missing}, extra {extra}")
        passed = not (data["failures"] or data["errors"] or data["unexpected_successes"])
        if not record["timed_out"] and record["rc"] != (0 if passed else 1):
            problems.append(f"exited {record['rc']} after a {'passing' if passed else 'failing'} result")
    ok = not problems and not (data["failures"] or data["errors"] or data["unexpected_successes"])
    return {"unit": unit, "ok": ok, "problems": problems, "data": data, "wrote": wrote, "seconds": record["seconds"],
            "output": output, "leftovers": leftovers}


def update_times(path, results):
    times = read_json(path) or {}
    modules = {}
    for r in results:
        unit = r["unit"]
        times[unit["name"]] = r["seconds"]
        modules[unit["module"]] = modules.get(unit["module"], 0.0) + r["seconds"]
        if unit["name"] == unit["module"]:  # a whole module: keep its class seconds for a later split
            times.update({k: round(v, 2) for k, v in r["data"]["class_seconds"].items()})
    times.update({m: round(s, 2) for m, s in modules.items()})
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(times, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)


def outcomes(results):
    """(id, outcome) for every id of every unit run, plus fixture errors (ids such as `setUpClass (m.C)`)."""
    rows = []
    for r in results:
        data = r["data"]
        for test_id in r["unit"]["ids"]:
            rows.append((test_id, data["outcomes"].get(test_id, "not-run" if r["wrote"] else "unit-error")))
        listed = set(r["unit"]["ids"])
        rows += [(k, v) for k, v in data["outcomes"].items() if k not in listed]
    return rows


def summary(results, wall):
    """The closing lines, in unittest's format. Returns (text, failed)."""
    lines = []
    totals = {"failures": 0, "errors": 0, "skipped": 0, "expected failures": 0, "unexpected successes": 0}
    ran = 0
    for r in results:
        data = r["data"]
        ran += data["ran"]
        totals["failures"] += len(data["failures"])
        totals["errors"] += len(data["errors"]) + (1 if r["problems"] else 0)  # a failed unit is one error
        totals["skipped"] += data["skipped"]
        totals["expected failures"] += data["expected_failures"]
        totals["unexpected successes"] += len(data["unexpected_successes"])
        for kind, rows in (("ERROR", data["errors"]), ("FAIL", data["failures"])):
            for test_id, tb in rows:
                lines += ["=" * 70, f"{kind}: {test_id}", "-" * 70, tb.rstrip()]
        for test_id in data["unexpected_successes"]:
            lines += ["=" * 70, f"UNEXPECTED SUCCESS: {test_id}"]
        if r["problems"]:
            tail = r["output"].rstrip().splitlines()[-TAIL:]
            lines += ["=" * 70, f"ERROR: {r['unit']['name']} (the unit's process): {'; '.join(r['problems'])}",
                      "-" * 70, f"last {len(tail)} lines of its output:"] + tail
    lines += ["", "Per-module seconds, longest first:"]
    for r in sorted(results, key=lambda r: -r["seconds"]):
        note = f"; leftover tmux sockets {r['leftovers']}" if r["leftovers"] else ""
        lines.append(f"{r['seconds']:9.1f}s  {'ok  ' if r['ok'] else 'FAIL'}  {r['unit']['name']} "
                     f"({r['data']['ran']} tests){note}")
    lines += ["-" * 70, f"Ran {ran} test{'s' if ran != 1 else ''} in {wall:.3f}s", ""]
    failed = totals["failures"] + totals["errors"] + totals["unexpected successes"]
    infos = ", ".join(f"{k}={v}" for k, v in totals.items() if v)
    if failed:
        lines.append(f"FAILED ({infos})")
    else:
        lines.append("OK" + (f" ({infos})" if infos else ""))
    return "\n".join(lines), bool(failed)


def main(argv=None, tests=None):
    tests = Path(tests or TESTS)
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], usage=argparse.SUPPRESS)
    parser.add_argument("-j", "--jobs", type=int, default=None,
                        help=f"units at once, 1 to the core count (default {default_jobs()}: {DEFAULT_JOBS}, or half the "
                             "cores on a smaller machine)")
    parser.add_argument("--timeout", "--module-timeout", dest="timeout", type=float, default=MODULE_TIMEOUT,
                        help=f"seconds per unit (default {MODULE_TIMEOUT:g})")
    parser.add_argument("--only", nargs="+", default=[], metavar="MODULE", help="run only these modules")
    parser.add_argument("--serial", action="store_true", help="run the one-process discovery unchanged")
    parser.add_argument("--split-seconds", type=float, default=SPLIT_SECONDS,
                        help=f"split a module by class when its last run took longer (default {SPLIT_SECONDS:g})")
    parser.add_argument("--times", default=str(TIMES), help="per-unit seconds of the last run (JSON)")
    parser.add_argument("--log", help="JSON lines file for run_bounded's per-unit records (default: run_bounded's)")
    parser.add_argument("--outcomes-out", metavar="PATH",
                        help="write `ID OUTCOME` per line, sorted: every test id discovery listed for a unit that ran "
                             "(ok, fail, error, skip, expected_failure, unexpected_success; not-run when its class or "
                             "module setup failed; unit-error when its unit wrote no result), plus each class or module "
                             "fixture error under unittest's own id for it")
    parser.add_argument("modules", nargs="*", help="same as --only")
    args = parser.parse_args(argv)
    env = scrubbed_env()
    wanted = [m[:-3] if m.endswith(".py") else m for m in args.modules + args.only]
    if args.serial:
        if wanted:
            parser.error("--serial runs the whole discovery; it takes no modules")
        return subprocess.call([sys.executable, "-m", "unittest", "discover", "-s", str(tests), "-p", PATTERN],
                               cwd=ROOT, env=env)
    jobs = default_jobs() if args.jobs is None else args.jobs
    if not 1 <= jobs <= max_jobs():
        parser.error(f"--jobs must be between 1 and {max_jobs()}")
    started = time.monotonic()
    base = Path(tempfile.mkdtemp(prefix="tprp", dir="/tmp"))
    results = []
    try:
        try:
            _, groups = discover(tests, base, env)
        except RunnerError as error:
            print(f"run_parallel: {error}", file=sys.stderr)
            return 2
        found = [m for m, _ in groups]
        missing_serial = set(SERIAL) - set(found)
        if missing_serial:
            parser.error("serial modules missing (review SERIAL after renaming): " + ", ".join(sorted(missing_serial)))
        unknown = sorted(set(wanted) - set(found))
        if unknown:
            parser.error("unknown modules: " + ", ".join(unknown))
        if wanted:
            groups = [(m, ids) for m, ids in groups if m in wanted]
        sizes = {}
        for module, _ in groups:
            try:
                sizes[module] = (tests / (module.replace(".", "/") + ".py")).stat().st_size
            except OSError:
                pass
        units = plan(groups, read_json(args.times) or {}, sizes, args.split_seconds)
        print(f"run_parallel: {sum(len(u['ids']) for u in units)} tests in {len(units)} units from {len(groups)} "
              f"modules, -j{jobs}, timeout {args.timeout:g}s", flush=True)

        def report(result):
            results.append(result)
            print(f"{'ok  ' if result['ok'] else 'FAIL'} {result['unit']['name']} ({result['data']['ran']} tests, "
                  f"{result['seconds']:.1f}s)", flush=True)

        pooled = [(i, u) for i, u in enumerate(units) if u["module"] not in SERIAL]
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = [pool.submit(run_unit, u, base, i, env, args.timeout, args.log, tests) for i, u in pooled]
            for future in as_completed(futures):
                report(future.result())
        for i, u in enumerate(units):
            if u["module"] in SERIAL:
                report(run_unit(u, base, i, env, args.timeout, args.log, tests))
    finally:
        shutil.rmtree(base, ignore_errors=True)
    if args.outcomes_out:
        Path(args.outcomes_out).write_text("".join(f"{i} {o}\n" for i, o in sorted(outcomes(results))))
    update_times(args.times, results)
    # The ids partition the discovery (checked in discover) and each child loaded exactly its own (checked in run_unit),
    # so a child that ran is never missing from the totals and one that did not is an error there.
    text, failed = summary(results, time.monotonic() - started)
    print(text, flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
