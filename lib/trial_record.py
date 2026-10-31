"""One comparable record per Qwen-trial attempt, appended to <project>/.tmux-pi/trial/records.jsonl.

`build` reads a finished task dir (meta.json, state.json, dispatch.json, request.md, the first review's result.md, the
transcript, docs-calls.jsonl) and returns a JSON-safe dict. It keeps counts, ids and verdict labels, never transcript or
command text. Both arms of a pair (`qwen`, `normal`) get a record joined by `pair_id`.

The first review is `meta.reviews[0]`, not the last: a later round says what the author fixed, not what the first attempt was.
`first_review_outcome` is `approved` (a complete APPROVED or APPROVED_WITH_NOTES), `request_changes`, or `inconclusive` (an
incomplete, unparsed or label-only review, which is neither an approval nor a rejection), and None when there is no first review.
"""

import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_verdict  # noqa: E402
import routing_evals  # noqa: E402
import trial_scan  # noqa: E402

RECORDS = "records.jsonl"
SCHEMA = 1
ARMS = ("qwen", "normal")
MODES = ("shadow", "replay")
SIZES = ("small", "medium", "large")
SMALL = (2, 50)     # at most this many files and changed lines
MEDIUM = (5, 200)
MAX_DOCS_CALLS = 50  # per-call rows kept in a record; the count is always the full count
MAX_DOCS_LINE = 64 * 1024
OUTCOMES = ("approved", "request_changes", "inconclusive")


class RecordError(ValueError):
    """The record cannot be built from what was given."""


def trial_dir(project):
    return Path(project) / ".tmux-pi" / "trial"


def size_bucket(files, lines):
    """small / medium / large from the final diff's file and changed-line counts; None when either is unknown."""
    if not isinstance(files, int) or not isinstance(lines, int) or isinstance(files, bool) or isinstance(lines, bool):
        return None
    if files < 0 or lines < 0:
        return None
    if files <= SMALL[0] and lines <= SMALL[1]:
        return "small"
    if files <= MEDIUM[0] and lines <= MEDIUM[1]:
        return "medium"
    return "large"


def _json(path):
    return routing_evals._read_json(path) or {}


def _review_ids(meta):
    ids = meta.get("reviews")
    if ids is None:
        ids = [meta["review"]] if meta.get("review") else []
    return [i for i in ids if isinstance(i, str) and routing_evals.TASK_ID.match(i)] if isinstance(ids, list) else []


def first_review(meta, state_dir):
    """(review count, first review summary or None). The summary has the verdict label, the outcome, the families and
    `Closes by:` labels, and the reviewer's model; fields are None when the first review's task or result is not on disk."""
    ids = _review_ids(meta)
    if not ids:
        return 0, None
    out = {"id": ids[0], "verdict": None, "outcome": None, "families": [], "closes_by": [], "reviewer_model": None}
    reviewer_dir = Path(state_dir) / "tasks" / ids[0]
    if not (reviewer_dir / "meta.json").is_file():
        reviewer_dir = Path(state_dir) / "archive" / ids[0]
    reviewer_meta = _json(reviewer_dir / "meta.json")
    if isinstance(reviewer_meta.get("model"), str):
        out["reviewer_model"] = reviewer_meta["model"]
    elif isinstance(meta.get("reviewer"), dict) and isinstance(meta["reviewer"].get("model"), str):
        out["reviewer_model"] = meta["reviewer"]["model"]  # the latest reviewer: all that is left when the first is gone
    try:
        text = review_verdict.read_review(reviewer_dir / "result.md")
    except review_verdict.ReviewMissing:
        return len(ids), out
    except ValueError as exc:
        out.update({"verdict": "unparsed", "outcome": "inconclusive", "reason": str(exc)[:200]})
        return len(ids), out
    try:
        stated, _, incomplete = review_verdict.assess(text)
    except ValueError as exc:
        out.update({"verdict": "unparsed", "outcome": "inconclusive", "reason": str(exc)[:200]})
        return len(ids), out
    out["families"] = review_verdict.families(text)
    out["closes_by"] = review_verdict.closes_by(text)
    if incomplete:
        out.update({"verdict": "incomplete", "stated_verdict": stated, "outcome": "inconclusive"})
    elif review_verdict.request_changes_problem(text):
        out.update({"verdict": "UNUSABLE", "stated_verdict": stated, "outcome": "inconclusive"})
    else:
        out["verdict"] = stated
        out["outcome"] = "request_changes" if stated == "REQUEST_CHANGES" else "approved"
    return len(ids), out


