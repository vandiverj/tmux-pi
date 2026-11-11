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
import shlex
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
        if not isinstance(want, (bool, int, float)):
            raise EpisodeError("%s: expected measure %s: must be a number (the most allowed) or true/false" % (where, name))
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
SHELL_READERS = ("grep", "egrep", "fgrep", "rg", "sed", "cat", "head", "tail", "awk", "less", "more", "find")


def normalise_tool(name):
    """(tool, safe) for a raw tool name: shell|read|grep|find|edit|write|other; safe is True/False for read and grep, else None."""
    if name in TOOL_TABLE:
        return TOOL_TABLE[name]
    if name.endswith("__safe_read"):
        return ("read", True)
    if name.endswith("__safe_grep"):
        return ("grep", True)
    return ("other", None)


PATTERN_FIRST = ("grep", "egrep", "fgrep", "rg", "sed", "awk")   # first operand is a pattern/script, not a path
OPTION_ARGS = {      # options whose value is the next word (so it is not a path operand)
    "grep": ("-e", "-f", "-A", "-B", "-C", "-m", "-d", "-D", "--regexp", "--file", "--max-count", "--context",
             "--after-context", "--before-context", "--label", "--exclude", "--include", "--exclude-dir"),
    "rg": ("-e", "-f", "-A", "-B", "-C", "-m", "-g", "-t", "-T", "-j", "-M", "-E", "-r", "--glob", "--iglob", "--type",
           "--type-not", "--regexp", "--file", "--max-count", "--max-columns", "--threads", "--encoding", "--context",
           "--after-context", "--before-context", "--sort", "--sortr", "--color", "--colors", "--max-depth", "--max-filesize",
           "--replace", "--type-add", "--pre", "--pre-glob"),
    "sed": ("-e", "-f", "--expression", "--file", "-l"),
    "awk": ("-F", "-v", "-f"),
    "head": ("-n", "-c"), "tail": ("-n", "-c"),
}
OPTION_ARGS["egrep"] = OPTION_ARGS["fgrep"] = OPTION_ARGS["grep"]
SCRIPT_OPTIONS = ("-e", "-f", "--regexp", "--file", "--expression")   # given: every operand is a path
FILE_OPTIONS = ("-f", "--file")                                       # their value is a file the program reads


def _norm(path):
    """Absolute, normalised, with macOS /private/{tmp,var,etc} aliases folded."""
    path = os.path.normpath(path)
    for alias in ("/private/tmp", "/private/var", "/private/etc"):
        if path == alias or path.startswith(alias + "/"):
            path = path[len("/private"):]
    return path


def _under(path, base):
    base = _norm(base).rstrip("/") or "/"
    return path == base or path.startswith(base + "/")


def _operands(program, words):
    """(paths, recursive, files) of one simple command's argument words. files: the values of -f/--file (a pattern or
    script file the program reads)."""
    takes = OPTION_ARGS.get(program, ())
    operands, script_given, recursive, files = [], False, False, []
    index = 0
    while index < len(words):
        word = words[index]
        if program == "find":
            if word.startswith("-") or word in ("(", "!", ")"):
                break                       # find's expression starts: the rest are not paths
            operands.append(word)
        elif word == "--":
            operands.extend(words[index + 1:])
            break
        elif word.startswith("--"):
            name = word.split("=", 1)[0]
            script_given = script_given or name in SCRIPT_OPTIONS
            recursive = recursive or name in ("--recursive", "--dereference-recursive")
            if name in takes:
                value = word.split("=", 1)[1] if "=" in word else (words[index + 1] if index + 1 < len(words) else None)
                if name in FILE_OPTIONS and value:
                    files.append(value)
                if "=" not in word:
                    index += 1
        elif word.startswith("-") and len(word) > 1:
            flags = word[1:]
            if program in ("grep", "egrep", "fgrep") and not word[:2] in takes:
                recursive = recursive or "r" in flags or "R" in flags
            for n, flag in enumerate(flags):          # -A3, -e PAT, -rnA 3: the first flag that takes a value ends the cluster
                if "-" + flag in takes:
                    script_given = script_given or "-" + flag in SCRIPT_OPTIONS
                    value = flags[n + 1:] or (words[index + 1] if index + 1 < len(words) else None)
                    if "-" + flag in FILE_OPTIONS and value:
                        files.append(value)
                    if n == len(flags) - 1:
                        index += 1
                    break
        else:
            operands.append(word)
        index += 1
    if program in PATTERN_FIRST and not script_given and operands:
        operands = operands[1:]
    return operands, recursive, files


class Word(str):
    """A shell word with its quotes removed (round 6 rule 2). op: a redirect operator with its fd prefix (`2>`, `>>`,
    `&>`, `<<`), never a quoted word. bare: the word's unquoted characters (a glob or a brace expansion is only there).
    expand (round 7 rule 2): the word holds an expansion the shell resolves at run time (`$VAR`, `${…}`, a substitution,
    a leading `~`), unquoted or inside double quotes; a `$` inside single quotes or escaped is literal."""
    op = False
    bare = ""
    expand = False


EXPANDS = re.compile(r"\$[A-Za-z_{(0-9@*#?!$-]")


def unresolved(word):
    """Whether a word's value is not its text (round 7 rule 2): it holds an expansion or an unquoted glob. A plain string
    (a test-run record's argv, exec'd with no shell) is always its own value."""
    return bool(getattr(word, "expand", False) or re.search(r"[*?]|\[.*\]", getattr(word, "bare", "") or ""))


