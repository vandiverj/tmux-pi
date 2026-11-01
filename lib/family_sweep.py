#!/usr/bin/env python3
"""sweep.md: the brief tp-agent generates for a retry that the repeated-family gate stopped (`--route rule-first` or `design`), so the
retry fixes every site of the finding and not the one the last review cited.

Everything in it comes from the author chain's review replies (review_rounds.chain_labels is the one reader of the chain, review_verdict
the one reader of the Family: and Closes by: labels): the Family: names each review gave, every file:line token any earlier
REQUEST_CHANGES finding cites, the finding of the latest review in the reviewer's own words, how to look for the sites no review
cited, and the table the coder reports back. A finding with no readable file:line token is listed
verbatim, never dropped. Every review reply goes through the redaction pass once, first, and the findings, Family: names and sites are all read from that redacted text (the gate family passed in is printed only as a name those replies give) (ids, paths and
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
# a quoted path (backticks or quotes) taken whole, spaces and all. A single quote opens only after a non-word character and closes
# only before one, so the apostrophe of "reviewer's" or "don't" is not a quote.
QUOTED = re.compile(r"(`)([^`\n]+)`|(\")([^\"\n]+)\"|(?<![\w'])(')([^'\n]+)'(?![\w])")
_COL = r"(?::\d+)?"  # the column of path:line:col, dropped
SPACED = re.compile(r"^(?P<path>" + _ROOT + r"[^:`\"'<>|?*\n]*[^\s:`\"'<>|?*])\s*" + _LINES.replace("(?![\\w])", "") + _COL + r"$")


def _quoted(inner):
    """(kind, token parts) for the inside of a quoted span. The whole span must be path:line, path:line:col or path:line-line, the path
    spaces and all ('site', (path, first, last)). Anything else that holds a site (prose around one, two sites) is 'ambiguous': it gives no
    token, not even a part of one. A span that holds no site at all (code, a host:port) is 'none'. The unquoted scanner never runs
    inside a quoted span except to see whether the span holds a site."""
    inner = inner.strip()  # a code span may be padded
    whole = SITE.match(inner)
    if whole and re.fullmatch(_COL, inner[whole.end():]):
        path = whole.group(1)
        if "." in path and path.rsplit(".", 1)[-1].lower() in TLDS and "/" not in path and "\\" not in path:
            return "none", None
        return "site", (path, whole.group(2), whole.group(3))
    spaced = SPACED.match(inner)
    if spaced and " " in spaced.group("path") and re.search(r"[\\/]", spaced.group("path")):
        return "site", (spaced.group("path"), spaced.group(2), spaced.group(3))
    tokens, ambiguous = _unquoted(inner)
    return ("ambiguous" if tokens or ambiguous else "none"), None


def _unquoted(text):
    found, ambiguous = [], False
    for match in SITE.finditer(text):
        path, start = match.group(1), match.start(1)
        before = text[:start]
        if before.endswith("\\ ") or (("\\" in path) and re.search(r"\\[^\s\\]+ $", before)):
            ambiguous = True
            continue
        if "." in path and path.rsplit(".", 1)[-1].lower() in TLDS and "/" not in path and "\\" not in path:
            continue
        token = path + ":" + match.group(2) + (("-" + match.group(3)) if match.group(3) else "")
        if token not in found:
            found.append(token)
    return found, ambiguous


def scan(text):
    """([site tokens], ambiguous): the distinct file:line (or file:line-line) tokens in `text`, in order, and whether some token has a
    path that cannot be told (a backslash-escaped space, a Windows path after a space inside a directory name, a quoted span that is
    more than one site). A token whose path is ambiguous is never emitted, not even in part. A backtick- or quote-quoted
    `path with spaces:12` is one token, whole, when the span is exactly that."""
    spans, ambiguous = [], False
    blanked = list(text)
    for match in QUOTED.finditer(text):
        kind, parts = _quoted(match.group(2) or match.group(4) or match.group(6))
        if kind == "none" and re.match(r":\d", text[match.end():]) and SITE.match(match.group(0)[1:-1].strip() + ":0"):
            kind = "ambiguous"  # `lib/a.py`:12, the line outside the quotes: a site that cannot be read whole
        if kind == "ambiguous":
            ambiguous = True
        elif kind == "site":
            spans.append((match.start(), parts))
        blanked[match.start():match.end()] = " " * (match.end() - match.start())  # the scanner below never reads inside a span
    rest = "".join(blanked)
    tokens, unsure = _unquoted(rest)
    ambiguous = ambiguous or unsure
    placed = [(start, path + ":" + first + (("-" + last) if last else "")) for start, (path, first, last) in spans]
    for match in SITE.finditer(rest):  # the unquoted tokens at their positions, so the order is the order of the text
        token = match.group(1) + ":" + match.group(2) + (("-" + match.group(3)) if match.group(3) else "")
        if token in tokens:
            placed.append((match.start(), token))
    found = []
    for _, token in sorted(placed, key=lambda item: item[0]):
        if token not in found:
            found.append(token)
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
    """([(review id, round, [(finding, names, sites, ambiguous)], families)], unread) for each REQUEST_CHANGES review of the chain,
    oldest first, plus the ids of reviews that could not be read. Each reply goes through the redaction pass (review_rounds.redact_text)
    once, and the findings, the Family: names and the sites are all read from that redacted text, never from the raw reply."""
    rows, unread = [], []
    for _, review, verdict, _, number in review_rounds.chain_labels(state_dir, task_id):
        if review is None or verdict != "REQUEST_CHANGES":
            if review is not None and number is not None and verdict is None:
                unread.append(review)
            continue
        try:
            raw = review_verdict.read_review(review_rounds.task_path(state_dir, review) / "result.md")
        except ValueError:
            unread.append(review)
            continue
        text, _ = review_rounds.redact_text(red, raw, f"review {review} for sweep.md")
        families = review_verdict.families(text)
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
    withheld = False
    if family:  # a value from outside is used only as a name the redacted reviews give; otherwise the redaction pass over it, or nothing
        if family not in {name for _, _, _, names in rows for name in names}:
            family, withheld = None, True  # the pass changed the name (or no review gives it): it is not repeated verbatim here
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
    title = (f"finding family `{family}`" if family else "a family whose name the redaction pass changed" if withheld
             else "the unnamed family (REQUEST_CHANGES twice in a row)")
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
            "The review of this retry confirms every row against the tree, adds any site the table missed as a row, and cannot approve a table "
            "that leaves a row `unresolved` without a `Closes by:` note in its Evidence.", "",
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


# --- the review of the retry (tp-agent review attaches sweep.md and the coder's table; bin/tp-review runs `family_sweep.py review`) ---
TABLE_HEAD = re.compile(r"^[ \t]*\|[ \t]*site[ \t]*\|[ \t]*disposition[ \t]*\|[ \t]*evidence[ \t]*\|[ \t]*$", re.I)
CLOSES_NOTE = re.compile(r"(?i)closes[ \t]+by[ \t]*(?:\*\*|__)?[ \t]*:[ \t]*(?:\*\*|__)?(.*)$")
SWEEP_TITLE = re.compile(r"^# Sweep: finding family `([a-z0-9][a-z0-9_-]*)`", re.M)


def _cells(line):
    parts = re.split(r"(?<!\\)\|", line.strip())
    if parts and not parts[0].strip():
        parts = parts[1:]
    if parts and not parts[-1].strip():
        parts = parts[:-1]
    return [part.replace("\\|", "|").strip() for part in parts]


def report_table(text):
    """The rows (site, disposition, evidence) of the last `Site | Disposition | Evidence` table in a coder's report, the disposition
    folded to lower case without Markdown marks; None when the report has no such table. The separator row is skipped; a row with
    fewer cells is padded, with more cells its extra cells join the evidence."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    rows = None
    for number, line in enumerate(lines):
        if not TABLE_HEAD.match(line):
            continue
        rows = []
        for row in lines[number + 1:]:
            if not row.strip().startswith("|"):
                break
            cells = _cells(row)
            if all(re.fullmatch(r":?-{1,}:?", cell) for cell in cells if cell) and any(cells):
                continue
            cells = (cells + ["", "", ""])[:3] if len(cells) <= 3 else cells[:2] + [" | ".join(cells[2:])]
            rows.append((cells[0], re.sub(r"[*_`]", "", cells[1]).strip().lower(), cells[2]))
    return rows


