"""Score one reply to a summarize chore. Pure and deterministic: no model, no network, only reads the fixture files.

    check(reply, answer, root) -> {"pass", "missing", "missing_citation", "unsupported", "order", "forbidden_hit",
                                   "bad_citations", "unchecked"}

answer (a line of tests/calibration/summarize/answers.jsonl):
  required     list of facts; a fact is a list of alternative regexes for ONE thing (case-insensitive, `.` does not match a
               newline, use [\\s\\S] to span lines); the fact is present when any alternative matches the reply TEXT WITHOUT ITS
               CITATIONS (below). A list of items (steps, states, exports) is one fact per item, never one alternation over the
               items; a fact about what code does is matched on words that say it, never on the code's own names alone.
  support_lines  one entry per required fact (same index): {"src/x.py": ["14", "20-22"]}, the fixture lines that are evidence of
               that fact. For a fact about what code does it is the lines that do it (body, return, raise, a docstring that states
               it) and the signature only when the signature alone shows it; for a fact about a name existing, the definition line.
  anchors      one entry per required fact: {"src/x.py": {"14": "return payload", ...}}, a short exact substring of EACH line in
               support_lines, so a fixture edit cannot silently move the evidence (tests/test_qual_summarize.py asserts it; the
               checker does not read it).
  order        optional list of indexes into required: the facts a chore wants in a given order (states, steps). The position of
               the first match of each must be greater than the one before; a fact that comes too early is listed in "order".
  forbidden    list of regexes for plausible fabrications; none may match (the reply, and the reply without its citations).
  cite_files   the files (relative to root) a citation may point at.

A citation is one of
  path:line, path:first-last   any path, any extension or none, also a bare file name (`worker.py:31`); the token must not be
                               glued to a longer word, a URL or a clock time
  Line N, Lines a-b            the nearest of this line and the headings, bold lines and bullets above it that names a file-like token sets
                               the file. A token is file-like when it is `name.ext` with ANY extension, or contains a "/" (a
                               path), whatever the extension: `nonexistent.xyz`, `src/nonexistent` and `queue.dead` all are. Code
                               in backticks (spaces, brackets, quotes), a name followed by "(" and e.g./i.e. are not. That line
                               must name exactly one file and it must be one of cite_files, as a whole path or a bare basename
                               (`cache.py.bak` is not `cache.py`): two files is `ambiguous_context`, any other file
                               `unknown_file`, no file anywhere `no_context`; each makes the reference bad. It never falls back
                               to an earlier heading's file.
It is good when its file is one of cite_files (a leading ./ or an absolute prefix ending in /path, or a bare basename, is
accepted), the lines exist in the file under root, and, if the reply quotes text right next to it, that text appears
(whitespace-normalised) in those lines. A quote is a `code` or "double-quoted" span right after the citation (separators : - and
the closing ` ) ] allowed) or right before it when the citation is in ( ) or [ ].

A range is at most MAX_RANGE (5) lines; a wider one is a bad citation (`... (range_too_wide)`), so a whole file cannot be cited.

Citations are not evidence for what a reply SAYS: before `required` is matched, every citation is removed from the reply text
(the line numbers of `path:N`, `Line N`, the quote next to it, and the parenthesised group they leave empty; the path stays when
the citation is not inside brackets, so `cli.py:29 creates` still names cli.py). A name plus a quoted signature then cannot
satisfy "what it does".

Every required fact needs a good citation on the same line or in the same bullet (a bullet runs over its wrapped lines, a
sub-bullet is its own bullet): a fact present without one is missing_citation. At least one of those citations must overlap the
fact's support_lines (its line or range, in its file): otherwise the fact is `unsupported`, however well the numbers are in
range. A quote is checked as well as support, never instead of it.

Every citation is checked. A quoted one must contain its quote. An unquoted one counts only through a fact's support; an
unquoted citation that no fact's support vouched for (on a line with no required fact, or one that overlaps no support there)
is `unchecked`: it is counted in the result and is not a failure. The reply passes when nothing is missing,
missing_citation, unsupported or out of order, no forbidden regex matches and no citation is bad.
"""
import bisect
import re
from pathlib import Path

CITE = re.compile(r"(?<![\w./:\\-])(?=[\w./-]*[A-Za-z_])(?P<path>[\w./-]+)[`*_]*:L?(?P<a>\d+)(?:[-–]L?(?P<b>\d+))?(?!\d)")
LINE_REF = re.compile(r"\b[Ll]ines?[ \t]+(?P<a>\d+)(?:[ \t]*[-–—][ \t]*(?P<b>\d+))?(?!\d)")
QUOTE_AFTER = re.compile(r"""^[`)\]*]*[ \t]*[:\-–—]?[ \t]*(?:`(?P<bq>[^`\n]+)`|"(?P<dq>[^"\n]+)")""")
QUOTE_BEFORE = re.compile(r"""(?:`(?P<bq>[^`\n]+)`|"(?P<dq>[^"\n]+)")[ \t]*[(\[][ \t]*[`*]*$""")
BULLET = re.compile(r"^[ \t]*(?:[-*+•]|\d+[.)])[ \t]+")
HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]")
BOLD_LINE = re.compile(r"^[ \t]*\*\*[^\n]*\*\*[ \t:]*$")  # **src/cli.py**, a heading in all but name
MAX_RANGE = 5
PATHISH = re.compile(r"(?<![\w./~-])[\w./~-]+")
CODE_SPAN = re.compile(r"`[^`\n]*[^`\n\w./~*-][^`\n]*`")  # backticks around code (a space, bracket, quote, colon ...), not a bare name
NOT_FILES = frozenset("e.g i.e etc vs and/or n/a w/o yes/no read/write i/o either/or he/she his/her input/output".split())
SUPPORT_LINES = re.compile(r"^(\d+)(?:-(\d+))?$")


