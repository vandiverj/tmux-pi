"""tp_detail.py (one pass, load time) must work out what the old per-row shell block in bin/tp-status worked out.

OLD below is that block (status/detail from lifecycle, collected/stopped, role, permission and capability requests, and
the selected row's first lines via grep), run per id in bash against the same seeded task directory.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
OLD = r'''
tasks="$1"; details="$2"
while IFS=$'\x1f' read -r id worker status tool tokens cost model; do
  [[ -n "$id" ]] || continue
  life="$(awk -F $'\x1f' -v id="$id" '$1 == id { print $2 }' <<<"$details")"
  if [[ -n "$life" ]]; then status="$life"; fi
  [[ ! -f "$tasks/$id/collected.json" ]] || status=completed
  [[ ! -f "$tasks/$id/stopped.json" ]] || status=stopped
  detail="$(awk -F $'\x1f' -v id="$id" '$1 == id { print $3 }' <<<"$details")"
  role="$(jq -r '.role // "generic"' "$tasks/$id/meta.json" 2>/dev/null)"
  detail="role $role${detail:+ · $detail}"
  if [[ -f "$tasks/$id/permission-request.json" ]]; then
    permission="$(jq -r 'select(.status == "pending") | "permission: " + (if .placeholder then (if .scope == "once" then "reveal " else "use " end) + .placeholder + (if .source then " from " + .source else "" end) + (if .scope then " [" + .scope + "]" else "" end) else (.target // "sensitive read") end)' "$tasks/$id/permission-request.json" 2>/dev/null)"
    if [[ -n "$permission" ]]; then detail="$permission · $detail"; [[ "$status" == working || "$status" == starting || "$status" == needs_input ]] && status=needs_input; fi
  fi
  if [[ -f "$tasks/$id/capability-request.json" ]]; then
    requested="$(jq -r 'select(.status == "pending") | "asks for " + (.kind // "help") + ": " + (.what // "")' "$tasks/$id/capability-request.json" 2>/dev/null)"
    if [[ -n "$requested" ]]; then detail="$requested · $detail"; [[ "$status" == working || "$status" == starting || "$status" == needs_input ]] && status=needs_input; fi
  fi
  d="$tasks/$id"
  tf="$(grep -m1 -v '^[[:space:]]*$' "$d/task.md" 2>/dev/null)"
  hr="" rf=""
  if [[ -s "$d/result.md" ]]; then hr=1; rf="$(grep -m1 -v '^[[:space:]]*$' "$d/result.md")"; fi
  printf '%s\x1f%s\x1f%s\x1f%s\x1f%s\n' "$status" "$detail" "$hr" "$tf" "$rf"
done
'''
SEP = "\x1f"


class TpDetailTest(unittest.TestCase):
    def test_same_as_the_old_block(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tasks = Path(tmp.name) / "tasks"
        tasks.mkdir()
        rows, lifecycle = [], []

        def task(name, status="working", meta=None, life=None, detail="", files=None):
            d = tasks / name
            d.mkdir()
            (d / "meta.json").write_text(json.dumps({"id": name, **(meta or {})}))
            for fname, content in (files or {}).items():
                (d / fname).write_text(content if isinstance(content, str) else json.dumps(content))
            rows.append(SEP.join((name, "pi", status, "", "", "", "m")))
            if life is not None or detail:
                lifecycle.append(SEP.join((name, life or "", detail)))

        pend = {"status": "pending"}
        task("plain")
        task("role", meta={"role": "coder"}, life="settled", detail="turns 3")
        task("role-null", meta={"role": None})
        task("role-number", meta={"role": 7})
        task("collected", files={"collected.json": {}})
        task("stopped", files={"collected.json": {}, "stopped.json": {}})
        task("perm-target", files={"permission-request.json": {**pend, "target": "~/.ssh"}})
        task("perm-default", files={"permission-request.json": pend})
        task("perm-placeholder", status="starting",
             files={"permission-request.json": {**pend, "placeholder": "{{K}}", "scope": "once", "source": "env"}})
        task("perm-use", status="needs_input", files={"permission-request.json": {**pend, "placeholder": "{{K}}", "scope": "session"}})
        task("perm-done", files={"permission-request.json": {"status": "granted", "target": "x"}})
        task("perm-bad", files={"permission-request.json": {**pend, "placeholder": 5}})
        # (a request file that is not exactly one JSON object is no longer skipped as jq skipped it: see UnreadableRequestTest)
        task("perm-settled", status="settled", life="settled", files={"permission-request.json": {**pend, "target": "t"}})
        task("cap", files={"capability-request.json": {**pend, "kind": "tool", "what": "jq"}})
        task("cap-default", status="needs_input", files={"capability-request.json": pend})
        task("cap-bad", files={"capability-request.json": {**pend, "what": 3}})
        task("both", files={"permission-request.json": {**pend, "target": "t"}, "capability-request.json": {**pend, "kind": "k", "what": "w"}})
        task("texts", files={"task.md": "\n  \t\n  first line\nsecond\n", "result.md": "\n\nreply one\nmore\n"})
        task("empty-result", files={"task.md": "only", "result.md": ""})
        task("blank-task", files={"task.md": "\n\n   \n"})
        task("deep-title", files={"task.md": "\n" * 17000 + "  the title\n", "result.md": " \t\n" * 40000 + "late reply\n"})
        task("long-line", files={"task.md": "L" * 5000 + " end\n", "result.md": "\n" + "R" * 3000 + "\r\n"})
        task("unicode", files={"task.md": "naïve — ✓\n", "result.md": "réponse\n"})
        rows_text = "\n".join(rows) + "\n"
        details = "\n".join(lifecycle) + "\n"
        run = lambda *cmd, **kw: subprocess.run(cmd, input=rows_text, capture_output=True, text=True, check=True, env=dict(os.environ, LC_ALL="en_US.UTF-8"), **kw).stdout
        old = run("bash", "-c", OLD, "old", str(tasks), details)
        new = run("python3", str(ROOT / "lib" / "tp_detail.py"), str(tasks), details)
        self.assertEqual(new, old)
        self.assertEqual(len(new.splitlines()), len(rows))
        self.assertIn("needs_input", new)
        self.assertIn("permission: reveal {{K}} from env [once]", new)
        self.assertIn("asks for tool: jq", new)

    def test_bound_on_a_huge_first_line(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name) / "tasks" / "big"
        d.mkdir(parents=True)
        (d / "meta.json").write_text("{}")
        (d / "task.md").write_text(" " * 200_000 + "\n" + "x" * 5_000_000)
        out = subprocess.run(["python3", str(ROOT / "lib" / "tp_detail.py"), str(d.parent), ""], input="big\x1f\x1fworking\n",
                             capture_output=True, text=True, check=True).stdout
        self.assertLessEqual(len(out), 66000)
        self.assertIn("xxxx", out)

    def test_list_style_prints_the_columns_and_detail_bin_tp_list_always_printed(self):
        """`--list` is bin/tp-list's reader: padded columns, a marker on the viewed row, the detail one line below, tp-list's
        words for a pending request, and a column that is too long still followed by spaces (as the printf did)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tasks = Path(tmp.name) / "tasks"
        for ident, files in (("short", {"permission-request.json": {"status": "pending", "target": "db"}}),
                             ("an-identifier-longer-than-22", {"capability-request.json": {"status": "pending", "kind": "tool", "what": "jq"}}),
                             ("quiet", {})):
            (tasks / ident).mkdir(parents=True)
            (tasks / ident / "meta.json").write_text(json.dumps({"id": ident, "role": "coder"}))
            for name, value in files.items():
                (tasks / ident / name).write_text(json.dumps(value))
        rows = "".join(SEP.join(f) + "\n" for f in (
            ("short", "pi", "working", "bash", "↑1 ↓2", "$0.1", "m"),
            ("an-identifier-longer-than-22", "pi", "working", "", "", "", ""),
            ("quiet", "claude", "settled", "", "", "", "m")))
        lifecycle = "short%sworking%s3 turns\n" % (SEP, SEP)
        out = subprocess.run(["python3", str(ROOT / "lib" / "tp_detail.py"), "--list", str(tasks), lifecycle, "quiet"], input=rows,
                             capture_output=True, text=True, check=True).stdout
        printf = r'''row() { printf '%s %s%*s%s%*s%s%*s%s%*s%s%*s%s\n' "$1" "$2" $((22 - ${#2})) "" "$3" $((8 - ${#3})) "" "$4" $((12 - ${#4})) "" "$5" $((16 - ${#5})) "" "$6" $((8 - ${#6})) "" "$7"; }
row " " short pi needs_input "↑1 ↓2" '$0.1' m; echo "    db · role coder · 3 turns · bash"
row " " an-identifier-longer-than-22 pi needs_input "" "" ""; echo "    jq · role coder"
row "▸" quiet claude settled "" "" m; echo "    role coder"'''
        expected = subprocess.run(["bash", "-c", printf], capture_output=True, text=True, check=True).stdout
        self.assertEqual(out, expected)   # the printf bin/tp-list used, byte for byte


