"""Refusals that tell the truth and name a remedy that works (the live pass's findings F1 to F6, and the audit of every other
refusal that names a command). A message names a command only if running it, in the state the message is printed in, clears
the refusal; each test here prints the refusal, runs what it names and shows the refusal gone. tmux is the private -L server
of tests/lane_fixture.py; no model runs.

  F1  a cross-session `stop` works as printed, typed in another tmux session
  F2  a reviewer that holds a lane is called a reviewer
  F3  a settled, uncollected worker is called settled; `collect` comes first, `stop` second, and waiting is not offered
  F4  a lane retry names collect/stop (not reap, not --force-alive)
  F6  a non-lane retry names collect/stop, reap only for a collected task, --force-alive only where it applies
  F5  a Markdown-decorated RESULT_STATUS line is a status line, through the one shared reader; the paused notice says the form
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from lane_fixture import MODEL, ROOT, TMUX  # noqa: E402
from test_lane_lease import LeaseCase  # noqa: E402
from test_lane_writer import git  # noqa: E402
from test_lane_steps import APPROVED, CHANGES, StepCase  # noqa: E402
import contract  # noqa: E402
import lane_lease  # noqa: E402
import result_status  # noqa: E402
import review_verdict  # noqa: E402
import worker_process  # noqa: E402

def named(text):
    """The tp-agent commands a refusal names, in the order it names them: each is in backticks, so what is pasted is exactly it."""
    return [c for c in re.findall(r"`([^`]+)`", text) if "tp-agent " in c]


class RemedyCase(LeaseCase):
    def shell(self, command, env=None):
        """Run `command` as typed at a prompt: through sh, with THIS worktree's tp-agent first on PATH (never an installed one)."""
        env = dict(env or self.env)
        env["PATH"] = os.pathsep.join([str(ROOT / "bin"), env["PATH"]])
        return subprocess.run(["sh", "-c", command], env=env, text=True, capture_output=True, timeout=90)

    def collect(self, ident):
        run = self.raw("collect", ident)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)


class CrossSessionStopTests(RemedyCase):
    """F1: a stop printed for a holder in another session works as printed, from a pane of any tmux session."""

    def setUp(self):
        super().setUp()
        # the fixture's tmux answers every show-options with its own state directory; the printed command is run through a
        # shim that talks to the real server, where each session keeps its own @tp_state_dir
        self.tmux("set-option", "-t", "anchor", "@tp_state_dir", str(self.state))
        self.tmux("new-session", "-d", "-s", "second", "sleep 300")
        self.tmux("set-option", "-t", "second", "@tp_state_dir", str(self.state2))
        self.second_pane = self.tmux("list-panes", "-t", "second", "-F", "#{pane_id}").splitlines()[0]
        shim = self.root / "real-bin"
        shim.mkdir()
        (shim / "tmux").write_text(f'#!/bin/sh\nexec {TMUX} -L {self.sock} "$@"\n')
        (shim / "tmux").chmod(0o755)
        path = os.pathsep.join([str(shim), str(ROOT / "bin"), os.environ["PATH"]])
        # a shell in the second session: inside tmux, its own pane, its own state directory
        self.typed = {**os.environ, "PATH": path, "TMUX": "/private/tmux,1,0", "TMUX_PANE": self.second_pane,
                      "TP_STATE_DIR": str(self.state2), "TP_SESSION": "second"}

    def live_holder(self):
        """A lane writer in this session whose pane is real, so the lease names a live process (the other session sees it)."""
        (self.state / "live-window").write_text("")
        item, task = self.lane_ok()
        (self.state / "live-window").unlink()
        return item, task, json.loads((task / "meta.json").read_text())["pane"]

    def test_f1_the_printed_stop_works_from_another_tmux_session(self):
        item, task, pane = self.live_holder()
        refused = self.run_in(self.env2, *self.lane_args(step="s1", extra=()))
        self.assertEqual(refused.returncode, 1, refused.stdout)
        commands = [c for c in named(refused.stderr) if " stop " in c]
        self.assertEqual(len(commands), 1, refused.stderr)
        stop = commands[0]
        self.assertTrue(stop.startswith("env -u TMUX -u TMUX_PANE -u TP_SESSION TP_STATE_DIR="), stop)
        # the control: the command without the unset, typed in the second session, is the refusal the live pass met
        plain = self.shell(f"TP_STATE_DIR={self.state} tp-agent stop {item['id']}", self.typed)
        self.assertEqual(plain.returncode, 1)
        self.assertIn("refusing to act on another workspace", plain.stderr)
        self.assertTrue(self.alive(pane))
        # the printed one, as printed
        done = self.shell(stop, self.typed)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(self.alive(pane))
        self.assertTrue((task / "stopped.json").is_file())
        again = self.run_in(self.env2, *self.lane_args(step="s1", extra=()))
        self.assertEqual(again.returncode, 0, again.stderr)

    def test_f1_the_stop_printed_in_the_holders_own_session_is_the_plain_command(self):
        item, task, pane = self.live_holder()
        refused = self.lane_dispatch(step="s1", extra=())
        self.assertEqual(refused.returncode, 1)
        self.assertIn(f"tp-agent stop {item['id']}", refused.stderr)
        self.assertNotIn("env -u TMUX", refused.stderr)

    def test_f1_a_settled_holder_in_another_session_names_collect_with_the_same_prefix(self):
        item, task, pane = self.live_holder()
        self.settle(task)
        refused = self.run_in(self.env2, *self.lane_args(step="s1", extra=()))
        commands = named(refused.stderr)
        self.assertEqual([c.split(" tp-agent ")[1] for c in commands], [f"collect {item['id']}", f"stop {item['id']}"], refused.stderr)
        done = self.shell(commands[0], self.typed)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.run_in(self.env2, *self.lane_args(step="s1", extra=())).returncode, 0)


