"""Lanes (T10, slice L1): the lane record, its validator and the configuration readers. No commands.

A lane is a git worktree and branch with a record, `<git common dir>/tmux-pi/lanes/<name>/lane.json`. The record lives in
the repository's common git directory (not in a tmux-pi session's state directory), so it outlives a session and is shared
by every worktree of the repository. A record that is unreadable or disagrees with git refuses with the file named, and
nothing is repaired. Configuration is the one place that defaults (owner decision 2026-10-08, an exception to "a missing
value refuses"): an ABSENT lanes block, file or key takes the default below and the loaded configuration lists it under
"defaulted" (the lane record stores that list); a key that is PRESENT but malformed or of the wrong type, or a file that
exists but cannot be read, still refuses naming key and file.

The record is read only through `lane_record` (shape) and `read_lane` (file); `lane_reality` checks it against git.

What later slices (L2-L8) must not assume this slice proved:
- `lane_reality` proves history shape only: that the worktree is the one the repository registered, on the recorded
  branch, and that base and every closed commit are in order on it. It does NOT prove HEAD == the last closed commit,
  a clean status, the reviewed tree, a review verdict or its lineage, ownership of `touches`, the occupant, or that no
  task is queued or live. L3-L8 derive and enforce those.
- `lane_record` and `read_lane` validate format only; they do not check git. Never use a raw record; call
  `lane_reality` before acting on one. `write_lane` does not read or validate the record it replaces, so a corrupt
  prior record is overwritten: the caller reads first.
- The lane lock (flock in the common git dir, owned by the thread that took it) does not replace a session's dispatch
  lock. Finding the tasks and reviews of a step after a session restart is unsolved here (a record holds no task ids).
  Its guarantees are: records are reached by no-follow traversal from the verified common git directory, and a write
  needs the lock held by the writing thread of the writing process. A hostile race between check and open is not a
  guarantee (the threat model is mistakes and stale state, not the owner).
- Rendering a branch or worktree name (`lane_branch`, `lane_worktree`) does not check it is free; call
  `precheck_lane_slot` before creating anything. No command exists in L1, nothing here runs a worker."""
import contextlib
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import threading

ROOT = Path(__file__).resolve().parent.parent
MACHINE_CONFIG = ROOT / "config/orchestration.json"
REPO_CONFIG = Path(".tmux-pi/lanes.json")  # relative to the repository's main working tree
SCHEMA = "tp-lane:v1"
KINDS = ("work", "integration")
STATES = ("open", "integrating", "integrated", "abandoned")
DEPS = ("shared", "own")  # shared: node_modules are symlinks to the main checkout's (lib/lane_deps.py)
VERDICTS = ("APPROVED", "APPROVED_WITH_NOTES")
EMPTY = "EMPTY"  # the verdict of a step closed without a review because its work left the tree as the last closed commit had it
LANE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")
RESERVED_NAMES = ("archive",)  # lane_names skips this directory, so a lane called so would be invisible to the count


def valid_lane_name(name):
    return isinstance(name, str) and bool(LANE_NAME.fullmatch(name)) and name not in RESERVED_NAMES
STEP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")  # tp-agent's IDENT
INTEGRATION_ID = re.compile(r"^integrate-[0-9a-f]{12}$")
HEX = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
TEST_LEVEL = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")
MAX_DOMAIN = 200
MAX_TOUCHES = 50
GIT_TIMEOUT = 30
REMOVE_TIMEOUT = 1800  # `git worktree remove` of a tree with node_modules is minutes of unlinking; the only git call given more than GIT_TIMEOUT
LANES_SUBDIR = Path("tmux-pi/lanes")
LOCK_NAME = ".lock"

_REQUIRED_RECORD = ("schema", "name", "kind", "state", "repo", "git_common_dir", "branch", "worktree", "base", "created",
                    "domain", "touches", "closed", "integrated", "defaulted")

# Owner defaults, used only for an absent value.
DEFAULT_MACHINE = {"max_active_lanes": 2}
DEFAULT_REPO = {"branch_prefix": "lane/", "worktree_pattern": "{repo}-wt-{name}", "integration_branch": "lane/integrate",
                "integration_lane": "integrate"}
DEFAULTABLE = tuple(sorted({*DEFAULT_MACHINE, *DEFAULT_REPO}))


# ---------------------------------------------------------------- git

class GitTimeout(ValueError):
    """A git call that did not finish in time and was killed; whatever it was doing may be half done."""


def _git(args, cwd, ok=(0,), timeout=None):
    """(returncode, stdout) of `git -C cwd args`; a return code outside `ok`, a timeout (GIT_TIMEOUT unless `timeout` is
    given) or a missing git refuses."""
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")}
    try:
        run = subprocess.run(["git", "-C", str(cwd)] + list(args), text=True, capture_output=True, timeout=timeout or GIT_TIMEOUT, env=env)
    except subprocess.TimeoutExpired as error:
        raise GitTimeout(f"git {' '.join(args)} could not run in {cwd} ({error.__class__.__name__})")
    except OSError as error:
        raise ValueError(f"git {' '.join(args)} could not run in {cwd} ({error.__class__.__name__})")
    if run.returncode not in ok:
        raise ValueError(f"git {' '.join(args)} failed in {cwd}: {run.stderr.strip() or 'exit ' + str(run.returncode)}")
    return run.returncode, run.stdout


def git_common_dir(path):
    """The realpath of the repository's common git directory, from any directory of any of its worktrees."""
    _, out = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], path)
    value = out.strip()
    if not value.startswith("/"):
        raise ValueError(f"cannot find a git repository from {path}")
    return os.path.realpath(value)


def main_worktree(common_dir):
    """The realpath of the repository's main working tree, from `git worktree list` (its first entry)."""
    _, out = _git(["worktree", "list", "--porcelain"], common_dir)
    first = out.split("\n\n", 1)[0].splitlines()
    if not first or not first[0].startswith("worktree ") or any(line == "bare" for line in first):
        raise ValueError(f"the repository at {common_dir} has no main working tree (a bare repository?)")
    return os.path.realpath(first[0][len("worktree "):])


def registered_worktrees(common_dir):
    """The realpaths of every worktree git lists for the repository, the main one first."""
    _, out = _git(["worktree", "list", "--porcelain"], common_dir)
    return [os.path.realpath(line[len("worktree "):]) for line in out.splitlines() if line.startswith("worktree ")]


