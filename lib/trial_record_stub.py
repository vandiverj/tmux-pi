"""STUB of lane L2's lib/trial_record.py (design §4), so lane L3 (trial shadow) can be built and tested before L2 merges.

This is not the real record. It has the real module's call shape, `build` and `append` and `trial_dir`, and returns only what
the shadow run itself knows from a task directory: ids, model, effort, state, the first review's stated verdict, the diff
counts, the scan flags the shadow already has. lib/trial_shadow.py imports `trial_record` and falls back to this file only
when that module does not exist (`load_record_module`). Once L2 is merged this file and its test can be deleted.

Signature copied from L2's trial_record at 790c52c:
    build(task_dir, arm, mode, pair_id, *, state_dir=None, role=None, diff_files=None, diff_lines=None,
          commit_messages=None, brief_drift=None, now=None) -> dict
    append(project, record)
    trial_dir(project)
`gate_runs` is not produced here: L3 treats it as opaque and never reads or builds it."""

import datetime
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import trial_session  # noqa: E402  (read_plain: the one way a file the worker can write is read)

RECORDS = "records.jsonl"
SCHEMA = 0  # 0 marks a stub record; L2's is 1
ARMS = ("qwen", "normal")
MODES = ("shadow", "replay")


class RecordError(ValueError):
    """The record cannot be built from what was given."""


def trial_dir(project):
    return Path(project) / ".tmux-pi" / "trial"


def _json(path):
    try:  # the task's files are the worker's to write: a FIFO, a link, a directory or a huge file reads as no file
        value = json.loads(trial_session.read_plain(path).decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def _first_review(meta, state_dir):
    ids = meta.get("reviews")
    if ids is None:
        ids = [meta["review"]] if meta.get("review") else []
    ids = [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else []
    if not ids:
        return 0, None, None
    reviewer = _json(Path(state_dir) / "tasks" / ids[0] / "meta.json")
    try:
        text = trial_session.read_text(Path(state_dir) / "tasks" / ids[0] / "result.md")
    except OSError:
        text = ""
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    verdict = first if first in ("APPROVED", "APPROVED_WITH_NOTES", "REQUEST_CHANGES") else None
    return len(ids), verdict, reviewer.get("model") if isinstance(reviewer.get("model"), str) else None


def build(task_dir, arm, mode, pair_id, *, state_dir=None, role=None, diff_files=None, diff_lines=None,
          commit_messages=None, brief_drift=None, now=None):
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
    rounds, verdict, reviewer_model = _first_review(meta, state_dir)
    created = now or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schema": SCHEMA, "stub": True, "created": created, "pair_id": pair_id.strip(), "arm": arm, "mode": mode,
        "task_id": meta.get("id"), "role": role or meta.get("role"),
        "diff": {"files": diff_files, "lines": diff_lines} if diff_files is not None or diff_lines is not None else None,
        "model": meta.get("model"), "effort": meta.get("thinking"),
        "first_review_verdict": verdict, "review_rounds": rounds, "reviewer_model": reviewer_model,
        "commit_messages": len(commit_messages) if commit_messages is not None else None,
        "base_commit": dispatch.get("base_commit"),
        "brief_drift": brief_drift if mode == "replay" else None,
    }


def append(project, record):
    """Append one JSON line to <project>/.tmux-pi/trial/records.jsonl (a stub: no lock; L2's append uses the shared eval-log lock)."""
    directory = trial_dir(project)
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(directory / RECORDS), os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
