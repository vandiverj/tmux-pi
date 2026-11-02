"""lib/trial_shadow.py, what a run does with branches, refs and a tree the worker left in an odd state (review 8, the worker is not hostile but makes mistakes):

F2  the runner deletes no branch. A branch HEAD of the worker's tree is on (one it made, renamed, or somebody else made meanwhile)
    is detached from and kept where it is; moving HEAD to the base never moves it. Every branch the worker made is listed in run.json.
    The refs the runner does make are recorded (name and commit) and removed only by that record.

Split from test_trial_shadow_worker_files.py so that each file is well under the 300 s bound. Fixtures: trial_shadow_case.py."""
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trial_shadow_case import *  # noqa: E402,F401,F403
import trial_session  # noqa: E402,F401

MALFORMED = ("empty", "truncated", "binary", "array", "deep")
LIMIT = 16384


class TreeCase(ShadowCase):
    def worktree_run(self, run_class=ts.Run, run=None):
        rdir, _ = self.prepare(run=run)
        run = run_class(rdir, self.env)
        run.make_worktree()
        self.addCleanup(lambda: ts.cleanup(rdir, self.env))
        return run

    def heads(self):
        return set(self.refs("refs/heads").splitlines())

    def detached(self, run):
        return subprocess.run(["git", "-C", str(run.worktree), "symbolic-ref", "-q", "HEAD"], capture_output=True, text=True).returncode == 1

