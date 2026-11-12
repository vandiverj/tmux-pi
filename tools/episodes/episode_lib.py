"""Shared reading of episode files and of the records an episode leaves (HB8). Python 3.9, standard library only.

The runner (run.py) uses it to wait on records; the scorer (score.py) to score them; the fake stand-ins (fake/) to write the same
records a real Main and real workers write. Nothing here starts a model, talks to the network or writes outside the run dir.

Records read (all optional; a missing record is reported as missing, never as zero):
  TP_STATE_DIR/tasks/<id>/  meta.json, state.json, packet.md, task.md, result.md, messages.jsonl, events.jsonl,
                            requests/*.json, test-runs/*.json, transcript-path (or transcript.jsonl)
  <clone>/.git/tmux-pi/lanes/<name>/lane.json
  Claude transcripts (JSONL, `message.content[]` tool_use / text parts) and pi transcripts (`toolCall` parts).
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
if str(ROOT / "lib") not in sys.path:
    sys.path.insert(0, str(ROOT / "lib"))

SCHEMA = "tp-episode:v1"
RUN_SCHEMA = "tp-episode-run:v1"
LOG_SCHEMA = "tp-episode-log:v1"
STEP_KINDS = ("owner", "wait", "compact", "restart_main", "queue_text", "hold_prompt", "note")
WAIT_KINDS = ("task", "lane", "request", "file", "main_quiet", "any", "all", "not")
OWNER_TAGS = ("goal", "process", "product", "followup")
CHECK_KINDS = ("change_present", "change_absent", "no_diff", "packet", "command", "main_said", "state_seen", "recovered",
               "message_after_state", "message_to_done", "event", "requests_distinct", "no_duplicate_messages", "tasks_overlap",
               "start_gap", "result_complete", "task_count", "message_count", "pause_backed")
CHECK_REQUIRED = {"change_present": ("path", "pattern"), "change_absent": ("path", "pattern"), "packet": ("pattern",),
                  "command": ("pattern",), "main_said": ("pattern",), "state_seen": ("state",), "message_after_state": ("state",),
                  "event": ("event",), "tasks_overlap": ("lanes",), "start_gap": ("lane", "after_lane"), "message_count": ("max",)}
CHECK_MEASURES = ("evidence", "recurrence", "leak", "recovery", "irrelevant_check")
TASK_STATES = ("starting", "working", "tool", "permission", "input", "waiting", "completed", "failed", "killed", "stopped",
               "escalated")
PROMPT_STATES = ("permission", "input")      # a worker that waits on someone else's answer
TERMINAL_STATES = ("completed", "failed", "killed", "stopped")
PLANT_KINDS = ("hold_prompt", "queue_text")  # forced conditions the episode depends on; a missed one makes its measures unknown
# Measures an episode may name in expected.measures: a number is the most it may be (n/a holds), a boolean the value it must be.
# Shell-read counts (safe_tools) are diagnostic only (round 3 rule 5): they are scored and compared, never expected.
EXPECTABLE_MEASURES = ("completed", "recurrence", "leak", "recovery", "avoidable_waiting", "false_claims", "irrelevant_checks",
                       "verification", "into_prompt")

# ---- records an episode's scoring needs (round 3 rule 1) -------------------------------------------------------------------
# Each episode lists these in expected.records; score.py builds a manifest of which exist before it scores anything.
RECORD_KINDS = ("episode_log",         # run/episode-log.jsonl with run.start and run.end
                "observations",        # at least one runner observation of every task (from the episode log)
                "state_dir",           # TP_STATE_DIR (the session's task records)
                "main_transcript",     # every Main transcript run.json lists, readable (empty only if the log shows Main ran)
                "worker_transcripts",  # every task's transcript, readable (empty only if the log saw that worker run)
                "worker_results",      # every task's result.md RESULT_STATUS (or an observed killed/stopped/failed end)
                "worker_packets",      # every task's packet.md or task.md
                "final_check",         # run/final-check.json with a tree and an exit (or timed_out)
                "clone",               # the scratch clone
                "events",              # every task's events.jsonl (HB0)
                "requests",            # every task's requests/ dir, unless the task was observed and never at a prompt (HB4)
                "plants")              # every planted condition (hold_prompt, queue_text) the log shows as done
CORE_RECORDS = ("episode_log", "state_dir")      # needed by every measure: missing either makes the run incomplete-records
BASE_RECORDS = ("episode_log", "observations", "state_dir", "main_transcript", "worker_transcripts", "worker_results",
                "worker_packets", "final_check", "clone")
CHECK_RECORDS = {"change_present": ("clone",), "change_absent": ("clone",), "no_diff": ("clone",),
                 "packet": ("state_dir", "observations", "worker_packets"), "main_said": ("main_transcript",),
                 "state_seen": ("observations",), "recovered": ("observations",),
                 "message_after_state": ("observations", "state_dir"), "message_to_done": ("observations", "state_dir"),
                 "event": ("state_dir", "events"), "requests_distinct": ("state_dir", "requests"),
                 "no_duplicate_messages": ("state_dir",), "tasks_overlap": ("state_dir", "observations", "worker_packets"),
                 "start_gap": ("state_dir", "observations", "worker_packets"), "result_complete": ("state_dir", "worker_results"),
                 "task_count": ("state_dir", "observations"), "message_count": ("state_dir",),
                 "pause_backed": ("main_transcript", "observations", "state_dir")}   # events.jsonl used when present
# Measures computed from the records directly (recurrence, leak and irrelevant_checks come from their checks).
MEASURE_RECORDS = {"completed": ("final_check", "clone"), "corrections": ("episode_log",),
                   "recurrence": (), "leak": (), "irrelevant_checks": (),
                   "recovery": ("observations", "main_transcript"),
                   "avoidable_waiting": ("observations", "main_transcript", "state_dir"),
                   "false_claims": ("main_transcript", "observations", "state_dir", "worker_results"),
                   "verification": ("main_transcript", "worker_transcripts", "state_dir"),
                   "safe_tools": ("worker_transcripts", "state_dir"),
                   "into_prompt": ("observations", "state_dir", "worker_transcripts")}
MEASURE_OPTIONAL = {"into_prompt": ("requests",)}          # needed only when the episode lists them
MEASURE_PLANTS = {"recovery": ("hold_prompt",), "avoidable_waiting": ("hold_prompt",), "into_prompt": ("hold_prompt", "queue_text")}


def check_records(check):
    """The record kinds one expected check reads (CORE_RECORDS always)."""
    if check.get("check") == "command":
        by = check.get("by", "any")
        need = ["main_transcript"] if by == "main" else ["main_transcript", "worker_transcripts", "state_dir"] if by == "any" \
            else ["worker_transcripts", "state_dir"]
        if check.get("final_tree"):
            need += ["final_check", "state_dir"]
        return tuple(CORE_RECORDS) + tuple(need)
    return tuple(CORE_RECORDS) + CHECK_RECORDS.get(check.get("check"), ())


def required_records(episode):
    """The record kinds an episode must list: the base kinds, those its checks read, and plants when it plants anything."""
    need = list(BASE_RECORDS)
    for check in episode.get("expected", {}).get("checks", []):
        need += [k for k in check_records(check) if k not in need]
    if any(s.get("do") in PLANT_KINDS for s in episode.get("steps", [])) and "plants" not in need:
        need.append("plants")
    return need
RESULT_RE = re.compile(r"^\s*RESULT_STATUS:\s*([A-Za-z_-]+)\s*$", re.M)


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_time(value):
    """ISO-8601 text (Z or offset) or epoch seconds/milliseconds -> aware datetime, else None."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000.0 if value > 1e11 else float(value)
        return datetime.fromtimestamp(seconds, timezone.utc)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def read_jsonl(path):
    """The JSON objects of a JSONL file; lines that are not JSON objects are skipped. None when the file is absent."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    out = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def read_jsonl_status(path):
    """(status, entries, why) of a JSONL record for the scorer's evidence reader (round 4 rule 1): "missing" when the file is
    absent, "unreadable" when it cannot be read or any non-blank line is not a JSON object (a lost line is lost evidence, not
    an absent event), else "ok" with every entry (possibly none)."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return "missing", None, "%s is absent" % path
    except OSError as error:
        return "unreadable", None, "%s: %s" % (path, error)
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError:
            return "unreadable", None, "%s line %d is not JSON" % (path, n)
        if not isinstance(value, dict):
            return "unreadable", None, "%s line %d is not a JSON object" % (path, n)
        out.append(value)
    return "ok", out, None


