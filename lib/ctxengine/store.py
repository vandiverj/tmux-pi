"""The knowledge store: one markdown note per file under <project>/.tmux-pi/knowledge/ (design §4).

    approved/<id>.md    notes Jerrick approved by hand (`tp-ctx knowledge approve`)
    candidates/<id>.md  notes a worker or a seed wrote; never counted as approved
    superseded/<id>.md  approved notes a later approval replaced; read only by id
    sources.json        extra read-only sources (engineering-context), listed here, read by later lanes

A note is frontmatter plus a body of at most 1,200 characters. The frontmatter is a strict subset of YAML (scalars, flow lists,
flow maps, `#` comments); an unknown or repeated key, a malformed line or an invalid value is an error, never a guess. Messages
name the rule and the field, never the value, so a refused note does not echo what made it unacceptable.

Everything written here goes through lib/safe_write: no link is followed, a new note is placed with link(2) so it can never
replace an existing one, and the screens in `redact` run before anything is written.
"""
import datetime as _dt
import json
import os
import re
import stat
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

import safe_write

from . import freshness

KINDS = ("convention", "framework", "relation", "decision", "lesson")
STATUSES = ("candidate", "approved")
DIRS = {"approved": "approved", "candidate": "candidates", "superseded": "superseded"}
BODY_MAX = 1200
TITLE_MAX = 100
NOTE_FILE_MAX = 16 * 1024
SCOPE_MAX, TAGS_MAX, EVIDENCE_MAX, VERSIONS_MAX, SUPERSEDES_MAX = 10, 10, 10, 10, 5
GLOB_MAX = 200
EVIDENCE_ITEM_MAX = 300
SOURCE_PREFIXES = ("review:", "doc:", "work:", "worker:")
FIELD_ORDER = ("id", "kind", "title", "scope", "tags", "versions", "source", "evidence", "evidence_hashes", "supersedes", "status",
               "created", "last_reviewed")
