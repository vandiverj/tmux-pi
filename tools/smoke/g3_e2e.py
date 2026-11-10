#!/usr/bin/env python3
"""G3 end-to-end smoke (docs/plans/det-prep/release-gates.md): one scripted run through the real bin/ tools, as subprocesses.

    setup -> dispatch -> prompt -> decline -> retry -> review -> close -> merge -> collect -> remove -> leftovers

The task is one tiny deterministic coder task: add the file G3_SMOKE.md whose whole content is one given line. Each step asserts
the records it leaves (task state, lane record, review verdict file, the commit on the scratch integration branch). The run
ends with a check that no worktree, branch, lane record, tmux session or pane named for it is left, and a `finally` block
cleans up even after a failure. A JSON summary (step name, rc, seconds, what was asserted) goes to --out.

  live (the default): a real worker and a real reviewer. Costs one small coder task, one retry and one small review. Run by Main only.
  --fake            : no model, no real worker. The tools are the real ones; only the tmux calls that would start a worker, the
                      worker's replies, the reviewer's verdict and tp-route's review plan are stubbed (see README.md).

Every subprocess has a timeout and runs in its own process group, killed whole on a timeout. The repository's lane config is read in a bounded
child process, not by importing lib/ here. Every collision and leftover check compares the exact computed resource (the lane branch, the
worktree path, the lane record path, the session name), never a name fragment. Python 3.9, standard library only. Exit 0: every step and the cleanup passed. Exit 1: a step or the
cleanup failed (stderr names it).
"""
import argparse
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DEAD = ("failed", "killed", "stopped", "completed", "escalated")   # a task in one of these will not reach the state a wait is for
STEPS = ("setup", "dispatch", "prompt", "decline", "retry", "review", "close", "merge", "collect", "remove", "leftovers")
SESSION_VARS = ("TMUX", "TMUX_PANE", "TP_TASK_ID", "TP_TASK_DIR", "TP_MAIN_ID", "TP_MAIN_CWD", "TP_TRIAL_ID", "TP_SESSION", "TP_STATE_DIR", "TP_WORK_DIR")
FILE = "G3_SMOKE.md"
# The permission prompt: one Bash command whose ask is decided by code (lib/redact_hook.py in redaction "on"; lib/sensitive_read.py in
# "shadow" and "off"), not by the model. It is harmless when accepted, by construction and whatever exists on disk or in the environment:
# it runs only the no-op builtin `:` and `exit`. `:` opens nothing and prints nothing; the two operands are only text for the hooks to
# match (a dotenv-shaped name, and a secret-shaped variable name in single quotes, so the shell does not even expand it). The `exit` keeps
# the redaction hook from wrapping the command, which is what makes it ask. Built from pieces so this file names no credential.
ABSENT = "." + "env." + "g3-smoke-absent"
SECRET_VAR = "G3_SMOKE_" + "API_" + "TOKEN"
PROMPT_COMMAND = f": ./{ABSENT} '${SECRET_VAR}'; exit 3"
STEP_ID = "s1"
# No branch name or worktree path is written in this file: the integration branch (the run's scratch branch), the lane branch prefix and the
# worktree pattern are the repository's own lane config (<repo>/.tmux-pi/lanes.json, defaults when absent), computed by the lane tools' own
# functions (lanes.load_repo_config, lane_branch, lane_worktree) in a bounded child process (CONFIG_CHILD, run by Smoke.load_config). The lane
# tools read the same config, so the script and the tools always agree on the names. This process imports nothing from lib/.
CONFIG_CHILD = """\
import json, sys
sys.path.insert(0, sys.argv[1])
import lanes
repo, name = sys.argv[2], sys.argv[3]
try:
    cfg = lanes.load_repo_config(repo)
    print(json.dumps({"integration_branch": cfg["integration_branch"], "lane_branch": lanes.lane_branch(cfg, name),
                      "lane_worktree": lanes.lane_worktree(cfg, repo, name)}))
except ValueError as error:      # the lanes module's own refusal of a config (a git timeout inside it is a ValueError too)
    print(json.dumps({"refused": str(error)}))
"""
GIT_IDENTITY = {"GIT_AUTHOR_NAME": "g3-smoke", "GIT_AUTHOR_EMAIL": "g3-smoke@invalid", "GIT_COMMITTER_NAME": "g3-smoke", "GIT_COMMITTER_EMAIL": "g3-smoke@invalid"}
# How a worker starts is Main's setting, and a worker pane takes its environment from the tmux server and session, not from the process that
# ran tmux. The smoke's own session therefore carries these variables of the caller's environment (set, even empty), and no others.
# bin/tp-pi runs pi on ${TP_PI_NODE-<pinned nvm node>}: without this a pi reviewer exits at once on a machine that lacks the pinned node.
CARRIED_ENV = ("TP_PI_NODE",)
MIN_TMUX_FOR_ENV = (3, 2)     # `new-session -e NAME=VALUE`
PANE_FORMAT = "#{pane_id}\t#{pane_dead}\t#{pane_current_command}"      # list-panes -a: what the collect check reads
MARKER = "@g3_run"     # the tmux session option that says which run made a session (set in the same tmux call that creates it)
FOREIGN_COMMIT = "someone else's work: a commit that is on no other branch"      # the collision tampers: things that are not this run's
FOREIGN_LANE = {"state": "open", "note": "someone else's lane record; the smoke run must leave it exactly as it is"}
TRUST_SCREEN = """ Accessing workspace:

 {folder}

 Quick safety check: Is this a project you created or one you trust? (Like your own
 code, a well-known open source project, or work from your team). If not, take a
 moment to review what's in this folder first.

 ❯ 1. Yes, I trust this folder
   2. No, exit
"""      # Claude Code's folder-trust screen, as the first live runs showed it (--tamper trust-dialog prints it in the worker's pane)
PLAN = {"review": "required", "policy": "required",   # the --fake tp-route: what `tp-route --review-plan` prints
        "reviewer": {"worker": "pi", "model": "openai/gpt-6-sol", "thinking": "low", "independence": "different_provider"}}
FAKE_REVIEW = """APPROVED

The change adds {file} with the one required line and nothing else.

Coverage
- Read the diff of the lane branch against its base and checked the added file against the request.
- Checked that no other file changed and that the content matches the requested line exactly.

RESULT_STATUS: complete
"""
# --tamper NAME (--fake only): break one record, or one tool answer, just before the check that reads it, to show that check fails by name.
# The step each one fails at; tests/test_g3_e2e.py holds the check each must name.
TAMPERS = {
    "not-git": "setup", "bad-lane-config": "setup", "dirty-repo": "setup", "on-scratch-branch": "setup", "integration-exists": "setup", "lane-branch-exists": "setup",
    "lane-record-exists": "setup", "worktree-exists": "setup", "session-exists": "setup", "pinode-mismatch": "setup",
    "dispatch-id": "dispatch", "dispatch-queued": "dispatch", "dispatch-meta": "dispatch", "dispatch-pane": "dispatch",
    "dispatch-lane": "dispatch", "dispatch-worktree": "dispatch",
    "no-prompt": "prompt", "prompt-no-reason": "prompt", "trust-dialog": "prompt",
    "still-pending": "decline", "authorized": "decline", "no-redirect-record": "decline", "first-never-settles": "decline",
    "first-complete": "decline", "first-changed": "decline", "settled-odd": "decline", "declined-no-event": "decline", "unconfirmed-redirect": "decline",
    "first-not-stopped": "retry", "retry-same-id": "retry", "retry-meta": "retry", "task-state": "retry", "retry-incomplete": "retry",
    "retry-extra-file": "retry", "retry-wrong-line": "retry",
    "reviewer-id": "review", "review-of": "review", "same-provider": "review", "independence": "review", "reviewed-tree": "review",
    "reviewer-never-settles": "review", "review-verdict": "review", "review-incomplete": "review",
    "close-dirty": "close", "lane-record": "close",
    "merge-parents": "merge", "merge-extra-file": "merge", "scratch-commit": "merge", "main-moved": "merge",
    "collect-id": "collect", "collect-state": "collect", "collect-reaped": "collect", "pane-alive": "collect",
    "remove-abandoned": "remove", "worktree-back": "remove", "lane-back": "remove",
    "leftover-worktree": "leftovers", "leftover-branch": "leftovers", "leftover-lane-record": "leftovers", "leftover-pane": "leftovers",
    "session-kept": "leftovers",
}


FAKE_SUBMIT = '''import json, os, subprocess, sys
hook, state, pane, typed = sys.argv[1:5]
tasks = os.path.join(state, "tasks")
for ident in sorted(os.listdir(tasks)):
    try:
        meta = json.load(open(os.path.join(tasks, ident, "meta.json")))
    except (OSError, ValueError):
        continue
    if meta.get("pane") == pane:
        event = {"hook_event_name": "UserPromptSubmit", "prompt": open(typed).read()}
        env = dict(os.environ, TP_TASK_DIR=os.path.join(tasks, ident), TP_TASK_ID=ident)
        subprocess.run(["bash", hook], input=json.dumps(event).encode(), env=env)
'''   # the fake worker's UserPromptSubmit: the real report.sh on the typed text (see fake_setup)


def shell_quote(text):
    """`text` as one shell word, as shlex.quote does (this script imports only what its static test allows): the owner pastes what it prints."""
    return text if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", text) else "'" + text.replace("'", "'\"'\"'") + "'"


