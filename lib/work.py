"""Durable work context (Phase A): workspace and note files in a user-level store.

A workspace is one Markdown file with small frontmatter; a note is the same kind of file for reusable knowledge. A decision is
a structured entry inside a workspace (`<workspace-id>/D<n>`). Nothing here is deleted: archive and supersede move files.

The store is `~/.config/tmux-pi/work` (config/work.json `store`), overridden by `TP_WORK_DIR`; tests always set it. Directories
are 0700, files 0600. Every write is a temp file, fsync and rename under one advisory lock, so a failure leaves the old file.
The parser tolerates hand edits: only the frontmatter, the section headings and the decision entries are read; anything else is
kept as it is.
"""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
KINDS = {"workspace": "workspaces", "note": "notes"}
WORKSPACE_STATUS = ("active", "paused", "done", "archived")
NOTE_STATUS = ("draft", "active", "superseded", "archived")
PRIORITIES = ("high", "normal", "low")
DECISION_STATUS = ("open", "answered", "withdrawn")
SECTIONS = ("Objective", "Scope", "Decisions", "Open decisions", "Blockers / questions", "Next actions", "Links", "Progress", "Sessions")
FIELDS = ("Question", "Background", "Options", "Recommended", "Affects", "Resolution", "By", "At", "Quote", "Withdrawn")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
DECISION_RE = re.compile(r"^([a-z0-9][a-z0-9-]*)/D(\d+)$")
HEADING_RE = re.compile(r"^### (\S+/D\d+)\s*·\s*(\S+)\s*·\s*(\S+)(?:\s*·\s*(blocking))?\s*$")
FIELD_RE = re.compile(r"^(" + "|".join(FIELDS) + r"):[ \t]?(.*)$")
FRONT_ORDER = ("id", "title", "status", "updated", "repos", "tickets", "tags", "goals", "workspaces", "important", "sources", "superseded_by")
LIST_KEYS = {"repos", "tickets", "tags", "goals", "workspaces", "sources"}
LOCK_SECONDS = 10.0


class WorkError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def config():
    try:
        value = json.loads((ROOT / "config" / "work.json").read_text())
    except (OSError, ValueError):
        value = {}
    return value if isinstance(value, dict) else {}


def store_root():
    explicit = os.getenv("TP_WORK_DIR")
    raw = explicit or config().get("store") or "~/.config/tmux-pi/work"
    return Path(os.path.expanduser(raw)).resolve()


def _private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def ensure_store():
    root = store_root()
    _private_dir(root)
    for sub in (*KINDS.values(),):
        _private_dir(root / sub)
        _private_dir(root / "archive" / sub)
    return root


def _file_path(kind, ident, archived=False):
    if not ID_RE.fullmatch(ident or ""):
        raise WorkError(f"invalid id {ident!r}: lowercase letters, digits and '-', at most 48 characters")
    base = store_root() / ("archive" if archived else "") / KINDS[kind]
    return base / f"{ident}.md"


