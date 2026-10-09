"""The ONE reader of a reply's RESULT_STATUS line.

Every consumer goes through this module: lifecycle and dependency admission (contract.py), tp-agent's records, tp-review's
gate, and the verdict reader (review_verdict.py). agent/extensions/dispatcher.ts keeps a copy of RESULT_STATUS_LEAD that
tests/test_refusal_remedies.py holds to the same answers on the same lines. It imports nothing from this repository, so
every other module can use it.

The line is `RESULT_STATUS:` and a status word, plain or decorated the way a model decorates a line: a heading marker (###), a
list marker, bold, italics or backticks around it. Any other text before the key ("Note: RESULT_STATUS: complete") is not a
status line. Text after the status word is not read (it never was).
"""
import re

STATUSES = ("complete", "empty", "blocked", "incomplete")
# up to three spaces of indent (four is a Markdown code block: a quoted line, not a status), then any heading or list markers,
# then emphasis or backticks, then the key; after the status word anything but a letter or digit (so `complete__` and `complete**`)
RESULT_STATUS_LEAD = r"^[ \t]{0,3}(?:(?:#{1,6}|[-+*]|\d+[.)])[ \t]+)*[*_`~]*RESULT_STATUS[ \t]*[*_`~]*[ \t]*:"
RESULT_STATUS_WORD = r"[\s*_`~]*(complete|empty|blocked|incomplete)(?![^\W_])"
RESULT_STATUS_RE = re.compile(RESULT_STATUS_LEAD + RESULT_STATUS_WORD, re.I | re.M)
RESULT_STATUS_LINE = re.compile(RESULT_STATUS_LEAD + r".*$", re.I | re.M)
# What a refusal tells a worker (or main) that a reply needs
RESULT_STATUS_FORM = ("a line of its own that reads RESULT_STATUS: complete (or empty, blocked or incomplete); Markdown around "
                      "it (### or ** or backticks) is fine, other text before it on the line is not")


def result_status(reply):
    """complete, empty, blocked or incomplete when `reply` has a RESULT_STATUS line (the first one), else None."""
    match = RESULT_STATUS_RE.search(reply if isinstance(reply, str) else "")
    return match.group(1).lower() if match else None


if __name__ == "__main__":  # result_status.py FILE: prints the status, exits 1 when there is none (bin/tp-review's gate)
    import sys
    try:
        with open(sys.argv[1], "rb") as handle:
            found = result_status(handle.read().decode("utf-8", "replace"))
    except (IndexError, OSError):
        sys.exit("usage: result_status.py FILE")
    if not found:
        sys.exit(1)
    print(found)
