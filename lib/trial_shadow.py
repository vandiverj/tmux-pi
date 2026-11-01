"""Shadow dispatch (design §3): run Qwen on the same brief a normal coder got, in a throwaway worktree, inside its own trial
session, review its diff and write a record. The real lane, step, branch and Main's state dir are never touched.

    prepare(...)   validate, check the guard, create the run (run.json) and its session; nothing is started yet
    launch(...)    start `bin/tp-trial run <run>` detached, so the caller is never blocked
    execute(...)   the run itself (what `tp-trial run` calls): worktree at base_commit, shared deps, tp-spawn, wait, soft reset,
                   review, record, cleanup, bounded by trial.attempt_timeout_s
    cleanup(...)   idempotent: end running trial tasks, drop the worktree and the shadow refs, end the session
    stop(...)      end a run early

Everything a run writes is under <project>/.tmux-pi/trial/<run>/ (run.json, request.md, final.patch, state/) and the shared
<project>/.tmux-pi/trial/records.jsonl. Main's state dir is read in two places only, both reads: the source task's files
(`read_source`, and `record_normal` for the normal arm's record) and the local-slot count of the guard.

What the run does to git: `git worktree add --detach` at the source task's base_commit (nothing under refs/heads), a temporary
ref refs/tmux-pi/shadow/<run>/head that keeps the worker's commits reachable until the record is written, and removal of both at
the end, on success and on failure. A branch the worker made in the worktree (`git checkout -b`) is detached and deleted; no
other branch is ever deleted.

Ownership (the worker runs as the same OS user, is not sandboxed, and can write run.json, its task's meta.json and everything beside
its tree, so nothing there proves anything). Every destructive act (a signal, a tmux kill, a worktree removal, a write into a run
directory) first matches an identity this attempt recorded itself; the full rule is on `Owner`. In short: `prepare` records the
identities of the run directory, the state dir and the tmux session before any worker exists; the runner holds the directories open
and writes run.json through them; a session is killed by its recorded id, and only while that id still has the recorded name and
state dir; `tp-agent stop` runs only for a pane the run recorded in its own session; the worktree is an attempt-unique place
`<run dir>/wt-<id>`, proven by the (device, inode) of the tree and of its git admin dir, both held open. Project, state dir, session
and task id come from where the run dir is, never from run.json. Whatever does not match is left as it is. The attempt clock (§8) is
checked before the worktree and before tp-spawn.

KNOWN LIMIT (not defended): a stop of a runner that has died reads those identities from the owner file
`<project>/.tmux-pi/trial-owner/<run>.json`, which the same user, so the worker, can rewrite (see `Owner`).

The record comes from lib/trial_record.py (lane L2). Until it exists, lib/trial_record_stub.py (same call shape) is used.
`gate_runs` is L2's field and is never read here.
"""

import datetime
import fcntl
import importlib.util
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
PLACE = re.compile(r"^wt-[0-9a-f]{12}$")  # the attempt-unique name of a run's worktree
OWNER_DIR = "trial-owner"
NOT_OURS = "not the tree this run created"
SETTLED = ("settled", "completed")
OTHER_LANES = "## Paths other lanes own"
GIT_TIMEOUT = 120
CMD_TIMEOUT = 300
# A trial-only line in the packet (design §12 question 2: the shadow skips tests that need the shared database, because two
# runs on one database would collide). Everything else in the packet is what the normal coder got.
TRIAL_NOTE = ("This is a trial run in a throwaway worktree. Do not run tests that need the shared database or any other shared "
              "service, and say that you skipped them.")

def workdir_text(worktree, project):
    """What the worker is told about where it works. Phase 0 found a shadow editing the main checkout because nothing named its
    working tree: the absolute path goes first in the request and again in the packet's notes."""
    return ("Your working directory is {wt}. Read and change repository files only there, using paths under {wt}. The main checkout "
            "{proj} is not yours: do not read it for the task's files or change anything in it.").format(wt=worktree, proj=project)