class ReviewerHoldingTests(RemedyCase):
    """F2: the holder that is a reviewer is called a reviewer, by dispatch's early look and by the reservation alike."""

    def hold_with_a_reviewer(self):
        self.lane_ok()
        reviewer, task = self.lane_ok(role="code-reviewer", step="s1", extra=())
        pane, _ = self.pane(self.worker_command(task))
        self.attach(task, pane)
        return reviewer, task, pane

    def test_f2_dispatch_says_a_reviewer_holds_the_lane(self):
        reviewer, task, pane = self.hold_with_a_reviewer()
        run = self.lane_dispatch(step="s1", extra=())
        self.refused(run, "lane alpha is in use by a reviewer", reviewer["id"], "(reviewer, running")
        self.assertNotIn("already has a writer", run.stderr)
        self.assertNotIn("(writer", run.stderr)
        # it is at work, so waiting is honest; the named stop also works
        self.assertIn("Wait for it to finish", run.stderr)
        stop = [c for c in named(run.stderr) if " stop " in c]
        self.assertEqual(stop, [f"tp-agent stop {reviewer['id']}"])
        self.assertEqual(self.shell(stop[0]).returncode, 0)
        self.assertFalse(self.alive(pane))
        self.lane_ok(step="s1", extra=())

    def test_f2_the_reservation_says_a_reviewer_holds_the_lane_too(self):
        self.lane_ok()
        lane_lease.reserve(self.common(), "alpha", "review-held0001", "code-reviewer", str(self.state), os.getpid())
        with self.assertRaises(ValueError) as caught:
            lane_lease.reserve(self.common(), "alpha", "coder-second001", "coder", str(self.state), os.getppid())
        self.assertIn("lane alpha is in use by a reviewer", str(caught.exception))
        self.assertNotIn("already has a writer", str(caught.exception))
        self.assertIn("review-held0001 (reviewer, starting", str(caught.exception))

    def test_f2_a_writer_is_still_called_a_writer(self):
        item, task = self.lane_ok()
        pane, _ = self.pane(self.worker_command(task))
        self.attach(task, pane)
        run = self.lane_dispatch(step="s1", extra=())
        self.refused(run, "lane alpha already has a writer", f"{item['id']} (writer, running")
        self.assertNotIn("in use by a reviewer", run.stderr)


class SettledHolderTests(RemedyCase):
    """F3: a worker that has settled is not running, waiting never frees the lane, and collect (then stop) does."""

    def settled_holder(self, **kw):
        (self.state / "live-window").write_text("")  # the worker's pane is real and the lease names it, as a started worker's does
        item, task = self.lane_ok(**kw)
        (self.state / "live-window").unlink()
        self.settle(task)
        return item, task

    def test_f3_a_settled_worker_is_called_settled_and_collect_comes_before_stop(self):
        item, task = self.settled_holder()
        self.assertEqual([h["task"] for h in self.lease()], [item["id"]])
        run = self.lane_dispatch(step="s1", extra=())
        self.refused(run, "lane alpha already has a writer", f"{item['id']} (writer, settled: finished, not collected")
        self.assertNotIn("running", run.stderr)
        self.assertNotIn("Wait for it", run.stderr)
        self.assertIn("waiting never frees the lane", run.stderr)
        self.assertEqual(named(run.stderr), [f"tp-agent collect {item['id']}", f"tp-agent stop {item['id']}"])
        self.assertNotIn("tp-agent reap", run.stderr)

    def test_f3_collect_as_named_frees_the_lane(self):
        item, task = self.settled_holder()
        run = self.lane_dispatch(step="s1", extra=())
        done = self.shell(named(run.stderr)[0])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["status"], "completed")
        self.lane_ok(step="s1", extra=())

    def test_f3_stop_as_named_frees_the_lane_too(self):
        item, task = self.settled_holder()
        run = self.lane_dispatch(step="s1", extra=())
        done = self.shell(named(run.stderr)[1])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.lane_ok(step="s1", extra=())

    def test_f3_a_worker_still_at_work_is_running_and_may_be_waited_for(self):
        (self.state / "live-window").write_text("")
        item, task = self.lane_ok()
        (self.state / "live-window").unlink()
        (task / "state.json").write_text(json.dumps({"state": "working"}))
        run = self.lane_dispatch(step="s1", extra=())
        self.refused(run, f"{item['id']} (writer, running", "Wait for it to finish")
        self.assertEqual(named(run.stderr), [f"tp-agent stop {item['id']}"])

    def test_f3_a_queued_item_waits_for_a_settled_writer_and_says_what_ends_it(self):
        item, task = self.settled_holder()
        queued = {"id": "coder-queued0001", "created": "2026-10-08T00:00:00Z", "role": "coder", "task": "t", "workdir": str(self.worktree),
                  "worker": "pi", "model": MODEL, "thinking": "medium", "route_info": {}, "deliverable": "", "parent": "", "after": [],
                  "context": [], "notes": [], "tests": "none", "lane": "alpha", "lane_step": "s1", "preamble_from": str(self.repo),
                  "queue_reason": ""}  # an entry that carries a reason is told why it waits on every tick
        (self.state / "queue").mkdir(exist_ok=True)
        (self.state / "queue/coder-queued0001.json").write_text(json.dumps(queued))
        self.cli("tick")
        reason = json.loads((self.state / "queue/coder-queued0001.json").read_text())["queue_reason"]
        self.assertIn(f"waiting for lane alpha's writer {item['id']}", reason)
        self.assertIn("will not finish by itself", reason)
        self.assertEqual(named(reason), [f"tp-agent collect {item['id']}", f"tp-agent stop {item['id']}"])
        self.assertEqual(self.shell(named(reason)[0]).returncode, 0)
        self.cli("tick")
        self.assertTrue((self.state / "tasks/coder-queued0001").is_dir())


