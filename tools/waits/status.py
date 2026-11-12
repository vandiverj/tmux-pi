#!/usr/bin/env python3
"""Where work waits, per open lane: the task, its state, its review, its open requests and the next valid action (HB6c).

    python3 tools/waits/status.py SESSION_TASKS_DIR [--repo R] [--json] [--now ISO]

One row per open lane of the repository (the lanes `tp-agent lanes` lists: lane_commands.list_lanes, with lane_steps.row_fields
for the open step). SESSION_TASKS_DIR is a session's tasks directory (or the session directory that holds tasks/ and archive/).
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


def _rule(number):
    row = NEXT_TABLE[number - 1]
    return {"next": row[2], "who": row[3], "rule": number}


def decide(facts):
    """The next action from `facts` (the table's conditions, already read): {next, who, rule}. First match wins."""
    kinds = [r["kind"] for r in facts["requests"]]
    if "permission" in kinds:
        return _rule(1)
    if kinds:
        return _rule(2)
    if facts["request_problem"]:
        return _unknown("a request record of the task is unreadable, so its open requests are not known")
    if facts["step_closed"]:
        if facts["merged"] is None:
            return _unknown("whether the lane branch is merged into main could not be read")
        return _rule(9) if not facts["merged"] else _unknown("the step is closed and the branch is merged; the lane is still open (the table has no action)")
    if facts["task"] is None:
        if facts["queued"]:
            return _unknown("the step's task is queued, not started")
        return _rule(3)
    life = facts["life"]
    if life in RUNNING:
        return _rule(4)
    if life not in FINAL:
        return _unknown(f"the task ended {life} with no usable result")
    review = facts["review"]
    if review is None:
        return _rule(6)
    if review["life"] in RUNNING:
        return _rule(5)
    if review["life"] not in lane_steps.FINISHED_REVIEW:
        return _unknown(f"the newest review ended {review['life']} with no verdict")
    verdict = review["verdict"]
    if verdict == "unparseable":
        return _unknown("the newest review's verdict could not be read")
    if review.get("incomplete"):
        return _unknown("the newest review is incomplete, so it neither approves nor rejects")
    if verdict == "REQUEST_CHANGES":
        return _unknown("the newest review asks for changes but labels none `Closes by: code`") if review.get("unusable") else _rule(7)
    if verdict in ("APPROVED", "APPROVED_WITH_NOTES"):
        return _rule(8)
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


def _request_files(task_path):
    folder = task_path / "requests"
    try:
        return sorted(folder.glob("req-*.json")) if folder.is_dir() and not folder.is_symlink() else []
    except OSError:
        return []


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
    for base in ("tasks",):
        try:
            for d in sorted((state_dir / base).iterdir()):
                if d.is_dir() and not d.is_symlink() and not d.name.startswith(".") and not (d / "meta.json").exists():
                    problems.append({"where": str(d), "problem": "task directory has no meta.json"})
        except OSError:
            pass
    rows = lane_commands.list_lanes(repo, steps=session and (lambda record: lane_steps.row_fields(session, record))) if session else []
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
        step = lane.get("open_step")
        writers = session.writers(lane["name"], step) if record is not None and step else []
        chosen.append((lane, record, _newest(writers)))
    ids = [t["path"].name for _, _, t in chosen if t is not None and t["where"] == "tasks"]
    report = timeline.build(state_dir / "tasks", ids or None, now) if ids else {"tasks": [], "problems": []}
    problems += report["problems"]
    by_task = {r["task"]: r for r in report["tasks"]}
    out = []
    for lane, record, task in chosen:
        if record is None:
            problems.append({"where": lane["name"], "problem": lane.get("error", "lane record unreadable")})
            out.append({"lane": lane["name"], "objective": "unknown", "step": "unknown", "age": "unknown", "task": None, "state": "unknown",
                        "review": None, "requests": [], **_unknown("the lane record is unreadable")})
            continue
        step = lane.get("open_step")
        reviews = [t for t in session.tasks if task and t["review_of"] == task["id"]]
        review_task = _newest(reviews)
        review = _read_review(review_task) if review_task else None
        requests, request_problem = [], False
        if task is not None:
            requests = [{"id": r["id"], "kind": r.get("kind")} for r in hub_records.open_requests(task["path"])]
            readable = {r["id"] for r in hub_records.requests(task["path"])}
            for path in _request_files(task["path"]):
                if path.stem not in readable:
                    request_problem = True
                    problems.append({"where": str(path), "problem": "malformed request record"})
        state = "unknown"
        row = by_task.get(task["path"].name) if task else None
        if row and row["intervals"]:
            last = row["intervals"][-1]
            state = {"state": last["state"], "since": timeline.iso(last["start"])}
        elif task:
            problems.append({"where": str(task["path"]), "problem": "no timeline for the task (archived or unreadable)"})
        step_closed = step is None and bool(record["closed"])
        facts = {"requests": requests, "request_problem": request_problem, "step_closed": step_closed, "task": task,
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
        lines.append("{:<7} {:<16} {:<6} age {:<5} task {} [{}] review {} requests {}  -> {}".format(
            r["who"], r["lane"], r["step"], r["age"], r["task"] or "none", state, review, reqs, r["next"]))
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
