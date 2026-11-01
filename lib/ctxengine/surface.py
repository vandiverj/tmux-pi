"""The context surface (CX4): the five operations a worker calls, one engine behind the pi extension, the MCP server and `tp-ctx`.

  ctx_brief      a fixed-shape briefing on 1-5 files: facts, exports, relations, shared contracts, notes, library hints
  ctx_symbol     where a name is defined, its signature and doc, a few references
  ctx_relations  who imports a file, what it imports, or the shared contracts it uses
  ctx_knowledge  search the approved notes (candidates only on request)
  ctx_note       offer a CANDIDATE note with file:line evidence (never approved; at most 5 per task)

Every operation checks its arguments (validate), checks every path at the redaction boundary before any navigator or file
sees it, answers in at most CAP bytes with a footer naming what was left out, and returns a calls.Answer whose status is
one word of calls.STATUSES. tp-ctx logs each call (main() below goes through calls.call, never operator mode), so a worker's
call that cannot be logged gets no answer.

Code navigation comes from two navigators this module does not contain: CX2's NavWeb (TypeScript language service, web
packages) and CX3's NavApi (Deno import graph, api packages). They are loaded by module name (NAV_MODULES); a module that is
missing or will not load is replaced by nav_stub, whose every answer is `unavailable` with a next step. A navigator's
refusal is shown with the reason it gave. Which navigator serves a path comes from the nearest deno.json (api) or tsconfig.json
(web) above it.
"""
import argparse
import importlib
import json
import os
import posixpath
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import calls, freshness, redact, search, store

CAP = 6144
TOOLS = ("ctx_brief", "ctx_symbol", "ctx_relations", "ctx_knowledge", "ctx_note")
COMMANDS = TOOLS + ("ctx_schema",)
PACKAGES = ("web", "api", "mc", "lib")
DIRECTIONS = ("importers", "imports", "contracts")
NAV_MODULES = {"web": "ctxengine.nav_web", "api": "ctxengine.nav_api"}
NOTES_PER_TASK = 5
NOTE_BODY_FULL = store.BODY_MAX   # ctx_knowledge: a note's body, whole (the store accepts no longer one)
NOTE_BODY_BRIEF = 300   # ctx_brief: the first characters of a note's body
EXIT = {"ok": 0, "invalid": 1, "error": 1, "stale_index": 1, "unavailable": 1, "refused": 2, "no_project": 2}
CODE_EXT = (".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs")
LABEL_ORDER = ("web", "api", "mc", "lib", "other")
WITHHELD = "<withheld>"
WEB_EDGES = 200   # the most import edges CX2 returns for one file
API_SYMBOLS = 100  # the most symbols CX3 lists for one file
SYMBOL_PLACES = 20   # declarations of one name that are listed
SYMBOL_FILES = 5     # files whose details ctx_symbol gives for one name
SCAN_FILES = 20000
ANSWER_FILE_KEYS = ("target", "path")  # top-level fields that name the file an answer is about (CX2 imports, CX3 importers/imports/symbols)
SCAN_BYTES = 1_000_000
CANDIDATES_WHY = "candidate notes that matched; include_candidates=true shows them, marked"
LIBRARIES_WHY = ("a file too large to read, an import list that was cut, or an import whose name is not a plain string, may import libraries "
                 "that are not listed")
SCAN_WHY = "code files the text scan could not read: a refused path, a file over 1 MB or past the scan cap"

# ------------------------------------------------------------------------------------------------------------- the schema

NAME_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")


def _spec(type_, description, **more):
    return {"type": type_, "description": description, **more}


SPECS = {
    "ctx_brief": {
        "paths": _spec("array", "1 to 5 project files to brief (relative to the project root)", required=True, min=1, max=5, item_max=300),
    },
    "ctx_symbol": {
        "name": _spec("string", "the symbol, a bare identifier", required=True, min=1, max=100, pattern="bare identifier"),
        "path": _spec("string", "the file that defines it, when known (relative to the project root)", min=1, max=300),
        "package": _spec("string", "limit the search for the definition to one package", enum=list(PACKAGES)),
    },
    "ctx_relations": {
        "target": _spec("string", "a project file path, or the name of a symbol that is defined once", required=True, min=1, max=300),
        "direction": _spec("string", "importers: who imports it; imports: what it imports; contracts: shared types it uses and who else uses them",
                           required=True, enum=list(DIRECTIONS)),
        "limit": _spec("integer", "most edges to show (default 10)", min=1, max=25),
    },
    "ctx_knowledge": {
        "query": _spec("string", "words to search the notes for", min=0, max=200),
        "paths": _spec("array", "return notes whose scope covers these files", min=0, max=5, item_max=300),
        "tags": _spec("array", "return notes with one of these tags", min=0, max=5, item_max=40),
        "include_candidates": _spec("boolean", "also return unapproved candidate notes, marked as such"),
        "limit": _spec("integer", "most notes to return (default 5)", min=1, max=10),
    },
    "ctx_note": {
        "kind": _spec("string", "what sort of note", required=True, enum=list(store.KINDS)),
        "scope": _spec("array", "globs of the files the note applies to", required=True, min=1, max=10, item_max=200),
        "text": _spec("string", "the note, one rule or fact with the reason", required=True, min=1, max=800),
        "evidence": _spec("array", "file:line places that show it is true", required=True, min=1, max=10, item_max=300),
        "title": _spec("string", "a one-line title", min=1, max=store.TITLE_MAX),
        "tags": _spec("array", "tags", min=0, max=5, item_max=40),
    },
}
DESCRIPTIONS = {
    "ctx_brief": "Brief on 1-5 files before you change them: size, tests, exports, who imports them, shared contracts, the project's notes "
                 "about them and the libraries they use. A fixed shape; read it instead of opening each file to find out how it fits.",
    "ctx_symbol": "Where a symbol is defined, with its signature, doc and a few references.",
    "ctx_relations": "Who imports a file (importers), what it imports (imports), or the shared contracts it uses and who else uses them (contracts).",
    "ctx_knowledge": "Search the project's approved notes (conventions, framework rules, decisions, lessons) by words, files or tags.",
    "ctx_note": "Offer a candidate note about the project for a person to review. Needs file:line evidence. At most 5 per task; it is never "
                "approved by you.",
}


def schema():
    """The specs the wrappers (pi extension, MCP server) must match: {tools: {name: {description, fields}}, cap}."""
    return {"tools": {tool: {"description": DESCRIPTIONS[tool], "fields": SPECS[tool]} for tool in TOOLS}, "cap": CAP}


def usage(tool):
    parts = []
    for name, spec in SPECS[tool].items():
        kind = {"array": "list", "integer": "number", "boolean": "true/false"}.get(spec["type"], spec["type"])
        if spec.get("enum"):
            kind = "|".join(spec["enum"])
        parts.append(f"{name}{'' if spec.get('required') else '?'}: {kind}")
    return f"{tool}({', '.join(parts)})"


def _range_words(spec, noun):
    low, high = spec.get("min", 0), spec["max"]
    return f"{low} to {high} {noun}" if low else f"at most {high} {noun}"


