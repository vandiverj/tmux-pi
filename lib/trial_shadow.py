"""Shadow dispatch (design §3): run Qwen on the same brief a normal coder got, in a throwaway worktree, inside its own trial
session, review its diff and write a record. The real lane, step, branch and Main's state dir are never touched.

    prepare(...)   validate, check the guard, create the run (run.json) and its session; nothing is started yet
    launch(...)    start `bin/tp-trial run <run>` detached, so the caller is never blocked
    execute(...)   the run itself (what `tp-trial run` calls): worktree at base_commit, shared deps, tp-spawn, wait, HEAD to the base,
                   review, cleanup, records, bounded by trial.attempt_timeout_s; the outcome is decided at the end by
                   one rule read from evidence (`Run.execute`), and only `completed` exits 0
    cleanup(...)   idempotent: end running trial tasks, drop the worktree and the shadow refs, end the session
    stop(...)      end a run early

Everything a run writes is under <project>/.tmux-pi/trial/<run>/ (run.json, request.md, final.patch, state/) and the shared
<project>/.tmux-pi/trial/records.jsonl. Main's state dir is read in two places only, both reads: the source task's files
(`read_source`, and `record_normal` for the normal arm's record) and the local-slot count of the guard.

What the run does to git: `git worktree add --detach` at the source task's base_commit (nothing under refs/heads), a temporary
ref refs/tmux-pi/shadow/<run>/head that keeps the worker's commits reachable until the cleanup, and removal of both at
the end, on success and on failure. The run deletes no branch, ever: it created none, so none is its to delete. If the worker left
the tree on a branch (`git checkout -b`, a rename, a branch someone else made meanwhile), the collect step detaches HEAD (so moving
it to the base cannot move that branch), leaves the branch where it is and reports it as `branch_left` {ref, commit} in run.json (every
branch made since the tree was, HEAD's or not, is listed there as `new_branches`, read only from the project and git directory
the run recorded, else `new_branches_error`); a
tree it cannot detach ends the collect with an error before HEAD is moved. HEAD is written with `update-ref --no-deref`, never
`checkout --detach` or `reset --soft`, which refuse in the middle of a merge, a rebase or a cherry-pick: the patch and the removal of
the tree do not depend on the state the worker left git in. `git add -A` skips a path git cannot index (a repository the worker made
inside the tree) and names it as `unindexed` in run.json; the worktree is removed with a rmtree that makes a read-only directory
writable first; git's output is decoded with errors replaced, and the patch is kept as bytes. The refs the run does make (the one above) are made
create-only, written to a ledger in the owner record (`refs`: name -> commit) and removed only when the ledger names them and they
still name the recorded commit; any other ref under the run's prefix, or one at another commit, is kept and reported by the
cleanup. A tree already on a branch that existed before the run (`heads_before`) is refused as damaged, never reset.

Ownership (the worker runs as the same OS user, is not sandboxed, and can write run.json, its task's meta.json and everything beside
its tree, so nothing there proves anything). Every destructive act (a signal, a tmux kill, a worktree removal, a write into a run
directory) first matches an identity this attempt recorded itself; the full rule is on `Owner`. In short: `prepare` records the
identities of the run directory, the state dir and the tmux session before any worker exists; the runner holds the directories open
and writes run.json through them; a session is killed by its recorded id, and only while that id still has the recorded name and
state dir; every tp-* command the run starts goes through one gate (`trial_session.start_tp` and its table TP, called by
`Owner.tp`; `Run.agent` and `Run.command` are the run's way to it): one the table does not name is refused and never started, and
one that can end anything (tp-agent end-author, stop and review, tp-spawn) runs only after `Owner.check_stop` passed: the session
the name resolves to is the run's own with @tp_viewing unset, the meta.json pane is one the run recorded in its own session, a
process.json matches the process the run recorded for that pane, and for `stop` no queue entry is there (otherwise nothing is run
and the session kill ends the pane); a prepare that fails ends only the session it made and removes only the run dir it made, each
by the identity it recorded when it made it; the worktree is an attempt-unique place
`<run dir>/wt-<id>`, proven by the (device, inode) of the tree and of its git admin dir, both held open. Project, state dir, session
and task id come from where the run dir is, never from run.json. Whatever does not match is left as it is. The attempt clock (§8) is
checked before the worktree and before tp-spawn.

Nothing the worker writes can hold the runner or change what it does: the runner's settings are run.json as it was when the
runner began (before any worker), plus what it noted itself, never the file read back (`Run.note`); a file the worker can write is
read only as a plain file, never waiting on a FIFO (`trial_session.read_plain`); a file is replaced through a temp file made new
(`write_at`), never written through what is at the temp name; run.json's lock is waited for at most LOCK_WAIT_S; stopping tasks
before the session kill has one budget, STOP_TASKS_S, however many task dirs there are; a base_commit from run.json reaches git only
as a commit id.

Every read of a file the worker can write (run.json, the task's meta.json, process.json and dispatch.json, the source task's files, the
worktree's .git file and the admin dir's gitdir, the owner record, and, in lib/trial_record.py, the task, reviewer, result and
transcript files) goes through `trial_session.read_plain`: opened O_NONBLOCK | O_NOFOLLOW, required to be a regular file of at most
MAX_READ bytes (8 MiB), else it is treated as missing or invalid (a FIFO, a link, a directory or a huge file never holds the runner,
so the session kill always runs). The record file is appended to only after `append_record` found its name and its lock's name
free of anything but a regular file (`trial_record.append` opens them blocking, and a FIFO there would hold it).

Known limits (non-hostile worker model). The worker is taken to make mistakes (a wrong branch, stray files, odd git state, a file
of an odd type made by accident); it is not taken to forge records or to attack the runner. These are known and not defended; the
planned fix is to run the worker as a separate macOS user, which this module does not do:
  F1 worktree ownership: a stop of a runner that has died reads the run's identities from the owner file
     `<project>/.tmux-pi/trial-owner/<run>.json`, which the same OS user, so the worker, can rewrite; a forged owner record can make
     `tp-trial stop` remove another lane's worktree.
  F3 git execution boundary: the runner's own git commands (`add -A`, `reset`, `diff`, ...) run in the worker's tree, so a git
     filter, hook or config the worker set there can run during them.
  F4 review workdir isolation: tp-review takes its working directory from the trial task's meta.json, which the worker can write; an
     edited `workdir` can point it at another checkout.
  F6 record integrity: `tp-trial normal` rereads run.json, which the worker can write, and the normal arm's record takes its
     base commit, source task and pair id from it.
Also not defended: a check is not atomic with the command after it (between `check_stop` and the tp-agent command that reads them,
the worker can rewrite meta.json and process.json or the session's options; see `Owner`); the tp-* commands the run starts read the
task's files themselves and are bounded only by CMD_TIMEOUT.

The record comes from lib/trial_record.py (lane L2): `build` for a task the worker left, `no_task` for a spawn that made none. Both
give the keys lib/trial_report.py reads (the first review's outcome, size, gate_runs, ...); `gate_runs` is L2's field and is never
read here.
"""

import datetime
import errno
import fcntl
import importlib
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lane_deps  # noqa: E402
import lanes  # noqa: E402
import trial_session  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
COMMIT = re.compile(r"^[0-9a-f]{7,64}$")
TESTS = ("affected", "full", "none")
SPEC = re.compile(r"^(.+):([0-9]+)-([0-9]+)$")
REF_PREFIX = "refs/tmux-pi/shadow/"
UNINDEXED = re.compile(r"^error: (unable to index file '.*'|'.*' does not have a commit checked out)$")  # what `git add` says of a path it cannot index
PLACE = re.compile(r"^wt-[0-9a-f]{12}$")  # the attempt-unique name of a run's worktree
OWNER_DIR = "trial-owner"
NOT_OURS = "not the tree this run created"
SETTLED = ("settled", "completed")
OTHER_LANES = "## Paths other lanes own"
GIT_TIMEOUT = 120
CMD_TIMEOUT = 300
LOCK_WAIT_S = 30  # the longest the runner waits for run.json's lock (`_take_lock`)
STOP_TASKS_S = CMD_TIMEOUT  # the longest the cleanup spends stopping tasks through tp-agent before it ends the session
# A trial-only line in the packet (design §12 question 2: the shadow skips tests that need the shared database, because two
# runs on one database would collide). Everything else in the packet is what the normal coder got.
TRIAL_NOTE = ("This is a trial run in a throwaway worktree. Do not run tests that need the shared database or any other shared "
              "service, and say that you skipped them.")

def workdir_text(worktree, project):
    """What the worker is told about where it works. Phase 0 found a shadow editing the main checkout because nothing named its
    working tree: the absolute path goes first in the request and again in the packet's notes."""
    return ("Your working directory is {wt}. Read and change repository files only there, using paths under {wt}. The main checkout "
            "{proj} is not yours: do not read it for the task's files or change anything in it.").format(wt=worktree, proj=project)


TERMINAL_OUTCOMES = ("completed", "timeout", "worker_failed", "paused", "stopped", "spawn_failed", "error", "record_failed", "no_review",
                     "review_timeout", "cleanup_failed")
REVIEW_VERDICTS = ("approved", "request_changes", "inconclusive")  # a first review's outcome that is a verdict (lib/trial_record.py OUTCOMES)


class ShadowError(Exception):
    """A step of the run cannot be done; the message says which and why."""


class AttemptExpired(ShadowError):
    """The attempt's wall clock (§8) was spent before the worker was started; nothing is launched."""


class Interrupted(BaseException):
    """The runner was told to stop (SIGTERM from `tp-trial stop`). A BaseException so a broad `except Exception` never eats it."""


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_record_module():
    """lib/trial_record.py (lane L2), imported when a record is first needed."""
    return importlib.import_module("trial_record")


def append_record(records, project, record):
    """`records.append`, but never into a name that is not a plain file: L2's append opens records.jsonl and its lock for writing and
    would wait for a reader at a FIFO, and the runner must go on to its cleanup. A name that is not there yet is made by the append."""
    directory = records.trial_dir(project)
    for name in (records.RECORDS, records.RECORDS + ".lock"):
        try:
            mode = os.lstat(str(directory / name)).st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(mode):
            raise ShadowError("{} is not a plain file; the record was not written".format(directory / name))
    records.append(project, record)


def record_rows(records, project, run, arm):
    """(count, None): how many rows of `arm` for run `run` records.jsonl has now, or (None, why it cannot be read). The completed
    rule reads the record back from here rather than trusting the append. The file is opened without waiting and never through a link,
    and must be a regular file (a FIFO there never holds the runner); it is read line by line, so a file of any size is counted. A
    line that is not a JSON object (a torn last line) is not a row."""
    path = records.trial_dir(project) / records.RECORDS
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return 0, None
    except OSError as error:
        return None, "{} cannot be opened as a plain file ({})".format(path, error.strerror)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None, "{} is not a plain file".format(path)
        count = 0
        with os.fdopen(os.dup(fd), "rb") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if isinstance(row, dict) and row.get("run") == run and row.get("arm") == arm:
                    count += 1
        return count, None
    except OSError as error:
        return None, "{} cannot be read ({})".format(path, error.strerror)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------------------------------------------- run.json

def run_file(rdir):
    return Path(rdir) / "run.json"


def read_run(rdir):
    try:
        value = json.loads(trial_session.read_plain(run_file(rdir)).decode("utf-8"))
    except (OSError, ValueError, RecursionError) as error:  # the worker can write run.json (a FIFO, nesting too deep)
        raise ShadowError("cannot read {}: {}".format(run_file(rdir), error.__class__.__name__ if isinstance(error, RecursionError) else error))
    if not isinstance(value, dict):
        raise ShadowError("{} is not an object".format(run_file(rdir)))
    return value


