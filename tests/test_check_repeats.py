"""tools/checks/repeats.py (HB5a, VR1 arm A): the derivation rules as one table, and the case-1 planted schedule scored pair by
pair.

The task directories are producer-shaped, copied field for field from:
  test-runs/<n>.json   tools/agent/test-run.sh:133-134 (tp-test-run:v1: label, cmd, cwd, head, tree, exit, timed_out, seconds,
                       log, summary; no start or end, the file time is the end)
  gate.json, gate-*    tools/agent/pre-commit-gate.sh:676-679 (tp-gate:v1: worktree, base{ref,sha}, head, tree, files,
                       checks[{name,status,exit,seconds}], exit, started, finished, log); the previous gate.json is renamed
                       gate-YYYYMMDD-HHMMSS-PID.json (:686-688)
  tests.jsonl          tests/run_bounded.py:189-190 (cmd, files, seconds, rc, timed_out, load1, per_file, leftovers)
  main-runs/<n>.json   tools/agent/test-run.sh --main (the same record plus role, lane, step; HB5b) in SESSION/main-runs, beside tasks/
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
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("check_repeats", ROOT / "tools" / "checks" / "repeats.py")
repeats = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repeats)

T, T2 = "a" * 40, "b" * 40
WT = "/work/tmux-pi-wt-x"
PY = ["python3", "-m", "unittest"]
P39, P314 = "/usr/bin/python3", "/opt/homebrew/Cellar/python@3.14/3.14.0/bin/python3.14"


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

    def run(self, ident, cmd, tree=T, exit=0, seconds=1.0, at=0, label="", cwd=WT, interp="absent", extra=None, dirname="test-runs"):
        """`interp`: absent (a record from before HB5b), None (null) or (path, version). `extra`: more fields (a Main record's
        role, lane, step). `dirname`: where the record goes under `ident` (a Main record: ident "." and dirname "main-runs")."""
        n = self.n[ident] = self.n.get(ident, 0) + 1
        path = self.root / ident / dirname / ("%d.json" % n)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"schema": "tp-test-run:v1", "label": label, "cmd": cmd, "cwd": cwd, "head": "c" * 40,
                  "tree": tree, "exit": exit, "timed_out": exit == 124, "seconds": seconds,
                  "log": "/tmp/tp-tools/x.log", "summary": ""}
        if interp != "absent":
            record["interpreter"] = None if interp is None else {"path": interp[0], "version": interp[1]}
        path.write_text(json.dumps(dict(record, **(extra or {}))))
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
            # name, a(cmd, tree, exit, seconds[, {interp}]), b(..., cwd, label[, {interp}]), expected b class, b repeated seconds, extra checks
            ("D1 same cmd+tree", (ut, T, 0, 3), (ut, T, 0, 3), "duplicate", 3, {}),
            ("D1 same cmd other tree", (ut, T, 0, 3), (ut, T2, 0, 3), "first", 0, {}),
            ("N1 other cwd (same relative argv, other worktree path)",
             (PY + ["tests/test_x.py"], T, 0, 3), (PY + ["tests/test_x.py"], T, 0, 3, "/work/other-wt"), "duplicate", 3, {}),
            ("R5 absolute worktree operand is not normalised (naive: duplicate of the relative spelling)",
             (PY + [WT + "/tests/test_x.py"], T, 0, 3), (PY + ["tests/test_x.py"], T, 0, 3, "/work/other-wt"), "first", 0, {}),
            ("N1 cd tests && ... is not understood (naive: duplicate)",
             (PY + ["test_x"], T, 0, 3), (["bash", "-c", "cd tests && python3 -m unittest test_x"], T, 0, 3), "outside", 0,
             {"outside": 1, "reason": "not understood: cd"}),
            ("N2 other interpreter (naive: duplicate)", (ut, T, 0, 3), (["/usr/bin/python3"] + ut[1:], T, 0, 3), "first", 0,
             {"interpreter_spelling_only": 1}),
            ("N2 interpreter path spelling", (["/usr/bin//python3"] + ut[1:], T, 0, 3), (["/usr/bin/python3"] + ut[1:], T, 0, 3),
             "duplicate", 3, {}),
            ("N3 TMUX_TMPDIR value", (["env", "TMUX_TMPDIR=/tmp/a.1"] + ut, T, 0, 3), (["env", "TMUX_TMPDIR=/tmp/b.2"] + ut, T, 0, 3),
             "duplicate", 3, {}),
            ("HB5c N3 TMUX value (a socket path, pid, index)", (["env", "TMUX=/tmp/x,1,1"] + ut, T, 0, 3),
             (["env", "TMUX=/tmp/tmux-501/default,99,2"] + ut, T, 0, 3), "duplicate", 3, {}),
            ("HB5c N3 TMUX present versus absent", (["env", "TMUX=/tmp/x,1,1"] + ut, T, 0, 3), (ut, T, 0, 3), "first", 0, {}),
            ("HB5c N3 TMUX present versus env -u TMUX", (["env", "TMUX=/tmp/x,1,1"] + ut, T, 0, 3), (["env", "-u", "TMUX"] + ut, T, 0, 3),
             "first", 0, {}),
            ("HB5c R4 absolute TMUX value is understood", (["env", "TMUX=/tmp/x,1,1"] + ut, T, 0, 3), (["env", "TMUX=/tmp/x,1,1"] + ut, T, 0, 3),
             "duplicate", 3, {}),
            ("HB5c N3 TMUX set but empty is still set (differs from absent)", (["env", "TMUX="] + ut, T, 0, 3), (ut, T, 0, 3), "first", 0, {}),
            ("HB5c R4 TMUX value with a .. component", (["env", "TMUX=/tmp/../x,1,1"] + ut, T, 0, 1), (["env", "TMUX=/tmp/../x,1,1"] + ut, T, 0, 1),
             "outside", 0, {"outside": 2, "reason": "not understood: an environment value"}),
            ("HB5c R4 TMUX value with a space", (["env", "TMUX=/tmp/a b,1,1"] + ut, T, 0, 1), (["env", "TMUX=/tmp/a b,1,1"] + ut, T, 0, 1),
             "outside", 0, {"outside": 2}),
            ("HB5c R4 env -u TMUX is still read", (["env", "-u", "TMUX"] + ut, T, 0, 1), (["env", "-u", "TMUX"] + ut, T, 0, 1),
             "duplicate", 1, {}),
            ("HB5c N3 TMUX as a shell prefix in bash -c", (["bash", "-c", "TMUX=/tmp/x,1,1 python3 -m unittest tests.test_x"], T, 0, 3),
             (["env", "TMUX=/tmp/y,2,2"] + ut, T, 0, 3), "duplicate", 3, {}),
            ("HB5c the grammar is not widened for another name (an absolute SOCK)", (["env", "SOCK=/tmp/x,1,1"] + ut, T, 0, 1),
             (["env", "SOCK=/tmp/x,1,1"] + ut, T, 0, 1), "outside", 0, {"outside": 2}),
            ("N3 other env value stays", (["env", "QUAL_ATTACK_COUNTS=1"] + ut, T, 0, 3), (["env", "QUAL_ATTACK_COUNTS=2"] + ut, T, 0, 3),
             "first", 0, {}),
            ("N4 module order", (PY + ["tests.test_x", "tests.test_y"], T, 0, 3), (PY + ["tests.test_y", "tests.test_x"], T, 0, 3),
             "duplicate", 3, {}),
            # HB5b: the interpreter the record names. The same spelling (python3) on two pythons is not the same check; naive
            # (HEAD's key, which never reads the field): duplicate.
            ("HB5b python3 on py3.9 then py3.14 (naive: duplicate)", (ut, T, 0, 3, {"interp": (P39, "3.9.6")}),
             (ut, T, 0, 3, WT, "", {"interp": (P314, "3.14.0")}), "first", 0, {}),
            ("HB5b same path, other version (an upgrade in place)", (ut, T, 0, 3, {"interp": (P314, "3.14.0")}),
             (ut, T, 0, 3, WT, "", {"interp": (P314, "3.14.1")}), "first", 0, {}),
            ("HB5b same interpreter = duplicate", (ut, T, 0, 3, {"interp": (P314, "3.14.0")}),
             (ut, T, 0, 3, WT, "", {"interp": (P314, "3.14.0")}), "duplicate", 3, {}),
            ("HB5b a record without the field keeps today's rule", (ut, T, 0, 3, {"interp": (P39, "3.9.6")}), (ut, T, 0, 3), "duplicate", 3, {}),
            ("HB5b the field is null (no python, or unresolved) keeps today's rule", (ut, T, 0, 3, {"interp": None}),
             (ut, T, 0, 3, WT, "", {"interp": (P314, "3.14.0")}), "duplicate", 3, {}),
            ("HB5b the field is null on both", (ut, T, 0, 3, {"interp": None}), (ut, T, 0, 3, WT, "", {"interp": None}), "duplicate", 3, {}),
            ("HB5b covered by a run on another interpreter is not covered", (PY + ["tests.test_x", "tests.test_y"], T, 0, 8, {"interp": (P39, "3.9.6")}),
             (ut, T, 0, 3, WT, "", {"interp": (P314, "3.14.0")}), "first", 0, {}),
            ("HB5b covered by a run on the same interpreter", (PY + ["tests.test_x", "tests.test_y"], T, 0, 8, {"interp": (P314, "3.14.0")}),
             (ut, T, 0, 3, WT, "", {"interp": (P314, "3.14.0")}), "first", 3, {"how": "measured"}),
            ("HB5b a retry on the other interpreter is a first run", (ut, T, 1, 3, {"interp": (P39, "3.9.6")}),
             (ut, T, 0, 3, WT, "", {"interp": (P314, "3.14.0")}), "first", 0, {}),
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
            ("D4b script inside the tree, relative", (["python3", "tools/x.py"], T, 0, 1), (["python3", "tools/x.py"], T, 0, 1),
             "duplicate", 1, {}),
            ("D4b absolute script in the worktree is not normalised (review F1)", (["python3", WT + "/tools/x.py"], T, 0, 1),
             (["python3", WT + "/tools/x.py"], T, 0, 1), "outside", 0, {"outside": 2, "reason": "not understood: an absolute path in the worktree"}),
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
             (["git", "-C", "wt", "status"], T, 0, 1), (["git", "-C", "wt", "status"], T, 0, 1), "outside", 0,
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
             (["bash", "-c", "cd tests && python3 ../tools/x.py; true"], T, 0, 1), "outside", 0, {"outside": 2, "reason": "not understood: a word with a leading dot"}),
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
             {"outside": 2, "reason": "not understood: a word outside the grammar"}),
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
            ("R3 in-tree executable path", (["tools/agent/x.sh"], T, 0, 1), (["tools/agent/x.sh"], T, 0, 1), "duplicate", 1, {}),
            # R4: a strict grammar on the words as written; a script takes no operand or option value, nothing is normalised first
            ("R4 option value outside the tree (review F2)", (["python3", "tools/waits/timeline.py", ".", "--lanes-dir=/tmp/lanes"], T, 0, 1),
             (["python3", "tools/waits/timeline.py", ".", "--lanes-dir=/tmp/lanes"], T, 0, 1), "outside", 0,
             {"outside": 2, "reason": "not understood: a word with a leading dot"}),
            ("R4 option with a value, no other operand", (["python3", "tools/x.py", "--lanes-dir=/tmp/lanes"], T, 0, 1),
             (["python3", "tools/x.py", "--lanes-dir=/tmp/lanes"], T, 0, 1), "outside", 0,
             {"outside": 2, "reason": "not understood: an option with a value"}),
            ("R4 option and value as two words", (["python3", "tools/x.py", "--out", "/tmp/o"], T, 0, 1),
             (["python3", "tools/x.py", "--out", "/tmp/o"], T, 0, 1), "outside", 0, {"outside": 2, "reason": "input outside the tree"}),
            ("R4 a relative operand of a script", (["python3", "tools/x.py", "sub"], T, 0, 1), (["python3", "tools/x.py", "sub"], T, 0, 1),
             "outside", 0, {"outside": 2, "reason": "not understood: operands"}),
            ("R4 operand of a shell script", (["bash", "tests/x.sh", "a"], T, 0, 1), (["bash", "tests/x.sh", "a"], T, 0, 1),
             "outside", 0, {"outside": 2, "reason": "not understood: operands of a bash script"}),
            ("R4 operand of a relative executable", (["tools/agent/x.sh", "a"], T, 0, 1), (["tools/agent/x.sh", "a"], T, 0, 1),
             "outside", 0, {"outside": 2, "reason": "not understood: operands"}),
            ("R4 tilde in a shell program (review F3)", (["bash", "-c", "python3 tools/checks/repeats.py ~/session-tasks"], T, 0, 1),
             (["bash", "-c", "python3 tools/checks/repeats.py ~/session-tasks"], T, 0, 1), "outside", 0, {"outside": 2, "reason": "not understood: a tilde word"}),
            ("R4 quoted tilde differs in meaning, not understood", (["bash", "-c", "python3 tools/checks/repeats.py '~/session-tasks'"], T, 0, 1),
             (["bash", "-c", "python3 tools/checks/repeats.py '~/session-tasks'"], T, 0, 1), "outside", 0,
             {"outside": 2, "reason": "not understood: quoted word"}),
            ("R4 quoted unittest name", (["bash", "-c", "python3 -m unittest 'tests.test_x'"], T, 0, 1),
             (["bash", "-c", "python3 -m unittest 'tests.test_x'"], T, 0, 1), "outside", 0, {"outside": 2, "reason": "not understood: quoted word"}),
            ("R4 escaped space is quoting", (["bash", "-c", "python3 -m unittest tests.test_x\\ y"], T, 0, 1),
             (["bash", "-c", "python3 -m unittest tests.test_x\\ y"], T, 0, 1), "outside", 0, {"outside": 2, "reason": "not understood: quoted word"}),
            ("R4 tilde as a unittest name", (PY + ["~/t"], T, 0, 1), (PY + ["~/t"], T, 0, 1), "outside", 0, {"outside": 2}),
            ("R4 glob and brace as unittest names", (PY + ["tests/test_*.py"], T, 0, 1), (PY + ["tests.{a,b}"], T, 0, 1), "outside", 0, {"outside": 2}),
            ("R4 -k pattern with a glob", (PY + ["-k", "a*", "tests.test_x"], T, 0, 1), (PY + ["-k", "a*", "tests.test_x"], T, 0, 1), "outside", 0, {"outside": 2}),
            ("R4 -k pattern is a word", (PY + ["-k", "test_a", "tests.test_x"], T, 0, 1), (PY + ["-k", "test_a", "tests.test_x"], T, 0, 1), "duplicate", 1, {}),
            ("R4 absolute .. path is not normalised (review F2)", (["python3", WT + "/tools/../tools/checks/repeats.py"], T, 0, 1),
             (["python3", WT + "/tools/../tools/checks/repeats.py"], T, 0, 1), "outside", 0, {"outside": 2, "reason": "not understood: an absolute path in the worktree"}),
            ("R4 absolute .. in a unittest path", (PY + [WT + "/tests/../tests/test_x.py"], T, 0, 1),
             (PY + [WT + "/tests/../tests/test_x.py"], T, 0, 1), "outside", 0, {"outside": 2}),
            ("R4 leading dot script", (["python3", "./tools/x.py"], T, 0, 1), (["python3", "./tools/x.py"], T, 0, 1), "outside", 0,
             {"outside": 2, "reason": "not understood: a word with a leading dot"}),
            ("R4 env value with a tilde", (["env", "X=~/a"] + ut, T, 0, 1), (["env", "X=~/a"] + ut, T, 0, 1), "outside", 0,
             {"outside": 2, "reason": "not understood: an environment value"}),
            ("R4 env value with an absolute path", (["env", "X=/etc/a"] + ut, T, 0, 1), (["env", "X=/etc/a"] + ut, T, 0, 1), "outside", 0,
             {"outside": 2}),
            ("R4 shell prefix value with a glob in bash -c", (["bash", "-c", "X=a* python3 -m unittest tests.test_x"], T, 0, 1),
             (["bash", "-c", "X=a* python3 -m unittest tests.test_x"], T, 0, 1), "outside", 0, {"outside": 2}),
            ("R4 env -u operand outside the grammar", (["env", "-u", "A-B"] + ut, T, 0, 1), (["env", "-u", "A-B"] + ut, T, 0, 1), "outside", 0,
             {"outside": 2, "reason": "not understood: an env -u operand"}),
            ("R4 run_bounded option value", (["python3", "tests/run_bounded.py", "--timeout", "$X", "tests/test_x.py"], T, 0, 1),
             (["python3", "tests/run_bounded.py", "--timeout", "$X", "tests/test_x.py"], T, 0, 1), "outside", 0, {"outside": 2}),
            ("R4 git fetch behind env -i bash -c (review probe)", (["env", "-i", "bash", "-c", "git -C /work/wt fetch"], T, 0, 1),
             (["env", "-i", "bash", "-c", "git -C /work/wt fetch"], T, 0, 1), "never", 0, {"never": 2}),
            # R5: every original word in every role passes the grammar before any recognition (review F1's five probes + roles)
            ("R5 tilde interpreter inside bash -c (probe 1)", (["bash", "-c", "~/venv/bin/python3 -m unittest tests.test_x"], T, 0, 1),
             (["bash", "-c", "~/venv/bin/python3 -m unittest tests.test_x"], T, 0, 1), "outside", 0, {"outside": 2, "reason": "not understood: a tilde word"}),
            ("R5 globbed interpreter in bash -c (probe 2)", (["bash", "-c", "venv*/bin/python3 -m unittest tests.test_x"], T, 0, 1),
             (["bash", "-c", "venv*/bin/python3 -m unittest tests.test_x"], T, 0, 1), "outside", 0, {"outside": 2, "reason": "not understood: a glob"}),
            ("R5 globbed env wrapper in bash -c (probe 3)", (["bash", "-c", "venv*/bin/env python3 -m unittest tests.test_x"], T, 0, 1),
             (["bash", "-c", "venv*/bin/env python3 -m unittest tests.test_x"], T, 0, 1), "outside", 0, {"outside": 2, "reason": "not understood: a glob"}),
            ("R5 absolute script in the worktree via argv (probe 4)", (["python3", WT + "/tools/x.py"], T, 0, 1),
             (["python3", WT + "/tools/x.py"], T, 0, 1), "outside", 0, {"outside": 2}),
            ("R5 tilde shell interpreter (probe 5)", (["bash", "-c", "~/bin/bash tests/x.sh"], T, 0, 1),
             (["bash", "-c", "~/bin/bash tests/x.sh"], T, 0, 1), "outside", 0, {"outside": 2, "reason": "not understood: a tilde word"}),
            ("R5 tilde interpreter in argv", (["~/venv/bin/python3", "-m", "unittest", "tests.test_x"], T, 0, 1),
             (["~/venv/bin/python3", "-m", "unittest", "tests.test_x"], T, 0, 1), "outside", 0, {"outside": 2, "reason": "not understood: a tilde word"}),
            ("R5 globbed interpreter in argv", (["venv*/bin/python3", "-m", "unittest", "tests.test_x"], T, 0, 1),
             (["venv*/bin/python3", "-m", "unittest", "tests.test_x"], T, 0, 1), "outside", 0, {"outside": 2}),
            ("R5 globbed wrapper in argv", (["venv*/bin/env", "python3", "-m", "unittest", "tests.test_x"], T, 0, 1),
             (["venv*/bin/env", "python3", "-m", "unittest", "tests.test_x"], T, 0, 1), "outside", 0, {"outside": 2}),
            ("R5 expanded env wrapper in argv", (["$HOME/bin/env", "python3", "-m", "unittest", "tests.test_x"], T, 0, 1),
             (["$HOME/bin/env", "python3", "-m", "unittest", "tests.test_x"], T, 0, 1), "outside", 0, {"outside": 2, "reason": "not understood: shell expansion"}),
            ("R5 tilde shell interpreter in argv", (["~/bin/bash", "tests/x.sh"], T, 0, 1), (["~/bin/bash", "tests/x.sh"], T, 0, 1),
             "outside", 0, {"outside": 2}),
            ("R5 tilde interpreter in nested sh -c", (["bash", "-c", "sh -c '~/venv/bin/python3 -m unittest tests.test_x'"], T, 0, 1),
             (["bash", "-c", "sh -c '~/venv/bin/python3 -m unittest tests.test_x'"], T, 0, 1), "outside", 0, {"outside": 2, "reason": "not understood: a tilde word"}),
            ("R5 relative non-absolute interpreter is fine", (["venv/bin/python3", "-m", "unittest", "tests.test_x"], T, 0, 1),
             (["venv/bin/python3", "-m", "unittest", "tests.test_x"], T, 0, 1), "duplicate", 1, {}),
            ("R5 absolute non-interpreter executable", (["/usr/local/bin/mytool", "tests/x.sh"], T, 0, 1),
             (["/usr/local/bin/mytool", "tests/x.sh"], T, 0, 1), "outside", 0, {"outside": 2, "reason": "input outside the tree: mytool"}),
            ("R5 absolute interpreter with .. is not plain", (["/opt/../usr/bin/python3", "-m", "unittest", "tests.test_x"], T, 0, 1),
             (["/opt/../usr/bin/python3", "-m", "unittest", "tests.test_x"], T, 0, 1), "outside", 0, {"outside": 2}),
            # R6: the perl alarm wrapper's own executable word is validated before the alarm text is exempt (review F1)
            ("R6 perl with .. in its path (review probe)", (["/usr/bin/../bin/perl", "-e", "alarm 60; exec @ARGV"] + ut, T, 0, 1),
             (["/usr/bin/../bin/perl", "-e", "alarm 60; exec @ARGV"] + ut, T, 0, 1), "outside", 0, {"outside": 2, "reason": "input outside the tree: perl"}),
            ("R6 perl relative ..", (["../bin/perl", "-e", "alarm 60; exec @ARGV"] + ut, T, 0, 1),
             (["../bin/perl", "-e", "alarm 60; exec @ARGV"] + ut, T, 0, 1), "outside", 0, {"outside": 2}),
            ("R6 perl tilde, glob and dollar", (["~/bin/perl", "-e", "alarm 60; exec @ARGV"] + ut, T, 0, 1),
             (["bin*/perl", "-e", "alarm 60; exec @ARGV"] + ut, T, 0, 1), "outside", 0, {"outside": 2}),
            ("R6 perl dollar", (["$HOME/bin/perl", "-e", "alarm 60; exec @ARGV"] + ut, T, 0, 1),
             (["$HOME/bin/perl", "-e", "alarm 60; exec @ARGV"] + ut, T, 0, 1), "outside", 0, {"outside": 2}),
            ("R6 perl plain absolute stays understood", (["/usr/bin/perl", "-e", "alarm 60; exec @ARGV"] + ut, T, 0, 1),
             (["/usr/bin/perl", "-e", "alarm 60; exec @ARGV"] + ut, T, 0, 1), "duplicate", 1, {}),
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
                ia = a[4]["interp"] if len(a) > 4 else "absent"
                ib = b[6]["interp"] if len(b) > 6 else "absent"
                s.run("coder-1", a[0], a[1], a[2], a[3], at=0, interp=ia)
                pb = s.run("review-coder-1-1", b[0], b[1], b[2], b[3], at=10, cwd=b[4] if len(b) > 4 else WT,
                           label=b[5] if len(b) > 5 else "", interp=ib)
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
            with self.subTest("gate checks"):
                for label in ("gitleaks", "gitleaks-dirty"):
                    self.assertEqual(classes(label)[:4], ["first", "duplicate", "first", "first"], label)  # head, then base changed
                for label in ("typecheck:web", "eslint:web", "newcheck"):
                    self.assertEqual(classes(label), ["outside"] * 4, label)
                self.assertEqual(classes("prettier:web"), ["first", "duplicate", "duplicate", "duplicate"])  # files+tree only
                self.assertEqual(classes("gitleaks")[4:], ["outside", "outside"])  # head or base.sha not recorded
                self.assertTrue(all("head/base" in r["outside"] for r in g3 if r["label"] == "gitleaks" and r["class"] == "outside"))
                self.assertTrue(all(r["outside"].startswith("not understood: ") for r in g3 if r["class"] == "outside"))
                self.assertEqual(rep["totals"]["not_understood"], len([r for r in rep["runs"] if r["class"] == "outside" and r["task"] == "coder-3"]))
            # R4 F1: each masking row is its own subTest, independent of the gate assertions above. An argv element NAME=VALUE
            # is hidden whole; shell text is hidden from its first secret-looking assignment to the end of the text.
            masking = [  # cmd, words that must not show, words that must (the text before the assignment)
                (["env", "API_TOKEN=s3cret", "python3"], ("s3cret",), ("API_TOKEN=<hidden>", "python3")),
                (["bash", "-c", "export DB_PASSWORD='p w' X=1; API_KEY=abc cmd"], ("p w", "X=1", "abc", "cmd"), ("export DB_PASSWORD=<hidden>",)),
                (["bash", "-c", "AUTH_HEADER=zzz python3 -m unittest"], ("zzz", "unittest"), ("AUTH_HEADER=<hidden>",)),
                (["env", "API_TOKEN=dummy firstw secondw", "python3"], ("firstw", "secondw"), ("API_TOKEN=<hidden>",)),
                (["env", "DB_PASSWORD=semi;colonw 'quotew\"", "python3"], ("colonw", "quotew"), ("DB_PASSWORD=<hidden>",)),
                (["bash", "-c", "export API_KEY=a\"qb cw\"dw ew; true"], ("qb", "cw", "dw", "ew", "true"), ("export API_KEY=<hidden>",)),
                (["bash", "-c", "echo x; AUTH_X=esc\\ apedw fw"], ("apedw", "fw"), ("echo x;", "AUTH_X=<hidden>")),
                (["bash", "-c", "SECRET_Y='open quoted w"], ("open", "quoted"), ("SECRET_Y=<hidden>",)),
                # the review's probes: an escaped double quote inside the value, and a command substitution in the value
                (["bash", "-c", "export API_KEY=\"abc\\\"defw ghiw\"; true"], ("abc", "defw", "ghiw", "true"), ("export API_KEY=<hidden>",)),
                (["bash", "-c", "echo ok; API_KEY=\"abc\\\"defw ghiw\" cmd"], ("defw", "ghiw", "cmd"), ("echo ok;", "API_KEY=<hidden>")),
                (["bash", "-c", "export API_KEY=$(printf \"dummyw wordsw\") cmd"], ("printf", "dummyw", "wordsw", "cmd"), ("export API_KEY=<hidden>",)),
                (["bash", "-c", "echo x; MY_TOKEN=`cat tokenfilew`; true"], ("tokenfilew", "true"), ("echo x;", "MY_TOKEN=<hidden>")),
            ]
            for cmd, leaked, kept in masking:
                shown = " ".join(repeats.shown_cmd(cmd))
                for word in leaked:
                    with self.subTest("masking: %s must not show %s" % (cmd[-1][:40], word)):
                        self.assertNotIn(word, shown)
                for word in kept:
                    with self.subTest("masking: %s keeps %s" % (cmd[-1][:40], word)):
                        self.assertIn(word, shown)
            with self.subTest("roles, tests.jsonl, bad records"):
                self.assertEqual({r["task"]: r["role"] for r in rep["runs"] if r["kind"] == "test-run"},
                                 {"main-like": "main", "no-meta": "unknown"})
                self.assertEqual(rep["totals"]["main_visible"], 1)
                self.assertEqual(rep["tests_jsonl"]["lines"], 1)                 # counted apart, never a run
                problems = " | ".join(p["problem"] for p in rep["problems"])
                for want in ("malformed test-run record", "invalid test-run record", "symlink skipped", "malformed tests.jsonl"):
                    self.assertIn(want, problems)

    def test_main_runs_join_the_groups_and_their_rerun_seconds_are_reported(self):
        """HB5b rule 3: SESSION/main-runs records (role main, lane, step from the record) join the same groups; per lane and step
        the seconds Main spent on checks a worker or reviewer had run on the same tree. One row per relation."""
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "session"
            tasks = session / "tasks"
            s, m = Tasks(tasks), Tasks(session)
            s.task("coder-1", lane="ln", step="S1")
            s.task("review-coder-1-1", lane="ln", step="S1", review_of="coder-1")
            xy, x, z = PY + ["tests.test_x", "tests.test_y"], PY + ["tests.test_x"], PY + ["tests.test_z"]
            main = lambda cmd, tree=T, seconds=1.0, at=0, interp="absent", lane="ln", step="S1": m.run(
                ".", cmd, tree, 0, seconds, at=at, interp=interp, dirname="main-runs",
                extra={"role": "main", **({"lane": lane} if lane else {}), **({"step": step} if step else {})})
            s.run("coder-1", xy, T, 0, 10, at=1, interp=(P314, "3.14.0"))
            s.run("review-coder-1-1", z, T, 0, 2, at=2, interp=(P314, "3.14.0"))
            main(xy, T, 9, 3, (P314, "3.14.0"))   # 1: same cmd as the worker, same tree and interpreter
            main(x, T, 4, 4, (P314, "3.14.0"))    # 2: a subset of the worker's run
            main(z, T, 2, 5)                      # 3: the reviewer's check, no interpreter field
            main(xy, T2, 8, 6, (P314, "3.14.0"))  # 4: same cmd on another tree
            main(xy, T, 7, 7, (P39, "3.9.6"))     # 5: same cmd, other interpreter
            main(PY + ["tests.test_q"], T, 5, 8)  # 6, and 7: Main's own repeat of its own run
            main(PY + ["tests.test_q"], T, 6, 9)
            main(xy, T, 20, 10, (P314, "3.14.0"), lane="other", step=None)  # 8: another lane, no step
            rep = repeats.build(str(tasks))
            got = {(r["lane"], r["step"], r["path"].rsplit("/", 1)[-1], r["role"]): r["class"] for r in rep["runs"] if r["role"] == "main"}
            self.assertEqual(got[("ln", "S1", "1.json", "main")], "duplicate")                 # worker's run, same everything
            self.assertEqual(got[("ln", "S1", "2.json", "main")], "first")                     # covered by the worker's run
            self.assertEqual(got[("ln", "S1", "3.json", "main")], "duplicate")                 # reviewer's run; no field keeps today's rule
            self.assertEqual(got[("ln", "S1", "4.json", "main")], "first")                     # other tree
            self.assertEqual(got[("ln", "S1", "5.json", "main")], "first")                     # other interpreter: never the same check
            self.assertEqual(got[("ln", "S1", "7.json", "main")], "duplicate")                 # Main's own repeat of Main's run
            self.assertEqual(got[("other", "(none)", "8.json", "main")], "duplicate")          # lane and step as the record says
            per = {(r["lane"], r["step"]): r for r in rep["per_lane_step"]}
            # duplicate xy 9 s + covered x 4 s + duplicate z 2 s; the repeat of Main's own run (6 s) and the other lane's (20 s,
            # its lane has no worker run: a duplicate of the worker's run on the same tree) are apart
            self.assertAlmostEqual(per[("ln", "S1")]["main_rerun"], 9 + 4 + 2)
            self.assertAlmostEqual(per[("other", "(none)")]["main_rerun"], 20.0)
            self.assertEqual(rep["totals"]["main_visible"], 8)
            self.assertIn("Main's reruns", repeats.render(rep))
            # --lane keeps the lane of the record; the tasks dir named otherwise does not look for main-runs
            self.assertEqual({r["lane"] for r in repeats.build(str(tasks), "other")["runs"]}, {"other"})
            self.assertEqual(repeats.build(str(tasks), main_dir=None)["totals"]["main_visible"], 0)
            self.assertNotIn("Main's reruns", repeats.render(repeats.build(str(tasks), main_dir=None)))
            # a bad interpreter field and a link are listed and skipped, never raised
            m.run(".", xy, T, 0, 1, dirname="main-runs", extra={"role": "main", "interpreter": "python3"})
            (session / "main-runs" / "11.json").symlink_to(session / "tasks")
            problems = " | ".join(p["problem"] for p in repeats.build(str(tasks))["problems"])
            self.assertIn("interpreter is not null or {path, version}", problems)
            self.assertIn("symlink skipped", problems)

    def test_main_rerun_seconds_are_judged_per_main_run_against_every_earlier_worker_or_reviewer_run(self):
        """HB5b round 2 F1: one scenario per row; each Main run's seconds count when an earlier (by end time) successful worker
        or reviewer run of the same tree, runner and interpreter holds its tests (all seconds), or lies inside them (the
        smaller time, estimated); other Main runs never cover a Main run."""
        xy, x, y = PY + ["tests.test_x", "tests.test_y"], PY + ["tests.test_x"], PY + ["tests.test_y"]
        I14, I39 = (P314, "3.14.0"), (P39, "3.9.6")
        # name, runs in time order: (who, cmd, tree, seconds, interp), expected main_rerun of the lane
        rows = [
            ("worker xy, Main x, Main x: each judged against the worker", [("w", xy, T, 3, I14), ("m", x, T, 4, I14), ("m", x, T, 5, I14)], 9),
            ("Main xy, worker xy, Main x: the later Main run is covered; the first is before any worker", [
                ("m", xy, T, 7, I14), ("w", xy, T, 3, I14), ("m", x, T, 4, I14)], 4),
            ("a reviewer is the earlier run", [("r", xy, T, 3, I14), ("m", x, T, 4, I14)], 4),
            ("a covering run on another tree", [("w", xy, T2, 3, I14), ("m", x, T, 4, I14)], 0),
            ("a covering run with another interpreter", [("w", xy, T, 3, I39), ("m", x, T, 4, I14)], 0),
            ("a partial cover: the worker's tests lie inside Main's", [("w", x, T, 3, I14), ("m", xy, T, 8, I14)], 3),
            ("no cover: disjoint tests", [("w", y, T, 3, I14), ("m", x, T, 4, I14)], 0),
            ("Main before any worker run", [("m", x, T, 4, I14), ("w", x, T, 3, I14)], 0),
            ("a failed worker run covers nothing", [("w", x, T, 3, I14, 1), ("m", x, T, 4, I14)], 0),
            # round 3: strict, numeric end-time comparison (the display order breaks ties by task name; this does not).
            # The optional 4th element is the end time of each run (None: unreadable); default 1, 2, ...
            ("equal end times, same check (worker name sorts first)", [("w", xy, T, 3, I14), ("m", xy, T, 4, I14)], 0, [5, 5]),
            ("equal end times, covered", [("w", xy, T, 3, I14), ("m", x, T, 4, I14)], 0, [5, 5]),
            ("equal end times, reviewer, covered", [("r", xy, T, 3, I14), ("m", x, T, 4, I14)], 0, [5, 5]),
            ("worker one microsecond earlier, same check", [("w", xy, T, 3, I14), ("m", xy, T, 4, I14)], 4, [5, 5.000001]),
            ("worker one microsecond earlier, covered", [("w", xy, T, 3, I14), ("m", x, T, 4, I14)], 4, [5, 5.000001]),
            ("worker later than Main", [("w", xy, T, 3, I14), ("m", xy, T, 4, I14)], 0, [5.000001, 5]),
            ("worker end time missing, same check", [("w", xy, T, 3, I14), ("m", xy, T, 4, I14)], 0, [None, 5]),
            ("worker end time missing, covered", [("w", xy, T, 3, I14), ("m", x, T, 4, I14)], 0, [None, 5]),
            ("Main end time missing", [("w", xy, T, 3, I14), ("m", xy, T, 4, I14)], 0, [5, None]),
            ("both end times missing", [("w", xy, T, 3, I14), ("m", x, T, 4, I14)], 0, [None, None]),
        ]
        for name, runs, want, *ends in rows:
            ends = ends[0] if ends else list(range(1, len(runs) + 1))
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                session = Path(tmp) / "session"
                s, m = Tasks(session / "tasks"), Tasks(session)
                s.task("coder-1")
                s.task("review-coder-1-1", review_of="coder-1")
                unreadable = set()
                for at, (who, cmd, tree, sec, interp, *rc) in zip(ends, runs):
                    path = (m.run(".", cmd, tree, 0, sec, at=at or 0, interp=interp, dirname="main-runs",
                                  extra={"role": "main", "lane": "ln", "step": "S1"}) if who == "m" else
                            s.run("coder-1" if who == "w" else "review-coder-1-1", cmd, tree, rc[0] if rc else 0, sec,
                                  at=at or 0, interp=interp))
                    if at is None:
                        unreadable.add(path)
                real = repeats.Reader.mtime
                with mock.patch.object(repeats.Reader, "mtime", lambda self, p: None if str(p) in unreadable else real(self, p)):
                    per = {(r["lane"], r["step"]): r for r in repeats.build(str(session / "tasks"))["per_lane_step"]}
                self.assertAlmostEqual(per[("ln", "S1")]["main_rerun"], want)

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
