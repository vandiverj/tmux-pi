#!/usr/bin/env python3
"""A reviewer's verdict: the whole first line of its reply is APPROVED, APPROVED_WITH_NOTES or REQUEST_CHANGES
(or "REQUEST_CHANGES: reason", the one longer form).

Anything else is refused, never read as an approval or a rejection. APPROVED_WITH_NOTES approves, and its
notes (the reply after the verdict line, minus the RESULT_STATUS line) are required and reported. The
dispatcher's TypeScript parser (agent/extensions/dispatcher.ts, parseVerdict) follows the same rules.

A review must end with a Coverage section that says something (coverage_problem defines it). A review file is read only
through read_review (strict UTF-8, refuses missing, empty, oversized or undecodable files). A reply without one keeps its stated verdict for the
record but is incomplete (assess): it never approves, whatever the verdict line says. assess() is the one place that
decides; every consumer of a verdict (tp-agent, task_record, routing_evals, the dispatcher, the collect tools) calls it
or this command.

A REQUEST_CHANGES whose findings are labelled `Closes by:` (code, check, decision or none) and none is `code` is unusable
(request_changes_problem): neither an approval nor a rejection. One with a `code` label, or with no labels, rejects as before.

When a task's brief or reviewer contract has a numbered list under `Done means` or `Acceptance criteria`, bin/tp-review records
its item numbers beside the review's result.md (acceptance-items.json); read_review then returns a ReviewText that carries them,
and assess calls the reply incomplete, like a missing Coverage section, when it has no `Acceptance criteria` section or that
section names fewer item numbers than the list has (acceptance_problem). With no list nothing changes.

usage: review_verdict.py <result.md>   prints {"verdict": ..., "notes": ..., "complete": bool, "incomplete_reason": str|null,
       "closes_by": [label, ...], "unusable_reason": str|null};
       exits 1 and says why when the reply is not a verdict or the file cannot be read
       review_verdict.py briefs <request.md>   prints {"briefs": [path, ...]}: the absolute .md paths the request names (bin/tp-review)
       review_verdict.py supplied <flag> <file>   prints why a file Main supplies is refused and exits 1, or prints nothing (bin/tp-review)
       review_verdict.py acceptance <file>...   prints {"items": [n, ...]}: the acceptance criteria numbers in those files (bin/tp-review)
"""
import json
import os
import re
import sys

from result_status import RESULT_STATUS_LEAD, RESULT_STATUS_LINE  # the one reader of the RESULT_STATUS line

VERDICTS = ("APPROVED", "APPROVED_WITH_NOTES", "REQUEST_CHANGES")
# The whole first line: exactly one of the three tokens, or (the one accepted longer form) "REQUEST_CHANGES: reason".
# The dispatcher's parseVerdict (agent/extensions/dispatcher.ts) must keep the same pattern.
FIRST_LINE = re.compile(r"(APPROVED_WITH_NOTES|APPROVED|REQUEST_CHANGES)(?::[ \t]*(\S.*))?")
FIRST_RESULT_STATUS = re.compile(RESULT_STATUS_LEAD + r"(.*)$", re.I | re.M)


def first_line_verdict(line):
    """(verdict, reason) when `line` is a whole verdict line, else None. Only REQUEST_CHANGES takes a reason."""
    match = FIRST_LINE.fullmatch(line.rstrip())
    if not match:
        return None
    verdict, reason = match.group(1), match.group(2) or ""
    if reason and verdict != "REQUEST_CHANGES":
        return None
    return verdict, reason