def update_run(rdir, **fields):
    """Merge `fields` into run.json under a lock, atomically (a reader never sees half a file). Returns the new content. The same
    as `update_run_at` (`tp-trial shadow` notes the runner's pid with it once the worker may be going): the lock is waited for at
    most LOCK_WAIT_S and nothing is read or written through a link."""
    try:
        fd = os.open(str(rdir), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise ShadowError("cannot open the run directory {}: {}".format(rdir, error))
    try:
        return update_run_at(fd, **fields)
    except OSError as error:
        raise ShadowError("cannot update {}: {}".format(run_file(rdir), error))
    finally:
        os.close(fd)


def _open_in(fd, name, flags, mode=0o644):
    """`name` inside the directory held open as `fd`, never through a link."""
    return os.open(name, flags | os.O_NOFOLLOW, mode, dir_fd=fd)


def read_run_at(fd):
    """run.json of the run directory held open as `fd`."""
    try:
        value = json.loads(trial_session.read_plain("run.json", dir_fd=fd).decode("utf-8"))
    except (OSError, ValueError, RecursionError) as error:  # the worker can write run.json: nesting too deep is unreadable too
        raise ShadowError("cannot read run.json: {}".format(error.__class__.__name__ if isinstance(error, RecursionError) else error))
    if not isinstance(value, dict):
        raise ShadowError("run.json is not an object")
    return value


TEMP_TRIES = 8


def write_at(fd, name, text):
    """Write `text` (str, or bytes written as they are) as `name` in the directory held open as `fd` (`name` a path and `fd` None: in its directory), atomically. The
    directory is the worker's to write and the temp name has the runner's pid, which run.json shows. A file or a link at the temp
    name is removed and the temp file is made new (O_EXCL), so nothing is written through a link the worker put there and no write
    waits on a FIFO. Anything that cannot be unlinked (a directory, and what is in it, is the worker's) is left alone: the next
    name, `<name>.tmp.<pid>.<n>`, is tried, TEMP_TRIES names in all, and then the write is an OSError."""
    name = str(name)
    binary = isinstance(text, bytes)
    last = None
    for attempt in range(TEMP_TRIES):
        tmp = "{}.tmp.{}{}".format(name, os.getpid(), ".{}".format(attempt) if attempt else "")
        try:
            os.unlink(tmp, dir_fd=fd)
        except FileNotFoundError:
            pass
        except OSError as error:
            last = error
            continue
        try:
            raw = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=fd)
        except FileExistsError as error:
            last = error
            continue
        with os.fdopen(raw, "wb" if binary else "w", **({} if binary else {"encoding": "utf-8"})) as handle:
            handle.write(text)
        os.rename(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
        return
    raise OSError("{}: every temp name is taken ({})".format(name, last))


def remove_tree(path):
    """`shutil.rmtree`, except that a directory the worker's tools made read-only (a module cache is) is made writable by its owner
    and the removal of what failed is tried again. Links are removed, never followed, and nothing outside `path` is changed."""
    def retry(function, failed, *_):
        for directory in (failed, os.path.dirname(failed)):
            if os.path.isdir(directory) and not os.path.islink(directory):
                os.chmod(directory, stat.S_IRWXU)
        function(failed)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


def _take_lock(lock, clock=time.monotonic, pause=time.sleep):
    """Take the run.json lock, waiting at most LOCK_WAIT_S. The lock file is in the run dir, so the worker can hold it: an
    unbounded wait would stop the runner, and with it the attempt clock and the cleanup. Raises ShadowError when it is not had."""
    deadline = clock() + LOCK_WAIT_S
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if clock() >= deadline:
                raise ShadowError("run.json is locked by another process (its .run.lock held for more than {} s); it was not changed".format(LOCK_WAIT_S))
            pause(0.05)


def update_run_at(fd, **fields):
    """`update_run` for the run directory held open as `fd`: the file changed is the one in that directory, whatever the path to it
    has become."""
    lock = _open_in(fd, ".run.lock", os.O_RDWR | os.O_CREAT | os.O_NONBLOCK)
    try:
        _take_lock(lock)
        try:
            current = read_run_at(fd)
        except ShadowError:
            if _exists_in(fd, "run.json"):  # only a file that is not there starts empty; one that cannot be read is an error
                raise
            current = {}
        current.update(fields)
        write_at(fd, "run.json", json.dumps(current, indent=2, sort_keys=True) + "\n")
    finally:
        os.close(lock)
    return current


def _exists_in(fd, name):
    try:
        os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


# --------------------------------------------------------------------------------------------------------------------- git

def git(cwd, *args, check=True, input_text=None, raw=False):
    """git in `cwd`. Output is text with undecodable bytes replaced (a commit message or a file the worker wrote in another encoding
    must not end the step with a decode error), or with `raw` the bytes as git wrote them (the patch)."""
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")}
    try:
        run = subprocess.run(["git", "-C", str(cwd)] + list(args), capture_output=True, timeout=GIT_TIMEOUT, env=env, input=input_text,
                             **({} if raw else {"text": True, "errors": "replace"}))
    except (subprocess.TimeoutExpired, OSError) as error:
        raise ShadowError("git {} could not run in {} ({})".format(" ".join(args[:2]), cwd, error.__class__.__name__))
    if check and run.returncode:
        stderr = run.stderr.decode("utf-8", "replace") if raw else run.stderr
        raise ShadowError("git {} failed in {}: {}".format(" ".join(args[:2]), cwd, stderr.strip() or "exit {}".format(run.returncode)))
    return run


def heads(project):
    return set(git(project, "for-each-ref", "--format=%(refname)", "refs/heads").stdout.split())


# -------------------------------------------------------------------------------------------------------------------- source

def find_task(state, task_id):
    """The directory of task `task_id` in the tmux-pi state dir `state` (active, else archived), or None."""
    if not isinstance(task_id, str) or not IDENT.fullmatch(task_id):
        raise ShadowError("not a task id: {!r}".format(task_id))
    for sub in ("tasks", "archive"):
        path = Path(state) / sub / task_id
        if (path / "meta.json").is_file() and not path.is_symlink():
            return path
    return None


def _json(path):
    try:
        value = json.loads(trial_session.read_plain(path).decode("utf-8"))
    except (OSError, ValueError, RecursionError):  # the worker can write the task's files: a FIFO, nesting too deep
        return {}
    return value if isinstance(value, dict) else {}


def _text(path):
    """The text of a file of the source task, "" when it is not there or is not a plain file within the size limit (the coder that
    wrote it could have left anything at the name)."""
    try:
        return trial_session.read_text(path)
    except OSError:
        return ""


def other_lane_section(packet_text):
    """The body of the packet's `Paths other lanes own` section, "" when it has none. The section ends at the next heading."""
    lines = packet_text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == OTHER_LANES:
            body = []
            for rest in lines[i + 1:]:
                if rest.startswith("#"):
                    break
                body.append(rest)
            return "\n".join(body).strip()
    return ""


def read_source(main_state, task_id):
    """What a normal coder was given, from its task dir in Main's state dir (read only): the request, the context specs, notes and
    test scope of dispatch.json, the base commit, and the packet's `Paths other lanes own` section, re-extracted (the packet
    itself is not reused)."""
    path = find_task(main_state, task_id)
    if path is None:
        raise ShadowError("no task '{}' in {}".format(task_id, main_state))
    if (path / "review-of").exists():
        raise ShadowError("task '{}' is a review, not a coder's work; shadow a coder task".format(task_id))
    meta, dispatch = _json(path / "meta.json"), _json(path / "dispatch.json")
    request = _text(path / "request.md")
    if not request.strip():
        raise ShadowError("task '{}' has no request.md text to copy".format(task_id))
    base = dispatch.get("base_commit")
    if not isinstance(base, str) or not COMMIT.fullmatch(base):
        raise ShadowError("task '{}' recorded no base_commit in dispatch.json (it did not start from a git checkout); there is nothing to start a shadow from".format(task_id))
    contexts, notes = dispatch.get("context", []), dispatch.get("notes", [])
    for name, value in (("context", contexts), ("notes", notes)):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ShadowError("task '{}': dispatch.json {} is not a list of text".format(task_id, name))
    tests = dispatch.get("tests") if dispatch.get("tests") in TESTS else None
    packet = _text(path / "packet.md")
    workdir = meta.get("workdir") if isinstance(meta.get("workdir"), str) else ""
    return {"task_id": task_id, "task_dir": str(path), "request": request, "base_commit": base, "context": list(contexts), "notes": list(notes),
            "tests": tests, "other_lane_paths": other_lane_section(packet), "role": meta.get("role") if isinstance(meta.get("role"), str) else "",
            "deliverable": meta.get("deliverable") if isinstance(meta.get("deliverable"), str) else "", "workdir": workdir,
            "reviewer_model": (meta.get("reviewer") or {}).get("model") if isinstance(meta.get("reviewer"), dict) else None}


def remap_context(specs, source_workdir, worktree):
    """The context specs for the shadow: an absolute path inside the original working tree (the lane's worktree) points at the
    shadow's tree instead, so the worker reads the base's files and not the original coder's later edits. A path:start-end range
    keeps its range. Relative paths and paths outside the working tree stay as they are."""
    out = []
    roots = [r for r in {source_workdir, os.path.realpath(source_workdir) if source_workdir else ""} if r]
    for spec in specs:
        match = SPEC.match(spec)
        path, tail = (match.group(1), ":{}-{}".format(match.group(2), match.group(3))) if match else (spec, "")
        for root in roots:
            if path == root or path.startswith(root.rstrip("/") + "/"):
                path = str(worktree) + path[len(root.rstrip("/")):]
                break
        out.append(path + tail)
    return out


def spawn_args(source, run, worktree, project, trial, request_file, task_id, docs_tools=True):
    """The tp-spawn argv for the shadow worker: explicit model and thinking (so the router, the coder floor and the escalation
    rule are never consulted), the worktree as workdir, the preamble read from the main checkout, the trial's two worker env
    values, and the same request, context, notes and test scope the normal coder got."""
    args = [str(ROOT / "bin" / "tp-spawn"), "--task-id", task_id, "--worker", "pi", "--model", trial["model"], "--thinking", trial["thinking"],
            "--workdir", str(worktree), "--preamble-from", str(project), "--worker-env", "TP_TRIAL_ID=" + run]
    if docs_tools:
        args += ["--worker-env", "TP_DOCS_TOOLS=1"]
    if source["role"]:
        args += ["--role", source["role"]]
    if source["tests"]:
        args += ["--tests", source["tests"]]
    if source["deliverable"]:
        args += ["--deliverable", source["deliverable"]]
    if source["other_lane_paths"]:
        args += ["--other-lane-paths", source["other_lane_paths"]]
    for spec in remap_context(source["context"], source["workdir"], worktree):
        args += ["--context", spec]
    for note in source["notes"] + [workdir_text(worktree, project), TRIAL_NOTE]:
        args += ["--note", note]
    return args + ["--file", str(request_file)]


# ----------------------------------------------------------------------------------------------------------- start a run

def default_run_id(task_id, stamp=None):
    clean = re.sub(r"[^a-z0-9]+", "-", task_id.lower()).strip("-")[:20].strip("-") or "task"
    return "sh-{}-{}".format(clean, stamp or datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S"))


def project_of(main_state, source):
    """The repository's main working tree: where the trial's directory lives and the preamble is read from. Found from the task's
    own working tree (a lane's worktree has a main checkout); the task's workdir must still exist."""
    workdir = source["workdir"]
    if not workdir or not os.path.isdir(workdir):
        raise ShadowError("the task's working tree {!r} is gone; give --project the repository's main checkout".format(workdir))
    try:
        return lanes.main_worktree(lanes.git_common_dir(workdir))
    except ValueError as error:
        raise ShadowError(str(error))


def _positive(value, shown, number=False):
    kinds = (int, float) if number else (int,)
    if isinstance(value, bool) or not isinstance(value, kinds) or not value > 0:
        raise ShadowError("{} must be a positive number: {!r}".format(shown, value))
    return value


def prepare(main_state, task_id, *, project=None, run=None, pair_id=None, trial=None, main_max=None, model=None, thinking=None, env=None,
            config_path=None, timeout_s=None, poll_s=2.0, review_timeout_s=None):
    """Check everything that can be checked before anything is made, then create the run directory, run.json and the session.
    Returns the run.json content. Raises ShadowError (or SessionError) with nothing created for a refusal that comes first."""
    if trial is None or main_max is None:
        loaded = trial_session.load_config(config_path)
        trial, main_max = trial or loaded[0], main_max or loaded[1]
    trial = dict(trial, **{k: v for k, v in (("model", model), ("thinking", thinking), ("attempt_timeout_s", timeout_s),
                                             ("review_timeout_s", review_timeout_s)) if v is not None})
    for key, shown in (("attempt_timeout_s", "--timeout-s"), ("review_timeout_s", "--review-timeout-s")):
        if key in trial:  # a zero or negative limit would end the wait at once (or never); refuse it before anything is made
            _positive(trial[key], shown)
    _positive(poll_s, "poll_s", number=True)
    if not isinstance(trial["model"], str) or not trial["model"]:
        raise ShadowError("model must be a model name: {!r}".format(trial["model"]))
    if trial["thinking"] not in trial_session.THINKING:
        raise ShadowError("thinking must be one of {}: {!r}".format(", ".join(trial_session.THINKING), trial["thinking"]))
    source = read_source(main_state, task_id)
    project = os.path.realpath(project) if project else project_of(main_state, source)
    if not lanes._git(["rev-parse", "--verify", "-q", source["base_commit"] + "^{commit}"], project, ok=(0, 1))[0] == 0:
        raise ShadowError("the base commit {} of task '{}' does not exist in {}".format(source["base_commit"], task_id, project))
    run = trial_session.valid_run(run or default_run_id(task_id))
    rdir = trial_session.run_dir(project, run)
    if rdir.exists():
        raise ShadowError("trial run '{}' already exists ({}); choose another --run".format(run, rdir))
    ok, reason = trial_session.guard(main_state, project, trial, main_max, trial["model"], env)
    if not ok:
        raise ShadowError("not started: " + reason)
    rdir.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkdir(str(rdir))  # this prepare's own run directory: one made by anyone else meanwhile (a racing prepare) is refused here
    except FileExistsError:
        raise ShadowError("trial run '{}' already exists ({}); choose another --run".format(run, rdir))
    made_st = os.lstat(str(rdir))
    made_dir = (made_st.st_dev, made_st.st_ino)  # what undo_prepare may remove: this directory, and nothing put at its name later
    made = []  # (id, created) of a session this prepare itself created; undo_prepare ends no other
    try:
        name, state = trial_session.ensure(project, run, env, made=made)
        worktree = rdir / ("wt-" + secrets.token_hex(6))  # this attempt's own place: never the same name twice, so a path is never reused
        try:
            owner_file(project, run).unlink()  # a record left by an earlier run of the same name is not this attempt's
        except FileNotFoundError:
            pass
        owner = Owner(rdir)
        try:
            owner.begin(trial_session.env_for(state, name, env))  # before any worker exists: what the run directory, state dir and session are
        finally:
            owner.close()
        (rdir / "request.md").write_text(workdir_text(worktree, project) + "\n\n" + source["request"].rstrip("\n") + "\n", encoding="utf-8")
        return update_run(rdir, run=run, session=name, state=str(state), main_state=str(Path(main_state).resolve()), project=project,
                          source_task=task_id, pair_id=pair_id or "shadow-" + task_id, base_commit=source["base_commit"], model=trial["model"],
                          thinking=trial["thinking"], workdir=str(worktree), timeout_s=trial["attempt_timeout_s"], review_timeout_s=trial.get("review_timeout_s", 1800),
                          poll_s=poll_s, max_local_workers=trial["max_local_workers"], main_max_local_workers=main_max,
                          worktree=str(worktree), task_id="trial-" + run, mode="shadow", phase="queued", outcome=None,
                          created=now(), pid=None)
    except BaseException:
        undo_prepare(project, run, rdir, made_dir, made, env)
        raise


def undo_prepare(project, run, rdir, made_dir, made, env=None):
    """A prepare that failed after it made its run directory. It undoes only what this prepare itself created, each proven by the
    identity it recorded when it made it, so nothing of a racing prepare or start of the same run is touched:

      the session    each (id, created) in `made` (what tmux printed when this prepare's `ensure` created it), by that id;
      the run dir    only while the path is still a directory (not a link) with the (device, inode) `made_dir`;
      the owner file only when it records `made_dir` as its run dir (`rdir_id`), and this process's own temporary file.

    No worker exists yet. Each step is best effort: the failure being reported is the original one."""
    rdir = Path(rdir)
    session_env = trial_session.env_for(rdir / "state", trial_session.session_name(run), env)
    for sid, created in made:
        try:
            trial_session.end_exact(sid, created, session_env)
        except (trial_session.SessionError, OSError, subprocess.SubprocessError):
            pass
    try:
        st = os.lstat(str(rdir))
        if stat.S_ISDIR(st.st_mode) and (st.st_dev, st.st_ino) == tuple(made_dir):
            shutil.rmtree(str(rdir))
    except OSError:
        pass
    record = owner_file(project, run)
    try:
        ident = json.loads(trial_session.read_plain(record).decode("utf-8")).get("rdir_id") or {}
        if (ident.get("dev"), ident.get("ino")) == tuple(made_dir):
            record.unlink()
    except (OSError, ValueError, AttributeError, RecursionError):
        pass
    try:
        record.with_name(record.name + ".tmp.{}".format(os.getpid())).unlink()
    except OSError:
        pass


def launch(rdir, project, env=None):
    """Start the run detached (its own session, output to run.log), so the caller returns at once. Returns the pid."""
    log = open(str(Path(rdir) / "run.log"), "ab")
    try:
        child = trial_session.start_tp([sys.executable, str(ROOT / "bin" / "tp-trial"), "run", "--run-dir", str(rdir)], env, cwd=str(rdir), detach=log)
    except trial_session.SessionError as problem:
        raise ShadowError(str(problem))
    finally:
        log.close()
    return child.pid


# ----------------------------------------------------------------------------------------------------------------- the run

class Run:
    """One shadow attempt. `sleep` and `clock` are what the wait loops use, so a test steps them instead of waiting."""

    def __init__(self, rdir, env=None, sleep=time.sleep, clock=time.monotonic, record_module=None):
        self.rdir = Path(rdir)
        # Where the run is decides what it is: project, run id, state dir, session and task id come from the run dir's own place and
        # name (the worker can edit run.json, so none of them is read from it). run.json supplies the settings and the chosen place.
        # The runner holds the run directory open from here on (before any worker exists) and reads and writes run.json through it.
        self.owner = Owner(self.rdir)
        self.info = self.owner.read()
        self.run, self.project = self.owner.run, self.owner.project
        self.worktree = Path(self.info["worktree"])
        self.state = self.owner.state
        self.main_state = Path(self.info["main_state"])
        self.task_id = self.owner.task_id
        self.env = trial_session.env_for(self.state, self.owner.session, env)
        self.sleep, self.clock = sleep, clock
        self.started_at = clock() - self.prepared_ago()
        self.records = record_module or load_record_module()
        self.outcome = None
        self.settled = False  # `settle` has decided the outcome from the end evidence: until then `completed` is a claim, not a result
        self.worker_ended = False
        self.recorded = False
        self.record_problem = None
        self.cleanup_problem = None
        self.normal_problem = None
        self.heads_before = set()
        self.heads_taken = False
        self.branches_listed = False
        self.diff = (None, None)
        self.commit_messages = None
        self.review_id = None
        self.review_result = None
        self.wait_result = None

    # -- helpers
    def prepared_ago(self):
        """Seconds since `prepare` wrote the run (run.json `created`, whole seconds, wall clock), never negative: the part of the attempt's
        clock spent before the runner began."""
        try:
            made = datetime.datetime.strptime(self.info.get("created") or "", "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            return 0.0
        return max(0.0, (datetime.datetime.now(datetime.timezone.utc) - made).total_seconds())

    def note(self, **fields):
        """Write `fields` into run.json. The runner's own view (`info`) is what it read at the start, before any worker existed, and
        what it noted since: the file is never read back into it, since the worker can write it (its attempt clock, its poll, its
        review limit, its model would be the worker's to choose)."""
        self.info.update(fields)
        self.owner.update(**fields)

    def note_end(self, **fields):
        """`note` at the end of a run, best effort: False when run.json could not be read or written (the worker can break it)."""
        try:
            self.note(**fields)
        except (ShadowError, OSError):
            return False
        return True

    def command(self, args, timeout=CMD_TIMEOUT):
        """Run a tp-* command of the run, only through the gate (`Owner.tp`). A command that can end anything is checked first. One
        the table does not name is refused (ShadowError) and never started."""
        return self.owner.tp(args, self.env, timeout)

    def agent(self, *args, timeout=CMD_TIMEOUT):
        """`tp-agent <args>`, the one way the run starts tp-agent (see `command`). inspect and list run as they are. end-author, stop
        and review run only after `Owner.check_stop` passed for the task. Anything else is refused."""
        return self.command([ROOT / "bin" / "tp-agent", *args], timeout)

    def task_path(self, task_id=None):
        return self.state / "tasks" / (task_id or self.task_id)

    def status_of(self, task_id):
        run = self.agent("inspect", task_id)
        if run.returncode:
            raise ShadowError("cannot read the state of {}: {}".format(task_id, run.stderr.strip() or "tp-agent exited {}".format(run.returncode)))
        try:
            return json.loads(run.stdout).get("status") or ""
        except ValueError:
            raise ShadowError("tp-agent inspect {} did not print JSON".format(task_id))

    # -- steps
    def make_worktree(self):
        self.note(phase="worktree")
        self.owner.check_place(self.worktree)
        if os.path.lexists(str(self.worktree)):
            raise ShadowError("{} already exists; a run makes its tree at a new place of its own and never reuses one".format(self.worktree))
        self.owner.hold_project()  # the branch listing later reads refs only from this project and git directory
        self.heads_before = heads(self.project)
        self.heads_taken = True
        git(self.project, "worktree", "add", "--detach", str(self.worktree), self.info["base_commit"])
        self.owner.create(self.worktree, self.heads_before)  # what this run created: every later git command proves it first
        self.note(worktree_removed=None)
        shared = lane_deps.share(self.project, self.owner.record["path"], self.info["base_commit"])
        self.note(deps=shared["deps"], deps_warnings=[w if isinstance(w, str) else json.dumps(w, sort_keys=True) for w in shared["warnings"]])

    def source(self):
        return read_source(self.main_state, self.info["source_task"])

    def prove_worktree(self):
        """(state, realpath) of the run's worktree, proven again now against the identity this run recorded when it created the tree
        (`Owner.prove`; run.json is not consulted: the worker can reach it). Raises when the place holds anything else."""
        return self.owner.prove(self.worktree)

    def wt_git(self, *args, check=True, raw=False):
        """git in the run's worktree, run on the proven real path and only after proving it, every time: the worker has write access
        to its tree and the directory around it and can swap the tree for a link to another one while it runs."""
        state, real = self.prove_worktree()
        if state != "owned":
            raise ShadowError("the run's worktree {} is {}; no git command was run there".format(
                self.worktree, "not there" if state == "absent" else "damaged ({})".format(self.owner.why)))
        return git(real, *args, check=check, raw=raw)

    def require_time(self, step):
        """Raise AttemptExpired when the attempt's clock (from `prepare`) is already spent: no worktree is made and no worker started."""
        if self.clock() >= self.attempt_deadline():
            raise AttemptExpired("the attempt's {} s were spent before {}; the worker was not started".format(self.info["timeout_s"], step))

    def recheck_capacity(self):
        """The guard again, just before tp-spawn: between `prepare` and here another run may have been prepared, or Main may have
        filled its slots. This run is left out of the count, and a run that is only queued counts only when it came first."""
        ok, reason = trial_session.guard(self.main_state, self.project, {"max_local_workers": self.info.get("max_local_workers", 1)},
                                         self.info.get("main_max_local_workers", 2), self.info["model"], self.env, exclude_run=self.run,
                                         ahead_of=(self.info.get("created") or "", self.run))
        if not ok:
            raise ShadowError("not started: " + reason)

    def spawn(self):
        self.note(phase="spawn")
        self.recheck_capacity()
        source = self.source()
        trial = {"model": self.info["model"], "thinking": self.info["thinking"]}
        args = spawn_args(source, self.run, self.worktree, self.project, trial, self.rdir / "request.md", self.task_id)
        self.require_time("tp-spawn")
        run = self.command(args)
        if run.returncode:
            raise ShadowError("tp-spawn refused: {}".format(run.stderr.strip()[-600:] or "exit {}".format(run.returncode)))
        spawned = run.stdout.strip().splitlines()
        if not spawned or spawned[-1] != self.task_id:
            raise ShadowError("tp-spawn printed {!r}, not the task id {}".format(spawned[-1:] or "nothing", self.task_id))
        self.note(spawned=now())

    def wait(self, task_id, deadline, phase, yield_to_main=False):
        """Wait for `task_id` to settle. Returns settled, failed, stopped, paused, preempted or timeout. The poll sleeps `poll_s` and
        re-reads the task's own record; `deadline` is a time on this run's clock, a limit and not a wait. With `yield_to_main` each poll
        also reads Main's local workers: when Main holds all its slots the wait ends `preempted` (§10.7: a trial does not continue a
        local run then). A task that has settled is taken as it is, whatever Main holds."""
        self.note(phase=phase)
        while True:
            status = self.status_of(task_id)
            if status in SETTLED:
                return "settled"
            if status in ("failed", "stopped", "paused"):
                return status
            if yield_to_main and self.main_is_full():
                return "preempted"
            if self.clock() >= deadline:
                return "timeout"
            self.sleep(self.info.get("poll_s", 2.0))

    def main_is_full(self):
        """Whether Main now holds all its local worker slots, for a run of a local model. An unreadable count stops the run: it is never read as zero."""
        if not self.info["model"].startswith(trial_session.LOCAL):
            return False
        try:
            main = trial_session.local_active(self.main_state, self.env)
        except trial_session.SessionError as problem:
            raise ShadowError(str(problem))
        return len(main) >= self.info.get("main_max_local_workers", 2)

    def attempt_deadline(self):
        """The end of the attempt's wall clock (§8): `timeout_s` after the run was prepared, so the time `tp-spawn` and the worktree took
        is spent from it and the worker has what remains."""
        return self.started_at + self.info["timeout_s"]

    def end_worker(self, outcome):
        """End the worker's process: `end-author` for a task that settled (its pane and scrollback are kept), `stop` for any other."""
        run = self.agent("end-author" if outcome == "settled" else "stop", self.task_id)
        if run.returncode:
            raise ShadowError("the worker could not be ended: {}".format(run.stderr.strip() or "tp-agent exited {}".format(run.returncode)))
        self.worker_ended = True  # what `settle` reads: the end command ran and said so

    def collect_work(self):
        """Read the commit messages, then move HEAD to the base (what a soft reset does) so a review sees the work as an uncommitted change (a non-lane
        review diffs HEAD). Returns after saving the patch and counting the final diff."""
        self.note(phase="reset")
        base = self.info["base_commit"]
        self.list_new_branches()  # first: the listing reads the project's refs, not the worker's tree, so a refusal below does not lose it
        state, _ = self.prove_worktree()
        if state == "absent":  # the work cannot be read, and a review of nothing is no review of the worker's work
            raise ShadowError("the run's worktree {} is gone; the worker's work was not collected".format(self.worktree))
        log = self.wt_git("log", "--reverse", base + "..HEAD", "--format=%B%x00", check=False)
        self.commit_messages = [m.strip("\n") for m in log.stdout.split("\0") if m.strip()] if log.returncode == 0 else None
        head = self.wt_git("rev-parse", "-q", "--verify", "HEAD", check=False).stdout.strip()
        if head and head != base:
            self.owner.keep_ref("{}{}/head".format(REF_PREFIX, self.run), head)  # a ref this run makes and records: cleanup removes it by name and commit
        self.leave_branch()
        # HEAD to the base, the index and the files as they are: what `reset --soft` does, except that it refuses in the middle of a merge
        # (a worker that gave up there would cost the whole patch), and that `--no-deref` can only ever move HEAD itself, never a branch
        self.wt_git("update-ref", "--no-deref", "-m", "tp-trial: HEAD to the base for the review", "HEAD", base)
        self.add_all()
        stat = self.wt_git("diff", "--cached", "--numstat", base).stdout.splitlines()
        lines = 0
        for row in stat:
            added, removed = row.split("\t")[:2]
            lines += (int(added) if added.isdigit() else 0) + (int(removed) if removed.isdigit() else 0)
        self.diff = (len(stat), lines)
        patch = self.wt_git("diff", "--cached", "--binary", base, raw=True).stdout
        write_at(self.owner.need("rdir"), "final.patch", patch)
        self.note(diff_files=self.diff[0], diff_lines=self.diff[1])

    def add_all(self):
        """`git add -A`: the worker's new files count in the diff and in the review's file list. A path git cannot index (a repository
        the worker made inside the tree with no commit in it) is skipped and named in run.json (`unindexed`), so one stray path does
        not cost the whole patch; any other failure ends the collect, as before."""
        added = self.wt_git("add", "-A", "--ignore-errors", check=False)
        if not added.returncode:
            return
        lines = [line.strip() for line in added.stderr.splitlines() if line.strip()]
        skipped = [line for line in lines if UNINDEXED.match(line)]
        if not lines or len(skipped) != len(lines):
            raise ShadowError("git add -A failed in {}: {}".format(self.worktree, added.stderr.strip() or "exit {}".format(added.returncode)))
        self.note(unindexed=[line[:200] for line in skipped if "unable to index" in line][:20])

    def leave_branch(self):
        """Make sure HEAD of the run's tree is detached before it is moved to the base, which would otherwise move the branch it is on. A
        branch HEAD is on is never deleted, moved or judged: the run created no branch (its tree is `worktree add --detach`), so it
        has none recorded as its own, and a branch it cannot show it made stays. It is named in run.json (`branch_left`: the ref and
        the commit it was at) for whoever wants it gone. Detaching only moves HEAD, so it is safe for any branch (one the worker made,
        renamed, or one somebody else made meanwhile). HEAD is written directly (`update-ref --no-deref`), not with `checkout --detach`,
        which refuses in the middle of a merge or a cherry-pick. Raises when HEAD cannot be shown detached: it is not moved."""
        probe = self.wt_git("symbolic-ref", "-q", "HEAD", check=False)
        if probe.returncode == 1:  # detached already: the move to the base moves HEAD alone
            return
        if probe.returncode != 0 or not probe.stdout.strip():
            raise ShadowError("cannot tell what HEAD of the run's tree is on; HEAD was not moved to the base")
        ref = probe.stdout.strip()
        tip = self.wt_git("rev-parse", "-q", "--verify", ref + "^{commit}", check=False).stdout.strip()
        if tip:  # a branch with no commit yet (`checkout --orphan`) has no commit for HEAD to name: it stays as it is, and the collect stops
            self.wt_git("update-ref", "--no-deref", "-m", "tp-trial: leave the branch", "HEAD", tip, check=False)
        if not tip or self.wt_git("symbolic-ref", "-q", "HEAD", check=False).returncode != 1:
            raise ShadowError("HEAD of the run's tree is on {} and could not be detached; HEAD was not moved to the base".format(ref))
        self.note(branch_left={"ref": ref, "commit": tip or None})

    def list_new_branches(self):
        """Design (f): a branch the worker made is left where it is and listed. `branch_left` names the one HEAD was on; this names every
        head that was not there when the run's tree was made (`heads_before`, the runner's own memory), whether HEAD is on it or not, as
        `new_branches` [{ref, commit}] in run.json (absent when there is none). One that somebody else made meanwhile is listed too: the
        run cannot tell who made it. Nothing is deleted or moved: this only reads the project's refs (the listing is of the project, not
        of the worker's tree, so it needs no proof of the tree). The refs are read only from the project the run recorded: its root and
        its git directory must still be the directories held open when the tree was made (`Owner.check_project`, before and after the
        query); when they are not (the project moved aside and another repository put in its place, a swapped `.git`), or the query
        fails, nothing is listed and `new_branches_error` says why. A run that never took `heads_before` (it stopped before its tree was
        made) lists nothing. It is called first in `collect_work`, and by `execute` when the run ended without collecting, so a run
        that stops at an ownership check still names the branches the worker made. A failed listing is not a failed collect."""
        if self.branches_listed or not self.heads_taken:
            return
        self.branches_listed = True  # one try: a listing that could not be made is reported, not retried against what is there later
        try:
            self.owner.check_project()
            listed = git(self.project, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", check=False)
            if listed.returncode:
                raise ShadowError("git for-each-ref failed in {}: {}".format(self.project, listed.stderr.strip() or "exit {}".format(listed.returncode)))
            self.owner.check_project()  # the project could have been swapped while the query ran: then what it read is not listed
        except (ShadowError, OSError) as error:
            self.note(new_branches_error="the branches the worker made could not be listed: {}".format(error))
            return
        rows = [line.split(" ", 1) for line in listed.stdout.splitlines() if " " in line]
        made = [{"ref": ref, "commit": commit} for ref, commit in sorted(rows) if ref not in self.heads_before]
        if made:
            self.note(new_branches=made)

    def review(self):
        self.note(phase="review")
        try:  # tp-review ends the author (tp-agent end-author) before it spawns the reviewer, so it is checked like any other end
            run = self.agent("review", "--force", self.task_id)
        except ShadowError as problem:
            self.note(review_error=str(problem)[-600:])
            return None
        if run.returncode:
            self.note(review_error=(run.stderr.strip() or "tp-agent review exited {}".format(run.returncode))[-600:])
            return None
        try:
            value = json.loads(run.stdout)
        except ValueError:
            self.note(review_error="tp-agent review did not print JSON")
            return None
        self.review_id = value.get("review")
        if not self.review_id:
            self.note(review_error=value.get("reason") or "no review was started")
            return None
        self.note(review_id=self.review_id)
        self.owner.record_panes(self.env)  # the reviewer's pane, now that it exists
        result = self.wait(self.review_id, self.clock() + self.info.get("review_timeout_s", 1800), "review-wait")
        self.review_result = result
        self.note(review_wait=result)
        if result == "timeout":
            try:
                self.agent("stop", self.review_id)
            except ShadowError as problem:
                self.note(review_error="the reviewer was not stopped: {}".format(problem))
        return self.review_id

    def reviewed_outcome(self):
        """Review the work and say how the steps ended: `completed` when the reviewer settled (still only a claim: the completed rule
        at the end of `execute` reads the verdict from the record and checks the rest), `no_review` when no review could be
        started or the reviewer failed, stopped or paused, `review_timeout` when it did not finish in time. The last two have no first
        review in the record, which L2's report counts as no review; the attempt is recorded either way."""
        if self.review() is None:
            return "no_review"
        if self.review_result == "settled":
            return "completed"
        return "review_timeout" if self.review_result == "timeout" else "no_review"

    def reviewer_model(self):
        meta = _json(self.task_path() / "meta.json")
        reviewer = meta.get("reviewer")
        return reviewer.get("model") if isinstance(reviewer, dict) and isinstance(reviewer.get("model"), str) else None

    def spawn_failed_record(self):
        """A failed spawn may leave no task dir, so the builder has nothing to read. The record still says what was tried and that it
        failed (a spawn failure is data too): L2's `no_task`, which has the keys the builder's records have and `no_task` set."""
        record = self.records.no_task("qwen", "shadow", self.info["pair_id"], task_id=self.task_id, role=self.source_role() or self.info.get("source_role"),
                                      model=self.info["model"], effort=self.info["thinking"], base_commit=self.info["base_commit"], now=now())
        record["error"] = self.info.get("error")
        return record

    def qwen_record(self):
        """The run's Qwen record, built and not yet written, or None when there is no task to build it from (and the run is not a
        spawn failure, which is recorded with L2's `no_task`). Raises when the builder cannot make it."""
        has_task = (self.task_path() / "meta.json").is_file()
        if not has_task and self.outcome != "spawn_failed":
            return None
        if not has_task:
            record = self.spawn_failed_record()
            record.update({"run": self.run, "source_task": self.info["source_task"], "workdir": str(self.worktree),
                           "normal_reviewer_model": self.info.get("normal_reviewer_model"), "reviewer_mismatch": None})
            return record
        record = self.records.build(self.task_path(), "qwen", "shadow", self.info["pair_id"], state_dir=self.state, role=self.source_role(),
                                    diff_files=self.diff[0], diff_lines=self.diff[1], commit_messages=self.commit_messages)
        normal = self.info.get("normal_reviewer_model")
        mine = self.reviewer_model()
        record.update({"run": self.run, "source_task": self.info["source_task"], "workdir": str(self.worktree), "normal_reviewer_model": normal,
                       "reviewer_mismatch": None if not (normal and mine) else normal != mine})
        return record

    def rows(self, arm):
        """(count, None) of the rows of `arm` for this run in records.jsonl, read back now, or (None, why they cannot be counted)."""
        return record_rows(self.records, self.project, self.run, arm)

    def collected(self):
        """Why the work is not shown collected from the proven tree, or None: `final.patch` is in the run directory (written by
        `collect_work` through the held directory, after every git command of the collect ran in the tree proven to be this run's)."""
        try:
            if self.diff[0] is not None and _exists_in(self.owner.need("rdir"), "final.patch"):
                return None
        except (ShadowError, OSError):
            pass
        return "the work was not collected: final.patch is not in the run directory"

    def cleanup_unmet(self):
        """Why the cleanup is not shown to have ended the run's session and removed its worktree, read from what is there now, or None."""
        try:
            if self.owner.session_alive(self.env):
                return "the cleanup did not end the session {}: it is still there".format(self.owner.session)
        except (ShadowError, trial_session.SessionError) as problem:
            return "the session {} cannot be shown to be gone: {}".format(self.owner.session, problem)
        for path in {(self.owner.record or {}).get("path"), str(self.worktree)} - {None}:
            if os.path.lexists(path):
                return "the cleanup did not remove the worktree {}: it is still there".format(path)
        return None

    def write_normal(self):
        """Write the normal arm's record (`record_normal`, best effort as before) and say whether its row is as the completed rule allows:
        None when its row is in records.jsonl, or when it is absent for a reason this names in run.json, which are the allowed absences:
        `normal_record_error` (it could not be built or appended; `tp-trial normal` redoes it) and `normal_record_absent` (the source
        task is gone, so there is no normal arm to record). Otherwise (an append that returned and left no row) why not."""
        try:
            record = record_normal(self.rdir, self.env, self.records, self.owner, info=self.info)
        except Exception as problem:  # the normal arm's record is best effort: a later `tp-trial normal` redoes it
            self.note_end(normal_record_error="{}: {}".format(problem.__class__.__name__, problem))
            return None
        if record is None:
            self.note_end(normal_record_absent="the source task {} is gone; there is no normal arm to record".format(self.info["source_task"]))
            return None
        count, why = self.rows("normal")
        if count:
            return None
        return "the normal arm's record was appended but records.jsonl has no normal row of this run ({})".format(why or "0 rows")

    def settle(self, record, missing=None):
        """The outcome by the completed rule (see `execute`): the outcome the steps gave when it is not `completed`, else the first condition
        that does not hold, from the evidence there is now. `record` is the Qwen record about to be written (None when none could be
        built, `missing` says why); the Qwen row itself is checked after the append (`write_record`)."""
        if self.outcome != "completed":
            return self.outcome, None
        if not self.worker_ended:
            return "error", "the worker was not shown ended: its end command did not run to success"
        why = self.collected()
        if why:
            return "error", why
        if record is None:
            return "record_failed", missing or "no Qwen record could be built"
        if record.get("first_review_outcome") not in REVIEW_VERDICTS:
            return "no_review", "the reviewer settled without a verdict the record can read"
        if self.normal_problem:
            return "record_failed", self.normal_problem
        if self.cleanup_problem:
            return "cleanup_failed", self.cleanup_problem
        return "completed", None

    def write_record(self):
        """Settle the outcome (`settle`), then append the run's Qwen record with that outcome and read it back: exactly one row of this
        run must be in records.jsonl afterwards. A record that could not be built or appended is tried once more, and an append is
        repeated only when the readback found no row (one that raised after writing is not written twice). When the row is not there
        exactly once, a run the rule had `completed` or `cleanup_failed` is `record_failed` (the Qwen row comes before the cleanup in
        the rule); any other outcome is kept. `record_error` says what failed; `recorded` is noted only after the readback found the
        row. A run with no task that does not need one (it stopped before its task existed) has no row, as before."""
        if self.recorded:
            return
        problems, unmet = [], None
        for _ in range(2):
            try:
                record = self.qwen_record()
                built, missing = True, None if record is not None else "the task's meta.json is not there, so no Qwen record could be built"
            except Exception as problem:
                record, built, missing = None, False, "{}: {}".format(problem.__class__.__name__, problem)
            self.outcome, unmet = self.settle(record, missing)
            self.settled = True
            if record is None:
                if built and self.outcome != "record_failed":
                    if unmet:
                        self.note_end(unmet=unmet)
                    return
                problems.append(missing)
                if built:  # meta.json does not come back by trying again
                    break
                continue
            self.note_end(phase="record")
            record["outcome"] = self.outcome
            try:
                append_record(self.records, self.project, record)
            except Exception as problem:
                problems.append("{}: {}".format(problem.__class__.__name__, problem))
            count, why = self.rows("qwen")
            if count == 1:
                self.recorded = True
                break
            if count is None or count > 1:
                problems.append(why or "records.jsonl has {} Qwen rows of this run, not one".format(count))
                break
            if len(problems) == 0:
                problems.append("the append returned and records.jsonl has no Qwen row of this run")
        if self.recorded:
            self.record_problem = None
            fields = {"recorded": now()}
            if problems:  # written by the second try: the first failure is kept under another name, so run.json does not claim a missing record
                fields.update(record_error=None, record_retried=problems[0])
            if unmet:
                fields["unmet"] = unmet
            self.note_end(**fields)
            return
        self.record_problem = "; ".join(problems) or "no Qwen record was written"
        if self.outcome in ("completed", "cleanup_failed"):
            self.outcome, unmet = "record_failed", self.record_problem
        fields = {"record_error": self.record_problem}
        if unmet:
            fields["unmet"] = unmet
        self.note_end(**fields)

    def source_role(self):
        meta = _json(self.task_path() / "meta.json")
        return meta.get("role") if isinstance(meta.get("role"), str) else None

    def cleanup(self):
        return cleanup(self.rdir, self.env, self.owner)

    # -- the whole run
    def execute(self):
        """The run. Its outcome is decided once, at the end, by one rule (review round 3, family false-completed-trial). A run is
        `completed` only when all of these hold, each read then from evidence, not from a flag set along the way:

          1. the worker ended                  its end command (end-author / stop) ran to success (`worker_ended`)   else `error`
          2. the work was collected            final.patch is in the run directory (`collected`)                     else `error`
             from the proven tree
          3. a review settled with a verdict   the reviewer settled (`reviewed_outcome`) and the record built from
                                               its files has a first-review outcome (`settle`)                         else `no_review`
                                                                                                          (`review_timeout` for a
                                                                                                          reviewer out of time)
          4. exactly one Qwen row of the run   read back from records.jsonl after the append (`write_record`)        else `record_failed`
          5. the normal arm's row exists, or   read back (`write_normal`); the allowed absences are named in run.json:
             its absence is an allowed one     `normal_record_error` and `normal_record_absent`                       else `record_failed`
          6. the cleanup ended the session     the session's recorded id is gone and nothing is at the worktree's
             and removed the worktree          place (`cleanup_unmet`)                                               else `cleanup_failed`

        A run whose steps already gave another outcome (timeout, preempted, worker_failed, stopped, paused, spawn_failed, error) keeps it:
        that is its first unmet condition. Otherwise the outcome names the first condition that does not hold, and `unmet` in run.json
        says why. The order at the end serves this: the cleanup, then the normal arm's record, then the Qwen record, so the Qwen row is
        written with the outcome the run ends with. `tp-trial` exits non-zero for any outcome but `completed`."""
        self.note(pid=os.getpid(), started=now(), phase="starting")
        error = None
        try:
            try:
                self.owner.check_place(self.worktree)  # the worktree is made, reset and read below: only at the run's own place
                source = self.source()
                self.note(normal_reviewer_model=source["reviewer_model"], source_role=source["role"] or None)
                self.require_time("the worktree was made")
                self.make_worktree()
                try:
                    self.spawn()
                except AttemptExpired:
                    raise
                except ShadowError as problem:
                    self.outcome, error = "spawn_failed", str(problem)
                    raise
                self.owner.record_panes(self.env)  # the worker's pane, now that it exists and before it could move anything
                result = self.wait(self.task_id, self.attempt_deadline(), "wait", yield_to_main=True)
                self.note(wait_result=result)
                if result in ("timeout", "preempted"):
                    self.outcome = result
                self.end_worker(result)
                self.collect_work()
                if result == "settled":
                    self.outcome = self.reviewed_outcome()
                elif result not in ("timeout", "preempted"):
                    self.outcome = {"failed": "worker_failed", "stopped": "stopped", "paused": "paused"}[result]
            except Interrupted:
                self.outcome = "stopped"
                raise
            except AttemptExpired as problem:
                self.outcome, error = self.outcome or "timeout", error or str(problem)
            except ShadowError as problem:
                self.outcome = self.outcome or "error"
                error = error or str(problem)
        except Interrupted:
            self.note_end(interrupted=True)
        except Exception as problem:  # anything else is a failed run with a reason, never a traceback that skips the cleanup
            self.outcome = self.outcome or "error"
            error = error or "{}: {}".format(problem.__class__.__name__, problem)
        finally:
            # Every note from here is best effort (`note_end`): run.json is the worker's to break, and a note that cannot be written
            # never keeps the cleanup (the session kill every refusal relies on) or the records from running.
            try:
                if error:
                    self.note_end(error=error)
                self.outcome = self.outcome or "error"
                self.list_new_branches()  # a run that stopped before its collect still names what the worker made, before the cleanup
                self.note_end(phase="cleanup")
                try:
                    leftovers = self.cleanup()
                except Exception as problem:
                    leftovers = ["cleanup: {}".format(problem)]
                self.note_end(cleanup_errors=leftovers)
                self.cleanup_problem = self.cleanup_unmet()
                self.normal_problem = self.write_normal()
                self.write_record()  # settles the outcome by the rule above and writes the Qwen row with it
            except Interrupted:
                self.note_end(interrupted=True)
                raise
            finally:
                if not self.settled and self.outcome == "completed":  # stopped before the rule was checked: not shown completed
                    self.outcome = "stopped"
                self.note_end(outcome=self.outcome, phase="done", finished=now())
        try:
            return self.owner.read()
        finally:
            self.owner.close()


def execute(rdir, env=None, sleep=time.sleep, clock=time.monotonic, record_module=None, install_signals=False):
    if install_signals:
        def stop(_signo, _frame):
            raise Interrupted()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
    info = read_run(rdir)
    if info.get("phase") != "queued":
        raise ShadowError("run {} is not queued (phase: {}); a run executes once".format(info.get("run"), info.get("phase")))
    return Run(rdir, env, sleep, clock, record_module).execute()


# ----------------------------------------------------------------------------------------------------------- normal record

def _diff_counts(workdir, base):
    """(files, lines) of the working tree against `base` in `workdir`, or (None, None) when that cannot be read."""
    if not isinstance(workdir, str) or not workdir or not os.path.isdir(workdir):
        return None, None
    if not isinstance(base, str) or not COMMIT.fullmatch(base):  # from run.json, the worker's to write: never an option to git
        return None, None
    run = git(workdir, "diff", "--numstat", base, "--", check=False)
    if run.returncode:
        return None, None
    rows = run.stdout.splitlines()
    lines = sum((int(a) if a.isdigit() else 0) + (int(r) if r.isdigit() else 0) for a, r in (row.split("\t")[:2] for row in rows))
    return len(rows), lines


def record_normal(rdir, env=None, record_module=None, owner=None, info=None):
    """Write the record of the normal arm of the pair (the source task, built from Main's task dir, read only) with the same
    builder. Safe to repeat: L2's report keeps the last record of a pair and arm, so a later call, once the normal task's review
    has landed, supersedes an earlier one. Returns the record or None when the source task is gone. `info` is the runner's own view
    of the run (`Run.info`); without it run.json is read, which the worker can have written."""
    mine = owner is None
    owner = owner or Owner(rdir)
    try:
        return _record_normal(owner, record_module, info)
    finally:
        if mine:
            owner.close()


def _record_normal(owner, record_module, info=None):
    info = info if info is not None else owner.read()
    project, run = owner.project, owner.run
    records = record_module or load_record_module()
    main_state = Path(info["main_state"])
    path = find_task(main_state, info["source_task"])
    if path is None:
        return None
    files, lines = _diff_counts(_json(path / "meta.json").get("workdir"), info["base_commit"])
    record = records.build(path, "normal", "shadow", info["pair_id"], state_dir=main_state, diff_files=files, diff_lines=lines)
    record.update({"run": run, "source_task": info["source_task"], "outcome": "completed"})
    append_record(records, project, record)
    owner.update(normal_recorded=now())
    return record


# ------------------------------------------------------------------------------------------------------------------ ownership

def owner_file(project, run):
    """The ownership record of a run: outside its run dir (and so outside the worktree's parent), `<project>/.tmux-pi/trial-owner/<run>.json`."""
    return Path(project) / ".tmux-pi" / OWNER_DIR / (trial_session.valid_run(run) + ".json")


def locate(rdir):
    """(project, run) from where the run dir is, `<project>/.tmux-pi/trial/<run>`: the caller names the directory, so these cannot
    be changed from inside the run. Raises when the directory is not in that shape."""
    path = Path(os.path.abspath(str(rdir)))
    if path.parent.name != "trial" or path.parent.parent.name != ".tmux-pi":
        raise ShadowError("{} is not a trial run directory (<project>/.tmux-pi/trial/<run>)".format(path))
    try:
        run = trial_session.valid_run(path.name)
    except trial_session.SessionError as problem:
        raise ShadowError(str(problem))
    return os.path.realpath(str(path.parent.parent.parent)), run


def _gitfile_target(path):
    """Where the `.git` file of the worktree at `path` points (the admin directory), as a real path, or None."""
    try:
        text = trial_session.read_plain(Path(path) / ".git").decode("utf-8").strip()  # the worker's tree: never a wait on a FIFO
    except (OSError, UnicodeDecodeError):
        return None
    return os.path.realpath(text[len("gitdir:"):].strip()) if text.startswith("gitdir:") else None


def _unreadable(error):
    """Why `trial_session.read_plain` refused a file, as the end of a sentence about it."""
    if error.errno == errno.EINVAL:
        return "is not a plain file"
    if error.errno == errno.EFBIG:
        return "is larger than {} bytes".format(trial_session.MAX_READ)
    return "cannot be opened as a plain file ({})".format(error.strerror)


def registered_worktrees(project):
    """{real path: branch ref or None} of every worktree git lists for the repository of `project`."""
    found = {}
    for block in git(project, "worktree", "list", "--porcelain").stdout.split("\n\n"):
        fields = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            fields[key] = value
        if fields.get("worktree"):
            found[os.path.realpath(fields["worktree"])] = fields.get("branch") or None
    return found


class Owner:
    """What one attempt created, and the proof that a directory, a tmux session or a pane is that. Built from where the run dir is
    (`locate`), never from run.json.

    Rule: every destructive act (a signal, a tmux kill, a worktree removal, a write into a run directory) first matches an identity
    this attempt recorded itself, never a path or an id read from a file the worker can edit:

      run dir, state dir   `prepare` records (device, inode) of both, before any worker exists. The runner opens both (`hold`,
                           never through a link) and keeps them open: run.json and final.patch are written through that descriptor
                           (`update`, `write_at`), so a rename of the run directory or a link put at its old name redirects nothing.
                           Before each command against the state dir the path is compared with the held directory (`check_state`).
                           A run dir that is a link, or is not the recorded one, is refused: nothing in it is read or changed.
      tmux session         `prepare` records the session id tmux gave it (with the time tmux created it). A session is killed
                           by that id, and only while it still has that id and created time (`end_session`): a session that took
                           the name later, or an id reused after a tmux restart, is not this run's. The name is not part of the
                           identity: a worker can rename the session and it is still ended.
      panes, their         after tp-spawn and after the review starts, the run records the panes tmux lists in its session and, for each
      processes            pane that is alive, the process it runs (pid, process group, start time). `tp-agent stop` (tp-kill) reads
                           two files of the task dir to choose what to end, both the worker's to write: the pane in meta.json, and
                           the pid and group in process.json. It is run only for a pane recorded here and still in that session
                           (`check_pane`), and only when process.json is absent or names exactly the process recorded for one of
                           the run's panes, with a pid that still has the recorded start time (`check_process_record`). Anything
                           else is not acted on: the task is reported and left running until the session kill takes its pane.
                           The same check (`check_stop`) runs before end-author and before review, which ends the author through
                           end-author, and every tp-* command goes through the one gate (`tp`, trial_session.TP) so none can skip
                           it. Before any of them, and before tp-spawn, the session the run's name resolves to (each tp-* command
                           finds it by TP_SESSION) must be the recorded one with @tp_viewing unset (tp-kill would swap panes through
                           tp-view). Before stop there must be no queue entry for the task. A general fix belongs in tp-kill /
                           worker_process (the recorded follow-up `tp-agent stop pane trust`); this is the trial's own guard until then.
      worktree             (device, inode) of the tree root and of its git admin dir, held open by the runner (`create`): while a
                           descriptor is open no other directory can have that identity, so nothing the worker does (remove and
                           re-add a tree at the same path, git reusing the admin name, `git worktree move`, a link, a rewritten
                           `.git`) produces a directory that proves as this run's.
      project, its git     (device, inode) of the project root and of its common git directory, held open by the runner from just
      directory            before the tree is made (`hold_project`). The branch listing reads refs/heads only while both paths are
                           still those directories and git in the project still finds that git directory (`check_project`); a
                           project moved aside with another repository in its place is not read (`new_branches_error`).

    KNOWN LIMIT (not fixed; the threat model is that the worker runs as the same OS user and nothing is sandboxed). A stop of a runner
    that has died holds no descriptors and no memory: it reads the identities above from the owner file
    `<project>/.tmux-pi/trial-owner/<run>.json`. That file is outside the run dir but is owned by the same user, so a worker can
    rewrite it, naming another registered worktree, session or pane, and a later `tp-trial stop` of the dead run then trusts it. A
    live runner never reads the file back (it holds what it recorded), so the limit applies only to a stop after the runner is gone.
    Nor can a check be made atomic with the act that follows it. Between `check_stop` (or `check_state`) and the tp-agent command
    that reads them, the worker can still rewrite `meta.json` and `process.json`, set the session's @tp_viewing or a queue entry,
    or swap what a path names. The command then follows what it reads at that moment.

    `prove` returns ("owned", path) when every worktree check holds now, ("damaged", path) when the directory is the one the run made
    but git's link to it is broken (git no longer knows it, its `.git` was rewritten, it is on a pre-existing branch: the run may
    delete the directory but runs no git command in it), ("absent", path) when nothing is there, and raises ShadowError for anything
    else: a link, another directory, a tree made after the run's own was removed, a record that cannot be read."""

    def __init__(self, rdir):
        self.fds = {}
        self.refused = {}
        self.rdir = Path(os.path.abspath(str(rdir)))
        self.rdir_real = os.path.join(os.path.realpath(str(self.rdir.parent)), self.rdir.name)  # the name is not resolved: a link is not followed
        self.project, self.run = locate(self.rdir)
        self.state = self.rdir / "state"
        self.session = trial_session.session_name(self.run)
        self.task_id = "trial-" + self.run
        self.file = owner_file(self.project, self.run)
        self.record = None
        self.problem = None
        self.why = ""
        self.load()
        self.hold()

    def __del__(self):
        self.close()

    # -- the record
    def load(self):
        """Read the owner file (a runner that has it in memory does not read it back)."""
        try:
            value = json.loads(trial_session.read_plain(self.file).decode("utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError, RecursionError) as error:  # the owner dir is the worker's to write too: a FIFO, nesting too deep
            error = error.__class__.__name__ if isinstance(error, RecursionError) else error
            self.problem = "the owner record {} cannot be read ({}); nothing is ended or removed".format(self.file, error)
            return
        try:
            ok = (isinstance(value, dict) and value["run"] == self.run and value["project"] == self.project
                  and value["run_dir"] == self.rdir_real
                  and all(isinstance(value[k][f], int) for k in ("rdir_id", "state_id") for f in ("dev", "ino"))
                  and isinstance(value["session"]["id"], str) and isinstance(value["session"]["created"], str) and value["session"]["created"].isdigit()
                  and isinstance(value["session"]["name"], str)
                  and isinstance(value["panes"], list) and all(isinstance(p, str) for p in value["panes"])
                  and isinstance(value.get("pane_procs", {}), dict)
                  and all(k in value["panes"] and isinstance(v, dict) and isinstance(v.get("start"), str) and v["start"]
                          and all(isinstance(v.get(f), int) and not isinstance(v.get(f), bool) and v[f] > 1 for f in ("pid", "pgid"))
                          for k, v in value.get("pane_procs", {}).items()))
            if ok and "path" in value:  # the worktree part exists once the tree was made
                ok = (PLACE.fullmatch(Path(value["path"]).name) is not None
                      and all(isinstance(value[k][f], int) for k in ("tree", "admin") for f in ("dev", "ino")) and isinstance(value["admin"]["path"], str)
                      and isinstance(value["heads_before"], list))
        except (KeyError, TypeError):
            ok = False
        if not ok:
            self.problem = "the owner record {} does not describe this run; nothing is ended or removed".format(self.file)
            return
        refs = value.get("refs", {})
        prefix = "{}{}/".format(REF_PREFIX, self.run)  # an entry that is not a ref of this run with a commit id is dropped: that ref is then kept
        value["refs"] = {k: v for k, v in refs.items() if isinstance(k, str) and k.startswith(prefix) and isinstance(v, str) and COMMIT.fullmatch(v)} \
            if isinstance(refs, dict) else {}
        self.record = value
        self.problem = None

    def _write(self):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        write_at(None, self.file, json.dumps(self.record, indent=2, sort_keys=True) + "\n")  # the owner dir is the worker's to write

    def hold(self):
        """Open the run directory and its state dir (never through a link) and keep them open. Each must be the directory the owner
        record names, when there is a record; one that is not is refused (`refused` says why) and is not held."""
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        self.refused = {}

        def take(kind, path, opener):
            try:
                fd = opener()
            except OSError as error:
                self.refused[kind] = "{} is not the run's own directory (it cannot be opened as one: {}); nothing in it is read or changed".format(path, error.strerror)
                return None
            st = os.fstat(fd)
            want = (self.record or {}).get(kind + "_id")
            if want and (st.st_dev, st.st_ino) != (want["dev"], want["ino"]):
                os.close(fd)
                self.refused[kind] = "{} is not the run's own directory (it is not the one prepared for this run); nothing in it is read or changed".format(path)
                return None
            self.fds[kind] = fd
            return fd

        rfd = take("rdir", self.rdir, lambda: os.open(str(self.rdir), flags))
        if rfd is not None:
            take("state", self.state, lambda: os.open("state", flags, dir_fd=rfd))

    def need(self, kind):
        fd = self.fds.get(kind)
        if fd is None:
            raise ShadowError(self.refused.get(kind) or self.refused.get("rdir") or "the {} is not held open".format(kind))
        return fd

    def check_state(self):
        """Raise unless the run directory and the state dir, at their paths now, are the directories held open."""
        for kind, path in (("rdir", self.rdir), ("state", self.state)):
            held = os.fstat(self.need(kind))
            try:
                now_ = os.lstat(str(path))
            except OSError:
                now_ = None
            if now_ is None or stat.S_ISLNK(now_.st_mode) or (now_.st_dev, now_.st_ino) != (held.st_dev, held.st_ino):
                raise ShadowError("{} is not the run's own directory any more (a link, or another directory); no command was run against it".format(path))

    def read(self):
        return read_run_at(self.need("rdir"))

    def update(self, **fields):
        return update_run_at(self.need("rdir"), **fields)

    def try_update(self, **fields):
        try:
            self.update(**fields)
        except (ShadowError, OSError):
            return False
        return True

    def begin(self, env):
        """Record the identities of the run directory, the state dir and the session just made (`prepare`; no worker exists yet)."""
        found = trial_session.named(self.session, env)
        if found is None or not found[3] or Path(found[3]).resolve() != self.state.resolve():
            raise ShadowError("the tmux session {} is not there with the state dir {}; the run was not prepared".format(self.session, self.state))
        ident = {kind: {"dev": os.fstat(self.need(kind)).st_dev, "ino": os.fstat(self.need(kind)).st_ino} for kind in ("rdir", "state")}
        self.record = {"schema": 2, "run": self.run, "project": self.project, "run_dir": self.rdir_real, "rdir_id": ident["rdir"],
                       "state_id": ident["state"], "session": {"id": found[0], "created": found[1], "name": found[2]}, "panes": [], "pane_procs": {}, "prepared": now()}
        self.problem = None
        self._write()

    def check_place(self, place):
        """Raise unless `place` is `wt-<id>` directly under this run's directory: the one shape of place a run ever makes a tree at."""
        place = Path(os.path.abspath(str(place)))
        if not PLACE.fullmatch(place.name) or os.path.join(os.path.realpath(str(place.parent.parent)), place.parent.name) != self.rdir_real:
            raise ShadowError("{} is not this run's own place ({}/wt-<id>); nothing is made or changed there".format(place, self.rdir))

    def create(self, place, heads_before):
        """Take the identity of the tree `git worktree add` just made at `place` (before any worker exists) and record it."""
        self.check_place(place)
        if self.record is None:
            raise ShadowError(self.problem or "there is no owner record for this run; it was not prepared")
        real = os.path.realpath(str(place))
        admin = _gitfile_target(real)
        if admin is None:
            raise ShadowError("{} is not a git worktree after `git worktree add`".format(place))
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fds = {"tree": os.open(real, flags)}
        try:
            fds["admin"] = os.open(admin, flags)
        except OSError:
            os.close(fds["tree"])
            raise ShadowError("the git directory {} of {} cannot be opened".format(admin, place))
        self._close("tree", "admin")
        self.fds.update(fds)
        ident = {k: {"dev": os.fstat(fd).st_dev, "ino": os.fstat(fd).st_ino} for k, fd in fds.items()}
        self.record.update({"path": real, "tree": ident["tree"], "admin": dict(ident["admin"], path=admin), "heads_before": sorted(heads_before),
                            "created": now(), "removed": None})
        self._write()

    def hold_project(self):
        """Open the project root and its git directory (the common one, where refs/heads is) and keep them open, before the run's
        tree is made: the branch listing reads the project's refs only while both paths still name these directories
        (`check_project`). The runner holds them in memory only: a stop after the runner is gone lists no branches."""
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        found = git(self.project, "rev-parse", "--git-common-dir")
        gitdir = os.path.realpath(os.path.join(self.project, found.stdout.strip()))
        fds = {}
        try:
            for kind, path in (("project", self.project), ("gitdir", gitdir)):
                fds[kind] = os.open(path, flags)
        except OSError as error:
            for fd in fds.values():
                os.close(fd)
            raise ShadowError("the project {} or its git directory {} cannot be opened ({})".format(self.project, gitdir, error.strerror))
        self._close("project", "gitdir")
        self.fds.update(fds)
        self.gitdir = gitdir

    def check_project(self):
        """Raise unless the project root and its git directory, at their paths now, are the directories `hold_project` opened, and
        git in the project still finds that git directory. Nothing in the worker's tree is needed for this."""
        for kind, path, what in (("project", self.project, "the project"), ("gitdir", getattr(self, "gitdir", None), "the project's git directory")):
            fd = self.fds.get(kind)
            if fd is None or path is None:
                raise ShadowError("{} was not held open when the run's tree was made; its refs are not read".format(what))
            held = os.fstat(fd)
            try:
                now_ = os.lstat(path)
            except OSError:
                now_ = None
            if now_ is None or stat.S_ISLNK(now_.st_mode) or (now_.st_dev, now_.st_ino) != (held.st_dev, held.st_ino):
                raise ShadowError("{} {} is not the one the run recorded any more (moved, a link, or another directory); its refs are not read".format(what, path))
        found = git(self.project, "rev-parse", "--git-common-dir", check=False)
        if found.returncode or os.path.realpath(os.path.join(self.project, found.stdout.strip())) != self.gitdir:
            raise ShadowError("git in the project {} no longer finds the git directory the run recorded ({}); its refs are not read".format(self.project, self.gitdir))

    def _close(self, *kinds):
        for kind in kinds:
            fd = self.fds.pop(kind, None)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def close(self):
        self._close(*list(getattr(self, "fds", {})))

    def mark_removed(self):
        """The run's tree is gone: from now on anything at the place is not it, whatever its inode."""
        if self.record is not None and "path" in self.record and not self.record.get("removed"):
            self.record["removed"] = now()
            try:
                self._write()
            except OSError:
                pass

    # -- the session and the panes
    def session_ref(self):
        """The recorded {id, created, name} of the run's session; raises when there is no record to take it from."""
        if self.record is None:
            raise ShadowError(self.problem or "there is no owner record for this run, so no tmux session or pane is known to be its own")
        return self.record["session"]

    def session_alive(self, env):
        s = self.session_ref()
        return trial_session.is_session(s["id"], s["created"], env)

    def end_session(self, env):
        """Kill the run's tmux session by the id recorded at `prepare`, and only while it still has that id and created time."""
        s = self.session_ref()
        return trial_session.end_exact(s["id"], s["created"], env)

    def record_panes(self, env):
        """Add the panes tmux lists in the run's session to the ones the run recorded, and for each pane that is alive the process it
        runs: pid, process group and start time (called when a worker or reviewer has just been started, before it could move a pane
        in or write a process record). A pane's process is recorded once, the first time it is seen alive; `check_process_record`
        matches every process record against it. A dead pane's pid may already be another process's, so none is taken for it."""
        s = self.session_ref()
        try:
            if not trial_session.is_session(s["id"], s["created"], env):
                raise ShadowError("the run's tmux session {} is gone or is not the one it created; its panes were not recorded".format(s["name"]))
            found = trial_session.panes_in(s["id"], env)
            live = trial_session.live_panes_in(s["id"], env)
        except trial_session.SessionError as problem:
            raise ShadowError(str(problem))
        merged = sorted(set(self.record["panes"]) | set(found))
        procs = self.record.setdefault("pane_procs", {})
        changed = merged != self.record["panes"]
        for pane, pid in live.items():
            if pane in procs:
                continue
            start = trial_session.process_start(pid)
            try:
                pgid = os.getpgid(pid)
            except OSError:
                continue
            if start and pgid > 1:
                procs[pane] = {"pid": pid, "pgid": pgid, "start": start}
                changed = True
        if changed:
            self.record["panes"] = merged
            self._write()

    @staticmethod
    def meta_pane(task_path):
        """The pane the task's meta.json names (the worker can edit it). None when there is no meta.json (no tp-* command acts on
        such a task) or it names none. Raises ShadowError for a file that is not one plain JSON object: tp-kill reads the pane with
        jq, which also takes a stream of values or a byte-order mark, so a file Python cannot read is never taken as "no pane"."""
        task = Path(task_path)

        def refuse(why):
            return ShadowError("task {}: meta.json {}; nothing was started for the task".format(task.name, why))
        try:
            data = trial_session.read_plain(task / "meta.json")
        except FileNotFoundError:
            return None
        except OSError as error:
            raise refuse(_unreadable(error))
        try:
            meta = json.loads(data.decode("utf-8"))  # one value only; a byte-order mark is refused (json refuses it in a str)
        except (UnicodeDecodeError, ValueError, RecursionError):  # nesting too deep to decode is a refusal, not a crash
            raise refuse("is not one JSON object")
        if not isinstance(meta, dict):
            raise refuse("is not one JSON object")
        return meta.get("pane")

    def tp(self, args, env, timeout=CMD_TIMEOUT):
        """Run the tp-* command `args` against the run's state dir, through the one gate (`trial_session.start_tp` and its table TP).
        A command that can end anything (end-author, stop, review, tp-spawn) starts only after `check_stop` passed for its task, right
        before it starts. One the table does not name is refused and never started. Raises ShadowError for a refusal or for a command
        that could not run."""
        self.check_state()  # every command here runs against the state dir: only against the run's own

        def check(name, task):
            self.check_state()
            self.check_stop(self.state / "tasks" / (task or ""), env, name.rsplit(" ", 1)[-1])
        try:
            return trial_session.start_tp(args, env, cwd=str(self.rdir), timeout=timeout, check=check)
        except trial_session.SessionError as problem:
            raise ShadowError(str(problem))
        except (subprocess.TimeoutExpired, OSError) as error:
            raise ShadowError("{} could not run ({})".format(Path(str(args[0])).name, error.__class__.__name__))

    def check_stop(self, task_path, env, verb="stop"):
        """Everything checked before a tp-* command that can end anything runs for the task at `task_path`. `verb` is the last word of
        its name in trial_session.TP: end-author, stop, review or tp-spawn.

        Each of them finds the session by its name (TP_SESSION). So that name must still be the session this run created, and its
        @tp_viewing must be unset: when it matches the task, tp-kill swaps the task's pane with @tp_viewer_pane through tp-view, and
        the trial never sets either option. For a task, the pane in meta.json (end-author, stop, and review through end-author) and
        process.json (stop, through tp-kill) must be the run's own. For `stop` there must also be no queue entry, since tp-agent stop
        unlinks queue/<task>.json (through a link at queue/ too). The worker can write every one of these."""
        self.check_session(env)
        if verb == "tp-spawn":
            return  # no task yet: tp-spawn ends only the pane and the task dir it made itself
        self.check_pane(task_path, env)
        self.check_process_record(task_path)
        if verb == "stop":
            self.check_queue(Path(task_path).name)

    def check_session(self, env):
        """Raise unless the session now called by the run's name is the one this run created (its recorded id and created time), and
        its @tp_viewing is unset."""
        s = self.session_ref()
        try:
            found = trial_session.named(self.session, env)
            viewing = trial_session.session_option(self.session, "@tp_viewing", env) if found is not None else ""
        except (trial_session.SessionError, OSError, subprocess.SubprocessError) as problem:
            raise ShadowError("the tmux session {} cannot be read ({}); nothing was started".format(self.session, problem))
        if found is None or (found[0], found[1]) != (s["id"], str(s["created"])):
            raise ShadowError("the tmux session named {} is not the one this run created ({}); nothing was started in it".format(self.session, s["id"]))
        if viewing:
            raise ShadowError("the tmux session {} has @tp_viewing set ({!r}), and tp-kill would swap panes through tp-view; nothing was started".format(self.session, viewing))

    def check_queue(self, task):
        """Raise when tp-agent stop would remove a queue entry: `state/queue` is a link, or `queue/<task>.json` is there."""
        queue = self.state / "queue"
        if queue.is_symlink() or os.path.lexists(str(queue / (task + ".json"))):
            raise ShadowError("task {}: the queue holds an entry for it (or state/queue is a link), which tp-agent stop would remove; the task was not stopped".format(task))

    def check_process_record(self, task_path):
        """Raise unless `<task>/process.json` is absent or names the process this run recorded for a pane of its own: the same pane, pid
        and process group, and a pid that still has the recorded start time (a reused pid has another). The worker can write the file, so
        nothing in it is trusted alone; one that cannot be matched is not acted on and `tp-agent stop` is not run for the task."""
        task = Path(task_path)

        def refuse(why):
            return ShadowError("task {}: process.json {}; the task was not stopped through tp-agent (it keeps running until the run's session is ended)".format(task.name, why))
        try:
            rec = json.loads(trial_session.read_plain(task / "process.json").decode("utf-8"))
        except FileNotFoundError:
            return
        except OSError as error:
            raise refuse(_unreadable(error))
        except (UnicodeDecodeError, ValueError, RecursionError):  # nesting too deep to decode is a refusal, not a crash
            raise refuse("cannot be read")
        if not (isinstance(rec, dict) and isinstance(rec.get("pane"), str) and isinstance(rec.get("group"), bool)
                and all(isinstance(rec.get(k), int) and not isinstance(rec.get(k), bool) for k in ("pid", "pgid"))):
            raise refuse("is malformed")
        ident = (self.record.get("pane_procs") or {}).get(rec["pane"]) if rec["pane"] in self.record["panes"] else None
        if ident is None:
            raise refuse("names pane {}, which has no process the run recorded for a pane of its own".format(rec["pane"]))
        meta = self.meta_pane(task)
        if meta not in (None, "") and meta != rec["pane"]:
            raise refuse("names pane {}, not the task's pane {}".format(rec["pane"], meta))
        if (rec["pid"], rec["pgid"], rec["group"]) != (ident["pid"], ident["pgid"], ident["pgid"] == ident["pid"]):
            raise refuse("names a process (pid {}, group {}) that is not the one the run recorded for pane {}".format(rec["pid"], rec["pgid"], rec["pane"]))
        running = trial_session.process_start(ident["pid"])
        if running is not None and running != ident["start"]:
            raise refuse("names pid {}, which now runs a process that started at another time than the run's pane process".format(ident["pid"]))

    def check_pane(self, task_path, env):
        """Raise unless the pane the task's meta.json names is a pane this run recorded and is still in the run's session. A task that
        names no pane has nothing for `tp-agent stop` to follow and passes."""
        pane = self.meta_pane(task_path)
        if pane in (None, ""):
            return
        s = self.session_ref()
        if not isinstance(pane, str) or pane not in self.record["panes"]:
            raise ShadowError("task {}: its pane {!r} is not a pane this run recorded in its own session; the task was not stopped".format(Path(task_path).name, pane))
        found = trial_session.identity_of(pane, env)
        if found is not None and found[0] != s["id"]:
            raise ShadowError("task {}: its pane {} is in tmux session {}, not this run's; the task was not stopped".format(Path(task_path).name, pane, found[2]))

    # -- the proof
    def identity(self, kind):
        fd = self.fds.get(kind)
        if fd is not None:
            st = os.fstat(fd)
            return st.st_dev, st.st_ino
        return self.record[kind]["dev"], self.record[kind]["ino"]

    def foreign(self, path, what):
        return ShadowError("{} is {} ({}); left as it is".format(path, NOT_OURS, what))

    def prove(self, hint=None):
        """See the class. `hint` is where run.json says the tree is, used only to name a tree found there when this run made none."""
        self.why = ""
        if self.record is None or "path" not in self.record:
            if self.record is None and self.problem:
                raise ShadowError(self.problem)
            if hint is not None and os.path.lexists(str(hint)):
                raise self.foreign(hint, "this run made no tree")
            return "absent", None
        path = self.record["path"]
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return "absent", path
        if self.record.get("removed"):
            raise self.foreign(path, "something was made at the place after the run removed its own")
        if stat.S_ISLNK(st.st_mode):
            raise self.foreign(path, "a link now; the run made a directory")
        if not stat.S_ISDIR(st.st_mode) or (st.st_dev, st.st_ino) != self.identity("tree"):
            raise self.foreign(path, "a different directory than the one the run made")
        admin = self.record["admin"]["path"]
        try:
            ast = os.lstat(admin)
        except FileNotFoundError:
            self.why = "git no longer knows it: its admin entry is gone"
            return "damaged", path
        if stat.S_ISLNK(ast.st_mode) or (ast.st_dev, ast.st_ino) != self.identity("admin"):
            self.why = "its git directory {} is not the one the run's tree had".format(admin)
            return "damaged", path
        if _gitfile_target(path) != admin:
            self.why = "its .git names {} and not the run's git directory".format(_gitfile_target(path))
            return "damaged", path
        try:
            back = os.path.realpath(os.path.dirname(trial_session.read_plain(Path(admin) / "gitdir").decode("utf-8").strip()))
        except (OSError, UnicodeDecodeError):
            back = None
        listed = registered_worktrees(self.project)
        if back != path or path not in listed:
            self.why = "git's own entry for it does not point back at it"
            return "damaged", path
        branch = listed[path]
        if branch and branch in self.record["heads_before"]:
            self.why = "it is on {}, a branch that was there before the run and is not the run's".format(branch)
            return "damaged", path
        return "owned", path

    def ref_prefix(self):
        return "{}{}/".format(REF_PREFIX, self.run)

    def keep_ref(self, ref, commit):
        """Make the ref `ref` (one under this run's own prefix) point at `commit`, only when there is no such ref, and record its
        name and commit in the owner record. `drop_refs` removes a ref only by that record, and only while it still names that commit.
        A ref that is already there and was not made by this call is an error: nothing is moved."""
        if not ref.startswith(self.ref_prefix()) or not COMMIT.fullmatch(commit or ""):
            raise ShadowError("{} at {!r} is not a ref this run may make".format(ref, commit))
        if self.record is None:
            raise ShadowError(self.problem or "there is no owner record for this run; it was not prepared")
        refs = self.record.setdefault("refs", {})
        if refs.get(ref) == commit:
            return
        git(self.project, "update-ref", ref, commit, "")  # an empty old value: the ref must not exist yet
        refs[ref] = commit
        try:
            self._write()
        except OSError:
            git(self.project, "update-ref", "-d", ref, commit, check=False)  # not recorded, so not left for a cleanup that cannot know it
            refs.pop(ref, None)
            raise

    def drop_refs(self):
        """Delete the refs under this run's prefix that the run recorded, each only while it still names the recorded commit (`update-ref
        -d <ref> <commit>` fails otherwise). A ref there that the run did not record, or that names another commit, is kept. Returns
        the list of those, and the caller reports them."""
        listing = git(self.project, "for-each-ref", "--format=%(refname) %(objectname)", self.ref_prefix(), check=False).stdout.splitlines()
        kept = []
        for line in listing:
            ref, _, oid = line.partition(" ")
            want = (self.record or {}).get("refs", {}).get(ref)
            if want is None or want != oid or git(self.project, "update-ref", "-d", ref, want, check=False).returncode:
                kept.append(ref)
        return kept

    def remove_admin(self):
        """Remove the run's own git admin directory when it is still there and is the very one recorded (not `git worktree prune`,
        which would also drop entries that belong to other lanes)."""
        if self.record is None or "admin" not in self.record:
            return
        admin = self.record["admin"]["path"]
        try:
            st = os.lstat(admin)
        except FileNotFoundError:
            return
        if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode) and (st.st_dev, st.st_ino) == self.identity("admin"):
            shutil.rmtree(admin)


def cleanup(rdir, env=None, owner=None):
    """End what the run left: its tasks (only those whose pane is one the run recorded), its tmux session (only the one it created, by
    the id recorded), then the worktree (only a tree proven to be this attempt's, and only once the worker cannot be running), the
    shadow refs. Idempotent and total: every step is attempted whatever the others did. Returns the list of what could not be done.
    A step that would need the run directory or the state dir is refused when either is a link or is not the one prepared."""
    mine = owner is None
    try:
        owner = owner or Owner(rdir)
    except ShadowError as problem:
        return ["not cleaned: {}".format(problem)]
    try:
        return _cleanup(owner, env)
    finally:
        if mine:
            owner.close()


def _cleanup(owner, env):
    project, run = owner.project, owner.run
    try:
        hint = Path(owner.read().get("worktree") or "")
    except ShadowError:
        hint = None
    leftovers = []
    run_env = trial_session.env_for(owner.state, owner.session, env)

    def attempt(label, fn):
        try:
            fn()
        except Exception as problem:
            leftovers.append("{}: {}".format(label, problem))

    def end_tasks():
        owner.check_state()
        tasks = owner.state / "tasks"
        refused = []
        # the worker can make any number of task dirs: the step has one budget, and the session kill ends what it did not reach
        deadline = time.monotonic() + STOP_TASKS_S
        for path in sorted(tasks.iterdir()) if tasks.is_dir() else []:
            if not (path / "meta.json").is_file() or (path / "stopped.json").exists() or (path / "exit").exists():
                continue
            task = path.name
            left = deadline - time.monotonic()
            if left <= 0:
                refused.append("tasks from {} on were not stopped within {} s".format(task, STOP_TASKS_S))
                break
            try:
                status = owner.tp([ROOT / "bin" / "tp-agent", "inspect", task], run_env, timeout=left)
                try:
                    state_name = json.loads(status.stdout).get("status") if status.returncode == 0 else None
                except (ValueError, AttributeError):
                    state_name = None
                if state_name not in trial_session.ACTIVE and state_name is not None:
                    continue  # nothing to stop: no pane is followed
                # through the gate: `Owner.check_stop` runs right before it (session, @tp_viewing, meta.json, process.json, the queue)
                left = deadline - time.monotonic()
                if left <= 0:
                    refused.append("tasks from {} on were not stopped within {} s".format(task, STOP_TASKS_S))
                    break
                stop = owner.tp([ROOT / "bin" / "tp-agent", "stop", task], run_env, timeout=left)
            except ShadowError as problem:
                refused.append(str(problem))
                continue
            if stop.returncode:
                refused.append("task {} could not be stopped: {}".format(task, stop.stderr.strip()))
        if refused:
            raise ShadowError("; ".join(refused) + " (the session is ended instead)")

    unsure = []

    def end_session():
        try:
            owner.end_session(run_env)
        except trial_session.SessionError:
            unsure.append(True)  # the recorded id is held by a session that is not this run's: it was not ended, so a worker may be in the tree
            raise

    def drop_worktree():
        if unsure:
            raise ShadowError("the run's session could not be shown to be gone, so the worker may still be running; the tree was not removed")
        if owner.record is not None and owner.session_alive(run_env):
            raise ShadowError("the session {} is still there, so the worker may still be running; the tree was not removed".format(owner.session))
        state, real = owner.prove(hint)  # raises for anything that is not this attempt's tree
        if state == "owned":
            lane_deps.unlink_shared(project, real)
            state, real = owner.prove(hint)  # before the removal itself
            if state == "owned":
                git(project, "worktree", "remove", "--force", real, check=False)
        if real is not None and os.path.lexists(real):  # damaged, or git would not remove it: the directory is the run's by identity
            state, real = owner.prove(hint)
            if state in ("owned", "damaged") and os.path.lexists(real):
                remove_tree(real)
        owner.remove_admin()
        owner.mark_removed()
        owner.try_update(worktree_removed=now())

    def drop_refs():
        kept = owner.drop_refs()
        if kept:
            raise ShadowError("kept {}: the run did not record making them, or they name another commit than the one it recorded".format(", ".join(kept)))

    attempt("tasks", end_tasks)
    attempt("session", end_session)
    attempt("worktree", drop_worktree)
    attempt("refs", drop_refs)
    return leftovers


# ---------------------------------------------------------------------------------------------------------------------- stop

def pid_is_run(pid, rdir):
    """Whether `pid` is the runner of the run whose directory is `rdir`: its command line names bin/tp-trial and ends in `--run-dir`
    and that whole directory, project and run both (the run name alone is the same in every project). Never signals a pid on a
    guess: a recorded pid may have been reused, or edited by the worker."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return False
    out = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)], text=True, capture_output=True)
    if out.returncode != 0:
        return False
    tails = {" --run-dir " + p for p in (os.path.abspath(str(rdir)), os.path.realpath(str(rdir)))}
    return any("tp-trial" in line and any(line.rstrip().endswith(t) for t in tails) for line in out.stdout.replace("\\", "").splitlines())


def stop(rdir, env=None, wait_s=120.0, sleep=time.sleep, clock=time.monotonic):
    """End a run early: signal its runner (its own cleanup then runs), wait for it to go, then run the idempotent cleanup anyway.
    A run that already finished is only cleaned. Returns the run.json content. A run directory that is a link, or is not the one
    prepared for the run, is not read or written and no process is signalled: the run's session is still ended by its recorded id,
    and the call then raises, naming what was refused."""
    owner = Owner(rdir)
    try:
        try:
            info = owner.read()
        except ShadowError:
            info = {}
        pid = info.get("pid")
        if info and info.get("phase") != "done" and pid_is_run(pid, owner.rdir):
            os.kill(pid, signal.SIGTERM)
            deadline = clock() + wait_s
            while pid_is_run(pid, owner.rdir) and clock() < deadline:  # the runner exiting is the event; the deadline only bounds it
                sleep(0.2)
            owner.load()  # the runner's own cleanup has marked what it removed
        leftovers = cleanup(rdir, env, owner)
        if "rdir" not in owner.fds:
            raise ShadowError("{}; its tmux session was ended if it was there (cleanup left: {})".format(owner.refused.get("rdir") or "the run directory is not held", "; ".join(leftovers) or "nothing"))
        info = owner.read()
        if info.get("phase") != "done":
            owner.update(outcome=info.get("outcome") or "stopped", phase="done", finished=now(), cleanup_errors=leftovers)
        return owner.read()
    finally:
        owner.close()


def list_runs(project):
    """run.json content of every run under the project, oldest first."""
    root = trial_session.trial_root(project)
    out = []
    try:
        names = sorted(p for p in root.iterdir() if p.is_dir() and trial_session.RUN.fullmatch(p.name))
    except OSError:
        return out
    for path in names:
        try:
            out.append(read_run(path))
        except ShadowError:
            continue
    return sorted(out, key=lambda r: r.get("created") or "")
