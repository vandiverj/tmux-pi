#!/usr/bin/env python3
"""tp_detail.py TASKS_DIR LIFECYCLE — what the status pane draws under and around each row, for every row, in one process.

Reads the rows tp_rows.py printed on stdin and LIFECYCLE (the output of `tp-contract status`: id, life, detail joined by
\\x1f). Prints one line per input row, in the same order, fields joined by \\x1f:
    status detail has_reply task_first reply_first
status and detail are what the old per-row shell block worked out (lifecycle, collected/stopped, role, pending permission
and capability requests, a capability awaiting human approval); task_first / reply_first are the first non-blank line of task.md / result.md, which the pane
shows for the selected row. Reading them for every row at load time is what lets a key redraw touch no file.
Each file is streamed line by line to its first non-blank line, however deep that is, without holding more than one
line. Only a single line longer than LINE bytes (64 KiB, far wider than any terminal) is cut there.

`tp_detail.py --list TASKS LIFECYCLE CURRENT` prints what bin/tp-list prints for those rows instead: the same status and
detail, but with tp-list's own words for a pending permission or capability request (the target / what, not "permission:" /
"asks for"), the task row padded into columns, a ▸ in front of the row whose id is CURRENT, and the detail on the line below.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import request_file  # noqa: E402

SEP = "\x1f"
LINE = 65536


class Refused(Exception):
    """A value jq could not concatenate."""


def read_json(path):
    try:
        with open(path, "rb") as f:
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    try:
        return json.JSONDecoder().raw_decode(text.lstrip())[0]
    except ValueError:
        return None


def plus(*parts):
    """jq string addition: null is neutral, any other non-string is an error."""
    out = ""
    for p in parts:
        if p is None:
            continue
        if not isinstance(p, str):
            raise Refused
        out += p
    return out


def truthy(x):
    return x is not None and x is not False


def first_line(path):
    try:
        with open(path, "rb") as f:
            while True:
                buf = b""
                while True:   # one line, at most LINE bytes of it kept
                    chunk = f.readline(LINE - len(buf))
                    buf += chunk
                    if not chunk or chunk.endswith(b"\n") or len(buf) >= LINE:
                        break
                if not buf:
                    return ""
                text = buf.decode("utf-8", "replace")
                if text.strip():
                    return text.rstrip("\n").replace(SEP, " ").replace("\r", "")
                if len(buf) >= LINE and not buf.endswith(b"\n"):
                    while True:   # whitespace-only giant line: skip to its end
                        rest = f.readline(LINE)
                        if not rest:
                            return ""
                        if rest.endswith(b"\n"):
                            break
    except OSError:
        return ""


def role_of(directory):
    meta = read_json(os.path.join(directory, "meta.json"))
    role = meta.get("role") if isinstance(meta, dict) else None
    if not truthy(role):
        return "generic"
    if isinstance(role, str):
        return role
    return json.dumps(role, ensure_ascii=False, separators=(",", ":"))


def request(directory, name):
    """The request file as an object, or None when there is none, it is unreadable (see unreadable) or it is not pending."""
    kind, req = request_file.read_request(os.path.join(directory, name))
    return req if kind == request_file.OBJECT and req.get("status") == "pending" else None


def unreadable(directory):
    """Words for each request file that exists but is not exactly one JSON object; they are never skipped."""
    broken = request_file.unreadable_names(directory)
    return "%s: %s" % (request_file.WORDS, ", ".join(broken)) if broken else ""


def permission(directory):
    req = request(directory, "permission-request.json")
    if req is None:
        return ""
    try:
        if truthy(req.get("placeholder")):
            return plus("permission: ", "reveal " if req.get("scope") == "once" else "use ", req["placeholder"],
                        plus(" from ", req["source"]) if truthy(req.get("source")) else "",
                        plus(" [", req["scope"], "]") if truthy(req.get("scope")) else "")
        target = req.get("target")
        return plus("permission: ", target if truthy(target) else "sensitive read")
    except Refused:
        return ""


def capability(directory):
    req = request(directory, "capability-request.json")
    if req is None:
        return ""
    kind, what = req.get("kind"), req.get("what")
    try:
        return plus("asks for ", kind if truthy(kind) else "help", ": ", what if truthy(what) else "")
    except Refused:
        return ""


def approval(directory):
    """A capability waiting for human approval (pending-capability.json, the same test lib/contract.py uses)."""
    path = os.path.join(directory, "pending-capability.json")
    if not os.path.exists(path):
        return ""
    req = read_json(path)
    request = req.get("request") if isinstance(req, dict) else None
    return "capability approval pending" + (": " + request if isinstance(request, str) and request else "")


def jq_text(value):
    """What `jq -r` prints for a value: a string as it is, anything else as JSON."""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return json.dumps(value, ensure_ascii=False)


def listed_request(directory, name):
    """bin/tp-list's words for a pending permission-request.json / capability-request.json: target // what // "input needed"."""
    req = request(directory, name)
    if req is None:
        return ""
    for key in ("target", "what"):
        if truthy(req.get(key)):
            return jq_text(req[key])
    return "input needed"