REDIRECT_OPS = ("&>>", "&>", "<<<", "<<-", "<<", ">>", ">|", ">&", "<&", "<>", ">", "<")


def _tokens(text):
    """The words of one simple command's text, quote-aware, with every redirect operator its own Word (op True): `changed>a.txt`
    is `changed`, `>`, `a.txt`; `2>&1` is `2>&`, `1`; a quoted `'>'` is a plain word. Digits directly before an operator
    are its fd only when they make the whole word so far, unquoted. ValueError on an unbalanced quote."""
    out, buf, bare, quoted, i, n = [], [], [], False, 0, len(text)
    expand = [False]

    def flush():
        if buf or quoted:
            word = Word("".join(buf))
            word.bare = "".join(bare)
            word.expand = expand[0]
            out.append(word)
        expand[0] = False

    while i < n:
        c = text[i]
        if c in " \t\n":
            flush()
            buf, bare, quoted = [], [], False
            i += 1
        elif c == "'":
            end = text.find("'", i + 1)
            if end < 0:
                raise ValueError("unbalanced quotes")
            buf.append(text[i + 1:end])
            quoted, i = True, end + 1
        elif c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n and text[j + 1] in '"\\$`\n':
                    buf.append(text[j + 1] if text[j + 1] != "\n" else "")
                    j += 2
                    continue
                if EXPANDS.match(text, j) or text[j] == "`":
                    expand[0] = True
                buf.append(text[j])
                j += 1
            if j >= n:
                raise ValueError("unbalanced quotes")
            quoted, i = True, j + 1
        elif c == "\\":
            if i + 1 < n and text[i + 1] != "\n":
                buf.append(text[i + 1])
                quoted = True
            i += 2
        elif c in "<>" or text.startswith("&>", i):
            op = next(o for o in REDIRECT_OPS if text.startswith(o, i))
            so_far = "".join(buf)
            fd = so_far if buf and not quoted and so_far.isdigit() else ""
            if not fd:
                flush()
            buf, bare, quoted = [], [], False
            word = Word(fd + op)
            word.op = True
            out.append(word)
            i += len(op)
        else:
            if EXPANDS.match(text, i) or c == "`" or (c == "~" and not buf and not quoted):
                expand[0] = True
            buf.append(c)
            bare.append(c)
            i += 1
    flush()
    return out


def _redirect_scan(words):
    """(words without redirections, input targets, output targets, [reasons the call is uncertain]) of one simple command
    (round 6 rule 2). `< f` reads f; `> f`, `>> f`, `>| f`, `&> f`, `&>> f`, `>& f` and `<> f` write f (a target under
    /dev, such as /dev/null, is no write: stage_writes); `2>&1`, `>&2` and `>&-` duplicate or close a descriptor; a heredoc
    delimiter and a here-string word are data. An operator with no word after it, or `<&` to a file, is outside the grammar.
    A list of plain strings (a record's argv) holds no operator: its `>` is an argument."""
    kept, inputs, outputs, reasons, i = [], [], [], [], 0
    while i < len(words):
        word = words[i]
        if not getattr(word, "op", False):
            kept.append(word)
            i += 1
            continue
        op = word.lstrip("0123456789")
        target = words[i + 1] if i + 1 < len(words) and not getattr(words[i + 1], "op", False) else None
        i += 1 if target is None else 2
        if target is None:
            reasons.append("a redirect with no target (%s)" % word)
        elif op in ("<<", "<<-", "<<<"):
            continue
        elif op in (">&", "<&") and re.match(r"^(\d+|-)$", target):
            continue
        elif op == "<&":
            reasons.append("a redirect the grammar does not model (%s%s)" % (word, target))
        elif op == "<":
            inputs.append(target)
        else:
            outputs.append(target)
            if op == "<>":
                inputs.append(target)
    return kept, inputs, outputs, reasons


# ---- shell command lines: stages, executions, writes (round 3 rule 4) -------------------------------------------------------
def _segments(command):
    """[(text, separator before it)] of a command line split on ; newline && || | & outside quotes. The & of `2>&1` and
    `&>` is a redirection, not a separator. A `#` that starts a word outside quotes starts a comment, which runs to the end
    of the line and is dropped (round 4 rule 5: a comment is never an execution); `;;` (a case arm's end) is one separator,
    given as `;;` (outside a case it is outside the grammar)."""
    out, buf, quote, i, before = [], [], None, 0, None
    while i < len(command):
        c = command[i]
        nxt = command[i + 1] if i + 1 < len(command) else ""
        sep = None
        if quote:
            if c == "\\" and quote == '"':
                buf.append(c)
                i += 1
                c = command[i] if i < len(command) else ""
            elif c == quote:
                quote = None
        elif c == "\\":
            buf.append(c)
            i += 1
            c = command[i] if i < len(command) else ""
        elif c in "'\"":
            quote = c
        elif c == "#" and (not buf or buf[-1] in " \t\n;&|()"):
            end = command.find("\n", i)
            i = len(command) if end < 0 else end
            continue
        elif c in ";\n":
            sep = ";;" if c == ";" and nxt == ";" else ";"
        elif c == "|" and not (buf and buf[-1] == ">"):          # `>|` is a redirect, not a pipe
            sep = "||" if nxt == "|" else "|"
        elif c == "&" and not (buf and buf[-1] in "<>") and nxt != ">":
            sep = "&&" if nxt == "&" else "&"
        if sep:
            out.append(("".join(buf), before))
            buf = []
            before = sep
            i += len(sep)
            continue
        buf.append(c)
        i += 1
    out.append(("".join(buf), before))
    return out


ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
SHELLS = ("sh", "bash", "zsh", "dash")
SHELL_KEYWORDS = ("for", "while", "until", "if", "then", "do", "else", "elif", "case", "function", "done", "fi")
OPAQUE_PROGRAMS = ("xargs", "parallel")     # run words the scorer reads only through _runs_something


