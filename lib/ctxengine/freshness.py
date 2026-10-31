"""Freshness flags for notes (design §6), computed on every call and never stored.

  stale_by_version   a note's `versions` range no longer matches what the lockfile pins (the note is flagged, never dropped)
  version_unchecked  packages whose pin could not be read (no lockfile reader, or the lockfile does not name the package)
  stale_by_path      a scope glob matches no file in the tree, or an evidence `file:line` names a missing file, a line past its
                     end, or a line whose text is no longer the text that was cited (a hash of the line is taken when a
                     candidate is written, and compared on every read; a note without one is checked for file and length only)
  age_days           present when `last_reviewed` is more than AGE_LIMIT_DAYS old

Lockfile versions come from a `pin(name) -> version | None` callable the caller supplies (the CLI wires L5's
`localdocs.lock` when it is merged); this module has no lockfile reader of its own. Nothing here edits a note.
"""
import datetime as _dt
import hashlib
import os
import re
import subprocess

from . import redact

AGE_LIMIT_DAYS = 180
TREE_LIMIT = 200_000
EVIDENCE_READ_MAX = 4 * 1024 * 1024
PRUNE = {".git", "node_modules", ".tmux-pi", "__pycache__", ".venv"}
EVIDENCE_LINE = re.compile(r"^(?P<path>[^:\s]+):(?P<line>[1-9]\d*)$")
_VERSION = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")
_COMPARATOR = re.compile(r"^(>=|<=|>|<|=|\^|~)?v?(\d+|[xX*])(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?$")
_ANY = ("x", "X", "*")
HASH_RE = re.compile(r"^[0-9a-f]{16}$")


# ------------------------------------------------------------------------------------------------------------ versions

def parse_version(text):
    """(major, minor, patch) of a version text such as `0.45.1`, `v5` or `1.2.3-beta.1` (the pre-release and build parts are
    ignored), or ValueError."""
    match = _VERSION.match(str(text).strip())
    if not match:
        raise ValueError("not a version")
    return tuple(int(part or 0) for part in match.groups())


def _bounds(op, parts):
    """The comparators (op, version) one range token stands for."""
    numbers = [None if part in _ANY or part is None else int(part) for part in parts]
    fixed = [n for n in numbers if n is not None]
    if None in numbers[:len(fixed)]:
        raise ValueError("a wildcard may only come last")
    padded = tuple((fixed + [0, 0, 0])[:3])
    if not fixed:
        return []  # `*` / `x`: any version
    low = padded
    if op in ("<", ">=", ">", "<=") and len(fixed) < 3:
        # `>=0.45` is >=0.45.0, `<0.46` is <0.46.0, `>1.2` means above every 1.2.x, `<=1.2` means up to every 1.2.x
        if op == ">":
            return [(">=", _bump(fixed, len(fixed) - 1))]
        if op == "<=":
            return [("<", _bump(fixed, len(fixed) - 1))]
        return [(op, low)]
    if op in ("<", ">=", ">", "<="):
        return [(op, low)]
    if op == "^":
        if fixed[0] > 0 or len(fixed) == 1:
            index = 0
        elif len(fixed) == 2 or fixed[1] > 0:
            index = 1
        else:
            index = 2
        return [(">=", low), ("<", _bump(fixed, index))]
    if op == "~":
        return [(">=", low), ("<", _bump(fixed, 1 if len(fixed) > 1 else 0))]
    # exact, or a partial version (`1.2`, `1.2.x`) which stands for every version below it
    if len(fixed) == 3:
        return [("=", low)]
    return [(">=", low), ("<", _bump(fixed, len(fixed) - 1))]


def _bump(fixed, index):
    head = list(fixed[:index + 1])
    head[index] += 1
    return tuple((head + [0, 0, 0])[:3])


def parse_range(text):
    """A range as a list of alternatives, each a list of (op, version) comparators that must all hold. Supports `>=1.2 <2`,
    `>=1.2, <2`, `^1.2.3`, `~1.2`, `1.2.x`, `1.2.3`, `*` and `a || b`. ValueError (a rule, not the text) when it cannot be read."""
    if not isinstance(text, str) or not text.strip() or len(text) > 100:
        raise ValueError("a range is 1 to 100 characters")
    alternatives = []
    for part in text.split("||"):
        tokens = [t for t in re.split(r"[\s,]+", part.strip()) if t]
        if not tokens:
            raise ValueError("an empty alternative in a range")
        comparators = []
        for token in tokens:
            match = _COMPARATOR.match(token)
            if not match:
                raise ValueError("a range is made of >=, <=, >, <, ^, ~ or exact versions (no spaces after an operator)")
            op, *parts = match.groups()
            comparators += _bounds(op or "=", parts)
        alternatives.append(comparators)
    return alternatives


def satisfies(version, spec):
    """Whether `version` (text) is in the range `spec` (text). ValueError when either cannot be read."""
    have = parse_version(version)
    for alternative in parse_range(spec):
        if all(_holds(have, op, want) for op, want in alternative):
            return True
    return False


def _holds(have, op, want):
    return {"=": have == want, ">=": have >= want, "<=": have <= want, ">": have > want, "<": have < want}[op]


def version_flags(versions, pin):
    """(stale, unchecked): the packages of a note's `versions` whose pinned version is outside the range, and the ones whose
    pin is unknown or whose range cannot be read. `pin` maps a package name to a version text or None; with no `pin` nothing
    can be checked."""
    stale, unchecked = [], []
    for package in sorted(versions):
        found = None
        if pin is not None:
            try:
                found = pin(package)
            except Exception:  # a broken lockfile reader never breaks an answer: the package is simply unchecked
                found = None
            found = getattr(found, "version", found)
        if not found:
            unchecked.append(package)
            continue
        try:
            if not satisfies(found, versions[package]):
                stale.append(package)
        except ValueError:
            unchecked.append(package)
    return stale, unchecked