def read_docs_calls(path):
    """The calls an L5 docs tool logged in `docs-calls.jsonl`: {"count", "hits", "bytes", "bad_lines", "calls"}.

    A line is a JSON object with `tool`, `args` (an object: `package` or `lib`, and `symbol` or `query`), `hit` (a bool, or
    `result` = "hit"/"miss") and `bytes`. Extra keys are ignored and a missing key becomes None. No file means the tool was
    off or never called: count 0, not an error. A torn or non-object line counts as bad and is not a call."""
    out = {"count": 0, "hits": 0, "bytes": 0, "bad_lines": 0, "calls": []}
    path = Path(path)
    try:
        if path.is_symlink() or not path.is_file():
            return out
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        out["bad_lines"] = 1
        return out
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line) if len(line) <= MAX_DOCS_LINE else None
        except ValueError:
            row = None
        if not isinstance(row, dict):
            out["bad_lines"] += 1
            continue
        args = row.get("args") if isinstance(row.get("args"), dict) else {}
        hit = row.get("hit")
        if not isinstance(hit, bool):
            hit = {"hit": True, "miss": False}.get(row.get("result")) if isinstance(row.get("result"), str) else None
        size = row.get("bytes") if isinstance(row.get("bytes"), int) and not isinstance(row.get("bytes"), bool) else None
        call = {"tool": row.get("tool") if isinstance(row.get("tool"), str) else None,
                "target": _short(args.get("package") or args.get("lib")),
                "symbol": _short(args.get("symbol") or args.get("query")),
                "hit": hit, "bytes": size}
        out["count"] += 1
        out["hits"] += 1 if hit else 0
        out["bytes"] += size or 0
        if len(out["calls"]) < MAX_DOCS_CALLS:
            out["calls"].append(call)
    return out


def _short(value, limit=120):
    return value[:limit] if isinstance(value, str) else None


def _count(path):
    try:
        return path.stat().st_size if path.is_file() and not path.is_symlink() else None
    except OSError:
        return None


def _slips(scan, extra):
    """{"complete", "total", "by_rule"}. `complete` is False when the transcript could not be scanned (a pi transcript until
    its schema is pinned, a missing file) or was scanned with lines skipped: the total is then a lower bound (what the lines
    read, the commit messages and the replies showed), and the report leaves it out of medians."""
    by_rule = dict(scan["rule_slips"]) if scan.get("rule_slips") is not None else {}
    for rule in extra:
        by_rule[rule] = by_rule.get(rule, 0) + 1
    return {"complete": bool(scan.get("complete")), "total": sum(by_rule.values()), "by_rule": dict(sorted(by_rule.items()))}


def _gates(scan):
    """None when there is no transcript scan; else {"complete", "total", "uncertain", "denied", "unattributed", "parse_failed", "runs"}. `total`
    counts gate runs known to have happened (not denied, not behind an unresolved `&&` / `||`); `unattributed` is how many of
    those have no exit status, because only the last command a call ran can claim the call's status. A scan with skipped
    lines is incomplete, so its total is a lower bound and the report leaves it out. `parse_failed` counts Bash calls the
    scanner could not read (an open quote, group or heredoc); it is a lower bound too, and the report says how many."""
    if scan.get("gate_runs") is None:
        return None
    return {"complete": bool(scan.get("complete")), **trial_scan.gate_totals(scan["gate_runs"]), "runs": scan["gate_runs"]}


