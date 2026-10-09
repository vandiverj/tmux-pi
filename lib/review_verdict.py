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

usage: review_verdict.py <result.md>   prints {"verdict": ..., "notes": ..., "complete": bool, "incomplete_reason": str|null};
       exits 1 and says why when the reply is not a verdict or the file cannot be read
"""
import json
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
    return text


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


def assess(text):
    """(verdict, notes, incomplete_reason) for a reviewer's reply; ValueError when the reply is not a verdict. The stated
    verdict is always kept for the record; incomplete_reason is None only for a review with a non-empty Coverage section."""
    verdict, notes = parse(text)
    return verdict, notes, coverage_problem(text)


def approves(text):
    """True for a complete APPROVED or APPROVED_WITH_NOTES; False for REQUEST_CHANGES, an incomplete review, or anything refused."""
    try:
        verdict, _, incomplete = assess(text)
    except ValueError:
        return False
    return verdict != "REQUEST_CHANGES" and incomplete is None


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    try:
        verdict, notes, incomplete = assess(read_review(sys.argv[1]))
    except ValueError as exc:
        print(f"review_verdict: {exc}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"verdict": verdict, "notes": notes, "complete": incomplete is None, "incomplete_reason": incomplete}))
