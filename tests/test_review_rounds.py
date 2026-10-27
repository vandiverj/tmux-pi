"""lib/review_rounds.py: earlier review rounds along the retry lineage, and the diff since the last reviewed tree."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import review_rounds  # noqa: E402
import sensitive_read  # noqa: E402


class RoundsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.state, self.work, self.out = base / "state", base / "work", base / "out"
        for d in (self.state / "tasks", self.work, self.out):
            d.mkdir(parents=True)
        self.git("init", "-q")

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.work), "-c", "user.email=t@t", "-c", "user.name=t", *args], check=True, capture_output=True)

    def task(self, ident, **meta):
        d = self.state / "tasks" / ident
        d.mkdir()
        (d / "meta.json").write_text(json.dumps({"id": ident, **meta}))
        return d

    def review(self, ident, text, state=None):
        d = self.task(ident)
        (d / "result.md").write_text(text)
        if state:
            (d / "reviewed-state.json").write_text(json.dumps(state))
        return d

    def prepare(self, ident):
        return review_rounds.prepare(self.state, ident, str(self.work), self.out)

    def test_no_earlier_review_gives_no_contexts(self):
        self.task("w1")
        got = self.prepare("w1")
        self.assertEqual((got["contexts"], got["rounds"]), ([], 0))
        self.assertIn("tree", got["snapshot"])

    def test_the_snapshot_sees_untracked_files_and_leaves_the_index_alone(self):
        (self.work / "new.txt").write_text("x")
        self.task("w1")
        tree = self.prepare("w1")["snapshot"]["tree"]
        listing = subprocess.run(["git", "-C", str(self.work), "ls-tree", "-r", "--name-only", tree], capture_output=True, text=True).stdout
        self.assertIn("new.txt", listing)
        self.assertEqual(subprocess.run(["git", "-C", str(self.work), "diff", "--cached", "--name-only"], capture_output=True, text=True).stdout, "")

    def test_the_snapshot_never_touches_the_authors_index_head_or_files(self):
        (self.work / "tracked.txt").write_text("one")
        (self.work / "staged.txt").write_text("s")
        self.git("add", "tracked.txt")
        self.git("commit", "-q", "-m", "base")
        (self.work / "tracked.txt").write_text("two")      # modified, unstaged
        self.git("add", "staged.txt")                       # staged only
        (self.work / "untracked.txt").write_text("u")
        (self.work / "sub").mkdir()
        (self.work / "sub" / "deep.txt").write_text("d")

        def observe():
            git_dir = self.work / ".git"
            files = {str(p.relative_to(self.work)): (p.read_bytes(), p.stat().st_mtime_ns)
                     for p in sorted(self.work.rglob("*")) if p.is_file() and ".git" not in p.relative_to(self.work).parts}
            run = lambda *a: subprocess.run(["git", "--no-optional-locks", "-C", str(self.work), *a], capture_output=True, text=True).stdout
            return {"index": ((git_dir / "index").read_bytes(), (git_dir / "index").stat().st_mtime_ns),
                    "head": run("rev-parse", "HEAD"), "refs": run("for-each-ref"), "status": run("status", "--porcelain"),
                    "cached": run("diff", "--cached"), "files": files}
        before = observe()
        self.task("w1")
        self.assertIn("tree", self.prepare("w1")["snapshot"])
        self.assertEqual(observe(), before)

    def test_outside_git_there_is_no_snapshot(self):
        self.task("w1")
        plain = self.work.parent / "plain"
        plain.mkdir()
        self.assertIsNone(review_rounds.prepare(self.state, "w1", str(plain), self.out)["snapshot"])

    def test_findings_come_oldest_first_across_ancestors_and_rereviews(self):
        self.review("r-a", "FIRST")
        self.review("r-b", "SECOND")
        self.review("r-c", "THIRD")
        self.task("w0", reviews=["r-a"])
        self.task("w1", parent="w0", reviews=["r-b", "r-c"])
        got = self.prepare("w1")
        names = [Path(p).name for p in got["contexts"]]
        self.assertEqual(names[:3], ["earlier-findings-01-r-a.md", "earlier-findings-02-r-b.md", "earlier-findings-03-r-c.md"])
        self.assertEqual([Path(p).read_text().split("]\n\n", 1)[1] for p in got["contexts"][:3]], ["FIRST", "SECOND", "THIRD"])
        self.assertEqual(got["rounds"], 3)

    def test_a_single_legacy_review_field_counts_as_one_round(self):
        self.review("r-a", "ONLY")
        self.task("w1", review="r-a")
        self.assertEqual(self.prepare("w1")["rounds"], 1)

    def test_diff_is_the_change_since_the_last_reviewed_tree(self):
        (self.work / "a.txt").write_text("one\n")
        self.review("r-a", "RC", self.review_state())
        (self.work / "a.txt").write_text("two\n")
        (self.work / "b.txt").write_text("new\n")
        self.task("w1", reviews=["r-a"])
        text = Path(self.prepare("w1")["contexts"][-1]).read_text()
        self.assertIn("-one", text)
        self.assertIn("+two", text)
        self.assertIn("b.txt", text)

    def review_state(self):
        self.task("probe")
        return review_rounds.prepare(self.state, "probe", str(self.work), self.out)["snapshot"]

    def test_an_unchanged_tree_says_so(self):
        (self.work / "a.txt").write_text("one\n")
        self.review("r-a", "RC", self.review_state())
        self.task("w1", reviews=["r-a"])
        self.assertIn("identical", Path(self.prepare("w1")["contexts"][-1]).read_text())

    def test_a_round_with_no_recorded_state_says_so_instead_of_guessing(self):
        self.review("r-a", "RC")
        self.task("w1", reviews=["r-a"])
        self.assertIn("recorded no reviewed state", Path(self.prepare("w1")["contexts"][-1]).read_text())

    def test_unusable_recorded_state_refuses(self):
        self.review("r-a", "RC", {"workdir": str(self.work.resolve()), "tree": "0" * 40})
        self.task("w1", reviews=["r-a"])
        with self.assertRaises(review_rounds.Refused):
            self.prepare("w1")
        bad = self.review("r-b", "RC")
        (bad / "reviewed-state.json").write_text("[]")
        self.task("w2", reviews=["r-b"])
        with self.assertRaises(review_rounds.Refused):
            self.prepare("w2")

    def test_a_different_workdir_is_named_not_diffed(self):
        self.review("r-a", "RC", {"workdir": "/elsewhere", "tree": "0" * 40})
        self.task("w1", reviews=["r-a"])
        self.assertIn("/elsewhere", Path(self.prepare("w1")["contexts"][-1]).read_text())

    def test_a_review_that_never_replied_is_skipped_and_reported(self):
        self.task("r-a")
        self.review("r-b", "REAL")
        self.task("w1", reviews=["r-a", "r-b"])
        got = self.prepare("w1")
        self.assertEqual((got["rounds"], got["skipped"]), (1, ["r-a"]))

    def test_an_unreadable_task_or_bad_id_in_the_lineage_refuses(self):
        with self.assertRaises(review_rounds.Refused):
            self.prepare("missing")
        self.task("w1", parent="../x")
        with self.assertRaises(review_rounds.Refused):
            self.prepare("w1")
        self.task("w2", reviews=["../etc"])
        with self.assertRaises(review_rounds.Refused):
            self.prepare("w2")

    def chain(self, length):
        """w0 <- w1 <- ... each with one review r<i> that replied; returns the newest id."""
        for i in range(length):
            self.review(f"r{i}", f"REQUEST_CHANGES\nround {i}")
            self.task(f"w{i}", reviews=[f"r{i}"], **({"parent": f"w{i - 1}"} if i else {}))
        return f"w{length - 1}"

    def test_a_long_lineage_is_walked_to_its_root(self):
        got = self.prepare(self.chain(40))
        self.assertEqual(got["rounds"], 40)
        self.assertTrue(Path(got["contexts"][0]).name.startswith("earlier-findings-01-r0"))

    def test_a_lineage_past_the_safety_limit_refuses_and_says_so(self):
        newest = self.chain(review_rounds.MAX_ANCESTORS + 2)
        with self.assertRaises(review_rounds.Refused) as caught:
            self.prepare(newest)
        self.assertIn("longer than", str(caught.exception))

    def test_a_lineage_that_loops_refuses(self):
        self.task("a", parent="b")
        self.task("b", parent="a")
        with self.assertRaises(review_rounds.Refused) as caught:
            self.prepare("a")
        self.assertIn("loops", str(caught.exception))

    def test_reviews_that_are_not_task_ids_refuse_instead_of_being_dropped(self):
        for bad in ({"reviews": ["r1", 7]}, {"reviews": "r1"}, {"review": 7}):
            with self.subTest(bad=bad):
                d = self.state / "tasks" / "w"
                d.mkdir(exist_ok=True)
                (d / "meta.json").write_text(json.dumps({"id": "w", **bad}))
                with self.assertRaises(review_rounds.Refused):
                    self.prepare("w")


class SecretsStayOutTests(RoundsTests):
    """The diff and findings attached to a review never carry a credential (all values here are made up)."""
    SECRET = "hunter2-MADEUP-zq81vx"

    def packet_text(self, ident):
        got = self.prepare(ident)
        return "\n".join(Path(f).read_text() for f in got["contexts"]), got

    def reviewed(self, text="RC"):
        self.review("r-a", text, self.review_state())
        self.task("w1", reviews=["r-a"])

    def test_an_untracked_env_file_is_named_and_its_contents_withheld(self):
        (self.work / "a.txt").write_text("one\n")
        self.reviewed()
        (self.work / ".env").write_text(f"DB_PASSWORD={self.SECRET}\n")
        (self.work / "a.txt").write_text("two\n")
        text, got = self.packet_text("w1")
        self.assertNotIn(self.SECRET, text)
        self.assertIn(".env", text)
        self.assertIn("contents were withheld", text)
        self.assertIn("+two", text)  # the rest of the diff is still there
        self.assertEqual(got["withheld"], [".env"])

    def test_a_tracked_sensitive_file_that_changed_is_withheld(self):
        (self.work / "server.pem").write_text("-----OLD-MADEUP-----\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.reviewed()
        (self.work / "server.pem").write_text(f"-----{self.SECRET}-----\n")
        text, got = self.packet_text("w1")
        self.assertNotIn(self.SECRET, text)
        self.assertNotIn("OLD-MADEUP", text)
        self.assertEqual(got["withheld"], ["server.pem"])

    def test_a_deleted_sensitive_file_does_not_show_its_old_contents(self):
        (self.work / ".env").write_text(f"DB_PASSWORD={self.SECRET}\n")
        self.reviewed()
        (self.work / ".env").unlink()
        text, got = self.packet_text("w1")
        self.assertNotIn(self.SECRET, text)
        self.assertIn("deleted", text)

    def test_a_rename_into_and_out_of_a_sensitive_path_withholds_the_sensitive_side(self):
        (self.work / "plain.txt").write_text(f"token note {self.SECRET}\n")
        (self.work / ".env.old").write_text(f"OTHER_MADEUP={self.SECRET}-two\n")
        self.reviewed()
        (self.work / "plain.txt").rename(self.work / ".env")           # into a sensitive path
        (self.work / ".env.old").rename(self.work / "public-copy.txt")  # out of one
        text, got = self.packet_text("w1")
        self.assertNotIn(self.SECRET, text)
        self.assertEqual(sorted(got["withheld"]), [".env", ".env.old"])

    HIDDEN = "ZXQ-madeup-7781-not-a-pattern"

    def assert_packet_clean(self, ident="w1"):
        text, got = self.packet_text(ident)
        for f in self.out.rglob("*"):
            if f.is_file():
                self.assertNotIn(self.HIDDEN, f.read_text(), f)
        return text, got

    def test_a_file_that_becomes_a_directory_of_a_sensitive_file_does_not_widen(self):
        (self.work / "foo").write_text("plain\n")
        self.reviewed()
        (self.work / "foo").unlink()
        (self.work / "foo").mkdir()
        (self.work / "foo" / ".env").write_text(f"DB_PASSWORD={self.HIDDEN}\n")
        text, got = self.assert_packet_clean()
        self.assertEqual(got["withheld"], ["foo/.env"])
        self.assertIn("-plain", text)

    def test_a_directory_that_becomes_a_file_does_not_show_the_old_sensitive_descendant(self):
        (self.work / "foo").mkdir()
        (self.work / "foo" / ".env").write_text(f"DB_PASSWORD={self.HIDDEN}\n")
        (self.work / "foo" / "keep.txt").write_text("k\n")
        self.reviewed()
        import shutil
        shutil.rmtree(self.work / "foo")
        (self.work / "foo").write_text("now a file\n")
        text, got = self.assert_packet_clean()
        self.assertEqual(got["withheld"], ["foo/.env"])
        self.assertIn("+now a file", text)

    def test_odd_names_are_taken_literally_and_never_select_a_neighbour(self):
        names = [":(glob)*", ":x", "a*b[1]", "q?", "-dash", "sp ace ", "tab\tname", "new\\nline"]
        for n in names:
            (self.work / n).write_text("old\n")
        (self.work / "dir").write_text("old\n")
        (self.work / ".env").write_text(f"DB_PASSWORD={self.HIDDEN}\n")
        self.reviewed()
        for n in names:
            (self.work / n).write_text("new\n")
        (self.work / ".env").write_text(f"DB_PASSWORD={self.HIDDEN}-two\n")
        (self.work / "dir").unlink()
        (self.work / "dir").mkdir()
        (self.work / "dir" / "server.pem").write_text(f"{self.HIDDEN}\n")
        text, got = self.assert_packet_clean()
        self.assertEqual(sorted(got["withheld"]), [".env", "dir/server.pem"])
        for n in names:
            self.assertIn("+new", text)
        self.assertEqual(text.count("diff --git "), len(names) + 1)  # the odd names and dir's deletion, nothing else

    def test_the_final_gate_refuses_a_header_that_was_not_checked(self):
        good = "diff --git a/ok b/ok\n--- a/ok\n+++ b/ok\n@@ -1 +1 @@\n-a\n+b\n"
        review_rounds.gate(good, ["ok"], [".env"])
        for bad in (good + "diff --git a/.env b/.env\n", good + "diff --git a/ok b/ok\n", ""):
            with self.assertRaises(review_rounds.Refused):
                review_rounds.gate(bad, ["ok"], [".env"])

    def test_a_widened_patch_is_refused_by_the_gate_not_attached(self):
        (self.work / "foo").write_text("plain\n")
        self.reviewed()
        (self.work / "foo").unlink()
        (self.work / "foo").mkdir()
        (self.work / "foo" / ".env").write_text(f"DB_PASSWORD={self.HIDDEN}\n")
        widened = "diff --git a/foo/.env b/foo/.env\n+" + self.HIDDEN + "\n"
        with mock.patch.object(review_rounds, "entry_patch", return_value=widened):
            with self.assertRaises(review_rounds.Refused):
                self.prepare("w1")
        self.assertEqual([f for f in self.out.rglob("diff-since*")], [])

    def test_every_file_written_for_the_reviewer_is_free_of_the_secret(self):
        self.reviewed(f"finding F1 mentions DB_PASSWORD={self.SECRET}-in-a-reply\n")
        (self.work / ".env").write_text(f"DB_PASSWORD={self.SECRET}\n")
        (self.work / "notes.txt").write_text(f"DB_PASSWORD={self.SECRET}-in-notes\n")
        got = self.prepare("w1")
        for f in self.out.rglob("*"):
            if f.is_file():
                self.assertNotIn(self.SECRET, f.read_text(), f)
        self.assertGreaterEqual(got["redactions"], 2)  # the finding and the safe file's line

    def test_a_rule_that_cannot_be_loaded_refuses(self):
        self.reviewed()
        (self.work / "a.txt").write_text("x\n")
        with mock.patch.dict(sys.modules, {"sensitive_read": None}):
            with self.assertRaises(review_rounds.Refused) as ctx:
                self.prepare("w1")
        self.assertIn("sensitive-read rule", str(ctx.exception))

    def test_a_rule_that_gives_no_answer_refuses(self):
        self.reviewed()
        (self.work / "a.txt").write_text("x\n")
        with mock.patch.object(sensitive_read, "classify", return_value={}):
            with self.assertRaises(review_rounds.Refused):
                self.prepare("w1")
        with mock.patch.object(sensitive_read, "classify", side_effect=RuntimeError("boom")):
            with self.assertRaises(review_rounds.Refused):
                self.prepare("w1")

    def test_a_path_that_cannot_be_decoded_refuses(self):
        (self.work / "a.txt").write_text("one\n")
        state = self.review_state()
        blob = subprocess.run(["git", "-C", str(self.work), "hash-object", "-w", "--stdin"], input=b"x", capture_output=True, check=True).stdout.strip()
        entry = b"100644 blob " + blob + b"\tbad-\xff-name\n"
        tree = subprocess.run(["git", "-C", str(self.work), "mktree"], input=entry, capture_output=True, check=True).stdout.decode().strip()
        self.review("r-a", "RC", {**state, "tree": tree})
        self.task("w1", reviews=["r-a"])
        with self.assertRaises(review_rounds.Refused) as ctx:
            self.prepare("w1")
        self.assertIn("cannot be decoded", str(ctx.exception))

    def test_a_redaction_pass_that_cannot_load_refuses(self):
        self.reviewed()
        with mock.patch.dict(sys.modules, {"redact": None}):
            with self.assertRaises(review_rounds.Refused):
                self.prepare("w1")


def run_git(directory, *args):
    return subprocess.run(["git", "-C", str(directory), "-c", "user.email=t@t", "-c", "user.name=t", *args], check=True, capture_output=True, text=True).stdout.strip()


class StepBaseTests(unittest.TestCase):
    """R6: a lane task is scoped from the last closed step's commit when it is an ancestor of HEAD, else from the lane base."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        import lanes
        self.lanes = lanes
        base = Path(tmp.name)
        self.main = base / "main"
        self.main.mkdir()
        run_git(self.main, "init", "-q", "-b", "main")
        (self.main / "a.txt").write_text("one\n")
        run_git(self.main, "add", "a.txt")
        run_git(self.main, "commit", "-qm", "base")
        self.base = run_git(self.main, "rev-parse", "HEAD")
        self.wt = base / "lane-wt"
        run_git(self.main, "worktree", "add", "-q", "-b", "lane-b", str(self.wt))
        self.wt = Path(self.wt).resolve()
        self.common = lanes.git_common_dir(str(self.main.resolve()))

    def commit(self, name, text="x\n"):
        (self.wt / name).write_text(text)
        run_git(self.wt, "add", name)
        run_git(self.wt, "commit", "-qm", f"add {name}")
        return run_git(self.wt, "rev-parse", "HEAD")

    def record(self, closed):
        entries = [{"step": step, "commit": commit, "tree": run_git(self.wt, "rev-parse", commit + "^{tree}"), "review": "r-1",
                    "verdict": "APPROVED", "closed_at": "2026-10-08T10:00:00+00:00"} for step, commit in closed]
        record = {"schema": "tp-lane:v1", "name": "alpha", "kind": "work", "state": "open", "repo": str(self.main.resolve()),
                  "git_common_dir": self.common, "branch": "lane-b", "worktree": str(self.wt), "base": {"ref": "main", "commit": self.base},
                  "created": "2026-10-08T10:00:00+00:00", "domain": "d", "touches": ["lib"], "closed": entries, "integrated": None, "defaulted": []}
        with self.lanes.lane_lock(self.common):
            self.lanes.write_lane(self.common, record)

    def scope(self):
        return review_rounds.scope_text(str(self.wt), "alpha")[0]

    def test_with_a_closed_step_the_base_is_its_commit_and_its_files_leave_the_list(self):
        s1 = self.commit("s1.txt")
        self.commit("n1.txt")
        self.commit("n2.txt")
        self.record([("s1", s1)])
        text = self.scope()
        self.assertIn(f"base: last closed step s1 at {s1[:12]}", text)
        self.assertIn(f"relative to {s1[:12]}", text)
        self.assertIn("- n1.txt\n", text)
        self.assertIn("- n2.txt\n", text)
        self.assertNotIn("s1.txt\n", text)
        self.assertIn(f"diff {s1} -- <path>", text)
        self.assertIn("closed steps are out of scope except where the new lines change or call them", text.lower().replace("steps closed earlier are", "closed steps are"))

    def test_the_last_of_several_closed_steps_is_the_base(self):
        s1 = self.commit("s1.txt")
        s2 = self.commit("s2.txt")
        self.commit("n1.txt")
        self.record([("s1", s1), ("s2", s2)])
        text = self.scope()
        self.assertIn(f"base: last closed step s2 at {s2[:12]}", text)
        self.assertNotIn("s2.txt\n", text)

    def test_a_lane_with_no_closed_step_is_scoped_from_the_lane_base_as_before(self):
        self.commit("n1.txt")
        self.record([])
        text = self.scope()
        self.assertIn(f"Diff scope: 1 file(s) changed relative to {self.base[:12]} (committed on the lane since its base", text)
        self.assertIn(f"base: lane base {self.base[:12]}", text)
        self.assertNotIn("closed step", text)
        self.assertIn(f"diff {self.base} -- <path>", text)

    def test_a_closed_commit_that_is_not_an_ancestor_of_head_falls_back_to_the_lane_base_and_says_so(self):
        run_git(self.main, "branch", "side", self.base)
        side = Path(self.wt.parent / "side-wt")
        run_git(self.main, "worktree", "add", "-q", str(side), "side")
        (side / "other.txt").write_text("o\n")
        run_git(side, "add", "other.txt")
        run_git(side, "commit", "-qm", "elsewhere")
        stray = run_git(side, "rev-parse", "HEAD")
        self.commit("n1.txt")
        self.record([("s1", stray)])
        text = self.scope()
        self.assertIn(f"base: lane base {self.base[:12]}", text)
        self.assertIn(f"closed step s1 at {stray[:12]} is not an ancestor of HEAD", text)
        self.assertIn(f"relative to {self.base[:12]}", text)
        self.assertNotIn("base: last closed step", text)

    def test_a_non_lane_scope_is_unchanged(self):
        text = review_rounds.scope_text(str(self.main), "")[0]
        self.assertIn("relative to HEAD (staged, unstaged and untracked)", text)
        self.assertNotIn("base:", text)


