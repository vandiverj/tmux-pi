"""bin/tp-docs as a subprocess: answers, exit codes, call logging, offline operation, and the separate fetch step."""
import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "localdocs"))
from project import FIXTURES, ROOT, ProjectCase  # noqa: E402

TP_DOCS = ROOT / "bin" / "tp-docs"

BLOCK_SOCKETS = '''
import os, socket
def _blocked(*a, **k):
    open(os.environ["BLOCK_MARK"], "a").write("blocked\\n")
    raise OSError("network blocked by the test")
for _name in ("connect", "connect_ex", "bind", "sendto"):
    setattr(socket.socket, _name, _blocked)
for _name in ("create_connection", "getaddrinfo", "gethostbyname"):
    setattr(socket, _name, _blocked)
open(os.environ["BLOCK_MARK"] + ".loaded", "w").write("1")
'''


class CliCase(ProjectCase):
    def run_cli(self, *args, env=None, task=True, cwd=None):
        full = {k: v for k, v in os.environ.items() if k not in ("TP_TASK_DIR", "TP_DOCS_PROJECT", "DENO_DIR")}
        if task:
            full["TP_TASK_DIR"] = str(self.task)
        full.update(env or {})
        return subprocess.run([sys.executable, str(TP_DOCS), *args], capture_output=True, text=True, env=full, cwd=cwd or self.base, timeout=60)

    def cli(self, *args, **kw):
        return self.run_cli("--project", str(self.project), *args, **kw)

    def log_lines(self):
        path = self.task / "docs-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class SignatureCliTests(CliCase):
    def test_signature_prints_the_declaration_and_logs_one_line(self):
        run = self.cli("signature", "plainpkg", "connect")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("export declare function connect(url: string", run.stdout)
        lines = self.log_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual((lines[0]["tool"], lines[0]["result"], lines[0]["index_version"]), ("api_signature", "hit", "1.2.3"))

    def test_the_answer_starts_with_provenance_and_json_mode_carries_it_as_data(self):
        run = self.cli("signature", "plainpkg", "connect")
        self.assertRegex(run.stdout.split("\n")[0], r"^\[provenance\] source=node_modules:\S+ version=1\.2\.3 exact=yes retrieved=\d{4}-\d\d-\d\dT")
        reply = json.loads(self.cli("--json", "signature", "plainpkg", "connect").stdout)
        self.assertEqual(sorted(reply["provenance"]), ["exact", "retrieved", "source", "version"])
        self.assertEqual((reply["provenance"]["version"], reply["provenance"]["exact"]), ("1.2.3", True))
        self.assertEqual(self.log_lines()[-1]["provenance"], reply["provenance"])
        self.assertIsNone(json.loads(self.cli("--json", "signature", "plainpkg", "nope").stdout)["provenance"])

    def test_symbol_not_found_exits_1_and_logs_a_miss(self):
        run = self.cli("signature", "plainpkg", "nope")
        self.assertEqual(run.returncode, 1)
        self.assertIn("nope not found", run.stdout)
        self.assertEqual(self.log_lines()[0]["result"], "miss")

    def test_json_mode_reports_status_and_always_exits_0_for_an_answer(self):
        hit = json.loads(self.cli("--json", "signature", "plainpkg", "connect").stdout)
        self.assertEqual((hit["status"], hit["version"]), ("hit", "1.2.3"))
        run = self.cli("--json", "signature", "left-pad", "x")
        self.assertEqual(run.returncode, 0)
        miss = json.loads(run.stdout)
        self.assertEqual(miss["status"], "miss")
        self.assertIn("no pinned version", miss["text"])
        refused = json.loads(self.cli("--json", "signature", "../../etc/passwd", "x").stdout)
        self.assertEqual(refused["status"], "rejected")
        self.assertEqual(len(self.log_lines()), 3)

    def test_unset_task_dir_fails_closed_with_exit_3_and_no_answer(self):
        run = self.cli("signature", "plainpkg", "connect", task=False)
        self.assertEqual(run.returncode, 3)
        self.assertEqual(run.stdout, "")
        self.assertIn("TP_TASK_DIR", run.stderr)
        run = self.cli("--json", "search", "plainpkg", "timeout", task=False)
        self.assertEqual((run.returncode, run.stdout), (3, ""))

    def test_a_task_dir_that_does_not_exist_fails_closed(self):
        run = self.cli("signature", "plainpkg", "connect", env={"TP_TASK_DIR": str(self.base / "gone")})
        self.assertEqual((run.returncode, run.stdout), (3, ""))
        self.assertFalse((self.base / "gone").exists())

    def test_log_flag_names_the_file_when_there_is_no_task_dir(self):
        target = self.base / "calls.jsonl"
        run = self.cli("--log", str(target), "signature", "plainpkg", "connect", task=False)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(len(target.read_text().splitlines()), 1)

    def test_project_is_found_from_the_working_directory_and_from_the_environment(self):
        deep = self.project / "src" / "a"
        deep.mkdir(parents=True)
        run = self.run_cli("signature", "plainpkg", "connect", cwd=deep)
        self.assertEqual(run.returncode, 0, run.stderr)
        run = self.run_cli("signature", "plainpkg", "connect", env={"TP_DOCS_PROJECT": str(self.project)})
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_deno_cache_flag_serves_a_deno_locked_project(self):
        (self.project / "package-lock.json").unlink()
        (self.project / "deno.lock").write_text((FIXTURES / "locks" / "deno.lock").read_text())
        run = self.cli("--deno-dir", str(FIXTURES / "deno"), "signature", "npm:denopkg", "fromDeno")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("denopkg@1.2.0", run.stdout)

    def test_response_is_capped(self):
        (self.project / "node_modules" / "plainpkg" / "index.d.ts").write_text(
            "/** " + "long " * 4000 + "*/\nexport declare function huge(): void;\n")
        out = self.cli("signature", "plainpkg", "huge").stdout
        self.assertLessEqual(len(out.encode()), 8192 + 1)
        self.assertIn("[truncated:", out)