class BranchTests(TreeCase):
    """F2: a branch is never deleted or moved by the runner; the refs the runner makes are removed by its own record."""

    def test_a_branch_the_worker_made_is_kept_where_it_was_and_reported(self):
        run = self.worktree_run()
        before = self.heads()
        git(run.worktree, "checkout", "-q", "-b", "worker-branch")
        self.edit(run)
        tip = git(run.worktree, "rev-parse", "HEAD")
        run.collect_work()
        self.assertEqual(self.heads(), before | {"refs/heads/worker-branch " + tip}, "the branch is where the worker left it; nothing else changed")
        self.assertEqual(run.info["branch_left"], {"ref": "refs/heads/worker-branch", "commit": tip})
        self.assertEqual(ts.read_run(run.rdir)["branch_left"], run.info["branch_left"], "reported in run.json")
        self.assertTrue(self.detached(run), "the tree is left detached, not on a branch")
        self.assertEqual(git(run.worktree, "rev-parse", "HEAD"), self.base, "HEAD was moved to the base, on a detached HEAD")
        self.assertEqual(run.diff[0], 2)
        self.assertEqual(ts.cleanup(run.rdir, self.env), [], "the cleanup deletes no branch and has nothing to report")
        self.assertEqual(self.heads(), before | {"refs/heads/worker-branch " + tip})
        self.assertEqual(self.refs("refs/tmux-pi"), "")

    def test_every_branch_the_worker_made_is_listed_in_run_json_whether_or_not_head_is_on_it(self):
        """Design (f): worker-made branches are left in place and listed. `branch_left` names the one HEAD was on; a branch made with
        `git branch` and left alone is not HEAD's, so `new_branches` lists every head that was not there when the tree was made."""
        run = self.worktree_run()
        before = self.heads()
        git(run.worktree, "branch", "scratch", self.base)
        git(run.worktree, "checkout", "-q", "-b", "kept")
        self.edit(run)
        tip = git(run.worktree, "rev-parse", "HEAD")
        run.collect_work()
        want = [{"ref": "refs/heads/kept", "commit": tip}, {"ref": "refs/heads/scratch", "commit": self.base}]
        self.assertEqual(run.info["new_branches"], want)
        self.assertEqual(ts.read_run(run.rdir)["new_branches"], want, "reported in run.json")
        self.assertEqual(self.heads(), before | {"refs/heads/kept " + tip, "refs/heads/scratch " + self.base}, "both are left where they are")
        self.assertEqual(ts.cleanup(run.rdir, self.env), [])
        self.assertEqual(self.heads(), before | {"refs/heads/kept " + tip, "refs/heads/scratch " + self.base})

    def test_a_branch_the_worker_made_is_listed_even_when_the_collect_stops_at_a_branch_that_was_there_before(self):
        """Review F3: the listing does not depend on the collect getting as far as `leave_branch`. The worker made a branch and then
        checked out one that predates the run: the collect refuses (and moves nothing), and the made branch is still named."""
        git(self.repo, "branch", "old-branch", self.base)
        run = self.worktree_run()
        git(run.worktree, "branch", "worker-scratch", self.base)
        git(run.worktree, "checkout", "-q", "old-branch")
        self.edit(run)  # the worker's commit moves old-branch: the worker did that, not the runner
        old_tip = git(run.worktree, "rev-parse", "HEAD")
        before = self.heads()
        with self.assertRaises(ts.ShadowError) as caught:
            run.collect_work()
        self.assertIn("old-branch", str(caught.exception))
        want = [{"ref": "refs/heads/worker-scratch", "commit": self.base}]
        self.assertEqual(run.info["new_branches"], want)
        self.assertEqual(ts.read_run(run.rdir)["new_branches"], want, "reported in run.json")
        self.assertEqual(self.heads(), before, "only the listing happened: nothing moved or deleted")
        self.assertEqual(git(self.repo, "rev-parse", "old-branch"), old_tip, "the branch that predates the run is where the worker left it")

    def other_repo(self, path):
        """A repository that is not the run's, with a branch of its own, at `path`."""
        path.mkdir()
        git(path, "init", "-q")
        (path / "f").write_text("other\n")
        git(path, "add", "f")
        git(path, "-c", "user.name=o", "-c", "user.email=o@x", "commit", "-q", "-m", "other")
        git(path, "branch", "foreign-branch")
        return path

    def test_review_f3_a_project_moved_aside_with_another_repository_in_its_place_has_its_branches_not_listed(self):
        """Review round 2 F3: the ref query runs only against the project the run held when it took `heads_before` (the root and its
        git dir, by identity). Another repository at the old path is not listed; that the branches could not be listed is recorded."""
        run = self.worktree_run()
        git(run.worktree, "branch", "worker-scratch", self.base)
        aside = self.repo.with_name(self.repo.name + "-aside")
        os.rename(str(self.repo), str(aside))
        self.addCleanup(lambda: (shutil.rmtree(str(self.repo), ignore_errors=True), os.rename(str(aside), str(self.repo))))
        self.other_repo(self.repo)
        with self.assertRaises(ts.ShadowError):
            run.collect_work()  # the tree is not at its path any more: the collect stops, and the listing came first
        info = run.owner.read()  # through the held run directory, which moved with the project
        self.assertNotIn("new_branches", info, "no head of the other repository is listed")
        self.assertIn("project", info["new_branches_error"])
        self.assertNotIn("foreign-branch", json.dumps(info))

    def test_review_f3_a_git_directory_swapped_under_the_project_has_its_branches_not_listed(self):
        run = self.worktree_run()
        git(run.worktree, "branch", "worker-scratch", self.base)
        other = self.other_repo(self.root / "another-repo")
        held = self.repo / ".git"
        os.rename(str(held), str(self.root / "git-aside"))
        self.addCleanup(lambda: (shutil.rmtree(str(held), ignore_errors=True), os.rename(str(self.root / "git-aside"), str(held))))
        os.rename(str(other / ".git"), str(held))
        run.list_new_branches()
        self.assertNotIn("new_branches", run.info)
        self.assertIn("git directory", run.info["new_branches_error"])
        self.assertNotIn("foreign-branch", json.dumps(ts.read_run(run.rdir)))

    def test_a_branch_the_worker_made_is_listed_when_the_tree_is_gone_at_the_collect(self):
        run = self.worktree_run()
        git(run.worktree, "branch", "worker-scratch", self.base)
        shutil.rmtree(str(run.worktree))
        with self.assertRaises(ts.ShadowError):
            run.collect_work()
        self.assertEqual(ts.read_run(run.rdir)["new_branches"], [{"ref": "refs/heads/worker-scratch", "commit": self.base}])

    def test_a_branch_the_worker_made_is_listed_when_the_run_stops_before_it_collects(self):
        class CannotEnd(StubSpawnRun):
            def end_worker(self, outcome):
                raise ts.ShadowError("the worker could not be ended: refused")
        rdir, _ = self.prepare()

        def poll(n, run):
            git(run.worktree, "branch", "side-work", self.base)
            self.settle_worker(run)
        _, done = self.run_stub(rdir, poll, run_class=CannotEnd)
        self.assertEqual(done["outcome"], "error", done)
        self.assertEqual(done["new_branches"], [{"ref": "refs/heads/side-work", "commit": self.base}])
        self.assertEqual(self.refs("refs/heads/side-work"), "refs/heads/side-work " + self.base, "left where it is")

    def test_a_run_that_made_no_branch_lists_none_when_it_stops_before_it_collects(self):
        class CannotEnd(StubSpawnRun):
            def end_worker(self, outcome):
                raise ts.ShadowError("the worker could not be ended: refused")
        rdir, _ = self.prepare()
        _, done = self.run_stub(rdir, lambda n, run: self.settle_worker(run), run_class=CannotEnd)
        self.assertEqual(done["outcome"], "error", done)
        self.assertNotIn("new_branches", done)

    def test_a_run_that_never_made_its_tree_lists_no_branches(self):
        """`heads_before` is taken when the tree is made; a run that stopped earlier has none, and must not name every branch as new."""
        rdir, _ = self.prepare()

        class NoTree(StubSpawnRun):
            def make_worktree(self):
                raise ts.ShadowError("refused before the tree")
        _, done = self.run_stub(rdir, run_class=NoTree)
        self.assertEqual(done["outcome"], "error", done)
        self.assertNotIn("new_branches", done)

    def test_a_branch_that_was_there_before_the_run_is_not_listed_as_new(self):
        git(self.repo, "branch", "old-branch", self.base)
        run = self.worktree_run()
        self.edit(run)
        run.collect_work()
        self.assertNotIn("new_branches", run.info)
        self.assertNotIn("new_branches", ts.read_run(run.rdir))

    def test_the_branches_of_a_finished_run_are_in_the_report_the_run_returns(self):
        rdir, info = self.prepare()

        def poll(n, run):
            if n == 1:
                git(run.worktree, "branch", "side-work", self.base)
                self.edit(run)
                self.settle_worker(run)
            elif n == 2:
                for p in (run.state / "tasks").iterdir():
                    if (p / "review-of").exists():
                        self.settle(p, "waiting", "RESULT_STATUS: complete\nAPPROVED\n")
        _, done = self.run_stub(rdir, poll)
        self.assertEqual(done["new_branches"], [{"ref": "refs/heads/side-work", "commit": self.base}])
        self.assertEqual(self.refs("refs/heads/side-work"), "refs/heads/side-work " + self.base)

    def test_a_branch_somebody_else_made_after_the_run_began_is_never_deleted_or_moved(self):
        # heads_before was taken when the tree was made: this branch is not in it, and the worker's HEAD is on it
        run = self.worktree_run()
        git(self.repo, "branch", "made-meanwhile", self.base)
        git(run.worktree, "checkout", "-q", "made-meanwhile")
        self.edit(run)
        tip = git(run.worktree, "rev-parse", "HEAD")
        self.assertNotEqual(tip, self.base)
        run.collect_work()
        self.assertEqual(git(self.repo, "rev-parse", "made-meanwhile"), tip, "kept, and not moved back to the base")
        self.assertEqual(run.info["branch_left"], {"ref": "refs/heads/made-meanwhile", "commit": tip})
        self.assertEqual(git(run.worktree, "rev-parse", "HEAD"), self.base)

    def test_a_branch_the_worker_renamed_is_kept_under_its_new_name(self):
        run = self.worktree_run()
        git(run.worktree, "checkout", "-q", "-b", "first-name")
        self.edit(run)
        git(run.worktree, "branch", "-m", "second-name")
        tip = git(run.worktree, "rev-parse", "HEAD")
        run.collect_work()
        self.assertEqual(git(self.repo, "rev-parse", "second-name"), tip)
        self.assertEqual(run.info["branch_left"]["ref"], "refs/heads/second-name")
        self.assertNotIn("refs/heads/first-name", self.refs("refs/heads"))

    def test_a_worker_on_a_branch_that_was_there_before_the_run_is_not_touched_and_the_collect_stops(self):
        run = self.worktree_run()
        git(run.worktree, "checkout", "-q", "-b", "tmp-local")
        git(run.worktree, "checkout", "-q", "--detach")
        git(self.repo, "branch", "other-lane", self.base)  # exists before the tree is made in the next run
        run2 = self.worktree_run(run="second-run")  # a run id is made from the second, so name it
        git(run2.worktree, "checkout", "-q", "other-lane")
        self.edit(run2)
        tip = git(run2.worktree, "rev-parse", "HEAD")
        with self.assertRaises(ts.ShadowError) as caught:
            run2.collect_work()
        self.assertIn("other-lane", str(caught.exception))
        self.assertEqual(git(self.repo, "rev-parse", "other-lane"), tip, "neither reset nor deleted")

    def test_a_worker_that_left_head_detached_has_no_branch_to_report(self):
        run = self.worktree_run()
        git(run.worktree, "checkout", "-q", "--detach")
        self.edit(run)
        run.collect_work()
        self.assertNotIn("branch_left", run.info)
        self.assertEqual(git(run.worktree, "rev-parse", "HEAD"), self.base)

    def test_head_that_cannot_be_detached_stops_the_collect_before_head_is_moved_to_the_base(self):
        class Stuck(ts.Run):
            def wt_git(self, *args, **kw):
                if args[:3] == ("update-ref", "--no-deref", "-m") and args[3].endswith("leave the branch"):
                    return subprocess.CompletedProcess(args, 1, "", "refused")
                return super().wt_git(*args, **kw)
        run = self.worktree_run(Stuck)
        git(run.worktree, "checkout", "-q", "-b", "worker-branch")
        self.edit(run)
        tip = git(run.worktree, "rev-parse", "HEAD")
        with self.assertRaises(ts.ShadowError) as caught:
            run.collect_work()
        self.assertIn("could not be detached", str(caught.exception))
        self.assertEqual(git(self.repo, "rev-parse", "worker-branch"), tip, "the branch was not moved to the base")
        self.assertFalse((run.rdir / "final.patch").exists())

    def test_head_that_cannot_be_read_stops_the_collect_before_head_is_moved_to_the_base(self):
        class Unreadable(ts.Run):
            def wt_git(self, *args, **kw):
                if args[:2] == ("symbolic-ref", "-q"):
                    return subprocess.CompletedProcess(args, 128, "", "fatal")
                return super().wt_git(*args, **kw)
        run = self.worktree_run(Unreadable)
        git(run.worktree, "checkout", "-q", "-b", "worker-branch")
        self.edit(run)
        tip = git(run.worktree, "rev-parse", "HEAD")
        with self.assertRaises(ts.ShadowError):
            run.collect_work()
        self.assertEqual(git(self.repo, "rev-parse", "worker-branch"), tip)

    # -- the refs the run makes
    def test_the_workers_last_commit_is_kept_under_a_recorded_ref_that_is_never_made_over_another(self):
        run = self.worktree_run()
        ref = "refs/tmux-pi/shadow/{}/head".format(run.run)
        git(self.repo, "update-ref", ref, self.base)  # somebody made it first
        self.edit(run)
        with self.assertRaises(ts.ShadowError):
            run.collect_work()
        self.assertEqual(git(self.repo, "rev-parse", ref), self.base, "not moved")
        self.assertEqual(git(run.worktree, "log", "--oneline", self.base + "..HEAD").count("\n") + 1, 1, "the worker's commit is still there: no reset ran")

    def test_the_ledger_names_the_refs_the_run_made_and_the_cleanup_removes_only_those_at_the_recorded_commit(self):
        run = self.worktree_run()
        self.edit(run)
        head = git(run.worktree, "rev-parse", "HEAD")
        run.collect_work()
        ref = "refs/tmux-pi/shadow/{}/head".format(run.run)
        record = json.loads(ts.owner_file(run.project, run.run).read_text())
        self.assertEqual(record["refs"], {ref: head})
        stranger = "refs/tmux-pi/shadow/{}/not-mine".format(run.run)
        git(self.repo, "update-ref", stranger, self.base)
        left = ts.cleanup(run.rdir, self.env)
        self.assertEqual(len(left), 1)
        self.assertIn("refs", left[0])
        self.assertIn(stranger, left[0])
        self.assertEqual(self.refs("refs/tmux-pi").split()[0], stranger, "the recorded ref went, the one the run did not make stayed")
        self.assertEqual(len(self.refs("refs/tmux-pi").splitlines()), 1)

    def test_a_ref_of_the_run_that_now_names_another_commit_is_kept(self):
        run = self.worktree_run()
        self.edit(run)
        run.collect_work()
        ref = "refs/tmux-pi/shadow/{}/head".format(run.run)
        git(self.repo, "update-ref", ref, self.base)
        left = ts.cleanup(run.rdir, self.env)
        self.assertTrue(any(ref in line for line in left), left)
        self.assertEqual(git(self.repo, "rev-parse", ref), self.base)

    def test_a_ledger_in_the_owner_record_that_is_not_a_map_of_this_runs_refs_is_read_as_no_refs(self):
        rdir, info = self.prepare()
        record = ts.owner_file(info["project"], info["run"])
        for ledger in ({"refs/heads/main": self.base}, {"refs/tmux-pi/shadow/{}/head".format(info["run"]): "--delete"}, ["x"], "x", 5):
            with self.subTest(ledger=ledger):
                value = json.loads(record.read_text())
                value["refs"] = ledger
                record.write_text(json.dumps(value))
                owner = ts.Owner(rdir)
                try:
                    self.assertIsNone(owner.problem)
                    self.assertEqual(owner.record["refs"], {})
                finally:
                    owner.close()
        self.assertEqual(ts.cleanup(rdir, self.env), [])
        self.assertIn("refs/heads/main " + git(self.repo, "rev-parse", "main"), self.refs("refs/heads"))