class SmokeError(Exception):
    """A failed step. `step` is the step's name; `rc` the return code of the command that failed, when one did."""

    def __init__(self, step, message, rc=1, timed_out=False):
        super().__init__(f"step '{step}': {message}")
        self.step, self.message, self.rc, self.timed_out = step, message, rc, timed_out


BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def base36(number):
    out = ""
    while True:
        number, digit = divmod(number, 36)
        out = BASE36[digit] + out
        if not number:
            return out


def new_run_id():
    """A run id that two runs cannot share: the time (minute), the process id and 24 random bits, as `MMDDHHMM-<pid>-<random>`. A lane name
    is at most 24 characters (lanes.valid_lane_name), and `g3-<run id>` is at most 24 (8 + 5 + 6 + 2 separators)."""
    return f"{time.strftime('%m%d%H%M', time.gmtime())}-{base36(os.getpid())}-{secrets.token_hex(3)}"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def worktree_paths(listing):
    """The paths in the output of `git worktree list --porcelain`."""
    return [line[len("worktree "):] for line in listing.splitlines() if line.startswith("worktree ")]


def same_path(listed, path):
    """Whether a path git lists is exactly `path` (the canonical path lanes.lane_worktree computed): equal, or equal after resolving
    symlinks in the listed one. Never a name or basename comparison."""
    return listed == path or os.path.realpath(listed) == path


def missing_parents(path):
    """The directories above `path` that do not exist (outermost first): what making `path` would also make. Ends at the first existing
    ancestor (the root exists), so it is bounded by the path's depth."""
    missing, parent = [], Path(path).parent
    while not os.path.lexists(parent):
        missing.append(parent)
        parent = parent.parent
    return missing[::-1]


def provider_of(worker, model):
    """The model's provider: a claude worker is Anthropic, 'provider/model' names its own, anything else is its worker."""
    if worker == "claude" or str(model).startswith(("claude", "anthropic")):
        return "anthropic"
    return str(model).split("/")[0] if "/" in str(model) else str(worker)


def bounded(argv, timeout, cwd=None, env=None, stdin=None):
    """subprocess.run with a hard bound. The child leads its own process group. On timeout the whole group is killed (a grandchild that
    hangs, such as the git a hung config-load child started, dies with it) and TimeoutExpired is raised; output a grandchild still holds
    open is given up after 2 s (subprocess.run would wait for it without end). The only place this script starts a process."""
    proc = subprocess.Popen(argv, text=True, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, cwd=cwd, env=env, start_new_session=True)
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)       # pid == pgid: start_new_session made the child its group's leader
        except OSError:
            proc.kill()
        try:
            proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe:
                    pipe.close()
        proc.wait()          # the child was killed: reaping it does not block
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


