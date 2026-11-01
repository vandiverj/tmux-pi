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
MIRROR = "@ROOT@"  # in a fake deno's canned output: the directory the fake runs in, which is the mirror Deno was given
HAVE_DENO = shutil.which("deno") is not None
NEEDS_DENO = unittest.skipUnless(HAVE_DENO, "deno is not on PATH")


# The fake `deno` scripts of these tests are shell scripts that run `sed`, `find` and write their logs next to the test: they need
# what the production profile (nav_api.SANDBOX_PROFILE) denies. They still run under sandbox-exec, with network denied; only the
# file rules are lifted. `SandboxCase` runs the real Deno under the production profile.
LOOSE_PROFILE = "(version 1)\n(allow default)\n(deny network*)\n"
SANDBOX_PRODUCTION = nav_api.SANDBOX_PROFILE  # captured before any test patches it


def stub_allow(project):
    """The guard NavApi uses, for an index built directly: allow(rel) is True when the stub check path accepts `rel`."""
    def allow(rel):
        try:
            nav_api.stub_check_path(str(project), rel)
        except nav_api.Refused:
            return False
        return True
    return allow


def project_cache(project, name="index.json"):
    """The default cache folder of a project (inside it, where the guard allows it), and a file in it."""
    return Path(project) / ".tmux-pi" / "ctx-cache" / name


def write(path, text=""):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class Tree(unittest.TestCase):
    """A temporary directory per test, with helpers to copy the fixture project into it or to write a fake `deno`."""

    profile = LOOSE_PROFILE  # None: the production profile

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name).resolve()
        if self.profile is not None:
            patch = mock.patch.object(nav_api, "SANDBOX_PROFILE", self.profile)
            patch.start()
            self.addCleanup(patch.stop)

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
        return nav_api.NavApi(str(project), **kw)  # the cache is the default one, inside the project


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
        return graph.resolve(spec, self.importer, self.imap, os.path.isfile)

    def test_relative_and_import_map_targets(self):
        self.assertEqual(self.res("./a.ts"), ("file", str(self.dir / "src" / "a.ts"), ""))
        self.assertEqual(self.res("@/a.ts"), ("file", str(self.dir / "src" / "a.ts"), ""))
        self.assertEqual(self.res("@x"), ("file", str(self.dir / "src" / "a.ts"), ""))
        self.assertEqual(self.res("@types/t.ts"), ("file", str(self.dir / "lib" / "types" / "t.ts"), ""))

    def test_the_longest_key_wins(self):
        imap = graph.ImportMap({"@/": "./src/", "@/deep/": "./lib/types/"}, str(self.dir))
        self.assertEqual(graph.resolve("@/deep/t.ts", self.importer, imap, os.path.isfile)[1], str(self.dir / "lib" / "types" / "t.ts"))

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
        kw.setdefault("cache_path", str(project_cache(project)))
        kw.setdefault("allow", stub_allow(project))
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
        link = project_cache(project)
        link.parent.mkdir(parents=True)
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
        self.assertEqual([(u["spec"], u["reason"]) for u in index.unresolved()], [("", "refused")])  # leads out through a link: blanked; the guard follows the link and refuses the name before graph looks at it

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

        def racing(paths, *a, **k):
            out = real(paths, *a, **k)
            target.write_text("import './config.ts'; // second edit, a different size\n")
            return out

        with mock.patch.object(graph, "read_sources", racing):
            stats = index.refresh()
        self.assertEqual(stats["stale"], ["src/lazy.ts"])
        after = index.refresh()
        self.assertEqual((after["scanned"], after["stale"]), (1, []))
        self.assertIn("src/lazy.ts", [e["from"] for e in index.importers("src/config.ts")])

    def test_an_unreadable_or_oversized_file_is_not_indexed_does_not_stop_the_build_and_is_a_gap(self):
        """Round 10: it used to be an entry with no imports (a file that imports nothing). It is no entry, and `gaps` says so; the
        rest of the index is built, and `NavApi` answers nothing from it (`IncompleteIndexCase`)."""
        project = self.project()
        write(project / "src" / "huge.ts", "import './config.ts';\n" + "// pad\n" * 10)
        with mock.patch.object(graph, "MAX_BYTES", 30):
            index = self.index(project, cache_path=None)
            index.refresh()
        self.assertIsNone(index.imports("src/huge.ts"))
        self.assertIn("source_unreadable", index.gaps)  # (with the limit at 30 bytes, the other files and the config are over it too)
        self.assertEqual(index.incomplete()[:2], ("incomplete", "index_incomplete"))

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
        self.assertEqual(seen[0], (str(project.resolve()), ".tmux-pi/ctx-cache"))  # the cache folder is judged once, by the guard
        self.assertEqual(seen[1], (str(project.resolve()), "src/main.ts"))

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
        self.assertEqual((answer["status"], answer["detail"]), ("error", 'error: Module not found "<file>"'))  # a path Deno quotes is not shown

    def test_output_that_is_not_what_deno_prints_is_an_error_not_a_crash(self):
        project = self.project()
        for body in ("echo not json", "echo '[]'", "echo '{}'", "echo '{\"modules\": 5}'"):
            nav = self.nav(project, deno=self.fake_deno(body))
            self.assertEqual(nav.forward_graph("src/main.ts")["status"], "error", body)
            self.assertEqual(nav.symbols("src/main.ts")["status"], "error", body)

    @NEEDS_DENO
    def test_the_same_question_gives_the_same_answer(self):
        project = self.project()
        a, b = self.nav(project), self.nav(project, cache_dir=str(project / ".tmux-pi" / "other-cache"))
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
            f'echo "$@" >> {log}\ncase "$1" in\n --version) echo "deno 9.9.9";;\n'
            f' doc) sed "s|{MIRROR}|$PWD|g" {self.dir / "doc.json"};;\n info) sed "s|{MIRROR}|$PWD|g" {self.dir / "info.json"};;\nesac')

    def deno_calls(self, verb):
        log = self.dir / "deno.log"
        return [line for line in (log.read_text().splitlines() if log.exists() else []) if line.startswith(verb + " ")]

    @staticmethod
    def doc_payload(root, rel, name, doc, line=0):
        """A canned `deno doc` answer with one export at `rel` below `root` (MIRROR, or another directory for a file outside)."""
        return {"version": 2, "nodes": {"n": {"symbols": [{"name": name, "declarations": [
            {"declarationKind": "export", "kind": "function", "jsDoc": {"doc": doc},
             "location": {"filename": f"file://{root}/{rel}", "line": line}}]}]}}}

    # --- F1

    def test_a_refused_file_is_never_read_indexed_or_named(self):
        project = self.project()
        write(project / "src" / ".env.ts", "import './hidden_marker.ts';\nexport const KEY = 'x';\n")
        write(project / "src" / "credentials.ts", "import './hidden_marker2.ts';\n")
        write(project / "src" / "gate.ts", "import { KEY } from './.env.ts';\nimport './credentials.ts';\nimport { c } from './config.ts';\n"
                                           "export { KEY, c };\n")
        opened = []
        real = graph.read_source
        with mock.patch.object(graph, "read_source", lambda path, *a: opened.append(path) or real(path, *a)):
            answer = self.nav(project).imports("src/gate.ts")
        self.assertFalse([p for p in opened if p.endswith((".env.ts", "credentials.ts"))], opened)
        self.assertEqual(answer["status"], "ok")
        self.assertEqual([f["path"] for f in answer["files"]], ["src/config.ts"])
        self.assertEqual(sorted((u["reason"], u["spec"]) for u in answer["unresolved"]), [("refused", ""), ("refused", "")])
        text = json.dumps(answer)
        for secret in (".env.ts", "credentials", "hidden_marker"):
            self.assertNotIn(secret, text)
        for cached in project_cache(project).parent.iterdir():
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
        self.assertEqual([(u["spec"], u["reason"]) for u in answer["unresolved"]], [("", "refused")])  # the guard follows the link and refuses the name before graph looks at it
        self.assertEqual(nav.importers("src/linked_dir/secret.ts")["status"], "refused")
        self.assertEqual(nav._get_index().importers("src/linked_dir/secret.ts"), [])
        for call in (nav.symbols, nav.forward_graph):
            refused = call("src/viaparent.ts")
            self.assertEqual((refused["status"], refused["reason"]), ("refused", "reaches_refused_path"), call.__name__)  # the guard
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
        doc = self.doc_payload(MIRROR, "src/config.ts", "kept", "fine")
        doc["nodes"]["n"]["symbols"] += self.doc_payload(MIRROR, "src/.env.ts", "leak", "x")["nodes"]["n"]["symbols"]
        doc["nodes"]["n"]["symbols"] += self.doc_payload(str(self.dir), "outside.ts", "away", "x")["nodes"]["n"]["symbols"]
        shown = self.nav(project, deno=self.scripted_deno(doc=doc)).symbols("src/main.ts")
        self.assertEqual([s["name"] for s in shown["symbols"]], ["kept"])

    # --- F3

    def test_cached_symbols_are_screened_with_the_guard_of_the_reader(self):
        project = self.project()
        exe = self.scripted_deno(doc=self.doc_payload(MIRROR, "src/app/run.ts", "run", "uses SECRETTOKEN here", line=4))
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
        write(project / "src" / "gate.ts", "import './credentials.ts';\nexport { c } from './config.ts';\n")
        doc = self.doc_payload(MIRROR, "src/config.ts", "kept", "fine")
        doc["nodes"]["n"]["symbols"] += self.doc_payload(MIRROR, "src/credentials.ts", "secretive", "x")["nodes"]["n"]["symbols"]
        exe = self.scripted_deno(doc=doc)
        permissive = self.nav(project, deno=exe, check_path=lambda root, path: path)
        self.assertEqual([s["name"] for s in permissive.symbols("src/gate.ts")["symbols"]], ["kept", "secretive"])
        again = self.nav(project, deno=exe).symbols("src/gate.ts")  # the cache holds the symbol; the walk now refuses the target
        self.assertEqual((again["status"], again["reason"]), ("refused", "reaches_refused_path"))
        self.assertNotIn("secretive", json.dumps(again))

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
        exe = self.scripted_deno(doc=self.doc_payload(MIRROR, "src/app/run.ts", "run", "x"))
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
        write(project / "src" / "zz_entry.ts", "import './a249.ts';\nimport './a248.ts';\n"
              + "".join(f"import './a{n:03d}.ts';\n" for n in range(248)) + "export const z = 1;\n")  # Deno's graph is what the walk reached
        url = lambda rel: f"file://{MIRROR}/{rel}"
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


class AllowListCase(Tree):
    """Round 3. Deno runs on a target only after a walk of every module reachable from it has shown each one allowed (family
    path-boundary, found in reviews 1 and 2). The walk reads the files itself: it does not depend on the index, so a file the index
    does not hold (excluded by deno.json) is followed like any other. Each refusal cause has its own test."""

    scripted_deno = ReviewFixesCase.scripted_deno
    deno_calls = ReviewFixesCase.deno_calls
    doc_payload = staticmethod(ReviewFixesCase.doc_payload)

    def deno_nav(self, project, **kw):
        return self.nav(project, deno=self.scripted_deno(doc={"nodes": {}}, info={"modules": []}), **kw)

    def assert_refused(self, project, target, reason, nav=None):
        """Both Deno operations refuse `target` for `reason`, and Deno is not started for either."""
        nav = nav or self.deno_nav(project)
        for call in (nav.symbols, nav.forward_graph):
            answer = call(target)
            self.assertEqual((answer["status"], answer.get("reason")), ("refused", reason), call.__name__)
        self.assertEqual(self.deno_calls("doc") + self.deno_calls("info"), [])
        return nav

    def test_a_spread_before_a_dynamic_import_is_still_a_real_import(self):
        """`...import('./x.ts')` is the spread of a real import, not a property named import, so the walk follows it."""
        project = self.project()
        write(project / "src" / ".env.ts", "export const KEY = 1;\n")
        write(project / "src" / "top.ts", "export const all = [...await import('./.env.ts')];\n")
        self.assertEqual([s["spec"] for s in graph.scan_specifiers("f(...import('./y.ts'))")], ["./y.ts"])
        self.assertEqual(graph.scan_specifiers("a.import('./z.ts'); a?.import('./z.ts')"), [])  # a property named import
        self.assert_refused(project, "src/top.ts", "reaches_refused_path")

    # --- the reviewer's repro

    def test_a_refused_file_behind_an_excluded_file_is_reached_and_refuses(self):
        project = self.project()
        write(project / "src" / ".env.ts", "import x from './ghost_SECRETNAME.ts';\nexport const KEY = x;\n")
        write(project / "skipped" / "mid.ts", "export { KEY } from '../src/.env.ts';\n")  # skipped/ is in deno.json "exclude"
        write(project / "src" / "top.ts", "export { KEY } from '../skipped/mid.ts';\n")
        self.assertFalse(self.nav(project)._get_index().indexed("skipped/mid.ts"))  # the index does not hold the middle file
        self.assert_refused(project, "src/top.ts", "reaches_refused_path")

    @NEEDS_DENO
    def test_the_repro_with_the_real_deno_names_nothing(self):
        project = self.project()
        write(project / "src" / ".env.ts", "import x from './ghost_SECRETNAME.ts';\nexport const KEY = x;\n")
        write(project / "skipped" / "mid.ts", "export { KEY } from '../src/.env.ts';\n")
        write(project / "src" / "top.ts", "export { KEY } from '../skipped/mid.ts';\n")
        nav = self.nav(project)
        for answer in (nav.symbols("src/top.ts"), nav.forward_graph("src/top.ts"), nav.imports("src/top.ts", source="deno")):
            self.assertEqual(answer["status"], "refused")
            self.assertNotIn("SECRETNAME", json.dumps(answer))

    @NEEDS_DENO
    def test_an_excluded_file_that_reaches_nothing_refused_is_followed_and_listed(self):
        project = self.project()
        write(project / "skipped" / "mid.ts", "import { c } from '../src/config.ts';\nexport const m = c;\n")
        write(project / "src" / "top.ts", "export { m } from '../skipped/mid.ts';\n")
        answer = self.nav(project).forward_graph("src/top.ts")
        self.assertEqual(answer["status"], "ok")
        self.assertTrue({m["path"] for m in answer["modules"]} >= {"skipped/mid.ts", "src/config.ts"})

    # --- one test per refusal cause

    def test_cause_a_reached_file_that_leaves_the_project(self):
        project = self.project()
        write(self.dir / "outside.ts", "export const o = 1;\n")
        write(project / "skipped" / "mid.ts", "export { o } from '../../outside.ts';\n")
        write(project / "src" / "top.ts", "export { o } from '../skipped/mid.ts';\n")
        self.assert_refused(project, "src/top.ts", "outside_project")

    def test_cause_a_reached_symlink_that_leads_out(self):
        project = self.project()
        outside = write(self.dir / "outside" / "secret.ts", "export const s = 1;\n")
        (project / "skipped" / "link.ts").symlink_to(outside)
        write(project / "skipped" / "mid.ts", "export { s } from './link.ts';\n")
        write(project / "src" / "top.ts", "export { s } from '../skipped/mid.ts';\n")
        self.assert_refused(project, "src/top.ts", "reaches_refused_path")  # the guard follows the link and refuses the name before graph looks at it

    def test_cause_a_percent_encoded_dot_dot_that_the_url_parser_turns_into_a_parent_directory(self):
        project = self.project()
        write(self.dir / "outside.ts", "export const o = 1;\n")
        write(project / "src" / "top.ts", "export { o } from './%2e%2e/%2e%2e/outside.ts';\n")  # Deno reads ../../outside.ts
        self.assert_refused(project, "src/top.ts", "outside_project")

    def test_cause_a_percent_encoded_name_of_a_refused_file(self):
        project = self.project()
        write(project / "src" / ".env.ts", "export const KEY = 1;\n")
        write(project / "src" / "top.ts", "export { KEY } from './%2eenv.ts';\n")
        self.assert_refused(project, "src/top.ts", "reaches_refused_path")

    def test_cause_an_import_map_entry_that_points_at_a_refused_file(self):
        project = self.project()
        write(project / "src" / ".env.ts", "export const KEY = 1;\n")
        config = json.loads((project / "deno.json").read_text())
        config["imports"]["@secret"] = "./src/.env.ts"
        (project / "deno.json").write_text(json.dumps(config))
        write(project / "skipped" / "mid.ts", "export { KEY } from '@secret';\n")
        write(project / "src" / "top.ts", "export { KEY } from '../skipped/mid.ts';\n")
        self.assert_refused(project, "src/top.ts", "reaches_refused_path")

    def test_cause_a_scope_of_the_import_map_that_points_at_a_refused_file(self):
        project = self.project()
        write(project / "src" / ".env.ts", "export const KEY = 1;\n")
        config = json.loads((project / "deno.json").read_text())
        config["scopes"] = {"./skipped/": {"@util": "./src/.env.ts"}}  # inside skipped/, "@util" is the refused file
        (project / "deno.json").write_text(json.dumps(config))
        write(project / "skipped" / "mid.ts", "export { KEY } from '@util';\n")
        write(project / "src" / "top.ts", "export { KEY } from '../skipped/mid.ts';\n")
        self.assert_refused(project, "src/top.ts", "reaches_refused_path")
        write(project / "src" / "other.ts", "export { slug } from '@util';\n")  # outside the scope "@util" is the util module
        self.assertEqual(self.deno_nav(project).symbols("src/other.ts")["status"], "ok")

    def test_cause_a_scope_that_cannot_be_modelled(self):
        project = self.project()
        for scopes in ({"./skipped/": "not a table"}, ["list"], {"./skipped/": {"@util": 5}}):
            config = json.loads((project / "deno.json").read_text())
            config["scopes"] = scopes
            (project / "deno.json").write_text(json.dumps(config))
            self.assert_refused(project, "src/main.ts", "unmodelled_config", self.deno_nav(project))

    def test_cause_a_config_that_changes_resolution_in_a_way_the_walk_does_not_model(self):
        project = self.project()
        original = (project / "deno.json").read_text()
        for key, value in (("importMap", "./map.json"), ("workspace", ["./a"]), ("links", ["../x"]), ("patch", ["../x"]),
                           ("unstable", ["sloppy-imports"])):
            config = json.loads(original)
            config[key] = value
            (project / "deno.json").write_text(json.dumps(config))
            self.assert_refused(project, "src/main.ts", "unmodelled_config", self.deno_nav(project))
        (project / "deno.json").write_text("{ not json")
        self.assert_refused(project, "src/main.ts", "unmodelled_config", self.deno_nav(project))
        (project / "deno.json").write_text(original)
        self.assertEqual(self.deno_nav(project).symbols("src/main.ts")["status"], "ok")

    def test_cause_a_package_json_dependency_that_is_a_local_path(self):
        project = self.project()
        write(project / "package.json", json.dumps({"dependencies": {"leaked": "file:../outside"}}))
        self.assert_refused(project, "src/main.ts", "unmodelled_config")

    def test_cause_a_compiler_option_that_loads_a_refused_file(self):
        project = self.project()
        write(project / "src" / ".env.ts", "export const KEY = 1;\n")
        config = json.loads((project / "deno.json").read_text())
        config["compilerOptions"] = {"types": ["./src/.env.ts"]}
        (project / "deno.json").write_text(json.dumps(config))
        self.assert_refused(project, "src/main.ts", "reaches_refused_path")

    def test_cause_a_dynamic_import_with_an_argument_that_is_not_a_literal(self):
        project = self.project()
        write(project / "skipped" / "mid.ts", "export const load = (n: string) => import(`./${n}.ts`);\n")
        write(project / "src" / "top.ts", "export { load } from '../skipped/mid.ts';\n")
        self.assert_refused(project, "src/top.ts", "unmodelled_import")
        write(project / "src" / "meta.ts", "export const u = import.meta.resolve('./x.ts');\n")
        self.assert_refused(project, "src/meta.ts", "unmodelled_import")
        self.assertEqual(self.deno_nav(project).symbols("src/main.ts")["status"], "ok")  # literal dynamic imports are followed

    def test_cause_a_pragma_that_names_a_module(self):
        project = self.project()
        write(project / "src" / ".env.ts", "export const KEY = 1;\n")
        for pragma in ('// @ts-types="./.env.ts"\nimport "./config.ts";\n', '// @deno-types="./.env.ts"\nimport "./config.ts";\n',
                       '/// <reference types="./.env.ts" />\n', '/// <reference path="./.env.ts" />\n',
                       '/** @jsxImportSource ./.env.ts */\n'):
            write(project / "skipped" / "mid.ts", pragma)
            write(project / "src" / "top.ts", "import '../skipped/mid.ts';\n")
            self.assert_refused(project, "src/top.ts", "reaches_refused_path")

    def test_cause_a_reached_file_the_walk_cannot_read(self):
        project = self.project()
        write(project / "skipped" / "big.ts", "export const big = 1;\n" + "// " + "x" * (graph.MAX_BYTES + 10) + "\n")
        write(project / "src" / "top.ts", "export { big } from '../skipped/big.ts';\n")
        self.assert_refused(project, "src/top.ts", "unreadable_file")

    def test_cause_a_graph_longer_than_the_walk_will_follow(self):
        project = self.project()
        write(project / "src" / "top.ts", "".join(f"import './g{n}.ts';\n" for n in range(6)))
        for n in range(6):
            write(project / "src" / f"g{n}.ts", "export {};\n")
        with mock.patch.object(graph, "MAX_REACH_FILES", 4):
            self.assert_refused(project, "src/top.ts", "too_many_files")

    def test_cause_a_walk_that_fails_unexpectedly(self):
        project = self.project()
        nav = self.deno_nav(project)
        with mock.patch.object(graph, "prove_reach", side_effect=RuntimeError("boom")):
            self.assert_refused(project, "src/main.ts", "precheck_failed", nav)

    def test_the_walk_does_not_depend_on_the_index(self):
        project = self.project()
        write(project / "src" / ".env.ts", "export const KEY = 1;\n")
        write(project / "skipped" / "mid.ts", "export { KEY } from '../src/.env.ts';\n")
        write(project / "src" / "top.ts", "export { KEY } from '../skipped/mid.ts';\n")
        nav = self.deno_nav(project)
        with mock.patch.object(graph.ImportIndex, "refresh", side_effect=OSError("disk")):
            self.assert_refused(project, "src/top.ts", "reaches_refused_path", nav)
            self.assertEqual(nav.symbols("src/main.ts")["status"], "ok")  # a clean target does not need the index

    # --- what Deno says is held to the same list

    def test_a_module_deno_lists_that_the_walk_did_not_reach_refuses_the_answer(self):
        project = self.project()
        write(project / "src" / "stray.ts", "export const s = 1;\n")
        url = lambda rel: f"file://{MIRROR}/{rel}"
        info = {"modules": [{"specifier": url("src/config.ts"), "dependencies": []},
                            {"specifier": url("src/stray.ts"), "dependencies": []}]}
        nav = self.nav(project, deno=self.scripted_deno(info=info))
        answer = nav.forward_graph("src/config.ts")
        self.assertEqual((answer["status"], answer["reason"]), ("refused", "unproven_module"))
        self.assertNotIn("stray", json.dumps(answer))

    def test_a_file_that_changes_while_deno_runs_gives_no_answer(self):
        project = self.project()
        target = project / "src" / "config.ts"
        exe = self.fake_deno('case "$1" in\n --version) echo "deno 9.9.9";;\n *) echo "// changed" >> ' + str(target)
                             + '; echo \'{"modules": []}\';;\nesac')
        answer = self.nav(project, deno=exe).forward_graph("src/config.ts")
        self.assertEqual(answer["status"], "stale_index")
        self.assertNotIn("modules", answer)

    def test_the_doc_cache_key_covers_an_excluded_file_the_target_reaches(self):
        project = self.project()
        write(project / "skipped" / "mid.ts", "export const m = 1;\n")
        write(project / "src" / "top.ts", "export { m } from '../skipped/mid.ts';\n")
        exe = self.scripted_deno(doc=self.doc_payload(MIRROR, "skipped/mid.ts", "m", "x"))
        self.nav(project, deno=exe).symbols("src/top.ts")
        self.assertTrue(self.nav(project, deno=exe).symbols("src/top.ts")["cached"])
        write(project / "skipped" / "mid.ts", "export const m = 2;\n")
        self.assertFalse(self.nav(project, deno=exe).symbols("src/top.ts")["cached"])

    def test_a_symbol_from_a_file_outside_the_walk_is_not_returned(self):
        project = self.project()
        write(project / "src" / "stray.ts", "export const s = 1;\n")
        doc = self.doc_payload(MIRROR, "src/config.ts", "kept", "fine")
        doc["nodes"]["n"]["symbols"] += self.doc_payload(MIRROR, "src/stray.ts", "stray", "x")["nodes"]["n"]["symbols"]
        shown = self.nav(project, deno=self.scripted_deno(doc=doc)).symbols("src/config.ts")
        self.assertEqual([s["name"] for s in shown["symbols"]], ["kept"])

    def test_deno_is_always_given_a_config_of_the_mirror_and_never_looks_for_one(self):
        """With a config and without one, Deno gets `--config` naming the mirror's own config, so it finds none above the mirror."""
        write(self.dir / "plain" / "a.ts", "export const a = 1;\n")
        nav = self.nav(self.dir / "plain", deno=self.scripted_deno(doc={"nodes": {}}, info={"modules": []}))
        nav.symbols("a.ts")
        proj = self.project()
        self.nav(proj, deno=self.scripted_deno(doc={"nodes": {}})).symbols("src/main.ts")
        for line in self.deno_calls("doc"):
            words = line.split()
            self.assertIn("--config", words)
            self.assertTrue(words[words.index("--config") + 1].endswith("deno.json"), line)
            self.assertNotIn("--no-config", words)
            self.assertIn("--no-remote", words)

    # --- specifier strings in answers

    def test_a_specifier_that_names_a_path_outside_or_refused_is_blanked_whether_or_not_the_file_exists(self):
        project = self.project()
        write(self.dir / "outside.ts", "export const o = 1;\n")
        write(project / "src" / ".env.ts", "export const KEY = 1;\n")
        write(project / "src" / "named.ts", "\n".join([
            "import '../../outside.ts';", "import '../../../.env';", "import './.env.local';", "import './%2e%2e/%2e%2e/outside.ts';",
            "import './.env.ts';", "import './nope.ts';", "import 'left-pad';", ""]))
        nav = self.nav(project)
        answer = nav.imports("src/named.ts")
        by_line = {u["line"]: u for u in answer["unresolved"]}
        for line in (1, 2, 3, 4, 5):
            self.assertEqual(by_line[line]["spec"], "", line)
        self.assertEqual((by_line[6]["spec"], by_line[6]["reason"]), ("./nope.ts", "missing"))  # allowed, so named as written
        self.assertEqual((by_line[7]["spec"], by_line[7]["reason"]), ("left-pad", "bare"))
        text = json.dumps([answer, nav._get_index().unresolved()])
        for needle in ("outside.ts", ".env", "%2e"):
            self.assertNotIn(needle, text)

    def test_a_specifier_reached_through_the_import_map_to_a_refused_path_is_blanked(self):
        project = self.project()
        config = json.loads((project / "deno.json").read_text())
        config["imports"]["@gone"] = "../../elsewhere.ts"
        (project / "deno.json").write_text(json.dumps(config))
        write(project / "src" / "named.ts", "import '@gone';\n")
        answer = self.nav(project).imports("src/named.ts")
        self.assertEqual([(u["spec"], u["reason"]) for u in answer["unresolved"]], [("", "outside")])  # judged by name: the same whether or not a file is there

    # --- scopes in the index

    def test_scopes_resolve_longest_scope_first_then_fall_back_to_the_top_level_map(self):
        write(self.dir / "a.ts")
        write(self.dir / "scoped" / "a.ts")
        write(self.dir / "scoped" / "deep" / "a.ts")
        imap = graph.ImportMap({"@a": "./a.ts"}, str(self.dir), scopes={
            "./scoped/": {"@a": "./scoped/a.ts"}, "./scoped/deep/": {"@a": "./scoped/deep/a.ts"}, "./scoped/x/": {"@other": "./a.ts"}})

        def res(spec, referrer):
            return graph.resolve(spec, str(self.dir / referrer), imap, os.path.isfile)[1]
        self.assertEqual(res("@a", "main.ts"), str(self.dir / "a.ts"))
        self.assertEqual(res("@a", "scoped/m.ts"), str(self.dir / "scoped" / "a.ts"))
        self.assertEqual(res("@a", "scoped/deep/m.ts"), str(self.dir / "scoped" / "deep" / "a.ts"))
        self.assertEqual(res("@a", "scoped/x/m.ts"), str(self.dir / "scoped" / "a.ts"))  # x/ has no "@a": the next scope up
        self.assertFalse(imap.unmodelled)

    def test_the_index_follows_scopes_and_no_longer_reports_them_as_ignored(self):
        project = self.project()
        config = json.loads((project / "deno.json").read_text())
        config["scopes"] = {"./src/app/": {"@util": "./src/config.ts"}}
        (project / "deno.json").write_text(json.dumps(config))
        write(project / "src" / "app" / "scoped.ts", "import { c } from '@util';\nexport { c };\n")
        nav = self.nav(project)
        self.assertEqual([f["path"] for f in nav.imports("src/app/scoped.ts")["files"]], ["src/config.ts"])
        self.assertNotIn("ignored_config", nav.importers("src/config.ts"))