class AccidentTests(TreeCase):
    """What a worker that only makes mistakes can leave in its tree (found by the family sweep of review 8): the collect still ends
    with a patch or an error, never a hang, and the cleanup removes the tree and everything outside it stays."""

    def clean_up_and_check(self, run, outside=None):
        self.assertEqual(ts.cleanup(run.rdir, self.env), [])
        self.assertFalse(run.worktree.exists(), "the tree is gone")
        self.assertEqual(self.refs("refs/tmux-pi"), "")
        if outside is not None:
            self.assertTrue(outside.exists(), "nothing outside the tree was touched")

    def test_a_tree_on_an_orphan_branch_that_has_no_commit_yet_stops_the_collect_and_the_cleanup_still_works(self):
        run = self.worktree_run()
        git(run.worktree, "checkout", "-q", "--orphan", "fresh")
        with self.assertRaises(ts.ShadowError):
            run.collect_work()  # HEAD names a branch with no commit: it cannot be detached
        self.assertNotIn("fresh", self.refs("refs/heads"))
        self.clean_up_and_check(run)

    def test_a_socket_and_a_fifo_in_the_tree_do_not_stop_the_collect(self):
        run = self.worktree_run()
        self.edit(run)
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        here = os.getcwd()
        os.chdir(str(run.worktree))  # a socket path is limited to about 100 bytes: bind it by a relative name
        try:
            sock.bind("dev.sock")
        finally:
            os.chdir(here)
        os.mkfifo(str(run.worktree / "pipe"))
        run.collect_work()
        self.assertEqual(run.diff[0], 2)
        self.assertIn("lib/new.py", (run.rdir / "final.patch").read_text())
        self.clean_up_and_check(run)

    def test_a_repository_the_worker_made_inside_the_tree_costs_the_patch_only_that_path(self):
        run = self.worktree_run()
        self.edit(run)
        (run.worktree / "inner").mkdir()
        git(run.worktree / "inner", "init", "-q")
        run.collect_work()
        self.assertEqual(run.diff[0], 2, "the committed change and the new file are in the patch")
        self.assertTrue(any("inner/" in line for line in run.info["unindexed"]), run.info.get("unindexed"))
        self.assertIn("lib/new.py", (run.rdir / "final.patch").read_text())
        self.clean_up_and_check(run)

    def test_files_and_commit_messages_in_another_encoding_are_in_the_patch_as_they_are(self):
        run = self.worktree_run()
        text = "caf\xe9 \xf7\n".encode("latin-1")
        (run.worktree / "lib" / "latin.py").write_bytes(text)
        git(run.worktree, "add", "-A")
        git(run.worktree, "-c", "i18n.commitEncoding=latin1", "commit", "-q", "-m", "caf\xe9".encode("latin-1").decode("latin-1"))
        (run.worktree / "lib" / "later.py").write_bytes(text)
        run.collect_work()
        patch = (run.rdir / "final.patch").read_bytes()
        self.assertEqual(patch.count(text), 2, "the bytes the worker wrote, not replacements")
        self.assertEqual(run.diff[0], 2)
        self.clean_up_and_check(run)

    def test_git_output_that_is_not_utf8_never_ends_a_step_with_a_decode_error(self):
        run = self.worktree_run()
        out = ts.git(self.repo, "-c", "core.quotepath=off", "log", "-1", "--format=%an", "--author=x", check=False)
        self.assertIsInstance(out.stdout, str)
        raw = ts.git(self.repo, "cat-file", "-p", "HEAD:lib/x.py", raw=True)
        self.assertIsInstance(raw.stdout, bytes)
        (run.worktree / "weird.txt").write_bytes(b"\xf7\xff\n")
        git(run.worktree, "add", "weird.txt")
        self.assertIn("\ufffd", ts.git(run.worktree, "diff", "--cached").stdout)

    def test_a_big_file_the_worker_left_is_in_the_patch_and_does_not_stop_the_collect(self):
        run = self.worktree_run()
        (run.worktree / "big.bin").write_bytes(b"\xf7" * (24 * 1024 * 1024))
        run.collect_work()
        self.assertEqual(run.diff[0], 1)
        self.clean_up_and_check(run)

    def test_a_read_only_directory_in_the_tree_does_not_keep_the_cleanup_from_removing_it(self):
        run = self.worktree_run()
        cache = run.worktree / "cache" / "deep"
        cache.mkdir(parents=True)
        (cache / "f").write_text("x")
        os.chmod(str(cache), 0o555)  # as a module cache is made
        os.chmod(str(cache.parent), 0o555)
        self.addCleanup(lambda: [os.chmod(str(p), 0o755) for p in (cache.parent, cache) if p.exists()])
        self.clean_up_and_check(run)

    def test_a_link_out_of_the_tree_is_removed_as_a_link_and_what_it_leads_to_stays(self):
        run = self.worktree_run()
        outside = self.root / "outside-dir"
        outside.mkdir()
        (outside / "keep").write_text("x")
        os.symlink(str(outside), str(run.worktree / "to-outside"))
        os.symlink(str(self.repo), str(run.worktree / "to-main"))
        self.edit(run)
        run.collect_work()
        self.clean_up_and_check(run, outside / "keep")
        self.assertTrue((self.repo / "lib" / "x.py").exists())

    def test_an_index_lock_the_worker_left_stops_the_collect_with_an_error_and_the_cleanup_still_works(self):
        run = self.worktree_run()
        self.edit(run)
        admin = Path(ts._gitfile_target(run.worktree))
        (admin / "index.lock").write_text("")
        with self.assertRaises(ts.ShadowError):
            run.collect_work()
        self.clean_up_and_check(run)

    def test_an_index_or_a_head_file_that_is_garbage_stops_the_collect_with_an_error_and_the_cleanup_still_works(self):
        for name in ("index", "HEAD"):
            with self.subTest(name=name):
                run = self.worktree_run()
                self.edit(run)
                admin = Path(ts._gitfile_target(run.worktree))
                (admin / name).write_bytes(b"\x00\xff not a git file \x00" * 64)
                with self.assertRaises(ts.ShadowError):
                    run.collect_work()
                self.assertFalse((run.rdir / "final.patch").exists())
                self.clean_up_and_check(run)

    def test_a_tag_and_a_stash_the_worker_made_stay_where_they_are(self):
        run = self.worktree_run()
        (run.worktree / "lib" / "x.py").write_text("changed\n")
        git(run.worktree, "stash", "-q")
        git(run.worktree, "tag", "worker-tag")
        stash = git(self.repo, "rev-parse", "refs/stash")
        self.edit(run)
        run.collect_work()
        self.clean_up_and_check(run)
        self.assertEqual(git(self.repo, "rev-parse", "worker-tag"), self.base)
        self.assertEqual(git(self.repo, "rev-parse", "refs/stash"), stash)

    def test_a_second_collect_after_the_first_changes_nothing_and_adds_no_ref(self):
        run = self.worktree_run()
        self.edit(run)
        run.collect_work()
        ledger = json.loads(ts.owner_file(run.project, run.run).read_text())["refs"]
        patch = (run.rdir / "final.patch").read_bytes()
        run.collect_work()
        self.assertEqual(json.loads(ts.owner_file(run.project, run.run).read_text())["refs"], ledger)
        self.assertEqual((run.rdir / "final.patch").read_bytes(), patch)
        self.clean_up_and_check(run)

    def test_the_worker_deleting_the_branch_it_was_on_after_leaving_it_is_the_same_as_a_detached_tree(self):
        run = self.worktree_run()
        git(run.worktree, "checkout", "-q", "-b", "temp")
        self.edit(run)
        git(run.worktree, "checkout", "-q", "--detach")
        git(run.worktree, "branch", "-q", "-D", "temp")
        run.collect_work()
        self.assertNotIn("branch_left", run.info)
        self.assertEqual(git(run.worktree, "rev-parse", "HEAD"), self.base)
        self.clean_up_and_check(run)


