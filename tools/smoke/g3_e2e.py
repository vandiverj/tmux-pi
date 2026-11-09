#!/usr/bin/env python3
"""G3 end-to-end smoke (docs/plans/det-prep/release-gates.md): one scripted run through the real bin/ tools, as subprocesses.

    dispatch -> prompt -> decline -> retry -> review -> close -> merge -> collect -> remove -> leftovers

The task is one tiny deterministic coder task: add the file G3_SMOKE.md whose whole content is one given line. Each step asserts
the records it leaves (task state, lane record, review verdict file, the commit on the scratch integration branch). The run
ends with a check that no worktree, branch, lane record, tmux session or pane named for it is left, and a `finally` block
cleans up even after a failure. A JSON summary (step name, rc, seconds, what was asserted) goes to --out.

  live (the default): a real worker and a real reviewer. Costs one small coder task and one small review. Run by Main only.
  --fake            : no model, no real worker. The tools are the real ones; only the tmux calls that would start a worker, the
                      worker's replies, the reviewer's verdict and tp-route's review plan are stubbed (see README.md).

Python 3.9, standard library only. Exit 0: every step and the cleanup passed. Exit 1: a step or the cleanup failed (stderr names it).
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
TAMPERS = ("task-state", "lane-record", "review-verdict", "scratch-commit")
FILE = "G3_SMOKE.md"
PROMPT_FILE = ".env.local"          # a credential-shaped name: the sensitive-read hook asks before a Read of it (nothing is created)
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
        # Isolation by construction. --fake never talks to the owner's tmux server: every subprocess gets its own TMUX_TMPDIR (a
        # fresh, short directory made here, before any tmux call; a unix socket path is limited to ~100 characters, so it is not
        # under self.tmp) and `-L <private socket>` is part of tmux_cmd from this line on, never added later.
        self.sock = f"g3-{os.getpid()}-{self.stamp[-6:]}"
        self.tmux_bin = shutil.which("tmux") or "tmux"
        self.tmux_tmp = Path(tempfile.mkdtemp(prefix="g3t-", dir="/tmp")).resolve() if self.fake else None
        self.tmux_cmd = [self.tmux_bin, "-L", self.sock] if self.fake else [self.tmux_bin]
        self.session_made = False   # set when this run created its tmux session; only then is the session killed by name
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

    def quiet(self, argv, **kw):
        """subprocess.run that never raises on the exit code, always with this run's env (cleanup and read-only probes)."""
        return subprocess.run([str(a) for a in argv], text=True, capture_output=True, env=kw.pop("env", None) or self.env(), **kw)

    def run(self, argv, rc_ok=(0,), cwd=None, timeout=120, env=None, what=None, stdin=None):
        """Run argv; record it for the step summary; a return code outside rc_ok, or a timeout, fails the step."""
        t0 = time.monotonic()
        label = what or " ".join(Path(argv[0]).name if i == 0 else str(a) for i, a in enumerate(argv))[:160]
        try:
            run = subprocess.run([str(a) for a in argv], text=True, capture_output=True, cwd=str(cwd or self.repo or self.tmp),
                                 env=env or self.env(), timeout=timeout, input=stdin)
        except subprocess.TimeoutExpired:
            self.commands.append({"cmd": label, "rc": None, "seconds": round(time.monotonic() - t0, 2)})
            raise SmokeError(self.step_name, f"`{label}` did not finish within {timeout} s") from None
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
        return self.run([*self.tmux_cmd, *args], rc_ok=rc_ok, timeout=30)

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

    def lane_json(self):
        return self.read(Path(self.facts["common"]) / "tmux-pi" / "lanes" / self.name / "lane.json")

    def write(self, path, text):
        Path(path).write_text(text)

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

    def step_setup(self):
        if self.fake:
            self.fake_setup()
        repo = self.repo
        self.check("the repository is a git checkout", (repo / ".git").exists(), str(repo))
        self.check("the repository has no uncommitted tracked change", self.git("status", "--porcelain", "--untracked-files=no") == "")
        branch = self.git("rev-parse", "--abbrev-ref", "HEAD")
        self.facts.update(main_branch=branch, main_head=self.git("rev-parse", "HEAD"))
        self.check("the scratch integration branch does not exist yet",
                   self.git("rev-parse", "--verify", "-q", f"refs/heads/{INTEGRATION}", rc_ok=(0, 1)).returncode == 1, INTEGRATION)
        self.check("the repository's own branch is not the scratch branch", branch != INTEGRATION, branch)
        self.check("no lane branch of this name exists", self.git("rev-parse", "--verify", "-q", f"refs/heads/lane/{self.name}", rc_ok=(0, 1)).returncode == 1)
        common = self.git("rev-parse", "--git-common-dir")
        self.facts["common"] = str((repo / common).resolve())
        self.git("branch", INTEGRATION, "HEAD")
        self.made_integration = True
        self.facts["integration_base"] = self.facts["main_head"]
        self.asserted.append(f"created the scratch integration branch {INTEGRATION} at {self.facts['main_head'][:12]}")
        self.start_session()

    def step_dispatch(self):
        task = (f"In the repository root create the file {FILE}. Its whole content is exactly one line: `{self.line}` followed by a "
                f"newline. Change nothing else, do not commit, and finish with RESULT_STATUS: complete. "
                f"Before you start, read the file {PROMPT_FILE} in the repository root with your Read tool.")
        self.facts["task"] = task
        item = self.agent_json("dispatch", "--role", "coder", "--lane", self.name, "--step", STEP_ID, "--repo", self.repo,
                               "--domain", "g3 smoke: one added file", "--touches", FILE, "--worker", "claude",
                               "--model", self.args.coder_model, "--thinking", self.args.coder_thinking, "--tests", "none",
                               "--deliverable", "the added file", "--task", task)
        ident = item.get("id", "")
        self.ids["first"] = ident
        self.check("dispatch returned a task id", ident.startswith("coder-"), item)
        self.check("the task was started, not queued or refused", item.get("status") not in (None, "queued"), item.get("status"))
        meta = self.read(self.task_dir(ident) / "meta.json", {})
        self.check("task record: meta.json names the lane and step", (meta.get("lane"), meta.get("lane_step")) == (self.name, STEP_ID),
                   (meta.get("lane"), meta.get("lane_step")))
        self.check("task record: a pane is recorded", str(meta.get("pane", "")).startswith("%"), meta.get("pane"))
        lane = self.lane_json() or {}
        self.facts.update(worktree=lane.get("worktree"), branch=lane.get("branch"), base=(lane.get("base") or {}).get("commit"))
        self.check("lane record: open, on branch lane/<name>, based on the scratch integration branch",
                   lane.get("state") == "open" and lane.get("branch") == f"lane/{self.name}" and self.facts["base"] == self.facts["integration_base"],
                   {k: lane.get(k) for k in ("state", "branch")})
        self.check("the lane worktree exists", Path(self.facts["worktree"] or "/nonexistent").is_dir(), self.facts["worktree"])

    def step_prompt(self):
        first = self.ids["first"]
        if self.fake:
            self.write(self.task_dir(first) / "permission-request.json",
                       json.dumps({"status": "pending", "target": PROMPT_FILE, "reason": "a read of a credential-shaped file", "updated": now()}) + "\n")
            self.write(self.task_dir(first) / "state.json", json.dumps({"state": "permission", "tool": "Read", "updated": now()}) + "\n")

        def probe():
            record = self.inspect(first)
            return record.get("status") == "needs_input" and (record.get("permission") or {}).get("status") == "pending", record
        record = self.wait("permission prompt (status needs_input with a pending permission request)", probe, self.args.prompt_wait,
                           dead=DEAD + ("settled",))   # a worker that finished without asking will not ask
        self.check("task state: needs_input", record["status"] == "needs_input", record["status"])
        self.check("task record: permission-request.json is pending and names the credential file",
                   record["permission"]["status"] == "pending" and PROMPT_FILE in str(record["permission"].get("target", "")), record["permission"])

    def step_decline(self):
        first, pane = self.ids["first"], self.read(self.task_dir(self.ids["first"]) / "meta.json", {}).get("pane")
        self.tmux("send-keys", "-t", pane, "Escape")           # decline the dialog, the way Main does
        if self.fake:
            request = self.read(self.task_dir(first) / "permission-request.json", {})
            request.update(status="denied", updated=now())
            self.write(self.task_dir(first) / "permission-request.json", json.dumps(request) + "\n")
            self.write(self.task_dir(first) / "state.json", json.dumps({"state": "working", "updated": now()}) + "\n")

        def declined():
            status = (self.inspect(first).get("permission") or {}).get("status")
            return status != "pending", status
        status = self.wait("the permission request to leave pending after Escape", declined, self.args.prompt_wait)
        self.check("task record: the declined request is not authorized", status != "authorized", status)
        redirect = (f"Do not read {PROMPT_FILE}: that request was declined. Do nothing else in this attempt and make no change. "
                    "Reply only with the line RESULT_STATUS: incomplete.")
        self.tool("tp-agent", "message", first, "--text", redirect)
        self.check("the redirect message was sent to the worker's pane", True)
        if self.fake:
            self.write(self.task_dir(first) / "result.md", "Declined; no change made.\nRESULT_STATUS: incomplete\n")
            self.write(self.task_dir(first) / "state.json", json.dumps({"state": "waiting", "updated": now()}) + "\n")
        record = self.wait_settled(first, "the first attempt to settle", self.args.settle_wait)
        self.check("task state: settled after the redirect", record["status"] == "settled", record["status"])
        self.check("task record: result_status incomplete (it made no change)", record.get("result_status") == "incomplete", record.get("result_status"))
        self.check("the lane worktree is still unchanged", self.git("status", "--porcelain", "--untracked-files=all", cwd=self.facts["worktree"]) == "")

    def step_retry(self):
        first, wt = self.ids["first"], self.facts["worktree"]
        self.tool("tp-agent", "stop", first)                    # a settled worker's process is alive; a lane allows one writer
        self.check("task state: the first attempt is stopped", self.inspect(first).get("status") == "stopped", self.inspect(first).get("status"))
        item = self.agent_json("retry", first, "--reason", "the credential read was declined; no change was made", "--task", self.facts["task"].split(" Before you start")[0],
                               "--worker", "claude", "--model", self.args.coder_model, "--thinking", self.args.coder_thinking)
        second = item.get("id", "")
        self.ids["retry"] = second
        self.check("retry started a new task", bool(second) and second != first, item)
        meta = self.read(self.task_dir(second) / "meta.json", {})
        self.check("task record: the retry keeps the lane and step", (meta.get("lane"), meta.get("lane_step")) == (self.name, STEP_ID), (meta.get("lane"), meta.get("lane_step")))
        if self.fake:
            self.write(Path(wt) / FILE, self.expected)
            self.write(self.task_dir(second) / "result.md", f"Added {FILE}.\nRESULT_STATUS: complete\n")
            self.write(self.task_dir(second) / "state.json", json.dumps({"state": "waiting", "updated": now()}) + "\n")
            if self.args.tamper == "task-state":
                self.write(self.task_dir(second) / "exit", "1\n")      # the worker's process exited nonzero: the task is failed
        record = self.wait_settled(second, "the retry to settle", self.args.settle_wait)
        self.check("task state: the retry is settled", record["status"] == "settled", record["status"])
        self.check("task record: result_status complete", record.get("result_status") == "complete", record.get("result_status"))
        self.check("the worktree holds exactly the one new file", self.git("status", "--porcelain", "--untracked-files=all", cwd=wt) == f"?? {FILE}")
        self.check("the file holds exactly the requested line", (Path(wt) / FILE).read_text() == self.expected)

    def step_review(self):
        second = self.ids["retry"]
        out = self.tool("tp-review", second, timeout=180).stdout.strip().splitlines()
        reviewer = out[-1].strip() if out else ""
        self.check("tp-review printed a reviewer task id", bool(reviewer) and (self.task_dir(reviewer)).is_dir(), out)
        self.ids["reviewer"] = reviewer
        review_dir = self.task_dir(reviewer)
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
        if self.fake:
            text = FAKE_REVIEW.format(file=FILE)
            if self.args.tamper == "review-verdict":
                text = text.replace("APPROVED", "REQUEST_CHANGES: the file is wrong", 1)
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
        self.check("the reviewed file is committed on the lane branch and the tree is clean",
                   self.git("status", "--porcelain", "--untracked-files=all", cwd=wt) == "" and tip != self.facts["base"])
        self.tool("tp-agent", "lane", "close", self.name, STEP_ID, "--repo", self.repo)
        if self.fake and self.args.tamper == "lane-record":
            lane = self.lane_json()
            lane["closed"] = []
            self.write(Path(self.facts["common"]) / "tmux-pi" / "lanes" / self.name / "lane.json", json.dumps(lane))
        lane = self.lane_json() or {}
        self.check("lane record: the step is closed", [c.get("step") for c in lane.get("closed", [])] == [STEP_ID], lane.get("closed"))

    def step_merge(self):
        """A merge commit on the scratch integration branch, made without a checkout: the lane's tree, parents (branch, lane tip)."""
        tip, base = self.facts["tip"], self.git("rev-parse", f"refs/heads/{INTEGRATION}")
        tree = self.git("rev-parse", f"{tip}^{{tree}}")
        merge = self.git("commit-tree", tree, "-p", base, "-p", tip, "-m", f"g3 smoke: merge lane/{self.name}", identity=True)
        if self.fake and self.args.tamper == "scratch-commit":
            blob = self.git("hash-object", "-w", "--stdin", stdin="wrong\n")      # a merge whose tree holds a different file content
            rows = [row for row in self.git("ls-tree", tip).splitlines() if not row.endswith(f"\t{FILE}")]
            tree = self.git("mktree", stdin="\n".join(rows + [f"100644 blob {blob}\t{FILE}"]) + "\n")
            merge = self.git("commit-tree", tree, "-p", base, "-p", tip, "-m", "tampered merge", identity=True)
        self.git("update-ref", f"refs/heads/{INTEGRATION}", merge, base)
        self.facts["merge"] = merge
        parents = self.git("rev-list", "--parents", "-n", "1", merge).split()[1:]
        self.check("scratch commit: a merge of the previous branch tip and the lane tip", parents == [base, tip], parents)
        self.check("scratch commit: the branch changed exactly one file, added",
                   self.git("diff", "--name-status", f"{base}..{INTEGRATION}") == f"A\t{FILE}", self.git("diff", "--name-status", f"{base}..{INTEGRATION}"))
        self.check("scratch commit: the added file holds exactly the requested line", self.git("show", f"{INTEGRATION}:{FILE}") == self.line)
        self.check("the repository's own branch did not move", self.git("rev-parse", self.facts["main_branch"]) == self.facts["main_head"])

    def step_collect(self):
        for key in ("retry", "reviewer"):
            ident = self.ids[key]
            record = self.agent_json("collect", ident)
            self.check(f"collect {key}: returned the task's record", record.get("id") == ident, record.get("id"))
            self.check(f"task state: {key} is completed", self.inspect(ident).get("status") == "completed", self.inspect(ident).get("status"))
            self.check(f"task record: {key} has its completed marker", any((self.task_dir(ident) / name).exists() for name in ("completed.json", "collected.json", "completed")),
                       sorted(p.name for p in self.task_dir(ident).iterdir()))
        panes = self.tmux("list-panes", "-a", "-F", "#{pane_id}").stdout.split()
        for key in ("first", "retry", "reviewer"):
            pane = self.read(self.task_dir(self.ids[key]) / "meta.json", {}).get("pane")
            self.check(f"{key}'s pane is gone from tmux", pane not in panes, pane)

    def step_remove(self):
        result = self.agent_json("lane", "remove", self.name, "--repo", self.repo)
        self.check("lane remove: removed, not abandoned, integrated in the scratch branch",
                   result.get("removed") is True and not result.get("abandoned") and INTEGRATION in str(result.get("integrated_in")), result)
        self.check("the lane's worktree directory is gone", not Path(self.facts["worktree"]).exists(), self.facts["worktree"])
        self.check("lane record: archived, no longer listed", self.lane_json() is None)

    def step_leftovers(self):
        problems = self.leftovers(session_alive=True)
        self.check("no worktree, branch, lane record or worker pane of this run is left", not problems, problems)
        if self.session_made:
            self.tmux("kill-session", "-t", f"={self.name}", rc_ok=(0, 1))     # the run's own session, by its own name
        again = self.leftovers(session_alive=False)
        self.check("the run's tmux session is gone and no pane of it remains", not again, again)

    def leftovers(self, session_alive):
        """What of this run is still there (empty list: nothing). Read-only; also used after the cleanup."""
        found = []
        repo = self.repo
        if repo and (repo / ".git").exists():
            listing = self.quiet(["git", "-C", repo, "worktree", "list", "--porcelain"]).stdout
            found += [f"worktree {line[9:]}" for line in listing.splitlines() if line.startswith("worktree ") and self.name in Path(line[9:]).name]
            branches = self.quiet(["git", "-C", repo, "branch", "--list", f"*{self.name}*"]).stdout.split()
            found += [f"branch {b}" for b in branches if b != "*"]
            lanes_dir = Path(self.facts.get("common") or "") / "tmux-pi" / "lanes" / self.name
            if self.facts.get("common") and lanes_dir.exists():
                found.append(f"lane record {lanes_dir}")
        rows = self.quiet([*self.tmux_cmd, "list-panes", "-a", "-F", "#{session_name}\t#{window_name}\t#{pane_id}"]).stdout
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
        if self.fake:
            self.tmux("new-session", "-d", "-s", "anchor", "sleep 600")
        self.tmux("new-session", "-d", "-s", self.name, "-c", cwd, "sleep 7200")
        self.session_made = True
        if not self.fake:                   # the wrapper answers these two in --fake
            pane = self.tmux("list-panes", "-t", f"={self.name}:", "-F", "#{pane_id}").stdout.split()[0]
            self.tmux("set-option", "-t", f"={self.name}:", "@tp_state_dir", str(self.state))
            self.tmux("set-option", "-t", f"={self.name}:", "@tp_main_pane", pane)
        self.asserted.append(f"started the tmux session {self.name} (state {self.state})")

    # ---- cleanup -------------------------------------------------------------------------------------------------------
    def cleanup(self):
        """Always runs. Each action is tried on its own; what could not be undone is reported, never raised."""
        report = self.cleanup_report
        report["ran"] = True

        def attempt(what, function, ok=(0,)):
            try:
                run = function()
                if isinstance(run, subprocess.CompletedProcess) and run.returncode not in ok:
                    report["errors"].append(f"{what}: exit {run.returncode}: {(run.stderr or run.stdout).strip()[-300:]}")
            except Exception as error:  # noqa: BLE001 - a cleanup step must not stop the next one
                report["errors"].append(f"{what}: {error}")

        tasks = self.state / "tasks"
        if tasks.is_dir():           # live and fake alike: only the panes this run's tasks recorded, by id
            for task in tasks.iterdir():
                pane = str((self.read(task / "meta.json", {}) or {}).get("pane") or "")
                if re.fullmatch(r"%\d+", pane):
                    attempt(f"kill pane {pane}", lambda pane=pane: self.quiet([*self.tmux_cmd, "kill-pane", "-t", pane], timeout=30), ok=(0, 1))
        if self.session_made:        # only the session this run created, by its own full name
            attempt("kill session", lambda: self.quiet([*self.tmux_cmd, "kill-session", "-t", f"={self.name}"], timeout=30), ok=(0, 1))
        if self.repo and self.facts.get("common"):
            lane_dir = Path(self.facts["common"]) / "tmux-pi" / "lanes" / self.name
            wt = self.facts.get("worktree")
            if lane_dir.exists() and wt and self.name in Path(wt).name and Path(wt).is_dir():   # this run's own tree: drop what it left, so remove does not refuse
                attempt("clean the lane worktree", lambda: self.quiet(["git", "-C", wt, "reset", "--hard", "-q"], timeout=60))
                attempt("clean the lane worktree", lambda: self.quiet(["git", "-C", wt, "clean", "-fdxq"], timeout=60))
            if lane_dir.exists():
                attempt("lane remove --abandon", lambda: self.quiet([self.root / "bin/tp-agent", "lane", "remove", self.name, "--abandon", "--repo", self.repo], timeout=60))
            wt = self.facts.get("worktree")
            if wt and Path(wt).exists() and self.name in Path(wt).name:
                attempt("git worktree remove", lambda: self.quiet(["git", "-C", self.repo, "worktree", "remove", "--force", wt], timeout=60))
            attempt("git worktree prune", lambda: self.quiet(["git", "-C", self.repo, "worktree", "prune"], timeout=60))
            attempt("delete the lane branch", lambda: self.quiet(["git", "-C", self.repo, "branch", "-D", f"lane/{self.name}"], timeout=60), ok=(0, 1))
            if self.made_integration:
                attempt("delete the scratch integration branch", lambda: self.quiet(["git", "-C", self.repo, "branch", "-D", INTEGRATION], timeout=60))
        report["leftovers"] = self.leftovers(session_alive=False) if self.repo else []
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
        return self.quiet([*cmd, "kill-server"], env=env, timeout=30)

    # ---- the whole run -------------------------------------------------------------------------------------------------
    def go(self):
        failure = None
        try:
            for name in STEPS:
                self.do_step(name, getattr(self, f"step_{name}"))
        except SmokeError as error:
            failure = error
        finally:
            self.cleanup()
        ok = failure is None and not self.cleanup_report["errors"] and not self.cleanup_report["leftovers"]
        summary = {"run": self.name, "mode": "fake" if self.fake else "live", "ok": ok, "repo": str(self.repo), "tmux_tmpdir": str(self.tmux_tmp) if self.tmux_tmp else None, "started": self.stamp,
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
    parser.add_argument("--keep", action="store_true", help="keep the temporary directory (state, logs) after the run")
    parser.add_argument("--inject-failure", choices=STEPS, help="test hook: fail this step on purpose, to show the cleanup and the error")
    parser.add_argument("--tamper", choices=TAMPERS, help="test hook (--fake only): break one record, to show its check fails by name")
    args = parser.parse_args(argv)
    if not args.fake and not args.repo:
        parser.error("a live run needs --repo (the battle-soccer checkout)")
    if args.tamper and not args.fake:
        parser.error("--tamper is a --fake test hook")
    return args


def main(argv=None):
    return Smoke(parse(sys.argv[1:] if argv is None else argv)).go()


if __name__ == "__main__":
    sys.exit(main())
