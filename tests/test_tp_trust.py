"""bin/tp-trust: pty-driven grant and revoke, refusals, atomicity, check against the reader."""
import contextlib
import importlib.machinery
import io
import importlib.util
import json
import os
import pty
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
TOOL = str(ROOT / "bin" / "tp-trust")
sys.path.insert(0, str(ROOT / "lib"))
import trust  # noqa: E402

CODE = re.compile(r"^\s{2}([a-z]+-[a-z]+-[a-z]+-[a-z]+)\s*$", re.M)


def load_tool():
    loader = importlib.machinery.SourceFileLoader("tp_trust_tool", TOOL)
    spec = importlib.util.spec_from_loader("tp_trust_tool", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def clean_env():
    return {k: v for k, v in os.environ.items() if not k.startswith("TP_")}


class Pty:
    """The tool run as the leader of its own session with a pty as controlling terminal."""

    def __init__(self, argv, env=None):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            try:
                os.execve(sys.executable, [sys.executable, TOOL] + argv, env or clean_env())
            finally:
                os._exit(127)
        self.out = ""

    def read_until(self, text, timeout=10):
        end = time.time() + timeout
        while text not in self.out and time.time() < end:
            if select.select([self.fd], [], [], 0.2)[0]:
                try:
                    data = os.read(self.fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                self.out += data.decode("utf-8", "replace")
        return text in self.out

    def send(self, text):
        os.write(self.fd, text.encode())

    def finish(self, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if select.select([self.fd], [], [], 0.2)[0]:
                try:
                    data = os.read(self.fd, 4096)
                except OSError:
                    data = b""
                if not data:
                    break
                self.out += data.decode("utf-8", "replace")
        for _ in range(50):
            done, status = os.waitpid(self.pid, os.WNOHANG)
            if done:
                break
            time.sleep(0.1)
        else:                       # still running (a broken tool waiting for input): fail, don't hang
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)
            status = 255 << 8
        os.close(self.fd)
        return os.waitstatus_to_exitcode(status)

    def code(self):
        self.assertion = self.read_until("> ")
        m = CODE.search(self.out)
        return m.group(1) if m else None

    def kill(self):
        os.kill(self.pid, signal.SIGKILL)
        os.waitpid(self.pid, 0)
        os.close(self.fd)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="tptrust-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.tdir = os.path.join(self.home, ".config", "tmux-pi", "trust")
        os.makedirs(self.tdir, mode=0o700)
        for d in (os.path.join(self.home, ".config"), os.path.join(self.home, ".config", "tmux-pi"), self.tdir):
            os.chmod(d, 0o700)
        self.repo = self.make_repo(os.path.join(self.home, "proj"))

    def make_repo(self, path):
        os.makedirs(os.path.join(path, ".git"))
        return os.path.realpath(path)

    def make_worktree(self, main, name="wt"):
        wt = os.path.join(os.path.dirname(main), name)
        entry = os.path.join(main, ".git", "worktrees", name)
        os.makedirs(entry)
        os.makedirs(wt)
        Path(wt, ".git").write_text("gitdir: %s\n" % entry)
        Path(entry, "commondir").write_text("../..\n")
        Path(entry, "gitdir").write_text(os.path.join(wt, ".git") + "\n")
        return os.path.realpath(wt)

    def opts(self):
        return ["--trust-dir", self.tdir, "--home", self.home, "--temp-root", "/nonexistent-temp"]

    def tool(self, *argv, env=None):
        return Pty(self.opts() + list(argv), env)

    def grant(self, path, *extra, code=True):
        p = self.tool("grant", path, *extra)
        c = p.code()
        self.assertIsNotNone(c, p.out)
        p.send((c if code else "wrong-wrong-wrong-wrong") + "\n")
        return p.finish(), p.out

    def plain(self, *argv, stdin=subprocess.DEVNULL, env=None, **kw):
        if "input" in kw:
            stdin = None
        return subprocess.run([sys.executable, TOOL] + self.opts() + list(argv), stdin=stdin,
                              capture_output=True, text=True, env=env or clean_env(), timeout=30, **kw)

    def records(self):
        return sorted(n for n in os.listdir(self.tdir))

    def reader(self, path=None):
        return trust.lookup(path or self.repo, self.tdir, home=self.home, temp_roots=("/nonexistent-temp",))


class GrantTests(Base):
    def test_grant_writes_a_record_the_reader_accepts(self):
        status, out = self.grant(self.repo, "--note", "my note")
        self.assertEqual(status, 0, out)
        names = self.records()
        self.assertEqual(names, [trust.record_name(self.repo)])
        path = os.path.join(self.tdir, names[0])
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o400)
        self.assertEqual(stat.S_IMODE(os.stat(self.tdir).st_mode), 0o700)
        rec = json.loads(Path(path).read_text())
        self.assertEqual((rec["root"], rec["note"], rec["worktrees"], rec["expires"]),
                         (self.repo, "my note", True, None))
        self.assertTrue(self.reader().trusted)

    def test_wrong_code_writes_nothing(self):
        status, out = self.grant(self.repo, code=False)
        self.assertEqual(status, 1)
        self.assertEqual(self.records(), [])
        self.assertIn("nothing written", out)

    def test_days_and_no_worktrees(self):
        status, out = self.grant(self.repo, "--days", "3", "--no-worktrees")
        self.assertEqual(status, 0, out)
        rec = json.loads(Path(self.tdir, trust.record_name(self.repo)).read_text())
        self.assertFalse(rec["worktrees"])
        self.assertTrue(rec["expires"].endswith("+00:00"))
        self.assertTrue(self.reader().trusted)

    def test_bad_days_and_note_refuse(self):
        for extra in (["--days", "0"], ["--days", "99999"], ["--note", "x" * 201], ["--note", "bad\x1b[31m"]):
            p = self.tool("grant", self.repo, *extra)
            self.assertEqual(p.finish(), 1, extra)
            self.assertEqual(self.records(), [])

    def test_scan_warns_and_still_requires_code(self):
        Path(self.repo, ".env").write_text("X=1\n")
        os.makedirs(os.path.join(self.repo, "deep", ".aws"))
        p = self.tool("grant", self.repo)
        c = p.code()
        self.assertIn(".env", p.out)
        self.assertIn("deep/.aws", p.out)
        self.assertIn("appears to hold credentials", p.out)
        p.send(c + "\n")
        self.assertEqual(p.finish(), 0)
        self.assertEqual(len(self.records()), 1)

    def test_scan_does_not_descend_nested_repos_or_links(self):
        nested = self.make_repo(os.path.join(self.repo, "vendor"))
        Path(nested, ".env").write_text("X=1\n")
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(os.path.join(outside, ".aws"))
        os.symlink(outside, os.path.join(self.repo, "link"))
        found = load_tool().scan(self.repo)
        self.assertEqual(found["nested"], ["vendor"])
        self.assertEqual((found["strong"], found["locations"]), ([], []))

    def test_scan_budget_reports_incomplete(self):
        tool = load_tool()
        for i in range(5):
            Path(self.repo, "f%d" % i).write_text("")
        with mock.patch.object(tool, "SCAN_ENTRIES", 2):
            self.assertFalse(tool.scan(self.repo)["complete"])

    def test_scan_reports_classifier_locations_inside_the_root_git(self):
        tool = load_tool()
        Path(self.repo, ".git", "config").write_text("[core]\n")
        Path(self.repo, ".git", "credentials").write_text("x\n")
        os.makedirs(os.path.join(self.repo, ".git", "objects", "ab"))
        Path(self.repo, ".git", "objects", "ab", "config").write_text("")       # deeper: not a known location
        found = tool.scan(self.repo)
        self.assertEqual(sorted(found["locations"]), [".git/config", ".git/credentials"])
        self.assertTrue(found["complete"])
        p = self.tool("grant", self.repo)
        p.code()
        self.assertIn(".git/config", p.out)
        self.assertIn("This tree appears to hold credentials", p.out.replace("this tree", "This tree"))
        p.kill()

    def test_git_config_alone_is_listed_without_the_credentials_warning(self):
        Path(self.repo, ".git", "config").write_text("[core]\n")
        p = self.tool("grant", self.repo)
        p.code()
        self.assertIn(".git/config", p.out)
        self.assertNotIn("appears to hold credentials", p.out)
        p.kill()

    def test_scan_does_not_follow_a_git_link(self):
        outside = os.path.join(self.tmp, "gitout")
        os.makedirs(outside)
        Path(outside, "config").write_text("")
        shutil.rmtree(os.path.join(self.repo, ".git"))
        os.symlink(outside, os.path.join(self.repo, ".git"))
        self.assertEqual(load_tool().scan(self.repo)["locations"], [])

    def test_scan_budget_applies_per_entry_inside_one_directory(self):
        tool = load_tool()
        seen = []

        class Endless:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def __iter__(self):
                return self

            def __next__(self):
                seen.append(1)
                if len(seen) > 10000:
                    raise AssertionError("the budget was not enforced while streaming")
                return types.SimpleNamespace(name="f", path="/x/f", is_dir=lambda follow_symlinks=False: False)
        with mock.patch.object(tool, "SCAN_ENTRIES", 50), mock.patch.object(tool.os, "scandir", return_value=Endless()):
            found = tool.scan(self.repo)
        self.assertFalse(found["complete"])
        self.assertLessEqual(len(seen), 52)

    def test_scan_time_budget_applies_per_entry(self):
        tool = load_tool()
        for i in range(5):
            Path(self.repo, "f%d" % i).write_text("")
        ticks = iter([0.0] + [0.0, 0.0, 99.0] + [99.0] * 50)
        with mock.patch.object(tool.time, "monotonic", side_effect=lambda: next(ticks)):
            found = tool.scan(self.repo)
        self.assertFalse(found["complete"])
        self.assertLess(found["entries"], 5)

    def test_help_states_the_grant_budget(self):
        out = self.plain("grant", "--help").stdout
        self.assertIn("200000 entries or 10 seconds", out.replace("\n", " ").replace("  ", " "))

    def test_regrant_replaces_record(self):
        self.assertEqual(self.grant(self.repo)[0], 0)
        self.assertEqual(self.grant(self.repo, "--days", "5")[0], 0)
        self.assertEqual(len(self.records()), 1)
        rec = json.loads(Path(self.tdir, self.records()[0]).read_text())
        self.assertIsNotNone(rec["expires"])

    def test_creates_missing_trust_directory(self):
        shutil.rmtree(self.tdir)
        self.assertEqual(self.grant(self.repo)[0], 0)
        self.assertEqual(stat.S_IMODE(os.stat(self.tdir).st_mode), 0o700)
        self.assertTrue(self.reader().trusted)

    def test_directory_changed_while_waiting_refuses(self):
        p = self.tool("grant", self.repo)
        c = p.code()
        os.rename(self.repo, self.repo + "-old")
        self.make_repo(self.repo)
        p.send(c + "\n")
        self.assertEqual(p.finish(), 1)
        self.assertIn("changed while waiting", p.out)
        self.assertEqual(self.records(), [])


class TerminalTests(Base):
    def test_no_stdin_terminal_refuses(self):
        r = self.plain("grant", self.repo)
        self.assertEqual(r.returncode, 1)
        self.assertIn("terminal", r.stderr)
        self.assertEqual(self.records(), [])

    def test_terminal_available_but_stdin_not_a_terminal_refuses(self):
        # A controlling terminal exists (pty.fork) but stdin is /dev/null: the code could be scripted.
        pid, fd = pty.fork()
        if pid == 0:
            os.execve("/bin/sh", ["sh", "-c", 'exec "$0" "$@" </dev/null', sys.executable, TOOL] +
                      self.opts() + ["grant", self.repo], clean_env())
            os._exit(127)
        q = Pty.__new__(Pty)
        q.pid, q.fd, q.out = pid, fd, ""
        self.assertEqual(q.finish(), 1)
        self.assertNotIn("Type this code", q.out)
        self.assertEqual(self.records(), [])

    def test_code_piped_on_stdin_does_not_count(self):
        r = self.plain("grant", self.repo, stdin=subprocess.PIPE, input="amber-amber-amber-amber\n")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.records(), [])

    def test_terminal_stdin_without_controlling_terminal_refuses(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        r = subprocess.run([sys.executable, TOOL] + self.opts() + ["grant", self.repo], stdin=slave,
                           stdout=slave, stderr=subprocess.PIPE, text=True, env=clean_env(),
                           start_new_session=True, timeout=30)
        self.assertEqual(r.returncode, 1)
        self.assertIn("controlling terminal", r.stderr)
        self.assertEqual(self.records(), [])

    def test_task_environment_refuses_before_any_prompt(self):
        for name in ("TP_TASK_ID", "TP_TASK_DIR", "TP_MODE"):
            env = clean_env()
            env[name] = "x"
            p = self.tool("grant", self.repo, env=env)
            self.assertEqual(p.finish(), 1, name)
            self.assertNotIn("Type this code", p.out)
            self.assertEqual(self.records(), [])
            Path(self.tdir, "x.json").write_text("{}")
            p = self.tool("revoke", "x", env=env)
            self.assertEqual(p.finish(), 1, name)
            self.assertTrue(os.path.exists(os.path.join(self.tdir, "x.json")))
            os.unlink(os.path.join(self.tdir, "x.json"))

    def test_read_only_commands_work_without_a_terminal_or_in_a_task(self):
        env = clean_env()
        env["TP_TASK_ID"] = "t"
        for argv in (["list"], ["check", self.repo], ["log"]):
            r = self.plain(*argv, env=env)
            self.assertIn(r.returncode, (0, 1), r.stderr)
            self.assertEqual(r.stderr, "")


class BreadthTests(Base):
    def refusal(self, path):
        tool = load_tool()
        env = tool.Env(tool_args(self))
        return tool.breadth_refusal(path, env)

    def test_function_level_refusals(self):
        self.assertIn("file system", self.refusal("/"))
        self.assertIn("home", self.refusal(self.home))
        self.assertIn("home", self.refusal(self.tmp))                       # ancestor of home
        self.assertIn("trust directory", self.refusal(os.path.join(self.home, ".config", "tmux-pi")))
        self.assertIsNone(self.refusal(self.repo))

    def test_config_ancestor_refused(self):
        other = os.path.join(self.tmp, "other-home")
        os.makedirs(other)
        tool = load_tool()
        args = tool_args(self)
        args.home = other
        self.assertIn("config directory", tool.breadth_refusal(self.home, tool.Env(args)))

    def test_temp_root_refused(self):
        tool = load_tool()
        args = tool_args(self)
        args.temp_root = [self.tmp]
        self.assertIn("temporary", tool.breadth_refusal(self.repo, tool.Env(args)))

    def test_credential_location_refused(self):
        repo = self.make_repo(os.path.join(self.home, ".ssh", "inner"))
        self.assertIn("credential", self.refusal(repo))
        repo = self.make_repo(os.path.join(self.home, "agent", "sessions", "r"))
        self.assertIn("credential", self.refusal(repo))

    def grant_refused(self, path, fragment):
        p = self.tool("grant", path)
        self.assertEqual(p.finish(), 1, p.out)
        self.assertIn(fragment, p.out)
        self.assertNotIn("Type this code", p.out)
        self.assertEqual(self.records(), [])

    def test_home_refused_end_to_end(self):
        os.makedirs(os.path.join(self.home, ".git"))
        self.grant_refused(self.home, "home")
        os.makedirs(os.path.join(self.tmp, ".git"))
        self.grant_refused(self.tmp, "home")

    def test_non_git_refused(self):
        plain = os.path.join(self.home, "plain")
        os.makedirs(plain)
        self.grant_refused(plain, "not a git repository")

    def test_git_file_root_refused(self):
        sub = os.path.join(self.home, "sub")
        os.makedirs(sub)
        Path(sub, ".git").write_text("gitdir: ../elsewhere\n")
        self.grant_refused(sub, "not a git repository")

    def test_subdirectory_refused(self):
        os.makedirs(os.path.join(self.repo, "src"))
        self.grant_refused(os.path.join(self.repo, "src"), "inside the repository")

    def test_symlink_path_refused(self):
        link = os.path.join(self.home, "link")
        os.symlink(self.repo, link)
        self.grant_refused(link, "symbolic link")

    def test_symlinked_parent_stores_real_path(self):
        parent = os.path.join(self.tmp, "viaparent")
        os.symlink(self.home, parent)
        self.assertEqual(self.grant(os.path.join(parent, "proj"))[0], 0)
        self.assertEqual(self.records(), [trust.record_name(self.repo)])

    def test_missing_path_refused(self):
        self.grant_refused(os.path.join(self.home, "nope"), "no such path")

    def test_temp_root_refused_end_to_end(self):
        p = Pty(["--trust-dir", self.tdir, "--home", self.home, "--temp-root", self.tmp, "grant", self.repo])
        self.assertEqual(p.finish(), 1)
        self.assertIn("temporary", p.out)
        self.assertEqual(self.records(), [])


def tool_args(case):
    import argparse
    return argparse.Namespace(trust_dir=case.tdir, home=case.home, temp_root=["/nonexistent-temp"])


class AtomicityTests(Base):
    def test_killed_at_the_prompt_leaves_nothing(self):
        p = self.tool("grant", self.repo)
        self.assertIsNotNone(p.code())
        p.kill()
        self.assertEqual(self.records(), [])

    def test_killed_leaves_existing_record_unchanged(self):
        self.assertEqual(self.grant(self.repo, "--note", "first")[0], 0)
        path = os.path.join(self.tdir, self.records()[0])
        before = Path(path).read_bytes()
        p = self.tool("grant", self.repo, "--note", "second")
        self.assertIsNotNone(p.code())
        p.kill()
        self.assertEqual(Path(path).read_bytes(), before)
        self.assertEqual(len(self.records()), 1)

    def test_sigterm_at_the_prompt_leaves_nothing(self):
        p = self.tool("grant", self.repo)
        self.assertIsNotNone(p.code())
        os.kill(p.pid, signal.SIGTERM)
        self.assertNotEqual(p.finish(), 0)
        self.assertEqual(self.records(), [])

    def test_stale_temp_files_are_swept_by_next_grant(self):
        stale = Path(self.tdir, ".grant-1-dead.tmp")
        stale.write_text("{")
        self.assertEqual(self.grant(self.repo)[0], 0)
        self.assertFalse(stale.exists())

    def run_grant_in_process(self, tool, note=None, **patches):
        args = tool_args(self)
        args.path, args.days, args.note, args.no_worktrees = self.repo, None, note, False
        with mock.patch.dict(os.environ, clean_env(), clear=True), contextlib.redirect_stdout(io.StringIO()) as out, \
                mock.patch.object(tool, "open_terminal", return_value=os.open(os.devnull, os.O_RDWR)), \
                mock.patch.object(tool, "ask_code", return_value=True):
            status = tool.cmd_grant(args, tool.Env(args))
        self.out = out.getvalue()
        return status

    def failing_step(self, tool, target):
        def step(name):
            if name == target:
                raise OSError("injected failure at " + name)
        return mock.patch.object(tool, "_step", side_effect=step)

    def only_record(self):
        return Path(self.tdir, trust.record_name(self.repo))

    BEFORE_RENAME = ("tmp_create", "tmp_write", "tmp_fsync", "read_old", "bak_write", "bak_fsync", "before_rename")

    def test_failure_before_the_rename_changes_nothing(self):
        tool = load_tool()
        for existing in (False, True):
            for step in self.BEFORE_RENAME:
                if not existing and step.startswith("bak"):
                    continue
                shutil.rmtree(self.tdir)
                os.makedirs(self.tdir, mode=0o700)
                if existing:
                    self.run_grant_in_process(tool, note="old")
                before = self.only_record().read_bytes() if existing else None
                with self.failing_step(tool, step):
                    with self.assertRaises(OSError, msg=(existing, step)):
                        self.run_grant_in_process(tool, note="new")
                self.assertEqual(self.records(), [self.only_record().name] if existing else [], (existing, step))
                if existing:
                    self.assertEqual(self.only_record().read_bytes(), before, step)
                    self.assertTrue(self.reader().trusted)

    def test_failing_rename_changes_nothing(self):
        tool = load_tool()
        for existing in (False, True):
            shutil.rmtree(self.tdir)
            os.makedirs(self.tdir, mode=0o700)
            if existing:
                self.run_grant_in_process(tool, note="old")
            before = self.only_record().read_bytes() if existing else None
            with mock.patch.object(tool.os, "rename", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    self.run_grant_in_process(tool, note="new")
            self.assertEqual(self.records(), [self.only_record().name] if existing else [])
            if existing:
                self.assertEqual(self.only_record().read_bytes(), before)

    def test_failed_directory_sync_after_the_rename_is_a_warning(self):
        tool = load_tool()
        for existing in (False, True):
            shutil.rmtree(self.tdir)
            os.makedirs(self.tdir, mode=0o700)
            if existing:
                self.run_grant_in_process(tool, note="old")
            with self.failing_step(tool, "fsync_dir"):
                self.assertEqual(self.run_grant_in_process(tool, note="new"), 0)
            self.assertIn("could not be synced", self.out)
            self.assertIn("Granted", self.out)
            self.assertEqual(json.loads(self.only_record().read_text())["note"], "new")
            self.assertEqual(self.records(), [self.only_record().name])
            self.assertTrue(self.reader().trusted)

    def test_real_fsync_failure_on_the_directory_keeps_the_new_record(self):
        tool = load_tool()
        self.run_grant_in_process(tool, note="old")
        real, calls = os.fsync, []

        def fsync(fd):
            calls.append(fd)
            if len(calls) == 3:             # temporary file, backup copy, then the directory
                raise OSError("boom")
            return real(fd)
        with mock.patch.object(tool.os, "fsync", side_effect=fsync):
            self.assertEqual(self.run_grant_in_process(tool, note="new"), 0)
        self.assertEqual(len(calls), 3)
        self.assertEqual(json.loads(self.only_record().read_text())["note"], "new")

    def test_record_the_reader_rejects_is_rolled_back(self):
        tool = load_tool()
        for existing in (False, True):
            shutil.rmtree(self.tdir)
            os.makedirs(self.tdir, mode=0o700)
            if existing:
                self.run_grant_in_process(tool, note="old")
            before = self.only_record().read_bytes() if existing else None
            for target in ("verify", None):
                real = trust.load
                calls = []

                def load(*a, **k):
                    calls.append(1)
                    return real(*a, **k) if len(calls) == 1 else trust.Result("ignored", "perms")
                with mock.patch.object(tool.trust, "load", side_effect=load) if target is None else \
                        self.failing_step(tool, "verify"):
                    with self.assertRaisesRegex(tool.Refused, "not accepted by the reader"):
                        self.run_grant_in_process(tool, note="new")
                self.assertEqual(self.records(), [self.only_record().name] if existing else [])
                if existing:
                    self.assertEqual(self.only_record().read_bytes(), before)
                    self.assertTrue(self.reader().trusted)

    def test_failed_rollback_says_where_the_old_record_is(self):
        tool = load_tool()
        self.run_grant_in_process(tool, note="old")
        with mock.patch.object(tool.trust, "load", side_effect=[trust.load(self.repo, self.tdir, home=self.home,
                               temp_roots=("/nonexistent-temp",)), trust.Result("ignored", "perms")]):
            with self.failing_step(tool, "rollback"):
                with self.assertRaisesRegex(tool.Refused, "could not be taken out.*previous record is kept at"):
                    self.run_grant_in_process(tool, note="new")
        self.assertTrue(any(n.startswith(".grant-") for n in self.records()))

    def test_failed_cleanup_of_the_copy_is_a_warning(self):
        tool = load_tool()
        self.run_grant_in_process(tool, note="old")
        with self.failing_step(tool, "cleanup"):
            self.assertEqual(self.run_grant_in_process(tool, note="new"), 0)
        self.assertIn("temporary copy", self.out)
        self.assertEqual(json.loads(self.only_record().read_text())["note"], "new")

    def test_signal_after_the_commit_point_is_held_off(self):
        tool = load_tool()
        tool._state["committed"] = True
        tool._raise_interrupted(signal.SIGTERM, None)         # does not raise
        tool._state["committed"] = False
        with self.assertRaises(tool.Interrupted):
            tool._raise_interrupted(signal.SIGTERM, None)

    def test_in_process_grant_works(self):
        tool = load_tool()
        self.assertEqual(self.run_grant_in_process(tool), 0)
        self.assertTrue(self.reader().trusted)

    def test_unacceptable_trust_directory_refuses_before_writing(self):
        os.chmod(self.tdir, 0o777)
        tool = load_tool()
        with self.assertRaisesRegex(tool.Refused, "not acceptable"):
            self.run_grant_in_process(tool)
        self.assertEqual(self.records(), [])


class RevokeTests(Base):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.grant(self.repo)[0], 0)

    def test_revoke_by_path(self):
        p = self.tool("revoke", self.repo)
        p.send((p.code() or "") + "\n")
        self.assertEqual(p.finish(), 0)
        self.assertEqual(self.records(), [])
        self.assertFalse(self.reader().trusted)

    def test_revoke_by_id(self):
        p = self.tool("revoke", self.records()[0][:8])
        p.send((p.code() or "") + "\n")
        self.assertEqual(p.finish(), 0)
        self.assertEqual(self.records(), [])

    def test_wrong_code_keeps_record(self):
        p = self.tool("revoke", self.repo)
        p.code()
        p.send("nope\n")
        self.assertEqual(p.finish(), 1)
        self.assertEqual(len(self.records()), 1)

    def test_no_terminal_keeps_record(self):
        r = self.plain("revoke", self.repo)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(self.records()), 1)

    def test_revoke_works_after_repo_deleted(self):
        rid = self.records()[0][:8]
        shutil.rmtree(self.repo)
        p = self.tool("revoke", rid)
        p.send((p.code() or "") + "\n")
        self.assertEqual(p.finish(), 0)
        self.assertEqual(self.records(), [])

    def run_revoke_in_process(self, tool):
        args = tool_args(self)
        args.target = self.repo
        with mock.patch.dict(os.environ, clean_env(), clear=True), contextlib.redirect_stdout(io.StringIO()) as out, \
                mock.patch.object(tool, "open_terminal", return_value=os.open(os.devnull, os.O_RDWR)), \
                mock.patch.object(tool, "ask_code", return_value=True):
            status = tool.cmd_revoke(args, tool.Env(args))
        self.out = out.getvalue()
        return status

    def test_failure_before_or_at_the_unlink_keeps_the_record(self):
        tool = load_tool()
        before = Path(self.tdir, self.records()[0]).read_bytes()
        with mock.patch.object(tool, "_step", side_effect=lambda n: (_ for _ in ()).throw(OSError("x"))
                               if n == "before_unlink" else None):
            with self.assertRaises(OSError):
                self.run_revoke_in_process(tool)
        with mock.patch.object(tool.os, "unlink", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                self.run_revoke_in_process(tool)
        self.assertEqual(Path(self.tdir, self.records()[0]).read_bytes(), before)
        self.assertTrue(self.reader().trusted)

    def test_failed_directory_sync_after_the_unlink_is_a_warning(self):
        tool = load_tool()
        with mock.patch.object(tool, "_step", side_effect=lambda n: (_ for _ in ()).throw(OSError("x"))
                               if n == "fsync_dir" else None):
            self.assertEqual(self.run_revoke_in_process(tool), 0)
        self.assertIn("could not be synced", self.out)
        self.assertIn("Revoked.", self.out)
        self.assertEqual(self.records(), [])

    def test_unknown_id_refuses(self):
        p = self.tool("revoke", "deadbeef")
        self.assertEqual(p.finish(), 1)
        self.assertEqual(len(self.records()), 1)

    def test_revoke_other_repo_keeps_this_record(self):
        other = self.make_repo(os.path.join(self.home, "other"))
        p = self.tool("revoke", other)
        self.assertEqual(p.finish(), 1)
        self.assertEqual(len(self.records()), 1)


class NormalisationTests(Base):
    def test_grant_from_worktree_records_main_root(self):
        wt = self.make_worktree(self.repo)
        status, out = self.grant(wt)
        self.assertEqual(status, 0, out)
        self.assertEqual(self.records(), [trust.record_name(self.repo)])
        rec = json.loads(Path(self.tdir, self.records()[0]).read_text())
        self.assertEqual(rec["root"], self.repo)
        self.assertIn("linked worktree", out)
        self.assertTrue(self.reader(wt).trusted)

    def test_forged_worktree_file_refused(self):
        fake = os.path.join(self.home, "fake")
        os.makedirs(fake)
        Path(fake, ".git").write_text("gitdir: %s/.git/worktrees/zz\n" % self.repo)
        p = self.tool("grant", fake)
        self.assertEqual(p.finish(), 1)
        self.assertEqual(self.records(), [])


class CheckTests(Base):
    def test_check_matches_the_reader(self):
        wt = self.make_worktree(self.repo)
        outside = os.path.join(self.home, "plain")
        os.makedirs(outside)
        cases = [self.repo, wt, outside]
        for before in (True, False):
            if not before:
                self.assertEqual(self.grant(self.repo)[0], 0)
            for path in cases:
                r = self.plain("check", path)
                self.assertEqual(r.returncode == 0, self.reader(path).trusted, (path, r.stdout))
        self.assertIn("TRUSTED (via worktree)", self.plain("check", wt).stdout)

    def test_check_names_the_reason(self):
        self.assertIn("no trust record", self.plain("check", self.repo).stdout)
        self.assertIn("not inside a git repository", self.plain("check", self.tmp).stdout)
        self.assertEqual(self.grant(self.repo, "--no-worktrees")[0], 0)
        wt = self.make_worktree(self.repo)
        self.assertIn("excludes linked worktrees", self.plain("check", wt).stdout)
        os.chmod(os.path.join(self.tdir, self.records()[0]), 0o666)
        out = self.plain("check", self.repo)
        self.assertEqual(out.returncode, 1)
        self.assertIn("record ignored: bad permissions", out.stdout)

    def test_check_calls_the_reader_lookup(self):
        tool = load_tool()
        args = tool_args(self)
        args.path = self.repo
        with mock.patch.object(tool.trust, "lookup", wraps=trust.lookup) as spy, \
                contextlib.redirect_stdout(io.StringIO()):
            tool.cmd_check(args, tool.Env(args))
        self.assertEqual(spy.call_count, 1)

    def test_moved_root_is_reported(self):
        self.assertEqual(self.grant(self.repo)[0], 0)
        shutil.rmtree(self.repo)
        self.make_repo(self.repo)
        out = self.plain("check", self.repo)
        self.assertEqual(out.returncode, 1)
        self.assertIn("root moved", out.stdout)


class ListShowLogTests(Base):
    def test_list_show(self):
        self.assertIn("No trust records", self.plain("list").stdout)
        self.assertEqual(self.grant(self.repo)[0], 0)
        out = self.plain("list").stdout
        self.assertIn(self.repo, out)
        self.assertIn("[trusted]", out)
        rid = self.records()[0][:8]
        self.assertIn("statement", self.plain("show", rid).stdout)
        shown = self.plain("show", self.repo).stdout
        self.assertIn("Reader verdict: trusted", shown)
        os.chmod(os.path.join(self.tdir, self.records()[0]), 0o666)
        self.assertIn("ignored: bad permissions", self.plain("list").stdout)

    def test_list_flags_copied_record_and_counts_skips(self):
        self.assertEqual(self.grant(self.repo)[0], 0)
        name = self.records()[0]
        shutil.copy(os.path.join(self.tdir, name), os.path.join(self.tdir, "f" * 32 + ".json"))
        log = os.path.join(self.home, ".config", "tmux-pi", "state", "trust")
        os.makedirs(log)
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        lines = [{"ts": ts, "event": "trust_skip", "record": name[:8]}] * 2 + \
                [{"ts": "2020-01-01T00:00:00Z", "event": "trust_skip", "record": name[:8]}]
        Path(log, "skipped.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
        out = self.plain("list").stdout
        self.assertIn("name does not match root", out)
        self.assertIn("skips(7d) 2", out)

    def test_show_judges_the_selected_file_not_the_one_the_root_names(self):
        self.assertEqual(self.grant(self.repo)[0], 0)
        shutil.copy(os.path.join(self.tdir, self.records()[0]), os.path.join(self.tdir, "f" * 32 + ".json"))
        out = self.plain("show", "ffffffff").stdout
        self.assertIn("Reader verdict: ignored: name does not match root", out)
        self.assertNotIn("Reader verdict: trusted", out)
        self.assertIn("Reader verdict: trusted", self.plain("show", self.records()[0][:8]).stdout)
        Path(self.tdir, "e" * 32 + ".json").write_text("{")
        self.assertIn("Reader verdict: ignored: unreadable record", self.plain("show", "eeeeeeee").stdout)

    def test_log_tail_and_sanitising(self):
        log = os.path.join(self.home, ".config", "tmux-pi", "state", "trust")
        os.makedirs(log)
        Path(log, "skipped.jsonl").write_text("one\ntwo\x1b[31m\nthree\n")
        out = self.plain("log", "-n", "2").stdout
        self.assertEqual(out.splitlines(), ["two?[31m", "three"])
        self.assertNotIn("\x1b", out)
        shutil.rmtree(log)
        self.assertIn("No skip log", self.plain("log").stdout)

    def test_test_options_need_trust_dir(self):
        r = subprocess.run([sys.executable, TOOL, "--home", self.home, "list"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, env=clean_env())
        self.assertEqual(r.returncode, 1)
        self.assertIn("need --trust-dir", r.stderr)


if __name__ == "__main__":
    unittest.main()
