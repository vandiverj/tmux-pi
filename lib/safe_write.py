"""safe_write — write tmux-pi state without ever following a link planted at the target.

A task directory, a session state directory and a lane record are written by tools and by workers that share the user's
rights. A symlink left where a state file belongs would send a plain open(path, "w") to any file the user can write. These
helpers never write through one:

  write_atomic  a temp file made with O_EXCL in the same directory, then one os.replace: a symlink at the path is replaced,
                the file it pointed at is not touched, and a reader sees the old file or the whole new one.
  open_append / append_text
                O_APPEND | O_CREAT | O_NOFOLLOW: a symlink at the path is refused (UnsafePath), never appended through.
  ensure_dir / refuse_link / check_below
                a directory the code creates or enters that it owns: a symlink there is refused, never followed.

Every helper takes a `root` (the session state directory, the work store root, the lane records directory: where the
caller already stands) and refuses when ANY component below it, down to the path itself, is a symlink: a linked `tasks/`
or `tasks/<id>/` would otherwise carry a leaf-safe os.replace to a directory outside the state. The root is never guessed
from the path: a caller that knows its state directory passes it, and `task_root(task_dir)` names it for a directory the
caller knows is `<state>/tasks/<id>` (or `archive/<id>`). With no root only the path itself is at risk, which the replace or
O_NOFOLLOW deals with; ensure_dir refuses a link at the directory it is given.

The file mode is what the old write gave it. An explicit `mode` is applied as given. Otherwise a new file gets the creation
mode (0o666, or 0o777 for an executable) less the umask; an existing regular file keeps its own mode when `keep_mode` (a
write into the file did that), and gets the creation mode too when not (a new file renamed over it did that). Standalone
(stdlib only) so a shell script can run it: `python3 lib/safe_write.py write|append|mkdir|check PATH` (stdin is the content).
"""
import argparse
import errno
import os
import secrets
import stat
import sys
from pathlib import Path

NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class UnsafePath(OSError):
    """A symlink where tmux-pi state belongs. An OSError, so the callers that already handle a failed write handle it."""

    def __init__(self, path, what="state path"):
        super().__init__(errno.ELOOP, f"unsafe {what} (symlink): {path}", str(path))


def is_link(path):
    return os.path.islink(path)


def refuse_link(path, what="state path"):
    """Raise UnsafePath when path is itself a symlink (a dangling one included). Only the last component is looked at."""
    if os.path.islink(path):
        raise UnsafePath(path, what)


def task_root(task_dir):
    """The state directory of a task directory the caller knows is `<state>/tasks/<id>` or `<state>/archive/<id>`: its
    grandparent, looked at only at that fixed depth, so it is never above the state directory. None for any other path."""
    parent = os.path.dirname(os.path.abspath(task_dir))
    return os.path.dirname(parent) if os.path.basename(parent) in ("tasks", "archive") else None


def _under(root, path):
    rel = os.path.relpath(path, root)
    return None if rel == os.pardir or rel.startswith(os.pardir + os.sep) else rel


def _rel_below(root, target):
    """target relative to root, or None. When the two are spelled differently (a linked prefix such as /var -> /private/var)
    the highest ancestor of target that IS root (same device and inode) is where root stands; nothing below it is resolved,
    so a link there is still seen by the walk."""
    rel = _under(root, target)
    if rel is not None:
        return rel
    try:
        want = os.stat(root)
    except OSError:
        return None
    found, here, parts = None, target, []
    while True:
        up = os.path.dirname(here)
        if up == here:
            return found
        parts.append(os.path.basename(here))
        here = up
        try:
            st = os.stat(here)
        except OSError:
            continue
        if (st.st_dev, st.st_ino) == (want.st_dev, want.st_ino):
            found = os.path.join(*reversed(parts))


def check_below(root, path, what="state path"):
    """Refuse (UnsafePath) when any component of path below root, the last one included, is a symlink (a dangling one
    too). root itself is not examined: it is where the caller already stands. With no root only path itself is looked at.
    Returns path relative to root (None with no root); ValueError when path is not under root."""
    if root is None:
        refuse_link(path, what)
        return None
    root, target = os.path.abspath(root), os.path.abspath(path)
    rel = _rel_below(root, target)
    if rel is None:
        raise ValueError(f"{path} is not under {root}")
    if rel == os.curdir:
        return rel
    here = root
    for part in rel.split(os.sep):
        here = os.path.join(here, part)
        if os.path.islink(here):
            raise UnsafePath(here, what)
    return rel


def check_parent(root, path):
    """check_below for the directory a file is written in. With no root there is nothing to compare against (a path the
    user named: its directory may legitimately be a link), and only the file itself, which the replace or O_NOFOLLOW deals
    with, is at risk."""
    if root is not None:
        check_below(root, os.path.dirname(os.path.abspath(path)), "state directory")