def valid_branch(name):
    """Whether `name` is a usable branch name (git's own rule, and not an option or a @{-n} shorthand)."""
    if not isinstance(name, str) or not name or name.startswith("-") or "\n" in name or "\x00" in name:
        return False
    run = subprocess.run(["git", "check-ref-format", f"refs/heads/{name}"], capture_output=True)
    return run.returncode == 0


# ---------------------------------------------------------------- paths and overlap

def path_prefix(value):
    """A repo-relative path prefix in its one canonical form, or ValueError. Nothing is rewritten: a leading or trailing
    '/', an empty, '.' or '..' part, a '.git' part or a control character refuses."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"path {value!r} is not text")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"path {value!r} has a control character")
    if value.startswith("/"):
        raise ValueError(f"path {value!r} is absolute; use a path relative to the repository")
    for part in value.split("/"):
        if part in ("", ".", ".."):
            raise ValueError(f"path {value!r} is not normalised (empty, '.' or '..' part, or a trailing '/')")
        if part == ".git":
            raise ValueError(f"path {value!r} is inside .git")
    return value


def path_prefixes(values, what="touches", minimum=0, maximum=MAX_TOUCHES):
    """A list of unique canonical path prefixes, sorted. Duplicates, a wrong count or a bad entry refuse."""
    if not isinstance(values, list):
        raise ValueError(f"{what} is not a list")
    if len(values) < minimum:
        raise ValueError(f"{what} needs at least {minimum} path{'s' if minimum != 1 else ''}")
    if len(values) > maximum:
        raise ValueError(f"{what} has {len(values)} paths (limit {maximum})")
    clean = [path_prefix(v) for v in values]
    if len(set(clean)) != len(clean):
        raise ValueError(f"{what} lists a path twice")
    return sorted(clean)


def paths_overlap(a, b):
    """Two prefixes overlap when equal or when one is a path-component prefix of the other (lib/a ~ lib/a/b.py, not lib/ab.py)."""
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def overlapping_paths(first, second):
    """The sorted distinct paths where the two lists overlap: for each overlapping pair, the more specific path."""
    found = set()
    for a in first:
        for b in second:
            if paths_overlap(a, b):
                found.add(a if len(a) >= len(b) else b)
    return sorted(found)


# ---------------------------------------------------------------- the record

def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _timestamp(value, what):
    if not isinstance(value, str):
        raise ValueError(f"{what} is not text")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{what} {value!r} is not an ISO-8601 time")
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{what} {value!r} is not in UTC")
    return value


def _hex(value, what):
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise ValueError(f"{what} {value!r} is not a full lowercase hex object id")
    return value


def _absolute(value, what):
    """An absolute path that is its own realpath (no symlink in it, existing or not: a worktree not yet created is
    judged by its existing ancestors), or ValueError."""
    if not isinstance(value, str) or not value.startswith("/") or os.path.normpath(value) != value or any(ord(c) < 32 for c in value):
        raise ValueError(f"{what} {value!r} is not a normalised absolute path")
    if os.path.realpath(value) != value:
        raise ValueError(f"{what} {value!r} is not a realpath (it goes through a symlink; the realpath is {os.path.realpath(value)!r})")
    return value


def _inside(path, parent):
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def _closed_entry(value, index):
    where = f"closed[{index}]"
    if not isinstance(value, dict):
        raise ValueError(f"{where} is not an object")
    for key in ("step", "commit", "tree", "review", "verdict", "closed_at"):
        if key not in value:
            raise ValueError(f"{where} has no {key}")
    if not isinstance(value["step"], str) or not STEP_ID.fullmatch(value["step"]):
        raise ValueError(f"{where}.step {value['step']!r} is not a valid step id")
    empty = value["verdict"] == EMPTY  # an empty step has no review: nothing was changed to review
    if empty:
        if value["review"] is not None:
            raise ValueError(f"{where}.review {value['review']!r} must be null for an {EMPTY} step")
    elif not isinstance(value["review"], str) or not TASK_ID.fullmatch(value["review"]):
        raise ValueError(f"{where}.review {value['review']!r} is not a task id")
    if not empty and value["verdict"] not in VERDICTS:
        raise ValueError(f"{where}.verdict {value['verdict']!r} is not one of {', '.join(VERDICTS)} (or {EMPTY})")
    return {"step": value["step"], "commit": _hex(value["commit"], f"{where}.commit"), "tree": _hex(value["tree"], f"{where}.tree"),
            "review": value["review"], "verdict": value["verdict"], "closed_at": _timestamp(value["closed_at"], f"{where}.closed_at")}


def lane_record(value, name=None):
    """The one check of a lane record (what lane.json holds). It covers every field any reader uses and returns a new dict
    of just those fields; callers take their data only from this return value. Unknown extra keys are dropped, an unknown
    schema refuses. `name` is the directory name the record's name must equal. ValueError says which field is wrong.
    Pure: it reads no file and runs no git (except git's branch-name rule); lane_reality compares with the repository."""
    if not isinstance(value, dict):
        raise ValueError("the lane record is not an object")
    if value.get("schema") != SCHEMA:
        raise ValueError(f"the lane record has schema {value.get('schema')!r}, expected {SCHEMA!r}")
    for key in _REQUIRED_RECORD:
        if key not in value:
            raise ValueError(f"the lane record has no {key}")
    lane = value["name"]
    if not valid_lane_name(lane):
        raise ValueError(f"invalid lane name {lane!r}: use 1-24 characters of a-z, 0-9 and -, starting with a letter or digit")
    if name is not None and lane != name:
        raise ValueError(f"the lane record is named {lane!r} but sits in the directory for {name!r}")
    if value["kind"] not in KINDS:
        raise ValueError(f"the lane record has an unknown kind {value['kind']!r}")
    if value["state"] not in STATES:
        raise ValueError(f"the lane record has an unknown state {value['state']!r}")
    repo = _absolute(value["repo"], "repo")
    common = _absolute(value["git_common_dir"], "git_common_dir")
    branch = value["branch"]
    if not valid_branch(branch):
        raise ValueError(f"branch {branch!r} is not a valid branch name")
    worktree = _absolute(value["worktree"], "worktree")
    if worktree == "/" or _inside(worktree, repo) or _inside(repo, worktree) or _inside(worktree, common):
        raise ValueError(f"worktree {worktree!r} must be a directory outside the repository {repo!r}")
    base = value["base"]
    if not isinstance(base, dict) or not isinstance(base.get("ref"), str) or not base["ref"] or any(ord(c) < 32 for c in base["ref"]):
        raise ValueError("base has no usable ref")
    base = {"ref": base["ref"], "commit": _hex(base.get("commit"), "base.commit")}
    domain = value["domain"]
    if not isinstance(domain, str) or not domain.strip() or len(domain) > MAX_DOMAIN or any(ord(c) < 32 for c in domain):
        raise ValueError(f"domain must be 1-{MAX_DOMAIN} characters of text on one line")
    touches = path_prefixes(value["touches"], "touches", minimum=1)
    if touches != value["touches"]:
        raise ValueError("touches is not sorted")
    if not isinstance(value["closed"], list):
        raise ValueError("closed is not a list")
    closed = [_closed_entry(entry, i) for i, entry in enumerate(value["closed"])]
    if len({entry["step"] for entry in closed}) != len(closed):
        raise ValueError("closed lists a step twice")
    integrated = value["integrated"]
    if integrated is not None:
        if not isinstance(integrated, dict) or not isinstance(integrated.get("integration"), str) \
                or not INTEGRATION_ID.fullmatch(integrated["integration"]):
            raise ValueError("integrated has no valid integration id")
        integrated = {"integration": integrated["integration"], "commit": _hex(integrated.get("commit"), "integrated.commit"),
                      "at": _timestamp(integrated.get("at"), "integrated.at")}
    if (value["state"] == "integrated") != (integrated is not None):
        raise ValueError("integrated is set exactly when state is 'integrated'")
    defaulted = value["defaulted"]
    if not isinstance(defaulted, list) or not all(isinstance(k, str) and k in DEFAULTABLE for k in defaulted) \
            or defaulted != sorted(set(defaulted)):
        raise ValueError(f"defaulted must be a sorted list of distinct names from {', '.join(DEFAULTABLE)}")
    deps = value.get("deps", "own")  # optional: a record from before shared dependencies has its own
    if deps not in DEPS:
        raise ValueError(f"deps {deps!r} is not one of {', '.join(DEPS)}")
    return {"schema": SCHEMA, "name": lane, "kind": value["kind"], "state": value["state"], "repo": repo, "git_common_dir": common,
            "branch": branch, "worktree": worktree, "base": base, "created": _timestamp(value["created"], "created"),
            "domain": domain, "touches": touches, "closed": closed, "integrated": integrated, "defaulted": list(defaulted), "deps": deps}


# ---------------------------------------------------------------- the files

def _fail_os(error, shown):
    return ValueError(f"cannot read {shown} ({error.__class__.__name__})")


def _open_dir(parent_fd, name, shown):
    """Open directory `name` inside the directory `parent_fd`, following no symlink. FileNotFoundError if it is missing;
    anything that is not a real directory refuses."""
    info = os.lstat(name, dir_fd=parent_fd)
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"{shown} is not a directory")
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as error:
        raise _fail_os(error, shown)
    after = os.fstat(fd)
    if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
        os.close(fd)
        raise ValueError(f"{shown} changed while it was being opened")
    return fd


