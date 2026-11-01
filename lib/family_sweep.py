#!/usr/bin/env python3
"""sweep.md: the brief tp-agent generates for a retry that the repeated-family gate stopped (`--route rule-first` or `design`), so the
retry fixes every site of the finding and not the one the last review cited.

Everything in it comes from the author chain's review replies (review_rounds.chain_labels is the one reader of the chain, review_verdict
the one reader of the Family: and Closes by: labels): the Family: names each review gave, every file:line token any earlier
REQUEST_CHANGES finding cites, the finding of the latest review in the reviewer's own words, how to look for the sites no review
cited, and the table the coder reports back. A finding with no file:line token is listed verbatim, never dropped. No model call.

A finding is the text that ends at a `Closes by:` line (the lines after the last one, up to a Coverage or Verification heading,
are one more finding); a reply with no `Closes by:` line is one finding. Reviews that are missing, cannot be read or are not a
REQUEST_CHANGES carry no findings.
"""
import re
from pathlib import Path

import review_rounds
import review_verdict

SWEEP_NAME = "sweep.md"
# path.ext:line or path.ext:line-line, in prose or backticks; not a URL port (no "//" before it), not a bare word with a colon
SITE = re.compile(r"(?<![\w/.-])(?<!//)((?:[\w.-]+/)+[\w-][\w.-]*|[\w-][\w.-]*\.[A-Za-z][A-Za-z0-9]{0,7}):(\d+)(?:-(\d+))?(?![\w])")
MAX_FINDING = 3000  # characters of one finding quoted back; the sites are always listed in full
DISPOSITIONS = ("fixed", "already correct", "out of scope", "unresolved")


def sites(text):
    """The distinct file:line (or file:line-line) tokens in `text`, in order of appearance."""
    found = []
    for match in SITE.finditer(text):
        token = match.group(1) + ":" + match.group(2) + (("-" + match.group(3)) if match.group(3) else "")
        if token not in found:
            found.append(token)
    return found


def findings(text):
    """[(finding text, family names, site tokens)] of a reviewer's reply, in order. Empty for a reply that is not a verdict."""
    try:
        review_verdict.parse(text)
        lines = review_verdict.normalise(text.strip()).split("\n")[1:]
    except ValueError:
        return []
    body = []
    for line in lines:
        if review_verdict.QUOTING_SECTION.match(line):  # Coverage / Verification: not findings
            break
        if not line.strip().upper().startswith("RESULT_STATUS"):
            body.append(line)
    blocks, current = [], []
    for line in body:
        current.append(line)
        if review_verdict.CLOSES_BY.search(line.rstrip()):
            blocks.append(current)
            current = []
    blocks.append(current)
    out = []
    for block in blocks:
        block_text = "\n".join(block).strip("\n")
        if not block_text.strip():
            continue
        names = list(dict.fromkeys(m.group(1) or m.group(2) for line in block
                                   for m in [review_verdict.FAMILY.search(line.rstrip())] if m))
        out.append((block_text, [name.lower() for name in names], sites(block_text)))
    return out


def _rows(state_dir, task_id):
    """[(review id, round, [(finding, names, sites)], families)] for each REQUEST_CHANGES review of the chain, oldest first, plus the
    ids of reviews that could not be read."""
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
        rows.append((review, number, findings(text), families))
    return rows, unread


def _asks_for_code(text):
    """A finding that is labelled `Closes by: code`, or carries no label."""
    labels = review_verdict.closes_by("REQUEST_CHANGES\n" + text)
    return not labels or "code" in labels


def quote(text):
    if len(text) > MAX_FINDING:
        text = text[:MAX_FINDING].rstrip() + "\n[... shortened; the review's result.md has the rest]"
    return "\n".join(("> " + line).rstrip() for line in text.split("\n"))