class MirrorCase(Tree):
    """Round 4. Deno is run on a temporary mirror of the files the walk proved, not on the project: whatever the walk misses or
    misreads is not in the mirror, so Deno cannot open it (family path-boundary, found in reviews 1, 2 and 3). The `recording_deno`
    wrapper writes what the directory Deno starts in holds, then runs the real Deno."""

    scripted_deno = ReviewFixesCase.scripted_deno
    deno_calls = ReviewFixesCase.deno_calls
    doc_payload = staticmethod(ReviewFixesCase.doc_payload)

    def recording_deno(self):
        """The real `deno`, behind a script that first lists every entry of the mirror it is started in (tree.log) and what it
        is given as arguments (args.log). Anything Deno could open below its working directory is in tree.log."""
        real = shutil.which("deno")
        return self.fake_deno(f'echo "$@" >> {self.dir / "args.log"}\n'
                              f'case "$1" in --version) ;; *) find . | sort >> {self.dir / "tree.log"}\n'
                              f'find . ! -type f ! -type d >> {self.dir / "special.log"};; esac\nexec {real} "$@"')

    def tree_seen_by_deno(self):
        log = self.dir / "tree.log"
        return log.read_text() if log.exists() else ""

    def refused_project(self):
        project = self.project()
        write(project / ".env.dev", "SECRET_CANARY=1\n")
        write(project / "src" / ".env.ts", "export const KEY = 'CANARY_VALUE';\n")
        return project

    def assert_deno_never_could_see(self, answers, canary=".env"):
        self.assertTrue(self.tree_seen_by_deno(), "Deno was not started, so the repro proves nothing")
        self.assertNotIn(canary, self.tree_seen_by_deno())
        for answer in answers:
            self.assertNotIn("CANARY", json.dumps(answer))

    # --- F1 of review 3: import-map entries the walk does not model

    def test_f1_an_import_map_key_that_is_a_path_or_a_file_url_is_not_modelled(self):
        project = self.refused_project()
        write(project / "src" / "real.ts", "export const KEY = 1;\n")
        write(project / "src" / "t2.ts", "export { KEY } from './real.ts';\n")
        original = json.loads((project / "deno.json").read_text())
        for key in ("./src/real.ts", "../proj/src/real.ts", "/src/real.ts", f"file://{project}/src/real.ts", ".\\src\\real.ts"):
            config = json.loads(json.dumps(original))
            config["imports"][key] = "./src/.env.ts"
            (project / "deno.json").write_text(json.dumps(config))
            nav = self.nav(project, deno=self.scripted_deno(doc={"nodes": {}}, info={"modules": []}))
            for call in (nav.symbols, nav.forward_graph):
                answer = call("src/t2.ts")
                self.assertEqual((answer["status"], answer.get("reason")), ("refused", "unmodelled_config"), (key, call.__name__))
        self.assertEqual(self.deno_calls("doc") + self.deno_calls("info"), [])

    def test_f1_an_import_map_target_that_is_a_file_url_or_an_absolute_path_is_not_modelled(self):
        project = self.refused_project()
        write(project / "src" / "t2.ts", "export { KEY } from '@s';\n")
        original = json.loads((project / "deno.json").read_text())
        for target in (f"file://{project}/src/.env.ts", f"FILE:///{project}/src/.env.ts", f"{project}/src/.env.ts", ".\\src\\.env.ts"):
            config = json.loads(json.dumps(original))
            config["imports"]["@s"] = target
            (project / "deno.json").write_text(json.dumps(config))
            nav = self.nav(project, deno=self.scripted_deno(doc={"nodes": {}}, info={"modules": []}))
            for call in (nav.symbols, nav.forward_graph):
                answer = call("src/t2.ts")
                self.assertEqual((answer["status"], answer.get("reason")), ("refused", "unmodelled_config"), (target, call.__name__))
        self.assertEqual(self.deno_calls("doc") + self.deno_calls("info"), [])

    def test_f1_the_same_rules_hold_inside_a_scope(self):
        project = self.refused_project()
        original = json.loads((project / "deno.json").read_text())
        for table in ({"./src/real.ts": "./src/.env.ts"}, {"@s": f"file://{project}/src/.env.ts"}):
            config = json.loads(json.dumps(original))
            config["scopes"] = {"./src/": table}
            (project / "deno.json").write_text(json.dumps(config))
            nav = self.nav(project, deno=self.scripted_deno(doc={"nodes": {}}, info={"modules": []}))
            self.assertEqual(nav.symbols("src/main.ts")["reason"], "unmodelled_config", table)

    def test_f1_an_ordinary_relative_target_and_a_scope_key_are_still_modelled(self):
        imap = graph.ImportMap({"@/": "./src/", "@up": "../lib/x.ts", "zod": "npm:zod@4"}, str(self.dir),
                               scopes={"./vendor/": {"@/": "./other/"}})
        self.assertFalse(imap.unmodelled)

    @NEEDS_DENO
    def test_f1_the_repro_with_the_real_deno_is_refused_before_deno_starts(self):
        project = self.refused_project()
        write(project / "src" / "real.ts", "export const KEY = 1;\n")
        write(project / "src" / "t2.ts", "export { KEY } from './real.ts';\n")
        config = json.loads((project / "deno.json").read_text())
        config["imports"]["./src/real.ts"] = "./src/.env.ts"
        (project / "deno.json").write_text(json.dumps(config))
        nav = self.nav(project, deno=self.recording_deno())
        answers = [nav.symbols("src/t2.ts"), nav.forward_graph("src/t2.ts")]
        self.assertEqual([a["status"] for a in answers], ["refused", "refused"])
        self.assertEqual(self.tree_seen_by_deno(), "")  # refused before Deno was started
        config["imports"].pop("./src/real.ts")
        config["imports"]["@s"] = f"file://{project}/src/.env.ts"
        (project / "deno.json").write_text(json.dumps(config))
        self.assertEqual(nav.symbols("src/t2.ts")["reason"], "unmodelled_config")
        self.assertEqual(self.tree_seen_by_deno(), "")

    # --- F2 of review 3: a construct the scanner misreads

    @NEEDS_DENO
    def test_f2_a_scanner_that_sees_no_import_at_all_still_leaves_the_refused_file_out_of_denos_reach(self):
        """The scanner now reads `a++ / 2` as a division. This test keeps the scanner out of it: with the scanner made blind to every
        import, Deno is started on the target and still cannot open the refused file, because it is not in the mirror."""
        project = self.refused_project()
        write(project / "src" / "t3.ts", "let a = 1;\na++ / 2; export const y = import('./.env.ts');\n")
        blind = {"specs": [], "pragmas": [], "unmodelled": []}
        nav = self.nav(project, deno=self.recording_deno())
        with mock.patch.object(graph, "scan_module", lambda text: blind):
            graph._SCAN_MEMO.clear()
            try:
                answers = [nav.symbols("src/t3.ts"), nav.forward_graph("src/t3.ts")]
            finally:
                graph._SCAN_MEMO.clear()
        self.assertEqual([a["status"] for a in answers], ["ok", "ok"])
        self.assert_deno_never_could_see(answers)
        self.assertEqual([m["path"] for m in answers[1]["modules"]], ["src/t3.ts"])  # the refused module is neither listed nor named
        self.assertNotIn(".env.ts", json.dumps(answers[1]))

    @NEEDS_DENO
    def test_f2_with_the_scanner_as_it_is_the_repros_refuse_before_deno_starts(self):
        project = self.refused_project()
        write(project / "src" / "t3.ts", "let a = 1;\na++ / 2; export const y = import('./.env.ts');\n")
        write(project / "src" / "t4.tsx", "const a = <div>x</div>; const b = await import('./.env.ts');\n")
        write(project / "src" / "t5.ts", "const a = 2; a-- / 2; const t = x.if(1) / 2; const u = import('./.env.ts');\n")
        nav = self.nav(project, deno=self.recording_deno())
        for target in ("src/t3.ts", "src/t4.tsx", "src/t5.ts"):
            for call in (nav.symbols, nav.forward_graph):
                answer = call(target)
                self.assertEqual((answer["status"], answer.get("reason")), ("refused", "reaches_refused_path"), (target, call.__name__))
        self.assertEqual(self.tree_seen_by_deno(), "")

    def test_f2_the_scanner_reads_a_slash_after_postfix_operators_member_keywords_and_closing_tags_as_it_should(self):
        def specs(src):
            return [s["spec"] for s in graph.scan_specifiers(src)]

        self.assertEqual(specs("let a = 1;\na++ / 2; export const y = import('./a.ts');"), ["./a.ts"])
        self.assertEqual(specs("a-- / 2; import('./a.ts')"), ["./a.ts"])
        self.assertEqual(specs("y.if(1) /2; import('./a.ts')"), ["./a.ts"])
        self.assertEqual(specs("z.while(1) / 2; import('./a.ts')"), ["./a.ts"])
        self.assertEqual(specs("a.in / 2; b.return / 3; import('./a.ts')"), ["./a.ts"])
        self.assertEqual(specs("const a = <div>x</div>; const b = await import('./a.ts');"), ["./a.ts"])
        for still_a_regex in ("if (a) /import('g.ts')/.test(b);", "const r = 1 + /import('g.ts')/.test(x)", "return /import('g.ts')/",
                              "x = a ? /import('g.ts')/ : 1"):
            self.assertEqual(specs(still_a_regex), [], still_a_regex)

    # --- the mirror itself

    def proved(self, project, target="src/main.ts", **config):
        nav = self.nav(project, **config)
        return graph.prove_reach(nav.project_root, os.path.join(nav.project_root, target), nav.config, nav._permits)

    def test_the_mirror_holds_the_proved_files_a_rewritten_config_and_nothing_else(self):
        project = self.refused_project()
        write(project / "package.json", json.dumps({"name": "p", "type": "module", "dependencies": {"zod": "^4"},
                                                  "imports": {"#k": "./src/.env.ts"}, "exports": "./src/.env.ts"}))
        config = json.loads((project / "deno.json").read_text())
        config["tasks"] = {"x": "echo"}
        config["lock"] = "../outside.lock"
        config["nodeModulesDir"] = "auto"
        (project / "deno.json").write_text(json.dumps(config))
        reach = self.proved(project)
        mirror = reach.materialize(str(self.dir))
        try:
            entries = [os.path.join(d, n) for d, ds, names in os.walk(mirror.root, followlinks=False) for n in ds + names]
            files = sorted(os.path.relpath(p, mirror.root) for p in entries if os.path.isfile(p))
            self.assertEqual(files, sorted(set(reach.files) | {"deno.json", "package.json"}))
            self.assertNotIn(".env", " ".join(files))
            self.assertFalse([p for p in entries if os.path.islink(p)])
            self.assertFalse(mirror.root.startswith(str(project) + os.sep))
            rewritten = json.loads(Path(mirror.path("deno.json")).read_text())
            self.assertEqual(rewritten["imports"], config["imports"])
            self.assertEqual((rewritten["lock"], rewritten["nodeModulesDir"]), (False, "none"))
            for gone in ("tasks", "exclude"):
                self.assertNotIn(gone, rewritten)
            package = json.loads(Path(mirror.path("package.json")).read_text())
            self.assertEqual(package, {"name": "p", "type": "module", "dependencies": {"zod": "^4"}})  # no `imports`, no `exports`
            for rel in reach.files:  # copies, equal in content, not the same file
                self.assertEqual(Path(mirror.path(rel)).read_bytes(), (project / rel).read_bytes())
                self.assertNotEqual(os.stat(mirror.path(rel)).st_ino, os.stat(project / rel).st_ino)
        finally:
            mirror.cleanup()
        self.assertFalse(os.path.exists(mirror.root))

    def test_a_project_without_a_config_gets_a_config_and_a_package_json_of_the_mirror_too(self):
        write(self.dir / "plain" / "a.ts", "export const a = 1;\n")
        reach = self.proved(self.dir / "plain", "a.ts")
        mirror = reach.materialize(str(self.dir))
        try:
            self.assertEqual(json.loads(Path(mirror.path("deno.json")).read_text()), {"lock": False, "nodeModulesDir": "none"})
            self.assertEqual(json.loads(Path(mirror.path("package.json")).read_text()), {})
        finally:
            mirror.cleanup()

    def test_a_config_below_the_project_root_is_mirrored_at_the_same_relative_place(self):
        project = self.dir / "mono"
        write(project / "lib" / "shared.ts", "export const s = 1;\n")
        write(project / "api" / "deno.json", json.dumps({"imports": {"@lib/": "../lib/"}}))
        write(project / "api" / "main.ts", "import { s } from '@lib/shared.ts';\nexport { s };\n")
        nav = self.nav(project, root=str(project / "api"))
        reach = graph.prove_reach(nav.project_root, str(project / "api" / "main.ts"), nav.config, nav._permits)
        mirror = reach.materialize(str(self.dir))
        try:
            self.assertEqual(sorted(reach.files), ["api/main.ts", "lib/shared.ts"])
            self.assertTrue(os.path.isfile(mirror.path("api/deno.json")) and os.path.isfile(mirror.path("lib/shared.ts")))
        finally:
            mirror.cleanup()

    @NEEDS_DENO
    def test_a_config_below_the_project_root_works_with_the_real_deno(self):
        project = self.dir / "mono"
        write(project / "lib" / "shared.ts", "/** The shared one. */\nexport const s = 1;\n")
        write(project / "api" / "deno.json", json.dumps({"imports": {"@lib/": "../lib/"}}))
        write(project / "api" / "main.ts", "import { s } from '@lib/shared.ts';\nexport { s };\n")
        nav = self.nav(project, root=str(project / "api"))
        answer = nav.symbols("api/main.ts")
        self.assertEqual((answer["status"], [(s["name"], s["path"]) for s in answer["symbols"]]), ("ok", [("s", "lib/shared.ts")]))
        info = nav.forward_graph("api/main.ts")
        self.assertEqual([m["path"] for m in info["modules"]], ["api/main.ts", "lib/shared.ts"])

    @NEEDS_DENO
    def test_a_package_json_subpath_import_cannot_reach_a_refused_file(self):
        """`#k` is a `package.json` `imports` entry the walk does not follow; Deno would resolve it to the refused file in the
        project. The mirror's package.json has no `imports`, so there is nothing for Deno to resolve it to."""
        project = self.refused_project()
        write(project / "package.json", json.dumps({"name": "p", "imports": {"#k": "./src/.env.ts"}}))
        write(project / "src" / "t4.ts", "import { KEY } from '#k';\nexport { KEY };\n")
        nav = self.nav(project, deno=self.recording_deno())
        answers = [nav.symbols("src/t4.ts"), nav.forward_graph("src/t4.ts")]
        self.assert_deno_never_could_see(answers)
        self.assertNotIn("KEY", [s["name"] for s in answers[0].get("symbols", [])])

    def test_config_keys_that_make_deno_read_directories_the_mirror_does_not_have_are_refused(self):
        project = self.project()
        original = (project / "deno.json").read_text()
        for key, value in (("vendor", True), ("nodeModulesDir", "manual")):
            config = json.loads(original)
            config[key] = value
            (project / "deno.json").write_text(json.dumps(config))
            nav = self.nav(project, deno=self.scripted_deno(doc={"nodes": {}}, info={"modules": []}))
            self.assertEqual((nav.symbols("src/main.ts")["reason"], nav.forward_graph("src/main.ts")["reason"]),
                             ("unmodelled_config",) * 2, key)
        for value in ("auto", "none", True, False):
            config = json.loads(original)
            config["nodeModulesDir"] = value
            (project / "deno.json").write_text(json.dumps(config))
            self.assertEqual(self.nav(project, deno=self.scripted_deno(doc={"nodes": {}})).symbols("src/main.ts")["status"], "ok", value)

    def test_a_mirror_that_would_sit_inside_the_project_is_refused(self):
        project = self.project()
        nav = self.nav(project, deno=self.scripted_deno(doc={"nodes": {}}, info={"modules": []}), mirror_parent=str(project))
        for call in (nav.symbols, nav.forward_graph):
            answer = call("src/main.ts")
            self.assertEqual((answer["status"], answer["reason"]), ("refused", "scratch_refused"))  # judged before it is made
        self.assertEqual(self.deno_calls("doc") + self.deno_calls("info"), [])
        self.assertEqual([p.name for p in project.iterdir() if p.name.startswith("tp-ctx-mirror")], [])  # and nothing was left behind

    def test_the_mirror_is_removed_after_every_outcome(self):
        project = self.project()
        parent = self.dir / "mirrors"
        parent.mkdir()
        for deno in (self.scripted_deno(doc={"nodes": {}}, info={"modules": []}), self.fake_deno("exit 3"), self.fake_deno("exec sleep 30")):
            nav = self.nav(project, deno=deno, mirror_parent=str(parent), timeout=0.5)
            nav.symbols("src/main.ts")
            nav.forward_graph("src/main.ts")
        self.assertEqual(list(parent.iterdir()), [])

    def test_a_module_outside_the_mirror_in_deno_output_is_an_unproven_module(self):
        """Deno listing a path of the project itself (not of the mirror) read something it was not given: no answer."""
        project = self.project()
        info = {"modules": [{"specifier": f"file://{MIRROR}/src/config.ts", "dependencies": []},
                            {"specifier": f"file://{project.resolve()}/src/config.ts", "dependencies": []}]}
        answer = self.nav(project, deno=self.scripted_deno(info=info)).forward_graph("src/config.ts")
        self.assertEqual((answer["status"], answer["reason"]), ("refused", "unproven_module"))

    def test_a_deno_failure_names_only_files_the_walk_proved(self):
        project = self.project()
        exe = self.fake_deno('echo "error: Module not found \\"file://$PWD/src/.env.ts\\" imported from \\"file://$PWD/src/main.ts\\"" >&2; exit 1')
        answer = self.nav(project, deno=exe).symbols("src/main.ts")
        self.assertEqual((answer["status"], answer["detail"]),
                         ("error", 'error: Module not found "<file>" imported from "src/main.ts"'))
        text = json.dumps(answer)
        self.assertNotIn("tp-ctx-mirror", text)
        self.assertNotIn(".env", text)

    def test_the_signature_line_is_read_from_the_files_deno_was_given(self):
        project = self.project()
        exe = self.scripted_deno(doc=self.doc_payload(MIRROR, "src/app/run.ts", "run", "x", line=4))
        shown = self.nav(project, deno=exe).symbols("src/app/run.ts")
        self.assertEqual(shown["symbols"][0]["signature"], "export function run(config: Config): string {")



class ConfigFreshnessCase(Tree):
    """Round 5 F2: the files the mirror's config and `package.json` are made from are part of what an answer is held to. A change to
    one during the call is a `stale_index`, as a change to a module is."""

    scripted_deno = ReviewFixesCase.scripted_deno
    deno_calls = ReviewFixesCase.deno_calls

    def nav_that_changes(self, project, change):
        """A NavApi on a scripted Deno whose every run is preceded by `change()`: the file moves while Deno is reading."""
        nav = self.nav(project, deno=self.scripted_deno(doc={"nodes": {}}, info={"modules": []}))
        real = nav._run_deno

        def run(*args, **kwargs):
            change()
            return real(*args, **kwargs)
        nav._run_deno = run
        return nav

    def assert_stale(self, setup, change):
        """For each operation, on a project of its own: `setup(project)` before the call, `change(project)` while Deno runs."""
        for call in ("symbols", "forward_graph"):
            project = self.project(call)
            setup(project)
            nav = self.nav_that_changes(project, lambda: change(project))
            answer = getattr(nav, call)("src/main.ts")
            self.assertEqual(answer["status"], "stale_index", (call, answer))

    def test_f2_the_import_map_changing_during_the_call_withholds_the_answer(self):
        def change(project):
            config = json.loads((project / "deno.json").read_text())
            config["imports"]["@host"] = "./src/mock_host.ts"
            (project / "deno.json").write_text(json.dumps(config))
        self.assert_stale(lambda project: None, change)

    def test_f2_the_config_being_removed_during_the_call_withholds_the_answer(self):
        self.assert_stale(lambda project: None, lambda project: (project / "deno.json").unlink())

    def test_f2_the_package_json_changing_during_the_call_withholds_the_answer(self):
        self.assert_stale(lambda project: write(project / "package.json", json.dumps({"name": "p", "dependencies": {"left-pad": "1.0.0"}})),
                          lambda project: write(project / "package.json", json.dumps({"name": "p", "type": "module"})))

    def test_f2_a_package_json_appearing_during_the_call_withholds_the_answer(self):
        self.assert_stale(lambda project: None, lambda project: write(project / "package.json", json.dumps({"name": "late"})))

    def test_f2_a_package_json_in_the_config_folder_is_watched_too(self):
        project = self.project()
        write(project / "api" / "deno.json", json.dumps({"imports": {}}))
        write(project / "api" / "main.ts", "export const a = 1;\n")
        nav = self.nav(project, root=str(project / "api"), deno=self.scripted_deno(doc={"nodes": {}}, info={"modules": []}))
        real = nav._run_deno
        nav._run_deno = lambda *a, **k: (write(project / "api" / "package.json", "{}"), real(*a, **k))[1]
        self.assertEqual(nav.symbols("api/main.ts")["status"], "stale_index")

    def test_f2_nothing_changing_gives_an_answer(self):
        project = self.project()
        write(project / "package.json", json.dumps({"name": "p"}))
        nav = self.nav_that_changes(project, lambda: None)
        self.assertEqual([nav.symbols("src/main.ts")["status"], nav.forward_graph("src/main.ts")["status"]], ["ok", "ok"])

    def test_f2_reach_holds_follows_the_config_and_package_files(self):
        project = self.project().resolve()
        write(project / "package.json", json.dumps({"name": "p"}))
        allow = lambda rel: True  # noqa: E731
        edits = {"config": lambda: write(project / "deno.json", "{}"), "package": lambda: write(project / "package.json", '{"name":"q"}'),
                 "package removed": lambda: (project / "package.json").unlink()}
        for label, edit in edits.items():
            reach = graph.prove_reach(str(project), str(project / "src" / "config.ts"), str(project / "deno.json"), allow)
            self.assertTrue(reach.holds(), label)
            edit()
            self.assertFalse(reach.holds(), label)
            shutil.rmtree(project)
            shutil.copytree(FIXTURE, project)
            write(project / "package.json", json.dumps({"name": "p"}))

    def test_f2_the_doc_cache_follows_the_config(self):
        """A cached `deno doc` answer made with one import map is not served for another."""
        project = self.project()
        nav = self.nav(project, deno=self.scripted_deno(doc={"nodes": {}}, info={"modules": []}))
        self.assertFalse(nav.symbols("src/main.ts")["cached"])
        self.assertTrue(nav.symbols("src/main.ts")["cached"])
        config = json.loads((project / "deno.json").read_text())
        config["imports"]["@host"] = "./src/mock_host.ts"
        (project / "deno.json").write_text(json.dumps(config))
        self.assertFalse(nav.symbols("src/main.ts")["cached"])
        write(project / "package.json", json.dumps({"name": "p"}))
        self.assertFalse(nav.symbols("src/main.ts")["cached"])


class PackageJsonImportCase(Tree):
    """Round 5 F3: a reached `package.json` (or `deno.json`) is the file the mirror also needs in its cut-down form. One file is
    written, in that form, and used for both."""

    scripted_deno = ReviewFixesCase.scripted_deno

    def project_importing(self, target):
        project = self.project()
        write(project / "package.json", json.dumps({"name": "p", "version": "1.0.0", "type": "module", "scripts": {"x": "y"},
                                                    "exports": {".": "./src/main.ts"}, "dependencies": {"left-pad": "1.0.0"}}))
        write(project / "src" / "meta.ts", f"import data from '{target}' with {{ type: 'json' }};\nexport {{ data }};\n")
        return project

    def test_f3_a_reached_package_json_does_not_stop_the_mirror(self):
        project = self.project_importing("../package.json")
        nav = self.nav(project, deno=self.scripted_deno(doc={"nodes": {}}, info={"modules": []}))
        for call in (nav.symbols, nav.forward_graph):
            answer = call("src/meta.ts")
            self.assertEqual(answer["status"], "ok", (call.__name__, answer))

    def test_f3_the_mirror_holds_one_package_json_and_it_is_the_cut_down_one(self):
        project = self.project_importing("../package.json")
        reach = graph.prove_reach(str(project.resolve()), str(project.resolve() / "src" / "meta.ts"), str(project.resolve() / "deno.json"),
                                  lambda rel: True)
        self.assertIn("package.json", reach.files)  # it was reached as data
        mirror = reach.materialize(str(self.dir))
        self.addCleanup(mirror.cleanup)
        self.assertEqual(json.loads(Path(mirror.path("package.json")).read_text()),
                         {"name": "p", "version": "1.0.0", "type": "module", "dependencies": {"left-pad": "1.0.0"}})
        self.assertTrue(Path(mirror.path("src/meta.ts")).is_file())

    def test_f3_a_reached_deno_json_does_not_stop_the_mirror_either(self):
        project = self.project_importing("../deno.json")
        nav = self.nav(project, deno=self.scripted_deno(doc={"nodes": {}}, info={"modules": []}))
        self.assertEqual([nav.symbols("src/meta.ts")["status"], nav.forward_graph("src/meta.ts")["status"]], ["ok", "ok"])
        reach = graph.prove_reach(str(project.resolve()), str(project.resolve() / "src" / "meta.ts"), str(project.resolve() / "deno.json"),
                                  lambda rel: True)
        mirror = reach.materialize(str(self.dir))
        self.addCleanup(mirror.cleanup)
        self.assertNotIn("exclude", json.loads(Path(mirror.path("deno.json")).read_text()))  # the rewritten config, not the project's

    @NEEDS_DENO
    def test_f3_the_real_deno_reads_the_cut_down_package_json(self):
        project = self.project_importing("../package.json")
        self.profile = None
        with mock.patch.object(nav_api, "SANDBOX_PROFILE", nav_api.SANDBOX_PROFILE):
            answer = self.nav(project).symbols("src/meta.ts")
        if answer.get("reason") in ("sandbox_unavailable", "sandbox_failed"):
            self.skipTest("the sandbox cannot be applied here")
        self.assertEqual(answer["status"], "ok", answer)


def sandbox_works():
    """True when sandbox-exec can run the real Deno here (it cannot inside another sandbox)."""
    if not HAVE_DENO:
        return False
    probe = nav_api.NavApi(str(ROOT))
    probe.mirror_parent = None
    return probe._check_sandbox() is None and bool(probe.deno_version())


NEEDS_SANDBOX = unittest.skipUnless(sandbox_works(), "deno or a usable sandbox-exec is missing")


