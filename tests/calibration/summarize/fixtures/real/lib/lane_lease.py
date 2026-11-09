"""Who is running on a lane's working tree (T10 L3): the lane's lease, shared by every session of the repository.

The one-writer rule needs the occupancy of a lane to live where the lane lives. A lane's record and lock are in the
repository's common git directory, which every session of the repository shares; a session's tasks and queue are not. So
the occupants are kept in `<common git dir>/tmux-pi/lanes/<name>/lease.json`, a list of holders, each naming the session
(its state directory), the task, the process (pid and start time) and, once the worker has a pane, the pane. Every change
is made under the lane lock.

Admission is ONE reservation, not a check followed by a start. `reserve` looks at the holders and adds the caller's in the
same locked step; the holder is the starting process (tp-spawn) and stays until `activate` replaces it with the worker's
own pane process, or `release` removes it when the start fails. Every path that starts a process on a lane tree runs
tp-spawn --lane (dispatch, tick, retry and tp-review all do), and tp-spawn calls `reserve` before it creates the pane, so
no path can start between another's check and its start.

A holder is stale only when its process is gone: no process with that pid and start time (a recycled pid has another start
time; a zombie is gone) AND nothing runs from the task's own run.sh. The second condition keeps a reservation whose tp-spawn
was killed (SIGKILL, a closed terminal) after the pane was made: the pane outlives its starter, and its run.sh is how it is
found again, with no record of the task needed. Age is never a reason. A lease file that cannot be read or does not validate refuses (it may hide a
writer); a missing file means nobody holds the lane. A writer excludes every other holder; a reader (a reviewer) excludes
only writers.

    python3 lane_lease.py reserve  --lane N --workdir DIR --task ID --role R --session S --pid P [--replaces ID]
    python3 lane_lease.py activate --lane N --workdir DIR --task ID --pane %N
    python3 lane_lease.py release  --lane N --workdir DIR --task ID
"""
import argparse
import json
import os
from pathlib import Path
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lanes  # noqa: E402
import worker_process  # noqa: E402

SCHEMA = "tp-lane-lease:v1"
FILE = "lease.json"
READ_ONLY = worker_process.READ_ONLY_ROLES
KEYS = {"kind", "access", "task", "role", "session", "pid", "start", "pane", "taken"}


def _holder(value, shown):
    if not isinstance(value, dict) or set(value) != KEYS:
        raise ValueError(f"{shown} holds an entry that is not a lease holder")
    if value["kind"] not in ("reservation", "worker") or value["access"] not in ("write", "read"):
        raise ValueError(f"{shown} holds an entry with an unknown kind or access")
    for key in ("task", "role", "session", "start", "taken"):
        if not isinstance(value[key], str) or not value[key]:
            raise ValueError(f"{shown} holds an entry whose {key} is not text")
    if not lanes.TASK_ID.fullmatch(value["task"]):
        raise ValueError(f"{shown} holds an entry with an invalid task id")
    if not isinstance(value["pid"], int) or isinstance(value["pid"], bool) or value["pid"] <= 0:
        raise ValueError(f"{shown} holds an entry whose pid is not a process id")
    if value["pane"] is not None and not isinstance(value["pane"], str):
        raise ValueError(f"{shown} holds an entry whose pane is not text")
    return value


def read(common_dir, name):
    """The holders recorded for lane `name`, stale ones included (validated). A missing file is []; a file that cannot be read
    or does not validate refuses naming it."""
    shown = lanes.lanes_dir(common_dir) / name / FILE
    text = lanes.read_lane_file(common_dir, name, FILE)
    if text is None:
        return []
    try:
        value = json.loads(text)
    except (ValueError, RecursionError) as error:
        raise ValueError(f"lease {shown} is not valid JSON ({error}); it may hide a writer, so it is not used. Once no worker runs on lane {name}, remove it: rm {shown}")
    if not isinstance(value, dict) or value.get("schema") != SCHEMA or not isinstance(value.get("holders"), list):
        raise ValueError(f"lease {shown} is not a {SCHEMA} lease; it may hide a writer, so it is not used. Once no worker runs on lane {name}, remove it: rm {shown}")
    return [_holder(entry, f"lease {shown}") for entry in value["holders"]]


def _running(holder, rows):
    row = rows.get(holder["pid"])
    return bool(row) and not row[1].startswith("Z") and row[2] == holder["start"]


def _task_dir(holder):
    return Path(holder["session"]) / "tasks" / holder["task"]


