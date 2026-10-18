"""Lanes L1: the lane record, its validator, reading and writing the file, the check against git, the configuration."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import lanes  # noqa: E402

SHA = "a" * 40
SHA2 = "b" * 40


def git(cwd, *args):
    run = subprocess.run(["git", "-C", str(cwd)] + list(args), text=True, capture_output=True,
                         env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                              "GIT_COMMITTER_EMAIL": "t@t"})
    assert run.returncode == 0, run.stderr
    return run.stdout.strip()


def good(**over):
    record = {"schema": "tp-lane:v1", "name": "alpha", "kind": "work", "state": "open", "repo": "/r/repo",
              "git_common_dir": "/r/repo/.git", "branch": "work/orch-alpha", "worktree": "/r/repo-wt-alpha",
              "base": {"ref": "work/orch-integrate", "commit": SHA}, "created": "2026-10-08T10:00:00+00:00",
              "domain": "the parser", "touches": ["lib/a", "lib/b.py"], "closed": [], "integrated": None, "defaulted": [], "deps": "own"}
    record.update(over)
    return record


class RecordTests(unittest.TestCase):
    def refuses(self, record, text, name=None):
        with self.assertRaises(ValueError) as caught:
            lanes.lane_record(record, name)
        self.assertIn(text, str(caught.exception))

    def test_a_good_record_returns_a_new_dict_of_known_fields_only(self):
        record = good(extra="x")
        clean = lanes.lane_record(record, "alpha")
        self.assertNotIn("extra", clean)
        self.assertEqual(clean, {k: v for k, v in good().items()})
        clean["touches"].append("z")
        self.assertEqual(record["touches"], ["lib/a", "lib/b.py"])

    def test_every_field_missing_or_wrong_type_refuses(self):
        for key in lanes._REQUIRED_RECORD:
            with self.subTest(missing=key):
                record = good()
                del record[key]
                with self.assertRaises(ValueError):
                    lanes.lane_record(record)
        wrong = {"name": 5, "kind": "other", "state": "done", "repo": "rel", "git_common_dir": 3, "branch": "-x", "worktree": "/",
                 "base": "x", "created": "yesterday", "domain": "", "touches": "lib", "closed": {}, "integrated": {}}
        for key, bad in wrong.items():
            with self.subTest(wrong=key):
                with self.assertRaises(ValueError):
                    lanes.lane_record(good(**{key: bad}))

    def test_specific_refusals(self):
        self.refuses(good(schema="tp-lane:v2"), "schema")
        self.refuses([], "not an object")
        self.refuses(good(), "directory for 'beta'", name="beta")
        self.refuses(good(name="Alpha"), "invalid lane name")
        self.refuses(good(name="a" * 25), "invalid lane name")
        self.refuses(good(name="-a"), "invalid lane name")
        self.refuses(good(worktree="/r/repo/wt"), "outside the repository")
        self.refuses(good(worktree="/r"), "outside the repository")
        self.refuses(good(worktree="/r/repo-wt-alpha/"), "normalised absolute")
        self.refuses(good(repo="repo"), "normalised absolute")
        self.refuses(good(branch="a..b"), "valid branch")
        self.refuses(good(branch="@{-1}"), "valid branch")
        self.refuses(good(created="2026-10-08T10:00:00"), "UTC")
        self.refuses(good(created="2026-10-08T10:00:00+02:00"), "UTC")
        self.refuses(good(domain="x" * 201), "domain")
        self.refuses(good(domain="two\nlines"), "domain")
        self.refuses(good(base={"ref": "b", "commit": "abc"}), "hex")
        self.refuses(good(base={"ref": "b", "commit": SHA.upper()}), "hex")
        self.refuses(good(touches=["lib/b.py", "lib/a"]), "not sorted")
        self.refuses(good(touches=[]), "at least 1")
        self.refuses(good(defaulted=["nope"]), "defaulted")
        self.refuses(good(defaulted=["max_active_lanes", "branch_prefix"]), "defaulted")
        self.refuses(good(defaulted="branch_prefix"), "defaulted")
        self.assertEqual(lanes.lane_record(good(defaulted=["branch_prefix", "max_active_lanes"]))["defaulted"], ["branch_prefix", "max_active_lanes"])

    def test_recorded_paths_must_be_realpaths(self):
        """Finding 4: every recorded path field that goes through a symlink refuses; a future worktree is judged by its ancestors."""
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(os.path.realpath(tmp))
            (real / "repo").mkdir()
            (real / "repo/.git").mkdir()
            (real / "alias").symlink_to(real / "repo")
            (real / "elsewhere").mkdir()
            (real / "wt-alias").symlink_to(real / "elsewhere")
            base = dict(repo=str(real / "repo"), git_common_dir=str(real / "repo/.git"), worktree=str(real / "repo-wt-alpha"))
            self.assertEqual(lanes.lane_record(good(**base))["worktree"], str(real / "repo-wt-alpha"))  # not created yet: fine
            self.refuses(good(**dict(base, repo=str(real / "alias"))), "not a realpath")
            self.refuses(good(**dict(base, git_common_dir=str(real / "alias/.git"))), "not a realpath")
            self.refuses(good(**dict(base, worktree=str(real / "wt-alias"))), "not a realpath")
            self.refuses(good(**dict(base, worktree=str(real / "wt-alias/alpha"))), "not a realpath")  # future dir under a symlink
            self.refuses(good(**dict(base, worktree=str(real / "repo/.git/wt"))), "outside the repository")

    def test_closed_entries(self):
        entry = {"step": "L1", "commit": SHA, "tree": SHA2, "review": "rev-1", "verdict": "APPROVED", "closed_at": "2026-10-08T10:00:00+00:00"}
        self.assertEqual(lanes.lane_record(good(closed=[entry]))["closed"], [entry])
        self.refuses(good(closed=[entry, dict(entry, commit=SHA2)]), "twice")
        self.refuses(good(closed=[dict(entry, verdict="REQUEST_CHANGES")]), "verdict")
        self.refuses(good(closed=[dict(entry, verdict="approved")]), "verdict")
        self.refuses(good(closed=[dict(entry, step="bad step")]), "step id")
        self.refuses(good(closed=[dict(entry, review="")]), "task id")
        self.refuses(good(closed=[dict(entry, tree="xyz")]), "hex")
        self.refuses(good(closed=["x"]), "not an object")
        missing = dict(entry)
        del missing["review"]
        self.refuses(good(closed=[missing]), "no review")

    def test_a_boolean_is_not_a_number_or_text(self):
        self.refuses(good(name=True), "invalid lane name")
        self.refuses(good(closed=True), "not a list")
        self.refuses(good(integrated=True), "integration id")

    def test_integrated_is_set_exactly_when_the_state_says_so(self):
        done = {"integration": "integrate-0123456789ab", "commit": SHA, "at": "2026-10-08T10:00:00+00:00"}
        self.assertEqual(lanes.lane_record(good(state="integrated", integrated=done))["integrated"], done)
        self.refuses(good(state="integrated"), "exactly when")
        self.refuses(good(integrated=done), "exactly when")
        self.refuses(good(state="integrated", integrated=dict(done, integration="integrate-xyz")), "integration id")

    def test_touches_table(self):
        for bad in ("", "/abs", "../x", "a/../b", "a//b", "a/", ".git", ".git/x", "a/.git/b", "./a", "a/.", "a\nb", 5, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    lanes.path_prefix(bad)
        with self.assertRaises(ValueError):
            lanes.path_prefixes(["a", "a"])
        with self.assertRaises(ValueError):
            lanes.path_prefixes([], minimum=1)
        with self.assertRaises(ValueError):
            lanes.path_prefixes([f"p{i}" for i in range(51)])
        self.assertEqual(len(lanes.path_prefixes([f"p{i}" for i in range(50)])), 50)
        self.assertEqual(lanes.path_prefixes(["b", "a/c", "a"]), ["a", "a/c", "b"])

    def test_prefix_overlap_table(self):
        for a, b, expected in (("lib/a", "lib/a/b.py", True), ("lib/a", "lib/ab.py", False), ("lib", "lib", True),
                               ("lib/a/b.py", "lib/a", True), ("lib", "bin", False), ("a/b", "a/c", False)):
            with self.subTest(a=a, b=b):
                self.assertEqual(lanes.paths_overlap(a, b), expected)
                self.assertEqual(lanes.paths_overlap(b, a), expected)
        self.assertEqual(lanes.overlapping_paths(["lib", "bin/x"], ["lib/a.py", "bin", "doc"]), ["bin/x", "lib/a.py"])
        self.assertEqual(lanes.overlapping_paths(["a"], ["b"]), [])


class RepoFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(os.path.realpath(self.tmp.name))
        self.repo = base / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "work/orch-integrate")
        (self.repo / "f").write_text("1")
        git(self.repo, "add", "f")
        git(self.repo, "commit", "-qm", "one")
        self.base_commit = git(self.repo, "rev-parse", "HEAD")
        self.common = lanes.git_common_dir(self.repo)
        self.wt = base / "repo-wt-alpha"
        git(self.repo, "worktree", "add", "-q", "-b", "work/orch-alpha", str(self.wt), "HEAD")

    def record(self, **over):
        fields = dict(repo=str(self.repo), git_common_dir=self.common, worktree=str(self.wt),
                      base={"ref": "work/orch-integrate", "commit": self.base_commit})
        fields.update(over)
        return good(**fields)

    def commit(self, text):
        (self.wt / "g").write_text(text)
        git(self.wt, "add", "g")
        git(self.wt, "commit", "-qm", text)
        return git(self.wt, "rev-parse", "HEAD")

    def entry(self, step, commit):
        return {"step": step, "commit": commit, "tree": git(self.wt, "rev-parse", f"{commit}^{{tree}}"), "review": "rev-1",
                "verdict": "APPROVED", "closed_at": "2026-10-08T10:00:00+00:00"}


    def write(self, record=None):
        with lanes.lane_lock(self.common):
            return lanes.write_lane(self.common, record or self.record())


class FileTests(RepoFixture):
    def test_the_record_lives_in_the_common_git_dir_and_round_trips(self):
        self.write()
        path = Path(self.common) / "tmux-pi/lanes/alpha/lane.json"
        self.assertTrue(path.is_file())
        self.assertEqual(lanes.read_lane(self.common, "alpha"), self.record())
        self.assertEqual(lanes.lane_names(self.common), ["alpha"])
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_a_linked_worktree_finds_the_same_record(self):
        self.write()
        self.assertEqual(lanes.git_common_dir(self.wt), self.common)
        self.assertEqual(lanes.read_lane(lanes.git_common_dir(self.wt), "alpha")["name"], "alpha")
        self.assertEqual(lanes.main_worktree(self.common), str(self.repo))

    def test_no_lanes_directory_means_no_lanes(self):
        self.assertEqual(lanes.lane_names(self.common), [])
        with self.assertRaises(ValueError) as caught:
            lanes.read_lane(self.common, "alpha")
        self.assertIn("lane.json", str(caught.exception))

    def test_unreadable_records_refuse_naming_the_file(self):
        self.write()
        path = Path(self.common) / "tmux-pi/lanes/alpha/lane.json"
        cases = {"not json": "{", "array": "[]", "wrong schema": json.dumps(dict(self.record(), schema="x")),
                 "other name": json.dumps(dict(self.record(), name="beta")),
                 "other repository": json.dumps(dict(self.record(), git_common_dir="/elsewhere/.git"))}
        for label, text in cases.items():
            with self.subTest(label):
                path.write_text(text)
                with self.assertRaises(ValueError) as caught:
                    lanes.read_lane(self.common, "alpha")
                self.assertIn(str(path), str(caught.exception))
                self.assertIn("not used", str(caught.exception))

    def test_a_symlink_or_fifo_is_refused_unopened(self):
        self.write()
        path = Path(self.common) / "tmux-pi/lanes/alpha/lane.json"
        real = path.with_name("real.json")
        path.rename(real)
        path.symlink_to(real)
        with self.assertRaises(ValueError) as caught:
            lanes.read_lane(self.common, "alpha")
        self.assertIn("not a regular file", str(caught.exception))
        path.unlink()
        os.mkfifo(path)
        done = []
        thread = threading.Thread(target=lambda: done.append(self._try_read()), daemon=True)
        thread.start()
        thread.join(5)
        self.assertEqual(len(done), 1, "read_lane blocked on a FIFO")
        self.assertIn("not a regular file", done[0])
        with self.assertRaises(ValueError):  # write_lane replaces nothing that is not a regular file
            with lanes.lane_lock(self.common):
                lanes.write_lane(self.common, self.record())

    def _try_read(self):
        try:
            lanes.read_lane(self.common, "alpha")
        except ValueError as error:
            return str(error)

    def test_a_symlinked_lane_directory_is_refused(self):
        self.write()
        d = Path(self.common) / "tmux-pi/lanes/alpha"
        moved = d.with_name("moved")
        d.rename(moved)
        d.symlink_to(moved)
        with self.assertRaises(ValueError) as caught:
            lanes.read_lane(self.common, "alpha")
        self.assertIn("not a directory", str(caught.exception))

    def test_the_archive_directory_name_is_not_a_lane_name(self):
        """lane_names skips `archive`; a lane of that name would hold a slot nobody counts."""
        self.assertFalse(lanes.valid_lane_name("archive"))
        with self.assertRaises(ValueError):
            lanes.lane_record(self.record(name="archive"))
        with self.assertRaises(ValueError):
            lanes.lane_path(self.common, "archive")

    def test_a_bad_lane_name_refuses_before_any_path_is_built(self):
        for name in ("../x", "a/b", "", "A", ".lock", "archive/x"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    lanes.read_lane(self.common, name)

    def test_write_needs_the_lock_and_a_valid_record_and_this_repository(self):
        with self.assertRaises(ValueError) as caught:
            lanes.write_lane(self.common, self.record())
        self.assertIn("lock", str(caught.exception))
        with lanes.lane_lock(self.common):
            with self.assertRaises(ValueError):
                lanes.write_lane(self.common, dict(self.record(), kind="x"))
            with self.assertRaises(ValueError):
                lanes.write_lane(self.common, dict(self.record(), git_common_dir="/elsewhere/.git"))
            with self.assertRaises(ValueError):
                with lanes.lane_lock(self.common):
                    pass
        self.assertEqual(lanes.lane_names(self.common), [])  # nothing written by the refusals
        with self.assertRaises(ValueError):  # the lock is released
            lanes.write_lane(self.common, self.record())

    def test_a_corrupt_record_does_not_hide_the_others(self):
        self.write()
        bad = Path(self.common) / "tmux-pi/lanes/broken"
        bad.mkdir()
        (bad / "lane.json").write_text("{")
        (Path(self.common) / "tmux-pi/lanes/archive").mkdir()
        self.assertEqual(lanes.lane_names(self.common), ["alpha", "broken"])
        self.assertEqual(lanes.read_lane(self.common, "alpha")["name"], "alpha")
        with self.assertRaises(ValueError):
            lanes.read_lane(self.common, "broken")

    def test_the_write_is_atomic_and_leaves_no_temporary_file(self):
        self.write()
        self.write(self.record(domain="changed"))
        self.assertEqual(os.listdir(Path(self.common) / "tmux-pi/lanes/alpha"), ["lane.json"])
        self.assertEqual(lanes.read_lane(self.common, "alpha")["domain"], "changed")


class AnchoredPathTests(RepoFixture):
    """Finding 2: every component from the common git directory is opened without following a symlink."""

    def setUp(self):
        super().setUp()
        self.outside = Path(self.tmp.name) / "outside"
        self.outside.mkdir()
        self.tp = Path(self.common) / "tmux-pi"

    def test_a_symlinked_lanes_directory_is_not_read_listed_locked_or_written(self):
        self.write()
        moved = self.tp / "moved"
        (self.tp / "lanes").rename(moved)
        (self.tp / "lanes").symlink_to(moved)
        with self.assertRaises(ValueError):
            lanes.read_lane(self.common, "alpha")
        with self.assertRaises(ValueError):
            lanes.lane_names(self.common)
        with self.assertRaises(ValueError):
            with lanes.lane_lock(self.common):
                pass
        self.assertEqual(sorted(os.listdir(moved)), [".lock", "alpha"])  # only what the first write made

    def test_a_symlinked_tmux_pi_directory_is_not_followed_and_nothing_is_written_outside(self):
        (self.outside / "lanes").mkdir()
        self.tp.symlink_to(self.outside)
        with self.assertRaises(ValueError):
            self.write()
        with self.assertRaises(ValueError):
            with lanes.lane_lock(self.common):
                pass
        with self.assertRaises(ValueError):
            lanes.lane_names(self.common)
        self.assertEqual(os.listdir(self.outside / "lanes"), [])

    def test_valid_json_behind_a_symlinked_ancestor_is_not_accepted(self):
        (self.outside / "alpha").mkdir()
        (self.outside / "alpha/lane.json").write_text(json.dumps(self.record()))
        self.tp.mkdir()
        (self.tp / "lanes").symlink_to(self.outside)
        with self.assertRaises(ValueError):
            lanes.read_lane(self.common, "alpha")

    def test_a_symlinked_common_dir_argument_is_resolved_not_trusted_below(self):
        link = Path(self.tmp.name) / "common-link"
        link.symlink_to(self.common)
        self.write()
        self.assertEqual(lanes.read_lane(str(link), "alpha")["name"], "alpha")  # the anchor is its realpath

    def test_a_directory_that_is_not_git_is_not_an_anchor(self):
        with self.assertRaises(ValueError):
            lanes.lane_names(self.outside)
        with self.assertRaises(ValueError):
            with lanes.lane_lock(self.outside):
                pass
        self.assertEqual(os.listdir(self.outside), [])

    def test_a_directory_holding_only_a_fake_head_is_not_an_anchor_for_any_entry_point(self):
        """Review round 2: a file named HEAD is not git's word. Nothing may be read, listed, locked or created there."""
        fake = Path(self.tmp.name) / "fake"
        fake.mkdir()
        (fake / "HEAD").write_text("not a git HEAD\n")
        record = self.record(git_common_dir=str(fake))
        calls = {
            "lane_names": lambda: lanes.lane_names(fake),
            "read_lane": lambda: lanes.read_lane(fake, "alpha"),
            "lane_lock": lambda: lanes.lane_lock(fake).__enter__(),
            "write_lane": lambda: lanes.write_lane(fake, record),
            "precheck": lambda: lanes.precheck_lane_slot(fake, lanes.load_repo_config(self.repo), "beta"),
            "active_lane_count": lambda: lanes.active_lane_count(fake),
        }
        for what, call in calls.items():
            with self.subTest(what=what):
                with self.assertRaises(ValueError):
                    call()
        self.assertEqual(os.listdir(fake), ["HEAD"])

    def test_a_fake_head_inside_a_real_repository_and_a_worktree_entry_are_not_anchors(self):
        inner = Path(self.common) / "not-the-common-dir"
        inner.mkdir()
        (inner / "HEAD").write_text("ref: refs/heads/x\n")
        entry = next((Path(self.common) / "worktrees").iterdir())  # a real git directory, but a linked worktree's
        for place in (inner, entry, self.repo):
            with self.subTest(place=str(place)):
                with self.assertRaises(ValueError):
                    lanes.lane_names(place)
                with self.assertRaises(ValueError):
                    with lanes.lane_lock(place):
                        pass
        self.assertEqual(os.listdir(inner), ["HEAD"])
        self.assertFalse((entry / "tmux-pi").exists())
        self.assertFalse((self.repo / "tmux-pi").exists())

    def test_a_record_swapped_for_a_link_after_the_check_is_not_followed(self):
        """The file is opened with no-follow relative to the checked directory, so a link put in its place refuses."""
        self.write()
        path = self.tp / "lanes/alpha/lane.json"
        outside = self.outside / "x.json"
        outside.write_text(json.dumps(self.record()))
        real_lstat = os.lstat
        calls = []

        def swapping(name, *args, **kwargs):
            result = real_lstat(name, *args, **kwargs)
            if name == "lane.json" and not calls:
                calls.append(1)
                path.unlink()
                path.symlink_to(outside)
            return result
        from unittest import mock
        with mock.patch.object(lanes.os, "lstat", swapping):
            with self.assertRaises(ValueError):
                lanes.read_lane(self.common, "alpha")
        self.assertEqual(calls, [1])


