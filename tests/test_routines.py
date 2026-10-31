import builtins
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "routines"
sys.path.insert(0, str(ROOT / "lib"))
import routines  # noqa: E402

TP_IMPROVE = ROOT / "bin" / "tp-improve"
GATE, MISC, PRETTIER, DOCKER, PI = ("claude_gate.jsonl", "claude_misc.jsonl", "claude_prettier.jsonl",
                                    "claude_docker.jsonl", "pi_session.jsonl")
GITLEAKS = "gitleaks run"
PRETTIER_PROBE = "prettier --check/--write"
COMMIT = "git add+commit"
LANE_COMMON = "Read: lane/brief common brief (lane-common/brief-common)"


def build_project(root, tasks):
    """tasks: [(id, role, created, fixture file)] -> a project dir laid out as tmux-pi lays out its tasks."""
    root = Path(root)
    for tid, role, created, fixture in tasks:
        d = root / ".tmux-pi" / "sessions" / "s1" / "tasks" / tid
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text(json.dumps({"id": tid, "role": role, "created": created}))
        (d / "transcript-path").write_text(str(FIX / fixture) + "\n")
    return root


def run_cli(*args):
    return subprocess.run([sys.executable, str(TP_IMPROVE), *args], capture_output=True, text=True, cwd=ROOT)


def by(entries, key):
    return {(e["role"], e[key]): e for e in entries}


class Parsing(unittest.TestCase):
    def test_claude_tool_use_and_result_are_paired(self):
        calls = routines.parse_transcript(FIX / GATE)
        self.assertEqual([c["name"] for c in calls], ["Bash"] * 5)
        self.assertEqual(calls[0]["input"]["command"], "gitleaks detect --source . --no-banner")
        self.assertEqual(calls[0]["bytes"], len("no leaks found"))
        self.assertEqual(calls[1]["bytes"], len("All matched files use Prettier code style!"))  # list content

    def test_pi_tool_call_and_result_are_paired_and_named_like_claude(self):
        calls = routines.parse_transcript(FIX / PI)
        self.assertEqual([c["name"] for c in calls], ["Read", "Bash", "Bash", "Read", "Edit"])
        self.assertEqual(calls[1]["input"]["command"], "gitleaks detect --source .")
        self.assertEqual(calls[0]["input"]["path"], "/work/app/briefs/lane-common.md")
        self.assertEqual(calls[1]["bytes"], len("no leaks found"))

    def test_both_formats_reach_the_same_probe(self):
        for fixture in (GATE, PI):
            probes = set()
            for call in routines.parse_transcript(FIX / fixture):
                probes |= routines.keys_for(call)[0]
            self.assertIn(GITLEAKS, probes, fixture)

    def test_bad_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.jsonl"
            p.write_text('not json\n[1]\n{"message": 3}\n' + (FIX / PI).read_text())
            self.assertEqual(len(routines.parse_transcript(p)), 5)

    def test_task_joined_through_meta_and_transcript_path_and_grouped_by_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_project(tmp, [("t1", "coder", "2026-10-01T00:00:00Z", GATE), ("t2", "review", "2026-10-01T00:00:00Z", PRETTIER),
                                ("t3", "coder", "2026-10-01T00:00:00Z", PI)])
            d = Path(tmp) / ".tmux-pi" / "sessions" / "s1" / "tasks" / "nometa"  # no meta.json: skipped
            d.mkdir()
            (d / "transcript-path").write_text(str(FIX / GATE))
            d = Path(tmp) / ".tmux-pi" / "sessions" / "s1" / "tasks" / "gone"  # transcript missing: skipped
            d.mkdir()
            (d / "meta.json").write_text('{"id": "gone", "role": "coder"}')
            (d / "transcript-path").write_text(str(FIX / "absent.jsonl"))
            roles = routines.mine(routines.find_tasks(str(Path(tmp) / ".tmux-pi" / "sessions")))
        self.assertEqual({r: v["tasks"] for r, v in roles.items()}, {"coder": 2, "review": 1})
        self.assertEqual(roles["coder"]["probes"][GITLEAKS]["tasks"], 2)
        self.assertNotIn(GITLEAKS, roles["review"]["probes"])


