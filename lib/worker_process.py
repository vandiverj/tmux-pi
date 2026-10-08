"""A worker's process and its working tree (T3): who is alive where, and ending the process of a finished task.

One module for every path that collects a task (tp-agent, tp-contract) or starts a second worker on a tree
(tp-agent retry, tp-review), so none can pass without it. Every reader here refuses on a value it cannot read:
an unreadable pane table, a task record that is not a JSON object, a live task with no usable working tree."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from contract import task_lifecycle

# Lifecycles from which a worker's process is no longer wanted: its task is collected or stopped.
FINISHED = {"completed", "stopped"}
# A settled task (finished its reply, not yet collected) may have its process ended too: at collect before the completed
# marker is written, and before its reviewer starts.
ENDABLE = FINISHED | {"settled"}
READ_ONLY_ROLES = {"code-reviewer", "data-flow-reviewer", "vulnerability-reviewer"}
FORCE_ALIVE = "pass --force-alive"  # retry and review have it; dispatch and tick do not (see alive_on_workdir)


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _or(override):
    return f", or {override}" if override else ""


def strict_meta(path, override=""):
    """The task's meta.json as an object, or a refusal naming the task. `override` is the flag the caller has to proceed
    anyway (retry and review have --force-alive; dispatch and tick have none and name nothing)."""
    try:
        value = json.loads((Path(path) / "meta.json").read_text())
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot read {Path(path).name}/meta.json ({error.__class__.__name__}), so a live process of "
                         f"it cannot be ruled out. Restore the file{_or(override)}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{Path(path).name}/meta.json is not an object, so a live process of it cannot be ruled out. "
                         f"Restore the file{_or(override)}")
    return value


def pane_table():
    """{pane id: (alive, pid)} for every tmux pane. An unreadable table, or any row of it that does not parse, refuses:
    a guess could end the wrong process or let a second writer start."""
    run = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_id} #{pane_dead} #{pane_pid}"], text=True, capture_output=True)
    if run.returncode:
        raise RuntimeError(f"cannot read tmux panes: {run.stderr.strip() or 'tmux failed'}. Run this from inside the tmux session "
                           "(`tmux list-panes -a` must work there), then try again")
    table = {}
    for line in run.stdout.splitlines():
        parts = line.split()
        if not (len(parts) == 3 and parts[1] in {"0", "1"} and parts[2].isdigit()):
            raise RuntimeError(f"cannot read tmux panes: unexpected row {line!r}")
        table[parts[0]] = (parts[1] == "0", int(parts[2]))
    return table


def live_process(path, table, meta=None):
    """(pane, pid) when the task's pane still runs a process, else None. A task with no pane recorded (queued) has none;
    a pane recorded as anything but a string refuses. A string that is no pane of the server (a gone pane) is not live."""
    meta = strict_meta(path) if meta is None else meta
    pane = meta.get("pane")
    if pane is None or pane == "":
        return None
    if not isinstance(pane, str):
        raise ValueError(f"{Path(path).name} records an unreadable pane {pane!r}, so a live process of it cannot be ruled out. "
                         f"Set pane in {Path(path).name}/meta.json to the pane id (like %3) or remove the key")
    entry = table.get(pane)
    return (pane, entry[1]) if entry and entry[0] else None


def same_tree(a, b):
    return os.path.realpath(a) == os.path.realpath(b)


def effective_role(role, task_id):
    """The role a task counts as: its recorded one, else a reviewer when it is named review-..., else generic."""
    return role or ("code-reviewer" if task_id.startswith("review-") else "generic")


def _commands():
    """[(pid, command)] of every running (not zombie) process; an unreadable table refuses."""
    run = subprocess.run(["ps", "-ww", "-ax", "-o", "pid=,stat=,command="], text=True, capture_output=True)
    if run.returncode:
        raise RuntimeError(f"cannot read the process table: {run.stderr.strip() or 'ps failed'}")
    rows = []
    for line in run.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and not parts[1].startswith("Z"):
            rows.append((int(parts[0]), parts[2]))
    return rows


def task_processes(task_dir, rows=None):
    """The pids of running processes started from the task's own run.sh (tp-spawn starts a worker as `bash <task dir>/run.sh`).
    It needs no record of the task: it is how a task whose meta.json was never written, or whose reservation outlived the
    tp-spawn that made it, is still found. The path is compared with backslashes removed (tp-spawn quotes it) and in both its
    given and resolved form."""
    task_dir = Path(task_dir)
    wanted = {f"{task_dir}/run.sh", f"{os.path.realpath(task_dir)}/run.sh"}
    return [pid for pid, command in (_commands() if rows is None else rows)
            if any(path in command.replace("\\", "") for path in wanted)]


def end_command(pids, table):
    """The command that ends the processes `pids`: their pane when one of them is a pane's process, else kill."""
    for pane, (alive, pid) in sorted(table.items()):
        if alive and pid in pids:
            return f"tmux kill-pane -t {pane}"
    return f"kill {pids[0]}"


