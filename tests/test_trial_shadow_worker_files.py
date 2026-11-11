"""lib/trial_shadow.py and lib/trial_session.py, review 8 (the worker is not hostile but makes mistakes):

F2  (branches, refs and a tree left in an odd state: tests/test_trial_shadow_branches.py)
F5  every read of a file the worker can write opens it O_NONBLOCK | O_NOFOLLOW, requires a regular file within a size limit, and
    otherwise treats it as missing or invalid; the session kill always runs. One matrix covers every site against every kind of
    thing the worker can leave at the name: a FIFO, a link, a file past the limit, a directory.

Fixtures: trial_shadow_case.py (TreeCase from test_trial_shadow_branches.py). A FIFO gets a writer thread, so a reader that wrongly waits on it gets valid data and the test fails
instead of hanging; nothing waits a fixed time."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trial_shadow_case import *  # noqa: E402,F401,F403
import trial_record  # noqa: E402

MALFORMED = ("empty", "truncated", "binary", "array", "deep")
LIMIT = 16384  # MAX_READ for these tests: a file one byte past it is refused, and no test has to write 8 MiB


def run_in(rdir, env, **kw):
    return ts.Run(rdir, env, **kw)


from test_trial_shadow_branches import TreeCase  # noqa: E402


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
        except (OSError, trial_record.RecordError) as problem:
            return problem

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

    def setup_run(self):
        rdir, info, pane = self.live_run_with_task("sh-wf")
        task = rdir / "state" / "tasks" / "trial-live"
        return rdir, info, pane, task



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
                run, done = self.run_stub(rdir, poll, clock=Clock(step=100.0))
                self.assertIsNone(trial_session.named(trial_session.session_name(info["run"]), self.env))
                self.assertFalse(self.alive(self.worker_pane))
                self.assertEqual(done["phase"], "done")


def trial_shadow_errno(name):
    import errno
    return getattr(errno, name)


if __name__ == "__main__":
    unittest.main()