def parse(text):
    """(verdict, notes) for a reviewer's reply; ValueError when the reply is not a verdict."""
    body = text.strip().strip("\ufeff").strip()
    first, _, rest = body.partition("\n")
    found = first_line_verdict(first)
    if not found:
        raise ValueError("reviewer verdict unparseable: the whole first line must be APPROVED, APPROVED_WITH_NOTES or "
                         "REQUEST_CHANGES (or 'REQUEST_CHANGES: reason'), without formatting")
    verdict, reason = found
    # the only contradiction read is a RESULT_STATUS line (at a line start); notes may quote earlier rounds' verdicts in prose
    status = next((m for m in FIRST_RESULT_STATUS.finditer(rest) if re.search(r"\bREQUEST_CHANGES\b", m.group(1))), None)
    if verdict != "REQUEST_CHANGES" and status:
        raise ValueError(f"reviewer verdict contradicts itself: {verdict} but its RESULT_STATUS line says REQUEST_CHANGES")
    notes = "\n".join(part for part in (reason, RESULT_STATUS_LINE.sub("", rest).strip()) if part)
    if verdict == "APPROVED_WITH_NOTES" and not notes:
        raise ValueError("APPROVED_WITH_NOTES has no notes; list them or reply APPROVED")
    return verdict, notes


MAX_REVIEW_BYTES = 1_000_000
FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
# the whole line is the word Coverage, with at most a Markdown heading marker, a bold marker and a colon around it
COVERAGE_HEADING = re.compile(r"(?i)^[ \t]{0,3}(?:#{1,6}[ \t]+)?(?:\*\*|__)?[ \t]*coverage[ \t]*:?[ \t]*(?:\*\*|__)?[ \t]*:?[ \t]*#*[ \t]*$")
LIST_MARKER = re.compile(r"^(?:[-*+]|\d+[.)])[ \t]+(?:\[[ xX]\][ \t]+)?")
# KEY: value metadata: an upper-case key (RESULT_STATUS, VERDICT) with anything after it, or any short key with at most two words
UPPER_KEY = re.compile(r"^[A-Z][A-Z0-9_]*[ \t]*:")
SHORT_PAIR = re.compile(r"^[^:]{1,40}:[ \t]*(?:\S+(?:[ \t]+\S+)?)?$")
WORD = re.compile(r"[A-Za-z0-9][\w./'-]*")
# Coverage text that says nothing: a placeholder, not a report of what was reviewed
BOILERPLATE = {"none", "n/a", "na", "nil", "tbd", "todo", "see above", "same", "same as above", "as above", "ok", "done",
               "complete", "completed", "all", "yes", "reviewed", "not applicable", "to be determined", "no coverage",
               "nothing to report", "all reviewed", "everything reviewed", "see the review above"}


class ReviewMissing(ValueError):
    """The review result file does not exist."""