UNREADABLE_SHAPES = {   # what a request file can hold that is not exactly one JSON object
    "null-then-object": 'null\n{"status":"pending","target":"db"}',
    "two-objects": '{"status":"pending","target":"a"}\n{"status":"pending","target":"b"}',
    "truncated": '{"status":"pend',
    "empty": "",
    "blank": "  \n",
    "array": '[{"status":"pending"}]',
    "string": '"pending"',
    "not-utf8": b'{"status":"pending","target":"\xff"}',
}


class UnreadableRequestTest(unittest.TestCase):
    """A request file that is not exactly one JSON object is never dropped and never aborts the list: the task says
    "unreadable request" and needs attention, in the pane's reader and in tp-list's alike."""

    def run_reader(self, tasks, rows, *extra):
        return subprocess.run(["python3", str(ROOT / "lib" / "tp_detail.py"), *extra, str(tasks), "", *(["x"] if extra else [])],
                              input=rows, capture_output=True, text=True, check=True).stdout

    def test_an_entry_that_exists_but_cannot_be_read_is_an_unreadable_request_not_an_absent_one(self):
        """A dangling link, a link loop, a directory and a file with no read permission all exist: none is "no request"."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tasks = Path(tmp.name) / "tasks"

        def dangling(path):
            os.symlink("missing-request.json", path)

        def loop(path):
            os.symlink(path.name, path)

        def directory(path):
            path.mkdir()

        def no_permission(path):
            path.write_text(json.dumps({"status": "pending"}))
            path.chmod(0)
            self.addCleanup(path.chmod, 0o600)
        entries = {"dangling": dangling, "loop": loop, "dir": directory, "mode0": no_permission}
        rows = []
        for kind, make in entries.items():
            for fname in ("permission-request.json", "capability-request.json"):
                ident = "%s-%s" % (kind, fname[0])
                (tasks / ident).mkdir(parents=True)
                (tasks / ident / "meta.json").write_text(json.dumps({"id": ident}))
                make(tasks / ident / fname)
                if kind == "mode0" and os.access(tasks / ident / fname, os.R_OK):
                    continue   # permissions are not enforced for this user (root)
                rows.append(SEP.join((ident, "pi", "working", "", "", "", "m")))
        # a task with no request entry at all is not accused
        (tasks / "none").mkdir()
        (tasks / "none" / "meta.json").write_text(json.dumps({"id": "none"}))
        rows.append(SEP.join(("none", "pi", "working", "", "", "", "m")))
        text = "\n".join(rows) + "\n"
        wording = lambda ident: "unreadable request: " + ("permission" if ident.endswith("p") else "capability") + "-request.json"
        for row, line in zip(rows, self.run_reader(tasks, text).splitlines()):
            ident = row.split(SEP)[0]
            status, detail = line.split(SEP)[:2]
            with self.subTest(task=ident, reader="pane"):
                if ident == "none":
                    self.assertNotIn("unreadable request", detail)
                    self.assertEqual(status, "working")
                else:
                    self.assertEqual(status, "needs_input")
                    self.assertIn(wording(ident), detail)
        # tp-list's reader: the id and status on a line, the detail on the line below it
        listed = self.run_reader(tasks, text, "--list").splitlines()
        for row in rows:
            ident = row.split(SEP)[0]
            at = [i for i, line in enumerate(listed) if line[2:2 + len(ident)] == ident and line[2 + len(ident):3 + len(ident)] == " "]
            self.assertEqual(len(at), 1, ident)
            with self.subTest(task=ident, reader="list"):
                if ident == "none":
                    self.assertNotIn("unreadable request", listed[at[0] + 1])
                else:
                    self.assertIn("needs_input", listed[at[0]].split())
                    self.assertIn(wording(ident), listed[at[0] + 1])

    def test_every_shape_in_either_request_file_is_shown_as_an_unreadable_request(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tasks = Path(tmp.name) / "tasks"
        rows = []
        for shape, content in UNREADABLE_SHAPES.items():
            for fname in ("permission-request.json", "capability-request.json"):
                ident = "%s-%s" % (shape, fname[0])   # short, so tp-list's 22-column id field leaves a space after it
                d = tasks / ident
                d.mkdir(parents=True)
                (d / "meta.json").write_text(json.dumps({"id": ident}))
                (d / fname).write_bytes(content if isinstance(content, bytes) else content.encode())
                rows.append(SEP.join((ident, "pi", "working", "", "", "", "m")))
        # a good neighbour is untouched, and an unreadable file on a task that is already finished still says so
        (tasks / "fine").mkdir()
        (tasks / "fine" / "meta.json").write_text(json.dumps({"id": "fine"}))
        (tasks / "fine" / "permission-request.json").write_text(json.dumps({"status": "granted", "target": "x"}))
        rows.append(SEP.join(("fine", "pi", "working", "", "", "", "m")))
        (tasks / "done").mkdir()
        (tasks / "done" / "meta.json").write_text(json.dumps({"id": "done"}))
        (tasks / "done" / "capability-request.json").write_text("")
        rows.append(SEP.join(("done", "pi", "failed", "", "", "", "m")))
        rows_text = "\n".join(rows) + "\n"
        pane = self.run_reader(tasks, rows_text).splitlines()
        listed = self.run_reader(tasks, rows_text, "--list").splitlines()
        self.assertEqual(len(pane), len(rows))
        for row, line in zip(rows, pane):
            ident = row.split(SEP)[0]
            status, detail = line.split(SEP)[:2]
            with self.subTest(task=ident):
                if ident == "fine":
                    self.assertEqual((status, detail), ("working", "role generic"))
                    continue
                self.assertIn("unreadable request: ", detail)
                self.assertEqual(status, "failed" if ident == "done" else "needs_input")
        # tp-list says the same: the status in the third column and the words on the line below
        for index, row in enumerate(rows):
            ident = row.split(SEP)[0]
            if ident == "fine":
                continue
            at = [i for i, text in enumerate(listed) if text[2:2 + len(ident)] == ident and text[2 + len(ident)] == " "]
            self.assertEqual(len(at), 1, ident)
            self.assertIn("failed" if ident == "done" else "needs_input", listed[at[0]].split())
            self.assertIn("unreadable request: ", listed[at[0] + 1])


if __name__ == "__main__":
    unittest.main()
