"""Task families linked from meta.json (fixtures in tests/fixtures/task_family; no model or network)."""

import argparse
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import task_family  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "task_family"
OK = "APPROVED\nlooks right\nCoverage\nreviewed the changed files."


def materialize(state, tasks):
    for ident, spec in tasks.items():
        d = Path(state) / "tasks" / ident
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps(dict({"id": ident}, **spec.get("meta", {}))))
        if "state" in spec:
            (d / "state.json").write_text(json.dumps(spec["state"]))
        for name, text in (spec.get("files") or {}).items():
            (d / name).write_text(text)


class FixtureTests(unittest.TestCase):
    """Each row of the brief's case table is one fixture file: the expected facts are in it."""

    def check(self, name):
        case = json.loads((FIXTURES / (name + ".json")).read_text())
        with tempfile.TemporaryDirectory() as tmp:
            materialize(tmp, case["tasks"])
            got = task_family.families(tmp)
        self.assertEqual(len(got), len(case["expect"]), [g["family_id"] for g in got])
        for family, want in zip(got, case["expect"]):
            self.assertEqual(family["family_id"], want["family_id"])
            self.assertEqual([a["id"] for a in family["attempts"]], want["attempts"])
            self.assertEqual([r["id"] for r in family["reviews"]], want["reviews"])
            for key in ("first_review_verdict", "review_rounds", "accepted", "status", "wall_seconds"):
                self.assertEqual(family[key], want[key], (name, key))
            self.assertEqual(family["root_missing"], want.get("root_missing", False))
        return got

    def test_single_attempt(self):
        self.check("single_attempt")

    def test_retry_after_request_changes_is_one_family_with_two_rounds(self):
        family = self.check("retry_after_request_changes")[0]
        self.assertEqual([t["id"] for t in family["timeline"]], ["coder-a", "review-a", "coder-b", "review-b"])
        self.assertEqual([r["round"] for r in family["reviews"]], [1, 2])

    def test_escalated_retry_joins_the_chain(self):
        family = self.check("escalated_retry")[0]
        self.assertTrue(all(a["escalated"] for a in family["attempts"]))

    def test_step_with_no_review_is_unreviewed_and_nothing_is_accepted(self):
        self.check("no_review")

    def test_two_independent_coders_are_two_families_of_one_step(self):
        got = self.check("two_independent_coders")
        self.assertEqual({(g["lane"], g["lane_step"]) for g in got}, {("L", "s1")})
        self.assertEqual(len({g["family_id"] for g in got}), 2)

    def test_missing_parent_roots_the_family_on_the_missing_id(self):
        self.check("missing_parent")

    def test_family_id_does_not_change_when_the_missing_root_appears(self):
        case = json.loads((FIXTURES / "missing_parent.json").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            materialize(tmp, case["tasks"])
            before = task_family.families(tmp)[0]["family_id"]
            materialize(tmp, {"coder-gone": {"meta": {"role": "coder", "created": "2026-10-01T09:00:00Z"}}})
            after = task_family.families(tmp)
        self.assertEqual((before, [f["family_id"] for f in after], after[0]["root_missing"]),
                         ("fam-coder-gone", ["fam-coder-gone"], False))

    def test_a_parent_loop_does_not_hang(self):
        with tempfile.TemporaryDirectory() as tmp:
            materialize(tmp, {"a": {"meta": {"parent": "b", "created": "2026-10-01T09:00:00Z"}},
                              "b": {"meta": {"parent": "a", "created": "2026-10-01T09:01:00Z"}}})
            got = task_family.families(tmp)
        self.assertEqual(len(got), 1)
        self.assertEqual(sorted(a["id"] for a in got[0]["attempts"]), ["a", "b"])


class PrunedReviewTests(unittest.TestCase):
    def families(self, tasks):
        with tempfile.TemporaryDirectory() as tmp:
            materialize(tmp, tasks)
            return task_family.families(tmp)

    def test_a_missing_first_review_still_counts_and_the_first_verdict_is_unknown(self):
        got = self.families({
            "coder-a": {"meta": {"role": "coder", "created": "2026-10-01T10:00:00Z", "reviews": ["review-a1", "review-a2"]}},
            "review-a2": {"meta": {"role": "code-reviewer", "created": "2026-10-01T10:20:00Z"}, "files": {"result.md": OK}}})
        family = got[0]
        self.assertEqual((family["review_rounds"], family["first_review_verdict"], family["first_review_known"]),
                         (2, None, False))
        self.assertEqual(([r["id"] for r in family["reviews"]], [r["round"] for r in family["reviews"]]), (["review-a2"], [2]))
        self.assertEqual(family["missing_reviews"], ["review-a1"])

    def test_a_missing_later_review_counts_and_the_first_verdict_is_kept(self):
        family = self.families({
            "coder-a": {"meta": {"role": "coder", "created": "2026-10-01T10:00:00Z", "reviews": ["review-a1", "review-a2"]}},
            "review-a1": {"meta": {"role": "code-reviewer", "created": "2026-10-01T10:10:00Z"}, "files": {"result.md": OK}}})[0]
        self.assertEqual((family["review_rounds"], family["first_review_verdict"], family["first_review_known"]),
                         (2, "APPROVED", True))

    def test_a_review_named_but_never_present_is_a_round_and_the_family_is_open(self):
        family = self.families({"coder-a": {"meta": {"role": "coder", "created": "2026-10-01T10:00:00Z", "review": "review-gone"}}})[0]
        self.assertEqual((family["review_rounds"], family["status"], family["first_review_verdict"]), (1, "open", None))


class TokenTests(unittest.TestCase):
    def families(self, tasks):
        with tempfile.TemporaryDirectory() as tmp:
            materialize(tmp, tasks)
            return task_family.families(tmp)

    def test_tokens_split_frontier_and_local_and_input_output_cache(self):
        got = self.families({
            "coder-a": {"meta": {"role": "coder", "created": "2026-10-01T10:00:00Z", "escalated_to": {"id": "coder-b"}},
                        "state": {"model": "lmstudio/qwen", "tokens": {"input": 1000, "output": 300, "cacheRead": 7, "cacheWrite": 3}}},
            "coder-b": {"meta": {"role": "coder", "created": "2026-10-01T10:06:00Z", "escalated_from": {"id": "coder-a"},
                                 "review": "review-b"},
                        "state": {"model": "claude-opus-5-5", "tokens": {"input": 200, "output": 50, "cacheRead": 500, "cacheWrite": 20}}},
            "review-b": {"meta": {"role": "code-reviewer", "created": "2026-10-01T10:16:00Z"},
                         "state": {"model": "openai/gpt-6-sol", "tokens": {"input": 10, "output": 5, "cacheRead": 0, "cacheWrite": 0}},
                         "files": {"result.md": OK}}})
        self.assertEqual(len(got), 1)
        frontier, local = got[0]["tokens"]["frontier"], got[0]["tokens"]["local"]
        self.assertEqual({k: frontier[k] for k in ("input", "output", "cache_read", "cache_write", "cache")},
                         {"input": 210, "output": 55, "cache_read": 500, "cache_write": 20, "cache": 520})
        self.assertEqual({k: local[k] for k in ("input", "output", "cache_read", "cache_write", "cache")},
                         {"input": 1000, "output": 300, "cache_read": 7, "cache_write": 3, "cache": 10})

    def test_unrecorded_tokens_are_none_not_zero_and_counted(self):
        got = self.families({
            "coder-a": {"meta": {"role": "coder", "created": "2026-10-01T10:00:00Z", "model": "claude-sonnet-5-5"},
                        "state": {"tokens": {"input": 5, "output": 2}}},
            "coder-b": {"meta": {"role": "coder", "parent": "coder-a", "created": "2026-10-01T10:10:00Z", "model": "claude-sonnet-5-5"}}})
        frontier, local = got[0]["tokens"]["frontier"], got[0]["tokens"]["local"]
        self.assertEqual((frontier["input"], frontier["input_unknown_tasks"], frontier["cache_read"], frontier["cache"]),
                         (5, 1, None, None))
        self.assertEqual((local["input"], local["output"], local["cache"]), (None, None, None))


class CommandTests(unittest.TestCase):
    def test_run_prints_json_and_text_for_a_project_with_sessions(self):
        case = json.loads((FIXTURES / "single_attempt.json").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            materialize(Path(tmp) / ".tmux-pi" / "sessions" / "s1", case["tasks"])
            buf = io.StringIO()
            records = task_family.run(tmp, as_json=True, out=buf)
            self.assertEqual([r["family_id"] for r in json.loads(buf.getvalue())], ["fam-coder-a"])
            self.assertEqual(records[0]["session"], "s1")
            buf = io.StringIO()
            task_family.run(tmp, out=buf)
            self.assertIn("fam-coder-a  accepted", buf.getvalue())

    def test_text_shows_local_input_output_and_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            materialize(tmp, {"coder-a": {"meta": {"role": "coder", "created": "2026-10-01T10:00:00Z"},
                                          "state": {"model": "lmstudio/qwen", "tokens": {"input": 1000, "output": 300, "cacheRead": 7, "cacheWrite": 3}}}})
            buf = io.StringIO()
            task_family.run(tmp, out=buf)
        self.assertIn("local in/out/cache=1000/300/10", buf.getvalue())

    def test_add_subcommand_parses_project_and_json(self):
        parser = argparse.ArgumentParser()
        task_family.add_subcommand(parser.add_subparsers(dest="command"))
        args = parser.parse_args(["families", "--project", "/x", "--json"])
        self.assertEqual((args.command, args.project, args.json, callable(args.func)), ("families", "/x", True, True))


if __name__ == "__main__":
    unittest.main()
