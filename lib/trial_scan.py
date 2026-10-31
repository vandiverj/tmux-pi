"""Transcript scanners for the Qwen trial: rule slips and gate runs, as counts and rule ids only.

A scan never keeps command text or transcript text. It reports which worker-preamble rule a tool call broke (the rule
id), and which gate (test, lint, tsc, prettier, gitleaks) a Bash call ran with the exit status when the transcript shows it.

Claude transcripts are JSONL: assistant lines hold `tool_use` blocks, user lines hold the matching `tool_result`. Pi
transcripts (`transcript.jsonl` in the task dir) have no pinned schema yet: the design waits for a Phase-0 sample, so
`scan_pi` returns `status: "unknown"` with null `rule_slips` and `gate_runs` instead of guessing.

Fixed waits (`write_slips`) follow one rule: a wait is flagged only when the scanner can positively identify it as a call
token in code that runs. Anything it cannot read with confidence is not flagged. A `timer-ok` mark clears a wait only when
it sits in a comment on the same line. Known limits, all of them in the "not flagged" direction unless noted:

- Only py (.py .pyi), shell (.sh .bash .zsh .bats) and js/ts (.js .mjs .cjs .jsx .ts .mts .cts .tsx) files are read. Any other
  file (.md, .json, .html, .vue, .rb, ...) has no code to the scanner, so a wait in it is not flagged.
- Comments, string text and JavaScript regular-expression literals are blanked; interpolations (`f"{...}"`, `${...}`,
  `$(...)`, backticks in shell strings) are code. A wait inside a string that another interpreter runs (`exec("sleep 2")`,
  `page.evaluate("setTimeout(...)")`, `bash -c '...'`) is therefore not seen.
- A JavaScript `/` after `)`, `}`, `+`, `-` or `.` could be a division or the start of a regex, and a
  regex that does not end on its line cannot be read: the whole line is left unflagged (`UNSURE`). JSX text is not parsed.
- Names decide, types do not: `sleep(`, `x.sleep(`, `setTimeout(` and `waitForTimeout(`/`wait_for_timeout(` are waits on any
  object, so a call to a user helper of that name is flagged (the other direction). `def sleep(` and `function sleep(`
  are definitions and are not; a JS method written `sleep(ms) {` is not told from a call and is flagged.
- A shell wait is `sleep N` (a digit) where a command starts: at the start of a line (not after a trailing backslash), after
  `; & | ( { ` ` ` or `then`/`do`/`else`/`elif`. `sleep $n`, `env X=1 sleep 2`, `timeout 5 sleep 2` and case patterns are not
  flagged. Heredoc bodies are data, so `$(...)` that an unquoted heredoc would expand is not seen either.
- Nesting deeper than MAX_NESTING levels is read as written (still flagged); `UNSURE` only ever clears a line.

Gate runs (`gate_runs`, `gate_kinds`): a gate inside a command substitution (`$(...)`, backticks, `<(...)`) is recorded with
a null exit, since the status the transcript shows is the status of the command around it. A gate nested more than
MAX_NESTING substitutions deep is not seen, and heredoc bodies are data, not commands.
"""

import bisect
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
# Rules that read a file write, not a command: the content a Write/Edit puts in a test file. A wait is flagged only when
# it is a call token in code that runs (not in a comment, a string or a regex literal) in a file of a known language.
# A Python or JS call is `sleep(`, `setTimeout(`, `waitForTimeout(`, `wait_for_timeout(`; a definition (`def sleep(`,
# `function sleep(`) is not a call. A shell wait is `sleep N` where a command starts.
_CALL = r"(?<!def )(?<!function )\bsleep\s*\("
FIXED_WAIT = {
    "py": re.compile(_CALL + r"|\bwait_for_timeout\s*\("),
    "js": re.compile(_CALL + r"|\b(?:setTimeout|waitForTimeout)\s*\("),
    "sh": re.compile(r"(?:^|[;&|(){`\n]|\b(?:then|do|else|elif))\s*sleep\s+\d"),
}
TIMER_OK = "timer-ok"
# Only a comment can carry TIMER_OK. Comments are dropped and the literal text of a string is blanked, but an
# interpolation (`f"{...}"`, `${...}` in a template, `$(...)` or backticks in shell) is code and is kept.
_LANGS = {".py": "py", ".pyi": "py", ".sh": "sh", ".bash": "sh", ".zsh": "sh", ".bats": "sh",
          ".js": "js", ".mjs": "js", ".cjs": "js", ".jsx": "js", ".ts": "js", ".mts": "js", ".cts": "js", ".tsx": "js"}
