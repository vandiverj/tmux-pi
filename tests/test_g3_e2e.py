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


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=True, env={**os.environ, **GIT_ENV}, timeout=60).stdout


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
            self.assertEqual(stopped.get(summary["tmux_tmpdir"]), 1, f"{summary['run']}: each run stopped its own server once")


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

    def test_the_trigger_names_nothing_that_exists(self):
        self.assertFalse((self.repo / g3.ABSENT).exists())
        self.assertNotIn(g3.SECRET_VAR, os.environ)
        self.assertIn(g3.ABSENT, g3.PROMPT_COMMAND)
        self.assertIn(g3.SECRET_VAR, g3.PROMPT_COMMAND)

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
        self.assertEqual(summary["cleanup"], {"ran": True, "errors": [], "leftovers": []})
        listing = git(F2_REPO["path"], "worktree", "list", "--porcelain")
        self.assertIn(str(F2_REPO["other"]), listing, "the unrelated registration was pruned")
        block = listing[listing.index(f"worktree {F2_REPO['other']}"):].split("\n\n")[0]
        self.assertIn("prunable", block)
        self.assertNotIn("g3-smoke", listing)
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
        self.assertIn("kill session: TimeoutExpired", errors)
        self.assertIn("leftover probe 'tmux list-panes' failed: TimeoutExpired", errors)
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
        git(smoke.repo, "branch", g3.INTEGRATION)
        smoke.made_integration = True
        repo, tmux_tmp = smoke.repo, smoke.tmux_tmp
        with mock.patch.dict(os.environ, {"G3_SHIM_HANG": "kill-pane kill-session list-panes"}):
            smoke.cleanup()
        errors = " | ".join(smoke.cleanup_report["errors"])
        for expected in (f"kill pane {pane}: TimeoutExpired", "kill session: TimeoutExpired", "leftover probe 'tmux list-panes' failed: TimeoutExpired"):
            self.assertIn(expected, errors)
        self.assertNotIn("stop the private tmux server", errors)
        self.assertEqual(git(repo, "branch", "--list", g3.INTEGRATION).strip(), "", "the next cleanup action ran after the timeouts")
        self.assertFalse(tmux_tmp.exists())
        self.assertEqual(len([c for c in records()[self.mark:] if "kill-server" in c["argv"]]), 1)


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
        smoke.session_made = True
        (smoke.state / "tasks" / "coder-x").mkdir()
        (smoke.state / "tasks" / "coder-x" / "meta.json").write_text(json.dumps({"pane": "%7"}))
        (smoke.state / "tasks" / "coder-y").mkdir()
        (smoke.state / "tasks" / "coder-y" / "meta.json").write_text(json.dumps({"pane": "-a; kill-server"}))
        calls = []
        with mock.patch.object(g3.Smoke, "quiet", lambda self, argv, **kw: calls.append([str(a) for a in argv]) or mock.Mock(returncode=0, stdout="", stderr="")):
            smoke.cleanup()
        tmux = [c[1:] for c in calls if c[0] == smoke.tmux_bin]
        self.assertIn(["kill-pane", "-t", "%7"], tmux)
        self.assertIn(["kill-session", "-t", f"={smoke.name}"], tmux)
        self.assertFalse([c for c in tmux if "kill-server" in c or "-a; kill-server" in c], tmux)
        self.assertEqual([c for c in tmux if c[0] == "kill-pane"], [["kill-pane", "-t", "%7"]])    # a pane id that is not %N is ignored

    def test_not_a_live_session_when_the_session_was_never_made(self):
        smoke = self.make("--repo", str(support.WORK))
        calls = []
        with mock.patch.object(g3.Smoke, "quiet", lambda self, argv, **kw: calls.append([str(a) for a in argv]) or mock.Mock(returncode=0, stdout="", stderr="")):
            smoke.cleanup()
        self.assertFalse([c for c in calls if "kill-session" in c], calls)

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
