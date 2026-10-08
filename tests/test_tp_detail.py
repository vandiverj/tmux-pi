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
        task("perm-broken", files={"permission-request.json": "{nope"})
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


if __name__ == "__main__":
    unittest.main()
