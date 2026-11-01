"""lib/trial_shadow.py, lib/trial_session.py and lib/trial_record_stub.py, review 8 (the worker is not hostile but makes mistakes):

F2  the runner deletes no branch. A branch HEAD of the worker's tree is on (one it made, renamed, or somebody else made meanwhile)
    is detached from and kept where it is; moving HEAD to the base never moves it. The refs the runner does make are recorded (name and
    commit) and removed only by that record.
F5  every read of a file the worker can write opens it O_NONBLOCK | O_NOFOLLOW, requires a regular file within a size limit, and
    otherwise treats it as missing or invalid; the session kill always runs. One matrix covers every site against every kind of
    thing the worker can leave at the name: a FIFO, a link, a file past the limit, a directory.

Fixtures: trial_shadow_case.py. A FIFO gets a writer thread, so a reader that wrongly waits on it gets valid data and the test fails
instead of hanging; nothing waits a fixed time."""
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trial_shadow_case import *  # noqa: E402,F401,F403
import io  # noqa: E402
import trial_record  # noqa: E402
import trial_scan  # noqa: E402

MALFORMED = ("empty", "truncated", "binary", "array", "deep")
LIMIT = 16384  # MAX_READ for these tests: a file one byte past it is refused, and no test has to write 8 MiB


def run_in(rdir, env, **kw):
    return ts.Run(rdir, env, **kw)


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
        run2 = self.worktree_run()
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


