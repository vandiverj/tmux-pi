"""tp-bench run: one fresh project copy and one detached workspace per repeat, every task through the arm's own tp-agent.

Everything that starts a process goes through `shell`; time goes through `clock` and `sleep`; so the tests drive the
whole run (including a task that times out) with fakes. Cleanup is in `finally`: tp-exit, then the copy."""
import json
import os
import random
import re
import secrets
import shutil
import time
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path

import trust_dialog  # lib/trust_dialog.py: the one detector of Claude Code's folder-trust prompt, shared with tp-agent and the status views
from . import answers, match, project, record, specs as specs_mod, trace
from .shell import SESSION_KEYS, ShellError

Probe = namedtuple("Probe", "state detail")  # state: present-ours | present-foreign | gone | unknown
Stop = namedtuple("Stop", "state reason")  # state: stopped | none | refused | unknown (only unknown leaves the copy and work dir in place)
ALLOWED_DIRTY = ("node_modules", ".tmux-pi")  # paths the workspace owns; `git status` may list them after a reset

TERMINAL = {"settled", "completed", "paused", "failed", "killed", "stopped", "escalated", "needs_input"}
FINISHED = {"settled", "completed", "paused"}  # the worker replied; anything else terminal is a failed task
DEFAULT_MAX_WORKER_RUNS = 40
LMSTUDIO = "http://localhost:1234/api/v0/models"
STUB_MODEL = "lmstudio/qwen/qwen3.6-35b-a3b"  # a local author, so the router picks a frontier reviewer
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SESSION_PREFIX = "bench-"
NEVER_COPY = (".env*", ".claude.json", ".credentials.json")  # secrets and the Claude profile's trust store stay where they are


class RunnerError(RuntimeError):
    pass


class ResetFailed(RunnerError):
    """The copy could not be put back on the base tag, so no further task may run in it."""


def parse_env(items):
    """['K=V', ...] -> {K: V}; refuses a malformed pair. An empty value is allowed (TP_PI_NODE=)."""
    out = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep or not ENV_KEY.fullmatch(key):
            raise RunnerError(f"--env wants KEY=VALUE, got {item!r}")
        if key in SESSION_KEYS:
            raise RunnerError(f"--env {key}: a session key would point the workspace at another session; the runner sets its own")
        out[key] = value
    return out


def check_guard(count, repeat, limit):
    if count * repeat > limit:
        raise RunnerError(f"{count} task(s) x {repeat} repeat(s) = {count * repeat} worker runs, over --max-worker-runs {limit}; "
                          "narrow --tasks, lower --repeat or raise the limit")


