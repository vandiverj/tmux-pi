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

Every subprocess has a timeout. Python 3.9, standard library only. Exit 0: every step and the cleanup passed. Exit 1: a step or the
cleanup failed (stderr names it).
"""
import argparse
import json
import os
import re
import shutil
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
# "shadow" and "off"), not by the model. It names a dotenv-shaped file that does not exist and an unset variable with a secret-shaped name,
# and has an `exit` so the redaction hook cannot wrap it. Nothing can be read. Built from pieces so this file names no credential.
ABSENT = "." + "env." + "g3-smoke-absent"
SECRET_VAR = "G3_SMOKE_" + "API_" + "TOKEN"
PROMPT_COMMAND = f'cat ./{ABSENT} "${SECRET_VAR}"; exit 3'
STEP_ID = "s1"
INTEGRATION = "lane/integrate"      # the tool's own default integration branch; the scratch branch of the run
GIT_IDENTITY = {"GIT_AUTHOR_NAME": "g3-smoke", "GIT_AUTHOR_EMAIL": "g3-smoke@invalid", "GIT_COMMITTER_NAME": "g3-smoke", "GIT_COMMITTER_EMAIL": "g3-smoke@invalid"}
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
    "not-git": "setup", "dirty-repo": "setup", "on-scratch-branch": "setup", "integration-exists": "setup", "lane-branch-exists": "setup",
    "dispatch-id": "dispatch", "dispatch-queued": "dispatch", "dispatch-meta": "dispatch", "dispatch-pane": "dispatch",
    "dispatch-lane": "dispatch", "dispatch-worktree": "dispatch",
    "no-prompt": "prompt", "prompt-no-reason": "prompt",
    "still-pending": "decline", "authorized": "decline", "no-redirect-record": "decline", "first-never-settles": "decline",
    "first-complete": "decline", "first-changed": "decline",
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


class SmokeError(Exception):
    """A failed step. `step` is the step's name; `rc` the return code of the command that failed, when one did."""

    def __init__(self, step, message, rc=1):
        super().__init__(f"step '{step}': {message}")
        self.step, self.message, self.rc = step, message, rc


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def provider_of(worker, model):
    """The model's provider: a claude worker is Anthropic, 'provider/model' names its own, anything else is its worker."""
    if worker == "claude" or str(model).startswith(("claude", "anthropic")):
        return "anthropic"
    return str(model).split("/")[0] if "/" in str(model) else str(worker)


