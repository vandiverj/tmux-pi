"""Lane dependencies (lib/lane_deps.py): main's node_modules linked into a lane's worktree, guarded. Every repository is a
temporary one; nothing runs npm."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from test_lane_create import Scratch, git  # noqa: E402
import lane_commands  # noqa: E402
import lane_deps  # noqa: E402
import lane_remove  # noqa: E402
import lanes  # noqa: E402


def count(path):
    return sum(len(dirs) + len(files) for _, dirs, files in os.walk(path))


class DepsCase(Scratch):
    IGNORE = "node_modules\n"

    def setUp(self):
        super().setUp()
        files = {".gitignore": self.IGNORE, "package.json": "{}\n", "package-lock.json": "{}\n", "web/package.json": '{"n":1}\n',
                 "web/package-lock.json": "{}\n", "api/package.json": "{}\n", "api/deno.json": "{}\n", "api/deno.lock": "{}\n",
                 "tool/package.json": "{}\n"}
        for name, text in files.items():
            (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / name).write_text(text)
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "manifests")
        git(self.repo, "branch", "-f", "lane/integrate", "HEAD")
        for where in ("", "web", "api"):  # tool has a package.json and no install
            modules = self.repo / where / "node_modules"
            (modules / "pkg").mkdir(parents=True)
            (modules / "pkg/index.js").write_text(f"{where}\n")
            (modules / ".bin").mkdir()

    def link_paths(self, record):
        return {w: Path(record["worktree"]) / w / "node_modules" for w in ("", "web", "api", "tool")}

    def remove(self, name="alpha"):
        with contextlib.redirect_stderr(io.StringIO()):
            return lane_remove.remove_lane(str(self.repo / ".tmux-pi"), str(self.repo), name)


class ShareTests(DepsCase):
    def test_equal_manifests_link_main_node_modules_and_record_shared(self):
        value = self.create()
        record = value["lane"]
        links = self.link_paths(record)
        for where in ("", "web"):
            self.assertTrue(links[where].is_symlink(), where)
            self.assertEqual(os.readlink(links[where]), str(self.repo / where / "node_modules"))
        self.assertFalse(os.path.lexists(links["api"]), "a Deno project's node_modules is never linked")
        self.assertFalse(os.path.lexists(links["tool"]), "main has none to link")
        self.assertEqual(record["deps"], "shared")
        self.assertEqual(lanes.read_lane(self.common, "alpha")["deps"], "shared")
        self.assertEqual(value["warnings"], [])
        self.assertEqual(git(record["worktree"], "status", "--porcelain"), "")

    def test_a_differing_lockfile_links_nothing_and_says_why(self):
        (self.repo / "package-lock.json").write_text('{"changed": true}\n')
        value = self.create()
        record = value["lane"]
        self.assertEqual(record["deps"], "own")
        self.assertFalse(any(os.path.lexists(p) for p in self.link_paths(record).values()))
        self.assertEqual([w["kind"] for w in value["warnings"]], ["deps"])
        self.assertIn("package-lock.json", lane_commands.warning_text(value["warnings"][0]))

    def test_a_differing_workspace_manifest_links_nothing(self):
        (self.repo / "web/package.json").write_text('{"n":1} \n')
        record = self.create()["lane"]
        self.assertEqual(record["deps"], "own")
        self.assertFalse(os.path.lexists(Path(record["worktree"]) / "node_modules"))

    def test_main_differing_only_in_deno_files_still_links(self):
        for name in ("api/deno.lock", "api/deno.json"):
            (self.repo / name).write_text('{"other": true}\n')
        value = self.create()
        self.assertEqual(value["lane"]["deps"], "shared")
        self.assertEqual(value["warnings"], [])
        self.assertFalse(os.path.lexists(self.link_paths(value["lane"])["api"]))

    def hide(self, where, packages, lock=None):
        """Give main's node_modules at `where` npm's hidden lockfile with `packages`, and commit `lock` packages at the base."""
        modules = self.repo / where / "node_modules"
        (modules / ".package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": packages}))

    def lock(self, packages, where=""):
        path = self.repo / where / "package-lock.json"
        path.write_text(json.dumps({"lockfileVersion": 3, "packages": dict(packages, **{"": {"name": "x"}})}))
        git(self.repo, "commit", "-q", "-am", "lock")
        git(self.repo, "branch", "-f", "lane/integrate", "HEAD")

    PKG = {"node_modules/a": {"version": "1.0.0", "resolved": "r/a-1.0.0", "integrity": "sha-a"}}

    def test_an_installed_tree_equal_to_the_lockfile_links_even_if_main_is_on_another_branch(self):
        self.lock(dict(self.PKG, **{"node_modules/opt": {"version": "1", "optional": True}}))
        self.hide("", self.PKG)
        git(self.repo, "checkout", "-q", "-b", "other")
        (self.repo / "package-lock.json").write_text("{}\n")
        git(self.repo, "commit", "-q", "-am", "main's branch has another lockfile")
        value = self.create()
        self.assertEqual(value["lane"]["deps"], "shared", value["warnings"])

    def test_an_installed_tree_that_differs_from_the_lanes_lockfile_links_nothing(self):
        self.lock({"node_modules/a": dict(self.PKG["node_modules/a"], version="1.0.1")})
        self.hide("", self.PKG)
        value = self.create()
        self.assertEqual(value["lane"]["deps"], "own")
        self.assertFalse(os.path.lexists(Path(value["lane"]["worktree"]) / "node_modules"))
        self.assertIn("installed at 1.0.0", value["warnings"][0]["note"])

    def test_a_package_the_lockfile_needs_but_main_lacks_links_nothing(self):
        self.lock(dict(self.PKG, **{"node_modules/b": {"version": "2.0.0"}}))
        self.hide("", self.PKG)
        value = self.create()
        self.assertEqual(value["lane"]["deps"], "own")
        self.assertIn("not installed", value["warnings"][0]["note"])

    def test_an_installed_package_missing_from_the_lockfile_links_nothing(self):
        self.lock({})
        self.hide("", self.PKG)
        self.assertEqual(self.create()["lane"]["deps"], "own")

    def test_the_hidden_lockfile_of_a_workspace_is_compared_with_that_workspaces_lockfile(self):
        self.lock(self.PKG)
        self.hide("", self.PKG)
        self.lock({"node_modules/w": {"version": "3"}}, "web")
        self.hide("web", {})
        self.assertEqual(self.create()["lane"]["deps"], "own")

    def test_an_unreadable_hidden_lockfile_links_nothing(self):
        self.lock(self.PKG)
        (self.repo / "node_modules/.package-lock.json").write_text("not json")
        value = self.create()
        self.assertEqual(value["lane"]["deps"], "own")
        self.assertIn("cannot be read", value["warnings"][0]["note"])

    def test_a_main_without_node_modules_is_a_no_op_with_a_warning(self):
        import shutil
        for where in ("", "web", "api"):
            shutil.rmtree(self.repo / where / "node_modules")
        value = self.create()
        self.assertEqual(value["lane"]["deps"], "own")
        self.assertFalse(os.path.lexists(Path(value["lane"]["worktree"]) / "node_modules"))
        self.assertEqual([w["kind"] for w in value["warnings"]], ["deps"])
        self.assertIn("no node_modules", value["warnings"][0]["note"])

    def test_a_repository_without_a_package_json_is_silent(self):
        subprocess.run(["git", "-C", str(self.repo), "rm", "-q", "-r", "package.json", "web", "tool", "api"], check=True)
        git(self.repo, "commit", "-q", "-m", "no node")
        git(self.repo, "branch", "-f", "lane/integrate", "HEAD")
        value = self.create()
        self.assertEqual((value["lane"]["deps"], value["warnings"]), ("own", []))

    def test_a_directory_ignore_rule_would_show_the_link_as_untracked_so_it_is_not_made(self):
        (self.repo / ".gitignore").write_text("node_modules/\n")
        git(self.repo, "commit", "-q", "-am", "dir-only ignore")
        git(self.repo, "branch", "-f", "lane/integrate", "HEAD")
        value = self.create()
        self.assertEqual(value["lane"]["deps"], "own")
        self.assertFalse(os.path.lexists(Path(value["lane"]["worktree"]) / "node_modules"))
        self.assertTrue(any("ignore" in w["note"] for w in value["warnings"]))
        self.assertEqual(git(value["lane"]["worktree"], "status", "--porcelain"), "")

    def test_a_record_without_deps_reads_as_own(self):
        record = self.create()["lane"]
        bare = {k: v for k, v in record.items() if k != "deps"}
        self.assertEqual(lanes.lane_record(bare)["deps"], "own")
        with self.assertRaises(ValueError):
            lanes.lane_record(dict(record, deps="maybe"))

    def test_a_failed_creation_after_linking_leaves_main_and_the_tree_alone(self):
        before = count(self.repo / "node_modules")
        from unittest import mock
        with mock.patch.object(lanes, "write_lane", side_effect=ValueError("boom")):
            with self.assertRaises(ValueError):
                self.create()
        self.assertFalse((self.parent / "proj-wt-alpha").exists())
        self.assertEqual(count(self.repo / "node_modules"), before)
        self.assertEqual(count(self.repo / "web/node_modules"), 3)


class RemoveAndGuardTests(DepsCase):
    def test_removing_a_lane_removes_only_the_links(self):
        record = self.create()["lane"]
        before = {w: count(self.repo / w / "node_modules") for w in ("", "web", "api")}
        self.assertGreater(before[""], 0)
        self.remove()
        self.assertFalse(Path(record["worktree"]).exists())
        self.assertEqual({w: count(self.repo / w / "node_modules") for w in ("", "web", "api")}, before)
        self.assertEqual((self.repo / "web/node_modules/pkg/index.js").read_text(), "web\n")

    def test_removal_unlinks_with_os_unlink_and_never_recurses_through_a_link(self):
        from unittest import mock
        record = self.create()["lane"]
        calls = []
        real = os.unlink
        with mock.patch("lane_deps.os.unlink", side_effect=lambda p, *a, **k: (calls.append(p), real(p, *a, **k))[1]):
            self.remove()
        self.assertEqual(sorted(calls), sorted(str(Path(record["worktree"]) / w / "node_modules") for w in ("", "web")))

    def test_a_link_someone_else_made_is_not_ours_and_is_left(self):
        record = self.create()["lane"]
        elsewhere = self.parent / "elsewhere" / "node_modules"
        elsewhere.mkdir(parents=True)
        (Path(record["worktree"]) / "tool/node_modules").symlink_to(elsewhere)
        self.assertEqual(sorted(lane_deps.shared_links(self.repo, record["worktree"])),
                         sorted(str(Path(record["worktree"]) / w / "node_modules") for w in ("", "web")))

    def test_the_install_guard_unlinks_first_and_records_own(self):
        record = self.create()["lane"]
        before = count(self.repo / "node_modules")
        gone = lane_deps.unshare_lane(record["worktree"])
        self.assertEqual(len(gone), 2)
        links = self.link_paths(record)
        self.assertFalse(any(os.path.lexists(p) for p in links.values()))
        self.assertEqual(lanes.read_lane(self.common, "alpha")["deps"], "own")
        self.assertEqual(count(self.repo / "node_modules"), before)
        (links[""]).mkdir()  # what `npm ci` would now do: a real directory of the lane's own
        (links[""] / "x").write_text("x\n")
        self.assertEqual(count(self.repo / "node_modules"), before)
        self.assertEqual(lane_deps.unshare_lane(record["worktree"]), [])  # nothing left to unlink, and a real directory is not touched
        self.assertTrue((links[""] / "x").exists())

    def test_the_guard_command_line(self):
        record = self.create()["lane"]
        run = subprocess.run([sys.executable, str(Path(lane_deps.__file__)), "unlink", record["worktree"]], text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(len(run.stdout.splitlines()), 2)
        self.assertFalse(os.path.lexists(Path(record["worktree"]) / "node_modules"))


if __name__ == "__main__":
    unittest.main()