def verified_common_dir(common_dir):
    """The realpath of `common_dir`, proven by git to be a repository's common git directory: git run in it must report
    that same directory as its common git directory. A directory that merely holds a file named HEAD, a linked
    worktree's entry, or a directory inside some other repository does not pass. Every record operation starts here."""
    if not isinstance(common_dir, (str, os.PathLike)):
        raise ValueError(f"{common_dir!r} is not a path")
    real = os.path.realpath(common_dir)
    try:
        found = git_common_dir(real)
    except ValueError as error:
        raise ValueError(f"{real} is not a git common directory ({error})")
    if found != real:
        raise ValueError(f"{real} is not a git common directory (git finds the common directory {found})")
    return real


def _walk(common_dir, parts, create=False):
    """A directory fd for <common git dir>/<parts...>, reached component by component with no symlink followed from the
    common git directory (the anchor, proven by git: verified_common_dir). A missing component gives None unless `create`.
    The caller closes the fd."""
    real = verified_common_dir(common_dir)
    try:
        fd = os.open(real, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as error:
        raise _fail_os(error, real)
    shown = Path(real)
    try:
        for part in parts:
            shown = shown / part
            if create:
                try:
                    os.mkdir(part, 0o777, dir_fd=fd)
                except FileExistsError:
                    pass
                except OSError as error:
                    raise _fail_os(error, shown)
            try:
                nxt = _open_dir(fd, part, shown)
            except FileNotFoundError:
                os.close(fd)
                return None
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        raise


def _dir_key(fd):
    info = os.fstat(fd)
    return (info.st_dev, info.st_ino)


def lanes_dir(common_dir):
    return Path(common_dir) / LANES_SUBDIR


def lane_path(common_dir, name):
    if not valid_lane_name(name):
        raise ValueError(f"invalid lane name {name!r}: use 1-24 characters of a-z, 0-9 and -, starting with a letter or digit")
    return lanes_dir(common_dir) / name / "lane.json"


def lane_names(common_dir):
    """The names of the lane directories that exist, sorted: every entry of the lanes directory except the archive and
    dot-files. A name that is not a valid lane name is listed too, so read_lane can refuse it with a reason. A missing
    lanes directory means no lanes; an unreadable one, or one reached through a symlink, refuses."""
    base = lanes_dir(common_dir)
    try:
        fd = _walk(common_dir, LANES_SUBDIR.parts)
        if fd is None:
            return []
        try:
            return sorted(n for n in os.listdir(fd) if n != "archive" and not n.startswith("."))
        finally:
            os.close(fd)
    except OSError as error:
        raise _fail_os(error, base)


def _read_regular(dir_fd, name, shown):
    """The text of regular file `name` inside `dir_fd`: lstat'ed first (a symlink, FIFO, ... refuses unopened), opened
    without following, and checked to be the file that was lstat'ed."""
    info = os.lstat(name, dir_fd=dir_fd)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("it is not a regular file")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    with os.fdopen(fd) as handle:
        after = os.fstat(handle.fileno())
        if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino) or not stat.S_ISREG(after.st_mode):
            raise ValueError(f"{shown} changed while it was being opened")
        return handle.read()


