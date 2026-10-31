"""The only door to a file for the local docs tools.

Reads are allowlisted: the extensions .d.ts, .d.mts, .d.cts and .md, and only under a root the caller names (a
package directory, the index directory, Deno's npm cache). Symlinks are resolved first and the result must still be
under the root, so a link planted in a package or in node_modules cannot lead out. A path with a component that
starts with `.env` is never read, whatever its extension or where the link points.
"""
import os
import stat
import time
from pathlib import Path

ALLOWED_SUFFIXES = (".d.ts", ".d.mts", ".d.cts", ".md")
MAX_FILE = 2_000_000


def now():
    """The UTC time as the tools record it: 2026-10-31T12:00:00Z."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Rejected(Exception):
    """A request the read-only rules refuse. The message is safe to show the model."""


def allowed_suffix(name):
    return name.lower().endswith(ALLOWED_SUFFIXES)


def reject_names(parts):
    for part in parts:
        if part.startswith(".env"):
            raise Rejected(f"refused: {part!r} is an environment file")
        if "\0" in part:
            raise Rejected("refused: path contains a NUL byte")


def real_root(root):
    """The root with links resolved. A root that is missing is a miss for the caller, not a read."""
    return Path(root).resolve(strict=True)


def resolve_inside(root, relative, *, suffix=True):
    """`relative` (a path under root) resolved through links; it must stay under root and, when `suffix`, end in an
    allowlisted extension (the extension is judged on the file the link finally reaches, too)."""
    base = real_root(root)
    requested = Path(str(relative))
    if requested.is_absolute():
        raise Rejected("refused: absolute path")
    reject_names(requested.parts)
    if ".." in requested.parts:
        raise Rejected("refused: path leaves the package directory")
    if suffix and not allowed_suffix(requested.name):
        raise Rejected(f"refused: {requested.name!r} is not a declaration or markdown file ({', '.join(ALLOWED_SUFFIXES)})")
    target = (base / requested).resolve(strict=False)
    try:
        inside = target.relative_to(base)
    except ValueError:
        raise Rejected("refused: path resolves outside the allowed directory") from None
    reject_names(inside.parts)
    if suffix and not allowed_suffix(target.name):
        raise Rejected(f"refused: {target.name!r} is not a declaration or markdown file")
    return target


def contained_dir(root, candidate):
    """`candidate` (a directory) resolved through links, required to be under root. Rejected when a link leads out."""
    base = real_root(root)
    try:
        target = Path(candidate).resolve(strict=True)
        target.relative_to(base)
    except ValueError:
        raise Rejected("refused: directory resolves outside the allowed root") from None
    return target


def read_text(path, *, limit=MAX_FILE):
    """The text of an already-resolved regular file; None if it is missing. Rejects non-files, huge and binary files."""
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise Rejected(f"refused: {Path(path).name!r} is not a regular file")
    if info.st_size > limit:
        raise Rejected(f"refused: {Path(path).name!r} is larger than {limit} bytes")
    with open(path, "rb") as handle:
        raw = handle.read(limit + 1)
    if b"\0" in raw:
        raise Rejected(f"refused: {Path(path).name!r} looks binary")
    return raw.decode("utf-8", "replace")


def read_record(path, *, limit=200_000):
    """The text of a cache record, or None. Never follows a link, at the file or at the directory holding it; only a
    regular file under `limit` bytes is read. The one read here that is not of an allowlisted docs file: it is a record
    this package wrote itself, under the task directory."""
    path = Path(path)
    if path.parent.is_symlink():
        return None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        return handle.read(limit).decode("utf-8", "replace")
