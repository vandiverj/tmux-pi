#!/usr/bin/env python3
"""Where work waits, per open lane: the task, its state, its review, its open requests and the next valid action (HB6c).

    python3 tools/waits/status.py SESSION_TASKS_DIR [--repo R] [--json] [--now ISO]

One row per open lane of the repository (the lanes `tp-agent lanes` lists: lane_commands.list_lanes, without its step fields; the open step comes
from lane_steps.open_step over the session snapshot, because row_fields runs the lane gate, which writes git objects). SESSION_TASKS_DIR is a session's tasks directory (or the session directory that holds tasks/ and archive/).
The next action is the first match of NEXT_TABLE below; every condition is read from a record, never guessed: a condition that
cannot be read gives `unknown` with the reason. The task's state comes from timeline.build (the one reader of a task's time).

Read-only: it opens files for reading only and writes nothing. It prints no transcript text and no file content beyond ids,
names, states, verdict words and times. A missing or malformed record makes that row's field `unknown` and is listed under
`problems`; it never raises (a bad argument exits 2).
"""
import argparse
import importlib.util
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "lib"))
import contract  # noqa: E402
import hub_records  # noqa: E402
import lane_commands  # noqa: E402
import lane_remove  # noqa: E402
import lane_steps  # noqa: E402
import lanes  # noqa: E402
import request_file  # noqa: E402
import review_verdict  # noqa: E402

_spec = importlib.util.spec_from_file_location("wait_timeline", HERE / "timeline.py")
timeline = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(timeline)

WHO_ORDER = {"owner": 0, "main": 1, "worker": 2, "reviewer": 2, "unknown": 3}
RUNNING = lane_steps.AT_WORK          # starting, working, needs_input, paused: not finished (a task cannot be asked "running" otherwise)
KINDS = ("permission", "input", "capability", "question")
FINAL = lane_steps.RETRYABLE          # settled, completed: a reply with a RESULT_STATUS line exists
# failed, killed, stopped, escalated: the task ended with no usable result; neither running nor final (rule 10)
NEXT_TABLE = (  # (row, condition, next, who): the rule; first match wins. decide() is the code of it.
    (1, "an open request of kind permission", "answer the permission prompt at the pane (Main never approves)", "owner"),
    (2, "another open request", "tp-agent message TASK --resolves REQ --text ...", "main"),
    (3, "no task for the open step", "dispatch the step, or remove the lane", "main"),
    (4, "the task is running", "wait", "worker"),
    (5, "a review of the task is running", "wait", "reviewer"),
    (6, "final result, no review", "Main's checks, then tp-agent review TASK", "main"),
    (7, "the newest review says REQUEST_CHANGES", "diagnose, then tp-agent retry TASK --reason BRIEF", "main"),
    (8, "the newest review approves, the step is not closed", "tp-agent lane close LANE STEP", "main"),
    (9, "the step is closed, the branch is not merged into main", "merge, then tp-agent lane remove LANE", "main"),
    (10, "anything else", "unknown", "unknown"),
)


def _unknown(why):
    return {"next": "unknown: " + why, "who": "unknown", "rule": 10}


def _rule(number, values=None):
    """Row `number` of the table; TASK, REQ, LANE and STEP are replaced by the row's own values when they are known."""
    row = NEXT_TABLE[number - 1]
    text = row[2]
    for word, value in (values or {}).items():
        if value:
            text = re.sub(r"\b%s\b" % word, lambda _: str(value), text)
    return {"next": text, "who": row[3], "rule": number}


