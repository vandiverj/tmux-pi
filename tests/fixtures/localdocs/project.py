"""Build throwaway projects from the localdocs fixtures. Symlinks and secret-looking files are made here at run time."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent
ROOT = FIXTURES.parent.parent.parent
sys.path.insert(0, str(ROOT / "lib"))


def make_project(base, packages=("plainpkg", "esmpkg", "reexp", "untyped", "@types/untyped", "@scope/scoped"), lock="package-lock.json"):
    """<base>/project with node_modules holding the named fixture packages and the named lockfile."""
    project = Path(base) / "project"
    modules = project / "node_modules"
    modules.mkdir(parents=True)
    for name in packages:
        shutil.copytree(FIXTURES / "packages" / name, modules / name)
    if lock:
        shutil.copy(FIXTURES / "locks" / lock, project / lock)
    return project


class ProjectCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.project = make_project(self.base)
        self.task = self.base / "task"
        self.task.mkdir()

    def write_lock(self, data, name="package-lock.json"):
        (self.project / name).write_text(data if isinstance(data, str) else json.dumps(data))