def pad(text, width):
    """printf's `%*s` with width - len(text): a negative count pads on the other side, so too long still gets spaces."""
    return " " * abs(width - len(text))


def main(tasks, lifecycle_text, current=None):
    listing = current is not None
    life = {}
    for line in lifecycle_text.split("\n"):
        parts = line.split(SEP)
        if parts and parts[0] and parts[0] not in life:
            life[parts[0]] = (parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else "")
    out = []
    for line in sys.stdin.read().split("\n"):
        if not line:
            continue
        fields = line.split(SEP)
        id_, status = fields[0], fields[2] if len(fields) > 2 else ""
        d = os.path.join(tasks, id_)
        state, detail = life.get(id_, ("", ""))
        if state:
            status = state
        if os.path.isfile(os.path.join(d, "collected.json")):
            status = "completed"
        if os.path.isfile(os.path.join(d, "stopped.json")):
            status = "stopped"
        detail = "role " + role_of(d) + (" · " + detail if detail else "")
        if listing:
            found_all = (listed_request(d, "permission-request.json"), listed_request(d, "capability-request.json"),
                         approval(d).replace("\n", " "))   # tp-list has always flattened this one only
        else:
            found_all = (permission(d), capability(d), approval(d))
        for found in found_all:
            if found:
                detail = found + " · " + detail
                if status in ("working", "starting", "needs_input"):
                    status = "needs_input"
        broken = unreadable(d)
        if broken:   # never dropped, whatever the task's state: the owner has to look at it
            detail = broken + " · " + detail
            if status in ("working", "starting", "needs_input", "settled"):
                status = "needs_input"
        if listing:
            fields += [""] * (7 - len(fields))
            worker, tool, tokens, cost, model = fields[1], fields[3], fields[4], fields[5], fields[6]
            if tool:
                detail = (detail + " · " if detail else "") + tool
            out.append("%s %s%s%s%s%s%s%s%s%s%s%s\n" % (
                "▸" if id_ == current else " ", id_, pad(id_, 22), worker, pad(worker, 8), status, pad(status, 12),
                tokens, pad(tokens, 16), cost, pad(cost, 8), model))
            if detail:
                out.append("    " + detail + "\n")
            continue
        result = os.path.join(d, "result.md")
        try:
            has_reply = os.stat(result).st_size > 0
        except OSError:
            has_reply = False
        out.append(SEP.join((status, detail.replace("\n", " "), "1" if has_reply else "",
                             first_line(os.path.join(d, "task.md")),
                             first_line(result) if has_reply else "")) + "\n")
    sys.stdout.buffer.write("".join(out).encode("utf-8", "replace"))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--list"]:
        main(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else "")
    else:
        main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "")
