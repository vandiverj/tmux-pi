"""Transcript scanners for the Qwen trial: rule slips and gate runs, as counts and rule ids only.

A scan never keeps command text or transcript text. It reports which worker-preamble rule a tool call broke (the rule
id), and which gate (test, lint, tsc, prettier, gitleaks) a Bash call ran with the exit status when the transcript shows it.

Claude transcripts are JSONL: assistant lines hold `tool_use` blocks, user lines hold the matching `tool_result`. Pi
transcripts (`transcript.jsonl` in the task dir) have no pinned schema yet: the design waits for a Phase-0 sample, so
`scan_pi` returns `status: "unknown"` with null `rule_slips` and `gate_runs` instead of guessing.
"""

import json
import re
from pathlib import Path

MAX_LINE_BYTES = 8 * 1024 * 1024
MAX_TRANSCRIPT_BYTES = 512 * 1024 * 1024

# Rule ids, one per preamble rule, with a description. SLIP_RULES is the list the tests walk: each id has a positive and
# a negative case in tests/test_trial_scan.py. Matching is over one Bash command string.
_GIT = r"\bgit(?:\s+(?:-[Cc]\s+\S+|--\S+))*\s+"
COMMAND_RULES = (
    ("heredoc", "heredoc in a Bash command", re.compile(r"<<(?!<)-?\s*['\"\\]?\w")),
    ("inline_script", "python3 -c / node -e / deno eval",
     re.compile(r"\bpython[\d.]*\s+(?:-\w+\s+)*-c\b|\bnode\s+(?:-\w+\s+)*(?:-e|--eval)\b|\bdeno\s+eval\b")),
    ("command_substitution", "$(...) in a Bash command", re.compile(r"\$\((?!\()")),
    ("repo_root_search", "rg or recursive grep instead of git -C <wt> grep",
     re.compile(r"(?:^|[;&|(]\s*|\bxargs\s+)rg\s|(?:^|[;&|(]\s*)grep\s+(?:-\w*[rR]\w*|--recursive)\b")),
    ("env_read", "reading a .env* file",
     re.compile(r"(?<![\w.-])\.env(?!\.(?:example|sample|template)\b)(?:\.[\w.-]+)?(?![\w-])")),
    ("npm_install", "npm ci / npm install", re.compile(r"\bnpm\s+(?:--?\S+\s+)*(?:ci|install|i)(?=\s|$|;|&|\|)")),
    ("mise_trust", "mise trust", re.compile(r"\bmise\s+trust\b")),
    ("git_push", "git push", re.compile(_GIT + r"push\b")),
    ("git_amend", "git commit --amend", re.compile(_GIT + r"commit\b[^|;&]*--amend\b")),
    ("git_rebase", "git rebase", re.compile(_GIT + r"rebase\b")),
    ("git_reset", "git reset", re.compile(_GIT + r"reset\b")),
    ("git_cherry_pick", "git cherry-pick", re.compile(_GIT + r"cherry-pick\b")),
)
# Rules that read a file write, not a command: the content a Write/Edit puts in a test file.
FIXED_WAIT = re.compile(r"\bsetTimeout\s*\(|\bwaitForTimeout\s*\(|\btime\.sleep\s*\(|(?<![\w.])sleep\s+\d|\bsleep\s*\(")
TIMER_OK = "timer-ok"
TEST_PATH = re.compile(r"(?:^|/)(?:tests?|__tests__|e2e)/|(?:^|/)test_[^/]*$|[._-]test\.\w+$|\.spec\.\w+$")
ENV_PATH = re.compile(r"(?:^|/)\.env(?!\.(?:example|sample|template)$)(?:\.[\w.-]+)?$")
# airtera-monorepo commit subjects are "DMP-NN: <imperative>" and end with a Co-Authored-By trailer
COMMIT_SUBJECT = re.compile(r"^DMP-\d+: \S")
COMMIT_TRAILER = re.compile(r"(?im)^Co-Authored-By:[ \t]*\S.*<[^<>\s]+@[^<>\s]+>[ \t]*$")
THINK_TAG = re.compile(r"</?think(?:ing)?\b[^>]*>", re.I)
# The Claude tool_result a refused tool call comes back as. Markers are lower-case substrings of an is_error result.
GUARD_MARKERS = ("denied this tool call", "has been denied", "hook blocked", "blocking error", "was blocked by")

