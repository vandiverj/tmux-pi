import json
import os
import random
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import context_broker  # noqa: E402
from context_broker import UNTRUSTED_INSTRUCTION, select_context  # noqa: E402


class ContextBrokerTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        (self.root / "src").mkdir()
        (self.root / "AGENTS.md").write_text("# Instructions\nUse pytest.\n")
        (self.root / "src" / "auth.py").write_text("def login():\n    return True\n")
        (self.root / "src" / "billing.py").write_text("def invoice():\n    return 'ok'\n")
        (self.root / ".env").write_text("TOKEN=secret\n")

    def test_manifest_records_repo_ticket_provenance_and_untrusted_instruction(self):
        out = select_context("Fix ticket: PROJ-12 in src/auth.py; do not use .env", self.root, preflight={"mode": "delegated", "artifact_tier": 1})
        manifest = out["manifest"]
        self.assertEqual(manifest["schema"], "tmux-pi-context-manifest:v1")
        self.assertIn(UNTRUSTED_INSTRUCTION, manifest["untrusted_content_instruction"])
        paths = [s.get("path") for s in manifest["sources"]]
        self.assertIn("AGENTS.md", paths)
        self.assertIn("src/auth.py", paths)
        self.assertNotIn(".env", out["contexts"])
        self.assertTrue(any(p.get("path") == ".env" and p.get("action") == "excluded" for p in manifest["protections"]))
        self.assertFalse([s for s in manifest["sources"] if s["source"] == "YouTrack"])
        self.assertIn("repository/filesystem search", manifest["selected_capabilities"])
        self.assertNotIn("YouTrack", manifest["selected_capabilities"])
        self.assertIn("capability_manifest", manifest)
        self.assertIn("youtrack", {f["id"] for f in manifest["capability_manifest"]["filtered"]})
        self.assertNotIn("YouTrack: authority", out["capability_note"])

    def test_filename_search_is_bounded_and_compact(self):
        big = self.root / "src" / "cache_policy.py"
        big.write_text("x = 1\n" * 5000)
        out = select_context("Review cache policy behavior", self.root)
        cache = [s for s in out["manifest"]["sources"] if s.get("path") == "src/cache_policy.py"][0]
        self.assertRegex(cache["range"], r"^1-\d+$")
        self.assertLessEqual(cache["content_size"], 16000)
        self.assertIn("src/cache_policy.py:" + cache["range"], out["contexts"])

    def test_profile_only_ticket_and_context_engine_remain_inactive(self):
        out = select_context("Use Context Engine background for ticket: PROJ-12 in src/auth.py", self.root)
        sources = [s["source"] for s in out["manifest"]["sources"]]
        self.assertNotIn("YouTrack", sources)
        self.assertNotIn("Context Engine", sources)
        filtered = {f["id"] for f in out["manifest"]["capability_manifest"]["filtered"]}
        self.assertIn("youtrack", filtered)
        self.assertIn("context_engine", filtered)

    def test_profile_only_db_microsoft_inactive_but_sequential_reference_remains(self):
        out = select_context(
            "Plan Azure SQL schema investigation with Context Engine background",
            self.root,
            preflight={"mode": "plan", "artifact_tier": 2},
        )
        by_source = {s["source"]: s for s in out["manifest"]["sources"]}
        self.assertNotIn("read-only DB", by_source)
        self.assertNotIn("official Microsoft docs", by_source)
        self.assertNotIn("Context Engine", by_source)
        self.assertIn("Sequential Thinking", by_source)
        filtered = {f["id"] for f in out["manifest"]["capability_manifest"]["filtered"]}
        self.assertIn("readonly_db", filtered)
        self.assertIn("official_docs", filtered)
        self.assertIn("context_engine", filtered)
        self.assertEqual(by_source["Sequential Thinking"]["trust"], "reasoning aid, not evidence")
        self.assertNotIn("read-only DB", "\n".join(out["contexts"]))