ID_RE = re.compile(r"^kn-[a-z0-9]+(?:-[a-z0-9]+)*$")
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,39}$")
PACKAGE_RE = re.compile(r"^[@a-z0-9][@a-z0-9._/-]{0,99}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
GLOB_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
PLAIN = re.compile(r"^[A-Za-z0-9_./@-][A-Za-z0-9_./@:#+-]*$")


class NoteError(ValueError):
    """A note that cannot be read, parsed or accepted. `problems` are rule names with their field, never values."""

    def __init__(self, problems):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("; ".join(self.problems))


class StoreError(RuntimeError):
    """An operation on the store that cannot proceed (missing note, existing note, unsafe location)."""


# ------------------------------------------------------------------------------------------------------------- globs

def parse_glob(glob):
    """The segments of a scope glob, or ValueError naming the rule. A glob is a relative posix path whose segments may use
    `*` and `?`; a segment that is exactly `**` stands for any number of directories. Nothing else is special."""
    if not isinstance(glob, str) or not glob:
        raise ValueError("a glob must be a non-empty string")
    if len(glob) > GLOB_MAX:
        raise ValueError(f"a glob is at most {GLOB_MAX} characters")
    if GLOB_CONTROL.search(glob) or "\\" in glob or any(ch in glob for ch in "[]{}!"):
        raise ValueError("a glob uses only path characters, `*`, `?` and `**`")
    if glob.startswith("/") or glob.endswith("/"):
        raise ValueError("a glob is relative and names files, not a trailing directory slash")
    segments = glob.split("/")
    for segment in segments:
        if segment in ("", ".", ".."):
            raise ValueError("a glob has no empty, `.` or `..` segment")
        if "**" in segment and segment != "**":
            raise ValueError("`**` must be a whole path segment")
    return segments


def valid_glob(glob):
    try:
        parse_glob(glob)
    except ValueError:
        return False
    return True


# -------------------------------------------------------------------------------------------------------- frontmatter

def _skip_ws(text, i):
    while i < len(text) and text[i] in " \t":
        i += 1
    return i


def _quoted(text, i):
    quote = text[i]
    if quote == '"':
        j = i + 1
        while j < len(text):
            if text[j] == "\\":
                j += 2
                continue
            if text[j] == '"':
                try:
                    return json.loads(text[i:j + 1]), j + 1
                except ValueError:
                    raise NoteError("bad escape in a quoted string") from None
            j += 1
    else:
        j = text.find("'", i + 1)
        if j >= 0:
            return text[i + 1:j], j + 1
    raise NoteError("unterminated quoted string")


def _plain(text, i, stops):
    j = i
    while j < len(text) and text[j] not in stops:
        j += 1
    return text[i:j].strip(), j


def _scalar(text, i, stops):
    i = _skip_ws(text, i)
    if i < len(text) and text[i] in "\"'":
        value, i = _quoted(text, i)
        return value, i
    value, i = _plain(text, i, stops)
    return value, i


def _flow_list(text, i):
    items, i = [], i + 1
    i = _skip_ws(text, i)
    if i < len(text) and text[i] == "]":
        return items, i + 1
    while True:
        value, i = _scalar(text, i, ",]")
        items.append(value)
        i = _skip_ws(text, i)
        if i >= len(text):
            raise NoteError("unterminated list")
        if text[i] == ",":
            i += 1
            continue
        if text[i] == "]":
            return items, i + 1
        raise NoteError("bad list")


def _flow_map(text, i):
    result, i = {}, i + 1
    i = _skip_ws(text, i)
    if i < len(text) and text[i] == "}":
        return result, i + 1
    while True:
        key, i = _scalar(text, i, ":,}")
        i = _skip_ws(text, i)
        if i >= len(text) or text[i] != ":":
            raise NoteError("a map entry is key: value")
        value, i = _scalar(text, i + 1, ",}")
        if key in result:
            raise NoteError("repeated map key")
        result[key] = value
        i = _skip_ws(text, i)
        if i >= len(text):
            raise NoteError("unterminated map")
        if text[i] == ",":
            i += 1
            continue
        if text[i] == "}":
            return result, i + 1
        raise NoteError("bad map")


def parse_value(text):
    """One frontmatter value: a scalar, a flow list `[a, "b"]` or a flow map `{k: v}`, with an optional trailing ` # comment`."""
    text = text.strip()
    if not text or text.startswith("#"):
        return ""
    if text[0] == "[":
        value, end = _flow_list(text, 0)
    elif text[0] == "{":
        value, end = _flow_map(text, 0)
    elif text[0] in "\"'":
        value, end = _quoted(text, 0)
    else:
        return re.split(r"\s#", text, maxsplit=1)[0].strip()
    rest = text[end:].strip()
    if rest and not rest.startswith("#"):
        raise NoteError("text after a value")
    return value


def split_frontmatter(text):
    """(dict of raw values, body) for a note file's text. NoteError when the frontmatter is missing, malformed or repeats a key."""
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        raise NoteError("frontmatter must start with a --- line")
    try:
        end = next(n for n in range(1, len(lines)) if lines[n].strip() == "---")
    except StopIteration:
        raise NoteError("frontmatter has no closing --- line") from None
    meta = {}
    for number, line in enumerate(lines[1:end], start=2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.match(r"^([a-z_]+):(?:\s+(.*))?$", line.rstrip())
        if not match:
            raise NoteError(f"frontmatter line {number} is not `key: value`")
        key = match.group(1)
        if key in meta:
            raise NoteError(f"frontmatter repeats the key {key}")
        try:
            meta[key] = parse_value(match.group(2) or "")
        except NoteError as exc:
            raise NoteError(f"frontmatter line {number} ({key}): {exc}") from None
    body = "\n".join(lines[end + 1:]).strip("\n")
    return meta, body


# ------------------------------------------------------------------------------------------------------------- notes

@dataclass
class Note:
    id: str
    kind: str
    scope: list
    source: str
    body: str
    status: str = "candidate"
    title: str = ""
    tags: list = field(default_factory=list)
    versions: dict = field(default_factory=dict)
    evidence: list = field(default_factory=list)
    evidence_hashes: dict = field(default_factory=dict)  # {evidence entry: hash of the cited line}, taken from the tree when a candidate is written
    supersedes: list = field(default_factory=list)
    created: str = ""
    last_reviewed: str = ""
    where: str = ""  # the directory it was read from: approved, candidate or superseded ("" for a new note)

    @property
    def headline(self):
        """The title, else the first line of the body (a leading `# ` dropped), cut to TITLE_MAX."""
        if self.title:
            return self.title
        for line in self.body.splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                return line[:TITLE_MAX]
        return self.id


def _strings(value, field_name, problems, maximum):
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        problems.append(f"{field_name} must be a list of strings")
        return []
    if len(value) > maximum:
        problems.append(f"{field_name} has at most {maximum} entries")
    return value


def from_meta(meta, body, where=""):
    """A Note from parsed frontmatter, or NoteError listing every problem found (unknown keys, wrong types, missing fields)."""
    problems = []
    unknown = sorted(set(meta) - set(FIELD_ORDER))
    if unknown:
        problems.append("unknown frontmatter key(s): " + ", ".join(k[:30] for k in unknown[:5]))
    for required in ("id", "kind", "scope", "source", "status", "created"):
        if required not in meta or meta[required] in ("", [], {}):
            problems.append(f"{required} is required")

    def text(name):
        value = meta.get(name, "")
        if not isinstance(value, str):
            problems.append(f"{name} must be text")
            return ""
        return value

    scope = _strings(meta.get("scope", []), "scope", problems, SCOPE_MAX) if "scope" in meta else []
    tags = _strings(meta.get("tags", []), "tags", problems, TAGS_MAX) if "tags" in meta else []
    evidence = _strings(meta.get("evidence", []), "evidence", problems, EVIDENCE_MAX) if "evidence" in meta else []
    supersedes = _strings(meta.get("supersedes", []), "supersedes", problems, SUPERSEDES_MAX) if "supersedes" in meta else []
    versions = meta.get("versions", {})
    if versions == "":
        versions = {}
    if not isinstance(versions, dict) or not all(isinstance(v, str) for v in versions.values()):
        problems.append("versions must be a map of package to range text")
        versions = {}
    hashes = meta.get("evidence_hashes", {})
    if hashes == "":
        hashes = {}
    if not isinstance(hashes, dict) or not all(isinstance(v, str) for v in hashes.values()):
        problems.append("evidence_hashes must be a map of evidence entry to hash text")
        hashes = {}
    if problems:
        raise NoteError(problems)
    return Note(id=text("id"), kind=text("kind"), scope=list(scope), source=text("source"), body=body, status=text("status"),
                title=text("title"), tags=list(tags), versions=dict(versions), evidence=list(evidence), evidence_hashes=dict(hashes),
                supersedes=list(supersedes), created=text("created"), last_reviewed=text("last_reviewed"), where=where)


def validate(note, require_evidence=None):
    """Raise NoteError listing every rule `note` breaks. Evidence is required for an approved note, and for any note when
    `require_evidence` is true (a new candidate always needs one)."""
    problems = []
    if not ID_RE.match(note.id or "") or len(note.id) > 80:
        problems.append("id must look like kn-2026-10-31-short-slug (lower case letters, digits, dashes; at most 80)")
    if note.kind not in KINDS:
        problems.append("kind must be one of " + ", ".join(KINDS))
    if note.status not in STATUSES:
        problems.append("status must be candidate or approved")
    if not 0 < len(note.scope) <= SCOPE_MAX:
        problems.append(f"scope needs 1 to {SCOPE_MAX} globs")
    for number, glob in enumerate(note.scope, start=1):
        try:
            parse_glob(glob)
        except ValueError as exc:
            problems.append(f"scope glob {number}: {exc}")
    if len(note.title) > TITLE_MAX or CONTROL.search(note.title) or "\n" in note.title:
        problems.append(f"title is one line of at most {TITLE_MAX} characters")
    for tag in note.tags:
        if not TAG_RE.match(tag):
            problems.append("a tag is lower case letters, digits, `_`, `.`, `-` (at most 40)")
            break
    if len(note.versions) > VERSIONS_MAX:
        problems.append(f"versions has at most {VERSIONS_MAX} packages")
    for package, spec in note.versions.items():
        if not PACKAGE_RE.match(package):
            problems.append("a versions key must be a package name")
            continue
        try:
            freshness.parse_range(spec)
        except ValueError as exc:
            problems.append(f"versions range for {package[:40]}: {exc}")
    if not note.source or (note.source != "human" and not note.source.startswith(SOURCE_PREFIXES)) or len(note.source) > 200 \
            or CONTROL.search(note.source):
        problems.append("source must be human, or start with review:, doc:, work: or worker:")
    needs = note.status == "approved" if require_evidence is None else (require_evidence or note.status == "approved")
    if needs and not note.evidence:
        problems.append("evidence is required (a file:line or a review id)")
    for item in note.evidence:
        if not item.strip() or len(item) > EVIDENCE_ITEM_MAX or CONTROL.search(item):
            problems.append(f"each evidence entry is 1 to {EVIDENCE_ITEM_MAX} characters, one line")
            break
    if len(note.evidence_hashes) > EVIDENCE_MAX or any(key not in note.evidence or not freshness.HASH_RE.match(value)
                                                        for key, value in note.evidence_hashes.items()):
        problems.append("evidence_hashes names listed evidence entries, each with a 16 character lower case hex hash")
    for other in note.supersedes:
        if not ID_RE.match(other):
            problems.append("supersedes lists note ids")
            break
    if note.id in note.supersedes:
        problems.append("a note cannot supersede itself")
    if len(set(note.supersedes)) != len(note.supersedes):
        problems.append("supersedes lists each id once")
    for name in ("created", "last_reviewed"):
        value = getattr(note, name)
        if value and not _real_date(value):
            problems.append(f"{name} must be a date YYYY-MM-DD")
    if not note.created:
        problems.append("created is required")
    if not note.body.strip():
        problems.append("the body is empty")
    elif len(note.body) > BODY_MAX:
        problems.append(f"the body is {len(note.body)} characters; the limit is {BODY_MAX}")
    if problems:
        raise NoteError(problems)
    return note


def _real_date(value):
    if not DATE_RE.match(value):
        return False
    try:
        _dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def parse(text, where=""):
    """A validated Note from a file's text."""
    meta, body = split_frontmatter(text)
    return validate(from_meta(meta, body, where))


def _scalar_text(value):
    return value if PLAIN.match(value) and value not in ("true", "false", "null") else json.dumps(value, ensure_ascii=False)


def render(note):
    """The note as file text: frontmatter in FIELD_ORDER, then the body. Call it on a validated note: `id`, `kind` and `status`
    are written as they are. Strings that are not plain words are JSON-quoted, so the file parses back to the same note."""
    lines = ["---"]
    for name in FIELD_ORDER:
        value = getattr(note, name)
        if name in ("id", "kind", "status"):
            lines.append(f"{name}: {value}")
        elif name == "title":
            if value:
                lines.append(f"title: {json.dumps(value, ensure_ascii=False)}")
        elif name in ("source", "created", "last_reviewed"):
            if value:
                lines.append(f"{name}: {_scalar_text(value)}")
        elif value or name == "scope":
            lines.append(f"{name}: {json.dumps(value, ensure_ascii=False)}")
    lines += ["---", note.body.strip("\n"), ""]
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------------------------- layout

def find_project(start=None, env=None):
    """The project a call is about: $TP_CTX_PROJECT, else the nearest ancestor of `start` (default the working directory) that
    holds a `.tmux-pi` directory or a `.git`. None when there is none."""
    env = os.environ if env is None else env
    chosen = env.get("TP_CTX_PROJECT")
    if chosen:
        return Path(chosen).resolve() if Path(chosen).is_dir() else None
    here = Path(start or os.getcwd()).resolve()
    for folder in (here, *here.parents):
        if (folder / ".tmux-pi").is_dir() or (folder / ".git").exists():
            return folder
    return None


class Layout:
    """Where a project's knowledge and cache live."""

    def __init__(self, project):
        self.project = Path(project)
        self.base = self.project / ".tmux-pi"
        self.knowledge = self.base / "knowledge"
        self.cache = self.base / "ctx-cache"
        self.sources = self.knowledge / "sources.json"

    def directory(self, where):
        return self.knowledge / DIRS[where]

    def path(self, where, ident):
        return self.directory(where) / f"{ident}.md"


def _no_links(layout, path):
    """StoreError when any component of `path` below the project is a symlink."""
    try:
        safe_write.check_below(layout.project, path, "knowledge path")
    except (safe_write.UnsafePath, ValueError) as exc:
        raise StoreError(f"refused: unsafe knowledge location ({type(exc).__name__}); nothing was read or written") from None


def read_file(path):
    """The text of a note file: a regular file (never a symlink) of at most NOTE_FILE_MAX bytes, valid UTF-8."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        raise NoteError("the file cannot be opened (missing or a symlink)") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise NoteError("not a regular file")
        if info.st_size > NOTE_FILE_MAX:
            raise NoteError(f"the file is over {NOTE_FILE_MAX} bytes")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(NOTE_FILE_MAX + 1)
    finally:
        os.close(fd)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise NoteError("the file is not UTF-8 text") from None


def load(layout, where, ident):
    """The validated note `ident` in directory `where`, checked against its place: the file name is the id, and the status
    matches the directory (approved/ holds approved notes, candidates/ holds candidates). StoreError when absent."""
    if not ID_RE.match(ident or ""):
        raise StoreError("not a note id")
    path = layout.path(where, ident)
    _no_links(layout, path)
    if not os.path.lexists(path):
        raise StoreError(f"no {where} note {ident}")
    return _checked(read_file(path), where, ident)


def _checked(text, where, ident):
    note = parse(text, where)
    if note.id != ident:
        raise NoteError("the id in the file does not match its file name")
    expected = {"approved": "approved", "candidate": "candidate"}.get(where)
    if expected and note.status != expected:
        raise NoteError(f"status is {note.status} but the file is in the {DIRS[where]} directory")
    return note


def scan(layout, wheres=("approved", "candidate")):
    """(notes, invalid) for the given directories. `invalid` is a list of {path, problems}: a bad file never hides the others,
    and is never silently dropped. A directory that is a symlink is reported once and not entered. A duplicate id (the same
    id in two directories) keeps the first of `wheres` and reports the rest."""
    notes, invalid, seen = [], [], {}
    for where in wheres:
        directory = layout.directory(where)
        rel = f"{layout.base.name}/knowledge/{DIRS[where]}"
        if not os.path.lexists(directory):
            continue
        try:
            _no_links(layout, directory)
            names = sorted(os.listdir(directory))
        except (StoreError, OSError) as exc:
            invalid.append({"path": rel, "problems": [str(exc) if isinstance(exc, StoreError) else "the directory cannot be listed"]})
            continue
        for name in names:
            if not name.endswith(".md") or name.startswith("."):
                continue
            ident = name[:-3]
            try:
                note = _checked(read_file(directory / name), where, ident)
            except NoteError as exc:
                invalid.append({"path": f"{rel}/{name}", "problems": exc.problems})
                continue
            if note.id in seen:
                invalid.append({"path": f"{rel}/{name}", "problems": [f"duplicate id (also in {seen[note.id]})"]})
                continue
            seen[note.id] = DIRS[where]
            notes.append(note)
    return notes, invalid


# ------------------------------------------------------------------------------------------------------------ writing

def today(env=None):
    env = os.environ if env is None else env
    return env.get("TP_CTX_TODAY") or _dt.date.today().isoformat()


def slug(text, limit=40):
    words = re.findall(r"[a-z0-9]+", text.lower())
    out = ""
    for word in words:
        if len(out) + len(word) + 1 > limit:
            break
        out = f"{out}-{word}" if out else word
    return out or "note"


def new_id(layout, title, date=None):
    """kn-<date>-<slug>, the first one that is not used by any note in any directory."""
    base = f"kn-{date or today()}-{slug(title)}"
    taken = {n[:-3] for where in DIRS for n in _names(layout.directory(where))}
    ident, count = base, 1
    while ident in taken:
        count += 1
        ident = f"{base}-{count}"
    return ident


def _names(directory):
    try:
        return [n for n in os.listdir(directory) if n.endswith(".md")]
    except OSError:
        return []


def _place(layout, where, note, text):
    """Write `text` as the new file of `note` in `where`, refusing to replace anything: the text goes to a temp file and is
    linked into place, which fails if the name exists."""
    directory = layout.directory(where)
    path = layout.path(where, note.id)
    try:
        safe_write.ensure_dir(directory, root=layout.project, what="knowledge directory")
        temp = safe_write.write_temp(path, text, mode=0o644, create=0o644, fsync=True, root=layout.project)
    except safe_write.UnsafePath:
        raise StoreError("refused: unsafe knowledge location (a symlink); nothing was written") from None
    except OSError as exc:
        raise StoreError(f"cannot write the {where} note ({exc.strerror or type(exc).__name__}); nothing was written") from None
    try:
        try:
            os.link(temp, path)
        except FileExistsError:
            raise StoreError(f"{where} note {note.id} already exists; nothing was overwritten") from None
        except OSError as exc:
            raise StoreError(f"cannot place the {where} note ({exc.strerror or type(exc).__name__}); nothing was written") from None
    finally:
        try:
            os.unlink(temp)
        except OSError:
            pass
    return path


def add_candidate(layout, note, screen=None):
    """Validate, screen and write `note` as a new candidate. It is forced to status candidate, today's `created`, and needs
    evidence. Never overwrites: an existing id in any directory is refused. `screen(note, text)` raises to refuse."""
    note.status = "candidate"
    note.where = "candidate"
    note.created = note.created or today()
    note.last_reviewed = ""
    note.evidence_hashes = freshness.hash_evidence(layout.project, note.evidence)  # what the cited lines say now, from the tree
    validate(note, require_evidence=True)
    for where in DIRS:
        if os.path.lexists(layout.path(where, note.id)):
            raise StoreError(f"note {note.id} already exists in {DIRS[where]}; nothing was overwritten")
    text = render(note)
    if screen:
        screen(note, text)
    return _place(layout, "candidate", note, text)


def approve_plan(layout, ident, screen=None):
    """(candidate, approved copy, rendered text, notes it supersedes) for approving `ident`, after every check; nothing is
    changed. The approved copy has status approved and today's `last_reviewed`."""
    candidate = load(layout, "candidate", ident)
    if os.path.lexists(layout.path("approved", ident)):
        raise StoreError(f"approved note {ident} already exists; nothing was overwritten")
    approved = Note(**{**candidate.__dict__, "status": "approved", "last_reviewed": today(), "where": "approved"})
    validate(approved)
    replaced = []
    for other in approved.supersedes:
        try:
            replaced.append(load(layout, "approved", other))
        except (StoreError, NoteError):
            raise StoreError(f"supersedes {other}: there is no valid approved note with that id") from None
        if os.path.lexists(layout.path("superseded", other)):
            raise StoreError(f"superseded/{other}.md already exists; nothing was overwritten")
    text = render(approved)
    if screen:
        screen(approved, text)
    return candidate, approved, text, replaced


def approve_apply(layout, plan):
    """Carry out `approve_plan` as one step: either the approved note is in place, the candidate retired and every replaced note
    moved to superseded/, or the store is exactly as it was and StoreError says so.

    Stage: place the approved note and link each replaced note into superseded/ (only new names; nothing is removed). Commit:
    move the candidate aside (a hidden name no scan reads) and remove each replaced note from approved/. Each step that
    succeeded registers its undo, and a failure at any step runs them in reverse. The one step after the last undo point, the
    removal of the hidden candidate copy, cannot undo anything: a failure there leaves a stray hidden file and no other change."""
    candidate, approved, text, replaced = plan
    new_path = layout.path("approved", approved.id)
    candidate_path = layout.path("candidate", candidate.id)
    aside = layout.directory("candidate") / f".retiring-{candidate.id}.md"
    undo = []
    try:
        _place(layout, "approved", approved, text)
        undo.append(partial(os.unlink, new_path))
        if replaced:
            safe_write.ensure_dir(layout.directory("superseded"), root=layout.project, what="knowledge directory")
        for old in replaced:
            source, target = layout.path("approved", old.id), layout.path("superseded", old.id)
            os.link(source, target)  # fails if the name exists; the old note is then still approved
            undo.append(partial(os.unlink, target))
        os.rename(candidate_path, aside)
        undo.append(partial(os.rename, aside, candidate_path))
        for old in replaced:
            source, target = layout.path("approved", old.id), layout.path("superseded", old.id)
            os.unlink(source)
            undo.append(partial(os.link, target, source))
    except (OSError, StoreError) as exc:
        stuck = 0
        for step in reversed(undo):
            try:
                step()
            except OSError:
                stuck += 1
        reason = exc.strerror or type(exc).__name__ if isinstance(exc, OSError) else str(exc)
        if stuck:
            raise StoreError(f"approval failed ({reason}) and {stuck} step(s) could not be put back; check {layout.knowledge.name}/ "
                             "before trying again") from None
        raise StoreError(f"approval failed ({reason}); nothing was changed") from None
    try:
        os.unlink(aside)
    except OSError:
        pass
    return new_path


def get(layout, ident, wheres=("approved", "candidate", "superseded")):
    """The note `ident` from the first directory of `wheres` that has a valid one; StoreError naming what was tried."""
    last = None
    for where in wheres:
        try:
            return load(layout, where, ident)
        except StoreError as exc:
            last = exc
        except NoteError as exc:
            raise StoreError(f"note {ident} in {DIRS[where]} is invalid: {'; '.join(exc.problems)}") from None
    raise last or StoreError(f"no note {ident}")


def list_sources(layout):
    """The extra read-only sources named in sources.json: a list of {id, path, exists}. A missing file is an empty list; a
    malformed or linked one is a StoreError. Their contents are read by later lanes, not here."""
    path = layout.sources
    if not os.path.lexists(path):
        return []
    _no_links(layout, path)
    try:
        data = json.loads(read_file(path))
    except (NoteError, ValueError):
        raise StoreError("sources.json is not valid JSON text") from None
    entries = data.get("sources") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise StoreError('sources.json must be {"sources": [{"id": ..., "path": ...}]}')
    out = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not isinstance(entry.get("path"), str):
            raise StoreError("each source needs a text id and path")
        out.append({"id": entry["id"][:60], "path": entry["path"], "exists": os.path.isdir(os.path.expanduser(entry["path"]))})
    return out