class UnfinishedGitTests(TreeCase):
    """A worker that gave up in the middle of a merge, a rebase or a cherry-pick leaves git in a state in which some commands refuse;
    the collect still produces the patch of what is in the tree, and the cleanup removes the tree."""

    def conflict(self, run, how, on_branch=False):
        """Two commits that change lib/x.py, the worker's own on a detached HEAD (or on a branch of its own) and a side branch's; then
        `how` is left unfinished."""
        git(run.worktree, "checkout", "-q", "-b", "side-" + run.run)
        (run.worktree / "lib" / "x.py").write_text("side\n")
        git(run.worktree, "commit", "-q", "-a", "-m", "side")
        if on_branch:
            git(run.worktree, "checkout", "-q", "-b", "mine-" + run.run, self.base)
        else:
            git(run.worktree, "checkout", "-q", "--detach", self.base)
        (run.worktree / "lib" / "x.py").write_text("mine\n")
        git(run.worktree, "commit", "-q", "-a", "-m", "mine")
        command = {"merge": ["merge", "side-" + run.run], "rebase": ["rebase", "side-" + run.run], "cherry-pick": ["cherry-pick", "side-" + run.run]}[how]
        result = subprocess.run(["git", "-C", str(run.worktree)] + command, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0, "the fixture must leave a conflict: " + result.stdout)

    def test_a_merge_a_rebase_or_a_cherry_pick_left_unfinished_still_gives_a_patch_and_a_removable_tree(self):
        for n, (how, on_branch) in enumerate((("merge", False), ("rebase", False), ("cherry-pick", False), ("merge", True), ("cherry-pick", True))):
            with self.subTest(unfinished=how, on_a_branch=on_branch):
                run = self.worktree_run(run="sh-un{}".format(n))
                self.conflict(run, how, on_branch)
                mine = git(run.worktree, "rev-parse", "mine-" + run.run) if on_branch else None
                (run.worktree / "lib" / "new.py").write_text("new\n")
                run.collect_work()
                self.assertEqual(git(run.worktree, "rev-parse", "HEAD"), self.base, "HEAD is at the base: the review sees the work as a change")
                self.assertIn("lib/new.py", (run.rdir / "final.patch").read_text())
                if on_branch:
                    self.assertEqual(git(self.repo, "rev-parse", "mine-" + run.run), mine, "the branch the worker was on is where it was")
                    self.assertEqual(run.info["branch_left"]["ref"], "refs/heads/mine-" + run.run)
                self.assertEqual(ts.cleanup(run.rdir, self.env), [])
                self.assertFalse(run.worktree.exists())


