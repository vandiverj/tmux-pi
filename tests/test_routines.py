import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
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
BUNDLE = "pre-commit gate bundle (prettier|eslint|gitleaks|tsc|deno check)"
LANE_BASE = "lane base / merge-base / origin base"


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

    def test_a_symlink_to_a_dotenv_file_is_never_opened(self):
        opened = []
        real_open = builtins.open

        def spy(file, *a, **k):
            opened.append(os.path.realpath(str(file)))
            return real_open(file, *a, **k)

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / (".en" + "v.production")
            target.write_text((FIX / GATE).read_text())
            link = Path(tmp) / "worker.jsonl"
            link.symlink_to(target)
            hidden = Path(tmp) / (".en" + "v.d")  # a symlinked directory component
            hidden.mkdir()
            (hidden / "t.jsonl").write_text((FIX / GATE).read_text())
            via_dir = Path(tmp) / "plain"
            via_dir.symlink_to(hidden)
            for name, tpath in (("t1", link), ("t2", via_dir / "t.jsonl")):
                d = Path(tmp) / ".tmux-pi" / "sessions" / "s1" / "tasks" / name
                d.mkdir(parents=True)
                (d / "meta.json").write_text(json.dumps({"id": name, "role": "coder", "created": "2026-10-01T00:00:00Z"}))
                (d / "transcript-path").write_text(str(tpath))
            sessions = str(Path(tmp) / ".tmux-pi" / "sessions")
            with mock.patch("builtins.open", spy):
                self.assertEqual(routines.find_tasks(sessions), [])
                # a caller that skips find_tasks is refused too
                self.assertEqual(routines.mine([({"id": "t1", "role": "coder"}, str(link))])["coder"]["tasks"], 0)
                with self.assertRaises(OSError):
                    routines.parse_transcript(str(link))
            self.assertFalse([p for p in opened if any(part.startswith(".en" + "v") for part in Path(p).parts)], opened)

    def test_every_reader_refuses_a_symlink_that_leads_to_a_dotenv_file(self):
        """meta.json, transcript-path and the previous counts file are checked like a transcript."""
        opened = []
        real_open = builtins.open
        hidden = ".en" + "v.production"

        def spy(file, *a, **k):
            opened.append(os.path.realpath(str(file)))
            return real_open(file, *a, **k)

        def leaked():
            return [p for p in opened if any(part.startswith(".en" + "v") for part in Path(p).parts)]

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            sessions = tmp / ".tmux-pi" / "sessions"
            good = {"id": "t1", "role": "coder", "created": "2026-10-01T00:00:00Z"}
            # t1: meta.json is a link to a hidden file holding valid-looking meta
            d1 = sessions / "s1" / "tasks" / "t1"
            d1.mkdir(parents=True)
            (tmp / hidden).write_text(json.dumps(good))
            (d1 / "meta.json").symlink_to(tmp / hidden)
            (d1 / "transcript-path").write_text(str(FIX / GATE))
            # t2: transcript-path is a link to a hidden file holding a valid transcript path
            d2 = sessions / "s1" / "tasks" / "t2"
            d2.mkdir(parents=True)
            (d2 / "meta.json").write_text(json.dumps(dict(good, id="t2")))
            (tmp / (hidden + ".path")).write_text(str(FIX / GATE))
            (d2 / "transcript-path").symlink_to(tmp / (hidden + ".path"))
            # the previous counts file is a link to a hidden file holding valid counts
            (tmp / (hidden + ".counts")).write_text(json.dumps({"version": routines.STATE_VERSION, "roles": {}}))
            counts = tmp / "report.json.counts.json"
            counts.symlink_to(tmp / (hidden + ".counts"))
            with mock.patch("builtins.open", spy):
                self.assertEqual(routines.find_tasks(str(sessions)), [])
                state, why = routines.load_state(str(counts))
            self.assertIsNone(state)
            self.assertTrue(why)  # the caller is told the counts could not be used
            self.assertEqual(leaked(), [])

    def test_an_ordinary_task_dir_and_counts_file_are_still_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_project(tmp, [("t1", "coder", "2026-10-01T00:00:00Z", GATE)])
            self.assertEqual(len(routines.find_tasks(str(Path(tmp) / ".tmux-pi" / "sessions"))), 1)
            counts = Path(tmp) / "c.json"
            counts.write_text(json.dumps({"version": routines.STATE_VERSION, "roles": {}}))
            self.assertEqual(routines.load_state(str(counts))[0]["roles"], {})

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
        # growth is judged on shares: coder went 4 -> 8 tasks, so gitleaks rose 3 -> 5 but fell from 75% to 62.5%;
        # prettier went 2 of 4 -> 4 of 8 (50% both); review prettier went 1 of 1 -> 2 of 2 (100% both)
        self.assertEqual(second["grew"], [])
        # docker compose: 2 tasks before, 7 now, no probe -> new. kubectl get was already at 3+ -> not new again.
        new = by(second["new_leads"], "lead")
        self.assertEqual(set(new), {("coder", "docker compose")})
        self.assertEqual(new[("coder", "docker compose")]["tasks"], 7)
        # git commit went 1 -> 0 (absent now); lane-common read went 2 -> 1; gitleaks and the bundle fell with the share
        dropped = by(second["dropped"], "probe")
        self.assertEqual({k: (v["previous"], v["tasks"]) for k, v in dropped.items()},
                         {("coder", GITLEAKS): (3, 5), ("coder", BUNDLE): (3, 5), ("coder", COMMIT): (1, 0),
                          ("coder", LANE_COMMON): (2, 1)})
        gitleaks = dropped[("coder", GITLEAKS)]
        self.assertEqual((gitleaks["previous_share"], gitleaks["share"]), (0.75, 0.625))
        self.assertEqual((gitleaks["previous_role_tasks"], gitleaks["role_tasks"]), (4, 8))

    def test_counts_file_is_rewritten_each_run_and_roles_not_analysed_are_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.txt"
            self.report(self.RUN1, out)
            counts = Path(str(out) + ".counts.json")
            first = json.loads(counts.read_text())
            self.assertEqual(first["roles"]["review"]["probes"][PRETTIER_PROBE], 1)
            self.assertEqual((first["roles"]["coder"]["tasks"], first["roles"]["review"]["tasks"]), (4, 1))  # denominators
            self.report(self.RUN2, out, "--role", "coder")
            second = json.loads(counts.read_text())
        self.assertEqual(second["roles"]["coder"]["probes"][GITLEAKS], 5)
        self.assertEqual(second["roles"]["review"], first["roles"]["review"])  # not analysed, so kept

    def test_a_role_that_vanishes_from_an_unscoped_scan_drops_to_zero(self):
        review_only = [t for t in self.RUN1 if t[1] == "review"]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.json"
            self.report(self.RUN1, out)
            second = self.report(review_only, out)
            saved = json.loads(Path(str(out) + ".counts.json").read_text())
            third = self.report(review_only, out)
        dropped = by(second["dropped"], "probe")
        self.assertEqual((dropped[("coder", GITLEAKS)]["previous"], dropped[("coder", GITLEAKS)]["tasks"]), (3, 0))
        self.assertEqual(dropped[("coder", COMMIT)]["tasks"], 0)
        self.assertNotIn("coder", saved["roles"])  # the stale counts do not linger
        self.assertEqual(third["dropped"], [])  # and are not reported a second time

    def test_a_role_filter_with_no_matching_tasks_drops_that_role_and_keeps_the_others(self):
        review_only = [t for t in self.RUN1 if t[1] == "review"]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.json"
            first = self.report(self.RUN1, out)
            saved_first = json.loads(Path(str(out) + ".counts.json").read_text())
            second = self.report(review_only, out, "--role", "coder")
            saved = json.loads(Path(str(out) + ".counts.json").read_text())
        self.assertEqual(by(second["dropped"], "probe")[("coder", GITLEAKS)]["tasks"], 0)
        self.assertEqual({e["role"] for e in second["dropped"]}, {"coder"})
        self.assertNotIn("coder", saved["roles"])
        self.assertEqual(saved["roles"]["review"], saved_first["roles"]["review"])

    def test_a_lead_reported_at_the_old_threshold_is_not_new_under_a_higher_one(self):
        three = [("d1", "coder", "2026-10-01T00:00:00Z", DOCKER), ("d2", "coder", "2026-10-01T00:00:00Z", DOCKER),
                 ("d3", "coder", "2026-10-01T00:00:00Z", DOCKER)]
        four = three + [("d4", "coder", "2026-10-02T00:00:00Z", DOCKER)]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.json"
            first = self.report(three, out, "--min-tasks", "3")
            second = self.report(four, out, "--min-tasks", "4")
            lower = self.report(four, Path(tmp) / "other.json", "--min-tasks", "2")
        self.assertEqual({e["lead"] for e in first["new_leads"]}, {"docker compose"})
        self.assertEqual(second["new_leads"], [])  # already reported once, at 3
        self.assertEqual({e["lead"] for e in lower["new_leads"]}, {"docker compose"})

    def test_a_reported_lead_stays_reported_across_a_raise_then_a_rise(self):
        """run 1: 3 tasks at --min-tasks 3 (reported); run 2: same tasks at 4; run 3: 4 tasks at 4."""
        three = [("d%d" % i, "coder", "2026-10-01T00:00:00Z", DOCKER) for i in (1, 2, 3)]
        four = three + [("d4", "coder", "2026-10-02T00:00:00Z", DOCKER)]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.json"
            first = self.report(three, out, "--min-tasks", "3")
            second = self.report(three, out, "--min-tasks", "4")
            saved = json.loads(Path(str(out) + ".counts.json").read_text())
            third = self.report(four, out, "--min-tasks", "4")
        self.assertEqual({e["lead"] for e in first["new_leads"]}, {"docker compose"})
        self.assertEqual(second["new_leads"], [])
        self.assertIn("docker compose", saved["roles"]["coder"]["leads"])  # kept although below 4
        self.assertEqual(third["new_leads"], [])

    def test_a_lead_never_reported_before_is_new_when_the_threshold_drops(self):
        three = [("d1", "coder", "2026-10-01T00:00:00Z", DOCKER), ("d2", "coder", "2026-10-01T00:00:00Z", DOCKER),
                 ("d3", "coder", "2026-10-01T00:00:00Z", DOCKER)]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.json"
            first = self.report(three, out, "--min-tasks", "4")
            second = self.report(three, out, "--min-tasks", "3")
        self.assertEqual(first["new_leads"], [])
        self.assertEqual({e["lead"] for e in second["new_leads"]}, {"docker compose"})

    def test_role_filter_limits_every_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.json"
            self.report(self.RUN1, out)
            second = self.report(self.RUN2, out, "--role", "review")
        self.assertEqual(list(second["summary"]), ["review"])
        for key in ("grew", "new_leads", "dropped"):
            self.assertEqual({e["role"] for e in second[key]}, {"review"} if second[key] else set(), key)
        self.assertEqual(second["grew"], [])  # review went 1 of 1 -> 2 of 2 tasks: its share did not change

    def test_since_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_project(tmp, self.RUN1)
            out = Path(tmp) / "r.json"
            res = run_cli("routines", "--project", tmp, "--since", "2026-10-15", "--json", "--min-tasks", "1", "--out", str(out))
            self.assertEqual(res.returncode, 0, res.stderr)
            newer = json.loads(out.read_text())
            res = run_cli("routines", "--project", tmp, "--json", "--min-tasks", "1")
            everything = json.loads(res.stdout)
            # --until keeps tasks on or before the date: t1, t2 (coder) and t5 (review); with --since, t3 only
            earlier = json.loads(run_cli("routines", "--project", tmp, "--until", "2026-10-15", "--json", "--min-tasks", "1").stdout)
            window = json.loads(run_cli("routines", "--project", tmp, "--since", "2026-10-15", "--until", "2026-10-20",
                                        "--json", "--min-tasks", "1").stdout)
        self.assertEqual({r: v["tasks"] for r, v in earlier["summary"].items()}, {"coder": 2, "review": 1})
        self.assertEqual(earlier["until"], "2026-10-15")
        self.assertEqual({r: v["tasks"] for r, v in window["summary"].items()}, {"coder": 1})
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
        # per-role denominators and ratios: gitleaks is in 3 of the coder role's 4 tasks (no previous run: was 0/0)
        self.assertIn("coder: 4 tasks", res.stdout)
        self.assertIn("calls per task", res.stdout)
        self.assertIn("tasks 3/4 (was 0/0)", res.stdout)
        self.assertIn("any", res.stdout)
        self.assertIn("excl", res.stdout)
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
            self.assertEqual(run_cli("routines", "--project", tmp, "--until", "yesterday").returncode, 2)
            self.assertEqual(run_cli("routines", "--project", tmp, "--min-tasks", "0").returncode, 2)
            self.assertEqual(run_cli("routines", "--project", os.path.join(tmp, "none")).returncode, 2)
        self.assertNotEqual(run_cli("routines").returncode, 0)  # --project is required