def order_tasks(selected, mode, seed, n):
    ordered = sorted(selected, key=lambda s: s["id"])
    if mode == "random":
        random.Random(f"{seed}:{n}").shuffle(ordered)
    return ordered


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Runner:
    def __init__(self, shell, bench_dir, project_dir, arm, results_dir, label, env=None, order="random", seed=None,
                 clock=time.monotonic, sleep=time.sleep, poll_s=5.0, stamp=now_iso, answers_directory=None, log=None, work_root=None, token=None):
        self.shell, self.bench_dir, self.project_dir = shell, Path(bench_dir), Path(project_dir)
        self.arm, self.results_dir, self.label = Path(arm), Path(results_dir), label
        self.env, self.order, self.clock, self.sleep, self.poll_s = dict(env or {}), order, clock, sleep, poll_s
        self.seed = seed if seed is not None else random.SystemRandom().randrange(1 << 31)
        self.stamp, self.answers_directory = stamp, answers_directory
        self.log = log or (lambda text: None)
        # Copies live under one directory the owner has made Claude trust once (bin/tp-claude-bootstrap <work_root>); see docs/bench.md.
        self.work_root = Path(work_root) if work_root else Path.home() / ".local" / "share" / "tp-bench" / "work"
        self.token = token or (lambda: secrets.token_hex(3))
        self.session = self.state_dir = self.copy = None
        self.agent_env, self.started = {}, False

    # ---- process helpers -------------------------------------------------
    # Every call here is checked: a nonzero exit raises ShellError. A call that reads the exit code itself passes tolerate="<why>".
    def agent(self, *args, timeout=120, tolerate=None):
        return self.shell.run([self.arm / "bin" / "tp-agent", *args], env=self.agent_env, cwd=self.copy, timeout=timeout, tolerate=tolerate)

    def agent_json(self, *args):
        result = self.agent(*args)
        try:
            return json.loads(result.stdout)
        except ValueError:
            raise RunnerError(f"tp-agent {args[0]} printed no JSON: {result.stdout.strip()[:200]}") from None

    # ---- workspace -------------------------------------------------------
    def probe_session(self):
        """THE one answer to "is this session there?", used at start and before every stop. Absence is proven, never inferred from a
        failed call: "gone" only when `tmux list-sessions` exits 0 and the exact name is not listed. A nonzero exit, a timeout, an
        exception, or a failed read of @tp_state_dir is "unknown". Otherwise "present-ours" (the @tp_state_dir this run started, or, when
        start failed half way, one inside its own copy) or "present-foreign"."""
        name = self.session
        try:
            listed = self.shell.run(["tmux", "list-sessions", "-F", "#{session_name}"], timeout=30,
                                    tolerate="a failed listing is the answer 'unknown', which is handled below; it never means 'gone'")
            if listed.returncode != 0:
                return Probe("unknown", f"tmux list-sessions exited {listed.returncode}: {(listed.stderr or listed.stdout).strip()[:200]}")
            if name not in listed.stdout.splitlines():
                return Probe("gone", "")
            shown = self.shell.run(["tmux", "show-options", "-qv", "-t", f"={name}:", "@tp_state_dir"], timeout=30,
                                   tolerate="a failed read is the answer 'unknown', which is handled below")
        except Exception as error:  # noqa: BLE001 - whatever went wrong, absence is not proven
            return Probe("unknown", f"the tmux probe raised {error!r}")
        if shown.returncode != 0:
            return Probe("unknown", f"tmux show-options exited {shown.returncode}: {(shown.stderr or shown.stdout).strip()[:200]}")
        found = shown.stdout.strip()
        mine = bool(found) and (found == self.state_dir if self.state_dir else Path(found).is_relative_to(self.copy))
        return Probe("present-ours" if mine else "present-foreign", found or "unset")

    def start_workspace(self):
        # The session is ours alone: a fresh name under the bench- prefix, and never one that already exists.
        if not self.session.startswith(SESSION_PREFIX):
            raise RunnerError(f"session name {self.session} does not start with {SESSION_PREFIX}")
        probe = self.probe_session()
        if probe.state == "unknown":
            raise RunnerError(f"cannot tell whether tmux session {self.session} exists ({probe.detail}); refusing to start")
        if probe.state != "gone":
            raise RunnerError(f"tmux session {self.session} already exists; refusing to start in it")
        env = {"TP_NO_ATTACH": "1", "TP_SESSION": self.session, **self.env}
        self.shell.run([self.arm / "bin" / "tp", self.copy], env=env, cwd=self.copy, timeout=180)
        self.started = True
        self.state_dir = self.shell.run(["tmux", "show-options", "-qv", "-t", f"={self.session}:", "@tp_state_dir"], timeout=30).stdout.strip()
        if not self.state_dir:
            raise RunnerError(f"workspace {self.session} started but has no @tp_state_dir")
        # worker panes read the tmux session environment, not the runner's: set the experiment env there too
        exported = dict(self.env)
        if "TP_PI_NODE" not in exported and "TP_PI_NODE" in os.environ:
            exported["TP_PI_NODE"] = os.environ["TP_PI_NODE"]  # the pinned Node path may not exist here; campaign runs pass TP_PI_NODE=
        for key, value in exported.items():
            self.shell.run(["tmux", "set-environment", "-t", f"={self.session}", key, value], timeout=30)
        self.agent_env = {"TP_STATE_DIR": self.state_dir, "TP_SESSION": self.session, **self.env}

    def stop_step(self, action):
        """THE one place a session-ending argv (tp-exit, kill-session) is built or run; tests/test_bench_shell.py checks that no
        other function names them. Every call first probes the session (`probe_session`) and runs the action only when it is
        present and ours. Returns ("gone", None) when absence is proven, ("refused", reason) when the name is somebody else's,
        ("unknown", reason) when the probe could not tell (nothing is stopped), else ("ran", None)."""
        probe = self.probe_session()
        if probe.state == "gone":
            return "gone", None
        if probe.state == "unknown":
            reason = f"did not stop {self.session}: could not prove its state ({probe.detail}); stopped nothing"
            self.log(reason)
            return "unknown", reason
        if probe.state == "present-foreign":
            reason = f"refused to stop {self.session}: its @tp_state_dir is {probe.detail}, not the one this run started ({self.state_dir or self.copy})"
            self.log(reason)
            return "refused", reason
        if action == "tp-exit":
            # scrubbed environment (Shell) plus an explicit session and state dir, which lib/tp.sh cross-checks
            self.shell.run([self.arm / "bin" / "tp-exit"], env={"TP_SESSION": self.session, "TP_STATE_DIR": probe.detail, "TP_NO_ATTACH": "1"},
                           cwd=self.copy, timeout=180,
                           tolerate="the next probe decides whether the session is gone; kill-session follows when it is not")
        elif action == "kill-session":
            self.shell.run(["tmux", "kill-session", "-t", f"={self.session}"], timeout=30,
                           tolerate="the final probe decides whether the session is gone; a session still there is recorded as not stopped")
        else:
            raise ValueError(action)
        return "ran", None

    def stop_workspace(self):
        """End the session this runner started, and only it: tp-exit, then kill-session if it is still there, each step through
        `stop_step` so ownership is proven again right before it. Returns a Stop: "stopped" (proven gone after our own action),
        "none" (never started and absent), "refused" (not ours, or already gone before we acted), "unknown" (not proven; the copy stays)."""
        ran = False
        for action in ("tp-exit", "kill-session"):
            outcome, reason = self.stop_step(action)
            if outcome == "gone":
                if ran:
                    return Stop("stopped", None)
                if not self.started:
                    return Stop("none", None)
                return Stop("refused", f"refused to stop {self.session}: it was already gone before this run stopped it")
            if outcome != "ran":
                return Stop(outcome, reason)
            ran = True
        last = self.probe_session()
        if last.state == "gone":
            return Stop("stopped", None)
        if last.state == "present-foreign":
            return Stop("refused", f"refused to stop {self.session}: the name now belongs to @tp_state_dir {last.detail}")
        return Stop("unknown", f"{self.session} not proven gone after tp-exit and kill-session ({last.state}: {last.detail})")

    def stop_safely(self):
        try:
            return self.stop_workspace()
        except Exception as error:  # noqa: BLE001 - a stop that blew up proved nothing
            return Stop("unknown", f"stopping {self.session} raised {error!r}")

    def trust_dialog_shown(self):
        """True when a pane of this runner's session shows Claude Code's folder-trust prompt. Read-only: the dialog is never answered."""
        listed = self.shell.run(["tmux", "list-panes", "-s", "-t", f"={self.session}", "-F", "#{pane_id}"], timeout=30,
                                tolerate="a failed listing shows no dialog; every poll looks again, and a stuck worker ends at its budget")
        for pane in listed.stdout.split() if listed.returncode == 0 else []:
            seen = self.shell.run(["tmux", "capture-pane", "-p", "-t", pane], timeout=30,
                                  tolerate="a pane that vanished shows no dialog; every poll looks again")
            if seen.returncode == 0 and trust_dialog.shown(seen.stdout):
                return True
        return False

    # ---- tasks -----------------------------------------------------------
    def reset_copy(self, spec):
        """Every task starts from the tag: tracked files restored, stray files gone (the workspace's own state and node_modules stay).
        The tree is then verified positively (HEAD is the tag's commit, nothing but the allowed paths is dirty); a reset that fails or
        a tree that is not clean raises ResetFailed and no further task is dispatched."""
        try:
            self.shell.run(["git", "-C", self.copy, "reset", "--hard", "-q", project.TAG], timeout=60)
            self.shell.run(["git", "-C", self.copy, "clean", "-fdq", "-e", "node_modules", "-e", ".tmux-pi"], timeout=60)
            head = self.shell.run(["git", "-C", self.copy, "rev-parse", "HEAD^{commit}"], timeout=60).stdout.strip()
            base = self.shell.run(["git", "-C", self.copy, "rev-parse", "--verify", f"refs/tags/{project.TAG}^{{commit}}"], timeout=60).stdout.strip()
            dirty = self.shell.run(["git", "-C", self.copy, "status", "--porcelain"], timeout=60).stdout
        except ShellError as error:
            raise ResetFailed(f"resetting the copy to {project.TAG} failed: {error}") from error
        if not head or head != base:
            raise ResetFailed(f"after the reset HEAD is {head[:10] or 'unknown'}, not {project.TAG} ({base[:10] or 'unknown'})")
        stray = [line for line in dirty.splitlines() if line.strip() and not self.allowed_dirty(line)]
        if stray:
            raise ResetFailed(f"after the reset the copy is not clean: {'; '.join(s.strip() for s in stray[:5])}")
        for source, target in self.fixtures(spec):
            out = Path(self.copy) / target
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, out)

    @staticmethod
    def allowed_dirty(line):
        """A `git status --porcelain` line about a path the workspace itself owns (its state dir, the linked node_modules)."""
        path = line[3:].split(" -> ")[-1].strip().strip('"')
        return any(path == name or path.startswith(name + "/") for name in ALLOWED_DIRTY)

    def fixtures(self, spec):
        return [(self.bench_dir / "fixtures" / name, name) for name in spec.get("fixtures", [])]

    def wait(self, ident, deadline):
        """Poll `tp-agent inspect` until the task is terminal. Returns (record, stopped_for): None when the task finished,
        "timeout" when it ran over its budget, "trust-dialog" when a worker is stuck at Claude Code's folder-trust prompt.
        The budget is checked on every observation, terminal or not: a result first seen after the deadline is a timeout."""
        while True:
            self.agent("tick", tolerate="tick only nudges the scheduler; the status read by the checked inspect below decides the task")
            info = self.agent_json("inspect", ident)
            late = self.clock() >= deadline  # the time of this first look at the result
            if info.get("status") in TERMINAL:
                return info, "timeout" if late else None
            if self.trust_dialog_shown():
                return info, "trust-dialog"
            if late:
                return info, "timeout"
            self.sleep(self.poll_s)

    def stopped_entry(self, spec, entry, why):
        """Mark a task whose worker was stopped by the runner: a timeout fails the task, a trust dialog is an error."""
        if why == "trust-dialog":
            entry["status"], entry["detail"]["error"] = "error", "trust-dialog"
            entry["detail"]["reason"] = "the worker stopped at Claude Code's folder-trust dialog; run bin/tp-claude-bootstrap on the work root once"
        else:
            entry["status"], entry["detail"]["reason"] = "timeout", f"over the {spec['budget_s']} s budget"

    def evidence(self, ident, info):
        """task id, task dir, transcript path, tokens (figures or None), model for one worker task."""
        task_dir = info.get("task_dir")
        transcript = info.get("transcript") if isinstance(info.get("transcript"), str) else None
        if not transcript and task_dir:
            pi_file, claude_file = Path(task_dir) / "transcript.jsonl", Path(task_dir) / "transcript-path"
            if pi_file.is_file():
                transcript = str(pi_file)
            elif claude_file.is_file():
                transcript = claude_file.read_text().strip() or None
        raw = info.get("tokens")
        return {"id": ident, "task_dir": task_dir, "transcript": transcript,
                "tokens": record.token_figures(raw) if isinstance(raw, dict) else None, "model": info.get("model"),
                "status": info.get("status"), "result_path": info.get("result_path")}

    def dispatch(self, spec):
        role = specs_mod.ROLE[spec["kind"]]
        args = ["dispatch", "--role", role, "--task", spec.get("brief") or spec["question"], "--workdir", self.copy, "--tests", "affected"]
        if spec["kind"] == "local":
            args.append("--local-only")
        else:
            args += ["--level", spec.get("level", specs_mod.DEFAULT_LEVEL)]
        return self.agent_json(*args)["id"]

    def new_entry(self, spec, status="error", detail=None):
        """A task's record entry, made from its spec before it runs: the planted defects the metrics divide by come from the
        sealed key here, never from what the run printed, so a task that errors or times out still counts in every denominator."""
        planted = 0
        if spec["kind"] == "review" and not spec.get("clean"):
            planted = len(answers.read_json(spec, self.answers_directory).get("defects", []))
        return {"id": spec["id"], "kind": spec["kind"], "clean": bool(spec.get("clean")), "planted": planted, "status": status, "task_ids": [],
                "task_dirs": [], "transcripts": [], "tokens": None, "tool_counts": {}, "wall_s": 0.0, "detail": detail or {}, "models": []}

    def run_task(self, spec):
        started = self.clock()
        entry = self.new_entry(spec)
        try:
            self.reset_copy(spec)
            if spec["kind"] == "review":
                self.run_review(spec, entry, started)
            else:
                self.run_worker(spec, entry, started)
        except ResetFailed as error:  # nothing was dispatched; run_repeat marks the rest of the repeat the same way
            entry["status"], entry["detail"]["error"], entry["detail"]["reason"] = "not_run", "reset-failed", str(error)[:300]
        except (RunnerError, ShellError, answers.AnswerError, OSError, KeyError) as error:
            entry["status"], entry["detail"]["error"] = "error", str(error)[:300]
        entry.pop("_tokens", None)
        entry["wall_s"] = round(self.clock() - started, 3)
        return entry

    def add_evidence(self, entry, ev):
        entry["task_ids"].append(ev["id"])
        entry["task_dirs"].append(ev["task_dir"])
        entry["transcripts"].append(ev["transcript"])
        if ev["model"] and ev["model"] not in entry["models"]:
            entry["models"].append(ev["model"])
        entry["_tokens"].append(ev["tokens"])

    @staticmethod
    def finish_tokens(entry):
        """Sum the task's workers; a figure unknown for any of them is None (never 0)."""
        parts = entry.pop("_tokens")
        entry["tokens"] = record.add_tokens([p or {} for p in parts]) if parts else None

    def run_worker(self, spec, entry, started):
        entry["_tokens"] = []
        ident = self.dispatch(spec)
        info, stopped = self.wait(ident, started + spec["budget_s"])
        ev = self.evidence(ident, info)
        self.add_evidence(entry, ev)
        try:
            if stopped is not None:
                self.stopped_entry(spec, entry, stopped)
            elif info["status"] not in FINISHED:
                entry["status"], entry["detail"]["reason"] = "fail", f"worker ended {info['status']}"
            else:
                self.score_worker(spec, entry, ev)
            self.finish_tokens(entry)
        finally:
            self.end_worker(entry, ident, info, stopped)

    def end_worker(self, entry, ident, info, stopped):
        """A worker the runner gave up on while it still ran is stopped; one that already finished (even if late) is collected.
        A stop or collect that fails is written into the task's detail, never dropped."""
        try:
            if stopped is not None and info.get("status") not in TERMINAL:
                self.agent("stop", ident)
            else:
                self.agent("collect", ident)
        except ShellError as error:
            entry["detail"]["worker_end_failed"] = str(error)[:300]

    def score_worker(self, spec, entry, ev):
        kind = spec["kind"]
        if kind == "orient":
            calls = trace.load_calls(ev["transcript"]) if ev["transcript"] else None
            if calls is None:
                entry["status"], entry["detail"]["reason"] = "error", "no readable transcript"
                return
            summary = trace.analyse(calls)
            entry["tool_counts"] = summary.pop("tool_counts")
            entry["detail"].update(summary)
            entry["status"] = "scored"
            return
        if ev["transcript"]:
            calls = trace.load_calls(ev["transcript"])
            if calls is not None:
                entry["tool_counts"] = trace.analyse(calls)["tool_counts"]
        if kind == "local":
            text = Path(ev["result_path"]).read_text() if ev["result_path"] else ""
            expected = answers.read_json(spec, self.answers_directory)["answer"]
            entry["detail"]["answer"] = match.reply_answer(text)
            entry["status"] = "pass" if match.score_local(text, expected) else "fail"
            return
        source = answers.verify(spec, self.answers_directory)
        target = Path(self.copy) / spec["test_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        ran = self.shell.run(["node_modules/.bin/vitest", "run", "--reporter=json", spec["test_path"]], cwd=self.copy, timeout=180,
                             tolerate="a failing test run is the score: match.code_passed reads the exit code and the JSON report")
        parsed = match.parse_vitest(ran.stdout)
        entry["detail"]["tests"] = {"passed": parsed[0], "total": parsed[1]} if parsed else None
        entry["status"] = "pass" if match.code_passed(ran.returncode, parsed) else "fail"

    def run_review(self, spec, entry, started):
        entry["_tokens"] = []
        key = answers.read_json(spec, self.answers_directory)
        patch = self.bench_dir / spec["diff"]
        try:
            self.shell.run(["git", "-C", self.copy, "apply", str(patch)], timeout=60)
        except ShellError as error:
            raise RunnerError(f"patch {spec['diff']} does not apply: {error}") from error
        author = self.stub_author(spec)
        entry["task_ids"].append(author)
        entry["task_dirs"].append(str(Path(self.state_dir) / "tasks" / author))
        entry["transcripts"].append(None)
        entry["tokens"] = None
        started_review = self.agent("review", author, "--force", timeout=300,
                                    tolerate="a refused review is recorded as this task's error with tp-agent's own message, below")
        if started_review.returncode != 0:
            entry["status"], entry["detail"]["reason"] = "error", (started_review.stderr or started_review.stdout).strip()[:300]
            return
        reviewer = self.reviewer_id(started_review.stdout)
        info, stopped = self.wait(reviewer, started + spec["budget_s"])
        ev = self.evidence(reviewer, info)
        self.add_evidence(entry, ev)
        try:
            if stopped:
                self.stopped_entry(spec, entry, stopped)
            else:
                text = Path(ev["result_path"]).read_text() if ev["result_path"] else ""
                entry["detail"].update(match.score_review(text, key, bool(spec.get("clean"))))
                entry["status"] = "scored"
            self.finish_tokens(entry)
        finally:
            self.end_worker(entry, reviewer, info, stopped)

    @staticmethod
    def reviewer_id(output):
        try:
            data = json.loads(output)
            if isinstance(data, dict) and isinstance(data.get("id"), str):
                return data["id"]
        except ValueError:
            pass
        found = re.findall(r"\b(review-[A-Za-z0-9._-]+)", output)
        if not found:
            raise RunnerError("tp-agent review printed no reviewer id: " + output.strip()[:200])
        return found[-1]

    def stub_author(self, spec):
        """A finished author task for the prepared diff: tp-agent review reviews the recorded task and its working tree."""
        ident = f"bench-author-{spec['id']}"
        task_dir = Path(self.state_dir) / "tasks" / ident
        task_dir.mkdir(parents=True, exist_ok=True)
        meta = {"id": ident, "role": "coder", "worker": "pi", "model": STUB_MODEL, "workdir": str(self.copy), "created": self.stamp(),
                "deliverable": "the change in the working tree"}
        (task_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
        (task_dir / "request.md").write_text(spec["request"].strip() + "\n")
        (task_dir / "result.md").write_text("Done as asked; the change is in the working tree.\n\nRESULT_STATUS: complete\n")
        (task_dir / "exit").write_text("0\n")
        (task_dir / "state.json").write_text(json.dumps({"state": "waiting", "model": STUB_MODEL, "updated": self.stamp()}) + "\n")
        return ident

    # ---- evidence kept after the copy is gone ------------------------------
    def keep_artifacts(self, tasks, n):
        """Copy the task dirs and transcripts each entry cites into results/<label>/<n>/artifacts/<task>/ and make the entry name the
        copies, so every path in a written record still exists after the project copy is removed. A path that cannot be copied
        becomes None (unavailable is null). `.env*` files and the Claude profile's trust store are never copied."""
        root = self.results_dir / self.label / str(n) / "artifacts"
        root.mkdir()  # exclusive: the number was claimed by record.reserve and nothing else writes under it
        for t in tasks:
            sources_dirs, sources_files = list(t["task_dirs"]), list(t["transcripts"])
            kept_dirs, kept_files = [], []
            for i, (src_dir, src_file) in enumerate(zip(sources_dirs, sources_files)):
                out = root / t["id"]
                dest_dir = self.copy_tree(src_dir, out / "tasks" / f"{i}-{Path(src_dir).name}") if src_dir else None
                kept_dirs.append(dest_dir)
                if not src_file:
                    kept_files.append(None)
                elif src_dir and dest_dir and Path(src_file).is_relative_to(src_dir):
                    inside = Path(dest_dir) / Path(src_file).relative_to(src_dir)
                    kept_files.append(str(inside) if inside.is_file() else None)
                else:
                    kept_files.append(self.copy_file(src_file, out / "transcripts" / f"{i}-{Path(src_file).name}"))
            t["task_dirs"], t["transcripts"] = kept_dirs, kept_files

    @staticmethod
    def copy_tree(src, dest):
        try:
            if not Path(src).is_dir():
                return None
            shutil.copytree(src, dest, symlinks=True, ignore=shutil.ignore_patterns(*NEVER_COPY))
            return str(dest)
        except (OSError, shutil.Error):
            return None

    @staticmethod
    def copy_file(src, dest):
        try:
            if not Path(src).is_file() or any(Path(src).match(p) for p in NEVER_COPY):
                return None
            record.copy_file_exclusive(src, dest)
            return str(dest)
        except OSError:
            return None

    # ---- repeats ---------------------------------------------------------
    def run_repeat(self, n, selected, deadline, arm_info, task_hash, versions):
        self.session = f"{SESSION_PREFIX}{self.label}-{n}-{self.token()}"
        self.state_dir, self.agent_env, self.started = None, {}, False
        parent = self.work_root / self.session
        parent.mkdir(parents=True, exist_ok=False)
        self.copy = str(parent / "project")
        ordered = order_tasks(selected, self.order, self.seed, n)
        tasks, began, failure, reset_broken = [], self.clock(), None, False
        try:
            project.make_copy(self.shell, self.project_dir, self.copy)
            self.start_workspace()
            for spec in ordered:
                if reset_broken:  # the copy is not on the base tag: nothing more may run in it, and each still counts in the denominators
                    tasks.append(self.new_entry(spec, "not_run", {"error": "reset-failed", "reason": "an earlier reset in this repeat failed"}))
                    continue
                if deadline is not None and self.clock() >= deadline:
                    tasks.append(self.new_entry(spec, "not_run", {"reason": "--budget-minutes used up"}))
                    continue
                tasks.append(self.run_task(spec))
                reset_broken = tasks[-1]["detail"].get("error") == "reset-failed"
                self.log(f"{self.session} {spec['id']}: {tasks[-1]['status']}")
            self.keep_artifacts(tasks, n)  # while the copy and the workspace still exist
        except BaseException as error:
            failure = error
            raise
        finally:
            stop = self.stop_safely()
            incomplete = stop.state == "unknown"
            leftover = {"session": self.session, "copy": self.copy, "work_dir": str(parent)} if incomplete else None
            if not incomplete:  # absence or a foreign owner is proven; with "unknown" a live workspace may still use the copy
                project.remove_copy(self.shell, self.project_dir, self.copy)
                shutil.rmtree(parent, ignore_errors=True)
            else:
                self.log(f"cleanup incomplete: {stop.reason}; left {self.copy} and {parent} for manual cleanup")
            if failure is not None and stop.state not in ("stopped", "none"):  # no run record will be written, so say it here
                record.write(record.cleanup_path(self.results_dir, self.label, n),
                             {"session": self.session, "stop": stop.state, "stop_refused": stop.reason,
                              "cleanup": "incomplete" if incomplete else "complete", "leftover": leftover, "error": repr(failure)[:300]})
        wall = round(self.clock() - began, 3)
        totals, sources = record.run_tokens(tasks)
        models = sorted({m for t in tasks for m in t["models"]})
        return {"schema": record.SCHEMA, "label": self.label, "n": n, "finished": self.stamp(), "wall_s": wall, "arm": arm_info,
                "task_set_hash": task_hash, "order": {"mode": self.order, "seed": self.seed, "tasks": [s["id"] for s in ordered]},
                "session": self.session, "stop": stop.state, "stop_refused": stop.reason if stop.state in ("refused", "unknown") else None,
                "cleanup": "incomplete" if incomplete else "complete", "leftover": leftover,
                "env": dict(self.env), "versions": versions, "models": models, "tasks": tasks,
                "metrics": record.metrics(tasks), "tokens": totals, "token_sources": sources}

    def run(self, selected, repeat, budget_minutes=None, max_worker_runs=DEFAULT_MAX_WORKER_RUNS, versions=None, arm_info=None):
        check_guard(len(selected), repeat, max_worker_runs)
        for spec in selected:
            answers.verify(spec, self.answers_directory)
        manifest = self.bench_dir / "fixtures" / "agent-tools.json"
        project.ensure_base(self.shell, self.project_dir, manifest)
        deadline = self.clock() + budget_minutes * 60 if budget_minutes else None
        task_hash = specs_mod.task_set_hash(selected, self.bench_dir)
        written = []
        for _ in range(repeat):
            n = record.reserve(self.results_dir, self.label)  # the number is claimed (a directory) before anything is written under it
            data = self.run_repeat(n, selected, deadline, dict(arm_info or {}), task_hash, dict(versions or {}))
            path = record.record_path(self.results_dir, self.label, n)
            record.write(path, data)
            written.append(path)
        return written