class LockOwnerTests(RepoFixture):
    """Finding 3: the lock belongs to the thread that took it."""

    def test_another_thread_cannot_write_while_one_holds_the_lock(self):
        outcome = []
        with lanes.lane_lock(self.common):
            thread = threading.Thread(target=lambda: outcome.append(self._write_error()))
            thread.start()
            thread.join(5)
            self.assertEqual(len(outcome), 1)
        self.assertIn("lock", outcome[0])
        self.assertEqual(lanes.lane_names(self.common), [])

    def _write_error(self):
        try:
            lanes.write_lane(self.common, self.record())
        except ValueError as error:
            return str(error)
        return "WROTE"

    def test_a_second_thread_waits_for_the_lock_and_then_writes(self):
        entered, release, done = threading.Event(), threading.Event(), threading.Event()

        def second():
            with lanes.lane_lock(self.common):
                entered.set()
                lanes.write_lane(self.common, self.record())
            done.set()
        with lanes.lane_lock(self.common):
            thread = threading.Thread(target=second)
            thread.start()
            self.assertFalse(entered.wait(0.5), "a second thread got the lock while it was held")
            self.assertEqual(lanes.lane_names(self.common), [])
        self.assertTrue(done.wait(10))
        thread.join(5)
        self.assertEqual(lanes.lane_names(self.common), ["alpha"])

    def test_the_same_thread_still_cannot_take_it_twice(self):
        with lanes.lane_lock(self.common):
            with self.assertRaises(ValueError):
                with lanes.lane_lock(self.common):
                    pass
        with lanes.lane_lock(self.common):  # released after the refusal
            pass

    def test_a_forked_child_does_not_own_the_parents_lock(self):
        with lanes.lane_lock(self.common):
            pid = os.fork()
            if pid == 0:
                code = 1
                try:
                    lanes.write_lane(self.common, self.record())
                except ValueError:
                    code = 0
                os._exit(code)
            _, status = os.waitpid(pid, 0)
        self.assertEqual(os.WEXITSTATUS(status), 0, "the forked child wrote with the parent's lock")
        self.assertEqual(lanes.lane_names(self.common), [])


