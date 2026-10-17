"""Checkpoints and recovery (Phase C): a validated point-in-time copy of a workspace's judgement state, and what to read after a
compaction or a restart.

A checkpoint is JSON in `<store>/checkpoints/<workspace-id>/<UTC date>-<UTC time>-<seq>.json`, written atomically (temp file, fsync, rename, fsync of
the directory) and then read back and checked against its own `sha256`: it is reported as written only if that check passes. Its job
is to let `work recover` print the judgement state when the workspace file is missing or corrupt, so the state is COPIED whole by
deterministic parsing, no model call: the objective head, every `Decisions` line, every `Open decisions` entry with all its fields,
every blocker, next action and source. The one limit is the byte bound `checkpoint_max_bytes`; items go in whole or not at all, what is
left out is counted in `truncated` (and `counts` still says what the workspace held), and such a checkpoint is labelled
`truncated: workspace file is the authority` and never called a complete copy. Execution state is not checkpointed as truth: `tasks`
is a snapshot kept only to diff against the live task records.

The recovery-pending marker (the run's state directory) is a monotonic generation: `compacted`/`arm` add one, lock-free, under
`session-recovery/`, and `recovered` records in `session-recovery.json` the generation it validated; pending = newer generations
exist (see "the marker" below). Arming does not depend on a checkpoint having been written.

Everything here that reads (status, latest, recover) creates and changes nothing. The writers are called by a command that already holds
the work store lock (work.locked is not re-entrant), except `compacted`, which needs no work store at all.
"""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from datetime import datetime, timezone

import safe_write
import work
from work import WorkError

SCHEMA = "tp-work-checkpoint:v1"
TRIGGERS = ("precompact", "manual", "session-end", "work-write")
OBJECTIVE_CHARS = 600
SUMMARY_CHARS = 1500
STALE_SECONDS = 600  # a work mutation writes a fresh checkpoint when the newest valid one is older than this
SECTIONS = ("decisions", "open_decisions", "blockers", "next_actions", "sources")
SECTION_TITLES = {"decisions": "Decisions", "blockers": "Blockers / questions", "next_actions": "Next actions", "sources": "Links"}
# The name is digits and hyphens only (`20261017-015054-0004.json`): the output boundary withholds a path segment that mixes
# letters and digits (as in `...T015054Z`), and the path of a written checkpoint must reach the user whole.
NAME_RE = re.compile(r"^(\d{8}-\d{6})-(\d{4,})\.json$")
RECOVERY_FILE = "session-recovery.json"
MARKER_REASON_CHARS = 200
TRUNCATED_LABEL = "truncated: workspace file is the authority"
DECISION_KEYS = ("id", "priority", "status", "blocking", "question", "background", "options", "recommended", "affects", "resolution",
                 "by", "at", "quote", "withdrawn", "extra")


def _utcnow():
    return datetime.now(timezone.utc)


def _parse(value):
    return work._parse_when(value)


def age_text(then, now=None):
    """A coarse age ('<1 min', '41 min', '3h', '2d'), so two reads in the same minute give the same words."""
    when = _parse(then)
    if when is None:
        return "an unknown time"
    seconds = max(0, int(((now or _utcnow()) - when).total_seconds()))
    if seconds < 60:
        return "<1 min"
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _limit():
    value = work.setting("checkpoint_max_bytes")
    return value if value > 0 else work.DEFAULTS["checkpoint_max_bytes"]


def _keep():
    value = work.setting("checkpoints_keep")
    return value if value > 0 else work.DEFAULTS["checkpoints_keep"]


# ------------------------------------------------------------------- the copy

def _lines(text):
    return [line.rstrip("\r") for line in (text or "").split("\n") if line.strip()]


def _entry(entry):
    value = {key: entry.get(key, "") for key in DECISION_KEYS if key != "extra"}
    value["blocking"] = bool(entry.get("blocking"))
    value["affects"] = list(entry.get("affects") or [])
    value["extra"] = "\n".join(entry.get("_extra") or [])
    return value


def judgement_state(doc):
    """The five judgement sections of a workspace, copied as they are by parsing alone (nothing is summarised or shortened)."""
    return {
        "objective": (work.get_section(doc.body, "Objective") or "").strip()[:OBJECTIVE_CHARS],
        "decisions": _lines(work.get_section(doc.body, "Decisions")),
        "open_decisions": [_entry(e) for e in work.decisions_of(doc)],
        "blockers": _lines(work.get_section(doc.body, "Blockers / questions")),
        "next_actions": _lines(work.get_section(doc.body, "Next actions")),
        "sources": _lines(work.get_section(doc.body, "Links")),
    }


def _canonical(body):
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def seal(body):
    return {**body, "sha256": hashlib.sha256(_canonical(body)).hexdigest()}


def _dump(sealed):
    return json.dumps(sealed, indent=2, ensure_ascii=False) + "\n"


