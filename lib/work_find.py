"""`tp-agent find` (Phase B): search the work store, task records of earlier sessions and an optional shared clone.

Metadata filters first, then a case-insensitive all-terms substring match over the file text. Pure Python, nothing indexed or
cached: every search re-reads the files. Workspace and note files are searched in full; task records are searched by their heads
(`TASK_HEAD`, as designed) and an oversized shared file by its first SHARED_MAX bytes, and that last cut is reported (search's
`incomplete` list, `incomplete` in the JSON, a trailing line in the text) rather than returned as "no matches". Output is one row per hit with a path:line and a snippet of at most
160 characters cut from a line that went through the redaction screen whole; a line (or title) over `find_line_max` characters
is not shown at all, only a placeholder naming where to open it. A whole document is never returned. `drafts/` is not searched, and
the shared clone is opened read-only (no git calls).

Two rules keep stored and configured values out of the output. A displayed path is relative to its root and carries the root's
label (`store:notes/x.md`, `task:<session>/tasks/<task>/meta.json`, `session:<session>/session-objective.md`,
`shared:docs/y.md`, `target:<file>` for a promotion), so no configured directory is ever printed. And each command's complete
output (the text, every string of the JSON, a warning, an error) passes through `guard` once, immediately before it is printed;
the per-field `Screen` only decides what to withhold whole, the boundary is the guarantee.
"""
import contextlib
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import sys

import work
from work import WorkError

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from redact import Redactor  # noqa: E402
from redact_rules import FORMATS  # noqa: E402
from redaction_store import Store  # noqa: E402

KINDS = ("workspace", "note", "decision", "task", "session", "shared")
SNIPPET = 160
TASK_HEAD = {"request.md": 4096, "task.md": 8192, "result.md": 4096}
META_FIELDS = ("id", "role", "deliverable", "lane", "parent")
SHARED_MAX = 8 * 1024 * 1024  # a shared file larger than this is searched only in its first SHARED_MAX bytes, and reported
SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TICKET_ROW = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
OMITTED = re.compile(r"^- Sensitive context (.+) was omitted from the packet\.$")


