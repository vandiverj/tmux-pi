"""Optional autonomous protection (Phase D1): while this run's recovery is pending, no new worker starts.

`session-autonomy.json` in the run's state directory is the opt-in (absent = interactive: nothing here does anything). While it exists
and the recovery marker (lib/work_checkpoint.py: a generation the compaction hooks advance) is pending, `gate` refuses every new worker
start whatever its role or none, with or without a lane or a deliverable. The one class admitted is the review of an existing finished
task that bin/tp-review launches with `--review-of`. Nothing is dissolved by time: only `tp-agent work recovered` clears the marker,
and only `tp-agent autonomy off` (a user action by policy, not authentication) turns the protection off. It reads only this run's
state, so another run or another workspace is never affected, and it never touches a running worker.

One policy, three callers: bin/tp-spawn (every start, whoever asks), tp-agent's `dispatch` (before any lane or branch is written) and
tp-agent's `capacity` (so a queued item reports why it waits). `python3 lib/autonomy.py gate ...` is the shell entry for the first.
The gate fails closed: a record it cannot read refuses.
"""
import argparse
import json
import os
from pathlib import Path
import re
import sys

import contract
import work
import work_checkpoint

FILE = "session-autonomy.json"
REVIEW_ROLE = "code-reviewer"
IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
REFUSED = 3  # the exit status of `gate` for a refusal (1 is an error, which also refuses)
UNAVAILABLE = ("autonomy is not available under Pi: Pi has no verified compaction event, so recovery cannot be tracked; "
               "run Claude main (tp-main with TP_MAIN_HARNESS=claude) for autonomous work")
CLEAR = 'tp-agent work recovered --evidence "what you checked"'


def _clip(text, size=200):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= size else text[:size - 1] + "…"


def _read(path):
    """(value, None) for a JSON object, (None, None) when the file is absent, (None, why) when it is there but unusable."""
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return None, None
    if path.is_symlink() or not path.is_file():
        return None, "not a regular file"
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        return None, type(error).__name__
    return (value, None) if isinstance(value, dict) else (None, "not a JSON object")


def record(state_dir):
    """This run's autonomy record: None when autonomy is off; a ValueError when the file is there but unusable (the gate refuses)."""
    value, problem = _read(Path(state_dir) / FILE)
    if problem:
        raise ValueError(f"{FILE} is unreadable ({problem}); remove it with `tp-agent autonomy off` if autonomy is not wanted")
    return value if value is not None and value.get("on") is True else None


def recovery(state_dir):
    """The recovery marker of a run with autonomy on, as {pending, since, reason}: pending while a generation newer than the last
    `work recovered` exists. Generation state that is unreadable or missing counts as pending (`autonomy on` armed it, so it cannot be
    legitimately absent): not knowing is not recovered."""
    value = work_checkpoint.marker(state_dir, missing_is_pending=True)
    if not value["pending"]:
        return {"pending": False, "since": "", "reason": ""}
    return {"pending": True, "since": _clip(value["since"], 40), "reason": _clip(value["problem"] or value["reason"], 80)}


# ------------------------------------------------------------------------ the gate

def _author_tags(meta):
    values = []
    for key in ("lane", "lane_step"):
        value = meta.get(key, "")
        if not isinstance(value, str):
            return None
        values.append(value)
    return tuple(values)