class TimerWaitsTests(unittest.TestCase):
    """T2: added lines in test files that wait on a clock are listed as facts for the reviewer."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.work = Path(tmp.name).resolve()
        run_git(self.work, "init", "-q", "-b", "main")
        (self.work / "old_test.ts").write_text("await sleep(1)\nconst a = 1\n")
        run_git(self.work, "add", "old_test.ts")
        run_git(self.work, "commit", "-qm", "base")

    def put(self, name, text):
        path = self.work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def waits(self):
        return review_rounds.timer_waits(str(self.work), "")

    def test_an_added_sleep_in_a_test_file_is_listed_with_path_and_line(self):
        self.put("src/foo_test.ts", "const a = 1\nawait sleep(50)\n")
        text = self.waits()
        self.assertIn("Timer waits in added test lines", text)
        self.assertIn("src/foo_test.ts:2: await sleep(50)", text)

    def test_the_same_line_in_a_file_that_is_not_a_test_path_is_not_listed(self):
        self.put("src/foo.ts", "await sleep(50)\n")
        text = self.waits()
        self.assertNotIn("src/foo.ts", text)
        self.assertIn("none were found", text)

    def test_every_test_path_pattern_and_every_timer_call_counts(self):
        calls = ("setTimeout(r, 5)", "sleep(2)", "delay(3)", "Deno.sleep(4)", "waitForTimeout(5)")
        for path in ("a/test_x.py", "a/x.spec.ts", "e2e/run.ts", "a/__tests__/x.ts", "lib/mytests/x.py"):
            self.put(path, "\n".join(calls) + "\n")
        text = self.waits()
        for path in ("a/test_x.py", "a/x.spec.ts", "e2e/run.ts", "a/__tests__/x.ts", "lib/mytests/x.py"):
            for number, call in enumerate(calls, 1):
                self.assertIn(f"{path}:{number}: {call}", text)

    def test_a_timer_ok_line_is_listed_and_marked_not_dropped(self):
        self.put("src/foo_test.ts", "setTimeout(r, 10) // timer-ok: debounce\nsleep(3)\n")
        text = self.waits()
        self.assertIn("src/foo_test.ts:1: setTimeout(r, 10) // timer-ok: debounce (timer-ok)", text)
        self.assertIn("src/foo_test.ts:2: sleep(3)\n", text + "\n")
        self.assertNotIn("sleep(3) (timer-ok)", text)

    def test_no_timer_lines_say_so_in_one_line(self):
        self.put("src/foo_test.ts", "const a = 1\n")
        lines = [line for line in self.waits().splitlines() if line.strip() and not line.startswith("#")]
        self.assertEqual(len(lines), 1)
        self.assertIn("none were found", lines[0])

    def test_only_added_lines_count_and_a_modified_tracked_file_gets_its_new_line_numbers(self):
        self.put("old_test.ts", "await sleep(1)\nconst a = 1\ndelay(7)\n")  # sleep(1) was already there
        text = self.waits()
        self.assertIn("old_test.ts:3: delay(7)", text)
        self.assertNotIn("old_test.ts:1:", text)

    def test_a_staged_new_file_and_a_committed_nothing_repository_are_read_too(self):
        self.put("new_test.py", "time.sleep(2)\n")
        run_git(self.work, "add", "new_test.py")
        self.assertIn("new_test.py:1: time.sleep(2)", self.waits())

    def test_a_withheld_path_is_never_read(self):
        self.put(".env.test", "sleep(9) SECRET-VALUE\n")
        text = self.waits()
        self.assertNotIn("SECRET-VALUE", text)
        self.assertNotIn(".env.test", text)

    def test_a_secret_in_a_listed_line_is_redacted(self):
        self.put("src/foo_test.ts", "sleep(1); const API_TOKEN = 'sk-abcdefghijklmnopqrstuvwxyz0123456789ABCD'\n")
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz0123456789ABCD", self.waits())

    def test_outside_git_it_says_it_was_not_computed(self):
        with tempfile.TemporaryDirectory() as plain:
            text = review_rounds.timer_waits(plain, "")
        self.assertIn("Timer waits in added test lines", text)
        self.assertIn("not a git repository", text)

    def test_past_the_cap_every_line_is_still_listed_as_path_and_line_and_the_header_counts(self):
        cap = review_rounds.MAX_TIMER_LINES
        self.put("src/foo_test.ts", "".join(f"sleep({n})\n" for n in range(cap + 1)))
        text = self.waits()
        entries = [line for line in text.splitlines() if line.startswith("src/foo_test.ts:")]
        self.assertEqual(len(entries), cap + 1)
        self.assertEqual(entries[-1], f"src/foo_test.ts:{cap + 1}")
        self.assertEqual(entries[cap - 1], f"src/foo_test.ts:{cap}: sleep({cap - 1})")
        self.assertEqual(text.splitlines()[0], f"# Timer waits in added test lines: {cap + 1} matching added line(s); 1 listed as path:line without their text")

    def test_at_or_under_the_cap_the_first_line_says_none_lack_text(self):
        self.put("src/foo_test.ts", "sleep(1)\n")
        self.assertEqual(self.waits().splitlines()[0], "# Timer waits in added test lines: 1 matching added line(s); 0 listed as path:line without their text")

    def test_with_no_match_the_first_line_says_none_found(self):
        self.put("src/foo_test.ts", "const a = 1\n")
        self.assertEqual(self.waits().splitlines()[0], "# Timer waits in added test lines: none found")

    def test_when_it_cannot_be_computed_the_first_line_still_starts_with_the_title(self):
        with tempfile.TemporaryDirectory() as plain:
            first = review_rounds.timer_waits(plain, "").splitlines()[0]
        self.assertTrue(first.startswith("# Timer waits in added test lines: "), first)

    def test_a_long_matching_line_is_cut_at_400_with_the_mark(self):
        self.put("src/foo_test.ts", "sleep(1) " + "x" * 491 + "\n")
        text = self.waits()
        self.assertIn("src/foo_test.ts:1: sleep(1) " + "x" * 391 + "… (truncated, 500 chars)", text)
        self.assertNotIn("x" * 392, text)

    def test_the_command_writes_the_file(self):
        self.put("src/foo_test.ts", "await sleep(50)\n")
        out = self.work.parent / "timers.md"
        run = subprocess.run([sys.executable, str(ROOT / "lib/review_rounds.py"), "timers", str(self.work), "", str(out)], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("src/foo_test.ts:1: await sleep(50)", out.read_text())

if __name__ == "__main__":
    unittest.main()