def _read(path, limit=None):
    """A regular file as text (the first `limit` bytes when given), or None (missing, unreadable, a symlink, not a file)."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        with open(path, "rb") as handle:
            return handle.read(limit).decode("utf-8", "replace")
    except OSError:
        return None


def _resolved(path):
    try:
        return Path(path).expanduser().resolve()
    except OSError:
        return Path(path)


def _stamp(path):
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except OSError:
        return ""


# ------------------------------------------------------------ output boundary

def _case_hidden(text):
    """`text` with every known-format secret that only matches once upper-cased replaced by a placeholder. A slug or id is a
    lower-cased title, which hides the shape of a key (`akia…`) from formats that match case."""
    up = "".join(c.upper() if len(c.upper()) == 1 else c for c in text)  # same length, so spans map back onto `text`
    spans = []
    for kind, pattern in FORMATS:
        for match in pattern.finditer(up):
            group = 1 if pattern.groups and match.group(1) is not None and kind != "private_key" else 0
            start, end = match.start(group), match.end(group)
            if end > start and text[start:end] != up[start:end]:
                spans.append((start, end, kind))
    out, pos = [], 0
    for start, end, kind in sorted(spans):
        if start < pos:
            continue
        out += [text[pos:start], f"<REDACTED:{kind}>"]
        pos = end
    return "".join(out) + text[pos:]


def _roots():
    """The configured directories a command may name, as (directory, label), longest first: the work store, the shared clone and
    the promotion targets. A broken configuration yields fewer roots, never an error."""
    found = []
    try:
        found.append((work.store_root(), "store"))
        shared = work.setting("shared").get("path")
        if shared:
            found.append((Path(os.path.expanduser(str(shared))), "shared"))
        found += [(root, "target") for root in promote_roots()]
    except (WorkError, OSError, ValueError, AttributeError, TypeError):
        pass
    out = {}
    for base, label in found:
        if not str(base) or base == base.parent:  # unset, or the filesystem root: nothing to hide
            continue
        for variant in {str(base), str(_resolved(base))}:
            out.setdefault(variant, label)
    return sorted(out.items(), key=lambda item: -len(item[0]))


def relabel(text, roots=None):
    """`text` with each configured root directory replaced by its label (`/x/work/notes/a.md` becomes `store:notes/a.md`)."""
    for variant, label in _roots() if roots is None else roots:
        text = text.replace(variant + os.sep, f"{label}:").replace(variant, f"{label}:")
    return text


def guard(text, roots=None):
    """The output boundary: the complete final string of a command, root directories relabelled and then redacted once
    immediately before it is printed."""
    return _case_hidden(Redactor(Store(persist=False)).redact(relabel(str(text), roots))[0])


def guard_json(value, roots=None):
    """`value` with every string in it (keys included) passed through `guard`; print it with json.dumps."""
    roots = _roots() if roots is None else roots
    if isinstance(value, str):
        return guard(value, roots)
    if isinstance(value, dict):
        return {guard(k, roots) if isinstance(k, str) else k: guard_json(v, roots) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [guard_json(v, roots) for v in value]
    return value


def describe_error(error):
    """An exception as one line for the user. An OS error names its file by the store-relative path or the bare file name, never
    by the directory it is in; any other exception is its message (the unexpected kinds with their type)."""
    if isinstance(error, OSError):
        names = []
        for name in (error.filename, error.filename2):
            if name:
                rel = _relative(os.fsdecode(name), work.store_root())
                names.append(f"store:{rel}" if rel is not None else Path(os.fsdecode(name)).name)
        what = error.strerror or type(error).__name__
        return f"{what}: {', '.join(names)}" if names else what
    if isinstance(error, (WorkError, ValueError, RuntimeError)):
        return str(error)
    return f"{type(error).__name__}: {error}"


def bounded(run):
    """Run one `find` or `work` command and print its whole output through the boundary: `run` returns the text or the JSON-able
    value to print (its stderr is captured), and any exception it raises, of any type, is reported as one guarded line. Nothing
    reaches stdout or stderr except through `guard`. Returns the exit code."""
    out, err = io.StringIO(), io.StringIO()
    code, value, failure = 0, None, None
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
        try:
            value = run()
        except SystemExit as stop:
            code = stop.code if isinstance(stop.code, int) else 1
        except Exception as error:  # noqa: BLE001 - the boundary is the point: every type ends up as a guarded line
            try:
                failure = describe_error(error)
            except Exception:  # noqa: BLE001
                failure = type(error).__name__
    roots = _roots()
    if out.getvalue():
        print(guard(out.getvalue().rstrip("\n"), roots))
    if value is not None:
        print(guard(value, roots) if isinstance(value, str) else json.dumps(guard_json(value, roots), indent=2))
    if err.getvalue():
        print(guard(err.getvalue().rstrip("\n"), roots), file=sys.stderr)
    if failure is not None:
        print(guard(f"tp-agent: {failure}", roots), file=sys.stderr)
        return 1
    return code


# --------------------------------------------------------------- display paths

def _relative(path, root):
    """`path` below `root` as a posix string, or None; both are tried as given and resolved."""
    for base in {Path(root), _resolved(root)}:
        for item in {Path(path), _resolved(path)}:
            try:
                return item.relative_to(base).as_posix()
            except ValueError:
                continue
    return None


def labelled(path, label, root):
    """`label:<path below root>`; a path outside the root shows only its file name, never the directory it is in."""
    rel = _relative(path, root) if root else None
    return f"{label}:{rel if rel is not None else Path(path).name}"


def store_path(path):
    return labelled(path, "store", work.store_root())


def promote_roots():
    """The working trees a promotion may write into, as configuration names them."""
    roots = [str(entry.get("target")) for entry in work.setting("projects").values() if isinstance(entry, dict) and entry.get("target")]
    shared = work.setting("shared").get("path")
    return [Path(os.path.expanduser(r)) for r in [*roots, *([shared] if shared else [])]]


def promoted_label(value):
    """A note's `promoted_to: <path>@<date>` for display: `target:<path below the configured tree>@<date>`."""
    path, _, date = str(value).rpartition("@")
    path = path or str(value)
    shown = next((f"target:{rel}" for root in promote_roots() if (rel := _relative(path, root)) is not None),
                 f"target:{Path(path).name}")
    return f"{shown}@{date}" if date else shown