def bounded(argv, timeout, cwd=None, env=None, stdin=None):
    """subprocess.run with a hard bound. On timeout the child is killed and TimeoutExpired is raised; output a grandchild still holds
    open is given up after 2 s (subprocess.run would wait for it without end)."""
    proc = subprocess.Popen(argv, text=True, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, cwd=cwd, env=env)
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
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
        self.name = f"g3-smoke-{self.stamp}"
        self.line = f"g3 smoke {self.stamp}"
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
        self.sock = f"g3-{os.getpid()}-{self.stamp[-6:]}"
        self.tmux_bin = shutil.which("tmux") or "tmux"
        self.tmux_tmp = Path(tempfile.mkdtemp(prefix="g3t-", dir="/tmp")).resolve() if self.fake else None
        self.tmux_cmd = [self.tmux_bin, "-L", self.sock] if self.fake else [self.tmux_bin]
        self.session_made = False   # set when this run is about to create its tmux session; only then is the session killed by name
        self.steps, self.cleanup_report = [], {"ran": False, "errors": [], "leftovers": []}
        self.step_name, self.asserted, self.commands = None, [], []
        self.made_integration = False
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
            raise SmokeError(self.step_name, f"`{label}` did not finish within {timeout:g} s") from None
        self.commands.append({"cmd": label, "rc": run.returncode, "seconds": round(time.monotonic() - t0, 2)})
        if run.returncode not in rc_ok:
            tail = (run.stderr.strip() or run.stdout.strip())[-600:]
            raise SmokeError(self.step_name, f"`{label}` exited {run.returncode}: {tail}", run.returncode)
        return run

    def tool(self, name, *args, **kw):
        return self.run([self.root / "bin" / name, *args], **kw)

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

    def check(self, description, ok, detail=""):
        """Record an assertion; a false one fails the step with the description and what was seen."""
        if not ok:
            raise SmokeError(self.step_name, f"{description}" + (f" (saw: {detail})" if detail != "" else ""))
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

    def read(self, path, default=None):
        try:
            return json.loads(Path(path).read_text())
        except (OSError, ValueError):
            return default

    def inspect(self, ident):
        return self.agent_json("inspect", ident)

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

    def break_repo(self):
        repo = self.repo
        if self.tamper("not-git"):
            shutil.rmtree(repo / ".git")
        elif self.tamper("dirty-repo"):
            (repo / "README.md").write_text("changed\n")
        elif self.tamper("integration-exists"):
            self.git("branch", INTEGRATION, "HEAD")
        elif self.tamper("on-scratch-branch"):
            self.git("checkout", "-q", "-b", INTEGRATION)
        elif self.tamper("lane-branch-exists"):
            self.git("branch", f"lane/{self.name}", "HEAD")

    def step_setup(self):
        if self.fake:
            self.fake_setup()
            self.break_repo()
        repo = self.repo
        self.check("the repository is a git checkout", (repo / ".git").exists(), str(repo))
        self.facts["common"] = str((repo / self.git("rev-parse", "--git-common-dir")).resolve())     # from here the cleanup knows the repository
        self.check("the repository has no uncommitted tracked change", self.git("status", "--porcelain", "--untracked-files=no") == "")
        branch = self.git("rev-parse", "--abbrev-ref", "HEAD")
        self.facts.update(main_branch=branch, main_head=self.git("rev-parse", "HEAD"))
        self.check("the repository's own branch is not the scratch branch", branch != INTEGRATION, branch)
        self.check("the scratch integration branch does not exist yet",
                   self.git("rev-parse", "--verify", "-q", f"refs/heads/{INTEGRATION}", rc_ok=(0, 1)).returncode == 1, INTEGRATION)
        self.check("no lane branch of this name exists", self.git("rev-parse", "--verify", "-q", f"refs/heads/lane/{self.name}", rc_ok=(0, 1)).returncode == 1)
        self.git("branch", INTEGRATION, "HEAD")
        self.made_integration = True
        self.facts["integration_base"] = self.facts["main_head"]
        self.start_session()

    def step_dispatch(self):
        task = (f"In the repository root create the file {FILE}. Its whole content is exactly one line: `{self.line}` followed by a "
                f"newline. Change nothing else, do not commit, and finish with RESULT_STATUS: complete.")
        self.facts["task"] = task
        first_task = (f"Before anything else, run exactly this one Bash command, unchanged, once: {PROMPT_COMMAND}\n"
                      f"(it names a file that does not exist; if the owner declines it, stop and wait for instructions). Then: {task}")
        item = self.agent_json("dispatch", "--role", "coder", "--lane", self.name, "--step", STEP_ID, "--repo", self.repo,
                               "--domain", "g3 smoke: one added file", "--touches", FILE, "--worker", "claude",
                               "--model", self.args.coder_model, "--thinking", self.args.coder_thinking, "--tests", "none",
                               "--deliverable", "the added file", "--task", first_task)
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
        self.check("lane record: open, on branch lane/<name>, based on the scratch integration branch",
                   lane.get("state") == "open" and lane.get("branch") == f"lane/{self.name}" and self.facts["base"] == self.facts["integration_base"],
                   {k: lane.get(k) for k in ("state", "branch")})
        self.check("the lane worktree exists", Path(self.facts["worktree"] or "/nonexistent").is_dir(), self.facts["worktree"])

    def step_prompt(self):
        first = self.ids["first"]
        if self.fake and not self.tamper("no-prompt"):
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
        self.check("task record: permission-request.json is pending and carries a reason", permission.get("status") == "pending" and bool(permission.get("reason")), permission)

    def step_decline(self):
        first, pane = self.ids["first"], self.read(self.task_dir(self.ids["first"]) / "meta.json", {}).get("pane")
        self.tmux("send-keys", "-t", pane, "Escape")           # decline the dialog, the way Main does
        if self.fake and not self.tamper("still-pending"):
            request = self.read(self.task_dir(first) / "permission-request.json", {})
            request.update(status="authorized" if self.tamper("authorized") else "denied", updated=now())
            self.write(self.task_dir(first) / "permission-request.json", json.dumps(request) + "\n")
            self.write(self.task_dir(first) / "state.json", json.dumps({"state": "working", "updated": now()}) + "\n")

        def declined():
            status = (self.inspect(first).get("permission") or {}).get("status")
            return status != "pending", status
        status = self.wait("the permission request to leave pending after Escape", declined, self.args.prompt_wait)
        self.check("task record: the declined request is not authorized", status != "authorized", status)
        redirect = ("Do not run that command: the request was declined. Do nothing else in this attempt and make no change. "
                    "Reply only with the line RESULT_STATUS: incomplete.")
        self.tool("tp-agent", "message", first, "--text", redirect)
        if self.tamper("no-redirect-record"):
            (self.task_dir(first) / "messages.jsonl").unlink()
        sent = [json.loads(line).get("text") for line in (self.task_dir(first) / "messages.jsonl").read_text().splitlines()] \
            if (self.task_dir(first) / "messages.jsonl").exists() else []
        self.check("task record: messages.jsonl holds the redirect message sent to the worker", redirect in sent, sent)
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
        self.check("the file holds exactly the requested line", (Path(wt) / FILE).read_text() == self.expected)

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
        self.check("review record: review-of names the reviewed task", (review_dir / "review-of").read_text().strip() == second)
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
        tip, base = self.facts["tip"], self.git("rev-parse", f"refs/heads/{INTEGRATION}")
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
        merge = self.git("commit-tree", tree, *parents, "-m", f"g3 smoke: merge lane/{self.name}", identity=True)
        self.git("update-ref", f"refs/heads/{INTEGRATION}", merge, base)
        if self.tamper("main-moved"):
            self.git("update-ref", f"refs/heads/{self.facts['main_branch']}", tip)
        self.facts["merge"] = merge
        found = self.git("rev-list", "--parents", "-n", "1", merge).split()[1:]
        self.check("scratch commit: a merge of the previous branch tip and the lane tip", found == [base, tip], found)
        self.check("scratch commit: the branch changed exactly one file, added",
                   self.git("diff", "--name-status", f"{base}..{INTEGRATION}") == f"A\t{FILE}", self.git("diff", "--name-status", f"{base}..{INTEGRATION}"))
        self.check("scratch commit: the added file holds exactly the requested line", self.git("show", f"{INTEGRATION}:{FILE}") == self.line)
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
        panes = self.tmux("list-panes", "-a", "-F", "#{pane_id}").stdout.split()
        for key in ("first", "retry", "reviewer"):
            pane = self.read(self.task_dir(self.ids[key]) / "meta.json", {}).get("pane")
            self.check("the panes of the three tasks are gone from tmux", pane not in panes, f"{key}: {pane}")

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
        self.check("lane remove: removed, not abandoned, integrated in the scratch branch",
                   result.get("removed") is True and not result.get("abandoned") and INTEGRATION in str(result.get("integrated_in")), result)
        self.check("the lane's worktree directory is gone", not Path(self.facts["worktree"]).exists(), self.facts["worktree"])
        self.check("lane record: archived, no longer listed", archived is None, archived)

    def step_leftovers(self):
        if self.tamper("leftover-worktree"):
            self.git("worktree", "add", "-q", "--detach", str(self.tmp / f"left-{self.name}"), "HEAD")
        if self.tamper("leftover-branch"):
            self.git("branch", f"left-{self.name}", "HEAD")
        if self.tamper("leftover-lane-record"):
            self.lane_path().parent.mkdir(parents=True)
        if self.tamper("leftover-pane"):
            self.tmux("new-window", "-d", "-t", f"={self.name}:", "-n", "sa:g3-left", "sleep 300")
        errors = []
        problems = self.leftovers(session_alive=True, errors=errors)
        self.check("the leftover probes ran to the end", not errors, errors)
        self.check("no worktree, branch, lane record or worker pane of this run is left", not problems, problems)
        if self.session_made and not self.tamper("session-kept"):
            self.tmux("kill-session", "-t", f"={self.name}", rc_ok=(0, 1))     # the run's own session, by its own name
        again = self.leftovers(session_alive=False, errors=errors)
        self.check("the run's tmux session is gone and no pane of it remains", not again and not errors, again or errors)

    def leftovers(self, session_alive, errors):
        """What of this run is still there (empty list: nothing). Read-only; also used after the cleanup. A probe that does not
        finish within --subprocess-timeout is added to `errors` and the next probe still runs."""
        found = []

        def probe(label, argv):
            try:
                return self.quiet(argv).stdout
            except (subprocess.TimeoutExpired, OSError) as error:
                errors.append(f"leftover probe '{label}' failed: {type(error).__name__}: {str(error)[:120]}")
                return ""

        repo = self.repo
        if repo and (repo / ".git").exists():
            listing = probe("git worktree list", ["git", "-C", repo, "worktree", "list", "--porcelain"])
            found += [f"worktree {line[9:]}" for line in listing.splitlines() if line.startswith("worktree ") and self.name in Path(line[9:]).name]
            branches = probe("git branch", ["git", "-C", repo, "branch", "--list", f"*{self.name}*"]).split()
            found += [f"branch {b}" for b in branches if b != "*"]
            lanes_dir = Path(self.facts.get("common") or "") / "tmux-pi" / "lanes" / self.name
            if self.facts.get("common") and lanes_dir.exists():
                found.append(f"lane record {lanes_dir}")
        rows = probe("tmux list-panes", [*self.tmux_cmd, "list-panes", "-a", "-F", "#{session_name}\t#{window_name}\t#{pane_id}"])
        for row in rows.splitlines():
            session, window, pane = (row.split("\t") + ["", "", ""])[:3]
            if session == self.name and (window.startswith("sa:") or not session_alive):
                found.append(f"pane {pane} ({window}) in session {session}")
        return found

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
        # Belt and braces: whatever calls this wrapper, it reaches the private server only (its own TMUX_TMPDIR, -L socket, no TMUX).
        wrapper.write_text(f'''#!/bin/sh
TMUX_TMPDIR='{self.tmux_tmp}'; export TMUX_TMPDIR; unset TMUX TMUX_PANE
case "$1" in
 new-session|list-panes|kill-pane|kill-session|has-session|send-keys|list-sessions) exec {tmux} -L {self.sock} "$@" ;;
 display-message) case "$*" in *pane_current_path*) printf '%s\\n' "$TP_MAIN_CWD"; exit 0 ;; esac; exec {tmux} -L {self.sock} "$@" ;;
 show-options) printf '%s\\n' "$TP_STATE_DIR" ;;
 new-window)
   name=; prev=; for last; do [ "$prev" = -n ] && name=$last; prev=$last; done
   pane=$({tmux} -L {self.sock} new-window -d -P -t "={self.name}:" -n "$name" -F '#{{pane_id}}' "sh -c 'sleep 300; :' sleeper $last") || exit 1
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

    def start_session(self):
        """The run's own tmux session, with the two options tp-spawn reads. --fake: on a private server behind the wrapper."""
        cwd = str(self.repo)
        self.session_made = True    # set first: a new-session that timed out may still have made the session, and the cleanup then kills it by name
        if self.fake:
            self.tmux("new-session", "-d", "-s", "anchor", "sleep 600")
        self.tmux("new-session", "-d", "-s", self.name, "-c", cwd, "sleep 7200")
        if not self.fake:                   # the wrapper answers these two in --fake
            pane = self.tmux("list-panes", "-t", f"={self.name}:", "-F", "#{pane_id}").stdout.split()[0]
            self.tmux("set-option", "-t", f"={self.name}:", "@tp_state_dir", str(self.state))
            self.tmux("set-option", "-t", f"={self.name}:", "@tp_main_pane", pane)

    # ---- cleanup -------------------------------------------------------------------------------------------------------
    def cleanup(self):
        """Always runs. Each action is tried on its own and is bounded; what could not be undone (an error, a timeout) is reported,
        never raised, and the next action still runs."""
        report = self.cleanup_report
        report["ran"] = True

        def attempt(what, function, ok=(0,)):
            try:
                run = function()
                if isinstance(run, subprocess.CompletedProcess) and run.returncode not in ok:
                    report["errors"].append(f"{what}: exit {run.returncode}: {(run.stderr or run.stdout).strip()[-300:]}")
            except Exception as error:  # noqa: BLE001 - a cleanup step must not stop the next one (a timeout included)
                report["errors"].append(f"{what}: {type(error).__name__}: {str(error)[:200]}")

        long = self.sub_timeout * 2
        tasks = self.state / "tasks"
        if tasks.is_dir():           # live and fake alike: only the panes this run's tasks recorded, by id
            for task in tasks.iterdir():
                pane = str((self.read(task / "meta.json", {}) or {}).get("pane") or "")
                if re.fullmatch(r"%\d+", pane):
                    attempt(f"kill pane {pane}", lambda pane=pane: self.quiet([*self.tmux_cmd, "kill-pane", "-t", pane]), ok=(0, 1))
        if self.session_made:        # only the session this run created, by its own full name
            attempt("kill session", lambda: self.quiet([*self.tmux_cmd, "kill-session", "-t", f"={self.name}"]), ok=(0, 1))
        if self.repo and self.facts.get("common"):
            lane_dir = Path(self.facts["common"]) / "tmux-pi" / "lanes" / self.name
            wt = self.facts.get("worktree")
            if lane_dir.exists() and wt and self.name in Path(wt).name and Path(wt).is_dir():   # this run's own tree: drop what it left, so remove does not refuse
                attempt("clean the lane worktree (reset)", lambda: self.quiet(["git", "-C", wt, "reset", "--hard", "-q"], timeout=long))
                attempt("clean the lane worktree (clean)", lambda: self.quiet(["git", "-C", wt, "clean", "-fdxq"], timeout=long))
            if lane_dir.exists():
                attempt("lane remove --abandon", lambda: self.quiet([self.root / "bin/tp-agent", "lane", "remove", self.name, "--abandon", "--repo", self.repo], timeout=long))
            # This run's worktree only, by its path. No `git worktree prune`: that would also drop other registrations of the repository.
            if wt and self.name in Path(wt).name:
                attempt("git worktree remove", lambda: self.quiet(["git", "-C", self.repo, "worktree", "remove", "--force", wt], timeout=long), ok=(0, 128))
            attempt("delete the lane branch", lambda: self.quiet(["git", "-C", self.repo, "branch", "-D", f"lane/{self.name}"], timeout=long), ok=(0, 1))
            if self.made_integration:
                attempt("delete the scratch integration branch", lambda: self.quiet(["git", "-C", self.repo, "branch", "-D", INTEGRATION], timeout=long), ok=(0, 1))
        if self.repo:
            try:
                report["leftovers"] = self.leftovers(session_alive=False, errors=report["errors"])
            except Exception as error:  # noqa: BLE001
                report["errors"].append(f"leftover check: {type(error).__name__}: {str(error)[:200]}")
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
        summary = {"run": self.name, "mode": "fake" if self.fake else "live", "ok": ok, "repo": str(self.repo), "tmux_tmpdir": str(self.tmux_tmp) if self.tmux_tmp else None,
                   "socket": self.sock if self.fake else None, "started": self.stamp,
                   "failed_step": failure.step if failure else None, "error": failure.message if failure else None,
                   "seconds": round(time.monotonic() - self.started, 2), "steps": self.steps, "cleanup": self.cleanup_report,
                   "ids": self.ids}
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
    return args


def main(argv=None):
    return Smoke(parse(sys.argv[1:] if argv is None else argv)).go()


if __name__ == "__main__":
    sys.exit(main())
