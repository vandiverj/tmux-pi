"""Transcript scanners for the Qwen trial: rule slips and gate runs, as counts and rule ids only.

A scan never keeps command text or transcript text. It reports which worker-preamble rule a tool call broke (the rule
id), and which gate (test, lint, tsc, prettier, gitleaks) a Bash call ran with the exit status when the transcript shows it.

Claude transcripts are JSONL: assistant lines hold `tool_use` blocks, user lines hold the matching `tool_result`. Pi
transcripts (`transcript.jsonl` in the task dir) have no pinned schema yet: the design waits for a Phase-0 sample, so
`scan_pi` returns `status: "unknown"` with null `rule_slips` and `gate_runs` instead of guessing.
"""

import itertools
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
# A quoted literal is a value, not code that runs: a fixture that spells `time.sleep(2)` in a string does not wait. Triple
# quotes and backticks may span lines; plain quotes stop at the end of the line so a stray apostrophe in a comment is harmless.
STRING_LITERAL = re.compile("|".join((
    r'"""[\s\S]*?"""', r"'''[\s\S]*?'''", r"`(?:[^`\\]|\\[\s\S])*`",
    r'"(?:[^"\\\n]|\\.)*"', r"'(?:[^'\\\n]|\\.)*'")))
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
_PREFIX = re.compile(r"^\s*(?:(?:\w+=\S*|env|npx|timeout\s+\d+\S*|time|command)\s+)*")
# Units that practically never fail: a failed exit after `cd x && npm test` is the gate's, not the cd's.
_RELIABLE = re.compile(r"\s*(?:cd|echo|export|printf|pwd|true|set|unset|:)(?:\s|$)")
MAX_FLEXIBLE_UNITS = 12
ARITHMETIC = re.compile(r"\$\(\(.*?\)\)", re.S)
_EXIT = re.compile(r"^\s*Exit code[: ]+(-?\d+)")


def command_slips(command):
    """Rule ids a single Bash command breaks, in rule order, each once."""
    if not isinstance(command, str):
        return []
    command = ARITHMETIC.sub(" ", command)  # $((1<<3)) is arithmetic, not a heredoc
    return [rule for rule, _, pattern in COMMAND_RULES if pattern.search(command)]


def _units(command):
    """[(connector, [pipeline stage text, ...])] for a shell command. The connector is what precedes the unit: None for the
    first, then `&&`, `||` or `;` (a newline or a lone `&` counts as `;`). Quotes and backslashes hide operators, and `>&`,
    `&>` redirects are not a connector. Empty units are dropped."""
    units, stages, buf, op, pending = [], [], [], None, None
    quote = None
    i, n = 0, len(command)

    def end_stage():
        stages.append("".join(buf))
        buf.clear()

    def end_unit(connector):
        nonlocal stages, op
        end_stage()
        if any(t.strip() for t in stages):
            units.append((op, stages))
            op = connector
        elif connector != ";":
            op = connector
        stages = []

    while i < n:
        c = command[i]
        if quote:
            buf.append(c)
            if c == "\\" and quote == '"' and i + 1 < n:
                i += 1
                buf.append(command[i])
            elif c == quote:
                quote = None
        elif c == "\\" and i + 1 < n:
            buf.append(c + command[i + 1])
            i += 1
        elif c in "'\"":
            quote = c
            buf.append(c)
        elif command.startswith("&&", i) or command.startswith("||", i):
            end_unit(command[i:i + 2])
            i += 1
        elif c == "|":
            end_stage()
        elif c in ";\n" or (c == "&" and not (buf and buf[-1] in "<>") and command[i + 1:i + 2] != ">"):
            end_unit(";")
        else:
            buf.append(c)
        i += 1
    end_unit(";")
    return units


def _stage_kind(stage):
    head = stage[_PREFIX.match(stage).end():]
    for kind, pattern in GATES:
        if pattern.match(head):
            return kind
    return None


def _plan(command):
    """[{"op", "gates": [kind...], "reliable"}] per shell unit. A pipeline is one unit, and its exit is attributed to every
    gate in it (the usual `... 2>&1 | tail` assumes pipefail or a gate in last place)."""
    plan = []
    for op, stages in _units(command):
        gates = [k for k in (_stage_kind(st) for st in stages) if k]
        plan.append({"op": op, "gates": gates, "reliable": len(stages) == 1 and bool(_RELIABLE.match(stages[0]))})
    return plan


def gate_kinds(command):
    """Gate kinds a Bash command names, in order, repeats kept. Only the head of a shell segment counts, so a commit
    message or an echo that mentions pytest is not a gate run. It does not say which ones ran; `gate_runs` does."""
    if not isinstance(command, str):
        return []
    return [kind for unit in _plan(command) for kind in unit["gates"]]