def build(task_dir, arm, mode, pair_id, *, state_dir=None, role=None, diff_files=None, diff_lines=None,
          commit_messages=None, brief_drift=None, now=None):
    """The record of one attempt. `task_dir` is <state dir>/tasks/<id> (or archive/<id>).

    `commit_messages` are the bodies of `git log <base>..HEAD`, read before the shadow worktree is reset; they feed the
    commit and think-leak rules. `diff_files`/`diff_lines` come from the final diff. `brief_drift` is replay-only."""
    if arm not in ARMS:
        raise RecordError("arm must be one of {}: {!r}".format(", ".join(ARMS), arm))
    if mode not in MODES:
        raise RecordError("mode must be one of {}: {!r}".format(", ".join(MODES), mode))
    if not isinstance(pair_id, str) or not pair_id.strip():
        raise RecordError("pair_id is required")
    task_dir = Path(task_dir)
    if not (task_dir / "meta.json").is_file():
        raise RecordError("not a task dir (no meta.json): {}".format(task_dir))
    state_dir = Path(state_dir) if state_dir else task_dir.parent.parent
    meta, dispatch = _json(task_dir / "meta.json"), _json(task_dir / "dispatch.json")
    dirs_by_id = {d.name: d for d in routing_evals._task_dirs(state_dir)}
    try:
        summary = routing_evals.task_summary(task_dir, dirs_by_id)
    except (KeyError, TypeError) as exc:  # task_summary reads worker, model and thinking from a recorded task
        raise RecordError("task record {} is incomplete: {!r}".format(task_dir, exc)) from exc
    rounds, first = first_review(meta, state_dir)
    scan = trial_scan.scan_task(task_dir)
    leaks = []
    for name in ("result.md", "report.md"):
        try:
            text = (task_dir / name).read_text(encoding="utf-8", errors="replace") if (task_dir / name).is_file() else ""
        except OSError:
            text = ""
        if trial_scan.think_leak(text):
            leaks.append("think_leak")
    extra = trial_scan.commit_message_slips(commit_messages) if commit_messages is not None else []
    context = dispatch.get("context") if isinstance(dispatch.get("context"), list) else None
    brief = _count(task_dir / "request.md")
    created = now or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schema": SCHEMA,
        "created": created,
        "pair_id": pair_id.strip(),
        "arm": arm,
        "mode": mode,
        "task_id": summary["task_id"],
        "role": role or (meta.get("role") if isinstance(meta.get("role"), str) else None),
        "size": size_bucket(diff_files, diff_lines),
        "diff": {"files": diff_files, "lines": diff_lines} if diff_files is not None or diff_lines is not None else None,
        "model": meta.get("model") if isinstance(meta.get("model"), str) else None,
        "effort": meta.get("thinking") if isinstance(meta.get("thinking"), str) else None,
        "state": summary["state"],
        "exit": summary["exit"],
        "first_review_verdict": first["verdict"] if first else None,
        "first_review_outcome": first["outcome"] if first else None,
        "review_rounds": rounds,
        "reviewer_model": first["reviewer_model"] if first else None,
        "finding_categories": first["families"] if first else [],
        "finding_closes_by": first["closes_by"] if first else [],
        "transcript": {"harness": scan["harness"], "status": scan["status"], "commands": scan["commands"],
                       "bad_lines": scan["bad_lines"]},
        "rule_slips": _slips(scan, extra + leaks),
        "gate_runs": _gates(scan),
        "tokens": summary["tokens"],
        "wall_time_s": summary["duration_seconds"],
        "turns": summary["turns"],
        "tool_errors": summary["tool_errors"],
        "brief_size_bytes": brief,
        "context_files": len(context) if context is not None else None,
        "docs_calls": read_docs_calls(task_dir / "docs-calls.jsonl"),
        "base_commit": dispatch.get("base_commit") if isinstance(dispatch.get("base_commit"), str) else None,
        "brief_drift": brief_drift if mode == "replay" else None,
    }


def append(project, record):
    """Append one record under the shared eval-log lock (routing_evals.append_jsonl)."""
    routing_evals.append_jsonl(trial_dir(project), RECORDS, record)


def read_records(path):
    """(records, malformed count) from a records.jsonl; a missing file is empty. Rows that are not objects are malformed."""
    rows, bad = routing_evals.read_jsonl(path)
    good = [r for r in rows if isinstance(r, dict)]
    return good, bad + (len(rows) - len(good))
