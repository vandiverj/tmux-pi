"""Context engine CX3 (lib/ctxengine/graph.py, lib/ctxengine/nav_api.py): the Deno reverse import index and the navigation answers.

Every project here is a copy of tests/fixtures/ctx_api/project or a small tree built in a temporary directory. Tests that run
Deno skip when it is not on PATH; none of them touches the network (the fixture imports nothing but local files and `node:path`),
and the fake `deno` programs used for the timeout, failure and environment cases are shell scripts written into the temporary
directory."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
from ctxengine import graph  # noqa: E402
from ctxengine import nav_api  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "ctx_api" / "project"
HAVE_DENO = shutil.which("deno") is not None
NEEDS_DENO = unittest.skipUnless(HAVE_DENO, "deno is not on PATH")


def write(path, text=""):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class Tree(unittest.TestCase):
    """A temporary directory per test, with helpers to copy the fixture project into it or to write a fake `deno`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name).resolve()

    def project(self, name="proj"):
        target = self.dir / name
        shutil.copytree(FIXTURE, target)
        return target

    def fake_deno(self, body):
        path = self.dir / "bin" / "deno"
        write(path, "#!/bin/sh\n" + body + "\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def nav(self, project, **kw):
        kw.setdefault("cache_dir", str(self.dir / "cache"))
        return nav_api.NavApi(str(project), **kw)


class ScannerCase(unittest.TestCase):
    def specs(self, text):
        return [(s["spec"], s["line"], s["type_only"], s["dynamic"]) for s in graph.scan_specifiers(text)]

    def test_every_import_and_export_form(self):
        src = "\n".join([
            "import a, { b as c } from './a.ts';",  # 1
            "import type { T } from \"./t.ts\"",  # 2
            "import * as ns from '@/x.ts'",  # 3
            "import d = require('./req.ts');",  # 4
            "export * from './re.ts';",  # 5
            "export * as q from './re2.ts';",  # 6
            "export { z } from './re3.ts';",  # 7
            "export { local };",  # 8
            "export type { Y } from './re4.ts';",  # 9
            "import './side.ts' with { type: 'json' };",  # 10
            "const lazy = await import('./dyn.ts');",  # 11
            "/// <reference path=\"./ref.d.ts\" />",  # 12
        ])
        self.assertEqual(self.specs(src), [
            ("./a.ts", 1, False, False), ("./t.ts", 2, True, False), ("@/x.ts", 3, False, False), ("./req.ts", 4, False, False),
            ("./re.ts", 5, False, False), ("./re2.ts", 6, False, False), ("./re3.ts", 7, False, False),
            ("./re4.ts", 9, True, False), ("./side.ts", 10, False, False), ("./dyn.ts", 11, False, True),
            ("./ref.d.ts", 12, True, False)])

    def test_things_that_look_like_an_import_are_not_edges(self):
        src = "\n".join([
            "// import 'comment.ts'",
            "/* import 'block.ts'",
            "   import 'block2.ts' */",
            "const s = \"import 'string.ts'\"; const t = 'export * from \"x.ts\"';",
            "const tpl = `import 'template.ts' ${ 1 + 1 }`;",
            "const re = /[\"']import 'regex.ts'/g;",
            "const o = { import: 1 }; o.import('call.ts'); const m = import.meta.url;",
            "const v = await import(variable);",
            "export { local };",
        ])
        self.assertEqual(self.specs(src), [])

    def test_an_import_inside_a_template_expression_and_after_a_division_is_found(self):
        src = "const x = `a ${ await import('./in_tpl.ts') } b`;\nlet r = a / b; import './after_div.ts'\n"
        self.assertEqual(self.specs(src), [("./in_tpl.ts", 1, False, True), ("./after_div.ts", 2, False, False)])

    def test_unterminated_input_does_not_hang_or_raise(self):
        for src in ("import { a", "const s = 'abc", "const t = `abc ${", "/* never closed", "import", "export *", "x = /abc"):
            graph.scan_specifiers(src)


class ResolveCase(Tree):
    def setUp(self):
        super().setUp()
        write(self.dir / "src" / "a.ts")
        write(self.dir / "src" / "helpers.tsx")
        write(self.dir / "lib" / "types" / "t.ts")
        self.imap = graph.ImportMap({"@/": "./src/", "@types/": "./lib/types/", "@types": "./lib/types/index.ts", "zod": "npm:zod@4",
                                     "@std/path": "jsr:@std/path@1", "@x": "./src/a.ts"}, str(self.dir))
        self.importer = str(self.dir / "src" / "main.ts")

    def res(self, spec):
        return graph.resolve(spec, self.importer, self.imap)

    def test_relative_and_import_map_targets(self):
        self.assertEqual(self.res("./a.ts"), ("file", str(self.dir / "src" / "a.ts"), ""))
        self.assertEqual(self.res("@/a.ts"), ("file", str(self.dir / "src" / "a.ts"), ""))
        self.assertEqual(self.res("@x"), ("file", str(self.dir / "src" / "a.ts"), ""))
        self.assertEqual(self.res("@types/t.ts"), ("file", str(self.dir / "lib" / "types" / "t.ts"), ""))

    def test_the_longest_key_wins(self):
        imap = graph.ImportMap({"@/": "./src/", "@/deep/": "./lib/types/"}, str(self.dir))
        self.assertEqual(graph.resolve("@/deep/t.ts", self.importer, imap)[1], str(self.dir / "lib" / "types" / "t.ts"))

    def test_packages_and_their_subpaths_are_external(self):
        self.assertEqual(self.res("zod"), ("external", "npm:zod@4", ""))
        self.assertEqual(self.res("zod/v4"), ("external", "npm:zod@4/v4", ""))
        self.assertEqual(self.res("@std/path/join"), ("external", "jsr:@std/path@1/join", ""))
        for spec in ("npm:left-pad@1", "jsr:@std/fs@1", "node:path", "https://example.test/a.ts"):
            self.assertEqual(self.res(spec), ("external", spec, ""))

    def test_a_query_suffix_is_the_same_file(self):
        self.assertEqual(self.res("./helpers.tsx?variant"), ("file", str(self.dir / "src" / "helpers.tsx"), ""))

    def test_what_cannot_be_resolved_says_why(self):
        self.assertEqual(self.res("./nope.ts"), ("unresolved", None, "missing"))
        self.assertEqual(self.res("@/nope.ts"), ("unresolved", None, "missing"))
        self.assertEqual(self.res("left-pad"), ("unresolved", None, "bare"))
        self.assertEqual(self.res("zod-extra"), ("unresolved", None, "bare"))  # not a subpath of the zod key


class IndexCase(Tree):
    def index(self, project, **kw):
        kw.setdefault("cache_path", str(self.dir / "cache" / "index.json"))
        return graph.ImportIndex(str(project), **kw)

    def test_importers_follow_the_import_map_and_relative_chain_in_order(self):
        project = self.project()
        index = self.index(project)
        stats = index.refresh()
        self.assertEqual((stats["files"], stats["scanned"]), (10, 10))  # skipped/ is excluded by deno.json
        self.assertFalse(index.indexed("skipped/ignored.ts"))
        self.assertEqual([(e["from"], e["line"], e["spec"]) for e in index.importers("src/app/run.ts")], [
            ("src/app/run_test.ts", 1, "@/app/run.ts"), ("src/main.ts", 1, "@/app/run.ts"), ("src/main.ts", 9, "./app/run.ts")])
        self.assertEqual([e["from"] for e in index.importers("src/util/slug.ts")], ["src/util/index.ts"])
        self.assertEqual([e["from"] for e in index.importers("src/util/index.ts")], ["src/app/run.ts", "src/main.ts"])
        self.assertEqual([e["from"] for e in index.importers("shared/types.ts")], ["src/main.ts"])
        self.assertEqual(index.importers("src/main.ts"), [])
        self.assertEqual(index.unresolved(), [])
        lazy = index.importers("src/lazy.ts")[0]
        self.assertTrue(lazy["dynamic"] and not lazy["type_only"])
        self.assertTrue(index.importers("src/config.ts")[0]["type_only"] or index.importers("src/config.ts")[1]["type_only"])

    @NEEDS_DENO
    def test_forward_edges_match_deno_info_for_every_file(self):
        project = self.project()
        index = self.index(project, config=str(project / "deno.json"))
        index.refresh()
        nav = self.nav(project)
        checked = 0
        for rel in sorted(index.entries):
            wanted = sorted({e["to"] for e in (index.imports(rel) or []) if e["kind"] == "file" and e["config"] is None})
            answer = nav.forward_graph(rel)
            self.assertEqual(answer["status"], "ok", (rel, answer))
            entry = next(m for m in answer["modules"] if m["path"] == rel)
            self.assertEqual(entry["imports"], wanted, rel)
            checked += 1
        self.assertEqual(checked, 10)

    def test_touching_one_file_rescans_only_that_file(self):
        project = self.project()
        index = self.index(project)
        index.refresh()
        before = {rel: entry for rel, entry in index.entries.items()}
        again = index.refresh()
        self.assertEqual((again["scanned"], again["unchanged"]), (0, 10))
        target = project / "src" / "config.ts"
        target.write_text(target.read_text() + "import { slug } from '@/util/index.ts';\n")
        stats = index.refresh()
        self.assertEqual((stats["scanned"], stats["unchanged"], stats["removed"]), (1, 9, 0))
        for rel, entry in index.entries.items():
            self.assertEqual(entry is before[rel], rel != "src/config.ts", rel)
        self.assertIn("src/config.ts", [e["from"] for e in index.importers("src/util/index.ts")])

    def test_a_file_that_appears_resolves_an_import_that_did_not_change(self):
        project = self.project()
        write(project / "src" / "early.ts", "import { later } from './later.ts';\nexport const early = later;\n")
        index = self.index(project)
        index.refresh()
        self.assertEqual([(u["from"], u["spec"], u["reason"]) for u in index.unresolved()], [("src/early.ts", "./later.ts", "missing")])
        write(project / "src" / "later.ts", "export const later = 1;\n")
        stats = index.refresh()
        self.assertEqual(stats["scanned"], 1)  # later.ts only; early.ts was not read again
        self.assertEqual(index.unresolved(), [])
        self.assertEqual([e["from"] for e in index.importers("src/later.ts")], ["src/early.ts"])

    def test_a_deleted_file_is_forgotten(self):
        project = self.project()
        index = self.index(project)
        index.refresh()
        (project / "src" / "app" / "run_test.ts").unlink()
        stats = index.refresh()
        self.assertEqual((stats["removed"], stats["scanned"]), (1, 0))
        self.assertEqual([e["from"] for e in index.importers("src/app/run.ts")], ["src/main.ts", "src/main.ts"])
        self.assertIsNone(index.imports("src/app/run_test.ts"))

    def test_the_cache_is_reused_by_a_new_process_and_ignored_when_it_does_not_fit(self):
        project = self.project()
        first = self.index(project)
        first.refresh()
        cache = Path(first.cache_path)
        self.assertTrue(cache.is_file())
        second = self.index(project)
        self.assertEqual(second.refresh()["scanned"], 0)
        self.assertEqual(second.importers("src/app/run.ts"), first.importers("src/app/run.ts"))
        (project / "src" / "lazy.ts").write_text("export const lazy = 'changed, and longer';\n")
        third = self.index(project)
        self.assertEqual(third.refresh()["scanned"], 1)
        data = json.loads(cache.read_text())
        data["version"] = 999
        cache.write_text(json.dumps(data))
        self.assertEqual(self.index(project).refresh()["scanned"], 10)  # a cache from another version is not trusted
        cache.write_text("{not json")
        self.assertEqual(self.index(project).refresh()["scanned"], 10)

    def test_a_symlink_at_the_cache_path_is_never_written_through(self):
        project = self.project()
        victim = write(self.dir / "victim.txt", "keep")
        link = self.dir / "cache" / "index.json"
        link.parent.mkdir()
        link.symlink_to(victim)
        index = self.index(project)
        index.refresh()
        self.assertFalse(index.save())
        self.assertEqual(victim.read_text(), "keep")
        self.assertTrue(link.is_symlink())

    def test_overlay_config_adds_the_edge_it_changes_and_names_its_file(self):
        project = self.project()
        index = self.index(project, overlays=[str(project / "deno.test.json")])
        index.refresh()
        plain = [(e["from"], e["config"]) for e in index.importers("src/real_host.ts")]
        mocked = [(e["from"], e["config"]) for e in index.importers("src/mock_host.ts")]
        self.assertEqual(plain, [("src/app/run_test.ts", None), ("src/main.ts", None)])
        self.assertEqual(mocked, [("src/app/run_test.ts", "deno.test.json"), ("src/main.ts", "deno.test.json")])
        self.assertEqual(self.index(project, cache_path=None).refresh()["files"], 10)
        without = self.index(project, cache_path=None)
        without.refresh()
        self.assertEqual(without.importers("src/mock_host.ts"), [])

    def test_symlinks_are_not_followed_in_the_walk_or_in_resolution(self):
        project = self.project()
        outside = write(self.dir / "outside" / "secret_module.ts", "export const s = 1;\n")
        (project / "src" / "linked.ts").symlink_to(outside)
        (project / "src" / "linked_dir").symlink_to(outside.parent)
        write(project / "src" / "uses_link.ts", "import { s } from './linked.ts';\nexport { s };\n")
        index = self.index(project)
        index.refresh()
        self.assertFalse(index.indexed("src/linked.ts"))
        self.assertFalse(any(rel.startswith("src/linked_dir") for rel in index.entries))
        self.assertEqual([(u["spec"], u["reason"]) for u in index.unresolved()], [("./linked.ts", "outside")])

    def test_a_relative_import_that_leaves_the_project_is_unresolved_not_returned(self):
        project = self.project()
        write(self.dir / "outside.ts", "export const o = 1;\n")
        write(project / "src" / "escape.ts", "import { o } from '../../outside.ts';\nexport { o };\n")
        index = self.index(project)
        index.refresh()
        self.assertEqual([(u["from"], u["reason"]) for u in index.unresolved()], [("src/escape.ts", "outside")])
        self.assertEqual(index.importers("../outside.ts"), [])

    def test_a_file_that_changes_while_it_is_read_is_reported_and_read_again_next_time(self):
        project = self.project()
        index = self.index(project)
        index.refresh()
        target = project / "src" / "lazy.ts"
        target.write_text("export const lazy = 1; // first edit\n")
        real = graph.read_sources

        def racing(paths, workers=8):
            out = real(paths, workers)
            target.write_text("import './config.ts'; // second edit, a different size\n")
            return out

        with mock.patch.object(graph, "read_sources", racing):
            stats = index.refresh()
        self.assertEqual(stats["stale"], ["src/lazy.ts"])
        after = index.refresh()
        self.assertEqual((after["scanned"], after["stale"]), (1, []))
        self.assertIn("src/lazy.ts", [e["from"] for e in index.importers("src/config.ts")])

    def test_an_unreadable_or_oversized_file_has_no_edges_and_does_not_stop_the_build(self):
        project = self.project()
        write(project / "src" / "huge.ts", "import './config.ts';\n" + "// pad\n" * 10)
        with mock.patch.object(graph, "MAX_BYTES", 30):
            index = self.index(project, cache_path=None)
            index.refresh()
        self.assertEqual(index.imports("src/huge.ts"), [])

    def test_project_path_keeps_a_path_inside_and_refuses_one_that_leaves(self):
        base = str(self.dir.resolve())
        outside = write(self.dir.parent / f"{self.dir.name}-sibling" / "x.ts")
        self.addCleanup(shutil.rmtree, outside.parent, True)
        (self.dir / "link.ts").symlink_to(outside)
        write(self.dir / "in.ts")
        (self.dir / "in_link.ts").symlink_to(self.dir / "in.ts")
        self.assertEqual(graph.project_path(base, "in.ts"), "in.ts")
        self.assertEqual(graph.project_path(base, str(self.dir / "in.ts")), "in.ts")
        self.assertEqual(graph.project_path(base, "in_link.ts"), "in_link.ts")
        self.assertIsNone(graph.project_path(base, "link.ts"))
        self.assertIsNone(graph.project_path(base, "../x.ts"))
        self.assertIsNone(graph.project_path(base, str(outside)))
        self.assertIsNone(graph.project_path(base, "."))


class NavCase(Tree):
    def test_importers_answer_and_limit(self):
        nav = self.nav(self.project())
        answer = nav.importers("src/app/run.ts")
        self.assertEqual(answer["status"], "ok")
        self.assertEqual([(i["path"], i["line"]) for i in answer["importers"]], [("src/app/run_test.ts", 1), ("src/main.ts", 1), ("src/main.ts", 9)])
        self.assertEqual((answer["total"], answer["truncated"], answer["unresolved_total"]), (3, False, 0))
        one = nav.importers("src/app/run.ts", limit=1)
        self.assertEqual((one["total"], one["truncated"], len(one["importers"])), (3, True, 1))
        self.assertEqual(nav.importers("src/app/run.ts"), answer | {"index_age_s": nav.importers("src/app/run.ts")["index_age_s"]}
                         | {"scanned": 0})

    def test_argument_errors_say_what_to_do(self):
        nav = self.nav(self.project())
        for limit in (0, 26, "3", None, True):
            answer = nav.importers("src/app/run.ts", limit=limit)
            self.assertEqual(answer["status"], "bad_argument", limit)
            self.assertIn("1 to 25", answer["detail"])
            self.assertTrue(answer["next_step"])
        for path in ("", "  ", None, 5, "a\0b"):
            self.assertEqual(nav.importers(path)["status"], "bad_argument", path)
        self.assertEqual(nav.imports("src/main.ts", source="magic")["status"], "bad_argument")
        self.assertEqual(nav.symbols("src/main.ts", limit=0)["status"], "bad_argument")

    def test_a_missing_file_and_a_missing_project(self):
        nav = self.nav(self.project())
        self.assertEqual(nav.importers("src/nope.ts")["status"], "not_found")
        self.assertEqual(nav.imports("src/nope.ts")["status"], "not_found")
        self.assertEqual(nav.symbols("src/nope.ts")["status"], "not_found")
        gone = nav_api.NavApi(str(self.dir), root=str(self.dir / "absent"), cache_dir=str(self.dir / "cache"))
        answer = gone.importers("anything.ts")
        self.assertEqual((answer["status"], bool(answer["next_step"])), ("no_project", True))

    def test_paths_outside_the_project_and_sensitive_paths_are_refused_without_being_read(self):
        project = self.project()
        outside = write(self.dir / "outside.ts", "export const o = 1;\n")
        (project / "src" / "leak.ts").symlink_to(outside)
        write(project / ".env", "TOKEN=abc\n")
        write(project / "config" / "local.json", "{}\n")
        write(project / ".env.test", "TOKEN=abc\n")
        nav = self.nav(project)
        for path, reason in (("../outside.ts", "outside_project"), (str(outside), "outside_project"), ("src/leak.ts", "outside_project"),
                             (".env", "sensitive_path"), (".env.test", "sensitive_path"), ("config/local.json", "sensitive_path")):
            for call in (nav.importers, nav.imports, nav.symbols, nav.forward_graph):
                answer = call(path)
                self.assertEqual((answer["status"], answer.get("reason")), ("refused", reason), (call.__name__, path))
        self.assertNotIn("TOKEN", json.dumps(nav.importers(".env")))

    def test_an_injected_path_check_and_guard_replace_the_stubs(self):
        project = self.project()
        seen = []

        def check_path(root, path):
            seen.append((root, path))
            if path.endswith("main.ts"):
                raise nav_api.Refused("blocked_by_test")
            return path

        nav = self.nav(project, check_path=check_path)
        self.assertEqual(nav.importers("src/main.ts")["reason"], "blocked_by_test")
        self.assertEqual(nav.importers("src/app/run.ts")["status"], "ok")
        self.assertEqual(seen[0], (str(project.resolve()), "src/main.ts"))

    def test_imports_split_files_packages_and_unresolved(self):
        project = self.project()
        write(project / "src" / "mixed.ts", "import { a } from './nope.ts';\nimport z from 'zod';\nimport { b } from './config.ts';\nexport { a, z, b };\n")
        (project / "deno.json").write_text(json.dumps({"imports": {"@/": "./src/", "zod": "npm:zod@4"}}))
        answer = self.nav(project).imports("src/mixed.ts")
        self.assertEqual(answer["status"], "ok")
        self.assertEqual(answer["source"], "index")
        self.assertEqual([f["path"] for f in answer["files"]], ["src/config.ts"])
        self.assertEqual(answer["external"], ["zod"])  # the specifier as written; the import map's target is not needed to read it
        self.assertEqual(answer["unresolved"], [{"line": 1, "spec": "./nope.ts", "reason": "missing"}])

    @NEEDS_DENO
    def test_forward_graph_lists_only_project_files_and_counts_the_rest(self):
        answer = self.nav(self.project()).imports("src/main.ts", source="deno")
        self.assertEqual((answer["status"], answer["source"]), ("ok", "deno_info"))
        paths = [m["path"] for m in answer["modules"]]
        self.assertEqual(paths, sorted(paths))
        self.assertIn("src/util/slug.ts", paths)
        self.assertNotIn("skipped/ignored.ts", paths)
        self.assertEqual(answer["external"], 1)  # node:path

    @NEEDS_DENO
    def test_symbols_come_from_deno_doc_with_definition_sites_and_are_cached_by_content(self):
        project = self.project()
        nav = self.nav(project)
        first = nav.symbols("src/main.ts")
        self.assertEqual((first["status"], first["cached"]), ("ok", False))
        self.assertEqual([(s["name"], s["kind"], s["path"], s["line"]) for s in first["symbols"]], [
            ("Id", "typeAlias", "shared/types.ts", 1), ("SHARED", "variable", "shared/types.ts", 2),
            ("run", "function", "src/app/run.ts", 5), ("main", "function", "src/main.ts", 16)])
        run = next(s for s in first["symbols"] if s["name"] == "run")
        self.assertEqual((run["signature"], run["doc"]), ("export function run(config: Config): string {", "Run the app for one config."))
        second = self.nav(project).symbols("src/main.ts")  # a new instance reads the cache file
        self.assertTrue(second["cached"])
        self.assertEqual(second["symbols"], first["symbols"])
        # A file a re-export comes from changes, the file asked about does not: the answer must not be the cached one.
        run_ts = project / "src" / "app" / "run.ts"
        run_ts.write_text(run_ts.read_text().replace("Run the app for one config.", "Run it, changed."))
        third = self.nav(project).symbols("src/main.ts")
        self.assertFalse(third["cached"])
        self.assertEqual(next(s for s in third["symbols"] if s["name"] == "run")["doc"], "Run it, changed.")
        self.assertEqual(self.nav(project).symbols("src/main.ts", limit=2)["truncated"], True)

    @NEEDS_DENO
    def test_a_screen_is_applied_to_text_taken_from_comments(self):
        project = self.project()
        nav = self.nav(project, guard=lambda text: text.replace("Run the app", "[screened]"))
        run = next(s for s in nav.symbols("src/app/run.ts")["symbols"] if s["name"] == "run")
        self.assertEqual(run["doc"], "[screened] for one config.")

    def test_no_deno_is_unavailable_for_deno_operations_and_the_index_still_answers(self):
        project = self.project()
        for nav in (self.nav(project, deno=str(self.dir / "no" / "such" / "deno")), self.nav(project)):
            with mock.patch.dict(os.environ, {"PATH": str(self.dir / "empty")}) if nav.deno == "deno" else mock.patch.dict(os.environ):
                for answer in (nav.symbols("src/main.ts"), nav.forward_graph("src/main.ts"), nav.imports("src/main.ts", source="deno")):
                    self.assertEqual(answer["status"], "unavailable", answer)
                    self.assertTrue(answer["next_step"])
                self.assertEqual(nav.importers("src/app/run.ts")["status"], "ok")
                self.assertEqual(nav.imports("src/main.ts")["status"], "ok")
                self.assertIsNone(nav.status()["deno"])

    def test_deno_runs_offline_with_a_minimal_environment_and_never_writes_the_lock(self):
        project = self.project()
        log = self.dir / "env.log"
        exe = self.fake_deno(f'echo "$@" >> {log}; env >> {log}; echo \'{{"version":2,"nodes":{{}}}}\'')
        with mock.patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": "do-not-pass", "PATH": os.environ.get("PATH", "")}):
            answer = self.nav(project, deno=exe).symbols("src/main.ts")
        self.assertEqual(answer["status"], "ok")
        text = log.read_text()
        self.assertIn("doc --json --no-remote --no-lock", text)
        self.assertIn("NPM_CONFIG_REGISTRY=http://127.0.0.1:1/", text)
        self.assertIn("DENO_NO_UPDATE_CHECK=1", text)
        self.assertNotIn("do-not-pass", text)
        self.assertNotIn("AWS_SECRET", text)
        log.write_text("")
        self.nav(project, deno=exe).forward_graph("src/main.ts")
        self.assertIn("info --json --no-remote --no-lock", log.read_text())

    def test_a_deno_that_takes_too_long_is_a_timeout_not_a_hang(self):
        nav = self.nav(self.project(), deno=self.fake_deno("exec sleep 30"), timeout=0.3)
        answer = nav.forward_graph("src/main.ts")
        self.assertEqual(answer["status"], "timeout")
        self.assertTrue(answer["next_step"])

    def test_a_deno_failure_is_an_error_with_one_screened_line(self):
        exe = self.fake_deno("echo 'Download http://x' >&2; echo 'error: Module not found \"file:///x.ts\"' >&2; echo more >&2; exit 1")
        nav = self.nav(self.project(), deno=exe, guard=lambda t: t.replace("x.ts", "[path]"))
        answer = nav.symbols("src/main.ts")
        self.assertEqual((answer["status"], answer["detail"]), ("error", 'error: Module not found "file:///[path]"'))

    def test_output_that_is_not_what_deno_prints_is_an_error_not_a_crash(self):
        project = self.project()
        for body in ("echo not json", "echo '[]'", "echo '{}'", "echo '{\"modules\": 5}'"):
            nav = self.nav(project, deno=self.fake_deno(body))
            self.assertEqual(nav.forward_graph("src/main.ts")["status"], "error", body)
            self.assertEqual(nav.symbols("src/main.ts")["status"], "error", body)

    @NEEDS_DENO
    def test_the_same_question_gives_the_same_answer(self):
        project = self.project()
        a, b = self.nav(project), self.nav(project, cache_dir=str(self.dir / "other-cache"))
        for call in ("importers", "imports", "symbols", "forward_graph"):
            first, second = getattr(a, call)("src/main.ts"), getattr(b, call)("src/main.ts")
            for key in ("index_age_s", "scanned", "cached"):
                first.pop(key, None)
                second.pop(key, None)
            self.assertEqual(first, second, call)

    def test_status_reports_what_would_be_used(self):
        nav = self.nav(self.project())
        nav.importers("src/main.ts")
        status = nav.status()
        self.assertEqual((status["status"], status["config_found"], status["files_indexed"]), ("ok", True, 10))

    def test_a_project_without_a_deno_json_is_indexed_with_relative_imports_only(self):
        write(self.dir / "plain" / "a.ts", "import { b } from './b.ts';\nimport x from '@/x.ts';\nexport { b, x };\n")
        write(self.dir / "plain" / "b.ts", "export const b = 1;\n")
        nav = self.nav(self.dir / "plain")
        self.assertEqual([i["path"] for i in nav.importers("b.ts")["importers"]], ["a.ts"])
        self.assertEqual([u["reason"] for u in nav.imports("a.ts")["unresolved"]], ["bare"])


class ReviewFixesCase(Tree):
    """Findings F1-F8 of the review of the first CX3 attempt. Each test failed against that attempt."""

    def scripted_deno(self, doc=None, info=None):
        """A fake `deno` that logs every call to self.dir/deno.log and answers `doc` / `info` with the given JSON."""
        for name, payload in (("doc", doc), ("info", info)):
            if payload is not None:
                write(self.dir / f"{name}.json", json.dumps(payload))
        log = self.dir / "deno.log"
        return self.fake_deno(
            f'echo "$@" >> {log}\ncase "$1" in\n --version) echo "deno 9.9.9";;\n doc) cat {self.dir / "doc.json"};;\n'
            f' info) cat {self.dir / "info.json"};;\nesac')

    def deno_calls(self, verb):
        log = self.dir / "deno.log"
        return [line for line in (log.read_text().splitlines() if log.exists() else []) if line.startswith(verb + " ")]

    @staticmethod
    def doc_payload(project, rel, name, doc, line=0):
        return {"version": 2, "nodes": {"n": {"symbols": [{"name": name, "declarations": [
            {"declarationKind": "export", "kind": "function", "jsDoc": {"doc": doc},
             "location": {"filename": f"file://{Path(project).resolve() / rel}", "line": line}}]}]}}}

    # --- F1

    def test_a_refused_file_is_never_read_indexed_or_named(self):
        project = self.project()
        write(project / "src" / ".env.ts", "import './hidden_marker.ts';\nexport const KEY = 'x';\n")
        write(project / "src" / "credentials.ts", "import './hidden_marker2.ts';\n")
        write(project / "src" / "gate.ts", "import { KEY } from './.env.ts';\nimport './credentials.ts';\nimport { c } from './config.ts';\n"
                                           "export { KEY, c };\n")
        opened = []
        real = graph.read_source
        with mock.patch.object(graph, "read_source", lambda path: opened.append(path) or real(path)):
            answer = self.nav(project).imports("src/gate.ts")
        self.assertFalse([p for p in opened if p.endswith((".env.ts", "credentials.ts"))], opened)
        self.assertEqual(answer["status"], "ok")
        self.assertEqual([f["path"] for f in answer["files"]], ["src/config.ts"])
        self.assertEqual(sorted((u["reason"], u["spec"]) for u in answer["unresolved"]), [("refused", ""), ("refused", "")])
        text = json.dumps(answer)
        for secret in (".env.ts", "credentials", "hidden_marker"):
            self.assertNotIn(secret, text)
        for cached in (self.dir / "cache").iterdir():
            self.assertNotIn("hidden_marker", cached.read_text())  # the refused files were not read, so their imports are not kept
            self.assertNotIn("\"src/.env.ts\"", cached.read_text())  # no entry of its own
        self.assertIn("src/gate.ts", [i["path"] for i in self.nav(project).importers("src/config.ts")["importers"]])

    def test_a_refused_file_that_is_already_in_the_cache_is_dropped(self):
        project = self.project()
        write(project / "src" / ".env.ts", "import './hidden_marker.ts';\n")
        permissive = self.nav(project, check_path=lambda root, path: path)
        self.assertIn("src/.env.ts", [i["path"] for i in permissive.importers("src/config.ts")["importers"]] + ["src/.env.ts"])
        permissive.imports("src/.env.ts")  # indexed and cached under the permissive guard
        strict = self.nav(project)
        self.assertEqual(strict.imports("src/.env.ts")["status"], "refused")
        strict.importers("src/main.ts")
        self.assertFalse(strict._get_index().indexed("src/.env.ts"))
        self.assertNotIn("hidden_marker", json.dumps(strict.importers("src/main.ts")))

    def test_every_string_in_an_answer_passes_the_guard(self):
        project = self.project()
        write(project / "src" / "tok.ts", "import z from 'https://example.test/a.ts?token=SECRETTOKEN';\n"
                                          "import { c } from './config.ts?x=SECRETTOKEN';\nexport { z, c };\n")
        nav = self.nav(project, guard=lambda text: text.replace("SECRETTOKEN", "[redacted]"))
        imports = nav.imports("src/tok.ts")
        importers = nav.importers("src/config.ts")
        self.assertEqual(imports["external"], ["https://example.test/a.ts?token=[redacted]"])
        self.assertIn("./config.ts?x=[redacted]", [i["spec"] for i in importers["importers"]])
        for answer in (imports, importers, nav.status()):
            self.assertNotIn("SECRETTOKEN", json.dumps(answer))

    def test_a_guard_that_raises_returns_an_error_and_no_content(self):
        def broken(text):
            raise RuntimeError("screen down")

        nav = self.nav(self.project(), guard=broken)
        for answer in (nav.importers("src/main.ts"), nav.imports("src/main.ts"), nav.status()):
            self.assertEqual(answer["status"], "error")
            self.assertNotIn("main.ts", json.dumps(answer))

    def test_the_stub_has_the_signatures_of_cx1_redact(self):
        project = self.project()
        self.assertEqual(nav_api.stub_check_path(str(project), "src/main.ts"), "src/main.ts")
        self.assertEqual(nav_api.stub_check_path(str(project), str(project / "src" / "main.ts")), "src/main.ts")
        for bad in ("../x.ts", ".env", "src/.env.local", ""):
            with self.assertRaises(nav_api.Refused):
                nav_api.stub_check_path(str(project), bad)
        self.assertEqual(nav_api.stub_guard("text"), "text")

    # --- F2

    def test_a_symlinked_directory_inside_the_project_does_not_lead_out(self):
        project = self.project()
        outside = write(self.dir / "outside" / "secret.ts", "import './deeper.ts';\nexport const s = 1;\n")
        (project / "src" / "linked_dir").symlink_to(outside.parent)
        write(project / "src" / "viaparent.ts", "import { s } from './linked_dir/secret.ts';\nexport { s };\n")
        nav = self.nav(project, deno=self.scripted_deno(doc={"nodes": {}}, info={"modules": []}))
        answer = nav.imports("src/viaparent.ts")
        self.assertEqual(answer["files"], [])
        self.assertEqual([(u["spec"], u["reason"]) for u in answer["unresolved"]], [("./linked_dir/secret.ts", "outside")])
        self.assertEqual(nav.importers("src/linked_dir/secret.ts")["status"], "refused")
        self.assertEqual(nav._get_index().importers("src/linked_dir/secret.ts"), [])
        for call in (nav.symbols, nav.forward_graph):
            refused = call("src/viaparent.ts")
            self.assertEqual((refused["status"], refused["reason"]), ("refused", "reaches_refused_path"), call.__name__)
        self.assertEqual(self.deno_calls("doc") + self.deno_calls("info"), [])  # Deno was not run

    def test_deno_is_not_run_on_a_file_that_reaches_a_refused_file(self):
        project = self.project()
        write(project / "src" / ".env.ts", "export const KEY = 1;\n")
        write(project / "src" / "mid.ts", "import { KEY } from './.env.ts';\nexport { KEY };\n")
        write(project / "src" / "top.ts", "export { KEY } from './mid.ts';\n")
        nav = self.nav(project, deno=self.scripted_deno(doc={"nodes": {}}, info={"modules": []}))
        self.assertEqual(nav.symbols("src/top.ts")["reason"], "reaches_refused_path")
        self.assertEqual(self.deno_calls("doc"), [])
        self.assertEqual(nav.symbols("src/main.ts")["status"], "ok")

    def test_deno_output_naming_a_refused_or_outside_file_is_dropped(self):
        project = self.project()
        write(project / "src" / ".env.ts", "export const KEY = 1;\n")
        doc = self.doc_payload(project, "src/config.ts", "kept", "fine")
        doc["nodes"]["n"]["symbols"] += self.doc_payload(project, "src/.env.ts", "leak", "x")["nodes"]["n"]["symbols"]
        doc["nodes"]["n"]["symbols"] += self.doc_payload(self.dir, "outside.ts", "away", "x")["nodes"]["n"]["symbols"]
        shown = self.nav(project, deno=self.scripted_deno(doc=doc)).symbols("src/main.ts")
        self.assertEqual([s["name"] for s in shown["symbols"]], ["kept"])

    # --- F3

    def test_cached_symbols_are_screened_with_the_guard_of_the_reader(self):
        project = self.project()
        exe = self.scripted_deno(doc=self.doc_payload(project, "src/app/run.ts", "run", "uses SECRETTOKEN here", line=4))
        first = self.nav(project, deno=exe).symbols("src/app/run.ts")  # the stub guard: nothing screened
        self.assertEqual((first["cached"], first["symbols"][0]["doc"]), (False, "uses SECRETTOKEN here"))
        strict = self.nav(project, deno=exe, guard=lambda text: text.replace("SECRETTOKEN", "[redacted]"))
        second = strict.symbols("src/app/run.ts")
        self.assertTrue(second["cached"])
        self.assertNotIn("SECRETTOKEN", json.dumps(second))
        self.assertIn("uses [redacted] here", second["symbols"][0]["doc"])
        stricter = self.nav(project, deno=exe, guard=lambda text: text.replace("run", "[r]"))
        for field in ("name", "path", "signature", "doc"):  # names and paths are screened as well as the text
            self.assertNotIn("run", stricter.symbols("src/app/run.ts")["symbols"][0][field].replace("[r]", ""), field)

    def test_a_cached_symbol_of_a_file_that_is_refused_now_is_not_returned(self):
        project = self.project()
        write(project / "src" / "credentials.ts", "export const k = 1;\n")
        doc = self.doc_payload(project, "src/config.ts", "kept", "fine")
        doc["nodes"]["n"]["symbols"] += self.doc_payload(project, "src/credentials.ts", "secretive", "x")["nodes"]["n"]["symbols"]
        exe = self.scripted_deno(doc=doc)
        permissive = self.nav(project, deno=exe, check_path=lambda root, path: path)
        self.assertEqual([s["name"] for s in permissive.symbols("src/main.ts")["symbols"]], ["kept", "secretive"])
        again = self.nav(project, deno=exe).symbols("src/main.ts")
        self.assertTrue(again["cached"])
        self.assertEqual([s["name"] for s in again["symbols"]], ["kept"])

    # --- F4

    def test_deno_doc_and_deno_info_both_run_with_no_remote(self):
        project = self.project()
        exe = self.scripted_deno(doc={"nodes": {}}, info={"modules": []})
        nav = self.nav(project, deno=exe)
        nav.symbols("src/main.ts")
        nav.forward_graph("src/main.ts")
        self.assertIn("--no-remote", self.deno_calls("doc")[0].split())
        self.assertIn("--no-remote", self.deno_calls("info")[0].split())

    # --- F5

    def test_a_slash_after_the_condition_of_if_while_for_starts_a_regex(self):
        for src in ('if (ok) /import("ghost.ts")/.test(value);', 'while (x) /import "ghost.ts"/.test(y);',
                    'for (const a of b) /import("ghost.ts")/.test(a);', 'for await (const a of b) /import("ghost.ts")/.test(a);',
                    'with (o) /import("ghost.ts")/.test(a);', 'if (a) { b(); } else if (c(d)) /import("ghost.ts")/.test(e);'):
            self.assertEqual(graph.scan_specifiers(src), [], src)

    def test_a_slash_after_a_call_or_a_parenthesised_value_is_still_a_division(self):
        src = "const r = f(a) / 2; const q = (a + b) / c; import './one.ts'\nif (x) y(z) / 2; import './two.ts'\n"
        self.assertEqual([s["spec"] for s in graph.scan_specifiers(src)], ["./one.ts", "./two.ts"])

    # --- F6

    def test_inline_type_specifiers_make_an_import_type_only_when_every_name_is_a_type(self):
        def flags(src):
            return [s["type_only"] for s in graph.scan_specifiers(src)]

        self.assertEqual(flags("import { type Thing } from './t.ts';"), [True])
        self.assertEqual(flags("import { type A, type B as C } from './t.ts';"), [True])
        self.assertEqual(flags("import { type A, b } from './t.ts';"), [False])
        self.assertEqual(flags("import d, { type A } from './t.ts';"), [False])
        self.assertEqual(flags("import { type } from './t.ts';"), [False])  # a value called `type`
        self.assertEqual(flags("import { type as other } from './t.ts';"), [False])
        self.assertEqual(flags("import { type as } from './t.ts';"), [True])  # a type called `as`
        self.assertEqual(flags("import {} from './t.ts';"), [False])
        self.assertEqual(flags("import type { A } from './t.ts';"), [True])
        self.assertEqual(flags("export { type Thing } from './t.ts';"), [True])
        self.assertEqual(flags("export { type A, type B } from './t.ts';"), [True])
        self.assertEqual(flags("export { type A, b } from './t.ts';"), [False])
        self.assertEqual(flags("export type { A } from './t.ts';"), [True])
        self.assertEqual(flags("export * from './t.ts';"), [False])
        self.assertEqual(flags("import * as ns from './t.ts';"), [False])

    # --- F7

    def test_the_doc_cache_follows_content_not_size_and_modification_time(self):
        project = self.project()
        exe = self.scripted_deno(doc=self.doc_payload(project, "src/app/run.ts", "run", "x"))
        run_ts = project / "src" / "app" / "run.ts"
        self.nav(project, deno=exe).symbols("src/main.ts")
        self.assertEqual(len(self.deno_calls("doc")), 1)
        self.assertTrue(self.nav(project, deno=exe).symbols("src/main.ts")["cached"])
        before = run_ts.stat()
        text = run_ts.read_text()
        self.assertIn("Run the app", text)
        run_ts.write_text(text.replace("Run the app", "Run THE app"))  # same length
        os.utime(run_ts, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = run_ts.stat()
        self.assertEqual((after.st_size, after.st_mtime_ns), (before.st_size, before.st_mtime_ns))
        answer = self.nav(project, deno=exe).symbols("src/main.ts")
        self.assertFalse(answer["cached"])
        self.assertEqual(len(self.deno_calls("doc")), 2)

    # --- F8

    def test_the_forward_graph_keeps_the_target_and_its_direct_imports_when_it_cuts(self):
        project = self.project()
        names = [f"src/a{n:03d}.ts" for n in range(250)]
        for name in names:
            write(project / name, "export const a = 1;\n")
        write(project / "src" / "zz_entry.ts", "import './a249.ts';\nimport './a248.ts';\nexport const z = 1;\n")
        url = lambda rel: f"file://{project.resolve() / rel}"
        modules = [{"specifier": url(name), "dependencies": []} for name in names]
        modules.append({"specifier": url("src/zz_entry.ts"), "dependencies": [
            {"specifier": "./a249.ts", "code": {"specifier": url("src/a249.ts")}},
            {"specifier": "./a248.ts", "code": {"specifier": url("src/a248.ts")}}]})
        answer = self.nav(project, deno=self.scripted_deno(info={"modules": modules})).forward_graph("src/zz_entry.ts")
        listed = {m["path"]: m["imports"] for m in answer["modules"]}
        self.assertEqual((answer["status"], answer["total"], len(listed), answer["truncated"]), ("ok", 251, 200, True))
        self.assertEqual(listed["src/zz_entry.ts"], ["src/a248.ts", "src/a249.ts"])
        self.assertIn("src/a249.ts", listed)
        self.assertEqual([m["path"] for m in answer["modules"]], sorted(listed))
        self.assertIn("src/a000.ts", listed)
        self.assertNotIn("src/a199.ts", listed)


if __name__ == "__main__":
    unittest.main()
