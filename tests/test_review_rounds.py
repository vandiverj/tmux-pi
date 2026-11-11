"""lib/review_rounds.py: earlier review rounds along the retry lineage, and the diff since the last reviewed tree."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import json
from pathlib import Path
import re
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

    def test_no_earlier_review_gives_no_findings_and_only_the_diff_against_the_review_base(self):
        # a plain task (no lane): round 1 carries the working tree against HEAD, staged, unstaged and untracked together
        (self.work / "a.txt").write_text("one\n")
        (self.work / "b.txt").write_text("keep\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        (self.work / "a.txt").write_text("two\n")
        (self.work / "staged.txt").write_text("s\n")
        self.git("add", "staged.txt")
        (self.work / "new.txt").write_text("fresh\n")
        self.task("w1")
        got = self.prepare("w1")
        self.assertEqual((got["rounds"], [Path(p).name for p in got["contexts"]]), (0, ["diff-now.md"]))
        self.assertIn("tree", got["snapshot"])
        text = Path(got["contexts"][0]).read_text()
        self.assertIn("against HEAD", text)
        for line in ("-one", "+two", "+s", "+fresh"):
            self.assertIn(line + "\n", text)
        self.assertNotIn("keep", text)  # an unchanged committed file is not in it

    def test_a_retried_task_nobody_reviewed_gets_the_change_against_head_not_a_diff_since_a_round(self):
        (self.work / "a.txt").write_text("one\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        (self.work / "a.txt").write_text("two\n")  # the first attempt's edit
        self.task("w0")
        (self.work / "b.txt").write_text("retry\n")  # the retry's edit
        self.task("w1", parent="w0")
        got = self.prepare("w1")
        self.assertEqual((got["rounds"], [Path(p).name for p in got["contexts"]]), (0, ["diff-now.md"]))
        text = Path(got["contexts"][0]).read_text()
        for line in ("-one", "+two", "+retry"):
            self.assertIn(line + "\n", text)

    def test_a_repository_with_no_commit_is_diffed_against_the_empty_tree(self):
        (self.work / "a.txt").write_text("one\n")
        self.task("w1")
        text = Path(self.prepare("w1")["contexts"][0]).read_text()
        self.assertIn("the empty tree", text)
        self.assertIn("+one\n", text)

    def test_the_first_round_diff_withholds_a_sensitive_path_and_redacts_a_secret_like_value(self):
        (self.work / ".env").write_text("DB_PASSWORD=madeup-ENVFILE-55kd\n")
        (self.work / "notes.txt").write_text("DB_PASSWORD=madeup-NOTES-31mv\n")
        self.task("w1")
        got = self.prepare("w1")
        text = Path(got["contexts"][0]).read_text()
        self.assertEqual(got["withheld"], [".env"])
        self.assertIn(".env", text)
        self.assertNotIn("madeup-ENVFILE-55kd", text)
        self.assertNotIn("madeup-NOTES-31mv", text)
        self.assertGreaterEqual(got["redactions"], 1)

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
        got = review_rounds.prepare(self.state, "w1", str(plain), self.out)
        self.assertEqual((got["snapshot"], got["contexts"]), (None, []))  # no tree, so no round-1 diff either

    def test_findings_come_oldest_first_across_ancestors_and_rereviews(self):
        self.review("r-a", "FIRST")
        self.review("r-b", "SECOND")
        self.review("r-c", "THIRD")
        self.task("w0", reviews=["r-a"])
        self.task("w1", parent="w0", reviews=["r-b", "r-c"])
        got = self.prepare("w1")
        names = [Path(p).name for p in got["contexts"]]
        self.assertEqual(names[0], "diff-since-round-3.md")  # the diff comes before the findings
        self.assertEqual(names[1:], ["earlier-findings-01-r-a.md", "earlier-findings-02-r-b.md", "earlier-findings-03-r-c.md"])
        self.assertEqual([Path(p).read_text().split("]\n\n", 1)[1] for p in got["contexts"][1:]], ["FIRST", "SECOND", "THIRD"])
        self.assertEqual(got["rounds"], 3)
        self.assertEqual(got["summarised"], [])  # no gate in the chain: nothing is summarised

    def gated_chain(self, gates, sweeps=()):
        """w0 reviewed by r-a; w1 by r-b, r-c; w2 by r-d. `gates` = {author: route} written to those tasks' meta.json; `sweeps` =
        authors given the sweep.md a gated retry of them writes (the only record of a gate before meta.json carried one)."""
        body = lambda name, head: f"REQUEST_CHANGES\nF1 — {head}\nFamily: {name}\nBODY-{name}-SECRET-TEXT\nCloses by: code\n"
        self.review("r-a", body("alpha", "first head"))
        self.review("r-b", body("beta", "second head"))
        self.review("r-c", body("gamma", "third head") + "F2 — another heading\n")
        self.review("r-d", body("delta", "fourth head"))
        self.task("w0", reviews=["r-a"])
        self.task("w1", parent="w0", reviews=["r-b", "r-c"], **({"gate": gates["w1"]} if "w1" in gates else {}))
        self.task("w2", parent="w1", reviews=["r-d"], **({"gate": gates["w2"]} if "w2" in gates else {}))
        for author in sweeps:
            (self.state / "tasks" / author / "sweep.md").write_text("# sweep\n")
        return self.prepare("w2")

    def attached(self, got):
        return [Path(p).name for p in got["contexts"][1:]]

    def test_a_gate_whose_trigger_is_the_first_review_leaves_every_round_in_full(self):
        got = self.gated_chain({"w1": "design"})  # r-a (round 1) triggered it: nothing precedes it
        self.assertEqual(self.attached(got), ["earlier-findings-01-r-a.md", "earlier-findings-02-r-b.md",
                                              "earlier-findings-03-r-c.md", "earlier-findings-04-r-d.md"])
        self.assertEqual((got["rounds"], got["summarised"]), (4, []))

    def test_the_review_before_the_gate_is_the_first_one_in_full_and_older_ones_are_summarised(self):
        got = self.gated_chain({"w2": "design"})  # w2's gate was triggered by r-c (round 3)
        self.assertEqual(self.attached(got), ["earlier-summary.md", "earlier-findings-03-r-c.md", "earlier-findings-04-r-d.md"])
        self.assertEqual([(s["round"], s["review"]) for s in got["summarised"]], [(1, "r-a"), (2, "r-b")])
        self.assertEqual(got["rounds"], 4)
        self.assertEqual(got["families"], [["alpha"], ["beta"], ["gamma"], ["delta"]])  # the family names of every round still reach the review text

    def test_two_gates_the_last_one_wins(self):
        # w1 alone (r-a triggered it) would summarise nothing; w2's gate, triggered by r-c, wins
        two = self.gated_chain({"w1": "rule-first", "w2": "design"})
        self.assertEqual([s["review"] for s in two["summarised"]], ["r-a", "r-b"])
        self.assertEqual(self.attached(two), ["earlier-summary.md", "earlier-findings-03-r-c.md", "earlier-findings-04-r-d.md"])

    def test_a_summary_line_keeps_the_verdict_families_and_headings_but_no_body(self):
        got = self.gated_chain({"w2": "rule-first"})
        text = Path(got["contexts"][1]).read_text()
        lines = [line for line in text.splitlines() if line.startswith("round ")]
        self.assertEqual(lines, ["round 1; review r-a; REQUEST_CHANGES; families: alpha; findings: F1 — first head",
                                 "round 2; review r-b; REQUEST_CHANGES; families: beta; findings: F1 — second head"])
        self.assertNotIn("SECRET-TEXT", text)
        self.assertIn("SECRET-TEXT", Path(got["contexts"][2]).read_text())  # a round inside the window keeps its body
        self.assertEqual(got["summarised"][0]["result"], str(self.state / "tasks" / "r-a" / "result.md"))

    def test_a_task_from_before_the_gate_field_is_gated_by_the_sweep_md_of_the_author_it_replaced(self):
        got = self.gated_chain({}, sweeps=["w1"])  # w1 was retried by a gated route (into w2) before retry wrote `gate`
        self.assertEqual(self.attached(got), ["earlier-summary.md", "earlier-findings-03-r-c.md", "earlier-findings-04-r-d.md"])
        self.assertEqual([s["review"] for s in got["summarised"]], ["r-a", "r-b"])

    def test_a_sweep_md_that_is_a_link_is_no_gate(self):
        target = self.state / "elsewhere.md"
        target.write_text("x")
        self.review("r-a", "REQUEST_CHANGES\nF1 — h\nFamily: alpha\nCloses by: code\n")
        self.review("r-b", "REQUEST_CHANGES\nF1 — h\nFamily: alpha\nCloses by: code\n")
        self.review("r-c", "REQUEST_CHANGES\nF1 — h\nFamily: alpha\nCloses by: code\n")
        self.task("w0", reviews=["r-a", "r-b"])
        self.task("w1", parent="w0", reviews=["r-c"])
        (self.state / "tasks" / "w0" / "sweep.md").symlink_to(target)
        self.assertEqual(self.prepare("w1")["summarised"], [])

    def test_a_gate_value_that_is_not_a_route_is_ignored(self):
        got = self.gated_chain({"w2": "park"})
        self.assertEqual((got["summarised"], len(self.attached(got))), ([], 4))

    def test_chain_labels_give_the_verdict_and_family_names_of_each_review_oldest_first(self):
        self.review("r-a", "REQUEST_CHANGES\nF1 x\nFamily: One\nCloses by: code\n")
        self.review("r-b", "APPROVED\n")
        self.review("r-c", "garbled\n")
        self.task("w0", reviews=["r-a", "r-b"])
        self.task("w1", parent="w0", reviews=["r-c", "r-gone"])
        self.assertEqual(review_rounds.chain_labels(self.state, "w1"),
                         [("w0", "r-a", "REQUEST_CHANGES", ["one"], 1), ("w0", "r-b", "APPROVED", [], 2),
                          ("w1", "r-c", None, [], 3), ("w1", "r-gone", None, [], None)])

    def test_chain_labels_number_only_reviews_with_a_reply_and_give_an_unreviewed_author_a_row(self):
        self.review("r-a", "REQUEST_CHANGES\n")
        self.review("r-b", "REQUEST_CHANGES\n")
        self.task("r-none")
        self.task("w0", reviews=["r-a"])
        self.task("w1", parent="w0")
        self.task("w2", parent="w1", reviews=["r-none", "r-b"])
        rows = review_rounds.chain_labels(self.state, "w2")
        self.assertEqual([(r[0], r[1], r[4]) for r in rows],
                         [("w0", "r-a", 1), ("w1", None, None), ("w2", "r-none", None), ("w2", "r-b", 2)])
        self.assertEqual(self.prepare("w2")["rounds"], 2)

    def test_prepare_returns_the_family_names_of_each_attached_round_and_skips_a_review_with_no_reply(self):
        self.review("r-a", "REQUEST_CHANGES\nF1 x\nFamily: one\nCloses by: code\n")
        self.review("r-b", "REQUEST_CHANGES\nF1 y\nCloses by: code\n")
        self.task("r-none")
        self.task("w1", reviews=["r-a", "r-none", "r-b"])
        got = self.prepare("w1")
        self.assertEqual((got["rounds"], got["families"]), (2, [["one"], []]))

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
        text = Path(self.prepare("w1")["contexts"][0]).read_text()
        self.assertIn("-one", text)
        self.assertIn("+two", text)
        self.assertIn("b.txt", text)

    def review_state(self):
        # the reviewed tree is committed, so HEAD holds it: the four states of every path start out alike
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", "reviewed")
        self.task("probe")
        return review_rounds.prepare(self.state, "probe", str(self.work), self.out)["snapshot"]

    def test_an_unchanged_tree_says_so(self):
        (self.work / "a.txt").write_text("one\n")
        self.review("r-a", "RC", self.review_state())
        self.task("w1", reviews=["r-a"])
        self.assertIn("identical", Path(self.prepare("w1")["contexts"][0]).read_text())

    def test_a_round_with_no_recorded_state_says_so_and_falls_back_to_the_change_against_the_review_base(self):
        (self.work / "a.txt").write_text("one\n")
        self.review("r-a", "RC")
        self.task("w1", reviews=["r-a"])
        text = Path(self.prepare("w1")["contexts"][0]).read_text()
        self.assertIn("recorded no reviewed state", text)
        self.assertIn("+one\n", text)  # the diff against the review base, not nothing

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

    def test_a_different_workdir_is_named_and_the_change_is_diffed_against_the_review_base_instead(self):
        (self.work / "a.txt").write_text("one\n")
        self.review("r-a", "RC", {"workdir": "/elsewhere", "tree": "0" * 40})
        self.task("w1", reviews=["r-a"])
        text = Path(self.prepare("w1")["contexts"][0]).read_text()
        self.assertIn("/elsewhere", text)
        self.assertIn("+one\n", text)

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
        self.assertTrue(Path(got["contexts"][1]).name.startswith("earlier-findings-01-r0"))  # [0] is the diff

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


class FourStateTests(unittest.TestCase):
    """The packet sections for a path whose HEAD or index differs from the files: base, HEAD, index and files, every way they can differ."""
    setUp, git, task, review = RoundsTests.setUp, RoundsTests.git, RoundsTests.task, RoundsTests.review

    @staticmethod
    def partitions(n=4):
        """Every way n states can be equal or different: 15 for four (restricted growth strings)."""
        def grow(prefix, top):
            if len(prefix) == n:
                yield tuple(prefix)
                return
            for value in range(top + 1):
                yield from grow(prefix + [value], max(top, value + 1))
        return list(grow([0], 1))

    def four_states(self, base, head, index, wt):
        """A fresh repository where a.txt is `base` in the base commit, `head` in HEAD, `index` in the index and `wt` in the files
        (bytes, or None for absent). Returns (the base commit, the working tree snapshot at the base)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.work = Path(tmp.name) / "w"
        self.work.mkdir()
        self.git("init", "-q")

        def put(content):
            if content is None:
                (self.work / "a.txt").unlink(missing_ok=True)
            else:
                (self.work / "a.txt").write_bytes(content)
        (self.work / "keep.txt").write_text("k\n")
        put(base)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        commit = subprocess.run(["git", "-C", str(self.work), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        at_base = review_rounds.snapshot(str(self.work))
        put(head)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", "head")
        put(index)
        self.git("add", "-A")
        put(wt)
        return commit, at_base

    def sections_of(self, text):
        found, current = {}, None
        for line in text.split("\n"):
            if line in (review_rounds.COMMITTED_HEAD, review_rounds.STAGED_HEAD):
                current = found[line] = []
            elif current is not None:
                current.append(line)
        return {k: "\n".join(v) for k, v in found.items()}

    def assert_sections(self, text, base, head, index, wt):
        """The rule: when HEAD or the index differs from the files, base to HEAD and HEAD to index each appear if not empty."""
        C, S = review_rounds.COMMITTED_HEAD, review_rounds.STAGED_HEAD
        lacks = head != wt or index != wt
        want = {}
        if lacks and base != head:
            want[C] = (base, head)
        if lacks and head != index:
            want[S] = (head, index)
        got = self.sections_of(text)
        self.assertEqual(set(got), set(want), text)
        for title, (old, new) in want.items():
            section = got[title]
            changed = [line for line in section.split("\n") if line and line[0] in "+-" and line[:3] not in ("+++", "---")]
            if any(c is not None and c.startswith(b"\0") for c in (old, new)):
                self.assertIn("Binary files a/a.txt and b/a.txt differ", section)
                self.assertEqual(changed, [])
                continue
            self.assertEqual(changed, ([] if old is None else ["-" + old.decode().strip()]) + ([] if new is None else ["+" + new.decode().strip()]), (title, text))
            self.assertEqual("new file mode" in section, old is None)
            self.assertEqual("deleted file mode" in section, new is None)

    def state_rows(self):
        """(name, base, head, index, wt): the 15 partitions of the four states, then deleted and binary rows."""
        rows = [("partition " + "".join(map(str, p)),) + tuple(f"v{k}\n".encode() for k in p) for p in self.partitions()]
        a, b = b"\0A", b"\0B"
        return rows + [("committed deletion, files restored", b"v0\n", None, None, b"v0\n"),
                       ("staged deletion, files restored", b"v0\n", b"v0\n", None, b"v0\n"),
                       ("added in HEAD, files removed", None, b"v0\n", b"v0\n", None),
                       ("binary committed, files restored", a, b, b, a),
                       ("binary staged, files restored", a, a, b, a)]

    @staticmethod
    def diff_paths(text):
        """The paths of the diff sections in text (main diff and held sections alike)."""
        return set(re.findall(r"^diff --git a/(.+) b/\1$", text, re.M))

    @staticmethod
    def listed_paths(scope):
        """The paths the scope lists (the `- path` lines before the commands)."""
        return {line[2:].split(" (untracked")[0] for line in scope.split("\n") if line.startswith("- ") and line != "- (none)"}

    def assert_scope(self, attached, plan, states=None):
        """The scope's path list equals the set of paths in the attached diff sections, and (when the four states are given) names
        a.txt exactly when they are not all alike: scope and sections are asserted together, from the plan the diffs were made with."""
        scope = review_rounds.scope_text(str(self.work), plan=plan)[0]
        self.assertEqual(self.listed_paths(scope), self.diff_paths(attached), scope + attached)
        if states is not None:
            self.assertEqual("a.txt" in self.listed_paths(scope), len(set(states)) > 1, scope)
        return scope

    def run_scope_commands(self, scope, repo=None):
        """Every scoped command the scope prints is run (with a.txt as the path) and must exit 0."""
        commands = re.findall(r"`(GIT_LITERAL_PATHSPECS=1 git [^`]*)`", scope)
        self.assertGreaterEqual(len(commands), 3, scope)
        for command in commands:
            command = re.sub(r"<path>(?: \[<path> \.\.\.\])?(?: \.\.\.)?", "a.txt", command)
            run = subprocess.run(command, shell=True, capture_output=True, text=True, cwd=repo or self.work)
            self.assertEqual(run.returncode, 0, (command, run.stderr))

    def test_round_one_sections_follow_the_four_state_rule_for_every_way_base_head_index_and_files_can_differ(self):
        self.assertEqual(len(self.partitions()), 15)
        red = review_rounds.redactor()
        for name, *states in self.state_rows():
            with self.subTest(name):
                commit, _ = self.four_states(*states)
                now = review_rounds.snapshot(str(self.work))
                main, _ = review_rounds.safe_diff(now["top"], commit, now["tree"])
                text, _, _, held = review_rounds.held_sections(str(self.work), commit, now["tree"], red)
                self.assert_sections(text, *states)
                plan = review_rounds.make_plan(now["top"], commit, "", now["tree"], held)
                self.assert_scope(main + text, plan, states)

    def test_a_later_round_follows_the_same_rule_whatever_the_record_says_about_head(self):
        # the previous round's tree is the base. Its record holds: the head at that time (HEAD has moved since for most rows), the
        # head HEAD is at now (HEAD has not moved since the previous review), or no head at all (an older record). The sections
        # depend on the four states alone, so all three must give the same packet.
        for variant in ("head then", "head now", "no head"):
            for number, (name, *states) in enumerate(self.state_rows()):
                with self.subTest(variant=variant, row=name):
                    commit, at_base = self.four_states(*states)
                    keep = {k: at_base[k] for k in ("workdir", "tree")}
                    if variant == "head then":
                        keep["head"] = at_base["head"]
                    elif variant == "head now":
                        keep["head"] = run_git(self.work, "rev-parse", "HEAD")
                    ident = f"{variant.replace(' ', '-')}-{number}"
                    self.review(f"r-{ident}", "REQUEST_CHANGES\n", state=keep)
                    self.task(f"w-{ident}", reviews=[f"r-{ident}"])
                    got = review_rounds.prepare(self.state, f"w-{ident}", str(self.work), self.out)
                    text = Path(got["contexts"][0]).read_text()
                    self.assert_sections(text, *states)
                    self.assert_scope(text, got["scope_input"], states)  # the real resolution: the plan prepare wrote, no patched base
                    main = text.split("\n" + review_rounds.COMMITTED_HEAD + "\n")[0].split("\n" + review_rounds.STAGED_HEAD + "\n")[0]
                    self.assertEqual("diff --git a/a.txt b/a.txt" in main, states[0] != states[3], main)


    def test_the_scope_names_each_comparison_of_the_sections(self):
        self.four_states(b"v0\n", b"v1\n", b"v2\n", b"v0\n")
        scope = review_rounds.scope_text(str(self.work))[0]
        self.assertIn(f"'{review_rounds.COMMITTED_HEAD}' is the base against HEAD", scope)
        self.assertIn(f"'{review_rounds.STAGED_HEAD}' is HEAD against the index", scope)
        self.assertIn("diff --cached HEAD -- <path> [<path> ...]` (staged: the index against HEAD)", scope)
        self.assertIn("- a.txt\n", scope)

    def test_every_scope_command_runs_in_a_round_one_and_a_later_round_scope(self):
        commit, at_base = self.four_states(b"v0\n", b"v1\n", b"v2\n", b"v0\n")
        self.run_scope_commands(review_rounds.scope_text(str(self.work))[0])
        self.review("r-cmd", "REQUEST_CHANGES\n", state={k: at_base[k] for k in ("workdir", "tree")})
        self.task("w-cmd", reviews=["r-cmd"])
        got = review_rounds.prepare(self.state, "w-cmd", str(self.work), self.out)
        scope = review_rounds.scope_text(str(self.work), plan=got["scope_input"])[0]
        self.assertIn("review round 1 reviewed", scope)
        self.run_scope_commands(scope)

    def test_a_repository_with_no_commit_follows_the_rule_with_the_empty_tree_as_head(self):
        # (index, files) with no commit: HEAD is the empty tree, the base of round 1 too; a later round's base is the tree it reviewed
        rows = [(None, b"v1\n"), (b"v1\n", b"v1\n"), (b"v1\n", None), (b"v1\n", b"v2\n"), (None, None)]
        for number, (index, wt) in enumerate(rows):
            for later in (False, True):
                with self.subTest(index=index, files=wt, later=later):
                    tmp = tempfile.TemporaryDirectory()
                    self.addCleanup(tmp.cleanup)
                    self.work = Path(tmp.name) / "w"
                    self.work.mkdir()
                    self.git("init", "-q")
                    (self.work / "a.txt").write_bytes(b"v0\n")
                    at_base = review_rounds.snapshot(str(self.work))
                    self.assertEqual(at_base["head"], "")
                    for content, staged in ((index, True), (wt, False)):
                        if content is None:
                            (self.work / "a.txt").unlink(missing_ok=True)
                        else:
                            (self.work / "a.txt").write_bytes(content)
                        if staged:
                            self.git("add", "-A")
                    ident = f"nc-{number}-{later}"
                    if later:
                        self.review(f"r-{ident}", "REQUEST_CHANGES\n", state={k: at_base[k] for k in ("workdir", "tree")})
                        self.task(f"w-{ident}", reviews=[f"r-{ident}"])
                    else:
                        self.task(f"w-{ident}")
                    got = review_rounds.prepare(self.state, f"w-{ident}", str(self.work), self.out)
                    text = Path(got["contexts"][0]).read_text()
                    scope = self.assert_scope(text, got["scope_input"])
                    sections = self.sections_of(text)
                    # HEAD is the empty tree: the staged section exists when the index holds the file, the committed one (the
                    # reviewed tree against the empty tree) in a later round whenever the index or the files hold it
                    self.assertEqual(review_rounds.STAGED_HEAD in sections, index is not None, text)
                    self.assertEqual(review_rounds.COMMITTED_HEAD in sections, later and not (index is None and wt is None), text)
                    self.run_scope_commands(scope)


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

    def round_one_diff(self):
        state, out = self.wt.parent / "state", self.wt.parent / "out"
        (state / "tasks" / "w1").mkdir(parents=True)
        out.mkdir()
        (state / "tasks" / "w1" / "meta.json").write_text(json.dumps({"id": "w1"}))
        got = review_rounds.prepare(state, "w1", str(self.wt), out, "alpha")
        self.assertEqual([Path(p).name for p in got["contexts"]], ["diff-now.md"])
        return Path(got["contexts"][0]).read_text()

    def test_a_lane_round_one_diff_is_against_the_last_closed_step(self):
        s1 = self.commit("s1.txt")
        self.commit("n1.txt")
        (self.wt / "dirty.txt").write_text("uncommitted\n")
        self.record([("s1", s1)])
        text = self.round_one_diff()
        self.assertIn(f"last closed step s1 at {s1[:12]}", text)
        for line in ("+++ b/n1.txt", "+++ b/dirty.txt", "+uncommitted"):
            self.assertIn(line, text)
        self.assertNotIn("s1.txt", text)  # the closed step is out of the diff

    def test_a_lane_round_one_diff_with_no_closed_step_is_against_the_lane_base(self):
        self.commit("n1.txt")
        self.record([])
        text = self.round_one_diff()
        self.assertIn(f"lane base {self.base[:12]}", text)
        self.assertIn("+++ b/n1.txt", text)

    def test_a_lane_commit_the_files_undo_and_a_staged_change_each_show_under_their_own_comparison(self):
        # base a.txt=one; HEAD two (committed), index three (staged), files one again: the main diff is empty
        self.record([])
        self.commit("a.txt", "two\n")
        (self.wt / "a.txt").write_text("three\n")
        run_git(self.wt, "add", "a.txt")
        (self.wt / "a.txt").write_text("one\n")
        text = self.round_one_diff()
        committed, staged = text.split(review_rounds.COMMITTED_HEAD + "\n")[1].split(review_rounds.STAGED_HEAD + "\n")
        self.assertIn("-one\n+two\n", committed)
        self.assertIn("-two\n+three\n", staged)
        self.assertNotIn("+three", committed)
        scope = self.scope()
        self.assertIn("- a.txt\n", scope)
        self.assertIn(f"diff {self.base} HEAD -- <path> [<path> ...]` (base against HEAD)", scope)
        self.assertIn("diff --cached HEAD -- <path> [<path> ...]` (staged: the index against HEAD)", scope)

    def test_a_lane_commit_the_files_undo_is_shown_when_the_index_equals_head(self):
        self.record([])
        self.commit("a.txt", "two\n")
        (self.wt / "a.txt").write_text("one\n")
        text = self.round_one_diff()
        self.assertIn(review_rounds.COMMITTED_HEAD + "\n", text)
        self.assertIn("-one\n+two\n", text)
        self.assertNotIn(review_rounds.STAGED_HEAD + "\n", text)
        self.assertIn("- a.txt\n", self.scope())

    def upstream(self, name, text):
        """A commit on main, the branch the lane started from (what the lane's step merges in)."""
        (self.main / name).write_text(text)
        run_git(self.main, "add", name)
        run_git(self.main, "commit", "-qm", f"upstream {name}")
        return run_git(self.main, "rev-parse", "HEAD")

    def merge(self, branch, resolve=None):
        run = subprocess.run(["git", "-C", str(self.wt), "-c", "user.email=t@t", "-c", "user.name=t", "merge", "--no-ff", "-m", "sync", branch],
                             capture_output=True, text=True)
        for name, text in (resolve or {}).items():
            (self.wt / name).write_text(text)
            run_git(self.wt, "add", name)
        if resolve:
            run_git(self.wt, "commit", "-qm", "sync")
        self.assertEqual(run.returncode, 1 if resolve else 0, run.stdout + run.stderr)
        return run_git(self.wt, "rev-parse", "HEAD")

    def test_a_step_that_merges_an_upstream_is_reviewed_by_its_resolution_not_by_what_upstream_brought_in(self):
        s1 = self.commit("s1.txt")  # the closed step the review is measured from; main does not descend from it
        self.record([("s1", s1)])
        before = self.commit("a.txt", "lane\n")
        self.upstream("a.txt", "up\n")
        self.upstream("big.txt", "UPSTREAM_ONLY\n" * 50)
        merge = self.merge("main", {"a.txt": "resolved\n"})
        (self.wt / "dirty.txt").write_text("uncommitted\n")
        text = self.round_one_diff()
        self.assertIn("An upstream merge was reduced to its resolution", text)
        self.assertIn(f"merge commit {merge[:12]} brought in upstream {run_git(self.wt, 'rev-parse', merge + '^2')[:12]}", text)
        before_part, rest = text.split(f"Committed before merge {merge[:12]}")[1].split("Resolution of merge")
        self.assertIn("+lane", before_part)  # what the lane committed before the merge: its own diff
        resolution, after = rest.split("Committed after the last merge")
        self.assertIn("-<<<<<<<", resolution)  # the conflict as it came out of the merge...
        self.assertIn("+resolved", resolution)  # ...and what the step made of it
        self.assertIn("+++ b/dirty.txt", after)
        self.assertIn("+uncommitted", after)
        self.assertNotIn("UPSTREAM_ONLY", text)
        self.assertNotIn("a/big.txt", text)  # (a commit subject in a conflict marker may name it)
        scope = self.scope()
        self.assertIn("- a.txt\n", scope)
        self.assertIn("- dirty.txt (untracked: read the file)\n", scope)
        self.assertNotIn("big.txt", scope)
        self.assertIn(f"show --remerge-diff {merge} -- <path>", scope)
        self.assertIn(f"diff {s1} {before} -- <path>", scope)
        self.assertIn(f"diff {merge} -- <path>", scope)
        self.assertIn("reduced to its resolution", scope)
        self.assertIn(f"relative to {s1[:12]} (an upstream merge reduced", scope)

    def test_a_clean_upstream_merge_shows_no_resolution_and_only_the_commit_on_top(self):
        s1 = self.commit("s1.txt")  # the closed step the review is measured from; main does not descend from it
        self.record([("s1", s1)])
        self.upstream("big.txt", "UPSTREAM_ONLY\n" * 50)
        self.merge("main")
        self.commit("n1.txt")
        text = self.round_one_diff()
        self.assertIn("(empty: the merge is a clean auto-merge", text)
        self.assertIn("+++ b/n1.txt", text)
        self.assertNotIn("a/big.txt", text)  # (a commit subject in a conflict marker may name it)
        self.assertNotIn("UPSTREAM_ONLY", text)
        scope = self.scope()
        self.assertIn("Diff scope: 1 file(s) changed", scope)
        self.assertIn("- n1.txt\n", scope)
        self.assertNotIn("big.txt", scope)

    def test_a_merge_of_a_branch_that_descends_from_the_base_is_an_ordinary_commit(self):
        s1 = self.commit("s1.txt")  # the closed step the review is measured from; main does not descend from it
        self.record([("s1", s1)])
        run_git(self.wt, "checkout", "-qb", "feature")
        self.commit("f1.txt", "FEATURE\n")
        run_git(self.wt, "checkout", "-q", "lane-b")
        self.commit("n1.txt")
        self.merge("feature")
        text = self.round_one_diff()
        self.assertNotIn("reduced to its resolution", text)
        self.assertIn("+FEATURE", text)
        self.assertNotIn("reduced", self.scope())

    def test_a_git_without_remerge_diff_keeps_the_whole_diff(self):
        s1 = self.commit("s1.txt")  # the closed step the review is measured from; main does not descend from it
        self.record([("s1", s1)])
        self.upstream("big.txt", "UPSTREAM_ONLY\n")
        self.merge("main")
        real = subprocess.run

        def old_git(command, *args, **kwargs):
            if "--remerge-diff" in command:
                return subprocess.CompletedProcess(command, 129, b"", b"unknown option")
            return real(command, *args, **kwargs)
        with mock.patch.object(review_rounds.subprocess, "run", old_git):
            text = self.round_one_diff()
        self.assertNotIn("reduced to its resolution", text)
        self.assertIn("+UPSTREAM_ONLY", text)

    def test_a_non_lane_scope_is_unchanged(self):
        text = review_rounds.scope_text(str(self.main), "")[0]
        self.assertIn("relative to HEAD (staged, unstaged and untracked)", text)
        self.assertNotIn("base:", text)


class WholeFileTests(unittest.TestCase):
    """RV-1: a modified text file whose hunks are longer than the file shown whole is attached whole (entry_patch, the one site).

    One repository, built once: the base commit is reviewed (round 1 state), then every kind of change is made in the files. The
    packet of round 1 (diff-now.md, against HEAD) and of round 2 (diff-since-round-1.md, against the reviewed tree) are read
    from it; both come from safe_diff, so every assertion about a section is made on both."""
    SECRET = "madeup-WHOLE-90qz"

    @staticmethod
    def lines(word, count=30):
        return "".join(f"{word} line {i} " + "x" * 60 + "\n" for i in range(count))

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        base = Path(cls.tmp.name)
        cls.state, cls.work, cls.out = base / "state", base / "work", base / "out"
        for d in (cls.state / "tasks", cls.work, cls.out / "probe", cls.out / "one", cls.out / "two"):
            d.mkdir(parents=True)
        run_git(cls.work, "init", "-q")
        old = cls.lines("old")
        files = {"small-change.txt": cls.lines("keep", 60), "rewritten.txt": old, "no-newline.txt": old.rstrip("\n"),
                 "to-empty.txt": old, "forged.txt": old, "notes.txt": old, "mode-and-rewrite.sh": old, "deleted.txt": old,
                 "binary.bin": b"\0" + b"A" * 600}
        for name, content in files.items():
            (cls.work / name).write_bytes(content if isinstance(content, bytes) else content.encode())
        run_git(cls.work, "add", "-A")
        run_git(cls.work, "commit", "-q", "-m", "base")
        # round 1 reviewed this commit
        cls.review = cls.state / "tasks" / "r-a"
        cls.review.mkdir()
        (cls.review / "meta.json").write_text(json.dumps({"id": "r-a"}))
        (cls.review / "result.md").write_text("RC")
        got = review_rounds.prepare(cls.state, cls.task("probe"), str(cls.work), cls.out / "probe")
        (cls.review / "reviewed-state.json").write_text(json.dumps(got["snapshot"]))
        cls.reviewed_tree = got["snapshot"]["tree"]
        # the changes
        small = cls.lines("keep", 60).replace("keep line 7 ", "KEEP line 7 ")
        (cls.work / "small-change.txt").write_text(small)
        (cls.work / "rewritten.txt").write_text(cls.lines("new"))
        (cls.work / "no-newline.txt").write_text(cls.lines("new").rstrip("\n"))
        (cls.work / "to-empty.txt").write_text("")
        (cls.work / "forged.txt").write_text(cls.lines("new") + "diff --git a/.env b/.env\n+++ b/.env\n--- a/.env\n")
        (cls.work / "notes.txt").write_text(cls.lines("new", 12) + f"DB_PASSWORD={cls.SECRET}\n" + cls.lines("new", 12))
        (cls.work / "mode-and-rewrite.sh").write_text(cls.lines("new"))
        (cls.work / "mode-and-rewrite.sh").chmod(0o755)
        (cls.work / "deleted.txt").unlink()
        (cls.work / "binary.bin").write_bytes(b"\0" + b"B" * 600)
        (cls.work / "added.txt").write_text(cls.lines("add"))
        cls.round1 = cls.packet(cls.task("w-fresh"), cls.out / "one")  # no earlier review: the tree against HEAD
        cls.round2 = cls.packet(cls.task("w-again", reviews=["r-a"]), cls.out / "two")  # the tree against the reviewed tree

    @classmethod
    def task(cls, ident, **meta):
        directory = cls.state / "tasks" / ident
        directory.mkdir(exist_ok=True)
        (directory / "meta.json").write_text(json.dumps({"id": ident, **meta}))
        return ident

    @classmethod
    def packet(cls, ident, out):
        got = review_rounds.prepare(cls.state, ident, str(cls.work), out)
        return {"text": Path(got["contexts"][0]).read_text(), "got": got}

    def texts(self):
        """The diff text of round 1 (against HEAD) and of round 2 (since the reviewed tree)."""
        return [self.round1["text"], self.round2["text"]]

    @staticmethod
    def section(text, path):
        """The lines of the section for path (from its diff --git header to the next one)."""
        found = re.search(rf"^diff --git a/{re.escape(path)} b/{re.escape(path)}\n(.*?)(?=^diff --git |\Z)", text, re.M | re.S)
        return found.group(1) if found else None

    def test_a_small_change_to_a_big_file_keeps_its_hunks(self):
        for text in self.texts():
            section = self.section(text, "small-change.txt")
            self.assertNotIn("Whole file", section)
            self.assertIn("-keep line 7 ", section)
            self.assertIn("+KEEP line 7 ", section)
            self.assertIn("@@ -", section)

    def test_a_rewritten_file_is_shown_whole_with_every_line_marked_plus_and_the_old_lines_left_out(self):
        for text in self.texts():
            section = self.section(text, "rewritten.txt")
            body = section.split("\n@@ whole file, 30 line(s) @@\n", 1)[1]
            self.assertEqual(body, self.lines("new").replace("new line", "+new line"))
            self.assertTrue(section.startswith("[Whole file shown instead of the hunks: its hunks are "), section[:80])
            self.assertEqual([l for l in section.split("\n") if l.startswith("-") and not l.startswith("--- ")], [])
            self.assertEqual(section.count("\n--- a/rewritten.txt\n+++ b/rewritten.txt\n"), 1)
            # the section is shorter than the hunks it replaced, and the note says both sizes
            note = section.split("\n", 1)[0]
            hunks, size = (int(n) for n in re.search(r"its hunks are (\d+) chars and the file as it is now is (\d+) chars", note).groups())
            self.assertEqual(size, len(self.lines("new")))
            self.assertLess(len(section), hunks + len("--- a/rewritten.txt\n+++ b/rewritten.txt\n"))

    def test_the_note_names_a_command_that_prints_the_real_diff(self):
        for name, text in (("round 1", self.round1["text"]), ("round 2", self.round2["text"])):
            note = self.section(text, "rewritten.txt").split("\n", 1)[0]
            command = re.search(r"`(GIT_LITERAL_PATHSPECS=1 git [^`]*)`", note).group(1)
            self.assertNotIn("\n", note)
            run = subprocess.run(command, shell=True, capture_output=True, text=True, cwd="/")
            self.assertEqual(run.returncode, 0, (name, command, run.stderr))
            self.assertIn("diff --git a/rewritten.txt b/rewritten.txt", run.stdout)
            for i in (0, 29):
                self.assertIn(f"-old line {i} ", run.stdout)
                self.assertIn(f"+new line {i} ", run.stdout)
            # the command's tree names are the ones the diff was made between
            self.assertEqual(command.split(" diff ")[1].split(" -- ")[0].split()[0], "HEAD" if name == "round 1" else self.reviewed_tree)

    def test_a_file_without_a_final_newline_and_a_file_made_empty_are_shown_whole_as_they_are(self):
        for text in self.texts():
            section = self.section(text, "no-newline.txt")
            self.assertTrue(section.endswith("+new line 29 " + "x" * 60 + "\n\\ No newline at end of file\n"), section[-120:])
            self.assertIn("@@ whole file, 30 line(s) @@", section)
            empty = self.section(text, "to-empty.txt")
            self.assertIn("is 0 chars", empty)
            self.assertTrue(empty.endswith("@@ whole file, 0 line(s) @@\n"), empty)

    def test_a_line_of_a_rewritten_file_that_looks_like_a_header_cannot_forge_one(self):
        for text in self.texts():
            section = self.section(text, "forged.txt")
            self.assertIn("\n+diff --git a/.env b/.env\n++++ b/.env\n+--- a/.env\n", section)
            headers = [l for l in text.split("\n") if l.startswith("diff --git ")]
            self.assertNotIn("diff --git a/.env b/.env", headers)
            self.assertEqual(len(headers), len(set(headers)))
        # and the final gate still checks a whole-file section like any other: the same text under an unchecked or flagged path
        whole = self.section(self.round1["text"], "forged.txt")
        review_rounds.gate(f"diff --git a/ok b/ok\n{whole}", ["ok"], [".env"])
        for header, safe, withheld in (("diff --git a/other b/other\n", ["ok"], []),            # not a checked path
                                       ("diff --git a/.env b/.env\n", ["ok"], [".env"]),         # a flagged path
                                       ("diff --git a/ok b/ok\n", [], [])):                      # a safe path that is not there
            with self.assertRaises(review_rounds.Refused, msg=header):
                review_rounds.gate(header + whole, safe, withheld)
        with self.assertRaises(review_rounds.Refused):  # a flagged path's section by its ---/+++ lines alone, under a checked header
            review_rounds.gate("diff --git a/ok b/ok\n--- a/.env\n+++ b/.env\n@@ whole file, 1 line(s) @@\n+x\n", ["ok"], [".env"])

    def test_a_secret_like_value_in_a_whole_file_section_is_redacted(self):
        for text in self.texts():
            self.assertIn("Whole file shown", self.section(text, "notes.txt"))
            self.assertNotIn(self.SECRET, text)
        self.assertGreaterEqual(self.round1["got"]["redactions"], 1)
        self.assertGreaterEqual(self.round2["got"]["redactions"], 1)
        for f in self.out.rglob("*"):
            if f.is_file():
                self.assertNotIn(self.SECRET, f.read_text(), f)

    def test_every_other_kind_of_entry_keeps_its_diff(self):
        for text in self.texts():
            added = self.section(text, "added.txt")
            self.assertTrue(added.startswith("new file mode 100644\n--- /dev/null\n+++ b/added.txt\n@@ -0,0 +1,30 @@\n"), added[:120])
            deleted = self.section(text, "deleted.txt")
            self.assertTrue(deleted.startswith("deleted file mode 100644\n--- a/deleted.txt\n+++ /dev/null\n@@ -1,30 +0,0 @@\n"), deleted[:120])
            self.assertEqual(self.section(text, "binary.bin"), "Binary files a/binary.bin and b/binary.bin differ\n")
            moded = self.section(text, "mode-and-rewrite.sh")  # a mode change with a rewrite: not a plain modification
            self.assertTrue(moded.startswith("old mode 100644\nnew mode 100755\n--- a/mode-and-rewrite.sh\n"), moded[:120])
            self.assertIn("-old line 0 ", moded)
            self.assertEqual(text.count("Whole file shown"), 5)  # rewritten, no-newline, to-empty, forged and secret: nothing else

    def test_the_boundary_a_tie_keeps_the_hunks_and_one_char_shorter_goes_whole(self):
        # entry_patch compares the hunk section with the whole-file section as attached, in characters. Git is stubbed (two blob
        # readers) so the hunks can be made exactly as long as the whole-file section.
        text = "a\nb\n"
        entry = ("p.txt", ("100644", "100644", "o" * 40, "n" * 40, "M"))
        whole = review_rounds.whole_file_section("/repo", "p.txt", "p.txt", "OLD", "NEW", "H" * 300, text)
        head = "--- a/p.txt\n+++ b/p.txt\n"

        def attach(hunks_len):
            hunks = ("@@ -1 +1 @@\n" + "-" * 2000)[:hunks_len - 1] + "\n"
            with mock.patch.object(review_rounds, "blob_patch", return_value=hunks), \
                    mock.patch.object(review_rounds, "blob_text", return_value=text):
                return review_rounds.entry_patch("/repo", "e" * 40, entry, "OLD", "NEW"), hunks
        tie = len(whole) - len(head)  # the hunks' length at which the two sections are equally long (the note's digits do not change here)
        self.assertEqual(len(review_rounds.whole_file_section("/repo", "p.txt", "p.txt", "OLD", "NEW", "H" * tie, text)), len(whole))
        patch, hunks = attach(tie)
        self.assertEqual(len(head + hunks), len(whole))
        self.assertEqual(patch, "diff --git a/p.txt b/p.txt\n" + head + hunks)  # equal: the hunks
        patch, hunks = attach(tie + 1)
        self.assertEqual(patch, "diff --git a/p.txt b/p.txt\n" + review_rounds.whole_file_section("/repo", "p.txt", "p.txt", "OLD", "NEW", hunks, text))  # one longer: whole
        patch, hunks = attach(tie - 1)
        self.assertEqual(patch, "diff --git a/p.txt b/p.txt\n" + head + hunks)  # shorter: the hunks


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

class FrozenCopiesTests(unittest.TestCase):
    """FC1: a file a `frozen-copies.json` manifest declares a byte-identical copy of an older commit's file is named, not repeated.

    One repository, built once: C1 holds the sources, C2 (HEAD, a child of C1) adds fx/old.txt and later.txt, and SIDE is a child of
    C1 that HEAD does not contain. Each test makes working-file changes (undone afterwards) and diffs the tree against HEAD."""
    LANES = "".join(f"lanes line {i} " + "x" * 40 + "\n" for i in range(40))

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.work = Path(cls.tmp.name) / "work"
        cls.work.mkdir()
        run_git(cls.work, "init", "-q")
        for name, text in {"lib/lanes.py": cls.LANES, "bin/tool": "#!/bin/sh\necho tool\n", "docs/é x.txt": "unicode name\n" * 5,
                           "server.pem": "-----MADEUP-KEY-----\n", "plain.txt": "A=madeup-value\n"}.items():
            (cls.work / name).parent.mkdir(exist_ok=True)
            (cls.work / name).write_text(text)
        (cls.work / "bin/tool").chmod(0o755)
        run_git(cls.work, "add", "-A")
        run_git(cls.work, "commit", "-q", "-m", "c1")
        cls.c1 = run_git(cls.work, "rev-parse", "HEAD")
        run_git(cls.work, "checkout", "-q", "-b", "side")
        (cls.work / "side.txt").write_text("side only\n")
        run_git(cls.work, "add", "-A")
        run_git(cls.work, "commit", "-q", "-m", "side")
        cls.side = run_git(cls.work, "rev-parse", "HEAD")
        run_git(cls.work, "checkout", "-q", "-b", "main2", cls.c1)
        (cls.work / "fx").mkdir()
        (cls.work / "fx/old.txt").write_text("something else\n")
        (cls.work / "later.txt").write_text("later\n")
        run_git(cls.work, "add", "-A")
        run_git(cls.work, "commit", "-q", "-m", "c2")
        cls.c2 = run_git(cls.work, "rev-parse", "HEAD")
        cls.orphan = run_git(cls.work, "commit-tree", "-m", "orphan", run_git(cls.work, "hash-object", "-t", "tree", "/dev/null"))  # a root: C1 is no ancestor of it
        cls.top = str(cls.work.resolve())

    def setUp(self):
        self.addCleanup(lambda: (run_git(self.work, "reset", "-q", "--hard"), run_git(self.work, "clean", "-fdq")))

    def put(self, name, text, mode=None):
        target = self.work / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        if mode:
            target.chmod(mode)

    def manifest(self, copies, directory="fx", raw=None):
        self.put(f"{directory}/frozen-copies.json" if directory else "frozen-copies.json", raw if raw is not None else json.dumps({"copies": copies}))

    def copy(self, path, source, commit=None):
        return {"path": path, "source": source, "commit": commit or self.c1[:7]}

    def diff(self, old="HEAD", anchor=None):
        tree = review_rounds.snapshot(self.work)["tree"]
        extra = {"anchor": anchor} if anchor else {}  # only the anchor tests need the new keyword
        return review_rounds.safe_diff(self.top, old, tree, **extra)

    def blob(self, source, commit=None):
        return run_git(self.work, "rev-parse", f"{commit or self.c1}:{source}")

    def left_out_note(self, path, source, label="added", commit=None):
        blob = self.blob(source)
        return (f'{json.dumps(path)} ({label}): byte-identical to {commit or self.c1[:7]}:{source} (blob {blob}), contents left out; '
                f"check with git cat-file -p {blob} or open the file\n")

    def assertShown(self, text, path):
        self.assertIn(f"diff --git a/{path} b/{path}\n", text)

    def assertLeftOut(self, text, path):
        self.assertNotIn(f"diff --git a/{path} b/{path}", text)

    def test_a_verified_copy_is_named_with_its_commit_source_and_blob_and_its_contents_left_out(self):
        self.put("fx/lanes.py", self.LANES)
        self.put("fx/tool", "#!/bin/sh\necho tool\n", 0o755)
        self.put("fx/old.txt", self.LANES)  # an existing file replaced by a copy: modified
        self.manifest([self.copy("lanes.py", "lib/lanes.py"), self.copy("tool", "bin/tool", self.c1),
                       self.copy("old.txt", "lib/lanes.py")])
        text, withheld = self.diff()
        self.assertEqual(withheld, [])
        self.assertIn(self.left_out_note("fx/lanes.py", "lib/lanes.py"), text)
        self.assertIn(self.left_out_note("fx/tool", "bin/tool", commit=self.c1), text)
        self.assertIn(self.left_out_note("fx/old.txt", "lib/lanes.py", "modified"), text)
        for path in ("fx/lanes.py", "fx/tool", "fx/old.txt"):
            self.assertLeftOut(text, path)
        self.assertNotIn("lanes line 3 ", text)
        self.assertShown(text, "fx/frozen-copies.json")  # the manifest is an ordinary changed file, shown in full
        self.assertIn('+{"copies": [{"path": "lanes.py"', text)
        self.assertEqual(text.count("diff --git "), 1)

    def test_a_copy_edited_by_one_byte_gets_its_full_diff_and_a_mismatch_note(self):
        self.put("fx/lanes.py", self.LANES.replace("lanes line 3 ", "lanes line 4 "))
        self.manifest([self.copy("lanes.py", "lib/lanes.py")])
        text, _ = self.diff()
        self.assertShown(text, "fx/lanes.py")
        self.assertIn("+lanes line 4 ", text)
        self.assertIn(f'frozen-copies.json: "fx/lanes.py" does not match {self.c1[:7]}:lib/lanes.py; its full diff follows\n', text)
        self.assertNotIn("byte-identical", text)

    def test_a_copy_with_the_wrong_mode_gets_its_full_diff_and_a_note(self):
        self.put("fx/tool", "#!/bin/sh\necho tool\n")  # same bytes, 100644 here and 100755 in the source
        self.manifest([self.copy("tool", "bin/tool")])
        text, _ = self.diff()
        self.assertShown(text, "fx/tool")
        self.assertIn(f'frozen-copies.json: "fx/tool" has mode 100644 but {self.c1[:7]}:bin/tool has mode 100755; its full diff follows\n', text)

    def test_a_commit_that_is_not_an_ancestor_or_does_not_exist_or_is_not_a_commit_id_gets_the_full_diff_and_a_note(self):
        self.put("fx/lanes.py", self.LANES)
        self.put("fx/side.txt", "side only\n")
        for path, source, commit, why in (
                ("lanes.py", "lib/lanes.py", "d" * 40, f"cannot be left out: commit {'d' * 40} does not resolve to a commit in this repository"),
                ("side.txt", "side.txt", self.side, f"cannot be left out: commit {self.side} is not an ancestor of {self.c2[:12]}"),
                ("lanes.py", "lib/lanes.py", "not-a-sha", "cannot be left out: its commit is not a commit id (7 to 64 hex digits)"),
                ("lanes.py", "lib/lanes.py", "HEAD", "cannot be left out: its commit is not a commit id (7 to 64 hex digits)"),
                ("lanes.py", "no/such/file", self.c1, f"cannot be left out: the source is not a file of commit {self.c1}"),
                ("lanes.py", "lib", self.c1, f"cannot be left out: the source is not a file of commit {self.c1}"),  # a directory is not a file
                ("lanes.py", "/lib/lanes.py", self.c1, "cannot be left out: its source is not a repository-relative path")):
            with self.subTest(commit=commit, source=source):
                self.manifest([self.copy(path, source, commit)])
                text, _ = self.diff()
                self.assertShown(text, f"fx/{path}")
                self.assertIn(f'frozen-copies.json: "fx/{path}" {why}; its full diff follows\n', text)
                self.assertNotIn("byte-identical", text)

    def test_the_commit_is_checked_against_the_commit_old_side_or_the_anchor_and_without_either_nothing_is_left_out(self):
        self.put("fx/lanes.py", self.LANES)
        self.manifest([self.copy("lanes.py", "lib/lanes.py")])
        tree = run_git(self.work, "rev-parse", "HEAD^{tree}")  # a later round's old side: a tree, not a commit
        text, _ = self.diff(old=tree)
        self.assertShown(text, "fx/lanes.py")
        self.assertIn(f"frozen-copies.json: \"fx/lanes.py\" cannot be left out: there is no commit on the old side to check that {self.c1[:7]} is an ancestor of; its full diff follows\n", text)
        text, _ = self.diff(old=tree, anchor=self.c2)  # the caller names the commit the old tree came from
        self.assertLeftOut(text, "fx/lanes.py")
        self.assertIn(self.left_out_note("fx/lanes.py", "lib/lanes.py"), text)
        text, _ = self.diff(old=tree, anchor=self.orphan)  # C1 is not an ancestor of the anchor
        self.assertShown(text, "fx/lanes.py")
        self.assertIn(f"cannot be left out: commit {self.c1[:7]} is not an ancestor of {self.orphan[:12]}; its full diff follows\n", text)
        text, _ = self.diff(old=self.c1)  # the old side is a commit: C2's own files are not an ancestor question here, C1 is its own ancestor
        self.assertLeftOut(text, "fx/lanes.py")

    def test_a_flagged_source_keeps_the_full_diff_and_a_flagged_path_keeps_its_withholding(self):
        self.put("fx/pem.txt", "-----MADEUP-KEY-----\n")
        self.put("fx/.env", "A=madeup-value\n")  # byte-identical to plain.txt, but a flagged path
        self.manifest([self.copy("pem.txt", "server.pem"), self.copy(".env", "plain.txt")])
        text, withheld = self.diff()
        self.assertEqual(withheld, ["fx/.env"])
        self.assertIn('"fx/.env" (added): sensitive path, its contents were withheld from this diff\n', text)
        self.assertNotIn("madeup-value", text)
        self.assertNotIn("byte-identical", text)
        self.assertShown(text, "fx/pem.txt")  # as without a manifest
        self.assertIn(f'frozen-copies.json: "fx/pem.txt" cannot be left out: {self.c1[:7]}:server.pem is a sensitive path; its full diff follows\n', text)

    def test_an_entry_the_manifest_does_not_list_is_an_ordinary_change_beside_a_listed_one(self):
        self.put("fx/lanes.py", self.LANES)
        self.put("fx/other.py", self.LANES)  # the same bytes, but not listed
        self.put("elsewhere/lanes.py", self.LANES)  # not under the manifest's directory
        self.manifest([self.copy("lanes.py", "lib/lanes.py")])
        text, _ = self.diff()
        self.assertLeftOut(text, "fx/lanes.py")
        self.assertShown(text, "fx/other.py")
        self.assertShown(text, "elsewhere/lanes.py")
        self.assertEqual(text.count("byte-identical"), 1)
        self.assertNotIn("frozen-copies.json: \"fx/other.py\"", text)

    def test_a_manifest_that_cannot_be_used_gives_full_diffs_and_a_note_and_never_raises(self):
        self.put("fx/lanes.py", self.LANES)
        good = self.copy("lanes.py", "lib/lanes.py")
        for raw, why in (("{not json", "it is not valid JSON"), ("[]", "it is not {"), ('{"copies": 3}', "it is not {"),
                         ('{"copies": [{"path": "lanes.py"}]}', "it is not {"), ('{"copies": [{"path": 1, "source": "a", "commit": "b"}]}', "it is not {"),
                         (json.dumps({"copies": [good, {"path": "x", "source": "", "commit": "c"}]}), "it is not {"),
                         ("[" * 200000, "it is not valid JSON"), ("\xff\xfe", "it is not valid JSON")):
            with self.subTest(raw=raw[:20]):
                self.manifest([], raw=raw)
                text, withheld = self.diff()
                self.assertShown(text, "fx/lanes.py")
                self.assertIn(f'frozen-copies.json: "fx/frozen-copies.json" cannot be used ({why}', text)
                self.assertIn('every entry under "fx" is shown in full\n', text)
                self.assertEqual(text.count("cannot be used"), 1)
                self.assertNotIn("byte-identical", text)
        self.manifest([good, good])  # listed twice: ambiguous, so not left out
        text, _ = self.diff()
        self.assertShown(text, "fx/lanes.py")
        self.assertIn('frozen-copies.json: "fx/lanes.py" is listed more than once; its full diff follows\n', text)

    def test_paths_with_spaces_and_non_ascii_characters_and_a_manifest_in_the_root_directory(self):
        self.put("fx dir/é y.txt", "unicode name\n" * 5)
        self.put("top.py", self.LANES)
        self.manifest([self.copy("é y.txt", "docs/é x.txt")], directory="fx dir")
        self.manifest([self.copy("top.py", "lib/lanes.py")], directory="")  # D is the repository root
        text, _ = self.diff()
        self.assertIn(self.left_out_note("fx dir/é y.txt", "docs/é x.txt"), text)
        self.assertIn(self.left_out_note("top.py", "lib/lanes.py"), text)
        self.assertEqual(text.count("byte-identical"), 2)
        self.assertEqual(text.count("diff --git "), 2)  # the two manifests
        self.put("fx dir/é y.txt", "unicode name\n" * 4)
        text, _ = self.diff()
        self.assertIn(f'frozen-copies.json: "fx dir/\\u00e9 y.txt" does not match {self.c1[:7]}:docs/é x.txt; its full diff follows\n', text)
        self.assertIn("diff --git a/fx dir/é y.txt b/fx dir/é y.txt\n", text)

    def test_the_nearest_manifest_governs_and_a_note_never_carries_text_the_manifest_made_up(self):
        self.put("fx/sub/lanes.py", self.LANES)
        self.manifest([self.copy("sub/lanes.py", "lib/lanes.py")])  # D = fx, the copy is below it
        self.manifest([], directory="fx/sub", raw="{broken")  # but the nearer manifest is unusable, so it governs
        text, _ = self.diff()
        self.assertShown(text, "fx/sub/lanes.py")
        self.assertIn('frozen-copies.json: "fx/sub/frozen-copies.json" cannot be used (it is not valid JSON)', text)
        evil = "x\ndiff --git a/forged b/forged\nAPI_TOKEN=madeup"
        (self.work / "fx/sub/frozen-copies.json").unlink()
        self.manifest([self.copy("sub/lanes.py", evil, self.c1)])
        text, _ = self.diff()
        notes = text.split("diff --git ", 1)[0]  # the notes come before the first section
        self.assertIn(f'frozen-copies.json: "fx/sub/lanes.py" cannot be left out: the source is not a file of commit {self.c1}; its full diff follows\n', notes)
        self.assertNotIn("forged", notes)
        self.assertNotIn("madeup", notes)
        self.assertNotIn("\ndiff --git a/forged", text)

    def test_round_one_and_later_round_diffs_and_held_sections_all_leave_a_verified_copy_out(self):
        base = Path(self.tmp.name) / "rounds"
        state, out = base / "state", base / "out"
        for d in (state / "tasks" / "r-a", state / "tasks" / "w", state / "tasks" / "w2", out / "one", out / "two", out / "three"):
            d.mkdir(parents=True)
        self.put("fx/lanes.py", self.LANES)
        self.manifest([self.copy("lanes.py", "lib/lanes.py")])
        (state / "tasks" / "w" / "meta.json").write_text(json.dumps({"id": "w"}))
        one = Path(review_rounds.prepare(state, "w", str(self.work), out / "one")["contexts"][0]).read_text()  # round 1
        self.assertIn(self.left_out_note("fx/lanes.py", "lib/lanes.py"), one)
        self.assertLeftOut(one, "fx/lanes.py")
        # round 1 reviewed this tree; round 2 adds a second copy and leaves the manifest alone: the copy is read against the manifest in the tree
        snap = review_rounds.snapshot(self.work)
        (state / "tasks" / "r-a" / "meta.json").write_text(json.dumps({"id": "r-a"}))
        (state / "tasks" / "r-a" / "result.md").write_text("RC")
        (state / "tasks" / "r-a" / "reviewed-state.json").write_text(json.dumps(snap))
        (state / "tasks" / "w2" / "meta.json").write_text(json.dumps({"id": "w2", "reviews": ["r-a"]}))
        self.manifest([self.copy("lanes.py", "lib/lanes.py"), self.copy("second.py", "lib/lanes.py")])
        review = lambda: Path(review_rounds.prepare(state, "w2", str(self.work), out / "two")["contexts"][0]).read_text()
        self.put("fx/second.py", self.LANES)
        two = review()
        self.assertIn(self.left_out_note("fx/second.py", "lib/lanes.py"), two)
        self.assertLeftOut(two, "fx/second.py")
        # a later round where only the manifest changes: no copy is in the diff, the manifest is shown, nothing raises
        run_git(self.work, "clean", "-fdq")
        run_git(self.work, "reset", "-q", "--hard")
        self.put("fx/lanes.py", self.LANES)
        self.manifest([self.copy("lanes.py", "lib/lanes.py")])
        snap = review_rounds.snapshot(self.work)
        (state / "tasks" / "r-a" / "reviewed-state.json").write_text(json.dumps(snap))
        self.manifest([self.copy("lanes.py", "lib/lanes.py"), self.copy("later.py", "lib/lanes.py")])
        three = review()
        main = three.split(f"\n{review_rounds.COMMITTED_HEAD}\n")[0]  # the main diff, before the held section for the files HEAD lacks
        self.assertEqual(main.count("diff --git "), 1)
        self.assertShown(main, "fx/frozen-copies.json")
        self.assertNotIn("byte-identical", three)
        # a held section: the copy is committed on top of C2, so base C2 to HEAD names it, and a later edit of the file keeps the section
        run_git(self.work, "add", "-A")
        run_git(self.work, "commit", "-q", "-m", "c3")
        self.addCleanup(lambda: run_git(self.work, "reset", "-q", "--hard", self.c2))
        self.put("fx/lanes.py", self.LANES + "edited\n")
        now = review_rounds.snapshot(self.work)
        text, _, _, held = review_rounds.held_sections(self.top, self.c2, now["tree"], review_rounds.redactor())
        self.assertEqual(held, ["fx/lanes.py"])
        self.assertIn(self.left_out_note("fx/lanes.py", "lib/lanes.py"), text)
        self.assertNotIn("diff --git a/fx/lanes.py", text)

    def test_the_gate_refuses_a_body_that_has_a_section_for_a_left_out_path(self):
        good = "diff --git a/ok b/ok\n--- a/ok\n+++ b/ok\n@@ -1 +1 @@\n-a\n+b\n"
        review_rounds.gate(good, ["ok"], [], ["fx/copy.txt"])
        for bad in (good + "diff --git a/fx/copy.txt b/fx/copy.txt\n+x\n",
                    good + "diff --git a/fx/copy.txt b/fx/copy.txt\n--- /dev/null\n+++ b/fx/copy.txt\n",
                    good + "--- a/fx/copy.txt\n+++ b/fx/copy.txt\n@@ -1 +1 @@\n"):  # a header pair under a checked header
            with self.assertRaises(review_rounds.Refused) as ctx:
                review_rounds.gate(bad, ["ok"], [], ["fx/copy.txt"])
            self.assertIn("left out", str(ctx.exception))
        review_rounds.gate(good + "--- a/fx/copy.txt\n", ["ok"], [], ["fx/copy.txt"])  # a lone line outside a hunk, with no `+++` line after it, is not a section
        with self.assertRaises(review_rounds.Refused):  # a path that is both safe and left out
            review_rounds.gate(good, ["ok"], [], ["ok"])

    def test_the_gate_reads_hunks_so_text_in_a_hunk_is_not_a_header_and_a_real_header_still_is(self):
        """Family frozen-gate-hunk-confusion: a removed line `-- a/x` and an added line `++ b/x` read `--- a/x` / `+++ b/x` in a hunk."""
        good = "diff --git a/ok b/ok\n--- a/ok\n+++ b/ok\n@@ -1 +1 @@\n-a\n+b\n"
        for name, safe, withheld, left in (("a left-out path", ["ok"], [], ["fx/copy.txt"]), ("a flagged path", ["ok"], [".env"], [])):
            path = left[0] if left else withheld[0]
            hunk_text = f"diff --git a/ok b/ok\n--- a/ok\n+++ b/ok\n@@ -1 +1 @@\n--- a/{path}\n+++ b/{path}\n"  # removed `-- a/x`, added `++ b/x`
            real_pair = f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-a\n+b\n"
            with self.subTest(name):
                review_rounds.gate(hunk_text, safe, withheld, left)  # (fails on 321568c: it read the two lines as a header pair)
                review_rounds.gate(hunk_text + "\\ No newline at end of file\n", safe, withheld, left)
                for what, body in (("a real header pair for it after a whole hunk", good + real_pair),
                                   ("a real header pair after a hunk cut short", good + "@@ -1,3 +1,3 @@\n-a\n+b\n" + real_pair.split("@@")[0]),
                                   ("a real header pair under a header that is not checked", "diff --git a/other b/other\n" + real_pair)):
                    with self.assertRaises(review_rounds.Refused, msg=what):  # (these refuse on 321568c too)
                        review_rounds.gate(body, safe, withheld, left)

    def test_a_diff_whose_hunks_cannot_be_followed_is_refused_not_attached(self):
        head = "diff --git a/ok b/ok\n--- a/ok\n+++ b/ok\n"
        for text in (head + "@@ -1,3 +1,3 @@\n-a\n+b\n",                                  # a hunk cut short by the end of the text
                     head + "@@ -1,3 +1,3 @@\n-a\n+b\ndiff --git a/ok b/ok\n",             # cut short by a header
                     head + "@@ -1,3 +1,3 @@\n-a\n+b\n\n c\n d\n",                        # an empty line is not a diff line
                     head + "@@ -0,0 +1 @@\n-a\n+b\n",                                    # more old lines than the header says
                     head + "@@ -1 +1 @@\n+a\n+b\n",                                      # more new lines than the header says
                     head + "@@ nonsense @@\n+a\n", head + "@@ -1 +1\n-a\n+b\n", head + "@@ whole file, x line(s) @@\n"):  # not hunk headers
            with self.subTest(text=text[len(head):]), self.assertRaises(review_rounds.Refused):
                review_rounds.gate(text, ["ok"], [])
        # what is followed: a missing count is 1, a backslash line uses neither count (mid-hunk or after), and the whole-file header
        for text in (head + "@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+a\n\\ No newline at end of file\n",
                     head + "@@ -1,2 +1,2 @@\n a\n-b\n+c\n@@ -9 +9 @@\n-x\n+y\n",
                     head + "@@ -0,0 +1,2 @@\n+a\n+b\n@@ whole file, 1 line(s) @@\n+z\n", head + "@@ -1 +1 @@\n-a\n+b\n"):
            with self.subTest(text=text[len(head):]):
                review_rounds.gate(text, ["ok"], [])

    def test_a_removed_line_and_an_added_line_that_read_like_headers_do_not_make_safe_diff_refuse(self):
        """The reviewer's case at 321568c: another file's hunk changes `-- a/copy` to `++ b/copy`, and `copy` is a verified copy."""
        self.put("later.txt", "-- a/copy\n")
        old = review_rounds.snapshot(self.work)["tree"]
        self.put("later.txt", "++ b/copy\n")
        self.put("copy", self.LANES)
        self.manifest([self.copy("copy", "lib/lanes.py")], directory="")
        new = review_rounds.snapshot(self.work)["tree"]
        text, withheld = review_rounds.safe_diff(self.top, old, new, anchor=self.c2)
        self.assertEqual(withheld, [])
        self.assertShown(text, "later.txt")
        self.assertIn("\n@@ -1 +1 @@\n--- a/copy\n+++ b/copy\n", text)  # the other file's hunk, shown as it is
        self.assertIn(self.left_out_note("copy", "lib/lanes.py"), text)
        self.assertLeftOut(text, "copy")

    def test_the_same_for_the_name_of_a_withheld_path_in_hunk_text(self):
        self.put("later.txt", "-- a/fx/k.pem\n")
        old = review_rounds.snapshot(self.work)["tree"]
        self.put("later.txt", "++ b/fx/k.pem\n")
        self.put("fx/k.pem", "-----MADEUP-KEY-----\n")  # a flagged path, changed in the same diff, so its name is the flagged name
        new = review_rounds.snapshot(self.work)["tree"]
        text, withheld = review_rounds.safe_diff(self.top, old, new)
        self.assertEqual(withheld, ["fx/k.pem"])
        self.assertShown(text, "later.txt")
        self.assertIn("\n@@ -1 +1 @@\n--- a/fx/k.pem\n+++ b/fx/k.pem\n", text)
        self.assertIn('"fx/k.pem" (added): sensitive path, its contents were withheld from this diff\n', text)
        self.assertNotIn("MADEUP-KEY", text)

    def git_calls(self, old="HEAD"):
        """({git subcommand: count}, [ls-tree argument lists], [cat-file argument lists]) of one safe_diff of the tree against `old`."""
        argvs, real = [], subprocess.Popen

        class Counting(real):  # subprocess.run starts its process through Popen, so this sees every call once
            def __init__(inner, argv, *args, **kwargs):
                if isinstance(argv, list) and argv[:2] == ["git", "-C"]:
                    argvs.append(argv[3:])
                super().__init__(argv, *args, **kwargs)
        tree = review_rounds.snapshot(self.work)["tree"]
        with mock.patch.object(subprocess, "Popen", Counting):
            text, _ = review_rounds.safe_diff(self.top, old, tree)
        counts = {}
        for argv in argvs:
            counts[argv[0]] = counts.get(argv[0], 0) + 1
        return counts, [a for a in argvs if a[0] == "ls-tree"], [a for a in argvs if a[0] == "cat-file"], text

    def test_git_work_follows_the_manifests_and_distinct_commits_not_the_copies(self):
        """rev-parse (old side, then each distinct commit) + merge-base + ls-tree (each commit's sources) once per commit, the manifests
        the diff does not show in one targeted ls-tree, and `cat-file -s` then `cat-file blob` per manifest."""
        sources = [("lanes.py", "lib/lanes.py", self.LANES), ("tool", "bin/tool", "#!/bin/sh\necho tool\n"),
                   ("é y.txt", "docs/é x.txt", "unicode name\n" * 5), ("plain.txt", "plain.txt", "A=madeup-value\n")]
        self.put("fx/lanes.py", self.LANES)
        self.manifest([self.copy("lanes.py", "lib/lanes.py")])
        one, ls, cat, text = self.git_calls()
        self.assertEqual(text.count("byte-identical"), 1)
        for many in (sources, sources + [(f"again{i}/{name}", source, body) for i, (name, source, body) in enumerate(sources)]):
            for name, _, body in many:
                self.put(f"fx/{name}", body, 0o755 if name.endswith("tool") else None)
            self.manifest([self.copy(name, source) for name, source, _ in many])
            counts, ls, cat, text = self.git_calls()
            with self.subTest(copies=len(many)):
                self.assertEqual(text.count("byte-identical"), len(many))
                for name in ("rev-parse", "merge-base", "ls-tree", "cat-file"):
                    self.assertEqual(counts.get(name), one.get(name), (name, counts, one))  # the same for 1 copy and for 8
        self.assertEqual({k: one[k] for k in ("rev-parse", "merge-base", "ls-tree", "cat-file")},
                         {"rev-parse": 2, "merge-base": 1, "ls-tree": 2, "cat-file": 2})
        self.assertTrue(all("-r" not in args for args in ls), ls)  # no whole-tree listing
        self.assertEqual(cat[0][1], "-s")  # the size is asked before the blob is read

    def test_no_whole_tree_listing_without_a_manifest_and_an_oversized_manifest_is_not_read(self):
        self.put("fx/a.txt", "a\n")
        self.put("b/c/d.txt", "d\n")
        counts, ls, cat, text = self.git_calls()
        self.assertEqual((counts.get("ls-tree"), counts.get("cat-file"), counts.get("merge-base")), (1, None, None))
        self.assertEqual(len(ls), 1)
        self.assertNotIn("-r", ls[0])  # asked for the manifest names of the touched directories only, never the tree
        self.assertEqual(sorted(a for a in ls[0] if a.endswith("frozen-copies.json")),
                         ["b/c/frozen-copies.json", "b/frozen-copies.json", "frozen-copies.json", "fx/frozen-copies.json"])
        self.assertEqual(text.count("diff --git "), 2)
        self.put("fx/lanes.py", self.LANES)
        self.manifest([self.copy("lanes.py", "lib/lanes.py")])
        with mock.patch.object(review_rounds, "MAX_MANIFEST", 20):
            counts, ls, cat, text = self.git_calls()
        self.assertEqual([a[1] for a in cat], ["-s"])  # the size was asked and the blob was not read
        self.assertIn('frozen-copies.json: "fx/frozen-copies.json" cannot be used (it is larger than 20 bytes); every entry under "fx" is shown in full\n', text)
        self.assertShown(text, "fx/lanes.py")

if __name__ == "__main__":
    unittest.main()
