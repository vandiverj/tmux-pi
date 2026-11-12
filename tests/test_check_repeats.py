"""tools/checks/repeats.py (HB5a, VR1 arm A): the derivation rules as one table, and the case-1 planted schedule scored pair by
pair.

The task directories are producer-shaped, copied field for field from:
  test-runs/<n>.json   tools/agent/test-run.sh:133-134 (tp-test-run:v1: label, cmd, cwd, head, tree, exit, timed_out, seconds,
                       log, summary; no start or end, the file time is the end)
  gate.json, gate-*    tools/agent/pre-commit-gate.sh:676-679 (tp-gate:v1: worktree, base{ref,sha}, head, tree, files,
                       checks[{name,status,exit,seconds}], exit, started, finished, log); the previous gate.json is renamed
                       gate-YYYYMMDD-HHMMSS-PID.json (:686-688)
  tests.jsonl          tests/run_bounded.py:189-190 (cmd, files, seconds, rc, timed_out, load1, per_file, leftovers)
  meta.json            bin/tp-spawn (id, role, lane, lane_step, workdir, created); review-of from bin/tp-review
"""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("check_repeats", ROOT / "tools" / "checks" / "repeats.py")
repeats = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repeats)

T, T2 = "a" * 40, "b" * 40
WT = "/work/tmux-pi-wt-x"
PY = ["python3", "-m", "unittest"]


class Tasks:
    """A tasks dir with producer-shaped records; `run` writes the next test-run record of a task with end time `at`."""

    def __init__(self, root):
        self.root = Path(root)
        self.n = {}

    def task(self, ident, role="coder", lane="ln", step="S1", review_of=None):
        d = self.root / ident
        (d / "test-runs").mkdir(parents=True, exist_ok=True)
        meta = {"id": ident, "worker": "claude", "created": "2026-11-12T08:00:00Z", "lane": lane, "lane_step": step,
                "workdir": WT}
        if role and not review_of:
            meta["role"] = role
        (d / "meta.json").write_text(json.dumps(meta))
        if review_of:
            (d / "review-of").write_text(review_of + "\n")
        return d

    def run(self, ident, cmd, tree=T, exit=0, seconds=1.0, at=0, label="", cwd=WT):
        n = self.n[ident] = self.n.get(ident, 0) + 1
        path = self.root / ident / "test-runs" / ("%d.json" % n)
        path.write_text(json.dumps({"schema": "tp-test-run:v1", "label": label, "cmd": cmd, "cwd": cwd, "head": "c" * 40,
                                    "tree": tree, "exit": exit, "timed_out": exit == 124, "seconds": seconds,
                                    "log": "/tmp/tp-tools/x.log", "summary": ""}))
        os.utime(path, (1_800_000_000 + at, 1_800_000_000 + at))
        return str(path)

    def gate(self, ident, name, tree=T, checks=(("prettier:web", "passed", 0, 2.0),), files=("web/a.ts",), finished="10",
             head="c" * 40, base="d" * 40):
        (self.root / ident / name).write_text(json.dumps({
            "schema": "tp-gate:v1", "worktree": WT, "base": {"ref": "main", "sha": base}, "head": head, "tree": tree,
            "files": list(files), "checks": [{"name": c, "status": s, "exit": e, "seconds": sec} for c, s, e, sec in checks],
            "exit": 0, "started": "2026-11-12T08:00:00Z", "finished": "2026-11-12T08:00:%sZ" % finished,
            "log": "/tmp/tp-tools/g.log"}))


def by_path(report):
    return {r["path"]: r for r in report["runs"]}