# ---- the wrapper table (round 7 rule 1): recognise, never strip -------------------------------------------------------------
# A stage is read only when every word is matched by an entry: leading NAME=value assignments, then any chain of the
# wrappers below, each with exactly the options it lists (an option word and the number of operand words it takes), then
# the program and its arguments. Any other option of a wrapper, a wrapper at another path, a launcher, or a wrapper with
# nothing to run makes the WHOLE call uncertain. The table is the grammar: WRAPPER_TABLE renders it, and score.py's header
# holds the same text (a test keeps them equal).
TEST_RUN = "tools/agent/test-run.sh"
DURATION = re.compile(r"^\d+(\.\d+)?[smhd]?$")
WRAPPERS = {
    # name: (options {word: operand words}, NAME=value words allowed, operand ("duration" after the options, "worktree"
    #        before them, or None), end word that must close the wrapper's own words, or None)
    "env": ({"-i": 0, "-u": 1}, True, None, None),
    "command": ({}, False, None, None),
    "timeout": ({"-s": 1, "-k": 1}, False, "duration", None),
    "nice": ({"-n": 1}, False, None, None),
    "time": ({"-p": 0}, False, None, None),
    "npx": ({"--yes": 0, "-y": 0, "--no-install": 0}, False, None, None),
    TEST_RUN: ({"--timeout": 1, "--label": 1}, False, "worktree", "--"),
}
# Programs that run another command in a way the table does not read: always uncertain.
LAUNCHERS = ("exec", "builtin", "nohup", "sudo", "doas", "stdbuf", "xvfb-run", "caffeinate", "unbuffer", "script", "watch",
             "strace", "ionice", "chrt", "taskset", "setsid", "flock", "cross-env", "dotenv", "bunx", "pnpx", "eval",
             "source", ".")
LAUNCHER_SUBCOMMANDS = {"npm": ("exec", "x"), "pnpm": ("exec", "dlx"), "yarn": ("exec", "dlx"), "uv": ("run",),
                        "poetry": ("run",), "bundle": ("exec",)}
RUNNER_PROGRAMS = ("npm", "pnpm", "yarn", "vitest", "jest", "tsc", "pytest", "eslint", "mypy", "go", "cargo", "make")


def _wrapper_rows():
    rows = []
    for name, (options, assigns, operand, end) in WRAPPERS.items():
        parts = [name] + (["WORKTREE"] if operand == "worktree" else [])
        parts += ["[%s%s]" % (o, " X" * k) for o, k in options.items()] + (["[NAME=value …]"] if assigns else [])
        parts += (["DURATION"] if operand == "duration" else []) + ([end] if end else [])
        rows.append("      " + " ".join(parts) + " CMD …")
    return rows


WRAPPER_TABLE = "\n".join(
    ["  The wrapper table (round 7 rule 1; episode_lib.WRAPPERS). A stage is read only when every word is matched:",
     "    [NAME=value …] then any chain of"] + _wrapper_rows() + [
     "    then the program and its arguments. A wrapper matches only as NAME, /usr/bin/NAME or /bin/NAME (test-run.sh:",
     "    tools/agent/test-run.sh with nothing, ./ or …/ before it); test-run.sh inside test-run.sh is not read; DURATION",
     "    is digits with an optional s, m, h or d. Uncertain (the WHOLE call, and the cwd after it in that shell): any other",
     "    option of a wrapper (`command -v`, `env -S`, `time -o`, `timeout --signal=K`), a wrapper at another path or with",
     "    nothing to run, a launcher (exec, builtin, nohup, sudo, stdbuf, cross-env, bunx …: LAUNCHERS), a launcher",
     "    subcommand (npm exec, pnpm dlx, uv run …), cd/pushd under a wrapper, an assignment-only stage, `$?`, `$(( ))`,",
     "    eval, source and `.`. A word holding an unresolved expansion or glob ($VAR, ${…}, a substitution, a leading ~,",
     "    *.ts) is read but has no known value: a check with one has no identity (rule 2). A check's identity is its argv",
     "    after the wrappers and assignments, as a JSON array, with its cwd."])


def _wrapper_name(word):
    """The WRAPPERS key a program word names, None when it names none; "" when its basename is a wrapper's at a path the
    table does not hold (then the stage is uncertain)."""
    for name in WRAPPERS:
        if name == TEST_RUN:
            if word in (TEST_RUN, "./" + TEST_RUN) or word.endswith("/" + TEST_RUN):
                return name
            if os.path.basename(word) == "test-run.sh":
                return ""
        elif word in (name, "/usr/bin/" + name, "/bin/" + name):
            return name
        elif os.path.basename(word) == name and "/" in word:
            return ""
    return None