SLIP_RULES = tuple(rule for rule, _, _ in COMMAND_RULES) + (
    "fixed_wait", "commit_subject", "commit_trailer", "guard_denial", "think_leak")

# Gate commands, matched at the start of one shell segment (after `cd x &&`, env assignments, npx, timeout).
GATES = (
    ("gitleaks", re.compile(r"gitleaks\b")),
    ("prettier", re.compile(r"(?:prettier\b|(?:npm|pnpm|yarn)\s+(?:run\s+)?(?:format|prettier)\b)")),
    ("tsc", re.compile(r"(?:tsc\b|deno\s+check\b|(?:npm|pnpm|yarn)\s+(?:run\s+)?(?:typecheck|tsc|type-check)\b)")),
    ("lint", re.compile(r"(?:eslint\b|deno\s+lint\b|ruff\b|flake8\b|(?:npm|pnpm|yarn)\s+(?:run\s+)?lint\b)")),
    ("test", re.compile(r"(?:python[\d.]*\s+(?:-\w+\s+)*-m\s+(?:unittest|pytest)\b|python[\d.]*\s+\S*run_parallel\.py\b|pytest\b|"
                        r"deno\s+test\b|node\s+--test\b|vitest\b|jest\b|(?:npm|pnpm|yarn)\s+(?:run\s+)?test\b)")),
)
_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|\n]")
_PREFIX = re.compile(r"^\s*(?:(?:\w+=\S*|env|npx|timeout\s+\d+\S*|time|command)\s+)*")
ARITHMETIC = re.compile(r"\$\(\(.*?\)\)", re.S)
_EXIT = re.compile(r"^\s*Exit code[: ]+(-?\d+)")


def command_slips(command):
    """Rule ids a single Bash command breaks, in rule order, each once."""
    if not isinstance(command, str):
        return []
    command = ARITHMETIC.sub(" ", command)  # $((1<<3)) is arithmetic, not a heredoc
    return [rule for rule, _, pattern in COMMAND_RULES if pattern.search(command)]


def gate_kinds(command):
    """Gate kinds a Bash command runs, in order, each once. Only the head of a shell segment counts, so a commit message
    or an echo that mentions pytest is not a gate run."""
    if not isinstance(command, str):
        return []
    kinds = []
    for segment in _SEGMENT_SPLIT.split(command):
        head = segment[_PREFIX.match(segment).end():]
        for kind, pattern in GATES:
            if pattern.match(head):
                if kind not in kinds:
                    kinds.append(kind)
                break
    return kinds


def write_slips(path, text):
    """Rule ids a Write/Edit of `text` to `path` breaks: a fixed wait in a test file that carries no `timer-ok` mark."""
    if not isinstance(path, str) or not isinstance(text, str) or not TEST_PATH.search(path):
        return []
    for line in text.splitlines():
        if FIXED_WAIT.search(line) and TIMER_OK not in line:
            return ["fixed_wait"]
    return []


def think_leak(text):
    """True when text holds a <think> or </think> tag."""
    return isinstance(text, str) and bool(THINK_TAG.search(text))


def commit_message_slips(messages):
    """Rule ids over commit messages (one string each, the whole body). A trial runner passes `git log <base>..HEAD`
    messages. Returns a list of rule ids, one entry per breach, so two bad commits count twice."""
    slips = []
    for message in messages or []:
        text = message if isinstance(message, str) else ""
        subject = text.strip().splitlines()[0] if text.strip() else ""
        if not COMMIT_SUBJECT.match(subject):
            slips.append("commit_subject")
        if not COMMIT_TRAILER.search(text):
            slips.append("commit_trailer")
        if think_leak(text):
            slips.append("think_leak")
    return slips


def _result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and isinstance(p.get("text"), str))
    return ""