def build(state_dir, task_id, family, workdir=None):
    """The text of sweep.md for a retry of `task_id`; `family` is the repeated name or None (the unnamed family: two REQUEST_CHANGES
    in a row). Raises review_rounds.Refused when the chain cannot be walked."""
    rows, unread = _rows(state_dir, task_id)
    cited, loose = {}, []  # site -> review ids; findings with no site: (review id, text)
    for review, _, found, _ in rows:
        for text, _, tokens in found:
            for token in tokens:
                cited.setdefault(token, [])
                if review not in cited[token]:
                    cited[token].append(review)
            if not tokens:
                loose.append((review, text))
    title = f"finding family `{family}`" if family else "the unnamed family (REQUEST_CHANGES twice in a row)"
    out = [f"# Sweep: {title}", "",
           f"The reviews of this work asked for changes more than once on the same kind of finding. Fixing the one site the last review "
           f"cited has not ended it, so this retry fixes the rule: every site of it, not one more.", "",
           "## Families named", ""]
    named = [f"- {review} (round {number}): {', '.join(f'`{n}`' for n in families) if families else 'no Family: line'}"
             for review, number, _, families in rows]
    out += named or ["- none of the reviews could be read"]
    out += ["", "## The rule, in the reviewer's words", ""]
    latest = rows[-1] if rows else None
    if latest:
        wanted = [f for f in latest[2] if family in f[1]] if family else [f for f in latest[2] if _asks_for_code(f[0])]
        wanted = wanted or latest[2]
        out.append(f"From review {latest[0]}, the latest:")
        for text, _, _ in wanted:
            out += ["", quote(text)]
    else:
        out.append("No finding could be read from the reviews; read them (tp-agent's retry note names the prior result).")
    out += ["", "## Sites the reviews cited", ""]
    if cited:
        out.append("Every file:line token in a finding of an earlier review. Each one needs a row in your table, even when the line moved or is already right.")
        out += [""] + [f"- `{token}` (cited by {', '.join(reviews)})" for token, reviews in cited.items()]
    else:
        out.append("No review cited a file:line site in a form that could be read; the findings are listed below.")
    if loose:
        out += ["", "Findings with no file:line token, verbatim. Find what they point at yourself and give each a row in your table:"]
        for review, text in loose:
            out += ["", f"From review {review}:", "", quote(text)]
    if unread:
        out += ["", f"Reviews that could not be read, so their sites are not listed: {', '.join(unread)}. Open their result.md yourself."]
    root = workdir or "<your working tree>"
    out += ["", "## Find the sites no review cited", "",
            "The cited sites are where to start, not where to stop. Name the mechanism the rule is about (the call, import, name, "
            "value or branch that the finding shows wrong), then enumerate every use of it:", "",
            f"- `git -C {root} grep -n -e <the shared call, import or name>`, and again for each spelling of it;",
            "- `tp-ctx` importers/references of the symbol, when `tp-ctx` is available;",
            "- the opposite and the repeat of the action (the reader of what a writer writes, the undo of what a call does).", "",
            "Read each hit. A hit that is not an instance of the rule still gets a row.", "",
            "## Table to report", "",
            f"End your report with this table, one row per site: each cited site above and each one you found. Disposition is one of "
            f"{', '.join(f'`{d}`' for d in DISPOSITIONS)}; give evidence for each (the changed line, the line that is already right, why it is out of scope, what blocks it). "
            "The review of this retry checks every row and reports any site the table missed.", "",
            "| Site | Disposition | Evidence |", "| --- | --- | --- |"]
    out += [f"| `{token}` | | |" for token in cited]
    out += ["| (sites you found) | | |", ""]
    return "\n".join(out)


def is_sweep(spec):
    """True for a context spec that is a sweep.md written into a task directory (…/tasks/<id>/sweep.md): retry must not replay it,
    the next retry makes its own."""
    path = Path(spec)
    return path.name == SWEEP_NAME and path.parent.parent.name == "tasks"