def classify(words, literal=False):
    """({words: [program by basename, args…], program, via: "test-run.sh" or None, wt: its WORKTREE, chain: [wrappers]},
    None) when every word of one simple command (redirects removed) is matched by the wrapper table (WRAPPER_TABLE);
    (None, reason) otherwise. literal: a record's argv, exec'd with no shell: a NAME=value word is no assignment."""
    i, n, chain, via, worktree = 0, len(words), [], None, None
    while i < n and ASSIGN.match(words[i]):
        if literal:
            return None, "an assignment word as the program of an exec'd argv"
        i += 1
    if i == n:
        return None, "an assignment-only stage"
    while i < n:
        name = _wrapper_name(words[i])
        if name == "":
            return None, "`%s` (a wrapper at a path the table does not hold)" % words[i]
        if name is None:
            break
        if name == TEST_RUN and via:
            return None, "test-run.sh inside test-run.sh"
        label = "test-run.sh" if name == TEST_RUN else name
        options, assigns, operand, end = WRAPPERS[name]
        start, i = i, i + 1
        if operand == "worktree":
            if i >= n or words[i].startswith("-"):
                return None, "test-run.sh with no WORKTREE"
            worktree, i = words[i], i + 1
        closed = False
        while i < n:
            word = words[i]
            if assigns and ASSIGN.match(word):
                i += 1
            elif word in options:
                if i + options[word] >= n:
                    return None, "`%s %s` with no operand" % (label, word)
                i += 1 + options[word]
            elif end and word == end:
                i, closed = i + 1, True
                break
            elif word.startswith("-") and word != "-":
                return None, "`%s %s` (an option the wrapper table does not hold)" % (label, word)
            else:
                break
        if end and not closed:
            return None, "%s with no `%s` before its command" % (label, end)
        if operand == "duration":
            if i >= n or not DURATION.match(words[i]):
                return None, "timeout with no duration"
            i += 1
        chain.append(label)
        via = via or ("test-run.sh" if name == TEST_RUN else None)
        if i >= n:
            if end or operand == "duration" or len(chain) > 1:
                return None, "%s with nothing to run" % label
            # a wrapper alone (`env`, `time`): it runs nothing else
            return {"words": [label] + list(words[start + 1:]), "program": words[start], "via": None, "wt": None,
                    "chain": []}, None
    program, args = words[i], list(words[i + 1:])
    base = os.path.basename(program)
    if base in ("eval", "source", "."):
        return None, base
    if base in LAUNCHERS:
        return None, "`%s` (a launcher the table does not read)" % base
    if args and args[0] in LAUNCHER_SUBCOMMANDS.get(base, ()):
        return None, "`%s %s` (a launcher the table does not read)" % (base, args[0])
    if chain and base in ("cd", "pushd", "popd"):
        return None, "%s under a wrapper" % base
    if chain and ASSIGN.match(program):
        return None, "an assignment word after a wrapper"
    return {"words": [base] + args, "program": program, "via": via, "wt": worktree, "chain": chain}, None


def stage_runs(stages, exit=None, blocked=False):
    """[(ran, exit)] per stage of one call (round 4 rule 5). ran True: the stage certainly ran (the start of an and-or list,
    a pipeline member, or a stage after && in the call's last list when every link of that list is && and the call exited
    0); None: uncertain (after ||, inside if/case/a loop, after && with a nonzero or unknown exit, or the call's result says
    it may never have run). exit: the stage's exit status when the call's status gives it, else None. Shell is never run."""
    if not stages:
        return []
    lists, current = [], []
    for index, stage in enumerate(stages):
        if index and stage["sep"] in (None, ";", "&"):
            lists.append(current)
            current = []
        current.append(index)
    lists.append(current)
    out = [(None, None)] * len(stages)
    for number, members in enumerate(lists):
        last_list = number == len(lists) - 1
        links = [stages[i]["sep"] for i in members[1:] if stages[i]["sep"] != "|"]
        all_and = all(s == "&&" for s in links)
        failed_link = False
        for position, i in enumerate(members):
            stage = stages[i]
            sep = stage["sep"] if position else None
            if sep in ("&&", "||"):
                failed_link = failed_link or sep == "||" or not (last_list and all_and and exit == 0)
            ran = None if (blocked or stage["cond"] or failed_link) else True
            ends_pipeline = i == members[-1] or stages[i + 1]["sep"] != "|"
            code = None
            if ran and ends_pipeline and last_list:
                if i == members[-1]:
                    code = exit
                elif all_and and exit == 0:
                    code = 0
            out[i] = (ran, code)
    return out


GIT_WRITES = ("checkout", "switch", "merge", "rebase", "reset", "apply", "stash", "cherry-pick", "pull", "restore", "am", "revert",
              "mv", "rm", "clean")
FILE_WRITERS = ("mv", "cp", "rm", "touch", "tee", "patch", "truncate", "ln", "rsync", "dd")


def _argv(words):
    return json.dumps([str(w) for w in words])


def check_identity(words):
    """The verification command a simple command runs, as a JSON array of its words (round 7 rule 2: `npm test -- "a b"`
    is ["npm","test","--","a b"], never the same check as `npm test -- a b`), or None. Distinct targets are distinct
    commands. `npm run test` and `npm t` are `npm test`; `python -m pytest` is `pytest`. The caller decides whether the
    words have known values (stage_check)."""
    if not words:
        return None
    program, args = os.path.basename(words[0]), list(words[1:])
    if program in ("npm", "pnpm", "yarn"):
        if args[:1] in (["test"], ["t"], ["tst"]):
            return _argv(["npm", "test"] + args[1:])
        if program == "yarn" and args[:1] and args[0] in ("build", "typecheck", "lint"):
            args = ["run"] + args
        if args[:2] == ["run", "test"]:
            return _argv(["npm", "test"] + args[2:])
        if args[:1] == ["run"] and len(args) > 1 and re.match(r"^(build|typecheck|type-check|lint|check|test:.*|e2e)$", args[1]):
            return _argv(["npm"] + args)
        return None
    if program in ("vitest", "jest", "tsc", "pytest", "eslint", "mypy"):
        return _argv([program] + args)
    if re.match(r"^python[0-9.]*$", program) and args[:1] == ["-m"] and len(args) > 1 and args[1] in ("unittest", "pytest"):
        return _argv((["pytest"] if args[1] == "pytest" else ["python", "-m", "unittest"]) + args[2:])
    if program in ("go", "cargo") and args[:1] in (["test"], ["build"], ["vet"]):
        return _argv([program] + args)
    if program == "make" and args[:1] and args[0] in ("test", "check", "build"):
        return _argv([program] + args)
    if program == "git" and "diff" in args and "--check" in args:
        return _argv([program] + args)
    return None


