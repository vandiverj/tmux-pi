#!/usr/bin/env python3
"""Run a command under a hard time bound and append one JSON line about the run to a log.

    tests/run_bounded.py [--timeout S] [--grace S] [--log PATH] -- CMD [ARG...]
    tests/run_bounded.py [--timeout S] [--durations N] tests/test_a.py tests/test_b.py

The command runs in a new session. When the deadline passes the parent sends SIGTERM and then SIGKILL to that
process group (the one it started) and to nothing else. A child that cancels its own alarm is still stopped.

The second form runs each file as `python3 -m unittest [--durations N] MODULE` with its own bound, one after another.

Log line fields: cmd, files, seconds, rc, timed_out, load1, per_file, leftovers. `leftovers` lists tmux sockets
(`tmux -L NAME`) that exist after the run and did not exist before it. They are only reported, never killed: the
parent cannot tell whether the command started them or a neighbour did.
The log is $TASK_DIR/tests.jsonl when TASK_DIR is set, else .tmux-pi/test-times.jsonl in the repository.
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TIMEOUT = 900.0
DEFAULT_GRACE = 5.0
RAN = re.compile(r"^Ran (\d+) tests? in ([0-9.]+)s", re.M)


def default_log():
    task_dir = os.environ.get("TASK_DIR")
    return Path(task_dir) / "tests.jsonl" if task_dir else ROOT / ".tmux-pi" / "test-times.jsonl"


def tmux_sockets():
    """Names of the tmux sockets in the directory `tmux -L` uses (the default socket is not counted)."""
    base = Path(os.environ.get("TMUX_TMPDIR") or "/tmp")
    directory = base / f"tmux-{os.getuid()}"
    try:
        return {p.name for p in directory.iterdir() if p.name != "default"}
    except OSError:
        return set()


def load1():
    try:
        return round(os.getloadavg()[0], 2)
    except OSError:
        return None


def stop_group(pgid, proc, grace):
    """SIGTERM the group, give the leader `grace` seconds, then SIGKILL the group."""
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, None)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            pass
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass


def append_record(log, record):
    log = Path(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, sort_keys=True) + "\n").encode()
    fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line)  # one write: concurrent runs do not interleave lines
    finally:
        os.close(fd)


def run_bounded(cmd, timeout=DEFAULT_TIMEOUT, grace=DEFAULT_GRACE, log=None, cwd=None, files=None, per_file=None,
                echo=True):
    """Run cmd in a new session under the bound. Returns (record, output). Appends the record to `log` (default
    log when None; pass False to write nothing). `per_file` maps file to seconds and is filled from the unittest
    `Ran N tests in Xs` line when the run has exactly one file."""
    files = list(files or [])
    before = tmux_sockets()
    chunks = []
    started = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True)
    pgid = proc.pid  # a new session makes the child its own group leader

    def pump():
        for data in iter(lambda: proc.stdout.read1(65536), b""):
            chunks.append(data)
            if echo:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        stop_group(pgid, proc, grace)
    except BaseException:  # the caller is interrupted: do not leave the group behind
        stop_group(pgid, proc, grace)
        raise
    reader.join(grace)  # a stray holder of the pipe must not hold the parent
    proc.stdout.close()
    seconds = time.monotonic() - started
    output = b"".join(chunks).decode(errors="replace")
    per_file = dict(per_file or {})
    ran = RAN.findall(output)
    if len(files) == 1 and files[0] not in per_file and ran and not timed_out:
        per_file[files[0]] = float(ran[-1][1])
    record = {"cmd": list(cmd), "files": files, "seconds": round(seconds, 2), "rc": proc.returncode,
              "timed_out": timed_out, "load1": load1(), "per_file": per_file,
              "leftovers": sorted(tmux_sockets() - before)}
    if log is not False:
        append_record(log or default_log(), record)
    return record, output


def module_of(path):
    path = Path(path)
    if path.suffix == ".py":
        path = path.with_suffix("")
    return path.name


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], usage=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds per command or file")
    parser.add_argument("--grace", type=float, default=DEFAULT_GRACE, help="seconds between SIGTERM and SIGKILL")
    parser.add_argument("--log", help="JSON lines file to append to")
    parser.add_argument("--durations", type=int, metavar="N", help="passed to unittest in file mode")
    parser.add_argument("rest", nargs=argparse.REMAINDER, help="-- CMD..., or test files")
    args = parser.parse_args(argv)
    rest = args.rest[1:] if args.rest[:1] == ["--"] else None
    log = args.log or None
    if rest is not None:
        if not rest:
            parser.error("no command after --")
        if args.durations is not None:
            parser.error("--durations goes in the command after -- ; it is for file mode")
        files = [a for a in rest if a.endswith(".py") and not a.startswith("-")]
        record, _ = run_bounded(rest, args.timeout, args.grace, log, files=files)
        return 124 if record["timed_out"] else record["rc"]
    if not args.rest:
        parser.error("give test files or -- CMD")
    status = 0
    for path in args.rest:
        cmd = [sys.executable, "-m", "unittest", "discover"]
        if args.durations is not None:
            cmd += ["--durations", str(args.durations)]  # after `discover`: it rejects options placed before it
        # the project's own command shape: discover in the file's directory, run from the repository root
        cmd += ["-s", str(Path(path).resolve().parent), "-p", module_of(path) + ".py"]
        record, _ = run_bounded(cmd, args.timeout, args.grace, log, cwd=ROOT, files=[path])
        if record["timed_out"]:
            print(f"run_bounded: {path} hit the {args.timeout:g}s bound and was stopped", file=sys.stderr)
            status = status or 124
        else:
            status = status or record["rc"]
    return status


if __name__ == "__main__":
    sys.exit(main())
