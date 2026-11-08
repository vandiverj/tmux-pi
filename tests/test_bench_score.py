import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench_fixture as fx  # noqa: E402
from bench import answers, compare, match, project, record, specs, trace  # noqa: E402
from bench.shell import Shell  # noqa: E402

DEFECTS = {"defects": [
    {"id": "D1", "file": "src/a.ts", "line_start": 10, "line_end": 12, "keywords": ["off-by-one", "bound"]},
    {"id": "D2", "file": "src/b.ts", "line_start": 5, "line_end": 5, "keywords": ["caller"]},
    {"id": "D3", "file": "src/c.ts", "line_start": 40, "line_end": 44, "keywords": ["message", "text"]},
]}


class Answers(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.bench, self.ans, self.specs = fx.make_bench(self.dir.name)

    def test_sealed_answers_verify_and_a_changed_one_refuses_with_the_task_id(self):
        for spec in self.specs:
            answers.verify(spec, self.ans)
        (self.ans / "local-q.json").write_text('{"answer": "forty three"}')
        with self.assertRaises(answers.AnswerError) as caught:
            answers.verify(next(s for s in self.specs if s["id"] == "local-q"), self.ans)
        self.assertEqual(caught.exception.task_id, "local-q")
        self.assertIn("changed", str(caught.exception))

    def test_missing_and_unsealed_answers_refuse_with_the_task_id(self):
        (self.ans / "code-f.test.ts").unlink()
        with self.assertRaises(answers.AnswerError) as missing:
            answers.verify(next(s for s in self.specs if s["id"] == "code-f"), self.ans)
        self.assertEqual(missing.exception.task_id, "code-f")
        unsealed = {"id": "x", "kind": "local", "answer": {"file": "x.json"}}
        with self.assertRaises(answers.AnswerError) as caught:
            answers.verify(unsealed, self.ans)
        self.assertEqual(caught.exception.task_id, "x")

    def test_seal_records_the_sha_of_the_copied_file_and_keeps_answers_out_of_the_bench_dir(self):
        spec = next(s for s in specs.load_specs(self.bench / "tasks") if s["id"] == "local-q")
        self.assertEqual(spec["answer"]["sha256"], hashlib.sha256((self.ans / "local-q.json").read_bytes()).hexdigest())
        self.assertEqual(oct((self.ans / "local-q.json").stat().st_mode & 0o777), "0o600")
        self.assertNotIn("forty two", json.dumps(spec))


class Matching(unittest.TestCase):
    def test_two_of_three_defects_named_scores_two_thirds(self):
        text = ("F1 src/a.ts:11 off-by-one: the loop bound is wrong\nCloses by: code\n"
                "F2 src/b.ts line 5 the caller in b still passes the old argument\nCloses by: code\n")
        scored = match.score_review(text, DEFECTS, clean=False)
        self.assertEqual((scored["caught"], scored["defects"], scored["caught_ids"]), (2, 3, ["D1", "D2"]))

    def test_a_finding_on_the_wrong_line_window_does_not_count(self):
        scored = match.score_review("F1 src/a.ts:30 off-by-one bound\nCloses by: code\n", DEFECTS, clean=False)
        self.assertEqual(scored["caught"], 0)

    def test_a_finding_without_the_file_or_without_a_keyword_does_not_count(self):
        self.assertEqual(match.score_review("F1 src/z.ts:11 off-by-one\n", DEFECTS, False)["caught"], 0)
        self.assertEqual(match.score_review("F1 src/a.ts:11 style nit\n", DEFECTS, False)["caught"], 0)

    def test_one_finding_catches_one_defect_only(self):
        both = "F1 src/a.ts:11 off-by-one and also src/b.ts line 5 caller\n"
        self.assertEqual(match.score_review(both, DEFECTS, False)["caught"], 1)

    def test_clean_diff_counts_closes_by_code_findings_as_false_positives(self):
        text = "F1 src/a.ts:1 nit\nCloses by: code\nF2 src/a.ts:2 question\nCloses by: decision\n"
        self.assertEqual(match.score_review(text, {"defects": []}, clean=True)["false_positives"], 1)
        self.assertEqual(match.score_review(text, {"defects": []}, clean=False)["false_positives"], 0)

    def test_local_answers_are_exact_after_whitespace_normalising(self):
        self.assertTrue(match.score_local("notes\nANSWER:  forty\n two ", "forty two"))
        self.assertFalse(match.score_local("ANSWER: forty three", "forty two"))
        self.assertFalse(match.score_local("forty two", "forty two"))

    def test_vitest_output_needs_every_test_passing(self):
        parsed = match.parse_vitest('noise {"numTotalTests": 3, "numPassedTests": 3} tail')
        self.assertTrue(match.code_passed(0, parsed))
        self.assertFalse(match.code_passed(1, match.parse_vitest('{"numTotalTests": 3, "numPassedTests": 2}')))
        self.assertFalse(match.code_passed(0, match.parse_vitest('{"numTotalTests": 0, "numPassedTests": 0}')))
        self.assertFalse(match.code_passed(0, match.parse_vitest("crashed")))


def task(task_id, kind, status, tokens=(100, 50), planted=0, **detail):
    return {"id": task_id, "kind": kind, "status": status, "clean": False, "planted": planted, "detail": detail, "wall_s": 1.0, "models": [],
            "tokens": {"input": tokens[0], "output": tokens[1], "cache_read": None, "cache_write": None}}


def run_record(label, n, tasks, env=None, task_hash="h"):
    return {"schema": record.SCHEMA, "label": label, "n": n, "task_set_hash": task_hash, "env": env or {}, "tasks": tasks,
            "metrics": record.metrics(tasks)}


def arm(passing, tokens=(100, 50), orient=4):
    tasks = [task(f"local-{i}", "local", "pass" if i < passing else "fail", tokens) for i in range(4)]
    return tasks + [task("orient-x", "orient", "scored", tokens, orient_waste=orient)]


class Compare(unittest.TestCase):
    def verdict(self, a_tasks, b_tasks, **kw):
        return compare.render([run_record("a", 1, a_tasks)], [run_record("b", 1, b_tasks, **kw)], "a", "b")

    def test_keep_when_correctness_rises_beyond_the_control_spread(self):
        text, word = self.verdict(arm(2), arm(4))
        self.assertEqual(word, "keep")
        self.assertIn("verdict: keep", text)
        self.assertIn("local_exact", text)

    def test_discard_when_correctness_drops(self):
        _, word = self.verdict(arm(4), arm(3))
        self.assertEqual(word, "discard")

    def test_inconclusive_when_nothing_moves(self):
        self.assertEqual(self.verdict(arm(3), arm(3))[1], "inconclusive")

    def test_keep_on_equal_correctness_and_ten_percent_fewer_tokens(self):
        self.assertEqual(self.verdict(arm(3, (100, 100)), arm(3, (80, 80)))[1], "keep")

    def test_a_task_that_flips_from_pass_to_fail_discards_even_when_the_mean_holds(self):
        a = arm(3)
        b = [dict(t, status="fail") if t["id"] == "local-0" else dict(t, status="pass") if t["id"] == "local-3" else t for t in arm(3)]
        self.assertEqual(compare.verdict([run_record("a", 1, a)], [run_record("b", 1, b)])[0], "discard")

    def test_spread_of_the_control_repeats_is_the_noise_floor(self):
        a = [run_record("a", 1, arm(2)), run_record("a", 2, arm(4))]
        _, word = compare.render(a, [run_record("b", 1, arm(4, (150, 75)))], "a", "b")  # same tokens per accepted task
        self.assertEqual(word, "inconclusive")

    def test_env_must_match_except_the_arm_gates(self):
        a = [run_record("a", 1, arm(3), env={"TP_PI_NODE": "", "TP_HL1": "0"})]
        ok = [run_record("b", 1, arm(3), env={"TP_PI_NODE": "", "TP_HL1": "1"})]
        self.assertEqual(compare.render(a, ok, "a", "b")[1], "inconclusive")
        bad = [run_record("b", 1, arm(3), env={"TP_PI_NODE": "/x", "TP_HL1": "1"})]
        with self.assertRaises(compare.CompareError) as caught:
            compare.render(a, bad, "a", "b")
        self.assertIn("TP_PI_NODE", str(caught.exception))
        compare.render(a, bad, "a", "b", allow_env_diff=True)

    def test_different_task_sets_are_refused(self):
        with self.assertRaises(compare.CompareError):
            compare.render([run_record("a", 1, arm(3), task_hash="1")], [run_record("b", 1, arm(3), task_hash="2")], "a", "b")


class Denominators(unittest.TestCase):
    """A task that errors or times out stays in every denominator and contributes only misses."""
    KINDS = {"local": ("local_exact", "pass"), "code": ("code_pass", "pass"), "review": ("review_catch", "scored")}

    def test_every_kind_times_every_outcome_counts_the_task(self):
        for kind, (name, good) in self.KINDS.items():
            for outcome in ("ok", "error", "timeout"):
                status = good if outcome == "ok" else outcome
                extra = {"caught": 2, "defects": 2} if kind == "review" and outcome == "ok" else {}  # an unscored review prints nothing
                ok = task("a", kind, good, planted=2 if kind == "review" else 0, caught=2, defects=2)
                other = task("b", kind, status, planted=2 if kind == "review" else 0, **extra)
                got = record.metrics([ok, other])[name]
                self.assertEqual(got, 1.0 if outcome == "ok" else 0.5, f"{kind} {outcome}")
        clean = dict(task("c", "review", "error"), clean=True)
        self.assertEqual(record.metrics([dict(clean, status="scored", detail={"false_positives": 1}), clean])["review_fp"], 0.5)
        for outcome in ("ok", "error", "timeout"):  # orient x {ok, error, timeout}: only a scored run knows its waste
            orient = [task("o1", "orient", "scored", orient_waste=2), task("o2", "orient", "scored" if outcome == "ok" else outcome,
                                                                          **({"orient_waste": 4} if outcome == "ok" else {}))]
            self.assertEqual(record.metrics(orient)["orient_waste"], 3.0 if outcome == "ok" else None,  # unknown, never averaged over the finished one
                             f"orient {outcome}")

    def test_a_scored_run_output_cannot_shrink_the_denominator(self):
        lying = task("r", "review", "scored", planted=3, caught=1, defects=1)
        self.assertEqual(record.metrics([lying])["review_catch"], 1 / 3)


class Records(unittest.TestCase):
    def test_unavailable_tokens_are_null_never_zero(self):
        self.assertEqual(record.token_figures({"input": 5, "output": None, "cacheRead": "x"}),
                         {"input": 5, "output": None, "cache_read": None, "cache_write": None})
        self.assertEqual(record.token_figures(None)["input"], None)
        total, notes = record.run_tokens([task("a", "code", "pass", (3, 4)), dict(task("b", "code", "pass"), tokens=None)])
        self.assertIsNone(total["input"])
        self.assertIn("b", notes["input"])
        self.assertIn("main", notes)

    def test_numbering_and_atomic_write_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(record.next_number(tmp, "ctl"), 1)
            data = run_record("ctl", 1, arm(2))
            record.write(record.record_path(tmp, "ctl", 1), data)
            self.assertEqual(record.next_number(tmp, "ctl"), 2)
            self.assertEqual(record.load_arm(tmp, "ctl")[0]["label"], "ctl")
            self.assertEqual([p.name for p in (Path(tmp) / "ctl").iterdir()], ["1.json"])
            with self.assertRaises(ValueError):
                record.load_arm(tmp, "none")

    def test_a_number_is_chosen_across_every_record_kind_and_nothing_is_replaced(self):
        # kind of the highest existing entry -> the next number must clear it
        for kind, name, as_dir in (("run record", "3.json", False), ("cleanup incident", "3.cleanup.json", False), ("artifacts dir", "3", True)):
            with self.subTest(kind), tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp) / "ctl"
                folder.mkdir()
                (folder / name).mkdir() if as_dir else (folder / name).write_text("{}")
                (folder / "1.json").write_text("{}")
                self.assertEqual(record.next_number(tmp, "ctl"), 4)
                self.assertEqual(record.reserve(tmp, "ctl"), 4)
                self.assertEqual(record.reserve(tmp, "ctl"), 5)
        with tempfile.TemporaryDirectory() as tmp:  # write() and copy_file_exclusive() refuse an existing file and leave it as it was
            target = Path(tmp) / "ctl" / "1.json"
            record.write(target, {"a": 1})
            before = target.read_bytes()
            with self.assertRaises(FileExistsError):
                record.write(target, {"a": 2})
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual([p.name for p in target.parent.iterdir()], ["1.json"])  # no temp file left behind
            copy = Path(tmp) / "copy.txt"
            record.copy_file_exclusive(target, copy)
            with self.assertRaises(FileExistsError):
                record.copy_file_exclusive(target, copy)
        with tempfile.TemporaryDirectory() as tmp:  # EEXIST on the claim: another run took the number between the look and the mkdir
            stale = iter([1, 1, 2])
            (Path(tmp) / "ctl" / "1").mkdir(parents=True)
            with mock.patch.object(record, "next_number", lambda *a: next(stale)):
                self.assertEqual(record.reserve(tmp, "ctl"), 2)