def has_closes_note(evidence):
    """True when the Evidence cell holds a `Closes by:` note that says something (not empty, not a placeholder such as `none` or `tbd`)."""
    match = CLOSES_NOTE.search(evidence)
    if not match:
        return False
    said = " ".join(re.sub(r"[^\w/ ]+", " ", match.group(1).lower()).split())
    return bool(said) and said not in review_verdict.BOILERPLATE


def open_rows(rows):
    """The sites of the `unresolved` rows with no `Closes by:` note: what keeps a review from being approved. [] for no table."""
    return [site for site, disposition, evidence in rows or [] if disposition == "unresolved" and not has_closes_note(evidence)]


def prepare_review(sweep_path, result_path, out_dir):
    """Write `sweep-review.md` in `out_dir`: the sweep as it was generated, then the coder's table as the report gave it (or the
    statement that the report holds none). Returns {"file", "family" (the name in the sweep's title, or None), "rows" (count),
    "open" (sites)}. A report that cannot be read counts as no table."""
    sweep = Path(sweep_path).read_text(encoding="utf-8", errors="replace")
    try:
        report = Path(result_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        report = ""
    rows = report_table(report)
    shown = ("no table with the columns Site, Disposition and Evidence was found in the coder's report; say so in your reply and build the "
             "rows yourself from the sites above." if rows is None else
             "\n".join(["| Site | Disposition | Evidence |", "| --- | --- | --- |"] + [f"| {a} | {b or ''} | {c} |" for a, b, c in rows]))
    found = SWEEP_TITLE.search(sweep)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    target = out / "sweep-review.md"
    target.write_text(f"# The sweep this work was given (generated by tp-agent)\n\n{sweep.rstrip()}\n\n"
                      f"# The coder's report table for it (read from the report; check each row)\n\n{shown}\n", encoding="utf-8")
    return {"file": str(target), "family": found.group(1) if found else None, "rows": len(rows or []), "open": open_rows(rows)}


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) == 5 and sys.argv[1] == "review":  # for bin/tp-review: SWEEP RESULT OUTDIR
        print(json.dumps(prepare_review(*sys.argv[2:])))
        sys.exit(0)
    sys.exit("usage: family_sweep.py review SWEEP.md RESULT.md OUTDIR")