class RetryRefusalTests(RemedyCase):
    """F4 and F6: the retry guard names what ends each live process in the state it is in, and each named command does."""

    def retry(self, ident, *extra):
        return self.raw("retry", ident, "--reason", "Again", "--worker", "pi", "--model", MODEL, *extra)

    def test_f4_a_lane_retry_names_collect_and_stop_and_offers_no_force_alive(self):
        first, first_task = self.lane_ok()
        self.settle(first_task)
        self.collect(first["id"])
        second, second_task = self.existing(step="s1")
        pane, _ = self.pane(self.worker_command(second_task))
        self.attach(second_task, pane)
        self.settle(second_task)
        run = self.retry(first["id"])
        self.refused(run, f"{second['id']} (writer, settled: finished, not collected", "does not apply to a lane's working tree")
        self.assertNotIn("or pass --force-alive", run.stderr)
        self.assertNotIn("tp-agent reap", run.stderr)
        self.assertEqual(named(run.stderr), [f"tp-agent collect {second['id']}", f"tp-agent stop {second['id']}"])
        # the flag the message does not offer does not help either: the lane's own rule is the next refusal
        forced = self.retry(first["id"], "--force-alive")
        self.assertEqual(forced.returncode, 1)
        self.assertIn("lane alpha already has a writer", forced.stderr)
        self.assertIn("settled", forced.stderr)
        self.assertEqual(named(forced.stderr), [f"tp-agent collect {second['id']}", f"tp-agent stop {second['id']}"])
        # collect, as named, and the retry goes through
        self.assertEqual(self.shell(named(run.stderr)[0]).returncode, 0)
        retried = self.retry(first["id"])
        self.assertEqual(retried.returncode, 0, retried.stderr)

    def test_f4_stop_as_named_clears_a_lane_retry_too(self):
        first, first_task = self.lane_ok()
        self.settle(first_task)
        self.collect(first["id"])
        second, second_task = self.existing(step="s1")
        pane, _ = self.pane(self.worker_command(second_task))
        self.attach(second_task, pane)
        self.settle(second_task)
        run = self.retry(first["id"])
        self.assertEqual(self.shell(named(run.stderr)[1]).returncode, 0)
        self.assertEqual(self.retry(first["id"]).returncode, 0)

    def two_settled(self):
        first, first_task, first_pane, _ = self.live_task()
        second, second_task, second_pane, _ = self.live_task()
        return (first, first_task, first_pane), (second, second_task, second_pane)

    def test_f6_a_retry_beside_settled_workers_names_collect_not_reap(self):
        (first, _, first_pane), (second, _, second_pane) = self.two_settled()
        run = self.retry(first["id"])
        self.refused(run, "settled: finished, not collected", "or pass --force-alive to start the attempt beside it")
        self.assertNotIn("tp-agent reap", run.stderr)
        self.assertEqual(sorted(named(run.stderr)), sorted([f"tp-agent collect {first['id']}", f"tp-agent stop {first['id']}",
                                                            f"tp-agent collect {second['id']}", f"tp-agent stop {second['id']}"]))
        for ident in (first["id"], second["id"]):  # collect is named before stop for each
            self.assertLess(named(run.stderr).index(f"tp-agent collect {ident}"), named(run.stderr).index(f"tp-agent stop {ident}"))
        # what the old message named lists nothing for a settled worker ...
        self.assertEqual(self.cli("reap")["tasks"], [])
        self.assertEqual(self.retry(first["id"]).returncode, 1)
        # ... and what it names now clears it, one by one
        self.collect(first["id"])
        self.assertEqual(self.retry(first["id"]).returncode, 1)
        self.collect(second["id"])
        self.assertFalse(self.alive(first_pane) or self.alive(second_pane))
        retried = self.retry(first["id"])
        self.assertEqual(retried.returncode, 0, retried.stderr)

    def test_f6_the_escalating_retry_gets_the_same_guard(self):
        (first, _, _), _ = self.two_settled()
        run = self.raw("retry", first["id"], "--reason", "Again", "--escalate")
        self.assertEqual(run.returncode, 1)
        self.assertIn(f"tp-agent collect {first['id']}", run.stderr)
        self.assertNotIn("tp-agent reap", run.stderr)

    def test_f6_force_alive_is_offered_where_it_works_and_does_work(self):
        (first, _, _), _ = self.two_settled()
        run = self.retry(first["id"])
        self.assertIn("pass --force-alive", run.stderr)
        forced = self.retry(first["id"], "--force-alive")
        self.assertEqual(forced.returncode, 0, forced.stderr)

    def test_f6_reap_is_named_for_a_collected_task_whose_process_is_alive_and_it_clears_it(self):
        item, task, pane, _ = self.live_task()
        (task / "collected.json").write_text(json.dumps({"collected": "2026-10-08T00:00:00Z"}))  # collected by an older version: pane left alive
        self.assertTrue(self.alive(pane))
        run = self.retry(item["id"])
        self.refused(run, "collected, its process still alive")
        self.assertEqual(named(run.stderr), [f"tp-agent collect {item['id']}", f"tp-agent reap {item['id']} --yes", f"tp-agent stop {item['id']}"])
        listed = self.cli("reap")
        self.assertEqual([row["id"] for row in listed["tasks"]], [item["id"]])
        done = self.shell(named(run.stderr)[1])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(self.alive(pane))
        self.assertEqual(self.retry(item["id"]).returncode, 0)

    def test_f6_reap_is_named_for_a_stopped_task_whose_process_survived_the_pane_and_it_clears_it(self):
        item, task, pane, _ = self.live_task()
        (task / "stopped.json").write_text(json.dumps({"stopped": "2026-10-08T00:00:00Z"}))
        run = self.retry(item["id"])
        self.refused(run, "stopped, its process still alive")
        self.assertEqual(named(run.stderr), [f"tp-agent reap {item['id']} --yes", f"tp-agent stop {item['id']}"])
        self.assertEqual(self.shell(named(run.stderr)[0]).returncode, 0)
        self.assertEqual(self.retry(item["id"]).returncode, 0)

    def test_a_retry_of_an_attempt_that_is_still_at_work_says_so_and_names_stop(self):
        item, task, pane, _ = self.live_task(state="working", result=None)
        run = self.retry(item["id"])
        self.refused(run, f"{item['id']} is still running", "Wait for it to settle")
        self.assertEqual(named(run.stderr), [f"tp-agent stop {item['id']}"])
        self.assertNotIn("or settle", run.stderr)
        self.assertEqual(self.shell(named(run.stderr)[0]).returncode, 0)
        self.assertEqual(self.retry(item["id"]).returncode, 0)

    def test_a_retry_of_an_attempt_waiting_for_an_answer_does_not_offer_waiting(self):
        item, task, pane, _ = self.live_task(state="waiting_input", result=None)
        (task / "state.json").write_text(json.dumps({"state": "needs_input"}))
        run = self.retry(item["id"])
        if "cannot be retried yet" in run.stderr:
            self.assertNotIn("Wait for it", run.stderr)
            self.assertEqual(named(run.stderr), [f"tp-agent stop {item['id']}"])


class ReviewRefusalTests(RemedyCase):
    """The review guard (tp-agent review / guard-review) names the same, state-aware commands."""

    def test_a_review_beside_a_settled_writer_names_collect_and_it_clears(self):
        author, author_task, author_pane, _ = self.live_task()
        writer, writer_task, writer_pane, _ = self.live_task()
        run = self.raw("guard-review", author["id"])
        self.assertEqual(run.returncode, 1)
        self.assertIn("a reviewer must not run beside it", run.stderr)
        self.assertIn("or pass --force-alive to review beside it", run.stderr)
        self.assertNotIn("tp-agent reap", run.stderr)
        for ident in (author["id"], writer["id"]):
            self.assertIn(f"tp-agent collect {ident}", run.stderr)
        self.collect(author["id"])
        self.collect(writer["id"])
        self.assertEqual(self.raw("guard-review", author["id"]).returncode, 0)

    def test_a_lane_review_guard_offers_no_force_alive(self):
        first, first_task = self.lane_ok()
        pane, _ = self.pane(self.worker_command(first_task))
        self.attach(first_task, pane)
        self.settle(first_task)
        run = self.raw("guard-review", first["id"])
        self.assertEqual(run.returncode, 1)
        self.assertIn("does not apply to a lane's working tree", run.stderr)
        self.assertNotIn("or pass --force-alive", run.stderr)
        self.assertEqual(self.shell(named(run.stderr)[0]).returncode, 0)
        self.assertEqual(self.raw("guard-review", first["id"]).returncode, 0)