MAX_NESTING = 32  # interpolations inside interpolations; past this the rest of the text is read as written
_PY_STRING = re.compile(r"(?:(?<!\w)([rRbBuUfF]{1,2}))?('\'\'|\"\"\"|'|\")")
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
# Builtins that do not fail in practice. `cd` is not one: a missing dir fails it and the gate behind `&&` never runs.
_RELIABLE = re.compile(r"\s*(?:echo|export|printf|pwd|true|set|unset|:)(?:\s|$)")
# `set -o pipefail`, `set -euo pipefail` turn pipefail on; `set +o pipefail` turns it off.
_PIPEFAIL = re.compile(r"^\s*set\s+(?:[-+]\w*\s+)*([-+])\w*o\s+pipefail\b")
ARITHMETIC = re.compile(r"\$\(\(.*?\)\)", re.S)
_EXIT = re.compile(r"^\s*Exit code[: ]+(-?\d+)")


def strip_shell_comments(command):
    """`command` without its shell comments: a `#` that starts a word, outside quotes, up to the end of its line. A `#`
    inside a word (`a#b`, `$#`, `${#a}`) or a quote is not a comment."""
    out, quote, i, n = [], None, 0, len(command)
    while i < n:
        c = command[i]
        if quote:
            if c == "\\" and quote == '"' and i + 1 < n:
                out.append(command[i:i + 2])
                i += 2
                continue
            if c == quote:
                quote = None
        elif c == "\\" and i + 1 < n:
            out.append(command[i:i + 2])
            i += 2
            continue
        elif c in "'\"":
            quote = c
        elif c == "#" and (i == 0 or command[i - 1] in " \t\n;&|()"):
            j = command.find("\n", i)
            i = n if j < 0 else j
            continue
        out.append(c)
        i += 1
    return "".join(out)


def command_slips(command):
    """Rule ids a single Bash command breaks, in rule order, each once. Shell comments do not run and break nothing."""
    if not isinstance(command, str):
        return []
    command = ARITHMETIC.sub(" ", strip_shell_comments(command))  # $((1<<3)) is arithmetic, not a heredoc
    return [rule for rule, _, pattern in COMMAND_RULES if pattern.search(command)]


HEREDOC = re.compile(r"(?<!<)<<(?!<)(-?)[ \t]*(?:'([^'\n]+)'|\"([^\"\n]+)\"|\\?(\w+))")


def _blank_heredocs(command):
    """`command` with each heredoc body (and its end line) turned to spaces, same length and same newlines. A body is data,
    not commands, so a line of it that reads `npm test` or `sleep 2` is not a run. A heredoc whose end line is never found
    is left as written, and `<<` inside `$(( ))` is arithmetic."""
    masked = ARITHMETIC.sub(lambda m: re.sub(r"[^\n]", " ", m.group()), command)
    out, pos, done = [], 0, 0
    while True:
        m = HEREDOC.search(masked, pos)
        if not m:
            break
        delimiter = next(g for g in m.groups()[1:] if g is not None)
        start = masked.find("\n", m.end())
        if start < 0:
            break
        i = start + 1
        end = None
        while i <= len(masked):
            j = masked.find("\n", i)
            j = len(masked) if j < 0 else j
            if (masked[i:j].lstrip("\t") if m.group(1) else masked[i:j]) == delimiter:
                end = j
                break
            if j == len(masked):
                break
            i = j + 1
        if end is None:
            pos = m.end()
            continue
        out.append(command[done:start + 1])
        out.append(re.sub(r"[^\n]", " ", command[start + 1:end]))
        done = pos = end
    out.append(command[done:])
    return "".join(out)