def read_lane(common_dir, name):
    """The validated record of lane `name`. Every directory from the common git directory down is opened without
    following a symlink and the file is lstat'ed first, so anything that is not a real directory / regular file is
    refused unopened. Every failure names the file. A record whose git_common_dir is not this repository's refuses (it
    was copied from elsewhere). Validates format only; see the module docstring."""
    path = lane_path(common_dir, name)
    try:
        fd = _walk(common_dir, LANES_SUBDIR.parts + (name,))
        if fd is None:
            raise ValueError("there is no such lane directory")
        try:
            text = _read_regular(fd, "lane.json", path)
        finally:
            os.close(fd)
        record = lane_record(json.loads(text), name)
        if record["git_common_dir"] != os.path.realpath(common_dir):
            raise ValueError(f"it belongs to the repository at {record['git_common_dir']}")
    except (OSError, ValueError, RecursionError) as error:
        raise ValueError(f"lane {name} has an unreadable record {path} ({error}); it is not used. Fix the file, or retire the lane (README.md, Lane recovery)")
    return record


# (lanes directory identity) -> (process id, thread id) that holds the lock. Cleared in a forked child, which does not
# own the parent's lock.
_held = {}
os.register_at_fork(after_in_child=_held.clear)


def _me():
    return (os.getpid(), threading.get_ident())


@contextlib.contextmanager
def lane_lock(common_dir):
    """Hold the repository's lane lock (flock on lanes/.lock) for a read-modify-write of lane records. Another thread or
    process waits its turn; taking it again in the thread that holds it refuses (it would never return). write_lane
    refuses unless the calling thread holds the lock."""
    dir_fd = _walk(common_dir, LANES_SUBDIR.parts, create=True)
    try:
        key = _dir_key(dir_fd)
        if _held.get(key) == _me():
            raise ValueError("the lane lock is already held by this thread")
        try:
            fd = os.open(LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
        except OSError as error:
            raise _fail_os(error, lanes_dir(common_dir) / LOCK_NAME)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            _held[key] = _me()
            try:
                yield
            finally:
                _held.pop(key, None)
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)


def write_lane(common_dir, record):
    """Validate and write the record atomically; returns the validated record. Must be called inside lane_lock by the
    thread that took it. It refuses a record of another repository, a lane directory that is not a real directory and a
    lane.json that is not a regular file. It does not read the record it replaces."""
    record = lane_record(record)
    path = lane_path(common_dir, record["name"])
    try:
        lanes_fd = _walk(common_dir, LANES_SUBDIR.parts)
        try:
            if lanes_fd is None or _held.get(_dir_key(lanes_fd)) != _me():
                raise ValueError("write_lane needs the lane lock (lane_lock) held by this thread")
            if record["git_common_dir"] != os.path.realpath(common_dir):
                raise ValueError(f"the record belongs to the repository at {record['git_common_dir']}, not {common_dir}")
            with contextlib.suppress(FileExistsError):
                os.mkdir(record["name"], 0o777, dir_fd=lanes_fd)
            dir_fd = _open_dir(lanes_fd, record["name"], path.parent)
        finally:
            if lanes_fd is not None:
                os.close(lanes_fd)
        try:
            try:
                if not stat.S_ISREG(os.lstat("lane.json", dir_fd=dir_fd).st_mode):
                    raise ValueError(f"{path} exists and is not a regular file")
            except FileNotFoundError:
                pass
            tmp = f".lane.json.{secrets.token_hex(6)}"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
            try:
                with os.fdopen(fd, "w") as out:
                    json.dump(record, out, indent=2)
                    out.write("\n")
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(tmp, "lane.json", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp, dir_fd=dir_fd)
                raise
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as error:
        raise _fail_os(error, path)
    return record


def read_lane_file(common_dir, name, filename):
    """The text of lanes/<name>/<filename> (a file kept beside the record, e.g. the lease), or None when the lane directory or
    the file does not exist. Reached with no symlink followed; anything else that is wrong with it refuses naming the file."""
    path = lane_path(common_dir, name).parent / filename
    try:
        fd = _walk(common_dir, LANES_SUBDIR.parts + (name,))
        if fd is None:
            return None
        try:
            return _read_regular(fd, filename, path)
        except FileNotFoundError:
            return None
        finally:
            os.close(fd)
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot read {path} ({error})")


def write_lane_file(common_dir, name, filename, text):
    """Replace lanes/<name>/<filename> atomically with `text`, or remove it when `text` is None. Same lock rule as write_lane;
    the lane's directory must exist (the lane has a record), and an existing entry that is not a regular file refuses."""
    path = lane_path(common_dir, name).parent / filename
    try:
        lanes_fd = _walk(common_dir, LANES_SUBDIR.parts)
        try:
            if lanes_fd is None or _held.get(_dir_key(lanes_fd)) != _me():
                raise ValueError("write_lane_file needs the lane lock (lane_lock) held by this thread")
            try:
                dir_fd = _open_dir(lanes_fd, name, path.parent)
            except FileNotFoundError:
                raise ValueError(f"lane {name} has no directory {path.parent}")
        finally:
            if lanes_fd is not None:
                os.close(lanes_fd)
        try:
            try:
                if not stat.S_ISREG(os.lstat(filename, dir_fd=dir_fd).st_mode):
                    raise ValueError(f"{path} exists and is not a regular file")
                exists = True
            except FileNotFoundError:
                exists = False
            if text is None:
                if exists:
                    os.unlink(filename, dir_fd=dir_fd)
                    os.fsync(dir_fd)
                return
            tmp = f".lane.json.{secrets.token_hex(6)}"  # discard_lane_record sweeps this name too
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
            try:
                with os.fdopen(fd, "w") as out:
                    out.write(text)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(tmp, filename, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp, dir_fd=dir_fd)
                raise
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as error:
        raise _fail_os(error, path)