# ----------------------------------------------------------------------------------------------------------- the tree

class Tree:
    """The files of a project tree as relative posix paths, listed once: from git when the project is a repository (tracked and
    untracked-but-not-ignored files, minus deleted ones), otherwise by walking it without entering PRUNE directories."""

    def __init__(self, project):
        self.project = str(project)
        self._files = None

    @property
    def files(self):
        if self._files is None:
            self._files = self._git() or self._walk()
        return self._files

    def _git(self):
        def run(*args):
            done = subprocess.run(["git", "-C", self.project, *args, "-z"], capture_output=True, timeout=20, check=False,
                                  env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
            if done.returncode != 0:
                raise OSError("git failed")
            return {p for p in done.stdout.decode("utf-8", "replace").split("\0") if p}
        try:
            listed = run("ls-files", "--cached", "--others", "--exclude-standard") - run("ls-files", "--deleted")
        except (OSError, subprocess.SubprocessError):
            return None
        return sorted(p for p in listed if len(p) < 1000)[:TREE_LIMIT]

    def _walk(self):
        found = []
        for folder, dirs, names in os.walk(self.project, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in PRUNE)
            base = os.path.relpath(folder, self.project)
            found += [n if base == "." else f"{base}/{n}".replace(os.sep, "/") for n in sorted(names)]
            if len(found) >= TREE_LIMIT:
                break
        return found[:TREE_LIMIT]


# -------------------------------------------------------------------------------------------------------------- paths

def _evidence_target(project, item):
    """(project-relative path, line number, full path) for a `file:line` evidence entry that names a readable place, else None:
    not that shape, a review/doc/work/http reference, or a refused (sensitive or outside) path, which is never read."""
    match = EVIDENCE_LINE.match(item)
    if not match or item.startswith(("review:", "doc:", "work:", "http")):
        return None
    try:
        rel = redact.check_path(project, match.group("path"))
    except redact.Refused:
        return None
    return rel, int(match.group("line")), os.path.join(str(project), rel)


def line_hash(text):
    """A short hash of one source line, ignoring the whitespace around it (a re-indented line is the same line)."""
    return hashlib.sha256(text.strip().encode("utf-8", "replace")).hexdigest()[:16]


def hash_evidence(project, evidence):
    """{evidence entry: line_hash} for the `file:line` entries that name a line that exists now, in a path the engine may read."""
    out = {}
    for item in evidence:
        target = _evidence_target(project, item)
        if target and not os.path.islink(target[2]):
            text = _line_text(target[2], target[1])
            if text is not None:
                out[item] = line_hash(text)
    return out


def path_reasons(note, project, tree, glob_matches):
    """Why a note is stale by path, as short codes: `scope_no_match:<glob>` for each scope glob matching no file,
    `evidence_missing:<path>`, `evidence_moved:<path>:<line>` (the file no longer has that line) and
    `evidence_changed:<path>:<line>` (the line is there but is not the text that was cited). `glob_matches(glob, path)` is
    search.glob_matches. Evidence for a refused (sensitive or outside) path is not read and not flagged."""
    reasons = []
    files = tree.files
    for glob in note.scope:
        if not any(glob_matches(glob, path) for path in files):
            reasons.append(f"scope_no_match:{glob}")
    for item in note.evidence:
        target = _evidence_target(project, item)
        if not target:
            continue
        rel, line, full = target
        if os.path.islink(full) or not os.path.isfile(full):
            reasons.append(f"evidence_missing:{rel}")
            continue
        text = _line_text(full, line)
        if text is None:
            reasons.append(f"evidence_moved:{rel}:{line}")
        elif item in note.evidence_hashes and line_hash(text) != note.evidence_hashes[item]:
            reasons.append(f"evidence_changed:{rel}:{line}")
    return reasons


def _line_text(path, number):
    """The text of line `number` (1-based) of the file, or None when the file cannot be read or has fewer lines."""
    try:
        with open(path, "rb") as handle:
            chunk = handle.read(EVIDENCE_READ_MAX)
    except OSError:
        return None
    lines = chunk.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()  # a final newline ends the last line; it does not start another
    if number > len(lines):
        return None
    return lines[number - 1].decode("utf-8", "replace").rstrip("\r")


# --------------------------------------------------------------------------------------------------------------- age

def age_days(last_reviewed, today=None):
    """Whole days since `last_reviewed` (YYYY-MM-DD), or None when it is not set or not a date."""
    try:
        then = _dt.date.fromisoformat(last_reviewed)
    except (TypeError, ValueError):
        return None
    now = _dt.date.fromisoformat(today) if today else _dt.date.today()
    return (now - then).days


def assess(note, project, tree, pin, glob_matches, today=None):
    """The freshness of one note as a dict: `flags` (a sorted list of stale_by_version / stale_by_path), plus
    `version_unchecked`, `path_reasons` and `age_days` when they apply. Only keys with something to say are present."""
    out = {"flags": []}
    stale, unchecked = version_flags(note.versions, pin) if note.versions else ([], [])
    if stale:
        out["flags"].append("stale_by_version")
        out["stale_versions"] = stale
    if unchecked:
        out["version_unchecked"] = unchecked
    reasons = path_reasons(note, project, tree, glob_matches)
    if reasons:
        out["flags"].append("stale_by_path")
        out["path_reasons"] = reasons
    age = age_days(note.last_reviewed, today)
    if age is not None and age > AGE_LIMIT_DAYS:
        out["age_days"] = age
    return out
