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
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "lib"))
import hub_records  # noqa: E402
import lane_commands  # noqa: E402
import lane_remove  # noqa: E402
import lane_steps  # noqa: E402
import lanes  # noqa: E402
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
    life = facts["life"]
    if life in RUNNING:
        return _rule(4, values)
    if life not in FINAL:
        return _unknown(f"the task ended {life} with no usable result")
    review = facts["review"]
    if review is None:
        return _rule(6, values)
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


def _read_review(task):
    """{id, life, verdict, incomplete, unusable} of a review task; verdict only when it has finished."""
    out = {"id": task["id"], "life": lane_steps.life_of(task), "verdict": None, "incomplete": None, "unusable": None}
    if out["life"] not in lane_steps.FINISHED_REVIEW:
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
        files = sorted(folder.glob("req-*.json")) if folder.is_dir() and not folder.is_symlink() else []
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
              and value.get("status") in hub_records.REQUEST_STATUSES and timeline.parse_time(value.get("opened")) is not None)
        if not ok:
            bad.append(path)
        elif value["status"] == "open":
            opened.append({"id": value["id"], "kind": value["kind"]})
    return opened, bad


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


def build(tasks_dir, repo, now=None):
    """The whole status as a dict. Never raises on bad records; a repository that cannot be read raises ValueError."""
    now = time.time() if now is None else now
    problems = []
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
    rows = lane_commands.list_lanes(repo) if session else []  # no step fields: lane_steps.row_fields runs the lane gate (writes)
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
            problems += [{"where": str(path), "problem": "malformed request record"} for path in bad]
        unreadable = ""
        try:
            tied = [u for u in session.unreadable_of(record) if u["step"] in (None, step)]
        except ValueError as error:
            tied = []
            unreadable = "the lane's lease cannot be read, so its unreadable tasks are not known (" + str(error).split(";")[0][:80] + ")"
        if tied:
            unreadable = "a task of this lane's step cannot be read: " + ", ".join(sorted(u["id"] for u in tied))
        state = "unknown"
        row = by_task.get(task["path"].name) if task else None
        if row and row["intervals"]:
            last = row["intervals"][-1]
            state = {"state": last["state"], "since": timeline.iso(last["start"])}
        elif task:
            problems.append({"where": str(task["path"]), "problem": "no timeline for the task (archived or unreadable)"})
        step_closed = step is None and bool(record["closed"])
        facts = {"lane": record["name"], "step": step, "unreadable": unreadable, "requests": requests, "request_problem": request_problem, "step_closed": step_closed, "task": task,
                 "queued": bool(step and session.queued_for(record["name"], step)), "life": lane_steps.life_of(task) if task else None,
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