@contextlib.contextmanager
def locked(timeout=None):
    """The store's advisory lock; refuses after `timeout` seconds (TP_WORK_LOCK_TIMEOUT) rather than hanging."""
    root = ensure_store()
    limit = LOCK_SECONDS if timeout is None else timeout
    env = os.getenv("TP_WORK_LOCK_TIMEOUT")
    if timeout is None and env:
        try:
            limit = float(env)
        except ValueError:
            pass
    fd = os.open(root / ".lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        deadline = time.monotonic() + limit
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise WorkError("the work store is locked by another tp-agent; try again")
                time.sleep(0.05)
        yield root
    finally:
        os.close(fd)


def atomic_write(path, text, exclusive=False):
    path = Path(path)
    if path.is_symlink():
        raise WorkError(f"unsafe work file (symlink): {path}")
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(name, 0o600)
        if exclusive:
            try:
                os.link(name, path)  # fails if the id is taken, and the file appears whole or not at all
            except FileExistsError:
                raise WorkError(f"id already exists: {path.stem}")
        else:
            os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


# ---------------------------------------------------------------- frontmatter

def _split_list(text):
    items, cur, quote = [], "", False
    for ch in text:
        if ch == '"':
            quote = not quote
            cur += ch
        elif ch == "," and not quote:
            items.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        items.append(cur.strip())
    return items


def _scalar(text):
    text = text.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        try:
            return json.loads(text)
        except ValueError:
            return text[1:-1]
    if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
        return text[1:-1]
    return text


def _value(raw, key):
    raw = raw.strip()
    if raw.startswith("["):
        end = raw.rfind("]")
        inner = raw[1:end if end > 0 else len(raw)]
        return [_scalar(i) for i in _split_list(inner)]
    if not raw.startswith('"'):
        raw = re.split(r"\s+#", raw, maxsplit=1)[0]
    value = _scalar(raw)
    if key in LIST_KEYS:
        return [value] if value else []
    if key == "important":
        return str(value).lower() in {"true", "yes", "1"}
    return value


def _emit_scalar(value):
    text = str(value)
    if text == "" or text != text.strip() or re.search(r"""[,\[\]"'#]|: |^[-?:]""", text) or "\n" in text:
        return json.dumps(text, ensure_ascii=False)
    return text


def parse(text):
    """(meta, body) from a file's text. No or broken frontmatter gives an empty meta and the whole text as the body."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    for end in range(1, len(lines)):
        if lines[end].strip() == "---":
            break
    else:
        return {}, text
    meta = {}
    for line in lines[1:end]:
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):(.*)$", line)
        if match:
            meta[match.group(1)] = _value(match.group(2), match.group(1))
    return meta, "\n".join(lines[end + 1:])


def render(meta, body):
    keys = [k for k in FRONT_ORDER if k in meta] + [k for k in meta if k not in FRONT_ORDER]
    out = ["---"]
    for key in keys:
        value = meta[key]
        if isinstance(value, list):
            out.append(f"{key}: [" + ", ".join(_emit_scalar(v) for v in value) + "]")
        elif isinstance(value, bool):
            out.append(f"{key}: {'true' if value else 'false'}")
        else:
            out.append(f"{key}: {_emit_scalar(value)}")
    out.append("---")
    return "\n".join(out) + "\n" + body


# ------------------------------------------------------------------- sections

def split_sections(body):
    """(preamble, [[title, text], ...]) split on `## ` headings; text excludes the heading line."""
    pre, sections, cur = [], [], None
    for line in body.split("\n"):
        match = re.match(r"^## (.+?)\s*$", line)
        if match:
            cur = [match.group(1), []]
            sections.append(cur)
        elif cur is None:
            pre.append(line)
        else:
            cur[1].append(line)
    return "\n".join(pre), [[t, "\n".join(ls)] for t, ls in sections]


def join_sections(pre, sections):
    out = [pre] if pre.strip() else []
    for title, text in sections:
        out.append(f"## {title}\n{text.strip(chr(10))}\n" if text.strip() else f"## {title}\n")
    body = "\n".join(out)
    return body if body.endswith("\n") else body + "\n"


def get_section(body, title):
    for t, text in split_sections(body)[1]:
        if t.lower() == title.lower():
            return text.strip("\n")
    return None


def set_section(body, title, text):
    pre, sections = split_sections(body)
    for item in sections:
        if item[0].lower() == title.lower():
            item[1] = text
            break
    else:
        sections.append([title, text])
        order = {name.lower(): i for i, name in enumerate(SECTIONS)}
        known = [s for s in sections if s[0].lower() in order]
        unknown = [s for s in sections if s[0].lower() not in order]
        if not unknown:  # keep canonical order when every section is ours; hand-added sections stay where they are
            sections = sorted(known, key=lambda s: order[s[0].lower()])
    return join_sections(pre, sections)


def append_line(body, title, line):
    current = get_section(body, title) or ""
    return set_section(body, title, (current + "\n" if current else "") + line)


# ------------------------------------------------------------------ documents

ARCHIVED_STATUS = ("archived", "superseded")


class Doc:
    """One loaded file: `meta`, `body`, `kind`, `path`. `in_archive` is where the file sits; `archived` is what it is:
    a file marked archived/superseded is archived wherever it sits, so a move interrupted after the mark leaves no live copy."""

    def __init__(self, kind, ident, path, meta, body, in_archive):
        self.kind, self.id, self.path, self.meta, self.body, self.in_archive = kind, ident, path, meta, body, in_archive

    @property
    def archived(self):
        return self.in_archive or self.meta.get("status") in ARCHIVED_STATUS

    def render(self):
        self.meta["updated"] = now()
        return render(self.meta, self.body)

    def save(self):
        ensure_store()
        atomic_write(self.path, self.render())


def _read(kind, path, in_archive):
    text = path.read_text(encoding="utf-8", errors="replace")
    meta, body = parse(text)
    ident = path.stem
    meta.setdefault("id", ident)
    meta.setdefault("title", ident)
    meta.setdefault("status", "active")
    return Doc(kind, ident, path, meta, body, in_archive)


def load(kind, ident, include_archived=True):
    """The one identity resolver. An archived document is found only with `include_archived`; otherwise it is refused by name."""
    for in_archive in (True, False):
        path = _file_path(kind, ident, in_archive)
        if path.is_file() and not path.is_symlink():
            doc = _read(kind, path, in_archive)
            if doc.archived and not include_archived:
                raise WorkError(f"{kind} {ident} is archived and read-only")
            return doc
    raise WorkError(f"unknown {kind}: {ident}")


def iter_docs(kind, archived=False):
    """Documents that are live (`archived=False`) or archived, by what they are and not by where they sit."""
    seen = set()
    for in_archive in ((True, False) if archived else (False,)):
        folder = store_root() / ("archive" if in_archive else "") / KINDS[kind]
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.md")):
            if path.stem in seen or not path.is_file() or path.is_symlink():
                continue
            try:
                doc = _read(kind, path, in_archive)
            except OSError:
                continue
            if doc.archived == archived:
                seen.add(path.stem)
                yield doc


def slugify(title):
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48].strip("-")
    return slug or "untitled"


def taken(ident):
    return any(_file_path(k, ident, a).exists() for k in KINDS for a in (False, True))


def new_id(title, wanted=None):
    base = wanted or slugify(title)
    if not ID_RE.fullmatch(base):
        raise WorkError(f"invalid id {base!r}: lowercase letters, digits and '-', at most 48 characters")
    if wanted:
        if taken(base):
            raise WorkError(f"id already exists: {base}")
        return base
    ident, n = base, 0
    while taken(ident):  # ids are never reused, archived ones included
        n += 1
        suffix = "-" + os.urandom(2).hex()
        ident = base[:48 - len(suffix)].rstrip("-") + suffix
        if n > 50:
            raise WorkError("could not find a free id")
    return ident


def create(kind, title, wanted=None, meta=None, body=""):
    """Create a document with an exclusive create: an existing file with that id is never replaced."""
    title = (title or "").strip()
    if not title:
        raise WorkError("a title is required")
    ident = new_id(title, wanted)
    values = {"id": ident, "title": title, "status": "active"}
    values.update({k: v for k, v in (meta or {}).items() if v not in (None, [], "")})
    doc = Doc(kind, ident, _file_path(kind, ident), values, body, False)
    ensure_store()
    atomic_write(doc.path, doc.render(), exclusive=True)
    return doc


def move_to_archive(doc, status, extra=None):
    """Archive `doc`: rewrite it in place marked `status` (atomic), then one rename into archive/. The marked file counts as
    archived wherever it sits, so there is never a live copy; retrying an interrupted archive only finishes the rename."""
    if doc.in_archive:
        raise WorkError(f"{doc.kind} {doc.id} is already archived")
    target = _file_path(doc.kind, doc.id, archived=True)
    if target.exists():
        raise WorkError(f"archive already holds {doc.kind} {doc.id}; resolve it by hand")
    if not doc.archived or extra:
        doc.meta["status"] = status
        doc.meta.update(extra or {})
        doc.save()
    os.rename(doc.path, target)
    doc.path, doc.in_archive = target, True
    return doc


# ------------------------------------------------------------------ decisions

def _clean(value):
    return value.strip("\n")


def parse_decisions(text):
    """The decision entries of an `Open decisions` section: dicts with id, priority, status, blocking, every field verbatim."""
    entries, cur, field = [], None, None
    for line in (text or "").split("\n"):
        head = HEADING_RE.match(line)
        if head:
            ident, priority, status, blocking = head.groups()
            cur = {"id": ident, "priority": priority, "status": status, "blocking": bool(blocking), "_raw": [line], "_extra": []}
            for name in FIELDS:
                cur[name.lower()] = ""
            entries.append(cur)
            field = None
            continue
        if cur is None:
            continue
        cur["_raw"].append(line)
        match = FIELD_RE.match(line)
        if match:
            field = match.group(1).lower()
            cur[field] = match.group(2)
        elif field is not None:
            cur[field] += "\n" + (line[2:] if line.startswith("  ") else line)
        elif line.strip():
            cur["_extra"].append(line)  # hand-written text between the heading and the first field
    for entry in entries:
        for name in FIELDS:
            entry[name.lower()] = _clean(entry[name.lower()])
        entry["affects"] = [a.strip() for a in entry["affects"].split("|") if a.strip()]
    return entries


def _field_text(name, value):
    lines = str(value).strip("\n").split("\n")
    return f"{name}: {lines[0]}" + "".join("\n  " + l for l in lines[1:])


def decisions_preamble(text):
    """Hand-written text before the first decision heading of an `Open decisions` section."""
    lines = []
    for line in (text or "").split("\n"):
        if HEADING_RE.match(line):
            break
        lines.append(line)
    return "\n".join(lines).strip("\n")


def render_decisions(entries, preamble=""):
    """Entries the writer did not touch (no `_dirty`) are emitted as they were read; edited ones are re-rendered with
    their hand-written extra lines; `preamble` is the text that preceded the first entry."""
    out = [preamble, ""] if preamble else []
    for e in entries:
        if e.get("_raw") and not e.get("_dirty"):
            out.extend(e["_raw"])
            while out and out[-1] == "":
                out.pop()
            out.append("")
            continue
        out.append(f"### {e['id']} · {e['priority']} · {e['status']}" + (" · blocking" if e.get("blocking") else ""))
        out.extend(e.get("_extra") or [])
        for name in FIELDS:
            value = e.get(name.lower())
            if name == "Affects" and isinstance(value, list):
                value = " | ".join(value)
            if value:
                out.append(_field_text(name, value))
        out.append("")
    return "\n".join(out).rstrip("\n")


def decisions_of(doc):
    return parse_decisions(get_section(doc.body, "Open decisions"))


def put_decisions(doc, entries):
    preamble = decisions_preamble(get_section(doc.body, "Open decisions"))
    doc.body = set_section(doc.body, "Open decisions", render_decisions(entries, preamble))


def next_decision_number(entries):
    # Numbers are never reused: entries are never removed, so the highest seen is the floor.
    return max([int(DECISION_RE.match(e["id"]).group(2)) for e in entries if DECISION_RE.match(e["id"])] + [0]) + 1


def find_decision(ident):
    """(workspace Doc, entries, entry) for a store-global decision id, in non-archived workspaces."""
    match = DECISION_RE.match(ident or "")
    if not match:
        raise WorkError(f"invalid decision id {ident!r}; expected <workspace-id>/D<n>")
    doc = load("workspace", match.group(1), include_archived=False)
    entries = decisions_of(doc)
    for entry in entries:
        if entry["id"] == ident:
            return doc, entries, entry
    raise WorkError(f"unknown decision: {ident}")


PRIORITY_RANK = {name: i for i, name in enumerate(PRIORITIES)}


def open_decisions(include_closed=False):
    """Every decision across all non-archived workspaces, blocking first, then priority, then age (workspace updated, oldest first)."""
    rows = []
    for doc in iter_docs("workspace"):
        for entry in decisions_of(doc):
            if include_closed or entry["status"] == "open":
                rows.append({**{k: v for k, v in entry.items() if not k.startswith("_")}, "workspace": doc.id, "workspace_updated": doc.meta.get("updated", "")})
    rows.sort(key=lambda r: (not r["blocking"], PRIORITY_RANK.get(r["priority"], 9), r["workspace_updated"], r["id"]))
    return rows
