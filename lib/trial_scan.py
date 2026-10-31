"""Transcript scanners for the Qwen trial: rule slips and gate runs, as counts and rule ids only.

A scan never keeps command text or transcript text. It reports which worker-preamble rule a tool call broke (the rule
id), and which gate (test, lint, tsc, prettier, gitleaks) a Bash call ran with the exit status when the transcript shows it.

Claude transcripts are JSONL: assistant lines hold `tool_use` blocks, user lines hold the matching `tool_result`. Pi
transcripts (`transcript.jsonl` in the task dir) have no pinned schema yet: the design waits for a Phase-0 sample, so
`scan_pi` returns `status: "unknown"` with null `rule_slips` and `gate_runs` instead of guessing.

Known limits. Every limit below is in the "not counted" direction unless it says otherwise: the scanner reports what it can
read with confidence and leaves the rest out, and no value it reports is inferred from a guess.

Fixed waits (`write_slips`), only in a test file of a known language (py .py .pyi; shell .sh .bash .zsh .bats; js/ts .js .mjs
.cjs .jsx .ts .mts .cts .tsx; any other file is not read, so a wait in a .md, .json, .html, .vue or .rb is not flagged):

- A wait is flagged only when it is a call statement: its first token is `sleep`, `time.sleep` or `asyncio.sleep` (py) or
  `sleep` or `setTimeout` (js/ts), after an optional `await`, and the `)` that closes the call is followed by the end of the
  line, `;` or `.`. A statement starts at the start of a line (not after a trailing backslash, py or shell) or after `;`.
  A definition (`def sleep(`, `async def   sleep(`, `function  sleep(`, `export const sleep = ...`) starts with another
  token and is never flagged, whatever the whitespace.
- NOT flagged, though they wait: a wait that does not start its statement (`await new Promise(r => setTimeout(r, 50))`,
  `x = time.sleep(1)`, `if x: time.sleep(1)`, `foo(sleep(1), 2)`, `cb = () => sleep(1)`, `{ sleep(1) }`), `waitForTimeout(`,
  `wait_for_timeout(`, `window.setTimeout(`, `x.sleep(`, and `time.sleep` in js or `setTimeout` in py. A js `sleep(` is a
  declaration and is not flagged when its `)` is followed by `{` (same line, or the next line: Allman style), or when it
  stands directly in an `interface` or `class` body (`sleep(ms: number);`, `sleep(ms: number): Promise<void>;`, a bare
  `sleep(ms)`). A body counts as one when the text from the last `;`, `{` or `}` to its `{` has the word `interface` or
  `class`. A real call followed by a `{` block on the next line is not flagged either (the other direction), and a call
  of a user helper named `sleep(` IS flagged (names decide, types do not; the other direction).
- `timer-ok` clears a wait only when it sits in a comment on the line the wait starts on.
- Comments, string text and JavaScript regular-expression literals are blanked; interpolations (`f"{...}"`, `${...}`,
  `$(...)`, backticks in shell strings) are code, so a statement start inside one counts. A wait inside a string another
  interpreter runs (`exec("sleep 2")`, `page.evaluate("setTimeout(...)")`, `bash -c '...'`) is not seen.
- A JavaScript `/` after `)`, `}`, `+`, `-` or `.` could be a division or the start of a regex, and a regex that does not
  end on its line cannot be read: the whole line is left unflagged (`UNSURE`). JSX text is not parsed.
- A shell wait is `sleep N` (a digit) where a command starts: at the start of a line, after `; & | ( { ` ` ` or
  `then`/`do`/`else`/`elif`. `sleep $n`, `env X=1 sleep 2`, `timeout 5 sleep 2` and case patterns are not flagged.
- Shell heredocs: a body is data, from the line after its `<<WORD` opener to the line that is WORD. In a written shell
  file, a body with no WORD line runs to the end of the text, as the shell reads it. (In a Bash command that is a
  `parse_failed` call; see the gate bullets.) `<<` in quotes, in `<<<`, or in `$((...))` is not an opener. `<<` in a `(( ))`
  command or `[[ ]]` IS read as an opener, which drops the text up to a line equal to the word after it (not counted).
- Nesting deeper than MAX_NESTING levels is read as written: a shell `sleep N` after `(` or `;` there is still flagged (the
  other direction), a py/js call is not (it no longer starts its statement). `UNSURE` only ever clears a line.

Gate runs (`gate_runs`, `gate_kinds`), over one Bash call:

- A gate (test, lint, tsc, prettier, gitleaks) is found only where a pipeline stage starts, in a simple command or in a plain
  `&&` / `||` / `;` / newline chain, after env assignments, `env`, `npx`, `timeout N`, `time`, `command`. A gate that appears
  only inside a heredoc body, a `$(...)` or backtick or `<(...)` body, arithmetic or a quoted string (`bash -c "npm test"`,
  `echo "$(npm test)"`) is not found, so it is not counted. `xargs`, `find -exec`, `sudo`, `!` and aliases hide a gate too.
- A stage headed by `(`, `{`, `then`, `do`, `else` or `elif` is not read, so its gate is NOT FOUND (neither counted nor
  `uncertain`): `(npm test)`, `{ npm test; }`, `cd a && (npm test)`, `x && { npm test; }`, `if x; then npm test; fi`,
  `for f in a; do npm test; done`. A gate that heads its own stage inside such a body is found and is `uncertain`: on its own
  line (`for f in a; do` newline `npm test` newline `done`), after `;` or `&&` inside a group (`(cd x && npm test)`).
- `exit` is the call's exit status for one gate only: the last command the call ran, i.e. the last stage of the last unit,
  when that unit is certain to have run, is not a background job (`&`), and, as one stage of a pipe, the text never says
  `pipefail`. Every other gate has `exit: None`. The status is never used to say which gate failed, so a call that failed
  after `npm test && npm run lint` says nothing about either. The call's status is taken as the gate's even when the shell
  failed around it (a redirection like `npm test > /no/dir/f`).
- `uncertain: True` marks a gate that was found but may not have run, and it has `exit: None`. It is set when any of these
  holds, and not otherwise: (1) the gate follows `&&` or `||`, unless the call passed (exit 0) and the gate is in the last
  `;`-list of the call and every link after that list's first command is `&&`; (2) the call has a compound command anywhere
  (a command that starts with `(`, `{`, `if`, `for`, `while`, `until`, `case`, `select`, `function`, `then`, `do`, `else`,
  `elif`, `fi`, `done`, `esac`, or is `name()`): then every gate found in the call is uncertain; (3) the gate follows `exit`
  or `return`; (4) the gate follows `set -e` (or `-o errexit`) in a call that did not pass (a non-zero or unknown status).
  `trap`, `bash -e -c`, `set +e` and a shell started with errexit are not seen.
- `gate_totals` counts `total` (known to have run), `uncertain`, `denied`, `unattributed` (gates in `total` that have no
  exit) and `parse_failed`. A call the guard refused has all its gates `denied`.
- `parse_failed`: a call whose text ends inside a quote, a `$(...)` / backtick / `<(...)` group or a heredoc with no end line,
  or nests deeper than MAX_NESTING, is not read. It is one entry `{"kind": None, "exit": None, "parse_failed": True}` and no
  gate (`gate_kinds` gives []), counted in `gate_totals` as `parse_failed` and not in `total`. A `total` above a
  `parse_failed` of 0 is exact for what was read; with `parse_failed` above 0 it is a lower bound, and the report says so.
  This is how a heredoc is read anywhere in a call (top level, `"$(...)"`, `$(...)`, backticks): an unquoted `<<WORD` opener
  makes everything up to the WORD line data, before quotes are followed again, so an apostrophe in a commit message heredoc
  opens no quote. A call that is wrong shell is parse_failed too (`git commit -m 'don't'`).
- A `#` comment inside `$(...)` is not recognised (outside one it is): an apostrophe in it opens a quote and the call is
  parse_failed. `<<` in a `(( ))` command or `[[ ]]` is read as an opener (see the heredoc bullet above).
- `|&` is read as `|` and a background `&`: a gate left of it is a background job (no exit) and a gate right of it is read as
  a separate command that certainly ran (the other direction: a gate that may not have run is counted).

Commands (`command_slips`): every rule is a text match over the command with comments and `$((...))` removed, so the words of
a rule inside a quote or a message count: `echo '<<EOF'` is a `heredoc` slip and `echo "git push"` is a `git_push` slip (the
other direction).
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
# Rules that read a file write, not a command: the content a Write/Edit puts in a test file. A wait is flagged only when it is
# a call statement in code that runs (not in a comment, a string or a regex literal) in a file of a known language: the
# first token of the statement is `sleep`, `time.sleep`, `asyncio.sleep` (py) or `sleep`, `setTimeout` (js/ts), after an
# optional `await`, and it is followed by a call whose `)` ends the statement. A statement starts at the start of a line
# (after its indentation) or after `;`. A definition (`def sleep(`, `function sleep(`) starts with another token, so it is
# not a call. A shell wait is `sleep N` where a command starts.
_STATEMENT = r"(?:^|;)[ \t]*(?:await[ \t]+)?"
FIXED_WAIT = {
    "py": re.compile(_STATEMENT + r"(?:time\.sleep|asyncio\.sleep|sleep)[ \t]*\("),
    "js": re.compile(_STATEMENT + r"(?:sleep|setTimeout)[ \t]*\("),
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
# A unit that starts a compound command (if/for/while/case, a function, a group): what is inside may run once, many times or
# never, so the scanner does not say a gate in the call ran.
_COMPOUND = re.compile(r"\s*(?:[({]|(?:if|for|while|until|case|select|function|then|do|else|elif|fi|done|esac)\b|[\w.-]+\s*\(\s*\))")
_HALTS = re.compile(r"\s*(?:exit|return)\b")  # nothing after it is known to run
_ERREXIT = re.compile(r"\s*set\s+(?:-\w+\s+)*-(?:\w*e|o\s+errexit)")  # a later failure may end the shell
_PIPEFAIL = re.compile(r"\bpipefail\b")
PARSE_FAILED = {"kind": None, "exit": None, "parse_failed": True}  # the one entry gate_runs gives a call it could not read
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


HEREDOC_OPEN = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]+)'|\"([^\"\n]+)\"|\\?(\w+))")


def _heredoc_at(text, i):
    """(delimiter, strip_tabs, index after the opener) when a heredoc opener (`<<WORD`, `<<-'WORD'`) starts at text[i], else
    None. `<<<` is a here-string, not a heredoc. The caller decides that text[i] is outside quotes and arithmetic."""
    if not text.startswith("<<", i) or text.startswith("<<<", i) or text[i - 1:i] == "<":
        return None
    m = HEREDOC_OPEN.match(text, i)
    if not m:
        return None
    return next(g for g in m.groups()[1:] if g is not None), bool(m.group(1)), m.end()


def _skip_heredoc_bodies(text, i, pending):
    """(index after the bodies, closed) of the heredocs in `pending` [(delimiter, strip_tabs)], whose first body line starts
    at text[i]. A body ends after the line that is its delimiter. `closed` is False when a body has no such line: the shell
    reads to the end of the text then, and the caller decides what that means."""
    n = len(text)
    closed = True
    for delimiter, tabs in pending:
        found = False
        while i < n and not found:
            j = text.find("\n", i)
            j = n if j < 0 else j
            line = text[i:j]
            i = min(j + 1, n)
            found = (line.lstrip("\t") if tabs else line) == delimiter
        closed = closed and found
    return i, closed


def _group_end(text, i, level=0):
    """(index after, closed) for the command substitution (`$(...)`, `<(...)`, `>(...)`), arithmetic (`$((...))`) or backtick
    pair that starts at text[i]. Quotes, nested groups and heredocs inside it are followed: the body of an unquoted
    `<<WORD` heredoc is data up to its end line, so an apostrophe in it opens no quote. `closed` is False when the text
    ends first (an open quote, group or heredoc inside it), and past MAX_NESTING levels, where the rest of the text is the group."""
    n = len(text)
    if level >= MAX_NESTING:
        return n, False
    backtick = text[i] == "`"
    arithmetic = text.startswith("$((", i)  # its `<<` is a shift, not a heredoc
    depth, j, pending, quote = 1, i + (1 if backtick else 2), [], None
    while j < n:
        c = text[j]
        if backtick and c == "`":  # a backtick ends the group wherever it stands, even inside a quote
            return j + 1, quote is None and not pending
        if c == "\\" and quote != "'":
            j += 2
        elif backtick and quote:
            quote = None if c == quote else quote
            j += 1
        elif backtick and c in "'\"":
            quote = c
            j += 1
        elif not backtick and c == "'":
            k = text.find("'", j + 1)
            if k < 0:
                return n, False
            j = k + 1
        elif not backtick and c == '"':
            j, closed = _dq_end(text, j + 1, level + 1)
            if not closed:
                return n, False
        elif not backtick and (c == "`" or text.startswith("$(", j)):
            j, closed = _group_end(text, j, level + 1)
            if not closed:
                return n, False
        elif c == "\n" and pending:
            j, closed = _skip_heredoc_bodies(text, j + 1, pending)
            pending = []
            if not closed:
                return n, False
        elif c == "<" and not arithmetic and _heredoc_at(text, j):
            delimiter, tabs, j = _heredoc_at(text, j)
            pending.append((delimiter, tabs))
        else:
            depth += (c == "(") - (c == ")") if not backtick else 0
            j += 1
            if depth == 0:
                return j, not pending
    return n, False


def _dq_end(text, i, level):
    """(index after, closed) for the double quote whose body starts at text[i]; groups in it are followed. A heredoc marker in
    a double quote is text, not an opener."""
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
        elif c == '"':
            return i + 1, True
        elif c == "`" or text.startswith("$(", i):
            i, closed = _group_end(text, i, level)
            if not closed:
                return n, False
        else:
            i += 1
    return n, False


def _units(command):
    """[(connector, [pipeline stage text, ...], background)] for a shell command. The connector is what precedes the unit:
    None for the first, then `&&`, `||` or `;` (a newline or a lone `&` counts as `;`); `background` is True for a unit a
    lone `&` ends. Quotes and backslashes hide operators, and so do command substitutions (`$(a && b)`, backticks,
    `<(...)`) and arithmetic (`$((1<<3))`); `>&`, `&>` redirects are not a connector. Comments are dropped. A heredoc body
    (from the line after its opener to its end line) is skipped: it is data, and `<<` inside quotes is not an opener.
    Empty units are dropped. Returns (units, failed): `failed` is True when the text ends inside a quote, a group or a heredoc
    (or nests deeper than MAX_NESTING), so what the units show is not the whole call."""
    units, stages, buf, op = [], [], [], None
    quote, pending, failed = None, [], False
    i, n = 0, len(command)

    def end_stage():
        stages.append("".join(buf))
        buf.clear()

    def end_unit(connector, background=False):
        nonlocal stages, op
        end_stage()
        if any(t.strip() for t in stages):
            units.append((op, stages, background))
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
                end, closed = _group_end(command, i)
                failed = failed or not closed
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
        elif c == "#" and (i == 0 or command[i - 1] in " \t\n;&|("):
            j = command.find("\n", i)
            i = n if j < 0 else j
            continue
        elif _starts_group(command, i) or command.startswith("$((", i):
            end, closed = _group_end(command, i)
            failed = failed or not closed
            buf.append(command[i:end])
            i = end
            continue
        elif c == "<" and _heredoc_at(command, i):
            delimiter, tabs, end = _heredoc_at(command, i)
            pending.append((delimiter, tabs))
            buf.append(command[i:end])
            i = end
            continue
        elif command.startswith("&&", i) or command.startswith("||", i):
            end_unit(command[i:i + 2])
            i += 1
        elif c == "|":
            end_stage()
        elif c == "\n":
            end_unit(";")
            if pending:
                i, closed = _skip_heredoc_bodies(command, i + 1, pending)
                failed = failed or not closed
                pending = []
                continue
        elif c == ";" or (c == "&" and not (buf and buf[-1] in "<>") and command[i + 1:i + 2] != ">"):
            end_unit(";", background=(c == "&"))
        else:
            buf.append(c)
        i += 1
    end_unit(";")
    return units, failed or quote is not None or bool(pending)


def _starts_group(text, i):
    """True when a command substitution or process substitution starts at text[i] outside double quotes."""
    return text[i] == "`" or (text.startswith("$(", i) and not text.startswith("$((", i)) or (
        text[i] in "<>" and text.startswith("(", i + 1))


def _mask_groups(stage):
    """`stage` with each command substitution replaced by `_`, so the head of the stage can be read. Single quotes and
    backslashes hide a substitution; double quotes do not (the shell still runs `"$(cmd)"`). Nothing in a substitution is
    read as a gate."""
    out, quote, i, n = [], None, 0, len(stage)
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
            out.append("_")
            i = _group_end(stage, i)[0]
            continue
        else:
            if c == "'" and quote is None:
                quote = "'"
            elif c == '"':
                quote = None if quote else '"'
            out.append(c)
        i += 1
    return "".join(out)


def _stage_kind(stage):
    head = stage[_PREFIX.match(stage).end():]
    for kind, pattern in GATES:
        if pattern.match(head):
            return kind
    return None


def _gates_in(units):
    """[(unit index, stage index, kind)] for the gates that head a pipeline stage, in the order they stand."""
    return [(u, j, kind) for u, (_, stages, _) in enumerate(units) for j, stage in enumerate(stages)
            for kind in [_stage_kind(_mask_groups(stage))] if kind]


def gate_kinds(command):
    """Gate kinds a Bash command names, in order, repeats kept. Only the head of a pipeline stage counts, so a commit
    message or an echo that mentions pytest is not a gate run. It does not say which ones ran; `gate_runs` does."""
    if not isinstance(command, str):
        return []
    units, failed = _units(command)
    return [] if failed else [kind for _, _, kind in _gates_in(units)]


def gate_runs(command, exit_code=None):
    """[{"kind", "exit"} | + "uncertain": True] for the gates one Bash command names, in the order they stand. `exit_code`
    is the whole command's exit status (None when the transcript does not show it).

    `exit` is a number only for a gate that is the last command the call ran: the last stage of the last unit, which is
    not a background job, is certain to have run, and (when it is one stage of a pipe) the text never mentions pipefail. It
    then is the call's exit status. Every other gate has `exit: None`, and the status of the call is not used to say which
    gate failed. A gate is certain to have run when every connector before it is `;` (or a newline), or when the call
    passed and the last `;`-list is only `&&` links; otherwise (`a && gate` that failed, `a || gate`, anything inside an
    `if`/loop/function/group, anything after `exit`, `return`, or `set -e` that may have stopped) it carries
    `uncertain: True`. A gate that appears only inside a heredoc, a command substitution, arithmetic or a quoted string is
    not found at all. A call the tokenizer cannot close (an open quote, group or heredoc) is one entry `PARSE_FAILED` and no
    gate, so the loss shows in `gate_totals` instead of the call reading as empty."""
    if not isinstance(command, str):
        return []
    units, failed = _units(command)
    if failed:
        return [dict(PARSE_FAILED)]
    n = len(units)
    last_list = max(i for i in range(n) if i == 0 or units[i][0] == ";") if n else 0
    and_tail = all(units[i][0] == "&&" for i in range(last_list + 1, n))
    passed = exit_code == 0
    compound = any(_COMPOUND.match(stages[0]) for _, stages, _ in units)
    halted = errexit = False
    certain = []
    for i, (op, stages, _) in enumerate(units):
        if compound or halted:
            certain.append(False)
        elif op in (None, ";"):
            certain.append(passed or not errexit)  # with errexit a failure may have ended the shell before this unit
        else:
            certain.append(i > last_list and passed and and_tail)
        head = stages[0]
        halted = halted or bool(_HALTS.match(head))
        errexit = errexit or bool(_ERREXIT.match(head))
    pipefail = bool(_PIPEFAIL.search(command))
    out = []
    for u, j, kind in _gates_in(units):
        _, stages, background = units[u]
        run = {"kind": kind, "exit": None}
        if not certain[u]:
            run["uncertain"] = True
        elif u == n - 1 and not background and j == len(stages) - 1 and (len(stages) == 1 or not pipefail):
            run["exit"] = exit_code
        out.append(run)
    return out


def gate_totals(runs):
    """{"total", "uncertain", "denied", "unattributed", "parse_failed"} over gate_runs entries. `total` counts runs known to
    have happened; `unattributed` is how many of those have no exit status (the call's status was not theirs to claim).
    `uncertain` runs may not have happened, `denied` ones were refused. `parse_failed` counts calls the tokenizer could not
    close: each is one entry, not a gate, so `total` is a lower bound when it is above 0."""
    runs = runs or []
    failed = sum(1 for r in runs if r.get("parse_failed"))
    runs = [r for r in runs if not r.get("parse_failed")]
    denied = sum(1 for r in runs if r.get("denied"))
    uncertain = sum(1 for r in runs if r.get("uncertain") and not r.get("denied"))
    unattributed = sum(1 for r in runs if r.get("exit") is None and not r.get("uncertain") and not r.get("denied"))
    return {"total": len(runs) - denied - uncertain, "uncertain": uncertain, "denied": denied, "unattributed": unattributed,
            "parse_failed": failed}


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
    pending = []  # shell heredocs opened on this line: their bodies start after its newline
    while i < n:
        c = text[i]
        if close and c == close and depth == 0:
            return i + 1
        if lang == "sh":
            if c == "\n" and pending:  # the bodies are data; their lines stay, empty
                j = _skip_heredoc_bodies(text, i + 1, pending)[0]  # no end line: the body runs to the end, as the shell reads it
                out.append("\n" * text.count("\n", i, j))
                i, pending = j, []
                continue
            if text.startswith("$((", i):  # arithmetic: its `<<` is a shift, not a heredoc
                j = _group_end(text, i)[0]
                out.append("\n" * text.count("\n", i, j))
                i = j
                continue
            if c == "<" and _heredoc_at(text, i):
                delimiter, tabs, j = _heredoc_at(text, i)
                pending.append((delimiter, tabs))
                out.append("<<" + "\n" * text.count("\n", i, j))
                i = j
                continue
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
        elif interpolates and lang == "sh" and text.startswith("$((", i):
            j = _group_end(text, i)[0]
            out.append("\n" * text.count("\n", i, j))
            i = j
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
    kept, shell heredoc bodies and arithmetic dropped. Its lines line up with `text`. A file that is not py, sh or js/ts has no code."""
    lang = _source_lang(path)
    if lang is None:
        return "\n" * text.count("\n"), set()
    out, marks = [], []
    _code_from(text, 0, lang, None, out, marks)
    starts = [0] + [m.end() for m in re.finditer("\n", text)]
    return "".join(out), {bisect.bisect_right(starts, k) - 1 for k in marks}


def executable_code(path, text):
    """`text` as the code that runs, for a file at `path` (see `_scan_source`). Lines line up with `text`."""
    return _scan_source(path, text)[0]


def _paren_pairs(code):
    """{index of "(": index of its ")"} over `code`, in one pass. Strings are already blanked, so parentheses in them are not
    counted; a "(" that never closes has no entry."""
    pairs, stack = {}, []
    for i, c in enumerate(code):
        if c == "(":
            stack.append(i)
        elif c == ")" and stack:
            pairs[stack.pop()] = i
    return pairs


def _call_ends_statement(code, pairs, opened, lang="py"):
    """True when the "(" at code[opened] closes and what follows its ")" ends the statement: the end of the line or text, `;`,
    or `.` (a chained call). In js a `{` on the next line after the `)` is a method body (Allman style), so the call is a
    declaration and does not end a statement."""
    i = pairs.get(opened)
    if i is None:
        return False
    i += 1
    while i < len(code) and code[i] in " \t":
        i += 1
    if lang == "js" and i < len(code) and code[i] == "\n":
        k = i
        while k < len(code) and code[k].isspace():
            k += 1
        if k < len(code) and code[k] == "{":
            return False
    return i >= len(code) or code[i] in ";.\n"


_MEMBER_HEADER = re.compile(r"\b(?:interface|class)\b")


def _member_bodies(code):
    """([index of each `{` or `}` in code], [True when the innermost braces after it are an interface or class body]). A body
    is a class or interface one when the text between the last `;`, `{` or `}` and its `{` has the word interface or class."""
    places, flags, stack, start = [], [], [], 0
    for m in re.finditer(r"[{};]", code):
        if m.group() == "{":
            stack.append(bool(_MEMBER_HEADER.search(code, start, m.start())))
        elif m.group() == "}" and stack:
            stack.pop()
        if m.group() != ";":
            places.append(m.start())
            flags.append(bool(stack) and stack[-1])
        start = m.end()
    return places, flags


def _in_member_body(bodies, at):
    """True when code[at] stands directly in an interface or class body, where `sleep(ms: number);` declares a member."""
    places, flags = bodies
    k = bisect.bisect_right(places, at) - 1
    return k >= 0 and flags[k]


def write_slips(path, text):
    """Rule ids a Write/Edit of `text` to `path` breaks: a fixed wait in the code of a test file, on a line with no
    `timer-ok` mark in a comment. It flags only a wait it can identify as code that runs (see Known limits above)."""
    if not isinstance(path, str) or not isinstance(text, str) or not TEST_PATH.search(path):
        return []
    lang = _source_lang(path)
    if lang is None:
        return []
    code, marked = _scan_source(path, text)
    pattern, continued, offset, pairs, bodies = FIXED_WAIT[lang], False, 0, None, None
    for n, line in enumerate(code.split("\n")):
        # a line after a trailing backslash continues a command or statement: it does not start one
        prefix = "\\ " if continued else ""
        continued = lang != "js" and line.rstrip().endswith("\\")
        if UNSURE not in line and n not in marked:
            for m in pattern.finditer(prefix + line):
                if lang == "sh":
                    return ["fixed_wait"]
                pairs = _paren_pairs(code) if pairs is None else pairs
                opened = offset + m.end() - len(prefix) - 1
                if _call_ends_statement(code, pairs, opened, lang):
                    if lang == "js":  # `sleep(ms: number);` directly in an interface or class body declares a member
                        bodies = _member_bodies(code) if bodies is None else bodies
                        if _in_member_body(bodies, opened):
                            continue
                    return ["fixed_wait"]
        offset += len(line) + 1
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