def decide(facts):
    """The next action from `facts` (the table's conditions, already read): {next, who, rule}. First match wins; evidence that
    cannot be read is never read as absence."""
    if facts["unreadable"]:
        return _unknown(facts["unreadable"])
    task = facts["task"]
    values = {"TASK": task["id"] if task else None, "LANE": facts["lane"], "STEP": facts["step"],
              "REQ": facts["requests"][0]["id"] if facts["requests"] else None}
    kinds = [r["kind"] for r in facts["requests"]]
    if "permission" in kinds:
        return _rule(1, values)
    if facts["request_problem"]:
        return _unknown("a request record of the task is unreadable, so its open requests are not known")
    if kinds:
        return _rule(2, values)
    if facts["step_closed"]:
        if facts["merged"] is None:
            return _unknown("whether the lane branch is merged into main could not be read")
        return _rule(9, values) if not facts["merged"] else _unknown("the step is closed and the branch is merged; the lane is still open (the table has no action)")
    if task is None:
        if facts["queued"]:
            return _unknown("the step's task is queued, not started")
        return _rule(3, values)
    if facts["life_problem"]:
        return _unknown("the task's lifecycle records cannot be read: " + facts["life_problem"])
    life = facts["life"]
    if life in RUNNING:
        return _rule(4, values)
    if life not in FINAL:
        return _unknown(f"the task ended {life} with no usable result")
    review = facts["review"]
    if review is None:
        return _rule(6, values)
    if review["life_problem"]:
        return _unknown("the newest review's lifecycle records cannot be read: " + review["life_problem"])
    if review["life"] in RUNNING:
        return _rule(5, values)
    if review["life"] not in lane_steps.FINISHED_REVIEW:
        return _unknown(f"the newest review ended {review['life']} with no verdict")
    verdict = review["verdict"]
    if verdict == "unparseable":
        return _unknown("the newest review's verdict could not be read")
    if review.get("incomplete"):
        return _unknown("the newest review is incomplete, so it neither approves nor rejects")
    if verdict == "REQUEST_CHANGES":
        return _unknown("the newest review asks for changes but labels none `Closes by: code`") if review.get("unusable") else _rule(7, values)
    if verdict in ("APPROVED", "APPROVED_WITH_NOTES"):
        return _rule(8, values)
    return _unknown("the newest review's verdict is not a verdict word")


def _newest(items):
    return max(items, key=lambda t: t["when"]) if items else None


class _Bad(Exception):
    """A record that exists and cannot be read as the field its reader uses: (path, why)."""


