"""tp-agent retry: a retry that carries its fix brief (--reason naming an .md file) in the request it starts with, and the
gate on a finding family a reviewer names twice. The fixture is the orchestration one (fake tmux, no model runs)."""
import json
import os
from pathlib import Path
import pwd
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_orchestration  # noqa: E402  (the module, so its own tests are not collected twice)
from lane_fixture import TMUX, TmuxCase  # noqa: E402

MODEL = test_orchestration.MODEL
OLD = "Make a focused change"


class RetryCase(unittest.TestCase):
    for _name in ("setUp", "raw", "cli", "task_dir", "packet", "settle", "dispatch", "retry"):
        locals()[_name] = test_orchestration.OrchestrationTests.__dict__[_name]
    del _name

    def request(self, item):
        return (self.task_dir(item) / "request.md").read_text()

    def settled(self):
        item = self.dispatch()
        self.settle(item, "RESULT_STATUS: complete\nDone.\n")
        return item

    def fix_brief(self, name="fix.md", text="# Fix\n\nDo the other thing.\n"):
        path = self.root / name
        path.write_text(text)
        return str(path)


class FixBriefTests(RetryCase):
    def sentence(self, brief):
        return f"Read and carry out the fix brief {brief} in full. It replaces the task below where they differ."

    def test_a_reason_naming_a_brief_file_starts_the_new_request_with_it(self):
        item = self.settled()
        brief = self.fix_brief()
        again = self.retry(item, brief)
        self.assertEqual(self.request(again), f"{self.sentence(brief)}\n\n{OLD}\n")
        self.assertIn(f"# Task\n\n{self.sentence(brief)}\n\n{OLD}\n", self.packet(again))
        self.assertIn(f"Retry of {item['id']}: {brief}. ", self.packet(again))  # the Notes line stays

    def test_with_task_the_request_is_that_task_only(self):
        item = self.settled()
        brief = self.fix_brief()
        again = self.retry(item, brief, "--task", "T")
        self.assertEqual(self.request(again), "T\n")
        self.assertNotIn("fix brief", self.request(again))
        self.assertIn(f"Retry of {item['id']}: {brief}. ", self.packet(again))
        self.assertNotIn("not used as a fix brief", self.packet(again))

    def test_a_plain_text_reason_leaves_the_request_as_it_was(self):
        for reason in ("plain words", "fix.md", "see /abs/fix.md for what changed", "/abs/fix.txt"):
            with self.subTest(reason=reason):
                item = self.settled()
                (self.root / "fix.md").write_text("relative, in the wrong place\n")
                again = self.retry(item, reason)
                self.assertEqual(self.request(again), f"{OLD}\n")
                self.assertNotIn("fix brief", self.packet(again))

    def test_a_refused_file_is_not_used_and_the_notes_say_why(self):
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        secret = self.root / ".env.md"
        secret.write_text("SECRET-FIX-BRIEF\n")
        (self.root / "dir.md").mkdir()
        (self.root / "empty.md").write_text("")
        cases = [(str(secret), "is a .env file"),
                 (str(home / ".config/tmux-pi/trust/never-written-fix.md"), "is inside the trust store"),  # never created or read
                 (str(home / ".config/tmux-pi/state/trust/never-written-fix.md"), "is inside the trust store"),
                 (str(self.root / "missing.md"), "does not exist"),
                 (str(self.root / "dir.md"), "is not a regular file"),
                 (str(self.root / "empty.md"), "is empty")]
        for path, why in cases:
            with self.subTest(path=path):
                item = self.settled()
                again = self.retry(item, path)
                self.assertEqual(self.request(again), f"{OLD}\n")
                packet = self.packet(again)
                self.assertNotIn("Read and carry out the fix brief", packet)
                self.assertNotIn("SECRET-FIX-BRIEF", packet)
                self.assertIn("not used as a fix brief", packet)
                self.assertIn(why, packet)

    def test_a_retry_of_a_retry_with_plain_words_keeps_the_fix_brief_of_the_attempt_it_replaces(self):
        item = self.settled()
        brief = self.fix_brief()
        first = self.retry(item, brief)
        self.settle(first, "RESULT_STATUS: complete\nDone.\n")
        second = self.retry(first, "still wrong")
        self.assertEqual(self.request(second), self.request(first))

    def test_the_review_of_the_retry_reads_the_fix_brief_it_is_named_in(self):
        sys.path.insert(0, str(Path(test_orchestration.ROOT) / "lib"))
        import review_verdict
        item = self.settled()
        brief = self.fix_brief()
        again = self.retry(item, brief)
        self.assertEqual(review_verdict.named_briefs(self.request(again)), [brief])


def review_text(*families):
    labels = "".join(f"F{n} [defect] one cell of the matrix is wrong.\nFamily: {name}\nCloses by: code\n\n" for n, name in enumerate(families, 1))
    return (f"REQUEST_CHANGES\nRESULT_STATUS: complete\n{labels or 'F1 [defect] wrong.\nCloses by: code\n'}\n"
            "Coverage\nI read the changed files and their callers.\n")