def discard_lane_record(common_dir, name):
    """Undo a record write that this process just made: remove lanes/<name>/lane.json, any temporary file left beside it
    and the then-empty directory. Same lock rule as write_lane. Only regular files are unlinked; a directory that holds
    anything else is left and refuses. Used by creation's rollback; closed lanes are archived by later slices, not here."""
    path = lane_path(common_dir, name)
    try:
        lanes_fd = _walk(common_dir, LANES_SUBDIR.parts)
        try:
            if lanes_fd is None or _held.get(_dir_key(lanes_fd)) != _me():
                raise ValueError("discard_lane_record needs the lane lock (lane_lock) held by this thread")
            try:
                dir_fd = _open_dir(lanes_fd, name, path.parent)
            except FileNotFoundError:
                return
            try:
                for entry in os.listdir(dir_fd):
                    if entry != "lane.json" and not entry.startswith(".lane.json."):
                        raise ValueError(f"{path.parent} holds {entry}, which is not part of the record; left in place")
                    if not stat.S_ISREG(os.lstat(entry, dir_fd=dir_fd).st_mode):
                        raise ValueError(f"{path.parent}/{entry} is not a regular file; left in place")
                    os.unlink(entry, dir_fd=dir_fd)
            finally:
                os.close(dir_fd)
            os.rmdir(name, dir_fd=lanes_fd)
        finally:
            if lanes_fd is not None:
                os.close(lanes_fd)
    except OSError as error:
        raise _fail_os(error, path)


def archive_lane_record(common_dir, name, stamp):
    """Move lanes/<name> (the record, its lease and anything else beside them) to lanes/archive/<name>-<stamp>/ and return
    the new path. The record is kept, never deleted; lane_names skips `archive`, so the name is free again. Same lock rule as
    write_lane. An existing archive entry of that name is never replaced: a counter is added."""
    path = lane_path(common_dir, name).parent
    try:
        lanes_fd = _walk(common_dir, LANES_SUBDIR.parts)
        try:
            if lanes_fd is None or _held.get(_dir_key(lanes_fd)) != _me():
                raise ValueError("archive_lane_record needs the lane lock (lane_lock) held by this thread")
            if not stat.S_ISDIR(os.lstat(name, dir_fd=lanes_fd).st_mode):
                raise ValueError(f"{path} is not a directory")
            with contextlib.suppress(FileExistsError):
                os.mkdir("archive", 0o777, dir_fd=lanes_fd)
            archive_fd = _open_dir(lanes_fd, "archive", path.parent / "archive")
            try:
                label, count = f"{name}-{stamp}", 1
                while True:
                    try:
                        os.lstat(label, dir_fd=archive_fd)
                    except FileNotFoundError:
                        break
                    count += 1
                    label = f"{name}-{stamp}-{count}"
                os.rename(name, label, src_dir_fd=lanes_fd, dst_dir_fd=archive_fd)
                os.fsync(archive_fd)
            finally:
                os.close(archive_fd)
            os.fsync(lanes_fd)
        finally:
            if lanes_fd is not None:
                os.close(lanes_fd)
    except OSError as error:
        raise _fail_os(error, path)
    return path.parent / "archive" / label


# ---------------------------------------------------------------- the record against git

def worktree_identity(common_dir, worktree):
    """Prove that `worktree` (a canonical path) is the linked worktree this repository registered, not just a directory
    at a registered path: its .git is the plain file git writes (not a symlink or directory), it points into
    <common>/worktrees/, that entry points back at this .git, and git run in it finds the same git directory and the
    same common directory. ValueError names the fault."""
    dot_git = os.path.join(worktree, ".git")
    try:
        if not stat.S_ISREG(os.lstat(dot_git).st_mode):
            raise ValueError(f"{dot_git} is not the plain file git writes for a linked worktree")
        with open(dot_git) as handle:
            first = handle.readline().strip()
        if not first.startswith("gitdir: "):
            raise ValueError(f"{dot_git} does not hold a gitdir line")
        pointer = first[len("gitdir: "):]
        entry = os.path.realpath(pointer if os.path.isabs(pointer) else os.path.join(worktree, pointer))
        if os.path.dirname(entry) != os.path.join(common_dir, "worktrees"):
            raise ValueError(f"{dot_git} points to {entry}, which is not a worktree entry of the repository at {common_dir}")
        with open(os.path.join(entry, "gitdir")) as handle:
            back = handle.readline().strip()
        if os.path.realpath(back) != dot_git:
            raise ValueError(f"the repository's entry {entry} belongs to {back}, not to {dot_git}")
    except OSError as error:
        raise ValueError(f"cannot read the worktree's git link ({error.__class__.__name__}: {error})")
    _, actual = _git(["rev-parse", "--absolute-git-dir"], worktree)
    if os.path.realpath(actual.strip()) != entry or git_common_dir(worktree) != common_dir:
        raise ValueError(f"git run in {worktree} finds {actual.strip()}, not the repository's entry {entry}")