def read_json_status(path):
    """(status, value, why) of a JSON record: missing, unreadable (not JSON or not an object) or ok."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return "missing", None, "%s is absent" % path
    except (OSError, ValueError) as error:
        return "unreadable", None, "%s: %s" % (path, error)
    try:
        value = json.loads(text)
    except ValueError:
        return ("empty" if not text.strip() else "unreadable"), None, "%s is %s" % (path, "empty" if not text.strip() else "not JSON")
    if not isinstance(value, dict):
        return "unreadable", None, "%s is not a JSON object" % path
    return "ok", value, None


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".%d.tmp" % os.getpid())
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


def append_jsonl(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True) + "\n")


# ---- episode files --------------------------------------------------------------------------------------------------------
class EpisodeError(ValueError):
    pass


def _check_condition(cond, where):
    if not isinstance(cond, dict) or cond.get("kind") not in WAIT_KINDS:
        raise EpisodeError("%s: a condition needs kind in %s, got %r" % (where, "|".join(WAIT_KINDS), cond))
    if cond["kind"] in ("any", "all"):
        if not isinstance(cond.get("of"), list) or not cond["of"]:
            raise EpisodeError("%s: %s needs a non-empty list `of`" % (where, cond["kind"]))
        for sub in cond["of"]:
            _check_condition(sub, where)
    if cond["kind"] == "not":
        _check_condition(cond.get("of"), where)
    if cond["kind"] == "task":
        states = cond.get("state")
        for state in ([states] if isinstance(states, str) else states or []):
            if state not in TASK_STATES:
                raise EpisodeError("%s: unknown task state %r" % (where, state))


def validate_episode(episode, where="episode"):
    """Raise EpisodeError for anything the runner or scorer could not act on. Returns the episode."""
    if not isinstance(episode, dict) or episode.get("schema") != SCHEMA:
        raise EpisodeError("%s: schema must be %s" % (where, SCHEMA))
    for key in ("id", "row", "variant", "title", "steps", "expected", "project"):
        if key not in episode:
            raise EpisodeError("%s: missing %s" % (where, key))
    if episode["variant"] not in ("positive", "negative"):
        raise EpisodeError("%s: variant must be positive or negative" % where)
    ids = set()
    for index, step in enumerate(episode["steps"]):
        here = "%s step %d" % (where, index + 1)
        if not isinstance(step, dict) or step.get("do") not in STEP_KINDS:
            raise EpisodeError("%s: do must be one of %s" % (here, "|".join(STEP_KINDS)))
        sid = step.get("id")
        if not sid or sid in ids:
            raise EpisodeError("%s: every step needs a unique id" % here)
        ids.add(sid)
        if step["do"] == "owner":
            if step.get("tag") not in OWNER_TAGS:
                raise EpisodeError("%s: owner tag must be one of %s" % (here, "|".join(OWNER_TAGS)))
            text = step.get("text")
            if not isinstance(text, str) or not text.strip() or "\n" in text:
                raise EpisodeError("%s: owner text must be one non-empty line" % here)
            if text.lstrip().startswith("!"):
                raise EpisodeError("%s: owner text must not start with ! (bash mode)" % here)
            if "unless" in step:
                _check_condition(step["unless"], here)
        if step["do"] in ("wait", "hold_prompt"):
            if step["do"] == "wait":
                _check_condition(step.get("until"), here)
            if not isinstance(step.get("timeout"), (int, float)) or step["timeout"] <= 0:
                raise EpisodeError("%s: needs a positive timeout" % here)
        if step["do"] == "queue_text" and (not isinstance(step.get("text"), str) or "\n" in step["text"]):
            raise EpisodeError("%s: queue_text needs one line of text" % here)
    plants = {s["id"] for s in episode["steps"] if s["do"] in PLANT_KINDS}
    measures = episode["expected"].get("measures", {})
    if not isinstance(measures, dict):
        raise EpisodeError("%s: expected.measures must be an object" % where)
    for name, want in measures.items():
        if name not in EXPECTABLE_MEASURES:
            raise EpisodeError("%s: expected measure %s: must be one of %s" % (where, name, "|".join(EXPECTABLE_MEASURES)))
        if want != "unknown" and not isinstance(want, (bool, int, float)):
            raise EpisodeError("%s: expected measure %s: must be a number (the most allowed), true/false or \"unknown\" "
                               "(matches only a scored unknown)" % (where, name))
    checks = episode["expected"].get("checks", [])
    records = episode["expected"].get("records")
    if not isinstance(records, list) or any(r not in RECORD_KINDS for r in records):
        raise EpisodeError("%s: expected.records must list the records scoring needs, each one of %s" % (where, "|".join(RECORD_KINDS)))
    for check in checks:
        if not isinstance(check, dict) or not check.get("id") or not check.get("check"):
            raise EpisodeError("%s: every expected check needs id and check" % where)
        if check["check"] not in CHECK_KINDS:
            raise EpisodeError("%s: check %s: unknown kind %s" % (where, check["id"], check["check"]))
        if check.get("measure", "evidence") not in CHECK_MEASURES:
            raise EpisodeError("%s: check %s: measure must be one of %s" % (where, check["id"], "|".join(CHECK_MEASURES)))
        for ref in ("after", "before"):
            if check.get(ref) and check[ref] not in ids:
                raise EpisodeError("%s: check %s names unknown step %s" % (where, check["id"], check[ref]))
        for need in check.get("needs", []):
            if need not in plants:
                raise EpisodeError("%s: check %s needs %s, which is not a %s step" % (where, check["id"], need, "|".join(PLANT_KINDS)))
        missing = [key for key in CHECK_REQUIRED.get(check["check"], ()) if key not in check]
        if missing:
            raise EpisodeError("%s: check %s (%s) needs %s" % (where, check["id"], check["check"], ", ".join(missing)))
        for key in ("pattern", "lane", "after_lane"):
            if isinstance(check.get(key), str):
                try:
                    re.compile(check[key])
                except re.error as error:
                    raise EpisodeError("%s: check %s: %s is not a regex: %s" % (where, check["id"], key, error))
    unlisted = [k for k in required_records(episode) if k not in records]
    if unlisted:
        raise EpisodeError("%s: expected.records must also list %s (read by its checks or plants)" % (where, ", ".join(unlisted)))
    return episode


def load_episode(path):
    episode = read_json(path)
    if episode is None:
        raise EpisodeError("%s: not a JSON file" % path)
    return validate_episode(episode, str(path))


def substitute(value, mapping):
    """Replace {name} placeholders in every string of value (lists and dicts walked)."""
    if isinstance(value, str):
        for key, sub in mapping.items():
            value = value.replace("{" + key + "}", sub)
        return value
    if isinstance(value, list):
        return [substitute(item, mapping) for item in value]
    if isinstance(value, dict):
        return {key: substitute(item, mapping) for key, item in value.items()}
    return value


# ---- task records ---------------------------------------------------------------------------------------------------------
def result_status(task_dir):
    """The last RESULT_STATUS in result.md, lower case; None when there is no result.md or no status line."""
    try:
        text = (Path(task_dir) / "result.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    found = RESULT_RE.findall(text)
    return found[-1].lower() if found else None


def transcript_path(task_dir):
    task_dir = Path(task_dir)
    try:
        named = (task_dir / "transcript-path").read_text(encoding="utf-8").strip()
    except OSError:
        named = ""
    if named and Path(named).exists():
        return Path(named)
    local = task_dir / "transcript.jsonl"
    return local if local.exists() else (Path(named) if named else None)


def task_snapshot(task_dir):
    task_dir = Path(task_dir)
    meta = read_json(task_dir / "meta.json", {}) or {}
    state = read_json(task_dir / "state.json", {}) or {}
    return {"id": task_dir.name, "dir": str(task_dir), "role": meta.get("role") or task_dir.name.split("-")[0],
            "lane": meta.get("lane"), "pane": meta.get("pane"), "workdir": meta.get("workdir"), "harness": meta.get("worker") or state.get("harness"),
            "created": meta.get("created"), "state": state.get("state"), "tool": state.get("tool") or "",
            "updated": state.get("updated"), "result": result_status(task_dir),
            "review_of": _text(task_dir / "review-of"), "parent": meta.get("parent")}


def _text(path):
    try:
        return Path(path).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def tasks(state_dir):
    base = Path(state_dir) / "tasks" if state_dir else None
    if not base or not base.is_dir():
        return []
    return [task_snapshot(entry) for entry in sorted(base.iterdir()) if entry.is_dir()]


def lanes(clone):
    base = Path(clone) / ".git" / "tmux-pi" / "lanes"
    out = []
    if base.is_dir():
        for entry in sorted(base.iterdir()):
            record = read_json(entry / "lane.json")
            if isinstance(record, dict):
                out.append(record)
    return out


def requests(task_dir):
    """Request records (HB4 interfaces section 4) of a task, or None when the task has no requests directory."""
    base = Path(task_dir) / "requests"
    if not base.is_dir():
        return None
    return [r for r in (read_json(p) for p in sorted(base.glob("*.json"))) if isinstance(r, dict)]


# ---- transcripts ----------------------------------------------------------------------------------------------------------
def claude_project_slug(path):
    """The directory name Claude Code keeps a working directory's transcripts under: every character that is not a letter or a
    digit becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def transcript_entries(path):
    """A Claude or pi transcript -> [{"at", "kind", ...}] in file order. kind: user (owner text), text (assistant text),
    tool (name, input), compact (a compaction boundary). None when the file cannot be read."""
    lines = read_jsonl(path) if path else None
    if lines is None:
        return None
    return transcript_from(lines)


