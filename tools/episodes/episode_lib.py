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
import routines  # noqa: E402  (shell command splitting shared with lib/routines.py)

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
EXPECTABLE_MEASURES = ("completed", "recurrence", "leak", "recovery", "avoidable_waiting", "false_claims", "irrelevant_checks",
                       "verification", "safe_tools", "into_prompt")
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
            "review_of": _text(task_dir / "review-of")}


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
    out = []
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
            elif kind == "tool_use":
                out.append({"at": at, "kind": "tool", "name": part.get("name") or "", "input": part.get("input") or {}})
            elif kind == "toolCall":
                out.append({"at": at, "kind": "tool", "name": part.get("name") or "", "input": part.get("arguments") or {}})
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
    """(paths, script_given, recursive) of one simple command's argument words."""
    takes = OPTION_ARGS.get(program, ())
    operands, script_given, recursive = [], False, False
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
            if name in takes and "=" not in word:
                index += 1
        elif word.startswith("-") and len(word) > 1:
            flags = word[1:]
            if program in ("grep", "egrep", "fgrep") and not word[:2] in takes:
                recursive = recursive or "r" in flags or "R" in flags
            for n, flag in enumerate(flags):          # -A3, -e PAT, -rnA 3: the first flag that takes a value ends the cluster
                if "-" + flag in takes:
                    script_given = script_given or "-" + flag in SCRIPT_OPTIONS
                    if n == len(flags) - 1:
                        index += 1
                    break
        else:
            operands.append(word)
        index += 1
    if program in PATTERN_FIRST and not script_given and operands:
        operands = operands[1:]
    return operands, recursive


REDIRECT = re.compile(r"^\d*(<|>>|>|&>)(&?\d*-?)?(.*)$")


def _redirects(words):
    """(words without redirections, input-redirect targets). `< f` / `<f` is a read of f; `> f`, `2>&1`, `&>f` are not."""
    kept, inputs, index = [], [], 0
    while index < len(words):
        match = REDIRECT.match(words[index])
        if not match:
            kept.append(words[index])
            index += 1
            continue
        target = match.group(3)
        if not target and not match.group(2) and index + 1 < len(words):
            index += 1
            target = words[index]
        if match.group(1) == "<" and target:
            inputs.append(target)
        index += 1
    return kept, inputs


def shell_readers(command, cwd=None, project_dirs=(), private_dirs=()):
    """The shell programs (SHELL_READERS) in a Bash command line that read project files: one entry per simple command that reads
    at least one path inside a project dir (the scratch clone and its lane worktrees) that is not inside a private dir (the
    harness's own task/state/run dirs). A relative path is under `cwd` (default the first project dir). With no path operand:
    rg, find and grep -r search `cwd` (a project read); the others read stdin (a pipe stage), which is not a file read."""
    if not isinstance(command, str):
        return []
    projects = [_norm(d) for d in project_dirs if d]
    base = cwd or (projects[0] if projects else None)
    found = []
    for _start, _end, tokens in routines._simple_commands(routines.HEREDOC.sub("", command)):
        words = [w for w in tokens if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w)]
        if not words:
            continue
        program = os.path.basename(words[0])
        if program not in SHELL_READERS:
            continue
        args, redirected = _redirects(words[1:])
        paths, recursive = _operands(program, args)
        if not paths and (program in ("rg", "find") or recursive):
            paths = ["."]
        paths = paths + redirected                      # `cmd < file` reads the file; output redirects read nothing
        for path in paths:
            if path.startswith("~") or base is None and not path.startswith("/"):
                continue                                   # home or no known cwd: not a project path
            full = _norm(path if path.startswith("/") else os.path.join(base, path))
            if any(_under(full, d) for d in private_dirs if d):
                continue
            if any(_under(full, d) for d in projects):
                found.append(program)
                break
    return found


TEST_RE = re.compile(r"\b(npm (?:run )?test|npx vitest|vitest(?: run)?|npm run build|npx tsc|tsc -b|pytest|python3? -m unittest|"
                     r"go test|cargo test|git diff --check)\b")
# A shell command that changes the working tree's content. `git commit`/`git add` do not (a commit of a tested tree is the same tree).
WRITE_RE = re.compile(r"(\bsed -i\b|>>?\s*[\w./~-]|\bgit (?:checkout|switch|merge|rebase|reset|apply|stash|cherry-pick|pull|restore|am)\b|"
                      r"\bnpm (?:install|ci)\b|\b(?:mv|cp|rm|touch|tee|patch)\b)")


def check_commands(command):
    """The verification commands named in a Bash command line, normalised (`npm run test` == `npm test`)."""
    if not isinstance(command, str):
        return []
    return [re.sub(r"^npm run test$", "npm test", re.sub(r"\s+", " ", m.group(1))) for m in TEST_RE.finditer(command)]


# ---- runner log -----------------------------------------------------------------------------------------------------------
def read_log(run_dir):
    return read_jsonl(Path(run_dir) / "run" / "episode-log.jsonl") or []