class RepeatsTest(unittest.TestCase):
    def test_rules(self):
        """One row per rule; each builds two runs a (earlier) and b (later) and states b's class, its repeated seconds, and
        the totals the row proves. `naive` names the obvious reading the rule replaces (it would have said duplicate)."""
        ut = PY + ["tests.test_x"]
        live_ut = PY + ["tests.test_qual_summarize_structured.LiveLmStudioTests"]
        rows = [
            # name, a(cmd, tree, exit, seconds), b(...), expected b class, b repeated seconds, extra checks
            ("D1 same cmd+tree", (ut, T, 0, 3), (ut, T, 0, 3), "duplicate", 3, {}),
            ("D1 same cmd other tree", (ut, T, 0, 3), (ut, T2, 0, 3), "first", 0, {}),
            ("N1 other cwd spelling (abs path in worktree, other worktree path)",
             (PY + [WT + "/tests/test_x.py"], T, 0, 3), (PY + ["tests/test_x.py"], T, 0, 3, "/work/other-wt"), "duplicate", 3, {}),
            ("N1 cd tests && ... is not understood (naive: duplicate)",
             (PY + ["test_x"], T, 0, 3), (["bash", "-c", "cd tests && python3 -m unittest test_x"], T, 0, 3), "outside", 0,
             {"outside": 1, "reason": "not understood: cd"}),
            ("N2 other interpreter (naive: duplicate)", (ut, T, 0, 3), (["/usr/bin/python3"] + ut[1:], T, 0, 3), "first", 0,
             {"interpreter_spelling_only": 1}),
            ("N2 interpreter path spelling", (["/usr/bin//python3"] + ut[1:], T, 0, 3), (["/usr/bin/python3"] + ut[1:], T, 0, 3),
             "duplicate", 3, {}),
            ("N3 TMUX_TMPDIR value", (["env", "TMUX_TMPDIR=/tmp/a.1"] + ut, T, 0, 3), (["env", "TMUX_TMPDIR=/tmp/b.2"] + ut, T, 0, 3),
             "duplicate", 3, {}),
            ("N3 other env value stays", (["env", "QUAL_ATTACK_COUNTS=1"] + ut, T, 0, 3), (["env", "QUAL_ATTACK_COUNTS=2"] + ut, T, 0, 3),
             "first", 0, {}),
            ("N4 module order", (PY + ["tests.test_x", "tests.test_y"], T, 0, 3), (PY + ["tests.test_y", "tests.test_x"], T, 0, 3),
             "duplicate", 3, {}),
            ("alarm bound stays in the key", (["perl", "-e", "alarm 60; exec @ARGV"] + ut, T, 0, 3),
             (["perl", "-e", "alarm 900; exec @ARGV"] + ut, T, 0, 3), "first", 0, {}),
            ("D2 fail then pass = retry (naive: duplicate)", (ut, T, 1, 3), (ut, T, 0, 3), "retry", 0, {"retries": 1}),
            ("D2 timeout then pass = retry", (ut, T, 124, 900), (ut, T, 0, 3), "retry", 0, {"retries": 1}),
            ("D2 pass then fail = duplicate, outcome differs", (ut, T, 0, 3), (ut, T, 1, 3), "duplicate", 3, {"differs": True}),
            ("D3 subset later = covered, measured", (PY + ["tests.test_x", "tests.test_y"], T, 0, 8), (ut, T, 0, 3), "first", 3,
             {"how": "measured"}),
            ("D3 test under a module = covered", (ut, T, 0, 8), (PY + ["tests.test_x.Case.test_one"], T, 0, 1), "first", 1,
             {"how": "measured"}),
            ("D3 superset later = covered, estimated", (ut, T, 0, 3), (PY + ["tests.test_x", "tests.test_z"], T, 0, 8), "first", 3,
             {"how": "estimated"}),
            ("D3 superset after a timeout = unknown", (ut, T, 124, 900), (PY + ["tests.test_x", "tests.test_z"], T, 0, 8),
             "retry", 0, {"retries": 1}),
            ("D3 subset of a failed run = retry (partial)", (PY + ["tests.test_x", "tests.test_y"], T, 1, 8), (ut, T, 0, 3),
             "retry", 0, {"retries": 1}),
            ("D3 overlap, neither a subset", (PY + ["tests.test_x", "tests.test_y"], T, 0, 8),
             (PY + ["tests.test_x", "tests.test_z"], T, 0, 8), "first", 0, {}),
            ("D3 run_bounded files form", (["python3", "tests/run_bounded.py", "tests/test_x.py", "tests/test_y.py"], T, 0, 8),
             (["python3", "tests/run_bounded.py", "tests/test_x.py"], T, 0, 3), "first", 3, {"how": "measured"}),
            ("D4 live model twice (naive: duplicate)", (["env", "TP_LIVE_LMSTUDIO=1"] + ut, T, 0, 30),
             (["env", "TP_LIVE_LMSTUDIO=1"] + ut, T, 0, 30), "never", 0, {"never": 2}),
            ("D4 network", (["curl", "https://example.invalid"], T, 0, 1), (["curl", "https://example.invalid"], T, 0, 1),
             "never", 0, {"never": 2}),
            ("D4 live tmux inside bash -c", (["bash", "-c", "tmux ls; true"], T, 0, 1), (["bash", "-c", "tmux ls; true"], T, 0, 1),
             "never", 0, {"never": 2}),
            ("D4 bench command", (["bin/tp-bench", "run"], T, 0, 1), (["bin/tp-bench", "run"], T, 0, 1), "never", 0, {"never": 2}),
            ("D4 dispatcher types", (["bash", "tests/check_dispatcher_types.sh"], T, 0, 1),
             (["bash", "tests/check_dispatcher_types.sh"], T, 0, 1), "never", 0, {"never": 2}),
            ("D4b script outside the tree twice (naive: duplicate)", (["python3", "/sessions/s/tasks/r/probes.py"], T, 0, 1),
             (["python3", "/sessions/s/tasks/r/probes.py"], T, 0, 1), "outside", 0, {"outside": 2}),
            ("D4b script inside the tree", (["python3", WT + "/tools/x.py"], T, 0, 1), (["python3", "tools/x.py"], T, 0, 1),
             "duplicate", 1, {}),
            # F2: every form a never-reused command can take (this repo's LiveLmStudioTests shape: env or prefix + unittest)
            ("D4 real LiveLmStudioTests shape (env)", (["env", "TP_LIVE_LMSTUDIO=1"] + live_ut, T, 0, 30),
             (["env", "TP_LIVE_LMSTUDIO=1"] + live_ut, T, 0, 30), "never", 0, {"never": 2}),
            ("D4 shell prefix assignment in bash -c (naive: duplicate)",
             (["bash", "-c", "TP_LIVE_LMSTUDIO=1 python3 -m unittest tests.test_q.LiveLmStudioTests"], T, 0, 30),
             (["bash", "-c", "TP_LIVE_LMSTUDIO=1 python3 -m unittest tests.test_q.LiveLmStudioTests"], T, 0, 30), "never", 0, {"never": 2}),
            ("D4 env inside opaque bash -lc (naive: duplicate)",
             (["bash", "-lc", "cd tests; env TP_LIVE_LMSTUDIO=1 python3 -m unittest test_q"], T, 0, 30),
             (["bash", "-lc", "cd tests; env TP_LIVE_LMSTUDIO=1 python3 -m unittest test_q"], T, 0, 30), "never", 0, {"never": 2}),
            ("D4 export inside nested sh -c", (["bash", "-c", "sh -c 'export TP_LIVE_LMSTUDIO_MODEL=m; python3 -m unittest test_q'"], T, 0, 1),
             (["bash", "-c", "sh -c 'export TP_LIVE_LMSTUDIO_MODEL=m; python3 -m unittest test_q'"], T, 0, 1), "never", 0, {"never": 2}),
            ("D4 env with a flag the parser keeps opaque", (["env", "-i", "TP_LIVE_LMSTUDIO=1"] + ut, T, 0, 1),
             (["env", "-i", "TP_LIVE_LMSTUDIO=1"] + ut, T, 0, 1), "never", 0, {"never": 2}),
            ("D4 budget switch set", (["env", "TP_LMSTUDIO_BUDGET=20"] + ut, T, 0, 1), (["env", "TP_LMSTUDIO_BUDGET=20"] + ut, T, 0, 1),
             "never", 0, {"never": 2}),
            ("D4 budget zero is the offline default", (["env", "TP_LMSTUDIO_BUDGET=0"] + ut, T, 0, 1),
             (["env", "TP_LMSTUDIO_BUDGET=0"] + ut, T, 0, 1), "duplicate", 1, {}),
            ("D4 extra LM Studio endpoint", (["env", "TP_LMSTUDIO_ENDPOINT=1235"] + ut, T, 0, 1),
             (["env", "TP_LMSTUDIO_ENDPOINT=1235"] + ut, T, 0, 1), "never", 0, {"never": 2}),
            ("D4 git -C fetch (naive: duplicate)", (["git", "-C", WT, "fetch"], T, 0, 1), (["git", "-C", WT, "fetch"], T, 0, 1),
             "never", 0, {"never": 2}),
            ("D4 git -c k=v --no-pager pull", (["git", "-c", "a.b=c", "--no-pager", "pull"], T, 0, 1),
             (["git", "-c", "a.b=c", "--no-pager", "pull"], T, 0, 1), "never", 0, {"never": 2}),
            ("D4 git --git-dir=D push in bash -c", (["bash", "-c", "git --git-dir=.git push origin x; true"], T, 0, 1),
             (["bash", "-c", "git --git-dir=.git push origin x; true"], T, 0, 1), "never", 0, {"never": 2}),
            ("D4 git submodule update", (["git", "-C", WT, "submodule", "update"], T, 0, 1),
             (["git", "-C", WT, "submodule", "update"], T, 0, 1), "never", 0, {"never": 2}),
            ("D4 git ls-remote / clone", (["git", "-C", WT, "ls-remote"], T, 0, 1), (["git", "-C", WT, "ls-remote"], T, 0, 1),
             "never", 0, {"never": 2}),
            ("D4 local git is not network, and not understood (reads refs and the index)",
             (["git", "-C", WT, "status"], T, 0, 1), (["git", "-C", WT, "status"], T, 0, 1), "outside", 0,
             {"outside": 2, "reason": "not understood: command git"}),
            # F3: shell text is words, never a path
            ("F3 bash -c of an absolute interpreter equals the direct form",
             (["/usr/bin/python3", "-m", "unittest", "tests/test_x.py"], T, 0, 3),
             (["bash", "-c", "/usr/bin/python3 -m unittest tests/test_x.py"], T, 0, 3), "duplicate", 3, {}),
            ("F3 opaque bash -c (pipe, redirect) is not understood, not a path",
             (["bash", "-c", "/usr/bin/python3 -m unittest tests/test_x.py 2>&1 | tail -3"], T, 0, 3),
             (["bash", "-c", "/usr/bin/python3 -m unittest tests/test_x.py 2>&1 | tail -3"], T, 0, 3), "outside", 0,
             {"outside": 2, "reason": "not understood: shell operator"}),
            ("F3 nested sh -c with an operator is not understood",
             (["bash", "-c", "sh -c '/usr/bin/python3 -m unittest tests/test_x.py'; true"], T, 0, 3),
             (["bash", "-c", "sh -c '/usr/bin/python3 -m unittest tests/test_x.py'; true"], T, 0, 3), "outside", 0,
             {"outside": 2, "reason": "not understood: shell operator"}),
            ("F3 nested plain sh -c equals the direct form",
             (PY + ["tests.test_x"], T, 0, 3), (["bash", "-c", "sh -c 'python3 -m unittest tests.test_x'"], T, 0, 3), "duplicate", 3, {}),
            ("F3 external script inside bash -c stays outside",
             (["bash", "-c", "python3 /sessions/s/tasks/r/probes.py > /dev/null"], T, 0, 1),
             (["bash", "-c", "python3 /sessions/s/tasks/r/probes.py > /dev/null"], T, 0, 1), "outside", 0,
             {"outside": 2, "reason": "input outside the tree: probes.py"}),
            # R3: eligible only when understood; every other form is outside, whatever it is spelt like
            ("R3 cd tests && .. path (review F4)", (["bash", "-c", "cd tests && python3 ../tools/x.py; true"], T, 0, 1),
             (["bash", "-c", "cd tests && python3 ../tools/x.py; true"], T, 0, 1), "outside", 0, {"outside": 2, "reason": "not understood: cd"}),
            ("R3 embedded .. leaves the tree (review F4)", (["bash", "-c", "python3 tools/../../probe.py; true"], T, 0, 1),
             (["bash", "-c", "python3 tools/../../probe.py; true"], T, 0, 1), "outside", 0, {"outside": 2}),
            ("R3 embedded .. in a direct argv", (["python3", "tools/../x.py"], T, 0, 1), (["python3", "tools/../x.py"], T, 0, 1),
             "outside", 0, {"outside": 2, "reason": "not understood: a .. component"}),
            ("R3 .. in an option value", (["python3", "tools/x.py", "--out=../r"], T, 0, 1), (["python3", "tools/x.py", "--out=../r"], T, 0, 1),
             "outside", 0, {"outside": 2}),
            ("R3 env option the parser does not read", (["env", "-i", "X=1"] + ut, T, 0, 1), (["env", "-i", "X=1"] + ut, T, 0, 1),
             "outside", 0, {"outside": 2, "reason": "not understood: an env option"}),
            ("R3 unknown wrapper", (["timeout", "60"] + ut, T, 0, 1), (["timeout", "60"] + ut, T, 0, 1), "outside", 0,
             {"outside": 2, "reason": "not understood: command timeout"}),
            ("R3 alias or other runner", (["deno", "test"], T, 0, 1), (["deno", "test"], T, 0, 1), "outside", 0, {"outside": 2}),
            ("R3 python -c code", (["python3", "-c", "print(1)"], T, 0, 1), (["python3", "-c", "print(1)"], T, 0, 1), "outside", 0,
             {"outside": 2, "reason": "not understood: python3 option -c"}),
            ("R3 python -m other than unittest", (["python3", "-m", "pytest", "tests"], T, 0, 1), (["python3", "-m", "pytest", "tests"], T, 0, 1),
             "outside", 0, {"outside": 2}),
            ("R3 unittest discover", (PY + ["discover"], T, 0, 1), (PY + ["discover"], T, 0, 1), "outside", 0, {"outside": 2}),
            ("R3 bash -lc is not plain", (["bash", "-lc", "python3 -m unittest tests.test_x"], T, 0, 1),
             (["bash", "-lc", "python3 -m unittest tests.test_x"], T, 0, 1), "outside", 0, {"outside": 2, "reason": "not understood: shell options"}),
            ("R3 shell expansion in a program", (["bash", "-c", "python3 -m unittest $MODS"], T, 0, 1),
             (["bash", "-c", "python3 -m unittest $MODS"], T, 0, 1), "outside", 0, {"outside": 2, "reason": "not understood: shell expansion"}),
            ("R3 in-tree script with a prefix assignment and perl alarm", (["perl", "-e", "alarm 9; exec @ARGV", "X=1", "python3", "tools/x.py"], T, 0, 1),
             (["perl", "-e", "alarm 9; exec @ARGV", "X=1", "python3", "tools/x.py"], T, 0, 1), "duplicate", 1, {}),
            ("R3 in-tree shell script", (["bash", "tests/x.sh"], T, 0, 1), (["bash", "tests/x.sh"], T, 0, 1), "duplicate", 1, {}),
            ("R3 in-tree executable path", (["tools/agent/x.sh", "a"], T, 0, 1), (["tools/agent/x.sh", "a"], T, 0, 1), "duplicate", 1, {}),
            # R3: the never scan reads every word, so a run that is not understood is still never counted
            ("R3 never behind an unknown wrapper", (["timeout", "60", "env", "TP_LIVE_LMSTUDIO=1"] + ut, T, 0, 1),
             (["timeout", "60", "env", "TP_LIVE_LMSTUDIO=1"] + ut, T, 0, 1), "never", 0, {"never": 2}),
            ("R3 never inside text an unknown wrapper passes on", (["timeout", "60", "bash", "-c", "TP_LIVE_LMSTUDIO=1 python3 -m unittest x; true"], T, 0, 1),
             (["timeout", "60", "bash", "-c", "TP_LIVE_LMSTUDIO=1 python3 -m unittest x; true"], T, 0, 1), "never", 0, {"never": 2}),
            ("R3 never in env -S text", (["env", "-S", "TP_LIVE_LMSTUDIO=1 python3 -m unittest x"], T, 0, 1),
             (["env", "-S", "TP_LIVE_LMSTUDIO=1 python3 -m unittest x"], T, 0, 1), "never", 0, {"never": 2}),
            ("R3 never named in python -c code", (["python3", "-c", "import os; os.environ['TP_LIVE_LMSTUDIO']='1'"], T, 0, 1),
             (["python3", "-c", "import os; os.environ['TP_LIVE_LMSTUDIO']='1'"], T, 0, 1), "never", 0, {"never": 2}),
            ("R3 git -C fetch behind an unknown wrapper", (["nice", "git", "-C", WT, "fetch"], T, 0, 1), (["nice", "git", "-C", WT, "fetch"], T, 0, 1),
             "never", 0, {"never": 2}),
            ("R3 unsetting a live switch is not live", (["env", "-u", "TP_LIVE_LMSTUDIO"] + ut, T, 0, 1),
             (["env", "-u", "TP_LIVE_LMSTUDIO"] + ut, T, 0, 1), "duplicate", 1, {}),
            ("D4 a module named bench is not a bench run", (PY + ["tests.test_bench_runner"], T, 0, 1),
             (PY + ["tests.test_bench_runner"], T, 0, 1, WT, "py-test_bench_runner"), "duplicate", 1, {}),
        ]
        for name, a, b, want_class, want_rep, extra in rows:
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                s = Tasks(tmp)
                s.task("coder-1")
                s.task("review-coder-1-1", review_of="coder-1")
                s.run("coder-1", a[0], a[1], a[2], a[3], at=0)
                pb = s.run("review-coder-1-1", b[0], b[1], b[2], b[3], at=10, cwd=b[4] if len(b) > 4 else WT,
                           label=b[5] if len(b) > 5 else "")
                rep = repeats.build(tmp)
                rb = by_path(rep)[pb]
                self.assertEqual(rb["class"], want_class)
                self.assertEqual(rb["role"], "reviewer")
                self.assertAlmostEqual(repeats.repeated_seconds(rb), want_rep)
                if "how" in extra:
                    self.assertEqual(rb["covered"]["how"], extra["how"])
                if "reason" in extra:
                    self.assertTrue(rb["outside"].startswith(extra["reason"]), rb["outside"])
                if "differs" in extra:
                    self.assertTrue(rb["outcome_differs"])
                for key in ("retries", "never", "outside", "interpreter_spelling_only"):
                    if key in extra:
                        self.assertEqual(rep["totals"][key], extra[key])

        # Gate checks, tests.jsonl, malformed and symlinked records, roles: one directory.
        with tempfile.TemporaryDirectory() as tmp:
            s = Tasks(tmp)
            s.task("coder-1")
            s.gate("coder-1", "gate-20261112-080000-1.json", finished="05",
                   checks=(("prettier:web", "passed", 0, 2.0), ("eslint:web", "missing", None, 0.0)))
            s.gate("coder-1", "gate.json", finished="09")                    # same check, same files, same tree
            s.task("coder-2")
            s.gate("coder-2", "gate.json", tree=T2, finished="20")           # other tree
            # F1: checks whose scope reads head/base (gitleaks BASE..HEAD, gitleaks-dirty, typecheck) key on them too.
            s.task("coder-3")
            # R3: only checks whose whole scope is in the record are comparable: prettier (files+tree), gitleaks (head+base),
            # gitleaks-dirty (head+tree). eslint (Deno lint reads the index: a file restored untracked, then staged, has
            # the same files/tree/head/base) and typecheck (files deleted since base) are not understood.
            scoped = (("gitleaks", "passed", 0, 3.0), ("gitleaks-dirty", "passed", 0, 1.0), ("typecheck:web", "passed", 0, 4.0),
                      ("eslint:web", "passed", 0, 5.0), ("newcheck", "passed", 0, 1.0))
            H1, H2, B1, B2 = "1" * 40, "2" * 40, "3" * 40, "4" * 40
            s.gate("coder-3", "gate-20261112-080000-1.json", checks=scoped + (("prettier:web", "passed", 0, 2.0),), tree="e" * 40, finished="31",
                   head=H1, base=B1)
            s.gate("coder-3", "gate-20261112-080000-2.json", checks=scoped + (("prettier:web", "passed", 0, 2.0),), tree="e" * 40, finished="32",
                   head=H1, base=B1)                                            # nothing changed: all four repeat
            s.gate("coder-3", "gate-20261112-080000-3.json", checks=scoped + (("prettier:web", "passed", 0, 2.0),), tree="e" * 40, finished="33",
                   head=H2, base=B1)                                            # partial commit: same tree and files, new head
            s.gate("coder-3", "gate-20261112-080000-4.json", checks=scoped + (("prettier:web", "passed", 0, 2.0),), tree="e" * 40, finished="34",
                   head=H2, base=B2)                                            # base moved
            s.gate("coder-3", "gate-20261112-080000-5.json", checks=(("gitleaks", "passed", 0, 3.0),), tree="e" * 40, finished="35", head="", base=B2)
            s.gate("coder-3", "gate.json", checks=(("gitleaks", "passed", 0, 3.0),), tree="e" * 40, finished="36", head=H2, base="")  # unrecorded
            (Path(tmp) / "coder-1" / "test-runs" / "7.json").write_text("{not json")
            (Path(tmp) / "coder-1" / "test-runs" / "8.json").write_text(json.dumps({"schema": "tp-test-run:v1", "cmd": "x"}))
            (Path(tmp) / "coder-1" / "test-runs" / "9.json").symlink_to(Path(tmp) / "coder-1" / "meta.json")
            (Path(tmp) / "coder-1" / "tests.jsonl").write_text(
                json.dumps({"cmd": ["python3", "-m", "unittest"], "files": [], "seconds": 4.0, "rc": 0, "timed_out": False,
                            "load1": 1.0, "per_file": {}, "leftovers": []}) + "\nnot json\n")
            s.task("main-like", role="main")
            s.run("main-like", PY + ["tests.test_q"], at=30)
            (Path(tmp) / "no-meta" / "test-runs").mkdir(parents=True)
            Tasks(tmp).run("no-meta", PY + ["tests.test_q"], at=40)
            rep = repeats.build(tmp)
            gates = sorted((r for r in rep["runs"] if r["kind"] == "gate" and r["task"] != "coder-3"), key=lambda r: r["end"])
            self.assertEqual([(r["label"], r["class"]) for r in gates],
                             [("prettier:web", "first"), ("prettier:web", "duplicate"), ("prettier:web", "first")])
            g3 = [r for r in rep["runs"] if r["task"] == "coder-3"]
            def classes(label):
                return [r["class"] for r in sorted((r for r in g3 if r["label"] == label), key=lambda r: r["end"])]
            for label in ("gitleaks", "gitleaks-dirty"):
                self.assertEqual(classes(label)[:4], ["first", "duplicate", "first", "first"], label)  # head, then base changed
            for label in ("typecheck:web", "eslint:web", "newcheck"):
                self.assertEqual(classes(label), ["outside"] * 4, label)
            self.assertEqual(classes("prettier:web"), ["first", "duplicate", "duplicate", "duplicate"])  # files+tree only
            self.assertEqual(classes("gitleaks")[4:], ["outside", "outside"])  # head or base.sha not recorded
            self.assertTrue(all("head/base" in r["outside"] for r in g3 if r["label"] == "gitleaks" and r["class"] == "outside"))
            self.assertTrue(all(r["outside"].startswith("not understood: ") for r in g3 if r["class"] == "outside"))
            self.assertEqual(rep["totals"]["not_understood"], len([r for r in rep["runs"] if r["class"] == "outside" and r["task"] == "coder-3"]))
            shown = " ".join(" ".join(repeats.shown_cmd(c)) for c in (
                ["env", "API_TOKEN=s3cret", "python3"], ["bash", "-c", "export DB_PASSWORD='p w' X=1; API_KEY=abc cmd"],
                ["bash", "-c", "AUTH_HEADER=zzz python3 -m unittest"],
                # R3 F1: an argv element NAME=VALUE is hidden whole; shell text hides through the end of the shell word
                ["env", "API_TOKEN=dummy firstw secondw", "python3"], ["env", "DB_PASSWORD=semi;colonw 'quotew\"", "python3"],
                ["bash", "-c", "export API_KEY=a\"qb cw\"dw ew; true"], ["bash", "-c", "echo x; AUTH_X=esc\\ apedw fw"],
                ["bash", "-c", "SECRET_Y='open quoted w"]))
            for leaked in ("s3cret", "p w", "abc", "zzz", "firstw", "secondw", "colonw", "quotew", "qb", "cw", "dw", "apedw", "open", "quoted"):
                self.assertNotIn(leaked, shown)
            for kept in ("X=1", "ew;", "fw"):
                self.assertIn(kept, shown)
            self.assertEqual({r["task"]: r["role"] for r in rep["runs"] if r["kind"] == "test-run"},
                             {"main-like": "main", "no-meta": "unknown"})
            self.assertEqual(rep["totals"]["main_visible"], 1)
            self.assertEqual(rep["tests_jsonl"]["lines"], 1)                 # counted apart, never a run
            problems = " | ".join(p["problem"] for p in rep["problems"])
            for want in ("malformed test-run record", "invalid test-run record", "symlink skipped", "malformed tests.jsonl"):
                self.assertIn(want, problems)

    def test_case1_planted_schedule(self):
        """Case 1: the brief's planted schedule, scored over every pair of runs, then the CLI over the same directory."""
        with tempfile.TemporaryDirectory() as tmp:
            s = Tasks(tmp)
            s.task("coder-1")
            s.task("review-coder-1-1", review_of="coder-1")
            s.task("main-like", role="main")
            s.task("coder-other", lane="other")
            xy, x, xyz = PY + ["tests.test_x", "tests.test_y"], PY + ["tests.test_x"], PY + ["tests.test_x", "tests.test_y", "tests.test_z"]
            live = ["env", "TP_LIVE_LMSTUDIO=1"] + PY + ["tests.test_qual_live"]
            runs = {
                "r1 coder X+Y on T": s.run("coder-1", xy, T, 0, 10, at=1),
                "r2 reviewer X on T": s.run("review-coder-1-1", x, T, 0, 4, at=2),
                "r3 main X+Y+Z on T": s.run("main-like", xyz, T, 0, 15, at=3),
                "r4 coder X+Y on T'": s.run("coder-1", xy, T2, 0, 10, at=4),
                "r5 coder W fails on T": s.run("coder-1", PY + ["tests.test_w"], T, 1, 2, at=5),
                "r6 coder W passes on T": s.run("coder-1", PY + ["tests.test_w"], T, 0, 2, at=6),
                "r7 live on T": s.run("coder-1", live, T, 0, 30, at=7),
                "r8 live on T again": s.run("coder-1", live, T, 0, 30, at=8),
                "r9 X+Y other interpreter on T": s.run("coder-1", ["/usr/bin/python3"] + xy[1:], T, 0, 10, at=9),
            }
            (Path(tmp) / "coder-1" / "test-runs" / "99.json").write_text("{")  # malformed
            s.run("coder-other", PY + ["tests.test_o"], T, 0, 1, at=10)  # another lane: --lane drops it
            rep = repeats.build(tmp)
            got = by_path(rep)
            want_class = {"r1 coder X+Y on T": "first", "r2 reviewer X on T": "first", "r3 main X+Y+Z on T": "first",
                          "r4 coder X+Y on T'": "first", "r5 coder W fails on T": "first", "r6 coder W passes on T": "retry",
                          "r7 live on T": "never", "r8 live on T again": "never", "r9 X+Y other interpreter on T": "first"}
            self.assertEqual({k: got[p]["class"] for k, p in runs.items()}, want_class)
            covered = {k: (got[p]["covered"]["by"], got[p]["covered"]["how"]) for k, p in runs.items() if got[p]["covered"]}
            self.assertEqual(covered, {"r2 reviewer X on T": (runs["r1 coder X+Y on T"], "measured"),
                                       "r3 main X+Y+Z on T": (runs["r1 coder X+Y on T"], "estimated")})

            # every pair: same check | covers | retry | never | none
            def relation(a, b):
                ra, rb = got[runs[a]], got[runs[b]]
                if "never" in (ra["class"], rb["class"]):
                    return "never"
                if (ra["canon"]["key"], ra["tree"]) == (rb["canon"]["key"], rb["tree"]):
                    return "retry" if rb["class"] == "retry" else "same"
                if ra["tree"] == rb["tree"] and ra["canon"]["runner"] is not None and ra["canon"]["runner"] == rb["canon"]["runner"] \
                        and (repeats._covers(ra["canon"]["tests"], rb["canon"]["tests"]) or
                             repeats._covers(rb["canon"]["tests"], ra["canon"]["tests"])):
                    return "covers"
                return "none"
            names = list(runs)
            want = {("r1 coder X+Y on T", "r2 reviewer X on T"): "covers", ("r1 coder X+Y on T", "r3 main X+Y+Z on T"): "covers",
                    ("r2 reviewer X on T", "r3 main X+Y+Z on T"): "covers",
                    ("r5 coder W fails on T", "r6 coder W passes on T"): "retry"}
            mismatches = []
            for i, a in enumerate(names):
                for b in names[i + 1:]:
                    expect = "never" if "live" in a or "live" in b else want.get((a, b), "none")
                    if relation(a, b) != expect:
                        mismatches.append((a, b, relation(a, b), expect))
            self.assertEqual(mismatches, [])
            self.assertEqual(len(names) * (len(names) - 1) // 2, 36)
            t = rep["totals"]
            self.assertEqual((t["repeated"], t["retries"], t["retry_seconds"], t["never"], t["interpreter_spelling_only"]),
                             (14.0, 1, 2.0, 2, 1))  # r2 4 s measured + r3 10 s estimated
            self.assertEqual(len([p for p in rep["problems"] if "malformed" in p["problem"]]), 1)

            # The CLI: --json parses, --lane filters, a missing dir is exit 2; nothing is written into the tasks dir.
            before = sorted(str(p) for p in Path(tmp).rglob("*"))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(repeats.main([tmp, "--json", "--lane", "ln"]), 0)
            data = json.loads(out.getvalue())
            self.assertEqual(data["totals"]["runs"], 9)
            self.assertNotIn("coder-other", {r["task"] for r in data["runs"]})
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(repeats.main([tmp + "/nope"]), 2)
                self.assertEqual(repeats.main([tmp]), 0)
            self.assertEqual(sorted(str(p) for p in Path(tmp).rglob("*")), before)


if __name__ == "__main__":
    unittest.main()