def _size(body):
    return len(_dump(seal(body)).encode("utf-8"))


def _fit(body, limit):
    """`body` cut to `limit` bytes of file, whole items only, oldest first, and every cut counted in `truncated.sections`. Every
    variable-length part is covered: the task snapshot (oldest rows first), the agent summary (whole), then the reverse of the fill
    order (open decisions, decisions, blockers, next actions, sources, objective head): the objective head, sources, next actions,
    blockers (oldest first, so the newest are kept), decisions (oldest first, so the newest are kept), then open decisions (answered or withdrawn before open
    ones, last first). One whole item is cut at a time and the real size of the file is measured after each cut, so it stops as soon
    as it fits and never cuts more than that needs. A limit below the smallest possible checkpoint is an error: nothing is written that breaks the bound."""
    if _size(body) <= limit:
        return body
    state = {key: (list(value) if isinstance(value, list) else value) for key, value in body["state"].items()}
    tasks, summary, dropped = list(body["tasks"]), dict(body["summary"]), {}

    def pick():
        """(section, index) of the next item to cut, or None when nothing is left to cut."""
        if tasks:
            return "tasks", 0
        if summary["text"]:
            return "summary", None
        if state["objective"]:
            return "objective", None
        for key in ("sources", "next_actions", "blockers"):
            if state[key]:
                return key, 0
        if state["decisions"]:
            return "decisions", 0
        entries = state["open_decisions"]
        if entries:
            closed = [i for i, e in enumerate(entries) if e.get("status") != "open"]
            return "open_decisions", (closed or [len(entries) - 1])[-1]
        return None

    def cut(section, index):
        if section == "summary":
            summary.update(authored_by=None, text="")
            summary.pop("carried_from", None)
        elif section == "objective":
            state["objective"] = ""
        elif section == "tasks":
            tasks.pop(index)
        else:
            state[section].pop(index)
        dropped[section] = dropped.get(section, 0) + 1

    def candidate():
        return {**body, "state": state, "tasks": tasks, "summary": dict(summary),
                "truncated": {"sections": dict(dropped), "bytes_limit": limit}}

    while _size(candidate()) > limit:
        target = pick()
        if target is None:
            break
        cut(*target)
    result = candidate()
    if _size(result) > limit:
        raise WorkError(f"checkpoint_max_bytes ({limit}) is smaller than the smallest checkpoint ({_size(result)} bytes); "
                        "raise it in config/work.json")
    return result


def snapshot(doc, run="", trigger="manual", tasks=(), summary=None, limit=None, carried=None):
    """The checkpoint body for a loaded workspace (not yet sealed), cut to the byte bound. `carried` ({text, seq, at}) is the agent
    summary of an earlier checkpoint, used only when this one brings none of its own, and marked as carried."""
    state = judgement_state(doc)
    note = {"authored_by": "agent" if summary else None, "text": (summary or "")[:SUMMARY_CHARS]}
    if not summary and carried:
        note = {"authored_by": "agent", "text": carried["text"][:SUMMARY_CHARS], "carried_from": {"seq": carried["seq"], "at": carried["at"]}}
    try:
        digest = hashlib.sha256(Path(doc.path).read_bytes()).hexdigest()
    except OSError:
        digest = ""
    body = {
        "schema": SCHEMA, "workspace": doc.id, "run": run, "at": _utcnow().strftime(work.STAMP), "trigger": trigger,
        "state": state, "counts": {key: len(state[key]) for key in SECTIONS}, "truncated": False,
        "workspace_updated": work.updated_iso(doc), "workspace_sha256": digest,
        "tasks": [{"id": str(t.get("id")), "status": t.get("status"), "result_path": t.get("result_path")} for t in tasks],
        "summary": note,
    }
    return _fit(body, limit or _limit())


# ----------------------------------------------------------------- validation

_ENTRY_TEXT_KEYS = tuple(key for key in DECISION_KEYS if key not in ("blocking", "affects"))
_CUT_KEYS = ("tasks", "summary", "objective", *SECTIONS)


def _count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _strings(value):
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _entry_ok(entry):
    return (isinstance(entry, dict) and all(isinstance(entry.get(key), str) for key in _ENTRY_TEXT_KEYS)
            and isinstance(entry.get("blocking"), bool) and _strings(entry.get("affects")))


def _task_ok(task):
    return (isinstance(task, dict) and isinstance(task.get("id"), str) and isinstance(task.get("status"), (str, type(None)))
            and isinstance(task.get("result_path"), (str, type(None))))