class RepeatedFamilyGateTests(RetryCase):
    def attach_review(self, item, review_id, *families):
        """A review task `review_id` with a reply naming `families`, recorded on the author `item` (reviews, as tp-review writes them)."""
        task = self.state / "tasks" / review_id
        task.mkdir(exist_ok=True)  # a test that chains twice (subTests) reuses the ids
        (task / "meta.json").write_text(json.dumps({"id": review_id, "role": "reviewer"}))
        (task / "result.md").write_text(review_text(*families))
        path = self.task_dir(item) / "meta.json"
        meta = json.loads(path.read_text())
        meta["reviews"] = meta.get("reviews", []) + [review_id]
        path.write_text(json.dumps(meta))

    def chain(self, *rounds):
        """The author reviewed once per round (each round is the families its review names), each retried from the last (plainly, or
        with --route design where the gate asks for a route); the newest author is returned. Every review is `review-N`."""
        item = self.settled()
        self.attach_review(item, "review-1", *rounds[0])
        for number, families in enumerate(rounds[1:], 2):
            run = self.plain_retry(item)
            if run.returncode:
                run = self.plain_retry(item, "--route", "design")
            self.assertEqual(run.returncode, 0, run.stderr)
            item = json.loads(run.stdout)
            self.settle(item, "RESULT_STATUS: complete\nDone.\n")
            self.attach_review(item, f"review-{number}", *families)
        return item

    def tasks(self):
        return sorted(p.name for p in (self.state / "tasks").iterdir())

    def plain_retry(self, item, *extra):
        return self.raw("retry", item["id"], "--reason", "again", "--worker", "pi", "--model", MODEL, *extra)

    def test_a_family_named_in_two_rounds_refuses_a_plain_retry(self):
        item = self.chain(["state-matrix"], ["state-matrix"])
        before = self.tasks()
        run = self.plain_retry(item)
        self.assertEqual(run.returncode, 1, run.stdout)
        for word in ("state-matrix", "review-1", "review-2", "--route rule-first", "--route design", "--route park"):
            self.assertIn(word, run.stderr)
        self.assertEqual(self.tasks(), before)  # no task was spawned

    def test_the_families_are_matched_by_name_with_the_case_folded(self):
        item = self.chain(["State-Matrix"], ["state-matrix"])
        self.assertEqual(self.plain_retry(item).returncode, 1)

    def test_route_design_and_rule_first_retry_and_the_notes_say_which_route(self):
        for route in ("design", "rule-first"):
            with self.subTest(route=route):
                item = self.chain(["state-matrix"], ["state-matrix"])
                run = self.plain_retry(item, "--route", route)
                self.assertEqual(run.returncode, 0, run.stderr)
                packet = self.packet(json.loads(run.stdout))
                self.assertIn(f"route {route}", packet)
                self.assertIn("state-matrix", packet)
                self.assertIn("the brief must change the rule or the design, not one more site", packet)
                self.assertTrue(any(line.startswith(f"- Retry of {item['id']}") and f"route {route}" in line for line in packet.splitlines()))

    def test_the_route_note_is_not_replayed_into_the_next_retry(self):
        item = self.chain(["state-matrix"], ["state-matrix"])
        again = json.loads(self.plain_retry(item, "--route", "design").stdout)
        self.settle(again, "RESULT_STATUS: complete\nDone.\n")
        self.assertNotIn("route design", self.packet(self.retry(again, "later")))

    def test_route_park_does_not_retry_and_writes_parked_md(self):
        item = self.chain(["state-matrix"], ["state-matrix"])
        before = self.tasks()
        run = self.plain_retry(item, "--route", "park")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.tasks(), before)
        parked = (self.task_dir(item) / "parked.md").read_text()
        for word in ("state-matrix", "review-1", "review-2"):
            self.assertIn(word, parked)
        self.assertIn("owner decides", run.stdout + run.stderr)
        self.assertEqual(Path(json.loads(run.stdout)["parked"]).resolve(), (self.task_dir(item) / "parked.md").resolve())

    def test_route_park_replaces_a_link_at_parked_md_and_writes_nothing_through_it(self):
        item = self.chain(["state-matrix"], ["state-matrix"])
        target = self.root / "elsewhere.txt"
        target.write_text("not the parked record\n")
        parked = self.task_dir(item) / "parked.md"
        parked.symlink_to(target)
        run = self.plain_retry(item, "--route", "park")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(target.read_text(), "not the parked record\n")
        self.assertFalse(parked.is_symlink())
        self.assertTrue(parked.is_file())
        self.assertIn("state-matrix", parked.read_text())

    def test_escalate_alone_does_not_pass_the_gate(self):
        item = self.chain(["state-matrix"], ["state-matrix"])
        before = self.tasks()
        run = self.raw("retry", item["id"], "--reason", "again", "--escalate")
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn("state-matrix", run.stderr)
        self.assertIn("--route", run.stderr)
        self.assertEqual(self.tasks(), before)

    def test_a_family_round_one_did_not_name_allows_a_plain_retry(self):
        item = self.chain(["state-matrix"], ["error-path"])
        run = self.plain_retry(item)
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_a_latest_review_naming_no_family_allows_a_plain_retry(self):
        item = self.chain(["state-matrix"], [])
        self.assertEqual(self.plain_retry(item).returncode, 0)

    def test_only_the_latest_review_is_judged_against_the_earlier_ones(self):
        item = self.chain(["state-matrix", "error-path"], ["state-matrix"], ["error-path"])  # round 3 repeats a family of round 1 only
        run = self.plain_retry(item)
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn("error-path", run.stderr)
        self.assertNotIn("family state-matrix", run.stderr)

    def test_an_attempt_not_yet_reviewed_after_a_routed_retry_retries_plainly(self):
        item = self.chain(["state-matrix"], ["state-matrix"])
        again = json.loads(self.plain_retry(item, "--route", "design").stdout)
        self.settle(again, "RESULT_STATUS: complete\nDone.\n")
        self.assertEqual(self.plain_retry(again).returncode, 0)

    def test_a_route_without_a_repeated_family_is_refused(self):
        item = self.chain(["state-matrix"], ["error-path"])
        before = self.tasks()
        run = self.plain_retry(item, "--route", "park")
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn("repeats no family", run.stderr)
        self.assertEqual(self.tasks(), before)
        self.assertFalse((self.task_dir(item) / "parked.md").exists())

    def test_a_review_that_cannot_be_read_is_skipped(self):
        item = self.chain(["state-matrix"], ["state-matrix"])
        (self.state / "tasks" / "review-1" / "result.md").unlink()
        self.assertEqual(self.plain_retry(item).returncode, 0)

    def test_a_retry_lineage_that_cannot_be_read_says_the_gate_was_not_checked(self):
        item = self.chain(["state-matrix"], ["state-matrix"])
        meta = self.task_dir(item) / "meta.json"
        value = json.loads(meta.read_text())
        parent = value["parent"]
        (self.state / "tasks" / parent / "meta.json").write_text("not json")
        run = self.plain_retry(item)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("repeated-family gate was not checked", run.stderr)