def _squash(text):
    return " ".join(text.split())


def support_sets(entry):
    """{"src/x.py": ["14", "20-22"]} -> {"src/x.py": {14, 20, 21, 22}}."""
    sets = {}
    for name, specs in entry.items():
        lines = set()
        for spec in specs:
            m = SUPPORT_LINES.match(spec)
            if m is None or int(m.group(2) or m.group(1)) < int(m.group(1)):
                raise ValueError("bad support_lines entry {!r} for {}".format(spec, name))
            lines.update(range(int(m.group(1)), int(m.group(2) or m.group(1)) + 1))
        sets[name] = lines
    return sets


def _file_for(path, cite_files):
    """The cite_files entry a cited path means, or None."""
    path = path[2:] if path.startswith("./") else path
    for name in cite_files:
        if path == name or path.endswith("/" + name) or name.endswith("/" + path):
            return name
    return None


def _file_like(token):
    """True when token reads as a file name: a path (it has a "/") or `name.ext` with any extension. Not e.g./i.e., not a
    number or a version (3.12, v1.2: the extension must start with a letter and the name with a letter or underscore)."""
    if token.lower() in NOT_FILES:
        return False
    if "/" in token:
        return any(c.isalpha() for c in token)
    parts = token.split(".")
    return len(parts) >= 2 and all(parts) and (parts[0][0].isalpha() or parts[0][0] == "_") and parts[-1][0].isalpha()


def _file_tokens(text):
    """The distinct file-like tokens in a line of text, in order."""
    text = CODE_SPAN.sub(" ", text)
    found = []
    for m in PATHISH.finditer(text):
        token = m.group(0).rstrip(".-/")
        if _file_like(token) and not text.startswith("(", m.end()) and token not in found:  # name.ext( is a call
            found.append(token)
    return found


def _context_file(lines, index, cite_files):
    """(file, reason) for a `Line N` on lines[index]: the nearest of this line, then the headings and bullets above it, that
    names any file-like token sets the file. file is a cite_files entry when that line names exactly that one file; otherwise
    file is None and reason is ambiguous_context (two or more files), unknown_file (a file that is not in cite_files, whatever
    its extension) or no_context (no such line)."""
    candidates = [index] + [j for j in range(index - 1, -1, -1) if HEADING.match(lines[j]) or BULLET.match(lines[j]) or BOLD_LINE.match(lines[j])]
    for j in candidates:
        tokens = _file_tokens(lines[j])
        if not tokens:
            continue
        keys = list(dict.fromkeys(_file_for(token, cite_files) or token for token in tokens))
        if len(keys) > 1:
            return None, "ambiguous_context"
        if _file_for(keys[0], cite_files) is None:
            return None, "unknown_file"
        return keys[0], None
    return None, "no_context"


def _quote_next_to(reply, start, end):
    """(quote text, (from, to) span of it in reply) for the quote next to the reference reply[start:end], or (None, None)."""
    m = QUOTE_AFTER.match(reply[end:])
    if m is not None:
        return m.group("bq") or m.group("dq"), (end, end + m.end())
    m = QUOTE_BEFORE.search(reply[:start])
    if m is not None:
        return m.group("bq") or m.group("dq"), (m.start(), start)
    return None, None


def _check_lines(file_lines, first, last, quote):
    """None when lines first..last of a file are a good citation, else "range" (not all in the file), "range_too_wide" (more than
    MAX_RANGE of them) or "quote" (the quote next to the reference is not in them)."""
    if file_lines is None or not 1 <= first <= last <= len(file_lines):
        return "range"
    if last - first + 1 > MAX_RANGE:
        return "range_too_wide"
    if quote is not None:
        wanted = _squash(quote)
        if wanted and wanted not in _squash(" ".join(file_lines[first - 1:last])):
            return "quote"
    return None


def _read(root, name):
    try:
        return (Path(root) / name).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None


def _units(lines):
    """unit[i]: the index of the first line of the bullet that line i belongs to, or i itself when it is in none."""
    unit = list(range(len(lines)))
    for i, line in enumerate(lines):
        if BULLET.match(line) or HEADING.match(line) or not line.strip():
            continue
        j = i - 1
        while j >= 0 and lines[j].strip() and not HEADING.match(lines[j]):
            if BULLET.match(lines[j]):
                unit[i] = j
                break
            j -= 1
    return unit


