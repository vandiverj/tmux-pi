#!/usr/bin/env python3
"""sweep.md: the brief tp-agent generates for a retry that the repeated-family gate stopped (`--route rule-first` or `design`), so the
retry fixes every site of the finding and not the one the last review cited.

Everything in it comes from the author chain's review replies (review_rounds.chain_labels is the one reader of the chain, review_verdict
the one reader of the Family: and Closes by: labels): the Family: names each review gave, every file:line token any earlier
REQUEST_CHANGES finding cites, the finding of the latest review in the reviewer's own words, how to look for the sites no review
cited, and the table the coder reports back. A finding with no readable file:line token is listed
verbatim, never dropped. Every review reply (and the Family: names read from it) goes through the redaction pass first (ids, paths and
the working directory are not review text). The file has a hard byte budget, SWEEP_BUDGET, derived from tp-spawn's cap on a context
file: findings are quoted whole while they fit and are never cut; the rest are listed as pointers (review, finding number, result.md);
the sites and the table are always complete, or SweepTooLarge is raised and the retry goes on without a sweep. No model call.

A finding is the text that ends at a `Closes by:` line (the lines after the last one, up to a Coverage or Verification heading,
are one more finding); a reply with no `Closes by:` line is one finding. Reviews that are missing, cannot be read or are not a
REQUEST_CHANGES carry no findings.
"""
import re
from pathlib import Path

import review_rounds
import review_verdict

SWEEP_NAME = "sweep.md"
SPAWN_MAX_FILE = 60000  # tp-spawn refuses a context file over this many characters (max_file in bin/tp-spawn)
SWEEP_BUDGET = SPAWN_MAX_FILE // 2  # bytes: the most sweep.md may hold, well under that cap whatever the characters are
DISPOSITIONS = ("fixed", "already correct", "out of scope", "unresolved")
TLDS = {"com", "net", "org", "io", "dev", "local", "internal", "edu", "gov", "co", "app", "ai"}  # host:port, not file:line
BARE_NAMES = ("Makefile", "Dockerfile", "Rakefile", "Gemfile", "Jenkinsfile", "Procfile")  # extensionless files that cite without a directory
_LINES = r":(\d+)(?:[-\u2013](\d+))?(?![\w])"
_ROOT = r"(?:~/|/|[A-Za-z]:[\\/]|\\\\[\w.-]+\\)?"
_DIRS = r"(?:@?[\w.-]+[\\/])+"
# path.ext:line or path.ext:line-line, in prose, backticks or code; a path with a directory needs no extension; not host:port
SITE = re.compile(r"(?<![\w/.\\~@-])(?<!//)(" + _ROOT + r"(?:" + _DIRS + r"[\w-][\w.-]*|[\w-][\w.-]*\.[A-Za-z][A-Za-z0-9]{0,7}|(?:"
                  + "|".join(BARE_NAMES) + r")))" + _LINES)
# a quoted path (backticks or quotes) taken whole, spaces and all: the content is exactly path:line
QUOTED = re.compile(r"(`)([^`\n]+)`|(\")([^\"\n]+)\"|(')([^'\n]+)'")
SPACED = re.compile(r"^(?P<path>" + _ROOT + r"[^:`\"'<>|?*\n]*[^\s:`\"'<>|?*])\s*" + _LINES.replace("(?![\\w])", "") + r"$")


def scan(text):
    """([site tokens], ambiguous): the distinct file:line (or file:line-line) tokens in `text`, in order, and whether some token has a
    path that cannot be told (a backslash-escaped space, a Windows path after a space inside a directory name). A token whose path is
    ambiguous is never emitted, not even in part. A backtick- or quote-quoted `path with spaces:12` is one token, whole."""
    found, ambiguous = [], False

    def add(path, first, last):
        token = path + ":" + first + (("-" + last) if last else "")
        if token not in found:
            found.append(token)
    blanked = list(text)
    for match in QUOTED.finditer(text):
        inner = match.group(2) or match.group(4) or match.group(6)
        spaced = SPACED.match(inner)
        if spaced and " " in spaced.group("path") and re.search(r"[\\/]", spaced.group("path")):
            add(spaced.group("path"), spaced.group(2), spaced.group(3))
            blanked[match.start():match.end()] = " " * (match.end() - match.start())
    rest = "".join(blanked)
    for match in SITE.finditer(rest):
        path, start = match.group(1), match.start(1)
        before = rest[:start]
        if before.endswith("\\ ") or (("\\" in path) and re.search(r"\\[^\s\\]+ $", before)):
            ambiguous = True
            continue
        if "." in path and path.rsplit(".", 1)[-1].lower() in TLDS and "/" not in path and "\\" not in path:
            continue
        add(path, match.group(2), match.group(3))
    return found, ambiguous


def sites(text):
    """The distinct file:line (or file:line-line) tokens in `text` (scan without the ambiguity flag)."""
    return scan(text)[0]