def review_problem(state_dir, review_of, role="", preamble_role="", deliverable="", lane="", lane_step=""):
    """Why this start is not the review of a finished task (a sentence), or None when it is. Every condition of the design's review
    admission: reviewer preamble, no role, no deliverable, an existing task of this run with a reply and a settled state, and the
    lane tags of that task."""
    if not review_of:
        return "no --review-of"
    if preamble_role != REVIEW_ROLE:
        return f"--review-of needs --preamble-role {REVIEW_ROLE}"
    if role:
        return "a review takes no --role"
    if deliverable:
        return "a review takes no --deliverable"
    if not IDENT.fullmatch(review_of):
        return f"{review_of!r} is not a task id"
    path = Path(state_dir) / "tasks" / review_of
    meta, problem = _read(path / "meta.json")
    if meta is None:
        return f"{review_of} is not a task of this run" + (f" ({problem})" if problem else "")
    result = path / "result.md"
    try:
        has_result = result.is_file() and not result.is_symlink() and result.stat().st_size > 0
    except OSError:
        has_result = False
    if not has_result:
        return f"{review_of} has no result yet"
    try:
        life = contract.task_lifecycle(path)[0]
    except (OSError, ValueError):
        return f"the state of {review_of} cannot be read"
    if life not in ("settled", "completed"):
        return f"{review_of} is not finished (state: {life})"
    tags = _author_tags(meta)
    if tags is None:
        return f"{review_of} has an unreadable lane tag"
    if tags != (lane, lane_step):
        return (f"the review's lane ({lane or 'none'}/{lane_step or 'none'}) is not the lane of {review_of} "
                f"({tags[0] or 'none'}/{tags[1] or 'none'})")
    return None


def refusal_text(state, review_note=""):
    since = f" since {state['since']}" if state["since"] else ""
    why = f" ({state['reason']})" if state["reason"] else ""
    return (f"autonomy is on and recovery is pending{since}{why}: no new worker starts, whatever its role, until recovery is done. "
            f"Reconcile by reading records (tp-agent work show, work recover, inspect, collect, list), then clear it with: {CLEAR} "
            f"(add --accept T1,T2 for tasks that changed, or --no-checkpoint when there is no checkpoint to compare). "
            f"Only the user turns protection off: tp-agent autonomy off --reason \"...\"" + review_note)


def gate(state_dir, role="", preamble_role="", review_of="", deliverable="", lane="", lane_step=""):
    """None when this start is admitted, else the refusal (a sentence naming the cause and the command that clears it).

    Autonomy off (no file) admits everything. Otherwise a start is admitted while no recovery is pending, or when it is the launch of a
    review of an existing finished task (review_problem). Never raises: a record that cannot be read is a refusal."""
    try:
        if record(state_dir) is None:
            return None
        state = recovery(state_dir)
        if not state["pending"]:
            return None
        problem = review_problem(state_dir, review_of, role, preamble_role, deliverable, lane, lane_step)
        if problem is None:
            return None
        return refusal_text(state, f" [not admitted as a review: {problem}]" if review_of else "")
    except (ValueError, OSError) as error:
        return f"autonomy state cannot be read, so no worker starts: {error}"


# --------------------------------------------------------------- on / off / status

def env_problem(env):
    """None when `env` is a verified Claude orchestrator main: Claude harness, orchestrator mode, and not a worker."""
    if env.get("TP_MAIN_HARNESS") == "claude" and env.get("TP_MODE") == "orchestrator" and "TP_TASK_DIR" not in env:
        return None
    return (f"{UNAVAILABLE} (seen: TP_MAIN_HARNESS={env.get('TP_MAIN_HARNESS') or 'unset'}, TP_MODE={env.get('TP_MODE') or 'unset'}, "
            f"{'a worker: TP_TASK_DIR is set' if 'TP_TASK_DIR' in env else 'TP_TASK_DIR unset'})")


def audit(workspace, text):
    """One dated line in the workspace's Sessions section. The change it records is already made: a failure is returned as a
    sentence, never raised."""
    try:
        with work.locked():
            doc = work.load("workspace", workspace, include_archived=False)
            doc.body = work.append_line(doc.body, "Sessions", f"- {work.now()[:10]} {text}")
            doc.save()
    except (OSError, ValueError) as error:
        return f"audit line not written to workspace {workspace}: {error}"
    return None


