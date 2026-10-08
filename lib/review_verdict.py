#!/usr/bin/env python3
"""A reviewer's verdict: the whole first line of its reply is APPROVED, APPROVED_WITH_NOTES or REQUEST_CHANGES
(or "REQUEST_CHANGES: reason", the one longer form).

Anything else is refused, never read as an approval or a rejection. APPROVED_WITH_NOTES approves, and its
notes (the reply after the verdict line, minus the RESULT_STATUS line) are required and reported. The
dispatcher's TypeScript parser (agent/extensions/dispatcher.ts, parseVerdict) follows the same rules.

usage: review_verdict.py <result.md>   prints {"verdict": ..., "notes": ...}; exits 1 and says why when refused
"""
import json
import re
import sys

VERDICTS = ("APPROVED", "APPROVED_WITH_NOTES", "REQUEST_CHANGES")
# The whole first line: exactly one of the three tokens, or (the one accepted longer form) "REQUEST_CHANGES: reason".
# The dispatcher's parseVerdict (agent/extensions/dispatcher.ts) must keep the same pattern.
FIRST_LINE = re.compile(r"(APPROVED_WITH_NOTES|APPROVED|REQUEST_CHANGES)(?::[ \t]*(\S.*))?")
RESULT_STATUS_LINE = re.compile(r"(?mi)^RESULT_STATUS:.*$")
FIRST_RESULT_STATUS = re.compile(r"(?mi)^RESULT_STATUS:(.*)$")


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
    # the only contradiction read is the current RESULT_STATUS line; the notes may quote earlier rounds' verdicts
    status = FIRST_RESULT_STATUS.search(rest)
    if verdict != "REQUEST_CHANGES" and status and re.search(r"\bREQUEST_CHANGES\b", status.group(1)):
        raise ValueError(f"reviewer verdict contradicts itself: {verdict} but its RESULT_STATUS line says REQUEST_CHANGES")
    notes = "\n".join(part for part in (reason, RESULT_STATUS_LINE.sub("", rest).strip()) if part)
    if verdict == "APPROVED_WITH_NOTES" and not notes:
        raise ValueError("APPROVED_WITH_NOTES has no notes; list them or reply APPROVED")
    return verdict, notes


def approves(text):
    """True for APPROVED and APPROVED_WITH_NOTES; False for REQUEST_CHANGES or anything refused."""
    try:
        return parse(text)[0] != "REQUEST_CHANGES"
    except ValueError:
        return False


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    try:
        with open(sys.argv[1], encoding="utf-8", errors="surrogateescape") as handle:
            verdict, notes = parse(handle.read())
    except (OSError, ValueError) as exc:
        print(f"review_verdict: {exc}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"verdict": verdict, "notes": notes}))
