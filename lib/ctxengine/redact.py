"""The engine's screens. No new patterns live here: every rule is an existing one, called.

  screen_note(note, text)   a note headed for disk: lib/work_promote.screen (private tag or fence, and the redaction screen's
                            findings). Refused with the rule name and the count, never the matching text.
  guard(text) / guard_json  everything that leaves the process: lib/work_find.guard, the output boundary of `tp-agent find`
                            (configured roots relabelled, then redacted).
  check_path(project, path) a path argument: inside the project after symlinks resolve, and not a sensitive path
                            (lib/sensitive_read.sensitive_path: .env*, config/local.json, agent/auth.json and the rest).
  Refused                   what these raise. Its message names a rule and never carries the refused content.

Other lanes (CX2, CX3) call `check_path` and `guard`; until this lane merges they use a stub with these signatures.
"""
import os
import re
from pathlib import PurePosixPath

import sensitive_read
import work
import work_find
import work_promote


class Refused(Exception):
    """A request the engine will not serve. The message is safe to show: a rule, a field, a count."""


class _Doc:
    """The part of a work document that work_promote.screen reads, for a note."""
    kind = "note"

    def __init__(self, note):
        self.id = note.id
        self.path = f"{note.id}.md"
        self.meta = {"tags": list(note.tags)}
        self.body = note.body


def screen_note(note, text):
    """Raise Refused unless `text` (the whole file about to be written for `note`) passes the work_promote screen."""
    try:
        work_promote.screen(_Doc(note), text)
    except work.WorkError as exc:
        # work_promote names a kind and a count, never a value; its location is a store path that means nothing for a note.
        found = re.search(r"contains (\d+ secret-like value\(s\) \([^)]*\))", str(exc))
        if found:
            raise Refused(f"refused: the note contains {found.group(1)}; nothing was written") from None
        raise Refused("refused: the note is tagged or fenced private/sensitive; nothing was written") from None


def guard(text):
    return work_find.guard(text)


def guard_json(value):
    return work_find.guard_json(value)


def check_path(project, value):
    """The project-relative posix path `value` names, or Refused. `value` may be relative to the project or absolute inside it.
    Refused: an empty or NUL-bearing value, a path leaving the project (by `..` or by a symlink), a sensitive path."""
    if not isinstance(value, str) or not value.strip() or "\0" in value or len(value) > 1000:
        raise Refused("refused: not a usable path")
    root = os.path.realpath(str(project))
    joined = value if os.path.isabs(value) else os.path.join(root, value)
    resolved = os.path.realpath(joined)  # a missing file is judged by where its directory really is
    if resolved != root and not resolved.startswith(root + os.sep):
        raise Refused("refused: the path is outside the project")
    if sensitive_read.sensitive_path(os.path.normpath(joined)) or sensitive_read.sensitive_path(resolved):
        raise Refused("refused: the path is a sensitive file or directory")
    rel = os.path.relpath(resolved, root)
    if rel == ".":
        raise Refused("refused: name a file, not the project root")
    return PurePosixPath(*rel.split(os.sep)).as_posix()


def work_note(ident):
    """The work-store note `ident` for `knowledge import`: a note tagged `lesson` that is not tagged or fenced private. Refused
    (rule only) otherwise; the note's text is not echoed."""
    try:
        doc = work.load("note", ident)
    except work.WorkError:
        raise Refused("no such work note") from None
    if "lesson" not in {str(t).lower() for t in doc.meta.get("tags", [])}:
        raise Refused("only a work note tagged `lesson` can be offered as a candidate")
    try:
        work_promote.check_document(doc)
    except work.WorkError:
        raise Refused("refused: the work note is tagged or fenced private/sensitive; nothing was written") from None
    return doc
