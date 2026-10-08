"""tp-review gates, the reviewer chosen by the review plan, and the oversized-packet boundary (stubbed tmux/tp-spawn/tp-route;
no models). The plan itself is tested in test_model_routing."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
SECRET = "SECRET-REQUEST-BODY"
PLAN = {"review": "required", "policy": "required",
        "reviewer": {"worker": "pi", "model": "openai-codex/gpt-6-sol", "thinking": "medium", "independence": "different_provider"}}


class TpReviewTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.root = base / "root"
        (self.root / "bin").mkdir(parents=True)
        (self.root / "lib").mkdir()
        shutil.copy(ROOT / "bin/tp-review", self.root / "bin/tp-review")
        shutil.copy(ROOT / "lib/tp.sh", self.root / "lib/tp.sh")
        # tp-review asks tp-agent's classifier whether the worker is paused (tp-agent shares lib/contract.py's pause rule).
        shutil.copy(ROOT / "bin/tp-agent", self.root / "bin/tp-agent")
        shutil.copy(ROOT / "lib/contract.py", self.root / "lib/contract.py")
        for name in ("review_verdict.py", "review_rounds.py", "sensitive_read.py", "redact.py", "redact_rules.py", "redaction_store.py",
                     "request_file.py", "lifecycle_files.py", "task_archive.py"):   # the last two: the files contract.py's lifecycle reads
            shutil.copy(ROOT / "lib" / name, self.root / "lib" / name)
        shutil.copy(ROOT / "lib/worker_process.py", self.root / "lib/worker_process.py")
        (self.root / "lib/task_record.py").write_text("def request_text(d):\n    return 'x'\n\ndef refresh_record(state_dir, task_id):\n    return ''\n")
        self.log = base / "spawn.log"
        self.state = base / "state"
        # tp-route --review-plan: prints plan.json and exits with plan.rc (0 plan, 3 no review needed, 4 no independent reviewer)
        self.plan = base / "plan.json"
        self.plan.write_text(json.dumps(PLAN))
        (base / "plan.rc").write_text("0")
        self.stub("tp-route", f"#!/usr/bin/env bash\necho \"$@\" > \"{base}/route.args\"\ncat \"{self.plan}\"\nexit \"$(cat \"{base}/plan.rc\")\"\n")
        # Record each --context file's length and content head instead of launching a worker.
        self.stub("tp-spawn", f'''#!/usr/bin/env bash
args=("$@"); : > "{self.log}"; echo "$*" > "{base}/spawn.args"
[[ -z "$STUB_FAIL" ]] || {{ for ((i=0;i<${{#args[@]}};i++)); do [[ "${{args[i]}}" == --context ]] && echo "${{args[i+1]}}" >> "{self.log}.ctx"; done; exit 1; }}
for ((i=0;i<${{#args[@]}};i++)); do
  if [[ "${{args[i]}}" == --context ]]; then f="${{args[i+1]}}"; echo "$f $(wc -c < "$f" | tr -d ' ')" >> "{self.log}"; cat "$f" >> "{self.log}.body"; mkdir -p "{self.log}.copies"; cp "$f" "{self.log}.copies/$(basename "$f")"; fi
  if [[ "${{args[i]}}" == -- ]]; then echo "TASK ${{args[i+1]}}" >> "{self.log}"; fi
done
mkdir -p "{self.state}/tasks/review-1"
echo '{{"id":"review-1"}}' > "{self.state}/tasks/review-1/meta.json"
echo review-1
''')
        self.state = base / "state"
        self.task = self.state / "tasks" / "w1"
        self.task.mkdir(parents=True)
        self.work = base / "work"
        self.work.mkdir()
        fake = base / "fakebin"
        fake.mkdir()
        tmux = fake / "tmux"
        tmux.write_text(f'#!/usr/bin/env bash\nif [[ "$1" == list-panes ]]; then echo "%0 1 99999999"; else echo "{self.state}"; fi\n')
        tmux.chmod(0o755)
        self.env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}", "TP_SESSION": "t"}
        self.env.pop("TMUX", None)
        self.env.pop("TP_STATE_DIR", None)  # the fake session defines its own state dir
        self.env["TMPDIR"] = str(base / "tmp")
        (base / "tmp").mkdir()
        (self.task / "meta.json").write_text(json.dumps({"id": "w1", "worker": "pi", "workdir": str(self.work)}))
        for name in ("task.md", "packet.md", "result.md"):
            (self.task / name).write_text(f"{name}\n")
        (self.task / "result.md").write_text("result.md\nRESULT_STATUS: complete\n")
        (self.task / "state.json").write_text('{"state":"waiting"}')
        (self.task / "request.md").write_text("do it\n")

    def stub(self, name, body):
        path = self.root / "bin" / name
        path.write_text(body)
        path.chmod(0o755)

    def review(self, *args):
        return subprocess.run([str(self.root / "bin/tp-review"), *args, "w1"], text=True, capture_output=True, env=self.env)

    def request(self, status):
        (self.task / "capability-request.json").write_text(json.dumps({"kind": "tool", "what": SECRET, "status": status}))

    def set_plan(self, rc=0, **plan):
        self.plan.write_text(json.dumps({**PLAN, **plan}))
        (self.plan.parent / "plan.rc").write_text(str(rc))

    def spawn_args(self):
        return (self.plan.parent / "spawn.args").read_text()

    def test_a_task_in_the_session_archive_is_named_by_place_under_either_archive_name(self):
        archive = self.state / "archive"
        archive.mkdir()
        for name in ("w1", "w1-ended-20261008010101", "w1-ended-20261008010101-1"):
            with self.subTest(archived_as=name):
                if self.task.exists():
                    self.task.rename(archive / name)
                else:
                    (archive / "w1").rename(archive / name)
                r = self.review()
                self.assertEqual(r.returncode, 1)
                self.assertIn(f"in the session archive at {archive / name}", r.stderr)
                self.assertNotIn("no subagent", r.stderr)
                (archive / name).rename(archive / "w1") if name != "w1" else None
                self.task = archive / "w1"
        r = subprocess.run([str(self.root / "bin/tp-review"), "nope"], env=self.env, capture_output=True, text=True)
        self.assertIn("no subagent 'nope'", r.stderr)

    def test_reviewer_comes_from_the_review_plan(self):
        self.set_plan(reviewer={"worker": "claude", "model": "claude-sonnet-5-5", "thinking": "high", "independence": "different_provider"})
        r = self.review()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--worker claude --model claude-sonnet-5-5 --thinking high", self.spawn_args())
        self.assertIn("Review policy: required (different_provider)", self.spawn_args())
        meta = json.loads((self.task / "meta.json").read_text())
        self.assertEqual(meta["reviewer"], {"worker": "claude", "model": "claude-sonnet-5-5", "thinking": "high", "independence": "different_provider"})
        self.assertEqual(meta["review_policy"], "required")
        self.assertIn(f"--review-plan {self.task}", (self.plan.parent / "route.args").read_text())

    def test_no_review_needed_exits_3_without_a_reviewer_and_force_asks_for_one(self):
        self.set_plan(rc=3, review="none")
        r = self.review()
        self.assertEqual(r.returncode, 3)
        self.assertIn("review not needed", r.stderr)
        self.assertFalse(self.log.exists())
        self.set_plan(review="forced")
        self.assertEqual(self.review("--force").returncode, 0)
        self.assertIn("--force", (self.plan.parent / "route.args").read_text())

    def test_no_independent_provider_stops_and_asks_the_human(self):
        self.set_plan(rc=4)
        r = self.review()
        self.assertEqual(r.returncode, 1)
        self.assertIn("no independent reviewer", r.stderr)
        self.assertIn("ask the human", r.stderr)
        self.assertFalse(self.log.exists())

    def test_a_failed_plan_never_falls_back_to_a_fixed_reviewer(self):
        self.set_plan(rc=1)
        r = self.review()
        self.assertEqual(r.returncode, 1)
        self.assertIn("cannot plan a review", r.stderr)
        self.assertFalse(self.log.exists())

    def test_level_effort_and_approval_flags_reach_the_plan(self):
        r = subprocess.run([str(self.root / "bin/tp-review"), "--level", "top", "--thinking", "high", "--approve-effort", "w1"],
                           text=True, capture_output=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        args = (self.plan.parent / "route.args").read_text()
        for part in ("--level top", "--thinking high", "--approve-effort"):
            self.assertIn(part, args)

    def test_pending_request_blocks_even_forced_without_leaking(self):
        self.request("pending")
        for args in ((), ("--force",)):
            r = self.review(*args)
            self.assertEqual(r.returncode, 1)
            self.assertIn("unresolved", r.stderr)
            self.assertNotIn(SECRET, r.stdout + r.stderr)
        self.assertFalse(self.log.exists())

    def test_unreadable_missing_null_unknown_or_non_object_status_blocks_even_forced(self):
        for raw in ('{not json', '{}', '{"what":"%s","status":null}' % SECRET, '{"status":"requested"}',
                    '{"status":7}', '[]', '"answered"'):
            (self.task / "capability-request.json").write_text(raw)
            for args in ((), ("--force",)):
                with self.subTest(raw=raw, args=args):
                    r = self.review(*args)
                    self.assertEqual(r.returncode, 1)
                    self.assertIn("unresolved", r.stderr)
                    self.assertNotIn(SECRET, r.stdout + r.stderr)
        self.assertFalse(self.log.exists())

    def test_answered_or_absent_request_allows_review(self):
        self.assertEqual(self.review().returncode, 0)
        self.request("answered")
        r = self.review()
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "review-1"), r.stderr)

    def test_waiting_worker_without_a_result_status_is_not_reviewed(self):
        (self.task / "result.md").write_text("Started a sleep; waiting.\n")
        r = self.review()
        self.assertEqual(r.returncode, 1)
        self.assertIn("without a result status", r.stderr)
        self.assertFalse(self.log.exists())
        (self.task / "exit").write_text("0\n")  # a clean exit does not excuse a missing status
        self.assertEqual(self.review().returncode, 1)
        (self.task / "result.md").write_text("RESULT_STATUS: blocked\n")
        self.assertEqual(self.review().returncode, 0)

    def test_recorded_pause_is_not_reviewed_whatever_the_exit_and_status_say(self):
        (self.task / "exit").write_text("0\n")  # clean exit and a statused reply (set up above)
        (self.task / "state.json").write_text('{"state":"paused"}')
        r = self.review()
        self.assertEqual(r.returncode, 1)
        self.assertIn("is paused", r.stderr)
        self.assertFalse(self.log.exists())
        (self.task / "state.json").write_text('{"state":"waiting","tools":3}')
        (self.task / "checkpoints.json").write_text('{"events":[{"action":"pause","at":1,"tools":3}]}')
        self.assertEqual(self.review().returncode, 1)
        (self.task / "checkpoints.json").write_text('{"events":[{"action":"pause","at":1,"tools":3},{"action":"extend","at":2}]}')
        self.assertEqual(self.review().returncode, 0)

    def test_legacy_pending_capability_still_blocks(self):
        (self.task / "pending-capability.json").write_text("{}")
        self.assertEqual(self.review().returncode, 1)

    def test_generic_and_research_prompts_require_verdict_then_result_status(self):
        self.review()
        generic = self.log.read_text()
        meta = json.loads((self.task / "meta.json").read_text())
        meta["contract"] = {"kind": "research", "artifact": "/tmp/report.md"}
        (self.task / "meta.json").write_text(json.dumps(meta))
        self.review()
        research = self.log.read_text()
        for task in (generic, research):
            self.assertIn("first line of your final reply must be exactly APPROVED, APPROVED_WITH_NOTES or REQUEST_CHANGES", task)
            self.assertIn("Return every finding in this one pass, ranked most severe first", task)
            self.assertIn("'defect'", task)
            self.assertIn("'note'", task)
            self.assertIn("Notes are returned in full but never justify another round", task)
            self.assertIn("REQUEST_CHANGES only when at least one finding is a defect", task)
            self.assertIn("name the family it belongs to", task)
            self.assertNotIn("review round", task)  # a first review does not claim earlier rounds
            self.assertRegex(task, r"RESULT_STATUS: complete\|empty\|blocked\|incomplete on the second line")
        self.assertIn("report.md", research)

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.work), "-c", "user.email=t@t", "-c", "user.name=t", *args], check=True, capture_output=True)

    def reviewer_dir(self, ident, text, workdir_state=None):
        directory = self.state / "tasks" / ident
        directory.mkdir()
        (directory / "meta.json").write_text(json.dumps({"id": ident, "worker": "claude"}))
        (directory / "result.md").write_text(text)
        if workdir_state:
            (directory / "reviewed-state.json").write_text(json.dumps(workdir_state))
        return directory

    def test_a_first_review_outside_git_attaches_nothing_extra_and_records_no_state(self):
        r = self.review()
        self.assertEqual(r.returncode, 0, r.stderr)
        names = [Path(p).name for p in self.spawn_contexts()]
        self.assertEqual(names, ["task.md", "packet.md", "result.md"])
        self.assertFalse((self.state / "tasks" / "review-1" / "reviewed-state.json").exists())
        meta = json.loads((self.task / "meta.json").read_text())
        self.assertEqual(meta["reviews"], ["review-1"])

    def test_a_first_review_in_a_repository_records_the_reviewed_tree_and_no_rounds(self):
        self.git("init", "-q")
        (self.work / "a.txt").write_text("one\n")
        r = self.review()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([Path(p).name for p in self.spawn_contexts()], ["task.md", "packet.md", "result.md"])
        state = json.loads((self.state / "tasks" / "review-1" / "reviewed-state.json").read_text())
        self.assertRegex(state["tree"], r"^[0-9a-f]{40,64}$")
        self.assertEqual(Path(state["workdir"]).resolve(), self.work.resolve())
        # the repository's own index is not touched
        status = subprocess.run(["git", "-C", str(self.work), "status", "--porcelain"], capture_output=True, text=True).stdout
        self.assertIn("?? a.txt", status)

    def test_a_retried_task_gets_earlier_findings_in_order_and_the_diff_since_the_last_review(self):
        self.git("init", "-q")
        (self.work / "a.txt").write_text("one\n")
        first = self.review()
        self.assertEqual(first.returncode, 0, first.stderr)
        (self.state / "tasks" / "review-1" / "result.md").write_text("REQUEST_CHANGES\nRESULT_STATUS: complete\nF1 defect: A-FIRST-FINDING\n")
        # a retry of w1 that edits the file and is reviewed in turn
        (self.work / "a.txt").write_text("two\n")
        retry = self.state / "tasks" / "w2"
        shutil.copytree(self.task, retry)
        meta = json.loads((retry / "meta.json").read_text())
        meta.update({"id": "w2", "parent": "w1"})
        meta.pop("review", None), meta.pop("reviews", None)
        (retry / "meta.json").write_text(json.dumps(meta))
        r = subprocess.run([str(self.root / "bin/tp-review"), "w2"], text=True, capture_output=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        names = [Path(p).name for p in self.spawn_contexts()]
        self.assertEqual(names[:3], ["task.md", "packet.md", "result.md"])
        self.assertEqual(names[3], "earlier-findings-01-review-1.md")
        self.assertEqual(names[4], "diff-since-round-1.md")
        copies = Path(str(self.log) + ".copies")
        self.assertIn("A-FIRST-FINDING", (copies / names[3]).read_text())
        diff = (copies / names[4]).read_text()
        self.assertIn("-one", diff)
        self.assertIn("+two", diff)
        self.assertIn("This is review round 2 of this work", self.log.read_text())
        self.assertIn("round 1 F2", self.log.read_text())
        # every round is a full review: the whole change and what it can affect, with a Coverage section, never only the fixes
        self.assertIn("Review the whole change every time", self.log.read_text())
        self.assertIn("Do not stop when you have enough findings", self.log.read_text())
        self.assertIn("section headed Coverage", self.log.read_text())
        self.assertIn("not only the fixes", self.log.read_text())
        for role in ("code-reviewer", "data-flow-reviewer", "vulnerability-reviewer"):
            text = (ROOT / "agent/roles" / f"{role}.md").read_text()
            self.assertIn("section headed Coverage", text, role)
            self.assertIn("Do not stop when you have enough findings", text, role)

    def retried_with_secrets(self):
        """A first review, then a retry w2 whose working tree gained a .env and a secret-bearing note (made-up values)."""
        self.git("init", "-q")
        (self.work / "a.txt").write_text("one\n")
        first = self.review()
        self.assertEqual(first.returncode, 0, first.stderr)
        (self.state / "tasks" / "review-1" / "result.md").write_text(
            "REQUEST_CHANGES\nRESULT_STATUS: complete\nF1 defect: the env dump shows\nDB_PASSWORD=madeup-FINDING-77qz\nand a key AKIAMADEUP7QZ81VXKD3\n")
        (self.work / "a.txt").write_text("two\n")
        (self.work / ".env").write_text("DB_PASSWORD=madeup-ENVFILE-55kd\n")
        (self.work / "notes.txt").write_text("DB_PASSWORD=madeup-NOTES-31mv\n")
        retry = self.state / "tasks" / "w2"
        shutil.copytree(self.task, retry)
        meta = json.loads((retry / "meta.json").read_text())
        meta.update({"id": "w2", "parent": "w1"})
        meta.pop("review", None), meta.pop("reviews", None)
        (retry / "meta.json").write_text(json.dumps(meta))

    def test_no_file_the_reviewer_is_given_carries_a_secret_from_the_diff_or_the_findings(self):
        self.retried_with_secrets()
        r = subprocess.run([str(self.root / "bin/tp-review"), "w2"], text=True, capture_output=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("'.env' changed since the last round", r.stderr)
        copies = Path(str(self.log) + ".copies")
        files = list(copies.iterdir())
        self.assertGreaterEqual(len(files), 5)
        for f in files:
            for secret in ("madeup-FINDING-77qz", "AKIAMADEUP7QZ81VXKD3", "madeup-ENVFILE-55kd", "madeup-NOTES-31mv"):
                self.assertNotIn(secret, f.read_text(), f.name)
        self.assertNotIn("madeup-", self.log.read_text())
        self.assertIn("+two", (copies / "diff-since-round-1.md").read_text())

    def test_a_review_whose_diff_cannot_be_checked_refuses_and_starts_nothing(self):
        self.retried_with_secrets()
        # the rule cannot be loaded: lib/sensitive_read.py is unreadable as a module in this copy of the install
        (self.root / "lib" / "sensitive_read.py").write_text("raise ImportError('rule unavailable')\n")
        r = subprocess.run([str(self.root / "bin/tp-review"), "w2"], text=True, capture_output=True, env=self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("cannot gather the earlier review rounds", r.stderr)
        self.assertIn("sensitive-read rule", r.stderr)
        self.assertFalse((Path(str(self.log) + ".copies") / "diff-since-round-1.md").exists())  # nothing was sent for round 2
        self.assertNotIn("reviews", json.loads((self.state / "tasks" / "w2" / "meta.json").read_text()))

    def test_two_earlier_rounds_are_attached_oldest_first_across_the_retry_lineage_and_a_rereview(self):
        self.git("init", "-q")
        (self.work / "a.txt").write_text("one\n")
        self.reviewer_dir("old-1", "REQUEST_CHANGES\nF1 OLDEST\n")
        self.reviewer_dir("old-2", "REQUEST_CHANGES\nF1 MIDDLE\n")
        parent = self.state / "tasks" / "w0"
        parent.mkdir()
        (parent / "meta.json").write_text(json.dumps({"id": "w0", "worker": "pi", "workdir": str(self.work), "review": "old-1", "reviews": ["old-1"]}))
        meta = json.loads((self.task / "meta.json").read_text())
        meta.update({"parent": "w0", "review": "old-2", "reviews": ["old-2"]})
        (self.task / "meta.json").write_text(json.dumps(meta))
        r = self.review()
        self.assertEqual(r.returncode, 0, r.stderr)
        names = [Path(p).name for p in self.spawn_contexts() if "earlier-findings" in p or "diff-since" in p]
        self.assertEqual(names, ["earlier-findings-01-old-1.md", "earlier-findings-02-old-2.md", "diff-since-round-2.md"])
        copies = Path(str(self.log) + ".copies")
        self.assertIn("OLDEST", (copies / names[0]).read_text())
        self.assertIn("MIDDLE", (copies / names[1]).read_text())
        # no recorded state for round 2: said so, not guessed
        self.assertIn("recorded no reviewed state", (copies / names[2]).read_text())
        self.assertEqual(json.loads((self.task / "meta.json").read_text())["reviews"], ["old-2", "review-1"])

    def test_recorded_state_that_cannot_be_used_refuses_before_spawning(self):
        self.git("init", "-q")
        self.reviewer_dir("old-1", "REQUEST_CHANGES\nF1\n", {"workdir": str(self.work.resolve()), "tree": "0" * 40})
        meta = json.loads((self.task / "meta.json").read_text())
        meta.update({"review": "old-1", "reviews": ["old-1"]})
        (self.task / "meta.json").write_text(json.dumps(meta))
        r = self.review()
        self.assertEqual(r.returncode, 1)
        self.assertIn("cannot gather the earlier review rounds", r.stderr)
        self.assertFalse((self.plan.parent / "spawn.args").exists())

    def test_unreadable_recorded_state_refuses(self):
        self.reviewer_dir("old-1", "REQUEST_CHANGES\nF1\n")
        (self.state / "tasks" / "old-1" / "reviewed-state.json").write_text("not json")
        meta = json.loads((self.task / "meta.json").read_text())
        meta.update({"review": "old-1"})
        (self.task / "meta.json").write_text(json.dumps(meta))
        r = self.review()
        self.assertEqual(r.returncode, 1)
        self.assertFalse((self.plan.parent / "spawn.args").exists())

    def test_inspect_reports_a_reviewers_verdict_and_notes(self):
        for ident, text, verdict in (("rv-n", "APPROVED_WITH_NOTES\nRESULT_STATUS: complete\nN1 test gap", "APPROVED_WITH_NOTES"),
                                     ("rv-x", "APPROVED_PENDING\nRESULT_STATUS: complete", "unparseable")):
            directory = self.reviewer_dir(ident, text)
            (directory / "review-of").write_text("w1\n")
            run = subprocess.run([str(self.root / "bin/tp-agent"), "inspect", ident], text=True, capture_output=True,
                                 env={**self.env, "TP_STATE_DIR": str(self.state)})
            self.assertEqual(run.returncode, 0, run.stderr)
            got = json.loads(run.stdout)
            self.assertEqual(got["verdict"], verdict)
        self.assertEqual(json.loads(subprocess.run([str(self.root / "bin/tp-agent"), "inspect", "rv-n"], text=True, capture_output=True,
                                                   env={**self.env, "TP_STATE_DIR": str(self.state)}).stdout)["verdict_notes"], "N1 test gap")

    def spawn_contexts(self):
        return [l.split()[0] for l in self.log.read_text().splitlines() if l.startswith("/")]

    def test_oversized_packet_and_result_are_split_losslessly_in_order(self):
        packet = "".join(chr(97 + i % 26) for i in range(100000)) + "PACKET-TAIL-MARKER"
        result = "RESULT_STATUS: complete\n" + "R" * 70000 + "RESULT-TAIL-MARKER"
        (self.task / "packet.md").write_text(packet)
        (self.task / "result.md").write_text(result)
        r = self.review()
        self.assertEqual(r.returncode, 0, r.stderr)
        paths = self.spawn_contexts()
        segs = [p for p in paths if "author-" in Path(p).name]
        self.assertEqual(len(segs), 4)
        for p in segs:
            self.assertLessEqual(len(self.copy(p).read_text()), 60000)
        body = Path(str(self.log) + ".body").read_text()
        self.assertIn("PACKET-TAIL-MARKER", body)
        self.assertIn("RESULT-TAIL-MARKER", body)
        self.assertIn(str(self.task / "packet.md"), body)
        self.assertIn("Review segment 2 of 2 of author-result", body)
        # Ordered concatenation after stripping labels reproduces the originals exactly.
        for label, original in (("author-packet", packet), ("author-result", result)):
            parts = [self.copy(p).read_text().split("]\n\n", 1)[1] for p in segs if label in Path(p).name]
            self.assertEqual("".join(parts), original)
            self.assertEqual(sorted(p for p in segs if label in Path(p).name), [p for p in segs if label in Path(p).name])
        # Temporary segments are removed after success.
        self.assertFalse(any(Path(p).exists() for p in segs))

    def test_aggregate_overflow_fails_before_spawn_and_cleans_up(self):
        (self.task / "packet.md").write_text("p" * 150000)
        (self.task / "result.md").write_text("RESULT_STATUS: complete\n" + "r" * 70000)
        r = self.review()
        self.assertEqual(r.returncode, 1)
        self.assertIn("without dropping author evidence", r.stderr)
        self.assertFalse(self.log.exists())
        self.assertEqual(self.leftover(), [])

    def test_spawn_failure_cleans_up_segments(self):
        (self.task / "packet.md").write_text("p" * 100000)
        self.env["STUB_FAIL"] = "1"
        r = self.review()
        self.assertEqual(r.returncode, 1)
        ctx = Path(str(self.log) + ".ctx").read_text().split()
        self.assertTrue(any("author-packet" in c for c in ctx))
        self.assertFalse(any(Path(c).exists() for c in ctx if "author-packet" in c))

    def copy(self, p):  # the stub spawner snapshots each context file it was handed
        return Path(str(self.log) + ".copies") / Path(p).name

    def leftover(self):
        return [p for p in Path(self.env["TMPDIR"]).glob("tmp.*") if (p / "author-packet.list").exists()]

    def test_packet_exactly_at_limit_is_passed_unchanged(self):
        (self.task / "packet.md").write_text("p" * 60000)
        self.assertEqual(self.review().returncode, 0)
        self.assertIn(str(self.task / "packet.md"), self.log.read_text())

    def preamble_world(self):
        """The reviewer's launch joined to the author's lifecycle: the stub spawner resolves the repository preamble for the
        --preamble-role and --workdir it is handed (the real lib/preamble.py), as tp-spawn does."""
        shutil.copy(ROOT / "lib/preamble.py", self.root / "lib/preamble.py")
        shutil.copytree(ROOT / "agent/roles", self.root / "agent/roles")
        base = self.plan.parent
        self.stub("tp-spawn", f'''#!/usr/bin/env bash
args=("$@"); workdir="" role=""
for ((i=0;i<${{#args[@]}};i++)); do
  [[ "${{args[i]}}" == --workdir ]] && workdir="${{args[i+1]}}"
  [[ "${{args[i]}}" == --preamble-role ]] && role="${{args[i+1]}}"
done
python3 "$(dirname "$0")/../lib/preamble.py" "$workdir" "$role" > "{base}/preamble.out" || exit 1
echo "$*" > "{base}/spawn.args"
mkdir -p "{self.state}/tasks/review-1"
echo '{{"id":"review-1"}}' > "{self.state}/tasks/review-1/meta.json"
echo review-1
''')
        (self.work / ".tmux-pi").mkdir()
        (self.work / ".tmux-pi/worker-preamble.md").write_text(
            "Repo-wide rule.\n\n## Role: code-reviewer\nREVIEWER-ONLY SECTION\n\n## Role: coder\nCODER-ONLY SECTION\n")

    def test_a_paused_author_gets_no_reviewer_and_a_settled_one_gets_the_reviewer_preamble(self):
        self.preamble_world()
        (self.task / "exit").write_text("0\n")
        (self.task / "state.json").write_text('{"state":"waiting","tools":3}')
        (self.task / "checkpoints.json").write_text('{"events":[{"action":"pause","at":1,"tools":3}]}')
        r = self.review()
        self.assertEqual(r.returncode, 1)
        self.assertIn("is paused", r.stderr)
        self.assertFalse((self.plan.parent / "spawn.args").exists())   # no reviewer started, so no preamble was resolved for one
        self.assertFalse((self.plan.parent / "preamble.out").exists())
        (self.task / "checkpoints.json").write_text('{"events":[{"action":"pause","at":1,"tools":3},{"action":"extend","at":2}]}')
        r = self.review()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"--workdir {self.work} --preamble-role code-reviewer", self.spawn_args())   # the author's tree, the reviewer's role
        preamble = (self.plan.parent / "preamble.out").read_text()
        self.assertIn("Repo-wide rule.", preamble)
        self.assertIn("REVIEWER-ONLY SECTION", preamble)
        self.assertNotIn("CODER-ONLY SECTION", preamble)   # the author's role section does not reach the reviewer

    def test_the_pause_is_the_reason_given_even_when_the_repository_preamble_is_unusable(self):
        self.preamble_world()
        (self.work / ".tmux-pi/worker-preamble.md").write_text("## Role: no-such-role\nx\n")
        (self.task / "state.json").write_text('{"state":"waiting","tools":3}')
        (self.task / "checkpoints.json").write_text('{"events":[{"action":"pause","at":1,"tools":3}]}')
        r = self.review()
        self.assertEqual(r.returncode, 1)
        self.assertIn("is paused", r.stderr)
        self.assertNotIn("could not start", r.stderr)
        (self.task / "checkpoints.json").unlink()    # not paused: now the preamble is what stops it
        r = self.review()
        self.assertEqual(r.returncode, 1)
        self.assertIn("could not start", r.stderr)
        self.assertFalse((self.plan.parent / "spawn.args").exists())


if __name__ == "__main__":
    unittest.main()