def _group_end(text, i, level=0):
    """(index after, closed) for the command substitution (`$(...)`, `<(...)`, `>(...)`) or backtick pair that starts at
    text[i]. Quotes and nested groups inside it are followed. Past MAX_NESTING levels the rest of the text is the group."""
    n = len(text)
    if level >= MAX_NESTING:
        return n, False
    if text[i] == "`":
        j = i + 1
        while j < n:
            if text[j] == "\\":
                j += 2
            elif text[j] == "`":
                return j + 1, True
            else:
                j += 1
        return n, False
    depth, j = 1, i + 2
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
        elif c == "'":
            k = text.find("'", j + 1)
            j = n if k < 0 else k + 1
        elif c == '"':
            j = _dq_end(text, j + 1, level + 1)
        elif c == "`" or text.startswith("$(", j):
            j = _group_end(text, j, level + 1)[0]
        else:
            depth += (c == "(") - (c == ")")
            j += 1
            if depth == 0:
                return j, True
    return n, False


def _dq_end(text, i, level):
    """Index after the double quote that closes the string whose body starts at text[i]; groups in it are followed."""
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
        elif c == '"':
            return i + 1
        elif c == "`" or text.startswith("$(", i):
            i = _group_end(text, i, level)[0]
        else:
            i += 1
    return n


def _units(command):
    """[(connector, [pipeline stage text, ...])] for a shell command. The connector is what precedes the unit: None for the
    first, then `&&`, `||` or `;` (a newline or a lone `&` counts as `;`). Quotes and backslashes hide operators, and so
    does a command substitution (`$(a && b)`, backticks, `<(...)`), and `>&`, `&>` redirects are not a connector. Shell
    comments are dropped first. Empty units are dropped."""
    command = strip_shell_comments(command)
    units, stages, buf, op = [], [], [], None
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
        if quote == "'":
            buf.append(c)
            quote = None if c == "'" else quote
        elif quote == '"':
            if c == "\\" and i + 1 < n:
                buf.append(command[i:i + 2])
                i += 2
                continue
            if c == "`" or command.startswith("$(", i):
                end = _group_end(command, i)[0]
                buf.append(command[i:end])
                i = end
                continue
            buf.append(c)
            if c == '"':
                quote = None
        elif c == "\\" and i + 1 < n:
            buf.append(c + command[i + 1])
            i += 1
        elif c in "'\"":
            quote = c
            buf.append(c)
        elif _starts_group(command, i):
            end = _group_end(command, i)[0]
            buf.append(command[i:end])
            i = end
            continue
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


def _starts_group(text, i):
    """True when a command substitution or process substitution starts at text[i] outside double quotes."""
    return text[i] == "`" or (text.startswith("$(", i) and not text.startswith("$((", i)) or (
        text[i] in "<>" and text.startswith("(", i + 1))


def _stage_groups(stage):
    """(stage with each command substitution replaced by `_`, [body text of each]) for one pipeline stage. Single quotes
    and backslashes hide a substitution; double quotes do not (the shell still runs `"$(cmd)"`)."""
    out, bodies, quote, i, n = [], [], None, 0, len(stage)
    while i < n:
        c = stage[i]
        if quote == "'":
            out.append(c)
            quote = None if c == "'" else quote
        elif c == "\\" and i + 1 < n:
            out.append(stage[i:i + 2])
            i += 2
            continue
        elif (c == "`" or (c == "$" and stage.startswith("$(", i) and not stage.startswith("$((", i))
              or (quote is None and c in "<>" and stage.startswith("(", i + 1))):
            end, closed = _group_end(stage, i)
            start = i + 1 if c == "`" else i + 2
            body = stage[start:end - 1 if closed else end]
            bodies.append(body.replace("\\`", "`") if c == "`" else body)
            out.append("_")
            i = end
            continue
        else:
            if c == "'" and quote is None:
                quote = "'"
            elif c == '"':
                quote = None if quote else '"'
            out.append(c)
        i += 1
    return "".join(out), bodies


