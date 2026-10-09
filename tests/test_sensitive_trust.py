"""T7 slice 4: what the owner's trust record lifts from the sensitive-read classifier, and what it never does.

Four groups:

  1. The whole of tests/test_sensitive_read.py re-run with no trust at all (`trust=None`), with a record for an
     unrelated repository, and with a refused record for the very tree under test. Every row must give the answer
     it gives today.
  2. The guarantees of design section 8, each in a trusted fixture tree: what stops prompting, what keeps prompting,
     and that no record, a bad record, a moved root and `trust=None` give identical answers.
  3. The skip log, the guard gate, the walk, and the library callers that must not be lifted.
  4. `bin/tp-sensitive-read --trusted` end to end, and the hook and Pi wiring that passes it.

Credential names are assembled from pieces (as in test_sensitive_read.py) so this source can be searched and read.
Everything uses a temp home passed as `home=` or `--home`; nothing here reads or writes the owner's real config.
"""
import datetime
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import redact_hook  # noqa: E402
import sensitive_read as sr  # noqa: E402
import trust  # noqa: E402
import trust_guard  # noqa: E402
import trust_lift  # noqa: E402

TOOL = str(ROOT / "bin" / "tp-sensitive-read")
NOW = datetime.datetime(2026, 10, 9, 12, 0, tzinfo=datetime.timezone.utc)