EXIT_RE = re.compile(r"(?i)\b(?:exit code|exited with code|exit status)[:\s]+(-?\d+)")


def _result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
    return ""


def transcript_from(lines):
    """Parsed transcript records -> entries (see transcript_entries). A tool entry carries `id` and, when the transcript
    holds the call's result (Claude tool_result, pi toolResult), `exit`: 0 for a result that is not an error, the code an
    error result names ("Exit code N"), else None with `blocked` True (an error with no exit code: the call may never have
    run). With no result: exit None, blocked False (round 4 rule 5)."""
    out, by_id = [], {}

    def settle(call_id, is_error, content, at):
        entry = by_id.get(call_id)
        if entry is None:
            return
        # Round 6 rule 5: the result's text is kept; a request id it names (an index, `tp-agent request`) is read.
        entry["result_text"] = _result_text(content)
        if not is_error:
            entry["exit"] = 0
        else:
            found = EXIT_RE.search(_result_text(content))
            entry["exit"] = int(found.group(1)) if found else None
            entry["blocked"] = found is None
        entry["result_at"] = at

    for rec in lines:
        at = parse_time(rec.get("timestamp"))
        if rec.get("type") == "system" and rec.get("subtype") == "compact_boundary":
            out.append({"at": at, "kind": "compact"})
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        at = at or parse_time(msg.get("timestamp"))
        role = msg.get("role")
        content = msg.get("content")
        if role == "toolResult":
            settle(msg.get("toolCallId"), bool(msg.get("isError")), content, at)
            continue
        if role == "user" and isinstance(content, str):
            out.append({"at": at, "kind": "user", "text": content})
            continue
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind == "text" and role == "assistant":
                out.append({"at": at, "kind": "text", "text": part.get("text") or ""})
            elif kind == "text" and role == "user" and not rec.get("isMeta"):
                out.append({"at": at, "kind": "user", "text": part.get("text") or ""})
            elif kind in ("tool_use", "toolCall"):
                entry = {"at": at, "kind": "tool", "name": part.get("name") or "", "id": part.get("id"),
                         "input": (part.get("input") if kind == "tool_use" else part.get("arguments")) or {},
                         "exit": None, "blocked": False}
                out.append(entry)
                if part.get("id"):
                    by_id[part["id"]] = entry
            elif kind == "tool_result":
                settle(part.get("tool_use_id"), bool(part.get("is_error")), part.get("content"), at)
    return out


