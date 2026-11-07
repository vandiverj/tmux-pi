"""lib/gate_record.py: load, validate, status (fresh, stale, invalid) and missing_required. The records the scripts write are
covered in test_agent_tools.py; here the records are built by hand."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import gate_record  # noqa: E402

GIT_ENV = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null", GIT_AUTHOR_NAME="t",
               GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")

GATE = {"schema": "tp-gate:v1", "worktree": "/w", "base": {"ref": "main", "sha": "abc"}, "head": "h", "tree": "t", "files": ["a"],
        "checks": [{"name": "prettier", "status": "passed", "exit": 0, "seconds": 1},
                   {"name": "typecheck:web", "status": "missing", "exit": None, "seconds": 0},
                   {"name": "gitleaks", "status": "missing", "exit": None, "seconds": 0},
                   {"name": "eslint", "status": "not_applicable", "exit": None, "seconds": 0}],
        "exit": 0, "started": "s", "finished": "f", "log": "/l"}
RUN = {"schema": "tp-test-run:v1", "label": "", "cmd": ["true"], "cwd": "/w", "head": "h", "tree": "t", "exit": 0,
       "timed_out": False, "seconds": 0.1, "log": "/l", "summary": ""}


class RecordTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.tmp / "repo"), *args], env=GIT_ENV, capture_output=True, text=True, check=True).stdout.strip()

    def repo(self):
        (self.tmp / "repo").mkdir()
        self.git("init", "-q", "-b", "main")
        (self.tmp / "repo" / "a.txt").write_text("one\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "first")
        return self.tmp / "repo"

    def test_load_reads_an_object_and_refuses_anything_else(self):
        path = self.tmp / "r.json"
        path.write_text(json.dumps(GATE))
        self.assertEqual(gate_record.load(path), GATE)
        for text in ("{not json", "[1]"):
            path.write_text(text)
            with self.assertRaises(ValueError):
                gate_record.load(path)

    def test_validate_names_the_schema_and_the_missing_keys(self):
        self.assertEqual(gate_record.validate(GATE), [])
        self.assertEqual(gate_record.validate(RUN), [])
        self.assertEqual(gate_record.validate([]), ["not an object"])
        self.assertIn("unknown schema: 'tp-gate:v2'", gate_record.validate(dict(GATE, schema="tp-gate:v2")))
        for record in (GATE, RUN):
            for key in [k for k in record if k != "schema"]:
                with self.subTest(schema=record["schema"], key=key):
                    broken = {k: v for k, v in record.items() if k != key}
                    self.assertEqual(gate_record.validate(broken), ["missing key: " + key])
        bad = copy.deepcopy(GATE)
        bad["checks"][0]["status"] = "skipped"
        self.assertEqual(gate_record.validate(bad), ["check prettier has status 'skipped'"])

    def test_a_record_missing_or_mistyping_a_required_field_is_invalid_and_never_fresh(self):
        repo = self.repo()
        head, tree = gate_record.fingerprint(repo)
        ran = {"name": "c", "status": "passed", "exit": 0, "seconds": 1.5}
        not_ran = {"name": "c", "status": "missing", "exit": None, "seconds": 0}
        good = {"passed": ran, "failed": dict(ran, status="failed", exit=1), "missing": not_ran,
                "not_applicable": dict(not_ran, status="not_applicable")}
        self.assertEqual(set(good), set(gate_record.CHECK_STATUSES))
        for state, check in good.items():
            record = dict(GATE, head=head, tree=tree, checks=[check])
            self.assertEqual(gate_record.status(record, repo), "fresh", state)
            for key in gate_record.CHECK_FIELDS[state]:  # a wrong type for each field is the generated test below
                with self.subTest(status=state, dropped=key):
                    broken = {k: v for k, v in check.items() if k != key}
                    self.assertEqual(gate_record.status(dict(record, checks=[broken]), repo), "invalid")
        # a check that ran needs an int exit (not null, not a bool); one that did not run has exit null
        for state, exit_code in (("passed", None), ("failed", True), ("missing", 0), ("not_applicable", 1)):
            with self.subTest(status=state, exit=exit_code):
                record = dict(GATE, head=head, tree=tree, checks=[dict(good[state], exit=exit_code)])
                self.assertEqual(gate_record.status(record, repo), "invalid")
        # the top-level fields of both schemas: dropped -> invalid
        for base in (dict(GATE, head=head, tree=tree), dict(RUN, head=head, tree=tree)):
            for key in gate_record.RECORD_FIELDS[base["schema"]]:
                if key in ("head", "tree"):
                    continue
                with self.subTest(schema=base["schema"], dropped=key):
                    self.assertEqual(gate_record.status({k: v for k, v in base.items() if k != key}, repo), "invalid")

    def test_every_field_set_to_every_json_type_is_valid_only_for_its_own_type_and_status_never_raises(self):
        samples = {"list": [1], "dict": {"x": 1}, "number": 7, "null": None, "bool": True, "string": "zz"}
        string, number = {"string"}, {"number"}
        # field -> the sample kinds that keep the record valid (written out here, not read from the table under test)
        gate = {"schema": set(), "worktree": string, "base": set(), "head": string, "tree": string, "files": set(),
                "checks": set(), "exit": number, "started": string, "finished": string, "log": string}
        run = {"schema": set(), "label": string, "cmd": set(), "cwd": string, "head": string, "tree": string, "exit": number,
               "timed_out": {"bool"}, "seconds": number, "log": string, "summary": string}
        ran = {"name": string, "status": set(), "exit": number, "seconds": number}
        not_ran = dict(ran, exit={"null"})
        cases = [(GATE, gate, lambda r, k, v: dict(r, **{k: v}))] + [
            (RUN, run, lambda r, k, v: dict(r, **{k: v}))]
        for state, rules in (("passed", ran), ("failed", ran), ("missing", not_ran), ("not_applicable", not_ran)):
            check = {"name": "c", "status": state, "exit": 0 if rules is ran else None, "seconds": 1}
            cases.append((dict(GATE, checks=[check]), rules,
                          lambda r, k, v: dict(r, checks=[dict(r["checks"][0], **{k: v})])))
        for record, rules, put in cases:
            self.assertEqual(gate_record.validate(record), [])
            self.assertEqual(set(rules), set(k for k in (record["checks"][0] if "status" in rules else record)))
            for field, valid in rules.items():
                for kind, value in samples.items():
                    with self.subTest(schema=record["schema"], field=field, kind=kind):
                        changed = put(record, field, value)
                        if kind in valid:
                            self.assertEqual(gate_record.validate(changed), [])
                        else:
                            self.assertNotEqual(gate_record.validate(changed), [])
                            self.assertEqual(gate_record.status(changed, self.tmp / "nowhere"), "invalid")
        # the elements of the compound fields
        for label, changed in {"base.ref": dict(GATE, base={"ref": 1, "sha": "a"}), "base.sha": dict(GATE, base={"ref": "m", "sha": None}),
                               "files item": dict(GATE, files=[1]), "cmd item": dict(RUN, cmd=[None]),
                               "check item": dict(GATE, checks=[5]), "check name": dict(GATE, checks=[{"name": [], "status": "passed"}]),
                               "unhashable status": dict(GATE, checks=[{"name": "c", "status": []}]),
                               "unhashable schema": {"schema": []}, "dict schema": {"schema": {}}}.items():
            with self.subTest(case=label):
                self.assertNotEqual(gate_record.validate(changed), [])
                self.assertEqual(gate_record.status(changed, self.tmp / "nowhere"), "invalid")
                self.assertEqual(gate_record.missing_required(changed, ["c"]), [])

    def test_missing_required_matches_the_name_or_name_colon_prefix_and_only_missing_checks(self):
        self.assertEqual(gate_record.missing_required(GATE, ["gitleaks"]), ["gitleaks"])
        self.assertEqual(gate_record.missing_required(GATE, ["typecheck"]), ["typecheck:web"])
        self.assertEqual(gate_record.missing_required(GATE, ["prettier", "eslint", "git"]), [])  # passed, not_applicable, no prefix match
        self.assertEqual(gate_record.missing_required(GATE, []), [])
        self.assertEqual(gate_record.missing_required(RUN, ["gitleaks"]), [])

    def test_status_is_fresh_until_any_content_or_head_change_and_invalid_for_a_bad_record(self):
        repo = self.repo()
        head, tree = gate_record.fingerprint(repo)
        record = dict(GATE, head=head, tree=tree)
        self.assertEqual(gate_record.status(record, repo), "fresh")
        self.assertEqual(gate_record.status(dict(RUN, head=head, tree=tree), repo), "fresh")
        (repo / "a.txt").write_text("two\n")  # tracked change
        self.assertEqual(gate_record.status(record, repo), "stale")
        (repo / "a.txt").write_text("one\n")
        self.assertEqual(gate_record.status(record, repo), "fresh")
        (repo / "new.txt").write_text("x\n")  # untracked file
        self.assertEqual(gate_record.status(record, repo), "stale")
        (repo / "new.txt").unlink()
        (repo / "a.txt").unlink()  # tracked deletion
        self.assertEqual(gate_record.status(record, repo), "stale")
        (repo / "a.txt").write_text("one\n")
        self.git("commit", "-q", "--allow-empty", "-m", "same content, new head")
        self.assertEqual(gate_record.fingerprint(repo)[1], tree)
        self.assertEqual(gate_record.status(record, repo), "stale")
        self.assertEqual(gate_record.status({"schema": "tp-gate:v1"}, repo), "invalid")
        self.assertEqual(gate_record.status(record, self.tmp / "nowhere"), "stale")


if __name__ == "__main__":
    unittest.main()