def _problem(value, workspace):
    if not isinstance(value, dict):
        return "not a JSON object"
    if value.get("schema") != SCHEMA:
        return "unknown schema"
    digest = value.get("sha256")
    body = {k: v for k, v in value.items() if k != "sha256"}
    if not isinstance(digest, str) or hashlib.sha256(_canonical(body)).hexdigest() != digest:
        return "sha256 does not match the body"
    if not isinstance(value.get("workspace"), str) or (workspace is not None and value["workspace"] != workspace):
        return "names another workspace"
    if not all(isinstance(value.get(key), str) for key in ("run", "at", "trigger", "workspace_updated", "workspace_sha256")) \
            or _parse(value["at"]) is None:
        return "malformed header fields"
    state, counts, truncated, summary = value.get("state"), value.get("counts"), value.get("truncated"), value.get("summary")
    if not isinstance(state, dict) or not isinstance(state.get("objective"), str):
        return "state is malformed"
    if not isinstance(value.get("tasks"), list) or not all(_task_ok(t) for t in value["tasks"]):
        return "tasks is malformed"
    if not isinstance(summary, dict) or summary.get("authored_by") not in ("agent", None) or not isinstance(summary.get("text"), str):
        return "summary is malformed"
    if "carried_from" in summary:
        source = summary["carried_from"]
        if summary["authored_by"] != "agent" or not isinstance(source, dict) or not _count(source.get("seq")) \
                or not isinstance(source.get("at"), str):
            return "summary is malformed"
    if not isinstance(counts, dict):
        return "counts is malformed"
    if truncated is not False:
        sections = truncated.get("sections") if isinstance(truncated, dict) else None
        if not isinstance(sections, dict) or not all(key in _CUT_KEYS and _count(n) for key, n in sections.items()) \
                or not _count(truncated.get("bytes_limit")):
            return "malformed truncated flag"
    left_out = truncated["sections"] if truncated else {}
    for key in SECTIONS:
        copied = state.get(key)
        if not _count(counts.get(key)) or not (_strings(copied) if key != "open_decisions" else
                                                (isinstance(copied, list) and all(_entry_ok(e) for e in copied))):
            return f"{key} is malformed"
        if len(copied) + left_out.get(key, 0) != counts[key]:
            return f"{key}: {len(copied)} copied, {left_out.get(key, 0)} left out, but the workspace held {counts[key]}"
    return ""


def check(value, workspace=None):
    """'' when `value` is a checkpoint whose own sha256 matches its body, whose every field has the shape it is read with, and whose
    copied lists agree with `counts` (allowing for what `truncated` says was left out); else one line saying what is wrong. Total: a
    malformed value is a problem line, never an exception."""
    try:
        return _problem(value, workspace)
    except Exception as error:  # noqa: BLE001 - validation must never raise on data it did not write
        return f"malformed ({type(error).__name__})"