def read_review(path):
    """The text of a review result file, the only way any consumer reads one for a verdict. Strict: a missing file
    (ReviewMissing), an unreadable, undecodable (not UTF-8), empty or oversized one raises ValueError with the reason.
    Nothing is replaced, escaped or defaulted."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_REVIEW_BYTES + 1)
    except FileNotFoundError:
        raise ReviewMissing(f"the review result {path} does not exist") from None
    except OSError as exc:
        raise ValueError(f"the review result {path} is unreadable: {exc}") from None
    if len(data) > MAX_REVIEW_BYTES:
        raise ValueError(f"the review result {path} is larger than {MAX_REVIEW_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"the review result {path} is not valid UTF-8: {exc}") from None
    if not text.strip():
        raise ValueError(f"the review result {path} is empty")
    return ReviewText(text, recorded_items(path))


class ReviewText(str):
    """A review reply read from a file, with the numbers of the acceptance criteria its task listed (None: no list), so
    that assess() applies the Acceptance criteria rule for every consumer that reads a reply through read_review. strip()
    keeps them: tp-agent, lane_steps, routing_evals and task_record all pass the text on after it."""

    def __new__(cls, text, items=None):
        self = super().__new__(cls, text)
        self.acceptance_items = list(items) if items else None
        return self

    def strip(self, *chars):
        return ReviewText(super().strip(*chars), self.acceptance_items)


ITEMS_FILE = "acceptance-items.json"  # beside a review's result.md; bin/tp-review writes it when the task listed numbered criteria


def recorded_items(result_path):
    """The item numbers bin/tp-review recorded beside `result_path`, or None when it recorded none. A record that exists
    but cannot be read refuses: a review is never judged against a guess."""
    path = os.path.join(os.path.dirname(os.fspath(result_path)), ITEMS_FILE)
    try:
        with open(path, "rb") as handle:
            value = json.loads(handle.read(MAX_REVIEW_BYTES).decode("utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ValueError(f"the acceptance items recorded in {path} are unreadable: {exc}") from None
    items = value.get("items") if isinstance(value, dict) else None
    if not isinstance(items, list) or not all(isinstance(i, int) and not isinstance(i, bool) and i > 0 for i in items):
        raise ValueError(f"the acceptance items recorded in {path} are not a list of item numbers")
    return items or None


def normalise(text):
    """`text` as the Coverage check sees it: CRLF and CR as LF, no fenced blocks (``` or ~~~, any indent, closed by a fence of
    the same kind at least as long), no HTML comments, no blockquote lines (any nesting). ValueError when a fence or a comment
    is never closed: such a review is malformed and cannot be checked."""
    lines, fence = [], None
    for line in text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").split("\n"):
        match = FENCE.match(line)
        if fence is None:
            if match and not (match.group(1)[0] == "`" and "`" in match.group(2)):
                fence = match.group(1)
            else:
                lines.append(line)
        elif match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence) and not match.group(2).strip():
            fence = None
    if fence is not None:
        raise ValueError("a fenced code block is never closed")
    joined = "\n".join(lines)
    stripped = re.sub(r"<!--.*?-->", "", joined, flags=re.S)
    if "<!--" in stripped:
        raise ValueError("an HTML comment is never closed")
    return "\n".join(line for line in stripped.split("\n") if not line.lstrip().startswith(">"))


def _says_something(line):
    """True when one line of a Coverage body is a statement: not metadata, not a placeholder, at least three words."""
    line = LIST_MARKER.sub("", line.strip())
    line = re.sub(r"^[*_`~\s]+|[*_`~\s]+$", "", line)
    if not line or UPPER_KEY.match(line) or SHORT_PAIR.match(line):
        return False
    if " ".join(re.sub(r"[^\w/ ]+", " ", line.lower()).split()) in BOILERPLATE:
        return False
    return len(WORD.findall(line)) >= 3


def coverage_problem(text):
    """Why `text` has no usable Coverage section, or None.

    The text is normalised once (normalise). The section starts at a line that is only the word Coverage (with at most a
    Markdown heading or bold marker and a colon); the last such line wins. Its body runs to the next Markdown heading and
    must hold at least one line that, without list and emphasis markers, is not a KEY: value metadata line or a placeholder
    and has at least three words. This catches an omitted or empty section; it cannot tell a reviewer who writes false
    coverage from one who writes true coverage."""
    try:
        lines = normalise(text.strip().strip("\ufeff")).split("\n")[1:]  # the first line is the verdict
    except ValueError as exc:
        return f"the review cannot be checked for a Coverage section: {exc}"
    starts = [n for n, line in enumerate(lines) if COVERAGE_HEADING.match(line)]
    if not starts:
        return "the review has no Coverage section"
    body = []
    for line in lines[starts[-1] + 1:]:
        if re.match(r"^[ \t]{0,3}#{1,6}[ \t]", line):
            break
        body.append(line)
    if not any(_says_something(line) for line in body):
        return "the review's Coverage section is empty"
    return None


# The acceptance list of a brief or contract: a line that is only `Done means` or `Acceptance criteria` (Markdown heading of any
# level, bold, or plain, with an optional colon), then the numbered items before the next heading. A reply to a task that had one
# must carry an `Acceptance criteria` section that names every item number (bin/tp-review tells the reviewer so).
_LIST_HEADING = re.compile(r"(?i)^[ \t]{0,3}(?:#{1,6}[ \t]+)?(?:\*\*|__)?[ \t]*(done means|acceptance criteria)[ \t]*:?[ \t]*(?:\*\*|__)?[ \t]*:?[ \t]*#*[ \t]*$")
_ANY_HEADING = re.compile(r"^[ \t]{0,3}(?:#{1,6}[ \t]|(?:\*\*|__)[^*_\n]+(?:\*\*|__)[ \t]*:?[ \t]*$)")
_ITEM = re.compile(r"^ {0,3}(\d+)[.)][ \t]+")
_REPLY_ROW = re.compile(r"(?i)^[|\s]*(?:[-*+][ \t]+)?(?:\*\*|__)?[ \t]*(?:ac|item|criterion|#)?[ \t]*(\d+)\b")


def acceptance_items(*texts):
    """The item numbers, sorted, of the numbered lists that follow a `Done means` or `Acceptance criteria` heading in each of
    `texts` (a list in a fenced block is not read; only top-level items count). [] when there is no such list."""
    found = set()
    for text in texts:
        inside, fence = False, None
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            match = FENCE.match(line)
            if fence is not None:
                if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence) and not match.group(2).strip():
                    fence = None
            elif match and not (match.group(1)[0] == "`" and "`" in match.group(2)):
                fence = match.group(1)
            elif _LIST_HEADING.match(line):
                inside = True
            elif _ANY_HEADING.match(line):
                inside = False
            elif inside and (item := _ITEM.match(line)):
                found.add(int(item.group(1)))
    return sorted(found)


# An absolute path ending in .md in a request ("Read and carry out the brief /abs/path.md"): no space, quote, bracket or comma in it
_NAMED_BRIEF = re.compile(r"(?<![\w.~/-])(/[^\s`'\"<>()\[\]|,;]+\.md)(?![\w/-]|\.\w)")


def named_briefs(request):
    """The absolute paths ending in .md that `request` names, in order of first appearance, each once. Only names: whether a
    path may be read is bin/tp-review's check (the one --contract gets)."""
    found = []
    for match in _NAMED_BRIEF.finditer(request):
        if match.group(1) not in found:
            found.append(match.group(1))
    return found


def supplied_problem(flag, path):
    """Why a file Main supplies (tp-review --evidence, --contract, --dispositions, a brief a request names; tp-agent retry's fix
    brief) is refused, as one line that names the flag and the file, or None. Refused: a file inside the trust store
    (~/.config/tmux-pi/trust, ~/.config/tmux-pi/state/trust under the home in the password database, never $HOME; a link is judged
    by where it leads), a `.env*` file, and one that is missing, not a regular file, unreadable or empty. The trust store and a
    .env file are refused by name before the file is looked at."""
    import pwd
    import unicodedata

    def fold(p):
        return unicodedata.normalize("NFC", p).casefold()

    def refuse(reason):
        return f"{flag} file '{path}' {reason}; refused"
    if not path.strip():
        return refuse("is not a file name")
    try:
        real = os.path.realpath(path)
    except (OSError, ValueError):
        return refuse("cannot be resolved")
    home = pwd.getpwuid(os.getuid()).pw_dir
    for store in (".config/tmux-pi/trust", ".config/tmux-pi/state/trust"):
        root = fold(os.path.realpath(os.path.join(home, store)))
        for p in (fold(real), fold(os.path.abspath(path))):
            if p == root or p.startswith(root + os.sep):
                return refuse("is inside the trust store")
    for name in (os.path.basename(path), os.path.basename(real)):
        if name.lower().startswith(".env"):
            return refuse("is a .env file")
    if not os.path.exists(path):
        return refuse("does not exist")
    if not os.path.isfile(path):
        return refuse("is not a regular file")
    try:
        with open(path, "rb") as handle:
            empty = not handle.read(1)
    except OSError:
        return refuse("cannot be read")
    return refuse("is empty") if empty else None


def acceptance_problem(text, items):
    """Why `text` does not account for the acceptance criteria numbered `items`, or None. The reply needs a section that is only
    the words Acceptance criteria (as Coverage is found) whose lines name every item number. This counts numbers; it cannot tell
    a true row from a false one. No items: nothing is asked."""
    if not items:
        return None
    try:
        lines = normalise(text.strip().strip("﻿")).split("\n")[1:]  # the first line is the verdict
    except ValueError as exc:
        return f"the review cannot be checked for an Acceptance criteria section: {exc}"
    starts = [n for n, line in enumerate(lines) if (m := _LIST_HEADING.match(line)) and m.group(1).lower() == "acceptance criteria"]
    if not starts:
        return f"the review has no Acceptance criteria section (the task listed item(s) {', '.join(map(str, items))})"
    named = set()
    for line in lines[starts[-1] + 1:]:
        if _ANY_HEADING.match(line) or COVERAGE_HEADING.match(line):  # the lines coverage_problem and the list reader treat as a heading
            break
        if row := _REPLY_ROW.match(line):
            named.add(int(row.group(1)))
    missing = [i for i in items if i not in named]
    if missing:
        return f"the review's Acceptance criteria section does not account for item{'s' if len(missing) > 1 else ''} {', '.join(map(str, missing))}"
    return None


def assess(text):
    """(verdict, notes, incomplete_reason) for a reviewer's reply; ValueError when the reply is not a verdict. The stated
    verdict is always kept for the record; incomplete_reason is None only for a review with a non-empty Coverage section and,
    when its task listed numbered acceptance criteria (a ReviewText from read_review carries them), an Acceptance criteria
    section that names every item."""
    verdict, notes = parse(text)
    return verdict, notes, coverage_problem(text) or acceptance_problem(text, getattr(text, "acceptance_items", None))


# "Closes by: code|check|decision|none" ends each finding (bin/tp-review findings_rule). Case-insensitive; Markdown bold may surround
# the label ("**Closes by:** `check`", "**Closes by: code.**"), the value may be in backticks, and a full stop may follow it. The
# label must end its line: a value that is not one of the four, or text after the label, makes it no label.
_VALUES = "code|check|decision|none"


def _label(words, values):
    """The pattern for a `<words>: <value>` label on a line of its own; `values` is a regex with no groups."""
    return re.compile(r"(?<!\w)" + words + r"(?:[ \t]*(?:\*\*|__))?[ \t]*:[ \t]*(?:\*\*|__)?[ \t]*(?:`(" + values + r")`|(" + values +
                      r"))[ \t]*\.?[ \t]*(?:\*\*|__)?[ \t]*$", re.I)


CLOSES_BY = _label(r"closes[ \t]+by", _VALUES)
# "Family: <short-kebab-name>" before the Closes by: line of a finding that repeats an earlier one (a new finding may name one too);
# read by the same rule as Closes by:, with the name folded to lower case.
FAMILY = _label("family", r"[a-z0-9][a-z0-9_-]*")
# the lines a label never counts under: a Coverage or Verification heading (a Markdown heading, a bold line, or the bare word) to
# the next Markdown heading
QUOTING_SECTION = re.compile(r"(?i)^[ \t]{0,3}(?:#{1,6}[ \t]+)?(?:(?:\*\*|__)[ \t]*)?(?:coverage|verification)\b(?:[ \t]*[:*_#]|[ \t]*$)")
MARKDOWN_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]")
UNUSABLE_CHANGES = "REQUEST_CHANGES without a Closes by: code finding"


