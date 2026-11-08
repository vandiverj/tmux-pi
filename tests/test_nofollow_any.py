"""The interpreter-independent macOS no-link-on-the-way flag."""
import session_env  # noqa: F401  first: discard the caller's session
import errno
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from ctxengine import graph


class NoFollowAnyCase(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "macOS kernel flag")
    def test_intermediate_link_is_refused_by_the_kernel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "a").mkdir()
            (root / "target").mkdir()
            (root / "target" / "b").write_bytes(b"ok")
            (root / "a" / "link").symlink_to(root / "target", target_is_directory=True)
            fd = os.open(str(root / "target" / "b"), os.O_RDONLY | graph.NOFOLLOW_ANY)
            os.close(fd)
            try:
                fd = os.open(str(root / "a" / "link" / "b"), os.O_RDONLY | graph.NOFOLLOW_ANY)
            except OSError as error:
                self.assertEqual(error.errno, errno.ELOOP)
            else:
                os.close(fd)
                self.fail("NOFOLLOW_ANY followed an intermediate link")

    def test_flag_selection_without_the_interpreter_constant(self):
        with mock.patch.dict(vars(os)):
            vars(os).pop("O_NOFOLLOW_ANY", None)
            for platform, error, expected in (
                ("darwin", None, 0x20000000),
                ("darwin", OSError(errno.EINVAL, "unsupported flag"), os.O_NOFOLLOW),
                ("linux", None, os.O_NOFOLLOW),
                ("freebsd", None, os.O_NOFOLLOW),
            ):
                with self.subTest(platform=platform, error=error), mock.patch.object(sys, "platform", platform), \
                        mock.patch.object(os, "open", return_value=123, side_effect=error) as opened, \
                        mock.patch.object(os, "close") as closed:
                    self.assertEqual(graph._nofollow_any_flag(), expected)
                    if platform == "darwin":
                        opened.assert_called_once_with(os.sep, graph.DIR_FLAGS | 0x20000000)
                        if error is None:
                            closed.assert_called_once_with(123)
                        else:
                            closed.assert_not_called()
                    else:
                        opened.assert_not_called()
                        closed.assert_not_called()

    def test_exported_flag_needs_no_probe_and_other_errors_are_not_hidden(self):
        with mock.patch.object(os, "O_NOFOLLOW_ANY", 0x20000000, create=True), mock.patch.object(os, "open") as opened:
            self.assertEqual(graph._nofollow_any_flag(), 0x20000000)
            opened.assert_not_called()
        with mock.patch.dict(vars(os)):
            vars(os).pop("O_NOFOLLOW_ANY", None)
            with mock.patch.object(sys, "platform", "darwin"), \
                    mock.patch.object(os, "open", side_effect=OSError(errno.EACCES, "denied")):
                with self.assertRaises(OSError) as raised:
                    graph._nofollow_any_flag()
                self.assertEqual(raised.exception.errno, errno.EACCES)


if __name__ == "__main__":
    unittest.main()
