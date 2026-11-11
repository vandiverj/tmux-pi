"""tp-spawn --worker-env (trial lane L3, design §3/§10): allowlisted KEY=VAL pairs are exported by run.sh, nothing else is; a spawn
without the option writes the same run.sh as before; --other-lane-paths works without --lane; --state-dir/--parent reach the
preamble of a task with a parent. Real tp-spawn, fake tmux, no model.

Every successful spawn is slow, so the tests that only read what a spawn wrote share it: SharedSpawnTests makes its spawns once in
setUpClass. Tests that need a fresh state dir (refusals leave no task) or different tree/manifest state make their own."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import router  # noqa: E402
GOLDEN = ROOT / "tests" / "fixtures" / "trial_shadow" / "run.sh.golden"
PACKET_GOLDEN = ROOT / "tests" / "fixtures" / "trial_shadow" / "packet_context.golden"


def build_fixture(target, cleanup):
    """State dir, project, fake tmux and spawner env on `target` (a test case or a test class)."""
    temp = tempfile.TemporaryDirectory()
    cleanup(temp.cleanup)
    target.base = Path(temp.name).resolve()
    target.state = target.base / ".tmux-pi"
    (target.state / "tasks").mkdir(parents=True)
    target.project = target.base / "project"
    target.project.mkdir()
    fake = target.base / "fake-bin"
    fake.mkdir()
    tmux = fake / "tmux"
    tmux.write_text('''#!/bin/sh
case "$1" in
 has-session) exit 0 ;;
 show-options)
   case "$*" in
     *'@tp_state_dir'*) printf '%s\\n' "$TP_STATE_DIR" ;;
     *'@tp_main_pane'*) printf '%s\\n' "$TP_MAIN_PANE" ;;
   esac ;;
 display-message) printf '%s\\n' "$TP_MAIN_CWD" ;;
 new-window) printf '%%fake\\n' ;;
esac
''')
    tmux.chmod(0o755)
    target.env = {**os.environ, "TP_STATE_DIR": str(target.state), "TP_SESSION": "fixture", "TP_MAIN_PANE": "%main",
                  "TP_MAIN_CWD": str(target.project), "PATH": str(fake) + os.pathsep + os.environ["PATH"]}
    for key in ("TMUX", "TMUX_PANE", "TP_TASK_ID", "TP_TASK_DIR", "TP_CTX_TOOLS"):
        target.env.pop(key, None)


class SpawnMixin:
    def spawn(self, *args, task="env-1", worker="claude", env=None):
        return subprocess.run([str(ROOT / "bin" / "tp-spawn"), "--worker", worker, "--task-id", task, *args, "Do a thing"],
                              env=env or self.env, text=True, capture_output=True, timeout=60)

    def task(self, task="env-1"):
        return self.state / "tasks" / task

    def run_sh(self, task="env-1"):
        return (self.task(task) / "run.sh").read_text()


class WorkerEnvCase(SpawnMixin, unittest.TestCase):
    def setUp(self):
        build_fixture(self, self.addCleanup)


class SharedSpawnTests(SpawnMixin, unittest.TestCase):
    """Spawns made once; each test reads the run.sh / packet of the spawn whose settings it is about."""
    SECTION = "## Paths other lanes own"
    LANE_BODY = "- lane beta (lane/beta) owns \"lib/x.py\"\nDo not edit these paths."
    CTX = (("1", True), ("0", False), ("true", False), ("", False))

    @classmethod
    def setUpClass(cls):
        build_fixture(cls, cls.addClassCleanup)
        cls.nasty = "a b'c\"d $(touch {m}) `touch {m}` ; touch {m} \\ *".format(m=cls.base / "ran")
        jobs = {
            "listed": (("--worker-env", "TP_TRIAL_ID=sh-a-1", "--worker-env", "TP_DOCS_TOOLS=1", "--worker-env", "TP_LMSTUDIO_BUDGET=3"), None),
            "nasty": (("--worker-env", "TP_DOCS_TOOLS=" + cls.nasty), None),
            "equals": (("--worker-env", "TP_TRIAL_ID=a=b", "--worker-env", "TP_DOCS_TOOLS="), None),
            "gold-1": ((), None),
            "lane": (("--other-lane-paths", cls.LANE_BODY), None),
            "facts": (("--lane-facts-file", str(cls.base / "facts.md"), "--worker-env", "LANE_BASE=0123abc", "--note", "n"), None),
            "gold-ctx": (("--context", "ctx.txt", "--note", "a note"), None),
            "main-ok": ((), {**cls.env, "TP_MAIN_ID": "main-a1"}),
            "main-bad": ((), {**cls.env, "TP_MAIN_ID": "a b; touch " + str(cls.base / "ran")}),
            "facts-secret": (("--lane-facts-file", str(cls.base / ".env")), None),
        }
        (cls.base / "facts.md").write_text("## Lane (facts)\nlane: x\n")
        (cls.base / ".env").write_text("API_KEY=hunter2-not-for-workers\n")
        (cls.project / "ctx.txt").write_text("hello\nworld\n")
        for value, _ in cls.CTX:
            jobs["ctx-" + (value or "empty")] = ((), {**cls.env, "TP_CTX_TOOLS": value})
        # independent processes with distinct task ids: start them together, wait for all
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = {task: pool.submit(cls.spawn_in, cls, *args, task=task, env=env) for task, (args, env) in jobs.items()}
            cls.runs = {task: future.result() for task, future in futures.items()}

    @staticmethod
    def spawn_in(cls, *args, task, env=None):
        return SpawnMixin.spawn(cls, *args, task=task, env=env)

    def test_a_listed_key_is_exported_by_run_sh_and_the_task_still_starts(self):
        run = self.runs["listed"]
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "listed")
        text = self.run_sh("listed")
        self.assertIn("export TP_TRIAL_ID=sh-a-1\n", text)
        self.assertIn("export TP_DOCS_TOOLS=1\n", text)
        self.assertLess(text.index("export TP_TASK_ID"), text.index("export TP_TRIAL_ID"))
        self.assertLess(text.index("export TP_TRIAL_ID"), text.index("tp-claude"))  # set before the worker starts
        # Main's LM Studio budget replaces the worker's 0, and the counter tp-spawn wrote lets the guard count in it
        exports = "\n".join(line for line in text.splitlines() if line.startswith("export TP_LMSTUDIO_BUDGET"))
        read = subprocess.run(["bash", "-c", exports + '\nprintf "%s\\n%s" "$TP_LMSTUDIO_BUDGET" "$TP_LMSTUDIO_BUDGET_FILE"'],
                              text=True, capture_output=True, timeout=20)
        budget, counter = read.stdout.split("\n")
        self.assertEqual((budget, counter, Path(counter).read_text()), ("3", str(self.task("listed") / "lmstudio-budget.count"), "0\n"))
        with mock.patch.dict(os.environ, {"TP_LMSTUDIO_BUDGET": budget, "TP_LMSTUDIO_BUDGET_FILE": counter}):
            os.close(router._take_budget())  # one request counted, as local_open does before it sends
        self.assertEqual(Path(counter).read_text(), "1")

    def test_values_are_quoted_so_the_shell_reads_them_back_as_text_and_runs_nothing(self):
        run = self.runs["nasty"]
        self.assertEqual(run.returncode, 0, run.stderr)
        line = next(l for l in self.run_sh("nasty").splitlines() if l.startswith("export TP_DOCS_TOOLS"))
        read = subprocess.run(["bash", "-c", line + '\nprintf %s "$TP_DOCS_TOOLS"'], text=True, capture_output=True, timeout=20)
        self.assertEqual(read.stdout, self.nasty)
        self.assertFalse((self.base / "ran").exists(), "quoting must not run the value")

    def test_the_value_may_be_empty_or_contain_equals(self):
        run = self.runs["equals"]
        self.assertEqual(run.returncode, 0, run.stderr)
        text = self.run_sh("equals")
        self.assertIn("export TP_TRIAL_ID=a=b\n", text)
        self.assertIn("export TP_DOCS_TOOLS=''\n", text)

    def test_without_the_option_run_sh_is_what_it_was(self):
        """Golden (design §10 item 4): the run.sh of a spawn without --worker-env, paths normalised, equals the one written before
        this option existed."""
        run = self.runs["gold-1"]
        self.assertEqual(run.returncode, 0, run.stderr)
        text = self.run_sh("gold-1")
        for value, name in ((str(self.task("gold-1")), "@TASK_DIR@"), (str(self.state), "@STATE@"), (str(self.project), "@WORKDIR@"), (str(ROOT), "@ROOT@")):
            text = text.replace(value, name)
        self.assertEqual(text, GOLDEN.read_text())
        self.assertEqual((self.task("gold-1") / "lmstudio-budget.count").read_text(), "0\n")  # the counter of the exported budget
        self.assertNotIn("TP_TRIAL_ID", text)
        self.assertNotIn("TP_DOCS_TOOLS", text)

    def test_the_dispatching_mains_id_follows_the_spawner_when_valid(self):
        """TP_MAIN_ID in the spawner's environment is exported by run.sh, before the worker starts, so the worker's own dispatches are
        tagged to the same main; a value that is not an id is not exported (no line, nothing run); unset gives the golden run.sh."""
        run = self.runs["main-ok"]
        self.assertEqual(run.returncode, 0, run.stderr)
        text = self.run_sh("main-ok")
        self.assertIn("export TP_MAIN_ID=main-a1\n", text)
        self.assertLess(text.index("export TP_MAIN_ID"), text.index("tp-claude"))
        bad = self.runs["main-bad"]
        self.assertEqual(bad.returncode, 0, bad.stderr)
        self.assertNotIn("TP_MAIN_ID", self.run_sh("main-bad"))
        self.assertFalse((self.base / "ran").exists())

    def test_ctx_tools_follow_the_spawner(self):
        """ctx-enable: main with TP_CTX_TOOLS=1 gives the worker `export TP_CTX_TOOLS=1` before it starts; any other value in main
        gives `unset TP_CTX_TOOLS`, and the effective value follows the spawner even if the worker window inherits a stale 1."""
        for value, exported in self.CTX:
            with self.subTest(value=value):
                task = "ctx-" + (value or "empty")
                run = self.runs[task]
                self.assertEqual(run.returncode, 0, run.stderr)
                text = self.run_sh(task)
                self.assertEqual("export TP_CTX_TOOLS=1\n" in text, exported)
                self.assertEqual("unset TP_CTX_TOOLS\n" in text, not exported)
                lines = [l for l in text.splitlines() if "TP_CTX_TOOLS" in l]
                read = subprocess.run(["bash", "-c", "\n".join(lines) + '\nprintf %s "${TP_CTX_TOOLS-unset}"'], text=True,
                                      capture_output=True, timeout=20, env={**os.environ, "TP_CTX_TOOLS": "1"})
                self.assertEqual(read.stdout, "1" if exported else "unset")
                if exported:
                    self.assertLess(text.index("export TP_CTX_TOOLS=1"), text.index("tp-claude"))

    def test_a_spawn_without_a_lane_passes_the_section_to_the_packet(self):
        run = self.runs["lane"]
        self.assertEqual(run.returncode, 0, run.stderr)
        packet = (self.task("lane") / "packet.md").read_text()
        self.assertIn(self.SECTION, packet)
        self.assertIn("lib/x.py", packet.split(self.SECTION, 1)[1])

    def test_no_section_without_the_option(self):
        self.assertEqual(self.runs["gold-1"].returncode, 0)
        self.assertNotIn(self.SECTION, (self.task("gold-1") / "packet.md").read_text())
        self.assertNotIn("## Lane", (self.task("gold-1") / "packet.md").read_text())

    def test_lane_facts_file_goes_right_after_the_working_directory_and_lane_base_is_allowed(self):
        run = self.runs["facts"]
        self.assertEqual(run.returncode, 0, run.stderr)
        packet = (self.task("facts") / "packet.md").read_text()
        self.assertRegex(packet, r"Working directory: [^\n]*\.\n\n## Lane \(facts\)\nlane: x\n")
        self.assertIn("export LANE_BASE=0123abc\n", self.run_sh("facts"))
        self.assertLess(packet.index("## Lane"), packet.index("## Notes"))

    def test_a_sensitive_lane_facts_file_is_screened_like_a_context_file(self):
        run = self.runs["facts-secret"]
        self.assertEqual(run.returncode, 0, run.stderr)
        packet = (self.task("facts-secret") / "packet.md").read_text()
        self.assertNotIn("hunter2", packet)
        self.assertIn("Sensitive context ", packet)

    def test_a_spawn_without_a_lane_writes_the_context_section_it_always_wrote(self):
        """Byte-identical (the golden was rendered by the tp-spawn before --lane-facts-file existed): context, note, no lane code path."""
        run = self.runs["gold-ctx"]
        self.assertEqual(run.returncode, 0, run.stderr)
        packet = (self.task("gold-ctx") / "packet.md").read_text()
        for value, name in ((str(self.task("gold-ctx")), "@TASK_DIR@"), (str(self.state), "@STATE@"), (str(self.project), "@WORKDIR@")):
            packet = packet.replace(value, name)
        self.assertEqual(packet[packet.index("# Context"):], PACKET_GOLDEN.read_text())


class WorkerEnvRefusalTests(WorkerEnvCase):
    """Refusals exit before anything is created; each test starts from an empty state dir."""

    def test_an_unlisted_key_is_refused_and_nothing_is_created(self):
        for key in ("PATH", "LD_PRELOAD", "TP_STATE_DIR", "TP_TASK_ID", "tp_trial_id", "TP_TRIAL_ID_X", "TP_CTX_TOOLS", ""):
            with self.subTest(key=key):
                run = self.spawn("--worker-env", key + "=x")
                self.assertNotEqual(run.returncode, 0)
                self.assertIn("not allowed", run.stderr)
                self.assertFalse(list((self.state / "tasks").iterdir()), "a refused spawn leaves no task")

    def test_a_pair_without_equals_a_repeat_and_a_line_break_are_refused(self):
        cases = (("TP_TRIAL_ID",), ("TP_TRIAL_ID=a", "--worker-env", "TP_TRIAL_ID=b"), ("TP_TRIAL_ID=a\nexport EVIL=1",))
        for case in cases:
            with self.subTest(case=case):
                args = []
                for value in case:
                    args += ["--worker-env", value] if not value.startswith("--") else [value]
                run = self.spawn(*args)
                self.assertNotEqual(run.returncode, 0)
                self.assertFalse(list((self.state / "tasks").iterdir()))


class PreambleArgsTests(WorkerEnvCase):
    """tp-spawn gives lib/preamble.py --state-dir and --parent for a task with a parent, so the agent-tools block can name the
    prior task's result and review report (L3 integration item 1). preamble.py on this tree takes both options, so there is no
    check of its text before passing them."""

    @classmethod
    def setUpClass(cls):
        """One copy of the tree whose preamble.py only records its argv (its source says nothing about any option); the argv goes
        to the file named by PREAMBLE_PROBE in the spawner env."""
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        cls.copy = Path(temp.name).resolve() / "tree"
        cls.copy.mkdir()
        for name in ("agent", "bin", "catalog", "claude", "config", "lib", "skills", "templates"):
            if (ROOT / name).exists():
                subprocess.run(["cp", "-R", str(ROOT / name), str(cls.copy / name)], check=True, capture_output=True)
        (cls.copy / "lib" / "preamble.py").write_text(
            "import os, sys\nif __name__ == '__main__':  # lib/launch_record.py imports this module too (HB2b); only the run as a script is probed\n"
            "    open(os.environ['PREAMBLE_PROBE'], 'w').write(' '.join(sys.argv[1:]))\n    print('PREAMBLE')\n")

    def spawn_probed(self, task, *args):
        probe = self.base / ("preamble-args-" + task)
        run = subprocess.run([str(self.copy / "bin" / "tp-spawn"), "--worker", "claude", "--task-id", task, *args, "Do"],
                             env={**self.env, "PREAMBLE_PROBE": str(probe)}, text=True, capture_output=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        return probe.read_text()

    def tools_manifest(self, tools):
        folder = self.project / ".tmux-pi"
        folder.mkdir(exist_ok=True)
        (folder / "agent-tools.json").write_text(json.dumps({"tools": tools}))

    def test_the_preamble_gets_state_dir_and_parent(self):
        argv = self.spawn_probed("pa-1", "--parent", "parent-1")
        self.assertIn("--harness claude --state-dir " + str(self.state), argv)
        self.assertIn("--parent parent-1", argv)

    def test_a_task_without_a_parent_passes_neither(self):
        argv = self.spawn_probed("pa-2")
        self.assertTrue(argv.endswith(" --harness claude"), argv)   # the worker contract is rendered for the worker's harness
        self.assertNotIn("--state-dir", argv)
        self.assertNotIn("--parent", argv)

    def test_the_prior_task_line_reaches_the_worker_system_prompt(self):
        """Real preamble.py and agent_tools.py: a parent that has a result and a review report names both in the system prompt a
        copy of which tp-spawn keeps, with no tools manifest (before the worker contract the line needed one)."""
        parent, review = self.task("parent-1"), self.task("review-1")
        for folder in (parent, review):
            folder.mkdir(parents=True)
        (review / "report.md").write_text("report")
        (parent / "result.md").write_text("done")
        (parent / "meta.json").write_text(json.dumps({"review": "review-1"}))
        run = self.spawn("--parent", "parent-1", "--role", "coder", task="pa-3")
        self.assertEqual(run.returncode, 0, run.stderr)
        prompt = (self.task("pa-3") / "system-prompt.md").read_text()
        self.assertIn("Prior result: " + str(parent / "result.md"), prompt)
        self.assertIn("review report: " + str(review / "report.md"), prompt)

    def test_no_prior_line_without_a_parent(self):
        self.tools_manifest([])
        run = self.spawn("--role", "coder", task="pa-4")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotIn("Prior result", (self.task("pa-4") / "system-prompt.md").read_text())


if __name__ == "__main__":
    unittest.main()