class Specs(unittest.TestCase):
    def test_validation_and_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            bench, _, loaded = fx.make_bench(tmp)
            self.assertEqual([s["id"] for s in specs.select(loaded, "local-*,code-*")], ["code-f", "local-q"])
            with self.assertRaises(specs.SpecError):
                specs.select(loaded, "nothing-*")
            with self.assertRaises(specs.SpecError):
                specs.validate({**loaded[0], "budget_s": 0})
            with self.assertRaises(specs.SpecError):
                specs.validate({**loaded[0], "schema": "other"})
            self.assertEqual(specs.task_set_hash(loaded, bench), specs.task_set_hash(list(reversed(loaded)), bench))
            before = specs.task_set_hash(loaded, bench)
            (bench / "diffs" / "r.patch").write_text("changed")
            self.assertNotEqual(before, specs.task_set_hash(loaded, bench))

    def test_the_shipped_specs_load_and_are_sealed_in_the_answers_directory(self):
        root = Path(__file__).resolve().parent.parent / "bench" / "tasks"
        loaded = specs.load_specs(root)
        kinds = sorted(s["kind"] for s in loaded)
        self.assertEqual(kinds.count("code"), 6)
        self.assertEqual(kinds.count("review"), 4)
        for spec in loaded:
            self.assertTrue(spec["kind"] == "orient" or spec["answer"].get("sha256"), spec["id"])