class SandboxCase(Tree):
    """Round 5 F1: Deno runs under an operating-system sandbox that lets it read the mirror and Deno's own files and nothing else,
    so an import the walk missed cannot be opened whatever its spelling (path-boundary: reviews 1 to 4). These tests run the real
    Deno under the production profile (`profile = None`)."""

    profile = None

    def outside_file(self, name="outside.ts"):
        folder = self.dir / "elsewhere"
        return write(folder / name, "export const CANARY = 1;\n").resolve()

    @staticmethod
    def blind_scanner():
        """A scanner that sees no import at all: whatever the walk misses, Deno is still to be kept from it."""
        return mock.patch.object(graph, "_scan_cached", lambda data: {"specs": [], "pragmas": [], "unmodelled": []})

    def raw_modules(self, nav, rel):
        """The module list Deno itself printed when run by `nav` on the mirror of `rel` (unfiltered)."""
        reach, refusal = nav._prove(rel)
        self.assertIsNone(refusal)
        refusal, out, _ = nav._run_mirrored(["info", "--json", "--no-remote", "--no-lock"], reach)
        self.assertIsNone(refusal)
        return json.loads(out)["modules"]

    def plain_modules(self, project, rel):
        """The same Deno on the project itself, without the sandbox (the control: it shows the file can be opened)."""
        run = subprocess.run([shutil.which("deno"), "info", "--json", "--no-remote", "--no-lock", "--config", str(project / "deno.json"),
                              str(project / rel)], cwd=project, capture_output=True, env=nav_api.NavApi(str(project))._base_env())
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)["modules"]

    @staticmethod
    def entry(modules, name):
        return next((m for m in modules if m.get("specifier", "").endswith("/" + name)), None)

    def blind_repro(self, spelling):
        outside = self.outside_file(f"outside_{spelling}.ts")
        project = self.project(f"proj_{spelling}")
        target = f"file://{outside}" if spelling == "url" else str(outside)
        write(project / "src" / "blind.ts", f"export const later = import('{target}');\n")
        return project, outside

    @NEEDS_SANDBOX
    def test_f1_an_import_the_scanner_misses_cannot_be_opened_by_deno(self):
        for spelling in ("url", "path"):
            project, outside = self.blind_repro(spelling)
            control = self.entry(self.plain_modules(project, "src/blind.ts"), outside.name)
            self.assertTrue(control and "local" in control and "error" not in control, "the control must be able to open the file")
            with self.blind_scanner():
                modules = self.raw_modules(self.nav(project), "src/blind.ts")
            got = self.entry(modules, outside.name)
            self.assertTrue(got is not None and "error" in got, (spelling, got))
            self.assertNotIn("local", got)
            self.assertNotIn("size", got)

    @NEEDS_SANDBOX
    def test_f1_the_answer_for_such_a_file_is_refused_and_names_nothing(self):
        project, outside = self.blind_repro("url")
        with self.blind_scanner():
            nav = self.nav(project)
            answers = [nav.forward_graph("src/blind.ts"), nav.symbols("src/blind.ts")]
        self.assertEqual((answers[0]["status"], answers[0]["reason"]), ("refused", "unproven_module"))
        for answer in answers:
            self.assertNotIn("CANARY", json.dumps(answer))  # (`symbols` may quote the importing line of the file itself, which is allowed)
        self.assertNotIn(str(outside), json.dumps(answers[0]))

    @NEEDS_SANDBOX
    def test_f1_the_review_repro_a_tsx_closing_tag_before_a_division(self):
        outside = self.outside_file()
        project = self.project()
        write(project / "src" / "view.tsx", f"const x = <div></div> / 2; export const y = import('file://{outside}');\n")  # one line, as in the review
        control = self.entry(self.plain_modules(project, "src/view.tsx"), outside.name)
        self.assertTrue(control and "local" in control, "the repro must be one that opens the file without the sandbox")
        nav = self.nav(project)
        reach, refusal = nav._prove("src/view.tsx")
        if refusal is None:  # the walk did not see the import (it does not, today): Deno is run, in the sandbox
            modules = self.raw_modules(nav, "src/view.tsx")
            got = self.entry(modules, outside.name)
            self.assertTrue(got is not None and "error" in got and "local" not in got, got)
        answer = nav.forward_graph("src/view.tsx")
        self.assertEqual(answer["status"], "refused")
        self.assertNotIn("CANARY", json.dumps(answer))

    @NEEDS_SANDBOX
    def test_the_profile_denies_reads_writes_and_the_network(self):
        """What the profile says, tried by the process itself: `deno eval` with every Deno permission granted."""
        nav = self.nav(self.project())
        secret = self.outside_file("note.txt")
        secret.write_text("hello")
        server = __import__("socket").socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)
        port = server.getsockname()[1]
        mirror = Path(tempfile.mkdtemp(dir=str(self.dir))).resolve()
        probes = {
            "read": (f"console.log(Deno.readTextFileSync({json.dumps(str(secret))}))", "hello"),
            "write": (f"Deno.writeTextFileSync({json.dumps(str(secret.parent / 'new.txt'))}, 'x'); console.log('ok')", "ok"),
            "network": (f"const c = await Deno.connect({{hostname: '127.0.0.1', port: {port}}}); c.close(); console.log('connected')",
                        "connected"),
            "inside": (f"Deno.writeTextFileSync({json.dumps(str(mirror / 'a.txt'))}, 'x'); console.log('wrote inside')", None),
            "listing": ("console.log([...Deno.readDirSync('/Users')].length)", None),
        }
        exe = nav._deno_exe()
        for label, (code, works) in probes.items():
            argv = [exe, "eval", "--allow-all", "--no-lock", f"try {{ {code} }} catch (e) {{ console.log('denied ' + e.name) }}"]
            plain = subprocess.run(argv, cwd=mirror, capture_output=True, env=nav._base_env()).stdout.decode()
            if works is not None:
                self.assertEqual(plain.strip(), works, f"{label}: the control must succeed without the sandbox")
            for leftover in (secret.parent / "new.txt", mirror / "a.txt"):
                leftover.unlink(missing_ok=True)  # what the control run wrote
            boxed_argv, boxed_env = nav._sandboxed(exe, argv[1:], str(mirror))
            boxed = subprocess.run(boxed_argv, cwd=mirror, capture_output=True, env=boxed_env)
            self.assertEqual(boxed.stdout.decode().strip(), "denied PermissionDenied", (label, boxed.stderr))
            self.assertFalse((secret.parent / "new.txt").exists() or (mirror / "a.txt").exists(), label)

    @NEEDS_SANDBOX
    def test_the_real_deno_still_answers_for_the_fixture_under_the_profile(self):
        project = self.project()
        nav = self.nav(project)
        answer = nav.symbols("src/main.ts")
        self.assertEqual(answer["status"], "ok", answer)
        graph_answer = nav.forward_graph("src/main.ts")
        self.assertEqual(graph_answer["status"], "ok", graph_answer)
        self.assertIn("src/main.ts", [m["path"] for m in graph_answer["modules"]])
        self.assertTrue(nav.deno_version().startswith("deno "))


class SandboxRefusalCase(Tree):
    """Without a sandbox Deno is not run, whatever the reason there is none. The fake `deno` leaves a marker file when it runs."""

    scripted_deno = ReviewFixesCase.scripted_deno
    profile = None

    def deno_that_marks(self):
        return self.fake_deno(f'echo ran >> {self.dir / "marker"}; echo "deno 9.9.9"')

    def fake_sandbox(self, body):
        path = self.dir / "bin" / "sandbox-exec"
        write(path, "#!/bin/sh\n" + body + "\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def assert_refused_without_running(self, nav, reason):
        for call in (nav.symbols, nav.forward_graph):
            answer = call("src/main.ts")
            self.assertEqual((answer["status"], answer.get("reason")), ("refused", reason), call.__name__)
            self.assertTrue(answer["next_step"])
        self.assertFalse((self.dir / "marker").exists(), "Deno ran without a sandbox")
        self.assertIsNone(nav.deno_version())

    def test_no_sandbox_exec_is_a_refusal(self):
        with mock.patch.object(nav_api, "SANDBOX_EXEC", str(self.dir / "absent")):
            self.assert_refused_without_running(self.nav(self.project(), deno=self.deno_that_marks()), "sandbox_unavailable")

    def test_a_platform_without_the_sandbox_is_a_refusal(self):
        with mock.patch.object(nav_api.sys, "platform", "linux"):
            self.assert_refused_without_running(self.nav(self.project(), deno=self.deno_that_marks()), "sandbox_unavailable")

    def test_a_profile_that_cannot_be_applied_is_a_refusal(self):
        fake = self.fake_sandbox('echo "sandbox-exec: sandbox_apply: Operation not permitted" >&2; exit 71')
        with mock.patch.object(nav_api, "SANDBOX_EXEC", fake):
            self.assert_refused_without_running(self.nav(self.project(), deno=self.deno_that_marks()), "sandbox_failed")

    def test_a_sandbox_that_fails_after_the_first_call_is_a_refusal_too(self):
        """The check is made once; each call is still judged by how its own sandbox start went."""
        state = self.dir / "state"
        fake = self.fake_sandbox(f'if [ -e {state} ]; then echo "sandbox-exec: sandbox_apply: Operation not permitted" >&2; exit 71; fi\n'
                                 f'touch {state}; echo "deno 9.9.9"')
        project = self.project()
        with mock.patch.object(nav_api, "SANDBOX_EXEC", fake):
            nav = self.nav(project, deno=self.deno_that_marks())
            self.assertEqual(nav.deno_version(), "deno 9.9.9")
            for call in (nav.symbols, nav.forward_graph):
                answer = call("src/main.ts")
                self.assertEqual((answer["status"], answer.get("reason")), ("refused", "sandbox_failed"))
        self.assertFalse((self.dir / "marker").exists())

    def test_every_deno_process_is_started_through_the_sandbox(self):
        project = self.project()
        started = []
        real = subprocess.run

        def spy(argv, *args, **kwargs):
            started.append(list(argv))
            return real(argv, *args, **kwargs)
        profile = mock.patch.object(nav_api, "SANDBOX_PROFILE", LOOSE_PROFILE)
        with profile, mock.patch.object(nav_api.subprocess, "run", spy):
            nav = self.nav(project, deno=self.scripted_deno(doc={"nodes": {}}, info={"modules": []}))
            nav.status()
            nav.symbols("src/main.ts")
            nav.forward_graph("src/main.ts")
        self.assertTrue(len(started) >= 3)
        for argv in started:
            self.assertEqual(argv[0], nav_api.SANDBOX_EXEC, argv)
            self.assertIn("-p", argv)


class SandboxGrantCase(Tree):
    """Round 6 F1 (path-boundary, the rule and not one more site): every path the sandbox profile grants, other than the per-call
    mirror, is resolved with `realpath` and must not equal, contain or sit inside the project, the Deno root or a monorepo root; the
    call is refused with `sandbox_grant_overlap` before Deno starts. Deno's cache is granted read-only. The cache directory is
    chosen by the worker's environment, so it is the grant that a worker can aim."""

    scripted_deno = ReviewFixesCase.scripted_deno
    deno_that_marks = SandboxRefusalCase.deno_that_marks

    def assert_overlap(self, nav, label=""):
        for call in (nav.symbols, nav.forward_graph):
            answer = call("src/main.ts")
            self.assertEqual((answer["status"], answer.get("reason")), ("refused", "sandbox_grant_overlap"), (label, call.__name__, answer))
            self.assertTrue(answer["next_step"])
        self.assertFalse((self.dir / "marker").exists(), f"Deno ran with an overlapping grant: {label}")
        self.assertIsNone(nav.deno_version(), label)

    def aimed(self, project, **env):
        return mock.patch.dict(os.environ, {key: str(value) for key, value in env.items()})

    def test_f1_deno_dir_naming_the_project_a_part_of_it_or_a_parent_is_refused(self):
        project = self.project()
        (project / "src" / "cache").mkdir()
        link = self.dir / "link-into-project"
        link.symlink_to(project / "src")
        for label, target in {"the project": project, "inside the project": project / "src" / "cache", "a parent": self.dir,
                              "the filesystem root": "/", "a symlink into the project": link}.items():
            with self.aimed(project, DENO_DIR=target):
                self.assert_overlap(self.nav(project, deno=self.deno_that_marks()), label)

    def test_f1_the_derived_cache_directory_is_judged_the_same_way(self):
        project = self.project()
        with self.aimed(project, XDG_CACHE_HOME=project.parent):  # Deno would use <parent>/deno, a sibling: allowed
            os.environ.pop("DENO_DIR", None)
            nav = self.nav(project, deno=self.deno_that_marks())
            self.assertEqual(nav.symbols("src/main.ts").get("reason"), None)
        (project / "deno").mkdir()
        (self.dir / "marker").unlink()
        with self.aimed(project, XDG_CACHE_HOME=project):  # <project>/deno
            os.environ.pop("DENO_DIR", None)
            self.assert_overlap(self.nav(project, deno=self.deno_that_marks()), "derived from XDG_CACHE_HOME")

    def test_f1_a_cache_directory_inside_a_monorepo_root_is_refused(self):
        mono = self.dir / "mono"
        (mono / ".git").mkdir(parents=True)
        project = mono / "apps" / "api"
        shutil.copytree(FIXTURE, project)
        for label, target in {"a sibling package": mono / "apps" / "web", "the monorepo": mono, "its root folder": mono / ".deno"}.items():
            with self.aimed(project, DENO_DIR=target):
                self.assert_overlap(self.nav(project, deno=self.deno_that_marks()), label)
        with self.aimed(project, DENO_DIR=self.dir / "elsewhere"):  # outside the monorepo
            self.assertIsNone(self.nav(project, deno=self.deno_that_marks()).symbols("src/main.ts").get("reason"))

    def test_f1_the_deno_executable_inside_the_project_is_refused(self):
        project = self.project()
        marker = self.dir / "marker"
        inside = write(project / "bin" / "deno", f'#!/bin/sh\necho ran >> {marker}; echo "deno 9.9.9"\n')
        inside.chmod(inside.stat().st_mode | stat.S_IXUSR)
        self.assert_overlap(self.nav(project, deno=str(inside)), "the executable")

    def test_f1_the_mirror_is_not_inside_a_cache_directory_either(self):
        """A cache directory that contains the mirror's parent would hand Deno every mirror made next to it."""
        project = self.project()
        parent = self.dir / "mirrors"
        parent.mkdir()
        with self.aimed(project, DENO_DIR=parent):
            self.assert_overlap(self.nav(project, deno=self.deno_that_marks(), mirror_parent=str(parent)), "contains the mirror")

    def test_f1_an_unrelated_cache_directory_is_fine_and_is_what_deno_is_told(self):
        project = self.project()
        cache = self.dir / "cache-elsewhere"
        link = self.dir / "cache-link"
        cache.mkdir()
        link.symlink_to(cache)
        started = []
        real = subprocess.run
        with self.aimed(project, DENO_DIR=link), \
                mock.patch.object(nav_api.subprocess, "run", lambda argv, *a, **k: (started.append(k["env"]), real(argv, *a, **k))[1]):
            nav = self.nav(project, deno=self.scripted_deno(doc={"nodes": {}}, info={"modules": []}))
            self.assertEqual([nav.symbols("src/main.ts")["status"], nav.forward_graph("src/main.ts")["status"]], ["ok", "ok"])
        self.assertTrue(started)
        for env in started:
            self.assertEqual(env["DENO_DIR"], str(cache.resolve()), "Deno must get the directory that was checked, not the link")

    def test_f1_the_production_profile_grants_the_cache_read_only(self):
        write_rules = [line for line in SANDBOX_PRODUCTION.splitlines() if line.startswith("(allow file-write*")]
        self.assertTrue(write_rules)
        for rule in write_rules:
            self.assertNotIn("DENODIR", rule)

    @NEEDS_SANDBOX
    def test_f1_the_review_repro_deno_dir_naming_the_project_does_not_open_an_unproven_file(self):
        project = self.project()
        inside = write(project / "inside.ts", "export const CANARY = 1;\n").resolve()
        write(project / "src" / "blind.ts", f"export const later = import('file://{inside}');\n")
        self.profile = None
        with mock.patch.object(nav_api, "SANDBOX_PROFILE", SANDBOX_PRODUCTION), self.aimed(project, DENO_DIR=project), \
                SandboxCase.blind_scanner():
            nav = self.nav(project)
            answers = [nav.forward_graph("src/blind.ts"), nav.symbols("src/blind.ts")]
        for answer in answers:
            self.assertEqual((answer["status"], answer.get("reason")), ("refused", "sandbox_grant_overlap"), answer)
            self.assertNotIn("CANARY", json.dumps(answer))

    @NEEDS_SANDBOX
    def test_f1_the_real_deno_still_answers_with_the_cache_read_only(self):
        project = self.project()
        cache = self.dir / "fresh-deno-dir"  # does not exist yet: nothing can be written to it
        with mock.patch.object(nav_api, "SANDBOX_PROFILE", SANDBOX_PRODUCTION), self.aimed(project, DENO_DIR=cache):
            nav = self.nav(project)
            answers = [nav.symbols("src/main.ts"), nav.forward_graph("src/main.ts")]
        for answer in answers:
            self.assertEqual(answer["status"], "ok", answer)
        self.assertFalse(cache.exists(), "the sandbox let Deno create its cache directory")


class FinalHoldsCase(Tree):
    """Round 6 F2/F3: every answer, cached or fresh, is held to `Reach.holds()` once more immediately before it is returned, and a
    config file has three states (absent, unreadable, content), any change between which is a change."""

    scripted_deno = ReviewFixesCase.scripted_deno

    def nav_for(self, project, **kw):
        return self.nav(project, deno=self.scripted_deno(doc={"nodes": {}}, info={"modules": []}), **kw)

    @staticmethod
    def rewrite_import_map(project):
        config = json.loads((project / "deno.json").read_text())
        config["imports"]["@host"] = "./src/mock_host.ts"
        (project / "deno.json").write_text(json.dumps(config))

    def test_f2_a_cached_symbols_answer_is_withheld_when_the_config_changes_during_the_call(self):
        project = self.project()
        nav = self.nav_for(project)
        self.assertEqual(nav.symbols("src/main.ts")["cached"], False)  # fills the cache
        real = nav._read_cache

        def read(kind, key):
            got = real(kind, key)
            self.rewrite_import_map(project)
            return got
        nav._read_cache = read
        answer = nav.symbols("src/main.ts")
        self.assertEqual(answer["status"], "stale_index", answer)
        self.assertNotIn("symbols", answer)

    def test_f2_the_last_step_of_every_answer_is_the_check(self):
        """A change after Deno answered and after the module check, while the answer is being put together."""
        for call in ("symbols", "forward_graph"):
            project = self.project(call)
            nav = self.nav(project, deno=self.scripted_deno(
                doc=ReviewFixesCase.doc_payload(MIRROR, "src/main.ts", "main", "d"),
                info={"modules": [{"specifier": f"file://{MIRROR}/src/main.ts",
                                   "dependencies": [{"code": {"specifier": f"file://{MIRROR}/src/config.ts"}}]}]}))
            armed = []
            real_run = nav._run_deno
            nav._run_deno = lambda *a, **k: (armed.append(1), real_run(*a, **k))[1]
            real_allowed = nav._allowed

            def allowed(rel, real_allowed=real_allowed, project=project):
                if armed and not (project / "moved").exists():
                    write(project / "moved", "")
                    self.rewrite_import_map(project)
                return real_allowed(rel)
            nav._allowed = allowed
            answer = getattr(nav, call)("src/main.ts")
            self.assertEqual(answer["status"], "stale_index", (call, answer))

    def test_f2_an_unchanged_tree_still_gives_cached_and_fresh_answers(self):
        project = self.project()
        nav = self.nav_for(project)
        self.assertEqual([nav.symbols("src/main.ts")["cached"], nav.symbols("src/main.ts")["cached"], nav.forward_graph("src/main.ts")["status"]],
                         [False, True, "ok"])

    # --- three states

    def test_f3_the_state_of_a_config_file_has_three_values(self):
        folder = self.dir / "states"
        folder.mkdir()
        state = lambda path: graph._config_state(path, str(folder), lambda rel: True)  # noqa: E731
        absent = state(str(folder / "a.json"))
        content = state(str(write(folder / "b.json", "{}")))
        big = write(folder / "c.json", " " * (graph.MAX_BYTES + 1))
        unreadable = state(str(big))
        self.assertEqual(len({absent, content, unreadable}), 3, (absent, content, unreadable))
        self.assertEqual(state(str(folder / "a.json")), absent)
        self.assertEqual(state(str(folder / "b.json")), content)
        (folder / "d.json").mkdir()
        self.assertNotEqual(state(str(folder / "d.json")), absent)  # something is there, and it is not a readable file
        os.chmod(folder / "b.json", 0)
        self.addCleanup(os.chmod, folder / "b.json", 0o644)
        self.assertEqual(state(str(folder / "b.json")), unreadable) if os.geteuid() else None

    def assert_stale_when(self, make, change, files_present=True):
        for call in ("symbols", "forward_graph"):
            project = self.project(call)
            make(project)
            nav = self.nav_for(project)
            real = nav._run_deno

            def run(*a, project=project, real=real, **k):
                change(project)
                return real(*a, **k)
            nav._run_deno = run
            answer = getattr(nav, call)("src/main.ts")
            self.assertEqual(answer["status"], "stale_index", (call, answer))

    def test_f3_a_config_that_was_absent_and_is_now_too_large_to_read_is_a_change(self):
        self.assert_stale_when(lambda project: (project / "deno.json").unlink(),
                               lambda project: write(project / "deno.json", " " * (graph.MAX_BYTES + 1)))

    def test_f3_a_package_json_that_was_absent_and_is_now_too_large_to_read_is_a_change(self):
        self.assert_stale_when(lambda project: None,
                               lambda project: write(project / "package.json", " " * (graph.MAX_BYTES + 1)))

    @unittest.skipIf(os.geteuid() == 0, "root can read any file")
    def test_f3_a_package_json_that_appears_unreadable_is_a_change(self):
        def change(project):
            write(project / "package.json", "{}")
            os.chmod(project / "package.json", 0)
        self.assert_stale_when(lambda project: None, change)

    def test_f3_a_config_that_becomes_too_large_to_read_is_a_change(self):
        self.assert_stale_when(lambda project: None, lambda project: write(project / "deno.json", " " * (graph.MAX_BYTES + 1)))

    def test_f3_a_config_that_stays_absent_is_not_a_change(self):
        project = self.project()
        (project / "deno.json").unlink()
        nav = self.nav_for(project)
        self.assertEqual([nav.symbols("src/main.ts")["status"], nav.forward_graph("src/main.ts")["status"]], ["ok", "ok"])


class ProbeDirectoryCase(Tree):
    """Round 6 F4: an unusable directory for the sandbox probe or the mirror is a status with a next step, from every operation."""

    def test_f4_a_missing_mirror_parent_is_a_status_for_every_operation(self):
        project = self.project()
        marker = self.dir / "marker"
        deno = self.fake_deno(f'echo ran >> {marker}; echo "deno 9.9.9"')
        nav = self.nav(project, deno=deno, mirror_parent=str(self.dir / "does" / "not" / "exist"))
        for label, call in (("symbols", lambda: nav.symbols("src/main.ts")), ("forward_graph", lambda: nav.forward_graph("src/main.ts")),
                            ("imports via deno", lambda: nav.imports("src/main.ts", source="deno"))):
            answer = call()
            self.assertEqual(answer["status"], "error", (label, answer))
            self.assertTrue(answer["next_step"], label)
        self.assertIsNone(nav.deno_version())
        self.assertEqual(nav.status()["status"], "ok")
        self.assertFalse(marker.exists(), "Deno ran with nowhere to put its files")

    def test_f4_the_probe_is_tried_again_once_the_directory_exists(self):
        project = self.project()
        parent = self.dir / "later"
        nav = self.nav(project, deno=self.fake_deno('echo "deno 9.9.9"'), mirror_parent=str(parent))
        self.assertEqual(nav.symbols("src/main.ts")["status"], "error")
        parent.mkdir()
        self.assertIsNone(nav._check_sandbox())


class CheckedValueCase(Tree):
    """Round 7 (review 6 F1 path-boundary, F2 config-freshness, and the audit for the same pattern): every check uses the exact value
    it hands on, and nothing runs between the final `holds()` and the return. Each test failed against f24ba54."""

    scripted_deno = ReviewFixesCase.scripted_deno

    def spied_launches(self):
        """Patch `subprocess.run` in nav_api to record (argv, env) of every process started; returns the list."""
        started = []
        real = subprocess.run

        def spy(argv, *args, **kwargs):
            started.append((list(argv), dict(kwargs.get("env") or {})))
            return real(argv, *args, **kwargs)
        patch = mock.patch.object(nav_api.subprocess, "run", spy)
        patch.start()
        self.addCleanup(patch.stop)
        return started

    @staticmethod
    def params(argv):
        return dict(argv[i + 1].split("=", 1) for i, word in enumerate(argv[:-1]) if word == "-D")

    def assert_launch_uses_the_checked_grants(self, argv, env, project):
        params = self.params(argv)
        self.assertEqual(env["DENO_DIR"], params["DENODIR"], "the cache Deno is told about must be the one the profile grants")
        self.assertEqual(argv[argv.index("-p") + 2], params["DENOBIN"], "the program run must be the checked executable path")
        for grant in (params["DENODIR"], params["DENOBIN"]):
            self.assertNotEqual(os.path.commonpath([grant, str(project)]), str(project), (grant, argv))
            self.assertFalse(str(project).startswith(grant + os.sep), (grant, argv))

    @NEEDS_SANDBOX
    def test_f1_a_cache_link_moved_into_the_project_after_the_check_is_never_granted(self):
        """The review repro: DENO_DIR is a link to a safe directory while the grant is checked and points into the project when the
        command and environment are made. The move is made right after each check in turn (the k-th time the guarded folders are
        computed); whatever k, no process is started with the project granted or named as its cache."""
        project = self.project()
        safe = self.dir / "safe-cache"
        safe.mkdir()
        link = self.dir / "cache-link"
        started = self.spied_launches()
        for k in range(1, 12):
            if link.is_symlink():
                link.unlink()
            link.symlink_to(safe)
            with mock.patch.dict(os.environ, {"DENO_DIR": str(link)}):
                nav = self.nav(project, deno=self.scripted_deno(doc={"nodes": {}}, info={"modules": []}))
                real_roots, calls = nav._guarded_roots, []

                def roots(real_roots=real_roots, calls=calls, k=k):
                    out = real_roots()
                    calls.append(1)
                    if len(calls) == k:
                        link.unlink()
                        link.symlink_to(project)
                    return out
                nav._guarded_roots = roots
                for call in (nav.symbols, nav.forward_graph):
                    call("src/main.ts")
        self.assertTrue(started)
        for argv, env in started:
            self.assert_launch_uses_the_checked_grants(argv, env, project.resolve())

    @NEEDS_SANDBOX
    def test_f1_audit_the_program_started_is_the_executable_that_was_checked(self):
        """`deno` named through a link: the command runs the resolved, checked file, not the link (which could be moved later)."""
        project = self.project()
        target = self.scripted_deno(doc={"nodes": {}}, info={"modules": []})
        link = self.dir / "deno-link"
        link.symlink_to(target)
        started = self.spied_launches()
        nav = self.nav(project, deno=str(link))
        self.assertEqual([nav.symbols("src/main.ts")["status"], nav.forward_graph("src/main.ts")["status"]], ["ok", "ok"])
        self.assertTrue(started)
        for argv, env in started:
            self.assert_launch_uses_the_checked_grants(argv, env, project.resolve())
            self.assertEqual(argv[argv.index("-p") + 2], str(Path(target).resolve()))

    def config_changing_guard(self, project):
        """A guard that rewrites the import map the first time it screens anything: a config edit while the answer is screened."""
        state = []

        def guard(text):
            if not state:
                state.append(1)
                FinalHoldsCase.rewrite_import_map(project)
            return text
        return guard

    def test_f2_a_config_change_while_the_answer_is_screened_withholds_it(self):
        """The review repro, for every answer that rests on a proof: fresh and cached `symbols`, `forward_graph` and `imports` from
        `deno info`."""
        info = {"modules": [{"specifier": f"file://{MIRROR}/src/main.ts", "dependencies": []}]}
        doc = ReviewFixesCase.doc_payload(MIRROR, "src/main.ts", "main", "d")
        cases = {"symbols": lambda nav: nav.symbols("src/main.ts"), "forward_graph": lambda nav: nav.forward_graph("src/main.ts"),
                 "imports via deno": lambda nav: nav.imports("src/main.ts", source="deno")}
        for label, call in cases.items():
            for cached in ((False, True) if label == "symbols" else (False,)):  # only `deno doc` answers are cached
                project = self.project(f"{label}-{cached}".replace(" ", "_"))
                deno = self.scripted_deno(doc=doc, info=info)
                if cached:
                    self.assertEqual(call(self.nav(project, deno=deno))["status"], "ok", label)  # fills the doc cache
                nav = self.nav(project, deno=deno, guard=self.config_changing_guard(project))
                answer = call(nav)
                self.assertEqual(answer["status"], "stale_index", (label, cached, answer))
                self.assertNotIn("symbols", answer)
                self.assertNotIn("modules", answer)

    def test_f2_an_answer_screened_without_a_change_is_still_given(self):
        project = self.project()
        nav = self.nav(project, deno=self.scripted_deno(doc={"nodes": {}}, info={"modules": []}), guard=lambda text: text)
        self.assertEqual([nav.symbols("src/main.ts")["status"], nav.symbols("src/main.ts")["cached"],
                          nav.forward_graph("src/main.ts")["status"]], ["ok", True, "ok"])

    # --- the audit: a name checked, then the file read by resolving the name again

    def swap_after_admit(self, project, rel, target):
        """Patch `graph.admit` so that right after it judges `rel` the second time (the walk judges a specifier's path when it
        queues it and again just before it reads it), the file there is replaced by a link to `target`."""
        real = graph.admit
        seen = []

        def admit(base, path, allow):
            out = real(base, path, allow)
            if os.path.normpath(path) == str(project / rel):
                seen.append(1)
                if len(seen) == 2:
                    os.unlink(path)
                    os.symlink(target, path)
            return out
        return mock.patch.object(graph, "admit", admit)

    def solo(self):
        """A project with an entry whose one import (`src/leaf.ts`) is reached once, and a refused file to swap in for it."""
        project = self.project().resolve()
        write(project / "src" / "solo.ts", "import { LEAF } from './leaf.ts';\nexport const SOLO = LEAF;\n")
        write(project / "src" / "leaf.ts", "export const LEAF = 1;\n")
        return project, write(project / "secrets.ts", "export const LEAKED_CANARY = 1;\n")

    def test_audit_the_walk_never_takes_the_bytes_of_a_file_it_did_not_check(self):
        """`prove_reach` judged `src/leaf.ts`, then read whatever the name led to by then: a link to a refused file put its
        bytes in the proof (and so in the mirror Deno is given, and in signature lines)."""
        project, secret = self.solo()
        with self.swap_after_admit(project, "src/leaf.ts", secret):
            try:
                reach = graph.prove_reach(str(project), str(project / "src/solo.ts"), str(project / "deno.json"),
                                          lambda rel: nav_api.stub_check_path(project, rel) is not None)
            except graph.Unproven as error:
                self.assertEqual(error.reason, "changed_during_walk")
            else:
                self.fail(f"the walk returned a proof holding: {[r for r, d in reach.contents.items() if b'LEAKED' in d]}")

    def test_audit_a_link_swapped_in_during_the_walk_gives_stale_index_from_the_answer(self):
        project, secret = self.solo()
        nav = self.nav(project, deno=self.scripted_deno(doc={"nodes": {}}, info={"modules": []}))
        with self.swap_after_admit(project, "src/leaf.ts", secret):
            answer = nav.forward_graph("src/solo.ts")
        self.assertEqual(answer["status"], "stale_index", answer)
        self.assertNotIn("LEAKED", json.dumps(answer))

    def test_audit_the_index_never_scans_a_file_swapped_for_a_link_after_the_walk(self):
        """The index's walk saw a regular file; before it was read the name became a link to a refused file. Its specifiers must
        not become edges."""
        project = self.project().resolve()
        secret = write(project / "secrets.ts", "import './LEAKED_CANARY.ts';\n")
        real = graph.read_sources

        def racing(paths, *a, **k):
            paths = list(paths)
            victim = str(project / "src" / "config.ts")
            if victim in paths:
                os.unlink(victim)
                os.symlink(secret, victim)
            return real(paths, *a, **k)
        nav = self.nav(project)
        with mock.patch.object(graph, "read_sources", racing):
            answer = nav.imports("src/config.ts", source="index")
        self.assertNotIn("LEAKED", json.dumps(answer))
        self.assertNotIn("LEAKED", json.dumps(nav.importers("src/main.ts")))

    def test_audit_the_config_read_is_judged_by_the_file_that_was_opened(self):
        """The config was checked inside the project, then swapped for a link to a config outside it before it was read."""
        project = self.project().resolve()
        outside = write(self.dir / "elsewhere" / "deno.json", json.dumps({"imports": {"@host": "./LEAKED.ts"}}))
        real = graph._config_read
        done = []

        def config_read(path, *a):
            if not done and path == str(project / "deno.json"):
                done.append(1)
                os.unlink(path)
                os.symlink(outside, path)
            return real(path, *a)
        with mock.patch.object(graph, "_config_read", config_read):
            with self.assertRaises(graph.Unproven) as raised:
                graph.prove_reach(str(project), str(project / "src/main.ts"), str(project / "deno.json"), lambda rel: True)
        self.assertEqual(raised.exception.reason, "changed_during_walk")

    def test_audit_holds_judges_where_a_file_leads_by_the_file_it_read(self):
        project = self.project().resolve()
        reach = graph.prove_reach(str(project), str(project / "src/config.ts"), str(project / "deno.json"), lambda rel: True)
        self.assertTrue(reach.holds())
        copy = write(self.dir / "elsewhere" / "config.ts", (project / "src" / "config.ts").read_text())
        (project / "src" / "config.ts").unlink()
        (project / "src" / "config.ts").symlink_to(copy)
        self.assertFalse(reach.holds(), "the same bytes behind a link that leaves the project are not the proven file")

    def test_audit_read_bounded_names_the_opened_file(self):
        folder = self.dir / "rc"
        real_file = write(folder / "a.txt", "x")
        (folder / "link").symlink_to(real_file)
        yes = lambda rel: True  # noqa: E731
        self.assertEqual(graph.read_bounded(str(folder / "link"), str(real_file), str(folder), yes), ("ok", b"x", str(real_file)))
        self.assertEqual(graph.read_bounded(str(folder / "missing"), str(folder / "missing"), str(folder), yes), ("absent", None, None))
        self.assertEqual(graph.read_bounded(str(folder), str(folder), str(self.dir), yes)[:2], ("unreadable", None))
        write(folder / "big", " " * (graph.MAX_BYTES + 1))
        self.assertIsNone(graph.read_bounded(str(folder / "big"), str(folder / "big"), str(folder), yes)[1])

class ReadRecorder:
    """Records the path (as the kernel names it) of every file whose bytes graph.py reads: through `os.read` (the bounded reader) or
    through a handle `open()` makes in graph.py (any other way). Opening a file is not recorded; reading from it is."""

    def __init__(self, test):
        self.paths = []
        real_read = os.read
        real_open = open

        def os_read(fd, n):
            self.paths.append(graph._opened_path(fd))
            return real_read(fd, n)

        def graph_open(file, mode="r", *args, **kwargs):
            handle = real_open(file, mode, *args, **kwargs)
            if not any(flag in mode for flag in "wxa"):
                self.paths.append(graph._opened_path(handle.fileno()))
            return handle
        for patch in (mock.patch.object(os, "read", os_read), mock.patch.object(graph, "open", graph_open, create=True)):
            patch.start()
            test.addCleanup(patch.stop)

    def read(self, path):
        return str(Path(path).resolve()) in self.paths


class BoundedReadCase(Tree):
    """Round 8 (review 7 F1-F2, path-boundary): there is one way to read file bytes in graph.py and nav_api.py, `read_bounded`,
    which checks the opened file (admitted, inside the boundary, allowed) before it reads. Each repro failed against d977e72."""

    solo = CheckedValueCase.solo
    swap_after_admit = CheckedValueCase.swap_after_admit

    def outside(self, name="outside.json", text=None):
        return write(self.dir / "elsewhere" / name, text if text is not None else json.dumps({"name": "outside-name", "type": "module"}))

    # --- review 7 F1: package.json

    def test_r7_f1_a_root_package_json_linked_outside_is_refused_and_not_read(self):
        project = self.project().resolve()
        outside = self.outside()
        (project / "package.json").symlink_to(outside)
        recorder = ReadRecorder(self)
        with self.assertRaises(graph.Unproven) as raised:
            graph.prove_reach(str(project), str(project / "src/config.ts"), str(project / "deno.json"), lambda rel: True)
        self.assertEqual(raised.exception.reason, "outside_project")
        self.assertFalse(recorder.read(outside), "the outside package.json was read")

    def test_r7_f1_a_config_folder_package_json_linked_outside_is_refused_and_not_read(self):
        project = self.project().resolve()
        write(project / "api" / "deno.json", "{}")
        outside = self.outside()
        (project / "api" / "package.json").symlink_to(outside)
        recorder = ReadRecorder(self)
        with self.assertRaises(graph.Unproven) as raised:
            graph.prove_reach(str(project), str(project / "src/config.ts"), str(project / "api" / "deno.json"), lambda rel: True)
        self.assertEqual(raised.exception.reason, "outside_project")
        self.assertFalse(recorder.read(outside))

    def test_r7_f1_a_refused_package_json_is_not_read(self):
        project = self.project().resolve()
        write(project / "package.json", json.dumps({"name": "x"}))
        recorder = ReadRecorder(self)
        with self.assertRaises(graph.Unproven) as raised:
            graph.prove_reach(str(project), str(project / "src/config.ts"), str(project / "deno.json"),
                              lambda rel: rel != "package.json")
        self.assertEqual(raised.exception.reason, "reaches_refused_path")
        self.assertFalse(recorder.read(project / "package.json"))

    def test_r7_f1_an_absent_config_that_becomes_a_link_outside_is_never_read(self):
        project = self.project().resolve()
        outside = self.outside("deno.json", json.dumps({"imports": {"@x": "./LEAKED.ts"}}))
        config = project / "conf" / "deno.json"
        config.parent.mkdir()
        recorder = ReadRecorder(self)
        real_isfile = graph.judged_is_file

        def isfile(base, path, allow):  # the walk sees no config, then a link to an outside one appears before its state is taken
            got = real_isfile(base, path, allow)
            if path == str(config) and not os.path.lexists(config):
                config.symlink_to(outside)
            return got
        with mock.patch.object(graph, "judged_is_file", isfile):
            with self.assertRaises(graph.Unproven) as raised:  # fail closed: the config moved between the two looks
                graph.prove_reach(str(project), str(project / "src/config.ts"), str(config), lambda rel: True)
        self.assertEqual(raised.exception.reason, "changed_during_walk")
        self.assertTrue(config.is_symlink())
        self.assertFalse(recorder.read(outside))

    # --- review 7 F2: a swapped refused file is checked before it is read

    def test_r7_f2_a_refused_file_swapped_in_after_admit_is_never_read(self):
        project, secret = self.solo()
        recorder = ReadRecorder(self)
        with self.swap_after_admit(project, "src/leaf.ts", secret):
            with self.assertRaises(graph.Unproven) as raised:
                graph.prove_reach(str(project), str(project / "src/solo.ts"), str(project / "deno.json"),
                                  lambda rel: nav_api.stub_check_path(project, rel) is not None)
        self.assertEqual(raised.exception.reason, "changed_during_walk")
        self.assertFalse(recorder.read(secret), "the refused file's bytes were read before the swap was noticed")

    def test_r7_f2_holds_does_not_read_a_refused_file_swapped_in(self):
        project, secret = self.solo()
        allow = lambda rel: nav_api.stub_check_path(project, rel) is not None  # noqa: E731
        reach = graph.prove_reach(str(project), str(project / "src/solo.ts"), str(project / "deno.json"), allow)
        os.unlink(project / "src" / "leaf.ts")
        os.symlink(secret, project / "src" / "leaf.ts")
        recorder = ReadRecorder(self)
        self.assertFalse(reach.holds())
        self.assertFalse(recorder.read(secret))

    def test_r7_f2_the_index_does_not_read_a_file_swapped_for_a_link_after_its_walk(self):
        project = self.project().resolve()
        secret = write(project / "secrets.ts", "import './LEAKED_CANARY.ts';\n")
        real = graph.read_sources

        def racing(paths, *a, **k):
            paths = list(paths)
            victim = str(project / "src" / "config.ts")
            if victim in paths:
                os.unlink(victim)
                os.symlink(secret, victim)
            return real(paths, *a, **k)
        recorder = ReadRecorder(self)
        with mock.patch.object(graph, "read_sources", racing):
            self.nav(project).imports("src/config.ts", source="index")
        self.assertFalse(recorder.read(secret))

    # --- the same rule at the other reads

    def test_r7_an_index_config_or_overlay_linked_outside_is_not_read(self):
        for which in ("config", "overlay"):
            project = self.project(which).resolve()
            outside = self.outside(f"{which}.json", json.dumps({"imports": {"@host": "./src/mock_host.ts"}}))
            target = project / ("deno.json" if which == "config" else "deno.test.json")
            target.unlink()
            target.symlink_to(outside)
            recorder = ReadRecorder(self)
            index = graph.ImportIndex(str(project), overlays=[str(project / "deno.test.json")],
                                      cache_path=str(project_cache(project)), allow=stub_allow(project))
            index.refresh()
            self.assertFalse(recorder.read(outside), which)
            if which == "config":
                self.assertEqual(index.meta["config_error"], "refused")  # the guard follows the link and refuses the name before graph looks at it

    def test_r7_a_cache_file_that_is_a_link_is_not_read(self):
        project = self.project().resolve()
        cache = project_cache(project)
        outside = self.outside("index.json", json.dumps({"version": graph.INDEX_VERSION}))
        cache.parent.mkdir(parents=True)
        cache.symlink_to(outside)
        recorder = ReadRecorder(self)
        graph.ImportIndex(str(project), cache_path=str(cache), allow=stub_allow(project))
        self.assertFalse(recorder.read(outside))
        nav = self.nav(project)
        os.makedirs(nav.cache_dir, exist_ok=True)
        doc = Path(nav._cache_file("doc", "k"))
        doc.symlink_to(outside)
        self.assertIsNone(nav._read_cache("doc", "k"))
        self.assertFalse(recorder.read(outside))

    def test_r7_read_bounded_reads_only_after_every_check(self):
        folder = (self.dir / "rb").resolve()
        good = write(folder / "a.txt", "x")
        refused = write(folder / "secret.txt", "LEAKED")
        outside = self.outside("o.txt", "LEAKED")
        (folder / "link").symlink_to(refused)
        (folder / "out").symlink_to(outside)
        recorder = ReadRecorder(self)
        yes = lambda rel: True  # noqa: E731
        self.assertEqual(graph.read_bounded(str(good), str(good), str(folder), yes), ("ok", b"x", str(good)))
        self.assertEqual(graph.read_bounded(str(refused), str(refused), str(folder), lambda rel: rel != "secret.txt")[:2],
                         ("refused", None))
        self.assertEqual(graph.read_bounded(str(folder / "link"), str(folder / "link"), str(folder), yes)[:2], ("changed", None))
        self.assertEqual(graph.read_bounded(str(folder / "out"), str(outside), str(folder), yes)[:2], ("outside", None))
        self.assertEqual(graph.read_bounded(str(folder / "missing"), str(folder / "missing"), str(folder), yes)[:2], ("absent", None))
        self.assertEqual(graph.read_bounded(str(folder), str(folder), str(self.dir), yes)[:2], ("unreadable", None))
        big = write(folder / "big", " " * (graph.MAX_BYTES + 1))
        self.assertEqual(graph.read_bounded(str(big), str(big), str(folder), yes)[:2], ("unreadable", None))
        self.assertFalse(recorder.read(refused) or recorder.read(outside) or recorder.read(big))
        self.assertEqual(graph.read_bounded(str(good), str(good), str(folder), lambda rel: 1 / 0)[0], "refused")  # a guard that fails

    # --- the rule, by scan

    def test_r7_graph_and_nav_api_read_bytes_only_through_read_bounded(self):
        """Any call that opens or reads a file (open, io.open, Path.open, read_text, read_bytes, os.read, os.fdopen, FileIO, json.load,
        a handle's .read) outside `graph.read_bounded` fails; `os.open` is allowed in `read_bounded` and, write-only and exclusive,
        in `graph.write_new`."""
        import ast
        reading = {"open", "fdopen", "FileIO", "read_text", "read_bytes", "read", "readline", "readlines", "readinto", "load",
                   "mmap", "sendfile", "copyfile", "copy", "copy2", "copyfileobj"}
        found = []
        for name in ("graph.py", "nav_api.py"):
            path = ROOT / "lib" / "ctxengine" / name
            tree = ast.parse(path.read_text(), str(path))
            for func in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))] + [tree]:
                body = func.body if func is not tree else [n for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.ClassDef))]
                for node in (n for stmt in body for n in ast.walk(stmt)):
                    if not isinstance(node, ast.Call):
                        continue
                    callee = node.func
                    called = callee.id if isinstance(callee, ast.Name) else callee.attr if isinstance(callee, ast.Attribute) else ""
                    owner = getattr(func, "name", "<module>")
                    if called not in reading:
                        continue
                    if owner == "read_bounded" and called == "open":
                        continue  # os.open, checked by the next test
                    if owner == "read_bounded" and called == "read" and isinstance(callee, ast.Attribute) \
                            and isinstance(callee.value, ast.Name) and callee.value.id == "os":
                        continue
                    if owner in ("open_folder_below", "_walk", "_look", "make_own_folder", "remove_own_folder") and ast.unparse(callee) == "os.open" \
                            and ast.unparse(node.args[1]).startswith("DIR_FLAGS"):
                        continue  # a descriptor on a folder (O_DIRECTORY): listed or written in, never read from
                    if owner == "write_new" and called == "open" and isinstance(callee, ast.Attribute) \
                            and ast.unparse(callee.value) == "os":
                        flags = ast.unparse(node.args[1])
                        if "O_WRONLY" in flags and "O_EXCL" in flags and "O_RDONLY" not in flags and "O_RDWR" not in flags:
                            continue
                    found.append(f"{name}:{node.lineno} {owner}: {ast.unparse(callee)}")
        self.assertEqual(found, [], "file bytes read other than through graph.read_bounded")

    def test_r7_read_bounded_opens_read_only_and_reads_after_its_checks(self):
        import ast
        import inspect
        source = inspect.getsource(graph.read_bounded)
        func = ast.parse(source).body[0]
        calls = [(n.lineno, ast.unparse(n.func)) for n in ast.walk(func) if isinstance(n, ast.Call)]
        opens = [line for line, name in calls if name == "os.open"]
        reads = [line for line, name in calls if name == "os.read"]
        checks = [line for line, name in calls if name in ("_opened_path", "lexical_path", "allow", "os.fstat")]
        self.assertEqual(len(opens), 1)
        self.assertIn("O_RDONLY", ast.unparse([n for n in ast.walk(func) if isinstance(n, ast.Call) and ast.unparse(n.func) == "os.open"][0]))
        self.assertTrue(reads and checks)
        self.assertLess(max(checks), min(reads), "a check comes after the read")


