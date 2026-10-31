"""Build throwaway projects from the localdocs fixtures. Symlinks and secret-looking files are made here at run time."""
import hashlib
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


def install_deno_remote(deno_dir, hosts=("jsr.io", "deno.land"), layout="sidecar"):
    """Fill `<deno_dir>/remote/https/<host>/<sha256 of the URL path>` from the fixture tree `remote/<host>/<path>`. `layout` is how
    Deno records the URL: "sidecar" writes a `.metadata.json` beside the file (older Deno); "embedded" appends
    `\\n// denoCacheMetadata={json}` to the file (Deno 2.x, as seen in a real cache). Returns {url: sha256 of the content without
    any trailer}, the shape of a deno.lock "remote" section."""
    hashes = {}
    for host in hosts:
        base = FIXTURES / "remote" / host
        for file in sorted(p for p in base.rglob("*") if p.is_file()):
            path = "/" + file.relative_to(base).as_posix()
            target = Path(deno_dir) / "remote" / "https" / host
            target.mkdir(parents=True, exist_ok=True)
            name = hashlib.sha256(path.encode()).hexdigest()
            record = {"headers": {}, "url": f"https://{host}{path}", "time": 1786123343}
            if layout == "embedded":
                (target / name).write_bytes(file.read_bytes() + b"\n// denoCacheMetadata=" + json.dumps(record, separators=(",", ":")).encode())
            else:
                shutil.copy(file, target / name)
                (target / (name + ".metadata.json")).write_text(json.dumps(record))
            hashes[f"https://{host}{path}"] = hashlib.sha256(file.read_bytes()).hexdigest()
    return hashes


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
