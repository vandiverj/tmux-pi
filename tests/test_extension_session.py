"""Pi main-agent extensions act on the session of their own pane and on TP_STATE_DIR, never a decoy."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests/extension_session_runtime.mjs"
REAL = "tmux-pi-0123456789ab-cdef01234567"

# Two sessions: the agent's own (%main) and a legacy 'tmux-pi' decoy. Like tmux when it cannot
# place the caller, a display-message without -t answers for the most recently used session
# (the decoy), and an unknown pane prints an empty line.
TMUX = '''#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_LOG"
case "$1" in
 show-options)
   case "$4$5" in
     "=tmux-pi:@tp_state_dir") printf '%s\\n' "$FAKE_DECOY_STATE" ;;
     "=$FAKE_REAL:@tp_state_dir") printf '%s\\n' "$FAKE_REAL_STATE" ;;
   esac ;;
 display-message)
   if [ "$3" = -t ]; then pane="$4"; format="$5"; else pane=%decoy; format="$3"; fi
   case "$pane$format" in
     '%main#S') printf '%s\\n' "$FAKE_REAL" ;;
     '%decoy#S'|'%decoy#{session_name}') printf 'tmux-pi\\n' ;;
     '%main#{session_name}') printf '%s\\n' "$FAKE_REAL" ;;
     '%main#{@tp_state_dir}') printf '%s\\n' "$FAKE_REAL_STATE" ;;
     '%decoy#{@tp_state_dir}') printf '%s\\n' "$FAKE_DECOY_STATE" ;;
     *) printf '\\n' ;;
   esac ;;
 list-panes) printf '%%7 0\\n%%9 0\\n' ;;
esac
'''


@unittest.skipUnless(shutil.which("node"), "node is required for the extension runtime harness")
class ExtensionSessionCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.project = self.base / "project"
        self.state = self.project / ".tmux-pi" / "sessions" / "run"
        self.decoy_state = self.base / "legacy" / ".tmux-pi"
        for path in (self.state / "tasks", self.decoy_state / "tasks"):
            path.mkdir(parents=True)
        fake = self.base / "fake-bin"
        fake.mkdir()
        (fake / "tmux").write_text(TMUX)
        (fake / "tmux").chmod(0o755)
        self.log = self.base / "tmux.log"
        self.log.write_text("")
        self.env = {**os.environ, "TMUX": "/tmp/fake,1,0", "TMUX_PANE": "%main", "TP_STATE_DIR": str(self.state),
                    "FAKE_LOG": str(self.log), "FAKE_REAL": REAL, "FAKE_REAL_STATE": str(self.state),
                    "FAKE_DECOY_STATE": str(self.decoy_state), "PATH": str(fake) + os.pathsep + os.environ["PATH"]}
        for name in ("TP_SESSION", "TP_TASK_DIR", "TP_TASK_ID", "TP_MODE"):
            self.env.pop(name, None)

    def node(self, scenario, **updates):
        env = {**self.env, **updates}
        for name in [key for key, value in env.items() if value is None]:
            del env[name]
        run = subprocess.run(["node", "--experimental-strip-types", str(HARNESS), scenario, str(self.project)],
                             cwd=ROOT, env=env, text=True, capture_output=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        return json.loads(run.stdout)

    def tmux_calls(self):
        return self.log.read_text().splitlines()


class GoalSessionTests(ExtensionSessionCase):
    def setUp(self):
        super().setUp()
        spec = {"id": "repair", "objective": "Repair all parser behaviors and regression tests",
                "done": "All original parser requirements verified", "scope": "Only this project parser and tests",
                "workdir": str(self.project),
                "budget": {"seconds": 3600, "turns": 10, "tokens": 10000, "cost_micros": 100000, "attempts": 6}}
        (self.base / "spec.json").write_text(json.dumps(spec))
        planned = json.loads(self.goal("plan", "--spec", str(self.base / "spec.json")))
        self.goal("start", "repair", "--confirm", "START-GOAL", "--approve-sha256", planned["spec_sha256"])

    def goal(self, *args):
        # A human shell outside the workspace: no session variables at all.
        env = {key: value for key, value in self.env.items() if key not in ("TMUX", "TMUX_PANE", "TP_STATE_DIR")}
        run = subprocess.run([str(ROOT / "bin/tp-goal"), "--project", str(self.project), *args], env=env, text=True,
                             capture_output=True, timeout=20)
        self.assertEqual(run.returncode, 0, run.stderr)
        return run.stdout

    def attempts(self):
        return json.loads((self.project / ".tmux-pi" / "goals" / "repair.json").read_text())["attempts"]

    def assert_continued(self, result):
        self.assertEqual(len(result["messages"]), 1, result)
        self.assertTrue(result["messages"][0].startswith("TP_GOAL_CONTINUATION repair "))
        self.assertEqual(self.attempts(), 1)

    def assert_refused(self, result, reason):
        self.assertEqual(result["messages"], [])
        self.assertIn(reason, " ".join(result["notices"]))
        self.assertEqual(self.attempts(), 0)

    def test_run_state_dir_of_own_pane_continues_despite_decoy_session(self):
        self.assert_continued(self.node("goal"))
        calls = self.tmux_calls()
        self.assertIn("display-message -p -t %main #S", calls)
        self.assertFalse([line for line in calls if "=tmux-pi:" in line], calls)

    def test_pane_session_is_used_without_state_dir_variable(self):
        self.assert_continued(self.node("goal", TP_STATE_DIR=None, TP_SESSION="tmux-pi"))

    def test_legacy_project_state_root_still_continues(self):
        legacy = self.project / ".tmux-pi"
        self.assert_continued(self.node("goal", TP_STATE_DIR=str(legacy), FAKE_REAL_STATE=str(legacy)))

    def test_pane_in_other_session_is_refused(self):
        self.assert_refused(self.node("goal", TMUX_PANE="%decoy"), "not TP_STATE_DIR")
        self.assert_refused(self.node("goal", TMUX_PANE="%decoy", TP_STATE_DIR=None), "not the approved project-local state root")

    def test_unplaceable_pane_and_missing_tmux_are_refused(self):
        self.assert_refused(self.node("goal", TMUX_PANE="%gone"), "no workspace guess")
        self.assert_refused(self.node("goal", TMUX=None), "requires an identified tmux-pi session")


class PaneTargetingTests(ExtensionSessionCase):
    """The same task id exists in both workspaces; only the pane recorded in ours may be typed into."""

    def setUp(self):
        super().setUp()
        for state, pane in ((self.state, "%7"), (self.decoy_state, "%9")):
            task = state / "tasks" / "probe"
            task.mkdir()
            (task / "meta.json").write_text(json.dumps({"id": "probe", "worker": "pi", "pane": pane}))
            (task / "state.json").write_text(json.dumps({"state": "waiting"}))
        # The extensions find bin/ beside the agent dir; a stand-in keeps real task tooling out of it.
        root = self.base / "root"
        (root / "agent").mkdir(parents=True)
        (root / "bin").mkdir()
        (root / "bin" / "tp-task").write_text('#!/bin/sh\nprintf \'{"blockers": [], "references": []}\\n\'\n')
        (root / "bin" / "tp-task").chmod(0o755)
        self.env.update(PI_CODING_AGENT_DIR=str(root / "agent"), TP_MODE="strict")

    def test_collect_result_says_an_incomplete_review_is_not_an_approval(self):
        (self.base / "root" / "lib").symlink_to(ROOT / "lib", target_is_directory=True)
        for ident, reply in (("rv", "APPROVED\nRESULT_STATUS: complete\nfine"),
                             ("rv-ok", "APPROVED\nRESULT_STATUS: complete\nCoverage\nreviewed a.py and checked its callers"),
                             ("plain", "APPROVED\nRESULT_STATUS: complete\nfine")):
            task = self.state / "tasks" / ident
            task.mkdir()
            (task / "meta.json").write_text(json.dumps({"id": ident, "worker": "pi", "pane": "%7"}))
            (task / "state.json").write_text(json.dumps({"state": "waiting"}))
            (task / "result.md").write_text(reply)
            if ident != "plain":
                (task / "review-of").write_text("author")
        run = subprocess.run(["node", "--experimental-strip-types", str(HARNESS), "subagents-collect", str(self.project), "rv", "rv-ok", "plain"],
                             cwd=ROOT, env=self.env, text=True, capture_output=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        incomplete, complete, plain = json.loads(run.stdout)["notices"]
        self.assertIn("NOT AN APPROVAL: this review is incomplete (the review has no Coverage section)", incomplete)
        self.assertNotIn("NOT AN APPROVAL", complete)
        self.assertNotIn("NOT AN APPROVAL", plain)

    def test_collect_result_says_a_request_changes_with_no_code_finding_is_no_rejection(self):
        (self.base / "root" / "lib").symlink_to(ROOT / "lib", target_is_directory=True)
        coverage = "\nCoverage\nreviewed a.py and checked its callers"
        for ident, reply in (("rv-check", "REQUEST_CHANGES\nRESULT_STATUS: complete\nF1 [note] no count. Closes by: check" + coverage),
                             ("rv-code", "REQUEST_CHANGES\nRESULT_STATUS: complete\nF1 [defect] wrong. Closes by: code" + coverage)):
            task = self.state / "tasks" / ident
            task.mkdir()
            (task / "meta.json").write_text(json.dumps({"id": ident, "worker": "pi", "pane": "%7"}))
            (task / "state.json").write_text(json.dumps({"state": "waiting"}))
            (task / "result.md").write_text(reply)
            (task / "review-of").write_text("author")
        run = subprocess.run(["node", "--experimental-strip-types", str(HARNESS), "subagents-collect", str(self.project), "rv-check", "rv-code"],
                             cwd=ROOT, env=self.env, text=True, capture_output=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        unusable, rejecting = json.loads(run.stdout)["notices"]
        for words in ("NOT A REJECTION AND NOT AN APPROVAL", "Closes by: code", "tp-agent review author"):
            self.assertIn(words, unusable)
        self.assertNotIn("NOT A REJECTION", rejecting)

    def assert_typed_into_own_task(self, scenario, **updates):
        self.log.write_text("")
        result = self.node(scenario, **updates)
        keys = [line for line in self.tmux_calls() if line.startswith("send-keys")]
        self.assertEqual([line.split()[2] for line in keys], ["%7", "%7"], (result, self.tmux_calls()))
        return [line for line in self.tmux_calls() if line.startswith("display-message")]

    def test_session_options_are_read_from_own_pane(self):
        for scenario in ("dispatcher", "subagents"):
            with self.subTest(scenario=scenario):
                lookups = self.assert_typed_into_own_task(scenario, TP_STATE_DIR=None)
                self.assertTrue(lookups)
                self.assertEqual([line for line in lookups if not line.startswith("display-message -p -t %main ")], [])

    def assert_refused(self, scenario, reason, **updates):
        self.log.write_text("")
        result = self.node(scenario, **updates)
        self.assertIn(reason, " ".join(result["notices"]))
        self.assertEqual([line for line in self.tmux_calls() if line.startswith("send-keys")], [])
        return " ".join(result["notices"])

    def test_pane_in_other_session_is_refused(self):
        # The same rule and wording as lib/tp.sh tp_session(): the state dir and the main pane
        # must not come from two workspaces.
        for scenario in ("dispatcher", "subagents"):
            with self.subTest(scenario=scenario):
                said = self.assert_refused(scenario, "not TP_STATE_DIR", TMUX_PANE="%decoy")
                self.assertIn(f"session 'tmux-pi' keeps its state in '{self.decoy_state}', "
                              f"not TP_STATE_DIR '{self.state}'; refusing to act on another workspace", said)

    def test_state_dir_variable_naming_own_state_through_a_symlink_is_accepted(self):
        link = self.base / "state-link"
        link.symlink_to(self.state, target_is_directory=True)
        for scenario in ("dispatcher", "subagents"):
            with self.subTest(scenario=scenario):
                self.assert_typed_into_own_task(scenario, TP_STATE_DIR=str(link))
                self.assert_typed_into_own_task(scenario, TP_STATE_DIR=str(self.state) + "/")

    def test_unplaceable_pane_never_guesses_a_workspace(self):
        for scenario in ("dispatcher", "subagents"):
            with self.subTest(scenario=scenario):
                self.assert_refused(scenario, "no workspace guess", TP_STATE_DIR=None, TMUX_PANE="%gone")
                self.assert_refused(scenario, "no workspace guess", TMUX_PANE="%gone")
                # Without TMUX_PANE tmux answers for the most recently used session, here the decoy.
                self.assert_refused(scenario, "not TP_STATE_DIR", TMUX_PANE=None)


if __name__ == "__main__":
    unittest.main()
