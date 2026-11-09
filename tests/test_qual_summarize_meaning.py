"""LS1c MG1: tools/qual/summarize_meaning.py (packet, judge, score, calibrate). No model is run: the judge is a stub script.
The reference data is tests/calibration/summarize/meaning/ (the 2026-11-09 hand grading: 12 packets, 12 grade files, key, run replies)."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import contextlib
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "qual"))
import summarize_meaning as sm  # noqa: E402

MEANING = ROOT / "tests" / "calibration" / "summarize" / "meaning"
TOOL = ROOT / "tools" / "qual" / "summarize_meaning.py"
RUNS = ["base", "t1", "t2", "t3", "t4", "t5", "t6", "t7", "pi"]
KEY = json.loads((MEANING / "key.json").read_text())
CHORES = sorted(KEY)

# docs/plans/det-prep/run-log.md, "LS1c tune runs re-scored strict vs meaning": run -> meaning, meaning>=80%, recall, wrong, unsupported
RUN_LOG = {"base": (4, 4, "64%", 0, 0), "t1": (4, 4, "56%", 2, 0), "t2": (5, 6, "76%", 4, 1), "t3": (7, 8, "85%", 0, 0),
           "t4": (6, 8, "85%", 0, 0), "t5": (5, 8, "85%", 0, 0), "t6": (3, 7, "84%", 0, 2), "t7": (4, 8, "82%", 3, 0),
           "pi": (1, 1, "45%", 0, 40)}

STUB = r'''
import json, os, re, sys, time
sys.path.insert(0, sys.argv[0].rsplit("/", 1)[0])
import summarize_meaning as sm
prompt = sys.argv[-1]
log = os.environ["MG_LOG"]
with open(log + ".calls", "a") as h:
    h.write(json.dumps({"cwd": os.getcwd(), "argv": sys.argv[1:-1], "env": sorted(k for k in os.environ if k.startswith(("TMUX", "TP_", "CLAUDE_CODE"))),
                        "prompt": prompt}) + "\n")
n = sum(1 for _ in open(log + ".calls"))
mode = os.environ["MG_MODE"]
chore = re.search(r"^# Chore (\S+)$", prompt, re.M).group(1)
facts, replies = sm.packet_shape(prompt[prompt.index("# Chore "):])
grade = {"chore": chore, "replies": {rid: {"facts": {f: "met" for f in facts},
         "lines": [{"n": i, "verdict": "ok"} for i in range(1, c + 1)], "note": "stub"} for rid, c in replies.items()}}
if mode == "flaky" and n == 1:
    mode = "nonjson"
if mode == "missing_reply":
    del grade["replies"]["R2"]
elif mode == "missing_fact":
    del grade["replies"]["R1"]["facts"]["F1"]
elif mode == "bad_line":
    grade["replies"]["R1"]["lines"][0]["n"] = 99
elif mode == "bad_verdict":
    grade["replies"]["R1"]["lines"][0]["verdict"] = "fine"
elif mode == "short_lines":
    grade["replies"]["R1"]["lines"].pop()
elif mode == "nonjson":
    print("I think reply R1 is fine.")
    sys.exit(0)
elif mode == "exit":
    sys.exit(3)
elif mode == "timeout":
    time.sleep(60)
text = json.dumps(grade)
print("```json\n" + text + "\n```" if mode == "fenced" else text)
'''


def run_main(argv, env=None):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = sm.main(argv, {} if env is None else env)
        except SystemExit as exc:
            rc = exc.code
    return rc, out.getvalue(), err.getvalue()


def run_args(names=RUNS, directory=MEANING / "runs"):
    out = []
    for n in names:
        out += ["--run", "{}={}".format(n, directory / (n + ".jsonl"))]
    return out


def grade_files(directory):
    return {p.name: json.loads(p.read_text()) for p in Path(directory).glob("*.grade.json")}


class Tmp(unittest.TestCase):
    def tmp(self):
        d = tempfile.mkdtemp(prefix="tp-mg1-")
        self.addCleanup(shutil.rmtree, d, True)
        return Path(d)


class PacketTest(Tmp):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="tp-mg1-"))
        cls.keyed = cls.root / "keyed"
        assert run_main(["packet", *run_args(), "--key", str(MEANING / "key.json"), "--out", str(cls.keyed)])[0] == 0

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, True)

    def test_key_reproduces_the_committed_packets_and_key_byte_for_byte(self):
        for chore in CHORES:
            self.assertEqual((self.keyed / (chore + ".md")).read_bytes(), (MEANING / (chore + ".md")).read_bytes(), chore)
        self.assertEqual((self.keyed / "key.json").read_bytes(), (MEANING / "key.json").read_bytes())

    def test_same_seed_same_bytes_other_seed_changes_only_the_reply_order_and_the_key(self):
        outs = {}
        for name, seed in (("a", 7), ("b", 7), ("c", 8)):
            self.assertEqual(run_main(["packet", *run_args(), "--seed", str(seed), "--out", str(self.root / name)])[0], 0)
            outs[name] = self.root / name
        for chore in CHORES:
            self.assertEqual((outs["a"] / (chore + ".md")).read_bytes(), (outs["b"] / (chore + ".md")).read_bytes())
        self.assertEqual((outs["a"] / "key.json").read_bytes(), (outs["b"] / "key.json").read_bytes())
        self.assertNotEqual((outs["a"] / "key.json").read_bytes(), (outs["c"] / "key.json").read_bytes())
        differs = 0
        for chore in CHORES:
            a, c = [(outs[x] / (chore + ".md")).read_text() for x in "ac"]
            ha, ra = a.split("\n## Replies\n")
            hc, rc = c.split("\n## Replies\n")
            self.assertEqual(ha, hc, chore)  # task, files, facts, forbidden: untouched
            blocks = lambda body: sorted(b.split("\n", 1)[1] for b in body.split("### ")[1:])  # noqa: E731  reply texts without ids
            self.assertEqual(blocks(ra), blocks(rc), chore)
            differs += ra != rc
            self.assertEqual(sorted(json.loads((outs["a"] / "key.json").read_text())[chore].values()), sorted(RUNS))
        self.assertGreater(differs, 6)

    def test_exactly_one_of_seed_or_key_and_runs_must_cover_the_chore(self):
        key = str(MEANING / "key.json")
        for extra in ([], ["--seed", "1", "--key", key]):
            rc, _, err = run_main(["packet", *run_args(), *extra, "--out", str(self.root / "x")])
            self.assertEqual(rc, 2, extra)
            self.assertIn("exactly one of --seed or --key", err)
        rc, _, err = run_main(["packet", *run_args(RUNS[:3]), "--key", key, "--out", str(self.root / "x")])
        self.assertEqual(rc, 2)
        self.assertIn("--key has no order of exactly these runs", err)


class GradeShapeTest(unittest.TestCase):
    def test_every_committed_grade_is_valid_against_its_committed_packet(self):
        for chore in CHORES:
            facts, replies = sm.packet_shape((MEANING / (chore + ".md")).read_text())
            grade = json.loads((MEANING / (chore + ".grade.json")).read_text())
            # the hand grader numbered one line more than the tool counts in exports-worker R7 (a markdown table row); every
            # other one of the 108 replies has exactly the cited lines the tool counts
            if chore == "exports-worker":
                self.assertEqual(replies["R7"], 8)
                replies["R7"] = 9
            self.assertEqual(sm.validate_grade(grade, chore, facts, replies), None, chore)
            self.assertEqual(sorted(replies), sorted(KEY[chore]))


class JudgeTest(Tmp):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="tp-mg1-"))
        (cls.root / "stub.py").write_text(STUB)
        shutil.copy(TOOL, cls.root / "summarize_meaning.py")  # the stub imports the tool next to itself
        shutil.copy(ROOT / "tools" / "qual" / "summarize_run.py", cls.root)
        shutil.copy(ROOT / "tools" / "qual" / "summarize_check.py", cls.root)
        cls.packets = cls.root / "packets"
        cls.packets.mkdir()
        for chore in ("diff-order", "use-lrucache"):
            shutil.copy(MEANING / (chore + ".md"), cls.packets)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, True)

    def judge(self, mode, *extra, model="openai-codex/gpt-6-sol", env=None, only="diff-order"):
        out = self.tmp()
        log = out / "stub"
        base_env = {"MG_MODE": mode, "MG_LOG": str(log), "PATH": os.environ.get("PATH", "")}
        base_env.update(env or {})
        argv = ["judge", "--packets", str(self.packets), "--model", model, "--out", str(out / "grades"), "--only", only,
                "--pi-command", "{} {}".format(sys.executable, self.root / "stub.py"), *extra]
        rc, stdout, stderr = run_main(argv, base_env)
        calls = [json.loads(x) for x in Path(str(log) + ".calls").read_text().splitlines()] if Path(str(log) + ".calls").exists() else []
        logf = out / "grades" / "judge.jsonl"
        lines = [json.loads(x) for x in logf.read_text().splitlines()] if logf.exists() else []
        grade = out / "grades" / (only + ".grade.json")
        return dict(rc=rc, stderr=stderr, calls=calls, log=lines, grade=json.loads(grade.read_text()) if grade.exists() else None)

    def test_a_good_grade_is_written_with_one_log_line(self):
        for mode in ("good", "fenced"):
            r = self.judge(mode)
            self.assertEqual(r["rc"], 0, mode)
            self.assertEqual(len(r["calls"]), 1)
            self.assertEqual(r["grade"]["chore"], "diff-order")
            self.assertEqual(sorted(r["grade"]["replies"]), ["R%d" % i for i in range(1, 10)])
            self.assertEqual(sorted(r["grade"]["replies"]["R1"]["facts"]), ["F%d" % i for i in range(1, 9)])
            self.assertEqual(len(r["log"]), 1)
            self.assertEqual((r["log"][0]["model"], r["log"][0]["rc"], r["log"][0]["error"]), ("openai-codex/gpt-6-sol", 0, None))
            self.assertIsInstance(r["log"][0]["seconds"], float)

    def test_the_prompt_is_the_rubric_with_amendment_one_plus_the_packet_and_the_worker_flags(self):
        call = self.judge("good")["calls"][0]
        self.assertIn((MEANING / "RUBRIC.md").read_text().strip(), call["prompt"])
        self.assertIn("Amendment 1: absence claims", call["prompt"])
        self.assertIn((self.packets / "diff-order.md").read_text(), call["prompt"])
        self.assertEqual(call["argv"], ["-p", "--no-session", "--no-context-files", "--model", "openai-codex/gpt-6-sol", "--"])

    def test_a_bad_answer_is_judge_error_after_one_retry_never_a_pass(self):
        rows = (("missing_reply", "bad grade: replies"), ("missing_fact", "bad grade: R1: facts"), ("bad_line", "line indexes"),
                ("short_lines", "line indexes"), ("bad_verdict", "bad grade: R1: a line is not"), ("nonjson", "not JSON"),
                ("exit", "exit 3"))
        for mode, why in rows:
            r = self.judge(mode)
            self.assertEqual(r["rc"], 1, mode)
            self.assertEqual(len(r["calls"]), 2, mode)  # the first call and one retry
            self.assertEqual(len(r["log"]), 2, mode)
            self.assertTrue(all(why in x["error"] for x in r["log"]), (mode, r["log"]))
            self.assertEqual(sorted(r["grade"]), ["chore", "judge_error"])
            self.assertIn(why, r["grade"]["judge_error"])

    def test_a_timeout_is_judge_error_and_the_child_is_killed(self):
        r = self.judge("timeout", "--timeout", "1")
        self.assertEqual(r["rc"], 1)
        self.assertEqual([(x["rc"], x["error"]) for x in r["log"]], [(None, "timeout")] * 2)
        self.assertEqual(r["grade"]["judge_error"], "timeout")
        self.assertLess(sum(x["seconds"] for x in r["log"]), 20)  # killed at the timeout, not at the stub's 60 s sleep

    def test_the_retry_recovers_a_flaky_answer(self):
        r = self.judge("flaky")
        self.assertEqual(r["rc"], 0)
        self.assertEqual([x["error"] is None for x in r["log"]], [False, True])
        self.assertEqual(r["grade"]["chore"], "diff-order")

    def test_a_local_provider_or_qwen_model_is_refused_before_any_call(self):
        cat = self.tmp() / "catalog.json"
        cat.write_text(json.dumps({"providers": {"local": {"worker": "pi", "kind": "local"}}, "levels": [
            {"key": "local", "models": [{"provider": "local", "model": "acme/local-judge"}]}], "router": {"chain": []}}))
        rows = (("acme/local-judge", ["--catalog", str(cat)], "provider local"), ("lmstudio/qwen/qwen3.6-35b-a3b", [], "qwen"),
                ("slotstream/Qwen3.8-flash-next:4bit", [], "qwen"))
        for model, extra, why in rows:
            r = self.judge("good", *extra, model=model)
            self.assertEqual(r["rc"], 2, model)
            self.assertIn(why, r["stderr"])
            self.assertEqual((r["calls"], r["log"], r["grade"]), ([], [], None), model)
        # the shipped catalog lists the qwen local models; the refusal there comes from the catalog too
        self.assertIn("lmstudio/qwen/qwen3.6-35b-a3b", sm.local_models())
        self.assertIsNone(sm.refuse_model("openai-codex/gpt-6-sol"))
        self.assertIn("provider local", sm.refuse_model("acme/local-judge", cat))
        rc, _, _ = run_main(["judge", "--packets", str(self.packets), "--out", str(self.tmp())])
        self.assertEqual(rc, 2)  # --model has no default

    def test_the_child_env_has_no_session_and_runs_in_a_fresh_temp_dir(self):
        env = {"TMUX": "/tmp/x,1,1", "TMUX_PANE": "%1", "TP_SESSION": "dummy", "TP_TASK_ID": "dummy", "TP_STATE_DIR": "/tmp/s",
               "CLAUDE_CODE_SESSION_ID": "c", "TP_LOCAL_CONFIG": "/tmp/local.json", "TP_PI_NODE": "/tmp/node"}
        call = self.judge("good", env=env)["calls"][0]
        self.assertEqual(call["env"], ["TP_LOCAL_CONFIG", "TP_PI_NODE"])
        self.assertNotEqual(Path(call["cwd"]).resolve(), ROOT)
        self.assertIn("tp-qual-meaning-", call["cwd"])
        self.assertFalse(Path(call["cwd"]).exists())


class ScoreTest(Tmp):
    def stats(self):
        grades, errors = sm.load_grades(MEANING, KEY)
        self.assertEqual(errors, {})
        return sm.run_stats(grades, errors, KEY)

    def test_the_committed_grades_give_the_run_log_meaning_table_and_no_run_meets_the_bar(self):
        stats = self.stats()
        got = {n: (s["meaning"], s["meaning80"], "{:.0%}".format(s["recall"]), s["wrong"], s["unsupported"]) for n, s in stats.items()}
        self.assertEqual(got, RUN_LOG)
        self.assertEqual({n: s["bar"] for n, s in stats.items()}, {n: False for n in RUN_LOG})
        self.assertEqual(stats["base"]["lines"], 82)
        self.assertEqual(stats["t7"]["lines"], 285)

    def test_cli_text_and_json(self):
        rc, text, _ = run_main(["score", "--grades", str(MEANING)])
        self.assertEqual(rc, 0)
        rows = {ln.split()[0]: ln.split() for ln in text.splitlines()[1:10]}
        self.assertEqual(rows["t3"][1:4], ["7/12", "8/12", "85%"])
        self.assertEqual(rows["pi"][1:6], ["1/12", "1/12", "45%", "0", "40"])
        self.assertIn("bar t3: no", text)
        r = subprocess.run([sys.executable, str(TOOL), "score", "--grades", str(MEANING), "--json"], capture_output=True, text=True,
                           env={"PATH": os.environ.get("PATH", "")})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["runs"]["base"]["meaning"], 4)

    def test_strict_pass_comes_from_the_run_records(self):
        d = self.tmp()
        for n in ("base", "t3"):
            (d / (n + ".jsonl")).write_text("".join(json.dumps({"chore": c, "pass": (i % 2 == 0) or n == "t3"}) + "\n"
                                                    for i, c in enumerate(CHORES)) + json.dumps({"summary": True}) + "\n")
        rc, text, _ = run_main(["score", "--grades", str(MEANING), *run_args(["base", "t3"], d)])
        rows = {ln.split()[0]: ln.split() for ln in text.splitlines()[1:]}
        self.assertEqual((rows["base"][1], rows["t3"][1]), ("6/12", "12/12"))

    def perfect(self, n_facts=10):
        key = {"c1": {"R1": "a", "R2": "b"}, "c2": {"R1": "b", "R2": "a"}}
        rep = lambda met: {"facts": {"F%d" % i: ("met" if i <= met else "not_met") for i in range(1, n_facts + 1)},  # noqa: E731
                           "lines": [{"n": 1, "verdict": "ok"}]}
        grades = {c: {"chore": c, "replies": {"R1": rep(n_facts), "R2": rep(n_facts)}} for c in key}
        return key, grades, rep

    def test_the_bar_needs_zero_wrong_zero_unsupported_no_error_recall_90_and_every_chore_80(self):
        key, grades, rep = self.perfect()
        self.assertTrue(sm.run_stats(grades, {}, key)["a"]["bar"])
        rows = {}
        g = copy.deepcopy(grades); g["c1"]["replies"]["R1"]["lines"][0]["verdict"] = "wrong"; rows["wrong"] = (g, {})
        g = copy.deepcopy(grades); g["c1"]["replies"]["R1"]["lines"][0]["verdict"] = "unsupported"; rows["unsupported"] = (g, {})
        rows["judge_error"] = ({"c1": None, "c2": grades["c2"]}, {"c1": "judge_error: timeout"})
        g = copy.deepcopy(grades); g["c1"]["replies"]["R1"] = rep(8); g["c2"]["replies"]["R2"] = rep(8)  # a=R1 in c1, R2 in c2
        rows["recall 80% < 90%"] = (g, {})
        g = copy.deepcopy(grades); g["c1"]["replies"]["R1"] = rep(7); rows["a chore at 70% (recall 95%)"] = (g, {})
        for name, (g, errors) in rows.items():
            self.assertFalse(sm.run_stats(g, errors, key)["a"]["bar"], name)
        g = copy.deepcopy(grades); g["c1"]["replies"]["R1"] = rep(8); g["c2"]["replies"]["R2"] = rep(10)  # 90% overall, 80% per chore
        self.assertTrue(sm.run_stats(g, {}, key)["a"]["bar"])
        self.assertEqual(sm.need(5), 4)
        self.assertEqual(sm.need(6), 5)

    def test_a_missing_or_malformed_grade_file_is_unparsed_and_fails_the_bar(self):
        d = self.tmp()
        for chore in CHORES:
            shutil.copy(MEANING / (chore + ".grade.json"), d)
        (d / "diff-order.grade.json").write_text("{not json")
        (d / "steps-states.grade.json").unlink()
        (d / "sum-worker.grade.json").write_text(json.dumps({"chore": "sum-worker", "judge_error": "timeout"}))
        grades, errors = sm.load_grades(d, KEY)
        self.assertEqual(sorted(errors), ["diff-order", "steps-states", "sum-worker"])
        self.assertTrue(errors["sum-worker"].startswith("judge_error"))
        self.assertFalse(any(s["bar"] for s in sm.run_stats(grades, errors, KEY).values()))


class CalibrateTest(Tmp):
    def copy_reference(self):
        d = self.tmp()
        for p in MEANING.glob("*.grade.json"):
            shutil.copy(p, d)
        return d

    def mutate(self, edit):
        d = self.copy_reference()
        for chore in CHORES:
            path = d / (chore + ".grade.json")
            grade = json.loads(path.read_text())
            edit(chore, grade)
            path.write_text(json.dumps(grade))
        return d

    def calibrate(self, judge):
        rc, text, _ = run_main(["calibrate", "--judge", str(judge), "--reference", str(MEANING), "--key", str(MEANING / "key.json"), "--json"])
        return rc, json.loads(text)

    def test_the_reference_against_itself_passes_with_100_percent_and_no_disagreement(self):
        rc, r = self.calibrate(MEANING)
        self.assertEqual(rc, 0)
        self.assertEqual((r["fact_agreement"]["rate"], r["line_agreement"]["rate"], r["precision"]["found_rate"]), (1.0, 1.0, 1.0))
        self.assertEqual((r["precision"]["reference_bad"], r["precision"]["added"], r["disagreements"]), (52, 0, []))
        self.assertEqual(r["fact_agreement"]["total"], 9 * 80)
        self.assertEqual({k: v["gap"] for k, v in r["runs"].items()}, {k: 0.0 for k in RUN_LOG})
        self.assertTrue(r["passed"] and all(r["checks"].values()))

    def first_line(self, verdict):
        """(chore, reply, n) of the first line the reference marked with verdict."""
        for chore in CHORES:
            grade = json.loads((MEANING / (chore + ".grade.json")).read_text())
            for rid, rep in grade["replies"].items():
                for ln in rep["lines"]:
                    if ln["verdict"] == verdict:
                        return chore, rid, ln["n"]

    def test_three_flipped_facts_and_one_removed_wrong_line_are_reported_exactly(self):
        flips = [("diff-order", "R1", f) for f in ("F1", "F2", "F3")]  # run t7: met -> not_met
        gone = self.first_line("wrong")

        def edit(chore, grade):
            for c, rid, fid in flips:
                if c == chore:
                    self.assertEqual(grade["replies"][rid]["facts"][fid], "met")
                    grade["replies"][rid]["facts"][fid] = "not_met"
            if chore == gone[0]:
                rep = grade["replies"][gone[1]]
                rep["lines"] = [ln for ln in rep["lines"] if ln["n"] != gone[2]]
        judge = self.mutate(edit)
        rc, r = self.calibrate(judge)
        got = sorted((d["kind"], d["chore"], d["reply"], d["item"], d["reference"], d["judge"]) for d in r["disagreements"])
        want = sorted([("fact", "diff-order", "R1", f, "met", "not_met") for f in ("F1", "F2", "F3")]
                      + [("line", gone[0], gone[1], gone[2], "wrong", "missing")])
        self.assertEqual(got, want)
        self.assertEqual(r["fact_agreement"]["agree"], 9 * 80 - 3)
        self.assertEqual((r["precision"]["reference_bad"], r["precision"]["found"], r["precision"]["added"]), (52, 51, 0))
        self.assertAlmostEqual(r["runs"]["t7"]["gap"], 3 / 80)  # 3 of the run's 80 facts
        self.assertEqual({k for k, v in r["runs"].items() if v["gap"]}, {"t7"})
        self.assertTrue(r["passed"], r["checks"])  # 99.6% facts, 98% found, 0 added, gap 3.75 points
        text = run_main(["calibrate", "--judge", str(judge), "--reference", str(MEANING), "--key", str(MEANING / "key.json")])
        self.assertEqual(text[0], 0)
        self.assertIn("disagreements: 4", text[1])
        self.assertIn("fact diff-order R1 (t7) F1: reference=met judge=not_met", text[1])
        self.assertIn("CALIBRATION PASS", text[1])

    def test_each_rule_fails_on_its_own(self):
        def flip_met(limit, only_run=None, per_reply=None):
            left = [limit]

            def edit(chore, grade):
                for rid, rep in grade["replies"].items():
                    if only_run and KEY[chore][rid] != only_run:
                        continue
                    n = 0
                    for fid, v in rep["facts"].items():
                        if v == "met" and left[0] > 0 and (per_reply is None or n < per_reply):
                            rep["facts"][fid] = "not_met"
                            left[0] -= 1
                            n += 1
            return edit

        def all_ok(chore, grade):  # the judge calls every line ok: it finds none of the reference's 52
            for rep in grade["replies"].values():
                for ln in rep["lines"]:
                    ln["verdict"] = "ok"

        added = [6]

        def add_bad(chore, grade):  # six lines the reference called ok are marked wrong
            for rep in grade["replies"].values():
                for ln in rep["lines"]:
                    if ln["verdict"] == "ok" and added[0] > 0:
                        ln["verdict"] = "wrong"
                        added[0] -= 1

        rows = (("fact_agreement", flip_met(80)), ("precision_found", all_ok), ("precision_added", add_bad),
                ("recall_gap", flip_met(100, only_run="t3", per_reply=2)))  # t3 loses 2 facts a chore: 6+ points
        for check, edit in rows:
            rc, r = self.calibrate(self.mutate(edit))
            self.assertEqual(rc, 1, check)
            self.assertFalse(r["passed"])
            self.assertFalse(r["checks"][check], (check, r["checks"]))
            others = [k for k, v in r["checks"].items() if not v]
            self.assertEqual(others if check != "fact_agreement" else [k for k in others if k != "recall_gap"], [check], check)

    def test_a_chore_the_judge_could_not_grade_fails_calibration(self):
        d = self.copy_reference()
        (d / "diff-order.grade.json").write_text(json.dumps({"chore": "diff-order", "judge_error": "timeout"}))
        rc, r = self.calibrate(d)
        self.assertEqual(rc, 1)
        self.assertFalse(r["checks"]["judged_every_chore"])
        self.assertEqual(r["fact_agreement"]["agree"], 9 * 80 - 72)  # diff-order's 9 replies x 8 facts count as disagreements
        self.assertEqual(r["judge_errors"]["diff-order"], "judge_error: timeout")

    def test_cli_exit_code_and_an_invalid_reference_is_refused(self):
        args = ["calibrate", "--judge", str(MEANING), "--reference", str(MEANING), "--key", str(MEANING / "key.json")]
        r = subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, env={"PATH": os.environ.get("PATH", "")})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("CALIBRATION PASS", r.stdout)
        empty = self.tmp()
        rc, _, err = run_main(["calibrate", "--judge", str(MEANING), "--reference", str(empty), "--key", str(MEANING / "key.json")])
        self.assertEqual(rc, 2)
        self.assertIn("the reference set is not valid", err)


if __name__ == "__main__":
    unittest.main()
