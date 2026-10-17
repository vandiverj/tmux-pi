"""`tp-agent work promote` (Phase B): turn a private note or workspace into a DRAFT `*.context.md` in a working tree.

The one route from the private store to the shared tier. It writes a file into the configured target working tree and shows the
diff; it never adds, commits, pushes, branches or opens a merge request, and it runs no git command at all. The target comes
from `--target` or config (`projects.<repo>.target`, `.promote_dir`; `shared.path`, `shared.promote_dir`), never from a constant.
The text goes through the redaction screen before anything is written: one finding refuses it, naming the kinds and not values.
Every message names a document by its root-relative location (`store:notes/<id>.md`, `target:<path in the tree>`), never by a
configured directory, and the caller prints it through the output boundary (`work_find.guard`).
"""
import difflib
import os
from pathlib import Path
import re
import sys
import tempfile

import work
from work import WorkError
import work_find

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from redact import Redactor  # noqa: E402
from redaction_store import Store  # noqa: E402

CONFIDENCE = ("low", "medium", "high")
TICKET_ID = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")
PRIVATE = re.compile(r"<!--\s*(?:private|sensitive)\s*-->", re.I)
PRIVATE_TAGS = {"private", "sensitive"}
BELIEF_HEADING = re.compile(r"^###\s+(Known|Believed|Unknown)\s*$", re.I | re.M)
# in-flight state: never part of a draft promoted from a workspace
WORKSPACE_PRIVATE = {"open decisions", "sessions", "next actions", "blockers / questions", "links"}


def _destination_roots(doc):
    """Repos a document lives in: its own, and for a note those of the workspaces it is linked to."""
    repos = list(doc.meta.get("repos", []))
    if doc.kind == "note":
        for ident in doc.meta.get("workspaces", []):
            try:
                repos += work.load("workspace", ident).meta.get("repos", [])
            except WorkError:
                continue
    return [str(Path(r).expanduser().resolve()) for r in repos if r]


def resolve_target(doc, target=None):
    """(tree, promote_dir): the working tree to write into and the sub-directory for a context file with no ticket."""
    projects = work.setting("projects")
    shared = work.setting("shared")
    entry = {}
    for repo in _destination_roots(doc):
        entry = projects.get(repo) or {}
        if entry:
            break
    sub = str(entry.get("promote_dir") or shared.get("promote_dir") or "")
    tree = target or entry.get("target") or shared.get("path") or ""
    if not target and not tree and os.path.isabs(sub):
        tree, sub = sub, ""
    if not tree:
        raise WorkError("no promote target configured: pass --target PATH (a working tree), or set projects.<repo-path>.target "
                        "(and promote_dir) or shared.path in config/work.local.json")
    root = Path(os.path.expanduser(str(tree)))
    if not root.is_dir():
        raise WorkError("the promote target (--target, or the configured target) is not a directory")
    if os.path.isabs(sub) or ".." in Path(sub).parts:
        raise WorkError("the configured promote_dir must be a relative path inside the target tree")
    return root.resolve(), sub


def _body(doc):
    if doc.kind == "workspace":
        pre, sections = work.split_sections(doc.body)
        kept = [(t, x) for t, x in sections if t.lower() not in WORKSPACE_PRIVATE]
        return work.join_sections("", kept).strip("\n")
    return doc.body.strip("\n")


def build_text(doc, confidence="low", today=None):
    body = _body(doc)
    repos = sorted({Path(r).name for r in _destination_roots(doc)})
    head = ["---", f"context_id: {doc.id.replace('-', '.')}", "status: draft", f"confidence: {confidence}",
            f"last_reviewed: {today or work.now()[:10]}", "repos: [" + ", ".join(repos) + "]", "---", "",
            f"# Context: {doc.meta.get('title', doc.id)}", "", "## Current Understanding", ""]
    if not BELIEF_HEADING.search(body):  # the note does not use Known/Believed/Unknown: it is what we believe, not what is verified
        head += ["### Believed", ""]
    return "\n".join(head) + (body + "\n" if body else "")


def location(doc):
    """Where a refused document is, for a message: its store-relative path, withheld whole if the screen finds anything in it."""
    return work_find.Screen().token(work_find.store_path(doc.path), "path")


def check_document(doc):
    """Refuse a document tagged or fenced private/sensitive. The message names the kind of marker and the location only."""
    tags = {str(t).lower() for t in doc.meta.get("tags", [])}
    if tags & PRIVATE_TAGS:
        raise WorkError(f"refused: the {doc.kind} at {location(doc)} is tagged {', '.join(sorted(tags & PRIVATE_TAGS))}; nothing "
                        "was written. Remove the tag to promote it on purpose")
    fence = PRIVATE.search(doc.body)
    if fence:
        line = doc.body[:fence.start()].count("\n") + 1
        kind = "sensitive" if "sensitive" in fence.group(0).lower() else "private"
        raise WorkError(f"refused: the {doc.kind} at {location(doc)} has a {kind} section (marker at body line {line}); nothing "
                        "was written. Remove the section or the marker and promote again")