def ensure_dir(path, mode=0o777, what="state directory", root=None):
    """mkdir -p for a directory tmux-pi owns; a symlink at path, or at any component below root, is refused rather than
    entered. Returns the Path."""
    path = Path(path)
    check_below(root, path, what)
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    return path


def _payload(data, encoding, errors):
    return data if isinstance(data, (bytes, bytearray)) else data.encode(encoding, errors)


def write_temp(path, data, mode=None, create=0o666, fsync=False, encoding="utf-8", errors="strict", root=None,
               keep_mode=True):
    """Write data to a new O_EXCL temp file beside path and return its name; the caller renames it into place (and
    removes it on failure). Never opens path itself; a link in any directory between root and path is refused first."""
    path = os.fspath(path)
    check_parent(root, path)
    directory, base = os.path.split(os.path.abspath(path))
    body = _payload(data, encoding, errors)
    if mode is None and keep_mode:
        try:
            old = os.lstat(path)
        except OSError:
            old = None
        if old is not None and stat.S_ISREG(old.st_mode):
            mode = stat.S_IMODE(old.st_mode)
    for _ in range(100):
        temp = os.path.join(directory, f".{base[:100]}.{secrets.token_hex(6)}")
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW | getattr(os, "O_CLOEXEC", 0), create)
            break
        except FileExistsError:
            continue
    else:
        raise OSError(errno.EEXIST, "no free temp name", directory)
    try:
        with os.fdopen(fd, "wb") as out:
            if mode is not None:
                os.fchmod(out.fileno(), mode)
            out.write(body)
            out.flush()
            if fsync:
                os.fsync(out.fileno())
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    return temp


def write_atomic(path, data, mode=None, create=0o666, fsync=False, encoding="utf-8", errors="strict", root=None,
                 keep_mode=True):
    """Replace path with data (str or bytes) through a temp file in the same directory. A symlink at path is replaced,
    never written through. An explicit mode wins; with none, an existing regular file keeps its mode when keep_mode, and
    otherwise (a new file, or keep_mode=False) the mode is `create` less the umask."""
    temp = write_temp(path, data, mode, create, fsync, encoding, errors, root, keep_mode)
    try:
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    return str(path)


def open_append(path, mode=0o666, encoding="utf-8", errors="strict", root=None):
    """The file at path opened for appending text (created with `mode` less the umask). A symlink there, or in a
    directory between root and path, is refused."""
    check_parent(root, path)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | NOFOLLOW | getattr(os, "O_CLOEXEC", 0), mode)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, getattr(errno, "EMLINK", errno.ELOOP)) or (NOFOLLOW and os.path.islink(path)):
            raise UnsafePath(path, "append target") from exc
        raise
    return os.fdopen(fd, "a", encoding=encoding, errors=errors)


def append_text(path, text, mode=0o666, encoding="utf-8", errors="strict", root=None):
    with open_append(path, mode, encoding, errors, root) as out:
        out.write(text)


# ------------------------------------------------------------------ the shell's way in

def main(argv=None):
    parser = argparse.ArgumentParser(prog="safe_write", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("write", "append"):
        p = sub.add_parser(name, help=f"{name} stdin to PATH")
        p.add_argument("path")
        p.add_argument("--root", help="refuse a symlink in any directory below ROOT (default: only a link at the path itself)")
        p.add_argument("--mode", help="octal mode; default: the existing file's, else the umask's")
        if name == "write":
            p.add_argument("--executable", action="store_true", help="make it executable: 0777 less the umask, an existing file's mode included")
            p.add_argument("--fresh", action="store_true", help="0666 less the umask even over an existing file (as a new file renamed over it)")
    p = sub.add_parser("mkdir", help="mkdir -p for owned directories; a symlink is refused")
    p.add_argument("--root")
    p.add_argument("paths", nargs="+")
    p = sub.add_parser("check", help="exit 1 when a path is a symlink")
    p.add_argument("--root")
    p.add_argument("paths", nargs="+")
    args = parser.parse_args(argv)
    try:
        if args.command == "mkdir":
            for path in args.paths:
                ensure_dir(path, root=args.root)
        elif args.command == "check":
            for path in args.paths:
                check_below(args.root, path, "state directory")
        else:
            body = sys.stdin.buffer.read()
            mode = int(args.mode, 8) if args.mode else None
            if args.command == "write":
                write_atomic(args.path, body, mode, create=0o777 if args.executable else 0o666, root=args.root,
                             keep_mode=not (args.executable or args.fresh))
            else:
                with open_append(args.path, mode if mode is not None else 0o666, root=args.root) as out:
                    out.buffer.write(body)
    except (OSError, ValueError) as exc:
        print(f"safe_write: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
