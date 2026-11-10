"""graph.remove_own_folder: removes the folder make_own_folder made, and what is in it, without following a link. Each check runs
twice on whichever Python runs the tests: as `remove_own_folder` chooses (shutil.rmtree with dir_fd on 3.11+, the fallback on
older Pythons) and with that choice forced to the descriptor-relative fallback, so every interpreter covers the fallback."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
from ctxengine import graph  # noqa: E402


class RemoveOwnFolderCase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())  # resolved: make_own_folder wants a parent with no link on it
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.parent = os.path.join(self.tmp, "parent")
        os.mkdir(self.parent)

    def modes(self):
        """Yield once as the code chooses, once with the fallback forced."""
        for forced in (False, True):
            with self.subTest(forced_fallback=forced):
                if forced:
                    with mock.patch.object(shutil.rmtree, "avoids_symlink_attacks", False, create=True):
                        yield forced
                else:
                    yield forced

    def make(self):
        return graph.make_own_folder(self.parent, "own-")

    def test_a_nested_tree_with_files_is_removed(self):
        for _ in self.modes():
            folder = self.make()
            os.makedirs(os.path.join(folder, "a", "b", "c"))
            for rel in ("f", "a/f", "a/b/f", "a/b/c/f"):
                Path(folder, rel).write_text("x")
            graph.remove_own_folder(folder)
            self.assertFalse(os.path.lexists(folder))

    def test_a_link_inside_is_removed_as_a_link_and_its_target_survives(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(os.path.join(outside, "sub"))
        Path(outside, "keep.txt").write_text("keep")
        Path(outside, "sub", "keep2.txt").write_text("keep")
        for _ in self.modes():
            folder = self.make()
            os.mkdir(os.path.join(folder, "d"))
            os.symlink(outside, os.path.join(folder, "link"))
            os.symlink(outside, os.path.join(folder, "d", "deep-link"))
            os.symlink(os.path.join(outside, "keep.txt"), os.path.join(folder, "filelink"))
            graph.remove_own_folder(folder)
            self.assertFalse(os.path.lexists(folder))
            self.assertEqual(Path(outside, "keep.txt").read_text(), "keep")
            self.assertEqual(Path(outside, "sub", "keep2.txt").read_text(), "keep")

    def test_a_parent_swapped_for_a_link_is_not_removed_through(self):
        for _ in self.modes():
            folder = self.make()
            Path(folder, "f").write_text("x")
            moved = os.path.join(self.tmp, "moved")
            os.rename(self.parent, moved)  # the folder is now under `moved`; `parent` becomes a link to it
            os.symlink(moved, self.parent)
            graph.remove_own_folder(folder)
            self.assertTrue(os.path.exists(os.path.join(moved, os.path.basename(folder), "f")))
            os.unlink(self.parent)
            os.rename(moved, self.parent)

    def test_the_fallback_runs_itself_and_stops_at_its_depth_bound(self):
        folder = self.make()
        path = folder
        for _ in range(3):
            path = os.path.join(path, "n")
            os.mkdir(path)
        fd = os.open(self.parent, graph.DIR_FLAGS)
        try:
            with mock.patch.object(graph, "REMOVE_DEPTH", 1):
                graph._rmtree_at(fd, os.path.basename(folder))
            self.assertTrue(os.path.isdir(folder))  # too deep to finish: left, not followed on without bound
            graph._rmtree_at(fd, os.path.basename(folder))
            self.assertFalse(os.path.lexists(folder))
        finally:
            os.close(fd)


if __name__ == "__main__":
    unittest.main()
