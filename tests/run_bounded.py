#!/usr/bin/env python3
"""Run a command under a hard time bound and append one JSON line about the run to a log.

    tests/run_bounded.py [--timeout S] [--grace S] [--log PATH] -- CMD [ARG...]
    tests/run_bounded.py [--timeout S] [--durations N] tests/test_a.py tests/test_b.py

The command runs in a new session. The run ends when no process is left in the group it started, not when the
leader exits: a child the leader left behind is still part of the run. When the deadline passes first, the parent
sends SIGTERM and then SIGKILL to that process group and to nothing else. A child that cancels its own alarm is
still stopped. `rc` is the leader's exit code when the group emptied in time; it is null when the run hit the bound.

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
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TIMEOUT = 900.0
DEFAULT_GRACE = 5.0
POLL = 0.05  # seconds between checks of the group once the leader has exited
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


def group_alive(pgid):
    """True while any process is left in group `pgid`. Reap the leader first: a zombie leader still counts."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # a member we may not signal is still a member
        return True
    return True


def wait_group(pgid, proc, until):
    """Reap the leader and wait for the group to empty, up to monotonic time `until`. True when it emptied."""
    while True:
        if proc.poll() is not None and not group_alive(pgid):
            return True
        left = until - time.monotonic()
        if left <= 0:
            return False
        if proc.returncode is None:
            try:
                proc.wait(timeout=min(POLL, left))
            except subprocess.TimeoutExpired:
                pass
        else:
            time.sleep(min(POLL, left))


def stop_group(pgid, proc, grace):
    """SIGTERM the group, give it `grace` seconds to empty, then SIGKILL it and wait up to `grace` more."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            break
        if wait_group(pgid, proc, time.monotonic() + grace):
            break
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
    # Our own pipe and raw reads: a buffered proc.stdout.close() blocks on the lock a blocked reader holds, so a
    # descendant that keeps the pipe open could hold the parent past its bound.
    read_fd, write_fd = os.pipe()
    try:
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=write_fd, stderr=write_fd,
                                    start_new_session=True)
        finally:
            os.close(write_fd)  # the child has its own copy; ours must go whether or not the launch worked
    except BaseException:
        os.close(read_fd)  # launch failed: nothing reads this end
        raise
    pgid = proc.pid  # a new session makes the child its own group leader
    stop = threading.Event()

    def pump():
        while not stop.is_set():
            if not select.select([read_fd], [], [], 0.1)[0]:
                continue
            data = os.read(read_fd, 65536)
            if not data:
                return
            chunks.append(data)
            if echo:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()

    reader = threading.Thread(target=pump, daemon=True)
    # The rule: the run ends only when no process is left in the group it started, or the deadline passes.
    #   leader exited | group empty | deadline | result
    #   yes           | yes         | before   | done: rc = leader's rc, timed_out false
    #   yes           | no          | before   | keep waiting (a child the leader left is part of the run)
    #   any           | no          | reached  | killpg TERM, grace, killpg KILL; timed_out true, rc null
    # The reader does not decide the end: after the end it drains for at most `grace` seconds, because a process
    # outside the group (its own session) may hold the pipe and is not ours to wait for or to kill.
    # Release rule: on every exit (normal, timeout, interrupt, error) the group is stopped if the run did not end
    # cleanly, the reader is told to stop, and the read end is closed unless the reader is still inside it.
    try:
        try:
            reader.start()
            timed_out = not wait_group(pgid, proc, started + timeout)
            if timed_out:
                stop_group(pgid, proc, grace)
        except BaseException:  # the caller is interrupted: do not leave the group behind
            stop_group(pgid, proc, grace)
            raise
        if reader.is_alive():
            reader.join(grace)  # bounded: a holder of the pipe outside the group must not hold the parent
    finally:
        stop.set()
        if reader.is_alive():
            reader.join(1)  # the reader wakes every 0.1 s, so it sees `stop`
        if not reader.is_alive():
            os.close(read_fd)
    seconds = time.monotonic() - started
    output = b"".join(chunks).decode(errors="replace")
    per_file = dict(per_file or {})
    ran = RAN.findall(output)
    if len(files) == 1 and files[0] not in per_file:
        if timed_out:
            per_file[files[0]] = round(seconds, 2)  # it ran to the bound; no summary line to read
        elif ran:
            per_file[files[0]] = float(ran[-1][1])
    rc = None if timed_out else proc.returncode  # the leader's rc counts only when its group emptied in time
    record = {"cmd": list(cmd), "files": files, "seconds": round(seconds, 2), "rc": rc, "timed_out": timed_out,
              "load1": load1(), "per_file": per_file, "leftovers": sorted(tmux_sockets() - before)}
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
    parser.add_argument("--durations", type=int, metavar="N", help="passed to unittest in file mode (Python 3.12+; older unittest has no such option, the per-file seconds still come from the `Ran N tests in Xs` line)")
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
        if args.durations is not None and sys.version_info >= (3, 12):  # unittest has no --durations before 3.12
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
