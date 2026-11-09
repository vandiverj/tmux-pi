"""tools/smoke/g3_e2e.py in --fake mode on a tiny temp repo (never battle-soccer, never a model): the whole flow, a failure injected at each step
(cleanup ran, the error names the step), the permission-prompt trigger shown to ask in every redaction mode by the real hooks, and the guards:
no repo-wide worktree prune (F2), test hooks refused in a live run (F3), every subprocess bounded (F4), and the tmux isolation (resume-1).
The tampered-record cases are in tests/test_g3_e2e_tamper.py. The shim and the audit are in tests/g3_e2e_support.py."""
import g3_e2e_support as support  # first: it imports session_env, which drops the caller's session

import contextlib
import importlib.util
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

from g3_e2e_support import Base, Parallel, ROOT, g3, records

setUpModule, tearDownModule = support.set_up_module, support.tear_down_module
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@invalid"}
_PARALLEL = Parallel({})
F2_REPO = {}
CONFIG_REPO = {}      # the repositories with their own lane config: "ok" (a full run) and "taken" (its integration branch already exists)
CONFIG = {"branch_prefix": "g3p/", "integration_branch": "g3int/scratch", "worktree_pattern": "{repo}-g3trees-{name}"}
HOOK = """#!/bin/sh
# every ref update that was committed in this repository (and its worktrees): "<old> <new> <ref>" per line
if [ "$1" = committed ]; then cat >> '%s'; else cat > /dev/null; fi
exit 0
"""


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=True, env={**os.environ, **GIT_ENV}, timeout=60).stdout