class Trace(unittest.TestCase):
    def test_orient_waste_counts_help_rereads_base_lookups_and_sleeps(self):
        read = {"name": "Read", "input": {"file_path": "/a", "offset": 1, "limit": 5}}
        calls = [{"name": "Bash", "input": {"command": "tp-agent dispatch --help"}}, read, dict(read),
                 {"name": "Bash", "input": {"command": "sleep 5"}}, {"name": "Bash", "input": {"command": "git merge-base HEAD main"}},
                 {"name": "Edit", "input": {"file_path": "/a"}}, dict(read)]
        got = trace.analyse(calls)
        self.assertEqual((got["help"], got["reread"], got["sleep_poll"], got["base_lookup"]), (1, 1, 1, 1))
        self.assertEqual(got["orient_waste"], 4)
        self.assertEqual(got["tool_counts"], {"Bash": 3, "Read": 3, "Edit": 1})


class ProjectLifecycle(unittest.TestCase):
    """Real git on a throwaway repo (the only test here that starts git; about 15 short processes)."""

    def test_tag_is_made_without_touching_the_working_tree_and_copies_leave_no_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

            def git(*args):
                return subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True, text=True, check=True).stdout.strip()
            git("init", "-q")
            (repo / "a.txt").write_text("one\n")
            git("add", "a.txt")
            git("commit", "-qm", "base")
            base = git("rev-parse", "HEAD")
            (repo / "wip.txt").write_text("uncommitted\n")  # the owner's work in progress must survive
            before = (git("status", "--porcelain"), git("rev-parse", "HEAD"), git("worktree", "list"))
            manifest = Path(tmp) / "agent-tools.json"
            manifest.write_text('{"tools": []}\n')
            shell = Shell()
            with mock.patch.object(project, "BASE_COMMIT", base):
                tag, created = project.ensure_base(shell, repo, manifest)
                self.assertTrue(created)
                self.assertEqual(project.ensure_base(shell, repo, manifest), (tag, False))
                self.assertEqual(git("rev-parse", f"{tag}^"), base)
                self.assertIn(project.MANIFEST_PATH, git("ls-tree", "-r", "--name-only", project.TAG))
                self.assertEqual((git("status", "--porcelain"), git("rev-parse", "HEAD"), git("worktree", "list")), before)
                copy = project.make_copy(shell, repo, Path(tmp) / "work" / "project")
                self.assertTrue((copy / "a.txt").is_file())
                self.assertFalse((copy / "wip.txt").exists())
                self.assertTrue(project.remove_copy(shell, repo, copy))
            self.assertEqual((git("status", "--porcelain"), git("rev-parse", "HEAD"), git("worktree", "list")), before)

    def test_a_tag_that_is_not_base_plus_manifest_is_not_touched(self):
        shell = fx.FakeShell(tempfile.gettempdir())
        shell.do_git = lambda argv, env: fx.Result(0, "other\n" if argv[-1].endswith("^") else "abc123\n", "")
        with self.assertRaises(project.ProjectError):
            project.ensure_base(shell, "/p", "/m")


if __name__ == "__main__":
    unittest.main()