def gate_runs(command, exit_code=None):
    """[{"kind", "exit"} | + "uncertain": True] for one Bash command whose whole exit status is `exit_code` (None when
    not shown). Gates behind `&&` / `||` are run only if the earlier units' status allowed it, so each possible pass/fail
    of every unit is tried against bash's list rules and kept when it ends in the shown status (0 or nonzero).
    A gate that ran in every surviving case has no mark; one that ran in some has `uncertain: True`; one that ran in none
    is left out. `exit` is a number only for a certain run when every surviving case agrees: 0, or the shown code for the unit whose status
    ended the list. Failures of cd / echo / true and the like are not tried."""
    plan = _plan(command) if isinstance(command, str) else []
    if not any(unit["gates"] for unit in plan):
        return []
    flexible = [i for i, unit in enumerate(plan) if not unit["reliable"]]
    cases = []
    if len(flexible) <= MAX_FLEXIBLE_UNITS:
        for bits in itertools.product((0, 1), repeat=len(flexible)):
            fails = dict(zip(flexible, bits))
            status, ran, last = 0, {}, None
            for i, unit in enumerate(plan):
                op = unit["op"]
                if i == 0 or op == ";" or (op == "&&" and status == 0) or (op == "||" and status != 0):
                    status = fails.get(i, 0)
                    ran[i], last = status, i
            if exit_code is None or (status == 0) == (exit_code == 0):
                cases.append((ran, last))
    out = []
    for i, unit in enumerate(plan):
        if cases:
            seen = [(ran[i], last == i) for ran, last in cases if i in ran]
            if not seen:
                continue
            certain = len(seen) == len(cases)
            values = {0 if code == 0 else (exit_code if ended and exit_code else "fail") for code, ended in seen}
            value = next(iter(values)) if len(values) == 1 and isinstance(next(iter(values)), int) else None
        else:  # nothing fits the shown status: only the first unit and `;` units are known to have run
            certain, value = unit["op"] in (None, ";"), None
        for kind in unit["gates"]:
            run = {"kind": kind, "exit": value}
            if not certain:  # its status only counts if it ran, so none is claimed
                run["exit"], run["uncertain"] = None, True
            out.append(run)
    return out


def gate_totals(runs):
    """{"total", "uncertain", "denied"} over gate_runs entries: `total` counts runs known to have happened."""
    runs = runs or []
    denied = sum(1 for r in runs if r.get("denied"))
    uncertain = sum(1 for r in runs if r.get("uncertain") and not r.get("denied"))
    return {"total": len(runs) - denied - uncertain, "uncertain": uncertain, "denied": denied}


def _blank_literal(match):
    return '""' + "\n" * match.group(0).count("\n")  # keep line numbers


def write_slips(path, text):
    """Rule ids a Write/Edit of `text` to `path` breaks: a fixed wait in a test file that carries no `timer-ok` mark."""
    if not isinstance(path, str) or not isinstance(text, str) or not TEST_PATH.search(path):
        return []
    for line in STRING_LITERAL.sub(_blank_literal, text).splitlines():
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
    """Scan a Claude transcript. Returns the scan dict. `status` is `missing` when the file is not there, `unreadable` when
    it cannot be read or is too large, `incomplete` when lines were skipped (malformed, oversized) so what was found is a
    lower bound, otherwise `ok`. `complete` is True only for `ok`."""
    path = Path(path)
    out = {"harness": "claude", "status": "ok", "complete": True, "bad_lines": 0, "commands": 0, "rule_slips": {}, "gate_runs": []}
    unusable = dict(out, status="unreadable", complete=False, commands=None, rule_slips=None, gate_runs=None)
    try:
        if path.is_symlink() or not path.is_file():
            return dict(unusable, status="missing")
        if path.stat().st_size > MAX_TRANSCRIPT_BYTES:
            raise OSError("transcript is larger than {} bytes".format(MAX_TRANSCRIPT_BYTES))
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return unusable
    slips = {}
    calls = []  # one gate-run list per Bash call, in order
    by_id = {}  # tool_use_id -> (index into calls, command)
    try:
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
                    _scan_block(entry, block, out, slips, calls, by_id)
    except (OSError, ValueError):  # a read that dies half way is a partial scan, not a clean one
        return dict(unusable)
    out["gate_runs"] = [run for runs in calls for run in runs]
    out["rule_slips"] = dict(sorted(slips.items()))
    if out["bad_lines"]:
        out["status"], out["complete"] = "incomplete", False
    return out


def _scan_block(entry, block, out, slips, calls, by_id):
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
            calls.append(gate_runs(command))
            if isinstance(block.get("id"), str):
                by_id[block["id"]] = (len(calls) - 1, command)
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
        call = by_id.get(block.get("tool_use_id"))
        if failed and any(m in text.lower() for m in GUARD_MARKERS):
            slips["guard_denial"] = slips.get("guard_denial", 0) + 1
            if call:  # a refused call never ran, so every gate in it is denied and has no exit
                calls[call[0]] = [{"kind": g, "exit": None, "denied": True} for g in gate_kinds(call[1])]
        elif call:
            match = _EXIT.match(text) if failed else None
            code = int(match.group(1)) if match else (None if failed else 0)
            calls[call[0]] = gate_runs(call[1], code)


def scan_pi(path):
    """Pi transcripts have no pinned schema until the Phase-0 sample exists: every field is unknown, not zero."""
    return {"harness": "pi", "status": "unknown", "complete": False, "bad_lines": 0, "commands": None, "rule_slips": None,
            "gate_runs": None}


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
    return {"harness": None, "status": "missing", "complete": False, "bad_lines": 0, "commands": None, "rule_slips": None,
            "gate_runs": None}
