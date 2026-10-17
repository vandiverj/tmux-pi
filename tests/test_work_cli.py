"""`tp-agent work` commands (Phase A) through the real CLI, with a throwaway store and session."""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent


class WorkCli(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.store = self.tmp / "work"
        self.state = self.tmp / "project" / ".tmux-pi"
        (self.state / "tasks").mkdir(parents=True)
        self.env = {**os.environ, "TP_WORK_DIR": str(self.store), "TP_STATE_DIR": str(self.state)}
        self.env.pop("TMUX", None)

    def raw(self, *args, env=None):
        return subprocess.run([str(ROOT / "bin/tp-agent"), "work", *args], env=env or self.env, text=True, capture_output=True, timeout=30)

    def ok(self, *args):
        run = self.raw(*args)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def text(self, *args):
        run = self.raw(*args)
        self.assertEqual(run.returncode, 0, run.stderr)
        return run.stdout

    def test_workspace_lifecycle(self):
        made = self.ok("new", "CI flakiness DMP-76", "--objective", "Stop flaky CI", "--ticket", "CM-67", "--tag", "ci")
        ident = made["id"]
        self.assertEqual(made["tickets"], ["CM-67", "DMP-76"])
        self.assertEqual(made["repos"], [])  # a workspace needs no repo
        self.ok("next", ident, "watch pipeline")
        self.ok("next", ident, "second")
        self.ok("next", ident, "--done", "1")
        self.ok("decide", ident, "Keep reviewers cross-provider", "--source", "user")
        self.ok("block", ident, "waiting on infra")
        self.ok("link", ident, "--artifact", "/tmp/a", "--task", "run/t1", "--goal", "g1")
        self.ok("set", ident, "--status", "paused", "--scope", "ci only")
        shown = self.ok("show", ident)
        self.assertEqual(shown["meta"]["status"], "paused")
        self.assertEqual(shown["meta"]["goals"], ["g1"])
        for needle in ("- [x] watch pipeline", "- [ ] second", "Keep reviewers cross-provider Source: user", "waiting on infra", "artifacts: /tmp/a", "ci only", "Stop flaky CI"):
            self.assertIn(needle, shown["text"])
        self.assertEqual(self.ok("show", ident, "--section", "next")["text"].count("- ["), 2)
        self.assertEqual([r["id"] for r in self.ok("list")], [ident])
        self.assertEqual(self.ok("list", "--status", "done"), [])

    def test_list_shows_workspaces_or_notes_newest_first(self):
        old = self.ok("new", "Older")["id"]
        self.ok("new", "Newer")
        path = self.store / "workspaces" / f"{old}.md"
        path.write_text("\n".join("updated: 2020-01-01T00:00:00Z" if line.startswith("updated:") else line for line in path.read_text().split("\n")))
        self.assertEqual([r["id"] for r in self.ok("list")], ["newer", "older"])
        self.ok("note", "add", "A note", "--body", "x")
        self.assertEqual([r["id"] for r in self.ok("list", "--kind", "note")], ["a-note"])
        self.assertEqual([r["id"] for r in self.ok("list", "--kind", "note", "--status", "superseded")], [])

    def test_note_add_warns_about_a_similar_note_but_still_creates_it(self):
        first = self.raw("note", "add", "Loader batch limits", "--body", "a", "--ticket", "DMP-77", "--tag", "loader")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stderr, "")  # nothing similar yet: no warning
        same_title = self.raw("note", "add", "Loader batch limits", "--body", "b")
        self.assertEqual(same_title.returncode, 0, same_title.stderr)
        self.assertIn("warning: similar note loader-batch-limits (same title)", same_title.stderr)
        self.assertNotIn("work note supersede", same_title.stderr)
        self.assertIn("work supersede loader-batch-limits --title", same_title.stderr)
        created = json.loads(same_title.stdout)
        self.assertEqual(created["near_duplicates"], ["loader-batch-limits"])
        self.assertTrue((self.store / "notes" / f"{created['id']}.md").is_file())
        shared = self.raw("note", "add", "Different words entirely", "--ticket", "DMP-77", "--tag", "loader")
        self.assertIn("same ticket and tag", shared.stderr)
        unrelated = self.raw("note", "add", "Gardening", "--ticket", "ZZ-1", "--tag", "garden")
        self.assertEqual(unrelated.stderr, "")

    def test_the_suggested_replacement_command_runs_through_the_parser(self):
        self.ok("note", "add", "Loader batch limits", "--body", "old facts")
        warning = self.raw("note", "add", "Loader batch limits", "--body", "again").stderr
        suggested = re.search(r"`(work supersede [^`]+)`", warning).group(1)
        body = self.tmp / "replacement.md"
        body.write_text("replacement facts\n")
        words = [{"<new title>": "Loader batch limits v2", "<file>": str(body)}.get(w, w) for w in shlex.split(suggested)]
        self.assertEqual(words[:2], ["work", "supersede"])
        done = self.ok(*words[1:])
        self.assertEqual((done["superseded"], done["by"]), ("loader-batch-limits", "loader-batch-limits-v2"))
        self.assertIn("replacement facts", (self.store / "notes" / "loader-batch-limits-v2.md").read_text())

    def test_note_supersede_archive(self):
        ws = self.ok("new", "Work")["id"]
        n = self.ok("note", "add", "Auth token fix", "--body", "facts", "--workspace", ws, "--ticket", "DMP-1", "--tag", "auth")
        self.assertEqual(self.ok("note", "show", n["id"])["text"].strip(), "facts")
        dup = self.ok("note", "add", "Auth token fix")
        self.assertEqual(dup["near_duplicates"], [n["id"]])
        new = self.ok("supersede", n["id"], "--title", "Auth token fix v2", "--body", "better")
        self.assertEqual(new["superseded"], n["id"])
        old = self.store / "archive" / "notes" / f"{n['id']}.md"
        self.assertIn("superseded_by: auth-token-fix-v2", old.read_text())
        self.assertIn("status: superseded", old.read_text())
        self.assertEqual(self.raw("note", "show", n["id"]).returncode, 0)  # still readable from the archive
        archived = self.ok("archive", ws)
        self.assertEqual(archived["kind"], "workspace")
        self.assertEqual(self.ok("list"), [])
        self.assertEqual([r["id"] for r in self.ok("list", "--all")], [ws])
        self.assertTrue((self.store / "archive" / "workspaces" / f"{ws}.md").is_file())
        self.assertNotEqual(self.raw("next", ws, "x").returncode, 0)  # archived: read-only

    def test_note_workspace_must_exist_and_link_updates_note(self):
        self.assertNotEqual(self.raw("note", "add", "N", "--workspace", "nope").returncode, 0)
        ws = self.ok("new", "W")["id"]
        n = self.ok("note", "add", "Standalone")["id"]
        self.ok("link", ws, "--note", n)
        self.assertEqual(self.ok("note", "show", n)["meta"]["workspaces"], [ws])

    def test_attach_writes_session_file(self):
        ws = self.ok("new", "W")["id"]
        self.assertEqual(self.ok("attach", ws), {"attached": [ws]})
        self.ok("attach", ws)
        self.assertEqual(json.loads((self.state / "session-work.json").read_text()), {"attached": [ws]})
        self.assertNotEqual(self.raw("attach", "missing").returncode, 0)

    def test_survives_session_end_and_clean(self):
        ws = self.ok("new", "Durable")["id"]
        self.assertFalse(str(self.store).startswith(str(self.state)))  # the store is not under any session or repo state
        self.assertEqual([r["id"] for r in self.ok("list")], [ws])
        shutil.rmtree(self.state)  # the whole run state is gone, as after tp-clean/end
        self.assertEqual([r["id"] for r in self.ok("list")], [ws])

    def test_errors_exit_nonzero_without_traceback(self):
        run = self.raw("show", "missing")
        self.assertEqual(run.returncode, 1)
        self.assertIn("unknown workspace: missing", run.stderr)
        self.assertNotIn("Traceback", run.stderr)
        self.assertEqual(self.raw("new", "x", "--id", "Bad_ID").returncode, 1)

    def test_no_session_needed_except_attach(self):
        env = {k: v for k, v in self.env.items() if k != "TP_STATE_DIR"}
        env["PATH"] = str(self.tmp)  # no tmux
        env["PATH"] += os.pathsep + "/usr/bin" + os.pathsep + "/bin"
        run = self.raw("new", "No session", env=env)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.raw("list", env=env).returncode, 0)


if __name__ == "__main__":
    unittest.main()