def screen(doc, text):
    """Refuse a private or sensitive document, and any text the redaction screen finds something in. Kinds only, never values.
    `text` is what will be written: screen it last, after every merge."""
    check_document(doc)
    findings = Redactor(Store(persist=False)).redact(text)[1]
    if findings:
        kinds = sorted({f["kind"] for f in findings})
        raise WorkError(f"refused: the draft of {location(doc)} contains {len(findings)} secret-like value(s) ({', '.join(kinds)}); "
                        "nothing was written. Edit the private note, then promote again")


def _content(text, keep_confidence=False):
    """The file without its review-state frontmatter lines, to tell a changed draft from a re-run on the same content.
    A person reviews `status`, `last_reviewed` and (unless `--confidence` was asked for) `confidence` in the file; a re-run must not
    undo that, so those lines are not compared. Only frontmatter lines are dropped: a body line that looks like one still counts."""
    skip = ("status:", "last_reviewed:") if keep_confidence else ("status:", "confidence:", "last_reviewed:")
    lines = text.split("\n")
    end = lines.index("---", 1) if lines[:1] == ["---"] and "---" in lines[1:] else 0
    return "\n".join(l for i, l in enumerate(lines) if not (i < end and l.startswith(skip)))


def _diff(old, new, rel):
    a = old.splitlines(keepends=True) if old is not None else []
    out = [f"diff --git a/{rel} b/{rel}\n"]
    if old is None:
        out.append("new file mode 100644\n")
    out += difflib.unified_diff(a, new.splitlines(keepends=True), "/dev/null" if old is None else f"a/{rel}", f"b/{rel}")
    return "".join(x if x.endswith("\n") else x + "\n" for x in out)


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(name, 0o644)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def promote(ident, kind_as="context", ticket=None, confidence=None, target=None, update=False):
    """Write the draft and return the text to print: the diff and the path. Caller holds the store lock.
    `confidence=None` means none was asked for: a new file gets `low`, and an update leaves a reviewed value alone."""
    if confidence is not None and confidence not in CONFIDENCE:
        raise WorkError("--confidence is one of " + ", ".join(CONFIDENCE))
    asked_confidence = confidence is not None
    confidence = confidence or "low"
    doc = None
    for kind in ("note", "workspace"):
        try:
            doc = work.load(kind, ident)
            break
        except WorkError:
            continue
    if doc is None:
        raise WorkError(f"unknown note or workspace: {ident}")
    if doc.archived:
        raise WorkError(f"the {doc.kind} at {location(doc)} is archived; promote the note that replaced it")
    if kind_as == "task" and not ticket:
        ticket = next(iter(doc.meta.get("tickets", [])), None)
        if not ticket:
            raise WorkError("--as task needs a ticket: pass --ticket T")
    if ticket and not TICKET_ID.match(ticket):
        raise WorkError(f"invalid ticket {ticket!r}")
    tree, sub = resolve_target(doc, target)
    folder = tree / "tasks" / ticket if ticket else tree / sub
    path = folder / f"{doc.id}.context.md"
    try:
        path.resolve().relative_to(tree)
    except ValueError:
        raise WorkError("refused: the destination escapes the target tree; nothing was written")
    rel = str(path.relative_to(tree))
    where = f"target:{rel}"
    if any(p.is_symlink() for p in (path, *path.parents) if tree in p.parents or p == path):
        raise WorkError(f"refused: unsafe destination {where} (a symlink on the path); nothing was written")
    text = build_text(doc, confidence)  # always status: draft, whatever the file on disk says
    screen(doc, text)
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old is not None and not update:
        raise WorkError(f"{where} already exists; pass --update to replace it (the diff is shown)")
    if old is not None and _content(old, asked_confidence) == _content(text, asked_confidence):  # same content: leave a reviewed file as it is
        if not str(doc.meta.get("promoted_to", "")).startswith(f"{path}@"):  # but the private note still records that it was promoted
            doc.meta["promoted_to"] = f"{path}@{work.now()[:10]}"
            doc.save()
        return f"no change: {where} already holds this draft"
    # the removed lines of the diff come from the file on disk, which a person may have edited: print them redacted
    shown = Redactor(Store(persist=False)).redact(old)[0] if old is not None else None
    diff = _diff(shown, text, rel)
    _write(path, text)
    doc.meta["promoted_to"] = f"{path}@{work.now()[:10]}"
    doc.save()
    return f"{diff}\nwrote {where}\nnothing was added to git: review the diff, then commit it yourself"