def enable(state_dir, env, workspace, reason=""):
    """Turn autonomy on for this run and arm recovery-pending (set-only: an already pending marker keeps its own time and reason).
    Every check comes before the first write, so a refusal writes nothing."""
    problem = env_problem(env)
    if problem:
        raise ValueError(problem)
    if state_dir is None or not Path(state_dir).is_dir():
        raise ValueError("no tmux-pi state directory; start a workspace with tp, or set TP_STATE_DIR")
    work.load("workspace", workspace, include_archived=False)  # raises when there is no such workspace
    # One critical section under the lock `work recovered` holds for its validate-and-clear, so no clear lands between the arm and
    # the record. Inside it the arm comes first: the gate takes no lock, and a record written before the arm would admit a start in
    # between. Armed and then refused (the record write fails), the generation is only advisory while autonomy is off.
    with work_checkpoint.state_lock(state_dir):
        current = record(state_dir)
        value = {"on": True, "since": (current or {}).get("since") or work.now(), "harness": "claude", "workspace": workspace,
                 "reason": _clip(reason, 200)}
        work_checkpoint.arm(state_dir, "autonomy on")
        work.atomic_write(Path(state_dir) / FILE, json.dumps(value, indent=2) + "\n")
    return value


def disable(state_dir, reason=""):
    """Turn autonomy off (the user's escape): the file goes, running workers are untouched, the recovery marker is left as it is."""
    try:
        current = record(state_dir)
    except ValueError:
        current = {}
    path = Path(state_dir) / FILE
    existed = path.exists() or path.is_symlink()
    if existed:
        path.unlink()
    return {"autonomy": "off", "was_on": existed, "workspace": (current or {}).get("workspace", ""), "reason": _clip(reason, 200)}


def status(state_dir):
    """What `autonomy status` prints: on or off, and while on the recovery state and what is blocked since when."""
    try:
        current = record(state_dir)
    except ValueError as error:
        return {"autonomy": "unreadable", "problem": str(error), "blocked": {"by": "unreadable autonomy record", "clear": "tp-agent autonomy off"}}
    if current is None:
        return {"autonomy": "off"}
    state = recovery(state_dir)
    out = {"autonomy": "on", "harness": current.get("harness", ""), "workspace": current.get("workspace", ""), "since": current.get("since", ""),
           "reason": current.get("reason", ""), "recovery": state, "blocked": None}
    if state["pending"]:
        out["blocked"] = {"since": state["since"], "by": "recovery", "clear": CLEAR}
    return out


def index_entry(state_dir):
    """The `autonomy` entry of the session index's work block: None when autonomy is off (the index is then what it was), else the
    state and one line for the injection. It says what is blocked and since when, so a user checking in sees it."""
    value = status(state_dir)
    if value["autonomy"] == "off":
        return None
    blocked = value.get("blocked")
    if value["autonomy"] == "on" and not blocked:
        line = f"autonomy on (workspace {_clip(value['workspace'], 60)}): recovery clear, dispatch admitted"
    else:
        since = f" since {_clip(blocked.get('since'), 40)}" if blocked.get("since") else ""
        line = f"autonomy on: dispatch blocked{since} by {blocked['by']}; clear it with `{blocked['clear']}`"
    return {"on": value["autonomy"] == "on", "workspace": value.get("workspace", ""), "blocked_since": (blocked or {}).get("since") or None,
            "blocked": blocked is not None, "line": line}


# ------------------------------------------------------------------------- shell

def main(argv=None):
    parser = argparse.ArgumentParser(description="the autonomy gate, for bin/tp-spawn")
    parser.add_argument("command", choices=["gate"])
    parser.add_argument("--state-dir", required=True)
    for name in ("role", "preamble-role", "review-of", "deliverable", "lane", "lane-step"):
        parser.add_argument(f"--{name}", default="")
    args = parser.parse_args(argv)
    refusal = gate(args.state_dir, args.role, args.preamble_role, args.review_of, args.deliverable, args.lane, args.lane_step)
    if refusal:
        print(f"tp-spawn: refused: {refusal}", file=sys.stderr)
        return REFUSED
    return 0


if __name__ == "__main__":
    sys.exit(main())
