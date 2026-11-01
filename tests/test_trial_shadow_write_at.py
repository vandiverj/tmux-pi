"""lib/trial_shadow.write_at: a file is replaced through a temp file made new, whatever the worker left at the temp name.

The temp name is `<name>.tmp.<runner pid>` and run.json shows the pid, so a worker (or an accident) can leave anything at it: a plain
file, a link, a FIFO, a directory. The write must finish, write nothing through what is there, wait on nothing, and delete nothing but
a file or a link (a directory and what is in it are the worker's)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import trial_shadow as ts  # noqa: E402


class WriteAtTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(os.path.realpath(self.tmp.name))
        self.fd = os.open(str(self.dir), os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.fd)
        self.temp = self.dir / "out.json.tmp.{}".format(os.getpid())

    def written(self, text="new\n"):
        ts.write_at(self.fd, "out.json", text)
        self.assertEqual((self.dir / "out.json").read_text(), text)

    def test_no_temp_name_in_the_way(self):
        self.written()
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["out.json"])

    def test_bytes_are_written_as_they_are(self):
        ts.write_at(self.fd, "out.json", b"\xff\x00raw")
        self.assertEqual((self.dir / "out.json").read_bytes(), b"\xff\x00raw")

    def test_a_plain_file_at_the_temp_name_is_replaced(self):
        self.temp.write_text("old")
        self.written()
        self.assertFalse(self.temp.exists())

    def test_a_link_at_the_temp_name_is_not_written_through(self):
        outside = self.dir / "outside"
        outside.write_text("keep")
        self.temp.symlink_to(outside)
        self.written()
        self.assertEqual(outside.read_text(), "keep")

    def test_a_fifo_at_the_temp_name_is_not_waited_on(self):
        os.mkfifo(str(self.temp))
        self.written()

    def test_an_empty_directory_at_the_temp_name_does_not_stop_the_write_and_stays(self):
        self.temp.mkdir()
        self.written()
        self.assertTrue(self.temp.is_dir())

    def test_a_directory_with_files_at_the_temp_name_does_not_stop_the_write_and_keeps_its_files(self):
        self.temp.mkdir()
        (self.temp / "mine").write_text("keep")
        (self.temp / "sub").mkdir()
        self.written()
        self.assertEqual((self.temp / "mine").read_text(), "keep")
        self.assertTrue((self.temp / "sub").is_dir())

    def test_the_write_is_repeatable_with_a_directory_in_the_way(self):
        self.temp.mkdir()
        for text in ("one\n", "two\n", "three\n"):
            self.written(text)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["out.json", self.temp.name])

    def test_every_name_taken_by_a_directory_is_an_error_not_a_hang_or_a_removal(self):
        for suffix in ("", ".1", ".2", ".3", ".4", ".5", ".6", ".7"):
            Path(str(self.temp) + suffix).mkdir()
        with self.assertRaises(OSError):
            ts.write_at(self.fd, "out.json", "x")
        self.assertFalse((self.dir / "out.json").exists())
        self.assertTrue(all(Path(str(self.temp) + s).is_dir() for s in ("", ".7")))

    def test_a_path_with_no_directory_fd_is_written_the_same_way(self):
        self.temp.mkdir()
        ts.write_at(None, str(self.dir / "out.json"), "p\n")
        self.assertEqual((self.dir / "out.json").read_text(), "p\n")


if __name__ == "__main__":
    unittest.main()