class FsTracer:
    """Records every location the file system is asked about while it is active, in this process: stat, lstat, open, scandir,
    listdir, readlink, access, mkdir, rmdir, unlink, rename, replace, chmod, utime (so `os.path.exists`, `isfile`, `lexists`,
    `realpath`, `shutil.which`, `tempfile` and `shutil.rmtree` are traced too: they call these). Each call is kept as the location it
    reaches: a `dir_fd`-relative name is joined to the folder the descriptor is open on (as the kernel names it); a call that follows
    links is kept as where they lead now; one that follows none (`lstat`, `O_NOFOLLOW`, `unlink`...) as its resolved folder and its own
    name; one under `O_NOFOLLOW_ANY` as written (the kernel follows no link on the way). `touched(place)` lists the calls at or below
    `place`."""

    CALLS = {"stat": True, "lstat": False, "open": None, "scandir": True, "listdir": True, "readlink": False, "access": True,
             "mkdir": False, "rmdir": False, "unlink": False, "remove": False, "rename": False, "replace": False, "chmod": True,
             "utime": True}

    def __init__(self, test):
        import threading
        self.calls = []
        self._busy = threading.local()
        self.on = True
        for name, follows in self.CALLS.items():
            real = getattr(os, name)
            patch = mock.patch.object(os, name, self._wrap(name, real, follows))
            patch.start()
            test.addCleanup(patch.stop)

    def _wrap(self, name, real, follows):
        def traced(*args, **kwargs):
            if self.on and not getattr(self._busy, "on", False):
                self._busy.on = True
                try:
                    self._note(name, args, kwargs, follows)
                finally:
                    self._busy.on = False
            return real(*args, **kwargs)
        return traced

    def _note(self, name, args, kwargs, follows):
        if name in ("rename", "replace"):
            pairs = [(args[0] if args else kwargs.get("src"), kwargs.get("src_dir_fd")),
                     (args[1] if len(args) > 1 else kwargs.get("dst"), kwargs.get("dst_dir_fd"))]
            follows = False
        else:
            target = args[0] if args else kwargs.get("path", ".")
            pairs = [(target, kwargs.get("dir_fd"))]
            if name == "open":
                flags = args[1] if len(args) > 1 else kwargs.get("flags", 0)
                anywhere = getattr(os, "O_NOFOLLOW_ANY", 0)
                follows = "none" if anywhere and flags & anywhere == anywhere else False if flags & os.O_NOFOLLOW else True
            if kwargs.get("follow_symlinks") is False:
                follows = False
        for target, dir_fd in pairs:
            self.calls.append((name, self.where(target, dir_fd, follows)))

    @staticmethod
    def where(target, dir_fd, follows):
        if isinstance(target, int):
            return graph._opened_path(target) or "?"
        target = os.fsdecode(os.fspath(target))
        if dir_fd is not None and not os.path.isabs(target):
            target = os.path.join(graph._opened_path(dir_fd) or "?", target)
        target = os.path.abspath(target)
        if follows == "none":
            return target
        if follows:
            return os.path.realpath(target)
        folder, name = os.path.split(target)
        return os.path.join(os.path.realpath(folder), name)

    def touched(self, place, but=()):
        place = os.path.realpath(str(place))
        allowed = {os.path.realpath(str(p)) for p in but}
        return [(name, path) for name, path in self.calls if (path == place or path.startswith(place + os.sep)) and path not in allowed]


