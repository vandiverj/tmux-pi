"""tp-refresh-status against a fake tmux; no live sessions."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
FAKE = '''#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
with open(os.environ['LOG'], 'a') as f: f.write(json.dumps(a) + '\\n')
cfg = json.load(open(os.environ['CFG']))
def t(): return a[a.index('-t') + 1]
# Like real tmux: a missing pane formats to an empty line with rc 0, an empty
# window target means the current window, and targets are ids, never patterns.
c = a[0]
if c == 'has-session': sys.exit(0)
if c == 'show-options':
    v = cfg['opts'].get(a[-1], '')
    if v: print(v)
elif c == 'display-message':
    src = cfg['starts'] if 'pane_start_command' in a[-1] else cfg['panes']
    v = src.get(t(), '')
    print('"%s"' % v if v and src is cfg['starts'] and cfg['quote'] else v)
elif c == 'list-panes':
    if '-s' in a: print('\\n'.join(cfg['session_panes']))
    else: print('\\n'.join(p for p, w in cfg['panes'].items() if w == (t() or cfg['current'])))
elif c == 'respawn-pane' and cfg.get('respawn_fail'): sys.exit(1)
'''

STATUS = f"trap '' INT QUIT TSTP; while true; do '{ROOT}/bin/tp-status'; sleep 1; done"
WORKER = "bash '/s/tasks/coder-1/run.sh'"

class RefreshStatusTests(unittest.TestCase):
    def run_cmd(self, opts=None, session_panes=None, respawn_fail=False, panes=None, starts=None, quote=True):
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        base = Path(d.name)
        (base / "tmux").write_text(FAKE); (base / "tmux").chmod(0o755)
        o = {"@tp_state_dir": "/s", "@tp_main_pane": "%1", "@tp_status_pane": "%2", "@tp_viewer_pane": "%3"}
        o.update(opts or {})
        panes = panes or {"%1": "@1", "%2": "@1", "%3": "@1", "%9": "@1", "%7": "@2"}
        s = {"%1": "pi", "%2": STATUS, "%3": "tail -f /dev/null", "%9": WORKER, "%7": WORKER}
        s.update(starts or {})
        cfg = {"opts": o, "panes": panes, "respawn_fail": respawn_fail, "starts": s, "quote": quote,
               "current": "@1", "session_panes": session_panes or ["%1", "%2", "%3", "%9"]}
        (base / "cfg.json").write_text(json.dumps(cfg))
        env = dict(os.environ, PATH=f"{base}:{os.environ['PATH']}", CFG=str(base / "cfg.json"),
                   LOG=str(base / "log"), TP_SESSION="tp")
        env.pop("TMUX", None); env.pop("TMUX_PANE", None)
        env.pop("TP_STATE_DIR", None)  # the fake session defines its own state dir
        r = subprocess.run([str(ROOT / "bin/tp-refresh-status")], env=env, capture_output=True, text=True)
        log = [json.loads(l) for l in (base / "log").read_text().splitlines()] if (base / "log").exists() else []
        return r, [l for l in log if l[0] in ("respawn-pane", "clear-history", "copy-mode")]

    def test_respawns_then_clears_only_status_pane(self):
        r, acts = self.run_cmd()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(acts, [["copy-mode", "-q", "-t", "%2"],
                                ["respawn-pane", "-k", "-t", "%2"],
                                ["clear-history", "-t", "%2"]])

    def test_not_tmux_pi_session(self):
        r, acts = self.run_cmd(opts={"@tp_state_dir": ""})
        self.assertNotEqual(r.returncode, 0); self.assertIn("not a tmux-pi session", r.stderr)
        self.assertEqual(acts, [])

    def test_no_recorded_status_pane(self):
        r, acts = self.run_cmd(opts={"@tp_status_pane": ""})
        self.assertNotEqual(r.returncode, 0); self.assertIn("no recorded status pane", r.stderr)
        self.assertEqual(acts, [])

    def test_missing_status_pane(self):
        r, acts = self.run_cmd(session_panes=["%1", "%3"])
        self.assertNotEqual(r.returncode, 0); self.assertIn("missing", r.stderr)
        self.assertEqual(acts, [])

    def test_refuses_main_or_viewer(self):
        for pane in ("%1", "%3"):
            r, acts = self.run_cmd(opts={"@tp_status_pane": pane})
            self.assertNotEqual(r.returncode, 0); self.assertIn("main or viewer", r.stderr)
            self.assertEqual(acts, [])

    def test_refuses_pane_in_other_window(self):
        r, acts = self.run_cmd(opts={"@tp_status_pane": "%7"}, session_panes=["%1", "%7"],
                               starts={"%7": STATUS})
        self.assertNotEqual(r.returncode, 0); self.assertIn("main window", r.stderr)
        self.assertEqual(acts, [])

    def test_respawn_failure_does_not_clear(self):
        r, acts = self.run_cmd(respawn_fail=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("clear-history", [a[0] for a in acts])

    def test_unquoted_start_command_accepted(self):
        r, acts = self.run_cmd(quote=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([a[0] for a in acts], ["copy-mode", "respawn-pane", "clear-history"])

    def test_refuses_worker_swapped_into_viewer_slot(self):
        # tp-view swap-pane: worker %5 sits in the main window, placeholder %3 in the worker's.
        r, acts = self.run_cmd(opts={"@tp_status_pane": "%5"},
                               panes={"%1": "@1", "%2": "@1", "%5": "@1", "%3": "@2"},
                               session_panes=["%1", "%2", "%5", "%3"], starts={"%5": WORKER})
        self.assertNotEqual(r.returncode, 0); self.assertIn("not started as the tp-status loop", r.stderr)
        self.assertNotIn(WORKER, r.stderr)  # Do not echo a worker's start command (may contain secrets).
        self.assertEqual(acts, [])

    def test_refuses_lookalike_start_commands(self):
        for cmd in ("", f"'{ROOT}/bin/tp-status'", f"bash -c 'run.sh # {STATUS}'", STATUS + "; rm -rf x",
                    STATUS.replace(str(ROOT), "/elsewhere")):
            r, acts = self.run_cmd(starts={"%2": cmd})
            self.assertNotEqual(r.returncode, 0, cmd); self.assertIn("tp-status loop", r.stderr)
            self.assertEqual(acts, [])

    def test_missing_main_pane(self):
        # %99 is gone: display-message prints an empty line with rc 0.
        r, acts = self.run_cmd(opts={"@tp_main_pane": "%99"})
        self.assertNotEqual(r.returncode, 0); self.assertIn("main pane %99 is missing", r.stderr)
        self.assertEqual(acts, [])

    def test_refuses_non_pane_id_values(self):
        for pane in ("%.", ".*", "%", "%2 ", "2", "%2x"):
            r, acts = self.run_cmd(opts={"@tp_status_pane": pane})
            self.assertNotEqual(r.returncode, 0, pane); self.assertIn("is not a pane id", r.stderr)
            self.assertEqual(acts, [])
        r, acts = self.run_cmd(opts={"@tp_main_pane": "%."})
        self.assertNotEqual(r.returncode, 0); self.assertIn("recorded main pane", r.stderr)
        self.assertEqual(acts, [])

if __name__ == "__main__":
    unittest.main()