def read_checkpoint(path):
    """(checkpoint, '') or (None, why it is not valid)."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as error:
        return None, f"unreadable ({type(error).__name__})"
    problem = check(value, Path(path).parent.name)
    return (None, problem) if problem else (value, "")


# ------------------------------------------------------------------ the files

def directory(ws_id):
    if not work.ID_RE.fullmatch(ws_id or ""):
        raise WorkError(f"invalid id {ws_id!r}: lowercase letters, digits and '-', at most 48 characters")
    return work.store_root() / "checkpoints" / ws_id


def files(ws_id):
    """The checkpoint files of a workspace, newest first (by sequence number, then name). Temp files are not counted."""
    try:
        names = [p for p in directory(ws_id).iterdir() if NAME_RE.fullmatch(p.name) and p.is_file() and not p.is_symlink()]
    except OSError:
        return []
    return sorted(names, key=lambda p: (int(NAME_RE.fullmatch(p.name).group(2)), p.name), reverse=True)


def latest(ws_id, run=None):
    """{'valid': (path, checkpoint) | None, 'files': N, 'newest_failed': bool, 'problem': why the newest failed}: the newest valid
    checkpoint, falling back past invalid ones. With `run`, the newest valid one that run wrote (the store is shared, so another
    run's newer checkpoint must not stand in for this run's). Reads only."""
    found = files(ws_id)
    result = {"valid": None, "files": len(found), "newest_failed": False, "problem": ""}
    for index, path in enumerate(found):
        value, problem = read_checkpoint(path)
        if value is not None and (run is None or value.get("run") == run):
            result["valid"] = (path, value)
            return result
        if index == 0:
            result["newest_failed"], result["problem"] = True, problem
    if found:
        result["newest_failed"] = True
    return result


def _seq(path):
    return int(NAME_RE.fullmatch(Path(path).name).group(2))


def carried_summary(ws_id):
    """The agent summary of the newest valid checkpoint as {text, seq, at} (where it was first written, however many automatic
    checkpoints have carried it since), or None: an automatic checkpoint brings no summary of its own, and the one the agent wrote
    before a manual /compact must not vanish from recovery because a newer snapshot was taken."""
    found = latest(ws_id)["valid"]
    if found is None:
        return None
    path, value = found
    note = value["summary"]
    if note.get("authored_by") != "agent" or not note.get("text"):
        return None
    origin = note.get("carried_from") or {"seq": _seq(path), "at": value["at"]}
    return {"text": note["text"], "seq": origin["seq"], "at": origin["at"]}


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _private(path):
    work.check_owned(path, work.store_root(), "checkpoint directory")  # checkpoints/ and checkpoints/<id>, neither a link
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def write(doc, run="", trigger="manual", tasks=(), summary=None):
    """Write and validate a checkpoint of `doc`; returns (path, checkpoint). The caller holds the store lock. Any failure raises
    WorkError and leaves no file of this write behind; the previous checkpoints are never touched except by pruning after success."""
    if trigger not in TRIGGERS:
        raise WorkError("unknown trigger; one of " + ", ".join(TRIGGERS))
    work.ensure_store()
    folder = directory(doc.id)
    if folder.is_symlink() or folder.parent.is_symlink():
        raise WorkError(f"unsafe checkpoint directory (symlink): {folder.name}")
    sealed = seal(snapshot(doc, run, trigger, tasks, summary, carried=None if summary else carried_summary(doc.id)))
    text = _dump(sealed)
    path, tmp = None, None
    try:
        _private(folder.parent)
        _private(folder)
        seq = max([int(NAME_RE.fullmatch(p.name).group(2)) for p in files(doc.id)] + [0]) + 1
        path = folder / f"{_utcnow().strftime('%Y%m%d-%H%M%S')}-{seq:04d}.json"
        fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=folder)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        tmp = None
        _fsync_dir(folder)
        back, problem = read_checkpoint(path)
        if back is None or back.get("sha256") != sealed["sha256"]:
            raise WorkError(f"checkpoint failed validation after writing ({problem or 'it differs from what was written'})")
    except WorkError:
        _discard(path, tmp)
        raise
    except (OSError, ValueError) as error:
        _discard(path, tmp)
        raise WorkError(f"checkpoint not written: {type(error).__name__}: {error}") from error
    _prune(doc.id)
    return path, back


def _discard(path, tmp):
    """Remove what this write created: its temp file, and its final file when that failed validation (it is ours, and invalid)."""
    for item in (tmp, path):
        if item:
            with contextlib.suppress(OSError):
                os.unlink(item)


def _prune(ws_id):
    """Keep the newest `checkpoints_keep` VALID checkpoints (and any invalid file newer than the oldest of them); everything beyond
    that is removed unread. Counting valid ones means pruning can never remove the last valid checkpoint."""
    valid = 0
    for path in files(ws_id):
        if valid >= _keep():
            with contextlib.suppress(OSError):
                os.unlink(path)
        elif read_checkpoint(path)[0] is not None:
            valid += 1


def due(ws_id, now=None):
    """True when there is no valid checkpoint or the newest valid one is older than STALE_SECONDS (the work-mutation trigger)."""
    found = latest(ws_id)["valid"]
    if found is None:
        return True
    when = _parse(found[1].get("at"))
    return when is None or ((now or _utcnow()) - when).total_seconds() > STALE_SECONDS


# ------------------------------------------------------------ task diff, status

def tasks_not_checkpointed(checkpoint):
    """How many task rows the byte bound left out of a checkpoint (0 when the snapshot is complete)."""
    cut = checkpoint.get("truncated")
    return cut["sections"].get("tasks", 0) if cut else 0


def diff_tasks(old, live, partial=False):
    """What changed between a checkpoint's task snapshot and the live records, one line each; empty when nothing did. With `partial`
    (the snapshot lost rows to the byte bound) only the checkpointed rows are compared: a live task that has no row may have existed
    all along, so it is never called new."""
    now = {str(t.get("id")): t for t in live}
    out = []
    for task in old:
        ident = str(task.get("id"))
        current = now.pop(ident, None)
        if current is None:
            out.append(f"{ident} missing (was {task.get('status')})")
        elif current.get("status") != task.get("status") or current.get("result_path") != task.get("result_path"):
            path = current.get("result_path")
            out.append(f"{ident} was {task.get('status')}, now {current.get('status')}" + (f" with result path {path}" if path else ""))
    if not partial:
        out += [f"{ident} is new ({t.get('status')})" for ident, t in now.items()]
    return out


def workspace_health(ws_id):
    """The one answer to 'can this workspace file be used?', for the index and for recover: (doc, 'ok'), or (None, 'file missing'),
    or (None, 'corrupt: <why>'). A file without frontmatter counts as corrupt. Live and archived workspaces are both looked at."""
    for archived in (False, True):
        path = work._file_path("workspace", ws_id, archived)
        if path.is_file() and not path.is_symlink():
            try:
                if not work.parse(path.read_text(encoding="utf-8", errors="replace"))[0]:
                    return None, "corrupt: no frontmatter"
                return work.load("workspace", ws_id), "ok"
            except (OSError, WorkError, ValueError) as error:
                return None, f"corrupt: {type(error).__name__}"
    return None, "file missing"


def _current_sha(doc):
    try:
        return hashlib.sha256(Path(doc.path).read_bytes()).hexdigest()
    except OSError:
        return ""


def _tasks_changed(checkpoint, run, tasks):
    if tasks is None or not run or checkpoint.get("run") != run:
        return None
    return len(diff_tasks(checkpoint["tasks"], tasks, bool(tasks_not_checkpointed(checkpoint))))


def status(ws_id, run="", tasks=None, opened=None):
    """The `checkpoint` entry of the index for one attached workspace: small, only ids, times and flags (no question or note text), plus
    `line` and `caution`, the exact words the injection and Pi show. `opened` is `workspace_health(ws_id)` when the caller already
    has it. Reads only."""
    doc, health = opened if opened is not None else workspace_health(ws_id)
    found = latest(ws_id)
    entry = {"workspace": ws_id, "state": "none", "workspace_file": health}
    valid = found["valid"]
    updated_age = age_text(work.updated_iso(doc)) if doc is not None else ""
    if valid is None:
        entry["state"] = "none-valid" if found["files"] else "none"
    else:
        value = valid[1]
        entry.update(state="older-valid" if found["newest_failed"] else "valid", at=value["at"], age=age_text(value["at"]),
                     truncated=bool(value["truncated"]), authored_by=value["summary"].get("authored_by"),
                     summary_from=value["summary"].get("carried_from"), tasks_partial=tasks_not_checkpointed(value),
                     stale=doc is not None and bool(value.get("workspace_sha256")) and _current_sha(doc) != value["workspace_sha256"],
                     tasks_changed=_tasks_changed(value, run, tasks))
    entry["line"] = _line(entry, updated_age)
    entry["caution"] = (f"workspace last updated {updated_age} ago: anything decided since then may exist only in the compaction "
                        "summary; re-confirm with the user.") if doc is not None else ""
    return entry


def _line(entry, updated_age):
    state, health = entry["state"], entry["workspace_file"]
    unusable = health != "ok"
    if state in ("none", "none-valid"):
        why = "none valid (newest failed validation)" if state == "none-valid" else "none"
        return "checkpoint: " + why + (f", and the workspace {health}" if unusable else "; the workspace file is the source")
    carried = entry.get("summary_from")
    kind = "valid snapshot" + ((" with agent summary" + (f" carried from seq {carried['seq']}" if carried else ""))
                               if entry.get("authored_by") == "agent" else "")
    if state == "older-valid":
        head = f"checkpoint: last valid is older ({entry['age']} old; newest failed validation)"
    else:
        head = f"checkpoint: {kind}, {entry['age']} old"
    notes = []
    if updated_age:
        notes.append(f"workspace updated {updated_age} ago")
    changed = entry.get("tasks_changed")
    if changed is not None:
        notes.append(f"{changed} task{'s' if changed != 1 else ''} changed since" if changed else "no task changed since")
    if entry.get("tasks_partial"):
        notes.append(f"task diff partial: {entry['tasks_partial']} task rows were not checkpointed")
    if entry.get("stale"):
        notes.append("stale: the workspace changed after it")
    if unusable:
        notes.append(f"workspace {health}; `tp-agent work recover` prints the checkpoint state")
    if entry.get("truncated"):
        notes.append(TRUNCATED_LABEL)
    return head + (f" ({'; '.join(notes)})" if notes else "")


# ------------------------------------------------------------------- recovery

def recover(ws_id, live_tasks=None, run=""):
    """The reconciliation facts for one workspace, read-only: the live workspace state (or, when its file is missing or corrupt, the
    latest valid checkpoint's copy, labelled), the checkpoint, the task diff against live records, and any section in which the
    workspace file holds fewer items than a complete checkpoint copy."""
    doc, health = workspace_health(ws_id)
    found = latest(ws_id)
    valid = found["valid"]
    out = {"workspace": ws_id, "file": health, "source": "none", "newest_failed": found["newest_failed"],
           "newest_problem": found["problem"], "checkpoint": None, "tasks": {"known": False, "changes": [], "partial": 0}, "state": None,
           "counts": None, "shortfall": {}, "workspace_age": ""}
    if valid is not None:
        path, value = valid
        out["checkpoint"] = {"path": path, "at": value["at"], "age": age_text(value["at"]), "trigger": value.get("trigger"),
                             "truncated": value["truncated"], "authored_by": value["summary"].get("authored_by"),
                             "summary": value["summary"].get("text", ""), "summary_from": value["summary"].get("carried_from"), "counts": value["counts"], "state": value["state"],
                             "stale": doc is not None and bool(value.get("workspace_sha256")) and _current_sha(doc) != value["workspace_sha256"]}
    own = (valid if valid is not None and valid[1].get("run") == run else latest(ws_id, run)["valid"]) if run else None
    if live_tasks is not None and own is not None:  # the task diff is against this run's newest checkpoint, whatever is newer
        missing = tasks_not_checkpointed(own[1])
        out["tasks"] = {"known": True, "changes": diff_tasks(own[1]["tasks"], live_tasks, bool(missing)), "partial": missing}
        out["tasks_against"] = {"path": own[0], "at": own[1]["at"]}
    if doc is not None:
        out["source"], out["state"] = "workspace", judgement_state(doc)
        out["workspace_age"] = age_text(work.updated_iso(doc))
        if valid is not None and not valid[1]["truncated"]:
            out["shortfall"] = {key: [len(out["state"][key]), valid[1]["counts"][key]] for key in SECTIONS
                                if len(out["state"][key]) < valid[1]["counts"][key]}
    elif valid is not None:
        out["source"], out["state"] = "checkpoint", valid[1]["state"]
    if out["state"] is not None:
        out["counts"] = {key: len(out["state"][key]) for key in SECTIONS}
    return out


def entry_lines(entry):
    """One checkpointed decision as the lines the workspace file holds, by `work.render_decisions` (the writer of that section), so
    every field of every entry, open, answered or withdrawn, is printed exactly as the workspace view prints it."""
    value = {key: entry.get(key, "") for key in DECISION_KEYS if key != "extra"}
    value.update(blocking=bool(entry.get("blocking")), affects=list(entry.get("affects") or []),
                 _extra=[line for line in str(entry.get("extra") or "").split("\n") if line.strip()], _dirty=True)
    return work.render_decisions([value]).split("\n")


def _open_count(lines):
    return sum(1 for line in lines if not re.match(r"^\s*[-*] \[[xX]\]", line))


def format_recover(info):
    out = [f"recover {info['workspace']}: workspace {info['file']}"]
    ckpt = info["checkpoint"]
    if ckpt is None:
        out.append("checkpoint: " + ("none valid (every checkpoint file failed validation)" if info["newest_failed"] else "none")
                   + ("" if info["source"] == "workspace" else "; nothing to recover from"))
    else:
        kind = "snapshot" + (" with agent summary" if ckpt["authored_by"] == "agent" else "")
        out.append(f"checkpoint: valid {kind}, {ckpt['age']} old, taken at {ckpt['at']} by {ckpt['trigger']}")
        if info["newest_failed"]:
            out.append(f"  the newest checkpoint failed validation ({info['newest_problem'] or 'unreadable'}); this is the last valid one")
        if ckpt["truncated"]:
            out.append(f"  {TRUNCATED_LABEL} (left out: {json.dumps(ckpt['truncated']['sections'], sort_keys=True)}; "
                       f"the workspace held {json.dumps(ckpt['counts'], sort_keys=True)})")
        if ckpt["stale"]:
            out.append("  stale: the workspace file changed after this checkpoint")
        if ckpt["summary"]:
            source = ckpt["summary_from"]
            label = (f"agent summary (carried forward from checkpoint seq {source['seq']}, written {age_text(source['at'])} ago)"
                     if source else "agent summary")
            out.append(f"  {label}: {ckpt['summary']}")
    tasks = info["tasks"]
    if tasks["known"]:
        out.append("tasks since the checkpoint: " + ("none changed" if not tasks["changes"] else ""))
        against = info.get("tasks_against") or {}
        if ckpt is None or against.get("path") != ckpt["path"]:
            out.append(f"  compared with this run's newest checkpoint, taken at {against.get('at')} (the one above is another run's)")
        out += [f"  - {line}" for line in tasks["changes"]]
        if tasks.get("partial"):
            out.append(f"task diff partial: {tasks['partial']} task rows were not checkpointed")
    else:
        out.append("tasks: no comparison (no checkpoint from this run, or no session); run `tp-agent list` for the live records")
    state = info["state"]
    if state is None:
        out.append("no workspace state to show: the workspace file is unavailable and there is no valid checkpoint")
        return "\n".join(out)
    label = "the workspace file" if info["source"] == "workspace" else "the latest valid checkpoint (the workspace file is unavailable)"
    out.append(f"state from {label}:")
    if info["workspace_age"]:
        out.append(f"  workspace last updated {info['workspace_age']} ago; anything decided since may exist only in the compaction summary: re-confirm with the user.")
    elif info["source"] == "checkpoint" and ckpt and ckpt["truncated"]:
        out.append(f"  {TRUNCATED_LABEL}; this is not a complete copy.")
    out.append(f"objective: {state['objective'] or '(none)'}")
    counts = info["counts"]
    out.append(f"decisions ({counts['decisions']}):")
    out += [f"  {line}" for line in state["decisions"]] or ["  (none)"]
    out.append(f"open decisions ({counts['open_decisions']}; tell the user about each one that is open):")
    for entry in state["open_decisions"]:
        out += [f"  {line}" for line in entry_lines(entry)]
    if not state["open_decisions"]:
        out.append("  (none)")
    out.append(f"blockers ({counts['blockers']}, {_open_count(state['blockers'])} open; [x] marks a done one):")
    out += [f"  {line}" for line in state["blockers"]] or ["  (none)"]
    out.append(f"next actions ({counts['next_actions']}, {_open_count(state['next_actions'])} unfinished; [x] marks a done one):")
    out += [f"  {line}" for line in state["next_actions"]] or ["  (none)"]
    out.append(f"sources ({counts['sources']}):")
    out += [f"  {line}" for line in state["sources"]] or ["  (none)"]
    for key, (live, copied) in info["shortfall"].items():
        out.append(f"warning: the workspace file holds {live} {key.replace('_', ' ')} but the checkpoint copied {copied}; "
                   f"the checkpoint's copy follows (the file may have lost text):")
        copy = ckpt["state"][key]
        out += [f"  {line}" for entry in copy for line in (entry_lines(entry) if key == "open_decisions" else [entry])]
    out.append("next: re-verify any \"done\" claim against live records (`tp-agent list`, `tp-agent inspect ID`); list the open "
               "decisions above for the user; resume from verified progress.")
    return "\n".join(out)


# ------------------------------------------------------------------ the marker
#
# A monotonic generation. Every arming event (PreCompact and SessionStart(compact) through `work compacted`, `work attach`,
# `autonomy on`) publishes the next number as a new file `session-recovery/<generation>.json` with os.link, which fails when the
# number is taken; the arm then takes the next one. Arming takes no lock and never waits, so a compaction cannot time out behind a
# slow `work recovered`. Numbers are only ever added, each one the maximum seen plus one, so a number an arm manages to publish is
# above every number that existed before it: an arm that read a stale maximum merely retries. Concurrent arms can therefore only add
# generations (over-arm), never reuse one that a clear has already covered (under-arm).
# `session-recovery.json` is the clear record, written only by `recovered` under the state lock: `cleared_generation` is the newest
# generation that existed BEFORE the reconciliation ran, and pending = current generation > cleared generation, so a compaction during
# or after the reconciliation stays pending by construction. A Phase C record `{pending: true}` (no cleared_generation) reads as
# pending until the next `recovered` replaces it.

RECOVERY_DIR = "session-recovery"
GENERATION_RE = re.compile(r"^(\d{12})\.json$")


@contextlib.contextmanager
def state_lock(state_dir, seconds=5.0):
    """The lock `recovered` (validate and clear) and `autonomy on` (write its record and arm) take, so neither runs inside the other.
    Arming alone takes no lock."""
    fd = os.open(Path(state_dir) / ".session-recovery.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        deadline = time.monotonic() + seconds
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise WorkError("the recovery state is locked by another tp-agent (work recovered or autonomy on); try again")
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def _generations(state_dir):
    """The published generation numbers, ascending; None when the directory does not exist. OSError when it cannot be read."""
    folder = Path(state_dir) / RECOVERY_DIR
    if folder.is_symlink():
        raise OSError(f"{RECOVERY_DIR} is a symlink")
    try:
        names = os.listdir(folder)
    except FileNotFoundError:
        return None
    return sorted(int(m.group(1)) for m in map(GENERATION_RE.fullmatch, names) if m)


def _event(state_dir, generation):
    try:
        value = json.loads((Path(state_dir) / RECOVERY_DIR / f"{generation:012d}.json").read_text())
    except (OSError, ValueError):
        value = None
    if not isinstance(value, dict):
        return {"since": "", "reason": "an unreadable recovery event"}
    return {"since": str(value.get("since") or ""), "reason": str(value.get("reason") or "")}


def marker(state_dir, missing_is_pending=False):
    """{pending, generation, cleared_generation, since, reason, problem, evidence}. Reads only and never raises.

    `since`/`reason` are those of the oldest event not yet cleared (so the first compaction's time stays while more happen), or of the
    last cleared one. Whatever cannot be read is pending (`problem` says why): not knowing is not recovered. A missing generation
    directory is clear for an interactive run, and pending with `missing_is_pending` (autonomy on armed it, so its absence means it
    was removed)."""
    out = {"pending": False, "generation": 0, "cleared_generation": 0, "since": "", "reason": "", "problem": "", "evidence": ""}
    path = Path(state_dir) / RECOVERY_FILE
    legacy = None
    try:
        if path.is_symlink():
            raise ValueError("a symlink")
        record = json.loads(path.read_text())
        if not isinstance(record, dict):
            raise ValueError("not a JSON object")
        if "cleared_generation" in record:
            cleared = record["cleared_generation"]
            if not isinstance(cleared, int) or isinstance(cleared, bool) or cleared < 0:
                raise ValueError("a bad cleared_generation")
            out["cleared_generation"], out["evidence"] = cleared, str(record.get("evidence") or "")
            out["since"], out["reason"] = str(record.get("since") or ""), str(record.get("reason") or "")
        elif record.get("pending") is True:  # a Phase C marker
            legacy = {"since": str(record.get("since") or ""), "reason": str(record.get("reason") or "compaction")}
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as error:
        out["problem"] = f"the recovery record is unreadable ({type(error).__name__ if isinstance(error, OSError) else error})"
    try:
        found = _generations(state_dir)
    except OSError as error:
        out.update(pending=True, problem=f"the recovery generations are unreadable ({type(error).__name__})")
        return out
    if found is None and missing_is_pending:
        out.update(pending=True, problem="the recovery generation state is missing")
        return out
    found = found or []
    out["generation"] = found[-1] if found else 0
    newer = [n for n in found if n > out["cleared_generation"]]
    if out["generation"] < out["cleared_generation"]:
        out["problem"] = out["problem"] or "the recovery generations are behind the clear record"
    if newer:
        out.update(_event(state_dir, newer[0]))
    elif legacy:
        out.update(legacy)
    out["pending"] = bool(newer or legacy or out["problem"])
    return out


def _fsync_dir(folder):
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def arm(state_dir, reason="compaction"):
    """Publish the next recovery generation (lock-free; see above). Returns the marker as it now reads, plus `armed` (the generation
    published) and `changed` (the run was not pending before this event)."""
    if state_dir is None or not Path(state_dir).is_dir():
        raise WorkError("no tmux-pi state directory; start a workspace with tp, or set TP_STATE_DIR")
    before = marker(state_dir)["pending"]
    folder = Path(state_dir) / RECOVERY_DIR
    try:
        safe_write.ensure_dir(folder, mode=0o700, what="recovery directory", root=state_dir)
    except safe_write.UnsafePath as exc:
        raise WorkError(str(exc))
    body = {"since": work.now(), "reason": re.sub(r"\s+", " ", str(reason or "compaction")).strip()[:MARKER_REASON_CHARS]}
    fd, name = tempfile.mkstemp(prefix=".arm.", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(json.dumps(body) + "\n")
            out.flush()
            os.fsync(out.fileno())
        while True:
            generation = (_generations(state_dir) or [0])[-1] + 1
            try:
                os.link(name, folder / f"{generation:012d}.json")  # whole, ours, or taken by another arm (then the next one)
                break
            except FileExistsError:
                continue
    finally:
        os.unlink(name)
    _fsync_dir(folder)
    return {**marker(state_dir), "armed": generation, "changed": not before}


def compacted(state_dir, reason="compaction"):
    """Arm recovery-pending for a compaction or an attach: a new generation every time, so a compaction that lands while
    `work recovered` is checking is never covered by that clear. Repeating it only adds generations; `since` and `reason` stay those of
    the first event not yet cleared. Only `work recovered` clears it."""
    return arm(state_dir, reason)


def recovered(state_dir, evidence, accepted=(), check=None):
    """Clear the marker up to the generation that existed before `check()` (the reconciliation; it raises when recovery is not
    complete) ran. The clear record keeps the time and reason of what was recovered from. Nothing clears it by time; clearing an already
    clear marker changes nothing and says so. It holds `state_lock`, so `autonomy on` cannot write its record and arm in between;
    compactions take no lock and, as newer generations, stay pending after this clear."""
    if state_dir is None or not Path(state_dir).is_dir():
        raise WorkError("no tmux-pi state directory; start a workspace with tp, or set TP_STATE_DIR")
    with state_lock(state_dir):
        try:
            found = _generations(state_dir)
        except OSError as error:
            raise WorkError(f"the recovery generations cannot be read ({type(error).__name__}); nothing was cleared") from error
        validated = (found or [0])[-1]
        before = marker(state_dir)
        if check is not None:
            check()
        if found is None:  # never armed, or removed (which autonomy reads as pending): after the check it comes back empty, i.e. clear
            try:
                safe_write.ensure_dir(Path(state_dir) / RECOVERY_DIR, mode=0o700, what="recovery directory", root=state_dir)
            except safe_write.UnsafePath as exc:
                raise WorkError(str(exc))
        if not before["pending"]:
            return {"pending": False, "changed": found is None, "cleared_generation": before["cleared_generation"]}
        value = {"cleared_generation": validated, "cleared": work.now(),
                 "evidence": re.sub(r"\s+", " ", str(evidence)).strip()[:MARKER_REASON_CHARS * 2],
                 "accepted": sorted(accepted), "since": before["since"], "reason": before["reason"]}
        work.atomic_write(Path(state_dir) / RECOVERY_FILE, json.dumps(value, indent=2) + "\n", root=state_dir)
        return {**value, "pending": marker(state_dir)["pending"], "changed": True}