def live(holders, rows=None):
    """The holders still running: their pid with the recorded start time, or else a process started from the task's run.sh.
    An unreadable process table refuses."""
    rows = worker_process._ps() if rows is None else rows
    commands, kept = None, []
    for holder in holders:
        if not _running(holder, rows):
            commands = worker_process._commands() if commands is None else commands
            if not worker_process.task_processes(_task_dir(holder), commands):
                continue
        kept.append(holder)
    return kept


def access_for(role, task):
    return "read" if worker_process.effective_role(role, task) in READ_ONLY else "write"


def blocking(holders, access):
    """The holders (already live) that keep a task of `access` out: any holder blocks a writer; only a writer blocks a reader."""
    return [h for h in holders if access == "write" or h["access"] == "write"]


def conflicts(common_dir, name, role, task=None):
    """The live holders of lane `name` that a new task of `role` could not run beside (read only, no lock needed: a change is
    one atomic replace). A task already holding the lane does not conflict with itself."""
    access = access_for(role, task or "")
    return [h for h in blocking(live(read(common_dir, name)), access) if h["task"] != task]


def in_session(holder, here=None):
    """The words to put before a tp-agent command so it acts on the session the holder is in, from wherever it is typed.
    Another session's state directory is chosen with TP_STATE_DIR, but a pane of a different tmux session wins over it
    (lib/tp.sh tp_session refuses "another workspace"), so the pane's own session is dropped too. In the holder's own
    session the plain command does."""
    if here is not None and os.path.realpath(str(here)) == os.path.realpath(holder["session"]):
        return ""
    return f"env -u TMUX -u TMUX_PANE -u TP_SESSION TP_STATE_DIR={shlex.quote(holder['session'])} "


def holder_state(holder):
    """The lifecycle word of the holder's task (see worker_process.task_state); "" for a reservation or an unreadable record."""
    if holder["kind"] != "worker" or not (_task_dir(holder) / "meta.json").is_file():
        return ""
    return worker_process.task_state(_task_dir(holder).parent, holder["task"])


def clear_commands(holder, here=None):
    """The commands that end this (live) holder, cheapest first, for a refusal to name: each one clears it in the state the
    holder is in now. `here` is the state directory of the session that prints them."""
    pids = worker_process.task_processes(_task_dir(holder))
    if holder["kind"] == "worker" and (_task_dir(holder) / "meta.json").is_file():
        return worker_process.clear_commands(holder_state(holder), holder["task"], in_session(holder, here))
    if pids:
        try:
            table = worker_process.pane_table()
        except RuntimeError:
            table = {}
        return [f"`{worker_process.end_command(pids, table)}`"]
    return [f"wait: {holder['task']} is still starting"]


def clear_command(holder, here=None):
    return " or ".join(clear_commands(holder, here))


def describe(holders, here=None):
    """`task (reviewer, settled: finished, not collected, pid P, session S; to end it: CMD or CMD)` for each holder."""
    parts = []
    for h in holders:
        kind = "reviewer" if h["access"] == "read" else "writer"
        life = holder_state(h)
        state = "starting" if h["kind"] == "reservation" else worker_process.state_words(life)
        parts.append(f"{h['task']} ({kind}, {state}, pid {h['pid']}, session {h['session']}; "
                     f"to end it: {clear_command(h, here)})")
    return ", ".join(parts)


def waiting_helps(holders):
    """Does waiting free the lane? Only if every holder is still at work (or starting): a settled one never finishes by itself."""
    return all(h["kind"] == "reservation" or worker_process.finishes_alone(holder_state(h)) for h in holders)


def occupied(name, holders):
    """The first words of a refusal: who holds lane `name`. A reviewer is called a reviewer, never a writer."""
    if any(h["access"] == "write" for h in holders):
        return f"lane {name} already has a writer"
    return f"lane {name} is in use by a reviewer"


def advice_text(waits):
    """What to do: wait only when waiting can help (every holder is at work), else end it with the command named for it."""
    if waits:
        return "Wait for it to finish, or end it with the command named for it"
    return "It will not finish by itself (waiting never frees the lane): end it with the command named for it"


def advice(holders):
    return advice_text(waiting_helps(holders))


class StillRunning(ValueError):
    """A task's holder cannot be released: a process of it still runs."""


def _write(common_dir, name, holders):
    text = None if not holders else json.dumps({"schema": SCHEMA, "holders": holders}, indent=2) + "\n"
    lanes.write_lane_file(common_dir, name, FILE, text)


def _mine(common_dir, name, workdir):
    """(common dir, record) of lane `name` when `workdir` is inside its working tree, else a refusal: a lane tag that names no
    lane, or a tree that is not the lane's, starts nothing."""
    common = lanes.verified_common_dir(common_dir)
    record = lanes.read_lane(common, name)
    if not lanes._inside(os.path.realpath(workdir), os.path.realpath(record["worktree"])):
        raise ValueError(f"{workdir} is not inside lane {name}'s working tree {record['worktree']}")
    return common, record