class ContentDiscoveryTests(unittest.TestCase):
    TASK = "Explain how frobnicate behaves"

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name).resolve() / "work"
        self.root.mkdir()
        self.outside = Path(d.name).resolve() / "outside"
        self.outside.mkdir()
        (self.root / "sample.ts").write_text("export function run() {\n  return frobnicate();\n}\n")

    def content_sources(self, out):
        return [s for s in out["manifest"]["sources"] if str(s.get("reason_selected", "")).startswith("content match:")]

    def test_body_only_hit_yields_enclosing_range(self):
        out = select_context(self.TASK, self.root)
        self.assertIn("sample.ts:1-3", out["contexts"])
        source = self.content_sources(out)[0]
        self.assertEqual(source["reason_selected"], "content match: frobnicate")
        self.assertEqual(source["range"], "1-3")
        self.assertEqual(source["hash"], context_broker._hash((self.root / "sample.ts").read_text()))
        # Only the hash, range and terms are recorded, never the matched text.
        self.assertNotIn("return frobnicate()", json.dumps(out))

    def test_no_match_returns_filename_result_only(self):
        out = select_context("Describe the zebra quokka", self.root)
        self.assertEqual(out["contexts"], [])
        self.assertEqual(self.content_sources(out), [])

    def test_sensitive_files_are_never_opened_or_listed(self):
        names = [".env", ".env.local", "credentials.json", "my_secret_notes.py", "server.pem"]
        for name in names:
            (self.root / name).write_text("frobnicate = 1\n")
        opened = []

        def recorder(original):
            def wrapped(path, *args, **kwargs):
                opened.append(Path(path).name)
                return original(path, *args, **kwargs)
            return wrapped

        with mock.patch.object(Path, "open", recorder(Path.open)), \
                mock.patch.object(Path, "read_bytes", recorder(Path.read_bytes)), \
                mock.patch.object(Path, "read_text", recorder(Path.read_text)):
            out = select_context(self.TASK, self.root)
        self.assertIn("sample.ts", opened)
        for name in names:
            self.assertNotIn(name, opened)
            self.assertNotIn(name, json.dumps(out))
        self.assertEqual(out["contexts"], ["sample.ts:1-3"])

    def test_symlink_leaving_workdir_is_skipped(self):
        target = self.outside / "escape.ts"
        target.write_text("function away() {\n  frobnicate();\n}\n")
        (self.root / "link.ts").symlink_to(target)
        (self.root / "linked").symlink_to(self.outside, target_is_directory=True)
        opened = []
        original = Path.read_bytes

        def read_bytes(path):
            opened.append(str(path))
            return original(path)

        with mock.patch.object(Path, "read_bytes", read_bytes):
            out = select_context(self.TASK, self.root)
        self.assertEqual(out["contexts"], ["sample.ts:1-3"])
        self.assertFalse([p for p in opened if "escape" in p or "link" in p])
        self.assertNotIn("escape", json.dumps(out))

    def test_symlink_to_sensitive_name_inside_workdir_is_skipped(self):
        (self.root / "conf").mkdir()
        (self.root / "conf" / "credentials.txt").write_text("frobnicate\n")
        (self.root / "alias.txt").symlink_to(self.root / "conf" / "credentials.txt")
        out = select_context(self.TASK, self.root)
        self.assertEqual(out["contexts"], ["sample.ts:1-3"])

    def test_local_config_logs_and_transcripts_are_never_opened(self):
        (self.root / "agent" / "sessions" / "run").mkdir(parents=True)
        (self.root / "conf").mkdir()
        denied = ["agent/sessions/run/chat.json", "agent/mcp.json", "agent/models-store.json", "conf/mcp.local.env",
                  "conf/appsettings.Development.json", "conf/api_token.txt", "trace.log", "events.jsonl"]
        for rel in denied:
            (self.root / rel).write_text("frobnicate\n")
        opened = []

        def recorder(original):
            def wrapped(path, *args, **kwargs):
                opened.append(Path(path).name)
                return original(path, *args, **kwargs)
            return wrapped

        with mock.patch.object(Path, "open", recorder(Path.open)), \
                mock.patch.object(Path, "read_bytes", recorder(Path.read_bytes)):
            hits = context_broker._content_matches(self.root, self.TASK, [], 5)
        self.assertEqual([(h["path"].name, h["range"], h["terms"]) for h in hits], [("sample.ts", (1, 3), ["frobnicate"])])
        self.assertIn("sample.ts", opened)
        self.assertFalse(set(opened) & {Path(rel).name for rel in denied})
        self.assertEqual(select_context(self.TASK, self.root)["contexts"], ["sample.ts:1-3"])

    def test_git_ignored_files_are_skipped_and_git_failure_disables_search(self):
        (self.root / "local.json").write_text('{"frobnicate": 1}\n')
        (self.root / "cache").mkdir()
        (self.root / "cache" / "dump.txt").write_text("frobnicate\n")
        with mock.patch.object(context_broker, "_git_ignored", return_value=({"local.json", "cache"}, "")):
            self.assertEqual(select_context(self.TASK, self.root)["contexts"], ["sample.ts:1-3"])
        with mock.patch.object(context_broker, "_git_ignored", return_value=(None, "git timed out")):
            self.assertEqual(select_context(self.TASK, self.root)["contexts"], [])
        if subprocess.run(["git", "init", "-q", str(self.root)], capture_output=True).returncode == 0:
            (self.root / ".gitignore").write_text("local.json\ncache/\n")
            self.assertEqual(context_broker._git_ignored(self.root), ({"local.json", "cache"}, ""))
            self.assertEqual(select_context(self.TASK, self.root)["contexts"], ["sample.ts:1-3"])

    def test_output_is_identical_across_runs(self):
        for index in range(8):
            (self.root / f"mod{index}.py").write_text(f"def f{index}():\n    return frobnicate({index})\n")
        first = select_context(self.TASK, self.root)
        second = select_context(self.TASK, self.root)
        self.assertEqual(first["contexts"], second["contexts"])
        self.assertEqual(len(first["contexts"]), 5)
        strip = lambda out: [{k: v for k, v in s.items() if k != "timestamp"} for s in out["manifest"]["sources"]]
        self.assertEqual(strip(first), strip(second))
        # Equal scores break ties on path.
        self.assertEqual(first["contexts"], sorted(first["contexts"]))

    def test_more_distinct_terms_rank_first(self):
        (self.root / "aaa.py").write_text("def one():\n    return frobnicate()\n")
        (self.root / "zzz.py").write_text("def two():\n    return frobnicate(quokka)\n")
        out = select_context("Explain frobnicate and quokka", self.root, max_repo_files=1)
        self.assertEqual(out["contexts"], ["zzz.py:1-2"])
        self.assertEqual(self.content_sources(out)[0]["reason_selected"], "content match: frobnicate, quokka")

    def test_range_is_clamped_at_end_of_file(self):
        lines = [f"line {n}" for n in range(1, 31)]
        lines[27] = "frobnicate here"
        (self.root / "flat.txt").write_text("\n".join(lines) + "\n")
        self.assertEqual(context_broker._enclosing_range(lines, 28), (8, 30))
        self.assertEqual(context_broker._enclosing_range(lines, 2), (1, 22))
        self.assertIsNone(context_broker._enclosing_range(lines, 31))
        self.assertIn("flat.txt:8-30", select_context(self.TASK, self.root)["contexts"])

    def test_oversized_block_is_truncated(self):
        body = ["    value_{} = '{}'".format(n, "x" * 80) for n in range(1000)]
        body[600] = "    frobnicate()"
        (self.root / "huge.py").write_text("def big():\n" + "\n".join(body) + "\n")
        out = select_context(self.TASK, self.root)
        source = [s for s in self.content_sources(out) if s["path"] == "huge.py"][0]
        start, end = (int(part) for part in source["range"].split("-"))
        self.assertLessEqual(start, 602)
        self.assertGreaterEqual(end, 602)
        self.assertLessEqual(source["content_size"], 16000)
        self.assertGreater(source["content_size"], 0)
        self.assertIsNone(context_broker._enclosing_range(["x" * 20000], 1))

    def test_discovered_extracts_respect_total_cap_and_file_size_cap(self):
        for index in range(5):
            body = ["    value_{} = '{}'".format(n, "x" * 80) for n in range(400)]
            body[10] = "    frobnicate()"
            (self.root / f"big{index}.py").write_text("def big():\n" + "\n".join(body) + "\n")
        (self.root / "sample.ts").unlink()
        (self.root / "giant.txt").write_text("frobnicate\n" + "y" * (600 * 1024) + "\n")
        out = select_context(self.TASK, self.root)
        sources = self.content_sources(out)
        self.assertTrue(sources)
        self.assertLessEqual(sum(s["content_size"] for s in sources), 60000)
        self.assertNotIn("giant.txt", [s["path"] for s in sources])

    def test_emitted_specs_match_spawn_regex_and_exist(self):
        (self.root / "src").mkdir()
        (self.root / "src" / "deep.py").write_text("class A:\n    def go(self):\n        return frobnicate()\n")
        (self.root / "odd:name.py").write_text("frobnicate()\n")
        (self.root / "blob.bin").write_bytes(b"frobnicate\0\0")
        spawn = (ROOT / "bin" / "tp-spawn").read_text()
        self.assertIn('"$spec" =~ ^(.+):([0-9]+)-([0-9]+)$', spawn)
        out = select_context(self.TASK, self.root)
        self.assertEqual(out["contexts"], ["sample.ts:1-3", "src/deep.py:2-3"])
        for spec in out["contexts"]:
            matched = re.fullmatch(r"(.+):([0-9]+)-([0-9]+)", spec)
            self.assertIsNotNone(matched)
            self.assertTrue((self.root / matched.group(1)).is_file())
            self.assertTrue(1 <= int(matched.group(2)) <= int(matched.group(3)))

    def test_unreadable_file_is_skipped(self):
        original = Path.read_bytes

        def read_bytes(path):
            if path.name == "broken.py":
                raise OSError("unreadable")
            return original(path)

        (self.root / "broken.py").write_text("frobnicate()\n")
        with mock.patch.object(Path, "read_bytes", read_bytes):
            out = select_context(self.TASK, self.root)
        self.assertEqual(out["contexts"], ["sample.ts:1-3"])

    def test_tp_agent_context_prints_contexts_and_reasons(self):
        run = subprocess.run([sys.executable, str(ROOT / "bin" / "tp-agent"), "context", "--task", self.TASK,
                              "--workdir", str(self.root)], text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout), {"contexts": ["sample.ts:1-3"],
                                                  "reasons": {"sample.ts:1-3": "content match: frobnicate"},
                                                  "skipped": []})

    def opened_during(self, call, record=lambda path: Path(path).name):
        opened = []

        def recorder(original):
            def wrapped(path, *args, **kwargs):
                opened.append(record(path))
                return original(path, *args, **kwargs)
            return wrapped

        with mock.patch.object(Path, "open", recorder(Path.open)), \
                mock.patch.object(Path, "read_bytes", recorder(Path.read_bytes)), \
                mock.patch.object(Path, "read_text", recorder(Path.read_text)):
            return call(), opened

    def test_filename_match_skips_transcripts_under_sessions(self):
        (self.root / "agent" / "sessions" / "run").mkdir(parents=True)
        (self.root / "agent" / "sessions" / "run" / "frobnicate_notes.md").write_text("chat\n")
        (self.root / "agent" / "sessions" / "run" / "frobnicate_chat.jsonl").write_text("{}\n")
        (self.root / "frobnicate_events.jsonl").write_text("{}\n")
        (self.root / "frobnicate_helper.py").write_text("x = 1\n")
        out, opened = self.opened_during(lambda: select_context(self.TASK, self.root))
        self.assertEqual(out["contexts"], ["frobnicate_helper.py", "sample.ts:1-3"])
        self.assertEqual(context_broker.context_reasons(out["manifest"])["frobnicate_helper.py"],
                         "deterministic filename/path match to task terms")
        self.assertFalse(set(opened) & {"frobnicate_notes.md", "frobnicate_chat.jsonl", "frobnicate_events.jsonl"})
        self.assertNotIn("sessions", json.dumps(out["manifest"]["sources"]))
        self.assertEqual(out["skipped"], [])

    def test_filename_match_skips_git_ignored_files_without_sniffing(self):
        (self.root / "frobnicate_local.py").write_text("x = 1\n")
        (self.root / "frobnicate.dat").write_text("plain text\n")
        (self.root / "cache").mkdir()
        (self.root / "cache" / "frobnicate_dump.txt").write_text("x\n")
        ignored = {"frobnicate_local.py", "frobnicate.dat", "cache"}
        with mock.patch.object(context_broker, "_git_ignored", return_value=(ignored, "")):
            out, opened = self.opened_during(lambda: select_context(self.TASK, self.root))
        self.assertEqual(out["contexts"], ["sample.ts:1-3"])
        self.assertFalse(set(opened) & {"frobnicate_local.py", "frobnicate.dat", "frobnicate_dump.txt"})
        with mock.patch.object(context_broker, "_git_ignored", return_value=(set(), "")):
            out = select_context(self.TASK, self.root, max_repo_files=8)
        self.assertEqual(out["contexts"][:3], ["frobnicate.dat", "frobnicate_local.py", "cache/frobnicate_dump.txt"])
        # If git cannot say what is ignored, nothing is proposed by filename either.
        with mock.patch.object(context_broker, "_git_ignored", return_value=(None, "git timed out")):
            out, opened = self.opened_during(lambda: select_context(self.TASK, self.root))
        self.assertEqual(out["contexts"], [])
        self.assertNotIn("frobnicate.dat", opened)

    def skipped_paths(self, out):
        for row in out["skipped"]:
            self.assertTrue(row["reason"], row)
        return [row["path"] for row in out["skipped"]]

    def test_explicit_guarded_path_is_reported_as_skipped_and_never_read(self):
        # Which paths the shared sensitive-read policy names is that module's business; here it is a
        # stand-in, and the assertions are on the path being skipped unread, never on the label.
        (self.root / "vault").mkdir()
        (self.root / "vault" / "notes.md").write_text("frobnicate\n")
        (self.root / "conf").mkdir()
        (self.root / "conf" / "credentials.json").write_text("{}\n")
        (self.root / "ignored.py").write_text("frobnicate()\n")
        (self.root / "blob.bin").write_bytes(b"frobnicate\0\0")
        task = "Explain vault/notes.md, conf/credentials.json, ignored.py, blob.bin and sample.ts"
        with mock.patch.object(context_broker, "_git_ignored", return_value=({"ignored.py"}, "")), \
                mock.patch.object(context_broker, "sensitive_path", lambda value: "vault" in Path(value).parts):
            out, opened = self.opened_during(lambda: select_context(task, self.root))
        self.assertEqual(self.skipped_paths(out), ["vault/notes.md", "conf/credentials.json", "ignored.py", "blob.bin"])
        self.assertEqual(out["manifest"]["skipped"], out["skipped"])
        self.assertEqual(out["contexts"], ["sample.ts"])
        self.assertIn("sample.ts", opened)
        self.assertFalse(set(opened) & {"notes.md", "credentials.json", "ignored.py"})
        run = subprocess.run([sys.executable, str(ROOT / "bin" / "tp-agent"), "context", "--task",
                              "Explain conf/credentials.json", "--workdir", str(self.root)], text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        answer = json.loads(run.stdout)
        self.assertEqual(self.skipped_paths(answer), ["conf/credentials.json"])
        self.assertEqual(answer["contexts"], [])

    def test_explicit_source_files_are_not_held_to_discovery_heuristics(self):
        named = ["src/sessions/store.py", "src/logs/parser.py", "src/transcripts/reader.py", "src/token.py",
                 "src/password_reset.py", "src/trace.log", "src/events.jsonl",
                 "src/model-store.json", ".github/workflow.yml"]
        for rel in named:
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text("plain = 1\n")
        out = select_context("Fix " + ", ".join(named), self.root, max_repo_files=len(named))
        self.assertEqual(out["contexts"], named)
        self.assertEqual(out["skipped"], [])
        self.assertEqual(set(context_broker.context_reasons(out["manifest"]).values()), {"explicit path in request"})

    def test_discovery_keeps_the_full_guard_for_the_same_files(self):
        hidden = ["src/sessions/store.py", "src/logs/parser.py", "src/transcripts/reader.py", "src/frobnicate_token.py",
                  "src/password_frobnicate.py", "src/frobnicate.local.py", "src/frobnicate.log", "src/frobnicate.jsonl",
                  "src/frobnicate-store.json", ".github/frobnicate.yml"]
        for rel in hidden:
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text("frobnicate = 1\n")
        out, opened = self.opened_during(lambda: select_context(self.TASK, self.root, max_repo_files=20))
        self.assertEqual(out["contexts"], ["sample.ts:1-3"])
        self.assertFalse(set(opened) & {Path(rel).name for rel in hidden})
        self.assertEqual(out["skipped"], [])

    def test_explicit_path_inside_git_metadata_is_refused(self):
        (self.root / ".git" / "hooks").mkdir(parents=True)
        (self.root / ".git" / "config").write_text("[remote]\n")
        (self.root / ".git" / "hooks" / "pre-commit.sh").write_text("true\n")
        (self.root / "sub").mkdir()
        (self.root / "sub" / ".git").write_text("gitdir: elsewhere\n")
        (self.root / "alias.txt").symlink_to(self.root / ".git" / "config")
        task = "Explain .git/config, .git/hooks/pre-commit.sh, sub/.git, alias.txt and sample.ts"
        with mock.patch.object(context_broker, "_git_ignored", return_value=(set(), "")):
            out, opened = self.opened_during(lambda: select_context(task, self.root))
        # Every refused name is reported, the link included: it is not followed to learn it is a duplicate.
        self.assertEqual(self.skipped_paths(out), [".git/config", ".git/hooks/pre-commit.sh", "sub/.git", "alias.txt"])
        self.assertEqual(out["contexts"], ["sample.ts"])
        self.assertFalse(set(opened) & {"config", "pre-commit.sh", ".git", "alias.txt"})

    def test_explicit_symlink_is_judged_by_its_target(self):
        (self.root / "conf").mkdir()
        (self.root / "conf" / "credentials.txt").write_text("frobnicate\n")
        (self.root / "alias.txt").symlink_to(self.root / "conf" / "credentials.txt")
        (self.outside / "escape.ts").write_text("frobnicate()\n")
        (self.root / "link.ts").symlink_to(self.outside / "escape.ts")
        out, opened = self.opened_during(lambda: select_context("Explain alias.txt, link.ts and sample.ts", self.root))
        self.assertEqual(out["contexts"], ["sample.ts"])
        self.assertFalse(set(opened) & {"alias.txt", "credentials.txt", "link.ts", "escape.ts"})
        self.assertNotIn("escape", json.dumps(out))

    def test_git_unavailable_lets_explicit_paths_through_and_says_why(self):
        (self.root / "src").mkdir()
        (self.root / "src" / "token.py").write_text("plain = 1\n")
        (self.root / "conf").mkdir()
        (self.root / "conf" / "credentials.json").write_text("{}\n")
        (self.root / "frobnicate_helper.py").write_text("frobnicate()\n")
        task = "Explain how frobnicate uses src/token.py and conf/credentials.json"
        with mock.patch.object(context_broker, "_git_ignored", return_value=(None, "git timed out")):
            out, opened = self.opened_during(lambda: select_context(task, self.root))
        # Discovery stays off (fail closed); the named file is judged on the remaining checks.
        self.assertEqual(out["contexts"], ["src/token.py"])
        self.assertEqual(self.skipped_paths(out), [".", "conf/credentials.json"])
        self.assertIn("git timed out", out["skipped"][0]["reason"])
        self.assertIn("git timed out", out["note"])
        self.assertIn("git ignore status unknown", context_broker.context_reasons(out["manifest"])["src/token.py"])
        self.assertFalse(set(opened) & {"credentials.json", "frobnicate_helper.py", "sample.ts"})
        with mock.patch.object(context_broker, "_git_ignored", side_effect=RuntimeError("boom")):
            out = select_context(task, self.root)
        self.assertEqual(out["contexts"], ["src/token.py"])
        self.assertIn("RuntimeError", out["skipped"][0]["reason"])

    def fake_git(self, body):
        fake = self.root.parent / "fake-bin"
        fake.mkdir(exist_ok=True)
        (fake / "git").write_text("#!/bin/sh\n" + body)
        (fake / "git").chmod(0o755)
        return mock.patch.dict(os.environ, {"PATH": str(fake) + os.pathsep + os.environ["PATH"], "LC_ALL": "de_DE.UTF-8"})

    def test_git_runs_in_the_c_locale_and_reports_why_it_failed(self):
        # A translated git prints another sentence outside a repository; LC_ALL=C keeps the known one.
        outside_repo = ('if [ "$LC_ALL" = C ]; then echo "fatal: not a git repository" >&2; '
                        'else echo "Schwerwiegend: kein Git-Repository" >&2; fi; exit 128\n')
        with self.fake_git(outside_repo):
            self.assertEqual(context_broker._git_ignored(self.root), (set(), ""))
            self.assertEqual(select_context(self.TASK, self.root)["contexts"], ["sample.ts:1-3"])
        with self.fake_git('echo "fatal: detected dubious ownership in repository" >&2\nexit 128\n'):
            ignored, why = context_broker._git_ignored(self.root)
            self.assertIsNone(ignored)
            self.assertIn("dubious ownership", why)
            run = subprocess.run([sys.executable, str(ROOT / "bin" / "tp-agent"), "context", "--task",
                                  "Explain how frobnicate uses sample.ts", "--workdir", str(self.root)],
                                 text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        answer = json.loads(run.stdout)
        self.assertEqual(answer["contexts"], ["sample.ts"])
        self.assertEqual(self.skipped_paths(answer), ["."])
        self.assertIn("dubious ownership", answer["skipped"][0]["reason"])
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 10)):
            self.assertEqual(context_broker._git_ignored(self.root), (None, "git timed out"))

    def trusted_paths(self, out):
        return [s["path"] for s in out["manifest"]["sources"] if s["trust"] == "trusted_operational_instruction"]

    def test_symlinked_root_agents_file_leaving_workdir_is_refused_unread(self):
        (self.outside / "rules.md").write_text("external instructions\n")
        (self.root / "AGENTS.md").symlink_to(self.outside / "rules.md")
        with mock.patch.object(context_broker, "_git_ignored", return_value=(set(), "")):
            out, opened = self.opened_during(lambda: select_context("Explain sample.ts", self.root))
        self.assertEqual(out["contexts"], ["sample.ts"])
        self.assertEqual(self.trusted_paths(out), [])
        self.assertEqual(out["skipped"], [{"path": "AGENTS.md", "reason": "outside workdir"}])
        self.assertEqual(out["manifest"]["skipped"], out["skipped"])
        self.assertFalse(set(opened) & {"AGENTS.md", "rules.md"})
        self.assertNotIn("rules.md", json.dumps(out))
        self.assertNotIn(str(self.outside), json.dumps(out))

    def test_symlinked_nested_agents_file_leaving_workdir_is_refused_unread(self):
        (self.outside / "rules.md").write_text("external instructions\n")
        (self.root / "sub").mkdir()
        (self.root / "sub" / "mod.py").write_text("x = 1\n")
        (self.root / "sub" / "AGENTS.md").symlink_to(self.outside / "rules.md")
        with mock.patch.object(context_broker, "_git_ignored", return_value=(set(), "")):
            out, opened = self.opened_during(lambda: select_context("Explain sub/mod.py", self.root))
        self.assertEqual(out["contexts"], ["sub/mod.py"])
        self.assertEqual(self.trusted_paths(out), [])
        self.assertEqual(out["skipped"], [{"path": "sub/AGENTS.md", "reason": "outside workdir"}])
        self.assertFalse(set(opened) & {"AGENTS.md", "rules.md"})
        self.assertNotIn(str(self.outside), json.dumps(out))

    def test_symlinked_agents_file_is_judged_by_its_target_inside_the_workdir(self):
        (self.root / "conf").mkdir()
        (self.root / "conf" / "credentials.txt").write_text("x\n")
        (self.root / "AGENTS.md").symlink_to(self.root / "conf" / "credentials.txt")
        with mock.patch.object(context_broker, "_git_ignored", return_value=(set(), "")):
            out, opened = self.opened_during(lambda: select_context("Explain sample.ts", self.root))
        self.assertEqual(out["contexts"], ["sample.ts"])
        self.assertEqual(self.skipped_paths(out), ["AGENTS.md"])
        self.assertFalse(set(opened) & {"AGENTS.md", "credentials.txt"})

    def test_git_ignored_agents_files_are_refused_unread(self):
        (self.root / "AGENTS.md").write_text("local only\n")
        (self.root / "sub").mkdir()
        (self.root / "sub" / "mod.py").write_text("x = 1\n")
        (self.root / "sub" / "AGENTS.md").write_text("local only\n")
        task = "Explain sub/mod.py"
        with mock.patch.object(context_broker, "_git_ignored", return_value=({"AGENTS.md", "sub/AGENTS.md"}, "")):
            out, opened = self.opened_during(lambda: select_context(task, self.root))
        self.assertEqual(out["contexts"], ["sub/mod.py"])
        self.assertEqual(self.trusted_paths(out), [])
        self.assertEqual(out["skipped"], [{"path": "AGENTS.md", "reason": "git-ignored"},
                                          {"path": "sub/AGENTS.md", "reason": "git-ignored"}])
        self.assertNotIn("AGENTS.md", opened)
        # Named in the task it is still refused, and reported once.
        with mock.patch.object(context_broker, "_git_ignored", return_value=({"AGENTS.md"}, "")):
            out = select_context("Explain AGENTS.md and sample.ts", self.root)
        self.assertEqual(out["contexts"], ["sample.ts"])
        self.assertEqual(out["skipped"], [{"path": "AGENTS.md", "reason": "git-ignored"}])
        if subprocess.run(["git", "init", "-q", str(self.root)], capture_output=True).returncode == 0:
            (self.root / ".gitignore").write_text("AGENTS.md\n")
            out = select_context(task, self.root)
            self.assertEqual(out["contexts"], ["sub/mod.py"])
            self.assertEqual(self.skipped_paths(out), ["AGENTS.md", "sub/AGENTS.md"])

    def git_ignoring(self, *patterns):
        """Make the workdir a real git repository that ignores patterns. The rules live in
        .git/info/exclude, which no selection route walks, so they cannot match a task term."""
        if subprocess.run(["git", "init", "-q", str(self.root)], capture_output=True).returncode:
            self.skipTest("git is not available")
        (self.root / ".git" / "info").mkdir(exist_ok=True)
        (self.root / ".git" / "info" / "exclude").write_text("".join(p + "\n" for p in patterns))
        ignored, why = context_broker._git_ignored(self.root)
        self.assertEqual((ignored, why), (set(patterns), ""))
        return ignored

    def test_git_ignored_root_agents_symlink_to_in_tree_file_is_refused(self):
        (self.root / "rules.md").write_text("hello instructions\n")
        (self.root / "AGENTS.md").symlink_to("rules.md")
        self.git_ignoring("AGENTS.md")
        out = select_context("Explain sample.ts", self.root)
        self.assertEqual(out["contexts"], ["sample.ts"])
        self.assertEqual(self.trusted_paths(out), [])
        self.assertEqual(out["skipped"], [{"path": "AGENTS.md", "reason": "git-ignored"}])
        self.assertEqual(out["manifest"]["skipped"], out["skipped"])
        self.assertNotIn("rules.md", json.dumps(out))

    def test_git_ignored_nested_agents_symlink_to_in_tree_file_is_refused(self):
        (self.root / "sub").mkdir()
        (self.root / "sub" / "mod.py").write_text("x = 1\n")
        (self.root / "sub" / "rules.md").write_text("hello instructions\n")
        (self.root / "sub" / "AGENTS.md").symlink_to("rules.md")
        self.git_ignoring("sub/AGENTS.md")
        out = select_context("Explain sub/mod.py", self.root)
        self.assertEqual(out["contexts"], ["sub/mod.py"])
        self.assertEqual(self.trusted_paths(out), [])
        self.assertEqual(out["skipped"], [{"path": "sub/AGENTS.md", "reason": "git-ignored"}])
        self.assertNotIn("rules.md", json.dumps(out))

    def test_agents_symlink_through_a_git_ignored_link_is_refused(self):
        # The name and the final target are both tracked; the link between them is local only.
        (self.root / "rules.md").write_text("hello instructions\n")
        (self.root / "mid.md").symlink_to("rules.md")
        (self.root / "AGENTS.md").symlink_to("mid.md")
        self.git_ignoring("mid.md")
        out = select_context("Explain sample.ts", self.root)
        self.assertEqual(out["contexts"], ["sample.ts"])
        self.assertEqual(self.trusted_paths(out), [])
        self.assertEqual(out["skipped"], [{"path": "AGENTS.md", "reason": "git-ignored"}])

    def test_explicit_git_ignored_symlink_to_in_tree_file_is_refused(self):
        (self.root / "notes.md").write_text("plain notes\n")
        (self.root / "alias.md").symlink_to("notes.md")
        (self.root / "real").mkdir()
        (self.root / "real" / "manual.md").write_text("plain manual\n")
        (self.root / "view").symlink_to("real", target_is_directory=True)
        ignored = self.git_ignoring("alias.md", "view")
        out = select_context("Explain alias.md and sample.ts", self.root)
        self.assertEqual(out["contexts"], ["sample.ts"])
        self.assertEqual(out["skipped"], [{"path": "alias.md", "reason": "git-ignored"}])
        self.assertEqual(out["manifest"]["skipped"], out["skipped"])
        self.assertNotIn("notes.md", json.dumps(out))
        # The tracked file is still usable under its own name, before or after the refused link.
        for task in ("Explain alias.md, notes.md and sample.ts", "Explain notes.md, alias.md and sample.ts"):
            out = select_context(task, self.root)
            self.assertEqual(out["contexts"], ["notes.md", "sample.ts"])
            self.assertEqual(out["skipped"], [{"path": "alias.md", "reason": "git-ignored"}])
        # A file reached through an ignored directory link is refused on every route.
        for walk in (False, True):
            self.assertEqual(context_broker._guard_reason(self.root, self.root / "view" / "manual.md", ignored, walk=walk),
                             "git-ignored")
            self.assertEqual(context_broker._guard_reason(self.root, self.root / "real" / "manual.md", ignored, walk=walk), "")

    def test_discovery_does_not_follow_git_ignored_symlink_to_in_tree_file(self):
        (self.root / "impl.py").write_text("x = 1\n")
        (self.root / "frobnicate_link.py").symlink_to("impl.py")
        ignored = self.git_ignoring("frobnicate_link.py")
        # The walk guard both discovery routes share refuses the link; the tracked file still passes.
        self.assertIsNone(context_broker._content_candidate(self.root, self.root / "frobnicate_link.py", ignored))
        self.assertEqual(context_broker._content_candidate(self.root, self.root / "impl.py", ignored), self.root / "impl.py")
        out = select_context(self.TASK, self.root)
        self.assertEqual(out["contexts"], ["sample.ts:1-3"])
        self.assertEqual(out["skipped"], [])
        self.assertNotIn("impl.py", json.dumps(out))

    # The guard's invariant: every filesystem object consulted on the way to a file passes the
    # guard, and the final real path is inside the workdir. One test per shape a path can take.

    def link_then_dotdot(self):
        """deep/rules.md is an ordinary file; middle is a directory link beside it, so
        middle/../rules.md is deep/rules.md and never the workdir's own rules.md."""
        (self.root / "deep" / "branch").mkdir(parents=True)
        (self.root / "deep" / "rules.md").write_text("hello instructions\n")
        (self.root / "middle").symlink_to("deep/branch", target_is_directory=True)

    def test_link_followed_by_dotdot_is_judged_on_the_link(self):
        self.link_then_dotdot()
        (self.root / "AGENTS.md").symlink_to("middle/../rules.md")
        (self.root / "alias.md").symlink_to("middle/../rules.md")
        ignored = self.git_ignoring("middle")
        refused = {"path": "AGENTS.md", "reason": "git-ignored"}
        out, opened = self.opened_during(lambda: select_context("Explain sample.ts", self.root))
        self.assertEqual(out["contexts"], ["sample.ts"])
        self.assertEqual(self.trusted_paths(out), [])
        self.assertEqual(out["skipped"], [refused])
        self.assertEqual(out["manifest"]["skipped"], out["skipped"])
        self.assertNotIn("AGENTS.md", opened)
        # Named in the task, as a link or spelled out, the route is refused all the same. One
        # slot, so deep/rules.md cannot come back as an ordinary filename match on "rules".
        out = select_context("Explain alias.md, middle/../rules.md and sample.ts", self.root, max_repo_files=1)
        self.assertEqual(out["contexts"], ["sample.ts"])
        self.assertEqual(out["skipped"], [{"path": "alias.md", "reason": "git-ignored"},
                                          {"path": "middle/../rules.md", "reason": "git-ignored"}, refused])
        for walk in (False, True):
            self.assertEqual(context_broker._guard_reason(self.root, self.root / "AGENTS.md", ignored, walk=walk),
                             "git-ignored")
            self.assertEqual(context_broker._guard_reason(self.root, self.root / "deep" / "rules.md", ignored, walk=walk), "")

    def test_named_path_through_a_link_and_dotdot_is_the_file_the_kernel_opens(self):
        self.link_then_dotdot()
        (self.root / "rules.md").write_text("decoy\n")
        with mock.patch.object(context_broker, "_git_ignored", return_value=(set(), "")):
            out = select_context("Explain middle/../rules.md", self.root, max_repo_files=1)
        self.assertEqual(out["contexts"], ["deep/rules.md"])
        self.assertEqual(context_broker.context_reasons(out["manifest"]), {"deep/rules.md": "explicit path in request"})
        self.assertEqual(out["skipped"], [])

    def test_dotdot_followed_by_a_link_is_judged_on_the_link(self):
        self.link_then_dotdot()
        (self.root / "view").symlink_to("deep", target_is_directory=True)
        (self.root / "AGENTS.md").symlink_to("deep/../view/rules.md")
        self.git_ignoring("view")
        out = select_context("Explain deep/../view/rules.md and sample.ts", self.root, max_repo_files=1)
        self.assertEqual(out["contexts"], ["sample.ts"])
        self.assertEqual(self.trusted_paths(out), [])
        self.assertEqual(out["skipped"], [{"path": "deep/../view/rules.md", "reason": "git-ignored"},
                                          {"path": "AGENTS.md", "reason": "git-ignored"}])

    def test_link_to_link_is_judged_on_every_link(self):
        (self.root / "notes.md").write_text("plain notes\n")
        (self.root / "hop.md").symlink_to("notes.md")
        (self.root / "first.md").symlink_to("hop.md")
        (self.root / "local.md").symlink_to("notes.md")
        (self.root / "second.md").symlink_to("local.md")
        self.git_ignoring("local.md")
        out = select_context("Explain second.md and first.md", self.root)
        self.assertEqual(out["contexts"], ["notes.md"])
        self.assertEqual(out["skipped"], [{"path": "second.md", "reason": "git-ignored"}])

    def test_link_loop_is_refused_and_reported(self):
        (self.root / "loop.md").symlink_to("pool.md")
        (self.root / "pool.md").symlink_to("loop.md")
        (self.root / "AGENTS.md").symlink_to("loop.md")
        with mock.patch.object(context_broker, "_git_ignored", return_value=(set(), "")):
            out = select_context("Explain loop.md and sample.ts", self.root)
        self.assertEqual(out["contexts"], ["sample.ts"])
        self.assertEqual(out["skipped"], [{"path": "loop.md", "reason": "too many symbolic links"},
                                          {"path": "AGENTS.md", "reason": "too many symbolic links"}])

    def test_absolute_link_target_is_walked_like_any_other(self):
        self.link_then_dotdot()
        (self.root / "notes.md").write_text("plain notes\n")
        (self.root / "abs.md").symlink_to(self.root / "notes.md")
        (self.root / "AGENTS.md").symlink_to(str(self.root) + "/middle/../rules.md")
        self.git_ignoring("middle")
        out = select_context("Explain abs.md", self.root)
        self.assertEqual(out["contexts"], ["notes.md"])
        self.assertEqual(self.trusted_paths(out), [])
        self.assertEqual(out["skipped"], [{"path": "AGENTS.md", "reason": "git-ignored"}])

    def test_link_climbing_out_and_back_in_may_only_pass_the_workdir_itself(self):
        (self.root / "notes.md").write_text("plain notes\n")
        (self.root / "sub").mkdir()
        (self.root / "sub" / "own.md").symlink_to("../../work/notes.md")
        # A link outside the workdir is something git cannot vouch for, even if it leads back in.
        (self.outside / "back").symlink_to(self.root, target_is_directory=True)
        (self.root / "via.md").symlink_to("../outside/back/notes.md")
        (self.root / "AGENTS.md").symlink_to("../outside/back/notes.md")
        with mock.patch.object(context_broker, "_git_ignored", return_value=(set(), "")):
            out = select_context("Explain sub/own.md and via.md", self.root)
        self.assertEqual(out["contexts"], ["notes.md"])
        self.assertEqual(self.trusted_paths(out), [])
        self.assertEqual(out["skipped"], [{"path": "via.md", "reason": "outside workdir"},
                                          {"path": "AGENTS.md", "reason": "outside workdir"}])

    def test_directory_link_in_the_middle_is_judged_with_its_target(self):
        (self.root / "real").mkdir()
        (self.root / "real" / "manual.md").write_text("plain manual\n")
        (self.root / "view").symlink_to("real", target_is_directory=True)
        (self.root / "cache").mkdir()
        (self.root / "cache" / "dump.md").write_text("local only\n")
        (self.root / "peek").symlink_to("cache", target_is_directory=True)
        self.git_ignoring("cache")
        out, opened = self.opened_during(lambda: select_context("Explain view/manual.md and peek/dump.md", self.root))
        self.assertEqual(out["contexts"], ["real/manual.md"])
        self.assertEqual(out["skipped"], [{"path": "peek/dump.md", "reason": "git-ignored"}])
        self.assertNotIn("dump.md", opened)

    def test_broken_link_is_refused_and_reported(self):
        (self.root / "broken.md").symlink_to("missing.md")
        (self.root / "AGENTS.md").symlink_to("gone/rules.md")
        with mock.patch.object(context_broker, "_git_ignored", return_value=(set(), "")):
            out = select_context("Explain broken.md, absent.md and sample.ts", self.root)
        self.assertEqual(out["contexts"], ["sample.ts"])
        # A name that was never there is not a refusal; a link that leads nowhere is.
        self.assertEqual(out["skipped"], [{"path": "broken.md", "reason": "broken symbolic link"},
                                          {"path": "AGENTS.md", "reason": "broken symbolic link"}])

    def test_case_only_difference_does_not_slip_past_the_ignore_list(self):
        if not (self.root / "SAMPLE.TS").exists():
            self.skipTest("the filesystem is case-sensitive")
        (self.root / "Notes.md").write_text("local only\n")
        (self.root / "alias.md").symlink_to("NOTES.MD")
        (self.root / "agents.md").write_text("local only\n")
        self.git_ignoring("Notes.md", "agents.md")
        task = "Explain notes.md, alias.md and sample.ts"
        out, opened = self.opened_during(lambda: select_context(task, self.root))
        self.assertEqual(out["contexts"], ["sample.ts"])
        self.assertEqual(self.trusted_paths(out), [])
        self.assertEqual(out["skipped"], [{"path": "notes.md", "reason": "git-ignored"},
                                          {"path": "alias.md", "reason": "git-ignored"},
                                          {"path": "AGENTS.md", "reason": "git-ignored"}])
        self.assertFalse({name.lower() for name in opened} & {"notes.md", "alias.md", "agents.md"})
        # Not ignored, the same file is found and reported under the name the directory gives it.
        with mock.patch.object(context_broker, "_git_ignored", return_value=(set(), "")):
            out = select_context("Explain alias.md", self.root)
        self.assertIn("Notes.md", out["contexts"])

    def test_trailing_slashes_and_dot_components_change_nothing(self):
        self.link_then_dotdot()
        (self.root / "AGENTS.md").symlink_to("./middle/.//../rules.md")
        (self.root / "slash").symlink_to("deep/", target_is_directory=True)
        self.git_ignoring("middle")
        out = select_context("Explain ./deep/./rules.md and slash/rules.md and sample.ts", self.root)
        self.assertEqual(out["contexts"], ["deep/rules.md", "sample.ts"])
        self.assertEqual(self.trusted_paths(out), [])
        self.assertEqual(out["skipped"], [{"path": "AGENTS.md", "reason": "git-ignored"}])

    def test_trailing_slash_on_a_file_is_refused_and_never_crashes_selection(self):
        (self.root / "rules.md").write_text("hello instructions\n")
        (self.root / "AGENTS.md").symlink_to("rules.md/")
        (self.root / "alias.md").symlink_to("rules.md/.")
        (self.root / "up.md").symlink_to("rules.md/..")
        (self.root / "good").symlink_to("sub/", target_is_directory=True)
        (self.root / "sub").mkdir()
        (self.root / "sub" / "ok.md").write_text("fine\n")
        with mock.patch.object(context_broker, "_git_ignored", return_value=(set(), "")):
            for name in ("AGENTS.md", "alias.md", "up.md"):
                for walk in (False, True):
                    self.assertEqual(context_broker._guard_reason(self.root, self.root / name, (), walk=walk),
                                     "not a directory", name)
            self.assertEqual(context_broker._guard_reason(self.root, self.root / "good" / "ok.md", ()), "")
            out = select_context("Explain alias.md and sample.ts", self.root)
        self.assertEqual(out["contexts"], ["sample.ts"])
        self.assertEqual(self.trusted_paths(out), [])
        self.assertEqual(out["skipped"], [{"path": "alias.md", "reason": "not a directory"},
                                          {"path": "AGENTS.md", "reason": "not a directory"}])

    def test_an_oserror_while_reading_an_attached_file_is_a_refusal_not_a_crash(self):
        (self.root / "AGENTS.md").write_text("# Instructions\n")
        real = context_broker._repo_entry

        def boom(workdir, path, *args, **kwargs):
            if Path(path).name == "AGENTS.md":
                raise NotADirectoryError(20, "Not a directory")
            return real(workdir, path, *args, **kwargs)

        with mock.patch.object(context_broker, "_git_ignored", return_value=(set(), "")), \
                mock.patch.object(context_broker, "_repo_entry", boom):
            out = select_context("Explain sample.ts", self.root)
        self.assertEqual(out["contexts"], ["sample.ts"])
        self.assertEqual(self.trusted_paths(out), [])
        self.assertEqual(out["skipped"], [{"path": "AGENTS.md", "reason": "path could not be checked"}])

    def test_guarded_directory_name_is_judged_even_when_dotdot_cancels_it(self):
        (self.root / "credentials").mkdir()
        (self.root / "notes.md").write_text("plain notes\n")
        (self.root / "AGENTS.md").symlink_to("credentials/../notes.md")
        with mock.patch.object(context_broker, "_git_ignored", return_value=(set(), "")):
            for walk in (False, True):
                self.assertEqual(context_broker._guard_reason(self.root, self.root / "credentials" / ".." / "notes.md",
                                                              (), walk=walk), "credentials/secret filename")
            out, opened = self.opened_during(lambda: select_context("Explain credentials/../notes.md and sample.ts", self.root, max_repo_files=1))
        self.assertEqual(out["contexts"], ["sample.ts"])
        self.assertEqual(self.trusted_paths(out), [])
        self.assertEqual(out["skipped"], [{"path": "credentials/../notes.md", "reason": "credentials/secret filename"},
                                          {"path": "AGENTS.md", "reason": "credentials/secret filename"}])
        self.assertNotIn("notes.md", opened)

    def test_explicit_symlink_leaving_workdir_is_reported_without_its_target(self):
        (self.outside / "data.md").write_text("external data\n")
        (self.root / "alias.md").symlink_to(self.outside / "data.md")
        (self.root / "sub").mkdir()
        (self.root / "sub" / "rel.md").symlink_to("../../outside/data.md")
        task = "Explain alias.md, sub/rel.md and sample.ts"
        with mock.patch.object(context_broker, "_git_ignored", return_value=(set(), "")):
            out, opened = self.opened_during(lambda: select_context(task, self.root))
        self.assertEqual(out["contexts"], ["sample.ts"])
        self.assertEqual(out["skipped"], [{"path": "alias.md", "reason": "outside workdir"},
                                          {"path": "sub/rel.md", "reason": "outside workdir"}])
        self.assertEqual(out["manifest"]["skipped"], out["skipped"])
        self.assertFalse(set(opened) & {"alias.md", "rel.md", "data.md"})
        self.assertNotIn("data.md", json.dumps(out))
        self.assertNotIn(str(self.outside), json.dumps(out))

    BAD_NAMES = {"cache", "local.md", ".git", "credentials.txt", "credentials"}

    def bad_name(self, part):
        return part.startswith("ign_") or part in self.BAD_NAMES

    @staticmethod
    def components(text):
        """A trailing slash means "." : the path must still be a directory there."""
        parts = [part for part in text.split("/") if part]
        return parts + ["."] if text.endswith("/") else parts

    def kernel_route(self, path):
        """Every directory entry looked up while path is followed, and where it ends; None
        when it leads nowhere. Deliberately plain: this is the test's own account of the walk."""
        cur, seen, hops = Path("/"), [], 0
        pending = self.components(str(path))
        while pending:
            part = pending.pop(0)
            if part in (".", ".."):
                if not cur.is_dir():
                    return None
                cur = cur.parent if part == ".." else cur
                continue
            entry = cur / part
            seen.append(entry)
            if entry.is_symlink():
                hops += 1
                if hops > 40:
                    return None
                target = os.readlink(entry)
                if target.startswith("/"):
                    cur = Path("/")
                pending = self.components(target) + pending
            elif entry.exists():
                cur = entry
            else:
                return None
        return seen, cur

    def clean_route(self, path):
        """True when nothing on the way to path is outside the workdir (its own ancestors
        apart) or carries an ignored or guarded name, and the path ends inside the workdir."""
        route = self.kernel_route(path)
        if route is None:
            return False
        seen, real = route
        self.assertEqual(str(real), os.path.realpath(path))
        for entry in seen:
            if entry == self.root or entry in self.root.parents:
                continue
            if self.root not in entry.parents or any(self.bad_name(part) for part in entry.relative_to(self.root).parts):
                return False
        return real == self.root or self.root in real.parents

    def test_random_link_chains_never_reach_outside_ignored_or_guarded_names(self):
        if subprocess.run(["git", "init", "-q", str(self.root)], capture_output=True).returncode:
            self.skipTest("git is not available")
        root = self.root
        for rel in ("notes.md", "pub/a.md", "pub/deep/b.md", "cache/c.md", "local.md", "conf/credentials.txt",
                    "credentials/d.md"):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text("plain\n")
        (self.outside / "escape.md").write_text("external data\n")
        (self.outside / "back").symlink_to(root, target_is_directory=True)
        (root / ".git" / "info").mkdir(exist_ok=True)
        (root / ".git" / "info" / "exclude").write_text("ign_*\ncache\nlocal.md\n")
        fixed = ["notes.md", "pub/a.md", "pub/deep/b.md", "pub/deep/../a.md", "./pub//deep/./b.md", "pub", "pub/deep/",
                 "cache", "cache/c.md", "cache/../notes.md", "local.md", "conf/credentials.txt", ".git/config",
                 "credentials", "credentials/", "credentials/../notes.md", "credentials/d.md", "notes.md/", "notes.md/.",
                 "pub/a.md/", "pub/a.md/..", "pub/deep/../",
                 ".git/../notes.md", "../work/pub/a.md", "../outside/back/notes.md", "../outside/escape.md",
                 "missing.md", str(root / "notes.md"), str(root) + "/pub/deep/../../notes.md",
                 str(self.outside / "escape.md"), str(self.outside / "back" / "pub" / "a.md")]
        chained = ["{}", "{}/a.md", "{}/../notes.md", "{}/deep/b.md", "./{}/", "{}/../pub/a.md", "{}/..", "{}/", "{}/."]
        soup = ["pub", "deep", "conf", "cache", "credentials", ".git", "..", "..", ".", "notes.md", "a.md", "b.md", "c.md",
                "local.md", "credentials.txt", ""]
        rng = random.Random(20261006)
        links = []
        accepted = refused = attached = 0
        for _round in range(16):
            fresh = []
            for _ in range(6):
                name = rng.choice(["l{}.md", "l{}.md", "d{}.d", "d{}.d", "ign_l{}.md", "ign_d{}.d", "pub/p{}.md"]).format(len(links))
                roll = rng.random()
                if roll < 0.4:
                    target = rng.choice(fixed)
                elif roll < 0.75 and links:
                    target = rng.choice(chained).format(rng.choice(links))
                else:
                    target = "/".join(rng.choice(soup) for _ in range(rng.randint(1, 5))) or "."
                if name.startswith("pub/") and not target.startswith("/"):
                    target = "../" + target
                (root / name).symlink_to(target)
                links.append(name)
                fresh.append(name)
            if os.path.lexists(root / "AGENTS.md"):
                (root / "AGENTS.md").unlink()
            (root / "AGENTS.md").symlink_to(rng.choice(fixed + links))
            names = links + [form.format(link) for link in rng.sample(links, 4) for form in chained[1:]]
            ignored, why = context_broker._git_ignored(root)
            self.assertEqual(why, "")
            for name in names:
                for walk in (False, True):
                    try:
                        reason = context_broker._guard_reason(root, str(root) + "/" + name, ignored, walk=walk)
                    except OSError:
                        reason = "nothing there"
                    if reason:
                        refused += 1
                        continue
                    accepted += 1
                    self.assertTrue(self.clean_route(str(root) + "/" + name), name)
            asked = fresh + [form.format(link) for link in fresh for form in chained[1:4]]
            allowed = {str(Path(os.path.realpath(root / name)).relative_to(root))
                       for name in asked if self.clean_route(root / name) and (root / name).is_file()}
            out, opened = self.opened_during(lambda: select_context("Explain " + ", ".join(asked), root, max_repo_files=40),
                                             record=lambda path: os.path.realpath(path))
            for source in out["manifest"]["sources"]:
                if source["source"] != "repo":
                    continue
                parts = Path(source["path"]).parts
                self.assertFalse(Path(source["path"]).is_absolute() or ".." in parts, source["path"])
                self.assertFalse([part for part in parts if self.bad_name(part)], source["path"])
                self.assertTrue((root / source["path"]).is_file() and not (root / source["path"]).is_symlink(), source["path"])
                if source["reason_selected"].startswith("explicit path"):
                    self.assertIn(source["path"], allowed)
                    attached += 1
            if self.trusted_paths(out):
                self.assertTrue(self.clean_route(root / "AGENTS.md"), os.readlink(root / "AGENTS.md"))
            for real in opened:
                if self.outside.parent not in Path(real).parents:
                    # The broker's own configuration, not something the task or a link led to.
                    continue
                self.assertIn(root, Path(real).parents, real)
                self.assertFalse([part for part in Path(real).relative_to(root).parts if self.bad_name(part)], real)
            self.assertNotIn("escape", json.dumps(out))
            self.assertNotIn(str(self.outside), json.dumps(out))
        # The run has to exercise both answers, or the assertions above prove nothing.
        self.assertGreater(accepted, 40)
        self.assertGreater(refused, 40)
        self.assertGreater(attached, 5)

    def test_in_tree_agents_files_are_still_attached_and_trusted(self):
        (self.root / "AGENTS.md").write_text("# Instructions\nUse pytest.\n")
        (self.root / "sub").mkdir()
        (self.root / "sub" / "mod.py").write_text("x = 1\n")
        (self.root / "sub" / "AGENTS.md").write_text("# Sub\n")
        with mock.patch.object(context_broker, "_git_ignored", return_value=(set(), "")):
            out = select_context("Explain sub/mod.py", self.root)
        self.assertEqual(out["contexts"], ["AGENTS.md", "sub/AGENTS.md", "sub/mod.py"])
        self.assertEqual(self.trusted_paths(out), ["AGENTS.md", "sub/AGENTS.md"])
        self.assertEqual(out["skipped"], [])
        root_entry = out["manifest"]["sources"][0]
        self.assertEqual(root_entry["hash"], context_broker._hash("# Instructions\nUse pytest.\n"))

    def test_missing_git_disables_discovery_and_says_why(self):
        (self.root / ".gitignore").write_text("ignored/\n")
        (self.root / "ignored").mkdir()
        (self.root / "ignored" / "module.py").write_text("def pineapple_walrus():\n    return 1\n")
        (self.root / "ignored" / "pineapple_walrus.py").write_text("x = 1\n")
        (self.root / "AGENTS.md").write_text("# Instructions\n")
        empty = self.root.parent / "empty-bin"
        empty.mkdir()
        task = "Locate pineapple_walrus implementation"
        with mock.patch.dict(os.environ, {"PATH": str(empty)}):
            ignored, why = context_broker._git_ignored(self.root)
            self.assertIsNone(ignored)
            self.assertIn("not installed", why)
            out, opened = self.opened_during(lambda: select_context(task, self.root))
            named = select_context(task + " in sample.ts", self.root)
        self.assertEqual(out["contexts"], [])
        self.assertEqual(self.skipped_paths(out), [".", "AGENTS.md"])
        self.assertIn("automatic file discovery is off", out["skipped"][0]["reason"])
        self.assertIn("not installed", out["skipped"][0]["reason"])
        self.assertIn("not installed", out["note"])
        self.assertFalse(set(opened) & {"module.py", "pineapple_walrus.py", "AGENTS.md"})
        self.assertNotIn("ignored/", json.dumps(out))
        # A path the task names is still judged on the remaining guards.
        self.assertEqual(named["contexts"], ["sample.ts"])
        self.assertIn("git ignore status unknown", context_broker.context_reasons(named["manifest"])["sample.ts"])
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError("git")):
            self.assertEqual(context_broker._git_ignored(self.root), (None, "git is not installed or not on PATH"))

    def test_stopword_only_overlap_is_not_a_content_match(self):
        (self.root / "words.txt").write_text("does this drop from that when with into\n")
        task = "What does this drop from that when with into"
        self.assertEqual(context_broker._content_terms(task), {})
        self.assertEqual(context_broker._content_matches(self.root, task, [], 5), [])
        out = select_context(task + " quokka", self.root)
        self.assertEqual(out["contexts"], [])

    def test_identifier_term_outranks_plain_word_hits(self):
        (self.root / "aaa.py").write_text("def one():\n    return quokka(zebra)\n")
        (self.root / "zzz.py").write_text("def two():\n    return frob_nicate()\n")
        task = "Explain how quokka zebra relate to frob_nicate"
        self.assertEqual(context_broker._content_terms("quokka frob_nicate camelCase lib/thing.py PLAIN"),
                         {"quokka": 1, "frob-nicate": 3, "camelcase": 3, "lib/thing.py": 3, "thing": 1, "plain": 1})
        hits = context_broker._content_matches(self.root, task, [], 5)
        self.assertEqual([h["path"].name for h in hits], ["zzz.py", "aaa.py"])
        first = select_context(task, self.root, max_repo_files=1)
        self.assertEqual(first["contexts"], ["zzz.py:1-2"])
        self.assertEqual(first["contexts"], select_context(task, self.root, max_repo_files=1)["contexts"])


if __name__ == "__main__":
    unittest.main()