def findings(text):
    """[(finding text, family names, site tokens, ambiguous)] of a reviewer's reply, in order; empty for a reply that is not a verdict.
    The finding text is the reply's own lines, fenced code included; its sites come from that raw text. The boundaries and the Family:
    names come from the lines outside fenced code, comments and quotes (the view review_verdict reads labels from)."""
    try:
        review_verdict.parse(text)
        review_verdict.normalise(text.strip())
    except ValueError:
        return []
    raw = text.strip().replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").split("\n")
    fence, outside = None, []  # per raw line: the line when it is outside fenced code, else None
    for line in raw:
        match = review_verdict.FENCE.match(line)
        if fence is None:
            if match and not (match.group(1)[0] == "`" and "`" in match.group(2)):
                fence = match.group(1)
                outside.append(None)
            else:
                outside.append(line)
        else:
            outside.append(None)
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence) and not match.group(2).strip():
                fence = None
    kept = [line for line in outside if line is not None]
    clean = re.sub(r"<!--.*?-->", lambda m: "\n" * m.group().count("\n"), "\n".join(kept), flags=re.S).split("\n")
    seen = iter(clean)
    visible = [None if line is None else next(seen) for line in outside]
    blocks, current, stop = [], [], len(raw)
    for index in range(1, len(raw)):  # line 0 is the verdict
        line, shown = raw[index], visible[index]
        if shown is not None and review_verdict.QUOTING_SECTION.match(shown):  # Coverage / Verification: not findings
            stop = index
            break
    for index in range(1, stop):
        line, shown = raw[index], visible[index]
        if shown is not None and shown.strip().upper().startswith("RESULT_STATUS"):
            continue
        current.append((line, shown))
        if shown is not None and not shown.lstrip().startswith(">") and review_verdict.CLOSES_BY.search(shown.rstrip()):
            blocks.append(current)
            current = []
    blocks.append(current)
    out = []
    for block in blocks:
        block_text = "\n".join(line for line, _ in block).strip("\n")
        if not block_text.strip():
            continue
        names = list(dict.fromkeys(m.group(1) or m.group(2) for _, shown in block if shown is not None and not shown.lstrip().startswith(">")
                                   for m in [review_verdict.FAMILY.search(shown.rstrip())] if m))
        tokens, ambiguous = scan(block_text)
        out.append((block_text, [name.lower() for name in names], tokens, ambiguous))
    return out


class SweepTooLarge(ValueError):
    """The part of sweep.md that must always be complete (the families, every site, the instructions and the table) does not fit the budget."""


def _rows(state_dir, task_id, red):
    """[(review id, round, [(finding, names, sites, ambiguous)], families)] for each REQUEST_CHANGES review of the chain, oldest first,
    plus the ids of reviews that could not be read. Each reply goes through the redaction pass (review_rounds.redact_text) before
    anything is taken from it."""
    rows, unread = [], []
    for _, review, verdict, families, number in review_rounds.chain_labels(state_dir, task_id):
        if review is None or verdict != "REQUEST_CHANGES":
            if review is not None and number is not None and verdict is None:
                unread.append(review)
            continue
        try:
            text = review_verdict.read_review(review_rounds.task_path(state_dir, review) / "result.md")
        except ValueError:
            unread.append(review)
            continue
        text, _ = review_rounds.redact_text(red, text, f"review {review} for sweep.md")
        families = [review_rounds.redact_text(red, name, f"family name of {review}")[0] for name in families or []]  # label text is review text
        rows.append((review, number, findings(text), families))
    return rows, unread


def _asks_for_code(text):
    """A finding that is labelled `Closes by: code`, or carries no label."""
    labels = review_verdict.closes_by("REQUEST_CHANGES\n" + text)
    return not labels or "code" in labels


def quote(text):
    return "\n".join(("> " + line).rstrip() for line in text.split("\n"))


def _size(text):
    return len(text.encode("utf-8"))


def _fill(items, room):
    """Items are (review id, finding number, text, path of the review's result.md). Returns (quoted, pointed): the leading items that
    are quoted whole and the rest as pointers, so that the quotes and every pointer fit `room` bytes. A finding is never cut."""
    def pointer(item):
        return f"- review {item[0]}, finding {item[1]}: {item[3]}"

    def quoted(item):
        return f"From review {item[0]}, finding {item[1]}:\n\n{quote(item[2])}\n\n"
    tails = [0] * (len(items) + 1)
    for index in range(len(items) - 1, -1, -1):
        tails[index] = tails[index + 1] + _size(pointer(items[index])) + 1
    used, count = 0, 0
    for index, item in enumerate(items):
        cost = _size(quoted(item))
        if used + cost + tails[index + 1] > room:
            break
        used, count = used + cost, index + 1
    return [quoted(item) for item in items[:count]], [pointer(item) for item in items[count:]]