class PathRuleCase(Tree):
    """Round 9 (review 8 F1, path-boundary, rule first): THE PATH RULE (graph.py docstring) — no file-system call touches a path
    until its name has been shown inside the boundary and allowed by the guard; the cache folder is judged like any other project
    path, before anything in it is read, and is refused (no cache, `cache_refused` on ok answers) when the guard refuses it or it is
    outside the project. Sandbox grants and the folder scratch copies are made in are resolved the same way with `/` as the boundary
    (`nav_api.judged_location`). Every probe of the family sweep is a test here; each failed against 0f4bf9f (see the report)."""

    scripted_deno = ReviewFixesCase.scripted_deno
    deno_calls = ReviewFixesCase.deno_calls
    deno_that_marks = SandboxRefusalCase.deno_that_marks
    aimed = SandboxGrantCase.aimed
    spied_launches = CheckedValueCase.spied_launches
    params = staticmethod(CheckedValueCase.params)

    def setUp(self):
        super().setUp()
        self.trap = self.dir / "trap"  # a folder beside the project: nothing in it may be looked at
        write(self.trap / "x.ts", "export const TRAPPED = 1;\n")
        write(self.trap / "deno.json", "{}")
        write(self.trap / "nav_api-doc-abc.json", "[]")

    def guarded_project(self):
        """The fixture with a `.git` holding a planted cache file (the review's repro), and refused folders inside `src`."""
        project = self.project().resolve()
        write(project / ".git" / "HEAD", "ref: refs/heads/main\n")
        write(project / ".git" / "nav_api-doc-abc.json", json.dumps([{"name": "PLANTED"}]))
        write(project / "src" / ".ssh" / "id.ts", "export const ID = 1;\n")
        write(project / "src" / "secrets" / "s.ts", "export const S = 1;\n")
        return project

    def every_question(self, nav):
        answers = [nav.status(), nav.importers("src/util/slug.ts"), nav.imports("src/main.ts", source="index"),
                   nav.symbols("src/main.ts"), nav.forward_graph("src/main.ts"), nav.imports("src/main.ts", source="deno")]
        nav._read_cache("doc", "abc")
        nav._write_cache("doc", "abc", [{"name": "x"}])
        return answers

    def assert_untouched(self, tracer, project, label=""):
        self.assertEqual(tracer.touched(self.trap), [], label)
        self.assertEqual(tracer.touched(project / ".git", but=[project / ".git"]), [], label)  # the monorepo-marker probe only
        self.assertEqual(tracer.touched(project / "src" / ".ssh"), [], label)
        self.assertEqual(tracer.touched(project / "src" / "secrets"), [], label)

    def listing(self, folder):
        return sorted((str(p.relative_to(folder)), p.read_bytes() if p.is_file() else b"") for p in Path(folder).rglob("*"))

    # --- the review 8 F1 repro and the cache folder

    def test_r8_f1_repro_a_cache_folder_in_git_is_refused_and_nothing_in_it_is_read_or_written(self):
        project = self.guarded_project()
        before = self.listing(project / ".git")
        self.assertRaises(nav_api.Refused, nav_api.stub_check_path, str(project), ".git/nav_api-doc-abc.json")
        tracer = FsTracer(self)
        nav = self.nav(project, cache_dir=str(project / ".git"), deno=self.scripted_deno(doc={"nodes": {}}, info={"modules": []}))
        self.assertEqual(nav.cache_refused, "cache_dir_refused")
        self.assertIsNone(nav._read_cache("doc", "abc"))
        self.assertIsNone(nav._cache.read_json("nav_api-doc-abc.json"))
        answers = self.every_question(nav)
        tracer.on = False
        self.assert_untouched(tracer, project)
        self.assertEqual(self.listing(project / ".git"), before)
        for answer in answers:
            if answer["status"] == "ok":
                self.assertEqual(answer.get("cache_refused"), "cache_dir_refused", answer)
        self.assertEqual(answers[1]["status"], "ok")
        self.assertNotIn("PLANTED", json.dumps(answers))

    def test_r8_cache_folders_that_are_refused_by_name_or_by_where_they_lead(self):
        project = self.guarded_project()
        (project / ".tmux-pi").mkdir()
        (project / ".tmux-pi" / "to-git").symlink_to(project / ".git")
        (project / ".tmux-pi" / "to-trap").symlink_to(self.trap)
        (project / ".tmux-pi" / "to-root").symlink_to(project)
        cases = {
            "in .git": (project / ".git", "cache_dir_refused"),
            "below .git": (project / ".git" / "cache", "cache_dir_refused"),
            "spelled through ..": (str(project / "src") + "/../.git", "cache_dir_refused"),
            "relative, through ..": ("src/../.git", "cache_dir_refused"),
            "a link to .git": (project / ".tmux-pi" / "to-git", "cache_dir_refused"),
            "below a link to .git": (project / ".tmux-pi" / "to-git" / "c", "cache_dir_refused"),
            # the stub guard follows links itself, so it refuses a link that leads out before graph looks at it
            "a link out of the project": (project / ".tmux-pi" / "to-trap", "cache_dir_refused"),
            "a link to the project root": (project / ".tmux-pi" / "to-root", "cache_dir_refused"),
            "outside the project": (self.trap, "cache_dir_outside_project"),
            "a new folder outside": (self.trap / "new-cache", "cache_dir_outside_project"),
            "the project root": (project, "cache_dir_outside_project"),
            "a sensitive folder in the project": (project / "src" / ".ssh" / "cache", "cache_dir_refused"),
            "a secrets folder": (project / "src" / "secrets", "cache_dir_refused"),
            "an .env folder": (project / ".env.d" / "cache", "cache_dir_refused"),
            ".git in another case (APFS ignores case)": (project / ".GIT", "cache_dir_refused"),
            "below .Git through ..": ("SRC/../.Git/c", "cache_dir_refused"),
        }
        for label, (folder, reason) in cases.items():
            with self.subTest(label):
                tracer = FsTracer(self)
                nav = self.nav(project, cache_dir=str(folder))
                self.assertEqual(nav.cache_refused, reason)
                self.assertIsNone(nav._read_cache("doc", "abc"))
                nav._write_cache("doc", "abc", [{"name": "x"}])
                self.assertEqual(nav.importers("src/util/slug.ts").get("cache_refused"), reason)
                tracer.on = False
                self.assert_untouched(tracer, project, label)
                self.assertEqual([call for call in tracer.calls if call[0] in ("mkdir", "rename")], [], label)
        self.assertFalse((self.trap / "new-cache").exists())
        self.assertFalse(list(project.glob("nav_api-*")) or list((project / ".git").glob("nav_api-index-*")))
        self.assertEqual(self.listing(self.trap), [("deno.json", b"{}"), ("nav_api-doc-abc.json", b"[]"),
                                                   ("x.ts", b"export const TRAPPED = 1;\n")])

    def test_r8_the_default_cache_folder_and_an_alias_spelling_of_it_still_work(self):
        project = self.project().resolve()
        alias = self.dir / "alias"
        alias.symlink_to(project)
        for label, nav in {"default": self.nav(project),
                           "alias": nav_api.NavApi(str(alias), cache_dir=str(alias / ".tmux-pi" / "ctx-cache"))}.items():
            self.assertIsNone(nav.cache_refused, label)
            nav._write_cache("doc", "k" + label, [{"name": label}])
            self.assertEqual(nav._read_cache("doc", "k" + label), [{"name": label}], label)
            self.assertNotIn("cache_refused", nav.importers("src/util/slug.ts"))
        self.assertTrue((project_cache(project).parent / "nav_api-doc-kalias.json").is_file())

    def test_r8_a_cache_folder_swapped_for_a_link_after_it_was_judged_is_neither_read_nor_written(self):
        project = self.project().resolve()
        nav = self.nav(project)
        nav._write_cache("doc", "k", [{"name": "mine"}])
        folder = project_cache(project).parent
        for target in (self.trap, project / ".git"):
            write(Path(target) / "nav_api-doc-k.json", json.dumps([{"name": "PLANTED"}]))
            shutil.rmtree(folder) if folder.is_dir() and not folder.is_symlink() else folder.unlink()
            folder.symlink_to(target)
            before = self.listing(target)
            tracer = FsTracer(self)
            self.assertIsNone(nav._read_cache("doc", "k"))
            nav._write_cache("doc", "k2", [{"name": "x"}])
            nav.importers("src/util/slug.ts")
            tracer.on = False
            self.assertEqual(tracer.touched(target, but=[project / ".git"]), [], target)
            self.assertEqual(self.listing(target), before)

    def test_r8_a_cache_file_that_is_a_folder_or_a_fifo_is_not_read_and_does_not_hang(self):
        project = self.project().resolve()
        nav = self.nav(project)
        folder = project_cache(project).parent
        folder.mkdir(parents=True)
        (folder / nav._cache_name("doc", "dir")).mkdir()
        os.mkfifo(folder / nav._cache_name("doc", "fifo"))
        self.assertIsNone(nav._read_cache("doc", "dir"))
        self.assertIsNone(nav._read_cache("doc", "fifo"))
        os.mkfifo(project_cache(project, "index-fifo.json"))
        index = graph.ImportIndex(str(project), cache_path=str(project_cache(project, "index-fifo.json")), allow=stub_allow(project))
        self.assertTrue(index.refresh() is not None or index.entries)

    def test_r8_an_index_without_a_guard_has_no_cache(self):
        project = self.project().resolve()
        cache = project_cache(project)
        index = graph.ImportIndex(str(project), cache_path=str(cache))
        self.assertIsNone(index.cache_path)
        self.assertEqual(index.cache_refusal, "cache_dir_unguarded")
        index.refresh()
        self.assertFalse(index.save())
        self.assertFalse(cache.parent.exists())

    # --- every other path the modules are given

    def test_r8_a_root_or_config_outside_or_refused_is_never_looked_at(self):
        project = self.guarded_project()
        (project / "to-git").symlink_to(project / ".git")
        (project / "to-trap").symlink_to(self.trap)
        for label, kw in {"root outside": {"root": str(self.trap)}, "root a link to .git": {"root": "to-git"},
                          "root .git": {"root": ".git"}, "root a link out": {"root": "to-trap"},
                          "config outside": {"config": str(self.trap / "deno.json")},
                          "config in .git": {"config": ".git/deno.json"},
                          "config through a link out": {"config": "to-trap/deno.json"},
                          "overlay outside": {"overlays": [str(self.trap / "deno.json")]}}.items():
            with self.subTest(label):
                tracer = FsTracer(self)
                nav = self.nav(project, deno=self.scripted_deno(doc={"nodes": {}}, info={"modules": []}), **kw)
                answers = self.every_question(nav)
                tracer.on = False
                self.assert_untouched(tracer, project, label)
                if label.startswith("root"):
                    self.assertEqual(answers[1]["status"], "refused", (label, answers[1]))

    def test_r8_a_target_outside_answers_the_same_whether_or_not_it_exists_and_is_not_looked_at(self):
        project = self.guarded_project()
        nav = self.nav(project, deno=self.scripted_deno(doc={"nodes": {}}, info={"modules": []}))
        answers = {}
        for name in ("x.ts", "missing.ts"):
            tracer = FsTracer(self)
            answers[name] = [call(target) for target in (f"../trap/{name}", str(self.trap / name), f"src/../../trap/{name}",
                                                         f".git/{name}", f"src/.ssh/{name}")
                             for call in (nav.importers, nav.symbols, nav.forward_graph, nav.imports)]
            tracer.on = False
            self.assert_untouched(tracer, project, name)
        self.assertEqual(answers["x.ts"], answers["missing.ts"])
        self.assertTrue(all(answer["status"] == "refused" for answer in answers["x.ts"]), answers["x.ts"])

    def test_r8_links_in_the_project_that_lead_out_or_to_git_are_never_followed_there(self):
        project = self.guarded_project()
        (project / "src" / "out.ts").symlink_to(self.trap / "x.ts")
        (project / "src" / "outdir").symlink_to(self.trap)
        (project / "src" / "gitdir").symlink_to(project / ".git")
        (project / "src" / "chain").symlink_to("hop")
        (project / "src" / "hop").symlink_to("../src/outdir/x.ts")
        write(project / "src" / "uses.ts", "import './out.ts';\nimport './outdir/x.ts';\nimport './chain';\nimport './gitdir/x.ts';\n"
              "import '../../trap/x.ts';\n")
        tracer = FsTracer(self)
        nav = self.nav(project, deno=self.scripted_deno(doc={"nodes": {}}, info={"modules": []}))
        index = nav._get_index()
        nav.status()
        answers = [nav.imports("src/uses.ts", source="index")] + [call(target) for target in
                                                                   ("src/out.ts", "src/outdir/x.ts", "src/chain", "src/gitdir/HEAD")
                                                                   for call in (nav.importers, nav.symbols)]
        for rel in ("src/out.ts", "src/outdir/x.ts", "src/chain", "src/gitdir/x.ts"):
            self.assertRaises(nav_api.Refused, nav_api.stub_check_path, str(project), rel)
            self.assertRaises(graph.Unproven, graph.judged_realpath, str(project), rel, stub_allow(project))
        tracer.on = False
        self.assert_untouched(tracer, project)
        self.assertFalse(any(rel.startswith(("src/outdir/", "src/gitdir/")) for rel in index.entries))
        self.assertEqual([a["status"] for a in answers[1:]], ["refused"] * (len(answers) - 1), answers)

    def test_r8_the_walk_does_not_list_or_look_at_a_refused_folder(self):
        project = self.guarded_project()
        write(project / "src" / "key.pem" / "k.ts", "export const K = 1;\n")
        tracer = FsTracer(self)
        index = graph.ImportIndex(str(project), cache_path=str(project_cache(project)), allow=stub_allow(project))
        index.refresh()
        tracer.on = False
        self.assert_untouched(tracer, project)
        self.assertEqual(tracer.touched(project / "src" / "key.pem"), [])
        self.assertFalse([rel for rel in index.entries if ".ssh" in rel or "secrets" in rel or ".pem" in rel])
        self.assertIn("src/main.ts", index.entries)

    def test_r8_a_fifo_source_or_config_does_not_hang_and_is_not_read(self):
        project = self.project().resolve()
        os.mkfifo(project / "src" / "pipe.ts")
        index = graph.ImportIndex(str(project), cache_path=str(project_cache(project)), allow=stub_allow(project))
        index.refresh()
        self.assertNotIn("src/pipe.ts", index.entries)
        for name in ("deno.json", "package.json"):
            fifo = self.project(f"fifo-{name}").resolve()
            (fifo / name).unlink(missing_ok=True)
            os.mkfifo(fifo / name)
            nav = self.nav(fifo, deno=self.scripted_deno(doc={"nodes": {}}, info={"modules": []}))
            answer = nav.symbols("src/main.ts")
            self.assertEqual(answer["status"], "refused", (name, answer))
            nav.importers("src/util/slug.ts")
            nav.status()

    def test_r8_a_folder_on_the_way_swapped_for_a_link_is_seen_by_the_look_and_the_read(self):
        project = self.project().resolve()
        target = project / "src" / "util" / "slug.ts"
        self.assertEqual(graph.read_bounded(str(target), str(target), str(project), lambda rel: True)[0], "ok")
        shutil.move(str(project / "src" / "util"), str(self.trap / "util"))
        (project / "src" / "util").symlink_to(self.trap / "util")
        tracer = FsTracer(self)
        self.assertEqual(graph.read_bounded(str(target), str(target), str(project), lambda rel: True)[:2], ("changed", None))
        self.assertRaises(OSError, graph.lstat_nofollow, str(target))
        tracer.on = False
        self.assertEqual(tracer.touched(self.trap), [])

    def test_r8_the_stub_guard_and_judged_realpath_look_at_nothing_for_a_name_they_refuse(self):
        project = self.guarded_project()
        tracer = FsTracer(self)
        for rel in (".git/x", ".git", "src/.ssh/id.ts", "../trap/x.ts", str(self.trap / "x.ts"), "src/../../trap/x.ts",
                    "src/secrets/s.ts", "config/local.json", ".env", "a/.ENV.local"):
            self.assertRaises(nav_api.Refused, nav_api.stub_check_path, str(project), rel)
            self.assertRaises(graph.Unproven, graph.judged_realpath, str(project), rel, stub_allow(project))
            self.assertIsNone(graph.project_path(str(project), rel, stub_allow(project)))
            self.assertFalse(graph.judged_is_file(str(project), rel, stub_allow(project)))
            self.assertFalse(graph.judged_is_dir(str(project), rel, stub_allow(project)))
        tracer.on = False
        self.assertEqual([call for call in tracer.calls if call[1] != str(project) and not str(project).startswith(call[1] + os.sep)],
                         [], "a refused name was looked at")

    # --- the sandbox grants and the scratch folder

    def test_r8_deno_dir_or_the_executable_in_or_led_to_a_sensitive_place_is_refused_unlooked_at(self):
        project = self.project().resolve()
        home = self.dir / "home"
        ssh = home / ".ssh"
        write(ssh / "deno" / "x", "")
        write(self.dir / "plain-file", "")
        (self.dir / "to-ssh").symlink_to(ssh / "deno")
        (self.dir / "to-ssh-bin").symlink_to(ssh)
        marker_deno = self.deno_that_marks()
        ssh_deno = ssh / "bin" / "deno"
        write(ssh_deno, Path(marker_deno).read_text())
        ssh_deno.chmod(0o755)
        cases = {"DENO_DIR in .ssh": ({"DENO_DIR": ssh / "deno"}, marker_deno),
                 "DENO_DIR a link into .ssh": ({"DENO_DIR": self.dir / "to-ssh"}, marker_deno),
                 "DENO_DIR below a link into .ssh": ({"DENO_DIR": self.dir / "to-ssh" / "sub"}, marker_deno),
                 "XDG_CACHE_HOME in .ssh": ({"DENO_DIR": "", "XDG_CACHE_HOME": ssh}, marker_deno),
                 "DENO_DIR a file": ({"DENO_DIR": self.dir / "plain-file"}, marker_deno),
                 "DENO_DIR the home folder": ({"DENO_DIR": home, "HOME": home}, marker_deno),
                 "DENO_DIR holding the home folder": ({"DENO_DIR": self.dir / "h", "HOME": self.dir / "h" / "me"}, marker_deno),
                 "the executable in .ssh": ({}, str(ssh_deno)),
                 "the executable through a link into .ssh": ({}, str(self.dir / "to-ssh-bin" / "bin" / "deno"))}
        for label, (env, deno) in cases.items():
            with self.subTest(label):
                (self.dir / "marker").unlink(missing_ok=True)
                with self.aimed(project, **env):
                    tracer = FsTracer(self)
                    nav = self.nav(project, deno=deno)
                    answers = [nav.symbols("src/main.ts"), nav.forward_graph("src/main.ts")]
                    nav.deno_version()
                    tracer.on = False
                for answer in answers:
                    self.assertEqual((answer["status"], answer.get("reason")), ("refused", "sandbox_grant_sensitive"), (label, answer))
                self.assertFalse((self.dir / "marker").exists(), label)
                self.assertEqual(tracer.touched(ssh), [], label)

    def test_r8_a_deno_on_path_in_a_sensitive_folder_is_not_looked_up_there(self):
        project = self.project().resolve()
        ssh_bin = self.dir / "home" / ".ssh" / "bin"
        ssh_bin.mkdir(parents=True)
        (self.dir / "linked-bin").symlink_to(ssh_bin)
        write(ssh_bin / "deno", Path(self.deno_that_marks()).read_text()).chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": f"{ssh_bin}{os.pathsep}{self.dir / 'linked-bin'}{os.pathsep}relative"}):
            tracer = FsTracer(self)
            nav = self.nav(project)
            self.assertIsNone(nav._deno_exe())
            self.assertEqual(nav.symbols("src/main.ts")["status"], "unavailable")
            tracer.on = False
        self.assertEqual(tracer.touched(ssh_bin), [])
        plain_bin = self.dir / "plain-bin"
        plain_bin.mkdir()
        (plain_bin / "deno").symlink_to(ssh_bin / "deno")  # a deno on PATH that is a link into .ssh
        with mock.patch.dict(os.environ, {"PATH": str(plain_bin)}):
            tracer = FsTracer(self)
            nav = self.nav(project)
            answer = nav.symbols("src/main.ts")
            tracer.on = False
        self.assertEqual((answer["status"], answer.get("reason")), ("refused", "sandbox_grant_sensitive"))
        self.assertEqual(tracer.touched(ssh_bin), [])
        self.assertFalse((self.dir / "marker").exists())

    def test_r8_own_scratch_folders_are_made_where_the_parent_was_judged_never_through_a_link(self):
        """The mirror and the probe folder are made relative to a descriptor on the judged parent (no link on the way), not by name:
        a parent swapped for a link (into .ssh) after it was judged refuses with `scratch_refused`, and nothing is made, resolved
        or looked at past the link."""
        project = self.project().resolve()
        ssh = self.dir / "home" / ".ssh"
        (ssh / "tmp").mkdir(parents=True)
        parent = self.dir / "scratch"
        parent.mkdir()
        made = graph.make_own_folder(str(parent), "tp-x-")
        self.assertEqual((os.path.dirname(made), stat.S_IMODE(os.lstat(made).st_mode)), (str(parent), 0o700))
        parent_real = nav_api.NavApi._scratch_parent

        def judged_then_swapped(nav):  # the judged parent becomes a link into .ssh just after it was judged
            judged = parent_real(nav)
            shutil.rmtree(parent)
            parent.symlink_to(ssh / "tmp")
            return judged
        shutil.rmtree(parent)
        parent.symlink_to(ssh / "tmp")
        tracer = FsTracer(self)
        with self.assertRaises(graph.Unproven) as raised:
            graph.Mirror(str(project), str(parent))
        self.assertEqual(raised.exception.reason, "scratch_refused")
        for call in ("symbols", "forward_graph"):
            parent.unlink()
            parent.mkdir()
            nav = self.nav(project, deno=self.scripted_deno(doc={"nodes": {}}, info={"modules": []}), mirror_parent=str(parent))
            with mock.patch.object(nav_api.NavApi, "_scratch_parent", judged_then_swapped):
                answer = getattr(nav, call)("src/main.ts")
            self.assertEqual((answer["status"], answer.get("reason")), ("refused", "scratch_refused"), (call, answer))
        tracer.on = False
        self.assertEqual(tracer.touched(ssh), [])
        self.assertEqual(list((ssh / "tmp").iterdir()), [])
        self.assertEqual(self.deno_calls("doc") + self.deno_calls("info"), [])

    def test_r8_a_mirror_folder_swapped_for_a_link_is_not_written_through(self):
        project = self.project().resolve()
        mirror = graph.Mirror(str(project), str(self.dir))
        self.addCleanup(mirror.cleanup)
        mirror.write("src/a.ts", b"x")
        shutil.rmtree(mirror.path("src"))
        os.symlink(self.trap, mirror.path("src"))
        before = self.listing(self.trap)
        self.assertRaises(OSError, mirror.write, "src/b.ts", b"LEAK")
        self.assertRaises(OSError, mirror.write, "src/deep/c.ts", b"LEAK")
        self.assertEqual(self.listing(self.trap), before)

    def test_r8_the_default_scratch_folder_is_judged_before_anything_is_made_or_written_there(self):
        """`TMPDIR` names the default scratch folder; `tempfile.gettempdir()` would write a test file in it before any judgement.
        A sensitive one (or one that leads there) refuses the call, and nothing is made, written or looked at in it."""
        project = self.project().resolve()
        ssh = self.dir / "home" / ".ssh"
        (ssh / "tmp").mkdir(parents=True)
        (self.dir / "to-ssh").symlink_to(ssh / "tmp")
        for tmpdir in (ssh / "tmp", self.dir / "to-ssh", "relative/tmp"):
            with mock.patch.dict(os.environ, {"TMPDIR": str(tmpdir)}), mock.patch.object(tempfile, "tempdir", None):
                tracer = FsTracer(self)
                nav = self.nav(project, deno=self.scripted_deno(doc={"nodes": {}}, info={"modules": []}))
                answer = nav.symbols("src/main.ts")
                tracer.on = False
            self.assertEqual((answer["status"], answer.get("reason")), ("refused", "scratch_refused"), (tmpdir, answer))
            self.assertEqual(tracer.touched(ssh), [], tmpdir)
            self.assertEqual(list((ssh / "tmp").iterdir()), [])
        with mock.patch.dict(os.environ, {"TMPDIR": str(self.dir)}):
            self.assertEqual(self.nav(project)._scratch_parent(), str(self.dir))

    def test_r8_an_own_folder_whose_parent_was_swapped_for_a_link_is_not_removed_through_it(self):
        parent = self.dir / "scratch"
        parent.mkdir()
        made = graph.make_own_folder(str(parent), "tp-x-")
        write(Path(made) / "a" / "f", "x")
        twin = self.trap / os.path.basename(made)  # a folder of the same name where the link leads
        write(twin / "keep", "KEEP")
        shutil.move(str(parent), str(self.dir / "moved"))
        parent.symlink_to(self.trap)
        graph.remove_own_folder(made)
        self.assertEqual((twin / "keep").read_text(), "KEEP")
        os.unlink(parent)
        shutil.move(str(self.dir / "moved"), str(parent))
        graph.remove_own_folder(made)
        self.assertFalse(os.path.lexists(made))

    def test_r8_a_mirror_is_made_only_in_a_judged_parent(self):
        project = self.project().resolve()
        tracer = FsTracer(self)
        with mock.patch.object(tempfile, "tempdir", str(self.trap)):
            for parent in (None, ""):
                with self.assertRaises(graph.Unproven) as raised:
                    graph.Mirror(str(project), parent)
                self.assertEqual(raised.exception.reason, "scratch_refused")
        tracer.on = False
        self.assertEqual(tracer.touched(self.trap), [])

    def test_r8_a_scratch_folder_that_is_sensitive_inside_the_project_or_led_there_is_refused_unlooked_at(self):
        project = self.project().resolve()
        ssh = self.dir / "home" / ".ssh"
        (ssh / "tmp").mkdir(parents=True)
        (self.dir / "to-ssh").symlink_to(ssh / "tmp")
        (self.dir / "to-project").symlink_to(project / "src")
        for label, parent in {"in .ssh": ssh / "tmp", "a link into .ssh": self.dir / "to-ssh",
                              "inside the project": project / "src", "a link into the project": self.dir / "to-project"}.items():
            with self.subTest(label):
                tracer = FsTracer(self)
                nav = self.nav(project, deno=self.scripted_deno(doc={"nodes": {}}, info={"modules": []}), mirror_parent=str(parent))
                answer = nav.symbols("src/main.ts")
                tracer.on = False
                self.assertEqual((answer["status"], answer.get("reason")), ("refused", "scratch_refused"), (label, answer))
                self.assertEqual(tracer.touched(ssh), [], label)
                self.assertEqual(list((ssh / "tmp").iterdir()), [])
                self.assertEqual(self.deno_calls("doc"), [])
        self.assertFalse([p for p in (project / "src").iterdir() if p.name.startswith("tp-ctx")])

    def test_r8_every_path_handed_to_a_process_is_the_sandbox_the_mirror_or_a_judged_grant(self):
        project = self.project().resolve()
        started = self.spied_launches()
        nav = self.nav(project, deno=self.scripted_deno(doc={"nodes": {}}, info={"modules": []}))
        for call in (nav.symbols, nav.forward_graph):
            call("src/main.ts")
        nav.imports("src/main.ts", source="deno")
        if not started:
            self.skipTest("no process was started (no sandbox here)")
        for argv, env in started:
            params = self.params(argv)
            self.assertEqual(argv[0], nav_api.SANDBOX_EXEC)
            granted = {params["ROOT"], params["DENODIR"], params["DENOBIN"]}
            for grant in (params["DENODIR"], params["DENOBIN"]):
                self.assertEqual(nav_api.judged_location(grant), grant)
                self.assertFalse(nav_api._sensitive_location(grant))
            self.assertEqual(env["DENO_DIR"], params["DENODIR"])
            for word in argv[1:]:
                for path in [word.split("=", 1)[1] if "=" in word else word]:
                    if path.startswith(os.sep) and path != nav_api.SANDBOX_EXEC:
                        self.assertTrue(path in granted or path.startswith(params["ROOT"] + os.sep), (path, argv))
            for key, value in env.items():
                if key not in ("PATH", "HOME", "TMPDIR") and value.startswith(os.sep):
                    self.assertIn(value, granted, key)
            self.assertFalse(params["ROOT"].startswith(str(project)), "the mirror is never in the project")

    def test_r8_the_tracer_sees_each_way_a_location_is_looked_at(self):
        """The tracer itself: each of these touches the trap, and is seen."""
        (self.dir / "to-trap").symlink_to(self.trap)
        ways = {"isfile": lambda: os.path.isfile(self.trap / "x.ts"), "exists": lambda: os.path.exists(self.dir / "to-trap" / "no"),
                "realpath": lambda: os.path.realpath(self.dir / "to-trap" / "x.ts"), "listdir": lambda: os.listdir(self.trap),
                "scandir": lambda: list(os.scandir(self.dir / "to-trap")),
                "dir_fd lstat": lambda: self._at(lambda fd: os.lstat("x.ts", dir_fd=fd)),
                "open": lambda: os.close(os.open(self.dir / "to-trap" / "x.ts", os.O_RDONLY)),
                "which": lambda: shutil.which("x.ts", path=str(self.trap)), "mkdir": lambda: os.mkdir(self.trap / "new")}
        for label, way in ways.items():
            tracer = FsTracer(self)
            way()
            tracer.on = False
            self.assertTrue(tracer.touched(self.trap), label)

    def _at(self, use):
        fd = os.open(self.trap, os.O_RDONLY)
        try:
            return use(fd)
        finally:
            os.close(fd)

    # --- the rule, by scan: every file-system call in both files, by owner

    FS_CALLS = {  # (file, owner, call): the check it passes. A call added anywhere, or moved, fails the test until it is listed here.
        ("graph.py", "resolve", "isfile"): "the caller's judge (`ImportIndex._verdicts`): judged_realpath, then lstat_nofollow",
        ("graph.py", "_look", "os.open"): "folder descriptor, NOFOLLOW_ANY; the caller judged the name (judged_realpath, lstat_nofollow)",
        ("graph.py", "_look", "os.lstat"): "dir_fd-relative to that descriptor",
        ("graph.py", "_look", "os.readlink"): "dir_fd-relative to that descriptor",
        ("graph.py", "_opened_path", "fcntl.fcntl"): "an open descriptor (F_GETPATH)",
        ("graph.py", "_opened_path", "os.readlink"): "/proc/self/fd (Linux)",
        ("graph.py", "read_bounded", "os.open"): "boundary + guard on both names before; NOFOLLOW_ANY; kernel path checked",
        ("graph.py", "read_bounded", "os.fstat"): "the checked descriptor",
        ("graph.py", "read_bounded", "os.read"): "after every check",
        ("graph.py", "write_new", "os.open"): "O_EXCL|O_NOFOLLOW, relative to a checked folder descriptor",
        ("graph.py", "write_new", "os.write"): "the new file",
        ("graph.py", "open_folder_below", "os.open"): "base NOFOLLOW_ANY, then each part O_NOFOLLOW relative to the one before",
        ("graph.py", "open_folder_below", "os.mkdir"): "dir_fd-relative, a part of a judged folder (cache) or of the own mirror",
        ("graph.py", "CacheDir.write_json", "os.lstat"): "dir_fd of the checked folder; a plain .json name the guard allows",
        ("graph.py", "CacheDir.write_json", "os.unlink"): "dir_fd of the checked folder",
        ("graph.py", "CacheDir.write_json", "os.rename"): "dir_fd of the checked folder",
        ("graph.py", "judged_is_dir", "os.lstat"): "the boundary itself (base == path)",
        ("graph.py", "make_own_folder", "os.open"): "the judged scratch parent, folder descriptor, NOFOLLOW_ANY; kernel path checked",
        ("graph.py", "make_own_folder", "os.mkdir"): "dir_fd-relative to that descriptor, a new random name",
        ("graph.py", "remove_own_folder", "os.open"): "the parent of an own folder, folder descriptor, NOFOLLOW_ANY; kernel path checked",
        ("graph.py", "remove_own_folder", "shutil.rmtree"): "dir_fd-relative to that descriptor: the own folder's name",
        ("graph.py", "prove_reach", "os.path.realpath"): "the boundary itself",
        ("graph.py", "ImportIndex.__init__", "os.path.realpath"): "the boundary itself",
        ("graph.py", "ImportIndex._walk", "os.open"): "a judged folder, NOFOLLOW_ANY; kernel path checked",
        ("graph.py", "ImportIndex._walk", "os.scandir"): "that descriptor",
        ("graph.py", "ImportIndex._walk", "os.lstat"): "dir_fd-relative, after the guard allowed the name",
        ("nav_api.py", "stub_check_path", "os.path.realpath"): "the boundary itself",
        ("nav_api.py", "NavApi.__init__", "os.path.realpath"): "the boundary itself",
        ("nav_api.py", "NavApi._executable", "os.path.isfile"): "a candidate resolved by judged_location (no link, not sensitive)",
        ("nav_api.py", "NavApi._executable", "os.access"): "the same resolved candidate",
        ("nav_api.py", "NavApi._guarded_roots", "os.path.lexists"): "monorepo markers: exact names in the project's ancestors",
        ("nav_api.py", "NavApi._probe_boundary", "os.lstat"): "monorepo root and markers: exact names in the project's ancestors, identity only",
        ("nav_api.py", "NavApi._grant_kinds", "os.lstat"): "a grant resolved by judged_location, not sensitive",
        ("nav_api.py", "NavApi._sandboxed", "os.access"): "the fixed SANDBOX_EXEC",
        ("nav_api.py", "NavApi._check_sandbox", "subprocess.run"): "SANDBOX_EXEC with judged grants and the probe folder",
        ("nav_api.py", "NavApi._run_deno", "subprocess.run"): "SANDBOX_EXEC with judged grants and the mirror",
    }

    @staticmethod
    def fs_calls():
        """[(file, owner, call)] for every call in graph.py and nav_api.py that asks the file system anything (os, os.path, shutil,
        tempfile, subprocess, fcntl, io, pathlib, glob, mmap, a Path method, `open`, the `isfile` judge), but the ones that only
        work on names."""
        import ast
        modules = ("os.", "shutil.", "tempfile.", "subprocess.", "fcntl.", "io.", "pathlib.", "Path(", "glob.", "mmap.")
        names_only = {"os.path.join", "os.path.normpath", "os.path.split", "os.path.dirname", "os.path.basename", "os.path.isabs",
                      "os.path.relpath", "os.path.commonpath", "os.path.abspath", "os.path.splitext", "os.fsdecode", "os.fspath",
                      "os.getpid", "os.close", "os.environ.get", "os.path.expanduser", "os.pathsep.join"}
        methods = {"stat", "lstat", "open", "read_text", "read_bytes", "write_text", "write_bytes", "iterdir", "glob", "rglob",
                   "exists", "is_file", "is_dir", "is_symlink", "resolve", "readlink", "scandir", "listdir", "mkdir", "unlink",
                   "touch", "samefile", "isfile", "isdir", "lexists", "realpath", "access", "which", "rename", "rmdir",
                   "chmod", "walk", "getsize", "getmtime"}  # os.replace and os.remove are found by their module
        found = []
        for name in ("graph.py", "nav_api.py"):
            tree = ast.parse((ROOT / "lib" / "ctxengine" / name).read_text())

            def visit(node, owner):
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, ast.ClassDef):
                        visit(child, child.name)
                    elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        visit(child, owner if owner and "." in owner or (owner and not owner[0].isupper())
                              else f"{owner}.{child.name}" if owner else child.name)
                    else:
                        if isinstance(child, ast.Call):
                            func = child.func
                            call = ast.unparse(func)
                            chained = isinstance(func, ast.Attribute) and isinstance(func.value, ast.Call)
                            method = isinstance(func, ast.Attribute) and func.attr in methods
                            if call not in names_only and (method or call in ("open", "isfile")
                                                           or (call.startswith(modules) and not chained)):
                                found.append((name, owner or "<module>", call))
                        visit(child, owner)
            visit(tree, "")
        return found

    def test_r8_every_file_system_call_in_both_files_is_a_listed_site_with_its_check(self):
        """Not only `open`: stat, lstat, listing, readlink, realpath, access, mkdir, rename, unlink, temp folders and processes.
        Each listed site's check is named in FS_CALLS and covered by the probes above."""
        found = self.fs_calls()
        self.assertEqual(sorted(set(found) - set(self.FS_CALLS)), [], "a file-system call that is not a listed site")
        self.assertEqual(sorted(set(self.FS_CALLS) - set(found)), [], "a listed site that is gone: update FS_CALLS")
        realpaths = sorted({(name, owner) for name, owner, call in found if call.endswith("realpath")})
        self.assertEqual(realpaths, [("graph.py", "ImportIndex.__init__"), ("graph.py", "prove_reach"),
                                     ("nav_api.py", "NavApi.__init__"), ("nav_api.py", "stub_check_path")],
                         "os.path.realpath follows links before their names are judged: only the project boundary itself")

    def test_r8_the_scan_sees_every_kind_of_call_it_bans(self):
        """The scan itself: each kind of call, put in a function of a scratch module, is found."""
        import ast
        sample = ("def f(p, d):\n    os.stat(p)\n    os.lstat(p)\n    os.listdir(p)\n    os.scandir(p)\n    os.path.realpath(p)\n"
                  "    os.path.isfile(p)\n    os.path.exists(p)\n    Path(p).read_text()\n    p.iterdir()\n    open(p)\n"
                  "    os.walk(p)\n    shutil.copy(p, d)\n    os.path.getsize(p)\n    glob.glob(p)\n")
        with mock.patch.object(Path, "read_text", lambda self: sample if self.name in ("graph.py", "nav_api.py") else ""):
            found = self.fs_calls()
        self.assertEqual(len([c for c in found if c[0] == "graph.py"]), 14, found)