def config_repo(label, existing_integration=False):
    """A disposable repository whose committed lane config sets another integration_branch, branch_prefix and worktree_pattern than the
    defaults. A reference-transaction hook logs every ref update, so a test can say what was (never) created. `existing_integration`: the
    configured integration branch is already there, as another run's would be."""
    repo = support.WORK / f"cfg-{label}"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("tiny\n")
    (repo / support.CONFIG_DIR).mkdir()
    (repo / support.CONFIG_DIR / "lanes.json").write_text(json.dumps(CONFIG) + "\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "init")
    if existing_integration:
        git(repo, "branch", CONFIG["integration_branch"])
    log = support.WORK / f"cfg-{label}-refs.log"
    log.write_text("")
    hook = repo / ".git" / "hooks" / "reference-transaction"
    hook.write_text(HOOK % log)
    hook.chmod(0o755)
    CONFIG_REPO[label] = {"path": repo, "log": log}
    return repo


def results():
    """Every full fake run the module needs, started together (memoised). `f2` is a run on a repository that already has an unrelated worktree
    whose directory is gone (a prunable registration): a repo-wide `git worktree prune` would drop it."""
    if not _PARALLEL.cases:
        repo = support.WORK / "f2-repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        (repo / "README.md").write_text("tiny\n")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "init")
        other = support.WORK / "f2-unrelated"
        git(repo, "worktree", "add", "-q", "-b", "unrelated", str(other))
        shutil.rmtree(other)
        F2_REPO["path"], F2_REPO["other"] = repo, other
        _PARALLEL.cases.update({"flow": []})
        _PARALLEL.cases.update({f"inject-{step}": ["--inject-failure", step] for step in g3.STEPS if step != "setup"})
        _PARALLEL.cases["f2"] = ["--repo", str(repo)]
        _PARALLEL.cases["config-ok"] = ["--repo", str(config_repo("ok"))]
        _PARALLEL.cases["config-taken"] = ["--repo", str(config_repo("taken", existing_integration=True))]
    return _PARALLEL()


class FlowTest(Base):
    def test_whole_flow_fake(self):
        rc, summary, err, out = results()["flow"]
        self.assertEqual((rc, err), (0, ""), summary.get("error"))
        self.assertIn("g3_e2e: ok (fake)", out)
        self.assertTrue(summary["ok"])
        self.assertEqual([s["name"] for s in summary["steps"]], list(g3.STEPS))
        for step in summary["steps"]:
            self.assertEqual(step["rc"], 0, step)
            self.assertTrue(step["asserted"], f"step {step['name']} asserted nothing")
        self.assert_clean(summary)
        self.assertEqual(set(summary["ids"]), {"first", "retry", "reviewer"})
        self.assertGreater(len(records()), 3)      # the runs did use tmux, all through the shim

    def test_every_parallel_run_used_only_its_own_private_tmux(self):
        """Each run's calls: its own TMUX_TMPDIR and -L socket (equality with its summary), and exactly one kill-server, its own."""
        runs = results()
        calls = records()
        self.assertGreater(len(calls), 40)
        summaries = [value[1] for value in runs.values()]
        self.assertEqual(len({s["tmux_tmpdir"] for s in summaries}), len(runs), "one private TMUX_TMPDIR per run")
        self.assertEqual(len({s["socket"] for s in summaries}), len(runs), "one private socket per run")
        stopped = support.audit(calls, support.RUNS)
        for summary in summaries:
            started = any(c["TMUX_TMPDIR"] == summary["tmux_tmpdir"] and "new-session" in c["argv"] for c in calls)   # a run refused at setup starts none
            self.assertEqual(stopped.get(summary["tmux_tmpdir"], 0), 1 if started else 0, f"{summary['run']}: each run stopped its own server once")


class HookTriggerTest(Base):
    """F1: the prompt trigger asks in every redaction mode a worker can run in, decided by the real hooks, and reads nothing."""

    @classmethod
    def setUpClass(cls):
        loader = SourceFileLoader("tp_sensitive_read", str(ROOT / "bin" / "tp-sensitive-read"))
        cls.sensitive = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(cls.sensitive)        # puts lib/ on sys.path
        import redact_hook
        cls.redact_hook = redact_hook

    def setUp(self):
        super().setUp()
        self.base = Path(tempfile.mkdtemp(prefix="g3hook-"))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.home, self.repo = self.base / "home", self.base / "repo"
        self.task = self.base / "state" / "tasks" / "coder-g3hook"
        for path in (self.home, self.repo, self.task):
            path.mkdir(parents=True)

    def event(self, name):
        """The event Claude Code gives a hook for the worker's Bash call: the command the first task tells the worker to run."""
        return {"hook_event_name": name, "tool_name": "Bash", "tool_input": {"command": g3.PROMPT_COMMAND, "description": "g3 smoke"},
                "cwd": str(self.repo), "session_id": "g3hook", "tool_use_id": "toolu_g3hook"}

    def env(self, mode):
        return {"HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(self.home / ".claude"), "TP_REDACTION": mode, "TP_TASK_DIR": str(self.task),
                "TP_TASK_ID": "coder-g3hook", "TP_REDACTION_DIR": str(self.base / "redaction"), "TP_REDACTION_LLM_URL": "http://127.0.0.1:9"}

    def decide(self, hook, event):
        """The decision of one real hook entry point for one event: (permissionDecision or None). `redact`: lib/redact_hook.handle (what
        bin/tp-redact --claude-hook runs); `sensitive`: bin/tp-sensitive-read main() with the arguments hooks/sensitive-read.sh passes."""
        if hook == "redact":
            output = self.redact_hook.handle(event)
        else:
            out = io.StringIO()
            with mock.patch.object(sys, "argv", ["tp-sensitive-read", "--claude-hook", "--trusted", "--home", str(self.home)]), \
                    mock.patch.object(sys, "stdin", io.StringIO(json.dumps(event))), contextlib.redirect_stdout(out):
                self.sensitive.main()
            output = json.loads(out.getvalue()) if out.getvalue().strip() else None
        return ((output or {}).get("hookSpecificOutput") or {}).get("permissionDecision")

    def request(self):
        return json.loads((self.task / "permission-request.json").read_text())

    def test_the_trigger_is_only_noop_builtins_with_operands(self):
        """Harmless by construction: it parses to exactly `:` (with the two operands) and `exit 3`, joined by `;`. No other command, no
        redirection, no substitution, no pipe or list operator."""
        lex = shlex.shlex(g3.PROMPT_COMMAND, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
        commands, current = [], []
        for token in tokens:
            if set(token) <= set(";&|<>()"):
                self.assertEqual(token, ";", f"an operator other than ';': {tokens}")
                commands.append(current)
                current = []
            else:
                current.append(token)
        commands.append(current)
        self.assertEqual([c[0] for c in commands], [":", "exit"], tokens)
        self.assertEqual(commands[0][1:], [f"./{g3.ABSENT}", f"${g3.SECRET_VAR}"], "the operands are only text for the hooks to match")
        self.assertEqual(commands[1], ["exit", "3"])
        self.assertNotIn("`", g3.PROMPT_COMMAND)
        self.assertNotIn("$(", g3.PROMPT_COMMAND)
        self.assertIn(f"'${g3.SECRET_VAR}'", g3.PROMPT_COMMAND, "single quoted: the shell does not even expand it")

    def test_accepting_the_trigger_is_harmless(self):
        """The owner accepts the dialog: the exact command runs under bash. The dotenv-shaped file exists with sentinel content and the secret-shaped
        variable is set to the path of another sentinel file; neither content nor path may come out, and the exit code is 3."""
        file_sentinel, var_sentinel = "SENTINEL-" + "OF-THE-FILE-OPERAND", "SENTINEL-" + "OF-THE-VARIABLE-TARGET"
        (self.repo / g3.ABSENT).write_text(file_sentinel + "\n")
        target = self.base / "variable-target.txt"
        target.write_text(var_sentinel + "\n")
        env = {**os.environ, g3.SECRET_VAR: str(target)}
        run = subprocess.run(["bash", "-c", g3.PROMPT_COMMAND], cwd=self.repo, env=env, text=True, capture_output=True, timeout=30)
        self.assertEqual(run.returncode, 3, run.stderr)
        for seen in (run.stdout, run.stderr):
            for secret in (file_sentinel, var_sentinel, str(target), g3.ABSENT):
                self.assertNotIn(secret, seen)
        self.assertEqual((run.stdout, run.stderr), ("", ""))
        self.assertEqual((self.repo / g3.ABSENT).read_text(), file_sentinel + "\n", "the file was not touched")

    def test_the_worker_loads_both_hooks_for_every_tool(self):
        hooks = json.loads((ROOT / "claude/plugins/tmux-pi/hooks/hooks.json").read_text())["hooks"]
        for event in ("PreToolUse", "PermissionDenied"):
            commands = [h["command"] for group in hooks[event] if group.get("matcher") == "*" for h in group["hooks"]]
            self.assertTrue(any("sensitive-read.sh" in c for c in commands), (event, commands))
            self.assertTrue(any("redact.sh" in c for c in commands), (event, commands))

    def test_the_trigger_asks_in_every_mode_then_is_denied(self):
        for mode, asker, quiet in (("on", "redact", "sensitive"), ("shadow", "sensitive", "redact"), ("off", "sensitive", "redact")):
            with self.subTest(mode=mode), mock.patch.dict(os.environ, self.env(mode)):
                if (self.task / "permission-request.json").exists():
                    (self.task / "permission-request.json").unlink()
                self.assertEqual(self.decide(asker, self.event("PreToolUse")), "ask")
                self.assertIsNone(self.decide(quiet, self.event("PreToolUse")), "the other hook defers in this mode")
                pending = self.request()
                self.assertEqual(pending["status"], "pending")
                self.assertTrue(pending["reason"] and pending["target"], pending)
                self.decide(asker, self.event("PermissionDenied"))      # the owner declined the dialog
                self.assertEqual(self.request()["status"], "denied")
                self.assertFalse((self.repo / g3.ABSENT).exists())

    def test_trust_guard_lets_the_trigger_through(self):
        env = {**os.environ, **self.env("on")}
        run = subprocess.run([str(ROOT / "bin" / "tp-trust-guard"), "--claude-hook", "--home", str(self.home)], input=json.dumps(self.event("PreToolUse")),
                             text=True, capture_output=True, env=env, timeout=30)
        decision = (json.loads(run.stdout).get("hookSpecificOutput") or {}).get("permissionDecision") if run.stdout.strip() else None
        self.assertEqual((run.returncode, decision), (0, None), run.stderr)


class InjectedFailureTest(Base):
    """A failure injected at each step: exit 1, the error names the step, the cleanup undid what the earlier steps made."""

    def test_failure_at_each_step(self):
        for step in g3.STEPS:
            if step == "setup":
                continue              # the earliest point: in-process, below
            with self.subTest(step=step):
                rc, summary, err, _ = results()[f"inject-{step}"]
                self.assertEqual(rc, 1)
                self.assertFalse(summary["ok"])
                self.assertEqual(summary["failed_step"], step)
                self.assertIn(f"step '{step}'", err)
                self.assertIn("injected failure", err)
                self.assertEqual(summary["steps"][-1]["name"], step)
                self.assertEqual(len(summary["steps"]), g3.STEPS.index(step) + 1)
                self.assert_clean(summary)

    def test_failure_before_any_tmux_call_makes_none_and_stops_no_server(self):
        """The earliest point: the failure comes before the private server exists, so no call is made at all."""
        rc, summary, err = self.smoke("--inject-failure", "setup")
        self.assertEqual(rc, 1)
        self.assertIn("step 'setup'", err)
        self.assert_clean(summary)
        self.assertEqual(records()[self.mark:], [])

    def test_missing_tmux_fails_setup_by_name_without_tmux_calls(self):
        with mock.patch.object(g3.shutil, "which", return_value=None):
            rc, summary, err = self.smoke()
        self.assertEqual(rc, 1)
        self.assertIn("step 'setup': tmux is not installed", err)
        self.assert_clean(summary)
        self.assertEqual(records()[self.mark:], [])


class PruneTest(Base):
    """F2: the cleanup removes this run's worktree by path; it does not run a repository-wide `git worktree prune`."""

    def test_an_unrelated_prunable_worktree_survives_a_run(self):
        rc, summary, err, _ = results()["f2"]
        self.assertEqual((rc, err), (0, ""), summary.get("error"))
        self.assertEqual(summary["cleanup"], {"ran": True, "errors": [], "leftovers": [], "kept": []})
        listing = git(F2_REPO["path"], "worktree", "list", "--porcelain")
        self.assertIn(str(F2_REPO["other"]), listing, "the unrelated registration was pruned")
        block = listing[listing.index(f"worktree {F2_REPO['other']}"):].split("\n\n")[0]
        self.assertIn("prunable", block)
        self.assertNotIn(summary["run"], listing)
        self.assertEqual(git(F2_REPO["path"], "branch", "--list", "lane/*").strip(), "")      # the run's lane branch and lane/integrate are gone

    def test_the_script_never_calls_prune(self):
        self.assertNotIn("prune", (ROOT / "tools/smoke/g3_e2e.py").read_text().replace("No `git worktree prune`", ""))


class HookFlagsTest(Base):
    """F3: --inject-failure and --tamper are refused unless --fake, before anything is made."""

    def test_refused_without_fake(self):
        for flag in (["--inject-failure", "dispatch"], ["--tamper", "lane-record"]):
            with self.subTest(flag=flag[0]):
                err = io.StringIO()
                with contextlib.redirect_stderr(err), mock.patch.object(g3.Smoke, "__init__", side_effect=AssertionError("constructed")), \
                        self.assertRaises(SystemExit) as caught:
                    g3.main(["--out", str(self.out), "--repo", str(support.WORK), *flag])
                self.assertEqual(caught.exception.code, 2)
                self.assertIn("--fake test hook", err.getvalue())
                self.assertFalse(self.out.exists())

    def test_refused_as_a_command_before_any_tmux_call(self):
        for flag in (["--inject-failure", "dispatch"], ["--tamper", "lane-record"]):
            with self.subTest(flag=flag[0]):
                run = subprocess.run([sys.executable, str(ROOT / "tools/smoke/g3_e2e.py"), "--out", str(self.out), "--repo", str(support.WORK), *flag],
                                     text=True, capture_output=True, timeout=30)
                self.assertEqual(run.returncode, 2)
                self.assertIn("--fake test hook", run.stderr)
        self.assertEqual(records()[self.mark:], [])

    def test_tamper_is_refused_on_a_repository_that_is_not_the_runs_own(self):
        """The tampers edit the repository (`not-git` deletes .git): they run only on the throwaway repository a fake run makes."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            g3.parse(["--fake", "--out", "x", "--repo", str(support.WORK), "--tamper", "not-git"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("not with --repo", err.getvalue())

    def test_accepted_with_fake(self):
        args = g3.parse(["--fake", "--out", "x", "--tamper", "lane-record", "--inject-failure", "close"])
        self.assertEqual((args.tamper, args.inject_failure), ("lane-record", "close"))


class BoundedTest(Base):
    """F4: every subprocess has a timeout; a timeout is a cleanup error and neither the rest of the cleanup nor the summary is skipped."""

    def test_bounded_gives_up_on_a_grandchild_that_holds_the_pipes(self):
        t0 = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            g3.bounded(["sh", "-c", "sleep 6 & sleep 6"], 1)
        self.assertLess(time.monotonic() - t0, 5)

    def test_every_subprocess_call_in_the_script_passes_a_timeout(self):
        source = (ROOT / "tools/smoke/g3_e2e.py").read_text()
        self.assertEqual(source.count("subprocess.run("), 0)
        self.assertEqual(source.count("subprocess.Popen("), 1)     # inside bounded(), which always calls communicate(timeout=...)

    def test_a_hanging_tmux_fails_the_step_and_still_writes_the_summary(self):
        """The first tmux call hangs (a shim that sleeps): the step fails by name after --subprocess-timeout, the cleanup's tmux calls and the
        leftover probe time out and are recorded as cleanup errors, the rest of the cleanup runs, and the JSON summary is written."""
        t0 = time.monotonic()
        with mock.patch.dict(os.environ, {"G3_SHIM_HANG": "new-session kill-session list-panes"}):
            rc, summary, err = self.smoke("--subprocess-timeout", "2")
        self.assertLess(time.monotonic() - t0, 40)
        self.assertEqual(rc, 1)
        self.assertEqual(summary["failed_step"], "setup")
        self.assertIn("did not finish within 2 s", summary["error"])
        self.assertTrue(summary["cleanup"]["ran"])
        errors = " | ".join(summary["cleanup"]["errors"])
        self.assertIn("leftover probe 'tmux list-panes' failed: TimeoutExpired", errors)
        self.assertEqual(summary["made"]["session"], False, "the run's own session was never made (the call before it hung)")
        self.assertFalse([c for c in records()[self.mark:] if "kill-session" in c["argv"]], "nothing was made, so nothing is killed")
        self.assertIn("cleanup: ", err)
        self.assertFalse(Path(summary["repo"]).exists(), "the rest of the cleanup ran")
        self.assertFalse(Path(summary["tmux_tmpdir"]).exists())

    def test_a_timeout_in_the_cleanup_does_not_stop_the_rest(self):
        """Every tmux call of the cleanup hangs except kill-server: each is a recorded error, the integration branch is still deleted, the
        private server is still stopped, the private directories are still removed."""
        out = support.WORK / "hang-cleanup.json"
        smoke = g3.Smoke(g3.parse(["--fake", "--out", str(out), "--subprocess-timeout", "1", "--keep"]))
        self.addCleanup(shutil.rmtree, smoke.tmp, ignore_errors=True)
        support.RUNS.append({"run": smoke.name, "tmux_tmpdir": str(smoke.tmux_tmp), "socket": smoke.sock})
        smoke.fake_setup()
        smoke.start_session()
        pane = smoke.tmux("list-panes", "-a", "-F", "#{pane_id}").stdout.split()[0]
        (smoke.state / "tasks" / "coder-x").mkdir()
        (smoke.state / "tasks" / "coder-x" / "meta.json").write_text(json.dumps({"pane": pane}))
        smoke.facts["common"] = str((smoke.repo / git(smoke.repo, "rev-parse", "--git-common-dir").strip()).resolve())
        smoke.load_config()
        git(smoke.repo, "branch", smoke.integration)
        smoke.made["integration"] = True
        repo, tmux_tmp = smoke.repo, smoke.tmux_tmp
        self.assertTrue(smoke.made["session"])
        with mock.patch.dict(os.environ, {"G3_SHIM_HANG": "kill-pane kill-session list-panes"}):
            smoke.cleanup()
        errors = " | ".join(smoke.cleanup_report["errors"])
        for expected in ("list the session's panes: TimeoutExpired", "kill session: TimeoutExpired", "leftover probe 'tmux list-panes' failed: TimeoutExpired"):
            self.assertIn(expected, errors)
        self.assertNotIn("kill pane", errors, "a pane that could not be verified as the session's is not killed")
        self.assertNotIn("stop the private tmux server", errors)
        self.assertEqual(git(repo, "branch", "--list", smoke.integration).strip(), "", "the next cleanup action ran after the timeouts")
        self.assertFalse(tmux_tmp.exists())
        self.assertEqual(len([c for c in records()[self.mark:] if "kill-server" in c["argv"]]), 1)


class SessionOwnershipTest(Base):
    """F1 (round 3): the cleanup kills a tmux session only if this run made it. Fake mode, the recording shim, a real private tmux server.
    The shim can make a rival session of the run's name (G3_SHIM_RIVAL), hang a call (G3_SHIM_HANG), or let the call succeed and then hang
    the client (G3_SHIM_AFTER); G3_SHIM_MATCH='-s g3-' aims it at the run's own session and not at the "anchor" one."""

    OWN = {"G3_SHIM_MATCH": "-s g3-"}

    def run_with(self, **env):
        with mock.patch.dict(os.environ, {**self.OWN, **env}):
            rc, summary, err = self.smoke("--subprocess-timeout", "2")
        calls = [c["argv"] for c in records()[self.mark:] if c["TMUX_TMPDIR"] == summary["tmux_tmpdir"]]
        return rc, summary, err, calls

    @staticmethod
    def index(calls, word, *needles):
        return [i for i, argv in enumerate(calls) if word in argv and all(n in argv for n in needles)]

    def test_a_definite_refusal_means_the_run_never_owned_it(self):
        """A rival makes the session first; new-session answers `duplicate session`. Nothing is recorded, nothing is killed, and the
        rival's session is still there with its pane afterwards (the leftover list names it; it is reported, not removed)."""
        rc, summary, err, calls = self.run_with(G3_SHIM_RIVAL="new-session")
        self.assertEqual(rc, 1)
        self.assertEqual(summary["failed_step"], "setup")
        self.assertIn("duplicate session", summary["error"])
        self.assertEqual(summary["made"]["session"], False)
        self.assertEqual(self.index(calls, "kill-session"), [], calls)
        self.assertEqual(self.index(calls, "kill-pane"), [])
        self.assertTrue(any(f"in session {summary['run']}" in line for line in summary["cleanup"]["leftovers"]), summary["cleanup"])
        self.assertEqual(summary["cleanup"]["errors"], [])
        self.assertFalse(Path(summary["repo"]).exists())

    def test_a_timeout_with_this_runs_marker_kills_the_session_after_reading_the_marker_back(self):
        """new-session succeeded in tmux but the client hung: indeterminate. The marker is this run's, so the session is killed; the marker
        is read back before the kill."""
        rc, summary, err, calls = self.run_with(G3_SHIM_AFTER="new-session")
        self.assertEqual(rc, 1)
        self.assertIn("did not finish within 2 s", summary["error"])
        self.assertTrue(summary["made"]["session"], "settled as the run's own by its marker")
        reads, kills = self.index(calls, "show-options", "@g3_run"), self.index(calls, "kill-session")
        self.assertEqual(len(kills), 1, calls)
        self.assertTrue(reads and reads[0] < kills[0], "the marker was read before the kill")
        self.assertEqual(calls[kills[0]][-1], f"={summary['run']}")
        self.assert_clean(summary)

    def test_a_timeout_with_another_marker_keeps_the_session_and_reports_it(self):
        """A rival's session of the same name carries another marker and the run's own call hung: the session is kept, with its pane, and
        reported by name and marker."""
        rc, summary, err, calls = self.run_with(G3_SHIM_RIVAL="new-session", G3_SHIM_HANG="new-session")
        self.assertEqual(rc, 1)
        self.assertIn("did not finish within 2 s", summary["error"])
        self.assertEqual(summary["made"]["session"], False)
        self.assertEqual(self.index(calls, "kill-session"), [], calls)
        self.assertTrue(self.index(calls, "show-options", "@g3_run"), "the marker was read")
        self.assertTrue(any("carries marker rival" in line and summary["run"] in line for line in summary["cleanup"]["kept"]), summary["cleanup"])
        self.assertTrue(any(f"in session {summary['run']}" in line for line in summary["cleanup"]["leftovers"]), summary["cleanup"])
        self.assertEqual(summary["cleanup"]["errors"], [])

    def test_a_timeout_where_no_session_appeared_kills_nothing(self):
        rc, summary, err, calls = self.run_with(G3_SHIM_HANG="new-session")
        self.assertEqual(rc, 1)
        self.assertEqual(summary["made"]["session"], False)
        self.assertEqual(self.index(calls, "kill-session"), [], calls)
        self.assertTrue(self.index(calls, "has-session"))
        self.assert_clean(summary)

    def test_the_session_is_marked_in_the_call_that_creates_it(self):
        rc, summary, err, _ = results()["flow"]          # the passing flow, started with the module's other full runs
        self.assertEqual(rc, 0, err)
        calls = [c["argv"] for c in records() if c["TMUX_TMPDIR"] == summary["tmux_tmpdir"]]
        creating = [argv for argv in calls if "new-session" in argv and f"{summary['run']}" in argv]
        self.assertEqual(len(creating), 1)
        self.assertEqual(creating[0][creating[0].index(";") + 1:], ["set-option", "-t", f"={summary['run']}:", "@g3_run", summary["run_id"]])
        self.assertTrue(summary["made"]["session"])
        self.assert_clean(summary)


class ConfiguredNamesTest(Base):
    """Round 4 F1: the integration branch, the lane branch prefix and the worktree pattern are the repository's own lane config, not constants of
    the script. The integration branch is the one shared name: a second run refuses at setup (it cannot carry a run id, the lane tools read it
    from the config), and nothing is removed."""

    def refs(self, label):
        return [line.split() for line in CONFIG_REPO[label]["log"].read_text().splitlines()]

    def test_a_flow_with_a_configured_integration_branch_prefix_and_worktree_pattern(self):
        rc, summary, err, _ = results()["config-ok"]
        self.assertEqual((rc, err), (0, ""), summary.get("error"))
        repo, run = CONFIG_REPO["ok"]["path"], summary["run"]
        self.assertEqual(summary["names"], {"integration_branch": "g3int/scratch", "lane_branch": f"g3p/{run}",
                                            "lane_worktree": f"{repo}-g3trees-{run}"})
        self.assertEqual(summary["cleanup"], {"ran": True, "errors": [], "leftovers": [], "kept": []})
        branches = [row for row in self.refs("ok") if row[2].startswith("refs/heads/")]
        made = {ref for old, new, ref in branches if set(old) == {"0"}}          # branches that came into being
        deleted = {ref for old, new, ref in branches if set(new) == {"0"}}
        self.assertEqual(made, {"refs/heads/g3int/scratch", f"refs/heads/g3p/{run}"})
        self.assertEqual(deleted, made, "the run removed what it made, and only that")
        self.assertFalse([ref for _, _, ref in self.refs("ok") if "lane/" in ref], "the default names were never created, not even briefly")
        self.assertEqual(git(repo, "branch", "--list").split(), ["*", "main"])
        self.assertFalse(Path(f"{repo}-g3trees-{run}").exists())
        self.assertEqual(git(repo, "worktree", "list").count("\n"), 1)

    def test_a_configured_integration_branch_that_exists_is_refused_by_name_and_survives(self):
        rc, summary, err, _ = results()["config-taken"]
        repo = CONFIG_REPO["taken"]["path"]
        self.assertEqual((rc, summary["failed_step"]), (1, "setup"))
        for piece in ("g3int/scratch", "integration branch", "wait for the other run", "remove the branch yourself"):
            self.assertIn(piece, summary["error"])
        self.assertEqual(summary["made"], {"session": False, "integration": False, "lane": False, "lane_branch": False, "worktree": None})
        self.assertEqual(summary["cleanup"], {"ran": True, "errors": [], "leftovers": [], "kept": []})
        self.assertEqual(git(repo, "rev-parse", "g3int/scratch"), git(repo, "rev-parse", "main"), "the branch is still there, unmoved")
        self.assertEqual(self.refs("taken"), [], "setup refused before it made or removed any ref (and never the default lane/integrate)")

    def test_the_script_writes_no_branch_prefix_or_worktree_path_of_its_own(self):
        code = [line for line in (ROOT / "tools/smoke/g3_e2e.py").read_text().splitlines() if not line.lstrip().startswith("#")]
        for line in code:
            self.assertNotRegex(line, r"lane/integrate|refs/heads/lane/|f\"lane/\{|-wt-", line)
        self.assertNotIn("INTEGRATION", "\n".join(code))


class GuardTest(Base):
    """kill-server is only sent when it provably reaches the run's own private server; no tmux call is made by these tests."""

    def make(self, *extra):
        args = g3.parse(["--out", str(self.out), *extra])
        smoke = g3.Smoke(args)
        self.addCleanup(shutil.rmtree, smoke.tmp, ignore_errors=True)
        if smoke.tmux_tmp:
            self.addCleanup(shutil.rmtree, smoke.tmux_tmp, ignore_errors=True)
        return smoke

    def test_fake_constructor_sets_the_private_socket_and_dir_before_any_call(self):
        smoke = self.make("--fake")
        self.assertEqual(smoke.tmux_cmd[1:], ["-L", smoke.sock])
        self.assertTrue(smoke.sock.startswith("g3-"))
        self.assertEqual(smoke.tmux_tmp.name[:4], "g3t-")
        env = smoke.env()
        self.assertEqual(env["TMUX_TMPDIR"], str(smoke.tmux_tmp))
        self.assertNotEqual(env["TMUX_TMPDIR"], str(support.DECOY))
        self.assertNotIn("TMUX", env)
        self.assertNotIn("TMUX_PANE", env)
        self.assertEqual(smoke.stop_private_server(), None)      # the private server never started: no call
        self.assertEqual(records()[self.mark:], [])

    def test_kill_server_refused_unless_all_three_hold(self):
        for what, breakit in {
            "no -L": lambda s: setattr(s, "tmux_cmd", [s.tmux_bin]),
            "another socket": lambda s: setattr(s, "tmux_cmd", [s.tmux_bin, "-L", "default"]),
            "an -S socket": lambda s: setattr(s, "tmux_cmd", [s.tmux_bin, "-S", "/tmp/x", "-L", s.sock]),
            "no private dir": lambda s: setattr(s, "tmux_tmp", Path(tempfile.gettempdir())),
            "not fake": lambda s: setattr(s, "fake", False),
        }.items():
            with self.subTest(what):
                smoke = self.make("--fake")
                breakit(smoke)
                with self.assertRaises(AssertionError):
                    smoke.stop_private_server()
        self.assertEqual(records()[self.mark:], [])

    def test_live_mode_never_stops_a_server_and_keeps_the_callers_tmux_tmpdir(self):
        smoke = self.make("--repo", str(support.WORK))
        self.assertFalse(smoke.fake)
        self.assertIsNone(smoke.tmux_tmp)
        self.assertEqual(smoke.tmux_cmd, [smoke.tmux_bin])
        self.assertEqual(smoke.env()["TMUX_TMPDIR"], str(support.DECOY))      # the caller's server, as given; TMUX/TMUX_PANE are dropped
        self.assertNotIn("TMUX", smoke.env())
        with self.assertRaises(AssertionError):
            smoke.stop_private_server()
        self.assertEqual(records()[self.mark:], [])

    def test_live_cleanup_kills_only_the_recorded_panes_and_its_own_session(self):
        smoke = self.make("--repo", str(support.WORK))
        smoke.made["session"] = True
        for task, pane in (("coder-x", "%7"), ("coder-y", "-a; kill-server"), ("coder-z", "%8")):
            (smoke.state / "tasks" / task).mkdir()
            (smoke.state / "tasks" / task / "meta.json").write_text(json.dumps({"pane": pane}))
        calls = []

        def quiet(self, argv, **kw):
            calls.append([str(a) for a in argv])
            listing = f"%7\t{smoke.name}\n%8\tsomeone-elses-session\n" if "list-panes" in argv else ""
            return mock.Mock(returncode=0, stdout=listing, stderr="")
        with mock.patch.object(g3.Smoke, "quiet", quiet):
            smoke.cleanup()
        tmux = [c[1:] for c in calls if c[0] == smoke.tmux_bin]
        self.assertIn(["kill-pane", "-t", "%7"], tmux)
        self.assertIn(["kill-session", "-t", f"={smoke.name}"], tmux)
        self.assertFalse([c for c in tmux if "kill-server" in c or "-a; kill-server" in c], tmux)
        # a pane id that is not %N is ignored, and %8 (recorded, but in another session) is not this run's to kill
        self.assertEqual([c for c in tmux if c[0] == "kill-pane"], [["kill-pane", "-t", "%7"]])

    def test_not_a_live_session_when_the_session_was_never_made(self):
        smoke = self.make("--repo", str(support.WORK))
        calls = []
        with mock.patch.object(g3.Smoke, "quiet", lambda self, argv, **kw: calls.append([str(a) for a in argv]) or mock.Mock(returncode=0, stdout="", stderr="")):
            smoke.cleanup()
        self.assertFalse([c for c in calls if "kill-session" in c or "kill-pane" in c], calls)

    def test_cleanup_removes_only_what_the_run_recorded(self):
        """The git half of the cleanup is driven by `made`: with nothing recorded it deletes no branch, removes no lane and no worktree,
        whatever the names (the F2 rule), and with each record it does exactly that one thing."""
        smoke = self.make("--repo", str(support.WORK))
        smoke.facts["common"] = str(support.WORK / "common")
        smoke.load_config()
        (support.WORK / "common" / "tmux-pi" / "lanes" / smoke.name).mkdir(parents=True)
        self.addCleanup(shutil.rmtree, support.WORK / "common", ignore_errors=True)
        calls = []
        with mock.patch.object(g3.Smoke, "quiet", lambda self, argv, **kw: calls.append([str(a) for a in argv]) or mock.Mock(returncode=0, stdout="", stderr="")):
            smoke.cleanup()
            self.assertFalse([c for c in calls if c[0] == "git" and ("branch" in c or "worktree" in c) or "abandon" in " ".join(c) or "--abandon" in c], calls)
            calls.clear()
            smoke.made.update(lane=True, lane_branch=True)
            smoke.cleanup()
        flat = [" ".join(c) for c in calls]
        self.assertTrue(any(c.endswith(f"branch -D lane/{smoke.name}") for c in flat), flat)
        self.assertTrue(any("lane remove" in c and "--abandon" in c for c in flat), flat)
        self.assertFalse(any(c.endswith(f"branch -D {smoke.integration}") for c in flat), "the integration branch was not recorded")
        self.assertFalse(any("worktree remove" in c for c in flat), "no worktree was recorded")

    def test_run_ids_cannot_collide_and_every_name_is_a_valid_lane_name(self):
        sys.path.insert(0, str(ROOT / "lib"))
        import lanes
        ids = {g3.new_run_id() for _ in range(200)}
        self.assertEqual(len(ids), 200, "200 ids in the same second are all different")
        with mock.patch.object(g3.time, "strftime", return_value="11091200"), mock.patch.object(g3.os, "getpid", return_value=4242):
            same = {g3.new_run_id() for _ in range(200)}
        self.assertEqual(len(same), 200, "the random part alone separates two runs with the same time and pid")
        smokes = [self.make("--fake") for _ in range(5)]
        self.assertEqual(len({s.name for s in smokes}), 5)
        for smoke in smokes:
            self.assertTrue(lanes.valid_lane_name(smoke.name), smoke.name)
            self.assertIn(g3.base36(os.getpid()), smoke.run_id.split("-"))
            self.assertTrue(smoke.name.endswith(smoke.run_id) and smoke.sock.endswith(smoke.run_id.rsplit("-", 1)[-1]))

    def test_the_audit_compares_with_each_runs_own_socket_and_dir(self):
        """The module audit is not a prefix check: a g3-* socket and a g3t-* dir of ANOTHER run fail it."""
        a = {"run": "a", "tmux_tmpdir": "/tmp/g3t-a", "socket": "g3-1-a"}
        b = {"run": "b", "tmux_tmpdir": "/tmp/g3t-b", "socket": "g3-2-b"}

        def call(tmpdir, *argv, tmux="", pane=""):
            return {"argv": list(argv), "TMUX_TMPDIR": tmpdir, "TMUX": tmux, "TMUX_PANE": pane}
        self.assertEqual(support.audit([call("/tmp/g3t-a", "-L", "g3-1-a", "kill-server"), call("/tmp/g3t-b", "-L", "g3-2-b", "list-panes")], [a, b]), {"/tmp/g3t-a": 1})
        for bad in (call("/tmp/g3t-a", "-L", "g3-2-b", "kill-server"),          # another run's socket, with a g3- name
                    call("/tmp/g3t-c", "-L", "g3-1-a", "kill-server"),          # a dir no run owns, with a g3t- name
                    call("/tmp/g3t-a", "list-panes"),                           # no -L
                    call("/tmp/g3t-a", "-L", "g3-1-a", "list-panes", tmux="x"),
                    call(str(support.DECOY), "-L", "g3-1-a", "list-panes")):
            with self.subTest(bad=bad), self.assertRaises(AssertionError):
                support.audit([bad], [a, b])
        with self.assertRaises(AssertionError):                                 # the same server stopped twice
            support.audit([call("/tmp/g3t-a", "-L", "g3-1-a", "kill-server")] * 2, [a, b])

    def test_arguments(self):
        for argv in (["--out", "x"],):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                g3.parse(argv)


if __name__ == "__main__":
    unittest.main()
