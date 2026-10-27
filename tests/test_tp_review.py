"""tp-review gates, the reviewer chosen by the review plan, and the oversized-packet boundary (stubbed tmux/tp-spawn/tp-route;
no models). The plan itself is tested in test_model_routing."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
SECRET = "SECRET-REQUEST-BODY"
PLAN = {"review": "required", "policy": "required",
        "reviewer": {"worker": "pi", "model": "openai/gpt-6-sol", "thinking": "medium", "independence": "different_provider"}}


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
        for name in ("safe_write.py", "review_verdict.py", "review_rounds.py", "sensitive_read.py", "redact.py", "redact_rules.py", "redaction_store.py",
                     "request_file.py", "lifecycle_files.py", "task_archive.py", "lanes.py", "lane_commands.py", "lane_lease.py", "lane_steps.py", "lane_remove.py", "lane_deps.py", "index_text.py", "failing_tests.py", "result_status.py", "work.py", "work_commands.py", "work_find.py", "work_index.py", "work_checkpoint.py", "work_promote.py", "autonomy.py", "owner_confirm.py"):   # the last two: the files contract.py's lifecycle and bin/tp-agent read: the files contract.py's lifecycle reads
            shutil.copy(ROOT / "lib" / name, self.root / "lib" / name)
        shutil.copy(ROOT / "lib/worker_process.py", self.root / "lib/worker_process.py")
        (self.root / "lib/task_record.py").write_text("def request_text(d):\n    return 'x'\n\ndef refresh_record(state_dir, task_id):\n    return ''\n\n"
                                                      "def iter_task_dirs(state_dir, include_archive=True):\n    return iter(())\n")  # lane_steps scans the session with it
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

    def test_same_provider_review_needs_the_users_quote_and_records_it(self):
        for args in (("--same-provider",), ("--same-provider", "--approved-by-user", "  "), ("--approved-by-user", "x")):
            r = self.review(*args)
            self.assertEqual(r.returncode, 1)
            self.assertIn("approv", r.stderr)
            self.assertFalse((self.plan.parent / "route.args").exists())  # refused before any plan
        quote = "use claude, gpt is out of credits"
        self.set_plan(reviewer={"worker": "claude", "model": "claude-sonnet-5-5", "thinking": "high",
                                "independence": "same_provider_user_approved", "approved_by_user": quote})
        r = self.review("--same-provider", "--approved-by-user", quote)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"--same-provider --approved-by-user {quote}", (self.plan.parent / "route.args").read_text())
        self.assertIn("(same_provider_user_approved)", self.spawn_args())
        self.assertIn("same-provider review approved by the user", self.spawn_args())
        self.assertIn("Check every claim yourself", self.spawn_args())
        meta = json.loads((self.task / "meta.json").read_text())
        self.assertEqual(meta["reviewer"]["independence"], "same_provider_user_approved")
        self.assertEqual(meta["reviewer"]["approved_by_user"], quote)

    def test_a_plan_marked_same_provider_without_the_quote_is_refused(self):
        self.set_plan(reviewer={"worker": "claude", "model": "m", "thinking": "high", "independence": "same_provider_user_approved"})
        r = self.review()
        self.assertEqual(r.returncode, 1)
        self.assertIn("without the user's approval quote", r.stderr)

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
            self.assertIn("REQUEST_CHANGES only when at least one finding is a defect as defined above and ends 'Closes by: code'", task)
            for label in ("code", "check", "decision", "none"):
                self.assertIn(f"'Closes by: {label}'", task)
            self.assertIn("A missing or wrong item in the author's report, or a command the author did not run, is never a defect: label it check "
                          "and list it under 'Owed before close'. Run it yourself when you can and give the result.", task)
            self.assertIn("name the family it belongs to", task)
            # Both providers have the redacted tools; a prompt on a bash search outside the worktree stalls an unattended reviewer.
            self.assertIn("Read or search files outside the working tree with safe_read and safe_grep, never with rg or grep through bash", task)
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
        self.assertEqual(names, ["task.md", "packet.md", "result.md", "timer-waits.md"])  # the computed timer file is the only addition
        self.assertFalse((self.state / "tasks" / "review-1" / "reviewed-state.json").exists())
        meta = json.loads((self.task / "meta.json").read_text())
        self.assertEqual(meta["reviews"], ["review-1"])

    def task_text(self):
        return self.log.read_text().split("TASK ", 1)[1]

    def test_a_non_lane_review_lists_changed_and_untracked_files_and_the_scoped_commands(self):
        self.git("init", "-q")
        (self.work / "a.txt").write_text("one\n")
        self.git("add", "a.txt")
        self.git("commit", "-qm", "first")
        (self.work / "a.txt").write_text("two\n")
        (self.work / "new.txt").write_text("fresh\n")
        r = self.review()
        self.assertEqual(r.returncode, 0, r.stderr)
        task = self.task_text()
        self.assertIn("Diff scope: 2 file(s) changed relative to HEAD", task)
        self.assertIn("- a.txt\n", task)
        self.assertIn("- new.txt (untracked: read the file)", task)
        self.assertIn(f"git -C {self.work.resolve()} diff HEAD -- <path>", task)
        self.assertIn("must name explicit file paths from the list above; never run them unscoped", task)

    def test_a_sensitive_path_is_listed_as_withheld_and_not_offered_for_diffing(self):
        self.git("init", "-q")
        (self.work / "a.txt").write_text("one\n")
        (self.work / ".env").write_text("TOKEN=x\n")
        task = self.review() and self.task_text()
        self.assertIn("- a.txt", task)
        self.assertNotIn("- .env", task)
        self.assertIn('Withheld (sensitive paths; never diff, show or read them): .env', task)

    def test_outside_git_the_scope_is_one_line_and_the_review_still_starts(self):
        r = self.review()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Diff scope: the working directory is not a git repository, so there is no changed-file list.", self.task_text())

    def test_a_long_list_is_bounded_with_the_command_for_the_rest(self):
        self.git("init", "-q")
        for i in range(205):
            (self.work / f"f{i:03d}.txt").write_text("x\n")
        self.assertEqual(self.review().returncode, 0)
        task = self.task_text()
        self.assertIn("Diff scope: 205 file(s)", task)
        self.assertIn("- f199.txt", task)
        self.assertNotIn("- f200.txt", task)
        full = self.task / "review-changed-files.txt"
        self.assertIn(f"and 5 more changed file(s): the complete list is in {full.resolve()} (one quoted path per line", task)
        self.assertNotIn("diff --name-only", task)
        self.assertNotIn("changed-files", [Path(p).name for p in self.spawn_contexts()])
        listed = full.read_text().splitlines()
        self.assertEqual(len(listed), 205)
        self.assertIn("f204.txt (untracked)", listed)

    def test_a_list_larger_than_the_packet_budget_still_starts_the_review(self):
        self.git("init", "-q")
        long = "d" * 120
        for i in range(2000):
            (self.work / f"{long}{i:04d}.txt").write_text("x\n")
        r = self.review()
        self.assertEqual(r.returncode, 0, r.stderr)
        full = self.task / "review-changed-files.txt"
        self.assertGreater(full.stat().st_size, 200000)
        self.assertEqual(len(full.read_text().splitlines()), 2000)
        self.assertIn(str(full.resolve()), self.task_text())
        self.assertTrue(all("changed-files" not in Path(p).name for p in self.spawn_contexts()))

    def test_the_suggested_commands_are_literal_so_a_magic_file_name_cannot_select_a_withheld_file(self):
        self.git("init", "-q")
        (self.work / ":(glob)*").write_text("one\n")
        (self.work / ".env").write_text("TOKEN=x\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "first")
        (self.work / ":(glob)*").write_text("two\n")
        (self.work / ".env").write_text("TOKEN=y\n")
        self.assertEqual(self.review().returncode, 0)
        task = self.task_text()
        top = os.path.realpath(self.work)
        self.assertIn(f"`GIT_LITERAL_PATHSPECS=1 git -C {top} diff HEAD -- <path> [<path> ...]`", task)
        self.assertIn("keep the GIT_LITERAL_PATHSPECS=1 prefix", task)
        self.assertIn("- ':(glob)*'\n", task)
        self.assertIn("Withheld (sensitive paths; never diff, show or read them): .env", task)
        diff = ["git", "-C", top, "diff", "--name-only", "HEAD", "--", ":(glob)*"]
        literal = subprocess.run(diff, text=True, capture_output=True, env={**os.environ, "GIT_LITERAL_PATHSPECS": "1"}).stdout.split("\n")
        self.assertEqual([l for l in literal if l], [":(glob)*"])
        control = subprocess.run(diff, text=True, capture_output=True, env={k: v for k, v in os.environ.items() if k != "GIT_LITERAL_PATHSPECS"}).stdout
        self.assertIn(".env", control.split("\n"))  # without the prefix the same command does select the withheld file

    def test_the_overflow_list_is_written_without_following_a_link_at_its_path(self):
        self.git("init", "-q")
        for i in range(201):
            (self.work / f"f{i:03d}.txt").write_text("x\n")
        victim = self.task.parent / "victim.txt"
        victim.write_text("original\n")
        (self.task / "review-changed-files.txt").symlink_to(victim)
        self.assertEqual(self.review().returncode, 0)
        self.assertEqual(victim.read_text(), "original\n")
        full = self.task / "review-changed-files.txt"
        self.assertFalse(full.is_symlink())
        self.assertEqual(len(full.read_text().splitlines()), 201)
        self.assertIn(str(full.resolve()), self.task_text())
        self.assertEqual([p.name for p in self.task.iterdir() if p.name.startswith(".review-changed-files-")], [])

    def test_long_names_are_bounded_by_bytes_and_a_near_limit_packet_still_starts_the_review(self):
        self.git("init", "-q")
        for i in range(200):
            (self.work / f"{i:03d}{'n' * 237}.txt"[:240]).write_text("x\n")
        (self.work / ".env").write_text("TOKEN=x\n")
        (self.task / "packet.md").write_text("p" * 95000)
        (self.task / "result.md").write_text("r" * 35000 + "\nRESULT_STATUS: complete\n")
        r = self.review()
        self.assertEqual(r.returncode, 0, r.stderr)
        task = self.task_text()
        scope = task[task.index("Diff scope:"):]
        listed = [line for line in scope.splitlines() if line.startswith("- ") and not line.startswith("- and ")]
        self.assertGreater(len(listed), 10)
        self.assertLess(len(listed), 200)
        self.assertLessEqual(sum(len(l) + 1 for l in listed), 16384)
        full = self.task / "review-changed-files.txt"
        self.assertIn(f"changed file(s): the complete list is in {full.resolve()}", task)
        lines = full.read_text().splitlines()
        self.assertEqual(len(lines), 201)
        self.assertIn("withheld: .env", lines)

    def test_the_scope_shrinks_to_the_packets_headroom_so_a_near_cap_packet_still_starts(self):
        self.git("init", "-q")
        for i in range(200):
            (self.work / f"f{i:03d}{'a' * 55}").write_text("x\n")
        (self.task / "packet.md").write_text("p" * 159000)
        r = self.review()
        self.assertEqual(r.returncode, 0, r.stderr)
        task = self.task_text()
        scope = task[task.index("Diff scope:"):]
        self.assertIn("the complete list is in", scope)
        self.assertEqual(len((self.task / "review-changed-files.txt").read_text().splitlines()), 200)

    def test_a_file_that_became_a_directory_holding_a_withheld_path_is_withheld_too(self):
        self.git("init", "-q")
        (self.work / "box").write_text("one\n")
        (self.work / "a.txt").write_text("one\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "first")
        (self.work / "box").unlink()
        (self.work / "box").mkdir()
        (self.work / "box/inner.txt").write_text("x\n")
        (self.work / "box/.env").write_text("TOKEN=x\n")
        (self.work / "a.txt").write_text("two\n")
        self.git("add", "-A")
        self.assertEqual(self.review().returncode, 0)
        task = self.task_text()
        scope = task[task.index("Diff scope:"):]
        listed = [line[2:].split(" (")[0] for line in scope.splitlines() if line.startswith("- ")]
        self.assertEqual(listed, ["a.txt", "box/inner.txt"])
        self.assertIn("Withheld (sensitive paths; never diff, show or read them): box, box/.env", scope)
        # every offered command, on every listed path, leaves the withheld file out
        top = os.path.realpath(self.work)
        env = {**os.environ, "GIT_LITERAL_PATHSPECS": "1"}
        for layer in ([], ["--cached"]):
            names = subprocess.run(["git", "-C", top, "diff", "--name-only", *layer, "HEAD", "--", *listed],
                                   text=True, capture_output=True, env=env).stdout.split("\n")
            self.assertNotIn("box/.env", names)
            self.assertNotIn("box", names)
        control = subprocess.run(["git", "-C", top, "diff", "--name-only", "HEAD", "--", "box"], text=True, capture_output=True, env=env).stdout
        self.assertIn("box/.env", control.split("\n"))  # the directory path itself would have selected it

    def test_a_staged_change_the_working_tree_undoes_is_listed_with_the_staged_command(self):
        self.git("init", "-q")
        (self.work / "a.txt").write_text("one\n")
        self.git("add", "a.txt")
        self.git("commit", "-qm", "first")
        (self.work / "a.txt").write_text("two\n")
        self.git("add", "a.txt")
        (self.work / "a.txt").write_text("one\n")
        self.assertEqual(self.review().returncode, 0)
        task = self.task_text()
        top = os.path.realpath(self.work)
        self.assertIn("Diff scope: 1 file(s) changed relative to HEAD", task)
        self.assertIn("- a.txt\n", task)
        self.assertIn(f"`GIT_LITERAL_PATHSPECS=1 git -C {top} diff --cached HEAD -- <path> [<path> ...]` (staged)", task)
        staged = subprocess.run(["git", "-C", top, "diff", "--cached", "HEAD", "--", "a.txt"], text=True, capture_output=True).stdout
        self.assertIn("+two", staged)

    def test_an_overflow_list_that_cannot_be_written_gives_the_one_line_fallback(self):
        self.git("init", "-q")
        for i in range(201):
            (self.work / f"f{i:03d}.txt").write_text("x\n")
        (self.task / "review-changed-files.txt").mkdir()
        r = self.review()
        self.assertEqual(r.returncode, 0, r.stderr)
        task = self.task_text()
        scope = task[task.index("Diff scope:"):]
        self.assertTrue(scope.startswith("Diff scope: the changed-file list could not be computed (the complete list of 201 paths could not be written"))
        self.assertIn("inspect the files you need directly.", scope)
        self.assertNotIn("- f000.txt", task)
        self.assertNotIn("not listed here", task)

    def test_a_filename_with_a_newline_stays_one_quoted_literal(self):
        self.git("init", "-q")
        (self.work / "odd\nname.txt").write_text("x\n")
        self.assertEqual(self.review().returncode, 0)
        task = self.task_text()
        self.assertIn("- $'odd\\x0aname.txt' (untracked: read the file)", task)
        self.assertEqual(len([line for line in task.splitlines() if line.startswith("- ")]), 1)

    def test_a_directory_with_a_space_is_quoted_in_the_suggested_commands(self):
        spaced = self.work.parent / "my work"
        spaced.mkdir()
        meta = json.loads((self.task / "meta.json").read_text())
        meta["workdir"] = str(spaced)
        (self.task / "meta.json").write_text(json.dumps(meta))
        subprocess.run(["git", "-C", str(spaced), "init", "-q"], check=True)
        (spaced / "a b.txt").write_text("x\n")
        self.assertEqual(self.review().returncode, 0)
        task = self.task_text()
        top = os.path.realpath(spaced)
        self.assertIn(f"git -C '{top}' diff ", task)
        self.assertIn("- 'a b.txt' (untracked", task)

    def test_a_lane_task_is_scoped_to_the_lanes_base(self):
        sys.path.insert(0, str(ROOT / "lib"))
        import lanes
        self.git("init", "-q", "-b", "main")
        (self.work / "a.txt").write_text("one\n")
        self.git("add", "a.txt")
        self.git("commit", "-qm", "base")
        base = subprocess.run(["git", "-C", str(self.work), "rev-parse", "HEAD"], text=True, capture_output=True).stdout.strip()
        wt = self.work.parent / "lane-wt"
        self.git("worktree", "add", "-q", "-b", "lane-b", str(wt))
        subprocess.run(["git", "-C", str(wt), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x"], check=True)
        (wt / "b.txt").write_text("committed\n")
        subprocess.run(["git", "-C", str(wt), "add", "b.txt"], check=True)
        subprocess.run(["git", "-C", str(wt), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "on the lane"], check=True)
        (wt / "c.txt").write_text("untracked\n")
        work, wt = os.path.realpath(self.work), os.path.realpath(wt)
        common = lanes.git_common_dir(work)
        record = {"schema": "tp-lane:v1", "name": "alpha", "kind": "work", "state": "open", "repo": work, "git_common_dir": common,
                  "branch": "lane-b", "worktree": wt, "base": {"ref": "main", "commit": base}, "created": "2026-10-08T10:00:00+00:00",
                  "domain": "d", "touches": ["lib"], "closed": [], "integrated": None, "defaulted": []}
        with lanes.lane_lock(common):
            lanes.write_lane(common, record)
        work = wt
        out = subprocess.run([sys.executable, str(self.root / "lib/review_rounds.py"), "scope", work, "alpha"],
                             text=True, capture_output=True, env=self.env)
        self.assertEqual(out.returncode, 0, out.stderr)
        text = json.loads(out.stdout)["text"]
        self.assertIn(f"Diff scope: 2 file(s) changed relative to {base[:12]} (committed on the lane since its base", text)
        self.assertIn("- b.txt\n", text)
        self.assertIn("- c.txt (untracked", text)
        self.assertNotIn("a.txt", text)
        self.assertIn(f"diff {base} -- <path>", text)
        # a lane the record does not hold: one line, exit 0
        gone = subprocess.run([sys.executable, str(self.root / "lib/review_rounds.py"), "scope", work, "nope"], text=True, capture_output=True)
        self.assertEqual(gone.returncode, 0)
        self.assertIn("could not be computed", json.loads(gone.stdout)["text"])

    def test_a_first_review_in_a_repository_records_the_reviewed_tree_and_no_rounds(self):
        self.git("init", "-q")
        (self.work / "a.txt").write_text("one\n")
        r = self.review()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([Path(p).name for p in self.spawn_contexts()], ["task.md", "packet.md", "result.md", "timer-waits.md"])
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
        (self.task / "packet.md").write_text("p" * 230000)
        (self.task / "result.md").write_text("RESULT_STATUS: complete\n" + "r" * 80000)
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
        self.assertIn("--preamble-role code-reviewer --review-of w1 ", self.spawn_args())   # the autonomy gate admits a review by this flag
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

    # --evidence / --contract: what Main hands the reviewer (P3, R8)
    def supplied(self, name, text="line one\nline two\n"):
        path = self.work / name
        path.write_text(text)
        return str(path)

    def test_evidence_is_attached_under_its_heading_with_its_full_text_and_the_task_names_it(self):
        a = self.supplied("a.txt", "COUNT: 42\nlog line\n")
        r = self.review("--evidence", a)
        self.assertEqual(r.returncode, 0, r.stderr)
        body = Path(str(self.log) + ".body").read_text()
        self.assertIn("Evidence supplied by Main: a.txt", body)
        self.assertIn("COUNT: 42\nlog line\n", body)
        self.assertIn("produced by the orchestrator, not the author", self.task_text())
        self.assertIn("checked like any other claim", self.task_text())
        self.assertNotIn("Reviewer contract", self.task_text())

    def test_two_evidence_files_are_attached_in_the_order_given(self):
        b = self.supplied("b.txt", "BBB-TEXT\n")
        a = self.supplied("a.txt", "AAA-TEXT\n")
        r = self.review("--evidence", b, "--evidence", a)
        self.assertEqual(r.returncode, 0, r.stderr)
        body = Path(str(self.log) + ".body").read_text()
        self.assertLess(body.index("Evidence supplied by Main: b.txt"), body.index("BBB-TEXT"))
        self.assertLess(body.index("BBB-TEXT"), body.index("Evidence supplied by Main: a.txt"))
        self.assertLess(body.index("Evidence supplied by Main: a.txt"), body.index("AAA-TEXT"))
        self.assertIn("2 evidence file(s)", self.task_text())

    def test_a_contract_is_attached_and_the_task_says_to_judge_against_it(self):
        c = self.supplied("c.md", "The reviewer must check X.\n")
        r = self.review("--contract", c)
        self.assertEqual(r.returncode, 0, r.stderr)
        body = Path(str(self.log) + ".body").read_text()
        self.assertIn("Reviewer contract (supplied by Main)", body)
        self.assertIn("The reviewer must check X.\n", body)
        self.assertIn("Judge the work against that contract together with the task", self.task_text())
        self.assertNotIn("Evidence supplied by Main", self.task_text())

    def test_a_missing_or_empty_file_is_refused_naming_it_and_no_reviewer_starts(self):
        missing = str(self.work / "missing.txt")
        empty = self.supplied("empty.txt", "")
        folder = self.work / "dir.txt"
        folder.mkdir()
        for flag in ("--evidence", "--contract"):
            for path, why in ((missing, "does not exist"), (empty, "is empty"), (str(folder), "not a regular file")):
                with self.subTest(flag=flag, path=path):
                    r = self.review(flag, path)
                    self.assertEqual(r.returncode, 1)
                    self.assertIn(f"'{path}'", r.stderr)
                    self.assertIn(why, r.stderr)
                    self.assertFalse((self.plan.parent / "route.args").exists())  # refused before any plan
                    self.assertFalse((self.plan.parent / "spawn.args").exists())

    def test_an_unreadable_file_is_refused_naming_it(self):
        locked = self.supplied("locked.txt")
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o644)
        if os.access(locked, os.R_OK):
            self.skipTest("running as a user that reads everything")
        r = self.review("--evidence", locked)
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"'{locked}'", r.stderr)
        self.assertFalse((self.plan.parent / "spawn.args").exists())

    def test_a_dotenv_file_and_a_link_to_one_are_refused(self):
        env_file = self.supplied(".env.local", "KEY=value\n")
        link = self.work / "notes.txt"
        link.symlink_to(env_file)
        for path in (env_file, str(link)):
            with self.subTest(path=path):
                r = self.review("--evidence", path)
                self.assertEqual(r.returncode, 1)
                self.assertIn(f"'{path}'", r.stderr)
                self.assertIn(".env", r.stderr)
                self.assertFalse((self.plan.parent / "spawn.args").exists())

    def test_a_file_inside_the_trust_store_is_refused_even_when_it_is_named_by_a_link(self):
        import pwd
        store = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".config/tmux-pi/trust"
        link = self.work / "t.txt"
        link.symlink_to(store / "nothing-here.json")  # never created or read: the place alone refuses
        for path in (str(store / "nothing-here.json"), str(link)):
            with self.subTest(path=path):
                r = self.review("--evidence", path)
                self.assertEqual(r.returncode, 1)
                self.assertIn(f"'{path}'", r.stderr)
                self.assertIn("trust store", r.stderr)
                self.assertFalse((self.plan.parent / "spawn.args").exists())

    def test_the_contract_may_be_given_only_once(self):
        c1, c2 = self.supplied("c1.md"), self.supplied("c2.md")
        r = self.review("--contract", c1, "--contract", c2)
        self.assertEqual(r.returncode, 1)
        self.assertIn("--contract may be given only once", r.stderr)
        self.assertIn("usage:", r.stderr)
        self.assertFalse((self.plan.parent / "spawn.args").exists())

    def test_an_empty_flag_value_is_a_missing_file_for_both_flags(self):
        for flag in ("--contract", "--evidence"):
            with self.subTest(flag=flag):
                r = self.review(flag, "")
                self.assertEqual(r.returncode, 1)
                self.assertIn(flag, r.stderr)
                self.assertFalse((self.plan.parent / "route.args").exists())
                self.assertFalse((self.plan.parent / "spawn.args").exists())

    def test_a_large_file_is_split_into_labelled_segments_that_hold_every_character(self):
        text = "".join(chr(97 + i % 26) for i in range(100000)) + "EVIDENCE-TAIL"
        r = self.review("--evidence", self.supplied("big.txt", text))
        self.assertEqual(r.returncode, 0, r.stderr)
        segs = [p for p in self.spawn_contexts() if "main-evidence" in Path(p).name]
        self.assertEqual(len(segs), 2)
        first = self.copy(segs[0]).read_text()
        self.assertIn("Review segment 1 of 2 of main-evidence-01", first)
        self.assertIn("big.txt", first)
        pieces = [self.copy(p).read_text().split("]\n\n", 1)[1] for p in segs]
        self.assertIn("Evidence supplied by Main: big.txt", pieces[0])
        self.assertTrue(("".join(pieces)).endswith(text))
        self.assertFalse(any(Path(p).exists() for p in segs))  # removed after the start

    def test_evidence_that_overflows_the_packet_budget_is_refused_without_trimming(self):
        (self.task / "packet.md").write_text("p" * 200000)
        r = self.review("--evidence", self.supplied("big.txt", "e" * 150000))
        self.assertEqual(r.returncode, 1)
        self.assertIn("without dropping author evidence", r.stderr)
        self.assertFalse(self.log.exists())
        self.assertEqual(self.leftover(), [])
        (self.task / "packet.md").write_text("p" * 20000)  # the same evidence fits beside a smaller packet
        self.assertEqual(self.review("--evidence", str(self.work / "big.txt")).returncode, 0)

    def test_a_contract_counts_against_the_budget_too(self):
        (self.task / "packet.md").write_text("p" * 200000)
        r = self.review("--contract", self.supplied("c.md", "c" * 150000))
        self.assertEqual(r.returncode, 1)
        self.assertIn("without dropping author evidence", r.stderr)

    def test_without_the_new_flags_the_packet_and_the_task_text_have_nothing_of_them(self):
        r = self.review()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([Path(p).name for p in self.spawn_contexts()], ["task.md", "packet.md", "result.md", "timer-waits.md"])
        for word in ("Main", "evidence file", "Reviewer contract", "Dispositions", "Acceptance criteria"):
            self.assertNotIn(word, self.task_text())

    def test_tp_agent_review_passes_both_flags_to_tp_review(self):
        record = self.plan.parent / "review.args"
        self.stub("tp-review", f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "{record}"\necho review-1\n')
        a, b, c = self.supplied("a.txt"), self.supplied("b.txt"), self.supplied("c.md")
        r = subprocess.run([str(self.root / "bin/tp-agent"), "review", "w1", "--evidence", a, "--evidence", b, "--contract", c],
                           text=True, capture_output=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(record.read_text().splitlines(), ["--evidence", a, "--evidence", b, "--contract", c, "w1"])
        r = subprocess.run([str(self.root / "bin/tp-agent"), "review", "w1"], text=True, capture_output=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(record.read_text().splitlines(), ["w1"])  # no flags, none passed

    def test_tp_agent_review_refuses_a_repeated_or_empty_contract_and_starts_no_reviewer(self):
        record = self.plan.parent / "review.args"
        self.stub("tp-review", f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "{record}"\necho review-1\n')
        c1, c2 = self.supplied("c1.md"), self.supplied("c2.md")
        for contracts in ((c1, c2), (str(self.work / "missing.md"), c2)):
            with self.subTest(contracts=contracts):
                r = subprocess.run([str(self.root / "bin/tp-agent"), "review", "w1", *[a for c in contracts for a in ("--contract", c)]],
                                   text=True, capture_output=True, env=self.env)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("--contract may be given only once", r.stderr)
                self.assertFalse(record.exists())
        r = subprocess.run([str(self.root / "bin/tp-agent"), "review", "w1", "--contract", ""], text=True, capture_output=True, env=self.env)
        self.assertEqual(record.read_text().splitlines(), ["--contract", "", "w1"])  # an empty value reaches tp-review, which refuses it

    # --dispositions (R5)
    def test_dispositions_are_attached_under_their_heading_and_the_task_says_a_listed_finding_is_closed(self):
        d = self.supplied("d.md", "round 1 F2: closed by decision (Main): out of scope\n")
        r = self.review("--dispositions", d)
        self.assertEqual(r.returncode, 0, r.stderr)
        body = Path(str(self.log) + ".body").read_text()
        self.assertIn("Dispositions of earlier findings (supplied by Main)", body)
        self.assertIn("round 1 F2: closed by decision (Main): out of scope\n", body)
        task = self.task_text()
        self.assertIn("Dispositions of earlier findings (supplied by Main)", task)
        self.assertIn("A finding listed there is closed", task)
        self.assertIn("raise it again only if the tree changed in a way that reopens it", task)
        self.assertIn("cite the disposition and the change", task)
        self.assertNotIn("Reviewer contract", task)

    def test_dispositions_come_after_the_contract_and_are_allowed_beside_it_and_evidence(self):
        r = self.review("--evidence", self.supplied("e.txt", "EVID\n"), "--contract", self.supplied("c.md", "CONTRACT-TEXT\n"),
                        "--dispositions", self.supplied("d.md", "DISPOSITION-TEXT\n"))
        self.assertEqual(r.returncode, 0, r.stderr)
        body = Path(str(self.log) + ".body").read_text()
        self.assertLess(body.index("CONTRACT-TEXT"), body.index("DISPOSITION-TEXT"))

    def test_a_bad_dispositions_file_is_refused_naming_it_and_no_reviewer_starts(self):
        folder = self.work / "dir.txt"
        folder.mkdir()
        env_file = self.supplied(".env.local", "K=v\n")
        for path, why in ((str(self.work / "missing.md"), "does not exist"), ("", "--dispositions"), (self.supplied("empty.md", ""), "is empty"),
                          (str(folder), "not a regular file"), (env_file, ".env")):
            with self.subTest(path=path):
                r = self.review("--dispositions", path)
                self.assertEqual(r.returncode, 1)
                self.assertIn("--dispositions", r.stderr)
                self.assertIn(why, r.stderr)
                if path:
                    self.assertIn(f"'{path}'", r.stderr)
                self.assertFalse((self.plan.parent / "route.args").exists())
                self.assertFalse((self.plan.parent / "spawn.args").exists())

    def test_a_dispositions_file_inside_the_trust_store_is_refused(self):
        import pwd
        store = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".config/tmux-pi/trust/nothing-here.json"
        r = self.review("--dispositions", str(store))
        self.assertEqual(r.returncode, 1)
        self.assertIn("trust store", r.stderr)
        self.assertFalse((self.plan.parent / "spawn.args").exists())

    def test_dispositions_may_be_given_only_once(self):
        r = self.review("--dispositions", self.supplied("d1.md"), "--dispositions", self.supplied("d2.md"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("--dispositions may be given only once", r.stderr)
        self.assertIn("usage:", r.stderr)
        self.assertFalse((self.plan.parent / "spawn.args").exists())

    def test_dispositions_count_against_the_budget_and_are_never_trimmed(self):
        (self.task / "packet.md").write_text("p" * 200000)
        r = self.review("--dispositions", self.supplied("d.md", "d" * 150000))
        self.assertEqual(r.returncode, 1)
        self.assertIn("without dropping author evidence", r.stderr)

    def test_a_large_dispositions_file_is_split_into_labelled_segments(self):
        r = self.review("--dispositions", self.supplied("big.md", "x" * 100000 + "DISPOSITION-TAIL"))
        self.assertEqual(r.returncode, 0, r.stderr)
        segs = [p for p in self.spawn_contexts() if "main-dispositions" in Path(p).name]
        self.assertEqual(len(segs), 2)
        self.assertIn("big.md", self.copy(segs[0]).read_text())

    def test_tp_agent_review_passes_dispositions_through_and_refuses_a_repeat(self):
        record = self.plan.parent / "review.args"
        self.stub("tp-review", f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "{record}"\necho review-1\n')
        d1, d2 = self.supplied("d1.md"), self.supplied("d2.md")
        r = subprocess.run([str(self.root / "bin/tp-agent"), "review", "w1", "--dispositions", d1], text=True, capture_output=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(record.read_text().splitlines(), ["--dispositions", d1, "w1"])
        record.unlink()
        r = subprocess.run([str(self.root / "bin/tp-agent"), "review", "w1", "--dispositions", d1, "--dispositions", d2], text=True, capture_output=True, env=self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--dispositions may be given only once", r.stderr)
        self.assertFalse(record.exists())
        r = subprocess.run([str(self.root / "bin/tp-agent"), "review", "w1", "--dispositions", ""], text=True, capture_output=True, env=self.env)
        self.assertEqual(record.read_text().splitlines(), ["--dispositions", "", "w1"])  # an empty value reaches tp-review, which refuses it

    # the acceptance list (R7)
    def set_brief(self, text):
        (self.task / "request.md").write_text(text)

    def marker(self):
        return self.state / "tasks" / "review-1" / "acceptance-items.json"

    def test_a_brief_with_a_numbered_done_means_list_tells_the_reviewer_to_account_for_each_item_and_records_the_count(self):
        self.set_brief("Do it.\n\n## Done means\n1. one\n2. two\n3. three\n")
        r = self.review()
        self.assertEqual(r.returncode, 0, r.stderr)
        task = self.task_text()
        self.assertIn("section headed Acceptance criteria", task)
        self.assertIn("one line per item", task)
        self.assertIn("not checked", task)
        self.assertIn("3 item(s)", task)
        self.assertEqual(json.loads(self.marker().read_text()), {"items": [1, 2, 3]})

    def test_the_contract_files_list_counts_too(self):
        r = self.review("--contract", self.supplied("c.md", "**Acceptance criteria**\n1. a\n2. b\n"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("section headed Acceptance criteria", self.task_text())
        self.assertEqual(json.loads(self.marker().read_text()), {"items": [1, 2]})

    def test_a_request_that_names_a_brief_gets_its_list_and_the_reply_is_held_to_it(self):
        brief = self.supplied("brief.md", "# Brief\n\n## Done means\n1. a\n2. b\n3. c\n")
        self.set_brief(f"Read and carry out the brief {brief} in full.\n")
        r = self.review()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("section headed Acceptance criteria", self.task_text())
        self.assertEqual(json.loads(self.marker().read_text()), {"items": [1, 2, 3]})
        review = self.state / "tasks" / "review-1"
        base = "APPROVED\nRESULT_STATUS: complete\n%sCoverage\nI reviewed a.py and checked its callers and tests.\n"
        for section, complete in (("", False), ("## Acceptance criteria\n1. met\n2. met\n3. met\n\n", True)):
            (review / "result.md").write_text(base % section)
            out = subprocess.run([sys.executable, str(self.root / "lib/review_verdict.py"), str(review / "result.md")], text=True, capture_output=True)
            self.assertEqual(json.loads(out.stdout)["complete"], complete, out.stdout)

    def test_a_named_brief_that_is_missing_or_refused_is_noted_not_an_error(self):
        env = self.work / ".env.md"
        env.write_text("## Done means\n1. SECRET-LIST\n")
        gone = str(self.work / "gone.md")
        self.set_brief(f"Read the brief {gone} and {env}\n")
        r = self.review()
        self.assertEqual(r.returncode, 0, r.stderr)
        task = self.task_text()
        self.assertIn(f"{gone}", task)
        self.assertIn("does not exist", task)
        self.assertIn(f"{env}", task)
        self.assertIn("is a .env file", task)
        self.assertNotIn("SECRET-LIST", task)
        self.assertNotIn("section headed Acceptance criteria", task)
        self.assertFalse(self.marker().exists())

    def test_a_named_brief_inside_the_trust_store_gives_no_list_and_the_task_text_says_why(self):
        import pwd
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        for store in (".config/tmux-pi/trust", ".config/tmux-pi/state/trust"):
            with self.subTest(store=store):
                brief = str(home / store / "never-written-brief.md")  # not created or read: the refusal comes before any read
                self.set_brief(f"Read and carry out the brief {brief} in full.\n")
                if self.marker().exists():
                    self.marker().unlink()
                r = self.review()
                self.assertEqual(r.returncode, 0, r.stderr)
                task = self.task_text()
                self.assertIn("did not read for the acceptance list", task)
                self.assertIn(brief, task)
                self.assertIn("is inside the trust store", task)
                self.assertNotIn("section headed Acceptance criteria", task)
                self.assertFalse(self.marker().exists())

    def test_the_request_list_the_named_briefs_and_the_contract_are_merged(self):
        brief = self.supplied("brief.md", "## Done means\n1. a\n2. b\n3. c\n4. d\n")
        self.set_brief(f"## Done means\n1. a\n2. b\n\nRead the brief {brief}\n")
        r = self.review("--contract", self.supplied("c.md", "**Acceptance criteria**\n1. x\n5. y\n"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(self.marker().read_text()), {"items": [1, 2, 3, 4, 5]})

    def test_without_a_numbered_list_the_rule_is_absent_and_no_marker_is_written(self):
        self.set_brief("Do it.\n\n## Done means\n- a bullet\n")
        r = self.review("--contract", self.supplied("c.md", "judge it well\n"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Acceptance criteria", self.task_text())
        self.assertFalse(self.marker().exists())

    def test_a_reply_missing_the_section_is_incomplete_through_tp_agent_once_the_marker_is_written(self):
        self.set_brief("## Done means\n1. a\n2. b\n")
        self.assertEqual(self.review().returncode, 0)
        review = self.state / "tasks" / "review-1"
        text = "APPROVED\nRESULT_STATUS: complete\nCoverage\nI reviewed a.py and checked its callers and tests.\n"
        (review / "result.md").write_text(text)
        out = subprocess.run([sys.executable, str(self.root / "lib/review_verdict.py"), str(review / "result.md")], text=True, capture_output=True)
        self.assertFalse(json.loads(out.stdout)["complete"])
        (review / "result.md").write_text(text.replace("Coverage", "## Acceptance criteria\n1. met - a.py:1\n2. met - t\n\nCoverage"))
        out = subprocess.run([sys.executable, str(self.root / "lib/review_verdict.py"), str(review / "result.md")], text=True, capture_output=True)
        self.assertTrue(json.loads(out.stdout)["complete"], out.stdout)

    # timer waits in added test lines (T2)
    def test_the_packet_carries_the_timer_file_and_with_no_new_flags_nothing_else_changes(self):
        self.git("init", "-q")
        (self.work / "x_test.ts").write_text("await sleep(50)\n")
        r = self.review()
        self.assertEqual(r.returncode, 0, r.stderr)
        names = [Path(p).name for p in self.spawn_contexts()]
        self.assertEqual(names, ["task.md", "packet.md", "result.md", "timer-waits.md"])
        timer = self.copy(self.spawn_contexts()[-1]).read_text()
        self.assertIn("Timer waits in added test lines", timer)
        self.assertIn("x_test.ts:1: await sleep(50)", timer)
        for word in ("Main", "Reviewer contract", "Dispositions", "Acceptance criteria"):
            self.assertNotIn(word, self.task_text())

    def test_with_no_timer_lines_the_file_says_none_were_found(self):
        self.git("init", "-q")
        (self.work / "x.ts").write_text("await sleep(50)\n")
        self.assertEqual(self.review().returncode, 0)
        self.assertIn("none were found", self.copy(self.spawn_contexts()[-1]).read_text())


if __name__ == "__main__":
    unittest.main()