def _quoted(prefix):
    """True when the text before a label leaves a backtick or a double quote open, so the label is being quoted."""
    return prefix.count("`") % 2 == 1 or prefix.count('"') % 2 == 1 or prefix.count("“") > prefix.count("”")


def _labels(text, pattern):
    """The values of `pattern` labels that sit on a finding in a reviewer's reply, lower-case, in order of appearance (repeats
    kept). A label counts when it ends its line (optionally bold, in backticks around the value, or followed by a full stop) and is
    none of: inside backticks or double quotes, in a fenced block or a `>` blockquote, or under a Coverage or Verification heading
    (a Markdown heading or a line that starts with the bold word; it runs to the next Markdown heading). A quoted label in the
    reviewer's prose is therefore no label. A reply that cannot be normalised (a fence or comment never closed) has none."""
    try:
        lines = normalise(text.strip()).split("\n")[1:]  # the first line is the verdict; normalise drops a leading BOM
    except ValueError:
        return []
    found, quoting = [], False
    for line in lines:
        if MARKDOWN_HEADING.match(line):
            quoting = False
        if QUOTING_SECTION.match(line):
            quoting = True
        match = None if quoting else pattern.search(line.rstrip())
        if match and not _quoted(line[:match.start()]):
            found.append((match.group(1) or match.group(2)).lower())
    return found


