import datetime
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import trust  # noqa: E402

NOW = datetime.datetime(2026, 10, 9, 12, 0, tzinfo=datetime.timezone.utc)


class Fixture(unittest.TestCase):
    """A fake home with a trust directory and a main repository, all in a private temp tree."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="trusttest-"))
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

    def fields(self, root, **over):
        st = os.lstat(root)
        rec = {"version": 1, "statement": trust.STATEMENT, "root": root, "root_dev": st.st_dev,
               "root_ino": st.st_ino, "worktrees": True, "granted_at": "2026-10-09T10:00:00Z",
               "granted_by": "uid %d" % os.getuid(), "expires": None, "note": "test"}
        rec.update(over)
        return {k: v for k, v in rec.items() if v is not Ellipsis}

    def write_raw(self, name, data, mode=0o400, directory=None):
        path = os.path.join(directory or self.tdir, name)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode())
        os.chmod(path, mode)
        return path

    def grant(self, root=None, mode=0o400, **over):
        root = root or self.repo
        return self.write_raw(trust.record_name(root), json.dumps(self.fields(root, **over)), mode)

    def load(self, root=None, **kw):
        kw.setdefault("temp_roots", ())
        kw.setdefault("now", NOW)
        return trust.load(root or self.repo, kw.pop("directory", self.tdir), home=kw.pop("home", self.home), **kw)

    def lookup(self, path, **kw):
        kw.setdefault("temp_roots", ())
        kw.setdefault("now", NOW)
        return trust.lookup(path, kw.pop("directory", self.tdir), home=kw.pop("home", self.home), **kw)

    def assertIgnored(self, res, code):
        self.assertFalse(res.trusted, res)
        self.assertEqual((res.state, res.reason), ("ignored", code))

    def add_worktree(self, main, work, name="wt1"):
        """Lay out a linked worktree exactly as `git worktree add` does."""
        entry = os.path.join(main, ".git", "worktrees", name)
        os.makedirs(entry)
        os.makedirs(work)
        Path(work, ".git").write_text("gitdir: %s\n" % entry)
        Path(entry, "commondir").write_text("../..\n")
        Path(entry, "gitdir").write_text(os.path.join(os.path.realpath(work), ".git") + "\n")
        Path(entry, "HEAD").write_text("ref: refs/heads/x\n")
        return os.path.realpath(work), entry


class RecordTests(Fixture):
    def test_valid_record_is_trusted(self):
        self.grant()
        res = self.load()
        self.assertTrue(res.trusted)
        self.assertEqual(res.record["root"], self.repo)
        self.assertEqual(res.suffix(), "")

    def test_absent_record_is_silent(self):
        res = self.load()
        self.assertEqual((res.state, res.reason), ("absent", "absent"))
        self.assertTrue(trust.is_silent(res))
        self.assertEqual(res.suffix(), "")

    def test_missing_trust_directory_is_absent(self):
        res = self.load(directory=os.path.join(self.home, ".config", "tmux-pi", "nothing"))
        self.assertEqual(res.state, "absent")

    def test_symlinked_record_ignored(self):
        real = self.write_raw("real.json", json.dumps(self.fields(self.repo)))
        os.symlink(real, os.path.join(self.tdir, trust.record_name(self.repo)))
        self.assertIgnored(self.load(), "symlink")

    def test_non_regular_record_ignored(self):
        os.mkdir(os.path.join(self.tdir, trust.record_name(self.repo)))
        self.assertIgnored(self.load(), "notfile")

    def test_hardlinked_record_ignored(self):
        path = self.grant()
        os.link(path, os.path.join(self.tmp, "second-name"))
        self.assertIgnored(self.load(), "nlink")

    def foreign(self, want_dir):
        """Patch lstat so directories (or files) look owned by another account."""
        real = os.lstat

        def lstat(p, *a, **k):
            st = real(p, *a, **k)
            if stat.S_ISDIR(st.st_mode) == want_dir:
                fields = list(st)
                fields[4] = st.st_uid + 1
                return os.stat_result(fields)
            return st

        return mock.patch.object(trust.os, "lstat", side_effect=lstat)

    def test_record_owned_by_someone_else_ignored(self):
        self.grant()
        with self.foreign(want_dir=False):
            self.assertIgnored(self.load(), "owner")

    def test_directory_owned_by_someone_else_ignored(self):
        self.grant()
        with self.foreign(want_dir=True):
            self.assertIgnored(self.load(), "owner")

    def test_trust_directory_that_is_a_file_ignored(self):
        shutil.rmtree(self.tdir)
        Path(self.tdir).write_text("x")
        self.assertIgnored(self.load(), "notfile")

    def test_wrong_effective_uid_ignored(self):
        self.grant()
        with mock.patch("os.geteuid", return_value=os.geteuid() + 1):
            self.assertIgnored(self.load(), "owner")

    def test_group_or_other_writable_record_ignored(self):
        for bits in (0o420, 0o402, 0o600 | 0o020, 0o666):
            path = self.grant(mode=bits)
            self.assertIgnored(self.load(), "perms")
            os.chmod(path, 0o600)
            os.unlink(path)

    def test_oversized_record_ignored(self):
        self.write_raw(trust.record_name(self.repo), json.dumps(self.fields(self.repo, note="x" * 5000)))
        self.assertIgnored(self.load(), "size")

    def test_file_that_grows_after_stat_is_ignored(self):
        self.grant()
        with mock.patch.object(trust.os, "read", return_value=b" " * (trust.MAX_RECORD + 1)):
            self.assertIgnored(self.load(), "size")

    def test_record_swapped_between_stat_and_open_is_ignored(self):
        path = self.grant()
        real_open = os.open

        done = []

        def swap(p, flags, *a):
            if p == path and not done:
                done.append(1)
                os.chmod(path, 0o600)
                os.unlink(path)
                self.write_raw(os.path.basename(path), json.dumps(self.fields(self.repo)))
            return real_open(p, flags, *a)

        with mock.patch.object(trust.os, "open", side_effect=swap):
            self.assertIgnored(self.load(), "symlink")

    def test_unparseable_records_ignored(self):
        name = trust.record_name(self.repo)
        good = json.dumps(self.fields(self.repo))
        cases = ["", "not json", "[1, 2]", "null", good[:-1], good.replace('"version": 1', '"version": 1, "version": 1'),
                 good.replace('"root_dev"', '"root_dev": NaN, "x"').replace('"x": ', '"x": 0, "y": '), "\xff\xfe"]
        for text in cases:
            path = self.write_raw(name, text.encode("latin-1", "replace") if text == "\xff\xfe" else text)
            self.assertIgnored(self.load(), "parse")
            os.chmod(path, 0o600)
            os.unlink(path)

    def test_version_must_be_one(self):
        for v in (2, 0, "1", True, 1.5, None, [1]):
            path = self.grant(version=v)
            self.assertIgnored(self.load(), "version")
            os.unlink(path)
        rec = self.fields(self.repo)
        del rec["version"]
        self.write_raw(trust.record_name(self.repo), json.dumps(rec))
        self.assertIgnored(self.load(), "version")

    def test_statement_must_match(self):
        self.grant(statement="trusted")
        self.assertIgnored(self.load(), "statement")

    def test_missing_or_mistyped_fields_ignored_unknown_keys_kept(self):
        for key in ("root", "root_dev", "root_ino", "worktrees", "granted_at", "granted_by"):
            rec = self.fields(self.repo)
            del rec[key]
            path = self.write_raw(trust.record_name(self.repo), json.dumps(rec))
            self.assertIgnored(self.load(), "fields")
            os.unlink(path)
        for key, bad in (("root_dev", "1"), ("root_ino", True), ("worktrees", "yes"), ("granted_by", 5),
                         ("note", "x" * 201), ("note", 3), ("expires", 5)):
            path = self.grant(**{key: bad})
            self.assertIgnored(self.load(), "fields")
            os.unlink(path)
        self.grant(future_field={"a": 1}, expires=Ellipsis, note=Ellipsis)
        self.assertTrue(self.load().trusted)

    def test_renamed_or_copied_record_ignored(self):
        other = self.make_repo(os.path.join(self.home, "other"))
        path = self.grant(other)
        shutil.copy(path, os.path.join(self.tdir, trust.record_name(self.repo)))
        os.chmod(os.path.join(self.tdir, trust.record_name(self.repo)), 0o400)
        self.assertIgnored(self.load(), "name_mismatch")
        self.assertTrue(self.load(other).trusted)

    def test_record_whose_inner_root_differs_from_its_name_ignored(self):
        other = self.make_repo(os.path.join(self.home, "other"))
        self.write_raw(trust.record_name(self.repo), json.dumps(self.fields(other)))
        self.assertIgnored(self.load(), "name_mismatch")

    def test_directory_case_variant_is_not_trusted(self):
        self.grant()
        variant = os.path.join(os.path.dirname(self.repo), "PROJ")
        self.assertFalse(self.load(variant).trusted)
        self.assertFalse(self.lookup(os.path.join(variant, "src")).trusted)

    def test_deleted_and_recreated_root_ignored(self):
        self.grant()
        shutil.rmtree(self.repo)
        self.assertIgnored(self.load(), "root_moved")
        self.make_repo(self.repo)
        self.assertIgnored(self.load(), "root_moved")

    def test_moved_root_and_symlink_replacement_ignored(self):
        self.grant()
        moved = self.repo + "-moved"
        os.rename(self.repo, moved)
        os.symlink(moved, self.repo)
        self.assertIgnored(self.load(), "root_moved")

    def test_root_that_is_not_a_repository_root_ignored(self):
        self.grant()
        shutil.rmtree(os.path.join(self.repo, ".git"))
        self.assertIgnored(self.load(), "not_git_root")
        Path(self.repo, ".git").write_text("gitdir: /elsewhere\n")
        self.assertIgnored(self.load(), "not_git_root")

    def test_roots_that_may_never_be_trusted(self):
        # The home directory, an ancestor of it and the filesystem root, even with a record that
        # otherwise validates (a .git would put the trust directory inside a repository, so none here).
        for root in (self.home, os.path.dirname(self.home), os.sep):
            self.write_raw(trust.record_name(root), json.dumps(self.fields(root)))
            self.assertIgnored(self.load(root), "bad_root")

    def test_relative_or_unnormalised_root_ignored(self):
        for root in ("proj", self.repo + "/", self.repo + "/../proj"):
            self.assertIgnored(self.load(root), "fields")

    def test_expiry(self):
        past = (NOW - datetime.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        future = (NOW + datetime.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        path = self.grant(expires=past)
        res = self.load()
        self.assertIgnored(res, "expired")
        self.assertEqual(res.suffix(), "trust record ignored: expired")
        os.unlink(path)
        path = self.grant(expires=future)
        self.assertTrue(self.load().trusted)
        os.unlink(path)
        path = self.grant(expires="2026-10-09")           # a bare date starts at midnight UTC
        self.assertIgnored(self.load(), "expired")
        os.unlink(path)
        path = self.grant(expires="2026-10-10")
        self.assertTrue(self.load().trusted)
        os.unlink(path)
        for bad in ("tomorrow", "2026-10-10T00:00:00"):
            path = self.grant(expires=bad)
            self.assertIgnored(self.load(), "parse")
            os.unlink(path)

    def test_suffix_names_the_reason(self):
        self.grant()
        shutil.rmtree(self.repo)
        self.make_repo(self.repo)
        self.assertEqual(self.load().suffix(), "trust record ignored: root moved")

    def test_any_exception_is_ignored_not_raised(self):
        self.grant()
        with mock.patch.object(trust, "_validate", side_effect=RuntimeError("boom")):
            self.assertIgnored(self.load(), "error")
            self.assertIgnored(self.lookup(self.repo), "error")
        with mock.patch.object(trust, "tree_info", side_effect=RuntimeError("boom")):
            self.assertIgnored(self.lookup(self.repo), "error")


class DirectoryTests(Fixture):
    def test_symlinked_trust_directory_ignored(self):
        self.grant()
        real = os.path.join(self.home, "elsewhere")
        os.rename(self.tdir, real)
        os.symlink(real, self.tdir)
        self.assertIgnored(self.load(), "symlink")

    def test_symlinked_trust_directory_into_a_repository_ignored(self):
        inside = os.path.join(self.repo, "trust")
        os.mkdir(inside, 0o700)
        shutil.copy(self.grant(), os.path.join(inside, trust.record_name(self.repo)))
        os.chmod(os.path.join(inside, trust.record_name(self.repo)), 0o400)
        shutil.rmtree(self.tdir)
        os.symlink(inside, self.tdir)
        self.assertFalse(self.load().trusted)
        self.assertIn(self.load().reason, ("symlink", "dir_in_repo"))

    def test_dotfile_layout_symlink_routed_into_a_repository_ignored(self):
        # ~/.config itself is a symlink (an owner's dotfile layout): allowed, unless it lands in a repo.
        dots = self.make_repo(os.path.join(self.home, "dotfiles"))
        os.makedirs(os.path.join(dots, "cfg", "tmux-pi", "trust"), mode=0o700)
        for d in ("cfg", "cfg/tmux-pi", "cfg/tmux-pi/trust"):
            os.chmod(os.path.join(dots, d), 0o700)
        shutil.rmtree(os.path.join(self.home, ".config"))
        os.symlink(os.path.join(dots, "cfg"), os.path.join(self.home, ".config"))
        self.write_raw(trust.record_name(self.repo), json.dumps(self.fields(self.repo)),
                       directory=os.path.join(dots, "cfg", "tmux-pi", "trust"))
        self.assertIgnored(self.load(), "dir_in_repo")

    def test_symlinked_config_directory_outside_any_repo_is_resolved_once(self):
        self.grant()
        real = os.path.join(self.tmp, "dotfiles-cfg")
        os.rename(os.path.join(self.home, ".config"), real)
        os.symlink(real, os.path.join(self.home, ".config"))
        self.assertTrue(self.load().trusted)

    def test_trust_directory_inside_a_repository_ignored(self):
        inside = os.path.join(self.repo, "t")
        os.makedirs(inside, mode=0o700)
        self.write_raw(trust.record_name(self.repo), json.dumps(self.fields(self.repo)), directory=inside)
        self.assertIgnored(self.load(directory=inside), "dir_in_repo")

    def test_trust_directory_in_a_temp_root_ignored(self):
        self.grant()
        self.assertTrue(self.load().trusted)
        self.assertIgnored(self.load(temp_roots=(self.tmp,)), "dir_in_temp")
        self.assertIn("/tmp", trust.DEFAULT_TEMP_ROOTS)
        self.assertIn("/private/var/folders", trust.DEFAULT_TEMP_ROOTS)

    def test_group_writable_or_foreign_directory_ignored(self):
        self.grant()
        for d in (self.tdir, os.path.dirname(self.tdir), os.path.join(self.home, ".config")):
            old = stat.S_IMODE(os.stat(d).st_mode)
            os.chmod(d, 0o770)
            self.assertIgnored(self.load(), "perms")
            os.chmod(d, 0o702)
            self.assertIgnored(self.load(), "perms")
            os.chmod(d, old)
        with mock.patch("os.geteuid", return_value=os.geteuid() + 1):
            self.assertIgnored(self.load(), "owner")
        self.assertTrue(self.load().trusted)

    def _each_bad_mode(self, directory):
        for mode in (0o770, 0o707, 0o777):
            old = stat.S_IMODE(os.stat(directory).st_mode)
            os.chmod(directory, mode)
            try:
                self.assertIgnored(self.load(), "perms")
            finally:
                os.chmod(directory, old)
        with mock.patch("os.geteuid", return_value=os.geteuid() + 1):
            self.assertIgnored(self.load(), "owner")
        self.assertTrue(self.load().trusted)

    def test_home_directory_itself_is_checked(self):
        self.grant()
        self.assertTrue(self.load().trusted)
        self._each_bad_mode(self.home)

    def test_every_link_of_the_plain_chain_is_checked(self):
        self.grant()
        for d in (self.home, os.path.join(self.home, ".config"), os.path.join(self.home, ".config", "tmux-pi"),
                  self.tdir):
            self._each_bad_mode(d)

    def test_home_that_is_not_a_directory_is_not_trusted(self):
        self.grant()
        fake = os.path.join(self.tmp, "homefile")
        Path(fake).write_text("x")
        self.assertFalse(self.load(home=fake).trusted)

    def test_every_link_above_a_symlinked_config_is_checked(self):
        # ~/.config -> ~/a/b/cfg: a, b and cfg all matter, and so does home.
        base = os.path.join(self.home, "a")
        os.makedirs(os.path.join(base, "b"), mode=0o700)
        real = os.path.join(base, "b", "cfg")
        self.grant()
        os.rename(os.path.join(self.home, ".config"), real)
        os.symlink(real, os.path.join(self.home, ".config"))
        for d in (base, os.path.join(base, "b"), real, os.path.join(real, "tmux-pi"), self.home):
            os.chmod(d, 0o700)
        self.assertTrue(self.load().trusted)
        for d in (self.home, base, os.path.join(base, "b"), real, os.path.join(real, "tmux-pi")):
            self._each_bad_mode(d)

    def test_every_ancestor_of_a_symlinked_config_outside_home_is_checked(self):
        outer = os.path.join(self.tmp, "outer")
        mid = os.path.join(outer, "mid")
        os.makedirs(mid, mode=0o700)
        self.grant()
        real = os.path.join(mid, "cfg")
        os.rename(os.path.join(self.home, ".config"), real)
        os.symlink(real, os.path.join(self.home, ".config"))
        self.assertTrue(self.load().trusted)
        for d in (outer, mid, real):
            self._each_bad_mode(d)
        os.chmod(self.tmp, 0o777)
        try:
            self.assertIgnored(self.load(), "perms")
        finally:
            os.chmod(self.tmp, 0o700)

    def test_symlink_inside_the_chain_below_config_ignored(self):
        self.grant()
        mid = os.path.join(self.home, ".config", "tmux-pi")
        os.rename(mid, mid + "-real")
        os.symlink(mid + "-real", mid)
        self.assertIgnored(self.load(), "symlink")

    def test_trust_directory_outside_home_ignored(self):
        other = os.path.join(self.tmp, "else")
        os.mkdir(other, 0o700)
        self.assertIgnored(self.load(directory=other), "dir_outside_home")
        self.assertIgnored(self.load(directory=self.home), "dir_outside_home")


class TreeTests(Fixture):
    def test_main_tree_and_subdirectory(self):
        os.makedirs(os.path.join(self.repo, "a", "b"))
        self.assertEqual(trust.tree_of(os.path.join(self.repo, "a", "b")), (self.repo, "main"))
        self.assertEqual(trust.tree_of(self.repo), (self.repo, "main"))
        self.assertEqual(trust.tree_of(os.path.join(self.home, ".config"))[1], "none")

    def test_nearest_git_wins_for_nested_repository(self):
        nested = self.make_repo(os.path.join(self.repo, "vendor", "lib"))
        self.assertEqual(trust.tree_of(os.path.join(nested, "x")), (nested, "main"))

    def test_symlink_into_tree_is_resolved(self):
        link = os.path.join(self.tmp, "alias")
        os.symlink(self.repo, link)
        self.assertEqual(trust.tree_of(link), (self.repo, "main"))

    def test_planted_git_file_is_not_a_worktree(self):
        work = os.path.join(self.tmp, "planted")
        os.makedirs(work)
        Path(work, ".git").write_text("gitdir: %s/.git/worktrees/none\n" % self.repo)
        self.assertEqual(trust.tree_of(work), (os.path.realpath(work), "gitfile"))
        self.assertEqual(trust.tree_info(work)[2], None)

    def test_linked_worktree_verified(self):
        work, _ = self.add_worktree(self.repo, os.path.join(self.tmp, "wt-a"))
        self.assertEqual(trust.tree_info(os.path.join(work, "src")), (work, "worktree", self.repo))

    def test_worktree_with_relative_links_verified(self):
        work, entry = self.add_worktree(self.repo, os.path.join(self.tmp, "wt-r"))
        Path(work, ".git").write_text("gitdir: %s\n" % os.path.relpath(entry, work))
        Path(entry, "gitdir").write_text(os.path.relpath(os.path.join(work, ".git"), entry) + "\n")
        self.assertEqual(trust.tree_of(work)[1], "worktree")

    def test_worktree_links_must_agree_both_ways(self):
        work, entry = self.add_worktree(self.repo, os.path.join(self.tmp, "wt-b"))
        other = os.path.join(self.tmp, "wt-other")
        os.makedirs(other)
        Path(entry, "gitdir").write_text(os.path.join(other, ".git") + "\n")        # back-link points elsewhere
        self.assertEqual(trust.tree_of(work)[1], "gitfile")
        os.unlink(os.path.join(entry, "gitdir"))                                       # back-link missing
        self.assertEqual(trust.tree_of(work)[1], "gitfile")
        Path(entry, "gitdir").write_text(os.path.join(work, ".git") + "\n")
        self.assertEqual(trust.tree_of(work)[1], "worktree")
        Path(entry, "commondir").write_text("../../..\n")                              # common dir is not <root>/.git
        self.assertEqual(trust.tree_of(work)[1], "gitfile")

    def test_worktree_pointing_at_someone_elses_entry_is_refused(self):
        work, entry = self.add_worktree(self.repo, os.path.join(self.tmp, "wt-c"))
        thief = os.path.join(self.tmp, "wt-thief")
        os.makedirs(thief)
        Path(thief, ".git").write_text("gitdir: %s\n" % entry)
        self.assertEqual(trust.tree_of(thief)[1], "gitfile")

    def test_submodule_style_gitfile_is_not_a_worktree(self):
        mod = os.path.join(self.repo, "sub")
        os.makedirs(os.path.join(self.repo, ".git", "modules", "sub"))
        os.makedirs(mod)
        Path(mod, ".git").write_text("gitdir: ../.git/modules/sub\n")
        self.assertEqual(trust.tree_of(mod)[1], "gitfile")

    def test_git_file_shapes_that_are_refused(self):
        work = os.path.join(self.tmp, "odd")
        os.makedirs(work)
        for text in ("", "gitdir:", "gitdir: a\ngitdir: b\n", "hello\n", "x" * 5000):
            Path(work, ".git").write_text(text)
            self.assertEqual(trust.tree_of(work)[1], "gitfile", text[:10])

    def test_inside(self):
        os.makedirs(os.path.join(self.repo, "a"))
        self.assertTrue(trust.inside(self.repo, self.repo))
        self.assertTrue(trust.inside(os.path.join(self.repo, "a", "new.txt"), self.repo))
        self.assertFalse(trust.inside(self.repo + "-two", self.repo))
        self.assertFalse(trust.inside(os.path.join(self.repo, ".."), self.repo))
        self.assertFalse(trust.inside(os.path.join(self.repo, "PROJ"), self.repo.upper()))
        outside = os.path.join(self.tmp, "out")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(self.repo, "escape"))
        self.assertFalse(trust.inside(os.path.join(self.repo, "escape", "f"), self.repo))
        self.assertTrue(trust.inside("/anything", "/"))


class LookupTests(Fixture):
    def test_path_in_trusted_main_tree(self):
        self.grant()
        os.makedirs(os.path.join(self.repo, "src"))
        res = self.lookup(os.path.join(self.repo, "src", "new.py"))
        self.assertTrue(res.trusted)
        self.assertEqual(res.root, self.repo)

    def test_paths_with_no_repository_or_no_record_are_silent(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        for res in (self.lookup(plain), self.lookup(self.repo)):
            self.assertFalse(res.trusted)
            self.assertTrue(trust.is_silent(res))

    def test_nested_repository_inherits_nothing(self):
        self.grant()
        nested = self.make_repo(os.path.join(self.repo, "vendor"))
        self.assertFalse(self.lookup(nested).trusted)

    def test_submodule_and_planted_gitfile_inherit_nothing(self):
        self.grant()
        sub = os.path.join(self.repo, "sub")
        os.makedirs(sub)
        Path(sub, ".git").write_text("gitdir: ../.git/modules/sub\n")
        res = self.lookup(sub)
        self.assertEqual((res.state, res.reason), ("absent", "unverified_tree"))

    def test_verified_worktree_inherits(self):
        self.grant()
        work, _ = self.add_worktree(self.repo, os.path.join(self.tmp, "wt-ok"))
        res = self.lookup(os.path.join(work, "deep", "file"))
        self.assertTrue(res.trusted)
        self.assertEqual(res.root, self.repo)

    def test_worktree_with_missing_backlink_not_trusted(self):
        self.grant()
        work, entry = self.add_worktree(self.repo, os.path.join(self.tmp, "wt-nb"))
        os.unlink(os.path.join(entry, "gitdir"))
        self.assertFalse(self.lookup(work).trusted)

    def test_worktrees_false_stops_inheritance(self):
        self.grant(worktrees=False)
        work, _ = self.add_worktree(self.repo, os.path.join(self.tmp, "wt-off"))
        self.assertTrue(self.lookup(self.repo).trusted)
        res = self.lookup(work)
        self.assertEqual((res.state, res.reason), ("absent", "worktrees_off"))

    def test_invalidating_the_main_record_removes_every_worktree(self):
        path = self.grant()
        work, _ = self.add_worktree(self.repo, os.path.join(self.tmp, "wt-gone"))
        self.assertTrue(self.lookup(work).trusted)
        os.chmod(path, 0o600)
        os.unlink(path)
        self.assertFalse(self.lookup(work).trusted)
        path = self.grant()
        self.assertTrue(self.lookup(work).trusted)
        os.chmod(path, 0o666)
        self.assertFalse(self.lookup(work).trusted)

    def test_worktree_in_a_forbidden_place_not_trusted(self):
        self.grant()
        # A worktree equal to or containing home, "/" or the trust directory, or inside the trust
        # directory. (End to end these also trip dir_in_repo, so the rule is tested directly.)
        refuse = trust._worktree_refusal
        for place in (self.home, os.path.dirname(self.home), os.sep, self.tdir, os.path.join(self.tdir, "w"),
                      os.path.dirname(self.tdir)):
            self.assertEqual(refuse(place, self.tdir, self.home), "worktree_home", place)
        self.assertIsNone(refuse(os.path.join(self.tmp, "wt"), self.tdir, self.home))
        self.assertIsNone(refuse(os.path.join(self.home, "projects", "wt"), self.tdir, self.home))

    def test_worktree_holding_credential_directories_not_trusted(self):
        self.grant()
        # Written out here on purpose: design section 3.3 lists these, and the classifier is checked below.
        named = (".ssh", ".aws", ".gnupg", ".kube", ".docker", ".config/gh", ".config/gcloud", "agent/sessions",
                 "config/local.json")
        for cred in named:
            work, _ = self.add_worktree(self.repo, os.path.join(self.tmp, "wt-" + cred.replace("/", "-")), name=cred.replace("/", "-"))
            os.makedirs(os.path.dirname(os.path.join(work, *cred.split("/"))), exist_ok=True)
            os.makedirs(os.path.join(work, *cred.split("/")))
            self.assertIgnored(self.lookup(work), "worktree_creds")
        clean, _ = self.add_worktree(self.repo, os.path.join(self.tmp, "wt-clean"), name="clean")
        self.assertTrue(self.lookup(clean).trusted)

    def test_credential_locations_are_the_classifiers(self):
        import sensitive_read
        expected = set(sensitive_read.SENSITIVE_DIR) | {"/".join(p) for p in sensitive_read.SENSITIVE_PAIRS
                                                        if p[0] != ".git"}
        self.assertEqual(set(trust._CRED_DIRS), expected)
        self.assertIn("agent/sessions", trust._CRED_DIRS)


class NoAmbientInputTests(Fixture):
    def test_module_source_has_no_process_or_environment_access(self):
        src = (ROOT / "lib" / "trust.py").read_text()
        code = "\n".join(line.split("#")[0] for line in src.splitlines())
        for needle in ("subprocess", "os.environ", "getenv", "expanduser", "Path.home", "import environ", "os.system",
                       "popen", "import tempfile", "gettempdir"):
            self.assertNotIn(needle, code, needle)

    def test_environment_does_not_change_anything(self):
        self.grant()
        elsewhere = os.path.join(self.tmp, "plain")
        os.makedirs(elsewhere)
        env = {"HOME": self.home, "TMPDIR": self.tmp, "XDG_CONFIG_HOME": os.path.join(self.home, ".config"),
               "GIT_DIR": os.path.join(self.repo, ".git"), "GIT_WORK_TREE": self.repo,
               "TP_TRUST_DIR": self.tdir, "TP_TRUST_ROOT": self.repo}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(trust.home_dir(), __import__("pwd").getpwuid(os.getuid()).pw_dir)
            self.assertNotEqual(trust.home_dir(), self.home)
            self.assertFalse(trust.trust_dir().startswith(self.tmp))
            res = trust.lookup(elsewhere, trust.trust_dir(), temp_roots=())
            self.assertFalse(res.trusted)
            self.assertEqual(res.reason, "no_repo")
            # A real, trusted path still needs the directory to be passed in; the default one is the account's.
            self.assertFalse(trust.lookup(self.repo, trust.trust_dir(), temp_roots=()).trusted)
        with mock.patch.dict(os.environ, env):
            self.assertTrue(self.lookup(self.repo).trusted)

    def test_trust_dir_is_under_the_password_database_home(self):
        import pwd
        self.assertEqual(trust.trust_dir(), os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".config", "tmux-pi", "trust"))

    def test_no_process_is_started(self):
        self.grant()
        with mock.patch("subprocess.Popen", side_effect=AssertionError("process started")), \
                mock.patch("os.system", side_effect=AssertionError("process started")), \
                mock.patch("os.getenv", side_effect=AssertionError("env read")):
            self.assertTrue(self.lookup(self.repo).trusted)


if __name__ == "__main__":
    unittest.main()