def lane_reality(record, history_only=False):
    """Check a validated record against the repository; every mismatch refuses naming the lane and the fact, nothing is
    repaired. Returns {"tip": commit of the branch}. Checks: the repository is the recorded one; the worktree is a directory
    git lists and is that registered worktree (worktree_identity); it is on the recorded branch (a symbolic ref, not detached); the branch exists; base and every closed
    commit is an ancestor of the tip, in order; every closed tree is its commit's tree. `history_only` skips the checks of the
    worktree (a removal that was interrupted has left a tree that is not one any more; lane_remove checks what is left itself)
    and asks only the repository: the branch, the base and the closed commits."""
    record = lane_record(record)
    name, repo, worktree, branch = record["name"], record["repo"], record["worktree"], record["branch"]

    def fail(text):
        raise ValueError(f"lane {name}: {text}")

    try:
        if not os.path.isdir(repo) or git_common_dir(repo) != record["git_common_dir"]:
            fail(f"repository {repo} is not the recorded one ({record['git_common_dir']})")
        if main_worktree(record["git_common_dir"]) != repo:
            fail(f"{repo} is not the repository's main working tree")
        common = record["git_common_dir"]
        if not history_only:
            if not os.path.isdir(worktree):
                fail(f"worktree {worktree} does not exist; restore it, or retire the lane by hand (README.md, Lane recovery)")
            if os.path.realpath(worktree) != worktree or worktree not in registered_worktrees(common):
                fail(f"worktree {worktree} is not registered with git")
            try:
                worktree_identity(common, worktree)
            except ValueError as error:
                fail(f"worktree {worktree} is not the worktree git registered ({error})")
            _, head = _git(["symbolic-ref", "-q", "HEAD"], worktree, ok=(0, 1))
            actual = head.strip()
            if actual != f"refs/heads/{branch}":
                fail(f"worktree {worktree} is on {actual[len('refs/heads/'):] if actual.startswith('refs/heads/') else 'a detached HEAD'}, "
                     f"expected {branch}; check it out again, or retire the lane by hand (README.md, Lane recovery)")
        # From here on ask the repository itself (the common git dir), never the worktree directory.
        code, out = _git(["rev-parse", "--verify", "-q", f"refs/heads/{branch}^{{commit}}"], common, ok=(0, 1))
        if code:
            fail(f"branch {branch} does not exist")
        tip = out.strip()

        def commit_exists(sha):
            return _git(["cat-file", "-e", f"{sha}^{{commit}}"], common, ok=(0, 1, 128))[0] == 0

        def ancestor(older, newer):
            return _git(["merge-base", "--is-ancestor", older, newer], common, ok=(0, 1))[0] == 0

        previous, previous_what = record["base"]["commit"], "base"
        if not commit_exists(previous) or not ancestor(previous, tip):
            fail(f"base {previous} is not an ancestor of branch {branch}")
        for entry in record["closed"]:
            what = f"closed step {entry['step']}"
            if not commit_exists(entry["commit"]) or not ancestor(entry["commit"], tip):
                if commit_exists(entry["commit"]):  # moved off by a reset or a rewrite: the commit is still there to go back to
                    back = (f"git -C {worktree} stash push -u -m \"lane {name}\"; git -C {worktree} branch saved/{name}-{tip[:8]} {tip} && "
                            f"git -C {worktree} reset --hard {entry['commit']}")
                    fail(f"{what} commit {entry['commit']} is not on branch {branch}; put the branch back on it, keeping what is there now "
                         f"(`{back}`)")
                fail(f"{what} commit {entry['commit']} is not on branch {branch} and no longer exists (README.md, Lane recovery)")
            if not ancestor(previous, entry["commit"]):
                fail(f"{what} commit {entry['commit']} does not follow {previous_what} (README.md, Lane recovery)")
            _, tree = _git(["rev-parse", f"{entry['commit']}^{{tree}}"], common)
            if tree.strip() != entry["tree"]:
                fail(f"{what} records tree {entry['tree']} but its commit's tree is {tree.strip()}; lane.json was changed by hand "
                     f"(README.md, Lane recovery)")
            previous, previous_what = entry["commit"], what
    except ValueError as error:
        if str(error).startswith(f"lane {name}:"):
            raise
        raise ValueError(f"lane {name}: cannot check the record against git ({error})")
    return {"tip": tip}


# ---------------------------------------------------------------- which lane owns a directory

def _checkout_tops(real):
    """Every directory at or above `real` that holds a `.git` entry (a repository's main tree, a linked worktree, a nested
    repository or a submodule), nearest first. Found by looking at the filesystem, so a git failure cannot turn into "not in a
    repository"."""
    tops, current = [], real
    while True:
        if os.path.lexists(os.path.join(current, ".git")):
            tops.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            return tops
        current = parent


def lane_containing(path):
    """(record, unrecorded) for the directory `path`: `record` is the validated record of the lane whose worktree is `path` or
    contains it, else None; `unrecorded` is True when the nearest checkout holding `path` is a linked worktree that no lane
    records (a hand-made one, allowed with a warning). A directory in no repository gives (None, False).

    EVERY checkout above `path` is asked, not only the nearest: a nested repository or a submodule inside a lane's tree has a
    git directory of its own, and its own lane records say nothing about the tree around it, but the enclosing checkout is
    the lane's linked worktree and its repository's records do. The path is resolved through symlinks first, so a link to
    a directory inside a lane's tree is that tree. Anything unreadable refuses: git failing in a checkout, a lane record
    that cannot be read (it may hold this very tree). Nothing is created or changed."""
    real = os.path.realpath(path)
    tops = _checkout_tops(real)
    if not tops:
        return None, False
    seen = set()
    for top in tops:
        common = git_common_dir(top)
        if common in seen:
            continue
        seen.add(common)
        for name in lane_names(common):
            record = read_lane(common, name)
            if _inside(real, os.path.realpath(record["worktree"])):
                return record, False
    return None, os.path.realpath(tops[0]) != main_worktree(git_common_dir(tops[0]))


def refuse_lane_tree(path, step_flag="--step"):
    """Refuse a dispatch that names no lane but whose working tree is a lane's: it would be a second way to write there,
    around the one-writer rule. Returns the warnings for a hand-made (unrecorded) linked worktree, which is allowed."""
    record, unrecorded = lane_containing(path)
    if record is not None:
        raise ValueError(f"{path} is inside lane {record['name']}'s working tree; pass --lane {record['name']} {step_flag} ID")
    if unrecorded:
        return [{"kind": "unrecorded-worktree", "path": os.path.realpath(path),
                 "note": "a linked git worktree that no lane records; it is used as it is, with no lane rules"}]
    return []


# ---------------------------------------------------------------- configuration

def _read_config(path, what, absent_ok=False):
    """The parsed JSON object, or None when `absent_ok` and the file does not exist. A file that exists but is not a
    regular file, cannot be read or is not a JSON object refuses."""
    try:
        try:
            info = Path(path).lstat()
        except FileNotFoundError:
            if absent_ok:
                return None
            raise
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("it is not a regular file")
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError, RecursionError) as error:
        raise ValueError(f"cannot read {what} {path} ({error})")
    if not isinstance(value, dict):
        raise ValueError(f"{what} {path} is not an object")
    return value


def _positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def load_machine_config(path=None):
    """The machine-level lane settings from config/orchestration.json "lanes": {"max_active_lanes": N}. An absent "lanes"
    block or key takes the default (2) and is listed in "defaulted"; a block that is not an object, a value that is not a
    positive integer, or an unreadable file refuses. Returns {"max_active_lanes": N, "defaulted": [...]}."""
    path = Path(path) if path else MACHINE_CONFIG
    shown = "config/orchestration.json"
    cfg = _read_config(path, "the orchestration config")
    block = cfg.get("lanes", {})
    if not isinstance(block, dict):
        raise ValueError(f'{shown} "lanes" must be an object, got {block!r}')
    if "max_active_lanes" not in block:
        return {"max_active_lanes": DEFAULT_MACHINE["max_active_lanes"], "defaulted": ["max_active_lanes"]}
    if not _positive_int(block["max_active_lanes"]):
        raise ValueError(f"{shown} lanes.max_active_lanes must be a positive integer, got {block['max_active_lanes']!r}")
    return {"max_active_lanes": block["max_active_lanes"], "defaulted": []}