def load_base():
    spec = importlib.util.spec_from_file_location("sensitive_read_table", ROOT / "tests" / "test_sensitive_read.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASE = load_base()
ENV = BASE.ENV
RSA = BASE.RSA
SSH = BASE.SSH
LOCAL = BASE.LOCAL
SESSIONS = BASE.SESSIONS
CREDS = BASE.CREDS + ".json"          # credentials.json
SECRETS = BASE.SECRETS                # secrets.json
SECRETS_YML = BASE.SECRETS_YML
PEM = "server." + "pem"
MLPACKAGE = "sec" + "ret.mlpackage"
TEST_SECRETS = "test_sec" + "rets.py"
CREDS_HELPER = BASE.CREDS + "_helper.md"
JSON_NAME = "app_sec" + "rets.json"      # weak, and a data file: the name an unknown command's operand is judged by
GIT = shutil.which("git")


# --- the fixture ------------------------------------------------------------------------------------------------

class World(unittest.TestCase):
    """A temp home with a trust directory, and a project tree the owner has vouched for."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="trust-lift-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.tdir = os.path.join(self.home, ".config", "tmux-pi", "trust")
        os.makedirs(self.tdir, mode=0o700)
        for folder in (os.path.join(self.home, ".config"), os.path.join(self.home, ".config", "tmux-pi"), self.tdir):
            os.chmod(folder, 0o700)
        self.logdir = os.path.join(self.tmp, "log")
        self.outside = os.path.join(self.tmp, "outside")
        os.makedirs(self.outside)
        for name in (MLPACKAGE, ENV, "notes.txt"):
            Path(self.outside, name).write_text("x\n")
        self.repo = self.make_tree(os.path.join(self.tmp, "proj"))

    # tree and record

    def make_tree(self, top):
        for folder in (".git", "agent/extensions", SESSIONS + "/2026", "claude/plugins", "config", "lib", "tests", "src",
                       "docs", "certs", "bin"):
            os.makedirs(os.path.join(top, folder))
        for name in ("README.md", "src/app.py", "lib/goal.py", "docs/notes.md", "config/router.json", MLPACKAGE,
                     "tests/" + TEST_SECRETS, CREDS_HELPER, LOCAL, "certs/" + PEM, ENV, RSA, "claude/plugins/p.json"):
            Path(top, name).write_text("x\n")
        return os.path.realpath(top)

    def fields(self, root, **over):
        st = os.lstat(root)
        rec = {"version": 1, "statement": trust.STATEMENT, "root": root, "root_dev": st.st_dev, "root_ino": st.st_ino,
               "worktrees": True, "granted_at": "2026-10-09T10:00:00Z", "granted_by": "uid %d" % os.getuid(),
               "expires": None, "note": "test"}
        rec.update(over)
        return rec

    def grant(self, root=None, mode=0o400, **over):
        root = root or self.repo
        path = os.path.join(self.tdir, trust.record_name(root))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(self.fields(root, **over), handle)
        os.chmod(path, mode)
        return path

    # resolvers and calls

    def resolver(self, **kw):
        kw.setdefault("home", self.home)
        kw.setdefault("temp_roots", ())
        kw.setdefault("now", NOW)
        kw.setdefault("log_dir", self.logdir)
        kw.setdefault("env", {})
        return trust_lift.Resolver(kw.pop("harness", "claude"), **kw)

    def bash(self, command, cwd=None, trust=True):
        res = self.resolver() if trust is True else trust
        return sr.classify("Bash", {"command": command}, cwd or self.repo, res)

    def plain(self, command, cwd=None):
        return sr.classify("Bash", {"command": command}, cwd or self.repo)

    def log(self):
        path = os.path.join(self.logdir, trust_lift.LOG_NAME)
        if not os.path.exists(path):
            return []
        return [json.loads(line) for line in Path(path).read_text().splitlines()]

    def git(self, *args, cwd=None):
        return subprocess.run([GIT, *args], cwd=cwd or self.repo, env=BASE.GIT_ENV, check=True, capture_output=True,
                              text=True, timeout=60).stdout


# --- 1. the existing table, re-run ------------------------------------------------------------------------------

class _Rerun:
    """Runs every test of a test_sensitive_read.py class with a resolver (or an explicit `trust=None`) behind classify."""
    mode = "none"

    def setUp(self):
        super().setUp()
        self.keep = tempfile.mkdtemp(prefix="trust-rerun-")
        self.addCleanup(shutil.rmtree, self.keep, True)
        home = os.path.join(self.keep, "home")
        tdir = os.path.join(home, ".config", "tmux-pi", "trust")
        os.makedirs(tdir, mode=0o700)
        for folder in (os.path.join(home, ".config"), os.path.join(home, ".config", "tmux-pi"), tdir):
            os.chmod(folder, 0o700)
        resolver = None
        if self.mode != "none":
            if self.mode == "unrelated":
                root = self.fixture_repo(os.path.join(self.keep, "elsewhere"))
                self.write(tdir, root)
            else:
                # a record for the very tree under test, refused: the statement is not the one the reader accepts
                self.write(tdir, BASE.CWD, statement="a different statement")
            resolver = trust_lift.Resolver("claude", home=home, temp_roots=(), now=NOW, env={},
                                           log_dir=os.path.join(self.keep, "log"))
        real_classify, real_hook = sr.classify, sr.hook_response
        patches = [
            mock.patch.object(BASE, "classify", lambda tool, params, cwd=None: real_classify(tool, params, cwd, resolver)),
            mock.patch.object(BASE, "hook_response", lambda event, cwd=None: real_hook(event, cwd, resolver)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    @staticmethod
    def fixture_repo(path):
        os.makedirs(os.path.join(path, ".git"))
        return os.path.realpath(path)

    @staticmethod
    def write(tdir, root, **over):
        st = os.lstat(root)
        rec = {"version": 1, "statement": trust.STATEMENT, "root": root, "root_dev": st.st_dev, "root_ino": st.st_ino,
               "worktrees": True, "granted_at": "2026-10-09T10:00:00Z", "granted_by": "uid %d" % os.getuid(),
               "expires": None, "note": "test"}
        rec.update(over)
        path = os.path.join(tdir, trust.record_name(root))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(rec, handle)
        os.chmod(path, 0o400)


for _mode in ("none", "unrelated", "refused"):
    for _base in (BASE.SensitiveReadTableTests, BASE.UnscopedPatchTests):
        _name = "%s_%s" % (_base.__name__, _mode)
        globals()[_name] = type(_name, (_Rerun, _base), {"mode": _mode, "__module__": __name__})
del _mode, _base, _name


# --- 2. the guarantees of section 8 -----------------------------------------------------------------------------

class LiftTests(World):
    """With a valid record, a name or a directory guess stops prompting; nothing else does."""

    def setUp(self):
        super().setUp()
        self.grant()

    def assertQuiet(self, command, cwd=None):
        result = self.bash(command, cwd)
        self.assertEqual(result, {"sensitive": False}, command)
        # and it is a lift: without the record the same command prompts
        self.assertTrue(self.plain(command, cwd)["sensitive"], command)

    def assertPrompts(self, command, cwd=None, target=None):
        result = self.bash(command, cwd)
        self.assertTrue(result["sensitive"], command)
        self.assertEqual(result, self.plain(command, cwd) if target is None else result, command)
        if target is not None:
            self.assertIn(target, result["target"], command)
        return result

    def test_weak_names_stop_prompting(self):
        for command in ("cat " + MLPACKAGE, "cat tests/" + TEST_SECRETS, "cat " + CREDS_HELPER,
                        "head -n 3 " + MLPACKAGE, "cat ./" + MLPACKAGE, "cat " + os.path.join(self.repo, MLPACKAGE),
                        "cat " + MLPACKAGE + " " + CREDS_HELPER, "sed -n 1p tests/" + TEST_SECRETS):
            with self.subTest(command=command):
                self.assertQuiet(command)

    def test_names_that_do_not_prompt_today_still_do_not(self):
        for command in ("python3 tests/" + TEST_SECRETS, "wc -l " + CREDS_HELPER, "ls " + MLPACKAGE):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command), self.plain(command))

    def test_tools_stop_prompting(self):
        for tool, params in (("Read", {"file_path": MLPACKAGE}), ("Read", {"file_path": "tests/" + TEST_SECRETS}),
                             ("Read", {"file_path": os.path.join(self.repo, CREDS_HELPER)})):
            with self.subTest(tool=tool, params=params):
                self.assertEqual(sr.classify(tool, params, self.repo, self.resolver()), {"sensitive": False})
                self.assertTrue(sr.classify(tool, params, self.repo)["sensitive"])

    def test_directory_guess_stops_prompting_when_the_walk_is_clean(self):
        for command in ("rg foo claude", "grep -rn foo claude", "rg -n foo ./claude", "grep -r foo " + os.path.join(self.repo, "claude")):
            with self.subTest(command=command):
                self.assertQuiet(command)
        # the Grep tool is not judged by directory name today, so nothing changes for it
        params = {"pattern": "x", "path": "claude"}
        self.assertEqual(sr.classify("Grep", params, self.repo, self.resolver()), sr.classify("Grep", params, self.repo))

    def test_directory_guess_keeps_prompting_when_the_walk_reaches_a_credential_location(self):
        for command in ("rg foo agent", "grep -rn foo agent", "rg foo config", "grep -rn foo config"):
            with self.subTest(command=command):
                result = self.bash(command)
                self.assertTrue(result["sensitive"], result)
                self.assertEqual(result["target"], self.plain(command)["target"])
        self.assertIn("reaches " + SESSIONS, self.bash("rg foo agent")["reason"])
        self.assertIn("reaches " + LOCAL, self.bash("rg foo config")["reason"])

    def test_still_prompting(self):
        for command in ("cat " + ENV, "cat certs/" + PEM, "cat " + RSA, "cat " + LOCAL, "cat .git/config",
                        "cat ~/" + SSH + "/x", "cat " + os.path.join(self.repo, SESSIONS, "2026", "x.jsonl"),
                        "cat " + CREDS, "cat " + SECRETS, "cat " + SECRETS_YML, "cat ." + CREDS, "cat .git-credentials",
                        "cat client_secret_1.json", "cat prod" + ENV, "cat " + ENV + ".local", "ls ~/" + SSH):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command), self.plain(command))
                self.assertTrue(self.bash(command)["sensitive"], command)

    def test_sessions_directory_listing_is_as_today(self):
        for command in ("ls " + SESSIONS, "ls " + SESSIONS + "/2026", "find " + SESSIONS):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command), self.plain(command))

    def test_exact_credential_names_with_weak_company_still_prompt(self):
        for name in (CREDS, "credentials", SECRETS, SECRETS_YML, "secrets.toml", ".credentials.json", ".git-credentials",
                     "client_secret_abc.json", "prod.tfvars", "prod" + ENV):
            with self.subTest(name=name):
                Path(self.repo, name).write_text("x\n")
                alone = self.plain("cat " + name)
                self.assertEqual(self.bash("cat " + name), alone)
                self.assertEqual(self.bash("cat " + name + " " + MLPACKAGE)["sensitive"], alone["sensitive"])
                read = {"file_path": name}
                self.assertEqual(sr.classify("Read", read, self.repo, self.resolver()), sr.classify("Read", read, self.repo))
        for name in (CREDS, SECRETS, SECRETS_YML, ".credentials.json", "client_secret_abc.json", "prod" + ENV):
            self.assertTrue(self.bash("cat " + name)["sensitive"], name)

    def test_secrets_directory_is_not_lifted(self):
        os.makedirs(os.path.join(self.repo, "secrets"))
        Path(self.repo, "secrets", "a.txt").write_text("x\n")
        for command in ("cat secrets/a.txt", "ls secrets", "cat secrets", "cat .secrets", "rg foo secrets"):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command), self.plain(command))

    def test_globs_are_never_lifted(self):
        for command in ("cat *secret*", "cat sec*", "cat tests/*secrets*", "cat ./*.mlpackage"):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command), self.plain(command))

    def test_paths_outside_the_tree_prompt(self):
        outside = os.path.join(self.outside, MLPACKAGE)
        self.assertTrue(self.bash("cat " + outside)["sensitive"])
        self.assertEqual(self.bash("cat " + outside), self.plain("cat " + outside))
        self.assertEqual(self.bash("cat " + MLPACKAGE, cwd=self.outside), self.plain("cat " + MLPACKAGE, cwd=self.outside))
        self.assertTrue(self.bash("cat " + MLPACKAGE, cwd=self.outside)["sensitive"])
        self.assertEqual(sr.classify("Read", {"file_path": outside}, self.repo, self.resolver())["sensitive"], True)
        self.assertEqual(sr.classify("Read", {"file_path": MLPACKAGE}, self.outside, self.resolver())["sensitive"], True)

    def test_one_outside_target_keeps_a_mixed_call_prompting(self):
        outside = os.path.join(self.outside, MLPACKAGE)
        for command in ("cat %s %s" % (MLPACKAGE, outside), "cat %s %s" % (outside, MLPACKAGE),
                        "cat %s %s %s" % (MLPACKAGE, CREDS_HELPER, outside), "cat " + MLPACKAGE + " /tmp/../etc/" + CREDS_HELPER):
            with self.subTest(command=command):
                self.assertTrue(self.bash(command)["sensitive"])
        self.assertIn("outside", self.bash("cat %s %s" % (MLPACKAGE, outside))["target"])
        # and a strong name beside a weak one
        self.assertTrue(self.bash("cat %s %s" % (MLPACKAGE, ENV))["sensitive"])
        self.assertEqual(self.bash("cat %s %s" % (MLPACKAGE, ENV))["target"], self.plain("cat %s %s" % (MLPACKAGE, ENV))["target"])

    def test_symlink_out_of_the_tree_prompts(self):
        os.symlink(os.path.join(self.outside, MLPACKAGE), os.path.join(self.repo, "link.mlpackage"))
        os.symlink(os.path.join(self.outside, "notes.txt"), os.path.join(self.repo, "notes-link"))
        self.assertTrue(self.bash("cat link.mlpackage")["sensitive"])
        self.assertEqual(self.bash("cat link.mlpackage"), self.plain("cat link.mlpackage"))
        self.assertTrue(sr.classify("Read", {"file_path": "link.mlpackage"}, self.repo, self.resolver())["sensitive"])
        # a symlink inside the tree to a file inside the tree is the file it names, and that file is covered
        os.symlink(os.path.join(self.repo, MLPACKAGE), os.path.join(self.repo, "in-tree-link"))
        self.assertQuiet("cat in-tree-link")

    def test_symlinked_directory_guess_is_as_today(self):
        shutil.rmtree(os.path.join(self.repo, "claude"))
        os.makedirs(os.path.join(self.outside, "plain"))
        os.symlink(os.path.join(self.outside, "plain"), os.path.join(self.repo, "claude"))
        for command in ("rg foo claude", "grep -rn foo claude", "grep -Rn foo claude", "rg -L foo claude"):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command), self.plain(command))

    def test_walk_follows_links_only_where_the_command_does(self):
        Path(self.outside, "plain").mkdir()
        os.symlink(os.path.join(self.outside, "plain"), os.path.join(self.repo, "claude", "out"))
        for command, prompts in (("grep -rn foo claude", False), ("grep -Rn foo claude", True), ("rg foo claude", False),
                                 ("rg -L foo claude", True), ("rg --follow foo claude", True)):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command)["sensitive"], prompts, command)

    def test_walk_honours_a_literal_exclude(self):
        os.makedirs(os.path.join(self.repo, "claude", "keys"))
        Path(self.repo, "claude", "keys", PEM).write_text("x\n")
        self.assertTrue(self.bash("grep -rn foo claude")["sensitive"])
        self.assertTrue(self.bash("rg foo claude")["sensitive"])
        self.assertEqual(self.bash("grep -rn --exclude-dir=keys foo claude"), {"sensitive": False})

    def test_walk_finds_what_a_search_could_read(self):
        for hidden, name in (("nested .git", ".git/config"), ("env file", ENV), ("key", "id." + "pem"), ("ssh", SSH + "/k"),
                             ("sessions", SESSIONS + "/x"), ("exact", CREDS), ("secrets dir", "secrets/a"),
                             ("hidden secrets dir", ".secrets/a"), ("credentials dir", "credentials/a"),
                             ("client secret", "client_secret_1.json"), ("secrets file", SECRETS_YML),
                             ("local config", LOCAL), ("env file variant", "prod" + ENV)):
            with self.subTest(hidden=hidden):
                path = os.path.join(self.repo, "claude", "deep", name)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                Path(path).write_text("x\n")
                self.assertTrue(self.bash("rg foo claude")["sensitive"], name)
                self.assertTrue(self.bash("grep -rn foo claude")["sensitive"], name)
                shutil.rmtree(os.path.join(self.repo, "claude", "deep"))
        self.assertEqual(self.bash("rg foo claude"), {"sensitive": False})

    def test_walk_over_budget_prompts(self):
        for index in range(30):
            Path(self.repo, "claude", "f%d.txt" % index).write_text("x\n")
        self.assertEqual(self.bash("rg foo claude"), {"sensitive": False})
        with mock.patch.object(sr, "WALK_ENTRIES", 10):
            self.assertTrue(self.bash("rg foo claude")["sensitive"])
            self.assertTrue(self.bash("grep -rn foo claude")["sensitive"])
        with mock.patch.object(sr, "WALK_SECONDS", -1.0):
            self.assertTrue(self.bash("rg foo claude")["sensitive"])

    def test_pathspec_guess_is_never_lifted(self):
        for command in ("git grep foo claude", "git grep -n foo -- claude", "git log -p -- claude"):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command), self.plain(command))

    def test_inline_name_text_prompts(self):
        for command in ("python3 -c \"open('" + ENV + "')\"", "node -e \"require('fs').readFileSync('" + RSA + "')\"",
                        "python3 -c \"open('~/" + SSH + "/id')\"", "echo " + SSH):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command), self.plain(command))
        self.assertEqual(self.bash("python3 -c \"open('" + ENV + "')\"")["sensitive"], True)

    def test_inline_literal_weak_name_is_a_name_like_any_other(self):
        # a literal path in an inline script is judged as the path it names: a covered weak name is lifted,
        # a name outside the tree is not
        command = "python3 -c \"open('" + MLPACKAGE + "')\""
        self.assertTrue(self.plain(command)["sensitive"])
        self.assertEqual(self.bash(command), {"sensitive": False})
        outside = "python3 -c \"open('" + os.path.join(self.outside, MLPACKAGE) + "')\""
        self.assertEqual(self.bash(outside), self.plain(outside))
        self.assertTrue(self.bash(outside)["sensitive"])

    def test_not_a_git_tree_is_not_lifted(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        Path(plain, MLPACKAGE).write_text("x\n")
        self.assertEqual(self.bash("cat " + MLPACKAGE, cwd=plain), self.plain("cat " + MLPACKAGE, cwd=plain))
        self.assertTrue(self.bash("cat " + MLPACKAGE, cwd=plain)["sensitive"])

    def test_a_subdirectory_of_the_tree_is_covered(self):
        self.assertEqual(self.bash("cat ../" + MLPACKAGE, cwd=os.path.join(self.repo, "src")), {"sensitive": False})
        self.assertEqual(self.bash("cat " + TEST_SECRETS, cwd=os.path.join(self.repo, "tests")), {"sensitive": False})
        self.assertEqual(self.bash("rg foo ../claude", cwd=os.path.join(self.repo, "src")), {"sensitive": False})

    def test_a_nested_repository_is_its_own_tree(self):
        inner = self.make_tree(os.path.join(self.repo, "vendor"))
        self.assertEqual(self.bash("cat vendor/" + MLPACKAGE), self.plain("cat vendor/" + MLPACKAGE))
        self.assertTrue(self.bash("cat vendor/" + MLPACKAGE)["sensitive"])
        self.assertTrue(inner)



@unittest.skipUnless(GIT, "git is not installed")
class GitListingTests(World):
    """A names-only git query lists weak names: lifted. Everything unproven keeps prompting."""

    def setUp(self):
        super().setUp()
        shutil.rmtree(os.path.join(self.repo, ".git"))
        # keep the credential-looking files out of the repository's history: the fixture is about listings
        for name in (ENV, RSA, LOCAL, "certs/" + PEM):
            os.unlink(os.path.join(self.repo, name))
        self.git("init", "-q", "-b", "main", ".")
        self.git("add", "-f", ".")
        self.git("commit", "-q", "-m", "first")
        self.grant()

    def test_a_linked_worktree_inherits_by_default(self):
        work = os.path.join(self.tmp, "wt")
        self.git("worktree", "add", "-q", work)
        work = os.path.realpath(work)
        self.assertEqual(self.bash("cat " + MLPACKAGE, cwd=work), {"sensitive": False})
        line = self.log()[-1]
        self.assertEqual((line["via"], line["worktree"]), ("worktree", work))
        self.assertEqual(line["root"], self.repo)

    def test_a_worktree_holding_credential_files_is_not_lifted(self):
        work = os.path.join(self.tmp, "wt2")
        self.git("worktree", "add", "-q", work)
        Path(work, LOCAL).parent.mkdir(exist_ok=True)
        Path(work, LOCAL).write_text("x\n")
        self.assertEqual(self.bash("cat " + MLPACKAGE, cwd=work), self.plain("cat " + MLPACKAGE, cwd=work))
        self.assertTrue(self.bash("cat " + MLPACKAGE, cwd=work)["sensitive"])

    def change(self, *names):
        for name in names:
            with open(os.path.join(self.repo, name), "a") as handle:
                handle.write("more\n")

    def test_weak_names_in_a_diff_stop_prompting(self):
        self.change(MLPACKAGE, "tests/" + TEST_SECRETS)
        for command in ("git diff", "git log -p -1", "git log -p -- tests/" + TEST_SECRETS):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command), {"sensitive": False}, command)
        self.assertTrue(self.plain("git diff")["sensitive"])

    def test_a_listed_credential_still_prompts(self):
        self.change(MLPACKAGE)
        Path(self.repo, LOCAL).parent.mkdir(exist_ok=True)
        Path(self.repo, LOCAL).write_text("x\n")
        self.git("add", "-f", LOCAL)
        self.git("commit", "-q", "-m", "second")
        self.change(LOCAL)
        self.assertTrue(self.bash("git diff")["sensitive"])
        self.assertEqual(self.bash("git diff")["target"], self.plain("git diff")["target"])

    def test_unproven_git_cases_prompt(self):
        self.change(MLPACKAGE)
        folder = Path(tempfile.mkdtemp(prefix="trust-fake-git-"))
        self.addCleanup(shutil.rmtree, str(folder), True)
        script = folder / "git"
        script.write_text("#!/bin/sh\nexit 1\n")
        script.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(folder) + os.pathsep + os.environ["PATH"]}):
            self.assertTrue(self.bash("git diff")["sensitive"])
            self.assertEqual(self.bash("git diff"), self.plain("git diff"))

    def test_a_gitlink_prompts(self):
        self.change(MLPACKAGE)
        sha = self.git("rev-parse", "HEAD").strip()
        self.git("update-index", "--add", "--cacheinfo", "160000,%s,vendor/sub" % sha)
        self.assertEqual(self.bash("git diff --cached"), self.plain("git diff --cached"))
        # the gitlink is unproven, so the call prompts and the weak name beside it is not what the prompt is about
        result = self.bash("git diff")
        self.assertTrue(result["sensitive"])
        self.assertIn("vendor/sub", result["target"])
        self.assertIn("submodule", result["reason"])
        self.assertEqual(self.log(), [])

    # git's own top level decides whether a listed name is in the tree (T7 3.3: core.worktree, includes, .git files)

    def point_git_at_outside(self, how):
        """Make git read its files from `self.outside`, which the record does not cover."""
        if how == "core.worktree":
            self.git("config", "core.worktree", self.outside)
        else:   # a `.git` file that points at a git directory kept elsewhere, whose own config names the work tree
            kept = os.path.join(self.tmp, "kept.git")
            shutil.move(os.path.join(self.repo, ".git"), kept)
            Path(self.repo, ".git").write_text("gitdir: %s\n" % kept)
            self.git("config", "--file", os.path.join(kept, "config"), "core.worktree", self.outside)

    def outside_listing_prompts(self, how):
        Path(self.outside, MLPACKAGE).write_text("more\n")
        self.point_git_at_outside(how)
        top = self.git("rev-parse", "--show-toplevel").strip()
        self.assertEqual(os.path.realpath(top), self.outside)       # the fixture really moves git's top level
        self.assertTrue(self.plain("git diff")["sensitive"])
        for command in ("git diff", "git log -p -1", "git diff --cached", "git diff HEAD"):
            with self.subTest(how=how, command=command):
                self.assertEqual(self.bash(command), self.plain(command))
                self.assertEqual(self.log(), [])

    def test_git_core_worktree_outside_the_tree_is_not_lifted(self):
        self.outside_listing_prompts("core.worktree")

    def test_git_file_pointing_elsewhere_is_not_lifted(self):
        self.outside_listing_prompts("gitfile")

    def test_stash_listing_outside_the_tree_is_not_lifted(self):
        self.change(MLPACKAGE)
        self.git("stash", "push", "-q")
        self.point_git_at_outside("core.worktree")
        command = "git stash show -p"
        self.assertEqual(self.bash(command), self.plain(command))
        self.assertTrue(self.bash(command)["sensitive"])
        self.assertEqual(self.log(), [])

    def test_the_same_listing_is_lifted_when_git_reads_the_tree(self):
        self.change(MLPACKAGE)
        self.git("config", "core.worktree", self.repo)      # set, and pointing at the tree itself: nothing moved
        self.assertEqual(self.bash("git diff"), {"sensitive": False})
        self.assertEqual(len(self.log()), 1)


# --- where a path really leads ----------------------------------------------------------------------------------

class WrittenPathTests(World):
    """A lift is decided on the path the kernel opens (links followed, `..` applied after each), not on its text.

    `linkout` is a link inside the tree to a directory outside it, so `linkout/..` is the outside directory and
    `linkout/../NAME` is a file there, although the text folds to `NAME` in the tree.
    """

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.outside, "sub"))
        os.makedirs(os.path.join(self.outside, "claude"))
        Path(self.outside, "claude", "a.txt").write_text("x\n")
        for folder in (self.outside, self.repo):
            Path(folder, JSON_NAME).write_text("x\n")
        os.symlink(os.path.join(self.outside, "sub"), os.path.join(self.repo, "linkout"))
        os.symlink(os.path.join(self.repo, "src"), os.path.join(self.repo, "linkin"))
        self.grant()

    # every entry point of a lift: (label, tool, params). `{p}` is the written path of the weak name,
    # `{d}` the written path of the directory guess.
    NAMES = (
        ("Read tool, file_path", "Read", {"file_path": "{p}"}),
        ("shell word of a reader (cat)", "Bash", {"command": "cat {p}"}),
        ("shell word of a reader (head)", "Bash", {"command": "head -n 3 {p}"}),
        ("redirect", "Bash", {"command": "cat < {p}"}),
        ("operand of a command not known to read (_weak_file)", "Bash", {"command": "mytool --in {q}"}),
        ("inline script literal", "Bash", {"command": "python3 -c \"open('{p}')\""}),
        ("search root that is a weak name (_layout, _named)", "Bash", {"command": "rg foo {p}"}),
        ("Grep tool search root", "Grep", {"pattern": "x", "path": "{p}", "output_mode": "content"}),
        ("pi grep tool search root", "grep", {"pattern": "x", "path": "{p}"}),
    )
    DIRS = (
        ("shell search root (rg)", "Bash", {"command": "rg foo {d}"}),
        ("shell search root (grep -r)", "Bash", {"command": "grep -rn foo {d}"}),
        ("Grep tool search root", "Grep", {"pattern": "x", "path": "{d}", "output_mode": "content"}),
        ("pi grep tool search root", "grep", {"pattern": "x", "path": "{d}"}),
    )

    def calls(self, table, **written):
        for label, tool, params in table:
            yield label, tool, {key: value.format(**written) for key, value in params.items()}

    def judge(self, tool, params):
        return (sr.classify(tool, params, self.repo, self.resolver()), sr.classify(tool, params, self.repo))

    def names(self, prefix=""):
        return self.calls(self.NAMES, p=prefix + MLPACKAGE, q=prefix + JSON_NAME)

    def test_every_entry_point_lifts_when_the_path_is_plain(self):
        # the control for the tests below: written plainly, each entry point is a prompt that the record lifts
        for label, tool, params in list(self.names()) + list(self.calls(self.DIRS, d="claude")):
            with self.subTest(entry=label):
                trusted, plain = self.judge(tool, params)
                self.assertTrue(plain["sensitive"], label)
                self.assertEqual(trusted, {"sensitive": False}, label)

    def test_a_path_that_really_ends_outside_the_tree_is_not_lifted(self):
        written = "linkout/../"        # folds to the covered file; opens the one outside the tree
        for label, tool, params in self.names(written):
            with self.subTest(entry=label):
                trusted, plain = self.judge(tool, params)
                self.assertEqual(trusted, plain)
        for label, tool, params in self.calls(self.DIRS, d="linkout/../claude"):
            with self.subTest(entry=label):
                trusted, plain = self.judge(tool, params)
                self.assertEqual(trusted, plain)
                self.assertTrue(plain["sensitive"], label)
        self.assertEqual(self.log(), [])
        # and without a record every one of them is a prompt, so the equality above is "prompts as today"
        for label, tool, params in self.names(written):
            self.assertTrue(sr.classify(tool, params, self.repo)["sensitive"], label)

    def test_a_longer_way_out_and_a_relative_working_directory(self):
        for cwd, written in ((self.repo, "src/../linkout/../" + MLPACKAGE), (os.path.join(self.repo, "src"), "../linkout/../" + MLPACKAGE),
                             (self.repo, "./linkout/./../" + MLPACKAGE), (self.repo, os.path.join(self.repo, "linkout", "..", MLPACKAGE))):
            with self.subTest(cwd=cwd, written=written):
                command = "cat " + written
                self.assertEqual(sr.classify("Bash", {"command": command}, cwd, self.resolver()),
                                 sr.classify("Bash", {"command": command}, cwd))
                self.assertTrue(sr.classify("Bash", {"command": command}, cwd)["sensitive"])

    def test_a_physical_cd_through_a_link_is_judged_where_it_lands(self):
        # `cd -P linkout/..` lands in the outside directory; a plain `cd linkout/..` folds back into the tree.
        for command in ("cd -P linkout/.. && cat " + MLPACKAGE, "cd -P linkout && cat ../" + MLPACKAGE):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command), self.plain(command))
                self.assertTrue(self.bash(command)["sensitive"])
        self.assertFalse(self.bash("cd linkout/.. && cat " + MLPACKAGE)["sensitive"])
        self.assertFalse(self.bash("cd -P src/.. && cat " + MLPACKAGE)["sensitive"])

    def test_a_path_that_leaves_through_a_link_and_ends_inside_the_tree_may_lift(self):
        # `linkin` leads inside the tree, so `linkin/..` is the tree and the text means what it says
        for label, tool, params in self.names("linkin/../"):
            with self.subTest(entry=label):
                trusted, plain = self.judge(tool, params)
                self.assertEqual(trusted, {"sensitive": False})
                self.assertTrue(plain["sensitive"])
        # out through `linkout` and back to the tree: the file opened is the covered one
        trusted, plain = self.judge("Read", {"file_path": "linkout/../../proj/" + MLPACKAGE})
        self.assertEqual((trusted, plain["sensitive"]), ({"sensitive": False}, True))

    def test_a_directory_guess_through_an_inner_link_still_walks(self):
        # the same words, but the walk is of the directory the kernel opens
        for label, tool, params in self.calls(self.DIRS, d="linkin/../claude"):
            with self.subTest(entry=label):
                trusted, plain = self.judge(tool, params)
                self.assertEqual(trusted, {"sensitive": False})
                self.assertTrue(plain["sensitive"])
        os.makedirs(os.path.join(self.repo, "claude", SSH))
        self.assertTrue(sr.classify("Bash", {"command": "rg foo linkin/../claude"}, self.repo, self.resolver())["sensitive"])

    def test_a_link_to_a_weak_name_outside_is_as_today(self):
        os.symlink(os.path.join(self.outside, MLPACKAGE), os.path.join(self.repo, "pkg-link"))
        for label, tool, params in self.names("pkg-link/../"):
            with self.subTest(entry=label):
                trusted, plain = self.judge(tool, params)
                self.assertEqual(trusted, plain)

    def test_a_strong_name_at_the_end_of_a_link_is_not_lifted(self):
        os.symlink(os.path.join(self.repo, ENV), os.path.join(self.repo, "pkg." + "mlpackage"))
        for tool, params in (("Read", {"file_path": "pkg.mlpackage"}), ("Bash", {"command": "cat pkg.mlpackage"})):
            trusted, plain = self.judge(tool, params)
            self.assertEqual(trusted, plain)

    def test_the_trusted_answer_for_plain_paths_is_unchanged(self):
        # `..` folds the same everywhere when nothing on the way is a link, so ordinary paths still lift
        for written in ("src/../" + MLPACKAGE, "./" + MLPACKAGE, "tests/../tests/" + TEST_SECRETS, "nothere/../" + MLPACKAGE):
            with self.subTest(written=written):
                self.assertEqual(self.judge("Read", {"file_path": written})[0], {"sensitive": False})
                self.assertEqual(self.judge("Bash", {"command": "cat " + written})[0], {"sensitive": False})
        self.assertEqual(self.judge("Bash", {"command": "rg foo src/../claude"})[0], {"sensitive": False})


# --- what the skip log says it would have prompted --------------------------------------------------------------

@unittest.skipUnless(GIT, "git is not installed")
class WouldHaveTests(World):
    """`would_have` is the reason the same call gives without a record, taken from the classifier itself."""

    def setUp(self):
        super().setUp()
        shutil.rmtree(os.path.join(self.repo, ".git"))
        for name in (ENV, RSA, LOCAL, "certs/" + PEM):
            os.unlink(os.path.join(self.repo, name))
        self.git("init", "-q", "-b", "main", ".")
        self.git("add", "-f", ".")
        self.git("commit", "-q", "-m", "first")
        self.grant()
        for name in (MLPACKAGE, "tests/" + TEST_SECRETS):
            with open(os.path.join(self.repo, name), "a") as handle:
                handle.write("more\n")

    CASES = (
        ("shell reader", "Bash", {"command": "cat " + MLPACKAGE}),
        ("shell reader, second family", "Bash", {"command": "head -n 2 tests/" + TEST_SECRETS}),
        ("two lifted words", "Bash", {"command": "cat %s %s" % (MLPACKAGE, CREDS_HELPER)}),
        ("redirect", "Bash", {"command": "cat < " + MLPACKAGE}),
        ("operand of an unknown command", "Bash", {"command": "mytool --in " + JSON_NAME}),
        ("inline literal", "Bash", {"command": "python3 -c \"open('" + MLPACKAGE + "')\""}),
        ("search for a directory guess (rg)", "Bash", {"command": "rg foo claude"}),
        ("search for a directory guess (grep -r)", "Bash", {"command": "grep -rn foo claude"}),
        ("archive of a directory guess", "Bash", {"command": "zip -r x.zip claude"}),
        ("copy of a directory guess", "Bash", {"command": "cp -r claude /tmp/x"}),
        ("Read tool", "Read", {"file_path": MLPACKAGE}),
        ("Grep tool, directory guess", "Grep", {"pattern": "x", "path": "claude", "output_mode": "content"}),
        ("pi grep tool, directory guess", "grep", {"pattern": "x", "path": "claude"}),
        ("git diff listing", "Bash", {"command": "git diff"}),
        ("git log listing", "Bash", {"command": "git log -p -1"}),
    )

    def test_each_lifted_call_logs_the_reason_it_would_have_given(self):
        lifted = 0
        for label, tool, params in self.CASES:
            with self.subTest(case=label):
                before = len(self.log())
                trusted = sr.classify(tool, params, self.repo, self.resolver())
                plain = sr.classify(tool, params, self.repo)
                if trusted["sensitive"]:
                    self.assertEqual(self.log()[before:], [], label)       # still a prompt: nothing logged
                    continue
                self.assertTrue(plain["sensitive"], label)
                lines = self.log()[before:]
                self.assertEqual(len(lines), 1, label)
                self.assertTrue(lines[0]["lifted"], label)
                for item in lines[0]["lifted"]:
                    self.assertEqual(item["would_have"], plain["reason"], label)
                lifted += 1
        self.assertGreaterEqual(lifted, 10)        # the table is mostly lifts; a handful may stay prompts

    def test_the_reasons_are_the_prompts_own_words(self):
        # the wording differs from one family to the next, which is the point: no second copy of it
        reasons = {}
        for label, tool, params in self.CASES:
            res = self.resolver()
            if not sr.classify(tool, params, self.repo, res)["sensitive"]:
                reasons[label] = self.log()[-1]["lifted"][0]["would_have"]
        self.assertEqual(reasons["shell reader"], "sensitive path in shell command")
        self.assertIn("search root", reasons["search for a directory guess (rg)"])
        self.assertEqual(reasons["Read tool"], "credential-bearing or sensitive configuration path")
        self.assertEqual(reasons["git diff listing"], "patch output covers a credential-bearing path")

    def test_a_call_that_would_not_have_prompted_logs_nothing(self):
        res = self.resolver()
        res.begin("Bash", {"command": "echo"}, self.repo)
        res.note("L1", os.path.join(self.repo, MLPACKAGE), "something")
        res.finish(False, lambda: None)
        self.assertEqual(self.log(), [])


# --- identical answers when nothing is lifted -------------------------------------------------------------------

class IdenticalTests(World):
    """No record, a refused record, a moved root and `trust=None` all give today's answer, byte for byte."""

    COMMANDS = ("cat " + MLPACKAGE, "cat tests/" + TEST_SECRETS, "cat " + CREDS_HELPER, "rg foo claude", "grep -rn foo agent",
                "cat " + ENV, "cat certs/" + PEM, "cat " + LOCAL, "cat %s %s" % (MLPACKAGE, ENV), "ls", "git status",
                "python3 -c \"open('" + ENV + "')\"", "cat *secret*", "echo hi", "cat src/app.py")
    TOOLS = (("Read", {"file_path": MLPACKAGE}), ("Read", {"file_path": "src/app.py"}), ("Grep", {"pattern": "x", "path": "claude"}),
             ("Read", {"file_path": ENV}))

    def same(self, resolver):
        for command in self.COMMANDS:
            with self.subTest(command=command):
                self.assertEqual(json.dumps(self.bash(command, trust=resolver), sort_keys=True),
                                 json.dumps(self.plain(command), sort_keys=True))
                event = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": self.repo}
                self.assertEqual(sr.hook_response(event, None, resolver), sr.hook_response(event))
        for tool, params in self.TOOLS:
            with self.subTest(tool=tool, params=params):
                self.assertEqual(json.dumps(sr.classify(tool, params, self.repo, resolver), sort_keys=True),
                                 json.dumps(sr.classify(tool, params, self.repo), sort_keys=True))

    def test_trust_none(self):
        self.grant()
        self.same(None)

    def test_no_record(self):
        self.same(self.resolver())
        self.assertEqual(self.log(), [])

    def test_unrelated_record(self):
        other = self.make_tree(os.path.join(self.tmp, "other"))
        self.grant(other)
        self.same(self.resolver())

    def test_refused_records(self):
        for code, over in (("statement", {"statement": "no"}), ("version", {"version": 2}),
                           ("root_moved", {"root_ino": 1}), ("expired", {"expires": "2026-10-01T00:00:00Z"}),
                           ("fields", {"worktrees": "yes"})):
            with self.subTest(code=code):
                path = self.grant(**over)
                self.same(self.resolver())
                os.chmod(path, 0o600)
                os.unlink(path)
        for mode in (0o664, 0o666, 0o620, 0o602):
            with self.subTest(mode=oct(mode)):
                path = self.grant(mode=mode)
                self.same(self.resolver())
                os.unlink(path)

    def test_moved_root(self):
        self.grant()
        moved = self.repo + "-moved"
        os.rename(self.repo, moved)
        old, self.repo = self.repo, os.path.realpath(moved)
        try:
            self.same(self.resolver())
        finally:
            self.repo = old

    def test_replaced_root(self):
        self.grant()
        shutil.rmtree(self.repo)
        self.make_tree(self.repo)
        self.same(self.resolver())

    def test_guard_refusal_is_identical(self):
        self.grant()
        for kind in (trust_guard.ASK, trust_guard.DENY):
            guard = lambda *a, **k: trust_guard.Answer(kind, "no")  # noqa: E731
            self.same(self.resolver(guard=guard))


# --- differential over the existing corpus ----------------------------------------------------------------------

class DifferentialTests(unittest.TestCase):
    """Over every command of test_sensitive_read.py in a trusted work tree: nothing newly prompts, and every change is a lift."""

    def test_corpus(self):
        keep = tempfile.mkdtemp(prefix="trust-diff-")
        self.addCleanup(shutil.rmtree, keep, True)
        home = os.path.join(keep, "home")
        tdir = os.path.join(home, ".config", "tmux-pi", "trust")
        os.makedirs(tdir, mode=0o700)
        for folder in (os.path.join(home, ".config"), os.path.join(home, ".config", "tmux-pi"), tdir):
            os.chmod(folder, 0o700)
        _Rerun.write(tdir, BASE.CWD)
        resolver = trust_lift.Resolver("claude", home=home, temp_roots=(), now=NOW, env={}, log_dir=os.path.join(keep, "log"))
        corpus = list(BASE.MUST_PASS) + list(BASE.KNOWN_LIMITS) + list(BASE.MUST_PROMPT)
        changed = []
        for command in corpus:
            before = sr.classify("Bash", {"command": command}, BASE.CWD)
            after = sr.classify("Bash", {"command": command}, BASE.CWD, resolver)
            if before["sensitive"] is False:
                self.assertEqual(after, before, command)
            elif after != before:
                changed.append((command, before, after))
        for command, before, after in changed:
            with self.subTest(command=command[:80]):
                if after["sensitive"]:
                    # still prompting, for the same kind of reason, only more specific
                    self.assertEqual(before["target"], after["target"])
                else:
                    self.assertIn(before["reason"], ("sensitive path in shell command", sr._BULK_REASON,
                                                    "credential-bearing or sensitive configuration path",
                                                    "search root is a directory known to hold credentials and may include credential files",
                                                    "patch output covers a credential-bearing path"))
        self.assertGreater(len(changed), 0)
        skipped = [json.loads(line) for line in Path(keep, "log", trust_lift.LOG_NAME).read_text().splitlines()]
        self.assertTrue(all(line["event"] == "trust_skip" for line in skipped))
        self.assertTrue(all(item["rule"] in ("L1", "L2") for line in skipped for item in line["lifted"]))


# --- the skip log -----------------------------------------------------------------------------------------------

class LogTests(World):
    def setUp(self):
        super().setUp()
        self.grant()

    def test_one_line_per_lifted_call(self):
        res = self.resolver(env={"TP_TASK_ID": "task-7"})
        self.assertEqual(self.bash("cat %s %s" % (MLPACKAGE, CREDS_HELPER), trust=res), {"sensitive": False})
        self.assertEqual(self.bash("rg foo claude", trust=res), {"sensitive": False})
        lines = self.log()
        self.assertEqual(len(lines), 2)
        first, second = lines
        self.assertEqual((first["event"], first["harness"], first["task"], first["tool"]), ("trust_skip", "claude", "task-7", "Bash"))
        self.assertEqual(first["lifted_total"], 2)
        self.assertEqual([item["rule"] for item in first["lifted"]], ["L1", "L1"])
        self.assertEqual(sorted(item["target"] for item in first["lifted"]), sorted([MLPACKAGE, CREDS_HELPER]))
        plain = self.plain("cat %s %s" % (MLPACKAGE, CREDS_HELPER))["reason"]
        self.assertEqual({item["would_have"] for item in first["lifted"]}, {plain})
        self.assertEqual(first["record"], trust.record_name(self.repo)[:8])
        self.assertEqual((first["root"], first["via"], first["worktree"]), (self.repo, "root", None))
        self.assertIsInstance(first["ms"], int)
        self.assertEqual(second["lifted"], [{"rule": "L2", "target": "claude", "would_have": self.plain("rg foo claude")["reason"]}])

    def test_the_tool_name_and_harness_are_logged(self):
        sr.classify("Read", {"file_path": MLPACKAGE}, self.repo, self.resolver(harness="pi"))
        line = self.log()[0]
        self.assertEqual((line["harness"], line["tool"], line["task"]), ("pi", "Read", None))

    def test_no_line_when_nothing_was_lifted(self):
        res = self.resolver()
        for command in ("echo hi", "cat src/app.py", "ls"):
            self.bash(command, trust=res)
        self.assertEqual(self.log(), [])

    def test_no_line_when_the_call_still_prompts(self):
        res = self.resolver()
        self.assertTrue(self.bash("cat %s %s" % (MLPACKAGE, ENV), trust=res)["sensitive"])
        self.assertTrue(self.bash("rg foo agent", trust=res)["sensitive"])
        self.assertEqual(self.log(), [])

    def test_no_line_without_a_resolver_or_a_record(self):
        self.plain("cat " + MLPACKAGE)
        sr.classify("Bash", {"command": "cat " + MLPACKAGE}, self.repo, None)
        os.chmod(os.path.join(self.tdir, trust.record_name(self.repo)), 0o600)
        os.unlink(os.path.join(self.tdir, trust.record_name(self.repo)))
        self.assertTrue(self.bash("cat " + MLPACKAGE)["sensitive"])
        self.assertEqual(self.log(), [])

    def test_ignored_record_is_logged(self):
        path = os.path.join(self.tdir, trust.record_name(self.repo))
        os.chmod(path, 0o600)
        os.unlink(path)
        self.grant(statement="something else")
        self.assertTrue(self.bash("cat " + MLPACKAGE)["sensitive"])
        lines = self.log()
        self.assertEqual(len(lines), 1)
        self.assertEqual((lines[0]["event"], lines[0]["reason"]), ("trust_ignored", "statement"))
        self.assertEqual(lines[0]["record"], trust.record_name(self.repo)[:8])

    def test_permissions_and_rotation(self):
        self.bash("cat " + MLPACKAGE)
        self.assertEqual(stat.S_IMODE(os.stat(self.logdir).st_mode), 0o700)
        path = os.path.join(self.logdir, trust_lift.LOG_NAME)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        Path(path).write_text("x" * 300 + "\n")
        with mock.patch.object(trust_lift, "LOG_LIMIT", 200):
            self.bash("cat " + MLPACKAGE)
        self.assertTrue(os.path.exists(path + ".1"))
        self.assertEqual(Path(path + ".1").read_text(), "x" * 300 + "\n")
        self.assertEqual(len(self.log()), 1)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_a_log_that_cannot_be_written_changes_nothing(self):
        blocker = os.path.join(self.tmp, "blocker")
        Path(blocker).write_text("x")
        res = self.resolver(log_dir=os.path.join(blocker, "nested"))
        self.assertEqual(self.bash("cat " + MLPACKAGE, trust=res), {"sensitive": False})

    def test_long_targets_are_capped(self):
        names = ["wk%03d_%s" % (index, MLPACKAGE) for index in range(trust_lift.LISTED + 5)]
        for name in names:
            Path(self.repo, name).write_text("x")
        self.assertEqual(self.bash("cat " + " ".join(names)), {"sensitive": False})
        line = self.log()[0]
        self.assertEqual(line["lifted_total"], len(names))
        self.assertEqual(len(line["lifted"]), trust_lift.LISTED)


# --- the guard gate ---------------------------------------------------------------------------------------------

class GuardGateTests(World):
    """A lift happens only after the guard answered allow."""

    def setUp(self):
        super().setUp()
        self.grant()

    def test_only_allow_lifts(self):
        seen = []

        def guard(seconds, tool, params, cwd, home=None):
            seen.append((tool, params, cwd, home, seconds))
            return trust_guard.Answer(trust_guard.ALLOW, None)

        res = self.resolver(guard=guard)
        self.assertEqual(self.bash("cat " + MLPACKAGE, trust=res), {"sensitive": False})
        self.assertEqual(seen, [("Bash", {"command": "cat " + MLPACKAGE}, self.repo, self.home, trust_lift.GUARD_SECONDS)])

    def test_ask_and_deny_are_never_lifted(self):
        for kind in (trust_guard.ASK, trust_guard.DENY):
            with self.subTest(kind=kind):
                res = self.resolver(guard=lambda *a, **k: trust_guard.Answer(kind, "no"))
                self.assertEqual(self.bash("cat " + MLPACKAGE, trust=res), self.plain("cat " + MLPACKAGE))
                self.assertTrue(self.bash("rg foo claude", trust=res)["sensitive"])
        self.assertEqual(self.log(), [])

    def test_errors_and_time_outs_are_never_lifted(self):
        def broken(*a, **k):
            raise RuntimeError("boom")

        def late(*a, **k):
            raise trust_guard._OutOfTime("late")

        for guard in (broken, late, lambda *a, **k: None, lambda *a, **k: "allow"):
            with self.subTest(guard=guard):
                res = self.resolver(guard=guard)
                self.assertTrue(self.bash("cat " + MLPACKAGE, trust=res)["sensitive"])
        self.assertEqual(self.log(), [])

    def test_the_guard_runs_once_per_call_and_only_when_a_lift_is_wanted(self):
        calls = []

        def guard(*a, **k):
            calls.append(1)
            return trust_guard.Answer(trust_guard.ALLOW, None)

        res = self.resolver(guard=guard)
        self.bash("echo hi", trust=res)
        self.bash("cat src/app.py", trust=res)
        self.assertEqual(calls, [])
        self.bash("cat %s %s" % (MLPACKAGE, CREDS_HELPER), trust=res)
        self.assertEqual(len(calls), 1)

    def test_a_call_the_real_guard_denies_is_not_lifted(self):
        store = os.path.join(self.tdir, "anything")
        for command in ("cat %s %s" % (MLPACKAGE, store), "cat %s; cat %s" % (MLPACKAGE, store),
                        "rm -r %s && cat %s" % (self.tdir, MLPACKAGE)):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command), self.plain(command))
        # the real guard allows an ordinary call here, which is what lets a lift through
        self.assertEqual(self.bash("cat " + MLPACKAGE), {"sensitive": False})