class Screen:
    """The one screen every value `find` (and `near_duplicates`) returns for display passes through: free text through `text` and
    `snippet`, identifiers (id, path, ticket, tag, status, session, workspace) through `token`, and a whole row through `row`.
    Nothing else formats a stored value for output."""

    def __init__(self):
        self.redactor = Redactor(Store(persist=False))
        self.line_max = work.setting("find_line_max")

    def clean(self, text):
        return self.redactor.redact(text)[0]

    def text(self, value, size=None):
        """`value` screened as a whole, then cut to `size` characters when given. Fail closed: a text longer than the line cap
        (`find_line_max`) is too costly to screen in full, and screening part of it can miss a secret whose name lies outside the
        part, so none of it is shown."""
        value = str(value)
        if len(value) > self.line_max:
            return f"[title of {len(value)} chars not shown]"
        return self.clean(value)[:size] if size else self.clean(value)

    def token(self, value, what="value"):
        """An identifier shown whole or not at all. Ids and paths are lower-cased slugs of titles, which hides the shape of a
        credential from a screen that matches case, so the upper-cased form of each segment is screened too; any finding in
        either form (or an over-long value) withholds the whole value, naming only what was withheld. A path is a labelled,
        root-relative one (`labelled`) and is screened whole."""
        value = str(value)
        if len(value) > self.line_max or self.clean(value) != value:
            return f"[{what} withheld]"
        for part in re.split(r"[/:]", value):
            if part and self.clean(part.upper()) != part.upper():
                return f"[{what} withheld]"
        return value

    def snippet(self, line, path, number, terms=()):
        """A window of at most SNIPPET characters on the first term in `line`, wherever in the line the term is. The complete
        line is redacted before it is windowed, so the screen sees every secret's whole context (an `API_KEY=` far from the
        term included). Fail closed: a line longer than `find_line_max` is too costly to screen in full (the screen is
        superlinear on a long unbroken run), and none of its text is shown, only where to open it."""
        if len(line) > self.line_max:
            where = self.token(f"{path}:{number}", "path")
            text = f"[line of {len(line)} chars not shown; open {where}]"
            return text if len(text) <= SNIPPET else f"[line of {len(line)} chars not shown; open it at path:line]"
        flat = re.sub(r"\s+", " ", self.clean(line)).strip()
        if len(flat) <= SNIPPET:
            return flat
        term = re.compile(re.escape(re.sub(r"\s+", " ", terms[0])), re.I) if terms else None
        hit = term.search(flat) if term else None
        start = max(0, min(hit.start() - 40, len(flat) - SNIPPET)) if hit else 0  # a term redacted away: the line's start
        return flat[start:start + SNIPPET]

    def row(self, row):
        """`row` with every display field screened: title as text, every other string (and each entry of a list) as a token.
        The snippet is made by `snippet` alone, which screens the whole line before it cuts a window, and may carry the path of
        a line it withheld, so it is not screened a second time. Idempotent for rows built from screened parts."""
        out = {}
        for key, value in row.items():
            if key == "title":
                out[key] = self.text(value)
            elif key in {"kind", "snippet"}:
                out[key] = value
            elif isinstance(value, str):
                out[key] = self.token(value, key)
            elif isinstance(value, (list, tuple)):
                out[key] = [self.token(v, key) if isinstance(v, str) else v for v in value]
            else:
                out[key] = value
        return out


class Query:
    def __init__(self, terms=(), kinds=None, ticket=None, tag=None, status=None, workspace=None, since=None, everything=False, limit=20):
        self.terms = [t.lower() for t in terms if t.strip()]
        self.kinds = set(kinds or KINDS)
        self.ticket = (ticket or "").strip()
        self.tag = (tag or "").strip().lower()
        self.status = (status or "").strip().lower()
        self.workspace = (workspace or "").strip()
        if since and not DATE_RE.match(since):
            raise WorkError("--since takes a date, YYYY-MM-DD")
        self.since = since or ""
        self.everything = everything
        self.limit = limit
        self.skipped = []  # store documents that could not be processed (see work.iter_docs)
        self.screen = Screen()

    def matches_text(self, text):
        low = text.lower()
        return all(t in low for t in self.terms)

    def passes_since(self, updated):
        return not self.since or (updated or "")[:10] >= self.since

    def title(self, text, size=None):
        return self.screen.text(text, size)

    def snippet(self, line, path, number):
        return self.screen.snippet(line, path, number, self.terms)


