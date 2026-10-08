"""Read and validate the owner's per-repository trust record (T7, slice 1).

A record says "this working tree holds no credentials". It is written only by an owner-run command
(not part of this module) into one file per repository under the owner's config directory. This
module only reads. It never writes, never starts a process and never reads the environment: the
home directory comes from the password database and every directory is a function argument.

Anything doubtful is "ignored" and the caller prompts as before. There is no fallback to an older
copy. A record that does not exist is "absent" and silent. Any exception is "ignored: error".

Every check that decides a record is a single `if` carrying a `check:<name>` comment marker, so a
mutation run can switch each one off in turn and see a test fail.
"""
import datetime
import hashlib
import json
import os
import pwd
import stat

import sensitive_read   # constants only; nothing here calls into it

STATEMENT = "this working tree holds no credentials"
VERSION = 1
MAX_RECORD = 4096
MAX_NOTE = 200
# Fixed, not read from $TMPDIR. Tests pass their own.
DEFAULT_TEMP_ROOTS = ("/tmp", "/var/tmp", "/private/tmp", "/private/var/tmp", "/var/folders",
                      "/private/var/folders", "/dev/shm")
# Known credential locations (R2), taken from the classifier so the two cannot drift. The `.git`
# pairs are left out: a worktree's own `.git` is a file and is judged by the link check.
_CRED_DIRS = tuple(sorted(set(sensitive_read.SENSITIVE_DIR) |
                          {"/".join(p) for p in sensitive_read.SENSITIVE_PAIRS if p[0] != ".git"}))
# Reasons that are not a damaged record: nothing to log, nothing to say to the owner.
_SILENT = frozenset({"absent", "no_repo", "unverified_tree", "worktrees_off"})
_WORDS = {
    "perms": "bad permissions", "owner": "wrong owner", "nlink": "extra hard link",
    "symlink": "symbolic link", "notfile": "not a regular file", "size": "too large",
    "parse": "unreadable", "version": "unknown version", "statement": "wrong statement",
    "fields": "missing or wrong fields", "name_mismatch": "name does not match root",
    "root_moved": "root moved", "not_git_root": "not a repository root", "bad_root": "root not allowed",
    "dir_in_repo": "trust directory inside a repository", "dir_in_temp": "trust directory in a temp root",
    "dir_outside_home": "trust directory outside home", "expired": "expired", "error": "internal error",
    "worktree_home": "worktree location not allowed", "worktree_creds": "worktree holds credential directories",
}


class Result:
    """state is 'trusted', 'absent' or 'ignored'; reason is None only when trusted."""
    __slots__ = ("state", "reason", "record", "root")

    def __init__(self, state, reason=None, record=None, root=None):
        self.state, self.reason, self.record, self.root = state, reason, record, root

    @property
    def trusted(self):
        return self.state == "trusted"

    @property
    def ignored(self):
        """A record exists but was refused: the caller logs it and adds the prompt suffix."""
        return self.state == "ignored"

    def suffix(self):
        if self.state != "ignored":
            return ""
        return "trust record ignored: " + _WORDS.get(self.reason, self.reason)

    def __repr__(self):
        return "Result(%s, %s)" % (self.state, self.reason)


def _absent(reason="absent"):
    return Result("absent", reason)


def _ignored(reason):
    return Result("ignored", reason)


def home_dir():
    """The account's home from the password database, never $HOME."""
    return pwd.getpwuid(os.getuid()).pw_dir


def trust_dir():
    return os.path.join(home_dir(), ".config", "tmux-pi", "trust")


def record_name(root):
    return hashlib.sha256(root.encode("utf-8", "surrogateescape")).hexdigest()[:32] + ".json"


def inside(path, tree):
    """True when path (symlinks resolved) is tree itself or below it. Exact, case-sensitive."""
    return _within(os.path.realpath(path), os.path.realpath(tree))


def _within(p, t):
    if p == t:
        return True
    return p.startswith(t if t.endswith(os.sep) else t + os.sep)


def _read_small(path, limit):
    """Text of a regular, non-link file of at most `limit` bytes, else None. File reads only."""
    fd = None
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != (before.st_dev, before.st_ino):
            return None
        data = os.read(fd, limit + 1)
        if len(data) > limit:
            return None
        return data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        if fd is not None:
            os.close(fd)


def _linked_main(work, gitfile):
    """Main root of a linked worktree when both links agree, else None."""
    text = _read_small(gitfile, 4096)
    if text is None:
        return None
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) != 1 or not lines[0].startswith("gitdir:"):
        return None
    given = lines[0][len("gitdir:"):].strip()
    if not given:
        return None
    entry = os.path.realpath(given if os.path.isabs(given) else os.path.join(work, given))
    worktrees = os.path.dirname(entry)
    common = os.path.dirname(worktrees)
    if os.path.basename(worktrees) != "worktrees" or os.path.basename(common) != ".git":
        return None
    try:
        if not stat.S_ISDIR(os.lstat(common).st_mode) or not stat.S_ISDIR(os.lstat(entry).st_mode):
            return None
    except OSError:
        return None
    cd = _read_small(os.path.join(entry, "commondir"), 4096)
    back = _read_small(os.path.join(entry, "gitdir"), 4096)
    if cd is None or back is None or not cd.strip() or not back.strip():
        return None
    cd, back = cd.strip(), back.strip()
    if os.path.realpath(cd if os.path.isabs(cd) else os.path.join(entry, cd)) != common:
        return None
    if os.path.realpath(back if os.path.isabs(back) else os.path.join(entry, back)) != \
            os.path.join(os.path.realpath(work), ".git"):
        return None
    return os.path.dirname(common)