def build(state_dir, task_id, family, workdir=None):
    """The text of sweep.md for a retry of `task_id`; `family` is the repeated name or None (the unnamed family: two REQUEST_CHANGES
    in a row). Raises review_rounds.Refused when the chain cannot be walked or the redaction pass cannot run, and SweepTooLarge when
    the part that must be complete does not fit SWEEP_BUDGET. The order is the order of importance: families, every site, the search
    and the table, then the reviewer's words, then the verbatim findings, then pointers for what no longer fits."""
    red = review_rounds.redactor()
    rows, unread = _rows(state_dir, task_id, red)
    cited, loose = {}, []  # site -> review ids; findings with no readable site: (review id, number, text, result.md)
    rule = []
    latest = rows[-1] if rows else None
    for review, _, found, _ in rows:
        result = str(review_rounds.task_path(state_dir, review) / "result.md")
        for number, (text, names, tokens, ambiguous) in enumerate(found, 1):
            for token in tokens:
                cited.setdefault(token, [])
                if review not in cited[token]:
                    cited[token].append(review)
            if not tokens or ambiguous:
                loose.append((review, number, text, result))
            if review == latest[0] and (family in names if family else _asks_for_code(text)):
                rule.append((review, number, text, result))
    if latest and not rule:
        rule = [(latest[0], number, text, str(review_rounds.task_path(state_dir, latest[0]) / "result.md"))
                for number, (text, _, _, _) in enumerate(latest[2], 1)]
    title = f"finding family `{family}`" if family else "the unnamed family (REQUEST_CHANGES twice in a row)"
    head = [f"# Sweep: {title}", "",
            "The reviews of this work asked for changes more than once on the same kind of finding. Fixing the one site the last review "
            "cited has not ended it, so this retry fixes the rule: every site of it, not one more.", "",
            "## Families named", ""]
    named = [f"- {review} (round {number}): {', '.join(f'`{n}`' for n in families) if families else 'no Family: line'}"
             for review, number, _, families in rows]
    head += named or ["- none of the reviews could be read"]
    head += ["", "## Sites the reviews cited", ""]
    if cited:
        head.append("Every file:line token in a finding of an earlier review. Each one needs a row in your table, even when the line moved or is already right.")
        head += [""] + [f"- `{token}` (cited by {', '.join(reviews)})" for token, reviews in cited.items()]
    else:
        head.append("No review cited a file:line site in a form that could be read; the findings are below or behind their pointers.")
    if unread:
        head += ["", f"Reviews that could not be read, so their sites are not listed: {', '.join(unread)}. Open their result.md yourself."]
    root = workdir or "<your working tree>"
    tail = ["## Find the sites no review cited", "",
            "The cited sites are where to start, not where to stop. Name the mechanism the rule is about (the call, import, name, "
            "value or branch that the finding shows wrong), then enumerate every use of it:", "",
            f"- `git -C {root} grep -n -e <the shared call, import or name>`, and again for each spelling of it;",
            "- `tp-ctx` importers/references of the symbol, when `tp-ctx` is available;",
            "- the opposite and the repeat of the action (the reader of what a writer writes, the undo of what a call does).", "",
            "Read each hit. A hit that is not an instance of the rule still gets a row.", "",
            "## Table to report", "",
            "End your report with this table, one row per site: each cited site above and each one you found. Disposition is one of "
            f"{', '.join(f'`{d}`' for d in DISPOSITIONS)}; give evidence for each (the changed line, the line that is already right, why it is out of scope, what blocks it). "
            "The review of this retry checks every row and reports any site the table missed.", "",
            "| Site | Disposition | Evidence |", "| --- | --- | --- |"]
    tail += [f"| `{token}` | | |" for token in cited]
    tail += ["| (sites you found) | | |", ""]
    sections = ("## The rule, in the reviewer's words\n\n", "## Findings with no file:line site, verbatim\n\n")
    fixed = "\n".join(head) + "\n\n" + "".join(sections) + "\n".join(tail)
    pointer_note = "\n\nNot quoted, to keep this file within its size limit; open the review's result.md for them:\n\n"
    room = SWEEP_BUDGET - _size(fixed) - 2 * _size(pointer_note) - 400
    if room < 0:
        raise SweepTooLarge(f"the sites and the table alone are {_size(fixed)} bytes; the budget is {SWEEP_BUDGET}")
    first = [item for item in rule]
    second = [item for item in loose if item not in rule]
    quoted_a, pointed_a = _fill(first, room)
    used = sum(_size(q) for q in quoted_a) + sum(_size(p) + 1 for p in pointed_a)
    quoted_b, pointed_b = _fill(second, room - used)

    def section(title_line, quoted, pointed, empty):
        body = "".join(quoted) if quoted else ""
        if pointed:
            body += pointer_note.lstrip("\n") + "\n".join(pointed) + "\n\n"
        return title_line + (body or empty)
    middle = (section(sections[0], quoted_a, pointed_a, "No finding could be read from the reviews; read them (tp-agent's retry note names the prior result).\n\n"),
              section(sections[1], quoted_b, pointed_b, "None.\n\n"))
    text = "\n".join(head) + "\n\n" + "".join(middle) + "\n".join(tail)
    if _size(text) > SWEEP_BUDGET:
        raise SweepTooLarge(f"sweep.md is {_size(text)} bytes; the budget is {SWEEP_BUDGET}")
    return text


def is_sweep(spec):
    """True for a context spec that is a sweep.md written into a task directory (…/tasks/<id>/sweep.md): retry must not replay it,
    the next retry makes its own."""
    path = Path(spec)
    return path.name == SWEEP_NAME and path.parent.parent.name == "tasks"