def _pick_line(lines, skip, query):
    """(1-based line number, text) of the first line that has the first term, else the first useful line; skipped lines never."""
    front = 0
    if lines and lines[0].strip() == "---":  # frontmatter lines can be matched, but are not the default snippet
        front = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), -1) + 1
    first = heading = None
    for i, line in enumerate(lines):
        if i in skip or not line.strip():
            continue
        if i >= front and line.strip() != "---":
            if line.startswith("#"):  # a heading is the default snippet only when no prose line exists
                heading = heading or (i + 1, line)
            elif first is None:
                first = (i + 1, line)
        if query.terms and query.terms[0] in line.lower():
            return i + 1, line
    return first or heading or (1, "")


def _row(kind, ident, title, updated, path, line, snippet, status="", **extra):
    return {"kind": kind, "id": ident, "title": title, "updated": updated, "path": str(path), "line": line,
            "snippet": snippet or title[:SNIPPET], "status": status, **extra}  # a file with no body still shows its title


# ------------------------------------------------------------------ the store

def _decision_ranges(lines):
    """Line index ranges of the `## Open decisions` section, and of each entry in it as (id, start, end)."""
    start = end = None
    for i, line in enumerate(lines):
        match = re.match(r"^## (.+?)\s*$", line)
        if not match:
            continue
        if match.group(1).lower() == "open decisions":
            start = i
        elif start is not None and end is None:
            end = i
    if start is None:
        return None, []
    end = len(lines) if end is None else end
    entries, cur = [], None
    for i in range(start + 1, end):
        head = work.HEADING_RE.match(lines[i])
        if head:
            if cur:
                entries.append((cur[0], cur[1], i))
            cur = (head.group(1), i)
    if cur:
        entries.append((cur[0], cur[1], end))
    return range(start, end), entries


def _meta_ok(query, meta):
    if query.ticket and query.ticket not in meta.get("tickets", []):
        return False
    return not query.tag or query.tag in [t.lower() for t in meta.get("tags", [])]


def _workspace_rows(query, doc):
    """Every row one workspace gives, computed whole before any is returned: a document that fails gives none of its rows."""
    text = doc.path.read_text(encoding="utf-8", errors="replace")
    lines = text.split("\n")
    section, entries = _decision_ranges(lines)
    skip = set(section or ())
    status = doc.meta.get("status", "")
    updated = work.updated_iso(doc)
    shown = store_path(doc.path)
    rows = []
    if query.workspace and doc.id != query.workspace:
        return rows
    if "workspace" in query.kinds and (not query.status or status == query.status) and query.passes_since(updated):
        body = "\n".join(l for i, l in enumerate(lines) if i not in skip)
        if _meta_ok(query, doc.meta) and query.matches_text(body):
            number, line = _pick_line(lines, skip, query)
            rows.append(_row("workspace", doc.id, query.title(doc.meta.get("title", doc.id)), updated, shown, number,
                             query.snippet(line, shown, number), status, tickets=doc.meta.get("tickets", []), archived=doc.archived))
    if "decision" in query.kinds and _meta_ok(query, doc.meta) and query.passes_since(updated):
        for ident, begin, finish in entries:
            chunk = lines[begin:finish]
            entry_status = (work.HEADING_RE.match(chunk[0]).group(3))
            if query.status and entry_status != query.status or not query.matches_text("\n".join(chunk)):
                continue
            number, line = _pick_line(chunk, set(), query)
            question = next((l.split(":", 1)[1].strip() for l in chunk if l.startswith("Question:")), "")
            rows.append(_row("decision", ident, query.title(question, 80), updated, shown, begin + number,
                             query.snippet(line, shown, begin + number),
                             entry_status, workspace=doc.id))
    return rows


def _note_rows(query, doc):
    status = doc.meta.get("status", "")
    updated = work.updated_iso(doc)
    if (query.status and status != query.status) or not query.passes_since(updated):
        return []
    if query.workspace and query.workspace not in doc.meta.get("workspaces", []):
        return []
    text = doc.path.read_text(encoding="utf-8", errors="replace")
    if not (_meta_ok(query, doc.meta) and query.matches_text(text)):
        return []
    number, line = _pick_line(text.split("\n"), set(), query)
    extra = {"promoted_to": promoted_label(doc.meta["promoted_to"])} if doc.meta.get("promoted_to") else {}
    shown = store_path(doc.path)
    return [_row("note", doc.id, query.title(doc.meta.get("title", doc.id)), updated, shown, number,
                 query.snippet(line, shown, number),
                 status, tickets=doc.meta.get("tickets", []), archived=doc.archived, **extra)]