def closes_by(text):
    """The `Closes by:` labels that sit on a finding in a reviewer's reply, lower-case, in order of appearance (repeats kept); []
    when it has none. This is the one rule for what counts (see _labels). A reply that cannot be normalised has none: the safe
    reading is that nothing says the review asks for no change."""
    return _labels(text, CLOSES_BY)


def families(text):
    """The distinct `Family:` names a reviewer's reply gives its findings, lower-case, in order of first appearance; [] when it names
    none or cannot be normalised. Counted by the rule for `Closes by:` labels."""
    return list(dict.fromkeys(_labels(text, FAMILY)))


def request_changes_problem(text):
    """Why a REQUEST_CHANGES reply is not a rejection, or None. It is not one when its findings are labelled and none is
    `Closes by: code`: nothing in the tree has to change, so it asks for no work. A reply with a `code` label, with no labels
    (an older reviewer, another template), or with any other verdict or none is not touched. It is no approval either."""
    try:
        verdict = parse(text)[0]
    except ValueError:
        return None
    labels = closes_by(text)
    return UNUSABLE_CHANGES if verdict == "REQUEST_CHANGES" and labels and "code" not in labels else None


def approves(text):
    """True for a complete APPROVED or APPROVED_WITH_NOTES; False for REQUEST_CHANGES, an incomplete review, or anything refused."""
    try:
        verdict, _, incomplete = assess(text)
    except ValueError:
        return False
    return verdict != "REQUEST_CHANGES" and incomplete is None


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "briefs":  # for bin/tp-review: the .md paths a request names
        try:
            print(json.dumps({"briefs": named_briefs(open(sys.argv[2], encoding="utf-8", errors="replace").read())}))
        except OSError as exc:
            print(f"review_verdict: {exc}", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)
    if len(sys.argv) == 4 and sys.argv[1] == "supplied":  # for bin/tp-review: prints why FILE is refused and exits 1, or prints nothing
        problem = supplied_problem(sys.argv[2], sys.argv[3])
        if problem:
            print(problem)
            sys.exit(1)
        sys.exit(0)
    if len(sys.argv) >= 3 and sys.argv[1] == "acceptance":  # for bin/tp-review: the item numbers of the brief and contract files
        try:
            texts = [open(path, encoding="utf-8", errors="replace").read() for path in sys.argv[2:]]
        except OSError as exc:
            print(f"review_verdict: {exc}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps({"items": acceptance_items(*texts)}))
        sys.exit(0)
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    try:
        text = read_review(sys.argv[1])
        verdict, notes, incomplete = assess(text)
    except ValueError as exc:
        print(f"review_verdict: {exc}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"verdict": verdict, "notes": notes, "complete": incomplete is None, "incomplete_reason": incomplete,
                      "closes_by": closes_by(text), "unusable_reason": request_changes_problem(text)}))