class IncompleteIndexCase(Tree):
    """Round 10 (review 9 F1-F2, rule first): THE INDEX KEEPS A RECORD OF EVERY GAP, AND NO ANSWER IS `ok` WITH ONE OPEN. The index
    (`graph.ImportIndex.gaps`) records whatever it could not get, and `NavApi._fresh` is the one place an index answer is let
    through: a gap that is a refusal refuses (as the Deno side does), a gap that is a race gives `stale_index`, any other gives
    `error` with `reason`; each names the gap codes (counts, never paths). The split, decided by Main: a file or folder the guard
    refuses BY ITS NAME is outside the index by policy and is not a gap (`test_r9_policy_*`); a refusal at READ time, a path that
    turns out to be outside the root, an unreadable folder, file or config, and a config the index cannot use are gaps. Every probe
    of the family sweep is a test here; each failed against a83d75b (see the report)."""

    needs_user = unittest.skipIf(os.geteuid() == 0, "root can read a file with mode 0")

    def denied(self, path):
        path = Path(path)
        mode = path.stat().st_mode
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, mode & 0o7777)

    def blocked(self, answer, status, reason, code, label=""):
        self.assertEqual((answer["status"], answer.get("reason")), (status, reason), (label, answer))
        self.assertIn(code, answer.get("gaps", {}), (label, answer))
        for key in ("importers", "files", "external", "unresolved", "total"):
            self.assertNotIn(key, answer, (label, answer))  # nothing is answered from part of the project
        self.assertTrue(answer.get("next_step"), (label, answer))

    def both(self, nav):
        return {"importers": nav.importers("src/app/run.ts"), "imports": nav.imports("src/main.ts", source="index")}

    def assert_whole_again(self, nav):
        """The same questions, once the cause is gone, are answered in full (a failed read left nothing behind in the entries or the cache)."""
        answers = self.both(nav)
        self.assertEqual(answers["importers"]["status"], "ok", answers)
        self.assertEqual([(i["path"], i["line"]) for i in answers["importers"]["importers"]],
                         [("src/app/run_test.ts", 1), ("src/main.ts", 1), ("src/main.ts", 9)])
        self.assertEqual(answers["imports"]["status"], "ok", answers)
        self.assertTrue(answers["imports"]["files"], answers)

    # --- F1: a folder or file the index could not read

    @needs_user
    def test_r9_f1_repro_an_unreadable_folder_is_not_an_empty_one(self):
        project = self.project().resolve()
        nav = self.nav(project)
        self.denied(project / "src")
        answer = nav.importers("shared/types.ts")  # asked about a file beside the folder, so the guard has no objection to the target
        self.blocked(answer, "error", "index_incomplete", "folder_unreadable")
        os.chmod(project / "src", 0o755)
        self.assertEqual([i["path"] for i in nav.importers("src/util/slug.ts")["importers"]], ["src/util/index.ts"])

    @needs_user
    def test_r9_f1_repro_an_unreadable_source_is_not_a_module_without_imports(self):
        project = self.project().resolve()
        nav = self.nav(project)
        self.denied(project / "src" / "main.ts")
        answers = self.both(nav)
        self.blocked(answers["importers"], "error", "index_incomplete", "source_unreadable")
        self.blocked(answers["imports"], "error", "index_incomplete", "source_unreadable")
        os.chmod(project / "src" / "main.ts", 0o644)
        self.assert_whole_again(nav)  # not an entry with no imports, kept because its mtime did not change
        self.assert_whole_again(self.nav(project))  # nor one saved in the cache

    def test_r9_an_oversized_source_is_a_gap_not_an_empty_module(self):
        project = self.project().resolve()
        write(project / "src" / "huge.ts", "import './config.ts';\n" + "// pad\n" * 10)
        with mock.patch.object(graph, "MAX_BYTES", 30):
            self.blocked(self.nav(project).importers("src/config.ts"), "error", "index_incomplete", "source_unreadable")
        (project / "src" / "huge.ts").unlink()
        self.assertEqual(self.nav(project).importers("src/config.ts")["status"], "ok")

    @needs_user
    def test_r9_a_folder_that_can_be_entered_but_not_listed_is_a_gap(self):
        project = self.project().resolve()
        nav = self.nav(project)
        os.chmod(project / "src" / "util", 0o311)
        self.addCleanup(os.chmod, project / "src" / "util", 0o755)
        self.blocked(nav.importers("src/app/run.ts"), "error", "index_incomplete", "folder_unreadable")

    @needs_user
    def test_r9_a_folder_that_can_be_listed_but_not_looked_into_is_a_gap_and_not_a_guard_refusal(self):
        """Mode 0644: the names can be listed, none can be looked at, so the guard cannot judge them (`unreadable_path`). That is not
        a refusal by name (the policy): before, the walk skipped each as one and the answer was `ok` without them."""
        project = self.project().resolve()
        nav = self.nav(project)
        os.chmod(project / "src" / "util", 0o644)
        self.addCleanup(os.chmod, project / "src" / "util", 0o755)
        self.blocked(nav.importers("src/app/run.ts"), "error", "index_incomplete", "guard_unjudged")
        os.chmod(project / "src" / "util", 0o755)
        self.assert_whole_again(nav)

    def test_r9_a_guard_that_could_not_look_is_a_gap_in_the_index_and_a_refusal_everywhere_else(self):
        project = self.project().resolve()
        real = nav_api.stub_check_path

        def guard(root, value):
            if str(value).endswith("slug.ts"):
                raise nav_api.Refused("unreadable_path")
            return real(root, value)
        nav = self.nav(project, check_path=guard)
        self.blocked(nav.importers("src/app/run.ts"), "error", "index_incomplete", "guard_unjudged")
        self.assertEqual(nav.importers("src/util/slug.ts")["status"], "refused")  # the target itself: a refusal, as ever
        self.assertTrue(nav._permits("src/main.ts"))
        with self.assertRaises(graph.CannotJudge):
            nav._permits("src/util/slug.ts")
        index = graph.ImportIndex(str(project), str(project), str(project / "deno.json"), [], allow=nav._permits)
        index.refresh()
        self.assertEqual(graph.judged_is_file(str(project), "src/util/slug.ts", nav._permits), False)  # not an index user: a refusal

    def test_r9_a_link_loop_in_the_tree_cannot_be_judged_and_a_dangling_link_can(self):
        """A link that loops is a name the guard cannot follow (`unreadable_path`): a gap. A link to nothing leads nowhere: nothing to
        judge, nothing missing. Neither is followed or indexed (the walk's policy for every link)."""
        project = self.project().resolve()
        os.symlink("nowhere.ts", project / "src" / "dangling.ts")
        self.assert_whole_again(self.nav(project))
        os.symlink("loop.ts", project / "src" / "loop.ts")
        self.blocked(self.nav(project).importers("src/app/run.ts"), "error", "index_incomplete", "guard_unjudged")
        (project / "src" / "loop.ts").unlink()
        self.assert_whole_again(self.nav(project))

    def test_r9_a_fifo_source_is_not_a_module_and_a_fifo_config_is_a_gap(self):
        project = self.project().resolve()
        os.mkfifo(project / "src" / "pipe.ts")
        self.assert_whole_again(self.nav(project))  # not a regular file: nothing a module can be read from, so nothing is missing
        fifo = self.project("fifo").resolve()
        (fifo / "deno.json").unlink()
        os.mkfifo(fifo / "deno.json")
        self.blocked(self.nav(fifo).importers("src/app/run.ts"), "error", "index_incomplete", "config_unreadable")

    def test_r9_a_file_that_vanishes_between_the_listing_and_the_look_is_a_race(self):
        project = self.project().resolve()
        nav = self.nav(project)
        real = os.lstat

        def vanishing(path, *a, **k):
            if k.get("dir_fd") is not None and path == "slug.ts":
                raise FileNotFoundError(2, "gone", path)
            return real(path, *a, **k)
        with mock.patch.object(os, "lstat", vanishing):
            self.blocked(nav.importers("src/util/slug.ts"), "stale_index", None, "entry_changed")
        self.assert_whole_again(nav)

    def test_r9_an_entry_that_cannot_be_looked_at_is_a_gap(self):
        project = self.project().resolve()
        nav = self.nav(project)
        real = os.lstat

        def denied(path, *a, **k):
            if k.get("dir_fd") is not None and path == "slug.ts":
                raise PermissionError(13, "denied", path)
            return real(path, *a, **k)
        with mock.patch.object(os, "lstat", denied):  # the guard and the walk both look at it: the guard cannot judge it
            self.blocked(nav.importers("src/app/run.ts"), "error", "index_incomplete", "guard_unjudged")
        self.assert_whole_again(nav)

    def test_r9_an_entry_the_guard_passed_but_the_walk_cannot_look_at_is_a_gap(self):
        project = self.project().resolve()
        nav = self.nav(project)
        real, seen = os.lstat, []

        def denied(path, *a, **k):
            if k.get("dir_fd") is not None and path == "slug.ts":
                seen.append(1)
                if len(seen) > 1:  # the first look is the guard's, the second the walk's
                    raise PermissionError(13, "denied", path)
            return real(path, *a, **k)
        with mock.patch.object(os, "lstat", denied):
            self.blocked(nav.importers("src/app/run.ts"), "error", "index_incomplete", "entry_unreadable")
        self.assert_whole_again(nav)

    def test_r9_a_file_that_vanishes_between_the_walk_and_the_read_is_a_race(self):
        project = self.project().resolve()
        nav = self.nav(project)
        real = graph.read_sources

        def racing(paths, *a, **k):
            os.unlink(project / "src" / "lazy.ts")
            return real(paths, *a, **k)
        with mock.patch.object(graph, "read_sources", racing):
            self.blocked(nav.importers("src/app/run.ts"), "stale_index", None, "source_changed")
        self.assertEqual(nav.importers("src/app/run.ts")["status"], "ok")

    def test_r9_a_file_swapped_for_a_link_between_the_walk_and_the_read_is_not_read_and_is_a_gap(self):
        project = self.project().resolve()
        secret = write(self.dir / "secret.ts", "import './LEAKED.ts';\n")
        real = graph.read_sources

        def racing(paths, *a, **k):
            os.unlink(project / "src" / "lazy.ts")
            os.symlink(secret, project / "src" / "lazy.ts")
            return real(paths, *a, **k)
        with mock.patch.object(graph, "read_sources", racing):  # a link that leads out is "outside" at read time: a gap, and not read
            self.blocked(self.nav(project).importers("src/app/run.ts"), "error", "index_incomplete", "source_refused")

    def test_r9_a_folder_swapped_for_a_link_after_the_listing_is_a_race(self):
        project = self.project().resolve()
        elsewhere = self.dir / "elsewhere"
        write(elsewhere / "slug.ts", "export const SWAPPED = 1;\n")
        real = os.open
        swapped = []

        def swapping(path, *a, **k):  # after the listing saw a folder, before the walk opens it
            if str(path) == str(project / "src" / "util") and not swapped:
                swapped.append(1)
                shutil.move(str(project / "src" / "util"), str(self.dir / "moved-util"))
                (project / "src" / "util").symlink_to(elsewhere)
            return real(path, *a, **k)
        with mock.patch.object(os, "open", swapping):
            self.blocked(self.nav(project).importers("src/app/run.ts"), "stale_index", None, "folder_changed")
        self.assertEqual(swapped, [1])

    def test_r9_a_walk_that_is_not_the_one_judged_is_a_race_and_one_the_kernel_cannot_name_is_a_gap(self):
        project = self.project().resolve()
        with mock.patch.object(graph, "_opened_path", lambda fd: "/somewhere/else"):
            self.blocked(self.nav(project).importers("src/app/run.ts"), "stale_index", None, "folder_changed")
        with mock.patch.object(graph, "_opened_path", lambda fd: None):
            self.blocked(self.nav(project).importers("src/app/run.ts"), "error", "index_incomplete", "folder_unreadable")

    # --- the split: refused by name is policy; refused or outside at read time is a gap

    def test_r9_policy_a_file_or_folder_refused_by_its_name_is_outside_the_index_and_not_a_gap(self):
        project = self.project().resolve()
        write(project / "src" / "secrets" / "s.ts", "import '../util/slug.ts';\n")
        write(project / "src" / ".env.ts", "import '../util/slug.ts';\n")
        write(project / "src" / ".ssh" / "id.ts", "import '../util/slug.ts';\n")
        answer = self.nav(project).importers("src/util/slug.ts")
        self.assertEqual(answer["status"], "ok", answer)
        self.assertNotIn("gaps", answer)
        self.assertEqual([i["path"] for i in answer["importers"]], ["src/util/index.ts"])  # the refused files' edges are not named

    def test_r9_a_refusal_at_read_time_is_a_gap(self):
        project = self.project().resolve()
        state = {"late": False}

        def check(root, path):
            if state["late"] and path.endswith("util/slug.ts"):
                raise nav_api.Refused("sensitive_path")  # allowed when the walk judged the name, refused when the read does
            return nav_api.stub_check_path(root, path)
        real = graph.read_sources

        def racing(paths, *a, **k):
            state["late"] = True
            return real(paths, *a, **k)
        with mock.patch.object(graph, "read_sources", racing):
            self.blocked(self.nav(project, check_path=check).importers("src/app/run.ts"), "error", "index_incomplete", "source_refused")

    def test_r9_a_read_that_is_outside_the_root_is_a_gap(self):
        project = self.project().resolve()
        real = graph.read_bounded

        def outside(path, admitted, boundary, allow, limit=None, **checks):  # `expect`, `take`: the read's own checks, passed on
            if str(path).endswith("util/slug.ts"):
                return "outside", None, None
            return real(path, admitted, boundary, allow, limit, **checks)
        with mock.patch.object(graph, "read_bounded", outside):
            self.blocked(self.nav(project).importers("src/app/run.ts"), "error", "index_incomplete", "source_refused")

    # --- F2: a config the index could not use

    def test_r9_f2_repro_a_refused_config_refuses_the_index_answers_as_it_does_deno(self):
        project = self.project().resolve()
        write(project / ".git" / "deno.json", (project / "deno.json").read_text())
        self.assertRaises(nav_api.Refused, nav_api.stub_check_path, str(project), ".git/deno.json")
        nav = self.nav(project, config=str(project / ".git" / "deno.json"), deno=self.fake_deno('echo "{}"'))
        self.assertEqual(nav.symbols("src/main.ts")["status"], "refused")  # the Deno side
        answers = self.both(nav)
        self.blocked(answers["importers"], "refused", "reaches_refused_path", "config_refused")
        self.blocked(answers["imports"], "refused", "reaches_refused_path", "config_refused")
        auto = nav.imports("src/main.ts")  # `auto` falls back to `deno info`, whose own proof refuses the same config
        self.assertEqual((auto["status"], auto.get("reason")), ("refused", "reaches_refused_path"), auto)
        self.assertNotIn(".git", json.dumps(answers))

    def test_r9_f2_a_refused_or_outside_overlay_refuses_too(self):
        project = self.project().resolve()
        write(project / ".git" / "deno.test.json", json.dumps({"imports": {"@host": "./src/mock_host.ts"}}))
        nav = self.nav(project, overlays=[str(project / ".git" / "deno.test.json")])
        self.blocked(nav.importers("src/app/run.ts"), "refused", "reaches_refused_path", "config_refused", "overlay")
        outside = write(self.dir / "outside.json", json.dumps({"imports": {}}))
        linked = self.project("linked").resolve()
        (linked / "deno.test.json").unlink()
        (linked / "deno.test.json").symlink_to(outside)
        got = self.nav(linked, overlays=[str(linked / "deno.test.json")]).importers("src/app/run.ts")
        self.assertEqual(got["status"], "refused", got)
        self.assertIn("config_refused", got["gaps"])

    def test_r9_f2_a_config_that_leads_outside_or_changes_is_refused_or_a_race(self):
        project = self.project().resolve()
        index = graph.ImportIndex(str(project), cache_path=None, allow=stub_allow(project))
        with mock.patch.object(graph, "_config_read", lambda *a: (("outside",), None)):
            index.refresh()
        self.assertEqual(index.incomplete()[:2], ("refused", "outside_project"))
        self.assertIn("config_outside", index.gaps)
        with mock.patch.object(graph, "_config_read", lambda *a: (("changed",), None)):
            index.refresh()
        self.assertEqual(index.incomplete()[0], "changed")
        self.assertIn("config_changed", index.gaps)

    def test_r9_f2_a_config_that_cannot_be_parsed_or_modelled_is_a_gap(self):
        project = self.project().resolve()
        original = (project / "deno.json").read_text()
        nav = self.nav(project)
        (project / "deno.json").write_text("{ not json")
        self.blocked(nav.importers("src/app/run.ts"), "error", "unmodelled_config", "config_invalid")
        (project / "deno.json").write_text("[1]")
        self.blocked(nav.importers("src/app/run.ts"), "error", "unmodelled_config", "config_invalid")
        for key, value in (("importMap", "./map.json"), ("patch", ["../x"]), ("unstable", ["sloppy-imports"]), ("workspace", ["./a"])):
            config = json.loads(original)
            config[key] = value
            (project / "deno.json").write_text(json.dumps(config))
            self.blocked(nav.importers("src/app/run.ts"), "error", "unmodelled_config", "config_unmodelled", key)
        (project / "deno.json").write_text(original)
        self.assert_whole_again(nav)

    def test_r9_no_config_at_all_is_a_complete_index(self):
        project = self.project().resolve()
        (project / "deno.json").unlink()
        answer = self.nav(project).importers("src/util/slug.ts")
        self.assertEqual(answer["status"], "ok", answer)  # nothing is there, and the kernel said so: the map is empty, not unknown
        self.assertNotIn("gaps", answer)

    @needs_user
    def test_r9_an_unreadable_config_is_a_gap(self):
        project = self.project().resolve()
        nav = self.nav(project)
        self.denied(project / "deno.json")
        self.blocked(nav.importers("src/app/run.ts"), "error", "index_incomplete", "config_unreadable")
        os.chmod(project / "deno.json", 0o644)
        self.assert_whole_again(nav)

    def test_r9_a_root_that_is_refused_is_a_gap_in_the_index_itself(self):
        project = self.project().resolve()
        index = graph.ImportIndex(str(project), root=str(project / ".ssh"), cache_path=None, allow=stub_allow(project))
        index.refresh()
        self.assertEqual(index.incomplete()[:2], ("refused", "reaches_refused_path"))
        self.assertIn("root_refused", index.gaps)

    # --- an edge whose target could not be looked at

    def test_r9_an_import_of_a_file_that_cannot_be_looked_at_is_not_a_missing_file(self):
        project = self.project().resolve()  # src/main.ts imports ./data.json, which the walk does not index: it is looked at by name
        real_look, real_nofollow = graph._look, graph.lstat_nofollow
        for label, patched, code in (("judging the way", "_look", "guard_unjudged"), ("looking at the file", "lstat_nofollow", "target_unreadable")):
            def denied(path, *a, _real=real_look if patched == "_look" else real_nofollow, **k):
                if str(path).endswith("data.json"):
                    raise PermissionError(13, "denied", path)
                return _real(path, *a, **k)
            with mock.patch.object(graph, patched, denied):
                self.blocked(self.nav(project).imports("src/main.ts", source="index"), "error", "index_incomplete", code, label)

    def test_r9_the_reach_walk_takes_nothing_there_as_nothing_and_a_file_it_cannot_look_at_as_unproven(self):
        """`prove_reach` (the Deno side) has its own `continue`: a file gone is nothing; one that cannot be looked at is not."""
        project = self.project().resolve()
        real = graph.lstat_nofollow

        def look(path):
            if str(path).endswith("config.ts"):
                raise PermissionError(13, "denied", path)
            return real(path)
        for patched, ok in ((real, True), (look, False)):
            with mock.patch.object(graph, "lstat_nofollow", patched):
                nav = self.nav(project)
                answer = nav.imports("src/main.ts", source="deno")
                if ok:
                    self.assertEqual(answer["status"], "ok", answer)
                else:
                    self.assertEqual((answer["status"], answer.get("reason")), ("refused", "unreadable_file"), answer)

    def test_r9_a_target_that_is_not_there_is_missing_and_not_a_gap(self):
        project = self.project().resolve()
        write(project / "src" / "uses.ts", "import './nowhere.ts';\n")
        answer = self.nav(project).imports("src/uses.ts", source="index")
        self.assertEqual(answer["status"], "ok", answer)
        self.assertEqual([u["reason"] for u in answer["unresolved"]], ["missing"])

    # --- bookkeeping

    def test_r9_gaps_are_forgotten_when_the_cause_is_gone_and_do_not_pile_up(self):
        project = self.project().resolve()
        nav = self.nav(project)
        seen = []
        for _ in range(2):
            with mock.patch.object(graph, "_opened_path", lambda fd: None):
                seen.append(nav.importers("src/app/run.ts")["gaps"])
        self.assertEqual(seen[0], seen[1])
        self.assert_whole_again(nav)

    def test_r9_status_says_what_the_index_could_not_get(self):
        project = self.project().resolve()
        nav = self.nav(project)
        with mock.patch.object(graph, "_opened_path", lambda fd: None):
            nav.importers("src/app/run.ts")
        self.assertIn("folder_unreadable", nav.status().get("index_gaps", {}))

    def test_r9_a_cache_written_before_this_change_is_not_trusted(self):
        project = self.project().resolve()
        cache = project_cache(project, nav_api.NavApi._cache_name("index", self.nav(project)._root_key()))
        write(cache, json.dumps({"version": 1, "base": str(project), "root": str(project),
                                 "entries": {"src/main.ts": {"key": [0, 0], "specs": []}}}))
        index = graph.ImportIndex(str(project), cache_path=str(cache), allow=stub_allow(project))
        self.assertEqual(index.entries, {})
        self.assertEqual(self.nav(project).importers("src/app/run.ts")["status"], "ok")


DATA_KEYS = ("target", "total", "truncated", "importers", "files", "external", "unresolved", "source", "index_age_s",
             "unresolved_total", "files_indexed", "scanned", "ignored_config")
QUESTIONS = (("importers", "src/app/run.ts"), ("importers", "src/real_host.ts"), ("importers", "src/mock_host.ts"),
             ("importers", "src/util/slug.ts"), ("imports", "src/main.ts"), ("importers", "src/later.ts"))