class StatusLineTests(RemedyCase):
    """F5: one reader of the RESULT_STATUS line, and a notice that says the form."""

    DECORATED = ["RESULT_STATUS: complete", "### RESULT_STATUS: complete", "## RESULT_STATUS: complete", "**RESULT_STATUS: complete**",
                 "**RESULT_STATUS:** complete", "`RESULT_STATUS: complete`", "__RESULT_STATUS: complete__", "- RESULT_STATUS: complete",
                 "* RESULT_STATUS: complete", "1. RESULT_STATUS: complete", "### **RESULT_STATUS: complete**", "  RESULT_STATUS: complete",
                 "result_status: Complete", "RESULT_STATUS : complete", "RESULT_STATUS: `complete`", "RESULT_STATUS: **complete**"]
    REFUSED = ["The RESULT_STATUS: complete line is below", "Note: RESULT_STATUS: complete", "> RESULT_STATUS: complete",
               "    RESULT_STATUS: complete", "RESULT_STATUS complete", "RESULT_STATUS: finished", "RESULT_STATUS:", "I will end with RESULT_STATUS: complete",
               "x**RESULT_STATUS: complete", "RESULT_STATUS_X: complete", "# Heading RESULT_STATUS: complete", ""]

    def test_f5_the_reader_accepts_the_decorated_forms_and_refuses_other_text(self):
        for line in self.DECORATED:
            with self.subTest(line=line):
                self.assertEqual(result_status.result_status(f"Done.\n\n{line}\n"), "complete")
        for line in self.REFUSED:
            with self.subTest(line=line):
                self.assertIsNone(result_status.result_status(f"Done.\n\n{line}\n"))
        for word in result_status.STATUSES:
            self.assertEqual(result_status.result_status(f"### RESULT_STATUS: {word.upper()}"), word)

    def test_f5_every_reader_agrees_with_the_shared_one(self):
        for line in self.DECORATED + self.REFUSED:
            with self.subTest(line=line):
                reply = f"Done.\n\n{line}\n"
                expected = result_status.result_status(reply)
                # the lifecycle classifier: a reply without a status line is paused, one with it is not
                self.assertEqual(contract.RESULT_STATUS_RE.search(reply) is not None, expected is not None)
                self.assertEqual(contract.satisfies_dependency("settled", reply=reply), expected is not None)
                # the verdict reader drops exactly the lines the shared reader recognises from a reviewer's notes
                if line.strip():
                    _, notes = review_verdict.parse(f"REQUEST_CHANGES: because\nbody\n{line}\n")
                    self.assertEqual(line.strip() in notes, result_status.RESULT_STATUS_LINE.search(line) is None, notes)
                file = self.root / "reply.md"
                file.write_text(reply)
                cli = subprocess.run([sys.executable, str(ROOT / "lib/result_status.py"), str(file)], text=True, capture_output=True)
                self.assertEqual(cli.stdout.strip() or None, expected)
                self.assertEqual(cli.returncode, 0 if expected else 1)

    def test_f5_the_typescript_reader_gives_the_same_answers(self):
        source = (ROOT / "agent/extensions/dispatcher.ts").read_text()
        literal = re.search(r"^const RESULT_STATUS_LEAD = (\".*\");$", source, re.M)
        self.assertIsNotNone(literal, "the dispatcher keeps a RESULT_STATUS_LEAD constant")
        self.assertEqual(json.loads(literal.group(1)), result_status.RESULT_STATUS_LEAD)
        lines = self.DECORATED + self.REFUSED
        # the dispatcher reads the key and its decoration (the verdict path drops the whole line); the status word is not its business
        script = (f"const L = {literal.group(1)}; const lines = JSON.parse(process.argv[1]);"
                  "console.log(JSON.stringify(lines.map(l => new RegExp(L, 'im').test('Done.\\n\\n' + l + '\\n'))))")
        run = subprocess.run(["node", "-e", script, json.dumps(lines)], text=True, capture_output=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout), [result_status.RESULT_STATUS_LINE.search(f"Done.\n\n{l}\n") is not None for l in lines])

    def test_f5_a_decorated_status_line_is_collected(self):
        for line in ["### RESULT_STATUS: complete", "**RESULT_STATUS: complete**", "`RESULT_STATUS: complete`"]:
            with self.subTest(line=line):
                item, task, pane, _ = self.live_task(result=f"Finished the change.\n\n{line}\n")
                inspected = self.cli("inspect", item["id"])
                self.assertEqual((inspected["status"], inspected["result_status"]), ("settled", "complete"))
                collected = self.collect(item["id"])
                self.assertEqual((collected["status"], collected["result_status"]), ("completed", "complete"))
                self.assertNotIn("notice", collected)
                self.assertFalse(self.alive(pane))

    def test_f5_a_decorated_status_line_is_reviewable(self):
        item, task, pane, _ = self.live_task(result="Finished the change.\n\n### RESULT_STATUS: complete\n")
        self.assertEqual(self.cli("inspect", item["id"])["status"], "settled")
        gate = subprocess.run([sys.executable, str(ROOT / "lib/result_status.py"), str(task / "result.md")], text=True, capture_output=True)
        self.assertEqual((gate.returncode, gate.stdout.strip()), (0, "complete"))

    def test_f5_text_before_the_key_is_still_paused_and_the_notice_states_the_form_and_the_command(self):
        item, task, pane, _ = self.live_task(result="Finished.\n\nNote: RESULT_STATUS: complete\n")
        run = self.raw("collect", item["id"])
        self.assertEqual(run.returncode, 0, run.stderr)
        value = json.loads(run.stdout)
        self.assertEqual(value["status"], "paused")
        self.assertFalse((task / "collected.json").exists())
        self.assertEqual(value["notice"], run.stderr.strip())
        self.assertIn(result_status.RESULT_STATUS_FORM, value["notice"])
        send = [c for c in named(value["notice"]) if " message " in c]
        self.assertEqual(send, [f"tp-agent message {item['id']} --text \"End your reply with a line that reads exactly RESULT_STATUS: complete\""])
        self.assertIn(f"tp-agent stop {item['id']}", value["notice"])

    def test_f5_the_message_the_notice_names_works_and_the_reply_after_it_collects(self):
        item, task, pane, _ = self.live_task(result="Finished.\n")
        run = self.raw("collect", item["id"])
        notice = json.loads(run.stdout)["notice"]
        command = [c for c in named(notice) if " message " in c]
        self.assertEqual(len(command), 1, notice)
        sent = self.shell(command[0])
        self.assertEqual(sent.returncode, 0, sent.stderr)
        self.assertTrue(json.loads(sent.stdout)["sent"])
        self.assertIn("RESULT_STATUS: complete", (task / "messages.jsonl").read_text())
        (task / "result.md").write_text("Finished.\n\n**RESULT_STATUS: complete**\n")  # the worker's answer, decorated
        (task / "state.json").write_text(json.dumps({"state": "waiting"}))
        collected = self.collect(item["id"])
        self.assertEqual((collected["status"], collected["result_status"]), ("completed", "complete"))

    def test_f5_a_paused_worker_whose_pane_is_gone_is_not_told_to_message_it(self):
        item, task, pane, _ = self.live_task(result="Finished.\n")
        self.tmux("kill-pane", "-t", pane)
        run = self.raw("collect", item["id"])
        value = json.loads(run.stdout)
        self.assertEqual(value["status"], "paused")
        self.assertNotIn("tp-agent message", value["notice"])
        self.assertEqual([c.split(" --reason")[0] for c in named(value["notice"])], [f"tp-agent retry {item['id']}", f"tp-agent stop {item['id']}"])
        self.assertIn(result_status.RESULT_STATUS_FORM, value["notice"])
        # the retry it names starts the worker again; the fixture's tmux does not run send-keys, so no message is tried here
        retried = self.shell(named(value["notice"])[0].replace("--reason", "--worker pi --model " + MODEL + " --reason", 1))
        self.assertEqual(retried.returncode, 0, retried.stderr)

    def test_f5_review_refuses_an_unstatused_reply_with_the_form_and_the_command(self):
        item, task, pane, _ = self.live_task(result="Finished.\n\nRESULT_STATUS - done\n")
        run = subprocess.run([str(ROOT / "bin/tp-review"), item["id"]], env=self.env, text=True, capture_output=True, timeout=90)
        self.assertEqual(run.returncode, 1)
        self.assertIn("RESULT_STATUS: complete", run.stderr)
        self.assertIn(f"tp-agent message {item['id']} --text", run.stderr)