def _warn(message):
    print(f"tp-agent: warning: {message}", file=sys.stderr)


def alive_on_workdir(tasks, workdir, skip=None, override="", warn=_warn):
    """Live tasks (id, role, pane, pid) whose recorded working tree is `workdir`, among the task directories of `tasks`.
    The one scan behind the retry/review guards and a lane's one-writer rule. A live task with no recorded working tree
    refuses: its tree is unknown, so it cannot be ruled out as a writer here. A task directory with no readable record (a
    spawn that failed or was killed after making it) refuses only when something still runs from it (its run.sh is a live
    process); a dead one is not a holder, and `warn` says so. `override` names the caller's flag to go on anyway, if any."""
    tasks = Path(tasks)
    table, found, rows = pane_table(), [], None
    for other in sorted(tasks.iterdir()):
        if not other.is_dir() or other.name.startswith(".") or other.name == skip:
            continue
        try:
            meta = strict_meta(other, override)
        except ValueError as unreadable:
            rows = _commands() if rows is None else rows
            running = task_processes(other, rows)
            if running:
                raise ValueError(f"{other.name} has no readable meta.json and a process of it is running (pid {', '.join(map(str, running))}), "
                                 f"so it may write on this tree. End it with `{end_command(running, table)}`{_or(override)}") from unreadable
            warn(f"{other.name} has no readable meta.json and nothing runs from it, so it is not a holder of any tree; "
                 f"it is left in place. Once you do not need it: rm -r {other}")
            continue
        live = live_process(other, table, meta)
        if not live:
            continue
        other_dir = meta.get("workdir")
        if not isinstance(other_dir, str) or not other_dir:
            raise ValueError(f"{other.name} is alive (pane {live[0]}, pid {live[1]}) with no recorded working tree, so it cannot be "
                             f"ruled out as a writer on this one. End it with `tmux kill-pane -t {live[0]}`{_or(override)}")
        if same_tree(other_dir, workdir):
            role = effective_role(meta.get("role"), other.name)
            found.append((other.name, role, *live))
    return found


def alive_on_tree(path, skip=None):
    """Live tasks (id, role, pane, pid) on `path`'s working tree. Refuses when the task's own working tree, or the
    record of any other task that might be alive, cannot be read."""
    path = Path(path)
    workdir = strict_meta(path, FORCE_ALIVE).get("workdir")
    if not isinstance(workdir, str) or not workdir:
        raise ValueError(f"{path.name} has no recorded working tree, so a live process on it cannot be ruled out; "
                         f"add workdir to {path.name}/meta.json, or {FORCE_ALIVE}")
    return alive_on_workdir(path.parent, workdir, skip, FORCE_ALIVE)


def describe(found):
    return ", ".join(f"{ident} (pane {pane}, pid {pid})" for ident, _, pane, pid in found)


def guard_retry(path):
    busy = alive_on_tree(path)
    if busy:
        raise ValueError(f"a process is alive on the same working tree: {describe(busy)}. Collect or stop it "
                         "(tp-agent reap ends finished ones), or pass --force-alive")


def guard_review(path):
    """A reviewer is read-only, so it may run beside other reviewers but beside nothing that writes, the reviewed task's
    own process included: tp-review ends that one (end_or_refuse) before this runs, so a live author refuses here too."""
    busy = [row for row in alive_on_tree(path) if row[1] not in READ_ONLY_ROLES]
    if busy:
        raise ValueError(f"a writer is alive on {Path(path).name}'s working tree: {describe(busy)}; a reviewer must not run beside it. "
                         f"End it (`tp-agent stop {busy[0][0]}`, or tp-agent reap for finished ones) or pass --force-alive; "
                         "--force-alive does not apply to a lane's working tree, where only ending the writer helps")


def _ended(path, life, pane, pid, how):
    if life != "stopped":  # a stopped task keeps its stopped marker; an ended settled or collected one is not a failure
        (path / "exit").write_text("0\n")
    (path / "reaped.json").write_text(json.dumps({"reaped": _now(), "pane": pane, "pid": pid, "signal": how}) + "\n")
    return {"ended": True, "pane": pane, "pid": pid, "signal": how}