def _text(path):
    """A file's text, None when there is no file; one that exists and cannot be read as UTF-8 text raises _Bad."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise _Bad(path, f"{path.name} is unreadable ({error.__class__.__name__})")


def _present(path):
    """Does a marker file exist (as contract.task_lifecycle's Path.exists sees it: a dangling link is absent)?"""
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as error:
        raise _Bad(path, f"{path.name} cannot be looked at ({error.__class__.__name__})")
    return True


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _json_object(path):
    raw = _text(path)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        value = None
    if not isinstance(value, dict):
        raise _Bad(path, f"{path.name} is not a JSON object")
    return value


def _state_of(path):
    """state.json as contract.lifecycle reads it: state (text), updated (a time), and when present tools (a number) and
    completedTurn (a turn number). Absent: {} (a task that has just started)."""
    state = _json_object(path)
    if state is None:
        return {}
    if not isinstance(state.get("state"), str):
        raise _Bad(path, "state.json has no text state")
    if not isinstance(state.get("updated"), str) or contract._epoch(state["updated"]) is None:
        raise _Bad(path, "state.json has no readable updated")
    if "tools" in state and not _number(state["tools"]):
        raise _Bad(path, "state.json tools is not a number")
    if "completedTurn" in state and contract._turn_number(state["completedTurn"]) is None:
        raise _Bad(path, "state.json completedTurn is not a turn number")
    return state


def _events_of(path):
    """checkpoints.json's events as contract.lifecycle reads each one: an object with a text action, and when present a numeric
    tools and a turn that is null or a turn number. Absent: no events."""
    value = _json_object(path)
    if value is None:
        return []
    events = value.get("events")
    if not isinstance(events, list):
        raise _Bad(path, "checkpoints.json events is not a list")
    for n, event in enumerate(events):
        ok = isinstance(event, dict) and isinstance(event.get("action"), str) and \
            ("tools" not in event or _number(event["tools"])) and \
            (event.get("turn") is None or contract._turn_number(event["turn"]) is not None)
        if not ok:
            raise _Bad(path, f"checkpoints.json event {n} is not an event (action, tools, turn)")
    return events


def _pending(path):
    """A request file of REQUEST_NAMES: absent is no request; one that exists must be one JSON object with a text status."""
    kind, value = request_file.read_request(path)
    if kind == request_file.ABSENT:
        return False
    if kind == request_file.UNREADABLE or not isinstance(value.get("status"), str):
        raise _Bad(path, f"{path.name} is not a request record (one JSON object with a text status)")
    return value["status"] == "pending"


# Every file _life reads, by the reader that checks it; tests hold it equal to lifecycle_files.LIFECYCLE_FILES.
LIFE_READS = {"state.json": "_state_of", "checkpoints.json": "_events_of", "result.md": "_text", "completed": "_text", "exit": "_text",
              "escalated": "_present", "stopped.json": "_present", "collected.json": "_present", "pending-capability.json": "_present",
              "requests": "_read_requests", "permission-request.json": "_pending", "capability-request.json": "_pending"}


def _life(task):
    """(lifecycle word, [(path, problem)]) of a task, decided by contract.lifecycle from inputs read and checked here.

    contract.task_lifecycle (and lane_steps.life_of over it) fills in a default for a record it cannot read: {} for state.json,
    no events, an empty reply, pending for a request file. Right for its own callers, those defaults would show here as a
    definite wait, so every file of LIFECYCLE_FILES is read here instead. A file that does not exist is absent (a task that has
    just started has written none of them). One that exists must give every field lifecycle uses, or the word is None and each
    such file is a problem. Markers (escalated, stopped.json, collected.json, pending-capability.json) need only exist."""
    d = task["path"]
    bad = []

    def read(reader, name, default=None):
        try:
            return reader(d / name)
        except _Bad as error:
            bad.append((error.args[0], error.args[1]))
            return default

    state = read(_state_of, "state.json", {})
    events = read(_events_of, "checkpoints.json", [])
    reply = read(_text, "result.md") or ""
    replied_at = None
    if read(_present, "result.md"):
        try:
            replied_at = (d / "result.md").stat().st_mtime
        except OSError as error:
            bad.append((d / "result.md", f"result.md cannot be looked at ({error.__class__.__name__})"))
    completed_at = read(_text, "completed")
    if completed_at is not None:
        try:
            completed_at = float(completed_at.strip())
        except ValueError:
            completed_at = None
        if not _number(completed_at):
            bad.append((d / "completed", "completed is not a number"))
    exit_code = read(_text, "exit")
    if exit_code is not None:
        exit_code = exit_code.strip()
        if not exit_code:
            bad.append((d / "exit", "exit is empty"))
    markers = {name: read(_present, name, False) for name in ("escalated", "stopped.json", "collected.json", "pending-capability.json")}
    asking = any([read(_pending, name, False) for name in request_file.REQUEST_NAMES])
    opened, broken = _read_requests(d)
    bad += [(path, "malformed request record") for path in broken]
    if bad:
        return None, bad
    life = contract.lifecycle(state, None, events, reply.strip(), completed_at, exit_code, markers["escalated"], replied_at,
                              stopped=markers["stopped.json"], collected=markers["collected.json"],
                              asking=asking or any(r["kind"] == "capability" for r in opened),
                              capability_pending=markers["pending-capability.json"])
    return life, []


def _why(bad):
    return "; ".join(why for _, why in bad)[:160]


def _read_review(task):
    """{id, life, life_problem, verdict, incomplete, unusable} of a review task; verdict only when it has finished."""
    life, bad = _life(task)
    out = {"id": task["id"], "life": life or "unknown", "life_problem": _why(bad), "bad": bad, "verdict": None, "incomplete": None, "unusable": None}
    if bad or out["life"] not in lane_steps.FINISHED_REVIEW:
        return out
    try:
        text = review_verdict.read_review(task["path"] / "result.md")
        out["verdict"], _, out["incomplete"] = review_verdict.assess(text)
        out["unusable"] = review_verdict.request_changes_problem(text)
    except (review_verdict.ReviewMissing, ValueError):
        out["verdict"] = "unparseable"
    return out


def _read_requests(task_path):
    """(open requests [{id, kind}], unreadable record paths) of a task, read here: a record counts only when its id (the file's
    name), kind, status and opened are all readable. Open ones are oldest id first."""
    folder = task_path / "requests"
    try:
        if not os.path.lexists(folder):
            return [], []
        if folder.is_symlink() or not folder.is_dir():
            return [], [folder]
        files = sorted(folder.glob("req-*.json"))
    except OSError:
        return [], [folder]
    opened, bad = [], []
    for path in files:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            value = None
        ok = (isinstance(value, dict) and hub_records.REQUEST_ID.fullmatch(path.stem) and value.get("id") == path.stem
              and isinstance(value.get("kind"), str) and value["kind"] in KINDS
              and isinstance(value.get("status"), str) and value["status"] in hub_records.REQUEST_STATUSES and timeline.parse_time(value.get("opened")) is not None)
        if not ok:
            bad.append(path)
        elif value["status"] == "open":
            opened.append({"id": value["id"], "kind": value["kind"]})
    return opened, bad


REVIEW_OF = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")  # tp-spawn's --review-of check


def _selection_bad(task):
    """[(path, problem)] for the fields a task is chosen by (lane_steps.Session reads them with defaults: created unreadable sorts
    as the oldest, a lane_step that is not text drops the task out of its step, an unreadable review-of makes a review a writer)."""
    meta, d, bad = task["meta"], task["path"], []
    created = meta.get("created")
    try:
        ok = isinstance(created, str) and datetime.fromisoformat(created.replace("Z", "+00:00")) is not None
    except ValueError:
        ok = False
    if not ok:
        bad.append((d / "meta.json", "meta.json created is not a time"))
    if not isinstance(meta.get("lane"), str):
        bad.append((d / "meta.json", "meta.json lane is not text"))
    if not (isinstance(meta.get("lane_step"), str) and lanes.STEP_ID.fullmatch(meta["lane_step"])):
        bad.append((d / "meta.json", "meta.json lane_step is not a step id"))
    if "role" in meta and not isinstance(meta["role"], str):
        bad.append((d / "meta.json", "meta.json role is not text"))
    try:
        raw = _text(d / "review-of")
        if raw is not None and not REVIEW_OF.fullmatch(raw.strip()):
            bad.append((d / "review-of", "review-of does not name a task"))
    except _Bad as error:
        bad.append((error.args[0], error.args[1]))
    return bad


def _lane_selection_bad(session, name, ids):
    """{task id: [(path, problem)]} over every task the lane's choice reads: each whose meta.json names lane `name`, each that
    carries a lane tag that names no lane (it could be this lane's), and each review of a task in `ids` (the lane's tasks)."""
    out = {}
    for t in session.tasks:
        meta = t["meta"]
        unplaced = ("lane" in meta or "lane_step" in meta) and not isinstance(meta.get("lane"), str)
        if meta.get("lane") == name or unplaced or (t["review_of"] in ids):
            bad = _selection_bad(t)
            if bad:
                out[t["id"]] = bad
    return out


def _problem_dirs(state_dir):
    """Task directories (tasks/ and archive/) whose meta.json is missing or malformed, with the problem."""
    out = []
    for base in ("tasks", "archive"):
        try:
            for d in sorted((state_dir / base).iterdir()):
                if not d.is_dir() or d.is_symlink() or d.name.startswith("."):
                    continue
                if not (d / "meta.json").exists():
                    out.append((d, "task directory has no meta.json"))
                    continue
                try:
                    ok = isinstance(json.loads((d / "meta.json").read_text()), dict)
                except (OSError, ValueError):
                    ok = False
                if not ok:
                    out.append((d, "meta.json is malformed"))
        except OSError:
            pass
    return out


def _merged(repo, branch):
    """True when the lane branch's tip is in main's HEAD, False when not, None when git cannot say."""
    try:
        _, tip = lanes._git(["rev-parse", "--verify", "-q", f"refs/heads/{branch}^{{commit}}"], repo)
        return lane_remove._is_ancestor(repo, tip.strip(), "HEAD")
    except ValueError:
        return None


def build(tasks_dir, repo, now=None, rows=None):
    """The whole status as a dict. Never raises on bad records; a repository that cannot be read raises ValueError.
    rows: the lane rows to report (lane_commands.list_lanes's {name, state, error?}); default every lane of the repository."""
    now = time.time() if now is None else now
    problems = []

    def problem(where, why):
        """One entry per record: a record timeline (or an earlier check) already lists is not listed again."""
        if not any(p["where"] == str(where) for p in problems):
            problems.append({"where": str(where), "problem": why})
    tasks_dir = Path(tasks_dir)
    state_dir = tasks_dir.parent if tasks_dir.name == "tasks" else tasks_dir
    common, main_repo = lane_commands._main_repo(repo)
    session = None
    try:
        session = lane_steps.Session(state_dir)
    except (ValueError, OSError) as error:
        problems.append({"where": str(state_dir), "problem": f"session unreadable ({error.__class__.__name__})"})
    for d, why in _problem_dirs(state_dir):
        problems.append({"where": str(d), "problem": why})
    if not session:
        rows = []
    elif rows is None:
        rows = lane_commands.list_lanes(repo)  # no step fields: lane_steps.row_fields runs the lane gate (writes)
    chosen = []  # (lane row, record, task)
    for lane in rows:
        if lane.get("state") not in ("open", "error"):
            continue
        record = None
        if lane["state"] == "open":
            try:
                record = lanes.read_lane(common, lane["name"])
            except ValueError as error:
                lane = {"name": lane["name"], "state": "error", "error": str(error)}
        step = lane_steps.open_step(session, record) if record is not None else None
        writers = session.writers(lane["name"], step) if record is not None and step else []
        chosen.append((lane, record, _newest(writers), step))
    ids = [t["path"].name for _, _, t, _ in chosen if t is not None and t["where"] == "tasks"]
    report = timeline.build(state_dir / "tasks", ids or None, now) if ids else {"tasks": [], "problems": []}
    problems += report["problems"]
    by_task = {r["task"]: r for r in report["tasks"]}
    out = []
    for lane, record, task, step in chosen:
        if record is None:
            problems.append({"where": lane["name"], "problem": lane.get("error", "lane record unreadable")})
            out.append({"lane": lane["name"], "objective": "unknown", "step": "unknown", "age": "unknown", "task": None, "state": "unknown",
                        "review": None, "requests": [], **_unknown("the lane record is unreadable")})
            continue
        reviews = [t for t in session.tasks if task and t["review_of"] == task["id"]]
        review_task = _newest(reviews)
        review = _read_review(review_task) if review_task else None
        requests, request_problem = [], False
        if task is not None:
            requests, bad = _read_requests(task["path"])
            request_problem = bool(bad)
            for path in bad:
                problem(path, "malformed request record")
        unreadable = ""
        own = {t["id"] for t in session.tasks if t["meta"].get("lane") == record["name"]}
        selection = _lane_selection_bad(session, record["name"], own)
        for bad in selection.values():
            for where, why in bad:
                problem(where, why)
        try:
            tied = [u for u in session.unreadable_of(record) if u["step"] in (None, step)]
        except ValueError as error:
            tied = []
            unreadable = "the lane's lease cannot be read, so its unreadable tasks are not known (" + str(error).split(";")[0][:80] + ")"
        if tied:
            unreadable = "a task of this lane's step cannot be read: " + ", ".join(sorted(u["id"] for u in tied))
        elif selection:
            unreadable = "which task or review this lane is at cannot be read: " + ", ".join(
                f"{ident} ({_why(bad)})" for ident, bad in sorted(selection.items()))
        state = "unknown"
        row = by_task.get(task["path"].name) if task else None
        if row and row["intervals"]:
            last = row["intervals"][-1]
            state = {"state": last["state"], "since": timeline.iso(last["start"])}
        elif task:
            problems.append({"where": str(task["path"]), "problem": "no timeline for the task (archived or unreadable)"})
        step_closed = step is None and bool(record["closed"])
        life, bad = _life(task) if task else (None, [])
        for where, why in bad + (review["bad"] if review else []):
            problem(where, why)
        life_problem = _why(bad)
        facts = {"lane": record["name"], "step": step, "unreadable": unreadable, "requests": requests, "request_problem": request_problem, "step_closed": step_closed, "task": task,
                 "queued": bool(step and session.queued_for(record["name"], step)), "life": life, "life_problem": life_problem,
                 "review": review, "merged": _merged(main_repo, record["branch"]) if step_closed else None}
        shown_step = step or (record["closed"][-1]["step"] if record["closed"] else None)
        out.append({"lane": record["name"], "objective": record["domain"], "step": shown_step or "none",
                    "age": lane_commands.age_text(record["created"], datetime.fromtimestamp(now, timezone.utc)),
                    "created": record["created"], "task": task["id"] if task else None, "state": state,
                    "review": {"id": review["id"], "verdict": review["verdict"] or review["life"]} if review else None,
                    "requests": requests, **decide(facts)})
    out.sort(key=lambda r: (WHO_ORDER[r["who"]], r.get("created", "9")))  # owner first, then main, then worker and reviewer; oldest first
    return {"now": timeline.iso(now), "lanes": out, "problems": problems}


def render(report):
    lines = []
    for r in report["lanes"]:
        state = r["state"] if isinstance(r["state"], str) else "{} since {}".format(r["state"]["state"], r["state"]["since"])
        review = "none" if not r["review"] else "{} {}".format(r["review"]["id"], r["review"]["verdict"])
        reqs = ",".join("{}:{}".format(q["id"], q["kind"]) for q in r["requests"]) or "-"
        lines.append("{:<7} {:<16} {:<6} age {:<5} task {} [{}] review {} requests {} objective {}  -> {}".format(
            r["who"], r["lane"], r["step"], r["age"], r["task"] or "none", state, review, reqs, r["objective"], r["next"]))
    if report["problems"]:
        lines.append("problems ({}):".format(len(report["problems"])))
        lines += ["  {}: {}".format(p["where"], p["problem"]) for p in report["problems"]]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="status.py", description="Per open lane: where it waits and the next valid action.")
    parser.add_argument("tasks_dir")
    parser.add_argument("--repo", default=os.getcwd())
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--now", help="the report time (ISO-8601); default the clock")
    args = parser.parse_args(argv)
    if not Path(args.tasks_dir).is_dir():
        parser.error("not a directory: " + args.tasks_dir)
    now = timeline.parse_time(args.now) if args.now else None
    if args.now and now is None:
        parser.error("--now is not an ISO-8601 time: " + args.now)
    try:
        report = build(args.tasks_dir, args.repo, now)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(report, indent=1) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