def _in_brackets(reply, position):
    """True when position is inside an unclosed ( or [ opened earlier on the same line."""
    before = reply[reply.rfind("\n", 0, position) + 1:position]
    return before.count("(") + before.count("[") > before.count(")") + before.count("]")


def _citations(reply, lines, offsets, cite_files, root):
    """([(label, good, line_index, file, first, last, quoted)], [(from, to)]): every citation in the reply, and the spans of the
    reply that are citation (to be removed before the facts are matched)."""
    found, cut = [], []
    cache = {}

    def file_lines(name):
        if name not in cache:
            cache[name] = _read(root, name) if name else None
        return cache[name]

    def add(label, name, m, index, cut_from):
        first = int(m.group("a"))
        last = int(m.group("b") or first)
        quote, span = _quote_next_to(reply, m.start(), m.end())
        reason = _check_lines(file_lines(name), first, last, quote) if name else "file"
        found.append((label + (" (range_too_wide)" if reason == "range_too_wide" else ""), reason is None, index, name, first, last,
                      quote is not None))
        cut.append((cut_from, m.end()))
        if span is not None:
            cut.append(span)

    for m in CITE.finditer(reply):
        # inside ( ) the whole citation goes; in running text only `:N`, so `cli.py:29 creates` still names cli.py
        cut_from = m.start() if _in_brackets(reply, m.start()) else m.end("path")
        add(m.group(0), _file_for(m.group("path"), cite_files), m, bisect.bisect_right(offsets, m.start()) - 1, cut_from)
    for m in LINE_REF.finditer(reply):
        index = bisect.bisect_right(offsets, m.start()) - 1
        name, reason = _context_file(lines, index, cite_files)
        add("{}: {}".format(name, m.group(0)) if name else "{} ({})".format(m.group(0), reason), name, m, index, m.start())
    return found, cut


def _without_citations(reply, cut):
    """(text, kept): reply with the cut spans removed, and the index in reply of each character of text. A bracket pair the cut
    left empty (`( )`, `(, )`) goes too, but one that was empty in the reply (`load_settings()`) stays."""
    gone = [False] * len(reply)
    for start, end in cut:
        for i in range(start, end):
            gone[i] = True
    masked = "".join("\0" if g else c for c, g in zip(reply, gone))
    for m in re.finditer(r"[(\[][\0 \t,;:.]*\0[\0 \t,;:.]*[)\]]", masked):
        for i in range(m.start(), m.end()):
            gone[i] = True
    kept = [i for i, g in enumerate(gone) if not g]
    return "".join(reply[i] for i in kept), kept


def check(reply, answer, root):
    required = answer["required"]
    support = [support_sets(entry) for entry in answer["support_lines"]]
    if len(support) != len(required):
        raise ValueError("support_lines has {} entries for {} required facts".format(len(support), len(required)))
    lines = reply.split("\n")
    offsets, at = [], 0
    for line in lines:
        offsets.append(at)
        at += len(line) + 1
    unit = _units(lines)
    cites, cut = _citations(reply, lines, offsets, answer["cite_files"], root)
    text, kept = _without_citations(reply, cut)
    bad = list(dict.fromkeys(label for label, good, *_ in cites if not good))

    missing, missing_citation, unsupported = [], [], []
    first_at = {}
    vouched = set()  # indexes into cites that a fact's support vouches for
    for number, fact in enumerate(required):
        spans = [m.span() for alt in fact for m in re.finditer(alt, text, re.I)] if kept else []
        if not spans:
            missing.append(fact[0])
            continue
        # the spans are in text; the citations of the fact are found in the reply, so map them back
        spans = [(kept[min(s, len(kept) - 1)], kept[min(max(e - 1, s), len(kept) - 1)] + 1) for s, e in spans]
        first_at[number] = min(start for start, _ in spans)
        cited = False
        matched = []
        for start, end in spans:
            first = bisect.bisect_right(offsets, start) - 1
            last = bisect.bisect_right(offsets, max(end - 1, start)) - 1
            units = {unit[i] for i in range(first, last + 1)}
            for n, (_, good, index, name, a, b, _) in enumerate(cites):
                if good and unit[index] in units:
                    cited = True
                    if support[number].get(name, set()).intersection(range(a, b + 1)):
                        matched.append(n)
        if not cited:
            missing_citation.append(fact[0])
        elif not matched:
            unsupported.append(fact[0])
        vouched.update(matched)
    out_of_order, last_at = [], -1
    for number in answer.get("order", []):
        if number in first_at:
            if first_at[number] < last_at:
                out_of_order.append(required[number][0])
            last_at = max(last_at, first_at[number])
    forbidden_hit = [pattern for pattern in answer["forbidden"] if re.search(pattern, reply, re.I) or re.search(pattern, text, re.I)]
    unchecked = sum(1 for n, (_, good, _, _, _, _, quoted) in enumerate(cites) if good and not quoted and n not in vouched)
    return {"pass": not (missing or missing_citation or unsupported or out_of_order or forbidden_hit or bad), "missing": missing,
            "missing_citation": missing_citation, "unsupported": unsupported, "order": out_of_order, "forbidden_hit": forbidden_hit,
            "bad_citations": bad, "unchecked": unchecked}