class RealityTests(RepoFixture):
    def refuses(self, record, text):
        with self.assertRaises(ValueError) as caught:
            lanes.lane_reality(record)
        self.assertIn(text, str(caught.exception))
        self.assertTrue(str(caught.exception).startswith("lane alpha:"))

    def test_a_matching_record_passes_and_returns_the_tip(self):
        tip = self.commit("two")
        self.assertEqual(lanes.lane_reality(self.record(closed=[self.entry("L1", tip)])), {"tip": tip})
        self.assertEqual(lanes.lane_reality(self.record())["tip"], tip)

    def test_checks_do_not_change_the_repository(self):
        before = (git(self.repo, "rev-parse", "HEAD"), git(self.repo, "status", "--porcelain"), git(self.repo, "branch", "--list"))
        lanes.lane_reality(self.record())
        self.assertEqual(before, (git(self.repo, "rev-parse", "HEAD"), git(self.repo, "status", "--porcelain"), git(self.repo, "branch", "--list")))

    def test_branch_switched_or_detached_in_the_worktree(self):
        git(self.wt, "checkout", "-q", "-b", "other")
        self.refuses(self.record(), "is on other, expected work/orch-alpha")
        git(self.wt, "checkout", "-q", "--detach")
        self.refuses(self.record(), "detached HEAD")

    def test_worktree_removed_by_hand_or_not_registered(self):
        record = self.record()
        git(self.repo, "worktree", "remove", "--force", str(self.wt))
        self.refuses(record, "does not exist")
        self.wt.mkdir()
        self.refuses(record, "not registered")

    def test_wrong_repository(self):
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        git(other, "init", "-q")
        self.refuses(dict(self.record(), repo=os.path.realpath(other)), "not the recorded one")
        self.refuses(dict(self.record(), repo=str(Path(os.path.realpath(self.tmp.name)) / "missing")), "not the recorded one")

    def test_base_and_closed_commits_are_checked_against_history(self):
        tip = self.commit("two")
        self.refuses(self.record(base={"ref": "x", "commit": SHA}), "base")
        self.refuses(self.record(closed=[dict(self.entry("L1", tip), commit=SHA)]), "not on branch")
        self.refuses(self.record(closed=[dict(self.entry("L1", tip), tree=SHA)]), "records tree")
        tip3 = self.commit("three")
        out_of_order = [self.entry("L2", tip3), self.entry("L1", tip)]
        self.refuses(self.record(closed=out_of_order), "does not follow")
        self.assertEqual(lanes.lane_reality(self.record(closed=[self.entry("L1", tip), self.entry("L2", tip3)]))["tip"], tip3)

    def test_a_closed_commit_that_left_the_branch_is_refused(self):
        tip = self.commit("two")
        record = self.record(closed=[self.entry("L1", tip)])
        git(self.wt, "reset", "-q", "--hard", self.base_commit)
        self.refuses(record, "not on branch")

    def clone_with_branch(self, commit=False):
        clone = Path(self.tmp.name) / "clone"
        git(self.tmp.name, "clone", "-q", str(self.repo), str(clone))
        git(clone, "checkout", "-q", "-b", "work/orch-alpha")
        if commit:
            (clone / "z").write_text("z")
            git(clone, "add", "z")
            git(clone, "commit", "-qm", "diverged")
        return clone

    def test_a_worktree_whose_git_link_was_replaced_by_another_repository_is_refused(self):
        clone = self.clone_with_branch(commit=True)
        record = self.record()
        lanes.lane_reality(record)
        (self.wt / ".git").unlink()
        (self.wt / ".git").symlink_to(clone / ".git")
        self.assertIn(str(self.wt), lanes.registered_worktrees(self.common))  # still listed by path
        self.assertNotEqual(lanes.git_common_dir(self.wt), self.common)
        self.refuses(record, "not the worktree git registered")

    def test_a_git_file_pointing_at_another_repository_is_refused(self):
        clone = self.clone_with_branch()
        (self.wt / ".git").unlink()
        (self.wt / ".git").write_text(f"gitdir: {clone}/.git\n")
        self.refuses(self.record(), "not the worktree git registered")

    def test_a_git_file_pointing_at_another_entry_of_the_same_repository_is_refused(self):
        other = Path(self.tmp.name) / "repo-wt-beta"
        git(self.repo, "worktree", "add", "-q", "-b", "work/orch-beta", str(other), "HEAD")
        (self.wt / ".git").write_text((other / ".git").read_text())
        self.refuses(self.record(), "not the worktree git registered")

    def test_a_missing_branch(self):
        record = self.record(branch="work/orch-gone")
        self.refuses(record, "expected work/orch-gone")


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(os.path.realpath(self.tmp.name))

    def machine(self, value, raw=None):
        path = self.dir / "orchestration.json"
        path.write_text(raw if raw is not None else json.dumps(value))
        return lanes.load_machine_config(path)

    def test_machine_config(self):
        self.assertEqual(self.machine({"lanes": {"max_active_lanes": 3}, "max_active_workers": 4}), {"max_active_lanes": 3, "defaulted": []})

    def test_machine_config_absent_block_or_key_takes_the_default_and_says_so(self):
        for value in ({"max_active_workers": 4}, {"lanes": {}}, {"lanes": {"other": 1}}):
            with self.subTest(value=value):
                self.assertEqual(self.machine(value), {"max_active_lanes": 2, "defaulted": ["max_active_lanes"]})

    def test_machine_config_present_but_invalid_or_unreadable_refuses(self):
        for value, text in (({"lanes": []}, "must be an object"), ({"lanes": None}, "must be an object"),
                            ({"lanes": {"max_active_lanes": 0}}, "positive integer"), ({"lanes": {"max_active_lanes": -1}}, "positive integer"),
                            ({"lanes": {"max_active_lanes": True}}, "positive integer"), ({"lanes": {"max_active_lanes": 2.0}}, "positive integer"),
                            ({"lanes": {"max_active_lanes": "2"}}, "positive integer"), ({"lanes": {"max_active_lanes": None}}, "positive integer")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    self.machine(value)
                self.assertIn(text, str(caught.exception))
        for raw in ("{", "[]"):
            with self.assertRaises(ValueError) as caught:
                self.machine(None, raw)
            self.assertIn("orchestration.json", str(caught.exception))
        with self.assertRaises(ValueError):
            lanes.load_machine_config(self.dir / "absent.json")  # the machine file itself must exist

    def test_the_shipped_config_sets_max_active_lanes(self):
        self.assertEqual(lanes.load_machine_config(), {"max_active_lanes": 5, "defaulted": []})

    def repo_config(self, value=None, raw=None, repo=None):
        repo = repo or self.dir
        (repo / ".tmux-pi").mkdir(exist_ok=True)
        (repo / ".tmux-pi/lanes.json").write_text(raw if raw is not None else json.dumps(value))
        return lanes.load_repo_config(repo)

    @staticmethod
    def full(**over):
        value = {"branch_prefix": "work/orch-", "worktree_pattern": "{repo}-wt-{name}", "integration_branch": "work/orch-integrate",
                 "integration_lane": "integrate"}
        value.update(over)
        return value

    DEFAULTS = {"branch_prefix": "lane/", "worktree_pattern": "{repo}-wt-{name}", "integration_branch": "lane/integrate",
                "integration_lane": "integrate"}

    def test_repo_config(self):
        clean = self.repo_config(self.full(setup=["x"], other=1))
        self.assertEqual(clean, dict(self.full(), defaulted=[]))
        self.assertEqual(lanes.lane_branch(clean, "alpha"), "work/orch-alpha")
        self.assertEqual(lanes.lane_worktree(clean, "/r/repo", "alpha"), "/r/repo-wt-alpha")
        full = self.repo_config(self.full(owned=["docs/tasks.md", "CHANGELOG.md"], tests={"full": {"argv": ["python3", "-m", "unittest"], "timeout_s": 900}}))
        self.assertEqual(full["owned"], ["CHANGELOG.md", "docs/tasks.md"])
        self.assertEqual(full["tests"]["full"]["timeout_s"], 900)

    def test_no_repo_file_takes_every_default_and_says_so(self):
        clean = lanes.load_repo_config(self.dir)  # no .tmux-pi at all
        self.assertEqual(clean, dict(self.DEFAULTS, defaulted=sorted(self.DEFAULTS)))
        self.assertEqual(lanes.defaulted_values(clean, {"defaulted": ["max_active_lanes"]}), sorted([*self.DEFAULTS, "max_active_lanes"]))

    def test_every_absent_key_takes_its_own_default_only(self):
        for key in self.DEFAULTS:
            value = self.full()
            del value[key]
            with self.subTest(key=key):
                clean = self.repo_config(value)
                self.assertEqual(clean[key], self.DEFAULTS[key])
                self.assertEqual(clean["defaulted"], [key])
                for other in self.DEFAULTS:
                    if other != key:
                        self.assertEqual(clean[other], self.full()[other])
        self.assertEqual(self.repo_config({})["defaulted"], sorted(self.DEFAULTS))

    def test_an_explicit_value_equal_to_the_default_is_not_defaulted(self):
        self.assertEqual(self.repo_config(dict(self.DEFAULTS))["defaulted"], [])

    def test_present_but_invalid_never_takes_the_default(self):
        """Missing-vs-invalid: each key absent defaults (above); the same key present and bad refuses naming key and file."""
        bad = {"branch_prefix": [5, None, " ", "a..b/", "-x"], "worktree_pattern": ["", None, "{repo}-wt", "{repo}-{name}-{name}", "wt-{name}", "{repo}-{x}-{name}"],
               "integration_branch": ["", None, "a b", 3], "integration_lane": ["", None, "Int", 4]}
        for key, values in bad.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    with self.assertRaises(ValueError) as caught:
                        self.repo_config(self.full(**{key: value}))
                    self.assertIn(key, str(caught.exception))
                    self.assertIn("lanes.json", str(caught.exception))

    def test_an_empty_branch_prefix_is_accepted_and_no_other_key_may_be_empty(self):
        clean = self.repo_config(self.full(branch_prefix=""))
        self.assertEqual(clean["branch_prefix"], "")
        self.assertNotIn("branch_prefix", clean["defaulted"])
        self.assertEqual(lanes.lane_branch(clean, "dmp-23-example"), "dmp-23-example")

    def test_bad_optional_repo_values_refuse(self):
        for over in ({"owned": "x"}, {"owned": ["/abs"]}, {"owned": ["a", "a"]}, {"tests": {}}, {"tests": {"full": {"argv": []}}},
                     {"tests": {"full": {"argv": ["x"]}}}, {"tests": {"full": {"argv": ["x"], "timeout_s": 0}}},
                     {"tests": {"full": {"argv": ["x"], "timeout_s": True}}}, {"tests": {"Full": {"argv": ["x"], "timeout_s": 1}}},
                     {"tests": {"full": {"argv": [1], "timeout_s": 1}}}):
            with self.subTest(over=over):
                with self.assertRaises(ValueError):
                    self.repo_config(self.full(**over))

    def test_an_unreadable_repo_file_refuses_and_is_not_treated_as_absent(self):
        for raw in ("{", "[]", ""):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as caught:
                    self.repo_config(raw=raw)
                self.assertIn("lanes.json", str(caught.exception))
        path = self.dir / ".tmux-pi/lanes.json"
        path.unlink()
        path.mkdir()  # exists, but is a directory
        with self.assertRaises(ValueError):
            lanes.load_repo_config(self.dir)
        path.rmdir()
        path.symlink_to(self.dir / "nowhere")  # a dangling link exists too
        with self.assertRaises(ValueError):
            lanes.load_repo_config(self.dir)
        path.unlink()
        (self.dir / ".tmux-pi").rmdir()
        (self.dir / ".tmux-pi").write_text("x")  # `.tmux-pi` is a file: not absent
        with self.assertRaises(ValueError):
            lanes.load_repo_config(self.dir)

    def test_a_symlinked_repo_config_is_refused(self):
        self.repo_config(self.full())
        path = self.dir / ".tmux-pi/lanes.json"
        real = path.with_name("real.json")
        path.rename(real)
        path.symlink_to(real)
        with self.assertRaises(ValueError):
            lanes.load_repo_config(self.dir)

    def test_the_integration_lane_name_is_reserved(self):
        cfg = dict(self.DEFAULTS, defaulted=[])
        with self.assertRaises(ValueError) as caught:
            lanes.lane_branch(cfg, "integrate")  # would otherwise render the integration branch lane/integrate
        self.assertIn("reserved", str(caught.exception))
        with self.assertRaises(ValueError):
            lanes.lane_worktree(cfg, "/r/repo", "integrate")
        self.assertEqual(lanes.lane_branch(cfg, "integrate", integration=True), "lane/integrate")
        self.assertEqual(lanes.lane_worktree(cfg, "/r/repo", "integrate", integration=True), "/r/repo-wt-integrate")
        with self.assertRaises(ValueError):
            lanes.lane_branch(cfg, "alpha", integration=True)
        self.assertEqual(lanes.lane_branch(cfg, "alpha"), "lane/alpha")
        with self.assertRaises(ValueError):  # a work lane whose branch would be the integration branch
            lanes.lane_branch(dict(cfg, integration_branch="lane/alpha"), "alpha")

    def test_worktree_names_follow_the_repository_folder_not_a_path(self):
        cfg = dict(self.DEFAULTS, defaulted=[])
        a, b = self.dir / "one/proj", self.dir / "two/proj"
        a.mkdir(parents=True)
        b.mkdir(parents=True)
        self.assertEqual(lanes.lane_worktree(cfg, str(a), "x"), str(self.dir / "one/proj-wt-x"))
        self.assertEqual(lanes.lane_worktree(cfg, str(b), "x"), str(self.dir / "two/proj-wt-x"))
        absolute = dict(cfg, worktree_pattern=str(self.dir / "wts/{repo}-{name}"))
        self.assertEqual(lanes.lane_worktree(absolute, str(a), "x"), lanes.lane_worktree(absolute, str(b), "x"))  # same: precheck catches it

    def test_worktree_location_refusals(self):
        cfg = dict(self.DEFAULTS, defaulted=[])
        repo = self.dir / "proj"
        repo.mkdir()
        (self.dir / "alias").symlink_to(self.dir)
        for pattern in ("{repo}/wt-{name}", str(repo / "wt-{name}"), str(self.dir / "alias/wt-{name}"), str(self.dir / "a/../wt-{name}")):
            with self.subTest(pattern=pattern):
                with self.assertRaises(ValueError):
                    lanes.lane_worktree(dict(cfg, worktree_pattern=pattern), str(repo), "x")
        with self.assertRaises(ValueError):
            lanes.lane_worktree(cfg, str(self.dir / "alias/proj"), "x")  # repo itself must be canonical


class PrecheckTests(RepoFixture):
    def setUp(self):
        super().setUp()
        self.cfg = dict(branch_prefix="lane/", worktree_pattern="{repo}-wt-{name}", integration_branch="lane/integrate",
                        integration_lane="integrate", defaulted=[])

    def refuses(self, name, text, **kwargs):
        with self.assertRaises(ValueError) as caught:
            lanes.precheck_lane_slot(self.common, self.cfg, name, **kwargs)
        self.assertIn(text, str(caught.exception))

    def test_a_free_slot_passes_and_creates_nothing(self):
        before = (git(self.repo, "branch", "--list"), os.listdir(self.tmp.name))
        self.assertEqual(lanes.precheck_lane_slot(self.common, self.cfg, "beta"),
                         {"branch": "lane/beta", "worktree": str(self.repo) + "-wt-beta"})
        self.assertEqual(before, (git(self.repo, "branch", "--list"), os.listdir(self.tmp.name)))
        self.assertEqual(lanes.precheck_lane_slot(self.common, self.cfg, "integrate", integration=True)["branch"], "lane/integrate")

    def test_an_existing_branch_is_refused_not_adopted(self):
        git(self.repo, "branch", "lane/beta")
        self.refuses("beta", "already exists")
        git(self.repo, "branch", "lane/delta/inner")  # a branch below the wanted one
        self.refuses("delta", "lane/delta/inner")

    def test_a_branch_that_conflicts_by_path_is_refused(self):
        git(self.repo, "branch", "lane2")
        cfg = dict(self.cfg, branch_prefix="lane2/")
        with self.assertRaises(ValueError) as caught:
            lanes.precheck_lane_slot(self.common, cfg, "beta")
        self.assertIn("lane2", str(caught.exception))

    def test_an_existing_path_or_registered_worktree_is_refused(self):
        Path(str(self.repo) + "-wt-beta").mkdir()
        self.refuses("beta", "already exists; it is not adopted")
        git(self.repo, "worktree", "add", "-q", "-b", "elsewhere", str(self.repo) + "-wt-gamma", "HEAD")
        self.refuses("gamma", "already exists; it is not adopted")
        os.rename(str(self.repo) + "-wt-gamma", str(self.repo) + "-wt-moved")  # git still lists the old path
        self.refuses("gamma", "git already lists")

    def test_an_existing_record_or_its_branch_blocks_the_slot(self):
        with lanes.lane_lock(self.common):
            lanes.write_lane(self.common, self.record(name="beta", branch="lane/beta", worktree=str(self.repo) + "-wt-beta"))
        self.refuses("beta", "already has a record")
        with lanes.lane_lock(self.common):  # a record for another name holding this lane's branch
            lanes.write_lane(self.common, self.record(name="other", branch="lane/gamma", worktree=str(self.repo) + "-wt-other"))
        self.refuses("gamma", "already uses")

    def test_an_unreadable_record_refuses_the_precheck(self):
        bad = Path(self.common) / "tmux-pi/lanes/broken"
        bad.mkdir(parents=True)
        (bad / "lane.json").write_text("{")
        self.refuses("beta", "unreadable record")

    def test_the_reserved_name_and_a_worktree_inside_the_repository_refuse(self):
        self.refuses("integrate", "reserved")
        inside = dict(self.cfg, worktree_pattern=str(self.repo / "wt-{name}"))
        with self.assertRaises(ValueError):
            lanes.precheck_lane_slot(self.common, inside, "beta")

    def test_two_repositories_with_the_same_folder_name_get_separate_slots(self):
        one, two = Path(self.tmp.name) / "p1/proj", Path(self.tmp.name) / "p2/proj"
        for repo in (one, two):
            repo.mkdir(parents=True)
            git(repo, "init", "-q", "-b", "main")
            (repo / "f").write_text("1")
            git(repo, "add", "f")
            git(repo, "commit", "-qm", "one")
        a = lanes.precheck_lane_slot(lanes.git_common_dir(one), self.cfg, "x")
        b = lanes.precheck_lane_slot(lanes.git_common_dir(two), self.cfg, "x")
        self.assertNotEqual(a["worktree"], b["worktree"])
        git(one, "worktree", "add", "-q", "-b", a["branch"], a["worktree"], "HEAD")
        self.assertEqual(lanes.precheck_lane_slot(lanes.git_common_dir(two), self.cfg, "x"), b)  # unaffected by the other repo
        with self.assertRaises(ValueError):
            lanes.precheck_lane_slot(lanes.git_common_dir(one), self.cfg, "x")
        shared = dict(self.cfg, worktree_pattern=str(Path(os.path.realpath(self.tmp.name)) / "wts/{repo}-{name}"))
        first = lanes.precheck_lane_slot(lanes.git_common_dir(one), shared, "y")
        os.makedirs(first["worktree"])  # the other repository's worktree now sits at the shared path
        with self.assertRaises(ValueError) as caught:
            lanes.precheck_lane_slot(lanes.git_common_dir(two), shared, "y")
        self.assertIn("already exists", str(caught.exception))

    def test_the_active_limit_counts_records_of_every_session(self):
        self.assertEqual(lanes.active_lane_count(self.common), 0)
        for name, state in (("one", "open"), ("two", "integrating"), ("three", "abandoned")):
            with lanes.lane_lock(self.common):
                lanes.write_lane(self.common, self.record(name=name, state=state, branch=f"lane/{name}", worktree=f"{self.repo}-wt-{name}"))
        self.assertEqual(lanes.active_lane_count(self.common), 2)
        self.assertEqual(lanes.check_lane_capacity(self.common, 3), 2)
        with self.assertRaises(ValueError):
            lanes.check_lane_capacity(self.common, 2)
        bad = Path(self.common) / "tmux-pi/lanes/broken"
        bad.mkdir()
        (bad / "lane.json").write_text("{")
        with self.assertRaises(ValueError):
            lanes.active_lane_count(self.common)


if __name__ == "__main__":
    unittest.main()