def _store_rows(query):
    """Rows from the store, one document at a time: a document that cannot be processed is skipped and named in `query.skipped`."""
    docs = []
    if query.kinds & {"workspace", "decision"}:
        for archived in ((False, True) if query.everything else (False,)):
            docs += [(doc, _workspace_rows) for doc in work.iter_docs("workspace", archived=archived, skipped=query.skipped)]
    if "note" in query.kinds:
        for archived in ((False, True) if query.everything else (False,)):
            docs += [(doc, _note_rows) for doc in work.iter_docs("note", archived=archived, skipped=query.skipped)]
    for doc, rows_of in docs:
        try:
            rows = rows_of(query, doc)
        except Exception:  # noqa: BLE001 - per-document isolation
            work.note_skipped(query.skipped, doc.path)
            continue
        yield from rows


# ---------------------------------------------------------- tasks and sessions

def project_roots(query):
    """The places whose `.tmux-pi` is searched: the current TP_WORKDIR and every workspace's repos entry."""
    roots = []
    current = os.getenv("TP_WORKDIR")
    if current:
        try:
            roots.append(Path(current).expanduser().resolve())
        except (OSError, ValueError, RuntimeError):
            pass
    for archived in ((False, True) if query.everything else (False,)):
        for doc in work.iter_docs("workspace", archived=archived, skipped=query.skipped):
            try:
                repos = doc.meta.get("repos", [])
                if not isinstance(repos, list):
                    raise ValueError("repos is not a list")
                roots += [Path(r).expanduser().resolve() for r in repos if r]
            except Exception:  # noqa: BLE001 - per-document isolation: one malformed repos entry drops that workspace's roots only
                work.note_skipped(query.skipped, doc.path)
    seen, out = set(), []
    for key in roots:
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def session_dirs(roots):
    """Every session state directory under the roots: `<root>/.tmux-pi/sessions/*`, plus a `.tmux-pi` that holds tasks itself."""
    seen, out = set(), []
    for root in roots:
        base = root / ".tmux-pi"
        candidates = [base] if (base / "tasks").is_dir() or (base / "archive").is_dir() else []
        sessions = base / "sessions"
        try:
            candidates += sorted(p for p in sessions.iterdir() if p.is_dir() and not p.is_symlink()) if sessions.is_dir() else []
        except OSError:
            continue
        for path in candidates:
            if path not in seen:
                seen.add(path)
                out.append(path)
    return out


def _omitted_specs(task):
    """Context specs tp-spawn left out of the packet as sensitive; no line that names one is searched."""
    text = _read(task / "packet.md") or ""
    return {m.group(1) for m in map(OMITTED.match, text.splitlines()) if m}


def _meta_fragments(raw, meta):
    """[(1-based line of meta.json, the `"key": value` text found on it)] for the META_FIELDS only, in file order."""
    found = []
    for key in META_FIELDS:
        if not isinstance(meta.get(key), (str, int)) or meta.get(key) == "":
            continue
        for hit in re.finditer(r'"%s"\s*:\s*("(?:[^"\\]|\\.)*"|-?\d+)' % key, raw):
            try:
                same = json.loads(hit.group(1)) == meta[key]
            except ValueError:
                continue
            if same:
                found.append((hit.start(), raw.count("\n", 0, hit.start()) + 1, hit.group(0)))
                break
    return [(row, text) for _, row, text in sorted(found)]


def _task_text(task):
    omitted = _omitted_specs(task)
    parts = []
    raw = _read(task / "meta.json") or ""
    try:
        meta = json.loads(raw or "{}")
    except ValueError:
        meta = {}
    meta = meta if isinstance(meta, dict) else {}
    fragments = _meta_fragments(raw, meta)
    parts.append(("meta.json", "\n".join(text for _, text in fragments)))
    for name, limit in TASK_HEAD.items():
        text = _read(task / name, limit)
        if text is not None:
            if omitted or "was omitted from the packet" in text:  # blank the line, so the lines after it keep their numbers
                text = "\n".join("" if "was omitted from the packet" in l or any(spec in l for spec in omitted) else l
                                 for l in text.split("\n"))
            parts.append((name, text))
    return meta, parts, fragments


