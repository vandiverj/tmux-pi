"""Score one reply to a summarize chore. Pure and deterministic: no model, no network, only reads the fixture files.

    check(reply, answer, root) -> {"pass", "missing", "missing_citation", "unsupported", "order", "forbidden_hit",
                                   "bad_citations", "unchecked", "uncited", "identifier_only", "failures"}

The reply format (round 6; rounds 2 to 5 graded free prose with regexes and found a new prose shape each time). A reply is a list
of FACT LINES, and only fact lines count:

    - <claim text> [<path>:<line>[-<line>]][, <path>:<line>...]

  Rule 1  One fact per line. A fact line starts with "- " and ends with ONE bracketed citation list; the claim holds no other
          bracketed citation list, and a line cannot be what two required facts are met by.
  Rule 2  Any other line (a heading, a sentence, a blank line, an indented "  - " line) is ignored for facts AND for citations: it
          never supplies a file, a line, or a claim. A line that starts with "- " but has no valid trailing citation list is
          `uncited` and fails the reply: no partial credit and no borrowing from the lines around it.
  Rule 3  A citation is valid only when its path is one of the chore's files (answer["cite_files"], written in full as the chore
          names it), its lines are in the file, and it spans at most MAX_RANGE (5) lines: otherwise it is a bad citation
          (`<citation> (unknown_file | range | range_too_wide)`). The citations of the fact line must also cover one of the
          fact's support_lines, or the fact is `unsupported`; a matching fact line with no valid citation is `missing_citation`.
  Rule 4  A required fact is met only by ONE fact line whose claim matches the fact AND whose citations support it, both on the
          same line. No other line, heading or neighbour counts. Facts are met by different lines. A fact no fact line states
          is `missing`; a line that states two facts meets one of them and the other is `missing` (shared_line, rule 1).
  Rule 5  A fact about what code does (answer["behaviour"]) is matched on the claim with every backticked span, every
          `path:line` and every identifier of the chore's code removed (names_in_code below, computed from the fixture): a claim of
          names and signature text has nothing left to match, however it is cited, so it never meets such a fact (the fact is
          `missing` or `unsupported`, by the other lines). A fact line with no word left once the names, the chore's file names and
          Python keywords are removed (`Job(name, payload, priority=0)`, `class LRUCache:`) that no fact needs is a failed line,
          `identifier_only` (rule 5): it is not a claim.

answer (a line of tests/calibration/summarize/answers.jsonl):
  required     list of facts; a fact is a list of alternative regexes for ONE thing (case-insensitive, matched against ONE claim),
               present when any alternative matches. A list of items (steps, states, exports) is one fact per item. A behaviour
               fact is matched on words that say it (returns, stores, evicts ...), never on a name.
  support_lines  one entry per required fact (same index): {"src/x.py": ["14", "20-22"]}, the fixture lines that are evidence of
               that fact: the lines that do it (body, return, raise, a docstring that states it), the signature only when the
               signature alone shows it; for a fact about a name existing, the definition line.
  anchors      one entry per required fact: {"src/x.py": {"14": "return payload", ...}}, a short exact substring of EACH line in
               support_lines, so a fixture edit cannot silently move the evidence (tests/test_qual_summarize.py asserts it; the
               checker does not read it).
  behaviour    optional {"<index into required>": {"name": ..., "sig": "path:line"}} the facts that are about what code does (rule 5).
               The value (the name the fact is about and the line that declares it) is for the tests.
  order        optional list of indexes into required: facts a chore wants in a given order (states, steps). The first fact line
               that states each must come after the one before; a fact that comes too early is listed in "order".
  forbidden    list of regexes for plausible fabrications; none may match the reply (any line of it).
  cite_files   the files (relative to root) a citation may name.

The result names, per failed line, which rule it broke: "failures" is a list of {"rule": 1-5 or None, "kind", "line" (1-based
line of the reply, or None), "text" (the line, or the fact for a fact no line carries)}. kind is one of uncited (2),
two_citation_lists (1), unknown_file / range / range_too_wide (3), missing_citation (3), unsupported (3), missing (4),
shared_line (1), identifier_only (5), order (rule None). "uncited" lists the lines of rules 1 and 2. A valid citation of a fact
line that no fact the line states vouches for is `unchecked`: counted, not a failure. The reply passes when nothing is missing,
uncited, identifier_only, missing_citation, unsupported or out of order, no forbidden regex matches and no citation is bad.
"identifier_only" lists the lines of rule 5.
"""
import ast
import keyword
import re
from pathlib import Path