class Normalization(unittest.TestCase):
    def test_paths_shas_ids_numbers_become_placeholders(self):
        n = routines.norm_cmd
        self.assertEqual(n("cd /work/app-wt-x && git diff 3f2a9c1 -- src/a.ts"), "git diff <SHA> -- <P>")
        self.assertEqual(n("cat ~/notes/a.md ./b.md ../c.md"), "cat <P> <P> <P>")
        self.assertEqual(n("tp-agent show coder-7f6a8ac6d1"), "tp-agent show <TASK>")
        self.assertEqual(n("curl 123e4567-e89b-12d3-a456-426614174000"), "curl <UUID>")
        self.assertEqual(n("npm run test -- --retries 3"), "npm run test -- --retries <N>")
        self.assertEqual(n("echo '{}'".format("x" * 30)), "echo '<STR>'")
        self.assertEqual(n("git  log\t--oneline"), "git log --oneline")
        self.assertEqual(n("ls deadbeef"), "ls deadbeef")  # a hex word with no digit is not a sha

    def test_heredoc_body_is_cut(self):
        self.assertEqual(routines.norm_cmd("git commit -F - <<'EOF'\nTighten eslint 42\nEOF"), "git commit -F - <<HEREDOC")
        self.assertEqual(routines.norm_cmd('cat <<"EOF" > f\nbody\nEOF'), "cat <<HEREDOC")

    def test_lead_shape(self):
        lead = lambda c: routines.lead_shape(routines.norm_cmd(c))
        self.assertEqual(lead("cd /w && npm run check:web -- x"), "npm run check:web")
        self.assertEqual(lead("FOO=1 kubectl get pods -n web | head"), "kubectl get")
        self.assertEqual(lead("git -C /w log --oneline"), "git log --oneline")
        self.assertEqual(lead("npx --yes prettier --check a"), "npx prettier")
        self.assertEqual(lead("/opt/bin/tool run"), "<P> run")
        self.assertEqual(lead("echo hi"), "")

    def test_heredoc_body_never_reaches_a_probe(self):
        call = {"name": "Bash", "bytes": 0, "input": {"command": "git commit -F - <<'EOF'\nTighten eslint\nrun gitleaks and prettier\nEOF"}}
        probes, lead = routines.keys_for(call)
        self.assertEqual(probes, {COMMIT})
        self.assertIsNone(lead)  # matched a probe, so it is not a probe-less lead


class Safety(unittest.TestCase):
    def mine_one(self, command):
        call = {"name": "Bash", "bytes": 0, "input": {"command": command}}
        return routines.keys_for(call)

    def test_credential_looking_commands_are_dropped(self):
        for cmd in ("export API_KEY=abc123 && gitleaks detect", "curl -H 'Authorization: Bearer abc' x",
                    "echo hello\nTOKEN=zzz\ngitleaks detect", "psql postgres://u:pw@host/db -c 'select 1'",
                    "echo AKIAABCDEFGHIJKL", "git commit -F - <<'EOF'\nthe password is hunter2\nEOF"):
            self.assertEqual(self.mine_one(cmd), (set(), None), cmd)

    def test_credential_calls_leave_no_trace_in_the_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_project(tmp, [("t1", "coder", "2026-10-01T00:00:00Z", MISC)])
            text, _ = routines.run(tmp, min_tasks=1, as_json=True)
        for leak in ("abc123", "API_KEY", "Bearer", "hunter2", "Authorization"):
            self.assertNotIn(leak, text)
        report = json.loads(text)
        # m2 (credential, "kubectl get pods") and m3 (credential line) are dropped; m4 stays
        leads = {e["lead"]: e for e in report["new_leads"]}
        self.assertEqual(leads["kubectl get"]["calls"], 1)

    def test_env_files_are_never_opened(self):
        opened = []
        real_open = builtins.open

        def spy(file, *a, **k):
            opened.append(str(file))
            return real_open(file, *a, **k)

        with tempfile.TemporaryDirectory() as tmp:
            build_project(tmp, [("t1", "coder", "2026-10-01T00:00:00Z", MISC)])
            env_transcript = Path(tmp) / ".env.transcript.jsonl"
            env_transcript.write_text((FIX / GATE).read_text())
            d = Path(tmp) / ".tmux-pi" / "sessions" / "s1" / "tasks" / "t2"
            d.mkdir()
            (d / "meta.json").write_text('{"id": "t2", "role": "coder", "created": "2026-10-01T00:00:00Z"}')
            (d / "transcript-path").write_text(str(env_transcript))
            with mock.patch("builtins.open", spy):
                roles = routines.mine(routines.find_tasks(str(Path(tmp) / ".tmux-pi" / "sessions")))
        self.assertEqual(roles["coder"]["tasks"], 1)  # only t1
        self.assertFalse([p for p in opened if os.path.basename(p).startswith(".env")], opened)

    def test_env_file_targets_never_match_a_probe(self):
        for tool, arg in (("Read", "file_path"), ("Edit", "path")):
            call = {"name": tool, "bytes": 0, "input": {arg: "/work/app/.env.lane-common"}}
            self.assertEqual(routines.keys_for(call), (set(), None))
        call = {"name": "Read", "bytes": 0, "input": {"file_path": "/work/app/briefs/lane-common.md"}}
        self.assertEqual(routines.keys_for(call), ({LANE_COMMON}, None))


