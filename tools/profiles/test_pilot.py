"""tools/profiles/pilot.py pinned_catalog: the derived catalog that pins a bench arm's model loads through the real validator and
gives level small that model alone; a model the catalog lacks, or whose provider small has no entry for, raises ValueError.
Run: python3 -m unittest tools/profiles/test_pilot.py"""
import contextlib
import io
import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pilot  # noqa: E402
import route_resolve  # noqa: E402
import router_catalog  # noqa: E402

SOURCE = ROOT / "config/model-catalog.json"


def models(catalog):
    return sorted(m["model"] for level in catalog["levels"] for m in level.get("models", []))


class PinnedCatalogTests(unittest.TestCase):
    def load(self, catalog):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            path.write_text(json.dumps(catalog))
            return router_catalog.load_catalog(path)

    def test_rows(self):
        shipped = json.loads(SOURCE.read_text())
        for model in ("claude-haiku-5-5", "claude-sonnet-5-5"):
            with self.subTest(model=model):
                pinned = pilot.pinned_catalog(SOURCE, model)
                loaded = self.load(pinned)
                small = next(level for level in loaded["levels"] if level["key"] == "small")
                self.assertEqual([m["model"] for m in small["models"]], [model])   # one model: no OpenAI or other alternative
                self.assertEqual(models(loaded), models(shipped))   # no model added or lost (the router's chain stays valid)
                # the real resolver, as bench dispatches (explicit --level small, role coder): healthy -> the pin; the pinned model or its
                # whole provider exhausted -> a refusal, never another model at any level (the resolver's fallback goes up, then down)
                decision = {"level_key": "small", "effort": "medium", "review": "optional", "shape": None, "source": "explicit_level"}
                provider = next(m["provider"] for level in loaded["levels"] for m in level["models"] if m["model"] == model)
                for label, health, expect in (("healthy", {}, model), ("model exhausted", {f"{provider}:{model}": {"status": "exhausted"}}, None),
                                              ("provider exhausted", {provider: {"status": "exhausted"}}, None)):
                    with self.subTest(model=model, health=label):
                        rec = route_resolve.resolve(dict(decision), loaded, health, {"role": "coder", "skip_caps": True})
                        self.assertEqual(rec.get("model"), expect)
                        if expect is None:
                            self.assertEqual(rec["source"], "stop_and_ask")
        for model in ("not/a-model", "lmstudio/qwen/qwen3.6-35b-a3b"):
            with self.subTest(refused=model):
                with self.assertRaises(ValueError):
                    pilot.pinned_catalog(SOURCE, model)

    def test_relative_out_gives_absolute_catalogs_and_commands_survive_spaces(self):
        seen = []

        def stub(box, checkout, brief, catalog, profile):
            seen.append(Path(catalog))
            small = next(level for level in json.loads(Path(catalog).read_text())["levels"] if level["key"] == "small")
            return b"packet", small["models"][0]["model"], "claude"

        here, original = os.getcwd(), pilot.Sandbox.dispatch
        with tempfile.TemporaryDirectory() as tmp:
            pilot.Sandbox.dispatch = stub
            try:
                os.chdir(tmp)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(pilot.dry_run("rel dir", str(ROOT), None, list(pilot.TASKS)), 0)
            finally:
                os.chdir(here)
                pilot.Sandbox.dispatch = original
            self.assertTrue(seen and all(p.is_absolute() and p.is_file() for p in seen))   # valid from the sandbox cwd, not just here
            summary = json.loads((Path(tmp).resolve() / "rel dir" / "summary.json").read_text())
            for line in summary["commands"]:
                argv = shlex.split(line)
                env = [argv[i + 1] for i, a in enumerate(argv) if a == "--env"]
                self.assertEqual(len(env), 2)
                catalog = next(e for e in env if e.startswith("TP_MODEL_CATALOG=")).split("=", 1)[1]
                self.assertTrue(Path(catalog).is_absolute() and " " in catalog and Path(catalog).is_file())
        spaced = pilot.bench_commands("/tmp/M1 checkout", {"haiku": "/tmp/M1 art/h.json", "sonnet": "/tmp/M1 art/s.json"}, ["t"])
        argv = shlex.split(spaced[0])
        self.assertEqual(argv[argv.index("--arm") + 1], "/tmp/M1 checkout")
        self.assertIn("TP_MODEL_CATALOG=/tmp/M1 art/h.json", argv)


if __name__ == "__main__":
    unittest.main()