class RefLedgerTests(TreeCase):
    def test_a_ref_whose_record_could_not_be_written_is_not_left_behind(self):
        run = self.worktree_run()
        ref = "refs/tmux-pi/shadow/{}/head".format(run.run)
        with mock.patch.object(run.owner, "_write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                run.owner.keep_ref(ref, self.base)
        self.assertEqual(self.refs("refs/tmux-pi"), "", "a ref that is not recorded is one the cleanup could not know to remove")
        self.assertEqual(run.owner.record.get("refs", {}), {})
        self.assertEqual(ts.cleanup(run.rdir, self.env), [])

    def test_a_ref_outside_the_runs_prefix_or_at_no_commit_is_refused_and_nothing_is_made(self):
        run = self.worktree_run()
        for ref, commit in (("refs/heads/evil", self.base), ("refs/tmux-pi/shadow/other-run/head", self.base),
                            ("refs/tmux-pi/shadow/{}/head".format(run.run), "--delete"), ("refs/tmux-pi/shadow/{}/head".format(run.run), "zz")):
            with self.subTest(ref=ref, commit=commit):
                with self.assertRaises(ts.ShadowError):
                    run.owner.keep_ref(ref, commit)
        self.assertEqual(self.refs("refs/tmux-pi"), "")
        self.assertNotIn("refs/heads/evil", self.refs("refs/heads"))

    def test_keeping_the_same_ref_twice_at_the_same_commit_is_the_same_as_once_and_at_another_commit_is_refused(self):
        run = self.worktree_run()
        ref = "refs/tmux-pi/shadow/{}/head".format(run.run)
        run.owner.keep_ref(ref, self.base)
        run.owner.keep_ref(ref, self.base)
        with self.assertRaises(ts.ShadowError):
            run.owner.keep_ref(ref, git(run.worktree, "commit-tree", "-m", "another", self.base + "^{tree}"))
        self.assertEqual(git(self.repo, "rev-parse", ref), self.base)
        self.assertEqual(ts.cleanup(run.rdir, self.env), [])
        self.assertEqual(self.refs("refs/tmux-pi"), "")