class AnswerProofCase(Tree):
    """Round 11 (review 10 F1-F3, rule first): EVERY INDEX ANSWER IS PROVED AT ANSWER TIME, AND A NON-OK ONE CARRIES THE GAP AND NO
    DATA. `NavApi._index_answer` is the one way an answer is made from the index: after the answer is built, `ImportIndex.prove()`
    re-proves every input it depends on (each folder listing by a new walk, each source by stat and open-for-read with its
    signature equal to the indexed one, each config and overlay by its fingerprint, each import target looked at by a new verdict);
    a failed proof is an answer with `gaps` and nothing else. Warm entries, entries loaded from the cache and a cold build get the
    same proof. The family sweep is `test_r10_sweep_*`: every input x every state x every entry path, held to one oracle (an `ok`
    answer is what a new instance with no cache says about the tree as it is after the change; anything else has gaps and no
    data; once nothing moves, no spurious failure)."""

    def ask(self, nav, question):
        verb, path = question
        return nav.importers(path) if verb == "importers" else nav.imports(path, source="index")

    def nav(self, project, cache="ctx-cache", **kw):
        kw.setdefault("overlays", [str(Path(project) / "deno.test.json")])
        return nav_api.NavApi(str(project), cache_dir=str(Path(project) / ".tmux-pi" / cache), **kw)

    def assert_no_data(self, answer, label=""):
        self.assertNotEqual(answer["status"], "ok", (label, answer))
        self.assertTrue(answer.get("gaps") or answer["status"] in ("not_found", "bad_argument", "refused"), (label, answer))
        for key in DATA_KEYS:
            self.assertNotIn(key, answer, (label, answer))

    @staticmethod
    def comparable(answer):
        return {k: v for k, v in answer.items() if k not in ("index_age_s", "scanned", "cache_refused")}

    def expected(self, project):
        """The answers of a new instance with no cache, on the tree as it is now: what an `ok` answer must say."""
        self._fresh_n = getattr(self, "_fresh_n", 0) + 1
        nav = self.nav(project, cache=f"fresh-{self._fresh_n}")
        return [self.ask(nav, q) for q in QUESTIONS]

    def sound(self, got, want, label, live):
        """`ok` only when it is what a fresh build says; otherwise gaps and no data. `live`: once nothing is moving, an answer the
        fresh build gives as `ok` is `ok` here too (no spurious failure)."""
        if got["status"] == "ok":
            self.assertEqual(self.comparable(got), self.comparable(want), label)
            return
        self.assert_no_data(got, label)
        if live:
            self.assertEqual(got["status"], want["status"], (label, got, want))

    def unlock(self, project):
        for folder, dirs, files in os.walk(project):
            for name in dirs + files:
                full = os.path.join(folder, name)
                if not os.path.islink(full):
                    try:
                        os.chmod(full, 0o755 if os.path.isdir(full) else 0o644)
                    except OSError:
                        pass

    def skip_if_root(self):
        if os.geteuid() == 0:
            self.skipTest("root can read a file with mode 0")

    # --- the findings, as reported

    def test_r10_f1_repro_a_warm_index_does_not_answer_from_a_source_that_became_unreadable(self):
        self.skip_if_root()
        project = self.project().resolve()
        nav = self.nav(project)
        self.assertEqual(nav.importers("src/app/run.ts")["status"], "ok")
        os.chmod(project / "src" / "main.ts", 0)
        self.addCleanup(os.chmod, project / "src" / "main.ts", 0o644)
        for label, instance in (("same instance", nav), ("fresh instance, loaded cache", self.nav(project))):
            answer = instance.importers("src/app/run.ts")
            self.assert_no_data(answer, label)
            self.assertEqual((answer["status"], answer.get("reason")), ("error", "index_incomplete"), (label, answer))
            self.assertIn("source_unreadable", answer["gaps"], (label, answer))
        os.chmod(project / "src" / "main.ts", 0o644)
        self.assertEqual(nav.importers("src/app/run.ts")["status"], "ok")

    def test_r10_f2_repro_a_config_that_changes_during_the_build_is_a_race(self):
        project = self.project().resolve()
        nav = self.nav(project, overlays=[])
        self.assertEqual(len(nav.importers("src/real_host.ts")["importers"]), 2)
        real = graph.read_sources
        config = project / "deno.json"

        def moving(paths, *a, **k):
            config.write_text(config.read_text().replace("./src/real_host.ts", "./src/mock_host.ts"))
            return real(paths, *a, **k)
        with mock.patch.object(graph, "read_sources", moving):
            answer = nav.importers("src/real_host.ts")
        self.assert_no_data(answer)
        self.assertEqual(answer["status"], "stale_index", answer)
        self.assertIn("config_changed", answer["gaps"])
        after = nav.importers("src/real_host.ts")
        self.assertEqual((after["status"], after["importers"]), ("ok", []), after)

    def test_r10_f2_repro_a_config_deleted_just_before_its_read_is_a_race_not_an_absent_config(self):
        project = self.project().resolve()
        nav = self.nav(project, overlays=[])
        real = graph.read_bounded
        config = str(project / "deno.json")
        done = []

        def vanishing(path, admitted, *a, **k):
            if str(admitted) == config and not done:
                done.append(1)
                os.unlink(config)
            return real(path, admitted, *a, **k)
        with mock.patch.object(graph, "read_bounded", vanishing):
            answer = nav.importers("src/real_host.ts")
        self.assertEqual(done, [1])
        self.assert_no_data(answer)
        self.assertEqual(answer["status"], "stale_index", answer)
        self.assertIn("config_changed", answer["gaps"])
        gone_before = self.nav(project, cache="other", overlays=[]).importers("src/real_host.ts")
        self.assertEqual(gone_before["status"], "ok", gone_before)  # absent before the call: nothing there, no gap

    def test_r10_f3_repro_a_stat_that_fails_after_the_read_never_returns_data(self):
        for label, error, status in (("denied", PermissionError(13, "denied"), "error"),
                                     ("gone", FileNotFoundError(2, "gone"), "stale_index")):
            project = self.project(label).resolve()
            nav = self.nav(project)
            after_read = []
            real_sources, real_nofollow, real_lstat = graph.read_sources, graph.lstat_nofollow, os.lstat

            def reading(paths, *a, **k):
                out = real_sources(paths, *a, **k)
                after_read.append(1)
                return out

            def nofollow(path, *a, **k):
                if after_read and str(path).endswith("src/main.ts"):
                    raise error
                return real_nofollow(path, *a, **k)

            def lstat(path, *a, **k):
                if after_read and k.get("dir_fd") is not None and path == "main.ts":
                    raise error
                return real_lstat(path, *a, **k)
            with mock.patch.object(graph, "read_sources", reading), mock.patch.object(graph, "lstat_nofollow", nofollow), \
                    mock.patch.object(os, "lstat", lstat):
                answer = nav.importers("src/app/run.ts")
            self.assert_no_data(answer, label)
            self.assertEqual(answer["status"], status, (label, answer))

    def test_r10_the_index_alone_says_what_its_proof_found(self):
        project = self.project().resolve()
        index = graph.ImportIndex(str(project), cache_path=None, allow=stub_allow(project))
        index.refresh()
        self.assertEqual(index.prove(), {})
        (project / "src" / "lazy.ts").write_text("export const lazy = 2; // longer\n")
        self.assertIn("source_changed", index.prove())
        write(project / "src" / "new.ts", "export const n = 1;\n")
        self.assertIn("folder_changed", index.prove())
        index.refresh()
        self.assertEqual(index.prove(), {})
        self.assertIsNone(index.incomplete())

    def test_r10_a_not_found_answer_is_proved_too_and_an_unreadable_target_is_not_a_missing_one(self):
        project = self.project().resolve()
        nav = self.nav(project)
        real = graph.lstat_nofollow

        def denied(path, *a, **k):
            if str(path).endswith("src/nothing.ts"):
                raise PermissionError(13, "denied", path)
            return real(path, *a, **k)
        self.assertEqual(nav.importers("src/nothing.ts")["status"], "not_found")
        with mock.patch.object(graph, "lstat_nofollow", denied):
            answer = nav.importers("src/nothing.ts")
        self.assert_no_data(answer)
        self.assertEqual((answer["status"], answer.get("reason")), ("error", "index_incomplete"), answer)
        self.assertIn("target_unreadable", answer["gaps"])

    def test_r10_sweep2_a_source_that_stops_being_a_file_during_the_read_is_a_race(self):
        """Sweep pass 2: the walk keyed a regular file; a folder, FIFO or bigger file found at its read is that file changing
        (`stale_index`, ask again), not a file that cannot be read; asked again, the answer is the tree's own."""
        for state in ("folder", "fifo", "grown"):
            with self.subTest(state=state):
                project = self.project(f"race-{state}").resolve()
                nav = self.nav(project, cache=f"cold-{state}")
                path = project / "src" / "main.ts"
                real, moved = graph.read_sources, []

                def moving(paths, *a, **k):
                    if not moved:
                        moved.append(1)
                        if state == "grown":
                            path.write_text("x" * (graph.MAX_BYTES + 1))
                        else:
                            path.unlink()
                            path.mkdir() if state == "folder" else os.mkfifo(path)
                    return real(paths, *a, **k)
                with mock.patch.object(graph, "read_sources", moving):
                    answer = nav.importers("src/app/run.ts")
                self.assert_no_data(answer, state)
                self.assertEqual(answer["status"], "stale_index", (state, answer))
                self.assertIn("source_changed", answer["gaps"], (state, answer))
                want = self.expected(project)
                self.sound(self.ask(nav, ("importers", "src/app/run.ts")), want[0], state, live=True)
                self.assertEqual(graph.read_bounded(str(path), str(path), str(project), lambda rel: True,
                                                    expect=[1, 1], take=False)[0], "changed", state)

    def test_r10_the_deno_side_takes_a_package_json_gone_at_its_read_as_a_change_too(self):
        """The F2 rule lives in `_config_read`, which the Deno side's reach proof shares: a `package.json` there when its name was
        judged and gone at its read refuses the call (`changed_during_walk`); it is not skipped as if there were none."""
        project = self.project("pkg-gone").resolve()
        write(project / "package.json", '{"name": "x"}')
        nav = self.nav(project)
        real = graph.read_bounded

        def vanishing(path, *a, **k):
            if str(path).endswith("package.json") and os.path.exists(path):
                os.unlink(path)
            return real(path, *a, **k)
        with mock.patch.object(graph, "read_bounded", vanishing):
            with self.assertRaises(graph.Unproven) as raised:
                graph.prove_reach(nav.project_root, str(project / "src" / "main.ts"), nav.config, nav._permits)
        self.assertEqual(raised.exception.reason, "changed_during_walk")
        graph.prove_reach(nav.project_root, str(project / "src" / "main.ts"), nav.config, nav._permits)  # gone before: no config

    def test_r10_the_gate_lets_no_data_out_with_a_status_other_than_ok(self):
        project = self.project().resolve()
        nav = self.nav(project)
        answer, blocked, proof = nav._index_answer(lambda index, stats: nav_api._answer("stale_index", target="x", importers=[1]))
        self.assertFalse(blocked)
        self.assertIsNotNone(proof)
        self.assertEqual(set(answer) - {"status", "next_step", "detail"}, set(), answer)

    # --- the family sweep: every input x every state x every entry path

    STATES = ("unchanged", "changed", "vanished", "unreadable", "folder", "file", "fifo", "link", "link_out", "stat_fails")
    # an entry path: `cold` (a new instance with no cache), `warm` (the instance that answered before), `loaded` (a new instance
    # loading the cache the first one saved), or a change made while an answer is being made: in the build's read of the sources
    # (`read_*`), at its walk (`walk_*`), or after the build, at the proof (`proof_*`), on a warm instance or a cold one
    # (round 12: `screen_*`, a change made while the answer is being screened, after the first proof and before the last)
    ROUTES = ("cold", "warm", "loaded", "read_warm", "read_cold", "walk_warm", "walk_cold", "proof_warm", "proof_cold",
              "screen_warm", "screen_cold")
    HOOKS = {"read": (graph, "read_sources"), "walk": (graph.ImportIndex, "_walk"), "proof": (graph.ImportIndex, "prove"),
             "screen": (None, "guard")}  # the output screen is the instance's own `guard`
    NAMES = {"source": "src/main.ts", "config": "deno.json", "overlay": "deno.test.json", "folder": "src/util",
             "nested_folder": "src/app", "target": "src/data.json", "missing_target": "src/later.ts"}

    def where(self, project, what):
        if what == "cache":
            folder = project / ".tmux-pi" / "ctx-cache"
            found = sorted(folder.glob("nav_api-index-*.json")) if folder.exists() else []
            return found[0] if found else folder / "nav_api-index-none.json"
        return project / self.NAMES[what]

    def mutate(self, project, what, state):
        """Put input `what` into `state`; returns a context manager the answers are asked in (for `stat_fails`, the injection)."""
        import contextlib
        path = self.where(project, what)
        if state == "unchanged":
            return contextlib.nullcontext()
        if what == "missing_target" and state != "vanished":
            write(path, "export const later = 1;\n")  # a target that was not there, now there in the given state
            if state == "changed":
                return contextlib.nullcontext()
        if state == "stat_fails":
            real, name = os.lstat, path.name

            def failing(p, *a, **k):
                if k.get("dir_fd") is not None and p == name:
                    raise PermissionError(13, "denied", p)
                return real(p, *a, **k)
            return mock.patch.object(os, "lstat", failing)
        if state == "changed":
            if what == "source":
                path.write_text(path.read_text().replace("import { run } from '@/app/run.ts';\n", "import './lazy.ts';\n"))
            elif what == "config":
                path.write_text(path.read_text().replace("./src/real_host.ts", "./src/mock_host.ts"))
            elif what == "overlay":
                path.write_text(path.read_text().replace("./src/mock_host.ts", "./src/real_host.ts"))
            elif what in ("folder", "nested_folder"):
                write(path / "extra.ts", "import '../app/run.ts';\n" if what == "folder" else "import './run.ts';\n")
            elif what == "target":
                path.write_text('{"changed": true, "longer": 1}\n')
            elif path.exists():  # the cache: one written from another state of the tree (its keys match nothing)
                data = json.loads(path.read_text())
                for entry in data["entries"].values():
                    entry["key"], entry["specs"] = [1, 1], []
                path.write_text(json.dumps(data))
            return contextlib.nullcontext()
        if not path.exists() and not path.is_symlink():
            return contextlib.nullcontext()
        if state in ("link", "link_out"):
            keep = (project / "skipped" if state == "link" else self.dir / f"out-{project.name}") / f"keep-{path.name}"
            keep.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(keep))
            path.symlink_to(keep)
        elif state == "vanished":
            shutil.rmtree(path) if path.is_dir() else path.unlink()
        elif state in ("folder", "file", "fifo"):
            shutil.rmtree(path) if path.is_dir() else path.unlink()
            path.mkdir() if state == "folder" else os.mkfifo(path) if state == "fifo" else path.write_text("x")
        elif state == "unreadable":
            os.chmod(path, 0)
        return contextlib.nullcontext()

    def sweep(self, what):
        self.skip_if_root()
        for state in self.STATES:
            for route in self.ROUTES:
                label = f"{what}/{state}/{route}"
                with self.subTest(label):
                    project = self.project(label.replace("/", "-")).resolve()
                    write(project / "src" / "uses.ts", "import './later.ts';\nimport '@/app/run.ts';\n")
                    try:
                        self.sweep_one(project, what, state, route, label)
                    finally:
                        self.unlock(project)

    def sweep_one(self, project, what, state, route, label):
        first = self.nav(project)
        if route not in ("cold",) and not route.endswith("_cold"):
            for q in QUESTIONS:
                self.assertIn(self.ask(first, q)["status"], ("ok", "not_found"), (label, "before", q))
        if "_" in route:
            hook, kind = route.split("_")
            asked = first if kind == "warm" else self.nav(project, cache="cold")
            owner, name = self.HOOKS[hook]
            owner = asked if owner is None else owner
            real = getattr(owner, name)
            moved = []

            def moving(*a, **k):
                if not moved:
                    moved.append(self.mutate(project, what, state))
                    moved[0].__enter__()
                return real(*a, **k)
            try:
                with mock.patch.object(owner, name, moving):
                    got = [self.ask(asked, q) for q in QUESTIONS]
                want = self.expected(project)
            finally:
                if moved:
                    moved[0].__exit__(None, None, None)
            for q, g, w in zip(QUESTIONS, got, want):
                self.sound(g, w, (label, q), live=False)
            return
        with self.mutate(project, what, state):
            asked = self.nav(project, cache="cold") if route == "cold" else first if route == "warm" else self.nav(project)
            got = [self.ask(asked, q) for q in QUESTIONS]
            want = self.expected(project)
        for q, g, w in zip(QUESTIONS, got, want):
            self.sound(g, w, (label, q), live=True)

    def test_r10_sweep_source(self):
        self.sweep("source")

    def test_r10_sweep_config(self):
        self.sweep("config")

    def test_r10_sweep_overlay(self):
        self.sweep("overlay")

    def test_r10_sweep_folder(self):
        self.sweep("folder")

    def test_r10_sweep_target(self):
        self.sweep("target")

    def test_r10_sweep_missing_target(self):
        self.sweep("missing_target")

    def test_r10_sweep_nested_folder(self):
        self.sweep("nested_folder")

    def test_r10_sweep_cache(self):
        self.sweep("cache")


    # --- round 12 (review 11 F1, rule first): THE PROOF IS THE LAST STEP BEFORE AN INDEX ANSWER IS RETURNED, after screening.
    # `NavApi._final` is the one place that makes it, for the index and for `deno info`/`deno doc` alike: it screens, then runs the
    # answer's proof, and returns the answer or (when the proof fails) the gaps and nothing else.
    #
    # Route table (every index answer -> the steps between its first proof and its return):
    #   importers             -> _index_answer (build, proof 1) -> _final: _out (cache_refused note, screen) -> proof 2 -> return
    #   imports (index/auto)  -> _index_answer (build, proof 1) -> _final: _out -> proof 2 -> return
    #   imports (auto, deno)  -> _forward_graph (reach walk) -> _final: _out -> reach.holds() -> return
    #   forward_graph/symbols -> _final: _out -> reach.holds() -> return (a cached doc answer too)
    #   status                -> carries no index edge data and makes no `ok` claim about a file: not an index answer
    # Nothing is filtered, paged or cached after proof 1 (`build` pages; `refresh` saves the cache before it).

    def screening_mutation(self, project, mutate, ask, before="ok", **kw):
        """(nav, answer): `ask` twice on one instance; the second time `mutate` runs on the output screen's first call."""
        nav = self.nav(project, **kw)
        self.assertEqual(ask(nav)["status"], before)  # warm
        real, moved = nav.guard, []

        def moving(text):
            if not moved:
                moved.append(1)
                mutate()
            return real(text)
        nav.guard = moving
        answer = ask(nav)
        nav.guard = real
        self.assertEqual(moved, [1])
        return nav, answer

    def test_r11_f1_repro_a_config_changed_during_screening_is_not_ok(self):
        project = self.project().resolve()
        config = project / "deno.json"
        nav, answer = self.screening_mutation(
            project, lambda: config.write_text(config.read_text().replace("./src/real_host.ts", "./src/mock_host.ts")),
            lambda n: n.importers("src/real_host.ts"), overlays=[])
        self.assert_no_data(answer)
        self.assertIn(answer["status"], ("stale_index", "error"), answer)
        self.assertIn("config_changed", answer["gaps"], answer)
        after = nav.importers("src/real_host.ts")  # asked again: the tree's own answer
        self.assertEqual((after["status"], after["importers"]), ("ok", []), after)

    def test_r11_f1_repro_a_source_changed_during_screening_is_not_ok(self):
        project = self.project().resolve()
        main = project / "src" / "main.ts"
        nav, answer = self.screening_mutation(
            project, lambda: main.write_text(main.read_text().replace("import { run } from '@/app/run.ts';\n", "import './lazy.ts';\n")),
            lambda n: n.importers("src/app/run.ts"))
        self.assert_no_data(answer)
        self.assertEqual(answer["status"], "stale_index", answer)
        self.assertIn("source_changed", answer["gaps"], answer)
        self.sound(nav.importers("src/app/run.ts"), self.expected(project)[0], "asked again", live=True)

    def test_r11_f1_imports_through_the_index_is_held_to_the_same_rule(self):
        project = self.project().resolve()
        config = project / "deno.json"
        nav, answer = self.screening_mutation(
            project, lambda: config.write_text(config.read_text().replace("./src/real_host.ts", "./src/mock_host.ts")),
            lambda n: n.imports("src/main.ts", source="index"), overlays=[])
        self.assert_no_data(answer)
        self.assertIn("config_changed", answer["gaps"], answer)

    def test_r11_f1_a_not_found_answer_is_proved_after_screening_as_well(self):
        project = self.project().resolve()
        nav, answer = self.screening_mutation(project, lambda: write(project / "src" / "nothing.ts", "export const n = 1;\n"),
                                              lambda n: n.importers("src/nothing.ts"), before="not_found")
        self.assert_no_data(answer)
        self.assertEqual(answer["status"], "stale_index", answer)
        self.assertEqual(nav.importers("src/nothing.ts")["status"], "ok")

    def test_r11_f1_nothing_runs_after_the_last_proof(self):
        """The order of events of an answered call: the first proof, then the screen's calls, then the proof as the last event."""
        project = self.project().resolve()
        for label, ask in (("importers", lambda n: n.importers("src/app/run.ts")),
                           ("imports", lambda n: n.imports("src/main.ts", source="index")),
                           ("auto", lambda n: n.imports("src/main.ts")),
                           ("not_found", lambda n: n.importers("src/nothing.ts"))):
            with self.subTest(label):
                nav = self.nav(project, cache=f"order-{label}")
                events, real_guard, real_prove = [], nav.guard, graph.ImportIndex.prove

                def guard(text):
                    events.append("screen")
                    return real_guard(text)

                def prove(index):
                    events.append("prove")
                    return real_prove(index)
                nav.guard = guard
                with mock.patch.object(graph.ImportIndex, "prove", prove):
                    answer = ask(nav)
                self.assertIn(answer["status"], ("ok", "not_found"), answer)
                self.assertEqual(events[-1], "prove", events)
                self.assertEqual(events.index("screen"), 1, events)  # the first proof still comes first
                self.assertEqual(events.count("prove"), 2, events)

    def test_r11_f1_one_final_check_for_every_route(self):
        project = self.project().resolve()
        nav = self.nav(project)
        answer = nav_api._answer("ok", target="x")
        self.assertEqual(nav._final(answer, None)["status"], "ok")
        events = []
        out = nav._final(answer, lambda: (events.append("proof"), None)[1])
        self.assertEqual((out["status"], events), ("ok", ["proof"]))
        out = nav._final(answer, lambda: nav_api._answer("stale_index", detail="moved"))
        self.assertEqual(out["status"], "stale_index")
        self.assertNotIn("target", out)

        class Reach:
            holds = staticmethod(lambda: False)
        self.assertEqual(nav._reach_proof(Reach)()["status"], "stale_index")
        self.assertIsNone(nav._reach_proof(None))

    def test_r11_sweep_has_the_screen_dimension(self):
        """The dimension added to the table: 8 inputs x 10 states x 11 routes (`screen_warm` and `screen_cold` are new) = 880 cells."""
        self.assertEqual(len(self.ROUTES), 11)
        self.assertEqual(len(self.STATES) * len(self.ROUTES) * (len(self.NAMES) + 1), 880)
        self.assertEqual({r for r in self.ROUTES if r.startswith("screen")}, {"screen_warm", "screen_cold"})


