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

Ownership: `make_worktree` records what it created (real path, git admin directory, branches that existed) in run.json and in memory;
before every git command in the worktree and every removal of it, `worktree_proof` checks the place against that record (no link,
same real path, same admin directory, listed by git, not on a pre-existing branch). A tree the run did not create, or one swapped
in since, is left as it is and the step fails. The attempt clock (§8) is checked before the worktree and before tp-spawn.

The record comes from lib/trial_record.py (lane L2). Until it exists, lib/trial_record_stub.py (same call shape) is used.
`gate_runs` is L2's field and is never read here.
"""

import datetime
import fcntl
import importlib.util
import json
import os
import re
import shutil
import signal
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
    worktree = rdir / "worktree"
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
        self.info = read_run(self.rdir)
        self.run = self.info["run"]
        self.project = self.info["project"]
        self.worktree = Path(self.info["worktree"])
        self.state = Path(self.info["state"])
        self.main_state = Path(self.info["main_state"])
        self.task_id = self.info["task_id"]
        self.env = trial_session.env_for(self.state, self.info["session"], env)
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
        self.owned = None

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
        self.info = update_run(self.rdir, **fields)

    def command(self, args, timeout=CMD_TIMEOUT):
        try:
            return subprocess.run([str(a) for a in args], text=True, capture_output=True, timeout=timeout, env=self.env, cwd=str(self.rdir))
        except (subprocess.TimeoutExpired, OSError) as error:
            raise ShadowError("{} could not run ({})".format(Path(str(args[0])).name, error.__class__.__name__))

    def agent(self, *args, timeout=CMD_TIMEOUT):
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
        self.heads_before = heads(self.project)
        git(self.project, "worktree", "add", "--detach", str(self.worktree), self.info["base_commit"])
        self.owned = ownership_record(self.worktree, self.heads_before)  # what this run created: every later git command proves it first
        self.note(owned_worktree=self.owned, worktree_removed=None)
        shared = lane_deps.share(self.project, self.owned["path"], self.info["base_commit"])
        self.note(deps=shared["deps"], deps_warnings=[w if isinstance(w, str) else json.dumps(w, sort_keys=True) for w in shared["warnings"]])

    def source(self):
        return read_source(self.main_state, self.info["source_task"])

    def prove_worktree(self):
        """(state, realpath) of the run's worktree, proven again now against the record made when this run created it (not run.json:
        the worker can reach that file). Raises when the place holds anything else."""
        return worktree_proof(self.rdir, self.info, self.owned)

    def wt_git(self, *args, check=True):
        """git in the run's worktree, run on the proven real path and only after proving it, every time: the worker has write access
        to its tree and the directory around it and can swap the tree for a link to another one while it runs."""
        state, real = self.prove_worktree()
        if state != "owned":
            raise ShadowError("the run's worktree {} is {}; no git command was run there".format(self.worktree, "not there" if state == "absent" else state))
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
        run = self.agent("end-author" if outcome == "settled" else "stop", self.task_id)
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
        (self.rdir / "final.patch").write_text(patch, encoding="utf-8")
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
        result = self.wait(self.review_id, self.clock() + self.info.get("review_timeout_s", 1800), "review-wait")
        self.note(review_wait=result)
        if result == "timeout":
            self.agent("stop", self.review_id)
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
        return cleanup(self.rdir, self.env)

    # -- the whole run
    def execute(self):
        self.note(pid=os.getpid(), started=now(), phase="starting")
        error = None
        try:
            try:
                check_ownership(self.rdir, self.info)  # the worktree is made, reset and read below: only at the run's own path
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
            record_normal(self.rdir, self.env, self.records)
        except Exception as problem:  # the normal arm's record is best effort: a later `tp-trial normal` redoes it
            self.note(normal_record_error="{}: {}".format(problem.__class__.__name__, problem))
        return read_run(self.rdir)


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


def record_normal(rdir, env=None, record_module=None):
    """Write the record of the normal arm of the pair (the source task, built from Main's task dir, read only) with the same
    builder. Safe to repeat: L2's report keeps the last record of a pair and arm, so a later call, once the normal task's review
    has landed, supersedes an earlier one. Returns the record or None when the source task is gone."""
    info = read_run(rdir)
    records = record_module or load_record_module()
    main_state = Path(info["main_state"])
    path = find_task(main_state, info["source_task"])
    if path is None:
        return None
    files, lines = _diff_counts(_json(path / "meta.json").get("workdir"), info["base_commit"])
    record = records.build(path, "normal", "shadow", info["pair_id"], state_dir=main_state, diff_files=files, diff_lines=lines)
    record.update({"run": info["run"], "source_task": info["source_task"], "outcome": "completed"})
    records.append(info["project"], record)
    update_run(rdir, normal_recorded=now())
    return record


# ------------------------------------------------------------------------------------------------------------------ cleanup

def _gitfile_target(path):
    """Where the `.git` file of the worktree at `path` points (the admin directory), as a real path, or None."""
    try:
        text = (Path(path) / ".git").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return os.path.realpath(text[len("gitdir:"):].strip()) if text.startswith("gitdir:") else None


def ownership_record(worktree, heads_before):
    """What proves a worktree is the one this run created: written right after `git worktree add` succeeded, so it exists only for
    a tree the run made. The real path, git's admin directory for it, and the branches that existed before (a branch of those is
    never the run's own)."""
    real = os.path.realpath(str(worktree))
    admin = _gitfile_target(real)
    if admin is None:
        raise ShadowError("{} is not a git worktree after `git worktree add`".format(worktree))
    return {"path": real, "gitdir": admin, "heads_before": sorted(heads_before), "created": now()}


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


def worktree_proof(rdir, info, owned=None):
    """Prove, now, that the worktree of the run at `rdir` is the one the run created. Returns (state, real path):

      owned     a real directory (no link) at the recorded real path, whose `.git` names the recorded admin directory, that git lists
                at that path, on a detached HEAD or a branch the run made, and whose admin directory points back at it
      absent    the run created none, or created one that is gone; nothing is there
      gone      a directory is there but git no longer knows it (its admin entry was pruned): the run's own leftovers
      unowned   something is at the place that the run did not create (a worktree that existed first, or one made since)

    and raises ShadowError when the run did create a tree and what is there now is not it (a link, another place, another worktree).
    `owned` is the record the runner kept in memory; without it the one in run.json is used (a stop from another process)."""
    check_ownership(rdir, info)
    path = Path(str(info["worktree"]))
    record = owned if owned is not None else info.get("owned_worktree")
    if not isinstance(record, dict) or info.get("worktree_removed") and owned is None:
        return ("unowned", None) if os.path.lexists(str(path)) else ("absent", None)
    if path.is_symlink():
        raise ShadowError("{} is a link now; the run created a directory there".format(path))
    real = os.path.realpath(str(path))
    if real != record.get("path"):
        raise ShadowError("{} resolves to {} and not to the worktree the run created ({})".format(path, real, record.get("path")))
    if not os.path.lexists(real):
        return "absent", real
    if not os.path.isdir(real):
        raise ShadowError("{} is not a directory".format(path))
    admin = record.get("gitdir")
    listed = registered_worktrees(info["project"])
    if real not in listed:
        if admin and not os.path.exists(admin):
            return "gone", real
        raise ShadowError("git does not list {} though the run's admin directory {} still exists".format(real, admin))
    if _gitfile_target(real) != admin:
        raise ShadowError("{} names another git directory ({}) than the run's ({})".format(real, _gitfile_target(real), admin))
    try:
        back = os.path.realpath(os.path.dirname((Path(admin) / "gitdir").read_text(encoding="utf-8").strip()))
    except (OSError, UnicodeDecodeError):
        back = None
    if back != real:
        raise ShadowError("the run's git directory {} points at {} and not at {}".format(admin, back, real))
    branch = listed[real]
    if branch and branch[len("refs/heads/"):] in [h[len("refs/heads/"):] for h in record.get("heads_before", ())]:
        raise ShadowError("{} is on {}, a branch that was there before the run and is not the run's".format(real, branch))
    return "owned", real


def check_ownership(rdir, info):
    """Raise unless run.json names this run's own things: the run directory is `<project>/.tmux-pi/trial/<run>`, and the state dir and
    the worktree are `state` and `worktree` directly under it, not links to another place. Cleanup checks this before it touches
    anything: run.json is a file the worker can reach, and `git worktree remove --force` on a path out of an altered one would
    delete another lane's tree."""
    here = Path(os.path.realpath(str(rdir)))
    try:
        expected = Path(os.path.realpath(str(trial_session.run_dir(info["project"], info["run"]))))
    except (trial_session.SessionError, TypeError, KeyError) as problem:
        raise ShadowError("run.json names no valid run or project ({})".format(problem))
    if expected != here:
        raise ShadowError("run.json puts the run at {} but it is at {}".format(expected, here))
    for key in ("worktree", "state"):
        path = Path(str(info.get(key) or ""))
        if path.name != key or not path.is_absolute() or Path(os.path.realpath(str(path))) != here / key:
            raise ShadowError("{} {} is not the run's own {}/{} (it resolves to {})".format(
                key, path, here, key, os.path.realpath(str(path)) if str(path) else "nothing"))


def cleanup(rdir, env=None):
    """End what the run left: trial tasks that still run, the worktree (and its shared node_modules links), the shadow refs, the
    session. Idempotent and total: every step is attempted whatever the others did. Returns the list of what could not be done."""
    info = read_run(rdir)
    project, worktree, state = info["project"], Path(info["worktree"]), Path(info["state"])
    leftovers = []
    try:
        check_ownership(rdir, info)
    except ShadowError as problem:
        return ["not cleaned: {}".format(problem)]
    run_env = trial_session.env_for(state, info["session"], env)

    def attempt(label, fn):
        try:
            fn()
        except Exception as problem:
            leftovers.append("{}: {}".format(label, problem))

    def end_tasks():
        tasks = state / "tasks"
        for path in sorted(tasks.iterdir()) if tasks.is_dir() else []:
            if not (path / "meta.json").is_file() or (path / "stopped.json").exists() or (path / "exit").exists():
                continue
            task = path.name
            status = subprocess.run([str(ROOT / "bin" / "tp-agent"), "inspect", task], text=True, capture_output=True, env=run_env, timeout=CMD_TIMEOUT)
            try:
                state_name = json.loads(status.stdout).get("status") if status.returncode == 0 else None
            except ValueError:
                state_name = None
            if state_name in trial_session.ACTIVE or state_name is None:
                stop = subprocess.run([str(ROOT / "bin" / "tp-agent"), "stop", task], text=True, capture_output=True, env=run_env, timeout=CMD_TIMEOUT)
                if stop.returncode:
                    raise ShadowError("task {} could not be stopped: {}".format(task, stop.stderr.strip()))

    def drop_worktree():
        proven = [None]

        def prove():
            state, real = worktree_proof(rdir, info, None)
            if state == "unowned":
                raise ShadowError("{} was not created by this run; left as it is".format(worktree))
            proven[0] = state
            return state, real
        state, real = prove()
        if state in ("owned", "gone"):
            if state == "owned":
                lane_deps.unlink_shared(project, real)
                state, real = prove()  # before the removal itself: the tree can be swapped between two steps
            if state == "owned":
                git(project, "worktree", "remove", "--force", real, check=False)
            if os.path.lexists(real):  # a tree git no longer knows, or would not remove: the run's own, under the run dir
                state, real = prove()
                if state in ("owned", "gone") and os.path.lexists(real):
                    shutil.rmtree(real)
            update_run(rdir, worktree_removed=now())
        if proven[0] is not None:
            git(project, "worktree", "prune", check=False)

    def drop_refs():
        listing = git(project, "for-each-ref", "--format=%(refname)", "{}{}/".format(REF_PREFIX, info["run"]), check=False).stdout.split()
        for ref in listing:
            git(project, "update-ref", "-d", ref)

    def end_session():
        trial_session.end(info["run"], run_env)

    attempt("tasks", end_tasks)
    attempt("worktree", drop_worktree)
    attempt("refs", drop_refs)
    attempt("session", end_session)
    return leftovers


# ---------------------------------------------------------------------------------------------------------------------- stop

def pid_is_run(pid, run):
    """Whether `pid` is this run's runner: its command line names bin/tp-trial and the run's directory. Never signals a pid on a
    guess, since a recorded pid may have been reused."""
    if not isinstance(pid, int) or pid <= 1:
        return False
    out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], text=True, capture_output=True)
    return out.returncode == 0 and "tp-trial" in out.stdout and "/{}".format(run) in out.stdout.replace("\\", "")


def stop(rdir, env=None, wait_s=120.0, sleep=time.sleep, clock=time.monotonic):
    """End a run early: signal its runner (its own cleanup then runs), wait for it to go, then run the idempotent cleanup anyway.
    A run that already finished is only cleaned. Returns the run.json content."""
    info = read_run(rdir)
    pid = info.get("pid")
    if info.get("phase") != "done" and pid_is_run(pid, info["run"]):
        os.kill(pid, signal.SIGTERM)
        deadline = clock() + wait_s
        while pid_is_run(pid, info["run"]) and clock() < deadline:  # the runner exiting is the event; the deadline only bounds it
            sleep(0.2)
    leftovers = cleanup(rdir, env)
    info = read_run(rdir)
    if info.get("phase") != "done":
        update_run(rdir, outcome=info.get("outcome") or "stopped", phase="done", finished=now(), cleanup_errors=leftovers)
    return read_run(rdir)


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