def _stage_kind(stage):
    head = stage[_PREFIX.match(stage).end():]
    for kind, pattern in GATES:
        if pattern.match(head):
            return kind
    return None


def _plan(command, level=0):
    """[{"op", "gates": [(kind, last_stage)...], "gate_stages", "nested": [(stage, kind, uncertain)...], "stages",
    "reliable", "pipefail"}] per shell unit. A pipeline is one unit; its status is its last stage's, or with pipefail the
    last failing stage's. `pipefail` is True/False when the unit sets it, else None. `nested` are the gates a command
    substitution in the unit runs (`echo "$(npm test)"`): they run when the unit runs, and their status is not the unit's."""
    plan = []
    for op, stages in _units(_blank_heredocs(command)):
        gates, gate_stages, nested = [], [], []
        for j, stage in enumerate(stages):
            masked, bodies = _stage_groups(stage) if level < MAX_NESTING else (stage, [])
            for body in bodies:
                nested += [(j, run["kind"], bool(run.get("uncertain"))) for run in _gate_runs(body, None, level + 1)]
            kind = _stage_kind(masked)
            if kind:
                gates.append((kind, j == len(stages) - 1))
                gate_stages.append(j)
        single = len(stages) == 1
        switch = _PIPEFAIL.match(stages[0]) if single else None
        plan.append({"op": op, "gates": gates, "gate_stages": gate_stages, "nested": nested, "stages": len(stages),
                     "reliable": single and bool(_RELIABLE.match(stages[0])),
                     "pipefail": (switch.group(1) == "-") if switch else None})
    return plan


def _slots(unit):
    """[(kind, last_stage, inner_uncertain)] for a unit in the order the shell reaches them: by stage, a stage's command
    substitutions before its own command. `last_stage` is None for a gate inside a substitution."""
    slots = [(j, 0, kind, None, unc) for j, kind, unc in unit["nested"]]
    slots += [(j, 1, kind, last, False) for j, (kind, last) in zip(unit["gate_stages"], unit["gates"])]
    return [slot[2:] for slot in sorted(slots, key=lambda slot: slot[:2])]


def gate_kinds(command):
    """Gate kinds a Bash command names, in order, repeats kept. Only the head of a shell segment counts, so a commit
    message or an echo that mentions pytest is not a gate run. It does not say which ones ran; `gate_runs` does."""
    if not isinstance(command, str):
        return []
    return [kind for unit in _plan(command) for kind, _, _ in _slots(unit)]


def _runs(unit, i, status):
    return i == 0 or unit["op"] == ";" or (unit["op"] == "&&" and status == 0) or (unit["op"] == "||" and status != 0)


def _after(unit, state, outcome):
    pipefail = state[1] if unit["pipefail"] is None else unit["pipefail"]
    return (outcome, pipefail)


def _gate_exit(outcome, ended, last_stage, single, pipefail, exit_code):
    """What one gate's exit is, given its unit's status (0 or 1 = failed) in one surviving case: a number, "fail" (failed,
    code not shown) or "unknown" (the unit's status does not say)."""
    if not (single or (last_stage and not pipefail) or (pipefail and outcome == 0)):
        return "unknown"  # an earlier stage without pipefail, or a failed pipefail pipe whose failing stage is not shown
    if outcome == 0:
        return 0
    return exit_code if ended and exit_code else "fail"


