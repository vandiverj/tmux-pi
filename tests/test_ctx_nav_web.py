"""ctx-nav-web (CX2): the TypeScript navigator. A 6-file fixture project (a `paths` alias, a project reference, a re-export) is copied into a
temp directory per test class; the real `typescript` is linked in from the machine's airtera-monorepo checkout (or $TP_CTX_TEST_TYPESCRIPT).
Tests that need typescript skip with that reason when it is missing; the protocol, restart and guard tests need only node. Nothing here
touches the network, a model, or the real monorepo (the link is read-only)."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
from ctxengine import nav_web  # noqa: E402
from ctxengine.nav_web import NavWeb, find_tsconfig  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "ctx_web"
PROJECT = FIXTURES / "project"
NODE = shutil.which("node")
AWS = "AKIA" + "IOSFODNN7EXAMPLE"  # assembled so this file holds no key-shaped literal


def find_typescript():
    for candidate in (os.environ.get("TP_CTX_TEST_TYPESCRIPT"),
                      Path.home() / "Documents" / "Projects" / "airtera-monorepo" / "node_modules" / "typescript"):
        if candidate and (Path(candidate) / "lib" / "typescript.js").is_file():
            return Path(candidate)
    return None


TYPESCRIPT = find_typescript()
needs_node = unittest.skipUnless(NODE, "node is not installed")
needs_ts = unittest.skipUnless(NODE and TYPESCRIPT, "node, or a typescript package (set TP_CTX_TEST_TYPESCRIPT), is not available")


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_project(base, with_typescript=True):
    proj = Path(os.path.realpath(base)) / "proj"
    shutil.copytree(PROJECT, proj)
    if with_typescript:
        (proj / "node_modules").mkdir()
        os.symlink(TYPESCRIPT, proj / "node_modules" / "typescript")
    return proj


class TempCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))

    def nav(self, root, tsconfig="tsconfig.json", **kwargs):
        nav = NavWeb(root, tsconfig, **kwargs)
        self.addCleanup(nav.stop)
        return nav


class RecordingGuard(nav_web.StubGuard):
    """The real guard plus a recording of every path the helper asked about, a folder it refuses (`hide`), and a failing mode."""

    def __init__(self, hide=None, fail=False):
        super().__init__()
        self.hide, self.fail, self.asked = hide, fail, []

    def check_path(self, path, root):
        self.asked.append(path)
        if self.hide and self.hide in path.replace(os.sep, "/"):
            if self.fail:
                raise RuntimeError("guard broke")
            return "test_hidden"
        return super().check_path(path, root)


def spots(rows, key="path"):
    return [f"{r[key]}:{r['line']}" for r in rows]


@needs_ts
class Navigation(unittest.TestCase):
    """Read-only questions against one shared helper."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.proj = make_project(cls._tmp.name)
        cls.nav_ = NavWeb(cls.proj, "tsconfig.json")

    @classmethod
    def tearDownClass(cls):
        cls.nav_.stop()
        cls._tmp.cleanup()

    def test_definition_through_paths_alias(self):
        got = self.nav_.definition("src/main.ts", name="greet", line=7)
        self.assertEqual(got["status"], "ok")
        self.assertEqual(spots(got["definitions"]), ["src/util.ts:2"])
        self.assertEqual(got["definitions"][0]["kind"], "function")
        self.assertIn("greet(name: string): string", got["signature"])
        self.assertEqual(got["doc"], "Greets a person.")

    def test_definition_through_project_reference_lands_on_source_not_declaration_file(self):
        got = self.nav_.definition("src/main.ts", name="makeThing", line=6)
        self.assertEqual(spots(got["definitions"]), ["lib/thing.ts:5"])
        got = self.nav_.definition("src/main.ts", line=6, column=15)  # the `Thing` annotation, by position
        self.assertEqual(spots(got["definitions"]), ["lib/thing.ts:1"])

    def test_definition_follows_a_re_export_chain_to_the_original(self):
        got = self.nav_.definition("src/main.ts", name="fmt", line=7)
        self.assertEqual(spots(got["definitions"]), ["src/format.ts:1"])
        got = self.nav_.definition("src/barrel.ts", name="fmt")  # the export specifier itself
        self.assertEqual(spots(got["definitions"]), ["src/format.ts:1"])

    def test_definition_by_name_alone_prefers_the_declaration(self):
        got = self.nav_.definition("src/util.ts", name="VERSION")
        self.assertEqual(got["at"], {"line": 6, "column": 14})
        self.assertEqual(spots(got["definitions"]), ["src/util.ts:6"])

    def test_references_cross_alias_re_export_and_project_reference(self):
        got = self.nav_.references("src/format.ts", name="fmt")
        self.assertEqual(spots(got["references"]), ["src/barrel.ts:1", "src/format.ts:1", "src/main.ts:2", "src/main.ts:7"])
        self.assertEqual([r["name"] for r in got["definitions"]], ["fmt", "fmt", "fmt"])
        self.assertEqual((got["total"], got["truncated"]), (4, False))
        got = self.nav_.references("src/main.ts", name="makeThing", line=6)
        self.assertEqual(spots(got["references"]), ["lib/thing.ts:5", "src/main.ts:3", "src/main.ts:6"])
        self.assertEqual(got["references"][1]["text"], 'import { makeThing, type Thing } from "@lib/thing";')

    def test_references_limit_keeps_the_total(self):
        got = self.nav_.references("src/format.ts", name="fmt", limit=2)
        self.assertEqual((len(got["references"]), got["total"], got["truncated"]), (2, 4, True))

    def test_exports_lists_local_and_re_exports_with_where_they_are_defined(self):
        got = self.nav_.exports("src/barrel.ts")
        self.assertEqual([(e["name"], e["kind"], e["line"], e["reexport"], e["defined_at"]) for e in got["exports"]], [
            ("fmt", "function", 1, True, "src/format.ts:1"),
            ("VERSION", "variable", None, True, "src/util.ts:6"),
            ("greet", "function", None, True, "src/util.ts:2"),
        ])
        got = self.nav_.exports("src/util.ts")
        self.assertEqual([(e["name"], e["line"], e["reexport"]) for e in got["exports"]], [("greet", 2, False), ("VERSION", 6, False)])
        self.assertEqual(self.nav_.exports("src/main.ts")["exports"][0]["name"], "message")

    def test_imports_resolve_alias_reference_dynamic_and_missing_packages(self):
        got = self.nav_.imports("src/main.ts")
        self.assertEqual([(e["line"], e["specifier"], e["resolved"], e["external"]) for e in got["edges"]], [
            (1, "@/util", "src/util.ts", False),
            (2, "@/barrel", "src/barrel.ts", False),
            (3, "@lib/thing", "lib/thing.ts", False),
            (4, "missing-package", None, True),
            (10, "./format", "src/format.ts", False),
        ])
        self.assertEqual(got["unresolved"], 1)

    def test_importers_direct_and_two_levels(self):
        got = self.nav_.imports("src/format.ts", direction="importers")
        self.assertEqual([(e["from"], e["line"], e["depth"]) for e in got["edges"]], [("src/barrel.ts", 1, 1), ("src/main.ts", 10, 1)])
        got = self.nav_.imports("src/format.ts", direction="importers", depth=2)
        self.assertEqual([(e["from"], e["to"], e["line"], e["depth"]) for e in got["edges"]], [
            ("src/barrel.ts", "src/format.ts", 1, 1), ("src/main.ts", "src/format.ts", 10, 1), ("src/main.ts", "src/barrel.ts", 2, 2)])
        self.assertEqual(self.nav_.imports("src/main.ts", direction="importers")["edges"], [])

    def test_answers_are_identical_across_calls_and_carry_the_index_age(self):
        a = self.nav_.references("src/format.ts", name="fmt")
        b = self.nav_.references("src/format.ts", name="fmt")
        for key in ("server_ms", "index_age_s"):
            a.pop(key), b.pop(key)
        self.assertEqual(a, b)
        self.assertIn("index_age_s", self.nav_.exports("src/util.ts"))

    def test_stats_report_the_program(self):
        got = self.nav_.stats()
        self.assertEqual((got["status"], got["typescript"], got["tsconfig"]), ("ok", TYPESCRIPT_VERSION, "tsconfig.json"))
        self.assertEqual(got["root_files"], 4)

    def test_bad_arguments_say_what_to_do(self):
        for call in (lambda: self.nav_.definition("src/main.ts"),
                     lambda: self.nav_.definition("src/main.ts", line=999, column=1),
                     lambda: self.nav_.imports("src/main.ts", direction="sideways"),
                     lambda: self.nav_.request("bogus"),
                     lambda: self.nav_.request("definition", name="x")):
            got = call()
            self.assertEqual(got["status"], "bad_request", got)
            self.assertTrue(got["next"])
        self.assertEqual(self.nav_.definition("src/main.ts", name="nothere")["status"], "not_found")
        self.assertEqual(self.nav_.definition("src/missing.ts", name="x")["status"], "not_found")

    def test_a_file_outside_the_tsconfig_is_not_in_project(self):
        write(self.proj / "scripts" / "tool.ts", "export const tool = 1;\n")
        self.addCleanup(lambda: shutil.rmtree(self.proj / "scripts"))
        got = self.nav_.exports("scripts/tool.ts")
        self.assertEqual(got["status"], "not_in_project")
        self.assertTrue(got["next"])