def _task_rows(query):
    wanted_ids = _linked_names(query.workspace) if query.workspace else None
    if wanted_ids is None and query.workspace:
        return
    for session in session_dirs(project_roots(query)):
        for folder, archived in ((session / "tasks", False), (session / "archive", True)):
            try:
                tasks = sorted(p for p in folder.iterdir() if p.is_dir() and not p.is_symlink()) if folder.is_dir() else []
            except OSError:
                continue
            for task in tasks:
                if not (task / "meta.json").is_file():
                    continue
                status = "archived" if archived else "active"
                if query.status and query.status != status:
                    continue
                if wanted_ids is not None and task.name not in wanted_ids:
                    continue
                meta, parts, fragments = _task_text(task)
                state = _read(task / "state.json", 4096) or "{}"
                try:
                    updated = (json.loads(state) or {}).get("updated") or meta.get("created") or _stamp(task / "meta.json")
                except (ValueError, AttributeError):
                    updated = meta.get("created") or _stamp(task / "meta.json")
                updated = str(updated)
                if not query.passes_since(updated):
                    continue
                text = "\n".join(t for _, t in parts)
                if query.tag or (query.ticket and query.ticket not in TICKET_ROW.findall(text)) or not query.matches_text(text):
                    continue
                name, chunk = next(((n, t) for n, t in parts if not query.terms or query.terms[0] in t.lower()), parts[0])
                if name == "meta.json" and fragments:  # the line the fragment really has in the file, and the text found there
                    number, line = next(((n, t) for n, t in fragments if not query.terms or query.terms[0] in t.lower()), fragments[0])
                else:
                    number, line = _pick_line(chunk.split("\n"), set(), query)
                request = next((t for n, t in parts if n == "request.md"), "")
                title = next((l.strip() for l in request.split("\n") if l.strip()), str(meta.get("id") or task.name))
                shown = f"task:{session.name}/{(task / name).relative_to(session).as_posix()}"
                yield _row("task", str(meta.get("id") or task.name), query.title(title, 80), updated, shown, number,
                           query.snippet(line, shown, number), status, session=session.name)


def _session_rows(query):
    if query.tag:
        return
    named = _linked_names(query.workspace) if query.workspace else None
    if query.workspace and named is None:
        return
    for session in session_dirs(project_roots(query)):
        text = _read(session / "session-objective.md", 4096)
        if text is None:
            continue
        updated = _stamp(session / "session-objective.md")
        if (query.status and query.status != "active") or not query.passes_since(updated):
            continue
        if named is not None and session.name not in named:
            continue
        if (query.ticket and query.ticket not in TICKET_ROW.findall(text)) or not query.matches_text(text):
            continue
        number, line = _pick_line(text.split("\n"), set(), query)
        shown = f"session:{session.name}/session-objective.md"
        yield _row("session", session.name, query.title(text.strip().split("\n")[0], 80), updated, shown, number,
                   query.snippet(line, shown, number), "active")


def _linked_names(ident):
    """Every word of a workspace's text (its Links and Sessions name task and run ids), or None when there is no such workspace."""
    try:
        return set(re.findall(r"[A-Za-z0-9][A-Za-z0-9._-]*", work.load("workspace", ident).body))
    except WorkError:
        return None


# ---------------------------------------------------------------- shared clone

def shared_root():
    """The configured shared clone when searching it is switched on, else None."""
    cfg = work.setting("shared")
    path = str(cfg.get("path") or "")
    if not cfg.get("search") or not path:
        return None
    root = Path(os.path.expanduser(path))
    return root if root.is_dir() else None


def _shared_rows(query, incomplete):
    root = shared_root()
    if root is None:
        return
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not Path(folder, d).is_symlink())
        for name in sorted(files):
            path = Path(folder, name)
            if not name.endswith(".md"):
                continue
            text = _read(path, SHARED_MAX)
            if text is None:
                continue
            shown = labelled(path, "shared", root)
            if path.stat().st_size > SHARED_MAX:
                incomplete.append(f"shared file {query.screen.token(shown, 'path')} is over {SHARED_MAX // (1024 * 1024)} MiB and was searched only in its first "
                                  f"{SHARED_MAX // (1024 * 1024)} MiB; open it directly or search it with grep")
            meta, _ = work.parse(text)
            status = str(meta.get("status", ""))
            updated = str(meta.get("last_reviewed") or _stamp(path))
            if (query.status and status != query.status) or not query.passes_since(updated):
                continue
            tags = [t.lower() for t in meta.get("tags", [])] if isinstance(meta.get("tags"), list) else []
            if (query.ticket and query.ticket not in TICKET_ROW.findall(text)) or (query.tag and query.tag not in tags):
                continue
            if query.workspace or not query.matches_text(text):
                continue
            number, line = _pick_line(text.split("\n"), set(), query)
            ident = str(meta.get("context_id") or path.name.removesuffix(".md"))
            title = next((l[2:].strip() for l in text.split("\n") if l.startswith("# ")), ident)
            yield _row("shared", ident, query.title(title, 80), updated, shown, number, query.snippet(line, shown, number), status)


