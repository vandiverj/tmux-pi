import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
from calibration import evaluate, load_answers, load_corpus, report, summary  # noqa: E402
from router import RouterError, load_model  # noqa: E402

CORPUS = ROOT / "tests/calibration/corpus.jsonl"
ANSWERS = ROOT / "tests/calibration/answers.jsonl"
MODEL = ROOT / "config/laya-model.json"


class CalibrationTests(unittest.TestCase):
    def test_corpus_is_valid_and_recorded(self):
        corpus = load_corpus(CORPUS)
        results, missing = evaluate(corpus, load_answers(ANSWERS), load_model(MODEL))
        self.assertEqual(missing, [], "run bin/tp-calibrate --record")
        self.assertEqual(len(results), len(corpus))
        self.assertIn("holdout", report(results, missing))

    def test_shipped_model_never_skips_a_required_review(self):
        results, _ = evaluate(load_corpus(CORPUS), load_answers(ANSWERS), load_model(MODEL))
        self.assertEqual(summary(results)["required_skipped"], 0)

    def test_fit_reproduces_shipped_model(self):
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest("training needs numpy")
        from calibration import fit
        shipped = load_model(MODEL)
        model = fit(load_corpus(CORPUS), load_answers(ANSWERS))
        for head in ("route", "intent", "gate", "level"):
            self.assertEqual(model[head]["labels"], shipped[head]["labels"])
            self.assertEqual(model[head]["lambda"], shipped[head]["lambda"])
            for got, want in zip(model[head]["bias"], shipped[head]["bias"]):
                self.assertAlmostEqual(got, want, places=4)

    def test_rejects_bad_rows(self):
        good = {"id": "a", "task": "t", "intent": "research", "route": "simple", "review": "none", "split": "tune"}
        cases = [
            ({**good, "route": "fallback"}, "route must be one of"),
            ({**good, "intent": "deploy"}, "intent must be one of"),
            ({**good, "split": "train"}, "split must be one of"),
            ({**good, "extra": 1}, "needs exactly"),
            ({**good, "id": "Bad Id"}, "id must be"),
            ({**good, "task": " "}, "task must contain text"),
        ]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "corpus.jsonl"
            for row, message in cases:
                with self.subTest(message=message):
                    path.write_text(json.dumps(row) + "\n")
                    with self.assertRaisesRegex(RouterError, message):
                        load_corpus(path)
            path.write_text(json.dumps(good) + "\n" + json.dumps(good) + "\n")
            with self.assertRaisesRegex(RouterError, "duplicate id"):
                load_corpus(path)

    def test_changed_task_text_needs_rerecording(self):
        corpus = load_corpus(CORPUS)[:1]
        answers = load_answers(ANSWERS)
        corpus[0] = {**corpus[0], "task": corpus[0]["task"] + " Now."}
        self.assertEqual(evaluate(corpus, answers, load_model(MODEL))[1], [corpus[0]["id"]])

    def test_changed_questions_need_rerecording(self):
        corpus = load_corpus(CORPUS)[:1]
        answers = load_answers(ANSWERS)
        answers[corpus[0]["id"]]["questions"] = "0" * 64
        self.assertEqual(evaluate(corpus, answers, load_model(MODEL))[1], [corpus[0]["id"]])


if __name__ == "__main__":
    unittest.main()
