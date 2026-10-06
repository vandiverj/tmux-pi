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
        (self.root / "lib/task_record.py").write_text("def request_text(d):\n    return 'x'\n")
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
        tmux.write_text(f'#!/usr/bin/env bash\necho "{self.state}"\n')
        tmux.chmod(0o755)
        self.env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}", "TP_SESSION": "t"}
        self.env.pop("TMUX", None)
        self.env.pop("TP_STATE_DIR", None)  # the fake session defines its own state dir
        self.env["TMPDIR"] = str(base / "tmp")
        (base / "tmp").mkdir()
        (self.task / "meta.json").write_text(json.dumps({"id": "w1", "worker": "pi", "workdir": str(self.work)}))
        for name in ("task.md", "packet.md", "result.md"):
            (self.task / name).write_text(f"{name}\n")
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
            self.assertIn("first line of your final reply must be exactly APPROVED or REQUEST_CHANGES", task)
            self.assertRegex(task, r"RESULT_STATUS: complete\|empty\|blocked\|incomplete on the second line")
        self.assertIn("report.md", research)

    def spawn_contexts(self):
        return [l.split()[0] for l in self.log.read_text().splitlines() if l.startswith("/")]

    def test_oversized_packet_and_result_are_split_losslessly_in_order(self):
        packet = "".join(chr(97 + i % 26) for i in range(100000)) + "PACKET-TAIL-MARKER"
        result = "R" * 70000 + "RESULT-TAIL-MARKER"
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
        (self.task / "result.md").write_text("r" * 70000)
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


if __name__ == "__main__":
    unittest.main()
