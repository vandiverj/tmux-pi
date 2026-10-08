#!/usr/bin/env python3
"""tp_window.py TASKS PANES CACHE KEEP START — what the status pane loads: a window of the task list, not all of it.

TASKS is the tasks directory, PANES `tmux list-panes -a -F '#{pane_id} #{pane_dead}'`, CACHE a file this helper keeps between
runs (or - for none), KEEP how many tasks the window holds, START where it begins: a position in the list (0 is the oldest
task) or `newest`. Tasks are ordered oldest first by meta.json mtime, as tp_rows.py orders them.

The window is KEEP consecutive tasks from START, plus every task that is starting, working, needs_input or paused, or has a
request file that cannot be read, whatever its age. Output, one record per line, first character says what it is:
    H<US>total<US>start<US>newest<US>positions   total tasks in the list, where the window really starts, the id of the newest
                                                  task, and the list position of each R line below, comma separated
    R<row>                                        a tp_rows.py row (id worker status tool tokens cost model)
    C<id>                                         a task outside the window whose files changed since the last run: the caller
                                                  refreshes its canonical record (task.md), which only the window's rows get otherwise
    U<line>                                       a line of the session token usage summary (lib/tp_usage.py)
Rows are the tp_rows.py rows, so a task reached by paging shows what a recent one shows; the caller joins the lifecycle
details afterwards.

A refresh must not read every record. CACHE keeps, per finished task, a fingerprint: the stat of meta.json and of every file
the lifecycle is worked out from (lifecycle_files.LIFECYCLE_FILES, the list contract.task_lifecycle itself is held to), plus the
pane's state, with the verdict and usage worked out from them. A task whose fingerprint is unchanged is taken from the cache; a
task that is active is never cached and is read every time; every other task is read again. Only finished tasks are written
to CACHE, and it is rewritten only when one of them is new, changed or gone, not on every refresh. The first run (no cache
yet) reads every record once and reports no C lines. A cache that cannot be read is rebuilt: it only ever holds what the
task files say.
"""
import json
import os
import stat
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import request_file  # noqa: E402
import tp_rows  # noqa: E402
import tp_usage  # noqa: E402
from lifecycle_files import LIFECYCLE_FILES  # noqa: E402

SEP = "\x1f"
ACTIVE = ("starting", "working", "needs_input", "paused")
VERSION = 4


def stamp(path):
    """'mtime_ns:size' of a regular file, '-' when it is missing, 'x' when it is something else (it still exists), 'l' when the
    entry exists but leads nowhere (a dangling link): that is not 'missing', request_file reads it as an unreadable request."""
    try:
        st = os.stat(path)
    except OSError:
        try:
            os.lstat(path)
        except OSError:
            return "-"
        return "l"
    return "%d:%d" % (st.st_mtime_ns, st.st_size) if stat.S_ISREG(st.st_mode) else "x"


def pane_word(dead_by_pane, pane):
    """The pane argument task_lifecycle takes: alive, dead or gone (bin/tp-contract's reading of the pane list)."""
    flag = dead_by_pane.get(pane)
    if flag is None:
        return "gone"
    return "alive" if flag == "0" else "dead"


def fingerprint(directory, pane_state):
    """Everything a task's verdict depends on, as one string: the stat of each lifecycle file and the pane's state."""
    prefix = directory + os.sep
    return "|".join([stamp(prefix + name) for name in LIFECYCLE_FILES] + [pane_state])


def load_cache(path):
    """The saved entries, {} when there are none, None when the cache is missing, unreadable or from another version."""
    if not path or path == "-":
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("v") != VERSION or not isinstance(data.get("e"), dict):
        return None
    return data["e"]


def save_cache(path, entries):
    if not path or path == "-":
        return
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"v": VERSION, "e": entries}, f, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def usage_of(directory):
    """The usage contribution of one state.json: a list of items, or None when the file is not valid JSON for jq."""
    data = tp_rows.read_bytes(os.path.join(directory, "state.json"))
    if data is None:
        return []
    try:
        return tp_usage.contribution(tp_rows.parse_all(data))
    except (tp_rows.Refused, RecursionError):
        return None