def show_identity(identity):
    """A check identity as display text (its words shell-quoted, so `"a b"` and `a b` read differently); text that is not a
    JSON array is shown as it is."""
    try:
        words = json.loads(identity)
    except (TypeError, ValueError):
        return identity
    return shlex.join(str(w) for w in words) if isinstance(words, list) else identity


def stage_check(stage):
    """(identity, doubt) of one read stage (round 7 rule 2). identity: check_identity of its argv when every word has a
    known value. doubt: why the stage may be a check whose identity is unknown: a word holds an unresolved expansion or
    glob and the program is a runner (or the words already make a check). Such a run never clears or repeats another."""
    words = stage["words"]
    identity = check_identity(words)
    if not stage.get("opaque"):
        return identity, None
    program = words[0]
    if (identity or program in RUNNER_PROGRAMS or re.match(r"^python[0-9.]*$", program)
            or (program == "git" and ("diff" in words or any(unresolved(w) for w in words[1:2])))):
        return None, "a check whose words hold an unresolved expansion or glob (%s)" % " ".join(words)[:60]
    return None, None


def _git_sub(args):
    index = 0
    while index < len(args) and args[index].startswith("-"):
        index += 2 if args[index] in ("-C", "-c", "--git-dir", "--work-tree") else 1
    return args[index] if index < len(args) else None


def stage_writes(stage):
    """Whether a simple command changes the working tree's content. `git add`/`git commit` do not (same tree)."""
    if any(not _norm(t if t.startswith("/") else "/x/" + t).startswith("/dev/") for t in stage["outputs"]):
        return True
    words = stage["words"]
    program, args = words[0], words[1:]
    if program in FILE_WRITERS:
        return True
    if program == "sed" and any(a == "-i" or a.startswith("--in-place") or re.match(r"^-[a-zA-Z]*i", a) for a in args):
        return True
    if program == "perl" and any(re.match(r"^-[a-zA-Z]*i", a) for a in args):
        return True
    if program == "git":
        return _git_sub(args) in GIT_WRITES
    if program in ("npm", "pnpm", "yarn") and args[:1] and args[0] in ("install", "i", "ci", "add", "uninstall", "remove"):
        return True
    return False


def command_events(command):
    """[(kind, text)] in execution order for one Bash call (or one test-run record's cmd list), read by grammar_call:
    ("write", stage text) for a stage that changes files (a redirect is a write whatever the command, `cd . > a.txt`
    included), ("check", display text of its identity) for a verification runner, ("doubt", why) for a check whose
    identity is unknown, and one ("uncertain", reason) for a call outside the grammar. A stage that both writes and checks
    records the write first. Text inside arguments (`grep 'npm run build' README.md`) is never an execution."""
    stages, why = grammar_call(command)
    if why:
        return [("uncertain", why)]
    out = []
    for stage in stages:
        if stage_writes(stage):
            out.append(("write", " ".join(stage["words"])))
        if stage["cd"] is not None:
            continue
        identity, doubt = stage_check(stage)
        if identity:
            out.append(("check", show_identity(identity)))
        elif doubt:
            out.append(("doubt", doubt))
    return out


def executions(command):
    """The executed commands of a Bash call (or a cmd list) inside the grammar, one text per stage: the check identity's
    display text for a verification runner, else the program and its arguments (wrappers, env assignments and redirections
    are not part of it). `cd` is not one. An uncertain call has none it can name: []."""
    stages, why = grammar_call(command)
    return [] if why else [show_identity(check_identity(s["words"])) if check_identity(s["words"]) and not s["opaque"]
                           else " ".join(s["words"]) for s in stages if s["cd"] is None]


def record_command(record):
    """The command a tp-test-run:v1 record ran: its `cmd` (a label never hides it), else its label as a command line."""
    cmd = record.get("cmd")
    if isinstance(cmd, list) and cmd:
        return [str(w) for w in cmd]
    return record.get("label") or ""