TERMINAL_OUTCOMES = ("completed", "timeout", "worker_failed", "paused", "stopped", "spawn_failed", "error")


class ShadowError(Exception):
    """A step of the run cannot be done; the message says which and why."""


class AttemptExpired(ShadowError):
    """The attempt's wall clock (§8) was spent before the worker was started; nothing is launched."""


class Interrupted(BaseException):
    """The runner was told to stop (SIGTERM from `tp-trial stop`). A BaseException so a broad `except Exception` never eats it."""


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_record_module():
    """lib/trial_record.py (lane L2) when it exists, else the stub with the same call shape."""
    name = "trial_record" if importlib.util.find_spec("trial_record") else "trial_record_stub"
    return importlib.import_module(name)


# ---------------------------------------------------------------------------------------------------------------- run.json

def run_file(rdir):
    return Path(rdir) / "run.json"


def read_run(rdir):
    try:
        value = json.loads(run_file(rdir).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ShadowError("cannot read {}: {}".format(run_file(rdir), error))
    if not isinstance(value, dict):
        raise ShadowError("{} is not an object".format(run_file(rdir)))
    return value


def update_run(rdir, **fields):
    """Merge `fields` into run.json under a lock, atomically (a reader never sees half a file). Returns the new content."""
    path = run_file(rdir)
    with open(str(Path(rdir) / ".run.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            current = {}
        current.update(fields)
        tmp = path.with_name(path.name + ".tmp.{}".format(os.getpid()))
        tmp.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(path))
    return current


def _open_in(fd, name, flags, mode=0o644):
    """`name` inside the directory held open as `fd`, never through a link."""
    return os.open(name, flags | os.O_NOFOLLOW, mode, dir_fd=fd)


def read_run_at(fd):
    """run.json of the run directory held open as `fd`."""
    try:
        with os.fdopen(_open_in(fd, "run.json", os.O_RDONLY), encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError) as error:
        raise ShadowError("cannot read run.json: {}".format(error))
    if not isinstance(value, dict):
        raise ShadowError("run.json is not an object")
    return value


def write_at(fd, name, text):
    """Write `text` as `name` in the directory held open as `fd`, atomically; nothing is written through a link."""
    tmp = "{}.tmp.{}".format(name, os.getpid())
    with os.fdopen(_open_in(fd, tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC), "w", encoding="utf-8") as handle:
        handle.write(text)
    os.rename(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)


def update_run_at(fd, **fields):
    """`update_run` for the run directory held open as `fd`: the file changed is the one in that directory, whatever the path to it
    has become."""
    lock = _open_in(fd, ".run.lock", os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
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

def git(cwd, *args, check=True, input_text=None):
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")}
    try:
        run = subprocess.run(["git", "-C", str(cwd)] + list(args), text=True, capture_output=True, timeout=GIT_TIMEOUT, env=env,
                             input=input_text)
    except (subprocess.TimeoutExpired, OSError) as error:
        raise ShadowError("git {} could not run in {} ({})".format(" ".join(args[:2]), cwd, error.__class__.__name__))
    if check and run.returncode:
        raise ShadowError("git {} failed in {}: {}".format(" ".join(args[:2]), cwd, run.stderr.strip() or "exit {}".format(run.returncode)))
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
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


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
    request = (path / "request.md").read_text(encoding="utf-8", errors="replace") if (path / "request.md").is_file() else ""
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
    packet = (path / "packet.md").read_text(encoding="utf-8", errors="replace") if (path / "packet.md").is_file() else ""
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
    name, state = trial_session.ensure(project, run, env)
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


def launch(rdir, project, env=None):
    """Start the run detached (its own session, output to run.log), so the caller returns at once. Returns the pid."""
    log = open(str(Path(rdir) / "run.log"), "ab")
    child = subprocess.Popen([sys.executable, str(ROOT / "bin" / "tp-trial"), "run", "--run-dir", str(rdir)], stdin=subprocess.DEVNULL,
                             stdout=log, stderr=subprocess.STDOUT, start_new_session=True, cwd=str(rdir), env=env)
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
        self.recorded = False
        self.heads_before = set()
        self.diff = (None, None)
        self.commit_messages = None
        self.review_id = None
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
        self.info = self.owner.update(**fields)

    def command(self, args, timeout=CMD_TIMEOUT):
        self.owner.check_state()  # every command here runs against the state dir: only against the run's own
        try:
            return subprocess.run([str(a) for a in args], text=True, capture_output=True, timeout=timeout, env=self.env, cwd=str(self.rdir))
        except (subprocess.TimeoutExpired, OSError) as error:
            raise ShadowError("{} could not run ({})".format(Path(str(args[0])).name, error.__class__.__name__))

    def agent(self, *args, timeout=CMD_TIMEOUT):
        return self.command([ROOT / "bin" / "tp-agent", *args], timeout)

    def end_task(self, verb, task_id):
        """`tp-agent <verb> <task>` for a verb that ends the task's process (stop, end-author): only when the pane the task's meta.json
        names is a pane the run recorded in its own session (`Owner.check_pane`; meta.json is the worker's to edit)."""
        self.owner.check_state()
        self.owner.check_pane(self.task_path(task_id), self.env)
        return self.agent(verb, task_id)

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
        self.heads_before = heads(self.project)
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

    def wt_git(self, *args, check=True):
        """git in the run's worktree, run on the proven real path and only after proving it, every time: the worker has write access
        to its tree and the directory around it and can swap the tree for a link to another one while it runs."""
        state, real = self.prove_worktree()
        if state != "owned":
            raise ShadowError("the run's worktree {} is {}; no git command was run there".format(
                self.worktree, "not there" if state == "absent" else "damaged ({})".format(self.owner.why)))
        return git(real, *args, check=check)

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
        run = self.end_task("end-author" if outcome == "settled" else "stop", self.task_id)
        if run.returncode:
            raise ShadowError("the worker could not be ended: {}".format(run.stderr.strip() or "tp-agent exited {}".format(run.returncode)))

    def collect_work(self):
        """Read the commit messages, then soft-reset to the base so a review sees the work as an uncommitted change (a non-lane
        review diffs HEAD). Returns after saving the patch and counting the final diff."""
        self.note(phase="reset")
        base = self.info["base_commit"]
        state, _ = self.prove_worktree()
        if state == "absent":
            return
        log = self.wt_git("log", "--reverse", base + "..HEAD", "--format=%B%x00", check=False)
        self.commit_messages = [m.strip("\n") for m in log.stdout.split("\0") if m.strip()] if log.returncode == 0 else None
        head = self.wt_git("rev-parse", "-q", "--verify", "HEAD", check=False).stdout.strip()
        if head and head != base:
            git(self.project, "update-ref", "{}{}/head".format(REF_PREFIX, self.run), head)
        stray = self.wt_git("symbolic-ref", "-q", "HEAD", check=False).stdout.strip()
        if stray.startswith("refs/heads/") and stray not in self.heads_before:  # the worker made a branch here: it is not ours to leave
            self.wt_git("checkout", "-q", "--detach", check=False)
            git(self.project, "update-ref", "-d", stray, check=False)
            self.note(stray_branch=stray)
        self.wt_git("reset", "--soft", base)
        self.wt_git("add", "-A")  # the worker's new files count in the diff and in the review's file list
        stat = self.wt_git("diff", "--cached", "--numstat", base).stdout.splitlines()
        lines = 0
        for row in stat:
            added, removed = row.split("\t")[:2]
            lines += (int(added) if added.isdigit() else 0) + (int(removed) if removed.isdigit() else 0)
        self.diff = (len(stat), lines)
        patch = self.wt_git("diff", "--cached", "--binary", base).stdout
        write_at(self.owner.need("rdir"), "final.patch", patch)
        self.note(diff_files=self.diff[0], diff_lines=self.diff[1])

    def review(self):
        self.note(phase="review")
        run = self.agent("review", "--force", self.task_id)
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
        self.note(review_wait=result)
        if result == "timeout":
            try:
                self.end_task("stop", self.review_id)
            except ShadowError as problem:
                self.note(review_error="the reviewer was not stopped: {}".format(problem))
        return self.review_id

    def reviewer_model(self):
        meta = _json(self.task_path() / "meta.json")
        reviewer = meta.get("reviewer")
        return reviewer.get("model") if isinstance(reviewer, dict) and isinstance(reviewer.get("model"), str) else None

    def spawn_failed_record(self):
        """A failed spawn may leave no task dir, so the builder has nothing to read. The record still says what was tried and that it
        failed (a spawn failure is data too), with the keys the builder's records share and `no_task` set."""
        return {"schema": getattr(self.records, "SCHEMA", None), "created": now(), "pair_id": self.info["pair_id"], "arm": "qwen", "mode": "shadow",
                "task_id": self.task_id, "role": self.source_role() or self.info.get("source_role"), "diff": None, "model": self.info["model"],
                "effort": self.info["thinking"], "first_review_verdict": None, "review_rounds": 0, "reviewer_model": None,
                "commit_messages": None, "base_commit": self.info["base_commit"], "brief_drift": None, "no_task": True,
                "error": self.info.get("error")}

    def write_record(self):
        if self.recorded:
            return
        has_task = (self.task_path() / "meta.json").is_file()
        if not has_task and self.outcome != "spawn_failed":
            return
        self.recorded = True
        self.note(phase="record")
        if not has_task:
            record = self.spawn_failed_record()
            record.update({"run": self.run, "source_task": self.info["source_task"], "outcome": self.outcome, "workdir": str(self.worktree),
                           "normal_reviewer_model": self.info.get("normal_reviewer_model"), "reviewer_mismatch": None})
            self.records.append(self.project, record)
            self.note(recorded=now())
            return
        record = self.records.build(self.task_path(), "qwen", "shadow", self.info["pair_id"], state_dir=self.state, role=self.source_role(),
                                    diff_files=self.diff[0], diff_lines=self.diff[1], commit_messages=self.commit_messages)
        normal = self.info.get("normal_reviewer_model")
        mine = self.reviewer_model()
        record.update({"run": self.run, "source_task": self.info["source_task"], "outcome": self.outcome, "workdir": str(self.worktree),
                       "normal_reviewer_model": normal,
                       "reviewer_mismatch": None if not (normal and mine) else normal != mine})
        self.records.append(self.project, record)
        self.note(recorded=now())

    def source_role(self):
        meta = _json(self.task_path() / "meta.json")
        return meta.get("role") if isinstance(meta.get("role"), str) else None

    def cleanup(self):
        return cleanup(self.rdir, self.env, self.owner)

    # -- the whole run
    def execute(self):
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
                    self.review()
                    self.outcome = "completed"
                elif result not in ("timeout", "preempted"):
                    self.outcome = {"failed": "worker_failed", "stopped": "stopped", "paused": "paused"}[result]
                self.write_record()
            except Interrupted:
                self.outcome = "stopped"
                raise
            except AttemptExpired as problem:
                self.outcome, error = self.outcome or "timeout", error or str(problem)
            except ShadowError as problem:
                self.outcome = self.outcome or "error"
                error = error or str(problem)
        except Interrupted:
            self.note(interrupted=True)
        except Exception as problem:  # anything else is a failed run with a reason, never a traceback that skips the cleanup
            self.outcome = self.outcome or "error"
            error = error or "{}: {}".format(problem.__class__.__name__, problem)
        finally:
            try:
                if error:
                    self.note(error=error)
                self.outcome = self.outcome or "error"
                try:
                    self.write_record()
                except Exception as problem:
                    self.note(record_error="{}: {}".format(problem.__class__.__name__, problem))
                try:
                    self.note(phase="cleanup")
                    leftovers = self.cleanup()
                except Exception as problem:
                    leftovers = ["cleanup: {}".format(problem)]
                self.note(cleanup_errors=leftovers)
            finally:
                self.note(outcome=self.outcome, phase="done", finished=now())
        try:
            record_normal(self.rdir, self.env, self.records, self.owner)
        except Exception as problem:  # the normal arm's record is best effort: a later `tp-trial normal` redoes it
            self.note(normal_record_error="{}: {}".format(problem.__class__.__name__, problem))
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
    if not workdir or not os.path.isdir(workdir):
        return None, None
    run = git(workdir, "diff", "--numstat", base, check=False)
    if run.returncode:
        return None, None
    rows = run.stdout.splitlines()
    lines = sum((int(a) if a.isdigit() else 0) + (int(r) if r.isdigit() else 0) for a, r in (row.split("\t")[:2] for row in rows))
    return len(rows), lines


def record_normal(rdir, env=None, record_module=None, owner=None):
    """Write the record of the normal arm of the pair (the source task, built from Main's task dir, read only) with the same
    builder. Safe to repeat: L2's report keeps the last record of a pair and arm, so a later call, once the normal task's review
    has landed, supersedes an earlier one. Returns the record or None when the source task is gone."""
    mine = owner is None
    owner = owner or Owner(rdir)
    try:
        return _record_normal(owner, record_module)
    finally:
        if mine:
            owner.close()


def _record_normal(owner, record_module):
    info = owner.read()
    project, run = owner.project, owner.run
    records = record_module or load_record_module()
    main_state = Path(info["main_state"])
    path = find_task(main_state, info["source_task"])
    if path is None:
        return None
    files, lines = _diff_counts(_json(path / "meta.json").get("workdir"), info["base_commit"])
    record = records.build(path, "normal", "shadow", info["pair_id"], state_dir=main_state, diff_files=files, diff_lines=lines)
    record.update({"run": run, "source_task": info["source_task"], "outcome": "completed"})
    records.append(project, record)
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
        text = (Path(path) / ".git").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return os.path.realpath(text[len("gitdir:"):].strip()) if text.startswith("gitdir:") else None


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
      panes                after tp-spawn and after the review starts, the run records the panes tmux lists in its session. `tp-agent
                           stop` follows the pane in the task's meta.json (the worker's to edit), so it is run only for a pane
                           recorded here and still in that session (`check_pane`).
      worktree             (device, inode) of the tree root and of its git admin dir, held open by the runner (`create`): while a
                           descriptor is open no other directory can have that identity, so nothing the worker does (remove and
                           re-add a tree at the same path, git reusing the admin name, `git worktree move`, a link, a rewritten
                           `.git`) produces a directory that proves as this run's.

    KNOWN LIMIT (not fixed; the threat model is that the worker runs as the same OS user and nothing is sandboxed). A stop of a runner
    that has died holds no descriptors and no memory: it reads the identities above from the owner file
    `<project>/.tmux-pi/trial-owner/<run>.json`. That file is outside the run dir but is owned by the same user, so a worker can
    rewrite it, naming another registered worktree, session or pane, and a later `tp-trial stop` of the dead run then trusts it. A
    live runner never reads the file back (it holds what it recorded), so the limit applies only to a stop after the runner is gone.
    Nor can a check be made atomic with the act that follows it: between `check_state` or `check_pane` and the `tp-agent` command
    that uses the path, the worker could still swap what the path names.

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
            value = json.loads(self.file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as error:
            self.problem = "the owner record {} cannot be read ({}); nothing is ended or removed".format(self.file, error)
            return
        try:
            ok = (isinstance(value, dict) and value["run"] == self.run and value["project"] == self.project
                  and value["run_dir"] == self.rdir_real
                  and all(isinstance(value[k][f], int) for k in ("rdir_id", "state_id") for f in ("dev", "ino"))
                  and isinstance(value["session"]["id"], str) and isinstance(value["session"]["created"], str) and value["session"]["created"].isdigit()
                  and isinstance(value["session"]["name"], str)
                  and isinstance(value["panes"], list) and all(isinstance(p, str) for p in value["panes"]))
            if ok and "path" in value:  # the worktree part exists once the tree was made
                ok = (PLACE.fullmatch(Path(value["path"]).name) is not None
                      and all(isinstance(value[k][f], int) for k in ("tree", "admin") for f in ("dev", "ino")) and isinstance(value["admin"]["path"], str)
                      and isinstance(value["heads_before"], list))
        except (KeyError, TypeError):
            ok = False
        if not ok:
            self.problem = "the owner record {} does not describe this run; nothing is ended or removed".format(self.file)
            return
        self.record = value
        self.problem = None

    def _write(self):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_name(self.file.name + ".tmp.{}".format(os.getpid()))
        tmp.write_text(json.dumps(self.record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(self.file))

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
                       "state_id": ident["state"], "session": {"id": found[0], "created": found[1], "name": found[2]}, "panes": [], "prepared": now()}
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
        """Add the panes tmux lists in the run's session to the ones the run recorded (called when a worker or reviewer has just been
        started, before it could move a pane in)."""
        s = self.session_ref()
        try:
            if not trial_session.is_session(s["id"], s["created"], env):
                raise ShadowError("the run's tmux session {} is gone or is not the one it created; its panes were not recorded".format(s["name"]))
            found = trial_session.panes_in(s["id"], env)
        except trial_session.SessionError as problem:
            raise ShadowError(str(problem))
        merged = sorted(set(self.record["panes"]) | set(found))
        if merged != self.record["panes"]:
            self.record["panes"] = merged
            self._write()

    def check_pane(self, task_path, env):
        """Raise unless the pane the task's meta.json names is a pane this run recorded and is still in the run's session. A task that
        names no pane has nothing for `tp-agent stop` to follow and passes."""
        try:
            meta = json.loads((Path(task_path) / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        pane = meta.get("pane") if isinstance(meta, dict) else None
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
            back = os.path.realpath(os.path.dirname((Path(admin) / "gitdir").read_text(encoding="utf-8").strip()))
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
        for path in sorted(tasks.iterdir()) if tasks.is_dir() else []:
            if not (path / "meta.json").is_file() or (path / "stopped.json").exists() or (path / "exit").exists():
                continue
            task = path.name
            status = subprocess.run([str(ROOT / "bin" / "tp-agent"), "inspect", task], text=True, capture_output=True, env=run_env, timeout=CMD_TIMEOUT)
            try:
                state_name = json.loads(status.stdout).get("status") if status.returncode == 0 else None
            except ValueError:
                state_name = None
            if state_name not in trial_session.ACTIVE and state_name is not None:
                continue  # nothing to stop: no pane is followed
            try:
                owner.check_pane(path, run_env)  # meta.json is the worker's: its pane decides nothing unless the run recorded it
                owner.check_state()
            except ShadowError as problem:
                refused.append(str(problem))
                continue
            stop = subprocess.run([str(ROOT / "bin" / "tp-agent"), "stop", task], text=True, capture_output=True, env=run_env, timeout=CMD_TIMEOUT)
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
                shutil.rmtree(real)
        owner.remove_admin()
        owner.mark_removed()
        owner.try_update(worktree_removed=now())

    def drop_refs():
        listing = git(project, "for-each-ref", "--format=%(refname)", "{}{}/".format(REF_PREFIX, run), check=False).stdout.split()
        for ref in listing:
            git(project, "update-ref", "-d", ref)

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