# ---- tool names, one table for both harnesses (interfaces.md section 3) -------------------------------------------------
TOOL_TABLE = {
    "Bash": ("shell", None), "bash": ("shell", None),
    "Read": ("read", False), "read": ("read", False),
    "safe_read": ("read", True), "mcp__tmux-pi-safe__safe_read": ("read", True),
    "Grep": ("grep", False), "grep": ("grep", False),
    "safe_grep": ("grep", True), "mcp__tmux-pi-safe__safe_grep": ("grep", True),
    "Glob": ("find", False), "find": ("find", False), "ls": ("find", False),
    "Edit": ("edit", None), "edit": ("edit", None), "MultiEdit": ("edit", None), "NotebookEdit": ("edit", None),
    "Write": ("write", None), "write": ("write", None),
}


def normalise_tool(name):
    """(tool, safe) for a raw tool name: shell|read|grep|find|edit|write|other; safe is True/False for read and grep, else None."""
    if name in TOOL_TABLE:
        return TOOL_TABLE[name]
    if name.endswith("__safe_read"):
        return ("read", True)
    if name.endswith("__safe_grep"):
        return ("grep", True)
    return ("other", None)


# Round 8 (records only): the Bash grammar that stood here (the classifier, the wrapper table, argv identity from Bash, cwd
# tracking, write detection and the shell-read scan) is removed. The scorer never reads a Bash command.
def _norm(path):
    """Absolute, normalised, with macOS /private/{tmp,var,etc} aliases folded."""
    path = os.path.normpath(path)
    for alias in ("/private/tmp", "/private/var", "/private/etc"):
        if path == alias or path.startswith(alias + "/"):
            path = path[len("/private"):]
    return path


# ---- runner log -----------------------------------------------------------------------------------------------------------
def read_log(run_dir):
    return read_jsonl(Path(run_dir) / "run" / "episode-log.jsonl") or []