class OfflineCliTests(CliCase):
    def blocked_env(self):
        scripts = self.base / "blocker"
        scripts.mkdir()
        (scripts / "sitecustomize.py").write_text(BLOCK_SOCKETS)
        mark = self.base / "mark"
        return {"PYTHONPATH": str(scripts), "BLOCK_MARK": str(mark)}, mark

    def test_signature_and_search_work_with_every_socket_call_blocked(self):
        env, mark = self.blocked_env()
        (self.base / "docs").mkdir()
        (self.base / "docs" / "README.md").write_text("# plainpkg\n\n## Timeouts\n\nSet `timeout` to cap the wait.\n")
        self.assertEqual(self.cli("build", "plainpkg", "--from", str(self.base / "docs"), env=env).returncode, 0)
        sig = self.cli("signature", "plainpkg", "connect", env=env)
        search = self.cli("search", "plainpkg", "timeout", env=env)
        self.assertEqual((sig.returncode, search.returncode), (0, 0), (sig.stderr, search.stderr))
        self.assertIn("Timeouts", search.stdout)
        self.assertTrue(Path(str(mark) + ".loaded").exists(), "the socket blocker was not active, so this test proved nothing")
        self.assertFalse(mark.exists(), "a query tried to use the network")
        self.assertEqual(len(self.log_lines()), 2)

    def test_the_blocker_really_blocks_fetch_so_only_fetch_could_use_the_network(self):
        env, mark = self.blocked_env()
        run = self.cli("fetch", "plainpkg", "--registry", "http://127.0.0.1:9", env=env)
        self.assertEqual(run.returncode, 2)
        self.assertTrue(mark.exists())