class Smoke:
    def __init__(self, args):
        self.args = args
        self.fake = args.fake
        self.stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        # Every name of this run (lane, branch, worktree, session, private socket) carries its run id: two runs started in the same second
        # share none of them. The lane name is limited to 24 characters, so the id is compact (see new_run_id).
        self.run_id = new_run_id()
        self.name = f"g3-{self.run_id}"
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]{0,23}", self.name), f"not a valid lane name: {self.name}"
        self.line = f"g3 smoke {self.stamp} {self.name}"
        self.expected = self.line + "\n"
        self.tmp = Path(tempfile.mkdtemp(prefix=f"{self.name}-")).resolve()
        self.state = self.tmp / "state"
        (self.state / "tasks").mkdir(parents=True)
        self.root = ROOT          # where bin/ is; --fake uses a copy with a stub tp-route
        self.repo = Path(args.repo).expanduser().resolve() if args.repo else None
        self.sub_timeout = args.subprocess_timeout
        # Isolation by construction. --fake never talks to the owner's tmux server: every subprocess gets its own TMUX_TMPDIR (a
        # fresh, short directory made here, before any tmux call; a unix socket path is limited to ~100 characters, so it is not
        # under self.tmp) and `-L <private socket>` is part of tmux_cmd from this line on, never added later.
        self.sock = f"g3-{os.getpid()}-{self.run_id.rsplit('-', 1)[-1]}"
        self.tmux_bin = shutil.which("tmux") or "tmux"
        self.tmux_tmp = Path(tempfile.mkdtemp(prefix="g3t-", dir="/tmp")).resolve() if self.fake else None
        self.tmux_cmd = [self.tmux_bin, "-L", self.sock] if self.fake else [self.tmux_bin]
        # Ownership: the cleanup removes only what this run made. A resource is recorded here only after its creation succeeded (or, for the
        # lane, branch and worktree that the lane tool makes, after setup proved the name free and the resource is observed afterwards:
        # `claim`). A definite refusal (a duplicate session, an existing branch) records nothing; a creation that timed out is indeterminate
        # (`session_unsure`) and is settled by the session's marker (@g3_run = the run id, set in the same tmux call that creates it).
        # "dirs": the parent directories of the worktree path that did not exist when setup looked and do now (outermost first): the pattern
        # `{repo}-{name}/checkout` makes `{repo}-{name}`, and `lane remove` leaves it. A directory that was already there is never listed.
        self.made = {"session": False, "integration": False, "lane": False, "lane_branch": False, "worktree": None, "dirs": []}
        self.session_unsure = False
        self.new_dirs = []          # the parents setup found missing (candidates for made["dirs"], decided by claim)
        # The repository's lane config, and the three names it gives (set by load_config; None until then, and the cleanup acts on
        # a name only when `made` says the run created it, which is after load_config).
        self.cfg, self.integration, self.lane_branch, self.lane_worktree = None, None, None, None
        self.clear = False         # setup verified that nothing of this run's name existed, before it made anything
        self.kept = []              # found but not made by this run: reported as leftovers, never removed
        self.steps, self.cleanup_report = [], {"ran": False, "errors": [], "leftovers": [], "kept": self.kept}
        self.step_name, self.asserted, self.commands = None, [], []
        self.ids = {}             # first, retry, reviewer task ids
        self.facts = {}
        self.started = time.monotonic()

    # ---- running things ------------------------------------------------------------------------------------------------
    def env(self):
        env = {k: v for k, v in os.environ.items() if k not in SESSION_VARS}   # no TMUX, TMUX_PANE of the caller; --fake replaces TMUX_TMPDIR below
        env.update(TP_SESSION=self.name, TP_STATE_DIR=str(self.state), TP_WORK_DIR=str(self.tmp / "work-store"))
        if self.fake:
            env["TMUX_TMPDIR"] = str(self.tmux_tmp)
            env["PATH"] = f"{self.tmp / 'fakebin'}{os.pathsep}{env['PATH']}"
            env["TP_MAIN_CWD"] = str(self.repo)
            env["TP_SEND_CONFIRM_SECONDS"] = "1"      # the fake hook has run before Enter returns; an unconfirmed-redirect tamper fails in ~3 s
        return env

    def quiet(self, argv, timeout=None, env=None):
        """Run argv with this run's env, never raising on the exit code (cleanup and read-only probes). Bounded: a command that does
        not finish in `timeout` (default --subprocess-timeout) is killed and subprocess.TimeoutExpired is raised."""
        return bounded([str(a) for a in argv], timeout or self.sub_timeout, env=env or self.env())

    def run(self, argv, rc_ok=(0,), cwd=None, timeout=120, env=None, what=None, stdin=None):
        """Run argv; record it for the step summary; a return code outside rc_ok, or a timeout, fails the step."""
        t0 = time.monotonic()
        label = what or " ".join(Path(argv[0]).name if i == 0 else str(a) for i, a in enumerate(argv))[:160]
        try:
            run = bounded([str(a) for a in argv], timeout, cwd=str(cwd or self.repo or self.tmp), env=env or self.env(), stdin=stdin)
        except subprocess.TimeoutExpired:
            self.commands.append({"cmd": label, "rc": None, "seconds": round(time.monotonic() - t0, 2)})
            raise SmokeError(self.step_name, f"`{label}` did not finish within {timeout:g} s", timed_out=True) from None
        self.commands.append({"cmd": label, "rc": run.returncode, "seconds": round(time.monotonic() - t0, 2)})
        if run.returncode not in rc_ok:
            tail = (run.stderr.strip() or run.stdout.strip())[-600:]
            raise SmokeError(self.step_name, f"`{label}` exited {run.returncode}: {tail}", run.returncode)
        return run

    def tool(self, name, *args, **kw):
        run = self.run([self.root / "bin" / name, *args], **kw)
        if self.fake and name == "tp-agent" and args[:1] == ("message",):
            self.fake_turn(args[1], args[args.index("--text") + 1])
        return run

    def fake_turn(self, ident, text):
        """Fake mode stands in for the worker's UserPromptSubmit hook: the owner's next prompt settles a request still pending, as
        settled_record in lib/redact_hook.py writes it (status, event, updated; every other field stays). Only after the prompt is
        in messages.jsonl, so a script that waits for the settle before sending it never sees it. Tamper `still-pending` never
        settles; `authorized` writes the wrong status."""
        path = self.task_dir(ident) / "permission-request.json"
        request = self.read(path, {})
        try:
            recorded = text in [json.loads(line).get("text") for line in self.text(self.task_dir(ident) / "messages.jsonl").splitlines()]
        except (OSError, ValueError):
            recorded = False
        if recorded and request.get("status") == "pending" and not self.tamper("still-pending"):
            request.update(status="authorized" if self.tamper("authorized") else "wedged" if self.tamper("settled-odd") else "declined",
                           event=None if self.tamper("declined-no-event") else "UserPromptSubmit", updated=now())
            self.write(path, json.dumps(request) + "\n")

    def agent_json(self, *args, **kw):
        out = self.tool("tp-agent", *args, **kw).stdout
        try:
            return json.loads(out)
        except ValueError:
            raise SmokeError(self.step_name, f"`tp-agent {' '.join(args[:2])}` printed no JSON: {out[:300]!r}") from None

    def git(self, *args, cwd=None, rc_ok=(0,), identity=False, stdin=None):
        env = {**self.env(), **GIT_IDENTITY} if identity else None
        run = self.run(["git", "-C", str(cwd or self.repo), *args], rc_ok=rc_ok, env=env, timeout=60, stdin=stdin)
        return run.stdout.strip() if rc_ok == (0,) else run

    def tmux(self, *args, rc_ok=(0,)):
        return self.run([*self.tmux_cmd, *args], rc_ok=rc_ok, timeout=self.sub_timeout)

    def check(self, description, ok, detail="", hint=""):
        """Record an assertion; a false one fails the step with the description, what was seen and, when given, what the operator can do."""
        if not ok:
            raise SmokeError(self.step_name, f"{description}" + (f" (saw: {detail})" if detail != "" else "") + (f". {hint}" if hint else ""))
        self.asserted.append(description)

    def wait(self, description, probe, timeout, interval=None, dead=DEAD):
        """Poll probe() -> (done, seen) until done or `timeout` seconds pass (a bounded wait: the error says what was waited for and
        the last thing seen). A seen task status in `dead` ends the wait at once: that task will not get there."""
        interval = interval or (0.05 if self.fake else 2.0)
        end = time.monotonic() + timeout
        while True:
            done, seen = probe()
            if done:
                self.asserted.append(f"reached: {description}")
                return seen
            status = seen.get("status") if isinstance(seen, dict) else seen
            if status in dead:
                raise SmokeError(self.step_name, f"task state: stopped waiting for {description}, the task is {status}")
            if time.monotonic() >= end:
                raise SmokeError(self.step_name, f"no {description} within {timeout:g} s (last seen: {status})")
            time.sleep(interval)

    def wait_settled(self, ident, description, timeout):
        def probe():
            record = self.inspect(ident)
            return record.get("status") == "settled", record
        return self.wait(description, probe, timeout)

    # ---- records -------------------------------------------------------------------------------------------------------
    def task_dir(self, ident):
        return self.state / "tasks" / ident

    def text(self, path):
        """The text of a regular file. Anything else (a FIFO, a device, a directory) is an OSError: opening a FIFO for reading blocks
        until a writer comes, and this script has no unbounded wait. Every read of a record or of the worker's file goes through here."""
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise OSError(f"{path} is not a regular file")
        return Path(path).read_text()

    def read(self, path, default=None):
        try:
            return json.loads(self.text(path))
        except (OSError, ValueError):
            return default

    def inspect(self, ident):
        """`tp-agent inspect`, which every wait on a task polls. A worker stopped at Claude Code's folder-trust dialog (inspect reports
        needs_input and trust_dialog: {dir}) writes nothing a wait is looking for, so it fails the step now, not at the wait's timeout.
        The dialog is the owner's to answer: nothing here sends a key to it."""
        record = self.agent_json("inspect", ident)
        found = record.get("trust_dialog") if isinstance(record, dict) else None
        if found:
            where = str((found.get("dir") if isinstance(found, dict) else None) or self.facts.get("worktree") or self.lane_worktree)
            raise SmokeError(self.step_name, f"worker stopped at Claude Code's folder-trust dialog for {where}; "
                                             f"the owner runs bin/tp-claude-bootstrap {shell_quote(where)} once, then reruns")   # a command the owner pastes: its operand is quoted
        return record

    def lane_path(self):
        return Path(self.facts["common"]) / "tmux-pi" / "lanes" / self.name / "lane.json"

    def lane_json(self):
        return self.read(self.lane_path())

    def write(self, path, text):
        Path(path).write_text(text)

    def edit(self, path, change):
        """Rewrite the JSON file at path with change(record) applied: a test hook's way to break one field of a real record."""
        record = self.read(path, {})
        change(record)
        self.write(path, json.dumps(record) + "\n")

    def tamper(self, name):
        return self.args.tamper == name       # parse() allows --tamper only with --fake

    # ---- steps ---------------------------------------------------------------------------------------------------------
    def do_step(self, name, function):
        self.step_name, self.asserted, self.commands = name, [], []
        t0 = time.monotonic()
        entry = {"name": name, "rc": 0, "seconds": 0, "asserted": self.asserted, "commands": self.commands}
        self.steps.append(entry)
        try:
            if self.args.inject_failure == name:
                raise SmokeError(name, "injected failure (--inject-failure)")
            function()
        except SmokeError as error:
            entry.update(rc=error.rc or 1, error=error.message)
            raise
        except Exception as error:  # a bug in the script is still a failure of this step, named
            entry.update(rc=1, error=f"{type(error).__name__}: {error}")
            raise SmokeError(name, f"{type(error).__name__}: {error}") from error
        finally:
            entry["seconds"] = round(time.monotonic() - t0, 2)

    def load_config(self):
        """The repository's lane config, once: the integration branch (the run's scratch branch), the lane branch of this run and its
        worktree path, exactly as the lane tools (lib/lanes.py) compute them. The lane functions run in a child process (CONFIG_CHILD)
        through bounded(), with the run's env and --subprocess-timeout: they start git (`check-ref-format`) with no timeout of their own,
        and a hung one must not hang this process. A config the tools would refuse, or a child that does not finish, fails the step."""
        if self.cfg is not None:
            return
        label = "the repository's lane config load (lanes.load_repo_config, lane_branch, lane_worktree in a child process)"
        out = self.run([sys.executable, "-I", "-c", CONFIG_CHILD, str(self.root / "lib"), str(self.repo), self.name],
                       timeout=self.sub_timeout, what=label).stdout
        try:
            got = json.loads(out)
        except ValueError:
            raise SmokeError(self.step_name, f"{label} printed no JSON: {out[:300]!r}") from None
        if not isinstance(got, dict) or "refused" in got:
            raise SmokeError(self.step_name, f"the repository's lane config (lanes.load_repo_config) is not usable: {got.get('refused') if isinstance(got, dict) else got!r}")
        names = [got.get(key) for key in ("integration_branch", "lane_branch", "lane_worktree")]
        if not all(isinstance(name, str) and name for name in names):
            raise SmokeError(self.step_name, f"{label} gave no usable names: {got!r}")
        self.cfg = got
        self.integration, self.lane_branch, self.lane_worktree = names
        self.asserted.append("the repository's lane config loads and gives the integration branch, the lane branch and the worktree path")

    def break_repo(self):
        repo = self.repo
        if self.tamper("not-git"):
            shutil.rmtree(repo / ".git")
        elif self.tamper("bad-lane-config"):
            (repo / ".tmux-pi").mkdir(exist_ok=True)
            (repo / ".tmux-pi" / "lanes.json").write_text(json.dumps({"integration_branch": 5}) + "\n")
        elif self.tamper("dirty-repo"):
            (repo / "README.md").write_text("changed\n")
        elif self.tamper("integration-exists"):
            self.load_config()
            self.git("branch", self.integration, "HEAD")
        elif self.tamper("on-scratch-branch"):
            self.load_config()
            self.git("checkout", "-q", "-b", self.integration)
        elif self.tamper("lane-branch-exists"):      # someone's branch with a commit that is on no other branch: deleting it loses the commit
            self.load_config()
            tree = self.git("rev-parse", "HEAD^{tree}")
            self.git("branch", self.lane_branch, self.git("commit-tree", tree, "-p", "HEAD", "-m", FOREIGN_COMMIT, identity=True))
        elif self.tamper("lane-record-exists"):
            record = (repo / self.git("rev-parse", "--git-common-dir")).resolve() / "tmux-pi" / "lanes" / self.name / "lane.json"
            record.parent.mkdir(parents=True)
            record.write_text(json.dumps(FOREIGN_LANE) + "\n")
        elif self.tamper("worktree-exists"):       # someone's registered worktree at the exact path the lane would get
            self.load_config()
            self.git("worktree", "add", "-q", "--detach", self.lane_worktree, "HEAD")
        elif self.tamper("session-exists"):
            self.tmux("new-session", "-d", "-s", self.name, "sleep 600")

    def step_setup(self):
        """Every collision check comes before anything is made: a name already taken is refused, and what was found is left alone."""
        if self.fake:
            self.fake_setup()
            self.break_repo()
        repo = self.repo
        self.check("the repository is a git checkout", (repo / ".git").exists(), str(repo))
        self.load_config()
        self.facts["common"] = str((repo / self.git("rev-parse", "--git-common-dir")).resolve())     # from here the cleanup knows the repository
        self.check("the repository has no uncommitted tracked change", self.git("status", "--porcelain", "--untracked-files=no") == "")
        branch = self.git("rev-parse", "--abbrev-ref", "HEAD")
        self.facts.update(main_branch=branch, main_head=self.git("rev-parse", "HEAD"))
        self.check("the repository's own branch is not the scratch branch", branch != self.integration, branch)
        # The integration branch is the one name that cannot carry the run id: the lane tools take it from the repository's lane config
        # (and `lane remove` checks ancestry against it). So it is shared by every run on this repository, and a second run refuses here.
        self.check("the scratch integration branch does not exist yet",
                   self.git("rev-parse", "--verify", "-q", f"refs/heads/{self.integration}", rc_ok=(0, 1)).returncode == 1, self.integration,
                   hint=f"The repository's integration branch '{self.integration}' (integration_branch in .tmux-pi/lanes.json) is shared by every run: "
                        f"wait for the other run to finish, or remove the branch yourself if no run uses it (git branch -D {self.integration}). "
                        f"Nothing was changed")
        self.check("no lane branch of this name exists", self.git("rev-parse", "--verify", "-q", f"refs/heads/{self.lane_branch}", rc_ok=(0, 1)).returncode == 1,
                   self.lane_branch)
        self.check("no lane record of this name exists", not self.lane_path().parent.exists(), str(self.lane_path().parent))
        # The exact path lanes.lane_worktree gives this run's name (not a name fragment): registered in git, or anything at all there (a
        # directory, a file, a symlink even if dangling), is a collision. Refused here, before anything is made, and left exactly as found.
        taken = []
        if any(same_path(listed, self.lane_worktree) for listed in worktree_paths(self.git("worktree", "list", "--porcelain"))):
            taken.append("registered in git worktree list")
        if os.path.lexists(self.lane_worktree):
            taken.append("exists on disk")
        self.check("nothing is registered or exists at the configured worktree path", not taken, f"{self.lane_worktree}: {', '.join(taken)}",
                   hint="Nothing was changed; the path is left as it is")
        self.new_dirs = missing_parents(self.lane_worktree)
        self.check("no tmux session of this name exists", self.tmux("has-session", "-t", f"={self.name}", rc_ok=(0, 1)).returncode == 1, self.name)
        self.clear = True            # nothing of this name existed: what appears under it from here on was made by this run
        self.git("branch", self.integration, "HEAD")
        self.made["integration"] = True       # recorded after the branch exists, not before
        self.facts["integration_base"] = self.facts["main_head"]
        self.start_session()

    def step_dispatch(self):
        task = (f"In the repository root create the file {FILE}. Its whole content is exactly one line: `{self.line}` followed by a "
                f"newline. Change nothing else, do not commit, and finish with RESULT_STATUS: complete.")
        self.facts["task"] = task
        first_task = (f"Before anything else, run exactly this one Bash command, unchanged, once: {PROMPT_COMMAND}\n"
                      f"(it is a no-op that opens no file and prints nothing; its only effect is that the owner may be asked to confirm it, and if "
                      f"the owner declines, stop and wait for instructions). Then: {task}")
        try:
            item = self.agent_json("dispatch", "--role", "coder", "--lane", self.name, "--step", STEP_ID, "--repo", self.repo,
                                   "--domain", "g3 smoke: one added file", "--touches", FILE, "--worker", "claude",
                                   "--model", self.args.coder_model, "--thinking", self.args.coder_thinking, "--tests", "none",
                                   "--deliverable", "the added file", "--task", first_task)
        finally:
            self.claim()          # the dispatch made the lane (record, branch, worktree) under a name setup proved free, or failed part way: record what exists
        ident = item.get("id", "")
        self.ids["first"] = ident
        meta_path = self.task_dir(ident) / "meta.json"
        if self.tamper("dispatch-id"):
            item["id"] = ident = "reviewer-x"
        if self.tamper("dispatch-queued"):
            item["status"] = "queued"
        if self.tamper("dispatch-meta"):
            self.edit(meta_path, lambda m: m.update(lane_step="s9"))
        if self.tamper("dispatch-pane"):
            self.edit(meta_path, lambda m: m.pop("pane", None))
        self.check("dispatch returned a task id", ident.startswith("coder-"), item)
        self.check("the task was started, not queued or refused", item.get("status") not in (None, "queued"), item.get("status"))
        meta = self.read(meta_path, {})
        self.check("task record: meta.json names the lane and step", (meta.get("lane"), meta.get("lane_step")) == (self.name, STEP_ID),
                   (meta.get("lane"), meta.get("lane_step")))
        self.check("task record: a pane is recorded", str(meta.get("pane", "")).startswith("%"), meta.get("pane"))
        lane = self.lane_json() or {}
        if self.tamper("dispatch-lane"):        # what the check reads is broken; the file stays, so the cleanup can still abandon the lane
            lane["state"] = "closed"
        if self.tamper("dispatch-worktree"):
            lane["worktree"] = str(lane.get("worktree")) + "-moved"
        self.facts.update(worktree=lane.get("worktree"), branch=lane.get("branch"), base=(lane.get("base") or {}).get("commit"))
        self.check("lane record: open, on the configured lane branch, based on the scratch integration branch",
                   lane.get("state") == "open" and lane.get("branch") == self.lane_branch and self.facts["base"] == self.facts["integration_base"],
                   {k: lane.get(k) for k in ("state", "branch")})
        self.check("the lane worktree exists at the configured worktree path",
                   self.facts["worktree"] == self.lane_worktree and Path(self.facts["worktree"] or "/nonexistent").is_dir(), self.facts["worktree"])

    def step_prompt(self):
        first = self.ids["first"]
        if self.fake and self.tamper("trust-dialog"):     # the worker's pane shows Claude Code's folder-trust screen for the lane worktree, as in the first live runs
            self.write(self.tmp / "trust-screen.txt", TRUST_SCREEN.format(folder=self.facts["worktree"]))
        elif self.fake and not self.tamper("no-prompt"):
            reason = "" if self.tamper("prompt-no-reason") else "This command reads a secret environment variable; its form means output cannot be redacted."
            self.write(self.task_dir(first) / "permission-request.json",
                       json.dumps({"status": "pending", "target": "a secret environment variable", "reason": reason, "call": "toolu_g3", "updated": now()}) + "\n")
            self.write(self.task_dir(first) / "state.json", json.dumps({"state": "permission", "tool": "Bash", "updated": now()}) + "\n")

        def probe():
            record = self.inspect(first)
            return record.get("status") == "needs_input" and (record.get("permission") or {}).get("status") == "pending", record
        record = self.wait("permission prompt (status needs_input with a pending permission request)", probe, self.args.prompt_wait,
                           dead=DEAD + ("settled",))   # a worker that finished without asking will not ask
        permission = record["permission"]
        self.check("task record: no folder-trust dialog stopped the worker (trust_dialog absent from inspect)", "trust_dialog" not in record, record.get("trust_dialog"))
        self.check("task record: permission-request.json is pending and carries a reason", permission.get("status") == "pending" and bool(permission.get("reason")), permission)

    def step_decline(self):
        """The order is the contract. Claude Code fires no PermissionDenied for an interactive reject, so the request stays `pending`
        until the asker's next UserPromptSubmit (or Stop) settles it `declined` (lib/redact_hook.py settle_declined). The redirect is
        that next prompt: send it first, check it is recorded, and only then wait for the request to leave pending."""
        first = self.ids["first"]
        pane = self.read(self.task_dir(first) / "meta.json", {}).get("pane")
        self.tmux("send-keys", "-t", pane, "Escape")           # decline the dialog, the way Main does
        redirect = ("Do not run that command: the request was declined. Do nothing else in this attempt and make no change. "
                    "Reply only with the line RESULT_STATUS: incomplete.")
        # exit 1 is "typed, not confirmed" (its JSON is still printed); only exit 0 with submitted: true counts. Either way the
        # failure names the tool's own words.
        run = self.tool("tp-agent", "message", first, "--text", redirect, rc_ok=(0, 1))    # fake mode: the fake hook settles the request once this is recorded
        try:
            sent = json.loads(run.stdout)
        except ValueError:
            sent = {}
        self.check("tp-agent message: the redirect was confirmed submitted (a promptLog record carries its delivery id)",
                   run.returncode == 0 and sent.get("submitted") is True,
                   f"exit {run.returncode}, {sent or run.stdout[:200]!r}: {(run.stderr.strip() or run.stdout.strip())[-400:]}")
        if self.tamper("no-redirect-record"):
            (self.task_dir(first) / "messages.jsonl").unlink()
        try:
            sent = [json.loads(line).get("text") for line in self.text(self.task_dir(first) / "messages.jsonl").splitlines()]
        except FileNotFoundError:
            sent = []
        self.check("task record: messages.jsonl holds the redirect message sent to the worker", redirect in sent, sent)

        def left_pending():
            status = (self.inspect(first).get("permission") or {}).get("status")
            return status != "pending", status
        status = self.wait("the permission request to leave pending after Escape", left_pending, self.args.prompt_wait)
        request = self.read(self.task_dir(first) / "permission-request.json", {})
        self.check("task record: the declined request is not authorized", status != "authorized", status)
        self.check("task record: the request is declined (by the redirect's turn) or denied", status in ("declined", "denied"), status)
        if status == "declined":
            self.check("task record: a declined request carries the event that settled it", request.get("event") in ("UserPromptSubmit", "Stop"), request.get("event"))
        self.facts["decline"] = {"status": status, "event": request.get("event")}
        if self.fake and not self.tamper("first-never-settles"):
            result = "RESULT_STATUS: complete" if self.tamper("first-complete") else "RESULT_STATUS: incomplete"
            self.write(self.task_dir(first) / "result.md", f"Declined; no change made.\n{result}\n")
            self.write(self.task_dir(first) / "state.json", json.dumps({"state": "waiting", "updated": now()}) + "\n")
        if self.tamper("first-changed"):
            self.write(Path(self.facts["worktree"]) / "STRAY.md", "x\n")
        record = self.wait_settled(first, "the first attempt to settle", self.args.settle_wait)
        self.check("task record: result_status incomplete (it made no change)", record.get("result_status") == "incomplete", record.get("result_status"))
        self.check("the lane worktree is still unchanged", self.git("status", "--porcelain", "--untracked-files=all", cwd=self.facts["worktree"]) == "")

    def step_retry(self):
        first, wt = self.ids["first"], self.facts["worktree"]
        self.tool("tp-agent", "stop", first)                    # a settled worker's process is alive; a lane allows one writer
        if self.tamper("first-not-stopped"):
            (self.task_dir(first) / "stopped.json").unlink()
        self.check("task state: the first attempt is stopped", self.inspect(first).get("status") == "stopped", self.inspect(first).get("status"))
        item = self.agent_json("retry", first, "--reason", "the command was declined; no change was made", "--task", self.facts["task"],
                               "--worker", "claude", "--model", self.args.coder_model, "--thinking", self.args.coder_thinking)
        second = item.get("id", "")
        self.ids["retry"] = second
        if self.tamper("retry-same-id"):
            second = item["id"] = first
        if self.tamper("retry-meta"):
            self.edit(self.task_dir(second) / "meta.json", lambda m: m.update(lane="other"))
        self.check("retry started a new task", bool(second) and second != first, item)
        meta = self.read(self.task_dir(second) / "meta.json", {})
        self.check("task record: the retry keeps the lane and step", (meta.get("lane"), meta.get("lane_step")) == (self.name, STEP_ID), (meta.get("lane"), meta.get("lane_step")))
        if self.fake:
            self.write(Path(wt) / FILE, "wrong\n" if self.tamper("retry-wrong-line") else self.expected)
            if self.tamper("retry-extra-file"):
                self.write(Path(wt) / "EXTRA.md", "x\n")
            result = "RESULT_STATUS: incomplete" if self.tamper("retry-incomplete") else "RESULT_STATUS: complete"
            self.write(self.task_dir(second) / "result.md", f"Added {FILE}.\n{result}\n")
            self.write(self.task_dir(second) / "state.json", json.dumps({"state": "waiting", "updated": now()}) + "\n")
            if self.tamper("task-state"):
                self.write(self.task_dir(second) / "exit", "1\n")      # the worker's process exited nonzero: the task is failed
        record = self.wait_settled(second, "the retry to settle", self.args.settle_wait)
        self.check("task record: result_status complete", record.get("result_status") == "complete", record.get("result_status"))
        self.check("the worktree holds exactly the one new file", self.git("status", "--porcelain", "--untracked-files=all", cwd=wt) == f"?? {FILE}")
        self.check("the file holds exactly the requested line", self.text(Path(wt) / FILE) == self.expected)

    def step_review(self):
        second = self.ids["retry"]
        out = self.tool("tp-review", second, timeout=180).stdout.strip().splitlines()
        if self.tamper("reviewer-id"):
            out = []
        reviewer = out[-1].strip() if out else ""
        self.check("tp-review printed a reviewer task id", bool(reviewer) and (self.task_dir(reviewer)).is_dir(), out)
        self.ids["reviewer"] = reviewer
        review_dir = self.task_dir(reviewer)
        if self.tamper("review-of"):
            self.write(review_dir / "review-of", "coder-other\n")
        if self.tamper("same-provider"):
            self.edit(self.task_dir(second) / "meta.json", lambda m: m["reviewer"].update(worker="claude", model="claude-opus-5-5"))
        if self.tamper("independence"):
            self.edit(self.task_dir(second) / "meta.json", lambda m: m["reviewer"].update(independence="same_provider"))
        if self.tamper("reviewed-tree"):
            self.edit(review_dir / "reviewed-state.json", lambda m: m.pop("tree", None))
        self.check("review record: review-of names the reviewed task", self.text(review_dir / "review-of").strip() == second)
        author, rmeta = self.read(self.task_dir(second) / "meta.json", {}), self.read(review_dir / "meta.json", {})
        planned = (author.get("reviewer") or {})
        author_provider = provider_of(author.get("worker"), author.get("model"))
        reviewer_provider = provider_of(planned.get("worker") or rmeta.get("worker"), planned.get("model") or rmeta.get("model"))
        self.check("review record: the reviewer's provider differs from the author's", author_provider != reviewer_provider,
                   f"author {author_provider}, reviewer {reviewer_provider}")
        self.check("review record: independence is different_provider", planned.get("independence") == "different_provider", planned)
        self.check("review record: reviewed-state.json names the reviewed tree", bool(self.read(review_dir / "reviewed-state.json", {}).get("tree")),
                   self.read(review_dir / "reviewed-state.json"))
        if self.fake and not self.tamper("reviewer-never-settles"):
            text = FAKE_REVIEW.format(file=FILE)
            if self.tamper("review-verdict"):
                text = text.replace("APPROVED", "REQUEST_CHANGES: the file is wrong", 1)
            if self.tamper("review-incomplete"):
                text = text.split("Coverage")[0] + "RESULT_STATUS: complete\n"
            self.write(review_dir / "result.md", text)
            self.write(review_dir / "state.json", json.dumps({"state": "waiting", "updated": now()}) + "\n")
        record = self.wait_settled(reviewer, "the reviewer to settle", self.args.review_wait)
        self.check("review verdict file: an approving verdict", record.get("verdict") in ("APPROVED", "APPROVED_WITH_NOTES"), record.get("verdict"))
        self.check("review verdict file: complete (it has a Coverage section)", record.get("review_complete") is True, record.get("review_incomplete_reason"))

    def step_close(self):
        wt = self.facts["worktree"]
        self.git("add", "--", FILE, cwd=wt)
        self.git("commit", "-q", "-m", f"g3 smoke: add {FILE}", cwd=wt, identity=True)
        tip = self.git("rev-parse", "HEAD", cwd=wt)
        self.facts["tip"] = tip
        if self.tamper("close-dirty"):
            self.write(Path(wt) / "STRAY.md", "x\n")
        self.check("the reviewed file is committed on the lane branch and the tree is clean",
                   self.git("status", "--porcelain", "--untracked-files=all", cwd=wt) == "" and tip != self.facts["base"])
        self.tool("tp-agent", "lane", "close", self.name, STEP_ID, "--repo", self.repo)
        if self.tamper("lane-record"):
            self.edit(self.lane_path(), lambda m: m.update(closed=[]))
        lane = self.lane_json() or {}
        self.check("lane record: the step is closed", [c.get("step") for c in lane.get("closed", [])] == [STEP_ID], lane.get("closed"))

    def step_merge(self):
        """A merge commit on the scratch integration branch, made without a checkout: the lane's tree, parents (branch, lane tip)."""
        tip, base = self.facts["tip"], self.git("rev-parse", f"refs/heads/{self.integration}")
        tree, parents = self.git("rev-parse", f"{tip}^{{tree}}"), ["-p", base, "-p", tip]
        rows = [row for row in self.git("ls-tree", tip).splitlines()]
        if self.tamper("scratch-commit"):        # a tree whose file holds different content
            blob = self.git("hash-object", "-w", "--stdin", stdin="wrong\n")
            rows = [row for row in rows if not row.endswith(f"\t{FILE}")] + [f"100644 blob {blob}\t{FILE}"]
            tree = self.git("mktree", stdin="\n".join(rows) + "\n")
        if self.tamper("merge-extra-file"):      # a tree with a second added file
            blob = self.git("hash-object", "-w", "--stdin", stdin="x\n")
            tree = self.git("mktree", stdin="\n".join(rows + [f"100644 blob {blob}\tEXTRA.md"]) + "\n")
        if self.tamper("merge-parents"):
            parents = ["-p", tip]
        merge = self.git("commit-tree", tree, *parents, "-m", f"g3 smoke: merge {self.lane_branch}", identity=True)
        self.git("update-ref", f"refs/heads/{self.integration}", merge, base)
        if self.tamper("main-moved"):
            self.git("update-ref", f"refs/heads/{self.facts['main_branch']}", tip)
        self.facts["merge"] = merge
        found = self.git("rev-list", "--parents", "-n", "1", merge).split()[1:]
        self.check("scratch commit: a merge of the previous branch tip and the lane tip", found == [base, tip], found)
        self.check("scratch commit: the branch changed exactly one file, added",
                   self.git("diff", "--name-status", f"{base}..{self.integration}") == f"A\t{FILE}", self.git("diff", "--name-status", f"{base}..{self.integration}"))
        self.check("scratch commit: the added file holds exactly the requested line", self.git("show", f"{self.integration}:{FILE}") == self.line)
        self.check("the repository's own branch did not move", self.git("rev-parse", self.facts["main_branch"]) == self.facts["main_head"])

    def step_collect(self):
        for key in ("retry", "reviewer"):
            ident = self.ids[key]
            record = self.agent_json("collect", ident)
            if self.tamper("collect-id"):
                record["id"] = "coder-other"
            if self.tamper("collect-state"):
                (self.task_dir(ident) / "collected.json").unlink()
            if self.tamper("collect-reaped"):
                (self.task_dir(ident) / "reaped.json").unlink()
            self.check("collect: returned the task's record", record.get("id") == ident, f"{key}: {record.get('id')}")
            self.check("task state: collected tasks are completed", self.inspect(ident).get("status") == "completed", f"{key}: {self.inspect(ident).get('status')}")
            self.check("task record: collected tasks have reaped.json (the worker's process was ended)", (self.task_dir(ident) / "reaped.json").exists(), key)
        if self.tamper("pane-alive"):          # a live pane recorded as the first task's
            made = self.tmux("new-window", "-d", "-P", "-t", f"={self.name}:", "-n", "sa:g3-alive", "-F", "#{pane_id}", "sleep 300").stdout.strip()
            self.edit(self.task_dir(self.ids["first"]) / "meta.json", lambda m: m.update(pane=made))
        self.check_no_task_pane_running(self.tmux("list-panes", "-a", "-F", PANE_FORMAT).stdout)

    def check_no_task_pane_running(self, listing):
        """`collect` keeps a collected task's pane as a dead pane (remain-on-exit) with its scrollback, and `tp-agent stop` kills the
        pane: so each task's recorded pane is absent, or present and dead. A present pane that is not dead fails, naming it and its
        command. `listing` is `list-panes -a` in PANE_FORMAT. A recorded id that another session's pane now holds counts as that pane."""
        rows = {}
        for line in listing.splitlines():
            pane, _, rest = line.partition("\t")
            rows[pane] = rest.partition("\t")
        self.facts["collect_panes"] = {}
        for key in ("first", "retry", "reviewer"):
            pane = self.read(self.task_dir(self.ids[key]) / "meta.json", {}).get("pane")
            dead, _, command = rows.get(pane, ("1", "", ""))
            self.facts["collect_panes"][key] = "absent" if pane not in rows else "dead" if dead == "1" else "running"
            self.check("no task pane is still running (each is gone, or kept dead)", dead == "1", f"{key}: {pane} is running {command!r}")

    def step_remove(self):
        before = self.lane_json()
        result = self.agent_json("lane", "remove", self.name, "--repo", self.repo)
        if self.tamper("remove-abandoned"):
            result["abandoned"] = True
        if self.tamper("worktree-back"):       # a registered worktree at the old path again
            self.git("worktree", "add", "-q", "--detach", self.facts["worktree"], "HEAD")
        archived = self.lane_json()
        if self.tamper("lane-back"):           # the record the check reads is still there
            archived = before
        self.drop_dirs([])                      # `lane remove` leaves the parent directories the worktree's creation made: they are this run's
        self.check("lane remove: removed, not abandoned, integrated in the scratch branch",
                   result.get("removed") is True and not result.get("abandoned") and self.integration in str(result.get("integrated_in")), result)
        self.check("the lane's worktree directory is gone", not Path(self.facts["worktree"]).exists(), self.facts["worktree"])
        self.check("lane record: archived, no longer listed", archived is None, archived)

    def drop_dirs(self, errors):
        """Remove the directories this run made (`made["dirs"]`, innermost first), each only if it is empty (os.rmdir refuses anything
        else): a directory that is not empty holds something that is not this run's, and stays. A failure is added to `errors`."""
        for directory in reversed(self.made["dirs"]):
            try:
                os.rmdir(directory)
            except FileNotFoundError:
                pass
            except OSError as error:
                errors.append(f"remove the directory {directory} this run made: {type(error).__name__}: {str(error)[:120]}")

    def step_leftovers(self):
        if self.tamper("leftover-worktree"):      # at the exact path the lane had
            self.git("worktree", "add", "-q", "--detach", self.lane_worktree, "HEAD")
        if self.tamper("leftover-branch"):        # the exact lane branch
            self.git("branch", self.lane_branch, "HEAD")
        if self.tamper("leftover-lane-record"):
            self.lane_path().parent.mkdir(parents=True)
        if self.tamper("leftover-pane"):
            self.tmux("new-window", "-d", "-t", f"={self.name}:", "-n", "sa:g3-left", "sleep 300")
        errors = []
        problems = self.leftovers(session_alive=True, errors=errors)
        self.check("the leftover probes ran to the end", not errors, errors)
        self.check("no worktree, branch, lane record or worker pane of this run is left", not problems, problems)
        if self.made["session"] and not self.tamper("session-kept"):
            self.tmux("kill-session", "-t", f"={self.name}", rc_ok=(0, 1))     # the run's own session, by its own name
        again = self.leftovers(session_alive=False, errors=errors)
        self.check("the run's tmux session is gone and no pane of it remains", not again and not errors, again or errors)

    def leftovers(self, session_alive, errors, final=False):
        """What of this run is still there (empty list: nothing): the exact resources the run's names compute to, never a name fragment.
        Read-only; also used after the cleanup. `final`: the cleanup's own pass, which also expects the scratch integration branch (if
        this run made it) to be gone. A probe that does not finish within --subprocess-timeout is added to `errors` and the next probe
        still runs. The broader scan for look-alike names is `scan`'s second list: reported by the cleanup, never a failure."""
        return self.scan(session_alive, errors, final)[0]

    def scan(self, session_alive, errors, final=False):
        """(exact leftovers, look-alikes). Exact: a worktree registered at, or anything on disk at, the path lanes.lane_worktree gave; the
        lane branch; the integration branch when `final` and this run made it; the directories this run made; the lane record's
        directory; a pane in the run's own session (exact session name; the window prefix `sa:` only tells the session's own anchor window
        from the workers' windows). Look-alikes: other worktrees and branches whose names merely contain the run's name."""
        found, similar = [], []

        def probe(label, argv):
            try:
                return self.quiet(argv).stdout
            except (subprocess.TimeoutExpired, OSError) as error:
                errors.append(f"leftover probe '{label}' failed: {type(error).__name__}: {str(error)[:120]}")
                return ""

        repo, path = self.repo, self.lane_worktree
        if repo and (repo / ".git").exists():
            listed = worktree_paths(probe("git worktree list", ["git", "-C", repo, "worktree", "list", "--porcelain"]))
            if path:
                registered = [p for p in listed if same_path(p, path)]
                found += [f"worktree {p}" for p in registered]
                if not registered and os.path.lexists(path):
                    found.append(f"path {path} exists on disk")
            similar += [f"worktree {p}" for p in listed if self.name in Path(p).name and not (path and same_path(p, path))]
            heads = [ref[len("refs/heads/"):] for ref in probe("git branch", ["git", "-C", repo, "for-each-ref", "--format=%(refname)", "refs/heads/"]).split()]
            exact = [b for b in (self.lane_branch, self.integration if final and self.made["integration"] else None) if b]
            found += [f"branch {b}" for b in heads if b in exact]
            similar += [f"branch {b}" for b in heads if self.name in b and b not in exact]
            lanes_dir = Path(self.facts.get("common") or "") / "tmux-pi" / "lanes" / self.name
            if self.facts.get("common") and lanes_dir.exists():
                found.append(f"lane record {lanes_dir}")
        found += [f"directory {d}" for d in self.made["dirs"] if os.path.lexists(d)]
        rows = probe("tmux list-panes", [*self.tmux_cmd, "list-panes", "-a", "-F", "#{session_name}\t#{window_name}\t#{pane_id}\t#{pane_dead}"])
        for row in rows.splitlines():
            session, window, pane, dead = (row.split("\t") + ["", "", "", ""])[:4]
            # while the session lives, a worker pane kept dead (remain-on-exit) with its scrollback is not a leftover: a running one is
            if session == self.name and ((window.startswith("sa:") and dead != "1") or not session_alive):
                found.append(f"pane {pane} ({window}) in session {session}")
        return found, similar

    # ---- fake backend --------------------------------------------------------------------------------------------------
    def fake_setup(self):
        """A copy of bin/ whose tp-route prints a fixed review plan; everything else is linked; a tmux wrapper that makes a
        sleeper pane for a worker and answers the session queries; a private tmux server."""
        fakebin, root = self.tmp / "fakebin", self.tmp / "root"
        fakebin.mkdir()
        shutil.copytree(ROOT / "bin", root / "bin")
        for entry in ROOT.iterdir():
            if entry.name not in ("bin", ".git") and not (root / entry.name).exists():
                (root / entry.name).symlink_to(entry)
        route = root / "bin" / "tp-route"
        route.write_text(f"#!/usr/bin/env bash\ncat <<'EOF'\n{json.dumps(PLAN)}\nEOF\n")
        route.chmod(0o755)
        self.root = root
        tmux = shutil.which("tmux")
        if not tmux:
            raise SmokeError("setup", "tmux is not installed; --fake still needs a (private) tmux server")
        assert self.tmux_cmd[1:] == ["-L", self.sock] and self.env()["TMUX_TMPDIR"] == str(self.tmux_tmp)   # set by the constructor
        wrapper = fakebin / "tmux"
        screen = self.tmp / "trust-screen.txt"
        # a worker pane is a sleeper; with --tamper trust-dialog it first waits for the fake trust screen (written by the prompt step) and prints it
        pane_script = f"until [ -f {screen} ]; do sleep 0.05; done; cat {screen}; sleep 300; :" if self.tamper("trust-dialog") else "sleep 300; :"
        # Belt and braces: whatever calls this wrapper, it reaches the private server only (its own TMUX_TMPDIR, -L socket, no TMUX).
        # The worker's UserPromptSubmit hook. Typed or pasted text is held per pane; the Enter after it submits it: the real
        # claude/plugins/tmux-pi/hooks/report.sh runs with a UserPromptSubmit event whose `prompt` is that text, so state.json gets
        # the promptLog record (n, at, tokens) exactly as the live hook writes it. The real script, not a copy: its fields, its bound
        # of 8 and its token pattern cannot drift from what lib/pane_send.py waits on. Cost: one python3, one bash and a few jq per
        # submit. --tamper unconfirmed-redirect drops the record: the text is typed and submitted, no hook runs.
        helper = fakebin / "fake_submit.py"
        helper.write_text(FAKE_SUBMIT)
        hook = ROOT / "claude" / "plugins" / "tmux-pi" / "hooks" / "report.sh"
        # A Claude task's text arrives as one paste (MS2): load-buffer -b NAME - keeps stdin per buffer name and paste-buffer -b NAME
        # -t PANE moves it to that pane's typed text, which the Enter then submits as above.
        paste_sh = f'''
   name=; pane=; prev=; for a; do [ "$prev" = -b ] && name=$a; [ "$prev" = -t ] && pane=$a; prev=$a; done
   buf="{self.tmp}/buf-$(printf %s "$name" | tr -c 'A-Za-z0-9' _)"
   case "$1" in
    load-buffer) cat > "$buf" ;;
    paste-buffer) [ -f "$buf" ] && mv "$buf" "{self.tmp}/typed-$(printf %s "$pane" | tr -c 'A-Za-z0-9' _)" ;;
   esac
   exit 0'''
        if self.tamper("unconfirmed-redirect"):
            submit_sh = f'exec {tmux} -L {self.sock} "$@"'
            paste_sh = 'case "$1" in load-buffer) cat >/dev/null ;; esac; exit 0'  # the paste goes nowhere and no hook runs
        else:
            submit_sh = f'''
   pane=; prev=; for a; do [ "$prev" = -t ] && pane=$a; prev=$a; done
   typed="{self.tmp}/typed-$(printf %s "$pane" | tr -c 'A-Za-z0-9' _)"
   case " $* " in *" -l "*) printf %s "$prev" > "$typed" ;; esac
   {tmux} -L {self.sock} "$@"; rc=$?
   if [ "$prev" = Enter ] && [ -f "$typed" ]; then {sys.executable} {helper} {hook} "$TP_STATE_DIR" "$pane" "$typed"; rm -f "$typed"; fi
   exit $rc'''
        wrapper.write_text(f'''#!/bin/sh
TMUX_TMPDIR='{self.tmux_tmp}'; export TMUX_TMPDIR; unset TMUX TMUX_PANE
case "$1" in
 set-option) case "$*" in *remain-on-exit*) exec {tmux} -L {self.sock} "$@" ;; esac; exit 0 ;;
 new-session|list-panes|kill-pane|kill-session|has-session|list-sessions|capture-pane) exec {tmux} -L {self.sock} "$@" ;;
 send-keys) {submit_sh} ;;
 load-buffer|paste-buffer) {paste_sh} ;;
 display-message) case "$*" in *pane_current_path*) printf '%s\\n' "$TP_MAIN_CWD"; exit 0 ;; esac; exec {tmux} -L {self.sock} "$@" ;;
 show-options) printf '%s\\n' "$TP_STATE_DIR" ;;
 new-window)
   name=; prev=; for last; do [ "$prev" = -n ] && name=$last; prev=$last; done
   pane=$({tmux} -L {self.sock} new-window -d -P -t "={self.name}:" -n "$name" -F '#{{pane_id}}' "sh -c '{pane_script}' sleeper $last") || exit 1
   printf '%s\\n' "$pane" ;;
 *) exit 0 ;;
esac
''')
        wrapper.chmod(0o755)
        if not self.repo:
            self.repo = self.tmp / "repo"
            self.repo.mkdir()
            for command in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "init"]):
                self.run(["git", "-C", self.repo, *command], env={**self.env(), **GIT_IDENTITY})
            (self.repo / "README.md").write_text("tiny\n")
            self.run(["git", "-C", self.repo, "add", "."])
            self.run(["git", "-C", self.repo, "commit", "-q", "-m", "readme"], env={**self.env(), **GIT_IDENTITY})

    def carried(self):
        """[(name, value)] of CARRIED_ENV that the caller's environment has (set, even empty), in tuple order."""
        return [(name, os.environ[name]) for name in CARRIED_ENV if name in os.environ]

    def require_env_flag(self):
        """`new-session -e` needs tmux 3.2: on an older (or unreadable) one the run refuses here, before the tmux session is created."""
        said = self.tmux("-V").stdout.strip()
        found = re.search(r"(\d+)\.(\d+)", said)
        if not found and re.search(r"\b(master|next)\b", said):
            return                        # a development build is newer than any release
        if not found or (int(found.group(1)), int(found.group(2))) < MIN_TMUX_FOR_ENV:
            names = ", ".join(name for name, _ in self.carried())
            raise SmokeError(self.step_name, f"tmux says {said!r}; carrying {names} into the run's session needs tmux {MIN_TMUX_FOR_ENV[0]}.{MIN_TMUX_FOR_ENV[1]} or newer "
                                             f"(new-session -e). No tmux session was created; upgrade tmux, or unset {names} in the environment that runs the smoke")

    def check_carried(self):
        """The session holds the caller's value of each carried variable (what a new pane of it gets)."""
        for name, value in self.carried():
            said = self.tmux("show-environment", "-t", f"={self.name}", name).stdout
            if self.tamper("pinode-mismatch"):
                said = f"{name}=tampered\n"
            self.check(f"the run's tmux session carries the caller's {name}", said == f"{name}={value}\n", said.rstrip("\n"),
                       hint="a worker pane takes its environment from the session, so a pi worker would start differently from Main's")

    def start_session(self):
        """The run's own tmux session, with the two options tp-spawn reads. --fake: on a private server behind the wrapper."""
        cwd = str(self.repo)
        carried = self.carried()
        if carried:
            self.require_env_flag()
        env_args = [arg for name, value in carried for arg in ("-e", f"{name}={value}")]       # one argv word each: no shell sees the value
        if self.fake:
            self.tmux("new-session", "-d", "-s", "anchor", "sleep 600")
        # One tmux call makes the session and marks it as this run's (the `;` runs set-option only if new-session succeeded), so the marker
        # exists exactly when this run made the session. Ownership is recorded after the call succeeded, never before:
        #  - a definite refusal (rc != 0, e.g. "duplicate session") means the run never owned it: nothing is recorded, nothing is killed;
        #  - a timeout is indeterminate (the session may or may not exist): `session_unsure`, settled by the cleanup from the marker.
        try:
            self.tmux("new-session", "-d", "-s", self.name, *env_args, "-c", cwd, "sleep 7200", ";", "set-option", "-t", f"={self.name}:", MARKER, self.run_id)
        except SmokeError as error:
            self.session_unsure = error.timed_out or "duplicate session" not in error.message     # only a plain duplicate is a definite refusal
            raise
        self.made["session"] = True
        self.check_carried()
        if not self.fake:                 # the wrapper answers these two in --fake
            pane = self.tmux("list-panes", "-t", f"={self.name}:", "-F", "#{pane_id}").stdout.split()[0]
            self.tmux("set-option", "-t", f"={self.name}:", "@tp_state_dir", str(self.state))
            self.tmux("set-option", "-t", f"={self.name}:", "@tp_main_pane", pane)

    # ---- ownership -----------------------------------------------------------------------------------------------------
    def claim(self):
        """Record what the lane tool made. Setup proved the lane's names free (`clear`) before anything was made, and every name carries the
        run id, so a lane record, a lane branch or a worktree under this run's name that exists now was made by this run. Without that proof
        nothing is claimed: whatever is found stays, and the leftover report names it."""
        if not (self.clear and self.facts.get("common")):
            return
        try:
            if self.lane_path().exists():
                self.made["lane"] = True
            if self.git("rev-parse", "--verify", "-q", f"refs/heads/{self.lane_branch}", rc_ok=(0, 1)).returncode == 0:
                self.made["lane_branch"] = True
            worktree = (self.lane_json() or {}).get("worktree")
            if worktree and worktree == self.lane_worktree and Path(worktree).is_dir():       # the path the config gives this run's name
                self.made["worktree"] = str(worktree)
            # The parents setup found missing and that exist now were made by the worktree's creation (setup proved they were absent).
            self.made["dirs"] = [str(d) for d in self.new_dirs if d.is_dir()]
        except SmokeError:
            pass              # a probe that failed claims nothing: the resource stays, and the leftover check reports it

    # ---- cleanup -------------------------------------------------------------------------------------------------------
    def cleanup(self):
        """Always runs, and removes only what this run made (`self.made`). Each action is tried on its own and is bounded; what could not
        be undone (an error, a timeout) is reported, never raised, and the next action still runs. What the run found and did not make is
        reported (`kept`, and the leftover list) and left in place."""
        report = self.cleanup_report
        report["ran"] = True

        def attempt(what, function, ok=(0,)):
            try:
                run = function()
                if isinstance(run, subprocess.CompletedProcess) and run.returncode not in ok:
                    report["errors"].append(f"{what}: exit {run.returncode}: {(run.stderr or run.stdout).strip()[-300:]}")
            except Exception as error:  # noqa: BLE001 - a cleanup step must not stop the next one (a timeout included)
                report["errors"].append(f"{what}: {type(error).__name__}: {str(error)[:200]}")

        self.cleanup_tmux(attempt)
        self.cleanup_git(attempt)
        if self.repo:
            try:
                report["leftovers"], similar = self.scan(session_alive=False, errors=report["errors"], final=True)
                # look-alikes (a name that merely contains the run's name) are reported with what was kept, and fail nothing
                self.kept += [f"similar name, not this run's exact resource: {x}" for x in similar if f"similar name, not this run's exact resource: {x}" not in self.kept]
            except Exception as error:  # noqa: BLE001
                report["errors"].append(f"leftover check: {type(error).__name__}: {str(error)[:200]}")
        self.teardown_private(attempt)

    def settle_session(self):
        """A session creation that timed out is indeterminate. The session is this run's only if it carries this run's marker, read back
        now; a session without it (a same-named session someone else made, or one made but not yet marked) is kept and reported."""
        target = f"={self.name}"
        if self.quiet([*self.tmux_cmd, "has-session", "-t", target]).returncode != 0:
            return                      # it never appeared: nothing to kill
        seen = self.quiet([*self.tmux_cmd, "show-options", "-t", f"{target}:", "-qv", MARKER]).stdout.strip()
        if seen == self.run_id:
            self.made["session"] = True
        else:
            self.kept.append(f"tmux session {self.name} carries marker {seen or '(none)'}, not this run's {self.run_id}: not killed")

    def cleanup_tmux(self, attempt):
        if self.session_unsure and not self.made["session"]:
            attempt("settle the session whose creation did not finish", self.settle_session)
        tasks = self.state / "tasks"
        if self.made["session"] and tasks.is_dir():        # the panes this run's tasks recorded, by id, and only if they are in this run's session
            def panes_of_session():
                rows = self.quiet([*self.tmux_cmd, "list-panes", "-a", "-F", "#{pane_id}\t#{session_name}"]).stdout.splitlines()
                return {row.split("\t")[0] for row in rows if row.split("\t")[1:] == [self.name]}
            try:
                ours = panes_of_session()
            except Exception as error:  # noqa: BLE001
                self.cleanup_report["errors"].append(f"list the session's panes: {type(error).__name__}: {str(error)[:200]}")
                ours = set()
            for task in tasks.iterdir():
                pane = str((self.read(task / "meta.json", {}) or {}).get("pane") or "")
                if re.fullmatch(r"%\d+", pane) and pane in ours:
                    attempt(f"kill pane {pane}", lambda pane=pane: self.quiet([*self.tmux_cmd, "kill-pane", "-t", pane]), ok=(0, 1))
        if self.made["session"]:        # only a session this run made, by its own full name
            attempt("kill session", lambda: self.quiet([*self.tmux_cmd, "kill-session", "-t", f"={self.name}"]), ok=(0, 1))

    def cleanup_git(self, attempt):
        if not (self.repo and self.facts.get("common")):
            return
        long = self.sub_timeout * 2
        lane_dir = Path(self.facts["common"]) / "tmux-pi" / "lanes" / self.name
        wt = self.made["worktree"]
        if wt and Path(wt).is_dir():       # this run's own tree: drop what it left, so remove does not refuse
            attempt("clean the lane worktree (reset)", lambda: self.quiet(["git", "-C", wt, "reset", "--hard", "-q"], timeout=long))
            attempt("clean the lane worktree (clean)", lambda: self.quiet(["git", "-C", wt, "clean", "-fdxq"], timeout=long))
        if self.made["lane"] and lane_dir.exists():
            attempt("lane remove --abandon", lambda: self.quiet([self.root / "bin/tp-agent", "lane", "remove", self.name, "--abandon", "--repo", self.repo], timeout=long))
        # This run's worktree only, by its path. No `git worktree prune`: that would also drop other registrations of the repository.
        if wt:
            attempt("git worktree remove", lambda: self.quiet(["git", "-C", self.repo, "worktree", "remove", "--force", wt], timeout=long), ok=(0, 128))
        attempt("remove the directories this run made", lambda: self.drop_dirs(self.cleanup_report["errors"]))     # empty ones only
        if self.made["lane_branch"]:
            attempt("delete the lane branch", lambda: self.quiet(["git", "-C", self.repo, "branch", "-D", self.lane_branch], timeout=long), ok=(0, 1))
        if self.made["integration"]:
            attempt("delete the scratch integration branch", lambda: self.quiet(["git", "-C", self.repo, "branch", "-D", self.integration], timeout=long), ok=(0, 1))

    def teardown_private(self, attempt):
        if self.fake:
            attempt("stop the private tmux server", self.stop_private_server)
            attempt("remove the private tmux directory", lambda: shutil.rmtree(self.tmux_tmp, ignore_errors=True))
        if not self.args.keep:
            attempt("remove the temporary directory", lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def stop_private_server(self):
        """`kill-server`, only when it provably reaches this run's own server: (a) fake mode, (b) the command carries `-L` with
        this run's private socket name and nothing else selects a server, (c) TMUX_TMPDIR is the run's own fresh directory.
        Any other state is a bug in this script: AssertionError, no call. Live mode never gets here."""
        cmd, env = self.tmux_cmd, self.env()
        assert self.fake, "kill-server is only for --fake runs"
        assert cmd[1:] == ["-L", self.sock] and self.sock.startswith("g3-") and "-S" not in cmd, f"kill-server without the private -L socket: {cmd}"
        assert self.tmux_tmp is not None and self.tmux_tmp.name.startswith("g3t-") and self.tmux_tmp.is_dir(), f"no private TMUX_TMPDIR: {self.tmux_tmp}"
        assert env.get("TMUX_TMPDIR") == str(self.tmux_tmp), f"kill-server with TMUX_TMPDIR={env.get('TMUX_TMPDIR')!r}, not the run's {self.tmux_tmp}"
        assert not env.get("TMUX") and not env.get("TMUX_PANE"), "kill-server with a tmux session in the env"
        if not (self.tmux_tmp / f"tmux-{os.getuid()}" / self.sock).exists():
            return None              # the private server never started: nothing to stop
        return self.quiet([*cmd, "kill-server"], env=env)

    # ---- the whole run -------------------------------------------------------------------------------------------------
    def go(self):
        failure = None
        try:
            for name in STEPS:
                self.do_step(name, getattr(self, f"step_{name}"))
        except SmokeError as error:
            failure = error
        finally:
            try:
                self.cleanup()
            except Exception as error:  # noqa: BLE001 - the summary is written whatever the cleanup did
                self.cleanup_report["errors"].append(f"cleanup: {type(error).__name__}: {str(error)[:200]}")
        ok = failure is None and not self.cleanup_report["errors"] and not self.cleanup_report["leftovers"]
        summary = {"run": self.name, "run_id": self.run_id, "made": self.made, "names": {"integration_branch": self.integration, "lane_branch": self.lane_branch, "lane_worktree": self.lane_worktree}, "tmp": str(self.tmp), "mode": "fake" if self.fake else "live", "ok": ok, "repo": str(self.repo), "tmux_tmpdir": str(self.tmux_tmp) if self.tmux_tmp else None,
                   "socket": self.sock if self.fake else None, "started": self.stamp,
                   "failed_step": failure.step if failure else None, "error": failure.message if failure else None,
                   "seconds": round(time.monotonic() - self.started, 2), "steps": self.steps, "cleanup": self.cleanup_report,
                   "ids": self.ids, "decline": self.facts.get("decline"), "collect_panes": self.facts.get("collect_panes")}
        out = Path(self.args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, indent=2) + "\n")
        if failure:
            print(f"g3_e2e: FAILED at step '{failure.step}': {failure.message}", file=sys.stderr)
        for line in self.cleanup_report["errors"] + [f"left behind: {x}" for x in self.cleanup_report["leftovers"]]:
            print(f"g3_e2e: cleanup: {line}", file=sys.stderr)
        if ok:
            print(f"g3_e2e: ok ({summary['mode']}) {self.name}: {len(self.steps)} steps in {summary['seconds']} s; summary {out}")
        return 0 if ok else 1