class KindCase(ShadowCase):
    """Things a worker leaves at the name of a file the runner reads: a FIFO, a link, a file past the limit, a directory, and the files
    a crashed worker leaves (empty, cut short, not text, not an object, nested too deep)."""

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(trial_session, "MAX_READ", LIMIT, create=True)  # create: the tests can also run against the code before review 8
        patch.start()
        self.addCleanup(patch.stop)
        self.feeders = []

    def tearDown(self):
        for stop in self.feeders:
            stop()
        super().tearDown()

    # -- putting a kind of thing at a name
    def feed(self, path, data):
        done = threading.Event()

        def writer():
            while not done.is_set():
                try:
                    fd = os.open(str(path), os.O_WRONLY | os.O_NONBLOCK)
                except OSError:  # no reader yet (ENXIO)
                    done.wait(0.005)
                    continue
                try:
                    os.write(fd, data)
                except OSError:
                    pass
                finally:
                    os.close(fd)
        thread = threading.Thread(target=writer, daemon=True)
        thread.start()

        def stop():
            done.set()
            thread.join()
        self.feeders.append(stop)
        return stop

    def clear(self, path):
        path = Path(path)
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(str(path))
        elif os.path.lexists(str(path)):
            path.unlink()

    def put(self, path, kind, valid):
        """Leave `kind` at `path`: what a plain file there would hold is `valid` (bytes). A FIFO is fed it, a link leads to a file that
        holds it, a huge file starts with it. Returns a function that ends the feeder, if any."""
        path = Path(path)
        self.clear(path)
        if kind == "plain":
            path.write_bytes(valid)
        elif kind == "fifo":
            os.mkfifo(str(path))
            return self.feed(path, valid)
        elif kind == "symlink":
            target = self.root / "link-target-{}".format(abs(hash((str(path), kind))))
            target.write_bytes(valid)
            os.symlink(str(target), str(path))
        elif kind == "huge":
            path.write_bytes(valid + b"\n" * (trial_session.MAX_READ + 1))  # valid JSON and text, one byte past the limit
        elif kind == "directory":
            path.mkdir()
        elif kind == "empty":
            path.write_bytes(b"")
        elif kind == "truncated":
            path.write_bytes(valid[:len(valid) // 2])
        elif kind == "binary":
            path.write_bytes(b"\xff\xfe\x00\x80\xf7")
        elif kind == "array":
            path.write_bytes(b"[1, 2]")
        elif kind == "deep":
            path.write_bytes(b"[" * 5000 + b"]" * 5000)
        else:
            raise AssertionError(kind)
        return lambda: None

    def observe(self, call):
        try:
            return call()
        except ts.ShadowError as problem:
            return problem
        except (OSError, trial_record_stub.RecordError, trial_record.RecordError) as problem:
            return problem


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


class PlaceTests(KindCase, TreeCase):
    """The places the runner removes things at, replaced by something else by a worker that cleaned up too much or too little: the
    tree, its git admin dir and the owner record. Nothing is waited on, nothing outside is touched, and what is not shown to be the
    run's is left as it is."""

    KINDS = ("fifo", "plain", "directory", "symlink", "absent")

    def replace(self, path, kind):
        """The worker removes what is at `path` and leaves `kind` there. Returns a file that must survive (what a link leads to)."""
        path = Path(path)
        self.clear(path)
        keep = self.root / "keep-{}".format(abs(hash((str(path), kind))))
        if kind == "symlink":
            keep.mkdir()
            (keep / "file").write_text("x")
            os.symlink(str(keep), str(path))
        elif kind != "absent":
            self.put(path, kind, b"gitdir: nowhere\n")
        return keep / "file"

    def test_the_tree_replaced_by_anything_else_is_not_removed_or_followed_and_nothing_waits_on_it(self):
        for n, kind in enumerate(self.KINDS):
            with self.subTest(kind=kind):
                run = self.worktree_run(run="sh-pl{}".format(n))
                keep = self.replace(run.worktree, kind)
                try:
                    run.collect_work()
                except ts.ShadowError:
                    pass
                left = ts.cleanup(run.rdir, self.env)
                self.assertIsInstance(left, list)
                if kind == "absent":
                    self.assertEqual(left, [])
                else:
                    self.assertTrue(os.path.lexists(str(run.worktree)), "what is at the place is not the run's tree: left as it is")
                if kind == "symlink":
                    self.assertTrue(keep.exists())
                self.assertEqual(self.refs("refs/tmux-pi"), "")

    def test_the_git_admin_dir_replaced_by_anything_else_is_not_removed_or_followed_and_nothing_waits_on_it(self):
        for n, kind in enumerate(self.KINDS):
            with self.subTest(kind=kind):
                run = self.worktree_run(run="sh-pa{}".format(n))
                admin = Path(ts._gitfile_target(run.worktree))
                keep = self.replace(admin, kind)
                with self.assertRaises(ts.ShadowError):  # git no longer knows the tree: no git command is run there
                    run.collect_work()
                left = ts.cleanup(run.rdir, self.env)
                self.assertIsInstance(left, list)
                if kind == "symlink":
                    self.assertTrue(keep.exists(), "the link was not followed")

    def test_an_owner_record_that_is_anything_but_the_plain_file_leaves_the_run_as_it_is_and_the_cleanup_returns(self):
        for n, kind in enumerate(("fifo", "symlink", "huge", "directory") + MALFORMED):
            with self.subTest(kind=kind):
                run = self.worktree_run(run="sh-po{}".format(n))
                record = ts.owner_file(run.project, run.run)
                saved = record.read_bytes()
                stop = self.put(record, kind, saved)
                try:
                    left = ts.cleanup(run.rdir, self.env)
                finally:
                    stop()
                self.assertTrue(left, "the leftovers are reported")
                self.assertTrue(run.worktree.exists(), "the identities cannot be read, so the tree is not shown to be the run's: left")
                self.clear(record)
                record.write_bytes(saved)
                self.assertEqual(ts.cleanup(run.rdir, self.env), [])


class RunFilesTests(KindCase):
    """A whole run (timed out, so it goes through the collect, the review attempt, the record and the cleanup) whose own files are
    replaced by something else while it runs: it ends, the worker's session is gone and its tree is removed."""

    NAMES = {".run.lock": lambda run: run.rdir / ".run.lock", "final.patch": lambda run: run.rdir / "final.patch",
             "request.md": lambda run: run.rdir / "request.md", "owner record": lambda run: ts.owner_file(run.project, run.run)}

    def test_a_run_whose_files_are_replaced_while_it_runs_still_ends_its_session_and_removes_its_tree(self):
        n = 0
        for name, where in self.NAMES.items():
            for kind in ("fifo", "symlink", "huge", "directory"):
                n += 1
                with self.subTest(file=name, kind=kind):
                    rdir, info = self.prepare(run="sh-rf{}".format(n), timeout_s=300)
                    seen = {}

                    def poll(count, run):
                        if count == 1:
                            seen["worktree"] = run.worktree
                            path = where(run)
                            self.put(path, kind, path.read_bytes() if path.is_file() else b"x")

                    try:
                        self.run_stub(rdir, poll, clock=Clock(step=100.0))
                    except Exception as problem:  # the run may end with an error: it must end
                        seen["error"] = problem
                    self.assertIsNone(trial_session.named(trial_session.session_name(info["run"]), self.env), "the run's session was ended")
                    self.assertFalse(self.alive(self.worker_pane), "the worker's pane went with the session")
                    self.assertFalse(seen["worktree"].exists(), "the tree was removed: {!r}".format(seen.get("error")))


class CommandTests(KindCase):
    """`tp-trial status` and `stop` on a run whose run.json the worker left as anything but the plain file: each ends at once with a
    message, never with a traceback, and shows nothing of a run it could not read."""

    def tp_trial(self, *args):
        return subprocess.run([str(ROOT / "bin" / "tp-trial"), *args, "--project", str(self.repo)], env={**self.env, "TP_STATE_DIR": str(self.state)},
                              text=True, capture_output=True, timeout=120)

    def test_status_and_stop_end_with_a_message_when_run_json_is_anything_but_the_plain_file(self):
        for n, kind in enumerate(("plain", "fifo", "symlink", "huge", "directory") + MALFORMED):
            with self.subTest(kind=kind):
                rdir, info = self.prepare(run="sh-cm{}".format(n))
                saved = (rdir / "run.json").read_bytes()
                stop = self.put(rdir / "run.json", kind, saved)
                if kind == "huge":  # the commands run with the real limit, not this process's: a sparse file one byte past 8 MiB
                    os.truncate(str(rdir / "run.json"), 8 * 1024 * 1024 + 1)
                try:
                    listing = self.tp_trial("status")
                    one = self.tp_trial("status", info["run"])
                    ended = self.tp_trial("stop", info["run"])
                finally:
                    stop()
                for result in (listing, one, ended):
                    self.assertNotIn("Traceback", result.stderr)
                if kind == "plain":
                    self.assertIn("run=" + info["run"], listing.stdout)
                    self.assertEqual(one.returncode, 0)
                else:
                    self.assertNotIn("run=" + info["run"], listing.stdout + one.stdout, "nothing is shown of a run that could not be read")
                    self.assertEqual(one.returncode, 1)
                self.clear(rdir / "run.json")
                (rdir / "run.json").write_bytes(saved)
                self.assertEqual(ts.cleanup(rdir, self.env), [])


class PlainFileTests(KindCase):
    """F5: `trial_session.read_plain` and every site that reads a file the worker can write."""

    def matrix(self, name, path, valid, call, ok, refused, restore=None, malformed=True):
        """`call` finds `valid` at `path` when it is a plain file (`ok` holds of what it gave) and finds the same thing as a FIFO, link,
        too-large file or directory (`refused` holds, and it returned: a FIFO is fed `valid`, so a reader that waited on it would give
        `ok`'s answer). With `malformed`, a file that is empty, cut short, not text, not an object or nested too deep is not taken for
        `valid` either, and no error other than the site's own escapes."""
        path = Path(path)
        saved = path.read_bytes() if path.is_file() else None
        try:
            for kind in ("plain", "fifo", "symlink", "huge", "directory") + (MALFORMED if malformed else ()):
                with self.subTest(site=name, kind=kind):
                    stop = self.put(path, kind, valid)
                    try:
                        got = self.observe(call)
                    finally:
                        stop()
                    if kind == "plain":
                        self.assertTrue(ok(got), "{}: {!r}".format(kind, got))
                    elif kind in MALFORMED:
                        self.assertFalse(ok(got), "{}: {!r}".format(kind, got))
                    else:
                        self.assertTrue(refused(got), "{}: {!r}".format(kind, got))
        finally:
            self.clear(path)
            if restore is not False and saved is not None:
                path.write_bytes(saved if restore is None else restore)

    def is_error(self, *words):
        return lambda got: isinstance(got, Exception) and all(w in str(got) for w in words)

    # -- the reader itself
    def test_read_plain_refuses_anything_but_a_plain_file_within_the_limit_before_reading_it(self):
        good = self.root / "good"
        good.write_bytes(b"x" * LIMIT)
        self.assertEqual(trial_session.read_plain(good), b"x" * LIMIT)
        (self.root / "big").write_bytes(b"x" * (LIMIT + 1))
        with self.assertRaises(OSError) as caught:
            trial_session.read_plain(self.root / "big")
        self.assertEqual(caught.exception.errno, trial_shadow_errno("EFBIG"))
        self.assertEqual(trial_session.read_plain(self.root / "big", limit=LIMIT + 1), b"x" * (LIMIT + 1))
        with self.assertRaises(OSError):
            trial_session.read_plain(self.root)  # a directory
        os.symlink(str(good), str(self.root / "link"))
        with self.assertRaises(OSError):
            trial_session.read_plain(self.root / "link")
        with self.assertRaises(FileNotFoundError):
            trial_session.read_plain(self.root / "none")

    def test_the_limit_is_8_mib_and_a_sparse_file_past_it_is_refused_by_its_size_alone(self):
        with mock.patch.object(trial_session, "MAX_READ", 8 * 1024 * 1024):
            sparse = self.root / "sparse"
            with open(str(sparse), "wb") as handle:
                handle.truncate(8 * 1024 * 1024 + 1)  # no data is written: the file is refused before a byte is read
            with self.assertRaises(OSError):
                trial_session.read_plain(sparse)
        self.assertEqual(trial_session.MAX_READ, LIMIT, "(the patch of this class)")
        self.assertEqual(ts.trial_session.MAX_READ, LIMIT)

    def test_a_file_that_grows_while_it_is_read_is_still_bounded(self):
        growing = self.root / "growing"
        growing.write_bytes(b"x" * 10)
        real_fstat = os.fstat

        def small(fd):  # fstat saw a small file; by the read it is large
            st = real_fstat(fd)
            growing.write_bytes(b"x" * (LIMIT * 4))
            return st
        with mock.patch.object(trial_session.os, "fstat", small):
            with self.assertRaises(OSError):
                trial_session.read_plain(growing)

    # -- every site, every kind
    def setup_run(self):
        rdir, info, pane = self.live_run_with_task("sh-wf")
        task = rdir / "state" / "tasks" / "trial-live"
        return rdir, info, pane, task

    def test_the_files_of_the_run_and_of_its_owner(self):
        rdir, info, pane, task = self.setup_run()
        run_json = (rdir / "run.json").read_bytes()
        record = ts.owner_file(info["project"], info["run"])
        self.matrix("read_run", rdir / "run.json", run_json, lambda: ts.read_run(rdir)["run"], lambda got: got == info["run"], self.is_error("cannot read"))
        self.matrix("update_run", rdir / "run.json", run_json, lambda: ts.update_run(rdir, probe=1)["probe"], lambda got: got == 1, self.is_error("cannot"))
        self.matrix("run_info", rdir / "run.json", run_json, lambda: trial_session._run_info(rdir).get("run"), lambda got: got == info["run"], lambda got: got is None)
        self.matrix("list_runs", rdir / "run.json", run_json, lambda: [r["run"] for r in ts.list_runs(info["project"])], lambda got: got == [info["run"]], lambda got: got == [])
        owner_bytes = record.read_bytes()

        def owner_problem():
            owner = ts.Owner(rdir)
            try:
                return owner.problem
            finally:
                owner.close()
        self.matrix("owner", record, owner_bytes, owner_problem, lambda got: got is None, lambda got: "cannot be read" in (got or ""))
        self.assertTrue(self.alive(pane))

    def test_the_source_tasks_files(self):
        task = self.state / "tasks" / "src-1"
        for name, key, ok, refused in (
                ("dispatch.json", "base_commit", lambda got: got == self.base, self.is_error("base_commit")),
                ("request.md", "request", lambda got: got.startswith("Make a focused"), self.is_error("request.md")),
                ("packet.md", "other_lane_paths", lambda got: "lane beta" in got, lambda got: got == "")):
            valid = (task / name).read_bytes()
            self.matrix("source " + name, task / name, valid, lambda: ts.read_source(self.state, "src-1")[key], ok, refused,
                        malformed=name == "dispatch.json")
        valid = (task / "meta.json").read_bytes()
        self.matrix("source meta.json", task / "meta.json", valid, lambda: ts.read_source(self.state, "src-1")["role"], lambda got: got == "coder",
                    lambda got: got == "" or isinstance(got, ts.ShadowError))

    def test_the_trial_tasks_files(self):
        rdir, info, pane, task = self.setup_run()
        run = ts.Run(rdir, self.env)
        self.addCleanup(run.owner.close)
        run.task_id = "trial-live"  # the fixture's hand-made task, not the "trial-<run>" tp-spawn would make
        meta = json.dumps({"id": "trial-live", "role": "coder", "pane": pane, "reviewer": {"model": "m-1"}}).encode()
        self.matrix("source_role", task / "meta.json", meta, run.source_role, lambda got: got == "coder", lambda got: got is None, restore=False)
        self.matrix("reviewer_model", task / "meta.json", meta, run.reviewer_model, lambda got: got == "m-1", lambda got: got is None, restore=False)
        self.matrix("meta_pane", task / "meta.json", meta, lambda: ts.Owner.meta_pane(task), lambda got: got == pane,
                    lambda got: isinstance(got, ts.ShadowError) and "nothing was started" in str(got) and "meta.json" in str(got), restore=False)
        (task / "meta.json").write_bytes(meta)
        self.matrix("process.json", task / "process.json", b"{}", lambda: run.owner.check_process_record(task), lambda got: "is malformed" in str(got),
                    lambda got: isinstance(got, ts.ShadowError) and "malformed" not in str(got) and "process.json" in str(got), restore=False, malformed=False)
        self.assertTrue(self.alive(pane))

    def test_the_worktrees_git_files(self):
        rdir, _ = self.prepare()
        run = ts.Run(rdir, self.env)
        run.make_worktree()
        self.addCleanup(lambda: ts.cleanup(rdir, self.env))
        gitfile = run.worktree / ".git"
        valid = gitfile.read_bytes()
        target = ts._gitfile_target(run.worktree)
        self.matrix("worktree .git", gitfile, valid, lambda: ts._gitfile_target(run.worktree), lambda got: got == target, lambda got: got is None)
        admin = Path(ts._gitfile_target(run.worktree))
        gitdir = admin / "gitdir"
        valid = gitdir.read_bytes()
        self.matrix("admin gitdir", gitdir, valid, lambda: run.owner.prove(run.worktree)[0], lambda got: got == "owned", lambda got: got == "damaged")

    def test_the_record_builders_read_of_the_task_files(self):
        rdir, info, pane, task = self.setup_run()
        reviewer = rdir / "state" / "tasks" / "trial-rev"
        reviewer.mkdir()
        (reviewer / "meta.json").write_text(json.dumps({"id": "trial-rev", "model": "rev-model"}))
        (reviewer / "result.md").write_text("APPROVED\nfine\n")
        meta = json.dumps({"id": "trial-live", "role": "coder", "model": MODEL, "reviews": ["trial-rev"]}).encode()
        (task / "meta.json").write_bytes(meta)
        state = rdir / "state"

        def build():
            return trial_record_stub.build(task, "qwen", "shadow", "p-1", state_dir=state)
        self.matrix("stub meta.json", task / "meta.json", meta, lambda: build()["task_id"], lambda got: got == "trial-live",
                    lambda got: got is None or isinstance(got, trial_record_stub.RecordError), restore=False)
        (task / "meta.json").write_bytes(meta)
        (task / "dispatch.json").write_text(json.dumps({"base_commit": self.base}))
        self.matrix("stub dispatch.json", task / "dispatch.json", json.dumps({"base_commit": self.base}).encode(), lambda: build()["base_commit"],
                    lambda got: got == self.base, lambda got: got is None)
        self.matrix("stub reviewer meta.json", reviewer / "meta.json", json.dumps({"id": "trial-rev", "model": "rev-model"}).encode(),
                    lambda: build()["reviewer_model"], lambda got: got == "rev-model", lambda got: got is None)
        self.matrix("stub result.md", reviewer / "result.md", b"APPROVED\nfine\n", lambda: build()["first_review_verdict"],
                    lambda got: got == "APPROVED", lambda got: got is None, malformed=False)

    # -- the real record builder (lib/trial_record.py, lane L2): every read of a file the worker can write is a read_plain read
    APPROVED = "APPROVED\nRESULT_STATUS: complete\n## Coverage\nReviewed lib/trial_record.py in full.\n"

    def real_fixture(self):
        """A finished task with every file the real builder reads, a reviewer that approved it, and a Claude transcript named by
        transcript-path. Returns (task dir, state dir, reviewer dir, transcript file, a function that builds the record)."""
        rdir, info, pane, task = self.setup_run()
        state = rdir / "state"
        reviewer = state / "tasks" / "trial-rev"
        reviewer.mkdir()
        (reviewer / "meta.json").write_text(json.dumps({"id": "trial-rev", "worker": "pi", "model": "rev-model", "thinking": "high"}))
        (reviewer / "result.md").write_text(self.APPROVED)
        (task / "meta.json").write_text(json.dumps({"id": "trial-live", "role": "coder", "worker": "pi", "model": MODEL, "thinking": "medium",
                                                    "review": "trial-rev", "reviews": ["trial-rev"], "created": "2026-10-31T10:00:00Z"}))
        (task / "state.json").write_text(json.dumps({"state": "completed", "updated": "2026-10-31T10:05:00Z", "turns": 3}))
        (task / "dispatch.json").write_text(json.dumps({"base_commit": self.base, "context": ["a.py"]}))
        (task / "exit").write_text("0\n")
        (task / "request.md").write_text("Do a thing\n")
        (task / "result.md").write_text("Done <think>oops</think>\n")
        (task / "report.md").write_text("report\n")
        (task / "docs-calls.jsonl").write_text(json.dumps({"tool": "docs", "args": {"package": "p", "symbol": "s"}, "hit": True, "bytes": 5}) + "\n")
        transcript = self.root / "claude-transcript.jsonl"
        transcript.write_bytes((ROOT / "tests" / "fixtures" / "trial" / "claude_transcript.jsonl").read_bytes())
        (task / "transcript-path").write_text(str(transcript))

        def build():
            return trial_record.build(task, "qwen", "shadow", "p-1", state_dir=state)
        return task, state, reviewer, transcript, build

    def test_the_real_record_builder_opens_no_file_of_the_task_or_its_reviewer_itself(self):
        """Whatever it reads of the task, the reviewer and the transcript goes through trial_session.read_plain: no `open`, no
        Path.read_text, no Path.open on a path under the state dir or on the transcript."""
        task, state, reviewer, transcript, build = self.real_fixture()
        (reviewer / "acceptance-items.json").write_text('{"items": [1]}')
        roots = (str(state), str(transcript))
        opened = []
        real_open = io.open

        def spy(file, *args, **kwargs):
            if isinstance(file, (str, bytes, os.PathLike)) and os.fsdecode(file).startswith(roots):
                opened.append(os.fsdecode(file))
            return real_open(file, *args, **kwargs)
        with mock.patch("builtins.open", spy), mock.patch("io.open", spy):
            record = build()
        self.assertEqual(opened, [])
        self.assertEqual((record["task_id"], record["state"], record["exit"], record["reviewer_model"]), ("trial-live", "completed", 0, "rev-model"))

    def test_the_real_record_builders_read_of_the_task_files(self):
        task, state, reviewer, transcript, build = self.real_fixture()

        def no_record(got):
            return got is None or isinstance(got, trial_record.RecordError)
        self.matrix("real meta.json", task / "meta.json", (task / "meta.json").read_bytes(), build,
                    lambda got: isinstance(got, dict) and got["model"] == MODEL, no_record, restore=False)
        (task / "meta.json").write_text(json.dumps({"id": "trial-live", "role": "coder", "worker": "pi", "model": MODEL, "thinking": "medium",
                                                    "review": "trial-rev", "reviews": ["trial-rev"], "created": "2026-10-31T10:00:00Z"}))
        for name, ok, refused, malformed in (
                ("dispatch.json", lambda got: got["base_commit"] == self.base, lambda got: got["base_commit"] is None, True),
                ("state.json", lambda got: got["state"] == "completed", lambda got: got["state"] is None, True),
                ("exit", lambda got: got["exit"] == 0, lambda got: got["exit"] is None, False),
                ("docs-calls.jsonl", lambda got: got["docs_calls"]["count"] == 1, lambda got: got["docs_calls"]["count"] == 0, True),
                ("result.md", lambda got: got["rule_slips"]["by_rule"].get("think_leak") == 1,
                 lambda got: "think_leak" not in got["rule_slips"]["by_rule"], False),
                ("transcript-path", lambda got: got["transcript"]["status"] == "ok", lambda got: got["transcript"]["status"] == "missing", False)):
            with self.subTest(file=name):
                self.matrix("real " + name, task / name, (task / name).read_bytes(), lambda: build(), ok, refused, malformed=malformed)
        for name, ok, refused in (
                ("meta.json", lambda got: got["reviewer_model"] == "rev-model", lambda got: got["reviewer_model"] is None),
                ("acceptance-items.json", lambda got: got["first_review_verdict"] == "incomplete", lambda got: got["first_review_verdict"] == "APPROVED")):
            if name == "acceptance-items.json":
                (reviewer / name).write_text('{"items": [1]}')
            with self.subTest(reviewer_file=name):
                self.matrix("real reviewer " + name, reviewer / name, (reviewer / name).read_bytes(), lambda: build(), ok, refused, malformed=False)
            if name == "acceptance-items.json":
                (reviewer / name).unlink()
        self.matrix("real reviewer result.md", reviewer / "result.md", self.APPROVED.encode(), lambda: build(),
                    lambda got: got["first_review_verdict"] == "APPROVED", lambda got: got["first_review_verdict"] is None, malformed=False)
        with mock.patch.object(trial_scan, "MAX_TRANSCRIPT_BYTES", LIMIT):
            self.matrix("real claude transcript", transcript, transcript.read_bytes(), lambda: build(),
                        lambda got: got["transcript"]["status"] == "ok", lambda got: got["transcript"]["status"] != "ok", malformed=False)

    def test_the_records_file_is_appended_to_without_waiting_for_a_reader(self):
        records = trial_record_stub.trial_dir(self.repo) / "records.jsonl"
        records.parent.mkdir(parents=True, exist_ok=True)

        def append():
            trial_record_stub.append(self.repo, {"x": 1})
            return records.read_text()
        for kind in ("plain", "fifo", "symlink", "directory"):
            with self.subTest(kind=kind):
                if os.path.lexists(str(records)):
                    self.clear(records)
                stop = self.put(records, kind, b"")
                try:
                    got = self.observe(append)
                finally:
                    stop()
                if kind == "plain":
                    self.assertEqual(got, '{"x": 1}\n')
                else:
                    self.assertIsInstance(got, OSError)
        self.clear(records)

    # -- the session kill always runs
    def test_a_run_whose_files_are_left_as_any_of_these_still_ends_its_session_and_removes_its_tree(self):
        for n, kind in enumerate(("symlink", "huge", "directory")):
            with self.subTest(kind=kind):
                outside = self.outside("outside-pf{}".format(n)) if hasattr(self, "outside") else None
                rdir, info = self.prepare(run="sh-pf{}".format(n))
                where = {}

                def poll(count, run):
                    if count == 1:
                        where["worktree"] = run.worktree
                        self.put(rdir / "run.json", kind, (rdir / "run.json").read_bytes())
                try:
                    with self.assertRaises(ts.ShadowError):  # the run's own record cannot be read back at the end
                        self.run_stub(rdir, poll)
                finally:
                    if outside:
                        self.assertTrue(self.alive(outside))
                self.assertIsNone(trial_session.named(trial_session.session_name(info["run"]), self.env), "the run's session was ended")
                self.assertFalse(self.alive(self.worker_pane), "the worker's pane went with the session")
                self.assertFalse(where["worktree"].exists(), "the tree was removed")

    def test_a_task_file_left_as_a_directory_or_a_link_does_not_keep_the_cleanup_from_ending_the_session(self):
        for n, kind in enumerate(("symlink", "huge", "directory", "fifo")):
            with self.subTest(kind=kind):
                rdir, info = self.prepare(run="sh-pt{}".format(n))

                def poll(count, run):
                    if count == 1:
                        self.put(run.task_path() / "process.json", kind, b"{}")
                        self.put(run.task_path() / "dispatch.json", kind, b"{}")
                run, done = self.run_stub(rdir, poll, clock=Clock(step=10.0))
                self.assertIsNone(trial_session.named(trial_session.session_name(info["run"]), self.env))
                self.assertFalse(self.alive(self.worker_pane))
                self.assertEqual(done["phase"], "done")


def trial_shadow_errno(name):
    import errno
    return getattr(errno, name)


if __name__ == "__main__":
    unittest.main()