def end_process(path):
    """End the worker process of a collected or stopped task, keeping its pane and scrollback. Records an exit of 0
    so the ended process does not turn a result into a failure. Refuses a task that is working, paused or failed."""
    path = Path(path)
    life = task_lifecycle(path)[0]
    if life not in ENDABLE:
        raise ValueError(f"{path.name} is {life}, not finished; its process is left running")
    live = live_process(path, pane_table())
    if not live:
        return {"ended": False, "reason": "no live process"}
    pane, pid = live
    for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 3)):
        try:
            if os.getpgid(pid) == pid:  # the pane process leads its group: the worker and its children go together
                os.killpg(pid, sig)
            else:
                os.kill(pid, sig)
        except ProcessLookupError:  # the process is gone already; tmux has not marked the pane dead yet
            return _ended(path, life, pane, pid, "none")
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not live_process(path, pane_table()):
                return _ended(path, life, pane, pid, sig.name)
            time.sleep(0.1)
    return {"ended": False, "pane": pane, "pid": pid, "reason": "process survived SIGKILL"}


def end_or_refuse(path):
    """end_process, but a process that is still alive afterwards, or that cannot be looked up, is a ValueError/RuntimeError
    rather than a result to inspect. Collect (tp-agent, tp-contract) calls this BEFORE it writes the completed marker, so a
    failure leaves the task settled and the next collect tries again; a task already completed (marked by an earlier
    version, or by a crash) is retried the same way."""
    ended = end_process(path)
    if ended.get("ended") is False and ended.get("reason") != "no live process":
        raise RuntimeError(f"{Path(path).name}'s process is still alive: {ended['reason']} (pane {ended.get('pane')}, pid {ended.get('pid')})")
    return ended


# --- stopping a task's pane (tp-kill) and orphans a failed stop leaves ---------------------------------------------
# process.json in the task dir names what a stop is about to end (pane, pid, process group, and every member with its
# start time). It is removed once the group is confirmed gone; one left behind is an orphan that `tp-agent reap` finds
# after the pane is gone, and ends only when a recorded member still runs with its recorded start time (a recycled pid
# of an unrelated process never matches).

def _ps():
    """{pid: (pgid, stat, start)} for every process; an unreadable table refuses."""
    run = subprocess.run(["ps", "-ax", "-o", "pid=,pgid=,stat=,lstart="], text=True, capture_output=True)
    if run.returncode:
        raise RuntimeError(f"cannot read the process table: {run.stderr.strip() or 'ps failed'}")
    rows = {}
    for line in run.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[0].isdigit() and parts[1].isdigit():
            rows[int(parts[0])] = (int(parts[1]), parts[2], " ".join(parts[3].split()))
    return rows


def _members(rec, rows):
    """Live (not zombie) processes of the recorded target: its whole group, or just its pid."""
    if rec["group"]:
        return {pid: row for pid, row in rows.items() if row[0] == rec["pgid"] and not row[1].startswith("Z")}
    row = rows.get(rec["pid"])
    return {rec["pid"]: row} if row and not row[1].startswith("Z") else {}


def _mine(rec, rows):
    """The recorded target's live members that are provably the recorded processes (same pid, same start time)."""
    known = {pid: start for pid, start in rec["members"]}
    return {pid: row for pid, row in _members(rec, rows).items() if known.get(pid) == row[2]}


def _signal(rec, sig):
    try:
        os.killpg(rec["pgid"], sig) if rec["group"] else os.kill(rec["pid"], sig)
    except ProcessLookupError:
        pass


def _escalate(rec):
    """TERM, then KILL, the recorded target; True once none of it is left."""
    for sig, wait in ((None, 2), (signal.SIGTERM, 4), (signal.SIGKILL, 4)):
        if sig is not None:
            _signal(rec, sig)
        deadline = time.monotonic() + wait
        while True:
            if not _members(rec, _ps()):
                return True
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
    return False


def _survivor(rec):
    return f"pid {rec['pid']}, process group {rec['pgid']}"


def prepare_stop(path, pane):
    """Validate the pane's process and record it as process.json before anything is ended. A pane that is gone or dead has
    nothing to record. A pid that cannot be read or is no valid worker pid (0, 1, the caller's own group) refuses."""
    path = Path(path)
    entry = pane_table().get(pane) if pane else None  # a task that never had a pane (queued) has nothing to read
    if not entry or not entry[0]:
        return None
    pid = entry[1]
    if pid <= 1:
        raise RuntimeError(f"pane {pane} reports pid {pid}, not a worker pid; nothing was stopped")
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError as error:
        raise RuntimeError(f"pane {pane} reports pid {pid}, which cannot be read as a process; nothing was stopped") from error
    if pgid <= 1 or pgid == os.getpgrp():
        raise RuntimeError(f"pane {pane} pid {pid} is in process group {pgid}, which is not a worker's; nothing was stopped")
    rec = {"pane": pane, "pid": pid, "pgid": pgid, "group": pgid == pid}
    rows = _ps()
    if pid not in rows:
        raise RuntimeError(f"pane {pane} pid {pid} is not in the process table; nothing was stopped")
    rec["members"] = [[p, row[2]] for p, row in sorted(_members(rec, rows).items())]
    rec["recorded"] = _now()
    tmp = path / "process.json.tmp"
    tmp.write_text(json.dumps(rec) + "\n")
    os.replace(tmp, path / "process.json")
    return rec