# ---- the stated grammar (round 5 rule 1) ------------------------------------------------------------------------------------
GRAMMAR = """A Bash call is read only if, after comment stripping (and heredoc bodies removed), it is a list of simple
commands joined by newline, `;` or `&&`. Each command is optional env assignments, a word list and optional redirects,
optionally followed by a pipeline (`|`) whose first stage is the command; every pipeline stage is itself a simple command.
Words are split quote-aware and every redirect operator is its own token (`changed>a.txt` writes a.txt). A redirect to a
file (`>`, `>>`, `>|`, `&>`, `&>>`, `>& f`, `<>`, `2> f`, a bare `> f`, `X=1 > f`) is a write at its target, resolved
against the stage's cwd; `2>&1`, `>&2`, `>&-` and a target under /dev (`>/dev/null`) are not writes; `< f` is a read; a
heredoc delimiter and a `<<<` word are data. A heredoc body is data only when its delimiter is quoted (`<<'EOF'`, `<<\EOF`)
or it holds no `$` or backtick. Each simple command is classified word by word against the wrapper table (WRAPPER_TABLE,
round 7 rule 1): it is read only when every word is matched; a test-run.sh call under any chain of table wrappers is
counted and runs in its WORKTREE (resolved against the stage's cwd), and a redirect on it writes at the outer cwd. `cd
DIR` as its own command (not in a pipeline, not under a wrapper) changes the cwd of the following commands of that call
only when it ran and its exit is known to be 0: a known nonzero exit leaves the cwd where it was, an unknown exit leaves it
unknown; a redirect on it is a write at the cwd before it. Anything else makes the WHOLE call uncertain: what the table
does not match, `||`, background `&`, `;;` outside a case, a list ending in `&&` or `|`, if/case/loops/select/functions/
`!`/`[[`, `( … )`, `{ … }`, process substitution, `sh -c`/`bash -c`, a shell reading its stdin (`bash`, `sh -s`,
`… | sh`), `$( … )` or backticks containing a runner or a write, `$(( ))`, `$?`, an unquoted heredoc body holding `$` or
a backtick, xargs/parallel/`find -exec` running a runner, a write or a shell (or `find -delete`), a program word that is
a variable or a glob, a brace expansion, a redirect with no target or `<&` to a file, pushd/popd, cd in a pipeline,
unbalanced quotes or parentheses. An uncertain call is never read in part: it may have run any command and written
anything, so it is one marker (a write of unknown place that may have run, no execution of its own), and the cwd after
it in the same shell is unknown; a check or measure over an interval that holds one is unknown (score.py's gate). A word
holding an unresolved expansion or glob has no known value: a check whose words hold one has no identity and is a doubt
(round 7 rule 2). Known limit: a program outside the writer list (stage_writes) is taken not to write."""
GRAMMAR_KEYWORDS = ("if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done", "case", "esac", "select",
                    "function", "!", "[[", "]]", "in", "coproc")
SUB = "$__SUB"                     # a $( … ) or backtick substitution, replaced before the call is split


HEREDOC_OP = re.compile(r"(?<!<)<<(-?)[ \t]*(\\?)(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\3")


def _heredocs(text):
    """(text without heredoc bodies, unterminated, expanding). Each `<<WORD` (`<<-WORD`, quoted `'WORD'`/`"WORD"`/`\WORD`)
    takes the following lines up to a line that is WORD (tabs stripped for <<-); the command line and everything after the
    delimiter stay. expanding (round 6 rule 2): an unquoted delimiter's body holds `$` or a backtick, so the shell expands
    it and a `$( … )` in it runs; a quoted delimiter's body is data. lib/routines' HEREDOC drops the rest of the call from
    `<<` on, commands after the heredoc included, so it is not used here."""
    out, pending, expanding = [], [], False
    for line in text.split("\n"):
        if pending:
            dash, word, quoted = pending[0]
            if (line.lstrip("\t") if dash else line) == word:
                pending.pop(0)
            elif not quoted and ("$" in line or "`" in line):
                expanding = True
            continue
        out.append(line)
        pending.extend((m.group(1), m.group(4), bool(m.group(2) or m.group(3))) for m in HEREDOC_OP.finditer(line))
    return "\n".join(out), bool(pending), expanding


def _close_paren(text, start):
    """Index of the `)` matching the `(` at text[start], quotes respected; None when unbalanced."""
    depth, quote, i = 0, None, start
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 1
            elif c == quote:
                quote = None
        elif c == "\\":
            i += 1
        elif c in "'\"":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _structure(text):
    """(text with comments dropped and every $( … ) / backtick substitution replaced by SUB, [substitution texts],
    [reasons the call is outside the grammar]). Single quotes hide everything; double quotes hide all but substitutions."""
    out, subs, reasons = [], [], []
    quote, i, n, depth = None, 0, len(text), 0

    def last():
        return out[-1][-1:] if out else ""

    while i < n:
        c = text[i]
        if quote == "'":
            out.append(c)
            quote = None if c == "'" else quote
            i += 1
            continue
        if c == "\\":
            out.append(text[i:i + 2])
            i += 2
            continue
        if c == "$" and (text.startswith("$?", i) or text.startswith("${?}", i)):
            reasons.append("`$?`")                    # round 7 rule 1: an exit status read back
        if c == "$" and text.startswith("$((", i):
            end = _close_paren(text, i + 1)
            if end is None:
                reasons.append("unbalanced parentheses")
                break
            reasons.append("`$(( ))` arithmetic")     # round 7 rule 1: it may hold a substitution, and its value is not read
            out.append(SUB)
            i = end + 1
            continue
        if c == "$" and text.startswith("$(", i):
            end = _close_paren(text, i + 1)
            if end is None:
                reasons.append("unbalanced parentheses")
                break
            subs.append(text[i + 2:end])
            out.append(SUB)
            i = end + 1
            continue
        if c == "`":
            end = i + 1
            while end < n and text[end] != "`":
                end += 2 if text[end] == "\\" else 1
            if end >= n:
                reasons.append("unbalanced backtick")
                break
            subs.append(text[i + 1:end])
            out.append(SUB)
            i = end + 1
            continue
        if quote == '"':
            out.append(c)
            quote = None if c == '"' else quote
            i += 1
            continue
        if c in "'\"":
            quote = c
        elif c == "#" and (not out or last() in " \t\n;&|()"):
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        elif c == "(":
            if last() in ("<", ">"):
                reasons.append("process substitution")
            elif text.startswith("()", i) and last() and last() not in " \t\n;&|":
                reasons.append("a function definition")
            else:
                reasons.append("( … ) subshell")
            depth += 1
        elif c == ")":
            if depth:
                depth -= 1                            # closes a ( already given as the reason
            else:
                reasons.append("a case pattern `)`")
        elif c in "{}" and (not out or last() in " \t\n;&|") and (i + 1 == n or text[i + 1] in " \t\n;&|"):
            reasons.append("{ … } group")
        out.append(c)
        i += 1
    if quote:
        reasons.append("unbalanced quotes")
    return "".join(out), subs, list(dict.fromkeys(reasons))