def parse(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, help="where the JSON summary is written")
    parser.add_argument("--repo", help="the test repository (live: battle-soccer, required; --fake: a tiny repo made for the run when absent)")
    parser.add_argument("--fake", action="store_true", help="no model: stubbed worker, reviewer verdict and review plan")
    parser.add_argument("--coder-model", default="claude-haiku-5-5")
    parser.add_argument("--coder-thinking", default="low")
    parser.add_argument("--prompt-wait", type=float, default=240, help="seconds to wait for the permission prompt, and for it to leave pending")
    parser.add_argument("--settle-wait", type=float, default=600, help="seconds to wait for a coder attempt to settle")
    parser.add_argument("--review-wait", type=float, default=600, help="seconds to wait for the reviewer to settle")
    parser.add_argument("--subprocess-timeout", type=float, default=30, help="seconds any tmux call, leftover probe or cleanup command may take (git and lane commands in the cleanup: twice this)")
    parser.add_argument("--keep", action="store_true", help="keep the temporary directory (state, logs) after the run")
    parser.add_argument("--inject-failure", choices=STEPS, help="test hook (--fake only): fail this step on purpose, to show the cleanup and the error")
    parser.add_argument("--tamper", choices=sorted(TAMPERS), help="test hook (--fake only): break one record, to show its check fails by name")
    args = parser.parse_args(argv)
    if not args.fake and not args.repo:
        parser.error("a live run needs --repo (the battle-soccer checkout)")
    for flag, value in (("--tamper", args.tamper), ("--inject-failure", args.inject_failure)):
        if value and not args.fake:
            parser.error(f"{flag} is a --fake test hook: a live run would start real workers before it failed")
    if args.tamper and args.repo:
        parser.error("--tamper breaks records of the repository (it can delete .git): it runs only on the throwaway repository a --fake run makes, so not with --repo")
    return args


def main(argv=None):
    return Smoke(parse(sys.argv[1:] if argv is None else argv)).go()


if __name__ == "__main__":
    sys.exit(main())