class IndexCliTests(CliCase):
    def setUp(self):
        super().setUp()
        self.docs = self.base / "docs"
        self.docs.mkdir()
        (self.docs / "README.md").write_text("# plainpkg\n\n## Retries\n\nThe `retries` option controls how often to try again.\n")

    def test_search_without_an_index_says_to_build_one_and_builds_nothing(self):
        run = self.cli("search", "plainpkg", "retries")
        self.assertEqual(run.returncode, 1)
        self.assertIn("tp-docs build plainpkg", run.stdout)
        self.assertFalse((self.project / ".tmux-pi").exists())

    def test_build_search_status_refresh(self):
        built = self.cli("build", "plainpkg", "--from", str(self.docs))
        self.assertEqual(built.returncode, 0, built.stderr)
        self.assertEqual(json.loads(built.stdout)["version"], "1.2.3")
        found = self.cli("search", "plainpkg", "retries option")
        self.assertEqual(found.returncode, 0)
        self.assertIn("README.md:", found.stdout)
        status = json.loads(self.cli("status").stdout)
        self.assertEqual([(i["lib"], i["version"], i["lock_changed"]) for i in status["indexes"]], [("plainpkg", "1.2.3", False)])
        first = json.loads(self.cli("refresh").stdout)
        self.assertEqual([r["action"] for r in first], ["unchanged"])
        self.write_lock((self.project / "package-lock.json").read_text().replace('"node_modules/esmpkg": { "version": "2.0.0" }',
                                                                              '"node_modules/esmpkg": { "version": "2.0.1" }'))
        self.assertTrue(json.loads(self.cli("status").stdout)["indexes"][0]["lock_changed"])
        self.assertEqual([r["action"] for r in json.loads(self.cli("refresh").stdout)], ["rebuilt"])
        self.assertEqual([r["action"] for r in json.loads(self.cli("refresh").stdout)], ["unchanged"])

    def test_build_without_a_pin_is_refused(self):
        run = self.cli("build", "left-pad", "--from", str(self.docs))
        self.assertEqual(run.returncode, 2)
        self.assertIn("no pinned version", run.stderr)

    def test_search_reaches_a_hit_logged_with_the_index_version(self):
        self.cli("build", "plainpkg", "--from", str(self.docs))
        self.cli("search", "plainpkg", "retries")
        self.assertEqual((self.log_lines()[0]["tool"], self.log_lines()[0]["index_version"]), ("docs_search", "1.2.3"))


class FetchCliTests(CliCase):
    def serve(self, members, integrity=True):
        registry = self.base / "registry" / "plainpkg" / "-"
        registry.mkdir(parents=True)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as archive:
            for name, text in members.items():
                data = text.encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        data = buf.getvalue()
        (registry / "plainpkg-1.2.3.tgz").write_bytes(data)
        if integrity:
            lockdata = json.loads((self.project / "package-lock.json").read_text())
            lockdata["packages"]["node_modules/plainpkg"]["integrity"] = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
            self.write_lock(lockdata)
        return (self.base / "registry").as_uri()

    def test_fetch_then_build_then_search_from_a_local_registry(self):
        registry = self.serve({"package/README.md": "# plainpkg\n\n## Backoff\n\nUse `connectWithBackoff` for flaky links.\n",
                               "package/index.js": "x"})
        fetched = self.cli("fetch", "plainpkg", "--registry", registry)
        self.assertEqual(fetched.returncode, 0, fetched.stderr)
        self.assertEqual(json.loads(fetched.stdout)["files"], 1)
        self.assertEqual(self.cli("build", "plainpkg").returncode, 0)
        found = self.cli("search", "plainpkg", "flaky backoff")
        self.assertIn("connectWithBackoff", found.stdout)

    def test_a_tarball_that_fails_the_lockfile_integrity_is_refused_and_nothing_is_kept(self):
        registry = self.serve({"package/README.md": "# r"}, integrity=False)  # the fixture lock says sha512-AAAA
        run = self.cli("fetch", "plainpkg", "--registry", registry)
        self.assertEqual(run.returncode, 2)
        self.assertIn("integrity mismatch", run.stderr)
        self.assertFalse((self.project / ".tmux-pi" / "docs-src").exists())

    def test_fetch_without_a_pin_does_not_reach_for_the_network(self):
        run = self.cli("fetch", "left-pad", "--registry", (self.base / "registry").as_uri())
        self.assertEqual(run.returncode, 2)
        self.assertIn("no pinned version", run.stderr)


class UsageTests(CliCase):
    def test_flags_go_before_the_subcommand_and_a_missing_subcommand_is_a_usage_error(self):
        self.assertEqual(self.run_cli().returncode, 2)
        self.assertEqual(self.cli("signature", "plainpkg").returncode, 2)
        self.assertIn("usage", self.run_cli("--help").stdout.lower())


if __name__ == "__main__":
    unittest.main()