def _check(name, spec, value):
    kind = spec["type"]
    if kind == "string":
        if not isinstance(value, str):
            return [f"{name} must be a string"]
        low, high = spec.get("min", 0), spec.get("max", 10 ** 6)
        if len(value) < low or len(value) > high:
            return [f"{name} must be {_range_words(spec, 'characters')}"]
        if spec.get("enum") and value not in spec["enum"]:
            return [f"{name} must be one of {', '.join(spec['enum'])}"]
        if spec.get("pattern") and not NAME_RE.match(value):
            return [f"{name} must be a bare identifier (letters, digits, _ and $; no spaces or dots)"]
        return []
    if kind == "integer":
        low, high = spec["min"], spec["max"]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            return [f"{name} must be a whole number from {low} to {high}"]
        return []
    if kind == "boolean":
        return [] if isinstance(value, bool) else [f"{name} must be true or false"]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return [f"{name} must be a list of strings"]
    if any(not v.strip() for v in value):
        return [f"{name} must be a list of strings (none empty)"]
    if not spec.get("min", 0) <= len(value) <= spec["max"]:
        return [f"{name} needs {_range_words(spec, 'entries')}"]
    if any(len(v) > spec.get("item_max", 300) for v in value):
        return [f"each entry of {name} is at most {spec.get('item_max', 300)} characters"]
    return []


def validate(tool, args):
    """(clean arguments, problems). A tool that is not one of TOOLS, or arguments that are not an object, are problems."""
    if tool not in SPECS:
        return {}, [f"unknown tool {tool[:40]!r}; the tools are {', '.join(TOOLS)}"]
    if not isinstance(args, dict):
        return {}, ["the arguments must be a JSON object"]
    problems = []
    for key in sorted(args, key=str):
        if key not in SPECS[tool]:
            problems.append(f"unknown argument {str(key)[:40]!r}")
    for name, spec in SPECS[tool].items():
        if name not in args or args[name] is None:
            if spec.get("required"):
                problems.append(f"{name} is required")
            continue
        problems += _check(name, spec, args[name])
    clean = {k: v for k, v in args.items() if k in SPECS[tool] and v is not None}
    return clean, problems


# --------------------------------------------------------------------------------------------------------- the answer text

def _size(text):
    return len(text.encode("utf-8"))


def _cut(text, limit):
    """`text` cut to at most `limit` bytes of UTF-8, never in the middle of a character."""
    return text.encode("utf-8")[:max(limit, 0)].decode("utf-8", "ignore")


