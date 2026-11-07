"""Worker cwd selection with real tp-spawn/tp-review and fake tmux (no agent launch)."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
HANG_GUARD = 300  # seconds; far above the worst load time seen (27 s for the slowest test), below a stuck run's bound

# Every row is (file name, lines, heading spec, expected section range). The heading/fence table and the markdown_it parity test
# both read it, so they cannot drift apart.
MD_TABLE = [(name, ["## S", op, inner, "## Inner", op, "text", "## Next"], "S", "1-6")
            for name, op, inner in (("f3", "```", "``"), ("f4", "````", "```"), ("f5", "`````", "````"),
                                    ("tilde", "~~~~", "~~~"))]   # an inner shorter fence and a heading do not end the section
MD_TABLE += [("unclosed", ["## S", "```", "a", "## Z", "b"], "S", "1-5"),       # an unclosed fence runs to the end
             ("indent", ["## S", "    ```", "## Z", "x"], "S", "1-2"),         # 4 spaces: code, not a fence
             ("csharp1", ["## C", "a", "## C#", "b"], "C", "1-2"),
             ("csharp2", ["## C", "a", "## C#", "b"], "C#", "3-4"),
             ("closing", ["## C ##", "x", "## D", "y"], "C", "1-2"),          # a closing run after a space is dropped
             ("hash", ["## S", "#hashtag", "more", "####### seven", "## E"], "S", "1-4"),   # neither is a heading
             ("nested", ["# T", "a", "## S", "b", "### D", "c", "## E", "d"], "S", "3-6")]


class WorkdirTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.state = self.base / ".tmux-pi"
        (self.state / "tasks").mkdir(parents=True)
        self.original = self.base / "original project"
        self.project = self.base / "new project"
        self.other = self.base / "later project"
        for path in (self.original, self.project, self.other):
            path.mkdir()
        fake = self.base / "fake-bin"
        fake.mkdir()
        tmux = fake / "tmux"
        tmux.write_text('''#!/bin/sh
case "$1" in
 has-session) exit 0 ;;
 show-options)
   case "$*" in
     *'@tp_state_dir'*) printf '%s\\n' "$TP_STATE_DIR" ;;
     *'@tp_main_pane'*) printf '%s\\n' "$TP_MAIN_PANE" ;;
   esac ;;
 display-message)
   [ "$5" = '#{pane_current_path}' ] && [ "$4" = '%main' ] || exit 1
   printf '%s\\n' "$TP_MAIN_CWD" ;;
 new-window)
   printf '%s\\n' "$*" >> "$TP_TMUX_LOG"
   printf '%%fake\\n' ;;
esac
''')
        tmux.chmod(0o755)
        self.log = self.base / "tmux.log"
        self.env = {**os.environ, "TP_STATE_DIR": str(self.state), "TP_SESSION": "fixture",
                    "TP_MAIN_PANE": "%main", "TP_MAIN_CWD": str(self.project),
                    "TP_TMUX_LOG": str(self.log), "PATH": str(fake) + os.pathsep + os.environ["PATH"]}
        self.env.pop("TMUX", None)

    def cli(self, script, *args, **updates):
        env = {**self.env, **updates}
        # The timeout is a hang guard, not a speed limit: one tp-review takes 10-15 s on a loaded machine (many process starts).
        return subprocess.run([str(ROOT / "bin" / script), *args], env=env, text=True,
                              capture_output=True, timeout=HANG_GUARD)

    def meta(self, run):
        self.assertEqual(run.returncode, 0, run.stderr)
        task_dir = self.state / "tasks" / run.stdout.strip()
        return task_dir, json.loads((task_dir / "meta.json").read_text())

    def test_main_pane_cwd_not_state_or_callers_cwd(self):
        run = self.cli("tp-spawn", "--worker", "claude", "--context", "context.txt", "Read context")
        self.assertNotEqual(run.returncode, 0)  # relative context belongs to main cwd
        (self.project / "context.txt").write_text("project data")
        task, meta = self.meta(self.cli("tp-spawn", "--worker", "claude", "--context", "context.txt", "Read context"))
        self.assertEqual(meta["workdir"], str(self.project.resolve()))
        self.assertIn("project data", (task / "packet.md").read_text())
        self.assertIn(f"cd {str(self.project.resolve()).replace(' ', chr(92) + ' ')}", (task / "run.sh").read_text())
        self.assertIn(f"-c {self.project.resolve()}", self.log.read_text())
        self.assertNotEqual(meta["workdir"], str(self.original.resolve()))

    def test_heading_context_resolves_the_section_labels_it_and_refuses_missing_or_repeated_headings(self):
        doc = ["# Title", "intro", "## Rules", "rule one", "### Detail", "detail line", "## Other", "other line",
               "```", "## Rules", "```", "## Dup", "a", "## dup", "b"]
        (self.project / "x.md").write_text("\n".join(doc) + "\n")
        (self.project / "a#b.md").write_text("one\ntwo\nthree\n")
        task, _ = self.meta(self.cli("tp-spawn", "--worker", "claude", "--context", "x.md#rules",
                                     "--context", "x.md##  Detail", "--context", "a#b.md:2-3", "Read"))
        packet = (task / "packet.md").read_text()
        # nested heading included, ends before the next same-level heading, a fenced "## Rules" is not a second match
        self.assertIn('## x.md (section "rules", lines 3-6)\n', packet)
        self.assertIn("rule one\n### Detail\ndetail line\n", packet)
        self.assertNotIn("other line", packet)
        self.assertIn('## x.md (section "Detail", lines 5-6)\n', packet)    # a '#' prefix and spaces of the spec are ignored
        self.assertIn("## a#b.md (lines 2-3)\n", packet)    # no regression: '#' in a file name with a line range
        for spec, parts in (("x.md#Nope", ["heading", "Nope", "x.md"]),
                            ("x.md#dup", ["ambiguous", "x.md", "12: ## Dup", "14: ## dup"])):
            with self.subTest(spec):
                run = self.cli("tp-spawn", "--check", "--worker", "claude", "--context", spec, "Read")
                self.assertNotEqual(run.returncode, 0)
                for part in parts:
                    self.assertIn(part, run.stderr)
        self.assertEqual(self.cli("tp-spawn", "--check", "--worker", "claude", "--context", "a#b.md", "Read").returncode, 0)

    def test_heading_context_follows_commonmark_fences_and_atx_headings(self):
        # One spawn reads all the range rows of MD_TABLE; the refusal rows use --check.
        rows = MD_TABLE
        packet_args = []
        for name, lines, spec, _ in rows:
            (self.project / f"{name}.md").write_text("\n".join(lines) + "\n")
            packet_args += ["--context", f"{name}.md#{spec}"]
        task, _ = self.meta(self.cli("tp-spawn", "--worker", "claude", *packet_args, "Read"))
        packet = (task / "packet.md").read_text()
        for name, _, spec, span in rows:
            with self.subTest(name):
                self.assertIn(f'## {name}.md (section "{spec}", lines {span})\n', packet)
        (self.project / "r.md").write_text("## S\n#hashtag\n")
        for spec, parts in (("r.md#hashtag", ["heading not found", "hashtag", "r.md"]),
                            ("r.md#", ["heading is empty", "r.md"]), ("r.md# ", ["heading is empty", "r.md"])):
            with self.subTest(spec):
                run = self.cli("tp-spawn", "--check", "--worker", "claude", "--context", spec, "Read")
                self.assertNotEqual(run.returncode, 0)
                for part in parts:
                    self.assertIn(part, run.stderr)

    def test_heading_parser_agrees_with_markdown_it(self):
        try:
            from markdown_it import MarkdownIt
        except ImportError:
            self.skipTest("markdown_it is not installed")
        # The same fixtures as the fence and heading table (MD_TABLE): for every heading markdown_it finds, section_range given
        # that heading's text must start at that line and, when the text is unique, end where markdown_it's next heading of
        # the same or higher level begins.
        source = (ROOT / "bin" / "tp-spawn").read_text()
        code = source.split("section_range() {\n  python3 - \"$1\" \"$2\" <<'PY'\n")[1].split("\nPY\n")[0]
        path = self.base / "p.md"
        for name, lines, _, _ in MD_TABLE:
            text = "\n".join(lines) + "\n"
            path.write_text(text)
            tokens = MarkdownIt().parse(text)
            heads = [(t.map[0] + 1, int(t.tag[1:]), tokens[i + 1].content) for i, t in enumerate(tokens)
                     if t.type == "heading_open"]
            for line, level, heading in heads:
                with self.subTest(name, heading=heading):
                    out = io.StringIO()
                    with mock.patch.object(sys, "argv", ["-", str(path), heading]), contextlib.redirect_stdout(out):
                        try:
                            exec(code, {"__name__": "section_range"})
                        except SystemExit:
                            pass
                    got = out.getvalue().split("\n")
                    same = [h for h in heads if h[2].casefold() == heading.casefold()]
                    if len(same) > 1:
                        self.assertEqual(got[0], "ambiguous")
                        self.assertIn(f"{line}: {'#' * level} {heading}", got)
                    else:
                        nxt = [h[0] for h in heads if h[0] > line and h[1] <= level]
                        self.assertEqual(got[0], f"{line} {(nxt[0] - 1) if nxt else len(lines)}")

    def test_context_spec_form_follows_one_parse_order(self):
        # heading spec (first '#' whose left side is a file) > line-range spec (left side is a file) > plain path
        for name, lines in (("h1.md", ["# Top", "## Rules:2-3", "body", "## Z"]), ("a#b.md", ["one", "two", "three"]),
                            ("a#h.md", ["## H", "x"]), ("c.md", ["## C", "x", "## C#", "y"]),
                            ("x:2-3", ["plain", "text", "here"])):
            (self.project / name).write_text("\n".join(lines) + "\n")
        specs = ["h1.md#Rules:2-3", "a#b.md:2-3", "a#h.md#H", "c.md#C ", "c.md#C#", "x:2-3"]
        args = [arg for spec in specs for arg in ("--context", spec)]
        task, _ = self.meta(self.cli("tp-spawn", "--worker", "claude", *args, "Read"))
        packet = (task / "packet.md").read_text()
        for label in ('## h1.md (section "Rules:2-3", lines 2-3)\n', "## a#b.md (lines 2-3)\n",   # the ':a-b' is heading text
                      '## a#h.md (section "H", lines 1-2)\n',                 # the second '#' splits: 'a' is not a file
                      '## c.md (section "C", lines 1-2)\n',                   # trailing whitespace is trimmed
                      '## c.md (section "C#", lines 3-4)\n',                  # a trailing '#' is kept
                      "## x:2-3\n\n```\nplain\ntext\nhere\n```\n"):                     # a file literally named x:2-3 is the whole file
            with self.subTest(label):
                self.assertIn(label, packet)
        for spec, parts in (("c.md#", ["heading is empty", "c.md"]), ("c.md# ", ["heading is empty", "c.md"]),
                            ("c.md#C#x", ["heading not found", "C#x", "c.md"]),
                            ("h1.md#Rules:2-4", ["heading not found", "Rules:2-4", "h1.md"])):
            with self.subTest(spec):
                run = self.cli("tp-spawn", "--check", "--worker", "claude", "--context", spec, "Read")
                self.assertNotEqual(run.returncode, 0)
                for part in parts:
                    self.assertIn(part, run.stderr)

    def test_missing_or_invalid_main_fails_closed(self):
        for updates in ({"TP_MAIN_PANE": ""}, {"TP_MAIN_CWD": ""}, {"TP_MAIN_CWD": str(self.base / "gone")}):
            with self.subTest(updates=updates):
                run = self.cli("tp-spawn", "--worker", "claude", "Task", **updates)
                self.assertNotEqual(run.returncode, 0)
                self.assertIn("workdir" if updates.get("TP_MAIN_CWD") else "main pane", run.stderr)
        self.assertFalse(list((self.state / "tasks").iterdir()))
        self.assertFalse(self.log.exists())

    def test_explicit_project_and_review_inherit_across_main_move(self):
        author, meta = self.meta(self.cli("tp-spawn", "--worker", "pi", "--workdir", str(self.original), "Task"))
        self.assertEqual(meta["workdir"], str(self.original.resolve()))
        (author / "result.md").write_text("Done\nRESULT_STATUS: complete\n")
        (author / "state.json").write_text('{"state":"waiting"}')
        # The current pane now belongs to a different project, but review must
        # use the durable author cwd. --force skips policy routing only.
        reviewer, review_meta = self.meta(self.cli("tp-review", "--force", meta["id"], TP_MAIN_CWD=str(self.other)))
        self.assertEqual(review_meta["worker"], "claude")
        self.assertEqual(review_meta["workdir"], str(self.original.resolve()))
        self.assertIn(f"-c {self.original.resolve()}", self.log.read_text().splitlines()[-1])
        self.assertEqual((reviewer / "review-of").read_text().strip(), meta["id"])
        self.assertEqual(json.loads((author / "meta.json").read_text())["review"], review_meta["id"])

    def test_claude_author_pi_reviewer_and_explicit_path_without_main(self):
        author, meta = self.meta(self.cli("tp-spawn", "--worker", "claude", "--workdir", str(self.project),
                                          "Task", TP_MAIN_PANE=""))
        (author / "result.md").write_text("Done\nRESULT_STATUS: complete\n")
        (author / "exit").write_text("0")
        _, reviewer_meta = self.meta(self.cli("tp-review", "--force", meta["id"], TP_MAIN_PANE=""))
        self.assertEqual(reviewer_meta["worker"], "pi")
        self.assertEqual(reviewer_meta["workdir"], meta["workdir"])

    def test_invalid_overrides_and_missing_author_cwd_do_not_spawn(self):
        for path in ("relative", str(self.base / "missing"), "/", str(self.state)):
            with self.subTest(path=path):
                self.assertNotEqual(self.cli("tp-spawn", "--workdir", path, "Task").returncode, 0)
        task = self.state / "tasks" / "old"
        task.mkdir()
        (task / "meta.json").write_text('{"id":"old","worker":"pi"}')
        (task / "result.md").write_text("Done\nRESULT_STATUS: complete\n")
        (task / "state.json").write_text('{"state":"waiting"}')
        run = self.cli("tp-review", "--force", "old")
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("author workdir missing", run.stderr)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