FACT_LINE = re.compile(r"^- (?P<claim>.*?\S)[ \t]*\[(?P<cites>[^\[\]]*)\]$")
CITATION = re.compile(r"^(?P<path>[^\s,:\[\]]+):(?P<a>\d+)(?:-(?P<b>\d+))?$")
BRACKETS = re.compile(r"\[[^\[\]]*\]")
BACKTICKS = re.compile(r"`[^`\n]*`")
PATH_LINE = re.compile(r"(?<![\w./-])[\w./-]+:\d+(?:-\d+)?")
IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
MAX_RANGE = 5
SUPPORT_LINES = re.compile(r"^(\d+)(?:-(\d+))?$")
_LINES, _NAMES = {}, {}


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


def _read(root, name):
    """The lines of a file under root, or None. Cached by (path, mtime, size), so an edit is seen."""
    path = Path(root) / name
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        if key not in _LINES:
            _LINES[key] = path.read_text(encoding="utf-8").splitlines()
        return _LINES[key]
    except (OSError, UnicodeDecodeError):
        return None


def names_in_code(root, files):
    """Every name that appears in the Python files (relative to root) of a chore, computed from them: functions, classes, parameters,
    assigned names and attributes, imports, the names and attributes the code uses (`time.sleep`, `heappush`), keyword arguments,
    and strings that are one identifier (dict keys, handler names). A claim cannot be met by saying them (rule 5)."""
    sources = tuple((name, _read(root, name) if name.endswith(".py") else None) for name in files)
    key = tuple((name, id(lines)) for name, lines in sources)
    if key not in _NAMES:
        found = set()
        for name, lines in sources:
            try:
                tree = ast.parse("\n".join(lines)) if lines else None
            except SyntaxError:
                tree = None
            for node in ast.walk(tree) if tree else ():
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    found.add(node.name)
                elif isinstance(node, ast.arg):
                    found.add(node.arg)
                elif isinstance(node, ast.Name):
                    found.add(node.id)
                elif isinstance(node, ast.Attribute):
                    found.add(node.attr)
                elif isinstance(node, ast.keyword) and node.arg:
                    found.add(node.arg)
                elif isinstance(node, ast.alias):
                    found.update(part for part in (node.name + "." + (node.asname or "")).split(".") if part)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    found.update(node.module.split("."))
                elif isinstance(node, ast.ExceptHandler) and node.name:
                    found.add(node.name)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str) and IDENTIFIER.fullmatch(node.value):
                    found.add(node.value)
        _NAMES[key] = frozenset(found)
    return _NAMES[key]


def without_names(claim, names):
    """The claim with every backticked span, every path:line and every name in names removed (rule 5)."""
    text = PATH_LINE.sub(" ", BACKTICKS.sub(" ", claim))
    return IDENTIFIER.sub(lambda m: " " if m.group(0) in names else m.group(0), text)


def without_files(text, files):
    """text with the chore's file names (written in full, or the bare name) and Python's keywords (def, class, return ...) removed:
    what is left is the words of the claim. A signature is names, file names and keywords."""
    for name in sorted(files, key=len, reverse=True):
        text = text.replace(name, " ").replace(Path(name).name, " ")
    return IDENTIFIER.sub(lambda m: " " if keyword.iskeyword(m.group(0)) else m.group(0), text)


def parse_fact_line(line):
    """(claim, [(text, path, first, last)], None) for a fact line; (None, None, kind) when "- " has no valid trailing citation list
    (uncited, rule 2) or the claim holds a second one (two_citation_lists, rule 1)."""
    m = FACT_LINE.match(line)
    cites = []
    for item in (m.group("cites").split(",") if m else ()):
        c = CITATION.match(item.strip())
        if c is None:
            m = None
            break
        cites.append((item.strip(), c.group("path"), int(c.group("a")), int(c.group("b") or c.group("a"))))
    if m is None or not cites:
        return None, None, "uncited"
    if any(all(CITATION.match(x.strip()) for x in b[1:-1].split(",")) for b in BRACKETS.findall(m.group("claim"))):
        return None, None, "two_citation_lists"
    return m.group("claim"), cites, None


def _cite_problem(root, files, path, first, last):
    """None for a good citation, else unknown_file, range or range_too_wide (rule 3)."""
    if path not in files:
        return "unknown_file"
    lines = _read(root, path)
    if lines is None or not 1 <= first <= last <= len(lines):
        return "range"
    return "range_too_wide" if last - first + 1 > MAX_RANGE else None