def tree_info(path):
    """(top, kind, main): the nearest directory holding a `.git`, found by file reads only.

    kind is 'main' (`.git` is a directory), 'worktree' (a `.git` file that git's own two-way link
    confirms; main is then the main root), 'gitfile' (any other `.git`: submodule, planted or
    unreadable; main is None) or 'none' (top and main are None).
    """
    d = os.path.realpath(path)
    while True:
        g = os.path.join(d, ".git")
        try:
            st = os.lstat(g)
        except (FileNotFoundError, NotADirectoryError):
            st = None
        except OSError:
            return d, "gitfile", None
        if st is not None:
            if stat.S_ISDIR(st.st_mode):
                return d, "main", d
            if stat.S_ISREG(st.st_mode):
                main = _linked_main(d, g)
                if main is not None:
                    return d, "worktree", main
            return d, "gitfile", None
        parent = os.path.dirname(d)
        if parent == d:
            return None, "none", None
        d = parent


def tree_of(path):
    top, kind, _ = tree_info(path)
    return top, kind


def _parse_expiry(value):
    when = datetime.datetime.fromisoformat(value)
    if when.tzinfo is None:
        if len(value) != 10:        # a bare date means that day, UTC; a time without a zone is refused
            raise ValueError("no zone")
        when = when.replace(tzinfo=datetime.timezone.utc)
    return when