def _program_args(words, takes=()):
    """The command after a runner's own options (xargs -n 1 -I {} CMD ...): words from the first non-option."""
    i = 0
    while i < len(words) and words[i].startswith("-"):
        i += 2 if words[i] in takes else 1
    return words[i:]


def _runs_something(words):
    """Whether a word list (an xargs/parallel/find -exec target) may run a verification runner, write, or start a shell:
    read by the wrapper table (round 7 rule 1). Words the table does not match may run anything: True."""
    if not words:
        return False
    info, why = classify(words)
    if why:
        return True
    inner = info["words"]
    stage = {"words": inner, "outputs": [], "opaque": any(unresolved(w) for w in words)}
    return bool(inner[0] in SHELLS or any(stage_check(stage)) or inner[0] in RUNNER_PROGRAMS or stage_writes(stage))


def _grammar_stage(words, sep, next_sep, literal=False):
    """(stage or None, reason or None) for one simple command of a call inside the grammar, its words classified by the
    wrapper table (round 7 rule 1). A stage of only redirects (`> a.txt`) runs `:` with them: its output target is a
    write; a stage of only assignments (`X=1`, `X=1 > a.txt`) is uncertain. stage["opaque"]: a word of its argv holds an
    unresolved expansion or glob (round 7 rule 2)."""
    if words[0] in GRAMMAR_KEYWORDS:
        return None, "if/case/loop/function/`!`/`[[` (%s)" % words[0]
    kept, inputs, outputs, bad = _redirect_scan(words)
    if bad:
        return None, "; ".join(bad)
    unquoted = " ".join(getattr(w, "bare", "") for w in kept)
    if re.search(r"\{[^{}\s]*(,|\.\.)[^{}\s]*\}", unquoted):
        return None, "a brace expansion"
    if not kept:
        info = {"words": [":"], "program": ":", "via": None, "wt": None, "chain": []}
    else:
        if unresolved(kept[0]) and not ASSIGN.match(kept[0]):
            return None, ("a program word that is a variable" if getattr(kept[0], "expand", False)
                          else "a glob in the program word")
        info, why = classify(kept, literal)
        if why:
            return None, why
    program, args = info["words"][0], info["words"][1:]
    if unresolved(info["program"]):
        return None, ("a program word that is a variable" if getattr(info["program"], "expand", False)
                      else "a glob in the program word")
    if program in SHELLS and any(re.match(r"^-[a-zA-Z]*c", a) for a in args if a.startswith("-") and not a.startswith("--")):
        return None, "sh -c"
    if program in SHELLS and (all(a.startswith("-") for a in args) or "-s" in args):
        return None, "a shell reading its stdin"
    if program in ("xargs", "parallel") and _runs_something(_program_args(args, ("-n", "-I", "-L", "-P", "-d", "-E", "-s", "-j"))):
        return None, "%s running a runner, a write or a shell" % program
    if program == "find":
        if "-delete" in args:
            return None, "find -delete"
        for flag in ("-exec", "-execdir", "-ok", "-okdir"):
            if flag in args and _runs_something(args[args.index(flag) + 1:]):
                return None, "find %s running a runner, a write or a shell" % flag
    if program in ("pushd", "popd"):
        return None, program
    if program == "cd" and "|" in (sep, next_sep):
        return None, "cd in a pipeline"
    return {"words": [program] + args, "inputs": inputs, "outputs": outputs, "piped": sep == "|", "sep": sep, "cond": False,
            "cd": (args[0] if args else "~") if program == "cd" else None, "sub": 0, "via": info["via"], "wt": info["wt"],
            "chain": info["chain"], "opaque": any(unresolved(w) for w in args)}, None