def load_repo_config(repo):
    """The per-repository lane settings from <repo main working tree>/.tmux-pi/lanes.json: branch_prefix,
    worktree_pattern ({repo} = the repository folder name, {name} = the lane name), integration_branch, integration_lane.
    branch_prefix may be "" (every other key must be non-empty).
    A file that does not exist, or a key that is absent, takes its default (DEFAULT_REPO) and is listed in "defaulted";
    a key that is present but malformed or of the wrong type, or a file that exists but cannot be read, refuses naming
    key and file. Optional and validated when present: owned (path prefixes only the orchestrator edits) and tests
    ({level: {argv, timeout_s}}). Other keys (setup, clone, ...) belong to later slices and are ignored. `repo` is the
    main working tree."""
    path = Path(repo) / REPO_CONFIG
    cfg = _read_config(path, "the repository lane config", absent_ok=True)
    defaulted = []

    def text(key, may_be_empty=False):
        if cfg is None or key not in cfg:
            defaulted.append(key)
            return DEFAULT_REPO[key]
        value = cfg[key]
        if isinstance(value, str) and value == "" and may_be_empty:
            return value
        if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError(f"{path} {key} must be non-empty text, got {value!r}")
        return value

    cfg_has = cfg or {}
    prefix = text("branch_prefix", may_be_empty=True)  # "" gives branches named exactly like the lane
    if not valid_branch(prefix + "x"):
        raise ValueError(f"{path} branch_prefix {prefix!r} does not make valid branch names")
    pattern = text("worktree_pattern")
    if re.sub(r"\{(?:repo|name)\}", "", pattern).count("{") or re.sub(r"\{(?:repo|name)\}", "", pattern).count("}"):
        raise ValueError(f"{path} worktree_pattern {pattern!r} may use only the placeholders {{repo}} and {{name}}")
    if pattern.count("{name}") != 1:
        raise ValueError(f"{path} worktree_pattern {pattern!r} must contain {{name}} exactly once")
    if not (pattern.startswith("/") or pattern.startswith("{repo}")):
        raise ValueError(f"{path} worktree_pattern {pattern!r} must be absolute or start with {{repo}}")
    branch = text("integration_branch")
    if not valid_branch(branch):
        raise ValueError(f"{path} integration_branch {branch!r} is not a valid branch name")
    integration = text("integration_lane")
    if not valid_lane_name(integration):
        raise ValueError(f"{path} integration_lane {integration!r} is not a valid lane name")
    clean = {"branch_prefix": prefix, "worktree_pattern": pattern, "integration_branch": branch, "integration_lane": integration}
    if "owned" in cfg_has:
        try:
            clean["owned"] = path_prefixes(cfg_has["owned"], "owned", maximum=1000)
        except ValueError as error:
            raise ValueError(f"{path} {error}")
    if "tests" in cfg_has:
        tests = cfg_has["tests"]
        if not isinstance(tests, dict) or not tests:
            raise ValueError(f"{path} tests must be an object of test levels")
        clean["tests"] = {}
        for level, spec in tests.items():
            if not TEST_LEVEL.fullmatch(level):
                raise ValueError(f"{path} tests level {level!r} is not a valid name")
            argv = spec.get("argv") if isinstance(spec, dict) else None
            if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
                raise ValueError(f"{path} tests.{level}.argv must be a non-empty list of non-empty strings")
            if not _positive_int(spec.get("timeout_s")):
                raise ValueError(f"{path} tests.{level}.timeout_s must be a positive integer, got {spec.get('timeout_s')!r}")
            clean["tests"][level] = {"argv": list(argv), "timeout_s": spec["timeout_s"]}
    clean["defaulted"] = sorted(defaulted)
    return clean


def defaulted_values(*configs):
    """The sorted names a lane record stores in "defaulted": every value any of the loaded configurations defaulted."""
    return sorted({key for config in configs for key in config["defaulted"]})


# ---------------------------------------------------------------- names and the precheck

def explicit_branch(branch):
    """`branch` (a lane's --branch) when `git check-ref-format --branch` accepts it as written, else ValueError."""
    run = None
    if isinstance(branch, str) and branch and not branch.startswith("-") and "\n" not in branch and "\x00" not in branch:
        run = subprocess.run(["git", "check-ref-format", "--branch", branch], capture_output=True, text=True)
    if run is None or run.returncode != 0 or run.stdout.strip() != branch:
        raise ValueError(f"--branch {branch!r} is not a valid branch name")
    return branch


def lane_branch(repo_config, name, integration=False, branch=None):
    """The branch for lane `name`: `branch` when one is given explicitly (a work lane only), else the configured prefix
    plus the name. The integration lane's name is reserved: a work
    lane may not have it, and with integration=True only that name is accepted and the integration branch is returned.
    A work branch equal to the integration branch refuses."""
    if not valid_lane_name(name):
        raise ValueError(f"invalid lane name {name!r}")
    reserved = repo_config["integration_lane"]
    if integration:
        if branch is not None:
            raise ValueError("the integration lane's branch is the configured integration branch; --branch does not apply")
        if name != reserved:
            raise ValueError(f"the integration lane is named {reserved!r}, not {name!r}")
        return repo_config["integration_branch"]
    if name == reserved:
        raise ValueError(f"the lane name {name!r} is reserved for the integration lane")
    branch = explicit_branch(branch) if branch is not None else repo_config["branch_prefix"] + name
    if branch == repo_config["integration_branch"]:
        raise ValueError(f"branch {branch!r} is the integration branch; choose another lane name")
    if not valid_branch(branch):
        raise ValueError(f"branch {branch!r} is not a valid branch name")
    return branch


