"""PD-D: the product design workflow resumes from the session objective and the package manifest, and the prepare-cases
reference's case changeset is one the package accepts. In-process (tp-agent through tests/tp_agent_inproc.py, tp-product
through its main); a scratch TP_STATE_DIR per session, no tmux, no model, no network."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import argparse
import contextlib
import io
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))
from product_design import cli  # noqa: E402
import tp_agent_inproc  # noqa: E402

FIXTURE = ROOT / "templates/product-design/examples/review-queue"
SKILL = ROOT / "catalog/skills/product-design/SKILL.md"
CASE_SHAPE = ROOT / "catalog/skills/prepare-cases/references/case-shape.md"
# The objective line the product-design skill tells Main to record (its "Persist on every meaningful pause" section).
OBJECTIVE = re.compile(r"product-design package=(?P<package>\S+) revision=(?P<revision>\d+) slice=(?P<slice>\S+) next: (?P<next>.+)")


def product(*argv):
    out, err = io.StringIO(), io.StringIO()
    rc = cli.main(list(argv), out=out, err=err)
    return rc, out.getvalue(), err.getvalue()


def status(pkg):
    rc, out, err = product("status", str(pkg), "--json")
    if rc != 0:
        raise AssertionError(f"status exited {rc}: {out}{err}")
    return json.loads(out)


class ResumeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.pkg = self.base / "workspace" / "product-design" / "review-queue-demo"
        shutil.copytree(FIXTURE, self.pkg)

    def agent(self, state, *argv):
        state.mkdir(exist_ok=True)
        env = session_env.outside_session()
        # a scratch session state, work store and work config: never the user's (~/.config/tmux-pi/work)
        (self.base / "work-config.json").write_text("{}")
        env.update(TP_STATE_DIR=str(state), TP_WORK_DIR=str(self.base / "work"),
                   TP_WORK_CONFIG=str(self.base / "work-config.json"), TP_WORKDIR=str(self.base / "workspace"))
        rc, out, err = tp_agent_inproc.run(list(argv), env=env)
        self.assertEqual(rc, 0, err)
        return out

    def fresh_main_reads_objective(self, state):
        """What a new main process in the session sees first: the index text (the SessionStart hook's), a pure read."""
        text = self.agent(state, "index", "--no-write", "--text")
        line = next(l for l in text.splitlines() if l.startswith("objective: "))[len("objective: "):]
        return OBJECTIVE.fullmatch(line), line

    def pause(self, state, slice_id):
        rep = status(self.pkg)
        line = (f"product-design package={self.pkg} revision={rep['revision']} slice={slice_id} "
                f"next: {rep['next_action']['text']}")
        self.agent(state, "objective", line)
        return rep

    def test_the_skill_prescribes_the_objective_this_test_reads(self):
        self.assertIn('tp-agent objective "product-design package=<absolute DIR> revision=<N> slice=<slice id or none> '
                      'next: <next action text>"', SKILL.read_text().replace("\n  ", " "))

    def test_a_fresh_main_in_the_same_session_recovers_package_revision_slice_and_next_action(self):
        state = self.base / "state-a"
        saved = self.pause(state, "slice.reassignment")
        got, _ = self.fresh_main_reads_objective(state)
        self.assertIsNotNone(got)
        self.assertEqual(Path(got["package"]), self.pkg)
        live = status(Path(got["package"]))  # the manifest is the record; the objective only points to it
        self.assertEqual(int(got["revision"]), live["revision"])
        self.assertEqual(got["next"], live["next_action"]["text"])
        self.assertIn(got["slice"], live["slices"])
        self.assertEqual(live["slices"][got["slice"]]["effective"], "blocked")
        self.assertEqual(live, saved)
        self.assertTrue(json.loads((state / "session-index.json").read_text())["objective"].startswith("product-design package="))

    def test_the_manifest_wins_over_an_older_objective(self):
        state = self.base / "state-a"
        self.pause(state, "slice.reassignment")
        change = self.base / "next.json"
        new_next = {"text": "Re-review slice.reassignment with the product owner.", "owner": "Product owner (synthetic)",
                    "set": "2026-11-10"}
        change.write_text(json.dumps({"schema": "tp-product-changeset", "schema_version": 1, "actor": "Main",
                                      "summary": "pause", "operations": [{"op": "set-manifest", "fields": {"next_action": new_next}}]}))
        rc, out, err = product("apply", str(self.pkg), str(change), "--expected-revision", "3")
        self.assertEqual(rc, 0, out + err)
        got, _ = self.fresh_main_reads_objective(state)
        live = status(Path(got["package"]))
        self.assertEqual((int(got["revision"]), live["revision"]), (3, 4))  # the objective is behind: the status is believed
        self.assertEqual(live["next_action"], new_next)
        self.assertNotEqual(got["next"], live["next_action"]["text"])

    def test_a_new_session_with_only_the_package_path_resumes_from_the_manifest(self):
        self.pause(self.base / "state-a", "slice.reassignment")
        got, line = self.fresh_main_reads_objective(self.base / "state-b")  # another tmux session: its own, empty state
        self.assertIsNone(got)
        self.assertEqual(line, "(none set)")
        live = status(self.pkg)
        manifest = json.loads((self.pkg / "package.json").read_text())
        self.assertEqual((live["revision"], live["activity"], live["next_action"]),
                         (manifest["revision"], manifest["activity"], manifest["next_action"]))
        self.assertTrue(live["trusted"])

    def test_a_long_next_action_keeps_the_package_path_in_the_index(self):
        state = self.base / "state-a"
        self.agent(state, "objective", f"product-design package={self.pkg} revision=3 slice=none next: " + "x" * 2000)
        _, line = self.fresh_main_reads_objective(state)
        self.assertTrue(line.startswith(f"product-design package={self.pkg} revision=3 slice=none next: "), line[:200])


class CaseShapeReferenceTests(unittest.TestCase):
    def test_the_reference_case_changeset_is_accepted_by_the_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "pkg"
            shutil.copytree(FIXTURE, pkg)
            rep = status(pkg)
            block = re.search(r"```json\n(.*?)```", CASE_SHAPE.read_text(), re.S).group(1)
            fp = rep["slices"]["slice.submit-and-queue"]["fingerprint"]
            change = json.loads(block.replace("sha256:<slices.slice.submit-and-queue.fingerprint>", fp))
            self.assertEqual(change["operations"][0]["record"]["source"]["package_revision"], rep["revision"])
            path = Path(tmp) / "cases.json"
            path.write_text(json.dumps(change))
            rc, out, err = product("apply", str(pkg), str(path), "--expected-revision", str(rep["revision"]))
            self.assertEqual(rc, 0, out + err)
            self.assertEqual(product("validate", str(pkg))[0], 0)
            cases = json.loads((pkg / "design.json").read_text())["cases"]
            self.assertEqual([c["id"] for c in cases], ["case.reject-missing-field"])


class RoutineUseDocTests(unittest.TestCase):
    """docs/product-design.md "Routine use": every command it shows exists and is accepted by the command's own parser (the
    one its --help prints), every file it names exists, and the command synopsis lists every tp-product command with only
    the options that command has."""
    DOC = ROOT / "docs/product-design.md"

    def section(self, title):
        text = self.DOC.read_text()
        return text.split(f"\n## {title}\n", 1)[1].split("\n## ", 1)[0]

    def shown_commands(self):
        blocks = re.findall(r"```sh\n(.*?)```", self.section("Routine use"), re.S)
        return [shlex.split(line) for b in blocks for line in b.splitlines() if line.strip()]

    def test_every_routine_command_parses_with_its_own_parser(self):
        shown = self.shown_commands()
        self.assertEqual({c[0] for c in shown}, {"tp-product", "tp-agent", "python3"})
        subs = next(a for a in cli.parser()._actions if isinstance(a, argparse._SubParsersAction)).choices
        used = set()
        for argv in shown:
            with self.subTest(argv=" ".join(argv)):
                if argv[0] == "tp-product":
                    with contextlib.redirect_stderr(io.StringIO()) as err:
                        try:
                            args = cli.parser(argv[1:]).parse_args(argv[1:])
                        except SystemExit:
                            self.fail(err.getvalue())
                    used.add(args.command)
                elif argv[0] == "tp-agent":
                    rc, out, err = tp_agent_inproc.run([argv[1], "--help"], env=session_env.outside_session())
                    self.assertEqual(rc, 0, err)
                    usage = " ".join(out.split("\n\n")[0].split())
                    positionals = [w for w in usage.split()[3:] if not w.startswith(("[", "-")) and not w.isupper()]
                    options = [w for w in argv[2:] if w.startswith("--")]
                    for o in options:
                        self.assertIn(o, out)
                    self.assertEqual(len([w for w in argv[2:] if not w.startswith("--")]), len(positionals), usage)
                else:
                    self.assertEqual(argv[1], "tools/smoke/product_design_demo.py")
                    r = subprocess.run([sys.executable, str(ROOT / argv[1]), "--help"], capture_output=True, text=True,
                                       timeout=30)
                    self.assertEqual(r.returncode, 0, r.stderr)
                    for o in (w for w in argv[2:] if w.startswith("--")):
                        self.assertIn(o, r.stdout)
        # the round uses each command a routine needs
        self.assertLessEqual({"init", "apply", "status", "build", "import-feedback", "remap-anchor", "impact", "cases",
                              "packet"}, used)
        self.assertLessEqual(used, set(subs))

    def test_the_files_and_entry_points_it_names_exist(self):
        text = self.section("Routine use")
        named = set(re.findall(r"`((?:claude|agent|tools|catalog|docs)/[^`\s]+)`", text))
        self.assertGreaterEqual(len(named), 5, named)
        for rel in named:
            self.assertTrue((ROOT / rel).is_file(), rel)
        for marker in ("/product-design", "/prepare-cases"):
            self.assertIn(f"`{marker}`", text)

    def test_the_synopsis_lists_every_command_with_only_its_options(self):
        block = re.search(r"```\n(tp-product init.*?)```", self.section("The `tp-product` command"), re.S).group(1)
        subs = next(a for a in cli.parser()._actions if isinstance(a, argparse._SubParsersAction)).choices
        listed = set()
        for line in block.splitlines():
            if not line.startswith("tp-product "):
                continue
            name = line.split()[1]
            listed.add(name)
            self.assertIn(name, subs)
            have = {o for a in subs[name]._actions for o in a.option_strings}
            self.assertLessEqual(set(re.findall(r"--[a-z-]+", line)), have, line)
        self.assertEqual(listed, set(subs))


if __name__ == "__main__":
    unittest.main()
