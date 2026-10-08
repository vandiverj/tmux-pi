#!/usr/bin/env python3
"""tp_rows.py TASKS_DIR PANES — the status rows for every task, in one process.

Replaces the shell chain tp_rows used to run per task (cat, jq, cut, awk, awk). Output is
identical: one line per task, oldest meta.json first, fields joined by \\x1f:
    id worker status tool tokens cost model
PANES is `tmux list-panes -a -F '#{pane_id} #{pane_dead}'`. A task whose meta.json or state.json
jq would refuse (invalid JSON, a value jq cannot index or join) is left out, as before.
The lifecycle state (tp-contract status) is not read here; callers join it afterwards.
"""
import json
import math
import os
import sys

SEP = "\x1f"


class Refused(Exception):
    """A record the old jq program would have failed on."""


def parse_all(data):
    """Every JSON value in data, like `jq -s`; Refused when jq would reject the text."""
    text = data.decode("utf-8", "replace")
    decoder, values, i, n = json.JSONDecoder(), [], 0, len(text)
    while True:
        while i < n and text[i] in " \t\n\r":
            i += 1
        if i >= n:
            return values
        try:
            value, i = decoder.raw_decode(text, i)
        except ValueError:
            raise Refused
        values.append(value)


def num(x):
    """A number as jq prints it."""
    if isinstance(x, float) and x == int(x) and abs(x) < 1e17:
        return str(int(x))
    return repr(x)


def isnum(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def tostr(x):
    """A value as `join` writes it; arrays and objects are an error there."""
    if x is None:
        return ""
    if x is True:
        return "true"
    if x is False:
        return "false"
    if isnum(x):
        return num(x)
    if isinstance(x, str):
        return x
    raise Refused


def alt(x, default):
    """jq `x // default`."""
    return default if x is None or x is False else x


def k(x):
    """Token count as 1.2k / 3.4M; null and booleans print as themselves, other types are an error."""
    if x is None or isinstance(x, bool):
        return tostr(x) if x is not None else "null"
    if not isnum(x):
        raise Refused
    if x >= 1000000:
        return num(math.floor(x / 100000) / 10) + "M"
    if x >= 1000:
        return num(math.floor(x / 100) / 10) + "k"
    return num(x)


def rounded(x):
    """jq `round` (half away from zero) for a positive number."""
    f = math.floor(x)
    return f + 1 if x - f >= 0.5 else f


def row_fields(meta_bytes, state_bytes):
    """(id, worker, pane, state, tool, tokens, cost, model) for one task."""
    values = parse_all(meta_bytes + (state_bytes if state_bytes is not None else b"{}"))
    m = values[0] if len(values) > 0 else None
    s = values[1] if len(values) > 1 else None
    m = {} if m is None else m
    s = {} if s is None else s
    if not isinstance(m, dict) or not isinstance(s, dict):
        raise Refused
    tokens = ""
    t = s.get("tokens")
    if t is not None and t is not False:
        if not isinstance(t, dict):
            raise Refused
        cache = alt(t.get("cacheRead"), 0), alt(t.get("cacheWrite"), 0)
        if not (isnum(cache[0]) and isnum(cache[1])):
            raise Refused
        tokens = "↑%s c%s ↓%s" % (k(t.get("input")), k(cache[0] + cache[1]), k(t.get("output")))
    cost_value = alt(s.get("cost"), 0)
    if s.get("costKnown") is False or (s.get("harness") == "claude" and cost_value == 0 and isnum(cost_value)):
        cost = "$?"
    elif isnum(cost_value) and cost_value > 0:
        cost = "$" + num(rounded(cost_value * 1000) / 1000)
    elif isnum(cost_value) or isinstance(cost_value, bool):
        cost = ""
    else:
        raise Refused   # a string, array or object cost: jq cannot round it
    default_model = "default" if m.get("model") == "" else m.get("model")
    model_parts = [alt(s.get("model"), default_model)]
    if m.get("thinking") != "":
        model_parts.append(m.get("thinking"))
    return (tostr(m.get("id")), tostr(m.get("worker")), tostr(m.get("pane")),
            tostr(alt(s.get("state"), "starting")), tostr(alt(s.get("tool"), "")),
            tokens, cost, ":".join(tostr(p) for p in model_parts))


def read_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def main(tasks, panes_text):
    dead_by_pane = {}
    for line in panes_text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            dead_by_pane.setdefault(parts[0], parts[1])
    found = []
    try:
        entries = list(os.scandir(tasks))
    except OSError:
        entries = []
    for entry in entries:
        meta = os.path.join(entry.path, "meta.json")
        try:
            found.append((os.stat(meta).st_mtime, entry.path, meta))
        except OSError:
            continue
    found.sort(key=lambda item: item[0])
    out = []
    for _, directory, meta in found:
        meta_bytes = read_bytes(meta)
        if meta_bytes is None:
            continue
        try:
            id_, worker, pane, state, tool, tokens, cost, model = row_fields(
                meta_bytes, read_bytes(os.path.join(directory, "state.json")))
        except (Refused, RecursionError):
            continue
        dead = dead_by_pane.get(pane)
        if dead == "1":
            code = read_bytes(os.path.join(directory, "exit"))
            code = "?" if code is None else code.decode("utf-8", "replace").rstrip("\n")
            state, tool = ("done" if code == "0" else "failed:" + code), ""
        elif dead != "0":
            state, tool = "killed", ""
        out.append(SEP.join((id_, worker, state, tool, tokens, cost, model)) + "\n")
    sys.stdout.buffer.write("".join(out).encode("utf-8", "replace"))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "")