class AuditTests(RemedyCase):
    """Every other refusal that names a command: the command exists and clears the refusal in the state it is printed in."""

    def test_every_tp_agent_command_a_refusal_names_is_a_command_tp_agent_has(self):
        help_text = subprocess.run([str(ROOT / "bin/tp-agent"), "--help"], text=True, capture_output=True, env=self.env).stdout
        commands = set(re.search(r"\{([a-z,-]+)\}", help_text).group(1).split(","))
        # words that follow "tp-agent" in a sentence, and the one command a message says does not exist yet (lane dispatch's
        # `--tests full` refusal: "tp-agent integrate ships in a later slice; until then run it there yourself")
        prose = {"does", "uses", "command", "calls", "integrate"}
        files = [ROOT / "bin" / name for name in ("tp-agent", "tp-spawn", "tp-review", "tp-kill")]
        files += [ROOT / "lib" / name for name in ("worker_process.py", "lane_lease.py", "lanes.py", "lane_commands.py", "lane_steps.py")]
        named_somewhere = {word for file in files for word in re.findall(r"tp-agent ([a-z][a-z-]*)", file.read_text())}
        self.assertTrue({"collect", "stop", "reap", "retry", "message"} <= named_somewhere)
        for word in sorted(named_somewhere - prose):
            with self.subTest(command=word):
                self.assertIn(word, commands)

    def test_an_unreadable_queue_entry_names_stop_and_stop_removes_it(self):
        self.lane_ok()
        (self.state / "queue").mkdir(exist_ok=True)
        (self.state / "queue/coder-broken0001.json").write_text(json.dumps({"id": "coder-broken0001", "lane": "alpha"}))
        run = self.lane_dispatch(step="s1", extra=())
        self.refused(run, "cannot be read", "remove it with: `tp-agent stop coder-broken0001`")
        self.assertEqual(self.shell(named(run.stderr)[0]).returncode, 0)
        self.assertFalse((self.state / "queue/coder-broken0001.json").exists())
        self.lane_ok(step="s1", extra=())

    def test_an_unreadable_queue_entry_whose_name_is_no_id_is_not_given_a_stop_that_cannot_work(self):
        self.lane_ok()
        (self.state / "queue").mkdir(exist_ok=True)
        entry = self.state / "queue/_broken.json"
        entry.write_text(json.dumps({"id": "_broken", "lane": "alpha"}))
        run = self.lane_dispatch(step="s1", extra=())
        self.refused(run, "cannot be read", f"remove it with: delete the file {entry}")
        self.assertNotIn("tp-agent stop _broken", run.stderr)
        entry.unlink()
        self.lane_ok(step="s1", extra=())

    def test_a_message_to_a_worker_without_a_pane_names_retry_and_retry_works(self):
        item, task = self.dispatch()
        self.settle(task, state="exited")
        self.attach(task, "")
        meta = json.loads((task / "meta.json").read_text())
        meta.pop("pane", None)
        (task / "meta.json").write_text(json.dumps(meta))
        run = self.raw("message", item["id"], "--text", "x")
        self.refused(run, "cannot receive a message", f"tp-agent retry {item['id']} --reason")
        retried = self.raw("retry", item["id"], "--reason", "Again", "--worker", "pi", "--model", MODEL)
        self.assertEqual(retried.returncode, 0, retried.stderr)

    def test_ending_a_task_that_is_not_finished_names_stop(self):
        item, task, pane, _ = self.live_task(state="working", result=None)
        with self.assertRaises(ValueError) as caught:
            worker_process.end_process(task)
        self.assertIn(f"tp-agent stop {item['id']}", str(caught.exception))
        self.assertEqual(self.shell(f"tp-agent stop {item['id']}", {**self.env, "PATH": f"{ROOT / 'bin'}{os.pathsep}{self.env['PATH']}"}).returncode, 0)
        self.assertFalse(self.alive(pane))

    def test_a_process_that_cannot_be_shown_to_be_the_worker_is_not_promised_a_reap(self):
        item, task = self.dispatch()
        # a failed stop recorded a process; its pane is gone, and the pid now belongs to something else (a different start time)
        other, other_pid = self.pane()
        rec = {"pane": "%9998", "pid": other_pid, "pgid": other_pid, "group": os.getpgid(other_pid) == other_pid,
               "members": [[other_pid, "Mon Jan  1 00:00:00 2001"]], "recorded": "2026-10-08T00:00:00Z"}
        (task / "process.json").write_text(json.dumps(rec))
        target = f"-{other_pid}" if rec["group"] else str(other_pid)
        failed = self.raw("reap", item["id"], "--yes")
        self.assertEqual(failed.returncode, 1)
        self.assertIn("not signalled", failed.stderr)
        self.assertIn(f"`kill -KILL -- {target}`", failed.stderr)
        self.assertIn(f"`tp-agent reap {item['id']} --yes` drops the record", failed.stderr)
        self.assertTrue(self.alive(other), "the unrelated process was left alone")
        # as the message says: the owner ends it, and reap then drops the record
        self.tmux("kill-pane", "-t", other)
        self.assertEqual(self.cli("reap", item["id"], "--yes")["confirmed"], True)
        self.assertFalse((task / "process.json").exists())