def gate_runs(command, exit_code=None):
    """[{"kind", "exit"} | + "uncertain": True] for one Bash command whose whole exit status is `exit_code` (None when
    not shown). A gate inside a command substitution (`$(...)`, backticks, `<(...)`) is recorded with a null exit: it runs
    whenever its unit does, but the status of the substitution is not the status of the command around it. Each unit of the list passes or fails (echo / true / set and the like only pass), and bash's `&&` / `||`
    / `;` rules decide which units run. A case is one pass/fail choice per unit that ends in the shown status (0 or
    nonzero). A gate that ran in every case has no mark; one that ran in some has `uncertain: True`; one that ran in none
    is left out. `exit` is a number only for a certain run when every case agrees on it: 0, or the shown code for the
    gate whose status ended the list. A gate that is not the last stage of a pipe has no exit unless pipefail is set
    and the pipe passed.

    The cases are not listed one by one: what decides the rest of the list is only (status, pipefail) after a unit, so a
    forward pass finds the states each unit can start in and a backward pass which states can still end in the shown
    status. A long list costs a few steps per unit, with no cap."""
    return _gate_runs(command, exit_code, 0)


def _gate_runs(command, exit_code, level, nested_only=False):
    plan = _plan(command, level) if isinstance(command, str) else []
    if not any(unit["gates"] or unit["nested"] for unit in plan):
        return []
    n = len(plan)

    def outcomes(unit):
        return (0,) if unit["reliable"] else (0, 1)

    states = [(s, p) for s in (0, 1) for p in (False, True)]
    # tail[i][state] = set of "did a later unit run" over continuations of units i.. that end in the shown status
    tail = [None] * (n + 1)
    tail[n] = {st: ({False} if exit_code is None or (st[0] == 0) == (exit_code == 0) else set()) for st in states}
    for i in range(n - 1, -1, -1):
        unit, row = plan[i], {}
        for st in states:
            if _runs(unit, i, st[0]):
                row[st] = {True} if any(tail[i + 1][_after(unit, st, o)] for o in outcomes(unit)) else set()
            else:
                row[st] = set(tail[i + 1][st])
        tail[i] = row
    if exit_code is not None and not tail[0][(0, False)]:
        # The shown status fits no way this list could run (`echo "$(npm test)" > /no/dir` fails though echo "cannot").
        # A substitution ran with its unit anyway, so it is still counted; the direct gates have nothing to say.
        return _gate_runs(command, None, level, nested_only=True)
    # head[i] = states the shell can be in just before unit i
    head = [{(0, False)}]
    for i, unit in enumerate(plan):
        nxt = set()
        for st in head[i]:
            nxt.update(_after(unit, st, o) for o in outcomes(unit)) if _runs(unit, i, st[0]) else nxt.add(st)
        head.append(nxt)

    out = []
    for i, unit in enumerate(plan):
        ran = [(st, o) for st in head[i] if _runs(unit, i, st[0]) for o in outcomes(unit) if tail[i + 1][_after(unit, st, o)]]
        if not ran:
            continue
        skipped = any(tail[i + 1][st] for st in head[i] if not _runs(unit, i, st[0]))
        for kind, last_stage, inner_uncertain in _slots(unit):
            if last_stage is None:  # inside a command substitution: ran with its unit, status not shown
                out.append(dict({"kind": kind, "exit": None}, **({"uncertain": True} if skipped or inner_uncertain else {})))
                continue
            if nested_only:
                continue
            values = set()
            for st, o in ran:
                for later in tail[i + 1][_after(unit, st, o)]:  # pipefail as it was while the pipe ran
                    values.add(_gate_exit(o, not later, last_stage, unit["stages"] == 1, st[1], exit_code))
            value = next(iter(values)) if len(values) == 1 else None
            run = {"kind": kind, "exit": value if isinstance(value, int) else None}
            if skipped:  # its status only counts if it ran, so none is claimed
                run["exit"], run["uncertain"] = None, True
            out.append(run)
    return out


def gate_totals(runs):
    """{"total", "uncertain", "denied"} over gate_runs entries: `total` counts runs known to have happened."""
    runs = runs or []
    denied = sum(1 for r in runs if r.get("denied"))
    uncertain = sum(1 for r in runs if r.get("uncertain") and not r.get("denied"))
    return {"total": len(runs) - denied - uncertain, "uncertain": uncertain, "denied": denied}