@unittest.skipUnless(TMUX, "tmux not installed")
class ParkWithALiveProcessTests(TmuxCase):
    """Parking starts no attempt, so a process alive on the working tree does not stop it; every other route still is stopped."""

    def reviewed(self, task, review_id, family):
        review = self.state / "tasks" / review_id
        review.mkdir()
        (review / "meta.json").write_text(json.dumps({"id": review_id, "role": "reviewer"}))
        (review / "result.md").write_text(review_text(family))
        meta = json.loads((task / "meta.json").read_text())
        (task / "meta.json").write_text(json.dumps(dict(meta, reviews=meta.get("reviews", []) + [review_id])))

    def repeated_family_with_a_live_process(self):
        first, first_task = self.dispatch()
        self.settle(first_task)
        self.reviewed(first_task, "review-1", "state-matrix")
        second = self.cli("retry", first["id"], "--reason", "again", "--worker", "pi", "--model", MODEL)
        second_task = self.state / "tasks" / second["id"]
        self.settle(second_task)
        self.reviewed(second_task, "review-2", "state-matrix")
        _, _, pane, _ = self.live_task()  # another task alive on the same working tree
        self.assertTrue(self.alive(pane))
        return second, second_task

    def test_route_park_writes_parked_md_beside_a_live_process_and_design_is_still_refused(self):
        second, task = self.repeated_family_with_a_live_process()
        before = sorted(p.name for p in (self.state / "tasks").iterdir())
        design = self.raw("retry", second["id"], "--reason", "again", "--worker", "pi", "--model", MODEL, "--route", "design")
        self.assertEqual(design.returncode, 1, design.stdout)
        self.assertIn("a process is alive on the same working tree", design.stderr)
        self.assertEqual(sorted(p.name for p in (self.state / "tasks").iterdir()), before)
        park = self.raw("retry", second["id"], "--reason", "again", "--route", "park")
        self.assertEqual(park.returncode, 0, park.stderr)
        self.assertIn("state-matrix", (task / "parked.md").read_text())
        self.assertEqual(sorted(p.name for p in (self.state / "tasks").iterdir()), before)  # nothing was started

    def test_a_plain_retry_beside_a_live_process_is_still_refused_by_the_guard(self):
        second, _ = self.repeated_family_with_a_live_process()
        run = self.raw("retry", second["id"], "--reason", "again", "--worker", "pi", "--model", MODEL)
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn("a process is alive on the same working tree", run.stderr)


if __name__ == "__main__":
    unittest.main()