def backticked(text):
    """Every command in backticks in a refusal, in order: what a person pastes."""
    return re.findall(r"`([^`]+)`", text)


class StepRemedyTests(StepCase):
    """L4: every refusal of the review gate, `lane close` and the next-step rule names a command; each test prints the refusal,
    runs what it names (through sh, as typed, with only the placeholders the text itself marks as ones to fill: NEW-ID, ROLE) and
    shows the refusal gone or moved on to the next step of the way. The last test walks one lane from a working writer to a
    closed step by only typing what the refusals said."""

    def shell(self, command, cwd=None):
        env = dict(self.env)
        env["PATH"] = os.pathsep.join([str(ROOT / "bin"), env["PATH"]])
        return subprocess.run(["sh", "-c", command], env=env, cwd=cwd or self.repo, text=True, capture_output=True, timeout=90)

    def type_(self, command, **fill):
        for word, value in fill.items():
            command = command.replace(word, value)
        run = self.shell(command)
        self.assertEqual(run.returncode, 0, f"{command}\n{run.stderr}")
        return run

    def printed(self, make_run, *words):
        """The commands (in backticks) the refusal prints, after checking it is a clean refusal that says `words`."""
        return backticked(self.untouched(make_run, *words))

    def new_reviewer(self, author, command=None):
        before = {p.name for p in (self.state / "tasks").glob("review-*")}
        run = self.type_(command or f"tp-agent review {author} --force")
        found = [p for p in (self.state / "tasks").glob("review-*") if p.name not in before]
        self.assertEqual(len(found), 1, run.stdout)
        return found[0]

    # --- the gate ---------------------------------------------------------------------------------------------------

    def test_a_working_writer_names_stop_and_stop_clears_it(self):
        author, task = self.writer(settled=False)
        self.edit()
        self.commit()
        commands = self.printed(self.next_step, author, f"`tp-agent stop {author}`")
        self.assertIn(f"tp-agent stop {author}", commands)
        self.type_(f"tp-agent stop {author}")
        # moved on: the next refusal does not mention the writer as unfinished, and it names a command too
        stderr = self.untouched(self.next_step, "step s1 is not closed", "No writer of the step can be reviewed")
        self.assertNotIn("so the step's work is not finished", stderr)

    def test_a_queued_task_names_stop_and_stop_takes_it_out(self):
        self.writer()
        self.edit()
        self.commit()
        queued = {"id": "coder-queued0002", "created": "2026-10-08T00:00:00Z", "role": "coder", "task": "t", "workdir": str(self.worktree),
                  "worker": "pi", "model": MODEL, "thinking": "medium", "route_info": {}, "deliverable": "", "parent": "", "after": [],
                  "context": [], "notes": [], "tests": "none", "lane": "alpha", "lane_step": "s1", "preamble_from": str(self.repo)}
        (self.state / "queue").mkdir(exist_ok=True)
        (self.state / "queue/coder-queued0002.json").write_text(json.dumps(queued))
        self.printed(self.close, "coder-queued0002 is queued for step s1", "`tp-agent stop coder-queued0002`")
        self.type_("tp-agent stop coder-queued0002")
        self.assertFalse((self.state / "queue/coder-queued0002.json").exists())
        self.untouched(self.close, "step s1 of lane alpha cannot be closed", "has no review yet")

    def test_a_settled_unreviewed_author_names_review_and_the_review_it_starts_counts(self):
        author, _ = self.writer()
        self.edit()
        self.commit()
        self.printed(self.close, "has no review yet", f"`tp-agent review {author} --force`")
        reviewer = self.new_reviewer(author)
        # the gate sees the reviewer tp-review made: started (so waiting), then, answered, approving
        self.untouched(self.close, f"{reviewer.name} is reviewing this tree")
        self.settle(reviewer, result=APPROVED)
        self.assertEqual(self.closes()["closed"][0]["review"], reviewer.name)

    def test_a_running_review_names_stop_and_a_stopped_one_names_review_again(self):
        author, _ = self.writer()
        self.edit()
        self.commit()
        reviewer = self.new_reviewer(author)
        self.printed(self.close, f"{reviewer.name} is reviewing this tree", f"`tp-agent stop {reviewer.name}`")
        self.type_(f"tp-agent stop {reviewer.name}")
        self.printed(self.close, f"{reviewer.name}: it ended stopped", f"`tp-agent review {author} --force`")
        second = self.new_reviewer(author)
        self.settle(second, result=APPROVED)
        self.assertEqual(self.closes()["closed"][0]["review"], second.name)

    def test_a_review_that_asked_for_changes_names_retry_and_retry_puts_a_new_writer_in_the_step(self):
        author, _ = self.writer()
        self.edit()
        self.commit()
        reviewer, _ = self.review(author, CHANGES)
        commands = self.printed(self.next_step, f"{reviewer} asked for changes")
        retry = next(c for c in commands if c.startswith("tp-agent retry"))
        self.type_(retry + " --worker pi --model " + MODEL)
        stderr = self.untouched(self.next_step, "step s1 is not closed")
        self.assertRegex(stderr, r"coder-[0-9a-f]+ is (starting|running)")  # the new writer, in the same step: the lane is moving

    def test_approved_work_that_is_not_committed_names_the_commit_and_the_close(self):
        author, _ = self.writer()
        self.edit()
        self.review(author)
        commands = self.printed(self.close, "the approved work is not committed")
        commit = next(c for c in commands if c.startswith("git -C"))
        self.type_(commit)  # `git -C WT add -A && git -C WT commit -m "..."`
        close = next(c for c in commands if c.startswith("tp-agent lane close"))
        self.type_(close)
        self.assertEqual(len(self.record()["closed"]), 1)

    def test_a_review_without_a_usable_verdict_names_review_and_a_second_review_settles_it(self):
        author, _ = self.writer()
        self.edit()
        self.commit()
        bad, _ = self.review(author, "no verdict here\n\nRESULT_STATUS: complete\n")
        self.printed(self.close, f"{bad}: ", f"`tp-agent review {author} --force`")
        second = self.new_reviewer(author)
        self.settle(second, result=APPROVED)
        self.assertEqual(self.closes()["closed"][0]["review"], second.name)

    def test_a_stale_review_names_review_and_the_new_review_counts(self):
        author, _ = self.writer()
        self.edit()
        self.commit()
        self.review(author)
        self.edit("later.py", "after the review\n")
        self.commit("later")
        self.printed(self.close, "the earlier ones judged other work", f"`tp-agent review {author} --force`")
        fresh = self.new_reviewer(author)
        self.settle(fresh, result=APPROVED)
        self.assertEqual(self.closes()["closed"][0]["review"], fresh.name)

    def test_with_no_writer_to_review_the_printed_dispatch_makes_one(self):
        author, _ = self.writer()
        self.edit()
        self.commit()
        (self.state / "archive").mkdir()
        shutil.move(str(self.state / "tasks" / author), str(self.state / "archive" / author))
        commands = self.printed(self.close, "No writer of the step can be reviewed")
        dispatch = next(c for c in commands if c.startswith("tp-agent dispatch"))
        self.type_(dispatch + " --worker pi --model " + MODEL)
        stderr = self.untouched(self.close, "cannot be closed")
        self.assertRegex(stderr, r"coder-[0-9a-f]+ is (starting|running)")

    def test_approved_and_committed_but_not_recorded_names_the_close_and_the_close_clears_it(self):
        self.approved_and_committed()
        commands = self.printed(self.next_step, "it is approved and committed but not recorded", "`tp-agent lane close alpha s1`")
        self.type_(next(c for c in commands if c.startswith("tp-agent lane close")))
        self.existing(step="s2")

    # --- lane close -------------------------------------------------------------------------------------------------

    def test_a_close_in_a_session_that_has_no_such_step_names_lanes_and_lanes_runs(self):
        self.writer()
        commands = self.printed(lambda: self.close("nope"), "lane alpha has no step nope in this session", "`tp-agent lanes alpha`")
        run = self.type_(next(c for c in commands if c.startswith("tp-agent lanes")))
        self.assertEqual(json.loads(run.stdout)[0]["open_step"], "s1")

    def test_a_close_from_another_session_names_the_state_directory_and_that_works(self):
        self.approved_and_committed()
        other = self.root / "other-state"
        (other / "tasks").mkdir(parents=True)
        env = {**self.env, "TP_STATE_DIR": str(other)}
        run = subprocess.run([str(ROOT / "bin/tp-agent"), "lane", "close", "alpha", "s1", "--repo", str(self.repo)], env=env, text=True, capture_output=True)
        self.refused(run, "TP_STATE_DIR=<its state directory> tp-agent lane close alpha s1")
        fixed = subprocess.run([str(ROOT / "bin/tp-agent"), "lane", "close", "alpha", "s1", "--repo", str(self.repo)],
                               env={**self.env, "TP_STATE_DIR": str(self.state)}, text=True, capture_output=True)
        self.assertEqual(fixed.returncode, 0, fixed.stderr)

    def test_closing_a_closed_step_names_a_new_step_and_that_dispatch_works(self):
        self.approved_and_committed()
        self.closes()
        commands = self.printed(self.close, "is already closed at")
        self.type_(next(c for c in commands if c.startswith("tp-agent dispatch")) + " --worker pi --model " + MODEL, **{"NEW-ID": "s2", "ROLE": "coder"})

    def test_closing_a_step_that_is_not_the_open_one_names_the_open_one_and_that_close_runs(self):
        self.writer("s1")
        newer = self.state / "tasks/coder-newer00002"
        newer.mkdir()
        meta = json.loads(next((self.state / "tasks").glob("coder-*/meta.json")).read_text())
        meta.update(id="coder-newer00002", lane_step="s2", created="2999-01-01T00:00:00Z")
        (newer / "meta.json").write_text(json.dumps(meta))
        self.settle(newer)
        commands = self.printed(lambda: self.close("s2"), "is not lane alpha's open step (s1, the oldest that is not closed)", "`tp-agent lane close alpha s1`")
        run = self.shell(next(c for c in commands if c.startswith("tp-agent lane close")))
        self.assertEqual(run.returncode, 0, run.stderr)  # s1 changed nothing: it closes empty, and s2 is then the open one
        self.assertEqual(self.lanes()["open_step"], "s2")

    def test_an_older_step_that_was_never_closed_holds_the_next_one_and_its_close_is_printed(self):
        author, _ = self.approved_and_committed()
        self.closes()
        # a step left unclosed from before the rule, older than the closed s1's tasks: it is open, whatever the newest task is
        old = self.state / "tasks/coder-oldstep0001"
        old.mkdir()
        meta = json.loads((self.state / "tasks" / author / "meta.json").read_text())
        meta.update(id="coder-oldstep0001", lane_step="s0", created="2000-01-01T00:00:00Z")
        (old / "meta.json").write_text(json.dumps(meta))
        self.settle(old)
        commands = self.printed(self.next_step, "lane alpha: step s0 is not closed", "it changed nothing, so it needs no review, only the close",
                                "`tp-agent lane close alpha s0`")
        self.type_(next(c for c in commands if c.startswith("tp-agent lane close")))
        self.existing(step="s2")

    def test_a_late_review_of_a_closed_step_does_not_let_the_next_step_past_the_open_one(self):
        author, _ = self.approved_and_committed()
        self.closes()
        second, _ = self.writer("s2")
        self.edit("s2.py")
        self.commit("s2 work")
        self.review(author, step="s1")  # a review of the closed s1, newer than every task of s2
        commands = self.printed(lambda: self.next_step("s3"), "lane alpha: step s2 is not closed", f"`tp-agent review {second} --force`")
        reviewer = self.new_reviewer(second, next(c for c in commands if c.startswith("tp-agent review")))
        self.settle(reviewer, result=APPROVED)
        commands = self.printed(lambda: self.next_step("s3"), "it is approved and committed but not recorded", "`tp-agent lane close alpha s2`")
        self.type_(next(c for c in commands if c.startswith("tp-agent lane close")))
        self.existing(step="s3")

    def test_an_empty_step_needs_only_the_close_and_the_printed_close_runs(self):
        self.writer()
        commands = self.printed(self.next_step, "lane alpha: step s1 is not closed", "it changed nothing, so it needs no review, only the close",
                                "`tp-agent lane close alpha s1`")
        run = self.type_(next(c for c in commands if c.startswith("tp-agent lane close")))
        self.assertEqual(json.loads(run.stdout)["closed"][0]["verdict"], "EMPTY")
        self.existing(step="s2")

    # --- an unreadable record that the lane names -------------------------------------------------------------------

    def test_an_unreadable_task_of_the_open_step_names_its_directory_and_the_removal_clears_it(self):
        author, task = self.writer()  # nothing changed: the removal leaves a clean lane at its base
        (task / "meta.json").write_text("{broken")
        commands = self.printed(self.next_step, "lane alpha: step s1 is not closed", f"{author} ({task})", "meta.json cannot be read")
        remove = next(c for c in commands if c.startswith("rm -r"))
        self.assertEqual(remove, f"rm -r {task}")
        self.type_(remove)
        self.existing(step="s2")  # the refusal is gone and nothing else holds the lane

    def test_the_same_refusal_from_lane_close_names_the_same_command(self):
        author, task = self.writer()
        (task / "meta.json").unlink()
        commands = self.printed(self.close, "step s1 of lane alpha cannot be closed", f"{author} ({task})")
        self.type_(next(c for c in commands if c.startswith("rm -r")))
        self.untouched(self.close, "has no step s1 in this session")  # nothing names the step any more

    def test_after_the_removal_work_left_in_the_tree_is_held_by_the_next_check_whose_commands_work(self):
        author, task = self.writer()
        self.edit()
        self.commit()
        (task / "meta.json").write_text("{broken")
        commands = self.printed(self.next_step, f"{author} ({task})")
        self.type_(next(c for c in commands if c.startswith("rm -r")))
        for command in self.printed(self.next_step, "lane alpha has changes since the lane's base", "1 commit(s) after the closed one"):
            self.type_(command)
        self.existing(step="s2")

    def test_a_running_process_of_an_unreadable_task_is_ended_by_the_printed_command_then_removed(self):
        author, task = self.writer()
        pane, _ = self.pane(self.worker_command(task))
        (task / "meta.json").write_text("{broken")
        commands = self.printed(self.next_step, f"{author} ({task})", "a process of it is running")
        kill = next(c for c in commands if c.startswith("tmux kill-pane"))
        self.assertEqual(kill, f"tmux kill-pane -t {pane}")
        self.type_(kill)
        self.assertFalse(self.alive(pane))
        commands = self.printed(self.next_step, f"{author} ({task})")
        self.assertEqual([c for c in commands if c.startswith("rm -r")], [f"rm -r {task}"])
        self.type_(f"rm -r {task}")
        self.existing(step="s2")

    def test_a_task_only_the_lease_names_is_cleared_by_the_same_removal(self):
        author, task = self.writer()
        (task / "dispatch.json").unlink()
        (task / "meta.json").write_text("{broken")
        (self.lane_dir() / "lease.json").write_text(json.dumps({"schema": "tp-lane-lease:v1", "holders": [
            {"kind": "worker", "access": "write", "task": author, "role": "coder", "session": str(self.state), "pid": 99999999,
             "start": "Mon Jan  1 00:00:00 2001", "pane": None, "taken": "2026-10-08T00:00:00+00:00"}]}))
        commands = self.printed(self.next_step, f"lane alpha: {author} ({task})", "belongs to an open step of this lane")
        self.type_(next(c for c in commands if c.startswith("rm -r")))
        self.existing(step="s2")

    # --- checks 15 and 17 -------------------------------------------------------------------------------------------

    def test_a_task_into_a_closed_step_names_a_new_id_and_a_new_id_works(self):
        self.approved_and_committed()
        self.closes()
        commands = self.printed(lambda: self.lane_dispatch(step="s1", extra=()), "was closed at")
        self.type_(next(c for c in commands if c.startswith("tp-agent dispatch")) + " --worker pi --model " + MODEL, **{"NEW-ID": "s1b", "ROLE": "coder"})

    def test_changes_made_by_hand_after_a_close_name_the_stash_and_the_stash_works(self):
        self.approved_and_committed()
        self.closes()
        self.edit("by-hand.py", "x\n")
        commands = self.printed(self.next_step, "lane alpha has changes since step s1 closed")
        for command in commands:
            self.type_(command)
        self.existing(step="s2")
        self.assertIn("by-hand.py", git(self.worktree, "stash", "show", "--include-untracked", "--name-only", "stash@{0}"))

    def test_commits_made_by_hand_after_a_close_name_the_save_and_reset_and_they_work(self):
        self.approved_and_committed()
        closed = self.closes()["closed"][0]["commit"]
        self.edit("by-hand.py", "x\n")
        by_hand = self.commit("by hand")
        self.edit("dirty.py", "y\n")  # both at once: the stash must come first or the reset would take the file
        commands = self.printed(self.next_step, "lane alpha has changes since step s1 closed", "1 commit(s) after the closed one", "1 uncommitted path(s)")
        self.assertEqual(len(commands), 2)
        for command in commands:
            self.type_(command)
        self.assertEqual(git(self.worktree, "rev-parse", "HEAD"), closed)
        self.assertEqual(git(self.repo, "rev-parse", f"saved/alpha-{by_hand[:8]}"), by_hand)
        self.assertIn("dirty.py", git(self.worktree, "stash", "show", "--include-untracked", "--name-only", "stash@{0}"))
        self.existing(step="s2")

    # --- one lane, from the first task to a closed step, typing only what the refusals said -------------------------------

    def test_a_lane_is_never_stuck_a_walk_from_a_working_writer_to_a_closed_step(self):
        author, task = self.writer(settled=False)
        self.edit("first.py", "a first try\n")  # the step changed the tree, so it needs a review and not only a close
        # 1. the writer is working: wait or stop it
        stop = next(c for c in self.printed(self.next_step, "step s1 is not closed") if c.startswith("tp-agent stop"))
        self.type_(stop)
        # 2. nothing to review: the printed dispatch makes a writer in the same step
        dispatch = next(c for c in self.printed(self.next_step, "No writer of the step can be reviewed") if c.startswith("tp-agent dispatch"))
        self.type_(dispatch + " --worker pi --model " + MODEL)
        second = next(p for p in (self.state / "tasks").glob("coder-*") if p.name != author)
        self.settle(second)
        self.edit()
        # 3. settled, work in the tree, no review: review it
        review = next(c for c in self.printed(self.next_step, "has no review yet") if c.startswith("tp-agent review"))
        self.type_(review)
        reviewer = next((self.state / "tasks").glob("review-*"))
        # 4. the reviewer is working: it finishes, and rejects
        self.untouched(self.next_step, f"{reviewer.name} is reviewing this tree")
        self.settle(reviewer, result=CHANGES)
        retry = next(c for c in self.printed(self.next_step, "asked for changes") if c.startswith("tp-agent retry"))
        # 5. the rejected tree changes, a new review of the new tree approves it
        self.type_(retry + " --worker pi --model " + MODEL)
        third = next(p for p in (self.state / "tasks").glob("coder-*") if p.name not in (author, second.name))
        self.settle(third)
        self.edit("fix.py", "the fix\n")
        review = next(c for c in self.printed(self.next_step, "the earlier ones judged other work") if c.startswith("tp-agent review"))
        self.type_(review)
        newest = max((p for p in (self.state / "tasks").glob("review-*") if p != reviewer), key=lambda p: p.stat().st_mtime_ns)
        self.settle(newest, result=APPROVED)
        # 6. approved, not committed: commit as reviewed, close
        commands = self.printed(self.next_step, "the approved work is not committed")
        self.type_(next(c for c in commands if c.startswith("git -C")))
        close = next(c for c in self.printed(self.next_step, "it is approved and committed but not recorded") if c.startswith("tp-agent lane close"))
        self.type_(close)
        # 7. step s2 starts, from the closed commit
        self.existing(step="s2")
        self.assertEqual(git(self.worktree, "rev-parse", "HEAD"), self.record()["closed"][0]["commit"])


if __name__ == "__main__":
    unittest.main()
