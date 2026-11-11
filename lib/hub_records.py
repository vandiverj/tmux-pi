"""hub_records — the shared records of the owner's hub workstream (docs/plans/det-prep/hub/interfaces.md, sections 3 and 4).

Python 3.9, stdlib only, so the shell hooks can run it with /usr/bin/python3. Later lanes extend this module; they do not
start another one for the same schema.

Event journal (section 3): `TASK_DIR/events.jsonl`, one JSON line per event, appended through lib/safe_write:
  {v: 1, id: "ev-<ulid>", at, task, actor, kind, ref?, state?, detail?, source, confidence}
`kind` is one of EVENT_KINDS. Tool names are normalised at write time by TOOL_TABLE (normalize_tool); the raw name is kept
in detail.raw_tool.

Request identity (section 4): `TASK_DIR/requests/<req-id>.json`
  {id, task, kind: permission|input|capability|question, opened, text_sha256, summary, status: open|resolved|withdrawn|superseded,
   resolved?, resolution?: {by, how, ref}}
A request is opened by the writer that first sees it (open_request). The same request seen again while it is open (same kind
and text_sha256) is the same id; once it is resolved, the same text is a new request with a new id. A message delivery never
resolves a request by itself: only resolve() with the request id does, and it says who, how and with what reference.

Command line (for the hooks; never fails the caller, prints one JSON object):
  hub_records.py claude-hook --task-dir D --prev-state S --new-state S [--prev-tool T]   (hook input JSON on stdin)
  hub_records.py pi-state --task-dir D --state S [--text-file F]
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import safe_write  # noqa: E402
from result_status import RESULT_STATUS_RE  # noqa: E402

EVENT_VERSION = 1
# The closed list of interfaces.md section 3, plus pause.unverified, which the HB4a brief (rule 4) asks for, and
# pause.unneeded (HB4a round 2 rule 4: the worker finished its turn before the pause took), which the list lacks
# (both proposed for interfaces.md in the HB4a report).
EVENT_KINDS = frozenset({
    "state", "request.opened", "request.resolved", "correction.requested", "correction.cancelled", "correction.delivered",
    "correction.confirmed", "correction.failed", "correction.unverified", "pause.requested", "pause.confirmed",
    "pause.unverified", "pause.unneeded", "hold.kept", "obligation.changed", "check.started", "check.finished", "queue.reason",
    "lease.taken", "lease.released"})
ACTORS = frozenset({"main", "worker", "reviewer", "hook", "watchdog", "owner", "test"})
SOURCES = frozenset({"file", "hook", "rpc", "pane", "transcript"})
CONFIDENCE = frozenset({"exact", "derived", "inferred"})
REQUEST_KINDS = frozenset({"permission", "input", "capability", "question"})
REQUEST_STATUSES = frozenset({"open", "resolved", "withdrawn", "superseded"})
REQUEST_ID = re.compile(r"req-[0-9A-HJKMNP-TV-Z]{26}")

# The one tool-name table (section 3): raw name -> (normalised name, safe). safe is None where it does not apply.
TOOL_TABLE = {
    "Bash": ("shell", None), "bash": ("shell", None),
    "Read": ("read", False), "read": ("read", False),
    "safe_read": ("read", True), "mcp__tmux-pi-safe__safe_read": ("read", True),
    "Grep": ("grep", False), "grep": ("grep", False),
    "safe_grep": ("grep", True), "mcp__tmux-pi-safe__safe_grep": ("grep", True),
    "Edit": ("edit", None), "edit": ("edit", None), "MultiEdit": ("edit", None),
    "Write": ("write", None), "write": ("write", None),
    "Glob": ("find", None), "glob": ("find", None), "find": ("find", None),
}

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_last = [0, 0]  # (ms, random) of the last id made in this process, so ids made in one millisecond still sort in order


def ulid():
    """26 characters, Crockford base32: 48 bits of milliseconds, then 80 random bits (one more than the last id made in
    this process in the same millisecond, so they sort in the order they were made)."""
    ms = int(time.time() * 1000)
    if ms <= _last[0]:
        ms, rand = _last[0], (_last[1] + 1) % (1 << 80)
    else:
        rand = secrets.randbits(80)
    _last[0], _last[1] = ms, rand
    value = (ms << 80) | rand
    return "".join(_CROCKFORD[(value >> (5 * i)) & 31] for i in reversed(range(26)))


def new_id(prefix):
    return "{}-{}".format(prefix, ulid())


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def text_sha256(text):
    return hashlib.sha256((text or "").encode("utf-8", "surrogatepass")).hexdigest()


def normalize_tool(name):
    """{"tool", "safe"?, "raw_tool"} for a raw tool name. A name not in TOOL_TABLE keeps its spelling as the tool."""
    name = name if isinstance(name, str) else ""
    tool, safe = TOOL_TABLE.get(name, (name, None))
    out = {"tool": tool, "raw_tool": name}
    if safe is not None:
        out["safe"] = safe
    return out


def _root(task_dir):
    """The state directory a task directory lives in, or None when it is not under tasks/ or archive/ (a test's dir)."""
    task_dir = Path(task_dir)
    return safe_write.task_root(task_dir) if task_dir.parent.name in ("tasks", "archive") else None


def _task(task_dir):
    return Path(task_dir).name


def event(task_dir, kind, actor, source, confidence, ref=None, state=None, detail=None):
    """Append one tp-event:v1 line to TASK_DIR/events.jsonl and return it. A tool name in detail["tool"] is normalised."""
    if kind not in EVENT_KINDS:
        raise ValueError("unknown event kind {!r}".format(kind))
    if actor not in ACTORS or source not in SOURCES or confidence not in CONFIDENCE:
        raise ValueError("bad actor, source or confidence: {!r} {!r} {!r}".format(actor, source, confidence))
    line = {"v": EVENT_VERSION, "id": new_id("ev"), "at": utc_now(), "task": _task(task_dir), "actor": actor, "kind": kind}
    if ref is not None:
        line["ref"] = ref
    if state is not None:
        line["state"] = state
    if detail:
        detail = dict(detail)
        if "tool" in detail:
            named = normalize_tool(detail.pop("tool"))
            detail.update(named)
        line["detail"] = detail
    line.update(source=source, confidence=confidence)
    safe_write.append_text(Path(task_dir) / "events.jsonl", json.dumps(line, sort_keys=False) + "\n", root=_root(task_dir))
    return line


def read_events(task_dir):
    """The events of a task, oldest first; a line that is not one JSON object is skipped."""
    out = []
    try:
        lines = (Path(task_dir) / "events.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for text in lines:
        try:
            value = json.loads(text)
        except ValueError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


# ------------------------------------------------------------------ requests

def requests_dir(task_dir):
    return Path(task_dir) / "requests"


@contextlib.contextmanager
def _locked(task_dir):
    """One writer at a time over a task's requests/ (fcntl, released on process death)."""
    folder = requests_dir(task_dir)
    safe_write.ensure_dir(folder, root=_root(task_dir))
    lock = folder / ".lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | safe_write.NOFOLLOW, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def read_request(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and REQUEST_ID.fullmatch(str(value.get("id", ""))) else None


def requests(task_dir):
    """Every readable request record of a task, oldest id first. No requests/ directory: []."""
    folder = requests_dir(task_dir)
    if not folder.is_dir() or folder.is_symlink():
        return []
    out = []
    for path in sorted(folder.glob("req-*.json")):
        record = read_request(path) if REQUEST_ID.fullmatch(path.stem) else None
        if record is not None and record["id"] == path.stem:  # a file is the request it is named for
            out.append(record)
    return out


def find_request(task_dir, req_id):
    if not isinstance(req_id, str) or not REQUEST_ID.fullmatch(req_id):
        return None
    record = read_request(requests_dir(task_dir) / (req_id + ".json"))
    return record if record is not None and record["id"] == req_id else None


def _save(task_dir, record):
    safe_write.write_atomic(requests_dir(task_dir) / (record["id"] + ".json"), json.dumps(record, indent=2) + "\n",
                            root=_root(task_dir))


def _close(task_dir, record, status, by, how, ref, actor, source, confidence="exact", detail=None):
    record = dict(record, status=status, resolved=utc_now(), resolution={"by": by, "how": how, "ref": ref})
    _save(task_dir, record)
    event(task_dir, "request.resolved", actor, source, confidence, ref=record["id"], state=status,
          detail=dict({"kind": record["kind"], "by": by, "how": how, "resolution_ref": ref}, **(detail or {})))
    return record


def open_request(task_dir, kind, text, summary, actor, source, confidence="exact", supersede=False, detail=None):
    """(record, created). The open request of this kind with this text's sha256, or a new one (request.opened event).
    supersede: every other open request of the same kind is closed as superseded by the new one (a Claude permission prompt
    replaced by a different one)."""
    if kind not in REQUEST_KINDS:
        raise ValueError("unknown request kind {!r}".format(kind))
    digest = text_sha256(text)
    with _locked(task_dir):
        current = [r for r in requests(task_dir) if r.get("status") == "open" and r.get("kind") == kind]
        for record in current:
            if record.get("text_sha256") == digest:
                return record, False
        record = {"id": new_id("req"), "task": _task(task_dir), "kind": kind, "opened": utc_now(), "text_sha256": digest,
                  "summary": _clip(summary), "status": "open"}
        _save(task_dir, record)
        event(task_dir, "request.opened", actor, source, confidence, ref=record["id"], state="open",
              detail=dict({"kind": kind, "summary": record["summary"]}, **(detail or {})))
        if supersede:
            for old in current:
                _close(task_dir, old, "superseded", actor, "superseded", record["id"], actor, source, confidence)
        return record, True


def resolve(task_dir, req_id, by, how, ref, actor, source, confidence="exact", status="resolved", detail=None):
    """(record, changed). Closes an open request; one already closed is returned unchanged. None when there is no such id."""
    if status not in REQUEST_STATUSES - {"open"}:
        raise ValueError("cannot close a request as {!r}".format(status))
    with _locked(task_dir):
        record = find_request(task_dir, req_id)
        if record is None:
            return None, False
        if record.get("status") != "open":
            return record, False
        return _close(task_dir, record, status, by, how, ref, actor, source, confidence, detail), True


def resolve_open(task_dir, kind, by, how, ref, actor, source, confidence="exact", detail=None):
    """Close every open request of `kind` (the worker left the state that asked); the closed records."""
    if not requests_dir(task_dir).is_dir():
        return []
    with _locked(task_dir):
        return [_close(task_dir, r, "resolved", by, how, ref, actor, source, confidence, detail)
                for r in requests(task_dir) if r.get("status") == "open" and r.get("kind") == kind]


def _clip(text, n=160):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= n else text[:n - 1] + "…"


def open_requests(task_dir, kinds=None):
    """The open requests of a task (of `kinds` when given), oldest first: what an "anything outstanding" reader asks."""
    return [r for r in requests(task_dir) if r.get("status") == "open" and (kinds is None or r.get("kind") in kinds)]


def request_log(task_dir):
    """Every request transition of a task, in the order events.jsonl has them: the append-only log a notifier delivers from
    (never from row_requests, which is a display window). Each is {seq, id, change: "opened"|"closed", kind, summary, state,
    how?}: seq counts request transitions only, from 1, so a reader keeps "the transitions I have taken" as one number per
    task. A request.opened or request.resolved event is written for every opening and every closing (resolved, superseded,
    withdrawn) under the requests/ lock, so the log has each transition once, in the order it happened."""
    out, summaries = [], {}
    for line in read_events(task_dir):
        kind, ref = line.get("kind"), line.get("ref")
        if kind not in ("request.opened", "request.resolved") or not REQUEST_ID.fullmatch(str(ref or "")):
            continue
        detail = line.get("detail") if isinstance(line.get("detail"), dict) else {}
        if kind == "request.opened":
            summaries[ref] = str(detail.get("summary") or "")
        entry = {"seq": len(out) + 1, "id": ref, "change": "opened" if kind == "request.opened" else "closed",
                 "kind": str(detail.get("kind") or ""), "summary": summaries.get(ref, ""), "state": str(line.get("state") or "")}
        if kind == "request.resolved":
            entry["how"] = str(detail.get("how") or line.get("state") or "")
        out.append(entry)
    return out


def row_requests(task_dir, keep=20):
    """What `tp-agent list` shows of a task's requests, oldest id first, as {id, kind, status, summary, opened, resolved?,
    how?}: EVERY open request, and the `keep` most recently closed ones (by close time, so a request that has just closed
    shows however old it is). Only history is bounded: an open request is never hidden behind closed ones. A display window
    only: notices come from request_log. None when the task has no request files (its row is then what it was before HB4a)."""
    found = requests(task_dir)
    if not found:
        return None
    closed = sorted((r for r in found if r.get("status") != "open"), key=lambda r: (str(r.get("resolved") or ""), r["id"]))
    shown = {r["id"] for r in closed[-keep:]} if keep > 0 else set()
    out = []
    for r in (r for r in found if r.get("status") == "open" or r["id"] in shown):
        row = {k: r.get(k) for k in ("id", "kind", "status", "summary", "opened")}
        if r.get("status") != "open":
            row["resolved"] = r.get("resolved")
            row["how"] = (r.get("resolution") or {}).get("how")
        out.append(row)
    return out


# ------------------------------------------------------------------ the harness writers

PERMISSION = re.compile(r"permission", re.I)
TOOL_IN_MESSAGE = re.compile(r"permission to use (\S+)", re.I)


def claude_hook(task_dir, hook, prev_state, new_state, prev_tool=""):
    """report.sh's request bookkeeping, after state.json was written. A permission Notification that put the worker in
    `permission` opens (or finds) the permission request for its message; leaving `permission` for any other state, or a
    PermissionDenied, resolves the open permission request(s) with how `left-state`."""
    name = hook.get("hook_event_name") if isinstance(hook, dict) else None
    done = {"opened": None, "resolved": []}
    if new_state == "permission" and name == "Notification":
        message = hook.get("message") if isinstance(hook.get("message"), str) else ""
        if PERMISSION.search(message):
            found = TOOL_IN_MESSAGE.search(message)
            tool = found.group(1).rstrip(".,;:") if found else (prev_tool.split(" ", 1)[0] if prev_tool else "")
            record, created = open_request(task_dir, "permission", message, message, "hook", "hook", supersede=True,
                                           detail={"tool": tool} if tool else None)
            done["opened"] = {"id": record["id"], "created": created}
    elif (prev_state == "permission" and new_state != "permission") or name == "PermissionDenied":
        done["resolved"] = [r["id"] for r in resolve_open(task_dir, "permission", "hook", "left-state", None, "hook", "hook",
                                                          detail={"hook": name, "state": new_state})]
    return done


def pi_state(task_dir, state, text=""):
    """reporter.ts's request bookkeeping. `waiting` (the agent settled, ready for input) with a reply that has no
    RESULT_STATUS line opens (or finds) the input request for its reply text: the worker waits for an answer. Any other
    state, or a reply that ends the task (a RESULT_STATUS line), resolves the open input request(s) with how `left-state`."""
    done = {"opened": None, "resolved": []}
    if state == "waiting" and not RESULT_STATUS_RE.search(text or ""):
        record, created = open_request(task_dir, "input", text, text.strip().splitlines()[-1] if text.strip() else "waiting for input",
                                       "worker", "hook", confidence="derived")
        done["opened"] = {"id": record["id"], "created": created}
    else:
        done["resolved"] = [r["id"] for r in resolve_open(task_dir, "input", "worker", "left-state", None, "worker", "hook",
                                                          confidence="derived", detail={"state": state})]
    return done


def main(argv=None):
    parser = argparse.ArgumentParser(prog="hub_records")
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("claude-hook")
    c.add_argument("--task-dir", required=True)
    c.add_argument("--prev-state", default="")
    c.add_argument("--new-state", default="")
    c.add_argument("--prev-tool", default="")
    p = sub.add_parser("pi-state")
    p.add_argument("--task-dir", required=True)
    p.add_argument("--state", required=True)
    p.add_argument("--text-file")
    args = parser.parse_args(argv)
    try:
        if not Path(args.task_dir).is_dir():
            raise ValueError("no task directory")
        if args.command == "claude-hook":
            try:
                hook = json.loads(sys.stdin.read() or "{}")
            except ValueError:
                hook = {}
            out = claude_hook(args.task_dir, hook, args.prev_state, args.new_state, args.prev_tool)
        else:
            text = ""
            if args.text_file:
                try:
                    text = Path(args.text_file).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    text = ""
            out = pi_state(args.task_dir, args.state, text)
    except Exception as error:  # noqa: BLE001 - a hook must never fail its caller; the error is printed
        print(json.dumps({"error": str(error)}))
        return 0
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