def _typescript_version():
    return json.loads((TYPESCRIPT / "package.json").read_text())["version"] if TYPESCRIPT else ""


TYPESCRIPT_VERSION = _typescript_version()


@needs_ts
class Freshness(TempCase):
    def test_an_edited_file_changes_the_answer_without_a_restart(self):
        proj = make_project(self.tmp)
        nav = self.nav(proj)
        self.assertEqual(spots(nav.definition("src/main.ts", name="greet", line=7)["definitions"]), ["src/util.ts:2"])
        pid, builds = nav.pid, nav.stats()["program_builds"]
        util = proj / "src" / "util.ts"
        util.write_text("// one\n// two\n" + util.read_text())
        got = nav.definition("src/main.ts", name="greet", line=7)
        self.assertEqual(spots(got["definitions"]), ["src/util.ts:4"])
        self.assertEqual(nav.pid, pid)
        self.assertEqual((nav.restarts, nav.stats()["program_builds"] > builds), (0, True))
        self.assertEqual(spots(nav.references("src/util.ts", name="greet")["references"]), ["src/main.ts:1", "src/main.ts:7", "src/util.ts:4"])

    def test_an_unchanged_project_keeps_its_program_when_a_package_is_installed_twice(self):
        # TypeScript gives two copies of one package@version a single source file and a single version, and asks for that version
        # under both names. A version taken from the mtime differs between the copies, so every call used to build a new program.
        proj = make_project(self.tmp)
        pkg = '{"name": "dup", "version": "1.0.0", "types": "index.d.ts"}\n'
        for where in ("dup", "holder/node_modules/dup"):
            write(proj / "node_modules" / where / "package.json", pkg)
            write(proj / "node_modules" / where / "index.d.ts", "export declare const twin: number;\n")
            os.utime(proj / "node_modules" / where / "index.d.ts", (1_700_000_000 + len(where), 1_700_000_000 + len(where)))
        write(proj / "node_modules" / "holder" / "package.json", '{"name": "holder", "version": "1.0.0", "types": "index.d.ts"}\n')
        write(proj / "node_modules" / "holder" / "index.d.ts", 'export { twin } from "dup";\n')
        write(proj / "src" / "uses.ts", 'import { twin } from "dup";\nimport { twin as again } from "holder";\nexport const both = twin + again;\n')
        nav = self.nav(proj)
        self.assertEqual(nav.exports("src/uses.ts")["status"], "ok")
        builds = nav.stats()["program_builds"]
        for _ in range(3):
            self.assertEqual(nav.exports("src/uses.ts")["status"], "ok")
            self.assertEqual(nav.imports("src/uses.ts")["status"], "ok")
        self.assertEqual(nav.stats()["program_builds"], builds)

    def test_a_new_import_and_a_new_file_are_seen_without_a_restart(self):
        proj = make_project(self.tmp)
        nav = self.nav(proj)
        self.assertEqual(nav.imports("src/format.ts", direction="importers")["total"], 2)
        pid = nav.pid
        write(proj / "src" / "extra.ts", 'import { fmt } from "./format";\nexport const extra = fmt(1);\n')
        got = nav.imports("src/format.ts", direction="importers")
        self.assertEqual([(e["from"], e["line"]) for e in got["edges"]], [("src/barrel.ts", 1), ("src/extra.ts", 1), ("src/main.ts", 10)])
        self.assertEqual(spots(nav.definition("src/extra.ts", name="fmt", line=1)["definitions"]), ["src/format.ts:1"])
        self.assertEqual(nav.pid, pid)

    def test_a_deleted_import_target_becomes_unresolved(self):
        proj = make_project(self.tmp)
        nav = self.nav(proj)
        self.assertEqual(nav.imports("src/barrel.ts")["unresolved"], 0)
        (proj / "src" / "format.ts").unlink()
        self.assertEqual(nav.imports("src/barrel.ts")["unresolved"], 1)

    def test_a_file_that_appears_where_an_earlier_candidate_won_changes_the_edge(self):
        proj = make_project(self.tmp)
        nav = self.nav(proj)
        (proj / "src" / "format.ts").rename(proj / "src" / "format.tsx")
        got = nav.imports("src/barrel.ts")
        self.assertEqual([e["resolved"] for e in got["edges"]][:1], ["src/format.tsx"])
        write(proj / "src" / "format.ts", "export function fmt(value: number): string { return String(value); }\n")
        got = nav.imports("src/barrel.ts")
        self.assertEqual([e["resolved"] for e in got["edges"]][:1], ["src/format.ts"])
        got = nav.imports("src/format.ts", direction="importers")
        self.assertEqual([(e["from"], e["line"]) for e in got["edges"]], [("src/barrel.ts", 1), ("src/main.ts", 10)])

    def test_a_package_that_appears_nearer_than_the_one_in_use_takes_over_the_edge(self):
        proj = make_project(self.tmp)
        write(proj / "node_modules" / "dup" / "package.json", '{"name": "dup", "version": "1.0.0", "types": "index.d.ts"}\n')
        write(proj / "node_modules" / "dup" / "index.d.ts", "export declare const twin: number;\n")
        write(proj / "src" / "uses.ts", 'import { twin } from "dup";\nexport const t = twin;\n')
        nav = self.nav(proj)
        self.assertEqual([(e["resolved"], e["external"]) for e in nav.imports("src/uses.ts")["edges"]], [("node_modules/dup/index.d.ts", True)])
        write(proj / "src" / "node_modules" / "dup" / "package.json", '{"name": "dup", "version": "2.0.0", "types": "index.d.ts"}\n')
        write(proj / "src" / "node_modules" / "dup" / "index.d.ts", "export declare const twin: string;\n")
        got = nav.imports("src/uses.ts")
        self.assertEqual([(e["resolved"], e["external"]) for e in got["edges"]], [("src/node_modules/dup/index.d.ts", True)])

    def test_a_missing_package_that_gets_installed_resolves_and_one_that_is_removed_does_not(self):
        proj = make_project(self.tmp)
        nav = self.nav(proj)
        self.assertEqual(nav.imports("src/main.ts")["unresolved"], 1)
        write(proj / "node_modules" / "missing-package" / "package.json", '{"name": "missing-package", "version": "1.0.0", "types": "index.d.ts"}\n')
        write(proj / "node_modules" / "missing-package" / "index.d.ts", "export {};\n")
        got = nav.imports("src/main.ts")
        self.assertEqual(got["unresolved"], 0)
        shutil.rmtree(proj / "node_modules" / "missing-package")
        self.assertEqual(nav.imports("src/main.ts")["unresolved"], 1)

    def test_an_edge_does_not_change_when_nothing_it_looked_at_changed(self):
        proj = make_project(self.tmp)
        nav = self.nav(proj)
        first = nav.imports("src/main.ts")["edges"]
        write(proj / "src" / "unrelated.ts", "export const z = 1;\n")
        self.assertEqual(nav.imports("src/main.ts")["edges"], first)

    def test_an_edited_tsconfig_is_reread(self):
        proj = make_project(self.tmp)
        nav = self.nav(proj)
        self.assertEqual(nav.exports("src/util.ts")["status"], "ok")
        config = json.loads((proj / "tsconfig.json").read_text())
        config["include"] = ["src/main.ts"]
        (proj / "tsconfig.json").write_text(json.dumps(config))
        self.assertEqual(nav.stats()["root_files"], 1)

    def test_a_file_that_changes_during_the_answer_is_reported_stale_once(self):
        proj = make_project(self.tmp)
        done = subprocess.run([NODE, str(FIXTURES / "stale_driver.mjs"), str(proj), str(proj / "tsconfig.json"), str(TYPESCRIPT),
                               str(proj / "src" / "util.ts")], capture_output=True, text=True, timeout=120, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        first, second = json.loads(done.stdout)
        self.assertEqual(first["status"], "stale_index")
        self.assertEqual((second["status"], [e["name"] for e in second["exports"]]), ("ok", ["greet", "VERSION"]))


@needs_ts
class Boundaries(TempCase):
    def test_paths_outside_the_root_are_refused_before_the_helper_starts(self):
        proj = make_project(self.tmp)
        write(self.tmp / "outside.ts", "export const out = 1;\n")
        nav = self.nav(proj)
        for target in (self.tmp / "outside.ts", "../outside.ts", "src/../../outside.ts", "/etc/passwd"):
            got = nav.exports(str(target))
            self.assertEqual(got["status"], "refused", target)
            self.assertIn("outside_root", got["message"])
        self.assertIsNone(nav.pid)

    def test_a_symlink_leaving_the_root_is_refused(self):
        proj = make_project(self.tmp)
        write(self.tmp / "outside.ts", "export const out = 1;\n")
        os.symlink(self.tmp / "outside.ts", proj / "src" / "link.ts")
        got = self.nav(proj).exports("src/link.ts")
        self.assertEqual((got["status"], got["message"][:21]), ("refused", "path refused (outside"))

    def test_credential_files_are_refused(self):
        proj = make_project(self.tmp)
        for name in (".env", ".env.local", "config/local.json", "agent/auth.json"):
            write(proj / name, "TOKEN=x\n")
            got = self.nav(proj).definition(name, name="TOKEN")
            self.assertEqual(got["status"], "refused", name)
            self.assertIn("sensitive_path", got["message"])

    def test_a_credential_file_reached_through_an_import_is_never_read_and_counts_as_absent(self):
        proj = make_project(self.tmp)
        config = json.loads((proj / "tsconfig.json").read_text())
        config["compilerOptions"]["resolveJsonModule"] = True
        (proj / "tsconfig.json").write_text(json.dumps(config))
        write(proj / "config" / "local.json", '{"token": "x"}\n')
        write(proj / "src" / "cfg.ts", 'import local from "../config/local.json";\nexport const token = local.token;\n')
        guard = RecordingGuard()
        nav = self.nav(proj, guard=guard)
        got = nav.imports("src/cfg.ts")
        self.assertEqual([(e["specifier"], e["resolved"]) for e in got["edges"]], [("../config/local.json", None)])
        self.assertEqual((got["unresolved"], "withheld" in got), (1, False))
        got = nav.definition("src/cfg.ts", name="token", line=2)
        self.assertNotIn("string", got["signature"])  # the JSON was not read, so `local.token` has no type from it
        self.assertEqual({d["path"] for d in got["definitions"]}, {"src/cfg.ts"})  # nothing points into the JSON file
        got = nav.references("src/cfg.ts", name="local", line=1)
        self.assertEqual({r["path"] for r in got["references"]}, {"src/cfg.ts"})
        self.assertNotIn('"token": "x"', json.dumps(got))
        self.assertTrue(any(p.endswith("config/local.json") for p in guard.asked), guard.asked)

    def test_a_source_the_guard_refuses_is_treated_as_absent_by_the_helper(self):
        proj = make_project(self.tmp)
        write(proj / "src" / "hidden" / "h.ts", "export const hidden: number = 1;\n")
        write(proj / "src" / "uses_hidden.ts", 'import { hidden } from "./hidden/h";\nexport const v = hidden;\n')
        guard = RecordingGuard(hide="/src/hidden/")
        nav = self.nav(proj, guard=guard)
        got = nav.imports("src/uses_hidden.ts")
        self.assertEqual([(e["resolved"], e["external"]) for e in got["edges"]], [(None, False)])
        self.assertNotIn("withheld", got)
        got = nav.definition("src/uses_hidden.ts", name="hidden", line=2)
        self.assertEqual({d["path"] for d in got["definitions"]}, {"src/uses_hidden.ts"})  # only the import itself; the source is unknown
        self.assertNotIn("number", got["signature"])
        self.assertEqual(nav.stats()["root_files"], 5)  # the 4 fixture files and uses_hidden.ts; the refused file is not listed
        self.assertTrue(any("/src/hidden/h.ts" in p for p in guard.asked), guard.asked)

    def test_a_guard_that_fails_while_the_helper_asks_refuses_the_path(self):
        proj = make_project(self.tmp)
        write(proj / "src" / "hidden" / "h.ts", "export const hidden: number = 1;\n")
        write(proj / "src" / "uses_hidden.ts", 'import { hidden } from "./hidden/h";\nexport const v = hidden;\n')
        nav = self.nav(proj, guard=RecordingGuard(hide="/src/hidden/", fail=True))
        self.assertEqual([e["resolved"] for e in nav.imports("src/uses_hidden.ts")["edges"]], [None])

    def test_a_source_reached_through_an_alias_or_symlink_outside_the_root_is_never_loaded(self):
        proj = make_project(self.tmp)
        write(self.tmp / "shared" / "x.ts", "export function outside(): number { return 1; }\n")
        config = json.loads((proj / "tsconfig.json").read_text())
        config["compilerOptions"]["paths"]["@out/*"] = ["../shared/*"]
        (proj / "tsconfig.json").write_text(json.dumps(config))
        write(proj / "src" / "uses.ts", 'import { outside } from "@out/x";\nexport const n = outside();\n')
        os.symlink(self.tmp / "shared" / "x.ts", proj / "src" / "linked.ts")
        write(proj / "src" / "uses_link.ts", 'import { outside as o2 } from "./linked";\nexport const m = o2();\n')
        nav = self.nav(proj)
        got = nav.definition("src/uses.ts", name="outside", line=2)
        self.assertEqual({d["path"] for d in got["definitions"]}, {"src/uses.ts"})
        self.assertNotIn("number", got["signature"])  # the file was not read, so TypeScript knows nothing about it
        got = nav.definition("src/uses_link.ts", name="o2", line=2)
        self.assertEqual({d["path"] for d in got["definitions"]}, {"src/uses_link.ts"})
        self.assertNotIn("number", got["signature"])
        self.assertEqual([e["resolved"] for e in nav.imports("src/uses.ts")["edges"]], [None])

    def test_typescripts_own_library_files_stay_readable_even_when_they_sit_outside_the_root(self):
        proj = make_project(self.tmp)
        write(proj / "src" / "lib_use.ts", "export const xs: Array<number> = [];\n")
        got = self.nav(proj).definition("src/lib_use.ts", name="Array", line=1)
        self.assertIn("interface Array", got["signature"])
        self.assertEqual((got["status"], got["definitions"]), ("not_found", []))
        self.assertGreaterEqual(got["omitted_outside_root"], 1)  # the declaration is in the typescript package: dropped and counted

    def test_free_text_is_screened(self):
        proj = make_project(self.tmp)
        write(proj / "src" / "key.ts", f'export const awsKey = "{AWS}";\n')
        write(proj / "src" / "use_key.ts", 'import { awsKey } from "./key";\nexport const k = awsKey;\n')
        got = self.nav(proj).definition("src/use_key.ts", name="awsKey", line=2)
        self.assertEqual(spots(got["definitions"]), ["src/key.ts:1"])
        self.assertNotIn(AWS, json.dumps(got))
        self.assertIn("REDACTED", got["definitions"][0]["text"])

    def test_every_string_in_an_answer_is_screened_not_only_chosen_keys(self):
        nav = self.nav(self.tmp)
        got = nav._finish({"status": "ok", "edges": [{"specifier": AWS, "resolved": None, "line": 1}], "other": [AWS, {"deep": {"name": AWS}}],
                           "kind": AWS})
        self.assertNotIn(AWS, json.dumps(got))
        self.assertEqual(got["edges"][0]["line"], 1)
        self.assertIsNone(got["edges"][0]["resolved"])
        self.assertEqual(got["status"], "ok")

    def test_an_import_specifier_that_holds_a_secret_is_screened_in_a_real_answer(self):
        proj = make_project(self.tmp)
        write(proj / "src" / "odd.ts", f'import "{AWS}";\nexport const odd = 1;\n')
        got = self.nav(proj).imports("src/odd.ts")
        self.assertEqual(got["status"], "ok")
        self.assertEqual(len(got["edges"]), 1)
        self.assertNotIn(AWS, json.dumps(got))

    def test_screening_everything_leaves_paths_and_names_of_an_ordinary_answer_unchanged(self):
        proj = make_project(self.tmp)
        got = self.nav(proj).imports("src/format.ts", direction="importers", depth=2)
        self.assertEqual([(e["from"], e["to"], e["specifier"]) for e in got["edges"]],
                         [("src/barrel.ts", "src/format.ts", "./format"), ("src/main.ts", "src/format.ts", "./format"),
                          ("src/main.ts", "src/barrel.ts", "@/barrel")])

    def test_the_helper_gets_only_allowlisted_environment(self):
        root = self.tmp / "fake"
        write(root / "mode", "env")
        write(root / "tsconfig.json", "{}")
        with mock.patch.dict(os.environ, {"TP_SECRET_FOR_TEST": "shh", "OPENAI_API_KEY": "k"}):
            got = self.nav(root, server=FIXTURES / "fake_server.mjs").exports("a.ts")
        self.assertEqual(got["status"], "ok")
        self.assertFalse({"TP_SECRET_FOR_TEST", "OPENAI_API_KEY"} & set(got["env"]))
        self.assertLessEqual({k for k in got["env"] if not k.startswith("__CF")}, set(nav_web.CHILD_ENV))  # macOS adds __CF_* to every process


@needs_node
class Failures(TempCase):
    def fake(self, mode, **kwargs):
        root = self.tmp / f"fake-{mode}"
        write(root / "mode", mode)
        write(root / "tsconfig.json", "{}")
        write(root / "a.ts", "export {};\n")
        return root, self.nav(root, server=FIXTURES / "fake_server.mjs", **kwargs)

    def test_no_tsconfig_is_no_project_and_no_helper_starts(self):
        nav = self.nav(self.tmp, "tsconfig.json")
        write(self.tmp / "a.ts", "export {};\n")
        got = nav.exports("a.ts")
        self.assertEqual(got["status"], "no_project")
        self.assertTrue(got["next"])
        self.assertIsNone(nav.pid)

    def test_find_tsconfig_is_the_nearest_below_the_root_and_never_above_it(self):
        write(self.tmp / "tsconfig.json", "{}")  # above the root: must not be found
        root = self.tmp / "repo"
        write(root / "app" / "src" / "a.ts", "")
        write(root / "app" / "tsconfig.json", "{}")
        write(root / "lib" / "b.ts", "")
        self.assertEqual(find_tsconfig(root / "app" / "src" / "a.ts", root), root / "app" / "tsconfig.json")
        self.assertIsNone(find_tsconfig(root / "lib" / "b.ts", root))
        write(root / "tsconfig.json", "{}")
        self.assertEqual(find_tsconfig(root / "lib" / "b.ts", root), root / "tsconfig.json")

    def test_a_project_without_typescript_is_no_typescript(self):
        proj = make_project(self.tmp, with_typescript=False)
        got = self.nav(proj).exports("src/util.ts")
        self.assertEqual(got["status"], "no_typescript")
        self.assertTrue(got["next"])

    def test_unparseable_tsconfig_is_no_project(self):
        proj = make_project(self.tmp, with_typescript=bool(TYPESCRIPT))
        (proj / "tsconfig.json").write_text("{ not json")
        if not TYPESCRIPT:
            self.skipTest("needs typescript to parse the config")
        self.assertEqual(self.nav(proj).exports("src/util.ts")["status"], "no_project")

    def test_the_first_question_that_outlasts_the_call_timeout_gives_warming_and_the_helper_keeps_working(self):
        _, nav = self.fake("slow_first", timeout=0.4)
        got = nav.exports("a.ts")
        self.assertEqual(got["status"], "warming")
        self.assertTrue(got["next"])
        pid = nav.pid
        self.assertIsNotNone(pid)
        got = nav.exports("a.ts")  # the earlier answer arrives first and is skipped
        self.assertEqual((got["status"], "late" in got), ("ok", False))
        self.assertEqual((nav.pid, nav.restarts), (pid, 0))

    def test_a_helper_that_never_answers_is_stopped_once_the_warm_limit_passes(self):
        _, nav = self.fake("hang", timeout=0.4)
        with mock.patch.object(nav_web, "WARM_LIMIT", 0.0):
            got = nav.exports("a.ts")
        self.assertEqual(got["status"], "timeout")
        self.assertIsNone(nav.pid)

    def test_a_call_over_the_timeout_stops_a_warm_helper_and_the_next_call_starts_a_fresh_one(self):
        root, nav = self.fake("hang_after_first", timeout=0.4)
        self.assertEqual(nav.exports("a.ts")["status"], "ok")
        got = nav.exports("a.ts")  # it has answered before, so this is a hang, not a build
        self.assertEqual(got["status"], "timeout")
        self.assertTrue(got["next"])
        self.assertIsNone(nav.pid)
        write(root / "mode", "ok")
        got = nav.exports("a.ts")
        self.assertEqual(got["status"], "ok")
        self.assertEqual(nav.restarts, 1)

    def test_a_helper_that_dies_mid_call_is_restarted_and_the_call_retried_once(self):
        root, nav = self.fake("die_once")
        self.assertEqual(nav.exports("a.ts")["status"], "ok")  # first launch dies on the request; the retry's launch answers
        self.assertTrue((root / "died").exists())
        first = nav.pid
        os.kill(first, 9)  # killed between calls: the next call notices and starts another
        got = nav.exports("a.ts")
        self.assertEqual(got["status"], "ok")
        self.assertNotEqual(nav.pid, first)
        self.assertGreaterEqual(nav.restarts, 1)

    def test_a_helper_that_always_dies_gives_crashed_after_one_retry(self):
        _, nav = self.fake("die_always")
        got = nav.exports("a.ts")
        self.assertEqual(got["status"], "crashed")
        self.assertTrue(got["next"])
        self.assertIsNone(nav.pid)

    def test_stale_index_is_retried_once_then_reported(self):
        _, nav = self.fake("stale_once")
        self.assertEqual(nav.exports("a.ts")["status"], "ok")
        _, nav = self.fake("stale_always")
        self.assertEqual(nav.exports("a.ts")["status"], "stale_index")

    def test_stop_ends_the_helper_and_a_later_call_starts_it_again(self):
        _, nav = self.fake("ok")
        self.assertEqual(nav.exports("a.ts")["status"], "ok")
        pid = nav.pid
        nav.stop()
        self.assertIsNone(nav.pid)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertEqual(nav.exports("a.ts")["status"], "ok")
        self.assertNotEqual(nav.pid, pid)

    def test_missing_node_is_unavailable(self):
        root = self.tmp / "p"
        write(root / "tsconfig.json", "{}")
        with mock.patch.object(nav_web.shutil, "which", return_value=None):
            got = self.nav(root).exports("a.ts")
        self.assertEqual(got["status"], "unavailable")

    def test_an_output_screen_that_fails_returns_nothing(self):
        _, nav = self.fake("ok")
        nav.guard = mock.Mock(wraps=nav.guard)
        nav.guard.screen.side_effect = RuntimeError("boom")
        self.assertEqual(nav.exports("a.ts")["status"], "unavailable")  # every string is screened, so even the status needs the screen
        self.assertEqual(nav._finish({"status": "ok", "text": "x"})["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
