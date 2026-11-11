"""Shared task interface and sensitive-read boundary without launching models."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import unittest
import sys

ROOT = Path(__file__).resolve().parent.parent
MODEL = "openai/gpt-6.1-sol"
WAITING = '{"state":"waiting"}'
# SG1's read/search rule, word for word; it reaches every worker inside the worker contract (agent/contract/worker.md).
_SAFE_SEARCH_RULE = ("Search file contents with {grep} (pattern, path). Read a file with {read} or "
                     "{tool} on an exact path. Use grep, rg, cat, head, tail or sed -n through {shell} only on exact paths in "
                     "the working tree that are known not to be sensitive (no .env files, keys or credentials, nothing under "
                     "agent/, config/ or claude/): in a worker task a shell search or read that may reach a sensitive file is "
                     "refused.")
SAFE_SEARCH_RULE_PI = _SAFE_SEARCH_RULE.format(grep="safe_grep", read="safe_read", tool="the read tool", shell="bash")
SAFE_SEARCH_RULE_CLAUDE = _SAFE_SEARCH_RULE.format(grep="mcp__tmux-pi-safe__safe_grep", read="mcp__tmux-pi-safe__safe_read",
                                                   tool="the Read tool", shell="Bash")
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_tools import fake_tmux  # noqa: E402
import preamble  # noqa: E402
from sensitive_read import classify, hook_response
CONTRACT = {harness: preamble.contract(harness) for harness in ("pi", "claude")}
from session_env import isolate_session, outside_session  # noqa: E402


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.state = self.project / ".tmux-pi"
        (self.state / "tasks").mkdir(parents=True)
        fake = self.root / "bin"
        fake.mkdir()
        fake_tmux(fake, '''case "$1" in
 has-session) exit 0 ;;
 show-options) printf '%s\\n' "$TP_STATE_DIR" ;;
 new-window) printf '%%fake\\n' ;;
 send-keys) exit 0 ;;
 *) exit 0 ;;
esac
''')
        isolate_session(self)  # the in-process load of bin/tp-agent reads os.environ too
        self.env = {**outside_session(), "TP_STATE_DIR": str(self.state), "TP_SESSION": "fixture", "TP_WORK_DIR": str(self.root / "work-store"),
                    "PATH": str(fake) + os.pathsep + os.environ["PATH"]}

    def raw(self, *args, env=None, timeout=25):
        return subprocess.run([str(ROOT / "bin/tp-agent"), *args], env=env or self.env, text=True,
                              capture_output=True, timeout=timeout)

    def cli(self, *args):
        run = self.raw(*args)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def task_dir(self, item):
        return self.state / "tasks" / item["id"]

    def packet(self, item):
        return (self.task_dir(item) / "packet.md").read_text()

    def system_prompt(self, item_or_dir):
        task = item_or_dir if isinstance(item_or_dir, Path) else self.task_dir(item_or_dir)
        return (task / "system-prompt.md").read_text() if (task / "system-prompt.md").exists() else ""

    def settle(self, item, result=None):
        task = self.task_dir(item)
        (task / "state.json").write_text(WAITING)
        if result is not None:
            (task / "result.md").write_text(result)
        return task

    def retry(self, item, reason="Again", *extra):
        return self.cli("retry", item["id"], "--reason", reason, "--worker", "pi", "--model", MODEL, *extra)

    def retry_packet(self, item, *extra):
        return self.packet(self.retry(item, "Again", *extra))

    def dispatch(self, role="coder", task="Make a focused change", **extra):
        args = ["dispatch", "--role", role, "--task", task, "--worker", "pi",
                "--model", MODEL, "--thinking", "medium",
                "--workdir", str(self.project), "--deliverable", "Report paths and checks"]
        for key, value in extra.items():
            args += ["--" + key.replace("_", "-"), value]
        return self.cli(*args)

    def test_role_packet_record_collect_and_index(self):
        item = self.dispatch()
        self.assertEqual(item["role"], "coder")
        self.assertEqual(item["status"], "starting")
        task = self.task_dir(item)
        packet = self.packet(item)
        self.assertIn("# Coder", packet)
        # The RESULT_STATUS rule is the contract's, in the system prompt; the pi packet does not repeat it.
        self.assertIn("RESULT_STATUS: complete|empty|blocked|incomplete", self.system_prompt(item))
        self.assertNotIn("RESULT_STATUS", packet)
        self.assertEqual(json.loads((task / "meta.json").read_text())["deliverable"], "Report paths and checks")
        self.settle(item, "RESULT_STATUS: complete\nVerified one path.\n")
        collected = self.cli("collect", item["id"])
        self.assertEqual(collected["status"], "completed")
        self.assertEqual(collected["result_status"], "complete")
        self.assertIn("Verified one path", collected["result"])
        index = self.cli("index")
        self.assertEqual(index["recent"][-1]["id"], item["id"])
        self.assertEqual(index["recent"][-1]["status"], "completed")

    def write_preamble(self, text):
        (self.state / "worker-preamble.md").write_text(text)

    def test_preamble_reaches_dispatch_retry_and_direct_spawn(self):
        self.write_preamble("Write your report to report.md.\n\n## Role: coder\nCoder-only rule.\n\n## Role: tester\nTester-only rule.\n")
        first = self.dispatch()
        prompt = self.system_prompt(first)
        self.assertIn("Write your report to report.md.", prompt)
        self.assertIn("Coder-only rule.", prompt)
        self.assertNotIn("Tester-only rule.", prompt)
        # delivered through the system-prompt channel, not the user packet
        self.assertNotIn("Write your report to report.md.", self.packet(first))
        self.assertIn("--append-system-prompt", (self.task_dir(first) / "run.sh").read_text())
        self.settle(first)
        again = self.retry(first)
        self.assertIn("Write your report to report.md.", self.system_prompt(again))
        spawned = subprocess.run([str(ROOT / "bin/tp-spawn"), "--worker", "pi", "--model", MODEL, "--workdir",
                                  str(self.project), "--", "Review it"], env=self.env, text=True, capture_output=True)
        self.assertEqual(spawned.returncode, 0, spawned.stderr)
        text = self.system_prompt(self.state / "tasks" / spawned.stdout.strip())
        self.assertIn("Write your report to report.md.", text)
        self.assertNotIn("Coder-only rule.", text)

    def test_claude_worker_gets_the_preamble_in_its_system_prompt_too(self):
        self.write_preamble("General rule.")
        spawned = subprocess.run([str(ROOT / "bin/tp-spawn"), "--worker", "claude", "--model", "sonnet", "--workdir",
                                  str(self.project), "--", "t"], env=self.env, text=True, capture_output=True)
        self.assertEqual(spawned.returncode, 0, spawned.stderr)
        task = self.state / "tasks" / spawned.stdout.strip()
        self.assertIn("General rule.", self.system_prompt(task))
        self.assertIn("--append-system-prompt", (task / "run.sh").read_text())
        self.assertNotIn("General rule.", (task / "packet.md").read_text())

    def test_system_prompt_prefix_is_stable_across_tasks_and_roles(self):
        general = "General rule one.\n\nGeneral rule two."
        self.write_preamble(general + "\n\n## Role: coder\nCoder-only rule.\n\n## Role: tester\nTester-only rule.\n")
        a = self.system_prompt(self.dispatch(role="coder", task="Fix the parser"))
        b = self.system_prompt(self.dispatch(role="tester", task="Check the importer", tests="full"))
        c = self.system_prompt(self.dispatch(role="researcher", task="Survey options"))
        head = CONTRACT["pi"] + "\n\n# Repository Instructions\n\n" + general   # the contract first, then the repository's text
        for prompt in (a, b, c):
            self.assertTrue(prompt.startswith(head), prompt)
        self.assertEqual(a, head + "\n\nCoder-only rule.")
        self.assertEqual(b, head + "\n\nTester-only rule.")
        self.assertEqual(c, head)  # no role section
        # nothing per task inside the prompt
        for prompt in (a, b, c):
            for word in ("Fix the parser", "Check the importer", "coder-", "tester-", "researcher-", str(self.project), "20"):
                self.assertNotIn(word, prompt)

    def test_real_tp_review_packet_gets_reviewer_section_only(self):
        self.write_preamble("Global rule.\n\n## Role: code-reviewer\nReviewer-only rule.\n\n## Role: coder\nCoder-only rule.\n")
        item = self.dispatch()
        self.settle(item, "RESULT_STATUS: complete\nDone.\n")
        run = self.raw("review", item["id"], "--force")
        self.assertEqual(run.returncode, 0, run.stderr)
        reviews = [d for d in (self.state / "tasks").iterdir() if (d / "review-of").exists()]
        self.assertEqual(len(reviews), 1)
        text = self.system_prompt(reviews[0])
        self.assertIn("Global rule.", text)
        self.assertIn("Reviewer-only rule.", text)
        self.assertNotIn("Coder-only rule.", text)
        head = (reviews[0] / "packet.md").read_text().split("# Context")[0]
        # The reviewer gets its role's method (agent/roles/code-reviewer.md) like any role, while meta.json records no role:
        # the autonomy gate admits a review start only without one (lib/autonomy.py).
        self.assertEqual(head.count("# Worker Role\n\n# Code reviewer\n"), 1)
        self.assertNotIn("role", json.loads((reviews[0] / "meta.json").read_text()))
        self.assertNotIn("Global rule.", head)

    def test_dispatch_without_workdir_validates_the_main_pane_workdir(self):
        fake = self.root / "bin2"
        fake.mkdir()
        fake_tmux(fake, f'''case "$1" in
 has-session) exit 0 ;;
 show-options) case "$4$5" in *@tp_main_pane*) echo %main ;; *) echo "$TP_STATE_DIR" ;; esac ;;
 display-message) echo "{self.project}" ;;
 *) exit 0 ;;
esac
''')
        env = {**self.env, "PATH": str(fake) + os.pathsep + self.env["PATH"]}
        args = ("dispatch", "--role", "coder", "--task", "t", "--worker", "pi", "--model", MODEL, "--thinking", "low")
        (self.state / "worker-preamble.md").write_text("x" * 8001)
        run = self.raw(*args, env=env)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("8001 bytes", run.stderr)
        self.assertEqual(list((self.state / "tasks").iterdir()), [])
        self.assertFalse((self.state / "queue").exists() and list((self.state / "queue").iterdir()))
        (self.state / "worker-preamble.md").write_text("Fine.")
        self.assertEqual(self.raw(*args, env=env).returncode, 0)

    def test_queued_dispatch_without_workdir_keeps_the_validated_workdir(self):
        other = self.root / "other"
        (other / ".tmux-pi").mkdir(parents=True)
        (other / ".tmux-pi" / "worker-preamble.md").write_text("OTHER_REPO_RULE")
        (self.project / ".tmux-pi" / "worker-preamble.md").write_text("PROJECT_RULE")
        pane = self.root / "pane"
        pane.write_text(str(self.project))
        fake = self.root / "bin2"
        fake.mkdir()
        tmux = fake / "tmux"
        tmux.write_text(f'''#!/bin/sh
case "$1" in
 has-session) exit 0 ;;
 show-options) case "$4$5" in *@tp_main_pane*) echo %main ;; *) echo "$TP_STATE_DIR" ;; esac ;;
 display-message) cat "{pane}" ;;
 new-window) echo %fake ;;
 *) exit 0 ;;
esac
''')
        tmux.chmod(0o755)
        env = {**self.env, "PATH": str(fake) + os.pathsep + self.env["PATH"]}
        first = json.loads(self.raw("dispatch", "--role", "researcher", "--task", "a", "--worker", "pi", "--model", MODEL,
                                    "--thinking", "low", "--workdir", str(self.project), env=env).stdout)
        queued = json.loads(self.raw("dispatch", "--role", "coder", "--task", "b", "--worker", "pi", "--model", MODEL,
                                     "--thinking", "low", "--after", first["id"], env=env).stdout)
        self.assertEqual(queued["status"], "queued")
        record = json.loads((self.state / "queue" / f"{queued['id']}.json").read_text())
        self.assertEqual(record["workdir"], str(self.project.resolve()))
        pane.write_text(str(other))  # the main pane moves before the queue drains
        self.settle(first, "RESULT_STATUS: complete\n")
        self.assertEqual(json.loads(self.raw("tick", env=env).stdout)["started"], [queued["id"]])
        text = self.system_prompt(self.state / "tasks" / queued["id"]) + (self.state / "tasks" / queued["id"] / "packet.md").read_text()
        self.assertIn("PROJECT_RULE", text)
        self.assertNotIn("OTHER_REPO_RULE", text)

    def test_queue_record_that_is_not_provably_valid_is_not_launched(self):
        first = self.dispatch()
        queued = self.dispatch(task="b", after=first["id"])
        path = self.state / "queue" / f"{queued['id']}.json"
        good = json.loads(path.read_text())
        self.settle(first, "RESULT_STATUS: complete\n")
        for label, change in (("no workdir", {"workdir": None}), ("relative workdir", {"workdir": "project"}),
                              ("bad tests", {"tests": "some"}), ("no task", {"task": ""})):
            with self.subTest(label):
                path.write_text(json.dumps({**good, **change}))
                self.assertEqual(self.cli("tick")["started"], [])
                self.assertIn("start refused", json.loads(path.read_text())["queue_reason"])
                self.assertFalse((self.state / "tasks" / queued["id"]).exists())

    def queued_pair(self):
        """A queued item waiting on a dependency, and its valid record; the dependency is then settled."""
        first = self.dispatch()
        queued = self.dispatch(task="b", after=first["id"])
        path = self.state / "queue" / f"{queued['id']}.json"
        good = json.loads(path.read_text())
        self.settle(first, "RESULT_STATUS: complete\n")
        return queued, path, good

    def test_queue_item_fields_any_reader_uses_are_all_checked(self):
        queued, path, good = self.queued_pair()
        changes = (("after has null", {"after": [None]}), ("after has an invalid id", {"after": ["../x"]}),
                   ("after is a string", {"after": "coder-1"}), ("context is a string", {"context": "spec.txt"}),
                   ("context has a number", {"context": ["a", 1]}), ("notes is a string", {"notes": "note"}),
                   ("notes is an object", {"notes": {}}), ("parent is a list", {"parent": []}),
                   ("deliverable is a number", {"deliverable": 3}), ("created is a number", {"created": 3}),
                   ("route_info is a list", {"route_info": []}),
                   ("tests is a list", {"tests": []}), ("unknown role", {"role": "wizard"}),
                   ("unknown worker", {"worker": "other"}), ("thinking missing", {"thinking": None}),
                   ("id does not match the file", {"id": "coder-0000000000"}))
        for label, change in changes:
            with self.subTest(label):
                path.write_text(json.dumps({**good, **change}))
                run = self.cli("tick")
                self.assertEqual(run["started"], [])
                self.assertEqual([entry["id"] for entry in run["refused"]], [queued["id"]])
                self.assertIn("start refused", json.loads(path.read_text())["queue_reason"])
                self.assertFalse((self.state / "tasks" / queued["id"]).exists())
                rows = {row["id"]: row for row in self.cli("index")["active"]}  # the index is still written
                self.assertEqual(rows[queued["id"]]["status"], "queued")
        path.write_text(json.dumps(good))  # the same record, restored, starts
        self.assertEqual(self.cli("tick")["started"], [queued["id"]])

    def test_one_bad_queue_entry_never_stops_the_others_or_the_index(self):
        first = self.dispatch()
        valid = self.dispatch(task="b", after=first["id"])
        queue = self.state / "queue"
        good = json.loads((queue / f"{valid['id']}.json").read_text())
        self.settle(first, "RESULT_STATUS: complete\n")
        entries = {"a-array": "[]", "b-string": '"x"', "c-null": "null", "d-number": "7", "e-broken": "{not json",
                   "f-after-null": json.dumps({**good, "id": "f-after-null", "after": [None]}),
                   "g-context-string": json.dumps({**good, "id": "g-context-string", "context": "spec.txt"})}
        for name, text in entries.items():
            (queue / f"{name}.json").write_text(text)
        self.assertEqual({row["id"] for row in self.cli("list") if row["status"] == "queued"},
                         {valid["id"], *entries})
        run = self.cli("tick")
        self.assertEqual(run["started"], [valid["id"]])
        self.assertEqual({entry["id"] for entry in run["refused"]}, set(entries))
        self.assertTrue((self.state / "session-index.json").is_file())
        self.assertEqual({row["id"] for row in self.cli("index")["active"] if row["status"] == "queued"}, set(entries))
        # unreadable and non-object entries are never rewritten; objects keep their data and gain the reason
        for name in ("a-array", "b-string", "c-null", "d-number", "e-broken"):
            self.assertEqual((queue / f"{name}.json").read_text(), entries[name])
        kept = json.loads((queue / "f-after-null.json").read_text())
        self.assertEqual((kept["task"], kept["after"]), (good["task"], [None]))
        self.assertIn("start refused", kept["queue_reason"])
        self.assertEqual(self.cli("tick")["started"], [])  # nothing left to start; still no abort
        for name in entries:  # a queued id can be stopped whatever its content
            self.assertEqual(self.cli("stop", name), {"id": name, "status": "stopped"})
        self.assertEqual(list(queue.iterdir()), [])

    def test_every_reader_refuses_every_invalid_queue_shape(self):
        """Invalid shapes (null, non-object, unparseable, empty, unreadable, an incomplete object, each required field
        missing and of the wrong type) crossed with every reader of a queue entry: the validator, capacity, launch,
        list, index (and the durable session index), the queue view, tp-list, tick and stop. tp-status shows the same
        lines (test_status_repaint). A missing or unreadable value refuses, never defaults."""
        from importlib.machinery import SourceFileLoader
        agent = SourceFileLoader("tp_agent_under_test", str(ROOT / "bin/tp-agent")).load_module()
        queued, valid_path, good = self.queued_pair()
        queue = self.state / "queue"
        wrong = {key: 3 for key in good}
        wrong.update(route_info=[], after="coder-1", context="spec.txt", notes="note")
        entries = {}  # file stem -> (what is wrong, the file's text)
        def add(label, content):
            entries[f"shape{len(entries):02d}"] = (label, content)
        for key in good:
            if key != "queue_reason":  # the one optional field may be absent
                add(f"{key} missing", {k: v for k, v in good.items() if k != key})
            add(f"{key} wrong type", {**good, key: wrong[key]})
        add("queue_reason a list", {**good, "queue_reason": ["x"]})
        add("queue_reason null", {**good, "queue_reason": None})
        add("after has null", {**good, "after": [None]})
        add("context has a number", {**good, "context": ["a", 1]})
        for label, content in (("empty object", {}), ("null", None), ("array", []), ("string", "x"), ("number", 7)):
            add(label, content)
        for stem, (label, content) in list(entries.items()):
            if isinstance(content, dict) and content and not label.startswith("id "):
                content = {**content, "id": stem}  # only the named fault: the id matches the file
            entries[stem] = (label, json.dumps(content))
        entries["broken"] = ("unparseable", "{not json")
        entries["empty-file"] = ("empty file", "")
        (queue / "dangling.json").symlink_to(queue / "no-such-target")  # not a regular file: still listed, refused
        (queue / "adir.json").mkdir()
        os.mkfifo(queue / "afifo.json")  # opening it would block for ever
        elsewhere = self.state / "elsewhere.json"  # a readable, valid record that a queue name points to
        elsewhere.write_text(json.dumps({**good, "id": "linked"}))
        (queue / "linked.json").symlink_to(elsewhere)
        for stem, label in (("dangling", "dangling symlink"), ("adir", "directory"), ("afifo", "FIFO"),
                            ("linked", "readable symlink to a valid record")):
            entries[stem] = (label, None)
        # A thread timer interrupts this thread with SIGUSR2. signal.alarm would share the process's one SIGALRM
        # timer and cancel an outer `perl -e 'alarm N'` bound. 180 s is a hang guard (the test takes ~28 s under load 14).
        previous = signal.signal(signal.SIGUSR2, lambda *_: self.fail("a reader blocked on a queue entry"))
        main_thread = threading.get_ident()
        watchdog = threading.Timer(180, signal.pthread_kill, (main_thread, signal.SIGUSR2))
        watchdog.daemon = True
        watchdog.start()
        self.addCleanup(signal.signal, signal.SIGUSR2, previous)
        self.addCleanup(watchdog.cancel)
        entries["unreadable"] = ("unreadable", json.dumps({**good, "id": "unreadable"}))
        for stem, (_, content) in entries.items():
            if content is not None:
                (queue / f"{stem}.json").write_text(content)
        (queue / "unreadable.json").chmod(0)
        if os.access(queue / "unreadable.json", os.R_OK):  # permissions are not enforced for this user (root)
            del entries["unreadable"]
            (queue / "unreadable.json").unlink()
        # readers that take a record: the validator, capacity and launch
        for stem, (label, _) in entries.items():
            with self.subTest(reader="validator, capacity, launch", entry=label):
                with self.assertRaises(ValueError):
                    agent.queue_item(agent.read_queue_file(queue / f"{stem}.json"), stem)
                try:
                    raw = None if entries[stem][1] is None else json.loads((queue / f"{stem}.json").read_text())
                except (OSError, ValueError):
                    raw = None
                for reader in (agent.capacity, agent.launch):
                    with self.assertRaises(ValueError):
                        reader(self.state, raw)
        with self.assertRaises(ValueError):  # a file that is gone
            agent.read_queue_file(queue / "gone.json")
        # readers of the directory: list, index, the durable index, the queue view, tp-list
        listed = subprocess.run([str(ROOT / "bin/tp-list")], env=self.env, text=True, capture_output=True, timeout=15)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        views = {"queue-view": self.raw("queue-view").stdout.splitlines(), "tp-list": listed.stdout.splitlines()}
        rows = {row["id"]: row for row in self.cli("list")}
        index = self.cli("index", "--no-tick")
        durable = json.loads((self.state / "session-index.json").read_text())
        self.assertEqual(durable["active"], index["active"])
        self.assertTrue(rows[queued["id"]]["reason"].startswith("waiting for "))  # the valid entry is an ordinary row
        for stem, (label, _) in entries.items():
            with self.subTest(reader="list, index, queue views", entry=label):
                self.assertEqual((rows[stem]["status"], rows[stem]["role"]), ("queued", None))
                reason = rows[stem]["reason"]
                self.assertTrue(reason.startswith("start refused: "), rows[stem])
                self.assertIn({"id": stem, "reason": reason}, [{k: r[k] for k in ("id", "reason")} for r in durable["active"]])
                for name, lines in views.items():
                    self.assertIn(f"  {stem}  queued  {reason}", lines, name)
        # tick: only the valid entry starts, nothing is lost, and the index it writes still carries every reason
        run = self.cli("tick")
        self.assertEqual(run["started"], [queued["id"]])
        self.assertEqual({entry["id"] for entry in run["refused"]}, set(entries))
        after = {row["id"]: row for row in json.loads((self.state / "session-index.json").read_text())["active"]}
        for stem, (label, content) in entries.items():
            with self.subTest(reader="tick", entry=label):
                self.assertEqual(after[stem]["status"], "queued")
                self.assertTrue(after[stem]["reason"].startswith("start refused: "), after[stem])
                if content is None:  # a symlink or directory: nothing to rewrite
                    continue
                (queue / f"{stem}.json").chmod(0o644)  # to read back what tick left
                kept = (queue / f"{stem}.json").read_text()
                try:
                    original = json.loads(content)
                except ValueError:
                    original = None
                if isinstance(original, dict) and stem != "unreadable":
                    now = json.loads(kept)  # an object keeps its data and gains the reason
                    self.assertTrue(now.pop("queue_reason").startswith("start refused: "))
                    original.pop("queue_reason", None)
                    self.assertEqual(now, original)
                else:
                    self.assertEqual(kept, content)  # anything else is never rewritten: that would destroy it
        for stem in entries:  # stop removes any queued id, whatever its content
            self.assertEqual(self.cli("stop", stem), {"id": stem, "status": "stopped"})
        self.assertEqual([entry.name for entry in queue.iterdir()], [])

    def test_main_is_recorded_from_an_explicit_id_and_absent_without_one(self):
        """The optional-main path: a main id set in the env is recorded (direct and queued); with none set nothing is
        recorded, so the "main missing" refusals above do not depend on the caller's session."""
        args = ["dispatch", "--role", "coder", "--task", "t", "--worker", "pi", "--model", MODEL, "--thinking", "medium",
                "--workdir", str(self.project), "--deliverable", "Report"]
        with_main = {**self.env, "TP_MAIN_ID": "main-x"}
        first = json.loads(self.raw(*args, env=with_main).stdout)
        self.assertEqual(json.loads((self.task_dir(first) / "dispatch.json").read_text())["main"], "main-x")
        queued = json.loads(self.raw(*args, "--after", first["id"], env=with_main).stdout)
        self.assertEqual(json.loads((self.state / "queue" / f"{queued['id']}.json").read_text())["main"], "main-x")
        bare = self.cli(*args)
        self.assertNotIn("main", json.loads((self.task_dir(bare) / "dispatch.json").read_text()))

    def test_readers_cannot_be_handed_an_unchecked_queue_item(self):
        from importlib.machinery import SourceFileLoader
        agent = SourceFileLoader("tp_agent_under_test", str(ROOT / "bin/tp-agent")).load_module()
        queued, path, good = self.queued_pair()
        bad = {**good, "after": [None], "context": "spec.txt"}
        with self.assertRaises(ValueError):
            agent.capacity(self.state, bad)
        with self.assertRaises(ValueError):
            agent.launch(self.state, bad)
        clean = agent.queue_item(good, queued["id"])
        self.assertEqual(agent.queue_item(clean, queued["id"]), clean)  # idempotent: tick passes its return value on
        self.assertEqual(set(clean), {"id", "created", "role", "task", "workdir", "worker", "model", "thinking",
                                      "route_info", "deliverable", "parent", "after", "context", "notes", "tests",
                                      "queue_reason"})

    def test_missing_preamble_adds_only_the_worker_contract(self):
        item = self.dispatch()
        self.assertNotIn("Repository Instructions", self.packet(item))
        # Every worker gets the contract, with or without a preamble or a tools manifest, through the system prompt.
        self.assertEqual(self.system_prompt(item), CONTRACT["pi"])
        self.assertIn("--append-system-prompt", (self.task_dir(item) / "run.sh").read_text())
        self.assertNotIn("contract: worker", self.packet(item))

    def test_every_route_delivers_the_contract_once_with_its_harness_tools(self):
        # The delivery table (HB2a). Each row is a launch route that reaches a distinct tp-spawn input: dispatch (--role),
        # retry and a retry of a retry (--parent: the prior-task block), review (--preamble-role, no role in meta), a direct
        # claude start with a manifest. The routes not driven here (lane step, -k contract task, investigate, trial, bench)
        # pass the same tp-spawn path; test_no_launcher_starts_a_harness_without_tp_spawn guards that.
        (self.state / "agent-tools.json").write_text(json.dumps([
            {"name": "demo", "path": "$TP_ROOT/tools/agent/lane-info.sh", "roles": ["coder", "code-reviewer"], "use": "orient",
             "usage": "WORKTREE", "output": "a summary"}]))
        first = self.dispatch()
        self.settle(first, "RESULT_STATUS: complete\nDone.\n")
        second = self.retry(first)
        self.settle(second, "RESULT_STATUS: complete\nDone.\n")
        third = self.retry(second)
        self.settle(third, "RESULT_STATUS: complete\nDone.\n")
        review = self.raw("review", third["id"], "--force")
        self.assertEqual(review.returncode, 0, review.stderr)
        reviewer = next(d for d in (self.state / "tasks").iterdir() if (d / "review-of").exists())
        self.blocks = {role: preamble._parts(str(self.project), role)[1] for role in ("coder", "code-reviewer")}   # before it goes
        (self.state / "agent-tools.json").unlink()
        spawned = subprocess.run([str(ROOT / "bin/tp-spawn"), "--worker", "claude", "--model", "sonnet", "--workdir", str(self.project),
                                  "--preamble-role", "code-reviewer", "--", "t"], env=self.env, text=True, capture_output=True)
        self.assertEqual(spawned.returncode, 0, spawned.stderr)
        # HB2b: the routes tp-spawn tells apart by its own inputs, each started directly (contract -k, trial, bench session).
        direct = {}
        for name, extra, env in (("contract", ["-k", "task"], {}), ("trial", ["--worker-env", "TP_TRIAL_ID=trial-1"], {}),
                                 ("bench", [], {"TP_SESSION": "bench-fixture"})):
            run = subprocess.run([str(ROOT / "bin/tp-spawn"), "--worker", "pi", "--model", MODEL, "--workdir", str(self.project),
                                  "--role", "coder", *extra, "--", "t"], env={**self.env, **env}, text=True, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            direct[name] = self.state / "tasks" / run.stdout.strip()
        rows = (("dispatch", self.task_dir(first), "pi", "# Coder", True, False, "spawn", "coder"),
                ("retry", self.task_dir(second), "pi", "# Coder", True, True, "retry", "coder"),
                ("retry of a retry", self.task_dir(third), "pi", "# Coder", True, True, "retry", "coder"),
                ("review", reviewer, json.loads((reviewer / "meta.json").read_text())["worker"], "# Code reviewer", True, False,
                 "review", "code-reviewer"),
                ("claude, no manifest", self.state / "tasks" / spawned.stdout.strip(), "claude", "# Code reviewer", False, False,
                 "spawn", "code-reviewer"),
                *((name, task, "pi", "# Coder", False, False, name, "coder") for name, task in direct.items()))
        for name, task, harness, role, tools, prior, route, role_name in rows:
            with self.subTest(row=name):
                self.assert_launch_record(task, harness, route, role_name, tools, prior)
                prompt, packet = self.system_prompt(task), (task / "packet.md").read_text().split("# Context")[0]
                self.assertTrue(prompt.startswith(CONTRACT[harness]), f"{name}: the prompt does not open with the {harness} contract")
                self.assertEqual((prompt + packet).count("contract: worker v1"), 1, f"{name}: the contract is not there once")
                self.assertIn(SAFE_SEARCH_RULE_PI if harness == "pi" else SAFE_SEARCH_RULE_CLAUDE, prompt, name)
                self.assertNotRegex(prompt + packet, r"grep -rn|\brg -|\brg\b.*recursive", name)
                # Each directive is given once: the packet states the report path as a fact and leaves the rules (where the
                # report goes, the RESULT_STATUS line) to the contract.
                self.assertEqual(packet.count("report.md"), 1, f"{name}: the report path is stated once")
                for directive in ("Put durable findings", "write it to", "end every final reply"):
                    self.assertNotIn(directive, packet, f"{name}: {directive!r} repeats the contract")
                self.assertEqual(prompt.count("RESULT_STATUS: complete|empty|blocked|incomplete"), 1, name)
                if name != "review":   # a review task places the line (after its verdict), as the contract allows
                    self.assertNotIn("RESULT_STATUS: complete|empty|blocked|incomplete", packet, name)
                self.assertEqual(packet.count("# Worker Role\n\n" + role + "\n"), 1, f"{name}: role text")
                self.assertEqual(packet.count("\n# Return\n"), 1, f"{name}: Return block")
                self.assertEqual("# Project Tools\n\n- demo: " in prompt, tools, f"{name}: Project Tools block")
                self.assertEqual("# Prior Task\n\nPrior result: " in prompt, prior, f"{name}: prior task block")
        self.assertIn(str(self.task_dir(second) / "result.md"), self.system_prompt(self.task_dir(third)))   # the nearest parent

    def assert_launch_record(self, task, harness, route, role, tools, prior):
        """HB2b: the task's launch.json names its route, role and harness, and each hash is the hash of what was delivered."""
        import hashlib
        import agent_tools
        sha = lambda text: hashlib.sha256(text.encode()).hexdigest()  # noqa: E731
        raw = (task / "launch.json").read_text()
        record = json.loads(raw)
        prompt, packet = self.system_prompt(task), (task / "packet.md").read_text()
        meta = json.loads((task / "meta.json").read_text())
        self.assertEqual((record["schema"], record["task"], record["route"], record["harness"], record["role"]),
                         ("tp-launch:v1", task.name, route, harness, role))
        self.assertEqual(record["model"], meta["model"] or None)
        self.assertIn(record["status"], ("prepared", "failed"))
        self.assertEqual(record["status"] == "failed", "failure" in record)
        pieces = {p["name"]: p for p in record["instructions"]}
        self.assertEqual((pieces["contract"]["sha256"], pieces["contract"]["version"]), (sha(CONTRACT[harness]), "v1"))
        self.assertEqual(pieces["system-prompt"]["sha256"], sha(prompt))
        self.assertEqual(pieces["packet"]["sha256"], sha(packet.rstrip("\n")))   # run.sh passes "$(cat packet.md)"
        role_body = (ROOT / "agent/roles" / f"{role}.md").read_text().rstrip("\n")
        self.assertIn(role_body, packet)
        self.assertEqual(pieces["role"]["sha256"], sha(role_body))
        block = self.blocks[role] if tools else ""
        self.assertEqual("manifest" in pieces, tools)
        if tools:
            self.assertIn(block, prompt)
            self.assertEqual(pieces["manifest"]["sha256"], sha(block))
        self.assertEqual("prior-task" in pieces, prior)
        if prior:
            text = "# Prior Task\n\n" + agent_tools.prior_line(str(self.state), meta["parent"])
            self.assertIn(text, prompt)
            self.assertEqual(pieces["prior-task"]["sha256"], sha(text))
            self.assertEqual(record["context"]["parent"], meta["parent"])
        self.assertEqual("bash-suffix-rule" in pieces, harness == "claude")
        for piece in record["instructions"]:   # a null hash always says why
            if piece["sha256"] is None:
                self.assertIn(f"instructions[{piece['name']}].sha256", record["null_reasons"])
        self.assertEqual(record["capabilities"], {k: preamble.HARNESS_ADAPTER[harness][k] for k in ("supported", "unsupported")})
        for tool in preamble.HARNESS_ADAPTER[harness]["tools"]:
            self.assertIn(tool, record["tools"]["exposed"])
        # No secrets or text: paths, hashes, names and versions only.
        for line in (prompt + "\n" + packet).splitlines():
            if len(line) > 40:
                self.assertNotIn(line, raw)
        self.assertNotIn("Make a focused change", raw)

    def test_launch_record_of_a_missing_harness_binary_is_failed_never_prepared(self):
        # HB2b: a harness the launcher cannot find gets a record marked failed, with why; the start itself goes on as before.
        bare = os.pathsep.join((self.env["PATH"].split(os.pathsep)[0], "/usr/bin", "/bin"))
        for harness, env in (("claude", {"PATH": bare}), ("pi", {"PATH": bare, "TP_PI_NODE": str(self.root / "no-node" / "node")}),
                             ("pi", {"PATH": bare, "TP_PI_NODE": ""})):
            with self.subTest(harness=harness, env=sorted(env)):
                run = subprocess.run([str(ROOT / "bin/tp-spawn"), "--worker", harness, "--workdir", str(self.project), "--role", "coder",
                                      "--", "t"], env={**self.env, **env}, text=True, capture_output=True)
                self.assertEqual(run.returncode, 0, run.stderr)
                record = json.loads((self.state / "tasks" / run.stdout.strip() / "launch.json").read_text())
                self.assertEqual(record["status"], "failed")
                self.assertIn(f"the {harness} harness binary is missing", record["failure"])
                self.assertIsNone(record["harness_version"])

    def test_launch_record_resolves_pi_from_the_panes_tmux_environment_not_tp_spawns(self):
        # HB2b: the pane takes TP_PI_NODE from the tmux session; the record follows what tmux says the pane will see.
        installed = self.root / "node" / "bin"
        installed.mkdir(parents=True)
        for name in ("node", "pi"):
            (installed / name).write_text("#!/bin/sh\nexit 0\n")
            (installed / name).chmod(0o755)
        package = self.root / "node/lib/node_modules/@earendil-works/pi-coding-agent/package.json"
        package.parent.mkdir(parents=True)
        package.write_text(json.dumps({"name": "@earendil-works/pi-coding-agent", "version": "9.9.9"}))
        missing = str(self.root / "no-node" / "node")
        for answer, own, status in ((f"TP_PI_NODE={installed / 'node'}", missing, "prepared"),
                                    (f"TP_PI_NODE={missing}", str(installed / "node"), "failed"),
                                    ("-TP_PI_NODE", str(installed / "node"), "failed")):
            with self.subTest(answer=answer):
                fake_tmux(self.root / "bin", 'case "$1" in has-session) exit 0 ;; show-options) printf "%s\\n" "$TP_STATE_DIR" ;; '
                                             f'show-environment) printf "%s\\n" "{answer}" ;; new-window) printf "%%fake\\n" ;; *) exit 0 ;; esac\n')
                run = subprocess.run([str(ROOT / "bin/tp-spawn"), "--worker", "pi", "--workdir", str(self.project), "--role", "coder",
                                      "--", "t"], env={**self.env, "TP_PI_NODE": own, "HOME": str(self.root)}, text=True, capture_output=True)
                self.assertEqual(run.returncode, 0, run.stderr)
                record = json.loads((self.state / "tasks" / run.stdout.strip() / "launch.json").read_text())
                self.assertEqual(record["status"], status, record)
                self.assertEqual(record["harness_version"], "9.9.9" if status == "prepared" else None)

    def test_a_start_that_fails_before_the_harness_leaves_no_launch_record(self):
        # HB2b: tp-spawn removes the task directory of a start that fails before the window (here the window itself fails).
        fake_tmux(self.root / "bin", 'case "$1" in has-session) exit 0 ;; show-options) printf "%s\\n" "$TP_STATE_DIR" ;; '
                                     'new-window) exit 1 ;; *) exit 0 ;; esac\n')
        run = subprocess.run([str(ROOT / "bin/tp-spawn"), "--worker", "pi", "--workdir", str(self.project), "--role", "coder", "--", "t"],
                             env=self.env, text=True, capture_output=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(list((self.state / "tasks").glob("*/launch.json")), [])

    def test_no_launcher_starts_a_harness_without_tp_spawn(self):
        # Every route reaches the contract only through bin/tp-spawn; a launcher that ran a harness itself would skip it.
        known = {"bin/tp-spawn": "every worker", "bin/tp-main": "Main itself (HB2 step b)",
                 "bin/tp-claude-bootstrap": "the owner's folder-trust start", "tools/qual/summarize_meaning.py": "a text summary, no task",
                 "tools/qual/summarize_run.py": "a text summary, no task",
                 "lib/launch_record.py": "reads bin/tp-pi's PI_NODE default as text for the launch record (HB2b); starts nothing",
                 "agent/extensions/assistant.ts": "ask_local, a read-only helper with its own prompt and no task (HB2 step b)"}
        forms = [form for h in ("tp-pi", "tp-claude") for form in (f'bin/{h}"', f'"bin" / "{h}"', f'bin("{h}")')]
        files = [p for d in ("bin", "lib", "tools/qual", "tools/agent", "agent/extensions", "claude/mcp")
                 for p in (ROOT / d).iterdir() if p.is_file() and (d in ("bin", "tools/qual", "tools/agent") or p.suffix in (".py", ".ts", ".mjs"))]
        starters = sorted(str(p.relative_to(ROOT)) for p in files
                          if any(form in line for line in p.read_text(errors="replace").splitlines()
                                 if not line.lstrip().startswith("#") for form in forms))
        self.assertEqual(sorted(set(starters) - set(known)), [], "a launcher starts a harness without tp-spawn")

    def test_role_files_hold_method_and_name_no_harness_tool(self):
        # Tool names come from lib/preamble.py HARNESS_TOOLS through the contract; a role names none.
        for role in (ROOT / "agent/roles").glob("*.md"):
            text = role.read_text()
            for word in ("grep -rn", "safe_grep", "safe_read", "mcp__", "RESULT_STATUS", "tp-agent request"):
                self.assertNotIn(word, text, f"{role.name}: {word}")

    def test_unusable_preamble_refuses_at_dispatch_and_spawn(self):
        for label, content in (("oversize", "x" * 8001), ("unknown role", "## Role: wizard\nHi\n"),
                               ("not utf-8", b"\xff\xfe")):
            with self.subTest(label):
                path = self.state / "worker-preamble.md"
                path.write_bytes(content if isinstance(content, bytes) else content.encode())
                run = self.raw("dispatch", "--role", "coder", "--task", "t", "--worker", "pi", "--model", MODEL,
                               "--thinking", "low", "--workdir", str(self.project))
                self.assertNotEqual(run.returncode, 0)
                if label == "oversize":
                    self.assertIn("8001 bytes", run.stderr)
                self.assertEqual(list((self.state / "tasks").iterdir()), [])
                self.assertFalse((self.state / "queue").exists() and list((self.state / "queue").iterdir()))
                spawned = subprocess.run([str(ROOT / "bin/tp-spawn"), "--worker", "pi", "--model", MODEL, "--workdir",
                                          str(self.project), "--", "t"], env=self.env, text=True, capture_output=True)
                self.assertNotEqual(spawned.returncode, 0)
                self.assertIn("worker preamble", spawned.stderr)
                self.assertEqual(list((self.state / "tasks").iterdir()), [])

    def test_unreadable_preamble_refuses(self):
        path = self.state / "worker-preamble.md"
        path.write_text("rule")
        path.chmod(0)
        self.addCleanup(path.chmod, 0o644)
        if os.access(path, os.R_OK):
            self.skipTest("file permissions are not enforced for this user")
        run = self.raw("dispatch", "--role", "coder", "--task", "t", "--worker", "pi", "--model", MODEL,
                       "--thinking", "low", "--workdir", str(self.project))
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("cannot read", run.stderr)

    def test_router_card_ignores_preamble(self):
        sys.path.insert(0, str(ROOT / "lib"))
        import route_card
        before = route_card.build_card("coder", "Fix the parser.", "Report", workdir=str(self.project))
        self.write_preamble("Write your report to report.md.")
        self.assertEqual(before, route_card.build_card("coder", "Fix the parser.", "Report", workdir=str(self.project)))
        self.assertNotIn("preamble", (ROOT / "lib/route_card.py").read_text().lower())

    def test_tests_scope_defaults_shows_near_top_records_and_survives_retry(self):
        default = self.dispatch()
        packet = self.packet(default)
        self.assertIn("# Test Scope", packet)
        self.assertIn("Do not run the whole suite", packet)
        self.assertLess(packet.index("# Test Scope"), packet.index("# Local Task Artifacts"))
        self.assertEqual(json.loads((self.task_dir(default) / "dispatch.json").read_text())["tests"], "affected")
        for scope, phrase in (("full", "Run the whole test suite."), ("none", "Do not run tests.")):
            item = self.dispatch(tests=scope)
            self.assertIn(phrase, self.packet(item))
            self.settle(item)
            self.assertIn(phrase, self.retry_packet(item))
        bad = self.raw("dispatch", "--role", "coder", "--task", "t", "--worker", "pi", "--model", MODEL,
                       "--workdir", str(self.project), "--tests", "some")
        self.assertNotEqual(bad.returncode, 0)

    def test_retry_refuses_invalid_recorded_test_scope(self):
        item = self.dispatch()
        self.settle(item)
        path = self.task_dir(item) / "dispatch.json"
        record = json.loads(path.read_text())
        record["tests"] = "bogus"
        path.write_text(json.dumps(record))
        run = self.raw("retry", item["id"], "--reason", "x", "--worker", "pi", "--model", MODEL)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("invalid test scope", run.stderr)

    def test_retry_scope_defaults_only_for_a_valid_record_without_the_field(self):
        item = self.dispatch(tests="full")
        self.settle(item)
        path = self.task_dir(item) / "dispatch.json"
        good = json.loads(path.read_text())
        del good["tests"]
        path.write_text(json.dumps(good))  # an older record
        self.assertIn("Do not run the whole suite", self.retry_packet(item))
        path.write_text('{"context": [], "notes": []}')  # the oldest shape, before omitted_context
        self.assertIn("Do not run the whole suite", self.retry_packet(item))
        for label, write in (("empty object", lambda: path.write_text("{}")),
                             ("context not a list", lambda: path.write_text('{"context": "x", "omitted_context": [], "notes": []}')),
                             ("notes missing", lambda: path.write_text('{"context": [], "omitted_context": []}')),
                             ("omitted_context not strings", lambda: path.write_text('{"context": [], "omitted_context": [1], "notes": []}')),
                             ("truncated", lambda: path.write_text('{"context": [], "tes')),
                             ("not an object", lambda: path.write_text("[]")),
                             ("missing", lambda: path.unlink())):
            with self.subTest(label):
                write()
                run = self.raw("retry", item["id"], "--reason", "x", "--worker", "pi", "--model", MODEL)
                self.assertNotEqual(run.returncode, 0)
                self.assertIn("prior dispatch record", run.stderr)

    def test_retry_takes_no_recorded_scope_from_a_record_that_is_not_valid(self):
        (self.project / "spec.txt").write_text("SPEC_MARKER\n")
        item, task = self.settled(context="spec.txt", note="Keep API stable", tests="full")
        path = task / "dispatch.json"
        tasks = 1
        for label, raw in (("tests only", '{"tests": "full"}'),
                           ("tests with context a string", '{"tests": "full", "context": "bad", "notes": []}'),
                           ("tests with notes an object", '{"tests": "full", "context": [], "notes": {}}'),
                           ("tests with omitted_context a string", '{"tests": "full", "context": [], "omitted_context": "x", "notes": []}'),
                           ("tests with a non-string entry", '{"tests": "full", "context": [3], "notes": []}'),
                           ("tests that is null", '{"tests": null, "context": [], "notes": []}')):
            with self.subTest(label):
                path.write_text(raw)
                run = self.raw("retry", item["id"], "--reason", "x", "--worker", "pi", "--model", MODEL)
                self.assertNotEqual(run.returncode, 0)
                self.assertIn("prior dispatch record", run.stderr)
                self.assertIn("pass --tests", run.stderr)
                self.assertEqual(len(list((self.state / "tasks").iterdir())), tasks)  # the refusal started nothing
                # the documented recovery: an explicit scope is used, nothing is replayed, and the output says so
                again = self.cli("retry", item["id"], "--reason", "x", "--worker", "pi", "--model", MODEL, "--tests", "none")
                self.assertTrue(again["prior_record"].startswith("not used: "))
                packet = self.packet(again)
                self.assertIn("Do not run tests.", packet)
                self.assertIn("prior dispatch record is malformed", packet)
                self.assertNotIn("SPEC_MARKER", packet)
                for other in (self.state / "tasks").iterdir():
                    (other / "state.json").write_text(WAITING)
                tasks += 1

    def test_every_reader_refuses_every_invalid_dispatch_record_shape(self):
        """Invalid dispatch.json shapes crossed with every reader of it: dispatch_record, prior_dispatch, replay_inputs
        and retry (its replay and its recorded test scope), with and without an explicit --tests."""
        from importlib.machinery import SourceFileLoader
        agent = SourceFileLoader("tp_agent_under_test", str(ROOT / "bin/tp-agent")).load_module()
        (self.project / "spec.txt").write_text("SPEC_MARKER\n")
        item, task = self.settled(context="spec.txt", note="Keep API stable", tests="full")
        path = task / "dispatch.json"
        good = json.loads(path.read_text())
        shapes = {"missing file": None, "null": "null", "array": "[]", "string": '"x"', "number": "7",
                  "unparseable": "{not json", "empty file": "", "empty object": "{}"}
        for key in ("context", "notes"):
            shapes[f"{key} missing"] = json.dumps({k: v for k, v in good.items() if k != key})
        for key in ("context", "notes", "omitted_context"):
            shapes[f"{key} a string"] = json.dumps({**good, key: "x"})
            shapes[f"{key} null"] = json.dumps({**good, key: None})
            shapes[f"{key} holds a number"] = json.dumps({**good, key: ["a", 1]})
        for label, value in (("tests a list", ["full"]), ("tests null", None), ("tests unknown", "some"), ("tests a number", 3)):
            shapes[label] = json.dumps({**good, "tests": value})  # the rest is valid
        shapes["valid tests, malformed rest"] = json.dumps({"tests": "full", "context": "x", "notes": []})
        shapes["unreadable"] = json.dumps(good)
        tasks = 1
        for label, raw in shapes.items():
            with self.subTest(label):
                if raw is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(raw)
                if label == "unreadable":
                    path.chmod(0)
                    if os.access(path, os.R_OK):
                        continue  # permissions are not enforced for this user (root)
                with self.assertRaises(agent.RecordError):
                    agent.dispatch_record(task)
                kept, problem = agent.prior_dispatch(task)
                self.assertIsNone(kept)
                self.assertIsInstance(problem, agent.RecordError)
                self.assertEqual(agent.replay_inputs(task, kept, problem)[:2], ([], []))  # replays nothing
                run = self.raw("retry", item["id"], "--reason", "x", "--worker", "pi", "--model", MODEL)
                self.assertNotEqual(run.returncode, 0)
                self.assertIn("pass --tests", run.stderr)  # the recorded scope is not taken from this record
                self.assertEqual(len(list((self.state / "tasks").iterdir())), tasks)
                again = self.cli("retry", item["id"], "--reason", "x", "--worker", "pi", "--model", MODEL, "--tests", "none")
                self.assertTrue(again["prior_record"].startswith("not used: "))
                packet = self.packet(again)
                self.assertIn("Do not run tests.", packet)
                self.assertNotIn("SPEC_MARKER", packet)
                self.assertNotIn("Keep API stable", packet)
                for other in (self.state / "tasks").iterdir():
                    (other / "state.json").write_text(WAITING)
                tasks += 1
        path.chmod(0o644)
        for older in ({"context": [], "notes": []}, {"context": [], "omitted_context": [], "notes": []}):
            path.write_text(json.dumps(older))  # older genuine records stay valid; the scope is then the documented default
            self.assertIsNone(agent.dispatch_record(task)["tests"])

    def test_retry_omitted_context_is_optional_but_a_list_of_strings_when_present(self):
        item, task = self.settled(tests="full")
        path = task / "dispatch.json"
        path.write_text('{"tests": "full", "context": [], "notes": []}')
        again = self.cli("retry", item["id"], "--reason", "x", "--worker", "pi", "--model", MODEL)
        self.assertNotIn("prior_record", again)
        self.assertIn("Run the whole test suite.", self.packet(again))
        self.settle(again)
        path.write_text('{"tests": "full", "context": [], "omitted_context": null, "notes": []}')
        run = self.raw("retry", item["id"], "--reason", "x", "--worker", "pi", "--model", MODEL)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("omitted_context is not a list of strings", run.stderr)

    def test_reply_without_result_status_is_paused_and_not_collected(self):
        item = self.dispatch()
        task = self.settle(item, "Started a background sleep; waiting for it.\n")
        self.assertEqual(self.cli("inspect", item["id"])["status"], "paused")
        self.assertEqual(self.cli("index")["recent"][-1]["status"], "paused")
        index = self.cli("index")
        self.assertEqual(index["pending"], [])
        self.assertEqual([r["id"] for r in index["unstatused"]], [item["id"]])
        self.assertEqual(index["paused"], [])
        run = self.raw("collect", item["id"])
        self.assertEqual(run.returncode, 0, run.stderr)
        notice = f"{item['id']} replied without a result status (paused); nothing was collected."
        out = json.loads(run.stdout)  # stdout stays pure JSON
        self.assertEqual(out["status"], "paused")
        # the notice goes on to state the accepted form and the command that goes on (tests/test_refusal_remedies.py proves them)
        self.assertTrue(out["notice"].startswith(notice + " A reply is collected when it has a line of its own that reads "
                                                 "RESULT_STATUS: complete"), out["notice"])
        self.assertEqual(next(iter(out)), "notice")
        self.assertIn(notice, run.stderr)
        self.assertFalse((task / "collected.json").exists())
        # A later reply that carries a status settles it.
        (task / "result.md").write_text("RESULT_STATUS: complete\nDone.\n")
        self.assertEqual(self.cli("inspect", item["id"])["status"], "settled")
        # A clean exit with an unstatused reply is paused too; with a status it settles.
        (task / "exit").write_text("0\n")
        (task / "result.md").write_text("no status here\n")
        self.assertEqual(self.cli("inspect", item["id"])["status"], "paused")
        (task / "result.md").write_text("RESULT_STATUS: blocked\n")
        self.assertEqual(self.cli("inspect", item["id"])["status"], "settled")
        # A dependent task does not start from a paused predecessor.
        (task / "result.md").write_text("no status here\n")
        run = self.raw("dispatch", "--role", "coder", "--task", "next", "--worker", "pi", "--model", MODEL,
                       "--thinking", "medium", "--workdir", str(self.project), "--after", item["id"])
        self.assertNotEqual(json.loads(run.stdout).get("status"), "starting")

    def test_paused_with_a_result_status_is_not_called_unstatused(self):
        item = self.dispatch()
        task = self.settle(item, "RESULT_STATUS: blocked\nOver budget.\n")
        state = json.loads((task / "state.json").read_text())
        (task / "state.json").write_text(json.dumps(dict(state, state="paused", updated="2020-01-01T00:00:00Z")))
        self.assertEqual(self.cli("inspect", item["id"])["status"], "paused")
        # A paused predecessor never starts a dependent task, with or without a result status.
        dependent = self.raw("dispatch", "--role", "coder", "--task", "next", "--worker", "pi", "--model", MODEL,
                             "--thinking", "medium", "--workdir", str(self.project), "--after", item["id"])
        self.assertEqual(dependent.returncode, 0, dependent.stderr)
        self.assertNotEqual(json.loads(dependent.stdout).get("status"), "starting")
        run = self.raw("collect", item["id"])
        out = json.loads(run.stdout)
        self.assertEqual((out["status"], out["result_status"]), ("paused", "blocked"))
        self.assertTrue(out["notice"].startswith(f"{item['id']} is paused; nothing was collected."), out["notice"])
        self.assertIn(f"`tp-agent stop {item['id']}`", out["notice"])
        self.assertNotIn("without a result status", run.stderr + run.stdout)
        index = self.cli("index")
        self.assertEqual(index["unstatused"], [])
        self.assertEqual([r["id"] for r in index["paused"]], [item["id"]])
        # still listed when 14 newer tasks push it out of the recent window (rows sort by second-resolution time)
        for _ in range(14):
            self.dispatch()
        index = self.cli("index")
        self.assertNotIn(item["id"], [r["id"] for r in index["recent"]])
        self.assertEqual([r["id"] for r in index["paused"]], [item["id"]])

    def test_raw_settled_state_without_a_result_status_is_paused(self):
        item = self.dispatch()
        task = self.settle(item, "Still going.\n")
        state = json.loads((task / "state.json").read_text())
        for raw in ("settled", "waiting"):
            (task / "state.json").write_text(json.dumps(dict(state, state=raw)))
            self.assertEqual(self.cli("inspect", item["id"])["status"], "paused", raw)
            self.assertNotIn("completed", self.raw("collect", item["id"]).stdout.split('"status"')[1][:20])
            self.assertFalse((task / "collected.json").exists())
        (task / "result.md").unlink()
        (task / "state.json").write_text(json.dumps(dict(state, state="settled")))
        self.assertEqual(self.cli("inspect", item["id"])["status"], "paused")
        (task / "result.md").write_text("RESULT_STATUS: complete\n")
        self.assertEqual(self.cli("inspect", item["id"])["status"], "settled")

    def test_dispatch_without_optional_deliverable_starts_worker(self):
        item = self.cli("dispatch", "--role", "coder", "--task", "Check the parser", "--worker", "pi",
                        "--model", MODEL, "--thinking", "low", "--workdir", str(self.project))
        self.assertTrue((self.task_dir(item) / "meta.json").exists())

    def test_dependency_queues_then_starts_with_result_pointer(self):
        first = self.dispatch(role="researcher", task="Find the relevant interface")
        second = self.dispatch(role="coder", task="Use the interface", after=first["id"])
        self.assertEqual(second["status"], "queued")
        self.assertIn("waiting for", second["reason"])
        self.settle(first, "RESULT_STATUS: complete\nInterface found.\n")
        started = self.cli("tick")
        self.assertEqual(started["started"], [second["id"]])
        packet = self.packet(second)
        self.assertIn(f"Dependency {first['id']} result:", packet)
        self.assertFalse((self.state / "queue" / f"{second['id']}.json").exists())

    def test_total_slots_queue_without_blocking_direct_answers(self):
        limit = json.loads((ROOT / "config/orchestration.json").read_text())["max_active_workers"]
        active = [self.dispatch(task=f"Task {i}") for i in range(limit)]
        self.assertTrue(all(item["status"] == "starting" for item in active))
        fifth = self.dispatch(task=f"Task {limit + 1}")
        self.assertEqual(fifth["status"], "queued")
        self.assertIn("global worker slots", fifth["reason"])
        self.settle(active[0], "RESULT_STATUS: complete")
        self.assertEqual(self.cli("tick")["started"], [fifth["id"]])

    def test_sensitive_context_is_omitted_without_stopping_work(self):
        (self.project / ".env").write_text("PRIVATE_TOKEN=example\n")
        item = self.dispatch(context=".env")
        packet = self.packet(item)
        self.assertNotIn("PRIVATE_TOKEN=example", packet)
        self.assertIn("Sensitive context .env was omitted", packet)
        self.assertEqual(item["status"], "starting")
        # Several contexts are screened in one call: each file gets its own verdict (the secret between two clean ones).
        (self.project / "before.txt").write_text("BEFORE_MARKER\n")
        (self.project / "after.txt").write_text("AFTER_MARKER\n")
        item = self.cli("dispatch", "--role", "coder", "--task", "Mixed contexts", "--worker", "pi", "--model", MODEL,
                        "--workdir", str(self.project), "--context", "before.txt", "--context", ".env",
                        "--context", "after.txt")
        packet = self.packet(item)
        self.assertIn("BEFORE_MARKER", packet)
        self.assertIn("AFTER_MARKER", packet)
        self.assertNotIn("PRIVATE_TOKEN=example", packet)
        self.assertIn("Sensitive context .env was omitted", packet)
        self.assertNotIn("Sensitive context before.txt", packet)
        self.assertNotIn("Sensitive context after.txt", packet)
        # A batched verdict binds to the file it screened: a context symlink retargeted to .env after screening is asked about alone.
        (self.project / "link.txt").symlink_to("after.txt")
        text = (ROOT / "bin/tp-spawn").read_text()
        funcs = text[text.index("# A verdict answers only"):text.index("# largest_range")]
        script = funcs + ('contexts=(link.txt); screen_contexts; secret_file "$workdir/link.txt" && echo WRONG-REFUSED\n'
                          'ln -sf .env link.txt; secret_file "$workdir/link.txt" && echo REFUSED || echo READ\n')
        run = subprocess.run(["bash", "-c", script], env={**self.env, "ROOT": str(ROOT), "workdir": str(self.project)},
                             cwd=self.project, text=True, capture_output=True, timeout=20)
        self.assertEqual(run.stdout.strip(), "REFUSED", run.stderr)
        # The batch screening fails closed, with ONE context too (an empty reply must not count as one "not sensitive" verdict):
        # a batch that exits nonzero, prints nothing, answers a different count, or answers with something but a clear
        # "sensitive": false refuses the file. A clean verdict still passes (the control, so the stub is not just refusing).
        replies = {"exit-1": "exit 1", "empty": "exit 0", "count": "echo '[]'",
                   "other": """echo '[{"sensitive":"no"}]'""", "clean": """echo '[{"sensitive":false}]'"""}
        script = funcs + 'contexts=(after.txt)\n'
        for name, body in replies.items():
            stub = self.project / f"fake-{name}/bin/tp-sensitive-read"
            stub.parent.mkdir(parents=True)
            stub.write_text(f"#!/bin/sh\ncat >/dev/null\n{body}\n")
            stub.chmod(0o755)
            script += (f'ROOT="{stub.parent.parent}"; screen_contexts\n'
                       f'secret_file "$workdir/after.txt" && echo {name}=REFUSED || echo {name}=READ\n')
        run = subprocess.run(["bash", "-c", script], env={**self.env, "ROOT": str(ROOT), "workdir": str(self.project)},
                             cwd=self.project, text=True, capture_output=True, timeout=20)
        self.assertEqual(run.stdout.split(), ["exit-1=REFUSED", "empty=REFUSED", "count=REFUSED", "other=REFUSED", "clean=READ"],
                         run.stderr)

    def test_worker_request_is_visible_and_message_answers_it(self):
        item = self.dispatch()
        task = self.task_dir(item)
        asked = self.raw("request", "--kind", "tool", "--what", "browser", "--reason", "Need rendered evidence",
                         env={**self.env, "TP_TASK_DIR": str(task)}, timeout=10)
        self.assertEqual(asked.returncode, 0, asked.stderr)
        self.assertEqual(self.cli("inspect", item["id"])["status"], "needs_input")
        self.assertEqual(self.cli("index")["active"][0]["capability_request"]["what"], "browser")
        sent = self.cli("message", item["id"], "--text", "Use the local HTML renderer.")
        self.assertEqual((sent["sent"], sent["submitted"], sent["attempts"]), (True, True, 1))
        line = json.loads((task / "messages.jsonl").read_text().splitlines()[-1])
        self.assertEqual((line["text"], line["submitted"], line["attempts"]), ("Use the local HTML renderer.", True, 1))
        # HB4a: a delivery alone never answers the request; a confirmed message that names it (--resolves) does.
        self.assertEqual(json.loads((task / "capability-request.json").read_text())["status"], "pending")
        req = json.loads((task / "capability-request.json").read_text())["id"]
        sent = self.cli("message", item["id"], "--resolves", req, "--text", "That is the answer.")
        self.assertEqual(sent["resolves"], {"request": req, "resolved": True, "status": "resolved"})
        self.assertEqual(json.loads((task / "capability-request.json").read_text())["status"], "answered")

    def test_message_that_is_never_submitted_exits_nonzero_and_leaves_the_request_pending(self):
        from private_tmux import PrivateTmux
        server = PrivateTmux()
        self.addCleanup(server.close)
        item = self.dispatch()
        task = self.task_dir(item)
        pane, log, raw = server.worker("never")  # a worker whose box never submits (the G3 live-run failure)
        meta = json.loads((task / "meta.json").read_text())
        (task / "meta.json").write_text(json.dumps({**meta, "pane": pane}))
        (task / "capability-request.json").write_text(json.dumps({"status": "pending", "kind": "tool", "what": "browser"}))
        run = self.raw("message", item["id"], "--text", "carry on", env={**self.env, **server.env(), "TP_SEND_CONFIRM_SECONDS": "0.5"}, timeout=30)
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn("not confirmed submitted", run.stderr)
        self.assertIn("press Enter there", run.stderr)  # what to do
        self.assertEqual((json.loads(run.stdout)["submitted"], json.loads(run.stdout)["attempts"]), (False, 3))
        line = json.loads((task / "messages.jsonl").read_text().splitlines()[-1])  # still written: the text is in the box
        self.assertEqual((line["text"], line["submitted"], line["attempts"]), ("carry on", False, 3))
        self.assertEqual(json.loads((task / "capability-request.json").read_text())["status"], "pending")
        self.assertEqual(server.typed(raw, "carry on"), 1)
        # MS2: a Claude worker whose prompt held the delivery id and only the start of the text did not get the message: exit 1,
        # what arrived, one Enter, and the request stays pending. The id and its space are 19 characters; " with it" is lost.
        pane, log, raw = server.worker("normal", task_dir=task, options=("--keep-first", "27"))
        (task / "meta.json").write_text(json.dumps({**meta, "worker": "claude", "pane": pane}))
        run = self.raw("message", item["id"], "--text", "carry on with it", env={**self.env, **server.env()}, timeout=30)
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn("not delivered whole: the worker's prompt held 7 of its 13 characters", run.stderr)
        self.assertEqual({k: json.loads(run.stdout)[k] for k in ("submitted", "attempts", "received", "length")},
                         {"submitted": False, "attempts": 1, "received": 7, "length": 13})
        line = json.loads((task / "messages.jsonl").read_text().splitlines()[-1])
        self.assertEqual((line["submitted"], line["received"], line["length"]), (False, 7, 13))
        self.assertEqual(json.loads((task / "capability-request.json").read_text())["status"], "pending")

    def test_retry_links_prior_attempt_and_records_reason(self):
        first = self.dispatch()
        self.settle(first, "RESULT_STATUS: incomplete\nCould not find the parser.\n")
        second = self.retry(first, "Look in the package directory")
        self.assertEqual(second["parent"], first["id"])
        packet = self.packet(second)
        self.assertIn("Look in the package directory", packet)
        self.assertIn(first["id"], packet)
        self.assertEqual(self.cli("inspect", first["id"])["result_status"], "incomplete")

    def test_retry_preserves_context_and_notes_without_stale_grants(self):
        (self.project / "spec.txt").write_text("SPEC_MARKER\n")
        (self.project / ".env").write_text("PRIVATE_TOKEN=example\n")
        dep = self.dispatch(role="researcher", task="Dependency")
        first = self.dispatch(task="Use spec", after=dep["id"], context="spec.txt", note="Keep API stable")
        self.assertEqual(first["status"], "queued")
        self.settle(dep, "RESULT_STATUS: complete\n")
        self.cli("tick")
        sensitive = self.dispatch(task="Env", context=".env")
        self.settle(first, "RESULT_STATUS: incomplete\n")
        second = self.retry(first, "Try again")
        packet = self.packet(second)
        self.assertIn("SPEC_MARKER", packet)
        self.assertIn("Keep API stable", packet)
        self.assertIn("Prior packet:", packet)
        self.assertNotIn("Dependency %s result" % dep["id"], packet)
        # a retry of a retry does not accumulate retry notes
        self.settle(second)
        packet3 = self.retry_packet(second)
        self.assertEqual(packet3.count("Retry of "), 1)
        self.assertIn("SPEC_MARKER", packet3)
        # sensitive context stays omitted on retry
        self.settle(sensitive)
        retry = self.retry(sensitive)
        packet4 = self.packet(retry)
        self.assertNotIn("PRIVATE_TOKEN=example", packet4)
        # the omitted input is recorded as omitted, never as admitted, and is not handed back to spawn
        kept = json.loads((self.task_dir(sensitive) / "dispatch.json").read_text())
        self.assertEqual((kept["context"], kept["omitted_context"]), ([], [".env"]))
        self.assertNotIn("Sensitive context .env was omitted", packet4)
        self.assertIn("1 context input(s) omitted as sensitive in the prior attempt were not replayed", packet4)
        again = json.loads((self.task_dir(retry) / "dispatch.json").read_text())
        self.assertEqual((again["context"], again["omitted_context"]), ([], []))

    def settled(self, **extra):
        item = self.dispatch(**extra)
        return item, self.settle(item)

    def test_retry_does_not_replay_raw_sensitive_spec_from_older_record(self):
        (self.project / ".env").write_text("PRIVATE_TOKEN=example\n")
        (self.project / "spec.txt").write_text("SPEC_MARKER\n")
        item, task = self.settled(context=".env")
        # a record written before admission was tracked lists the raw operator specs
        (task / "dispatch.json").write_text(json.dumps({"context": [".env", "spec.txt"], "notes": []}))
        packet = self.retry_packet(item)
        self.assertNotIn("PRIVATE_TOKEN=example", packet)
        self.assertNotIn("Sensitive context .env was omitted", packet)
        self.assertIn("SPEC_MARKER", packet)
        self.assertIn("1 context input(s) omitted as sensitive", packet)

    def test_retry_without_dispatch_record_points_at_prior_packet(self):
        (self.project / "spec.txt").write_text("SPEC_MARKER\n")
        item, task = self.settled(context="spec.txt", note="Keep API stable")
        (task / "dispatch.json").unlink()
        packet = self.retry_packet(item, "--tests", "affected")
        self.assertIn("has no dispatch record, so its selected context and notes were not replayed", packet)
        self.assertIn(f"Prior packet: {(task / 'packet.md').resolve()}", packet)
        self.assertNotIn("SPEC_MARKER", packet)

    def test_retry_with_malformed_dispatch_record_replays_nothing(self):
        (self.project / "spec.txt").write_text("SPEC_MARKER\n")
        item, task = self.settled(context="spec.txt", note="Keep API stable")
        for raw in ("[]", "null", "{not json", '{"context":null,"notes":[]}', '{"context":[],"notes":null}',
                    '{"context":["spec.txt",7],"notes":[]}', '{"context":"spec.txt","notes":[]}',
                    '{"context":["spec.txt"],"omitted_context":{},"notes":[]}'):
            with self.subTest(raw=raw):
                (task / "dispatch.json").write_text(raw)
                again = self.retry(item, "Again", "--tests", "affected")
                self.assertIn("not used", again["prior_record"])
                packet = self.packet(again)
                self.assertIn("prior dispatch record is malformed", packet)
                self.assertNotIn("SPEC_MARKER", packet)
                self.assertNotIn("Keep API stable", packet)
                for path in (self.state / "tasks").iterdir():
                    (path / "state.json").write_text(WAITING)

    def test_retry_replays_more_than_twenty_inputs(self):
        args = ["dispatch", "--role", "coder", "--task", "Many inputs", "--worker", "pi",
                "--model", MODEL, "--workdir", str(self.project)]
        for i in range(21):
            (self.project / f"ctx{i}.txt").write_text(f"CTX_MARKER_{i:02d}\n")
            args += ["--context", f"ctx{i}.txt", "--note", f"NOTE_MARKER_{i:02d}"]
        item = self.cli(*args)
        self.settle(item)
        packet = self.retry_packet(item)
        for i in range(21):
            self.assertIn(f"CTX_MARKER_{i:02d}", packet)
            self.assertIn(f"NOTE_MARKER_{i:02d}", packet)

    def test_incompatible_harness_model_is_rejected(self):
        run = self.raw("dispatch", "--role", "coder", "--task", "Check config", "--worker", "claude",
                       "--model", MODEL, "--workdir", str(self.project), timeout=10)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("belongs to pi", run.stderr)


class SensitiveReadTests(unittest.TestCase):
    def test_known_files_and_common_shell_routes_prompt(self):
        # A relative root such as `find .` is judged against the working directory, so name it
        # rather than inherit the directory the test run happens to start in.
        cwd = str(ROOT)
        cases = [
            ("Read", {"file_path": ".env"}),
            ("Read", {"file_path": "config/appsettings.Production.json"}),
            ("Grep", {"path": "/tmp/project/.aws/credentials"}),
            ("Read", {"file_path": "/tmp/home/.config/gcloud/application_default_credentials.json"}),
            ("Read", {"file_path": "/tmp/home/.config/gh/hosts.yml"}),
            ("Bash", {"command": "cat .env"}),
            ("Bash", {"command": "rg TOKEN .env.local"}),
            ("Bash", {"command": "rg --hidden password ."}),
            ("Bash", {"command": "python3 -c 'from pathlib import Path; print(Path(\".env\").read_text())'"}),
            ("Bash", {"command": "find . -type f -exec cat {} \\;"}),
            ("Bash", {"command": "find . -type f -print0 | xargs -0 cat"}),
        ]
        for tool, params in cases:
            with self.subTest(tool=tool, params=params):
                self.assertTrue(classify(tool, params, cwd)["sensitive"])
                response = hook_response({"tool_name": tool, "tool_input": params, "cwd": cwd})
                self.assertEqual(response["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertFalse(classify("Bash", {"command": "rg handler src"}, cwd)["sensitive"])
        self.assertFalse(classify("Read", {"file_path": "src/handler.py"}, cwd)["sensitive"])

    def test_claude_hook_keeps_request_pending_until_matching_tool_settles(self):
        with tempfile.TemporaryDirectory() as folder:
            def hook(name, path):
                event = {"hook_event_name": name, "tool_name": "Read", "tool_input": {"file_path": path}}
                return subprocess.run([str(ROOT / "bin/tp-sensitive-read"), "--claude-hook"],
                                      input=json.dumps(event), text=True, capture_output=True,
                                      env={**outside_session(), "TP_TASK_DIR": folder, "TP_REDACTION": "shadow"}, check=True)
            asked = hook("PreToolUse", ".env")
            self.assertEqual(json.loads(asked.stdout)["hookSpecificOutput"]["permissionDecision"], "ask")
            request = Path(folder) / "permission-request.json"
            hook("PostToolUse", "README.md")
            self.assertEqual(json.loads(request.read_text())["status"], "pending")
            hook("PermissionDenied", ".env")
            self.assertEqual(json.loads(request.read_text())["status"], "denied")

    def test_claude_main_hook_asks_without_worker_task_directory(self):
        event = {"hook_event_name": "PreToolUse", "tool_name": "Read",
                 "tool_input": {"file_path": ".env"}}
        env = {**outside_session(), "TP_REDACTION": "shadow"}  # in "on" mode tp-redact gates instead
        env.pop("TP_TASK_DIR", None)
        run = subprocess.run([str(ROOT / "claude/plugins/tmux-pi/hooks/sensitive-read.sh")],
                             input=json.dumps(event), text=True, capture_output=True,
                             env=env, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["hookSpecificOutput"]["permissionDecision"], "ask")


if __name__ == "__main__":
    unittest.main()