def grammar_call(command):
    """(stages, reason) for one Bash call (round 5 rule 1, round 6 rule 2, round 7 rule 1). reason None: the call is inside
    the grammar (GRAMMAR, WRAPPER_TABLE) and stages are its simple commands in order (sep: None ; && |). reason text: the
    whole call is uncertain and stages is [] (round 7: recognise, never strip; an uncertain call is never read in part). A
    list of words (a record's cmd, exec'd with no shell) is one simple command whose words are literal."""
    if isinstance(command, (list, tuple)):
        words = [str(w) for w in command]
        if not words:
            return [], None
        stage, why = _grammar_stage(words, None, None, literal=True)
        return ([stage] if stage else [], None) if why is None else ([], why)
    if not isinstance(command, str):
        return [], None
    text, unterminated, expanding = _heredocs(command)
    flat, subs, reasons = _structure(text)
    if unterminated:
        reasons.append("an unterminated heredoc")
    if expanding:
        reasons.append("an unquoted heredoc whose body holds $ or a backtick")
    for inner in subs:
        inner_stages, inner_why = grammar_call(inner)
        if inner_why or any(any(stage_check(s)) or stage_writes(s) for s in inner_stages):
            reasons.append("$( … ) containing a runner or a write")
    stages, segments, pending = [], _segments(flat) if not reasons else [], None
    for k, (text, sep) in enumerate(segments):
        if sep in ("||", "&", ";;"):
            reasons.append({"||": "||", "&": "background &", ";;": "`;;` outside a case"}[sep])
            continue
        if pending and sep in (";", None):
            sep, pending = pending, None              # `a &&\n b`: the line break after && or | continues the list
        try:
            words = _tokens(text)
        except ValueError:
            reasons.append("unbalanced quotes")
            continue
        if not words:
            if sep in ("&&", "|"):
                pending = sep
            continue
        stage, why = _grammar_stage(words, sep, segments[k + 1][1] if k + 1 < len(segments) else None)
        if why:
            reasons.append(why)
        elif stage:
            stages.append(stage)
    if pending:
        reasons.append("a list ending in %s" % pending)
    if reasons:
        return [], "; ".join(dict.fromkeys(reasons))
    return stages, None


def call_stages(command, exit=None, blocked=False):
    """(stages, [(ran, exit)] per stage, reason) for one Bash call under the grammar: an uncertain call (reason set) has no
    stages."""
    stages, reason = grammar_call(command)
    if reason:
        return [], [], reason
    return stages, stage_runs(stages, exit, blocked), None


def record_shape(record):
    """(stage, identity, reason) of a tp-test-run:v1 record (round 5 rule 3): its exit backs a verification only when its
    cmd is one simple command inside the grammar, with no shell wrapper; identity is its check identity, else its argv as
    a JSON array (round 7 rule 2: a record's cmd is literal argv, exec'd with no shell). A shell (`sh -c …`), a compound
    list, a cd, or nothing to run: (None, None, reason): an uncertain execution."""
    stages, why = grammar_call(record_command(record))
    if why:
        return None, None, "the record's cmd is outside the grammar (%s): its exit is the shell's" % why
    if len(stages) != 1:
        return None, None, "the record's cmd is %s: its exit is the last command's" % (
            "a compound list" if stages else "empty")
    stage = stages[0]
    if stage["cd"] is not None:
        return None, None, "the record's cmd is a cd"
    return stage, check_identity(stage["words"]) or _argv(stage["words"]), None


READ_WORD = re.compile(r"[A-Za-z0-9_./-]+")


def shell_read_scan(command, cwd=None, project_dirs=(), private_dirs=()):
    """(programs, uncounted) for one Bash command line; diagnostic only. programs: one SHELL_READERS entry per simple command
    that reads a file path operand (or a -f/--file operand, or an input redirect) inside a project dir and outside the
    private dirs, resolved against the cwd as `cd` stages change it. With no path operand rg and grep -r search the cwd unless
    their stdin is a pipe (then they read the pipe, not a file), find searches the cwd, the others read stdin. uncounted:
    reader stages the scorer cannot follow: every reader word of an uncertain call (loops, eval, a substitution running a
    runner), a reader whose words hold $VAR or a substitution, xargs/parallel running a reader, a relative path after a cd
    it cannot resolve. Read by grammar_call (round 7 rule 1)."""
    if not isinstance(command, str):
        return [], 0
    stages, why = grammar_call(command)
    if why:
        return [], sum(1 for w in READ_WORD.findall(command) if os.path.basename(w) in SHELL_READERS)
    projects = [_norm(d) for d in project_dirs if d]
    here = _norm(cwd) if cwd else (projects[0] if projects else None)
    found, uncounted = [], 0
    for stage in stages:
        words = stage["words"]
        if stage["cd"] is not None:
            target = stage["cd"]
            if target.startswith("/"):
                here = _norm(target)
            elif target.startswith("~") or target == "-" or "$" in target or here is None:
                here = None
            else:
                here = _norm(os.path.join(here, target))
            continue
        program = words[0]
        if program not in SHELL_READERS:
            if program in OPAQUE_PROGRAMS + ("find",) and any(os.path.basename(w) in SHELL_READERS for w in words[1:]):
                uncounted += 1                     # e.g. `xargs grep x`, `find . -exec cat {} ;`
            continue
        if stage["opaque"] or any(unresolved(t) for t in stage["inputs"]):
            uncounted += 1
            continue
        paths, recursive, files = _operands(program, words[1:])
        if not paths and program == "find":
            paths = ["."]
        elif not paths and (program == "rg" or recursive) and not stage["piped"]:
            paths = ["."]
        paths = paths + files + stage["inputs"]
        hit = False
        for path in paths:
            if path.startswith("~"):
                continue                                   # home: not a project path
            if here is None and not path.startswith("/"):
                uncounted += 1
                break
            full = _norm(path if path.startswith("/") else os.path.join(here, path))
            if any(_under(full, d) for d in private_dirs if d):
                continue
            if any(_under(full, d) for d in projects):
                hit = True
                break
        if hit:
            found.append(program)
    return found, uncounted


def shell_readers(command, cwd=None, project_dirs=(), private_dirs=()):
    """The shell programs in a Bash command line that read project files (shell_read_scan's first part). Diagnostic only."""
    return shell_read_scan(command, cwd, project_dirs, private_dirs)[0]


# ---- runner log -----------------------------------------------------------------------------------------------------------
def read_log(run_dir):
    return read_jsonl(Path(run_dir) / "run" / "episode-log.jsonl") or []