def check(reply, answer, root):
    required, files = answer["required"], answer["cite_files"]
    support = [support_sets(entry) for entry in answer["support_lines"]]
    if len(support) != len(required):
        raise ValueError("support_lines has {} entries for {} required facts".format(len(support), len(required)))
    behaviour = {int(k) for k in answer.get("behaviour", {})}
    names = names_in_code(root, files)
    failures, bad, uncited = [], [], []

    def fail(rule, kind, line, text):
        failures.append({"rule": rule, "kind": kind, "line": line, "text": text})

    facts = []  # one dict per fact line: n (reply line number), line, claim, stripped (names removed), good (valid citations)
    for number, line in enumerate(reply.split("\n"), 1):
        line = line.rstrip()
        if not line.startswith("- "):
            continue  # rule 2: not a fact line, so neither a fact nor a citation
        claim, cites, kind = parse_fact_line(line)
        if kind:
            uncited.append(line)
            fail(2 if kind == "uncited" else 1, kind, number, line)
            continue
        good = []
        for text, path, first, last in cites:
            problem = _cite_problem(root, files, path, first, last)
            if problem:
                bad.append("{} ({})".format(text, problem))
                fail(3, problem, number, text)
            else:
                good.append((text, path, first, last))
        stripped = without_names(claim, names)
        # a claim with no word left once names and the chore's file names are gone says nothing (rule 5)
        bare = not re.search(r"[A-Za-z]{2,}", without_names(without_files(claim, files), names))
        facts.append({"n": number, "line": line, "claim": claim, "stripped": stripped, "good": good, "names_only": bare})
    bad = list(dict.fromkeys(bad))

    def covers(fact, k):
        return [c for c in fact["good"] if support[k].get(c[1], set()).intersection(range(c[2], c[3] + 1))]

    missing, missing_citation, unsupported = [], [], []
    first_at, ok, vouched = {}, {}, set()
    for k, alternatives in enumerate(required):
        label = alternatives[0]
        text = "stripped" if k in behaviour else "claim"
        matches = [f for f in facts if any(re.search(a, f[text], re.I) for a in alternatives)]
        if not matches:
            missing.append(label)
            fail(4, "missing", None, label)
            continue
        first_at[k] = matches[0]["n"]
        for f in matches:
            vouched.update((f["n"], c) for c in covers(f, k))
        ok[k] = sorted((f for f in matches if covers(f, k)), key=lambda f: f["names_only"])  # a names-only line is the last to be used
        if not ok[k]:
            kind, into = ("unsupported", unsupported) if any(f["good"] for f in matches) else ("missing_citation", missing_citation)
            into.append(label)
            fail(3, kind, matches[0]["n"], label)
    owner = {}  # reply line number -> the fact it meets

    def assign(k, seen):
        for f in ok[k]:
            if f["n"] not in seen:
                seen.add(f["n"])
                if f["n"] not in owner or assign(owner[f["n"]], seen):
                    owner[f["n"]] = k
                    return True
        return False

    for k in ok:
        if ok[k] and not assign(k, set()):
            missing.append(required[k][0])
            fail(1, "shared_line", ok[k][0]["n"], required[k][0])
    identifier_only = []  # rule 5: a line of names, signature text and keywords that no fact needed
    for f in facts:
        if f["names_only"] and f["n"] not in owner:
            identifier_only.append(f["line"])
            fail(5, "identifier_only", f["n"], f["line"])
    out_of_order, last_at = [], -1
    for k in answer.get("order", []):
        if k in first_at:
            if first_at[k] < last_at:
                out_of_order.append(required[k][0])
                fail(None, "order", first_at[k], required[k][0])
            last_at = max(last_at, first_at[k])
    forbidden_hit = [pattern for pattern in answer["forbidden"] if re.search(pattern, reply, re.I)]
    unchecked = sum(1 for f in facts for c in f["good"] if (f["n"], c) not in vouched)
    return {"pass": not (missing or missing_citation or unsupported or out_of_order or forbidden_hit or bad or uncited or identifier_only),
            "missing": missing, "missing_citation": missing_citation, "unsupported": unsupported, "order": out_of_order,
            "forbidden_hit": forbidden_hit, "bad_citations": bad, "unchecked": unchecked, "uncited": uncited, "identifier_only": identifier_only,
            "failures": failures}