def read_record(path):
    """process.json as a validated record, None when absent; an unreadable or malformed one refuses."""
    file = Path(path) / "process.json"
    if not file.exists():
        return None
    try:
        rec = json.loads(file.read_text())
        ok = (isinstance(rec, dict) and all(isinstance(rec.get(k), int) and not isinstance(rec.get(k), bool) and rec[k] > 1
                                            for k in ("pid", "pgid")) and isinstance(rec.get("group"), bool)
              and isinstance(rec.get("members"), list) and all(isinstance(m, list) and len(m) == 2 and isinstance(m[0], int)
                                                               and isinstance(m[1], str) for m in rec["members"]))
    except (OSError, ValueError):
        ok = False
    if not ok:
        raise RuntimeError(f"{Path(path).name}/process.json is unreadable, so what a failed stop left running cannot be told")
    return rec


def finish_stop(path, pane):
    """Kill the pane, then end its recorded process and confirm it gone. Raises RuntimeError naming the survivor when it
    is not; process.json is then kept for `tp-agent reap`. A leftover record from an earlier failed stop is retried too."""
    path = Path(path)
    rec = read_record(path)
    if pane and pane in pane_table():
        run = subprocess.run(["tmux", "kill-pane", "-t", pane], text=True, capture_output=True)
        if run.returncode and pane in pane_table():
            raise RuntimeError(f"cannot kill pane {pane}: {run.stderr.strip() or 'tmux failed'}")
    if rec is None:
        return None
    state = orphan_state(rec)
    if state == "gone":
        (path / "process.json").unlink(missing_ok=True)
        return rec
    if state != "ours" or not _escalate(rec):
        raise RuntimeError(f"{path.name}: {'cannot tell that the process is the worker' if state != 'ours' else 'process survived SIGKILL'} "
                           f"({_survivor(rec)}); recorded in {path.name}/process.json, `tp-agent reap --yes` retries")
    (path / "process.json").unlink(missing_ok=True)
    return rec


def orphan_state(rec):
    """gone (nothing of it runs) | ours (a recorded member runs with its recorded start time) | unknown (something runs in
    that group or pid that cannot be shown to be the worker: never signalled)."""
    rows = _ps()
    if not _members(rec, rows):
        return "gone"
    return "ours" if _mine(rec, rows) else "unknown"


def orphans(tasks, only=None):
    """Rows for tasks whose process.json names a process that still runs with its pane gone: {id, pane, pid, pgid, state}."""
    table, found = pane_table(), []
    for path in sorted(Path(tasks).iterdir()) if Path(tasks).exists() else []:
        if not path.is_dir() or (only and path.name not in only):
            continue
        rec = read_record(path)
        if rec is None:
            continue
        entry = table.get(rec["pane"])
        if entry and entry[0] and entry[1] == rec["pid"]:
            continue  # its pane still runs it: an ordinary live task, not an orphan
        found.append({"id": path.name, "pane": rec["pane"], "pid": rec["pid"], "pgid": rec["pgid"],
                      "state": orphan_state(rec), "orphan": True})
    return found


def end_orphan(tasks, row):
    """End one orphan row from orphans(); gone ones just lose their record. Refuses one that cannot be shown to be the worker."""
    path = Path(tasks) / row["id"]
    rec = read_record(path)
    if rec is None or orphan_state(rec) == "gone":
        (path / "process.json").unlink(missing_ok=True)
        return {"ended": True, "signal": "none"}
    if orphan_state(rec) != "ours":
        return {"ended": False, "reason": "cannot tell that the process is the worker's, so it is not signalled"}
    if not _escalate(rec):
        return {"ended": False, "reason": "process survived SIGKILL"}
    (path / "process.json").unlink(missing_ok=True)
    return {"ended": True, "signal": "KILL"}


if __name__ == "__main__":  # tp-kill: prepare <task-dir> <pane> | finish <task-dir> <pane>
    import sys
    try:
        if len(sys.argv) != 4 or sys.argv[1] not in {"prepare", "finish"}:
            raise RuntimeError("usage: worker_process.py prepare|finish <task-dir> <pane>")
        (prepare_stop if sys.argv[1] == "prepare" else finish_stop)(sys.argv[2], sys.argv[3])
    except (RuntimeError, OSError, ValueError) as error:
        print(f"tp-kill: {error}", file=sys.stderr)
        sys.exit(1)
