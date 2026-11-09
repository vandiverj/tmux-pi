"""tests/lib_closure.py: the check that a partial install holds every lib module its files import (in-process, scratch dirs only)."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
from pathlib import Path
import tempfile
import unittest

import lib_closure

ROOT = Path(__file__).resolve().parent.parent

SOURCE = {"a.py": "x = 1\n", "b.py": "x = 1\n", "c.py": "x = 1\n", "d.py": "x = 1\n", "pkg/__init__.py": "", "pkg/inner.py": "x = 1\n"}


class LibClosureTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.source = self.base / "source/lib"
        self.install = self.base / "install"
        self.put(self.base / "source/lib", SOURCE)
        (self.install / "bin").mkdir(parents=True)

    def put(self, folder, files):
        for name, text in files.items():
            path = folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def install_lib(self, files, bin=None):
        self.put(self.install / "lib", files)
        for name, text in (bin or {}).items():
            self.put(self.install / "bin", {name: text})

    def missing(self, **kw):
        return lib_closure.missing_imports(self.source, self.install, **kw)

    def test_each_kind_of_import_is_followed_and_each_gap_names_its_importer(self):
        cases = [
            ("top-level import", {"m.py": "import a\n"}, None, {"a": ["m"]}),
            ("from import", {"m.py": "from b import x\n"}, None, {"b": ["m"]}),
            ("dotted import", {"m.py": "import pkg.inner\n"}, None, {"pkg": ["m"]}),
            ("function-level import", {"m.py": "def f():\n    import c\n"}, None, {"c": ["m"]}),
            ("transitive: the copied module's own import", {"m.py": "import a\n", "a.py": "import d\n"}, None, {"d": ["a"]}),
            ("standard library and unknown names", {"m.py": "import os, json\nfrom nothere import y\n"}, None, {}),
            ("relative import stays in its package", {"pkg/__init__.py": "", "pkg/x.py": "from . import inner\n"}, None, {}),
            ("a python script in bin", {}, {"tool": "#!/usr/bin/env python3\nimport a\n"}, {"a": ["bin/tool"]}),
            ("a shell script naming lib/NAME.py", {}, {"tool": "#!/usr/bin/env bash\npython3 \"$ROOT/lib/b.py\"\n"}, {"b": ["bin/tool"]}),
            ("two importers of one module", {"m.py": "import a\n", "n.py": "import a\n"}, None, {"a": ["m", "n"]}),
            ("everything present", {"m.py": "import a\nimport pkg\n", "a.py": "", "pkg/__init__.py": ""}, None, {}),
        ]
        for label, files, bin, want in cases:
            with self.subTest(label):
                self.setUp()
                self.install_lib(files, bin)
                self.assertEqual(self.missing(), want)

    def test_a_stub_is_read_as_the_stub(self):
        # tests replace a module with a stub that imports nothing: the real module's imports are not the install's
        self.install_lib({"m.py": "import a\n", "a.py": "def stub():\n    return 1\n"})
        (self.source / "a.py").write_text("import d\n")
        self.assertEqual(self.missing(), {})

    def test_only_the_allowed_importer_of_a_module_is_excused(self):
        self.install_lib({"m.py": "import a\n"}, {"tool": "#!/usr/bin/env python3\ndef run():\n    import a\n"})
        self.assertEqual(self.missing(), {"a": ["bin/tool", "m"]})
        self.assertEqual(self.missing(allowed=[("a", "bin/tool")]), {"a": ["m"]})

    def test_the_message_names_the_module_and_who_imports_it(self):
        text = lib_closure.describe({"command_wrappers": ["sensitive_read"]})
        self.assertIn("lib/command_wrappers", text)
        self.assertIn("imported by sensitive_read", text)

    def test_the_real_lib_is_read_without_error(self):
        # every lib module and bin script parses with the running Python; a copy of all of lib/ is never missing a module
        install = self.base / "everything"
        (install / "bin").mkdir(parents=True)
        (install / "lib").symlink_to(ROOT / "lib")
        self.assertEqual(lib_closure.missing_imports(ROOT / "lib", install), {})


if __name__ == "__main__":
    unittest.main()