class Report(unittest.TestCase):
    RUN1 = [("t1", "coder", "2026-10-01T00:00:00Z", GATE), ("t2", "coder", "2026-10-05T00:00:00Z", GATE),
            ("t3", "coder", "2026-10-20T00:00:00Z", MISC), ("t4", "coder", "2026-10-21T00:00:00Z", PI),
            ("t5", "review", "2026-10-02T00:00:00Z", PRETTIER)]
    RUN2 = [("g1", "coder", "2026-11-01T00:00:00Z", GATE), ("g2", "coder", "2026-11-01T00:00:00Z", GATE),
            ("g3", "coder", "2026-11-01T00:00:00Z", GATE), ("g4", "coder", "2026-11-01T00:00:00Z", GATE),
            ("p1", "coder", "2026-11-01T00:00:00Z", PI),
            ("d1", "coder", "2026-11-01T00:00:00Z", DOCKER), ("d2", "coder", "2026-11-01T00:00:00Z", DOCKER),
            ("d3", "coder", "2026-11-01T00:00:00Z", DOCKER),
            ("r1", "review", "2026-11-01T00:00:00Z", PRETTIER), ("r2", "review", "2026-11-01T00:00:00Z", PRETTIER)]

    def report(self, tasks, out, *extra):
        with tempfile.TemporaryDirectory() as tmp:
            build_project(tmp, tasks)
            res = run_cli("routines", "--project", tmp, "--out", str(out), "--json", *extra)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(Path(out).read_text())

    def test_counts_for_one_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_project(tmp, self.RUN1)
            roles = routines.mine(routines.find_tasks(str(Path(tmp) / ".tmux-pi" / "sessions")))
        coder = roles["coder"]
        self.assertEqual({k: v["tasks"] for k, v in coder["probes"].items()},
                         {GITLEAKS: 3, PRETTIER_PROBE: 2, COMMIT: 1, LANE_COMMON: 2,
                          "pre-commit gate bundle (prettier|eslint|gitleaks|tsc|deno check)": 3})
        # a task counts once however many calls it makes: kubectl get has 2 calls in t1
        self.assertEqual(coder["leads"]["kubectl get"]["tasks"], 4)
        self.assertGreater(coder["leads"]["kubectl get"]["calls"], 4)
        self.assertEqual(coder["leads"]["docker compose"]["tasks"], 2)
        self.assertNotIn("git commit", coder["leads"])  # it matched a probe
        self.assertNotIn("npx prettier", roles["review"]["leads"])

    def test_three_lists_across_two_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "reports" / "routines.json"
            first = self.report(self.RUN1, out)
            self.assertTrue(first["baseline"])
            self.assertTrue(Path(str(out) + ".counts.json").is_file())
            # run 1 as baseline: leads at 3+ tasks are new, probes grew from 0
            self.assertEqual({(e["role"], e["lead"]) for e in first["new_leads"]}, {("coder", "kubectl get")})
            self.assertEqual(first["dropped"], [])
            self.assertEqual(by(first["grew"], "probe")[("coder", GITLEAKS)]["previous"], 0)

            second = self.report(self.RUN2, out)
        self.assertFalse(second["baseline"])
        grew = by(second["grew"], "probe")
        self.assertEqual((grew[("coder", GITLEAKS)]["previous"], grew[("coder", GITLEAKS)]["tasks"]), (3, 5))
        self.assertEqual((grew[("coder", PRETTIER_PROBE)]["previous"], grew[("coder", PRETTIER_PROBE)]["tasks"]), (2, 4))
        self.assertEqual((grew[("review", PRETTIER_PROBE)]["previous"], grew[("review", PRETTIER_PROBE)]["tasks"]), (1, 2))
        self.assertNotIn(("coder", COMMIT), grew)
        self.assertNotIn(("coder", LANE_COMMON), grew)
        # docker compose: 2 tasks before, 7 now, no probe -> new. kubectl get was already at 3+ -> not new again.
        new = by(second["new_leads"], "lead")
        self.assertEqual(set(new), {("coder", "docker compose")})
        self.assertEqual(new[("coder", "docker compose")]["tasks"], 7)
        # git commit went 1 -> 0 (absent now); lane-common read went 2 -> 1
        dropped = by(second["dropped"], "probe")
        self.assertEqual({k: (v["previous"], v["tasks"]) for k, v in dropped.items()},
                         {("coder", COMMIT): (1, 0), ("coder", LANE_COMMON): (2, 1)})

    def test_counts_file_is_rewritten_each_run_and_roles_not_analysed_are_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.txt"
            self.report(self.RUN1, out)
            counts = Path(str(out) + ".counts.json")
            first = json.loads(counts.read_text())
            self.assertEqual(first["roles"]["review"]["probes"][PRETTIER_PROBE], 1)
            self.report(self.RUN2, out, "--role", "coder")
            second = json.loads(counts.read_text())
        self.assertEqual(second["roles"]["coder"]["probes"][GITLEAKS], 5)
        self.assertEqual(second["roles"]["review"], first["roles"]["review"])  # not analysed, so kept

    def test_role_filter_limits_every_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.json"
            self.report(self.RUN1, out)
            second = self.report(self.RUN2, out, "--role", "review")
        self.assertEqual(list(second["summary"]), ["review"])
        for key in ("grew", "new_leads", "dropped"):
            self.assertEqual({e["role"] for e in second[key]}, {"review"} if second[key] else set(), key)
        self.assertIn(("review", PRETTIER_PROBE), by(second["grew"], "probe"))

    def test_since_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_project(tmp, self.RUN1)
            out = Path(tmp) / "r.json"
            res = run_cli("routines", "--project", tmp, "--since", "2026-10-15", "--json", "--min-tasks", "1", "--out", str(out))
            self.assertEqual(res.returncode, 0, res.stderr)
            newer = json.loads(out.read_text())
            res = run_cli("routines", "--project", tmp, "--json", "--min-tasks", "1")
            everything = json.loads(res.stdout)
        # t3 and t4 are on or after 2026-10-15; t1, t2, t5 are not
        self.assertEqual(newer["summary"], {"coder": {"tasks": 2, "calls": 11}})
        self.assertEqual(newer["since"], "2026-10-15")
        self.assertEqual(everything["summary"]["coder"]["tasks"], 4)
        self.assertEqual(by(newer["grew"], "probe")[("coder", GITLEAKS)]["tasks"], 1)
        self.assertEqual(by(everything["grew"], "probe")[("coder", GITLEAKS)]["tasks"], 3)

    def test_min_tasks_applies_to_new_leads(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_project(tmp, self.RUN1)
            at3 = json.loads(run_cli("routines", "--project", tmp, "--json").stdout)
            at2 = json.loads(run_cli("routines", "--project", tmp, "--json", "--min-tasks", "2").stdout)
        self.assertEqual({e["lead"] for e in at3["new_leads"]}, {"kubectl get"})
        self.assertEqual({e["lead"] for e in at2["new_leads"]}, {"kubectl get", "docker compose"})

    def test_text_report_has_the_three_lists(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_project(tmp, self.RUN1)
            res = run_cli("routines", "--project", tmp)
        self.assertEqual(res.returncode, 0, res.stderr)
        for heading in ("(a) probes in more tasks", "(b) new Bash lead commands", "(c) probes in fewer tasks"):
            self.assertIn(heading, res.stdout)
        self.assertIn("kubectl get", res.stdout)
        self.assertFalse(os.path.exists(os.path.join(tmp, "x")))

    def test_no_out_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_project(tmp, self.RUN1)
            before = sorted(str(p) for p in Path(tmp).rglob("*"))
            run_cli("routines", "--project", tmp)
            self.assertEqual(sorted(str(p) for p in Path(tmp).rglob("*")), before)

    def test_unusable_previous_counts_are_reported_and_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_project(tmp, self.RUN1)
            out = Path(tmp) / "r.json"
            Path(str(out) + ".counts.json").write_text("{broken")
            res = run_cli("routines", "--project", tmp, "--out", str(out))
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("previous counts unreadable", res.stderr)
            self.assertEqual(json.loads(Path(str(out) + ".counts.json").read_text())["version"], routines.STATE_VERSION)

    def test_bad_arguments_fail_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_project(tmp, self.RUN1)
            self.assertEqual(run_cli("routines", "--project", tmp, "--since", "yesterday").returncode, 2)
            self.assertEqual(run_cli("routines", "--project", tmp, "--min-tasks", "0").returncode, 2)
            self.assertEqual(run_cli("routines", "--project", os.path.join(tmp, "none")).returncode, 2)
        self.assertNotEqual(run_cli("routines").returncode, 0)  # --project is required


class ExistingReportUnchanged(unittest.TestCase):
    def test_report_output_matches_the_output_before_routines_existed(self):
        res = run_cli("report", str(FIX / PI))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, (FIX / "tp_improve_report.golden").read_text())

    def test_report_json_still_has_its_keys(self):
        res = run_cli("report", "--json", str(FIX / PI))
        data = json.loads(res.stdout)
        self.assertEqual(sorted(data[0]), ["blocks", "compactions", "errors", "findings", "guard_edits", "reread",
                                           "session", "tools", "usage", "worst_error_streak"])

    def test_a_subcommand_is_still_required(self):
        self.assertNotEqual(run_cli().returncode, 0)


if __name__ == "__main__":
    unittest.main()