def _no_dupes(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate key")
        out[k] = v
    return out


def _refuse_constant(name):
    raise ValueError(name)


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _check_dir_chain(directory, home, temp_roots):
    """Reason code for a bad trust directory, 'absent' when it does not exist, else None."""
    home_real = os.path.realpath(home)
    cfg = os.path.join(home_real, ".config")
    cfg_real = os.path.realpath(cfg)         # the one place a symlink is tolerated: the owner's dotfile layout
    lexical = os.path.normpath(directory)
    for base in (os.path.join(home, ".config"), cfg):
        if _within(lexical, os.path.normpath(base)):
            start, anchor = cfg_real, os.path.normpath(base)
            break
    else:
        for base in (home, home_real):
            if _within(lexical, os.path.normpath(base)):
                start, anchor = home_real, os.path.normpath(base)
                break
        else:
            return "dir_outside_home"
    parts = [p for p in lexical[len(anchor):].split(os.sep) if p]
    uid = os.geteuid()
    # Every directory on the resolved path, home first. A resolved .config inside home adds the
    # directories between home and it; one outside home adds all of its ancestors (those may be root's).
    chain = [home_real]
    if start != home_real:
        if _within(start, home_real):
            cur = home_real
            for p in [p for p in start[len(home_real):].split(os.sep) if p]:
                cur = os.path.join(cur, p)
                chain.append(cur)
        else:
            cur = os.sep
            chain.append(cur)
            for p in [p for p in start.split(os.sep) if p]:
                cur = os.path.join(cur, p)
                chain.append(cur)
    cur = start
    for p in parts:
        cur = os.path.join(cur, p)
        chain.append(cur)
    if chain[-1] == home_real:
        return "dir_outside_home"
    for c in chain:
        try:
            st = os.lstat(c)
        except (FileNotFoundError, NotADirectoryError):
            return "absent"
        if stat.S_ISLNK(st.st_mode):                                                # check:dir_symlink
            return "symlink"
        if not stat.S_ISDIR(st.st_mode):                                            # check:dir_notdir
            return "notfile"
        if st.st_uid != uid and not (st.st_uid == 0 and not _within(c, home_real)):  # check:dir_owner
            return "owner"
        if st.st_mode & 0o022:                                                      # check:dir_perms
            return "perms"
    resolved = os.path.realpath(chain[-1])
    if tree_info(resolved)[1] != "none":                                            # check:dir_in_repo
        return "dir_in_repo"
    for t in temp_roots:
        if _within(resolved, os.path.realpath(t)):                                  # check:dir_in_temp
            return "dir_in_temp"
    return None


def _validate(root, directory, home, now, temp_roots):
    if not os.path.isabs(root) or os.path.normpath(root) != root:
        return _ignored("fields")
    bad = _check_dir_chain(directory, home, temp_roots)
    if bad == "absent":
        return _absent()
    if bad:
        return _ignored(bad)
    path = os.path.join(directory, record_name(root))
    try:
        before = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return _absent()
    if stat.S_ISLNK(before.st_mode):                                                # check:symlink
        return _ignored("symlink")
    if not stat.S_ISREG(before.st_mode):                                            # check:notfile
        return _ignored("notfile")
    if before.st_uid != os.geteuid():                                               # check:owner
        return _ignored("owner")
    if before.st_mode & 0o022:                                                      # check:perms
        return _ignored("perms")
    if before.st_nlink != 1:                                                        # check:nlink
        return _ignored("nlink")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        # The same checks on the open file, so a swap between lstat and open cannot slip through.
        if (st.st_dev, st.st_ino) != (before.st_dev, before.st_ino):                # check:swap
            return _ignored("symlink")
        data = os.read(fd, MAX_RECORD + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_RECORD:                                                      # check:size
        return _ignored("size")
    try:
        rec = json.loads(data.decode("utf-8"), object_pairs_hook=_no_dupes, parse_constant=_refuse_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):                        # check:parse
        return _ignored("parse")
    if not isinstance(rec, dict):                                                   # check:parse_object
        return _ignored("parse")
    if not _int(rec.get("version")) or rec["version"] != VERSION:                   # check:version
        return _ignored("version")
    if rec.get("statement") != STATEMENT:                                           # check:statement
        return _ignored("statement")
    if not (isinstance(rec.get("root"), str) and _int(rec.get("root_dev")) and _int(rec.get("root_ino"))
            and isinstance(rec.get("worktrees"), bool) and isinstance(rec.get("granted_at"), str)
            and isinstance(rec.get("granted_by"), str)
            and isinstance(rec.get("note", ""), str) and len(rec.get("note", "")) <= MAX_NOTE
            and (rec.get("expires") is None or isinstance(rec["expires"], str))):    # check:fields
        return _ignored("fields")
    if rec["root"] != root or record_name(rec["root"]) != os.path.basename(path):   # check:name
        return _ignored("name_mismatch")
    home_real = os.path.realpath(home)
    if root == os.sep or _within(home_real, root) or _within(root, os.path.realpath(directory)):  # check:bad_root
        return _ignored("bad_root")
    try:
        rst = os.lstat(root)
    except OSError:
        return _ignored("root_moved")
    if (not stat.S_ISDIR(rst.st_mode) or (rst.st_dev, rst.st_ino) != (rec["root_dev"], rec["root_ino"])
            or os.path.realpath(root) != root):                                      # check:root_moved
        return _ignored("root_moved")
    try:
        gst = os.lstat(os.path.join(root, ".git"))
    except OSError:
        return _ignored("not_git_root")
    if not stat.S_ISDIR(gst.st_mode):                                               # check:not_git_root
        return _ignored("not_git_root")
    if rec.get("expires") is not None:
        try:
            when = _parse_expiry(rec["expires"])
        except ValueError:
            return _ignored("parse")
        if when <= now:                                                             # check:expired
            return _ignored("expired")
    return Result("trusted", None, rec, root)


def load(root, directory, home=None, now=None, temp_roots=None):
    """Judge the record for `root` (a main work tree, realpath form) kept in `directory`.

    Never raises. `home`, `now` and `temp_roots` exist for tests; the defaults are the password
    database, the clock and DEFAULT_TEMP_ROOTS.
    """
    try:
        return _validate(root, directory, home if home is not None else home_dir(),
                         now if now is not None else datetime.datetime.now(datetime.timezone.utc),
                         DEFAULT_TEMP_ROOTS if temp_roots is None else tuple(temp_roots))
    except Exception:
        return _ignored("error")


def _worktree_refusal(work, directory, home):
    tdir = os.path.realpath(directory)
    if work == os.sep or _within(home, work) or _within(work, tdir) or _within(tdir, work):  # check:wt_place
        return "worktree_home"
    if any(os.path.lexists(os.path.join(work, *c.split("/"))) for c in _CRED_DIRS):  # check:wt_creds
        return "worktree_creds"
    return None


def lookup(path, directory, home=None, now=None, temp_roots=None):
    """Is `path` in a tree the owner has declared free of credentials?

    A path in a main work tree uses that tree's record. A path in a linked worktree uses the main
    root's record when `worktrees` is true and both git links agree. A nested repository, a
    submodule or an unverified `.git` file inherits nothing. Never raises.
    """
    try:
        home = home if home is not None else home_dir()
        top, kind, main = tree_info(path)
        if kind == "none":
            return _absent("no_repo")
        if kind == "gitfile":
            return _absent("unverified_tree")
        res = load(main, directory, home=home, now=now, temp_roots=temp_roots)
        if kind == "main" or not res.trusted:
            return res
        if res.record["worktrees"] is not True:                                      # check:wt_off
            return _absent("worktrees_off")
        bad = _worktree_refusal(top, directory, os.path.realpath(home))
        return _ignored(bad) if bad else res
    except Exception:
        return _ignored("error")


def is_silent(result):
    """True when the result needs no log line and no prompt suffix."""
    return result.reason is None or result.reason in _SILENT