class MonorepoCase(Tree):
    """Step 2 (lane ctx-nav-api, owner decision "Monorepo-wide, guarded"): a project inside a monorepo may import any file inside the
    monorepo, provided each reached file passes the guard and the boundary check and is mirrored like a project file. The monorepo
    root is the nearest folder with a `.git` that also has a workspace marker; it is the boundary of every read. Only the workspace
    form the launchpad tree uses (`package.json` `workspaces` as a list of plain folder paths) is modelled; any other workspace form
    is `unmodelled_config`. A specifier that resolves outside the monorepo root stays `outside_project`. Paths in and out stay
    relative to the project (`../../lib/x.ts` for a file outside it)."""

    scripted_deno = ReviewFixesCase.scripted_deno
    deno_calls = ReviewFixesCase.deno_calls
    doc_payload = staticmethod(ReviewFixesCase.doc_payload)

    def mono(self, markers=("nx.json",), workspaces=("apps/web", "apps/e2e")):
        """(monorepo root, project): `mono/apps/api` is the fixture, whose config maps `@lib/` to `mono/lib/`."""
        mono = self.dir / "mono"
        write(mono / ".git" / "HEAD", "ref: refs/heads/main\n")
        for name in markers:
            write(mono / name, "{}")
        write(mono / "package.json", json.dumps({"name": "m", "private": True, "workspaces": list(workspaces)}))
        write(mono / "lib" / "shared" / "util.ts", "import { M } from './deep/more.ts';\nexport const U = M + 1;\n")
        write(mono / "lib" / "shared" / "deep" / "more.ts", "export const M = 2;\n")
        project = mono / "apps" / "api"
        shutil.copytree(FIXTURE, project)
        config = json.loads((project / "deno.json").read_text())
        config["imports"]["@lib/"] = "../../lib/"
        write(project / "deno.json", json.dumps(config))
        write(project / "src" / "uses_lib.ts", "import { U } from '@lib/shared/util.ts';\nimport { U as V } from '../../../lib/shared/util.ts';\n"
                                                "export const W = U + V;\n")
        write(self.dir / "outside.ts", "export const OUTSIDE = 1;\n")
        return mono, project

    env_patchers = ()

    def undo_env(self):
        for patcher in reversed(self.env_patchers):
            patcher.stop()
        self.env_patchers = []

    def reset(self):
        self.undo_env()  # an edit that changed HOME is undone with the tree it belonged to
        shutil.rmtree(self.dir / "mono", ignore_errors=True)
        return self.mono()

    def deno_nav(self, project, **kw):
        return self.nav(project, deno=self.scripted_deno(doc={"nodes": {}}, info={"modules": []}), **kw)

    def refusal(self, nav, target="src/uses_lib.ts"):
        out = {}
        for call in (nav.symbols, nav.forward_graph):
            answer = call(target)
            out[call.__name__] = (answer["status"], answer.get("reason"))
        return out

    def assert_refused(self, nav, reason, target="src/uses_lib.ts", label=""):
        self.assertEqual(self.refusal(nav, target), {"symbols": ("refused", reason), "forward_graph": ("refused", reason)}, label)

    def assert_ok(self, nav, target="src/uses_lib.ts", label=""):
        self.assertEqual(self.refusal(nav, target), {"symbols": ("ok", None), "forward_graph": ("ok", None)}, label)

    # --- the boundary

    def test_the_boundary_is_the_monorepo_root_and_status_says_so(self):
        mono, project = self.mono()
        nav = self.deno_nav(project)
        self.assertEqual(nav.boundary, str(mono))
        self.assertEqual(nav.project_root, str(project))
        self.assertEqual(nav.status()["boundary"], "../..")
        self.assertNotIn("boundary", self.deno_nav(self.project("plain")).status())

    def test_every_workspace_marker_makes_a_monorepo_root(self):
        for marker in nav_api.WORKSPACE_MARKERS:
            with self.subTest(marker):
                mono, project = self.reset()
                for name in os.listdir(mono):
                    if name in nav_api.WORKSPACE_MARKERS:
                        (mono / name).unlink()
                write(mono / marker, "{}")
                self.assertEqual(nav_api.NavApi(str(project)).boundary, str(mono))

    def test_where_there_is_no_monorepo_root_the_boundary_is_the_project(self):
        cases = {
            "no .git": lambda mono, project: shutil.rmtree(mono / ".git"),
            "a .git and no marker": lambda mono, project: (mono / "nx.json").unlink(),
            "a nearer .git between, without a marker": lambda mono, project: write(mono / "apps" / ".git" / "HEAD", "x"),
            "the project itself has the .git": lambda mono, project: write(project / ".git" / "HEAD", "x"),
        }
        for label, change in cases.items():
            with self.subTest(label):
                mono, project = self.reset()
                change(mono, project)
                self.assertEqual(nav_api.NavApi(str(project)).boundary, str(project))

    def test_a_monorepo_root_that_holds_the_home_folder_is_not_taken(self):
        mono, project = self.mono()
        for home, boundary in ((mono / "apps", project), (mono, project), (self.dir / "elsewhere", mono)):
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                self.assertEqual(nav_api.NavApi(str(project)).boundary, str(boundary), str(home))

    def test_a_monorepo_root_with_a_sensitive_name_is_not_taken(self):
        mono = self.dir / ".ssh" / "m"
        write(mono / ".git" / "HEAD", "x")
        write(mono / "nx.json", "{}")
        project = mono / "app"
        shutil.copytree(FIXTURE, project)
        self.assertEqual(nav_api.NavApi(str(project)).boundary, str(project))

    def test_the_guard_is_asked_from_the_boundary_and_names_files_from_it(self):
        mono, project = self.mono()
        asked = []

        def guard(root, rel):
            asked.append((root, rel))
            return nav_api.stub_check_path(root, rel)
        nav = self.nav(project, check_path=guard)
        nav.imports("src/uses_lib.ts", source="index")
        self.assertEqual({root for root, _ in asked}, {str(mono)})
        names = [rel for _, rel in asked]
        self.assertIn("apps/api/src/uses_lib.ts", names)
        self.assertIn("lib/shared/util.ts", names)

    def test_a_guard_that_refuses_a_file_of_the_monorepo_by_name_is_obeyed_on_every_route(self):
        mono, project = self.mono()

        def guard(root, rel):
            if rel.startswith("lib/shared/deep"):
                raise nav_api.Refused("sensitive_path")
            return nav_api.stub_check_path(root, rel)
        nav = self.nav(project, check_path=guard, deno=self.scripted_deno(doc={"nodes": {}}, info={"modules": []}))
        self.assert_refused(nav, "reaches_refused_path")
        self.assertEqual(self.deno_calls("doc") + self.deno_calls("info"), [])
        self.assertEqual(nav.importers("../../lib/shared/deep/more.ts")["status"], "refused")

    # --- the index

    def test_imports_into_the_monorepo_are_edges_with_project_relative_names(self):
        mono, project = self.mono()
        answer = self.nav(project).imports("src/uses_lib.ts", source="index")
        self.assertEqual(answer["status"], "ok", answer)
        self.assertEqual([(f["path"], f["line"]) for f in answer["files"]],
                         [("../../lib/shared/util.ts", 1), ("../../lib/shared/util.ts", 2)])
        self.assertEqual((answer["unresolved"], answer["external"], answer["target"]), ([], [], "src/uses_lib.ts"))

    def test_importers_of_a_file_outside_the_project_are_the_project_files_that_import_it(self):
        mono, project = self.mono()
        nav = self.nav(project)
        for target in ("../../lib/shared/util.ts", str(mono / "lib" / "shared" / "util.ts"), "src/../../../lib/shared/util.ts"):
            answer = nav.importers(target)
            self.assertEqual(answer["status"], "ok", (target, answer))
            self.assertEqual(answer["target"], "../../lib/shared/util.ts")
            self.assertEqual([(e["path"], e["line"]) for e in answer["importers"]], [("src/uses_lib.ts", 1), ("src/uses_lib.ts", 2)])

    def test_a_specifier_beyond_the_monorepo_root_is_still_outside_the_project_and_nothing_there_is_looked_at(self):
        mono, project = self.mono()
        trap = self.dir / "trap"
        write(trap / "x.ts", "export const T = 1;\n")
        config = json.loads((project / "deno.json").read_text())
        config["imports"]["@out/"] = "../../../trap/"
        write(project / "deno.json", json.dumps(config))
        write(project / "src" / "up.ts", "import '../../../../outside.ts';\nimport '@out/x.ts';\nimport '../../../../trap/x.ts';\n")
        nav = self.deno_nav(project)
        answer = nav.imports("src/up.ts", source="index")
        self.assertEqual(answer["status"], "ok", answer)
        self.assertEqual(answer["files"], [])
        self.assertEqual([u["reason"] for u in answer["unresolved"]], ["outside"] * 3)
        self.assert_refused(nav, "outside_project", "src/up.ts")
        self.assertEqual(self.deno_calls("doc") + self.deno_calls("info"), [])

    def test_a_file_of_the_monorepo_the_guard_refuses_is_not_named_in_the_index_either(self):
        mono, project = self.mono()
        write(mono / "lib" / ".env.ts", "export const E = 1;\n")
        write(project / "src" / "bad.ts", "import '../../../lib/.env.ts';\n")
        answer = self.nav(project).imports("src/bad.ts", source="index")
        self.assertEqual(answer["status"], "ok", answer)
        self.assertEqual([u["reason"] for u in answer["unresolved"]], ["refused"])
        self.assertNotIn(".env", json.dumps(answer))

    def test_links_in_the_monorepo_are_judged_by_where_they_lead(self):
        mono, project = self.mono()
        write(mono / "lib" / ".env.ts", "export const E = 1;\n")
        (mono / "lib" / "to_outside.ts").symlink_to(self.dir / "outside.ts")
        (mono / "lib" / "to_env.ts").symlink_to(mono / "lib" / ".env.ts")
        (mono / "lib" / "to_util.ts").symlink_to(mono / "lib" / "shared" / "util.ts")
        for name in ("to_outside", "to_env", "to_util"):
            write(project / "src" / f"via_{name}.ts", f"import '../../../lib/{name}.ts';\n")
        nav = self.deno_nav(project)
        self.assert_refused(nav, "reaches_refused_path", "src/via_to_outside.ts")
        self.assert_refused(nav, "reaches_refused_path", "src/via_to_env.ts")
        self.assert_ok(nav, "src/via_to_util.ts")

    def test_the_index_routes_agree_cold_warm_and_loaded_from_the_cache(self):
        mono, project = self.mono()
        cache = str(self.dir / "cache")
        cold = nav_api.NavApi(str(project), cache_dir=cache)
        first = cold.importers("../../lib/shared/util.ts")
        warm = cold.importers("../../lib/shared/util.ts")
        loaded = nav_api.NavApi(str(project), cache_dir=cache).importers("../../lib/shared/util.ts")
        self.assertEqual(first["status"], "ok", first)
        volatile = ("index_age_s", "scanned", "cache")
        for label, answer in (("warm", warm), ("loaded", loaded)):
            self.assertEqual({k: v for k, v in answer.items() if k not in volatile},
                             {k: v for k, v in first.items() if k not in volatile}, label)

    # --- the path arguments

    def test_paths_asked_about_may_go_up_to_the_monorepo_root_and_no_further(self):
        mono, project = self.mono()
        write(mono / "lib" / ".env.ts", "export const E = 1;\n")
        nav = self.deno_nav(project)
        cases = {"../../../outside.ts": "outside_project", str(self.dir / "outside.ts"): "outside_project", "../..": "project_root",
                 "../../..": "outside_project", ".": "project_root", "../../.git/HEAD": "sensitive_path",
                 "../../lib/.env.ts": "sensitive_path"}
        for path, reason in cases.items():
            for call in (nav.importers, nav.symbols, nav.forward_graph, nav.imports):
                with self.subTest(path, call=call.__name__):
                    answer = call(path)
                    self.assertEqual((answer["status"], answer.get("reason")), ("refused", reason), answer)
        self.assertEqual(self.deno_calls("doc") + self.deno_calls("info"), [])

    # --- Deno: the walk, the mirror, the answers

    def info_payload(self):
        """Deno runs in the config's folder of the mirror (`mirror/apps/api`), so the monorepo's lib is two folders up from MIRROR."""
        return {"modules": [
            {"specifier": f"file://{MIRROR}/src/uses_lib.ts", "dependencies": [
                {"specifier": "@lib/shared/util.ts", "code": {"specifier": f"file://{MIRROR}/../../lib/shared/util.ts"}}]},
            {"specifier": f"file://{MIRROR}/../../lib/shared/util.ts", "dependencies": [
                {"specifier": "./deep/more.ts", "code": {"specifier": f"file://{MIRROR}/../../lib/shared/deep/more.ts"}}]},
            {"specifier": f"file://{MIRROR}/../../lib/shared/deep/more.ts", "dependencies": []}]}

    def test_forward_graph_follows_an_import_into_the_monorepo(self):
        mono, project = self.mono()
        nav = self.nav(project, deno=self.scripted_deno(doc={"nodes": {}}, info=self.info_payload()))
        answer = nav.forward_graph("src/uses_lib.ts")
        self.assertEqual(answer["status"], "ok", answer)
        self.assertEqual({m["path"]: m["imports"] for m in answer["modules"]},
                         {"src/uses_lib.ts": ["../../lib/shared/util.ts"], "../../lib/shared/util.ts": ["../../lib/shared/deep/more.ts"],
                          "../../lib/shared/deep/more.ts": []})
        self.assertEqual(answer["target"], "src/uses_lib.ts")

    def test_deno_is_given_a_mirror_of_the_monorepo_layout_holding_only_what_was_reached(self):
        mono, project = self.mono()
        write(mono / "lib" / "shared" / "unreached.ts", "export const NOPE = 1;\n")
        write(mono / "apps" / "package.json", json.dumps({"name": "apps", "workspaces": ["api"], "scripts": {"x": "y"}}))
        reach = graph.prove_reach(str(mono), str(project / "src" / "uses_lib.ts"), str(project / "deno.json"), lambda rel: True)
        self.assertEqual(sorted(reach.files), ["apps/api/src/uses_lib.ts", "lib/shared/deep/more.ts", "lib/shared/util.ts"])
        mirror = reach.materialize(str(self.dir))
        self.addCleanup(mirror.cleanup)
        root = Path(mirror.root)
        have = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
        self.assertEqual(have, ["apps/api/deno.json", "apps/api/package.json", "apps/api/src/uses_lib.ts", "apps/package.json", "lib/shared/deep/more.ts",
                                "lib/shared/util.ts", "package.json"])
        self.assertNotIn("workspaces", json.loads((root / "package.json").read_text()))
        self.assertEqual(json.loads((root / "apps" / "package.json").read_text()), {"name": "apps"})

    def test_a_mirror_made_inside_the_monorepo_is_refused(self):
        mono, project = self.mono()
        (mono / "scratch").mkdir()
        nav = self.deno_nav(project, mirror_parent=str(mono / "scratch"))
        for call in (nav.symbols, nav.forward_graph):
            answer = call("src/uses_lib.ts")
            self.assertEqual((answer["status"], answer.get("reason")), ("refused", "scratch_refused"), answer)

    def test_an_import_of_a_refused_file_of_the_monorepo_refuses_the_walk_and_deno_is_not_started(self):
        mono, project = self.mono()
        write(mono / "lib" / ".env.ts", "export const E = 1;\n")
        write(mono / "lib" / "shared" / "viaenv.ts", "import '../.env.ts';\n")
        write(project / "src" / "bad1.ts", "import '../../../lib/.env.ts';\n")
        write(project / "src" / "bad2.ts", "import '@lib/shared/viaenv.ts';\n")
        write(project / "src" / "bad3.ts", "import 'file://" + str(mono / "lib" / ".env.ts") + "';\n")
        nav = self.deno_nav(project)
        for name in ("bad1", "bad2", "bad3"):
            self.assert_refused(nav, "reaches_refused_path", f"src/{name}.ts", name)
        self.assertEqual(self.deno_calls("doc") + self.deno_calls("info"), [])

    def test_an_absolute_import_inside_the_monorepo_is_followed_and_one_outside_is_not(self):
        mono, project = self.mono()
        write(project / "src" / "abs_in.ts", f"import 'file://{mono}/lib/shared/util.ts';\n")
        write(project / "src" / "abs_out.ts", f"import 'file://{self.dir}/outside.ts';\n")
        nav = self.deno_nav(project)
        self.assert_ok(nav, "src/abs_in.ts")
        self.assert_refused(nav, "outside_project", "src/abs_out.ts")

    def test_a_file_the_walk_cannot_read_in_the_monorepo_refuses_with_unreadable_file(self):
        if os.geteuid() == 0:
            self.skipTest("root can read a file with mode 0")
        mono, project = self.mono()
        victim = mono / "lib" / "shared" / "deep" / "more.ts"
        os.chmod(victim, 0)
        self.addCleanup(os.chmod, victim, 0o644)
        self.assert_refused(self.deno_nav(project), "unreadable_file")

    def test_a_reached_file_that_changes_during_deno_withholds_the_answer(self):
        edits = {"edited": lambda v: v.write_text("export const M = 3;\n"),
                 "removed": lambda v: v.unlink(),
                 "replaced by a link to outside": lambda v: (v.unlink(), v.symlink_to(self.dir / "outside.ts"))}
        for label, edit in edits.items():
            for call in ("symbols", "forward_graph"):
                with self.subTest(label, call=call):
                    mono, project = self.reset()
                    nav = self.deno_nav(project)
                    victim = mono / "lib" / "shared" / "deep" / "more.ts"
                    real = nav._run_deno
                    nav._run_deno = lambda *a, _real=real, _edit=edit, _v=victim, **k: (_edit(_v), _real(*a, **k))[1]
                    answer = getattr(nav, call)("src/uses_lib.ts")
                    self.assertEqual(answer["status"], "stale_index", answer)

    @NEEDS_SANDBOX
    def test_the_real_deno_answers_through_the_sandbox_for_a_file_in_the_monorepo(self):
        mono, project = self.mono()
        with mock.patch.object(nav_api, "SANDBOX_PROFILE", SANDBOX_PRODUCTION):
            nav = self.nav(project)
            answer = nav.forward_graph("src/uses_lib.ts")
        self.assertEqual(answer["status"], "ok", answer)
        self.assertIn("../../lib/shared/util.ts", {m["path"] for m in answer["modules"]})

    # --- workspaces: the one modelled form, and everything else refused

    def with_root_package(self, workspaces):
        mono, project = self.reset()
        write(mono / "package.json", json.dumps({"name": "m", "workspaces": workspaces}))
        return mono, project

    def test_the_plain_path_list_form_is_modelled(self):
        for workspaces in (["apps/web", "apps/e2e"], ["./apps/web/"], ["apps/web", "apps/web"]):
            with self.subTest(workspaces):
                mono, project = self.with_root_package(workspaces)
                self.assert_ok(self.deno_nav(project))

    def test_any_other_workspace_form_is_unmodelled_config(self):
        forms = {"an object with packages": {"packages": ["apps/web"]}, "a glob": ["apps/*"], "a double star": ["apps/**"],
                 "a negation": ["apps/web", "!apps/web/x"], "a brace": ["apps/{a,b}"], "a character class": ["apps/[ab]"],
                 "a question mark": ["apps/w?b"], "a parent": ["../other"], "a parent in the middle": ["apps/web/../x"],
                 "an absolute path": ["/etc"], "a number": [1], "null in the list": [None], "a string": "apps/web", "a number alone": 5,
                 "the root itself": ["."], "blank": ["  "], "a backslash": ["apps\\web"], "a guard-refused name": ["apps/.env-app"],
                 "a nested list": [["apps/web"]], "true": True, "an empty object": {"x": 1},
                 "null": None, "false": False, "zero": 0, "an empty string": "", "an empty list": [], "an empty dict": {},
                 "a mixed list": ["apps/web", 1], "a list with an empty string": ["apps/web", ""], "a list with a dict": ["apps/web", {}]}
        for label, workspaces in forms.items():
            with self.subTest(label):
                mono, project = self.with_root_package(workspaces)
                self.assert_refused(self.deno_nav(project), "unmodelled_config", label=label)

    def test_a_workspaces_key_that_is_present_is_never_read_as_absent(self):
        """Review 1 F1: `null`, `false`, `0`, `""`, `[]` and `{}` are falsy but present; each is refused in the root, in a folder
        between and in the project folder, from the Deno route and from `check_workspaces` itself; an absent key is no workspace."""
        for folder in ("", "apps", "apps/api"):
            for label, value in (("null", None), ("false", False), ("zero", 0), ("empty string", ""), ("empty list", []), ("empty dict", {})):
                with self.subTest(f"{folder}:{label}"):
                    mono, project = self.reset()
                    write(mono / folder / "package.json", json.dumps({"name": "x", "workspaces": value}))
                    self.assert_refused(self.deno_nav(project), "unmodelled_config", label=label)
                    with self.assertRaises(graph.Unproven):
                        graph.check_workspaces(str(mono), str(mono / folder), value, lambda rel: True)
            mono, project = self.reset()
            write(mono / folder / "package.json", json.dumps({"name": "x"}))
            self.assert_ok(self.deno_nav(project), label=f"absent in {folder!r}")

    def test_a_workspaces_in_a_package_json_between_the_project_and_the_root_follows_the_same_rule(self):
        mono, project = self.mono()
        write(mono / "apps" / "package.json", json.dumps({"name": "apps", "workspaces": ["api"]}))
        self.assert_ok(self.deno_nav(project))
        write(mono / "apps" / "package.json", json.dumps({"name": "apps", "workspaces": ["*"]}))
        self.assert_refused(self.deno_nav(project), "unmodelled_config")
        write(mono / "apps" / "package.json", json.dumps({"name": "apps", "workspaces": ["../lib"]}))
        self.assert_refused(self.deno_nav(project), "unmodelled_config")

    def test_a_workspace_file_of_another_tool_is_unmodelled_config_wherever_deno_looks(self):
        for name in graph.UNMODELLED_WORKSPACE_FILES:
            for folder in ("", "apps", "apps/api"):
                with self.subTest(name, folder=folder):
                    mono, project = self.mono(markers=("nx.json", "turbo.json"))
                    self.assert_ok(self.deno_nav(project))
                    write(mono / folder / name, "packages:\n  - 'apps/*'\n")
                    self.assert_refused(self.deno_nav(project), "unmodelled_config")
                    shutil.rmtree(self.dir / "mono")

    def test_a_package_json_that_is_not_modelled_is_refused_wherever_deno_looks(self):
        bad = {"not json": "{", "not an object": "[]", "a local dependency": json.dumps({"dependencies": {"x": "file:../x"}}),
               "a workspace dependency": json.dumps({"devDependencies": {"x": "workspace:*"}})}
        for label, text in bad.items():
            for folder in ("", "apps", "apps/api"):
                with self.subTest(label, folder=folder):
                    mono, project = self.reset()
                    write(mono / folder / "package.json", text)
                    self.assert_refused(self.deno_nav(project), "unmodelled_config")

    def test_a_workspace_config_in_each_unusable_state_refuses_with_its_reason(self):
        if os.geteuid() == 0:
            self.skipTest("root can read a file with mode 0")

        def link(mono, target):
            (mono / "package.json").unlink()
            (mono / "package.json").symlink_to(target)

        def unreadable(mono):
            os.chmod(mono / "package.json", 0)
            self.addCleanup(os.chmod, mono / "package.json", 0o644)

        def a_folder(mono):
            (mono / "package.json").unlink()
            (mono / "package.json").mkdir()

        def to_refused(mono):
            write(mono / ".env.json", "{}")
            link(mono, mono / ".env.json")

        def to_inside(mono):
            write(mono / "real.json", '{"name":"m"}')
            link(mono, mono / "real.json")
        cases = {"unreadable": (unreadable, "unreadable_file"), "a folder": (a_folder, "unreadable_file"),
                 "a link out": (lambda mono: link(mono, self.dir / "outside.ts"), "reaches_refused_path"),
                 "a link to a refused file": (to_refused, "reaches_refused_path"), "a link to a file inside": (to_inside, None),
                 "pnpm-workspace.yaml a folder": (lambda mono: (mono / "pnpm-workspace.yaml").mkdir(), "unreadable_file"),
                 "lerna.json a link out": (lambda mono: (mono / "lerna.json").symlink_to(self.dir / "outside.ts"), "reaches_refused_path")}
        for label, (change, reason) in cases.items():
            with self.subTest(label):
                mono, project = self.reset()
                change(mono)
                nav = self.deno_nav(project)
                if reason is None:
                    self.assert_ok(nav)
                else:
                    self.assert_refused(nav, reason)

    def test_the_workspace_checks_look_at_nothing_outside_the_monorepo(self):
        mono, project = self.mono()
        write(self.dir / "trap_pkg.json", '{"workspaces": ["apps/web"]}')
        (mono / "package.json").unlink()
        (mono / "package.json").symlink_to(self.dir / "trap_pkg.json")
        tracer = FsTracer(self)
        nav = self.deno_nav(project)
        self.assert_refused(nav, "reaches_refused_path")
        tracer.on = False
        self.assertEqual(tracer.touched(self.dir / "trap_pkg.json"), [])

    # --- workspace configs are inputs: a change after the walk is a stale answer

    def test_a_workspace_config_changing_during_the_call_withholds_the_answer(self):
        edits = {
            "the workspaces list edited": lambda mono: write(mono / "package.json", json.dumps({"name": "m", "workspaces": ["apps/web"]})),
            "the workspaces list made a glob": lambda mono: write(mono / "package.json", json.dumps({"name": "m", "workspaces": ["apps/*"]})),
            "the root package.json removed": lambda mono: (mono / "package.json").unlink(),
            "an intermediate package.json appearing": lambda mono: write(mono / "apps" / "package.json", '{"name":"late"}'),
            "pnpm-workspace.yaml appearing": lambda mono: write(mono / "pnpm-workspace.yaml", "packages: []\n"),
            "lerna.json appearing in the project folder": lambda mono: write(mono / "apps" / "api" / "lerna.json", "{}"),
        }
        for label, edit in edits.items():
            for call in ("symbols", "forward_graph"):
                with self.subTest(label, call=call):
                    mono, project = self.reset()
                    nav = self.deno_nav(project)
                    real = nav._run_deno
                    nav._run_deno = lambda *a, _real=real, _edit=edit, _mono=mono, **k: (_edit(_mono), _real(*a, **k))[1]
                    answer = getattr(nav, call)("src/uses_lib.ts")
                    self.assertEqual(answer["status"], "stale_index", answer)

    def test_reach_holds_follows_every_config_file_of_the_chain(self):
        edits = {"root package.json": lambda mono: write(mono / "package.json", '{"name":"q"}'),
                 "apps package.json appears": lambda mono: write(mono / "apps" / "package.json", "{}"),
                 "lerna.json appears at the root": lambda mono: write(mono / "lerna.json", "{}"),
                 "rush.json appears in apps": lambda mono: write(mono / "apps" / "rush.json", "{}"),
                 "the config changes": lambda mono: write(mono / "apps" / "api" / "deno.json", "{}")}
        for label, edit in edits.items():
            with self.subTest(label):
                mono, project = self.reset()
                reach = graph.prove_reach(str(mono), str(project / "src" / "uses_lib.ts"), str(project / "deno.json"), lambda rel: True)
                self.assertTrue(reach.holds())
                edit(mono)
                self.assertFalse(reach.holds())

    # --- sweep passes: states of the new inputs on the routes the first tests did not take

    def test_a_git_file_and_a_marker_that_is_a_dangling_link_still_make_a_monorepo_root(self):
        mono, project = self.mono()
        shutil.rmtree(mono / ".git")
        write(mono / ".git", "gitdir: /elsewhere\n")  # a worktree: `.git` is a file
        (mono / "nx.json").unlink()
        (mono / "turbo.json").symlink_to(self.dir / "nowhere.json")  # existence only: nothing is read through the link
        self.assertEqual(nav_api.NavApi(str(project)).boundary, str(mono))

    def test_a_doc_cache_hit_does_not_skip_the_workspace_check(self):
        mono, project = self.mono()
        doc = self.doc_payload(MIRROR, "src/uses_lib.ts", "W", "w", line=3)
        cache = str(self.dir / "cache")
        nav = self.nav(project, deno=self.scripted_deno(doc=doc, info=self.info_payload()), cache_dir=cache)
        first = nav.symbols("src/uses_lib.ts")
        self.assertEqual(first["status"], "ok", first)
        write(mono / "package.json", json.dumps({"name": "m", "workspaces": ["apps/*"]}))
        for fresh in (nav, self.nav(project, deno=self.scripted_deno(doc=doc, info=self.info_payload()), cache_dir=cache)):
            answer = fresh.symbols("src/uses_lib.ts")
            self.assertEqual((answer["status"], answer.get("reason")), ("refused", "unmodelled_config"), answer)
            self.assertNotIn("symbols", answer)

    def test_every_config_file_of_the_chain_in_every_unusable_state(self):
        """Sweep: package.json and the three workspace files of other tools, in the root, the folder between and the project, each
        unreadable, a folder, a link out and a link to a refused file; the reason is the same wherever Deno looks."""
        if os.geteuid() == 0:
            self.skipTest("root can read a file with mode 0")
        states = {"unreadable": ("unreadable_file", lambda f, m: (write(f, "{}"), os.chmod(f, 0))),
                  "a folder": ("unreadable_file", lambda f, m: f.mkdir()),
                  "a link out": ("reaches_refused_path", lambda f, m: f.symlink_to(self.dir / "outside.ts")),
                  "a link to a refused file": ("reaches_refused_path", lambda f, m: (write(m / ".env.json", "{}"), f.symlink_to(m / ".env.json")))}
        for name in ("package.json", *graph.UNMODELLED_WORKSPACE_FILES):
            for folder in ("", "apps", "apps/api"):
                for label, (reason, make) in states.items():
                    with self.subTest(name, folder=folder, state=label):
                        mono, project = self.reset()
                        target = mono / folder / name
                        if target.exists() or target.is_symlink():
                            target.unlink()
                        make(target, mono)
                        self.addCleanup(lambda t=target: os.path.lexists(t) and not t.is_symlink() and t.is_file() and os.chmod(t, 0o644))
                        self.assert_refused(self.deno_nav(project), reason)

    def test_an_io_error_on_the_index_route_inside_a_monorepo_is_never_an_ok_answer(self):
        if os.geteuid() == 0:
            self.skipTest("root can read a file with mode 0")
        mono, project = self.mono()
        write(project / "src" / "sub" / "deep.ts", "export const D = 1;\n")
        for label, path, mode in (("unreadable source", project / "src" / "uses_lib.ts", 0), ("unreadable folder", project / "src" / "sub", 0),
                                  ("unreadable config", project / "deno.json", 0)):
            with self.subTest(label):
                real_mode = stat.S_IMODE(os.stat(path).st_mode)
                os.chmod(path, mode)
                self.addCleanup(os.chmod, path, real_mode)
                for call in (lambda n: n.importers("../../lib/shared/util.ts"), lambda n: n.imports("src/uses_lib.ts", source="index")):
                    answer = call(self.nav(project))
                    self.assertNotEqual(answer["status"], "ok", answer)
                    self.assertNotIn("importers", answer)
                    self.assertNotIn("files", answer)
                os.chmod(path, real_mode)

    def test_odd_targets_in_the_monorepo_on_the_index_route(self):
        mono, project = self.mono()
        nav = self.nav(project)
        write(mono / "lib" / ".env.ts", "export const E = 1;\n")
        (mono / "lib" / "dir.ts").mkdir()
        (mono / "lib" / "out.ts").symlink_to(self.dir / "outside.ts")
        cases = {"../../lib/shared": "not_found", "../../lib/dir.ts": "not_found", "../../lib/nope.ts": "not_found",
                 "../../lib/out.ts": "refused", "../../lib/.env.ts": "refused"}
        for path, status in cases.items():
            for call in (nav.importers, nav.imports):
                with self.subTest(path, call=call.__name__):
                    self.assertEqual(call(path)["status"], status, call(path))

    # --- review 1 F2: the boundary decision is an input of every answer

    def boundary_edits(self):
        """Every input of the boundary decision (the monorepo `.git`, each marker, a nearer `.git`, the home folder) x a state
        (removed, replaced by a folder or a link, changed, added). Each is a function (mono, project) that changes the tree."""
        def replace(path, make):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            elif os.path.lexists(path):
                path.unlink()
            make(path)

        def home(mono, project):
            patcher = mock.patch.dict(os.environ, {"HOME": str(mono)})
            patcher.start()
            if not self.env_patchers:
                self.addCleanup(self.undo_env)
            self.env_patchers = [*self.env_patchers, patcher]
        return {
            ".git removed": lambda mono, project: shutil.rmtree(mono / ".git"),
            ".git replaced by a file": lambda mono, project: replace(mono / ".git", lambda p: write(p, "gitdir: /elsewhere\n")),
            ".git replaced by a link": lambda mono, project: replace(mono / ".git", lambda p: p.symlink_to(self.dir / "outside.ts")),
            "nx.json removed": lambda mono, project: (mono / "nx.json").unlink(),
            "nx.json replaced by a folder": lambda mono, project: replace(mono / "nx.json", lambda p: p.mkdir()),
            "nx.json replaced by a link": lambda mono, project: replace(mono / "nx.json", lambda p: p.symlink_to(self.dir / "outside.ts")),
            "nx.json changed": lambda mono, project: write(mono / "nx.json", '{"changed": "yes, and longer"}'),
            "another marker added": lambda mono, project: write(mono / "turbo.json", "{}"),
            "a nearer .git between": lambda mono, project: write(mono / "apps" / ".git" / "HEAD", "x"),
            "a .git in the project": lambda mono, project: write(project / ".git" / "HEAD", "x"),
            "the home folder becomes the root": home,
        }

    def not_a_monorepo_edits(self):
        """The same, for a project that was not in a monorepo when the instance was made (its boundary is the project)."""
        return {
            "the marker appears": lambda mono, project: write(mono / "nx.json", "{}"),
            "the .git appears": lambda mono, project: write(mono / ".git" / "HEAD", "x"),
            "the .git appears as a file": lambda mono, project: write(mono / ".git", "gitdir: /elsewhere\n"),
        }

    def no_data(self, answer, label):
        if "home folder" in label and answer.get("reason") == "sandbox_grant_overlap":
            pass  # Deno's cache sits under the new HOME, inside the new "root": the grants refuse it first; still no data
        else:
            self.assertEqual(answer["status"], "stale_index", (label, answer))
        for key in DATA_KEYS + ("modules", "symbols"):
            self.assertNotIn(key, answer, (label, answer))

    index_calls = (lambda n: n.importers("../../lib/shared/util.ts"), lambda n: n.imports("src/uses_lib.ts", source="index"))
    deno_calls_ = (lambda n: n.symbols("src/uses_lib.ts"), lambda n: n.forward_graph("src/uses_lib.ts"))

    def during(self, nav, hook, edit):
        """Runs `edit()` once, at `hook`: after the index refresh, inside the Deno run, or after the output screen (so after
        everything but the last proof)."""
        done = []

        def once(real):
            def wrapper(*a, **k):
                out = real(*a, **k)
                if not done:
                    done.append(1)
                    edit()
                return out
            return wrapper
        if hook == "refresh":
            index = nav._get_index()
            index.refresh = once(index.refresh)
        elif hook == "deno":
            nav._run_deno = once(nav._run_deno)
        else:
            nav._out = once(nav._out)

    def test_a_change_to_the_boundary_decision_during_a_call_withholds_the_answer(self):
        """Sweep: input x state x route (index: after the refresh, after the screen; Deno: inside the run, after the screen)."""
        routes = [("index", "refresh", self.index_calls), ("index", "screen", self.index_calls),
                  ("deno", "deno", self.deno_calls_), ("deno", "screen", self.deno_calls_)]
        for label, edit in self.boundary_edits().items():
            for kind, hook, calls in routes:
                for number, call in enumerate(calls):
                    with self.subTest(label, route=kind, hook=hook, call=number):
                        mono, project = self.reset()
                        nav = self.deno_nav(project)
                        self.during(nav, hook, lambda: edit(mono, project))
                        self.no_data(call(nav), label)

    def test_a_change_to_the_boundary_decision_between_calls_withholds_the_answer(self):
        """Warm instance (index kept, Deno answer cached) and an instance made before the change that loads the cache."""
        for label, edit in self.boundary_edits().items():
            with self.subTest(label):
                mono, project = self.reset()
                cache = str(self.dir / "cache")
                doc = self.doc_payload(MIRROR, "src/uses_lib.ts", "W", "w", line=3)

                def make():
                    return self.nav(project, deno=self.scripted_deno(doc=doc, info=self.info_payload()), cache_dir=cache)
                warm, cold = make(), make()
                for call in (*self.index_calls, *self.deno_calls_):
                    self.assertEqual(call(warm)["status"], "ok")
                edit(mono, project)
                for number, call in enumerate((*self.index_calls, *self.deno_calls_)):
                    self.no_data(call(warm), f"{label} warm {number}")
                    self.no_data(call(cold), f"{label} cache-loaded {number}")

    def test_a_project_that_became_part_of_a_monorepo_during_a_call_withholds_the_answer(self):
        for label, edit in self.not_a_monorepo_edits().items():
            for kind, hook, calls in (("index", "refresh", self.index_calls), ("index", "screen", self.index_calls),
                                      ("deno", "deno", self.deno_calls_), ("deno", "screen", self.deno_calls_)):
                for number, call in enumerate(calls):
                    with self.subTest(label, route=kind, hook=hook, call=number):
                        mono, project = self.reset()
                        keep = ".git" if "marker" in label else "nx.json"  # the one input already there; the edit adds the other
                        gone = mono / ("nx.json" if keep == ".git" else ".git")
                        shutil.rmtree(gone) if gone.is_dir() else gone.unlink()
                        nav = self.deno_nav(project)
                        self.assertEqual(nav.boundary, str(project))
                        self.during(nav, hook, lambda: edit(mono, project))
                        answer = call(nav)
                        self.assertNotEqual(answer["status"], "ok", (label, answer))
                        for key in DATA_KEYS + ("modules", "symbols"):
                            self.assertNotIn(key, answer, (label, answer))

    def test_reach_and_the_index_hold_only_while_the_boundary_decision_holds(self):
        for label, edit in self.boundary_edits().items():
            with self.subTest(label):
                mono, project = self.reset()
                nav = self.deno_nav(project)
                reach, refusal = nav._prove("src/uses_lib.ts")
                self.assertIsNone(refusal)
                nav._fresh()
                self.assertTrue(reach.holds())
                self.assertEqual(nav._get_index().prove(), {})
                edit(mono, project)
                self.assertFalse(reach.holds())
                self.assertNotEqual(nav._get_index().prove(), {})

    def test_an_unchanged_boundary_decision_is_not_a_change(self):
        mono, project = self.mono()
        nav = self.deno_nav(project)
        for _ in range(3):
            self.assertEqual(nav.importers("../../lib/shared/util.ts")["status"], "ok")
            self.assertEqual(nav.symbols("src/uses_lib.ts")["status"], "ok")
        write(mono / "lib" / "other.ts", "export const O = 1;\n")  # not an input of the decision
        self.assertTrue(nav._boundary_holds())

    def test_reach_holds_follows_the_package_json_chain_in_every_state(self):
        """Sweep pass 2: each package.json Deno looks at (root, between, project), present or absent when proved, then removed,
        replaced by a folder or a link, made unreadable, or made a different file."""
        if os.geteuid() == 0:
            self.skipTest("root can read a file with mode 0")

        def unreadable(f):
            write(f, "{}")
            os.chmod(f, 0)
        edits = {"removed": lambda f: f.unlink(), "a folder": lambda f: (f.unlink(), f.mkdir()),
                 "a link": lambda f: (f.unlink(), f.symlink_to(self.dir / "outside.ts")),
                 "unreadable": lambda f: (f.unlink(), unreadable(f)),
                 "another file": lambda f: write(f, '{"name": "another, longer"}')}
        for folder in ("", "apps", "apps/api"):
            for present in (True, False):
                for label, edit in edits.items():
                    if not present and label in ("removed", "another file"):
                        continue
                    with self.subTest(folder, present=present, state=label):
                        mono, project = self.reset()
                        target = mono / folder / "package.json"
                        if present:
                            write(target, '{"name": "x"}')
                        elif target.exists():
                            target.unlink()
                        reach = graph.prove_reach(str(mono), str(project / "src" / "uses_lib.ts"), str(project / "deno.json"),
                                                  lambda rel: True)
                        reach.boundary_holds = None
                        self.assertTrue(reach.holds())
                        if not present:
                            write(target, '{"name": "x"}')
                        edit(target)
                        self.addCleanup(lambda t=target: os.path.lexists(t) and t.is_file() and not t.is_symlink() and os.chmod(t, 0o644))
                        self.assertFalse(reach.holds())

    # --- documentation

    def test_the_known_limits_say_what_the_final_proof_does_not_cover(self):
        text = " ".join(nav_api.__doc__.split()).lower()
        self.assertIn("a change after the final proof returns is not detected", text)
        self.assertIn("same mtime and size", text)


if __name__ == "__main__":
    unittest.main()