def _source_lang(path):
    """"py", "sh" or "js" (JavaScript and TypeScript) from the file name, None for any other kind of file."""
    name = path.rsplit("/", 1)[-1]
    return _LANGS.get(name[name.rfind("."):].lower() if "." in name else "")


# After one of these a `/` starts a regular expression; after a word or a closing bracket it is a division; after
# `)`, `}`, `+`, `-`, `.` it could be either, and the scanner does not guess.
_REGEX_AFTER = frozenset("(,=:[!&|?{;<>*%^~")
_REGEX_AFTER_WORD = frozenset(("return", "typeof", "case", "do", "else", "in", "of", "instanceof", "new", "delete", "void",
                               "throw", "yield", "await"))
UNSURE = "\x00"  # written into the code of a line the scanner could not read with confidence: no wait is flagged on it


def _regex_end(text, i):
    """Index after the regular-expression literal (and its flags) that starts at text[i], None when it does not end on
    its line."""
    j, n, in_class = i + 1, len(text), False
    while j < n:
        c = text[j]
        if c == "\n":
            return None
        if c == "\\":
            j += 2
            continue
        if in_class:
            in_class = c != "]"
        elif c == "[":
            in_class = True
        elif c == "/":
            j += 1
            while j < n and text[j].isalpha():
                j += 1
            return j
        j += 1
    return None


def _note(text, i, j, marks):
    """Remember where a `timer-ok` mark sits inside the comment text[i:j]."""
    k = text.find(TIMER_OK, i, j)
    while k >= 0:
        marks.append(k)
        k = text.find(TIMER_OK, k + 1, j)


def _code_from(text, i, lang, close, out, marks, level=0):
    """Copy the code in text[i:] to `out` until an unmatched `close` or the end, and return the index after it. A comment
    becomes nothing (the places of its `timer-ok` marks go to `marks`), a string or a regular-expression literal becomes
    `""` with its newlines kept (so lines still line up), an interpolation is code. A `/` that could be a division or the
    start of a regular expression puts UNSURE in the code."""
    opener = {"}": "{", ")": "("}.get(close)
    depth, n = 0, len(text)
    prev, word, gap = "", "", False  # last significant code char, the word it ends, whether a space came after it
    while i < n:
        c = text[i]
        if close and c == close and depth == 0:
            return i + 1
        if lang == "js" and text.startswith("//", i) or c == "#" and (
                lang == "py" or lang == "sh" and (i == 0 or text[i - 1] in " \t\n;&|()")):
            j = text.find("\n", i)
            j = n if j < 0 else j  # the newline itself is code
            _note(text, i, j, marks)
            i = j
            continue
        if lang == "js" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            _note(text, i, j, marks)
            out.append("\n" * text.count("\n", i, j))
            i = j
            continue
        string = _PY_STRING.match(text, i) if lang == "py" else None
        if string:
            i = _string(text, string.end(), lang, string.group(2), "f" in (string.group(1) or "").lower(), out, marks, level)
            prev, word = '"', ""
            continue
        if lang != "py" and (c in "'\"" or c == "`" and lang == "js"):
            i = _string(text, i + 1, lang, c, c == "`" or (lang == "sh" and c == '"'), out, marks, level)
            prev, word = '"', ""
            continue
        if lang == "js" and c == "/":
            kind = _slash_kind(prev, word)
            end = _regex_end(text, i) if kind == "regex" else None
            if end:
                out.append('""')
                i, prev, word = end, '"', ""
                continue
            if kind != "divide":
                out.append(UNSURE)
                i += 1
                prev, word = c, ""
                continue
        if opener and c == opener:
            depth += 1
        elif close and c == close:
            depth -= 1
        out.append(c)
        i += 1
        if c.isspace():
            gap = True
        else:
            if c.isalnum() or c in "_$":
                word = c if (gap or not word) else word + c
            else:
                word = ""
            prev, gap = c, False
    return i