def reserve(common_dir, name, task, role, session, pid, replaces=None):
    """Take lane `name` for `task` (process `pid`, the starting one) or refuse naming the holders. Done under the lane lock:
    the stale holders are dropped and the new one added in the same step. `replaces` is the task of a worker of this session
    that is handing its work to `task` (escalation) and ends right after: its holder does not keep `task` out, and it stays in
    the lease until it is gone, so the lane is never unheld. Returns the holder written."""
    access = access_for(role, task)
    with lanes.lane_lock(common_dir):
        rows = worker_process._ps()
        row = rows.get(pid)
        if not row or row[1].startswith("Z"):
            raise ValueError(f"process {pid}, which would hold lane {name}, is not running")
        kept = live(read(common_dir, name), rows)
        busy = [h for h in blocking(kept, access) if not (replaces and h["task"] == replaces and h["session"] == session)]
        if any(h["task"] == task for h in kept):
            raise ValueError(f"task {task} already holds lane {name}")
        if busy and access == "write":
            raise ValueError(f"{occupied(name, busy)}: {describe(busy, session)}. {advice(busy)}, then start this task again")
        if busy:
            raise ValueError(f"{occupied(name, busy)}: {describe(busy, session)}; a reviewer must not run beside it. "
                             f"{advice(busy)} (--force-alive does not change this), then review again")
        holder = {"kind": "reservation", "access": access, "task": task, "role": worker_process.effective_role(role, task),
                  "session": session, "pid": pid, "start": row[2], "pane": None, "taken": lanes.utc_now()}
        _write(common_dir, name, kept + [holder])
        return holder


def release(common_dir, name, task):
    """Remove `task`'s holder (and any stale one) from lane `name`; nothing to remove is fine. A task that still has a process
    running from its run.sh keeps its holder (raises StillRunning naming the command that ends it): a start that failed
    must not free a lane whose pane it could not end."""
    with lanes.lane_lock(common_dir):
        held = read(common_dir, name)
        mine = [h for h in held if h["task"] == task]
        running = [h for h in mine if worker_process.task_processes(_task_dir(h))]
        kept = [h for h in live(held) if h["task"] != task] + running
        if kept != held:
            _write(common_dir, name, kept)
        if running:
            raise StillRunning(f"task {task} still has a process running, so lane {name} stays held by it. End it with "
                               f"{clear_command(running[0])}; the lane is free once it is gone")


def activate(common_dir, name, task, pane):
    """Turn `task`'s reservation into the worker's: the holder becomes the pane's process. A pane that is already gone (or whose
    process is) leaves nothing alive, so the holder is dropped. Returns the new holder or None. An unreadable tmux pane table
    refuses and leaves the reservation, which the caller releases."""
    table = worker_process.pane_table()
    with lanes.lane_lock(common_dir):
        rows = worker_process._ps()
        holders = live(read(common_dir, name), rows)
        mine = [h for h in holders if h["task"] == task]
        if len(mine) != 1:
            raise ValueError(f"task {task} does not hold lane {name} (no reservation to activate)")
        others = [h for h in holders if h["task"] != task]
        entry = table.get(pane)
        row = rows.get(entry[1]) if entry and entry[0] else None
        if not row or row[1].startswith("Z"):
            _write(common_dir, name, others)
            return None
        worker = dict(mine[0], kind="worker", pid=entry[1], start=row[2], pane=pane)
        _write(common_dir, name, others + [worker])
        return worker


def main(argv):
    parser = argparse.ArgumentParser(prog="lane_lease.py")
    parser.add_argument("action", choices=("reserve", "activate", "release"))
    parser.add_argument("--lane", required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--role", default="")
    parser.add_argument("--session", default="")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--pane")
    parser.add_argument("--replaces")
    args = parser.parse_args(argv)
    try:
        common = lanes.git_common_dir(args.workdir)
        common, _ = _mine(common, args.lane, args.workdir)
        if args.action == "reserve":
            if not args.pid or not args.session:
                raise ValueError("reserve needs --pid and --session")
            reserve(common, args.lane, args.task, args.role, args.session, args.pid, args.replaces)
        elif args.action == "activate":
            if not args.pane:
                raise ValueError("activate needs --pane")
            activate(common, args.lane, args.task, args.pane)
        else:
            release(common, args.lane, args.task)
    except StillRunning as error:  # exit 3: the holder was kept; tp-spawn leaves its task directory alone
        print(f"tp-spawn: {error}", file=sys.stderr)
        return 3
    except (ValueError, RuntimeError, OSError) as error:
        print(f"tp-spawn: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