class FirstMatch(unittest.TestCase):
    """Calls are claimed by their first probe (exclusive) and also counted under every probe (any); repeats
    of one normalised command count once as distinct; the tp-agent and git fetch probes are split out."""

    @staticmethod
    def mine_calls(calls):
        """Bash calls [(command, result text)] of one coder task, mined from a Claude-format transcript."""
        with tempfile.TemporaryDirectory() as tmp:
            lines = []
            for i, (cmd, result) in enumerate(calls):
                lines.append(json.dumps({"message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "u%d" % i, "name": "Bash", "input": {"command": cmd}}]}}))
                lines.append(json.dumps({"message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "u%d" % i, "content": result}]}}))
            transcript = Path(tmp) / "t.jsonl"
            transcript.write_text("\n".join(lines) + "\n")
            return routines.mine([({"id": "t1", "role": "coder"}, str(transcript))])["coder"]

    def test_a_call_matching_two_probes_counts_its_tokens_once_exclusive_and_twice_any(self):
        probes = self.mine_calls([("gitleaks detect --source .", "x" * 400)])["probes"]  # 400 bytes = 100 tokens
        self.assertEqual(set(probes), {GITLEAKS, BUNDLE})  # the gitleaks probe and the bundle that contains it
        self.assertEqual(probes[GITLEAKS]["result_tokens"] + probes[BUNDLE]["result_tokens"], 200)  # any: twice
        self.assertEqual(probes[GITLEAKS]["exclusive"]["result_tokens"], 100)  # first match claims it
        self.assertEqual(probes[BUNDLE]["exclusive"]["result_tokens"], 0)  # and nothing is left for the bundle

    def test_identical_normalised_commands_in_one_task_count_once_as_distinct(self):
        same = self.mine_calls([("sed -n 1,40p x", "a" * 8), ("sed -n 1,40p x", "a" * 8)])["leads"]["sed -n"]
        self.assertEqual((same["calls"], same["distinct"], same["tasks"]), (2, 1, 1))
        different = self.mine_calls([("sed -n 1,40p x", "a" * 8), ("sed -n 1,40p y", "a" * 8)])["leads"]["sed -n"]
        self.assertEqual((different["calls"], different["distinct"]), (2, 2))

    def test_tp_agent_help_and_request_are_different_probes_and_git_fetch_is_not_lane_base(self):
        probes = lambda cmd: routines.keys_for({"name": "Bash", "bytes": 0, "input": {"command": cmd}})[0]
        help_probes, request_probes = probes("tp-agent --help"), probes("tp-agent request --kind review --file x")
        self.assertEqual(routines.first_probe(help_probes), "tp-agent --help/-h/help")
        self.assertEqual(routines.first_probe(request_probes), "tp-agent request")
        self.assertIn("tp-agent (any)", help_probes)  # the general probe still sees every tp-agent call
        self.assertNotIn("tp-agent request", help_probes)
        self.assertNotIn("tp-agent --help/-h/help", request_probes)
        self.assertIn("git fetch", probes("git fetch origin"))
        self.assertNotIn(LANE_BASE, probes("git fetch origin"))
        self.assertIn(LANE_BASE, probes("git merge-base HEAD origin/develop"))
        # a lookup of a tool is the lookup probe, not the tool's own probe
        self.assertEqual(routines.first_probe(probes("which prettier")), "node_modules/.bin lookup (ls/which)")

    @staticmethod
    def fetch_probes(cmd):
        return routines.keys_for({"name": "Bash", "bytes": 0, "input": {"command": cmd}})[0]

    def test_git_fetch_is_found_by_parsing_git_options_not_by_matching_text(self):
        for cmd in ("git fetch", "git fetch origin/develop", "git -C x fetch o/develop",
                    "git -C 'a b' fetch \"origin/develop\"", "git -c protocol.version=2 fetch origin/develop",
                    "git -C x -c a=b fetch --prune origin/DMP-1", "git --no-pager fetch o/develop",
                    "git -p fetch o/develop", "git --git-dir=.git fetch o/develop",
                    "git --git-dir .git --work-tree . fetch o/develop", "git --namespace=n fetch o/develop",
                    "git --bare fetch o/develop", "git --exec-path=/x fetch o/develop",
                    "git --config-env=a.b=ENV fetch o/develop", "FOO=1 git fetch o/develop",
                    "/usr/bin/git fetch o/develop", "cd x && git fetch origin/develop", "git fetch o/develop | tail -3"):
            found = self.fetch_probes(cmd)
            self.assertEqual(routines.first_probe(found), "git fetch", cmd)
            self.assertNotIn(LANE_BASE, found, cmd)
        self.assertEqual(self.fetch_probes("git merge-base HEAD origin/develop") & {LANE_BASE, "git fetch"}, {LANE_BASE})
        self.assertNotIn("git fetch", self.fetch_probes("git log origin/develop"))
        self.assertEqual(self.fetch_probes("git -c a=b merge-base HEAD origin/develop") & {LANE_BASE, "git fetch"}, {LANE_BASE})
        both = self.fetch_probes("git fetch origin && git merge-base HEAD origin/develop")  # the merge-base still counts
        self.assertLessEqual({LANE_BASE, "git fetch"}, both)
        self.assertNotIn("git fetch", self.fetch_probes('echo "git fetch origin/develop"'))
        self.fetch_probes("git fetch 'unterminated")  # no exception

    def test_every_value_taking_git_global_option_is_skipped_before_the_subcommand(self):
        # the value-taking global options of `git --help`: -C and -c take the next token; the long ones take
        # `=value` or the next token (--exec-path only as `=value`; alone it is a flag)
        for opt in ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env", "--exec-path"):
            forms = [] if opt in ("-C", "-c") else ["git %s=v fetch o/develop" % opt]
            if opt != "--exec-path":
                forms.append("git %s v fetch o/develop" % opt)
            for cmd in forms:
                self.assertEqual([s for s, _ in routines.git_subcommands(cmd)], ["fetch"], cmd)
                self.assertNotIn(LANE_BASE, self.fetch_probes(cmd), cmd)

    def test_growth_and_drop_are_judged_on_the_share_of_the_role_tasks(self):
        def stat(tasks):
            return {"tasks": tasks, "calls": tasks, "distinct": tasks, "result_tokens": 0, "exclusive": {"calls": 0, "result_tokens": 0}}
        before = {"roles": {"coder": {"tasks": 10, "probes": {"X": 5}, "leads": {}}}}
        # raw count falls 5 -> 3, but the role's tasks fall 10 -> 4: the share rises 50% -> 75%
        lists = routines.compare(before, {"coder": {"tasks": 4, "probes": {"X": stat(3)}, "leads": {}}}, 3)
        self.assertEqual([e["probe"] for e in lists["grew"]], ["X"])
        self.assertEqual(lists["dropped"], [])
        # raw count rises 5 -> 6, but the role's tasks rise 10 -> 20: the share falls 50% -> 30%
        lists = routines.compare(before, {"coder": {"tasks": 20, "probes": {"X": stat(6)}, "leads": {}}}, 3)
        self.assertEqual(lists["grew"], [])
        self.assertEqual([e["probe"] for e in lists["dropped"]], ["X"])

    def test_a_dropped_probe_shows_its_token_columns_in_the_text_report(self):
        stat = {"tasks": 1, "calls": 7, "distinct": 5, "result_tokens": 300, "exclusive": {"calls": 4, "result_tokens": 120}}
        before = {"roles": {"coder": {"tasks": 4, "probes": {"X": 3}, "leads": {}}}}
        roles = {"coder": {"tasks": 4, "calls": 9, "probes": {"X": stat}, "leads": {}}}
        report = routines.build_report(roles, before, 3, now="2026-11-07T00:00:00Z")
        entry = report["dropped"][0]
        self.assertEqual((entry["calls"], entry["distinct"], entry["result_tokens"], entry["exclusive_result_tokens"]), (7, 5, 300, 120))
        line = [l for l in routines.render_text(report).splitlines() if l.rstrip().endswith(" X") and "75.0%" in l][0]
        self.assertRegex(line, r"calls\s+7 distinct\s+5 tok\s+300 any\s+120 excl")


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


class ProjectOverrideTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, ".tmux-pi"))
        self.path = os.path.join(self.dir, ".tmux-pi", "routines.json")

    def write(self, doc):
        with open(self.path, "w") as fh:
            json.dump(doc, fh)

    def test_no_override_is_the_shared_config(self):
        lane, probes, paths = routines.load_config(project=self.dir)
        self.assertEqual((list(probes), paths), (list(routines.PROBES), routines.PATH_PROBES))

    def test_override_replaces_in_place_and_new_names_go_first(self):
        self.write({"lane_pattern": r"\bxyz-\d+", "probes": {"gitleaks run": "GL", "mine": "mine"}, "path_probes": {"Read: x": "x"}})
        lane, probes, paths = routines.load_config(project=self.dir)
        names = list(routines.PROBES)
        self.assertEqual(list(probes), ["mine"] + names)
        self.assertEqual(probes["gitleaks run"], "GL")
        self.assertEqual(list(paths)[0], "Read: x")
        self.assertEqual(lane.pattern, r"\bxyz-\d+")

    def test_bad_override_names_the_file(self):
        self.write({"probes": {"a": "("}})
        with self.assertRaisesRegex(ValueError, "routines.json"):
            routines.load_config(project=self.dir)

    def test_tables_are_restored_after_use(self):
        before = (routines.LANE, routines.PROBES, routines.PROBE_ORDER)
        self.write({"probes": {"mine": "mine"}})
        saved = routines.use_project_config(self.dir)
        self.assertIn("mine", routines.PROBES)
        routines.restore_config(saved)
        self.assertEqual((routines.LANE, routines.PROBES, routines.PROBE_ORDER), before)