def _slash_kind(prev, word):
    """"regex", "divide" or "unsure" for a `/` that follows the code char `prev` (the end of `word` when it is a word)."""
    if prev == "" or prev in _REGEX_AFTER:
        return "regex"
    if prev.isalnum() or prev in "_$":
        return "regex" if word in _REGEX_AFTER_WORD else "divide"
    if prev in "]\"":
        return "divide"
    return "unsure"


def _string(text, i, lang, quote, interpolates, out, marks, level):
    """Blank a string body that starts at text[i] and return the index after its closing quote. A one-line quote (`'`
    or `"` outside shell, not tripled) also ends at a newline, so a stray apostrophe cannot swallow the next line."""
    one_line = len(quote) == 1 and quote != "`" and lang != "sh"
    raw_shell = lang == "sh" and quote == "'"
    out.append('"')
    n = len(text)
    if level >= MAX_NESTING:  # too deep to follow: keep the rest as code, so a wait in it still counts
        out.append(text[i:])
        return n
    while i < n:
        if text.startswith(quote, i):
            i += len(quote)
            break
        c = text[i]
        if c == "\n":
            if one_line:
                break
            out.append(c)
        elif c == "\\" and not raw_shell and i + 1 < n:
            if text[i + 1] == "\n":
                out.append("\n")
            i += 2
            continue
        elif interpolates and lang == "py" and c == "{":
            if text.startswith("{{", i):
                i += 2
                continue
            out.append(" ")
            i = _code_from(text, i + 1, lang, "}", out, marks, level + 1)
            out.append(" ")
            continue
        elif interpolates and lang == "js" and text.startswith("${", i):
            out.append(" ")
            i = _code_from(text, i + 2, lang, "}", out, marks, level + 1)
            out.append(" ")
            continue
        elif interpolates and lang == "sh" and (text.startswith("$(", i) or c == "`"):
            out.append(";")  # the start of a command, so `sleep 2` in it is found where a command begins
            i = _code_from(text, i + (2 if c == "$" else 1), lang, ")" if c == "$" else "`", out, marks, level + 1)
            out.append(";")
            continue
        i += 1
    out.append('"')
    return i


def _scan_source(path, text):
    """(code, lines that carry a `timer-ok` mark in a comment) for `text` written to `path`. The code is what runs:
    comments dropped, literal text of strings and regular-expression literals blanked, the code inside interpolations
    kept, shell heredoc bodies blanked. Its lines line up with `text`. A file that is not py, sh or js/ts has no code."""
    lang = _source_lang(path)
    if lang is None:
        return "\n" * text.count("\n"), set()
    out, marks = [], []
    if lang == "sh":
        text = _blank_heredocs(text)
    _code_from(text, 0, lang, None, out, marks)
    starts = [0] + [m.end() for m in re.finditer("\n", text)]
    return "".join(out), {bisect.bisect_right(starts, k) - 1 for k in marks}


def executable_code(path, text):
    """`text` as the code that runs, for a file at `path` (see `_scan_source`). Lines line up with `text`."""
    return _scan_source(path, text)[0]


def write_slips(path, text):
    """Rule ids a Write/Edit of `text` to `path` breaks: a fixed wait in the code of a test file, on a line with no
    `timer-ok` mark in a comment. It flags only a wait it can identify as code that runs (see Known limits above)."""
    if not isinstance(path, str) or not isinstance(text, str) or not TEST_PATH.search(path):
        return []
    lang = _source_lang(path)
    if lang is None:
        return []
    code, marked = _scan_source(path, text)
    pattern, continued = FIXED_WAIT[lang], False
    for n, line in enumerate(code.split("\n")):
        read = ("\\ " + line) if continued else line  # the line after a trailing backslash continues a command
        continued = lang == "sh" and line.rstrip().endswith("\\")
        if UNSURE not in line and n not in marked and pattern.search(read):
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