# --- library callers --------------------------------------------------------------------------------------------

class LibraryCallerTests(World):
    """review_rounds, context_broker and route_card never pass a resolver, so a record changes nothing for them."""

    def setUp(self):
        super().setUp()
        self.grant()
        # a resolver that fails loudly if anything asks it for a tree
        self.trap = mock.Mock(side_effect=AssertionError("a library caller reached the trust record"))
        patch = mock.patch.object(trust, "lookup", self.trap)
        patch.start()
        self.addCleanup(patch.stop)

    def test_sensitive_path_ignores_the_record(self):
        import context_broker
        for path in (os.path.join(self.repo, MLPACKAGE), os.path.join(self.repo, CREDS_HELPER)):
            self.assertTrue(sr.sensitive_path(path), path)
            self.assertTrue(context_broker.sensitive_path(path), path)
        self.assertFalse(sr.sensitive_path(os.path.join(self.repo, "src", "app.py")))
        self.trap.assert_not_called()

    def test_a_classified_call_leaves_no_resolver_behind(self):
        self.bash("cat " + MLPACKAGE)
        self.trap.reset_mock()
        self.assertIsNone(sr._SHELL["trust"])
        self.assertTrue(sr.sensitive_path(os.path.join(self.repo, MLPACKAGE)))
        self.trap.assert_not_called()

    def test_review_rounds_ignores_the_record(self):
        import review_rounds
        check = review_rounds.sensitive_checker()
        self.assertTrue(check(self.repo, MLPACKAGE))
        self.assertTrue(check(self.repo, "tests/" + TEST_SECRETS))
        self.assertFalse(check(self.repo, "src/app.py"))
        self.trap.assert_not_called()

    def test_route_card_ignores_the_record(self):
        import route_card
        sensitive = Path(self.repo, "brief-" + CREDS_HELPER)
        sensitive.write_text("a brief\n")
        ordinary = Path(self.repo, "brief-notes.md")
        ordinary.write_text("a brief\n")
        task = "read the brief first"
        self.assertEqual(route_card.brief_head(task, [str(sensitive)], self.repo), "")
        self.assertEqual(route_card.brief_head(task, [str(ordinary)], self.repo), "a brief")
        self.trap.assert_not_called()

    def test_classify_without_a_resolver_ignores_the_record(self):
        for command in ("cat " + MLPACKAGE, "rg foo claude"):
            self.assertTrue(sr.classify("Bash", {"command": command}, self.repo)["sensitive"])
        self.trap.assert_not_called()


