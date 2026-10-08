"""tp_rows (one python pass) must print exactly what the old per-task shell builder printed.

The old builder is kept in tests/fixtures/tp_rows_old.sh only for this comparison. Fake tmux; never a live session.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOKENS = {"input": 1234, "cacheRead": 2000000, "cacheWrite": 500, "output": 12}


class TpRowsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.state = self.base / "state"
        (self.state / "tasks").mkdir(parents=True)
        (self.state / "queue").mkdir()
        bindir = self.base / "bin"
        bindir.mkdir()
        tmux = bindir / "tmux"
        tmux.write_text('#!/bin/sh\ncase "$1" in\n  show-options) printf "%s\\n" "$FAKE_BASE/state" ;;\n'
                        '  list-panes) cat "$FAKE_BASE/panes" ;;\nesac\n')
        tmux.chmod(0o755)
        self.env = dict(os.environ, FAKE_BASE=str(self.base), ROOT=str(ROOT),
                        PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}")
        self.env.pop("TMUX", None)
        self.panes = []
        self.clock = 1_700_000_000

    def task(self, name, meta=None, state=None, pane="%1", dead=None, exit_text=None, raw_meta=None, raw_state=None):
        directory = self.state / "tasks" / name
        directory.mkdir()
        meta_path = directory / "meta.json"
        if raw_meta is not None:
            meta_path.write_text(raw_meta)
        else:
            meta_path.write_text(json.dumps({"id": name, "worker": "pi", "pane": pane, "model": "", "thinking": "", **(meta or {})}))
        if raw_state is not None:
            (directory / "state.json").write_text(raw_state)
        elif state is not None:
            (directory / "state.json").write_text(json.dumps(state))
        if exit_text is not None:
            (directory / "exit").write_text(exit_text)
        if dead is not None:
            self.panes.append(f"{pane} {dead}")
        self.clock += 10     # a distinct mtime each, so the order is the creation order
        os.utime(meta_path, (self.clock, self.clock))

    def build(self):
        """One task of every row kind, then compare."""
        working = {"state": "working", "tool": "bash", "model": "p/m", "tokens": TOKENS, "cost": 0.01249}
        self.task("live", state=working, pane="%1", dead=0)
        self.task("live-claude", meta={"worker": "claude", "model": "opus", "thinking": "high"}, pane="%2", dead=0,
                  state={"state": "working", "harness": "claude", "cost": 0, "tokens": {"input": 5, "output": 999}})
        self.task("claude-unknown", pane="%3", dead=0, state={"state": "settled", "costKnown": False, "cost": 3, "tokens": TOKENS})
        self.task("no-state-file", pane="%4", dead=0)
        self.task("empty-state", pane="%5", dead=0, raw_state="")
        self.task("null-state", pane="%6", dead=0, raw_state="null")
        self.task("finished", pane="%7", dead=1, exit_text="0\n", state={"state": "working", "tool": "x", "cost": 1.5})
        self.task("failed", pane="%8", dead=1, exit_text="137\n", state=working)
        self.task("failed-no-exit", pane="%9", dead=1, state=working)
        self.task("failed-empty-exit", pane="%10", dead=1, exit_text="", state=working)
        self.task("killed-gone", pane="%11", state=working)                    # pane not listed
        self.task("killed-odd", pane="%12", dead=2, state=working)
        self.task("empty-pane", meta={"pane": ""}, state=working)
        self.task("model-null", meta={"model": None, "thinking": None}, pane="%13", dead=0, state={"state": "starting"})
        self.task("model-thinking", meta={"model": "m", "thinking": "low"}, pane="%14", dead=0, state={"model": None})
        self.task("model-state", meta={"model": "m", "thinking": ""}, pane="%15", dead=0, state={"model": "from-state", "state": False, "tool": False})
        self.task("numbers", meta={"id": 7, "worker": True, "pane": "%16"}, pane="%16", dead=0,
                  state={"state": 3, "tool": 1.5, "tokens": {"input": 999, "output": 1000, "cacheRead": 0.5, "cacheWrite": 999.5}, "cost": 0.0005})
        self.task("tokens-missing", pane="%17", dead=0, state={"tokens": {}, "cost": 123456.78})
        self.task("tokens-false", pane="%18", dead=0, state={"tokens": False, "cost": True})
        self.task("tokens-edges", pane="%19", dead=0, state={"tokens": {"input": 1999999, "output": 999999, "cacheRead": 1000000, "cacheWrite": None}, "cost": 0.0004})
        self.task("unicode", meta={"worker": "π"}, pane="%20", dead=0, state={"state": "naïve", "model": "mödel"})
        self.task("cost-string", pane="%21", dead=0, state={"cost": "1"})              # jq cannot round it: row left out
        self.task("tokens-string", pane="%22", dead=0, state={"tokens": "x"})          # left out
        self.task("tokens-input-string", pane="%23", dead=0, state={"tokens": {"input": "5", "output": 1}})   # left out
        self.task("state-array", pane="%24", dead=0, state=[1])                       # left out
        self.task("bad-state", pane="%25", dead=0, raw_state="{not json")             # left out
        self.task("bad-meta", raw_meta="{not json")                                   # left out
        self.task("empty-meta", pane="%26", dead=0, raw_meta="", state=working)       # old: state becomes the meta
        self.task("meta-array", raw_meta="[1]", state=working)                        # left out
        self.task("model-object", pane="%27", dead=0, state={"model": {"a": 1}})      # left out
        self.task("two-values", pane="%28", dead=0, raw_state='{"state":"a"}{"state":"b"}')
        (self.base / "panes").write_text("\n".join(self.panes) + "\n")
        (self.state / "tasks" / "no-meta").mkdir()                                    # a directory without meta.json
        (self.state / "queue" / "q-valid.json").write_text("{not json")
        (self.state / "queue" / "q-null.json").write_text("null")

    def run_both(self):
        script = ('source "$ROOT/lib/tp.sh"; source "$ROOT/tests/fixtures/tp_rows_old.sh"; '
                  'tp_rows_old s > "$FAKE_BASE/old.out"; tp_rows s > "$FAKE_BASE/new.out"; '
                  'echo "rc $?"; tp_queue_lines "$FAKE_BASE/state"')
        done = subprocess.run(["bash", "-c", script], env=self.env, text=True, capture_output=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        old = (self.base / "old.out").read_bytes()
        new = (self.base / "new.out").read_bytes()
        return old, new, done.stdout

    def test_same_rows_as_the_old_builder_for_every_row_kind(self):
        self.build()
        old, new, queue = self.run_both()
        self.assertEqual(new, old)
        text = new.decode()
        lines = [line.split("\x1f") for line in text.splitlines()]
        names = [line[0] for line in lines]
        # the comparison is only worth something if the interesting kinds are really present
        for name in ("live", "finished", "failed", "failed-no-exit", "killed-gone", "model-thinking", "two-values"):
            self.assertIn(name, names)
        by_name = {line[0]: line for line in lines}
        self.assertEqual(by_name["finished"][2], "done")
        self.assertEqual(by_name["failed"][2], "failed:137")
        self.assertEqual(by_name["failed-no-exit"][2], "failed:?")
        self.assertEqual(by_name["killed-gone"][2], "killed")
        self.assertEqual(by_name["live-claude"][5], "$?")
        self.assertEqual(by_name["live"][4:6], ["↑1.2k c2M ↓12", "$0.012"])
        for name in ("cost-string", "tokens-string", "bad-state", "bad-meta", "state-array", "meta-array", "model-object", "no-meta"):
            self.assertNotIn(name, names)
        self.assertEqual(names[:3], ["live", "live-claude", "claude-unknown"])      # oldest meta.json first
        # the queue refusal rows come through the same unchanged function
        self.assertRegex(queue, r"q-null  queued  start refused: ")
        self.assertRegex(queue, r"q-valid  queued  start refused: ")

    def test_no_tasks_and_missing_directory(self):
        (self.base / "panes").write_text("")
        old, new, _ = self.run_both()
        self.assertEqual((old, new), (b"", b""))
        (self.state / "tasks").rmdir()
        old, new, _ = self.run_both()
        self.assertEqual((old, new), (b"", b""))


if __name__ == "__main__":
    unittest.main()
