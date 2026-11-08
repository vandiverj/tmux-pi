"""Every module under lib/ imports under the interpreter running the tests (the owner's machines use /usr/bin/python3, 3.9).

`X | None` in a dataclass field is evaluated at class creation and raises TypeError before 3.10 unless the file has
`from __future__ import annotations`. One subprocess imports each module (package-qualified, lib on the path, as the
other tests set it up) and the test names every module that does not import."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "lib"

PROBE = r"""
import importlib, json, sys
sys.path.insert(0, sys.argv[2])
bad = {}
for name in json.loads(sys.argv[1]):
    try:
        importlib.import_module(name)
    except BaseException as e:  # a module that exits at import time is also a failure to import
        bad[name] = "%s: %s" % (type(e).__name__, e)
print(json.dumps(bad))
"""


def module_names():
    names = []
    for path in sorted(LIB.rglob("*.py")):
        parts = list(path.relative_to(LIB).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            names.append(".".join(parts))
    return names


class LibImportsCase(unittest.TestCase):
    def test_every_lib_module_imports(self):
        names = module_names()
        self.assertGreater(len(names), 20)
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        proc = subprocess.run([sys.executable, "-I", "-c", PROBE, json.dumps(names), str(LIB)], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        bad = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(bad, {}, "modules that do not import on Python %d.%d:\n%s" % (
            sys.version_info[:2] + ("\n".join("  %s  %s" % kv for kv in sorted(bad.items())),)))


if __name__ == "__main__":
    unittest.main()