def short(text, limit):
    """One line of `text` (white space collapsed) of at most `limit` characters, ending in … when cut."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit - 1].rstrip() + "…"


CUT_SHOWN = "at least {n} shown, list cut by the navigator"
NOT_ALL_SHOWN = "at least {n} shown, not all returned: {why}"
PATH_WHY = "a path that is refused or outside the project"
NOT_RETURNED_WHY = "rows the navigator did not hand over"
INVALID_WHY = "note files that could not be read"
NONE_SHOWN = "none can be shown ({n} withheld, {why})"
CUT_NOTHING = "not found in the part the navigator returned (list cut)"
NOT_AVAILABLE = "not available (the read failed or was not run)"
NOT_ANSWERED = "not available (the navigator answered {status})"
UNRESOLVED_WHY = "imports the navigator could not resolve"
EXTERNAL_WHY = "places inside installed packages that the navigator dropped"
CONFIG_WHY = "config entries the navigator's index ignored"
ERRORS_WHY = "parts the navigator reported errors for"
UNREADABLE_WHY = "rows the navigator returned in a form this surface cannot read"
UNKNOWN_WHY = "fields of the answer this surface does not know ({keys})"


@dataclass(frozen=True)
class Completeness:
    """Whether a read of a source was complete, the one thing an absence may be built on. Only `answer_read` makes one, from the whole
    answer of the read (a navigator answer, a note search, the surface's own scan); `Completeness.of` joins records it made. A test
    fails if brief.py or surface.py builds one anywhere else.
      ok       the answer's status is `ok` (False for any other status, a navigator that failed or was unavailable, a read never run)
      withheld items of the source left out because their path is refused or the navigator withheld them
      cut      the navigator said it cut its list
      unread   items of the source that were not read: imports it could not resolve, rows not handed over, a file too large or past
               the scan cap, a note that failed to parse, a field of the answer this surface does not know
      why      what `withheld` and `unread` were, in words
      status   the navigator's own word when `ok` is False ("" for a read that never ran)"""
    ok: bool = True
    withheld: int = 0
    cut: bool = False
    unread: int = 0
    why: str = PATH_WHY
    status: str = ""

    @property
    def left(self):
        return self.withheld + self.unread

    @property
    def whole(self):
        """True only when the source was read completely: the read succeeded, nothing was withheld or unread, nothing was cut."""
        return self.ok and not self.cut and self.left == 0

    @classmethod
    def of(cls, *parts):
        """The record for a source made of several reads: whole only if every part is. No parts is a read that never ran."""
        if not parts:
            return answer_read(None)
        why = ", and ".join(dict.fromkeys(p.why for p in parts if p.left))
        failed = [p.status for p in parts if not p.ok and p.status]
        return cls(all(p.ok for p in parts), sum(p.withheld for p in parts), any(p.cut for p in parts), sum(p.unread for p in parts),
                   why or PATH_WHY, failed[0] if failed else "")


# The fields an answer may carry, for `answer_read`. Every field of an answer is one of these three kinds, or unknown.
# HARMLESS: never a sign that part of the source was left out (the rows themselves, echoes of the question, ages, totals that the
# caller compares with the rows it was handed, free text). LOSS: a count, list or flag of what was left out, with what it was.
# Anything else that is not empty is unknown, and an unknown field makes the read not whole: a new navigator field is a loss until
# someone reads it and puts it on one of the lists.
COUNTS = frozenset({"total"})  # harmless only when a whole number: any other value makes the read not whole
HARMLESS = frozenset({
    "status",                                                    # checked first: only "ok" is a whole read
    "edges", "exports", "importers", "files", "external", "symbols", "definitions", "references", "hits",   # the rows
    "direction", "target", "path", "source", "cached", "at", "is_module",                               # echoes of the question
    "signature", "doc",                                          # text about the one symbol asked for
    "total",                                                     # compared with the rows handed over (`not_returned`)
    "index_age_s", "files_indexed", "scanned", "cache",          # how fresh the index is and how much it re-read
})
LOSS = {  # field -> (kind, what it counted); kind is withheld, unread or cut
    "withheld": ("withheld", PATH_WHY), "omitted_outside_root": ("withheld", PATH_WHY), "refused": ("withheld", PATH_WHY),
    "candidates_counted": ("withheld", CANDIDATES_WHY),
    "unresolved": ("unread", UNRESOLVED_WHY), "unresolved_total": ("unread", UNRESOLVED_WHY),
    "omitted_external": ("unread", EXTERNAL_WHY), "ignored_config": ("unread", CONFIG_WHY),
    "skipped": ("unread", SCAN_WHY), "libraries_unread": ("unread", LIBRARIES_WHY), "invalid": ("unread", INVALID_WHY),
    "errors": ("unread", ERRORS_WHY),
    "truncated": ("cut", ""), "cut": ("cut", ""),
}
OVERLAP = {"unresolved_total": "unresolved"}  # CX3's index-wide count includes the file's own `unresolved` list: the larger is taken


def _amount(value):
    """How many items a loss field names: a list's length, a count, 1 for any other value that is not empty."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value if value >= 0 else 1
    if isinstance(value, (list, tuple, set, frozenset, dict, str)):
        return len(value)
    return 0 if value is None else 1


def answer_read(answer, screened=0, not_returned=0, unreadable=0):
    """The Completeness of one read, from its whole answer: the one place a Completeness is made. `answer` is the navigator's answer
    dict, a note search result (its fields), or the surface's own scan as a dict; `screened` the rows the surface dropped because
    their path fails the path policy, `not_returned` the rows of the navigator's total it did not hand over, `unreadable` the rows
    it handed over in a form the surface cannot read (listed_rows).
    The read is whole only when the status is `ok`, every LOSS field is empty, and no field outside HARMLESS and LOSS has a value.
    No answer (None, not a dict) is a read that never ran."""
    if not isinstance(answer, dict):
        return Completeness(ok=False)
    status = answer.get("status")
    counts = {"withheld": [], "unread": []}
    cut, unknown, shared = False, [], {}
    for key in sorted(answer, key=str):
        value = answer[key]
        if key in COUNTS and value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            unreadable += 1  # a count that is not a whole number cannot be compared with the rows
            continue
        if key in HARMLESS:
            continue
        if key in LOSS:
            kind, why = LOSS[key]
            if kind == "cut":
                cut = cut or bool(value)
            elif key in OVERLAP or key in OVERLAP.values():
                group = OVERLAP.get(key, key)
                shared[group] = (kind, why, max(shared.get(group, (kind, why, 0))[2], _amount(value)))
            elif _amount(value):
                counts[kind].append((_amount(value), why))
        elif _amount(value):
            unknown.append(str(key))
    for kind, why, amount in shared.values():
        if amount:
            counts[kind].append((amount, why))
    if unknown:
        counts["unread"].append((len(unknown), UNKNOWN_WHY.format(keys=short(", ".join(unknown), 80))))
    if screened:
        counts["withheld"].append((screened, PATH_WHY))
    if not_returned:
        counts["unread"].append((not_returned, NOT_RETURNED_WHY))
    if unreadable:
        counts["unread"].append((unreadable, UNREADABLE_WHY))
    whys = [why for kind in ("withheld", "unread") for n, why in counts[kind] if n]
    word = "" if status == "ok" else short(str(status or "status missing"), 40)
    return Completeness(status == "ok", sum(n for n, _ in counts["withheld"]), cut, sum(n for n, _ in counts["unread"]),
                        ", and ".join(dict.fromkeys(whys)) or PATH_WHY, word)


def listed_rows(raw, key):
    """(the rows of the list `raw[key]` that are objects, the number of items the navigator handed over, the number this surface
    cannot read): an item that is not an object, or a list that is missing or not a list, is counted as unreadable, never dropped
    silently, so the read is not whole."""
    items = raw.get(key)
    if not isinstance(items, list):
        return [], 0, 1
    rows = [item for item in items if isinstance(item, dict)]
    return rows, len(items), len(items) - len(rows)


def absent(text, read, label=None):
    """The one place an absence is worded. `text` ("none found", "no definition of X", "nothing imports X", a zero) is returned only
    when `read` (a Completeness, built from the read result) says the source was read completely. A read that failed, was
    unavailable or never ran is "not available"; a cut list is "not found in the part ... returned (list cut)"; a source with items
    withheld or unread is "none can be shown (N withheld, why)". `label` prefixes the substitute ("label: none can be shown ...")."""
    if not read.ok:
        said = NOT_ANSWERED.format(status=read.status) if read.status else NOT_AVAILABLE
    elif read.cut:
        said = CUT_NOTHING
    elif read.left > 0:
        said = NONE_SHOWN.format(n=read.left, why=read.why)
    else:
        return text
    return f"{label}: {said}" if label else said


def list_head(label, shown, total, read, empty=None, of=False):
    """The line that opens a list of `shown` rows from a navigator, the one place a list's count, zero or absence is worded.
    `read` is the Completeness of the read the rows came from. When it is cut the count is a lower bound and `total` is never stated:
    "label (at least N shown, list cut by the navigator):", or "label: not found in the part the navigator returned (list cut)" when
    nothing was shown. When nothing is shown and the read was not whole (failed, withheld rows, unread rows) the list is not known
    to be empty: "label: not available ..." or "label: none can be shown (N withheld, ...)", never a zero or an absence. Otherwise
    "label (total):" ("label (shown of total):" with `of`); an empty list says `empty` when given and nothing at all was reported."""
    if shown > 0 and read.cut:
        return f"{label} ({CUT_SHOWN.format(n=shown)}):"
    if shown > 0 and read.unread:
        return f"{label} ({NOT_ALL_SHOWN.format(n=shown, why=read.why)}):"
    if shown <= 0 and not read.whole:
        return absent("", read, label=label)
    if shown <= 0 and empty and total <= 0:
        return absent(empty, read)
    return f"{label} ({shown} of {total}):" if of and shown > 0 else f"{label} ({total}):"


def notes_read(result):
    """The Completeness of a note search, from every field of its result: candidate notes that matched but were not asked for, note
    files that failed to parse, matches past the hits it returned, and any field the surface does not know."""
    fields = vars(result)
    hits = fields.get("hits")
    shown = len(hits) if isinstance(hits, list) else 0
    return answer_read({"status": "ok", **fields}, not_returned=max(_int(fields.get("total")) - shown, 0), unreadable=0 if isinstance(hits, list) else 1)


def notes_left(result):
    """The `left_out` entry for note files that failed to parse (they may be among the matches), or None."""
    return f"notes: {len(result.invalid)} withheld ({INVALID_WHY}, so matching notes may be missing)" if result.invalid else None


def more_note(label, beyond, cut, advice, total=None):
    """The `left_out` entry for rows of a navigator list that are not shown. A cut list gives no count (and no total): it says the
    list was cut. Otherwise `beyond` rows are named, with `total` when given."""
    if cut:
        return f"{label}: the navigator cut its list ({advice})"
    if beyond > 0:
        return f"{label}: {beyond} more ({f'{total} total; ' if total is not None else ''}{advice})"
    return None


def footer(left_out):
    if not left_out:
        return ""
    line = "(left out: " + "; ".join(left_out) + ")"
    return line if _size(line) <= 700 else _cut(line, 696).rstrip() + "…)"


def fit(lines, left_out):
    """(text, left_out) with the text at most CAP bytes: whole lines are dropped from the end, a single line that is still too
    big is cut, and the footer names what was left out (including the lines dropped here)."""
    left, body, dropped = list(left_out), list(lines), 0

    def build():
        notes = left + ([f"{dropped} more lines past the {CAP}-byte cap"] if dropped else [])
        foot = footer(notes)
        return "\n".join(body + ([foot] if foot else [])), notes

    text, notes = build()
    while _size(text) > CAP and len(body) > 1:
        body.pop()
        dropped += 1
        text, notes = build()
    if _size(text) > CAP:
        room = CAP - _size(footer(notes)) - 2
        body = [_cut(body[0], room) + "…"] if body else []
        text, notes = build()
        text = _cut(text, CAP)
    return text, notes


def failure_text(kind, raw):
    """The words for a navigator answer that is not ok: its status, reason and message as given, and a next step."""
    status = str(raw.get("status", "error"))
    reason = raw.get("reason")
    detail = raw.get("detail") or raw.get("message") or ""
    head = f"{kind} navigator: {status}"
    if reason and reason != status:
        head += f" ({reason})"
    nxt = raw.get("next") or raw.get("next_step") or NEXT_DEFAULT[MAP_STATUS.get(status, "error")]
    return head + (f": {detail}" if detail else "") + f"\nNext: {nxt}"


MAP_STATUS = {"ok": "ok", "not_found": "ok", "refused": "refused", "warming": "unavailable", "timeout": "unavailable",
              "crashed": "unavailable", "unavailable": "unavailable", "no_typescript": "no_project", "not_in_project": "no_project",
              "no_project": "no_project", "bad_request": "invalid", "bad_argument": "invalid", "stale_index": "stale_index", "error": "error"}
NEXT_DEFAULT = {
    "refused": "ask about a file inside the project that is not sensitive",
    "unavailable": "try again in a minute; if it stays unavailable, read the files directly",
    "no_project": "ask about a TypeScript file in a package that has a tsconfig.json or deno.json",
    "invalid": "check the arguments and ask again",
    "stale_index": "ask again in a moment; the index is being rebuilt",
    "error": "read the files directly and tell the operator the navigator failed",
    "ok": "",
}


@dataclass
class Got:
    """What a navigator call came to, in one shape for every navigator: rows are (a, b, c) tuples as the caller defined."""
    ok: bool = True
    status: str = "ok"
    text: str = ""
    rows: list = field(default_factory=list)
    total: int = 0
    external: list = field(default_factory=list)
    age: int | None = None
    raw: dict = field(default_factory=dict)
    seen: int = 0            # rows the navigator handed over or withheld itself: what `total` is compared with
    screened: int = 0        # rows the surface dropped because their path fails the path policy (counted, never named)
    unreadable: int = 0      # rows the navigator handed over in a form the surface cannot read (listed_rows)

    @property
    def beyond(self):
        """Rows of the navigator's full answer that are not in `rows` and were not refused: the part not read."""
        return max(self.total - self.seen, 0)

    def completeness(self):
        """The Completeness of this read, from the navigator's whole answer (answer_read) with the rows the surface screened out and
        the rows it was not handed. Not ok when the navigator failed."""
        if not self.ok:
            read = answer_read(self.raw or None)
            return read if not read.ok else answer_read(None)
        return answer_read(self.raw, screened=self.screened, not_returned=self.beyond, unreadable=self.unreadable)

    @property
    def refused(self):
        """Rows left out because a path is refused: the ones the surface screened out and the ones the navigator withheld."""
        return self.completeness().withheld

    @property
    def cut(self):
        """The navigator said its list was cut (`truncated`), whatever its `total` says."""
        return self.completeness().cut

    @property
    def incomplete(self):
        """Part of the answer was not read: rows not handed over, a cut list, unresolved imports or a field this surface does not know
        (withheld rows are counted apart)."""
        read = self.completeness()
        return read.cut or read.unread > 0

    @property
    def unresolved(self):
        """The navigator's count of imports it could not resolve, to show (answer_read counts it as unread)."""
        return _amount(self.raw.get("unresolved")) or _amount(self.raw.get("unresolved_total"))


def label_for(path):
    """The package label of a project path, for grouping: web, api, mc, lib or other."""
    parts = path.split("/")
    if parts[0] == "lib":
        return "lib"
    if parts[0] == "apps" and len(parts) > 1 and parts[1] == "mission-control":
        return "mc"
    if parts[0] == "apps" and len(parts) > 2 and parts[2] in ("web", "api"):
        return parts[2]
    return "other"


def library_of(specifier):
    """The package a bare import specifier names, or None for a relative path, an alias or a builtin."""
    spec = specifier.strip()
    for prefix in ("npm:", "jsr:"):
        if spec.startswith(prefix):
            spec = spec[len(prefix):]
    if not spec or spec[0] in "./#~" or spec.startswith(("node:", "@/", "http:", "https:", "file:", "data:")):
        return None
    parts = spec.split("/")
    name = "/".join(parts[:2]) if spec.startswith("@") and len(parts) > 1 else parts[0]
    name = re.sub(r"(?<=.)@[^/@]*$", "", name)
    return name if re.match(r"^@?[A-Za-z0-9][A-Za-z0-9._/-]*$", name) else None


IMPORT_RE = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*)(['"])([^'"\n]{1,200})\1""")


def libraries_in(text):
    return {lib for _, spec in IMPORT_RE.findall(text) if (lib := library_of(spec))}


DYNAMIC_RE = re.compile(r"""\b(?:import|require)\s*\(\s*(?!['"][^'"\n]{1,200}['"]\s*[,)])""")


def declaration_pattern(name):
    """The text-scan pattern for a top-level declaration of `name`, group `export` set when it is exported: a declaration
    (function, class, const, let, var, type, interface, enum, namespace, module), a destructured const/let/var, `import name =`,
    and the export forms that bind a name (`export { a as name }`, `export * as name`). A line may start with a byte order mark;
    `name` is bounded by characters that cannot be part of a name, so a name with `$` matches."""
    n = r"(?<![\w$])" + re.escape(name) + r"(?![\w$])"
    lead = r"^[ \t\ufeff]*(?P<export>export\b[ \t]*)?"
    forms = (r"(?:default[ \t]+)?(?:declare[ \t]+)?(?:abstract[ \t]+)?(?:async[ \t]+)?"
             r"(?:function[ \t]*\*?|class|const|let|var|type|interface|enum|namespace|module)[ \t]+" + n,
             r"(?:declare[ \t]+)?(?:const|let|var)[ \t]*[{\[][^=;]*?" + n + r"[^=;]*?[}\]][ \t]*(?::[^=;]*)?=",
             r"import[ \t]+" + n + r"[ \t]*=",
             r"(?<=export)[ \t]*(?:\{[^}]*?\bas[ \t\r\n]+" + n + r"[^}]*\}|\*[ \t]*as[ \t]+" + n + ")")
    return re.compile(lead + "(?:" + "|".join(forms) + ")", re.M)


def unnamed_imports(text):
    """The number of `import(...)` and `require(...)` calls whose module is not a plain string (a template or an expression): the
    library scan cannot name what they load, so "no library" cannot be said for the file."""
    return len(DYNAMIC_RE.findall(text))


# ---------------------------------------------------------------------------------------------------------- navigators

class WebGuard:
    """CX2's guard interface (check_path(path, root) -> reason or None, screen(text)) over the engine's redaction boundary."""

    def check_path(self, path, root):
        try:
            redact.check_path(root, path)
        except redact.Refused as refusal:
            return str(refusal)
        return None

    def screen(self, text):
        return redact.guard(text)


def modules_from_env(env):
    """TP_CTX_NAV_MODULES: `name` (one module for both navigators) or `web=name,api=name`. Unset means the default modules."""
    raw = (env.get("TP_CTX_NAV_MODULES") or "").strip()
    if not raw:
        return None
    if "=" not in raw:
        return {"web": raw, "api": raw}
    chosen = dict(part.split("=", 1) for part in raw.split(",") if "=" in part)
    return {k: v.strip() for k, v in chosen.items() if k in ("web", "api")}


class Navigators:
    """Builds the navigators on first use, one NavWeb per tsconfig and one NavApi per Deno package root, from the modules named
    in `modules` ({"web": module name, "api": module name}; None means NAV_MODULES). A missing or unloadable module, or a
    constructor that raises, gives the stub, and `why` says so."""

    def __init__(self, layout, modules=None):
        self.project = os.path.realpath(str(layout.project))
        self.modules = dict(NAV_MODULES if modules is None else modules)
        self.instances = {}
        self.why = {}
        self.classes = {}

    def _class(self, kind):
        if kind not in self.classes:
            wanted = {"web": "NavWeb", "api": "NavApi"}[kind]
            name = self.modules.get(kind)
            cls = None
            if name:
                try:
                    cls = getattr(importlib.import_module(name), wanted)
                except (ImportError, AttributeError, SyntaxError) as exc:
                    self.why[kind] = f"{name} did not load ({type(exc).__name__})"
                except Exception as exc:  # a module that fails while it loads is still only unavailable
                    self.why[kind] = f"{name} did not load ({type(exc).__name__})"
            if cls is None:
                from . import nav_stub
                cls = getattr(nav_stub, wanted)
            self.classes[kind] = cls
        return self.classes[kind]

    def get(self, kind, where):
        """(navigator, None), or (None, an unavailable answer) when it cannot be built. `where` is the tsconfig path (web) or the
        package directory (api), relative to the project."""
        key = (kind, where)
        if key not in self.instances:
            try:
                cls = self._class(kind)
                if kind == "web":
                    made = cls(root=self.project, tsconfig=os.path.join(self.project, where), guard=WebGuard())
                else:
                    made = cls(project_root=self.project, root=os.path.join(self.project, where) if where else self.project,
                               check_path=redact.check_path, guard=redact.guard)
            except Exception as exc:
                made = None
                self.why[kind] = f"{type(exc).__name__} while starting it"
            self.instances[key] = made
        made = self.instances[key]
        if made is None:
            step = NEXT_DEFAULT["unavailable"]
            return None, {"status": "unavailable", "message": f"the {kind} navigator did not start ({self.why.get(kind, 'unknown')}); using no navigator",
                          "next": step}
        return made, None

    def close(self):
        for made in self.instances.values():
            stop = getattr(made, "stop", None)
            if made is not None and stop:
                try:
                    stop()
                except Exception:
                    pass
        self.instances.clear()


# -------------------------------------------------------------------------------------------------------------- the engine

def pin_reader(project):
    """A `pin(name)` callable from L5's lockfile reader when that module is present, else None (versions are then unchecked)."""
    try:
        from localdocs import lock  # noqa: PLC0415
    except ImportError:
        return None
    return lambda name: lock.pin(project, name)


class NoteWriter:
    """ctx_note: the one operation that writes. It offers a candidate and nothing more."""

    def op_ctx_note(self, args):
        spec_problems = []
        evidence = args["evidence"]
        for item in evidence:
            path, _, number = item.rpartition(":")
            if not path or not number.isdigit() or int(number) < 1:
                spec_problems.append(f"evidence {short(item, 60)!r} is not file:line")
                continue
            self.path(path)  # raises redact.Refused for a path outside the project or a sensitive one
        if spec_problems:
            return self.finish("invalid", ["invalid note: " + "; ".join(spec_problems),
                                           "Next: give each evidence entry as relative/path.ts:LINE, naming a line that exists."])
        hashes = freshness.hash_evidence(self.project, evidence)
        missing = [short(e, 60) for e in evidence if e not in hashes]
        if missing:
            return self.finish("invalid", ["invalid note: evidence does not name an existing line: " + ", ".join(missing),
                                           "Next: cite a file that exists and a line number within it."])
        taken = self.notes_this_task()
        if taken >= NOTES_PER_TASK:
            return self.finish("refused", [f"refused: {taken} notes already offered in this task; the limit is {NOTES_PER_TASK} notes per task",
                                           "Next: keep the rest in your report; a person can add them."], reason="note limit")
        source = f"worker:{self.env.get('TP_TASK_ID') or 'unknown'}"
        text = args["text"].strip()
        note = store.Note(id="", kind=args["kind"], scope=list(args["scope"]), source=source, body=text, title=args.get("title", ""),
                          tags=list(args.get("tags", [])), evidence=list(evidence), created=self.today)
        note.id = store.new_id(self.layout, note.title or text, date=self.today)
        try:
            path = store.add_candidate(self.layout, note, screen=redact.screen_note)
        except (store.NoteError, store.StoreError) as exc:
            problems = exc.problems if isinstance(exc, store.NoteError) else [str(exc)]
            return self.finish("invalid", ["invalid note: " + "; ".join(problems), "Next: fix the listed problems and call ctx_note again."],
                               reason="; ".join(problems))
        rel = os.path.relpath(path, self.project)
        return self.finish("ok", [f"{note.id} written as a candidate ({rel}); it is not approved and a person must review it",
                                  f"{taken + 1} of {NOTES_PER_TASK} notes used in this task"], notes=[note.id],
                           data={"result": {"id": note.id, "status": "candidate", "path": rel}}, undo=lambda: _remove(path))

    def notes_this_task(self):
        """How many ctx_note calls this task's call log already shows as ok (0 when there is no task directory)."""
        task = self.env.get("TP_TASK_DIR")
        if not task:
            return 0
        path = os.path.join(task, calls.LOG_NAME)
        try:
            if os.path.islink(path) or os.path.getsize(path) > 4 * 1024 * 1024:
                return NOTES_PER_TASK
            with open(path, encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
        except OSError:
            return 0
        count = 0
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and entry.get("tool") == "ctx_note" and entry.get("status") == "ok":
                count += 1
        return count


class Engine(NoteWriter):
    def __init__(self, layout, navigators=None, pin=None, env=None):
        self.layout = layout
        self.project = Path(os.path.realpath(str(layout.project)))
        self.navs = navigators or Navigators(layout)
        self.pin = pin
        self.env = os.environ if env is None else env
        self.today = store.today(self.env)
        self._tree = None

    def close(self):
        self.navs.close()

    # ------------------------------------------------------------------------------------------------------- shared

    @property
    def tree(self):
        if self._tree is None:
            self._tree = freshness.Tree(self.project)
        return self._tree

    def path(self, value):
        """The project-relative posix path `value` names. Raises redact.Refused."""
        return redact.check_path(self.project, value)

    def read(self, rel, limit=SCAN_BYTES):
        """The text of a project file (at most `limit` bytes, None when it is missing, a directory, too big or a link out)."""
        full = self.project / rel
        try:
            if not full.is_file() or full.stat().st_size > limit:
                return None
            with open(full, "rb") as handle:
                return handle.read(limit).decode("utf-8", "replace")
        except OSError:
            return None

    def package_for(self, rel):
        """("api", package directory) or ("web", directory holding the tsconfig) for the nearest one above `rel`, else None."""
        here = posixpath.dirname(rel)
        while True:
            base = self.project / here if here else self.project
            if (base / "deno.json").is_file() or (base / "deno.jsonc").is_file():
                return "api", here
            if (base / "tsconfig.json").is_file():
                return "web", here
            if not here:
                return None
            here = posixpath.dirname(here)

    def pinned(self, name):
        if self.pin is None:
            return None
        try:
            return self.pin(name)
        except Exception:
            return None

    def finish(self, status, lines, left_out=(), notes=(), age=None, data=None, reason=None, undo=None):
        text, left = fit([redact.guard(line) for line in lines], left_out)
        info = {"truncated": bool(left), "left_out": left, **(data or {})}
        return calls.Answer(status, redact.guard(text), list(notes), age, reason, info, undo)

    def invalid(self, tool, problems):
        lines = ["invalid arguments: " + "; ".join(problems), f"Usage: {usage(tool)}" if tool in SPECS else f"Tools: {', '.join(TOOLS)}",
                 f"Next: fix the argument and call {tool if tool in SPECS else 'one of the tools'} again."]
        return self.finish("invalid", lines, reason="; ".join(problems))

    def run(self, tool, args):
        clean, problems = validate(tool, args)
        if problems:
            return self.invalid(tool, problems)
        try:
            return getattr(self, "op_" + tool)(clean)
        except redact.Refused as refusal:
            return self.finish("refused", [str(refusal), "Next: ask about a file inside the project that is not sensitive."], reason=str(refusal))

    # ---------------------------------------------------------------------------------------------------- navigation

    def _ask(self, rel, wanted):
        """(navigator kind, navigator, None) for the file `rel`, or (None, None, Got) when no navigator can serve it."""
        package = self.package_for(rel)
        if package is None:
            return None, None, Got(False, "no_project", f"no package covers this path ({rel}): no deno.json or tsconfig.json above it\n"
                                   f"Next: {NEXT_DEFAULT['no_project']}")
        kind, where = package
        made, failure = self.navs.get(kind, os.path.join(where, "tsconfig.json") if kind == "web" else where)
        if failure:
            return None, None, self.failed(kind, failure)
        return kind, made, None

    def failed(self, kind, raw):
        status = MAP_STATUS.get(str(raw.get("status")), "error")
        return Got(False, status, failure_text(kind, raw), raw=raw)

    def call(self, kind, made, method, *args, **kw):
        """The navigator's answer as a dict; an exception or a non-answer becomes an `unavailable` answer, and an answer whose `target`
        or `path` names another file than the one asked about (args[0]), or an ok answer that does not echo the `direction` asked
        for, an `error`: its rows are not about this question."""
        try:
            raw = getattr(made, method)(*args, **kw)
        except Exception as exc:
            return {"status": "unavailable", "message": f"the {kind} navigator failed ({type(exc).__name__})", "next": NEXT_DEFAULT["unavailable"]}
        if not isinstance(raw, dict):
            return {"status": "error", "message": f"the {kind} navigator gave an answer that is not an object"}
        asked = args[0] if args and isinstance(args[0], str) else None
        if asked and any(key in raw and self.allowed(raw[key]) != asked for key in ANSWER_FILE_KEYS):
            return {"status": "error", "message": f"the {kind} navigator answered about another file than {asked}"}
        if "direction" in kw and raw.get("status") == "ok" and raw.get("direction") != kw["direction"]:
            return {"status": "error", "message": f"the {kind} navigator did not answer for direction {kw['direction']}"}
        return raw

    def _status(self, kind, raw):
        word = str(raw.get("status", "error"))
        return None if MAP_STATUS.get(word, "error") == "ok" else self.failed(kind, raw)

    def allowed(self, value):
        """The project-relative path of a path a navigator returned, or None when it fails the path policy (outside the project,
        a link out of it, sensitive, empty, or a placeholder a navigator left for a path it withheld)."""
        if not isinstance(value, str) or value.strip() in ("", WITHHELD):
            return None
        try:
            return self.path(value)
        except redact.Refused:
            return None

    @staticmethod
    def loss(raw):
        """(paths the navigator left out itself, whether it cut its list) from any navigator answer, as answer_read counts them."""
        read = answer_read(raw)
        return read.withheld, read.cut

    def screen_rows(self, rows, index):
        """(rows whose path at position `index` passes the path policy, with that path normalized; number refused)."""
        kept, refused = [], 0
        for row in rows:
            rel = self.allowed(row[index])
            if rel is None:
                refused += 1
            else:
                kept.append(row[:index] + (rel,) + row[index + 1:])
        return kept, refused

    def screen_dicts(self, rows, key="path"):
        """The same for navigator rows that are dicts: (rows with a normalized `key`, number refused). Non-dict rows count as refused."""
        kept, refused = [], 0
        for row in rows:
            rel = self.allowed(row.get(key)) if isinstance(row, dict) else None
            if rel is None:
                refused += 1
            else:
                kept.append({**row, key: rel})
        return kept, refused

    def exports(self, rel, limit=200):
        """Got whose rows are (name, kind, line) for the names `rel` defines or exports."""
        kind, made, failure = self._ask(rel, "exports")
        if failure:
            return failure
        if kind == "web":
            raw = self.call(kind, made, "exports", rel, limit=limit)
            bad = self._status(kind, raw)
            if bad:
                return bad
            listed, handed, unreadable = listed_rows(raw, "exports")
            rows = [(str(e.get("name", "")), str(e.get("kind", "")), _int(e.get("line"))) for e in listed]
            return Got(rows=rows, total=_int(raw.get("total", handed)), seen=handed, unreadable=unreadable, raw=raw)
        raw = self.call(kind, made, "symbols", rel, limit=API_SYMBOLS)
        bad = self._status(kind, raw)
        if bad:
            return bad
        listed, handed, unreadable = listed_rows(raw, "symbols")
        rows = [(str(s.get("name", "")), str(s.get("kind", "")), _int(s.get("line"))) for s in listed]
        return Got(rows=rows, total=_int(raw.get("total", handed)), seen=handed, unreadable=unreadable, raw=raw)

    def _edges(self, raw, rows, index, handed, unreadable, **more):
        """A Got from navigator rows: refused paths dropped and counted, rows it cannot read counted, and the navigator's own
        truncation kept."""
        kept, screened = self.screen_rows(rows, index)
        withheld, _ = self.loss(raw)
        return Got(rows=kept, total=_int(raw.get("total", handed)), seen=handed + withheld, screened=screened, unreadable=unreadable,
                   raw=raw, **more)

    def importers(self, rel, limit=10):
        """Got whose rows are (importing file, line, rel)."""
        kind, made, failure = self._ask(rel, "importers")
        if failure:
            return failure
        if kind == "web":
            raw = self.call(kind, made, "imports", rel, direction="importers", limit=limit)
            bad = self._status(kind, raw)
            if bad:
                return bad
            listed, handed, unreadable = listed_rows(raw, "edges")
            rows = [(e.get("from"), _int(e.get("line")), rel) for e in listed]
            return self._edges(raw, rows, 0, handed, unreadable)
        raw = self.call(kind, made, "importers", rel, limit=limit)
        bad = self._status(kind, raw)
        if bad:
            return bad
        listed, handed, unreadable = listed_rows(raw, "importers")
        rows = [(e.get("path"), _int(e.get("line")), rel) for e in listed]
        return self._edges(raw, rows, 0, handed, unreadable, age=raw.get("index_age_s"))

    def imports(self, rel, limit=WEB_EDGES):
        """Got whose rows are (rel, line in rel, imported file); `external` names the libraries it imports."""
        kind, made, failure = self._ask(rel, "imports")
        if failure:
            return failure
        if kind == "web":
            raw = self.call(kind, made, "imports", rel, direction="imports", limit=limit)
            bad = self._status(kind, raw)
            if bad:
                return bad
            listed, handed, unreadable = listed_rows(raw, "edges")
            rows = [(rel, _int(e.get("line")), e.get("to")) for e in listed]
            return self._edges(raw, rows, 2, handed, unreadable)
        raw = self.call(kind, made, "imports", rel)
        bad = self._status(kind, raw)
        if bad:
            return bad
        listed, handed, unreadable = listed_rows(raw, "files")
        rows = [(rel, _int(e.get("line")), e.get("path")) for e in listed]
        kept, screened = self.screen_rows(rows, 2)
        omitted, _ = self.loss(raw)
        return Got(rows=kept, total=max(_int(raw.get("total")), handed) + omitted, seen=handed + omitted, screened=screened,
                   unreadable=unreadable, external=[str(x) for x in raw.get("external") or []], age=raw.get("index_age_s"), raw=raw)

    # ----------------------------------------------------------------------------------------------- text-scan locator

    def locate(self, name, package=None, limit=20):
        """([(path, line, exported)], Completeness): where a top-level declaration of `name` sits, by a text scan of the project's code files
        (neither navigator can search the whole workspace by name), exported declarations first, then by path and line; and the Completeness
        of the scan, whose `unread` is the number of code files it did not read (a refused path, a file it could not read, files past
        SCAN_FILES), so that "no declaration found" is never said when a file was skipped."""
        pattern = declaration_pattern(name)
        found, skipped = [], 0
        ordered = sorted(self.tree.files)
        for rel in ordered[:SCAN_FILES]:
            if not rel.endswith(CODE_EXT) or (package and label_for(rel) != package):
                continue
            try:
                redact.check_path(self.project, rel)
            except redact.Refused:
                skipped += 1
                continue
            text = self.read(rel)
            if text is None:
                skipped += 1
                continue
            if not text or name not in text:
                continue
            for match in pattern.finditer(text):
                found.append((rel, text.count("\n", 0, match.start()) + 1, bool(match.group("export"))))
        skipped += sum(1 for rel in ordered[SCAN_FILES:] if rel.endswith(CODE_EXT) and not (package and label_for(rel) != package))
        found.sort(key=lambda f: (not f[2], f[0], f[1]))
        return found[:limit], answer_read({"status": "ok", "skipped": skipped})

    # ------------------------------------------------------------------------------------------------------ the notes

    def flags_of(self, notes):
        return {n.id: freshness.assess(n, self.project, self.tree, self.pin, search.glob_matches, self.today) for n in notes}

    def note_line(self, note, verdict, body_limit):
        """One note on one line: id, kind, marks, title, then the body cut at `body_limit` characters (never less of the body because
        the title is long). Returns (line, whether the body was cut)."""
        marks = ["candidate"] if note.status != "approved" else []
        marks += verdict.get("flags", [])
        if verdict.get("age_days"):
            marks.append(f"{verdict['age_days']} days since review")
        head = f"  {note.id} ({note.kind})" + (f" [{', '.join(marks)}]" if marks else "")
        if verdict.get("version_unchecked"):
            head += f" (version not checked: {', '.join(verdict['version_unchecked'])})"
        title = " ".join(note.headline.split())
        body = " ".join(note.body.split())
        if not body or body == title:
            return f"{head} — {title}", False
        cut = len(body) > body_limit
        shown = body[:body_limit].rstrip() + "…" if cut else body
        return f"{head} — {title}: {shown}", cut

    def find_notes(self, paths=(), query=None, tags=(), include_candidates=False, limit=5, body_limit=NOTE_BODY_BRIEF):
        """(search result, note lines, ids of the notes whose body was cut)."""
        result = search.query(self.layout, query=query, paths=list(paths), tags=list(tags), include_candidates=include_candidates, limit=limit)
        verdicts = self.flags_of([h.note for h in result.hits])
        lines, cut = [], []
        for hit in result.hits:
            line, was_cut = self.note_line(hit.note, verdicts[hit.note.id], body_limit)
            lines.append(line)
            if was_cut:
                cut.append(hit.note.id)
        return result, lines, cut

    # -------------------------------------------------------------------------------------------------------- tools

    def op_ctx_knowledge(self, args):
        paths = [self.path(p) for p in args.get("paths", [])]
        limit = args.get("limit", 5)
        result, lines, _ = self.find_notes(paths, args.get("query"), args.get("tags", []), args.get("include_candidates", False), limit,
                                           NOTE_BODY_FULL)
        left = [notes_left(result)] if result.invalid else []
        if not lines:
            lines = [absent("No matching notes.", notes_read(result))]
        else:
            lines.insert(0, f"{len(lines)} of {result.total} matching notes:")
            if result.total > len(result.hits):
                left.append(f"notes: {result.total - len(result.hits)} more (raise limit up to 10, or narrow with query, paths or tags)")
            if result.candidates_counted:
                lines.append(f"{result.candidates_counted} candidate(s) matched but are not shown; include_candidates=true shows them, marked.")
        return self.finish("ok", lines, left, [h.note.id for h in result.hits], result.index_age_s)

    def op_ctx_brief(self, args):
        from . import brief
        paths = [self.path(p) for p in args["paths"]]
        return brief.build(self, list(dict.fromkeys(paths)))

    def op_ctx_symbol(self, args):
        name, package = args["name"], args.get("package")
        left, lines = [], []
        if "path" in args:
            rel = self.path(args["path"])
            if not (self.project / rel).is_file():
                return self.finish("invalid", [f"no such file: {rel}", "Next: check the path, or leave path out to search by name."])
            files = [rel]
        else:
            where, scanned = self.locate(name, package, SYMBOL_PLACES + 1)
            if not where:
                scope = f" in the {package} package" if package else ""
                return self.finish("ok", [absent(f"no definition of {name} found{scope} (text scan of the project's code files)", scanned,
                                                 label=f"definitions of {name}{scope}"),
                                          "Next: check the spelling, or pass path if you know the file."])
            if scanned.unread:
                left.append(f"name search: {scanned.unread} withheld ({SCAN_WHY})")
            if len(where) > SYMBOL_PLACES:
                left.append(f"definitions: more than {SYMBOL_PLACES} places declare {name}; pass package or path to narrow it")
                where = where[:SYMBOL_PLACES]
            files = list(dict.fromkeys(place[0] for place in where))
            if len(where) > 1:
                lines.append(f"{name} is declared in {len(where)} places:")
                lines += [f"  {p}:{n}" for p, n, _ in where[:5]]
                if len(where) > 5:
                    left.append(f"definitions: {len(where) - 5} more places")
            if len(files) > SYMBOL_FILES:
                left.append(f"details for {len(files) - SYMBOL_FILES} more file(s) not shown: {', '.join(files[SYMBOL_FILES:SYMBOL_FILES + 3])}"
                            + (" and others" if len(files) > SYMBOL_FILES + 3 else "") + " (pass path to see one)")
        results = []
        for rel in files[:SYMBOL_FILES]:
            if len(files) > 1:
                lines.append(f"== {rel}")
            results.append(self._symbol_in(rel, name, lines, left))
        good = [r for r in results if r[0] == "ok"]
        if good:
            return self.finish("ok", lines, left)
        return self.finish(results[0][0], lines, left, reason=results[0][1])

    def _symbol_in(self, rel, name, lines, left):
        """Add the details of `name` in the file `rel` to `lines`. Returns (status, reason) of that file's lookup."""
        kind, made, failure = self._ask(rel, "definition")
        if failure:
            lines.append(failure.text)
            return failure.status, failure.text.splitlines()[0]
        if kind == "web":
            return self._symbol_web(kind, made, rel, name, lines, left)
        return self._symbol_api(kind, made, rel, name, lines, left)

    def _definition_lines(self, rows, left, cut, label="definitions"):
        lines = []
        for row in rows[:5]:
            lines.append(f"{row['path']}:{row.get('line', 0)} {row.get('kind', '')} {short(row.get('text', ''), 160)}".rstrip())
        if len(rows) > 5 and not cut:  # a cut list already has its note; it names no count
            left.append(f"{label}: {len(rows) - 5} more")
        return lines

    def _withheld_note(self, count, left, what):
        if count:
            left.append(f"{what}: {count} withheld (a refused path)")

    def _symbol_web(self, kind, made, rel, name, lines, left):
        raw = self.call(kind, made, "definition", rel, name=name)
        bad = self._status(kind, raw)
        if bad:
            lines.append(bad.text)
            return bad.status, bad.text.splitlines()[0]
        listed, handed, unreadable = listed_rows(raw, "definitions")
        rows, screened = self.screen_dicts(listed)
        read = answer_read(raw, screened=screened, not_returned=max(_int(raw.get("total", handed)) - handed, 0), unreadable=unreadable)
        refused, cut = read.withheld, read.cut
        self._withheld_note(refused, left, f"definitions of {name} in {rel}")
        head = f"definitions of {name} in {rel}"
        if cut:
            left.append(more_note(head, 0, True, "the definition may be in the part not returned"))
        if not rows and refused and not cut:
            lines.append(f"{name} in {rel}: every definition the navigator found is at a refused path")
            return "ok", None
        lines.append(list_head(head, len(rows[:5]), len(rows), read,
                               empty=f"no definition of {name} in {rel} (the web navigator found none)"))
        if not rows:
            return "ok", None
        lines += self._definition_lines(rows, left, cut, f"definitions of {name}")
        if raw.get("signature"):
            lines.append(f"  signature: {short(raw['signature'], 300)}")
        if raw.get("doc"):
            lines.append(f"  doc: {short(raw['doc'], 400)}")
        refs = self.call(kind, made, "references", rel, name=name, limit=25)
        bad = self._status(kind, refs)
        if bad:
            lines.append("references unavailable — " + bad.text.replace("\n", " "))
            return "ok", None
        listed, handed, unreadable = listed_rows(refs, "references")
        shown, screened = self.screen_dicts(listed)
        read = answer_read(refs, screened=screened, not_returned=max(_int(refs.get("total", handed)) - handed, 0), unreadable=unreadable)
        refused, cut = read.withheld, read.cut
        total = _int(refs.get("total", len(shown) + refused))
        lines.append(list_head(f"references of {name}", min(5, len(shown)), total, read, of=True))
        lines += [f"  {r['path']}:{r.get('line', 0)}  {short(r.get('text', ''), 100)}".rstrip() for r in shown[:5]]
        add = more_note(f"references of {name}", total - min(5, len(shown)) - refused, cut, "pass path to another file or read the callers directly")
        if add:
            left.append(add)
        self._withheld_note(refused, left, f"references of {name}")
        return "ok", None

    def _symbol_api(self, kind, made, rel, name, lines, left):
        raw = self.call(kind, made, "symbols", rel, limit=API_SYMBOLS)
        bad = self._status(kind, raw)
        if bad:
            lines.append(bad.text)
            return bad.status, bad.text.splitlines()[0]
        listed, handed, unreadable = listed_rows(raw, "symbols")
        total = _int(raw.get("total", handed))
        named, screened = self.screen_dicts([s for s in listed if s.get("name") == name])
        read = answer_read(raw, screened=screened, not_returned=max(total - handed, 0), unreadable=unreadable)
        refused, cut = read.withheld, read.cut
        partial = cut or total > handed
        self._withheld_note(refused, left, f"definitions of {name} in {rel}")
        if not named:
            if partial:
                lines.append(list_head(f"definitions of {name} in {rel}", 0, total, read))
                lines.append("the listing was cut, so this does not show that it is not defined there" if cut else
                             "the navigator did not return the whole listing, so this does not show that it is not defined there")
                lines.append("Next: read the file directly, or ask ctx_relations for its importers.")
                left.append(f"symbols of {rel}: " + (f"{total - handed} not listed" if not cut else "more not listed (the navigator cut its list)"))
            else:
                lines.append(absent(f"no definition of {name} in {rel} (the api navigator lists none by that name)",
                                    read, label=f"definitions of {name} in {rel}"))
            return "ok", None
        lines.append(list_head(f"definitions of {name} in {rel}", min(5, len(named)), len(named), read))
        lines += [f"{s['path']}:{s.get('line', 0)} {s.get('kind', '')}" for s in named[:5]]
        add = more_note(f"definitions of {name}", len(named) - 5, partial, "pass path to a narrower file")
        if add:
            left.append(add)
        first = named[0]
        if first.get("signature"):
            lines.append(f"  signature: {short(first['signature'], 300)}")
        if first.get("doc"):
            lines.append(f"  doc: {short(first['doc'], 400)}")
        lines.append(f"references: api files have none here; ctx_relations(target={rel!r}, direction='importers') lists who imports this file")
        return "ok", None

    def op_ctx_relations(self, args):
        from . import brief
        target, direction, limit = args["target"], args["direction"], args.get("limit", 10)
        lines = []
        if "/" in target or "." in target:
            rel = self.path(target)
            if not (self.project / rel).is_file():
                return self.finish("invalid", [f"no such file: {rel}", "Next: check the path, or name a symbol defined once in the project."])
        else:
            where, scanned = self.locate(target, limit=SYMBOL_PLACES + 1)
            if not where:
                return self.finish("invalid", [f"{target} is not a file path; " + absent("no declaration of that name was found", scanned),
                                               "Next: pass a project file path, or check the symbol's spelling."])
            files = list(dict.fromkeys(place[0] for place in where))
            if len(files) > 1:
                shown = ", ".join(files[:5])
                return self.finish("invalid", [f"{target} is declared in {len(files)}{'+' if len(where) > SYMBOL_PLACES else ''} files ({shown}"
                                               f"{', ...' if len(files) > 5 else ''}), so the relations of {target} are not one answer",
                                               "Next: call ctx_relations again with target set to the path of the file you mean."],
                                   reason=f"{target} is declared in more than one file")
            rel = files[0]
            lines.append(f"target resolved to {rel} (declared at line {where[0][1]})")
        return brief.relations(self, rel, direction, limit, lines)


def _remove(path):
    """Take back a candidate that was written but whose call could not be logged."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _int(value):
    """A whole number from a navigator field (0 when it is anything else)."""
    if isinstance(value, bool):
        return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# ----------------------------------------------------------------------------------------------------------- command line

def main(argv):
    """tp-ctx ctx_* [--project DIR] [--json] [--input JSON]: one logged operation. Exit 0 answered, 1 no answer, 2 refused, 3 not logged."""
    parser = argparse.ArgumentParser(prog="tp-ctx", description="the context surface: " + ", ".join(COMMANDS))
    parser.add_argument("--project")
    parser.add_argument("--log")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("tool", choices=COMMANDS)
    parser.add_argument("--input", default=None, help="the tool's arguments as a JSON object")
    args = parser.parse_args(argv)
    problem = None
    parsed = {}
    if args.input is not None:
        try:
            parsed = json.loads(args.input)
        except ValueError:
            problem = "the input is not valid JSON"
    logged = parsed if isinstance(parsed, dict) else {"input": args.input}

    def run():
        if problem:
            return calls.Answer("invalid", f"invalid arguments: {problem}\nUsage: {usage(args.tool) if args.tool in SPECS else args.tool}\n"
                                "Next: pass --input '{...}' as one JSON object.", reason=problem, data={"truncated": False, "left_out": []})
        chosen = args.project or os.environ.get("TP_CTX_PROJECT")
        project = Path(chosen).resolve() if chosen else store.find_project()
        if project is None or not project.is_dir():
            return calls.Answer("no_project", "no project: pass --project DIR or run inside a directory with .tmux-pi or .git",
                                data={"truncated": False, "left_out": []})
        layout = store.Layout(project)
        if args.tool == "ctx_schema":
            return calls.Answer("ok", json.dumps(schema(), sort_keys=True), data={"truncated": False, "left_out": []})
        engine = Engine(layout, Navigators(layout, modules_from_env(os.environ)), pin_reader(project), os.environ)
        try:
            return engine.run(args.tool, parsed)
        finally:
            engine.close()

    try:
        answer = calls.call(args.tool, logged, run, log_file=args.log, operator=False, exclusive=args.tool == "ctx_note")
    except calls.LogUnavailable as exc:
        print(f"tp-ctx: {redact.guard(str(exc))}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 130
    data = answer.data if isinstance(answer.data, dict) else {}
    if args.json:
        payload = {"status": answer.status, "text": answer.text, "truncated": bool(data.get("truncated")), "left_out": data.get("left_out", [])}
        if isinstance(data.get("result"), dict):
            payload["result"] = data["result"]  # ctx_note: {id, status: "candidate", path}
        print(json.dumps(redact.guard_json(payload), ensure_ascii=False))
    elif answer.status != "ok":
        print(redact.guard(answer.text), file=sys.stderr)
    else:
        print(redact.guard(answer.text))
    return EXIT[answer.status]