# ----------------------------------------------------------------------- entry

def search(terms=(), kind=None, ticket=None, tag=None, status=None, workspace=None, since=None, everything=False, limit=20,
           incomplete=None):
    """(rows, total): the best `limit` hits, newest first, and how many matched. Anything the search could not cover in full is
    appended to the `incomplete` list (when given) as a sentence that says what was cut and how to narrow or cover it."""
    incomplete = [] if incomplete is None else incomplete
    if kind and kind not in KINDS:
        raise WorkError("unknown kind; one of " + ", ".join(KINDS))
    query = Query(terms, [kind] if kind else None, ticket, tag, status, workspace, since, everything, limit)
    rows = []
    if query.kinds & {"workspace", "note", "decision"}:
        rows += list(_store_rows(query))
    if "task" in query.kinds:
        rows += list(_task_rows(query))
    if "session" in query.kinds:
        rows += list(_session_rows(query))
    if "shared" in query.kinds:
        rows += list(_shared_rows(query, incomplete))
    rows.sort(key=lambda r: r["updated"] or "", reverse=True)
    if work.skipped_count(query.skipped):
        incomplete.append(f"{work.skipped_line(query.skipped)} ({', '.join(work.skipped_files(query.skipped))} skipped)")
    return [query.screen.row(r) for r in rows[:max(1, limit)]], len(rows)


def _mark(row):
    marks = []
    if row["kind"] in {"note", "workspace"} and (row.get("archived") or row["status"] in work.ARCHIVED_STATUS):
        marks.append(row["status"] if row["status"] in work.ARCHIVED_STATUS else "archived")
    if row.get("promoted_to"):
        marks.append("promoted")
    return " [" + ", ".join(marks) + "]" if marks else ""


PATHS_LEGEND = ("paths: store: is the work store (TP_WORK_DIR; or `tp-agent work show|note show <id>`), task: and session: are "
                "under <project>/.tmux-pi/sessions/, shared: is the configured shared clone")


def format_rows(rows, total, incomplete=()):
    """`kind  id  title  updated  path:line  snippet` per hit; superseded, archived and promoted notes are marked. A search that
    could not cover everything says so on a final line per reason, also when it found nothing."""
    out = []
    for r in rows:
        title = re.sub(r"\s+", " ", r["title"]) + _mark(r)
        out.append(f"{r['kind']}  {r['id']}  {title}  {r['updated']}  {r['path']}:{r['line']}  {r['snippet']}")
    if total > len(rows):
        out.append(f"+{total - len(rows)} more; narrow the search or raise --limit")
    if rows:
        out.append(PATHS_LEGEND)
    out = out or ["no matches"]
    out += [f"incomplete: {reason}" for reason in incomplete]
    return "\n".join(out)


def near_duplicates(title, tickets=(), tags=(), skipped=None):
    """Live notes a new note would repeat: the same title slug, or a shared ticket and a shared tag. For `work note add`.
    A note that cannot be read or checked is skipped and its file appended to `skipped` (when given), so the caller can say the check was incomplete."""
    slug = work.slugify(title)
    screen = Screen()
    found = []
    for doc in work.iter_docs("note", skipped=skipped):
        try:  # per-document isolation: a malformed note is never a duplicate, and never stops the check
            if doc.id == slug or work.slugify(str(doc.meta.get("title", ""))) == slug:
                why = "same title"
            elif set(tickets) & set(doc.meta.get("tickets", [])) and set(tags) & set(doc.meta.get("tags", [])):
                why = "same ticket and tag"
            else:
                continue
            shown = {"id": screen.token(doc.id, "id"), "path": screen.token(store_path(doc.path), "path")}
            found.append({**shown, "title": screen.text(doc.meta.get("title", doc.id), 80), "why": why,
                          "withheld": any(v.startswith("[") and v.endswith(" withheld]") for v in shown.values())})
        except Exception:  # noqa: BLE001 - per-document isolation: counted by the caller's `skipped`
            work.note_skipped(skipped, doc.path)
    return found