def evaluate(directory, pane_flags, task_lifecycle):
    """Read one task's records: id, pane, whether it is left out of the list, whether it must stay in the window ("a"), usage."""
    entry = {"i": "", "p": "", "s": True, "a": False, "u": usage_of(directory)}   # every task's state.json counts, listed or not
    meta_bytes = tp_rows.read_bytes(os.path.join(directory, "meta.json"))
    if meta_bytes is None:
        return entry
    try:
        fields = tp_rows.row_fields(meta_bytes, tp_rows.read_bytes(os.path.join(directory, "state.json")))
    except (tp_rows.Refused, RecursionError):
        return entry
    entry["i"], entry["p"], entry["s"] = fields[0], fields[2], False
    if fields[1] == "main":
        return entry    # a main-owned record is not a worker process; bin/tp-contract leaves it out too
    try:
        life = task_lifecycle(directory, pane_word(pane_flags, fields[2]))[0]
    except Exception:   # a record the lifecycle cannot read: show the task rather than hide it
        life = "working"
    entry["a"] = life in ACTIVE or bool(request_file.unreadable_names(directory))
    return entry


def main(tasks, panes_text, cache_path, keep, start):
    pane_flags = tp_rows.dead_flags(panes_text)
    try:
        scanned = list(os.scandir(tasks))
    except OSError:
        scanned = []
    found = []
    for entry in scanned:
        try:
            st = os.stat(os.path.join(entry.path, "meta.json"))
        except OSError:
            continue
        found.append((st.st_mtime_ns, entry.name, entry.path, "%d:%d" % (st.st_mtime_ns, st.st_size)))
    found.sort(key=lambda item: (item[0], item[1]))

    old = load_cache(cache_path)
    cold = old is None
    old = old or {}
    cache, saved, fresh = {}, {}, set()   # every task / the finished ones, which are written back / the ones read this run
    retained = 0
    task_lifecycle = None
    for _, name, directory, meta_stamp in found:
        previous = old.get(name)
        entry = None
        if isinstance(previous, dict) and previous.get("m") == meta_stamp:
            if previous.get("f") == fingerprint(directory, pane_word(pane_flags, previous.get("p"))):
                entry = previous
                retained += 1
        if entry is None:
            if task_lifecycle is None:
                from contract import task_lifecycle   # imported only when a record has to be read
            entry = evaluate(directory, pane_flags, task_lifecycle)
            fresh.add(name)
            if not entry["a"]:
                entry["m"], entry["f"] = meta_stamp, fingerprint(directory, pane_word(pane_flags, entry["p"]))
        cache[name] = entry
        if not entry["a"]:
            saved[name] = entry
    if cold or len(saved) != retained or retained != len(old):
        save_cache(cache_path, saved)

    listed = [(name, directory) for _, name, directory, _ in found if not cache[name]["s"]]
    total = len(listed)
    if start == "newest":
        first = max(0, total - keep)
    else:
        first = min(max(0, int(start)), max(0, total - keep))
    chosen = [i for i, (name, _) in enumerate(listed) if first <= i < first + keep or cache[name]["a"]]
    rows, positions, shown = [], [], set()
    for i in chosen:
        line = tp_rows.row_line(listed[i][1], pane_flags)
        if line is not None:
            rows.append(line)
            positions.append(str(i))
            shown.add(listed[i][0])
    newest = cache[listed[-1][0]]["i"] if listed else ""
    out = ["H" + SEP + SEP.join((str(total), str(first), newest, ",".join(positions)))]
    out.extend("R" + line.rstrip("\n") for line in rows)
    if not cold:
        out.extend("C" + cache[name]["i"] for name, _ in listed if name in fresh and name not in shown)

    records = []
    poisoned = False
    names = sorted(cache)
    for name in names:
        contributions = cache[name]["u"]
        if contributions is None:
            poisoned = True
        else:
            records.extend((False, item) for item in contributions)
    has_files = any(stamp(os.path.join(tasks, name, "state.json")) != "-" for name in names)
    main_state = os.path.join(os.path.dirname(tasks.rstrip("/")), "main", "state.json")
    main_items = None
    if stamp(main_state) != "-":
        has_files = True
        main_items = usage_of(os.path.dirname(main_state))
        if main_items is None:
            poisoned = True
        else:
            records.extend((True, item) for item in main_items)
    if has_files and not poisoned:
        out.extend("U" + line for line in tp_usage.lines(records, len(found)))
    sys.stdout.buffer.write(("\n".join(out) + "\n").encode("utf-8", "replace"))


if __name__ == "__main__":
    if len(sys.argv) != 6:
        sys.exit("usage: tp_window.py TASKS PANES CACHE KEEP START")
    try:
        keep_arg = int(sys.argv[4])
        if keep_arg < 1:
            raise ValueError
        if sys.argv[5] != "newest":
            int(sys.argv[5])
    except ValueError:
        sys.exit("tp_window.py: KEEP must be a positive number and START a number or 'newest'")
    main(sys.argv[1], sys.argv[2], sys.argv[3], keep_arg, sys.argv[5])
