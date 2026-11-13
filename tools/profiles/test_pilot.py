"""tools/profiles/pilot.py pinned_catalog: the derived catalog that pins a bench arm's model loads through the real validator and
gives level small the model; a model the catalog lacks, or whose provider small has no entry for, raises ValueError.
Run: python3 -m unittest tools/profiles/test_pilot.py"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pilot  # noqa: E402
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
        for model, swapped in (("claude-haiku-5-5", False), ("claude-sonnet-5-5", True)):
            with self.subTest(model=model):
                pinned = pilot.pinned_catalog(SOURCE, model)
                loaded = self.load(pinned)
                small = next(level for level in loaded["levels"] if level["key"] == "small")
                self.assertIn(model, [m["model"] for m in small["models"]])
                self.assertEqual([m["provider"] for m in small["models"]],
                                 [m["provider"] for level in shipped["levels"] if level["key"] == "small" for m in level["models"]])
                self.assertEqual(models(loaded), models(shipped))   # a swap: no model added or lost
                self.assertEqual(pinned["levels"] != shipped["levels"], swapped)
        for model in ("not/a-model", "lmstudio/qwen/qwen3.6-35b-a3b"):
            with self.subTest(refused=model):
                with self.assertRaises(ValueError):
                    pilot.pinned_catalog(SOURCE, model)


if __name__ == "__main__":
    unittest.main()