# --- the command line and the wiring ----------------------------------------------------------------------------

def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("TP_")}
    env["TP_REDACTION"] = "shadow"
    env.update(extra)
    return env


class CommandLineTests(World):
    def setUp(self):
        super().setUp()
        self.grant()

    def run_tool(self, event, *flags, env=None):
        proc = subprocess.run([sys.executable, TOOL, *flags], input=json.dumps(event), capture_output=True, text=True,
                              env=env or clean_env(), timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def trusted_flags(self, *more):
        return ("--trusted", "--home", self.home, "--temp-root", os.path.join(self.tmp, "no-such-temp-root"), *more)

    def event(self, command, **extra):
        return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": self.repo, **extra}

    def test_plain_mode_lifts_only_with_the_flag(self):
        command = "cat " + MLPACKAGE
        lifted = json.loads(self.run_tool(self.event(command), *self.trusted_flags()))
        self.assertEqual(lifted, {"sensitive": False})
        for flags in ((), ("--home", self.home)):
            kept = json.loads(self.run_tool(self.event(command), *flags))
            self.assertTrue(kept["sensitive"], flags)
            self.assertEqual(kept, self.plain(command))

    def test_hook_mode_lifts_only_with_the_flag(self):
        command = "cat " + MLPACKAGE
        event = self.event(command, hook_event_name="PreToolUse")
        self.assertEqual(self.run_tool(event, "--claude-hook", *self.trusted_flags()), "")
        shown = json.loads(self.run_tool(event, "--claude-hook"))
        self.assertEqual(shown["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertEqual(shown, sr.hook_response(event))

    def test_hook_mode_still_asks_for_what_is_not_lifted(self):
        event = self.event("cat %s %s" % (MLPACKAGE, ENV), hook_event_name="PreToolUse")
        shown = json.loads(self.run_tool(event, "--claude-hook", *self.trusted_flags()))
        self.assertEqual(shown["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertEqual(shown, sr.hook_response(event))
        self.assertEqual(self.skip_lines(), [])

    def skip_lines(self):
        path = os.path.join(self.home, ".config", "tmux-pi", "state", "trust", trust_lift.LOG_NAME)
        return [json.loads(line) for line in Path(path).read_text().splitlines()] if os.path.exists(path) else []

    def test_one_log_line_per_lifted_call_in_each_mode(self):
        command = "cat " + MLPACKAGE
        self.run_tool(self.event(command), *self.trusted_flags())
        self.run_tool(self.event(command, hook_event_name="PreToolUse"), "--claude-hook", *self.trusted_flags())
        lines = self.skip_lines()
        self.assertEqual([line["harness"] for line in lines], ["pi", "claude"])
        self.assertEqual({line["event"] for line in lines}, {"trust_skip"})
        self.assertEqual({line["task"] for line in lines}, {None})

    def test_task_id_is_logged(self):
        self.run_tool(self.event("cat " + MLPACKAGE), *self.trusted_flags(), env=clean_env(TP_TASK_ID="coder-1"))
        self.assertEqual(self.skip_lines()[0]["task"], "coder-1")

    def test_flag_without_a_record_or_with_a_broken_home_is_today(self):
        command = "cat " + MLPACKAGE
        empty = os.path.join(self.tmp, "empty-home")
        os.makedirs(empty)
        out = json.loads(self.run_tool(self.event(command), "--trusted", "--home", empty, "--temp-root", "/nonexistent-root"))
        self.assertEqual(out, self.plain(command))
        gone = os.path.join(self.tmp, "gone")
        out = json.loads(self.run_tool(self.event(command), "--trusted", "--home", gone, "--temp-root", "/nonexistent-root"))
        self.assertEqual(out, self.plain(command))

    def test_post_tool_use_writes_no_log(self):
        command = "cat " + MLPACKAGE
        self.run_tool(self.event(command, hook_event_name="PostToolUse"), "--claude-hook", *self.trusted_flags())
        self.assertEqual(self.skip_lines(), [])

    def test_a_sensitive_call_in_hook_mode_records_the_request_only_when_it_still_asks(self):
        task = os.path.join(self.tmp, "task")
        os.makedirs(task)
        env = clean_env(TP_TASK_DIR=task)
        self.run_tool(self.event("cat " + MLPACKAGE, hook_event_name="PreToolUse"), "--claude-hook", *self.trusted_flags(), env=env)
        self.assertFalse(os.path.exists(os.path.join(task, "permission-request.json")))
        self.run_tool(self.event("cat " + ENV, hook_event_name="PreToolUse"), "--claude-hook", *self.trusted_flags(), env=env)
        self.assertEqual(json.loads(Path(task, "permission-request.json").read_text())["status"], "pending")


class WiringTests(unittest.TestCase):
    """Hooks and Pi pass --trusted; offload.ts and the spawner, which only ask whether text may leave, do not."""

    def test_claude_hook_passes_the_flag(self):
        text = (ROOT / "claude/plugins/tmux-pi/hooks/sensitive-read.sh").read_text()
        self.assertIn('"$ROOT/bin/tp-sensitive-read" --claude-hook --trusted', text)

    def test_pi_extension_passes_the_flag(self):
        text = (ROOT / "agent/extensions/sensitive.ts").read_text()
        self.assertIn('execFileSync(classifier, ["--trusted"]', text)
        self.assertNotIn('execFileSync(classifier, []', text)

    def test_offload_does_not(self):
        text = (ROOT / "agent/extensions/offload.ts").read_text()
        self.assertIn("execFileSync(bin, [], {", text)
        self.assertNotIn("--trusted", text)

    def test_nothing_else_asks_for_lifts(self):
        spawn = (ROOT / "bin/tp-spawn").read_text()
        self.assertNotIn("--trusted", spawn)
        for name in ("review_rounds", "context_broker", "route_card"):
            self.assertNotIn("trust_lift", (ROOT / "lib" / (name + ".py")).read_text(), name)
        for path in (ROOT / "lib").glob("*.py"):
            if path.name in ("trust_lift.py", "sensitive_read.py"):
                continue
            self.assertNotIn("trust_lift.Resolver", path.read_text(), path.name)

    def test_the_classifier_imports_no_trust_module(self):
        text = (ROOT / "lib/sensitive_read.py").read_text()
        for name in ("import trust", "from trust", "import trust_lift", "import trust_guard"):
            self.assertNotIn(name, text)


class DriftTests(unittest.TestCase):
    """The exact credential names the lift refuses to touch must keep up with the names the rest of the product treats as credentials."""

    def test_no_file_sample_is_ever_lifted(self):
        """Every name the classifier tests globs against, present as a file in a trusted tree, is judged as it is today."""
        world = World()
        world.setUp()
        self.addCleanup(world.doCleanups)
        world.grant()
        res = world.resolver()
        for sample in sr._FILE_SAMPLES:
            with self.subTest(sample=sample):
                Path(world.repo, sample).write_text("x\n")
                for tool, params in (("Bash", {"command": "cat " + sample}), ("Read", {"file_path": sample})):
                    self.assertEqual(sr.classify(tool, params, world.repo, res), sr.classify(tool, params, world.repo))
        self.assertEqual(world.log(), [])

    def test_the_redaction_hooks_credential_names_are_never_lifted(self):
        for name in (CREDS, "credentials", SECRETS, SECRETS_YML, "secrets.yaml", "secrets.toml", "prod.tfvars", "Credentials.JSON"):
            with self.subTest(name=name):
                self.assertTrue(redact_hook.CREDENTIAL_FILE.match(name) or name.lower().startswith("credentials"), name)
                self.assertTrue(sr._CREDENTIAL_FILE.match(name), name)

    def test_the_lift_only_covers_weak_names(self):
        for name in (MLPACKAGE, TEST_SECRETS, CREDS_HELPER):
            with self.subTest(name=name):
                self.assertFalse(sr._CREDENTIAL_FILE.match(name))
        for name in (ENV, RSA, "id." + "pem", "a.key", "x.p12"):
            with self.subTest(name=name):
                self.assertFalse(sr._weak_only(name))


if __name__ == "__main__":
    unittest.main()
