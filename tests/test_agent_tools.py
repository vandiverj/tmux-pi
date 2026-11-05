"""tools/agent/*.sh against throwaway git repositories, with stub tools on PATH. No network; no real prettier, eslint, jest, tsc, deno or gitleaks."""
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools" / "agent"
LAST = re.compile(r"^exit=(\d+) log=(\S+)$")


class ScriptCase(unittest.TestCase):
    """A temp dir with a stub bin directory, a scratch TMPDIR and an empty HOME, so nothing of the machine leaks in."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.bin = self.tmp / "bin"
        self.calls = self.tmp / "calls"
        self.scratch = self.tmp / "tmpdir"
        self.home = self.tmp / "home"
        for d in (self.bin, self.calls, self.scratch, self.home):
            d.mkdir()
        os.symlink(shutil.which("git"), self.bin / "git")
        self.env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home), "TMPDIR": str(self.scratch),
                    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "LC_ALL": "C.UTF-8"}

    # --- fixtures
    def stub(self, name, exit_code=0, output="", where=None, fail_on_call=None):
        """An executable that appends its cwd, TZ and arguments to calls/<name>, prints `output`, exits `exit_code`
        (`fail_on_call` N: only the Nth call exits 1)."""
        directory = where or self.bin
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        code = f'[ "$n" = "{fail_on_call}" ] && exit 1; exit 0' if fail_on_call else f"exit {exit_code}"
        path.write_text(
            "#!/bin/sh\n"
            f'log="{self.calls}/{name}"\n'
            'n=$(($(cat "$log.count" 2>/dev/null || echo 0) + 1)); echo "$n" > "$log.count"\n'
            '{ echo "--- call $n cwd=$PWD TZ=${TZ-unset}"; for a in "$@"; do echo "arg: $a"; done; } >> "$log"\n'
            f"cat <<'OUT'\n{output}\nOUT\n"
            f"{code}\n")
        path.chmod(0o755)
        return path

    def calls_of(self, name):
        path = self.calls / name
        return path.read_text() if path.exists() else ""

    def git(self, cwd, *args):
        env = dict(self.env, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
        return subprocess.run(["git", "-C", str(cwd), *args], env=env, capture_output=True, text=True, check=True).stdout.strip()

    def repo(self, name="repo", files=None):
        """A repository on branch main with one commit, then branch `lane` checked out. Returns its path."""
        path = self.tmp / name
        path.mkdir()
        self.git(path, "init", "-q", "-b", "main")
        self.write(path, files or {"README.md": "hello\n"})
        self.git(path, "add", "-A")
        self.git(path, "commit", "-q", "-m", "first")
        self.git(path, "checkout", "-q", "-b", "lane")
        return path

    def write(self, repo, files):
        for rel, text in files.items():
            target = repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)

    def commit(self, repo, files, message="change"):
        self.write(repo, files)
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", message)

    # --- running
    def run_script(self, script, *args, env=None):
        out = subprocess.run([str(TOOLS / script), *map(str, args)], env=dict(self.env, **(env or {})), capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stderr, "", f"stderr of {script}")
        return out

    def last_line(self, out):
        lines = out.stdout.splitlines()
        match = LAST.match(lines[-1])
        self.assertIsNotNone(match, f"last line is not `exit=N log=PATH`: {lines[-1:]!r}")
        self.assertEqual(int(match.group(1)), out.returncode)
        return int(match.group(1)), match.group(2), lines[:-1]

    def assertLogged(self, log, within=None):
        path = Path(log)
        self.assertTrue(path.is_file(), f"log {log} does not exist")
        self.assertEqual(path.parent, (within or self.scratch) / "tp-tools")
        return path.read_text()


class ContractTests(ScriptCase):
    """Every script: usage errors and a non-repository also end with `exit=N log=PATH`."""

    def test_scripts_are_executable_and_pass_a_syntax_check(self):
        for script in ("pre-commit-gate.sh", "lane-info.sh", "web-jest.sh"):
            with self.subTest(script=script):
                self.assertTrue(os.access(TOOLS / script, os.X_OK))
                subprocess.run(["bash", "-n", str(TOOLS / script)], check=True)

    def test_usage_errors_end_with_the_exit_line_and_no_log(self):
        cases = [("pre-commit-gate.sh", []), ("lane-info.sh", []), ("web-jest.sh", []),
                 ("pre-commit-gate.sh", [self.tmp]), ("lane-info.sh", [self.tmp, "main"]), ("web-jest.sh", [self.tmp, "a.test.js"]),
                 ("lane-info.sh", ["--bogus", self.tmp])]
        for script, args in cases:
            with self.subTest(script=script, args=[str(a) for a in args]):
                out = self.run_script(script, *args)
                code, log, body = self.last_line(out)
                self.assertEqual((code, log), (2, "-"))
                self.assertTrue(body, "no message above the last line")

    def test_a_missing_base_stops_the_gate(self):
        repo = self.repo()
        out = self.run_script("pre-commit-gate.sh", repo)
        self.assertEqual(self.last_line(out)[0], 2)
        self.assertIn("no base", out.stdout)
        out = self.run_script("pre-commit-gate.sh", repo, "no-such-ref")
        self.assertEqual(self.last_line(out)[0], 2)
        self.assertIn("base ref not found", out.stdout)


class GateTests(ScriptCase):
    def lane_with_web_app(self):
        repo = self.repo(files={"README.md": "hi\n"})
        self.commit(repo, {
            "web/package.json": "{}\n", "web/tsconfig.json": "{}\n", "web/eslint.config.js": "module.exports = []\n",
            "web/app/(protected)/page.ts": "export const a = 1\n", "web/notes.txt": "plain\n",
        })
        return repo

    def test_all_checks_pass_and_the_last_line_is_the_contract(self):
        repo = self.lane_with_web_app()
        for tool in ("prettier", "eslint", "tsc", "gitleaks"):
            self.stub(tool)
        out = self.run_script("pre-commit-gate.sh", repo, "main")
        code, log, body = self.last_line(out)
        self.assertEqual(code, 0)
        for line in ("prettier 0", "eslint 0", "typecheck:web 0", "gitleaks 0"):
            self.assertIn(line, body)
        self.assertIn("== prettier", self.assertLogged(log))

    def test_the_files_go_to_prettier_and_eslint_as_one_argument_each_from_the_app_directory(self):
        repo = self.lane_with_web_app()
        for tool in ("prettier", "eslint", "tsc", "gitleaks"):
            self.stub(tool)
        self.run_script("pre-commit-gate.sh", repo, "main")
        prettier = self.calls_of("prettier")
        self.assertIn(f"cwd={repo}/web", prettier)
        self.assertIn("arg: --check", prettier)
        self.assertIn("arg: app/(protected)/page.ts", prettier)
        self.assertNotIn("notes.txt", prettier)
        self.assertIn("arg: app/(protected)/page.ts", self.calls_of("eslint"))
        self.assertIn("arg: eslint.config.js\n", self.calls_of("eslint"))  # a changed JS file is linted even when it is the config
        self.assertIn("arg: --noEmit", self.calls_of("tsc"))
        self.assertIn(f"cwd={repo}/web", self.calls_of("tsc"))

    def test_gitleaks_scans_base_to_head(self):
        repo = self.lane_with_web_app()
        for tool in ("prettier", "eslint", "tsc", "gitleaks"):
            self.stub(tool)
        base = self.git(repo, "rev-parse", "main")
        self.run_script("pre-commit-gate.sh", repo, "main")
        calls = self.calls_of("gitleaks")
        self.assertIn("arg: git", calls)
        self.assertIn(f"arg: --log-opts={base}..HEAD", calls)

    def test_a_failing_check_fails_the_gate_and_shows_its_output(self):
        repo = self.lane_with_web_app()
        self.stub("prettier", exit_code=1, output="[warn] app/(protected)/page.ts\n[warn] Code style issues found")
        for tool in ("eslint", "tsc", "gitleaks"):
            self.stub(tool)
        out = self.run_script("pre-commit-gate.sh", repo, "main")
        code, log, body = self.last_line(out)
        self.assertEqual(code, 1)
        self.assertIn("prettier 1", body)
        self.assertIn("eslint 0", body)
        self.assertIn("    [warn] Code style issues found", body)

    def test_a_missing_tool_is_skipped_and_does_not_fail(self):
        repo = self.lane_with_web_app()
        out = self.run_script("pre-commit-gate.sh", repo, "main")
        code, log, body = self.last_line(out)
        self.assertEqual(code, 0)
        for line in ("prettier skipped (not found)", "eslint skipped (not found)", "typecheck:web skipped (not found)", "gitleaks skipped (not found)"):
            self.assertIn(line, body)

    def test_tools_are_found_in_node_modules_up_the_tree(self):
        repo = self.lane_with_web_app()
        self.stub("prettier", where=repo / "node_modules" / ".bin")
        self.stub("tsc", where=repo / "web" / "node_modules" / ".bin")
        out = self.run_script("pre-commit-gate.sh", repo, "main")
        body = self.last_line(out)[2]
        self.assertIn("prettier 0", body)
        self.assertIn("typecheck:web 0", body)
        self.assertIn("eslint skipped (not found)", body)

    def test_eslint_without_a_config_is_skipped(self):
        repo = self.repo()
        self.commit(repo, {"web/package.json": "{}\n", "web/a.js": "x\n"})
        self.stub("eslint")
        body = self.last_line(self.run_script("pre-commit-gate.sh", repo, "main"))[2]
        self.assertIn("eslint skipped (no eslint config)", body)
        self.assertEqual(self.calls_of("eslint"), "")

    def test_a_deno_app_is_checked_with_deno_check_and_a_web_app_with_tsc(self):
        repo = self.repo()
        self.commit(repo, {"api/deno.json": "{}\n", "api/main.ts": "x\n", "web/package.json": "{}\n", "web/tsconfig.json": "{}\n", "web/a.ts": "x\n",
                           "lib/helper.ts": "x\n"})
        for tool in ("deno", "tsc"):
            self.stub(tool)
        body = self.last_line(self.run_script("pre-commit-gate.sh", repo, "main"))[2]
        self.assertIn("typecheck:api 0", body)
        self.assertIn("typecheck:web 0", body)
        self.assertIn("typecheck:. skipped (no deno.json or tsconfig.json)", body)
        deno = self.calls_of("deno")
        self.assertIn(f"cwd={repo}/api", deno)
        self.assertIn("arg: check\narg: --allow-import\narg: main.ts", deno)
        self.assertEqual(self.calls_of("tsc").count("--- call"), 1)

    def test_a_change_to_a_web_apps_config_or_js_still_typechecks_the_app(self):
        repo = self.repo()
        self.commit(repo, {"web/package.json": "{}\n", "web/tsconfig.json": "{}\n", "web/a.ts": "x\n"})
        self.git(repo, "checkout", "-q", "-b", "lane2")
        self.commit(repo, {"web/package.json": '{"a":1}\n', "web/tsconfig.json": '{"a":1}\n', "web/a.js": "x\n"})
        self.stub("tsc")
        body = self.last_line(self.run_script("pre-commit-gate.sh", repo, "lane"))[2]
        self.assertIn("typecheck:web 0", body)
        self.assertEqual(self.calls_of("tsc").count("--- call"), 1)
        self.assertNotIn("typecheck skipped", "\n".join(body))

    def test_a_package_json_alone_typechecks_a_web_app(self):
        repo = self.repo()
        self.commit(repo, {"web/package.json": "{}\n", "web/tsconfig.json": "{}\n"})
        self.git(repo, "checkout", "-q", "-b", "lane2")
        self.commit(repo, {"web/package.json": '{"a":1}\n'})
        self.stub("tsc")
        body = self.last_line(self.run_script("pre-commit-gate.sh", repo, "lane"))[2]
        self.assertIn("typecheck:web 0", body)

    def test_a_failing_tsc_after_a_config_only_change_fails_the_gate(self):
        repo = self.repo()
        self.commit(repo, {"web/package.json": "{}\n", "web/tsconfig.json": "{}\n"})
        self.git(repo, "checkout", "-q", "-b", "lane2")
        self.commit(repo, {"web/tsconfig.json": '{"a":1}\n'})
        self.stub("tsc", exit_code=2, output="error TS1: broke")
        code, log, body = self.last_line(self.run_script("pre-commit-gate.sh", repo, "lane"))
        self.assertEqual(code, 1)
        self.assertIn("typecheck:web 2", body)

    def test_a_deno_app_with_only_config_or_js_changes_checks_all_its_ts_files(self):
        repo = self.repo()
        self.commit(repo, {"api/deno.json": "{}\n", "api/main.ts": "x\n", "api/sub/util.ts": "x\n", "api/readme.txt": "x\n"})
        self.git(repo, "checkout", "-q", "-b", "lane2")
        self.commit(repo, {"api/deno.json": '{"a":1}\n', "api/script.js": "x\n"})
        self.stub("deno")
        body = self.last_line(self.run_script("pre-commit-gate.sh", repo, "lane"))[2]
        self.assertIn("typecheck:api 0", body)
        deno = self.calls_of("deno")
        self.assertIn(f"cwd={repo}/api", deno)
        self.assertIn("arg: check\n", deno)
        self.assertIn("arg: main.ts\n", deno)
        self.assertIn("arg: sub/util.ts\n", deno)

    def test_a_deno_app_with_no_ts_files_is_skipped_with_a_reason(self):
        repo = self.repo()
        self.commit(repo, {"api/deno.json": "{}\n"})
        self.stub("deno")
        body = self.last_line(self.run_script("pre-commit-gate.sh", repo, "main"))[2]
        self.assertIn("typecheck:api skipped (no ts files)", body)
        self.assertEqual(self.calls_of("deno"), "")

    def test_an_app_with_no_typecheck_config_and_no_ts_changes_prints_no_typecheck_line_for_it(self):
        repo = self.repo()
        self.commit(repo, {"docs/package.json": "{}\n", "docs/a.js": "x\n"})
        body = self.last_line(self.run_script("pre-commit-gate.sh", repo, "main"))[2]
        self.assertNotIn("typecheck:docs", "\n".join(body))
        self.assertIn("typecheck skipped (no ts files)", body)

    def test_deno_is_found_with_mise_when_it_is_not_on_path(self):
        repo = self.repo()
        self.commit(repo, {"api/deno.json": "{}\n", "api/main.ts": "x\n"})
        real = self.stub("deno-real", where=self.tmp / "installs")
        mise = self.bin / "mise"
        mise.write_text(f'#!/bin/sh\n[ "$1 $2" = "which deno" ] && echo "{real}"\n')
        mise.chmod(0o755)
        body = self.last_line(self.run_script("pre-commit-gate.sh", repo, "main"))[2]
        self.assertIn("typecheck:api 0", body)

    def test_the_base_comes_from_lane_base_then_the_upstream_branch(self):
        repo = self.lane_with_web_app()
        self.stub("gitleaks")
        base = self.git(repo, "rev-parse", "main")
        self.last_line(self.run_script("pre-commit-gate.sh", repo, env={"LANE_BASE": "main"}))
        self.assertIn(f"--log-opts={base}..HEAD", self.calls_of("gitleaks"))
        self.git(repo, "config", "branch.lane.remote", ".")
        self.git(repo, "config", "branch.lane.merge", "refs/heads/main")
        (self.calls / "gitleaks").unlink()
        out = self.run_script("pre-commit-gate.sh", repo)
        self.assertEqual(self.last_line(out)[0], 0)
        self.assertIn(f"--log-opts={base}..HEAD", self.calls_of("gitleaks"))

    def test_uncommitted_and_untracked_files_are_checked_and_deleted_ones_are_not(self):
        repo = self.lane_with_web_app()
        self.git(repo, "rm", "-q", "web/notes.txt")
        self.write(repo, {"web/new.ts": "x\n", "web/app/(protected)/page.ts": "export const a = 2\n"})
        self.stub("prettier")
        out = self.run_script("pre-commit-gate.sh", repo, "main")
        self.assertIn("changed files: ", out.stdout)
        calls = self.calls_of("prettier")
        self.assertIn("arg: new.ts", calls)
        self.assertNotIn("notes.txt", calls)

    def test_no_changed_files(self):
        repo = self.repo()
        body = self.last_line(self.run_script("pre-commit-gate.sh", repo, "main"))[2]
        self.assertIn("prettier skipped (no files)", body)
        self.assertIn("typecheck skipped (no ts files)", body)

    # --- deletions: a deleted file can break its app's importers, so it touches the app
    def lane_deleting(self, files, delete, commit=True):
        """`files` committed on main; then on the lane `delete` is removed (committed, or only removed from the worktree)."""
        repo = self.repo()
        self.commit(repo, files)
        self.git(repo, "checkout", "-q", "-b", "lane2")
        for rel in delete:
            if commit:
                self.git(repo, "rm", "-q", rel)
            else:
                (repo / rel).unlink()
        if commit:
            self.git(repo, "commit", "-q", "-m", "delete")
        return repo

    def test_deleting_a_ts_file_of_a_deno_app_checks_the_remaining_ts_files(self):
        repo = self.lane_deleting({"api/deno.json": "{}\n", "api/main.ts": "x\n", "api/gone.ts": "x\n", "api/sub/util.ts": "x\n"}, ["api/gone.ts"])
        self.stub("deno")
        body = self.last_line(self.run_script("pre-commit-gate.sh", repo, "lane"))[2]
        self.assertIn("typecheck:api 0", body)
        deno = self.calls_of("deno")
        self.assertIn("arg: main.ts\n", deno)
        self.assertIn("arg: sub/util.ts\n", deno)
        self.assertNotIn("gone.ts", deno)
        self.assertNotIn("typecheck skipped", "\n".join(body))

    def test_deleting_a_ts_file_of_a_web_app_runs_tsc(self):
        repo = self.lane_deleting({"web/package.json": "{}\n", "web/tsconfig.json": "{}\n", "web/a.ts": "x\n", "web/gone.ts": "x\n"}, ["web/gone.ts"])
        self.stub("tsc", exit_code=2, output="error TS2307: Cannot find module './gone'")
        code, log, body = self.last_line(self.run_script("pre-commit-gate.sh", repo, "lane"))
        self.assertEqual(code, 1)
        self.assertIn("typecheck:web 2", body)
        self.assertEqual(self.calls_of("tsc").count("--- call"), 1)

    def test_an_uncommitted_deletion_also_typechecks_the_app(self):
        repo = self.lane_deleting({"web/package.json": "{}\n", "web/tsconfig.json": "{}\n", "web/a.ts": "x\n", "web/gone.ts": "x\n"}, ["web/gone.ts"], commit=False)
        self.stub("tsc")
        body = self.last_line(self.run_script("pre-commit-gate.sh", repo, "lane"))[2]
        self.assertIn("typecheck:web 0", body)

    def test_deleting_a_non_ts_file_of_a_typed_app_also_typechecks_it(self):
        repo = self.lane_deleting({"web/package.json": "{}\n", "web/tsconfig.json": "{}\n", "web/a.ts": "x\n", "web/data.json": "{}\n"}, ["web/data.json"])
        self.stub("tsc")
        self.assertIn("typecheck:web 0", self.last_line(self.run_script("pre-commit-gate.sh", repo, "lane"))[2])

    def test_a_ts_file_moved_out_of_an_app_typechecks_the_app_it_left(self):
        repo = self.repo()
        self.commit(repo, {"web/package.json": "{}\n", "web/tsconfig.json": "{}\n", "web/moved.ts": "export const m = 1\n",
                           "other/package.json": "{}\n", "other/tsconfig.json": "{}\n"})
        self.git(repo, "checkout", "-q", "-b", "lane2")
        self.git(repo, "mv", "web/moved.ts", "other/moved.ts")
        self.git(repo, "commit", "-q", "-m", "move")
        self.stub("tsc")
        body = self.last_line(self.run_script("pre-commit-gate.sh", repo, "lane"))[2]
        self.assertIn("typecheck:web 0", body)
        self.assertIn("typecheck:other 0", body)

    def test_deleting_the_last_ts_file_of_a_deno_app_does_not_claim_there_were_no_ts_files(self):
        repo = self.lane_deleting({"api/deno.json": "{}\n", "api/gone.ts": "x\n"}, ["api/gone.ts"])
        self.stub("deno")
        body = self.last_line(self.run_script("pre-commit-gate.sh", repo, "lane"))[2]
        skipped = [line for line in body if line.startswith("typecheck:api skipped")]
        self.assertEqual(len(skipped), 1, body)
        self.assertNotIn("no ts files", skipped[0])
        self.assertEqual(self.calls_of("deno"), "")

    def test_deleting_a_ts_file_in_an_app_without_a_typecheck_config_is_reported(self):
        repo = self.lane_deleting({"docs/package.json": "{}\n", "docs/gone.ts": "x\n"}, ["docs/gone.ts"])
        body = self.last_line(self.run_script("pre-commit-gate.sh", repo, "lane"))[2]
        self.assertIn("typecheck:docs skipped (no deno.json or tsconfig.json)", body)

    def test_a_deletion_that_no_typecheck_cares_about_leaves_the_summary_alone(self):
        repo = self.lane_deleting({"docs/package.json": "{}\n", "docs/gone.md": "x\n"}, ["docs/gone.md"])
        body = self.last_line(self.run_script("pre-commit-gate.sh", repo, "lane"))[2]
        self.assertNotIn("typecheck:docs", "\n".join(body))
        self.assertIn("typecheck skipped (no ts files)", body)

    def test_the_ts_listing_of_a_deno_app_survives_literal_pathspecs(self):
        repo = self.repo()
        self.commit(repo, {"api/deno.json": "{}\n", "api/main.ts": "x\n", "api/sub/util.tsx": "x\n", "api/readme.txt": "x\n"})
        self.git(repo, "checkout", "-q", "-b", "lane2")
        self.commit(repo, {"api/deno.json": '{"a":1}\n'})
        self.stub("deno")
        body = self.last_line(self.run_script("pre-commit-gate.sh", repo, "lane", env={"GIT_LITERAL_PATHSPECS": "1"}))[2]
        self.assertIn("typecheck:api 0", body)
        deno = self.calls_of("deno")
        self.assertIn("arg: main.ts\n", deno)
        self.assertIn("arg: sub/util.tsx\n", deno)
        self.assertNotIn("readme.txt", deno)

    def test_literal_pathspecs_do_not_change_the_changed_file_list(self):
        repo = self.lane_with_web_app()
        for tool in ("prettier", "eslint", "tsc", "gitleaks"):
            self.stub(tool)
        self.run_script("pre-commit-gate.sh", repo, "main", env={"GIT_LITERAL_PATHSPECS": "1"})
        self.assertIn("arg: app/(protected)/page.ts", self.calls_of("prettier"))


class LaneInfoTests(ScriptCase):
    def with_remote(self):
        remote = self.tmp / "remote.git"
        remote.mkdir()
        self.git(remote, "init", "-q", "--bare", "-b", "main")
        repo = self.repo(files={"README.md": "hi\n", "package.json": "{}\n"})
        self.git(repo, "remote", "add", "origin", str(remote))
        self.git(repo, "push", "-q", "origin", "main")
        self.git(repo, "fetch", "-q", "origin")
        return remote, repo

    def advance_remote(self, remote):
        other = self.tmp / "other"
        self.git(self.tmp, "clone", "-q", str(remote), str(other))
        self.commit(other, {"news.txt": "n\n"}, "news")
        self.git(other, "push", "-q", "origin", "main")
        return self.git(other, "rev-parse", "HEAD")

    def test_summary_and_last_line(self):
        remote, repo = self.with_remote()
        self.commit(repo, {"a.txt": "a\n", "d/b.txt": "b\n"}, "lane work")
        self.write(repo, {"dirty.txt": "x\n"})
        code, log, body = self.last_line(self.run_script("lane-info.sh", repo, "origin/main"))
        text = "\n".join(body)
        self.assertEqual(code, 0)
        self.assertIn("branch: lane", text)
        self.assertRegex(text, r"head: [0-9a-f]+ lane work")
        self.assertIn(f"base: origin/main {self.git(repo, 'rev-parse', 'origin/main')[:10]}", text)
        self.assertIn(f"merge-base: {self.git(repo, 'rev-parse', 'main')}", text)
        self.assertIn("ahead/behind: 1/0", text)
        self.assertIn("  A\ta.txt", text)
        self.assertIn("  A\td/b.txt", text)
        self.assertIn("  ?? dirty.txt", text)
        self.assertIn("lane work", self.assertLogged(log) + "lane work")
        self.assertIn("branch: lane", self.assertLogged(log))

    def test_no_fetch_without_the_flag_and_a_fetch_with_it(self):
        remote, repo = self.with_remote()
        before = self.git(repo, "rev-parse", "origin/main")
        new = self.advance_remote(remote)
        body = "\n".join(self.last_line(self.run_script("lane-info.sh", repo, "origin/main"))[2])
        self.assertIn("fetch: not run (pass --fetch)", body)
        self.assertEqual(self.git(repo, "rev-parse", "origin/main"), before)
        self.assertIn(f"base: origin/main {before[:10]}", body)
        body = "\n".join(self.last_line(self.run_script("lane-info.sh", repo, "origin/main", "--fetch"))[2])
        self.assertIn("fetch: done", body)
        self.assertEqual(self.git(repo, "rev-parse", "origin/main"), new)
        self.assertIn(f"base: origin/main {new[:10]}", body)
        self.assertIn("ahead/behind: 0/1", body)

    def test_the_flag_may_come_before_the_other_arguments(self):
        remote, repo = self.with_remote()
        new = self.advance_remote(remote)
        self.last_line(self.run_script("lane-info.sh", "--fetch", repo, "origin/main"))
        self.assertEqual(self.git(repo, "rev-parse", "origin/main"), new)

    def test_a_failed_fetch_is_reported_and_does_not_fail_the_script(self):
        remote, repo = self.with_remote()
        shutil.rmtree(remote)
        code, _, body = self.last_line(self.run_script("lane-info.sh", repo, "origin/main", "--fetch"))
        self.assertEqual(code, 0)
        self.assertTrue(any(line.startswith("fetch: failed") for line in body), body)

    def test_base_from_lane_base_and_none(self):
        repo = self.repo()
        body = "\n".join(self.last_line(self.run_script("lane-info.sh", repo))[2])
        self.assertIn("base: none", body)
        body = "\n".join(self.last_line(self.run_script("lane-info.sh", repo, env={"LANE_BASE": "main"}))[2])
        self.assertIn("base: main ", body)
        body = "\n".join(self.last_line(self.run_script("lane-info.sh", repo, "nope"))[2])
        self.assertIn("base: nope (not found)", body)

    def test_node_modules_symlink_directory_and_missing(self):
        repo = self.repo(files={"package.json": "{}\n", "apps/web/package.json": "{}\n", "apps/api/package.json": "{}\n",
                                "apps/web/node_modules/.gitkeep": ""})
        shared = self.tmp / "shared_modules"
        shared.mkdir()
        os.symlink(shared, repo / "node_modules")
        (repo / "apps" / "api" / "node_modules").mkdir()
        body = "\n".join(self.last_line(self.run_script("lane-info.sh", repo, "main"))[2])
        self.assertIn(f"  .: symlink -> {shared}", body)
        self.assertIn("  apps/api: directory", body)
        self.assertIn("  apps/web: directory", body)
        shutil.rmtree(repo / "apps" / "api" / "node_modules")
        body = "\n".join(self.last_line(self.run_script("lane-info.sh", repo, "main"))[2])
        self.assertIn("  apps/api: missing", body)

    def test_deno_path_from_path_and_not_found(self):
        repo = self.repo()
        body = "\n".join(self.last_line(self.run_script("lane-info.sh", repo, "main"))[2])
        self.assertIn("deno: not found", body)
        deno = self.stub("deno")
        body = "\n".join(self.last_line(self.run_script("lane-info.sh", repo, "main"))[2])
        self.assertIn(f"deno: {deno}", body)

    def test_deno_falls_back_to_the_newest_mise_install(self):
        repo = self.repo()
        self.stub("deno", where=self.home / ".local/share/mise/installs/deno/2.1.0/bin")
        newest = self.stub("deno", where=self.home / ".local/share/mise/installs/deno/2.9.7/bin")
        body = "\n".join(self.last_line(self.run_script("lane-info.sh", repo, "main"))[2])
        self.assertIn(f"deno: {newest}", body)

    def test_drizzle_hook_names_the_newest_file_when_the_folder_exists(self):
        repo = self.repo(files={"README.md": "x\n", "api/drizzle/0001_a.sql": "", "api/drizzle/0012_b.sql": "", "api/drizzle/meta/_journal.json": "{}"})
        body = "\n".join(self.last_line(self.run_script("lane-info.sh", repo, "main"))[2])
        self.assertIn("drizzle: newest in api/drizzle is 0012_b.sql", body)
        plain = "\n".join(self.last_line(self.run_script("lane-info.sh", self.repo("plain"), "main"))[2])
        self.assertNotIn("drizzle", plain)


class WebJestTests(ScriptCase):
    PASS = "PASS app/(protected)/page.test.ts\n  page\n    ✓ renders (4 ms)\n    ✓ links (1 ms)\n    ○ skipped later\n\nTests:       2 passed, 1 skipped, 3 total"

    def web_repo(self):
        repo = self.repo(files={"README.md": "x\n"})
        self.commit(repo, {
            "apps/web/package.json": json.dumps({"devDependencies": {"jest": "^29"}}),
            "apps/web/app/(protected)/page.test.ts": "test\n", "apps/web/app/plain.test.ts": "test\n",
            "apps/other/jest.config.js": "module.exports = {}\n", "apps/other/a.test.js": "test\n",
            "apps/nojest/package.json": "{}\n", "apps/nojest/x.test.js": "test\n"})
        return repo

    def test_runs_jest_from_the_owning_package_with_the_flags_and_one_argument_per_path(self):
        repo = self.web_repo()
        self.stub("jest", output=self.PASS)
        test = repo / "apps/web/app/(protected)/page.test.ts"
        code, log, body = self.last_line(self.run_script("web-jest.sh", repo, test))
        self.assertEqual(code, 0)
        calls = self.calls_of("jest")
        self.assertIn(f"cwd={repo}/apps/web TZ=UTC", calls)
        self.assertIn("arg: --runTestsByPath\narg: --ci", calls)
        self.assertIn("arg: app/(protected)/page.test.ts\n", calls)
        self.assertEqual(calls.count("--- call"), 1)
        self.assertIn("  PASS app/(protected)/page.test.ts passed=2 failed=0 skipped=1", body)
        self.assertIn("  Tests:       2 passed, 1 skipped, 3 total", body)

    def test_a_path_with_parentheses_is_quoted_in_the_logged_command(self):
        repo = self.web_repo()
        self.stub("jest", output=self.PASS)
        log = self.last_line(self.run_script("web-jest.sh", repo, "apps/web/app/(protected)/page.test.ts"))[1]
        text = self.assertLogged(log)
        self.assertIn(r"app/\(protected\)/page.test.ts", text)
        self.assertIn("TZ=UTC", text)

    def test_a_relative_path_is_taken_from_the_worktree(self):
        repo = self.web_repo()
        self.stub("jest", output=self.PASS)
        self.last_line(self.run_script("web-jest.sh", repo, "apps/web/app/plain.test.ts"))
        self.assertIn("arg: app/plain.test.ts", self.calls_of("jest"))

    def test_tests_of_two_packages_run_in_their_own_directories(self):
        repo = self.web_repo()
        self.stub("jest", output="PASS a.test.js\n  ✓ one")
        out = self.run_script("web-jest.sh", repo, "apps/web/app/plain.test.ts", "apps/other/a.test.js", "apps/web/app/(protected)/page.test.ts")
        self.assertEqual(self.last_line(out)[0], 0)
        calls = self.calls_of("jest")
        self.assertEqual(calls.count("--- call"), 2)
        self.assertIn(f"cwd={repo}/apps/web ", calls)
        self.assertIn(f"cwd={repo}/apps/other ", calls)
        web = calls.split("--- call")[1]
        self.assertIn("arg: app/plain.test.ts", web)
        self.assertIn("arg: app/(protected)/page.test.ts", web)

    def test_repeat_gives_one_run_line_per_run(self):
        repo = self.web_repo()
        self.stub("jest", output=self.PASS)
        code, log, body = self.last_line(self.run_script("web-jest.sh", repo, "--repeat", "3", "apps/web/app/plain.test.ts"))
        self.assertEqual(code, 0)
        self.assertEqual([line for line in body if line.startswith("run ")], ["run 1 0", "run 2 0", "run 3 0"])
        self.assertEqual(self.calls_of("jest").count("--- call"), 3)

    def test_repeat_prints_per_file_counts_for_runs_that_all_pass(self):
        repo = self.web_repo()
        self.stub("jest", output=self.PASS)
        code, log, body = self.last_line(self.run_script("web-jest.sh", repo, "--repeat", "3", "apps/web/app/(protected)/page.test.ts"))
        self.assertEqual(code, 0)
        self.assertEqual(sum("PASS app/(protected)/page.test.ts passed=2 failed=0 skipped=1" in line for line in body), 3)
        # each run's counts sit right under its own run line
        runs = [i for i, line in enumerate(body) if line.startswith("run ")]
        for i in runs:
            self.assertIn("PASS app/(protected)/page.test.ts", body[i + 1])

    def test_a_failing_run_is_shown_with_its_counts_and_fails_the_script(self):
        repo = self.web_repo()
        self.stub("jest", output="FAIL app/plain.test.ts\n    ✓ a\n    ✕ b\n\nTests:       1 failed, 1 passed, 2 total", fail_on_call=2)
        code, log, body = self.last_line(self.run_script("web-jest.sh", repo, "--repeat", "3", "apps/web/app/plain.test.ts"))
        self.assertEqual(code, 1)
        self.assertEqual([line for line in body if line.startswith("run ")], ["run 1 0", "run 2 1", "run 3 0"])
        # the stub prints the same FAIL output on every call, so each run shows its counts
        self.assertEqual(sum("FAIL app/plain.test.ts passed=1 failed=1 skipped=0" in line for line in body), 3)

    def test_jest_missing_is_reported_and_fails(self):
        repo = self.web_repo()
        code, log, body = self.last_line(self.run_script("web-jest.sh", repo, "apps/web/app/plain.test.ts"))
        self.assertEqual(code, 127)
        self.assertTrue(any("jest not found" in line for line in body), body)

    def test_jest_is_found_in_node_modules_of_the_package(self):
        repo = self.web_repo()
        self.stub("jest", output=self.PASS, where=repo / "apps/web/node_modules/.bin")
        self.assertEqual(self.last_line(self.run_script("web-jest.sh", repo, "apps/web/app/plain.test.ts"))[0], 0)

    def test_bad_arguments(self):
        repo = self.web_repo()
        self.stub("jest")
        # (arguments, message, whether the script stopped before it made a log). The path checks run after the log exists.
        for args, needle, early in [(["--repeat", "x", "apps/web/app/plain.test.ts"], "whole number", True),
                                    (["--repeat", "0", "apps/web/app/plain.test.ts"], "1 to 50", True),
                                    (["--repeat"], "needs a number", True),
                                    ([], "no TEST_PATH", True),
                                    (["apps/web/app/missing.test.ts"], "not found", False),
                                    (["apps/nojest/x.test.js"], "no package with jest", False),
                                    (["--config-hook", "rel.js", "apps/web/app/plain.test.ts"], "absolute", True),
                                    (["--config-hook", str(self.tmp / "nope.js"), "apps/web/app/plain.test.ts"], "not found", True),
                                    (["--nope", "apps/web/app/plain.test.ts"], "unknown option", True)]:
            with self.subTest(args=args):
                out = self.run_script("web-jest.sh", repo, *args)
                code, log, body = self.last_line(out)
                self.assertEqual(code, 2)
                if early:
                    self.assertEqual(log, "-")
                else:
                    self.assertTrue(Path(log).is_file(), log)
                self.assertIn(needle, "\n".join(body))
        self.assertEqual(self.calls_of("jest"), "")

    def test_a_test_outside_the_worktree_is_refused(self):
        repo = self.web_repo()
        outside = self.tmp / "elsewhere.test.js"
        outside.write_text("x")
        out = self.run_script("web-jest.sh", repo, outside)
        self.assertEqual(self.last_line(out)[0], 2)
        self.assertIn("outside the worktree", out.stdout)

    @unittest.skipUnless(shutil.which("node"), "node is needed to load the wrapper config")
    def test_config_hook_wraps_the_package_config(self):
        repo = self.web_repo()
        os.symlink(shutil.which("node"), self.bin / "node")
        self.write(repo, {"apps/web/jest.config.js": "module.exports = { testEnvironment: 'node', moduleNameMapper: { '^a$': 'a' } }\n"})
        hook = self.tmp / "hook.js"
        hook.write_text("module.exports = (config, ctx) => ({ ...config, moduleNameMapper: { ...config.moduleNameMapper, '^zod$': 'zod/v4' },"
                        " displayName: ctx.packageDir.split('/').pop() })\n")
        self.stub("jest", output=self.PASS)
        out = self.run_script("web-jest.sh", repo, "--config-hook", hook, "apps/web/app/plain.test.ts")
        self.assertEqual(self.last_line(out)[0], 0)
        calls = self.calls_of("jest")
        match = re.search(r"arg: --config\narg: (\S+)", calls)
        self.assertIsNotNone(match, calls)
        wrapper = Path(match.group(1))
        self.assertEqual(wrapper.parent, self.scratch / "tp-tools")
        env = dict(self.env, TP_JEST_PACKAGE=str(repo / "apps/web"), TP_JEST_WORKTREE=str(repo), TP_JEST_BASE=str(repo / "apps/web/jest.config.js"), TP_JEST_HOOK=str(hook))
        loaded = subprocess.run(["node", "-e", f"require({json.dumps(str(wrapper))})().then(c => console.log(JSON.stringify(c)))"],
                                env=dict(os.environ, **env), capture_output=True, text=True, check=True)
        config = json.loads(loaded.stdout)
        self.assertEqual(config["moduleNameMapper"], {"^a$": "a", "^zod$": "zod/v4"})
        self.assertEqual((config["testEnvironment"], config["displayName"], config["rootDir"]), ("node", "web", str(repo / "apps/web")))

    def test_without_the_hook_no_config_is_passed(self):
        repo = self.web_repo()
        self.stub("jest", output=self.PASS)
        self.run_script("web-jest.sh", repo, "apps/web/app/plain.test.ts")
        self.assertNotIn("--config", self.calls_of("jest"))


if __name__ == "__main__":
    unittest.main()
