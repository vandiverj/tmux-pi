"""A worker's process ends with its task (T3) and its report.md survives the final reply (T5).

Panes are real, on a private tmux server (-L socket); only the calls that would launch a worker are stubbed."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
MODEL = "openai-codex/gpt-6-sol"
TMUX = shutil.which("tmux")


@unittest.skipUnless(TMUX, "tmux not installed")
class ProcessEndTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.other = self.root / "other"
        self.other.mkdir()
        self.state = self.project / ".tmux-pi"
        (self.state / "tasks").mkdir(parents=True)
        self.sock = f"tp-t3-{os.getpid()}-{self.id().split('.')[-1][-20:]}"
        fake = self.root / "bin"
        fake.mkdir()
        wrapper = fake / "tmux"
        # Pane queries and kills are real; everything that would start a worker is a no-op.
        wrapper.write_text(f'''#!/bin/sh
case "$1" in
 list-panes) [ ! -f "$TP_STATE_DIR/list-fail" ] || {{ echo "no server" >&2; exit 1; }}
   if [ -f "$TP_STATE_DIR/list-rows" ]; then cat "$TP_STATE_DIR/list-rows"; exit 0; fi
   exec {TMUX} -L {self.sock} "$@" ;;
 kill-pane|display-message|new-session|new-window-real) exec {TMUX} -L {self.sock} "$@" ;;
 show-options) printf '%s\\n' "$TP_STATE_DIR" ;;
 new-window) printf '%%9999\\n' ;;
 *) exit 0 ;;
esac
''')
        wrapper.chmod(0o755)
        # ps shows what is really running, plus (while ghost-ps exists) one recorded row that never dies: a survivor of KILL.
        (fake / "ps").write_text(f'''#!/bin/sh
out=$(/bin/ps "$@"); printf '%s\\n' "$out"
if [ -f "$TP_STATE_DIR/ghost-ps" ]; then
  pid=$(cut -d' ' -f1 "$TP_STATE_DIR/ghost-ps")
  printf '%s\\n' "$out" | awk -v p="$pid" '$1 == p && $3 !~ /^Z/ {{ f = 1 }} END {{ exit !f }}' || cat "$TP_STATE_DIR/ghost-ps"
fi
''')
        (fake / "ps").chmod(0o755)
        self.env = {**os.environ, "TP_STATE_DIR": str(self.state), "TP_SESSION": "fixture",
                    "PATH": str(fake) + os.pathsep + os.environ["PATH"]}
        self.env.pop("TMUX", None)
        self.env.pop("TMUX_PANE", None)
        self.tmux("new-session", "-d", "-s", "anchor", "sleep 300")
        self.addCleanup(self.stop_server)

    def tmux(self, *args):
        return subprocess.run([TMUX, "-L", self.sock, *args], text=True, capture_output=True, check=True).stdout.strip()

    def stop_server(self):
        try:
            os.kill(int(self.tmux("display-message", "-p", "#{pid}")), signal.SIGKILL)
        except (subprocess.CalledProcessError, ProcessLookupError, ValueError):
            pass

    def pane(self, command="sleep 300"):
        """A real pane that outlives its command, like a worker's. Returns (pane id, pid)."""
        out = self.tmux("new-window", "-d", "-P", "-F", "#{pane_id} #{pane_pid}", command)
        pane, pid = out.split()
        self.tmux("set-option", "-p", "-t", pane, "remain-on-exit", "on")
        return pane, int(pid)

    def alive(self, pane):
        rows = dict(line.split() for line in self.tmux("list-panes", "-a", "-F", "#{pane_id} #{pane_dead}").splitlines())
        return rows.get(pane) == "0"

    def raw(self, *args):
        return subprocess.run([str(ROOT / "bin/tp-agent"), *args], env=self.env, text=True, capture_output=True, timeout=60)

    def cli(self, *args):
        run = self.raw(*args)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def dispatch(self, workdir=None, role="coder"):
        item = self.cli("dispatch", "--role", role, "--task", "Make a focused change", "--worker", "pi", "--model", MODEL,
                        "--thinking", "medium", "--workdir", str(workdir or self.project), "--deliverable", "Report")
        return item, self.state / "tasks" / item["id"]

    def attach(self, task, pane):
        meta = json.loads((task / "meta.json").read_text())
        meta["pane"] = pane
        (task / "meta.json").write_text(json.dumps(meta))

    def settle(self, task, state="waiting", result="RESULT_STATUS: complete\nDone.\n"):
        (task / "state.json").write_text(json.dumps({"state": state}))
        if result is not None:
            (task / "result.md").write_text(result)

    def live_task(self, state="waiting", result="RESULT_STATUS: complete\nDone.\n", workdir=None, role="coder"):
        item, task = self.dispatch(workdir, role)
        pane, pid = self.pane()
        self.attach(task, pane)
        self.settle(task, state, result)
        return item, task, pane, pid

    # --- T3: collect ends the process --------------------------------------------------------

    def test_collect_ends_the_process_and_stays_completed(self):
        item, task, pane, pid = self.live_task()
        self.assertTrue(self.alive(pane))
        collected = self.cli("collect", item["id"])
        self.assertEqual(collected["status"], "completed")
        self.assertTrue(collected["process"]["ended"])
        self.assertFalse(self.alive(pane))
        self.assertEqual((task / "exit").read_text().strip(), "0")
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        # the same record, read again with the pane dead, is still a completed result and not a failure
        self.assertEqual(self.cli("inspect", item["id"])["status"], "completed")
        self.assertIn("Done.", (task / "result.md").read_text())

    def test_collect_of_a_paused_or_working_task_leaves_the_process(self):
        paused, _, pane_p, _ = self.live_task(result="No result status here.\n")
        self.assertEqual(self.cli("collect", paused["id"]).get("status"), "paused")
        working, _, pane_w, _ = self.live_task(state="working", result=None)
        self.cli("collect", working["id"])
        self.assertTrue(self.alive(pane_p))
        self.assertTrue(self.alive(pane_w))

    def test_collect_with_no_live_process_reports_that(self):
        item, task = self.dispatch()
        self.settle(task)  # pane is the stub's %9999: not a pane on the server
        self.assertEqual(self.cli("collect", item["id"])["process"], {"ended": False, "reason": "no live process"})

    # --- T3: retry and review refuse while the tree has a live process ------------------------

    def test_retry_refuses_while_the_previous_attempt_is_alive_naming_it(self):
        item, task, pane, pid = self.live_task()
        run = self.raw("retry", item["id"], "--reason", "Again", "--worker", "pi", "--model", MODEL)
        self.assertEqual(run.returncode, 1)
        self.assertIn(item["id"], run.stderr)
        self.assertIn(pane, run.stderr)
        self.assertIn(str(pid), run.stderr)
        self.assertIn("--force-alive", run.stderr)
        self.assertEqual(len(list((self.state / "tasks").iterdir())), 1, "a refused retry starts nothing")

    def test_retry_with_force_alive_proceeds_and_so_does_retry_after_collect(self):
        item, task, pane, _ = self.live_task()
        forced = self.cli("retry", item["id"], "--reason", "Again", "--worker", "pi", "--model", MODEL, "--force-alive")
        self.assertEqual(forced["parent"], item["id"])
        self.assertTrue(self.alive(pane))
        self.cli("collect", item["id"])
        later = self.cli("retry", item["id"], "--reason", "Once more", "--worker", "pi", "--model", MODEL)
        self.assertEqual(later["parent"], item["id"])

    def test_retry_refuses_for_a_second_worker_alive_on_the_same_tree_but_not_another_tree(self):
        item, task = self.dispatch()
        self.settle(task)
        self.cli("collect", item["id"])
        _, _, pane, _ = self.live_task()  # another task, same tree, alive
        run = self.raw("retry", item["id"], "--reason", "Again", "--worker", "pi", "--model", MODEL)
        self.assertEqual(run.returncode, 1)
        self.assertIn(pane, run.stderr)
        self.tmux("kill-pane", "-t", pane)
        # alive, but on a different working tree: not a conflict
        self.live_task(workdir=self.other)
        self.assertEqual(self.cli("retry", item["id"], "--reason", "Again", "--worker", "pi", "--model", MODEL)["parent"], item["id"])

    def test_retry_refuses_when_the_working_tree_is_not_recorded(self):
        item, task = self.dispatch()
        self.settle(task)
        meta = json.loads((task / "meta.json").read_text())
        del meta["workdir"]
        (task / "meta.json").write_text(json.dumps(meta))
        run = self.raw("retry", item["id"], "--reason", "Again", "--worker", "pi", "--model", MODEL)
        self.assertEqual(run.returncode, 1)
        self.assertIn("no recorded working tree", run.stderr)

    def review_cmds(self, *flags):
        return ([str(ROOT / "bin/tp-agent"), "review", self.item_id, *flags], [str(ROOT / "bin/tp-review"), *flags, self.item_id])

    def run_cmd(self, command, env=None):
        return subprocess.run(command, env=env or self.env, text=True, capture_output=True, timeout=90)

    def test_review_refuses_beside_a_second_live_writer_but_not_beside_a_live_reviewer(self):
        # Both entry points: `tp-agent review` and the bin/tp-review that subagents.mjs calls directly.
        item, task, own_pane, _ = self.live_task()
        self.item_id = item["id"]
        for command in self.review_cmds():
            with self.subTest(command=command[0]):
                _, _, writer_pane, _ = self.live_task()
                run = self.run_cmd(command)
                self.assertEqual(run.returncode, 1, run.stdout)
                self.assertIn("a writer is alive", run.stderr)
                self.assertIn(writer_pane, run.stderr)
                self.assertNotIn(own_pane, run.stderr)  # the author was ended first, so it is not the conflict
                self.tmux("kill-pane", "-t", writer_pane)
        # a live reviewer on the tree is not a writer: the guard does not name it
        self.live_task(role="code-reviewer")
        self.assertNotIn("a writer is alive", self.run_cmd(self.review_cmds()[1]).stderr)
        # --force-alive passes with a writer alive, and leaves the author alone too
        item2, _, author2, _ = self.live_task()
        self.item_id = item2["id"]
        _, _, writer_pane, _ = self.live_task()
        for command in self.review_cmds("--force-alive"):
            run = self.run_cmd(command)
            self.assertNotIn("a writer is alive", run.stderr)
            self.assertNotIn("refused", run.stderr)
        self.assertTrue(self.alive(writer_pane))
        self.assertTrue(self.alive(author2), "--force-alive reviews beside the author without ending it")

    def test_a_live_author_never_runs_beside_its_reviewer_it_is_ended_first_and_stays_collectable(self):
        item, task, pane, pid = self.live_task()
        self.item_id = item["id"]
        run = self.run_cmd(self.review_cmds()[0])
        self.assertNotIn("a writer is alive", run.stderr)
        self.assertNotIn("refused", run.stderr)
        self.assertFalse(self.alive(pane), "the settled author's process is ended before the reviewer can start")
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        # lifecycle unchanged: still settled, not collected, pane (scrollback) kept
        self.assertEqual(self.cli("inspect", item["id"])["status"], "settled")
        self.assertFalse((task / "collected.json").exists())
        self.assertIn(pane, self.tmux("list-panes", "-a", "-F", "#{pane_id}"))
        # a message to it refuses clearly; a retry of it still works; collect after the review still completes it
        run = self.raw("message", item["id"], "--text", "hello")
        self.assertEqual(run.returncode, 1)
        self.assertIn("process was ended", run.stderr)
        self.assertEqual(self.cli("collect", item["id"])["status"], "completed")
        self.assertEqual(self.cli("retry", item["id"], "--reason", "Again", "--worker", "pi", "--model", MODEL)["parent"], item["id"])

    def test_review_refuses_and_leaves_the_author_when_its_process_cannot_be_ended_or_checked(self):
        item, task, pane, _ = self.live_task()
        self.item_id = item["id"]
        env = self.odd_rows_env()
        for command in self.review_cmds():
            run = self.run_cmd(command, env)
            self.assertEqual(run.returncode, 1, run.stdout)
            self.assertIn("cannot read tmux panes", run.stderr)
            self.assertTrue(self.alive(pane))
        # a paused author is refused as before, and its process is not touched
        paused, _, ppane, _ = self.live_task(result="No result status here.\n")
        self.item_id = paused["id"]
        for command in self.review_cmds():
            self.assertEqual(self.run_cmd(command).returncode, 1)
            self.assertTrue(self.alive(ppane))

    def test_collect_that_cannot_end_the_process_fails_leaves_the_task_settled_and_a_later_collect_recovers(self):
        env = self.odd_rows_env()
        for name, command in (("tp-agent", [str(ROOT / "bin/tp-agent"), "collect"]), ("tp-contract", [str(ROOT / "bin/tp-contract"), "collect"])):
            with self.subTest(name):
                item, task, pane, _ = self.live_task()
                run = self.run_cmd([*command, item["id"]], env)
                self.assertEqual(run.returncode, 1, run.stdout)
                self.assertIn("cannot read tmux panes", run.stderr)
                self.assertTrue(self.alive(pane))
                self.assertFalse((task / "collected.json").exists() or (task / "completed").exists(), "no completed marker before the process is gone")
                self.assertEqual(self.cli("inspect", item["id"])["status"], "settled")
                run = self.run_cmd([*command, item["id"]])
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertFalse(self.alive(pane))
                self.assertEqual(self.cli("inspect", item["id"])["status"], "completed")

    def test_collecting_an_already_completed_task_retries_the_cleanup(self):
        for name, command in (("tp-agent", [str(ROOT / "bin/tp-agent"), "collect"]), ("tp-contract", [str(ROOT / "bin/tp-contract"), "collect"])):
            with self.subTest(name):
                item, task, pane, _ = self.live_task()
                (task / "collected.json").write_text(json.dumps({"collected": "earlier"}))  # marked by a version that did not end it
                self.assertEqual(self.cli("inspect", item["id"])["status"], "completed")
                run = self.run_cmd([*command, item["id"]])
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertFalse(self.alive(pane))

    def mcp(self, name, **arguments):
        """One tool call against the real claude/mcp/subagents.mjs (the legacy Claude-main path), on the private server."""
        self.tmux("set-option", "-g", "@tp_state_dir", str(self.state))
        calls = [{"id": 1, "method": "initialize", "params": {}}, {"method": "notifications/initialized"},
                 {"id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}}]
        env = {k: v for k, v in self.env.items() if k not in {"TP_MODE", "TP_TASK_DIR"}}
        env.update(TMUX="/nonexistent/sock,1,0", TP_MAIN="claude")
        run = subprocess.run([shutil.which("node"), str(ROOT / "claude/mcp/subagents.mjs")], env=env, cwd=ROOT, text=True, timeout=90,
                             input="".join(json.dumps({"jsonrpc": "2.0", **c}) + "\n" for c in calls), capture_output=True)
        reply = {m["id"]: m for m in map(json.loads, run.stdout.splitlines())}[2]["result"]
        return reply["isError"] if "isError" in reply else False, reply["content"][0]["text"]

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_mcp_entry_points_end_the_process_show_the_report_and_guard_the_review(self):
        item, task, pane, _ = self.live_task()
        (task / "report.md").write_text("twelve bytes")
        _, writer_task, writer_pane, _ = self.live_task()
        failed, text = self.mcp("review_subagent", id=item["id"])
        self.assertTrue(failed)
        self.assertIn("a writer is alive", text)
        self.assertTrue(self.alive(writer_pane))
        failed, text = self.mcp("collect_result", id=item["id"])
        self.assertFalse(failed, text)
        self.assertIn(f"report_path: {task / 'report.md'}", text)
        self.assertIn("report_bytes: 12", text)
        self.assertFalse(self.alive(pane))
        self.assertTrue(self.alive(writer_pane))
        self.assertTrue((task / "collected.json").exists() or (task / "completed").exists())

    def odd_rows_env(self):
        """A tmux whose pane list succeeds but carries one row that is not a pane row."""
        odd = self.root / "odd"
        odd.mkdir(exist_ok=True)
        (odd / "tmux").write_text(f"#!/bin/sh\nif [ \"$1\" = list-panes ]; then {TMUX} -L {self.sock} \"$@\"; echo garbage here; "
                                  f"else exec {self.env['PATH'].split(os.pathsep)[0]}/tmux \"$@\"; fi\n")
        (odd / "tmux").chmod(0o755)
        return {**self.env, "PATH": f"{odd}{os.pathsep}{self.env['PATH']}"}

    def test_tp_contract_collect_ends_the_process_and_refuses_when_it_cannot_look(self):
        item, task, pane, pid = self.live_task()
        run = subprocess.run([str(ROOT / "bin/tp-contract"), "collect", item["id"]], env=self.env, text=True, capture_output=True, timeout=60)
        self.assertEqual((run.returncode, run.stdout.strip()), (0, "completed"), run.stderr)
        self.assertFalse(self.alive(pane))
        self.assertEqual((task / "exit").read_text().strip(), "0")
        # a settled task whose pane list has a row that cannot be read is collected, but the command fails, naming why
        other, other_task, other_pane, _ = self.live_task()
        env = self.odd_rows_env()
        run = subprocess.run([str(ROOT / "bin/tp-contract"), "collect", other["id"]], env=env, text=True, capture_output=True, timeout=60)
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn("could not be ended", run.stderr)
        self.assertTrue(self.alive(other_pane))

    # Every reader the guard relies on, on a value it cannot read: each refuses (override --force-alive).
    def retry(self, item, *extra):
        return self.raw("retry", item["id"], "--reason", "Again", "--worker", "pi", "--model", MODEL, *extra)

    def test_a_live_task_with_an_unusable_record_refuses_retry_and_review_alike(self):
        item, task = self.dispatch()
        self.settle(task)
        other, other_task, pane, _ = self.live_task()
        original = (other_task / "meta.json").read_text()
        meta = json.loads(original)
        cases = {
            "missing workdir": lambda m: m.pop("workdir"),
            "workdir not a string": lambda m: m.update(workdir=7),
            "empty workdir": lambda m: m.update(workdir=""),
        }
        for label, damage in cases.items():
            with self.subTest(label):
                damaged = json.loads(original)
                damage(damaged)
                (other_task / "meta.json").write_text(json.dumps(damaged))
                for run in (self.retry(item), self.raw("review", item["id"]),
                            subprocess.run([str(ROOT / "bin/tp-review"), item["id"]], env=self.env, text=True, capture_output=True)):
                    self.assertEqual(run.returncode, 1, label)
                    self.assertIn(other["id"], run.stderr)
                    self.assertIn("working tree", run.stderr)
        for label, text in (("malformed json", "{not json"), ("not an object", "[1]"), ("empty file", "")):
            with self.subTest(label):
                (other_task / "meta.json").write_text(text)
                for run in (self.retry(item), self.raw("review", item["id"])):
                    self.assertEqual(run.returncode, 1, label)
                    self.assertIn(other["id"], run.stderr)
                    self.assertIn("meta.json", run.stderr)
        (other_task / "meta.json").unlink()  # a task directory with no record at all
        self.assertEqual(self.retry(item).returncode, 1)
        (other_task / "meta.json").write_text(original)
        meta["pane"] = 7
        (other_task / "meta.json").write_text(json.dumps(meta))
        run = self.retry(item)
        self.assertEqual(run.returncode, 1)
        self.assertIn("unreadable pane", run.stderr)
        self.assertTrue(self.alive(pane))
        # the override is the only way past: the same damage with --force-alive starts the retry
        self.assertEqual(self.retry(item, "--force-alive").returncode, 0)

    def test_a_dead_or_queued_task_with_no_workdir_does_not_block(self):
        item, task = self.dispatch()
        self.settle(task)
        other, other_task = self.dispatch()  # stub pane %9999 is not on the server
        meta = json.loads((other_task / "meta.json").read_text())
        meta.pop("workdir")
        (other_task / "meta.json").write_text(json.dumps(meta))
        self.assertEqual(self.retry(item).returncode, 0)

    def test_a_malformed_row_in_a_successful_pane_list_refuses(self):
        item, task = self.dispatch()
        self.settle(task)
        env = self.odd_rows_env()
        for command in (["retry", item["id"], "--reason", "x", "--worker", "pi", "--model", MODEL], ["review", item["id"]], ["reap"]):
            run = subprocess.run([str(ROOT / "bin/tp-agent"), *command], env=env, text=True, capture_output=True)
            self.assertEqual(run.returncode, 1, command)
            self.assertIn("unexpected row", run.stderr)

    def test_unreadable_pane_table_refuses(self):
        item, task = self.dispatch()
        self.settle(task)
        broken = self.root / "broken"
        broken.mkdir()
        (broken / "tmux").write_text("#!/bin/sh\nexit 1\n")
        (broken / "tmux").chmod(0o755)
        env = {**self.env, "PATH": f"{broken}{os.pathsep}{self.env['PATH']}"}
        run = subprocess.run([str(ROOT / "bin/tp-agent"), "retry", item["id"], "--reason", "x", "--worker", "pi", "--model", MODEL],
                             env=env, text=True, capture_output=True)
        self.assertEqual(run.returncode, 1)
        self.assertIn("cannot read tmux panes", run.stderr)

    # --- T3: reap ----------------------------------------------------------------------------

    def test_reap_lists_without_yes_and_ends_only_finished_tasks_with_it(self):
        done, done_task, done_pane, _ = self.live_task()
        self.cli("collect", done["id"])
        self.settle(done_task)  # collected, then its process was somehow still around: put one back
        again, again_task, again_pane, _ = self.live_task()
        self.cli("collect", again["id"])  # ends its own process; reuse a fresh one below
        revived_pane, _ = self.pane()
        self.attach(again_task, revived_pane)
        working, _, working_pane, _ = self.live_task(state="working", result=None)
        waiting, _, waiting_pane, _ = self.live_task()  # settled but not collected
        listed = self.cli("reap")
        self.assertFalse(listed["confirmed"])
        self.assertEqual({row["id"] for row in listed["tasks"]}, {again["id"]})
        self.assertTrue(self.alive(revived_pane), "nothing is ended without --yes")
        ended = self.cli("reap", "--yes")
        self.assertTrue(ended["tasks"][0]["ended"])
        self.assertFalse(self.alive(revived_pane))
        self.assertTrue(self.alive(working_pane), "a task recorded as working is never reaped")
        self.assertTrue(self.alive(waiting_pane), "a settled but uncollected task is not finished")

    def test_reap_with_an_unknown_id_refuses(self):
        run = self.raw("reap", "nope")
        self.assertEqual(run.returncode, 1)
        self.assertIn("unknown task: nope", run.stderr)

    def test_reap_never_ends_a_task_that_resumed_work(self):
        item, task, pane, _ = self.live_task()
        self.cli("collect", item["id"])
        revived, _ = self.pane()
        self.attach(task, revived)
        (task / "exit").unlink()  # as if a live worker took the place of the ended one
        self.settle(task, state="working", result=None)  # the collected worker went back to work
        self.assertEqual(self.cli("reap", "--yes")["tasks"], [])
        self.assertTrue(self.alive(revived))

    def test_message_to_a_worker_ended_at_collect_refuses(self):
        item, task, pane, _ = self.live_task()
        self.cli("collect", item["id"])
        run = self.raw("message", item["id"], "--text", "hello")
        self.assertEqual(run.returncode, 1)
        self.assertIn("process was ended", run.stderr)

    # --- T3: stop ----------------------------------------------------------------------------

    def test_stop_ends_a_process_that_ignores_hangup(self):
        item, task = self.dispatch()
        pane, pid = self.pane("trap '' HUP TERM; while true; do sleep 1; done")
        self.attach(task, pane)
        time.sleep(0.5)  # let the trap install
        self.assertEqual(self.cli("stop", item["id"])["status"], "stopped")
        self.assertFalse(self.alive(pane))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    # --- T3: a stop that cannot read or end the process ----------------------------------------

    HUP_PROOF = "trap '' HUP TERM; while true; do sleep 1; done"

    def gone(self, pid):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.1)
        return False

    def stopper(self):
        item, task = self.dispatch()
        pane, pid = self.pane(self.HUP_PROOF)
        self.attach(task, pane)
        time.sleep(0.5)
        return item, task, pane, pid

    def assertUntouched(self, task, pane, pid, run):
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertTrue(self.alive(pane))
        os.kill(pid, 0)
        self.assertFalse((task / "stopped.json").exists())
        self.assertFalse((task / "process.json").exists())

    def test_stop_refuses_when_the_pane_pid_cannot_be_read_and_changes_nothing(self):
        item, task, pane, pid = self.stopper()
        (self.state / "list-fail").write_text("")
        run = self.raw("stop", item["id"])
        (self.state / "list-fail").unlink()
        self.assertIn("cannot read tmux panes", run.stderr)
        self.assertUntouched(task, pane, pid, run)

    def test_stop_refuses_an_invalid_pid_and_changes_nothing(self):
        item, task, pane, pid = self.stopper()
        for bad in ("0", "1", "abc", str(os.getpgrp())):
            (self.state / "list-rows").write_text(f"{pane} 0 {bad}\n")
            run = self.raw("stop", item["id"])
            self.assertUntouched(task, pane, pid, run)
            self.assertIn("tp-kill", run.stderr, bad)
        (self.state / "list-rows").unlink()

    def test_stop_that_cannot_end_the_group_fails_naming_it_keeps_the_record_and_a_second_stop_recovers(self):
        item, task, pane, pid = self.stopper()
        start = subprocess.run(["/bin/ps", "-o", "lstart=", "-p", str(pid)], text=True, capture_output=True).stdout.strip()
        (self.state / "ghost-ps").write_text(f"{pid} {pid} S {start}\n")
        run = self.raw("stop", item["id"])
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn(f"pid {pid}, process group {pid}", run.stderr)
        self.assertIn("survived SIGKILL", run.stderr)
        self.assertFalse((task / "stopped.json").exists(), "stopped is not recorded while the process may live")
        self.assertEqual(json.loads((task / "process.json").read_text())["pgid"], pid)
        self.assertFalse(self.alive(pane))
        self.assertTrue(self.gone(pid), "the real group was ended; only the table lied")
        (self.state / "ghost-ps").unlink()
        self.assertEqual(self.cli("stop", item["id"])["status"], "stopped")
        self.assertTrue((task / "stopped.json").exists())
        self.assertFalse((task / "process.json").exists())

    def orphan(self, task, start_override=None):
        """A HUP- and TERM-proof process group outside tmux, recorded as a failed stop would have, with its pane gone."""
        proc = subprocess.Popen(["sh", "-c", self.HUP_PROOF], start_new_session=True)
        self.addCleanup(lambda: (proc.poll() is None) and (os.killpg(proc.pid, signal.SIGKILL), proc.wait()))
        time.sleep(0.5)
        start = start_override or subprocess.run(["/bin/ps", "-o", "lstart=", "-p", str(proc.pid)], text=True,
                                                 capture_output=True).stdout.split()
        start = start if isinstance(start, str) else " ".join(start)
        (task / "process.json").write_text(json.dumps({"pane": "%9999", "pid": proc.pid, "pgid": proc.pid, "group": True,
                                                       "members": [[proc.pid, start]], "recorded": "x"}))
        return proc

    def test_reap_finds_an_orphan_whose_pane_is_gone_and_ends_it_only_with_yes(self):
        item, task = self.dispatch()
        proc = self.orphan(task)
        listed = self.cli("reap")
        self.assertEqual([(r["id"], r["pid"], r["state"]) for r in listed["tasks"]], [(item["id"], proc.pid, "ours")])
        self.assertIsNone(proc.poll())
        ended = self.cli("reap", "--yes")
        self.assertTrue(ended["tasks"][0]["ended"])
        proc.wait(timeout=10)
        self.assertFalse((task / "process.json").exists())
        self.assertEqual(self.cli("reap", "--yes")["tasks"], [])

    def test_reap_never_signals_a_process_it_cannot_show_is_the_worker(self):
        item, task = self.dispatch()
        proc = self.orphan(task, start_override="Mon Jan  1 00:00:00 2001")  # as a recycled pid would differ
        self.assertEqual(self.cli("reap")["tasks"][0]["state"], "unknown")
        run = self.raw("reap", "--yes")
        self.assertEqual(run.returncode, 1)
        self.assertIn(f"pid {proc.pid}", run.stderr)
        self.assertIn("cannot tell", run.stderr)
        self.assertIsNone(proc.poll(), "an unverifiable process is never signalled")
        self.assertTrue((task / "process.json").exists())

    def test_reap_that_cannot_end_an_orphan_fails_naming_it_and_a_later_reap_retries(self):
        item, task = self.dispatch()
        proc = self.orphan(task)
        start = json.loads((task / "process.json").read_text())["members"][0][1]
        (self.state / "ghost-ps").write_text(f"{proc.pid} {proc.pid} S {start}\n")
        run = self.raw("reap", "--yes")
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn(f"pid {proc.pid}", run.stderr)
        self.assertIn("survived SIGKILL", run.stderr)
        self.assertTrue((task / "process.json").exists())
        (self.state / "ghost-ps").unlink()
        self.assertEqual(self.raw("reap", "--yes").returncode, 0)
        self.assertFalse((task / "process.json").exists())

    def test_stop_of_a_task_with_no_pane_stops_normally(self):
        item, task = self.dispatch()
        meta = json.loads((task / "meta.json").read_text())
        meta.pop("pane", None)
        (task / "meta.json").write_text(json.dumps(meta))
        self.assertEqual(self.cli("stop", item["id"])["status"], "stopped")
        self.assertTrue((task / "stopped.json").exists())
        item2, task2 = self.dispatch()
        self.attach(task2, "%424242")  # a pane that no longer exists
        self.assertEqual(self.cli("stop", item2["id"])["status"], "stopped")

    # --- T5: the report ----------------------------------------------------------------------

    def test_packet_names_report_md_and_collect_shows_its_path_and_size(self):
        item, task = self.dispatch()
        self.assertIn(f"{task}/report.md", (task / "packet.md").read_text())
        self.settle(task)
        (task / "report.md").write_text("# Report\nfindings\n")
        self.cli("collect", item["id"])
        self.assertEqual(self.cli("collect", item["id"])["report_path"], str(task / "report.md"))
        self.assertEqual(self.cli("inspect", item["id"])["report_bytes"], len("# Report\nfindings\n"))

    def test_the_final_reply_does_not_touch_report_md_and_result_md_stays_the_reply(self):
        item, task = self.dispatch()
        (task / "report.md").write_text("kept\n")
        self.settle(task, result="RESULT_STATUS: complete\nLast reply.\n")
        record = self.cli("inspect", item["id"])
        self.assertEqual((task / "report.md").read_text(), "kept\n")
        self.assertEqual(record["result"], "RESULT_STATUS: complete\nLast reply.\n")
        self.assertEqual(record["result_status"], "complete")

    def test_no_report_means_no_report_fields_set(self):
        item, task = self.dispatch()
        record = self.cli("inspect", item["id"])
        self.assertIsNone(record["report_path"])
        self.assertIsNone(record["report_bytes"])


if __name__ == "__main__":
    unittest.main()
