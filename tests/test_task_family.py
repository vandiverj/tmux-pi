"""Task families linked from meta.json (fixtures in tests/fixtures/task_family; no model or network)."""

import argparse
import io
import json
import subprocess
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


class FamilyLabelTests(unittest.TestCase):
    """Family: names per review round, the repeated ones, and the `tp-evals families` summary line."""

    ENV = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"}

    @staticmethod
    def review(verdict, *names):
        body = "".join("F{0} [defect] problem {0}.\nFamily: {1}\nCloses by: code\n".format(i, n) for i, n in enumerate(names, 1))
        return verdict + "\n" + (body or "F1 [defect] problem.\nCloses by: code\n") + "Coverage\nreviewed the changed files."

    def chain(self, state, prefix, results, bad=()):
        """One coder chain with one review per entry of `results` (the result.md text; "" for a round with no result.md)."""
        tasks = {}
        for i, text in enumerate(results, 1):
            coder, rev = "coder-%s%d" % (prefix, i), "review-%s%d" % (prefix, i)
            meta = {"role": "coder", "created": "2026-10-01T1%d:00:00Z" % i, "reviews": [rev]}
            if i > 1:
                meta["parent"] = "coder-%s%d" % (prefix, i - 1)
            tasks[coder] = {"meta": meta}
            tasks[rev] = {"meta": {"role": "code-reviewer", "created": "2026-10-01T1%d:30:00Z" % i},
                          "files": {"result.md": text} if text else {}}
        materialize(state, tasks)
        for i in bad:  # a result.md that cannot be read: not UTF-8
            (Path(state) / "tasks" / ("review-%s%d" % (prefix, i)) / "result.md").write_bytes(b"\xff\xfe REQUEST_CHANGES\n")

    def fixture(self, state):
        rc = self.review
        # a: "cache-key" repeated in rounds 1 and 3, round 2 unlabelled, round 4 approves
        self.chain(state, "a", [rc("REQUEST_CHANGES", "cache-key", "Other_Name"), rc("REQUEST_CHANGES"),
                                rc("REQUEST_CHANGES", "cache-key"), rc("APPROVED")])
        # b: 3 REQUEST_CHANGES, every name different: no repeat
        self.chain(state, "b", [rc("REQUEST_CHANGES", "one"), rc("REQUEST_CHANGES", "two"), rc("REQUEST_CHANGES", "three")])
        # c: round 2 has no result.md, round 3 has one that cannot be read
        self.chain(state, "c", [rc("REQUEST_CHANGES", "x"), "", "unused"], bad=(3,))

    def test_record_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.fixture(tmp)
            got = {r["root"]: r for r in task_family.families(tmp)}
        a, b, c = got["coder-a1"], got["coder-b1"], got["coder-c1"]
        self.assertEqual(a["families"], [["cache-key", "other_name"], [], ["cache-key"], []])
        self.assertEqual((a["labelled_rounds"], a["repeated_families"], a["review_rounds"]), (2, ["cache-key"], 4))
        self.assertEqual(b["families"], [["one"], ["two"], ["three"]])
        self.assertEqual((b["labelled_rounds"], b["repeated_families"]), (3, []))
        self.assertEqual(c["families"], [["x"], [], []])
        self.assertEqual((c["labelled_rounds"], c["repeated_families"]), (1, []))
        self.assertEqual([r["families"] for r in a["reviews"]], a["families"])

    def test_a_pruned_review_is_an_unlabelled_round(self):
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            self.chain(tmp, "p", [self.review("REQUEST_CHANGES", "x"), self.review("REQUEST_CHANGES", "x")])
            shutil.rmtree(Path(tmp) / "tasks" / "review-p1")
            rec = task_family.families(tmp)[0]
        self.assertEqual((rec["families"], rec["labelled_rounds"], rec["repeated_families"]), ([[], ["x"]], 1, []))

    def test_summary_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.fixture(tmp)
            self.assertEqual(task_family.summary(task_family.families(tmp)),
                             "family labels: rounds with a label 6/10; families with a repeated name 1; "
                             "families with 3+ REQUEST_CHANGES and no repeated name 1")

    def test_cli_prints_the_summary_last_and_survives_an_unreadable_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.fixture(Path(tmp) / ".tmux-pi" / "sessions" / "s1")
            cmd = [sys.executable, str(ROOT / "bin" / "tp-evals"), "families", "--project", tmp]
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=self.ENV)
            data = subprocess.run(cmd + ["--json"], capture_output=True, text=True, timeout=60, env=self.ENV)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(out.stdout.splitlines()[-1].startswith("family labels: rounds with a label 6/10;"), out.stdout)
        self.assertEqual({r["root"]: r["repeated_families"] for r in json.loads(data.stdout)}["coder-a1"], ["cache-key"])

    def test_text_output_lists_names_per_family(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.fixture(tmp)
            self.chain(tmp, "n", [self.review("APPROVED")])  # no label: no families line
            lines = task_family.render(task_family.families(tmp)).splitlines()
        fam = [l for l in lines if l.startswith("    families:")]
        self.assertEqual(fam, ["    families: cache-key,other_name | - | cache-key | -  labelled rounds 2/4  repeated: cache-key",
                               "    families: one | two | three  labelled rounds 3/3  repeated: -",
                               "    families: x | - | -  labelled rounds 1/3  repeated: -"])
        n = next(i for i, l in enumerate(lines) if l.startswith("fam-coder-n1"))
        self.assertFalse(lines[n + 1:n + 3] and any(l.startswith("    families:") for l in lines[n + 1:n + 3]))

    def test_a_bad_companion_file_does_not_drop_the_label(self):
        for bad in ("{not json", '{"items": "x"}', '{"items": [0]}'):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as tmp:
                self.chain(tmp, "m", [self.review("REQUEST_CHANGES", "keep")])
                (Path(tmp) / "tasks" / "review-m1" / "acceptance-items.json").write_text(bad)
                rec = task_family.families(tmp)[0]
            self.assertEqual((rec["families"], rec["labelled_rounds"]), ([["keep"]], 1))

    def test_each_unreadable_result_is_unlabelled(self):
        for kind in ("missing", "empty", "not utf-8", "oversized", "directory"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                self.chain(tmp, "u", [self.review("REQUEST_CHANGES", "k")])
                path = Path(tmp) / "tasks" / "review-u1" / "result.md"
                if kind == "missing":
                    path.unlink()
                elif kind == "empty":
                    path.write_text("  \n")
                elif kind == "not utf-8":
                    path.write_bytes(b"\xff\xfe Family: k\n")
                elif kind == "oversized":
                    path.write_text("Family: k\n" + "x" * 1_000_001)
                else:
                    path.unlink()
                    path.mkdir()
                rec = task_family.families(tmp)[0]
            self.assertEqual((rec["families"], rec["labelled_rounds"]), ([[]], 0))

    def test_unreadable_meta_files_do_not_crash_or_invent_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.chain(tmp, "j", [self.review("REQUEST_CHANGES", "k")])
            (Path(tmp) / "tasks" / "review-j1" / "meta.json").write_text("{broken")
            (Path(tmp) / "tasks" / "review-j1" / "state.json").write_text("[")
            rec = task_family.families(tmp)[0]
        self.assertEqual((rec["families"], rec["labelled_rounds"]), ([["k"]], 1))  # label comes from result.md, round from the author's record


class TpEvalsFamiliesTests(unittest.TestCase):
    """`bin/tp-evals families` reaches task_family.run and needs no routing eval dir."""

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "bin" / "tp-evals"), "families", *args], capture_output=True, text=True,
                              timeout=60, env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"})

    def test_prints_text_and_json_for_a_project(self):
        case = json.loads((FIXTURES / "single_attempt.json").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            materialize(Path(tmp) / ".tmux-pi" / "sessions" / "s1", case["tasks"])
            text = self.run_cli("--project", tmp)
            self.assertEqual(text.returncode, 0, text.stderr)
            self.assertIn("fam-coder-a  accepted", text.stdout)
            data = self.run_cli("--project", tmp, "--json")
            self.assertEqual(data.returncode, 0, data.stderr)
            self.assertEqual([r["family_id"] for r in json.loads(data.stdout)], ["fam-coder-a"])

    def test_a_project_with_no_tasks_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self.run_cli("--project", tmp)
            self.assertEqual((out.returncode, out.stdout), (0, ""), out.stderr)


if __name__ == "__main__":
    unittest.main()