def _blocks(entry):
    message = entry.get("message") if isinstance(entry, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def scan_claude(path):
    """Scan a Claude transcript. Returns the scan dict; status is `missing` when the file is not there, `unreadable`
    when it cannot be read or is too large, otherwise `ok`."""
    path = Path(path)
    out = {"harness": "claude", "status": "ok", "bad_lines": 0, "commands": 0, "rule_slips": {}, "gate_runs": []}
    try:
        if path.is_symlink() or not path.is_file():
            out["status"] = "missing"
            out["rule_slips"] = out["gate_runs"] = None
            return out
        if path.stat().st_size > MAX_TRANSCRIPT_BYTES:
            raise OSError("transcript is larger than {} bytes".format(MAX_TRANSCRIPT_BYTES))
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        out["status"] = "unreadable"
        out["rule_slips"] = out["gate_runs"] = None
        return out
    slips = {}
    gate_calls = {}  # tool_use_id -> index into out["gate_runs"]
    with handle:
        for line in handle:
            if len(line) > MAX_LINE_BYTES:
                out["bad_lines"] += 1
                continue
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                out["bad_lines"] += 1
                continue
            for block in _blocks(entry):
                kind = block.get("type")
                if kind == "text" and entry.get("type") == "assistant" and think_leak(block.get("text")):
                    slips["think_leak"] = slips.get("think_leak", 0) + 1
                elif kind == "tool_use":
                    tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                    found = []
                    if block.get("name") == "Bash":
                        out["commands"] += 1
                        command = tool_input.get("command")
                        found = command_slips(command)
                        for gate in gate_kinds(command):
                            out["gate_runs"].append({"kind": gate, "exit": None})
                            if isinstance(block.get("id"), str):
                                gate_calls.setdefault(block["id"], []).append(len(out["gate_runs"]) - 1)
                    elif block.get("name") in ("Write", "Edit", "MultiEdit"):
                        target = tool_input.get("file_path")
                        edits = tool_input.get("edits") if isinstance(tool_input.get("edits"), list) else [tool_input]
                        for edit in edits:
                            text = edit.get("content", edit.get("new_string")) if isinstance(edit, dict) else None
                            found += [r for r in write_slips(target, text) if r not in found]
                    elif block.get("name") in ("Read", "Grep", "Glob", "NotebookRead"):
                        target = tool_input.get("file_path") or tool_input.get("path")
                        if isinstance(target, str) and ENV_PATH.search(target):
                            found = ["env_read"]
                    for rule in found:
                        slips[rule] = slips.get(rule, 0) + 1
                elif kind == "tool_result":
                    text = _result_text(block.get("content"))
                    failed = bool(block.get("is_error"))
                    if failed and any(m in text.lower() for m in GUARD_MARKERS):
                        slips["guard_denial"] = slips.get("guard_denial", 0) + 1
                        status = None  # a refused call never ran, so it has no exit
                        denied = True
                    else:
                        denied = False
                        match = _EXIT.match(text) if failed else None
                        status = int(match.group(1)) if match else (None if failed else 0)
                    for index in gate_calls.get(block.get("tool_use_id"), []):
                        out["gate_runs"][index]["exit"] = status
                        if denied:
                            out["gate_runs"][index]["denied"] = True
    out["rule_slips"] = dict(sorted(slips.items()))
    return out


def scan_pi(path):
    """Pi transcripts have no pinned schema until the Phase-0 sample exists: every field is unknown, not zero."""
    return {"harness": "pi", "status": "unknown", "bad_lines": 0, "commands": None, "rule_slips": None, "gate_runs": None}


def transcript_for(task_dir):
    """(harness, path) of a task's transcript, or (None, None). A `transcript-path` file names a Claude transcript; a
    `transcript.jsonl` in the task dir is a pi session."""
    task_dir = Path(task_dir)
    named = task_dir / "transcript-path"
    try:
        if named.is_file() and not named.is_symlink():
            text = named.read_text(encoding="utf-8").strip()
            if text:
                return "claude", Path(text)
    except (OSError, UnicodeDecodeError):
        pass
    pi = task_dir / "transcript.jsonl"
    if pi.is_file() and not pi.is_symlink():
        return "pi", pi
    return None, None


def scan_task(task_dir):
    """Scan whatever transcript the task has. No transcript is `status: "missing"` with null fields."""
    harness, path = transcript_for(task_dir)
    if harness == "claude":
        return scan_claude(path)
    if harness == "pi":
        return scan_pi(path)
    return {"harness": None, "status": "missing", "bad_lines": 0, "commands": None, "rule_slips": None, "gate_runs": None}