def lane_worktree(repo_config, repo, name, integration=False):
    """The worktree path for lane `name`: the configured pattern with {repo} (the repository FOLDER NAME, the last part of
    `repo`) and {name} filled in. A pattern that starts with {repo} is placed in the folder that holds the repository, so
    two repositories with the same folder name in different parents get different worktrees. `repo` is the main working
    tree's realpath. The result must be a canonical path (no symlink), outside the repository and not inside its git
    directory. Same reserved-name rule as lane_branch. Does not check the path is free (precheck_lane_slot)."""
    if not valid_lane_name(name):
        raise ValueError(f"invalid lane name {name!r}")
    reserved = repo_config["integration_lane"]
    if integration != (name == reserved):
        raise ValueError(f"the integration lane is named {reserved!r}, not {name!r}" if integration
                         else f"the lane name {name!r} is reserved for the integration lane")
    if not isinstance(repo, str) or not repo.startswith("/") or os.path.realpath(repo) != repo or repo == "/":
        raise ValueError(f"repo {repo!r} is not a canonical absolute path")
    pattern = repo_config["worktree_pattern"]
    path = re.sub(r"\{(repo|name)\}", lambda m: os.path.basename(repo) if m.group(1) == "repo" else name, pattern)
    if pattern.startswith("{repo}"):
        path = os.path.join(os.path.dirname(repo), path)
    if not path.startswith("/") or os.path.normpath(path) != path:
        raise ValueError(f"worktree_pattern gives {path!r}, which is not a normalised absolute path")
    if os.path.realpath(path) != path:
        raise ValueError(f"worktree {path!r} is not a canonical path (it goes through a symlink; the realpath is {os.path.realpath(path)!r})")
    if path == "/" or _inside(path, repo) or _inside(repo, path):
        raise ValueError(f"worktree {path!r} must be a directory outside the repository {repo!r}")
    return path


def _branch_in_use(common_dir, branch):
    """The existing branch that is `branch` or conflicts with it (a ref that is a parent or a child of its path), or None."""
    _, out = _git(["for-each-ref", "--format=%(refname)", "refs/heads/"], common_dir)
    ref = f"refs/heads/{branch}"
    for existing in out.splitlines():
        if existing == ref or existing.startswith(ref + "/") or ref.startswith(existing + "/"):
            return existing[len("refs/heads/"):]
    return None


def _case_clash(common_dir, branch):
    """An existing local branch that equals `branch` or conflicts with its path when case is ignored, or None."""
    _, out = _git(["for-each-ref", "--format=%(refname:short)", "refs/heads/"], common_dir)
    low = branch.lower()
    for existing in out.splitlines():
        e = existing.lower()
        if e == low or e.startswith(low + "/") or low.startswith(e + "/"):
            return existing
    return None


def precheck_lane_slot(common_dir, repo_config, name, integration=False, branch=None):
    """Before anything is created for lane `name`: render its branch and worktree and refuse if either is already in use,
    so an existing branch or directory is never adopted. Refuses when: a record for the lane exists or another record
    uses the branch or worktree; the branch exists (or conflicts with an existing branch's path); the worktree path
    exists or git lists it; the worktree is inside the repository (lane_worktree). A record that cannot be read refuses,
    because it may hold the slot. An explicit `branch` is also refused when it is the main checkout's current branch, or
    matches an existing local branch or another lane's branch ignoring case. Returns {"branch", "worktree"}. Nothing is
    created or changed."""
    common_dir = os.path.realpath(common_dir)
    repo = main_worktree(common_dir)
    explicit = branch is not None
    branch = lane_branch(repo_config, name, integration, branch)
    worktree = lane_worktree(repo_config, repo, name, integration)
    if explicit:
        _, current = _git(["symbolic-ref", "-q", "--short", "HEAD"], repo, ok=(0, 1))
        if branch == current.strip():
            raise ValueError(f"branch {branch!r} is the main checkout's current branch; choose another --branch")
    taken = lane_names(common_dir)
    if name in taken:
        raise ValueError(f"lane {name} already has a record in {lanes_dir(common_dir)}")
    for other in taken:
        record = read_lane(common_dir, other)
        if record["branch"] == branch or record["worktree"] == worktree or (explicit and record["branch"].lower() == branch.lower()):
            raise ValueError(f"lane {other} already uses the branch {branch} or the worktree {worktree}")
    clash = _branch_in_use(common_dir, branch)
    if clash is None and explicit:
        clash = _case_clash(common_dir, branch)
    if clash is not None:
        raise ValueError(f"branch {branch} cannot be used: branch {clash} already exists" if clash != branch
                         else f"branch {branch} already exists; it is not adopted")
    if os.path.lexists(worktree):
        raise ValueError(f"worktree {worktree} already exists; it is not adopted")
    if worktree in registered_worktrees(common_dir):
        raise ValueError(f"git already lists {worktree} as a worktree")
    return {"branch": branch, "worktree": worktree}


def active_lane_names(common_dir):
    """Names of the work lanes in use (state open or integrating), read from every record in the common git directory, so it
    covers every session of the repository. A record that cannot be read refuses (the count would be unknown)."""
    common_dir = os.path.realpath(common_dir)
    records = [read_lane(common_dir, name) for name in lane_names(common_dir)]
    return sorted(r["name"] for r in records if r["kind"] == "work" and r["state"] in ("open", "integrating"))


def active_lane_count(common_dir):
    return len(active_lane_names(common_dir))


def check_lane_capacity(common_dir, max_active_lanes):
    """Refuse when creating one more work lane would exceed `max_active_lanes` (see active_lane_names)."""
    names = active_lane_names(common_dir)
    if len(names) >= max_active_lanes:
        raise ValueError(f"{len(names)} lanes are open (limit {max_active_lanes}): {', '.join(names)}. Retire one first: tp-agent lane integrate and lane remove ship in a later slice, "
                         "so today follow README.md, Lane recovery, Retiring a lane by hand")
    return len(names)


if __name__ == "__main__":  # tp-spawn: check-tree <workdir> (no --lane was given: refuse a lane's tree) | main-checkout <workdir>
    import sys
    try:
        if len(sys.argv) != 3 or sys.argv[1] not in ("check-tree", "main-checkout"):
            raise ValueError("usage: lanes.py check-tree|main-checkout <workdir>")
        if sys.argv[1] == "check-tree":
            refuse_lane_tree(sys.argv[2], "--lane-step")
        else:  # the repository's main working tree, where the preamble of a lane's tasks is read
            print(main_worktree(git_common_dir(sys.argv[2])))
    except ValueError as error:
        print(f"tp-spawn: {error}", file=sys.stderr)
        sys.exit(1)
