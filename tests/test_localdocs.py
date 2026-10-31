"""lib/localdocs: lockfile pins, allowlisted reads, api_signature, the docs index, call logging. No network, no models."""
import ast
import io
import json
import os
import re
import shutil
import socket
import sys
import tarfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "localdocs"))
from project import FIXTURES, ROOT, ProjectCase, make_project  # noqa: E402

from localdocs import calls, fetch, guard, index, lock, log, signature  # noqa: E402

PKG_ONLY = ("plainpkg",)


def sig(project, package, symbol, **kw):
    return signature.api_signature(project, package, symbol, **kw)


class LockTests(ProjectCase):
    def test_package_lock_pin_is_the_root_entry(self):
        pin = lock.pin(self.project, "plainpkg")
        self.assertEqual((pin.version, pin.lockfile, pin.integrity), ("1.2.3", "package-lock.json", "sha512-AAAA"))

    def test_nested_versions_are_ambiguous_until_the_installed_one_picks(self):
        pin = lock.pin(self.project, "multi")
        self.assertFalse(pin)
        self.assertEqual(pin.candidates, ["1.0.0", "2.0.0"])
        self.assertIn("more than one version", pin.reason)
        self.assertEqual(lock.pin(self.project, "multi", installed="2.0.0").version, "2.0.0")

    def test_pnpm_lock_importer_pin_strips_peer_suffix_and_reads_integrity(self):
        (self.project / "package-lock.json").unlink()
        shutil.copy(FIXTURES / "locks" / "pnpm-lock.yaml", self.project)
        self.assertEqual(lock.pin(self.project, "@scope/scoped").version, "4.0.0")
        self.assertEqual(lock.pin(self.project, "plainpkg").integrity, "sha512-AAAA")
        self.assertEqual(lock.pin(self.project, "other").version, "5.0.0")  # only in packages:, still the one version

    def test_pnpm_lock_v5_style_keys(self):
        (self.project / "package-lock.json").unlink()
        self.write_lock("lockfileVersion: 5.4\n\ndependencies:\n  left-pad: 1.3.0\n\npackages:\n\n  /left-pad/1.3.0_react@18.0.0:\n"
                        "    resolution: {integrity: sha512-X}\n", "pnpm-lock.yaml")
        self.assertEqual(lock.pin(self.project, "left-pad").version, "1.3.0")

    def test_deno_lock_specifier_pin(self):
        (self.project / "package-lock.json").unlink()
        shutil.copy(FIXTURES / "locks" / "deno.lock", self.project)
        pin = lock.pin(self.project, "denopkg")
        self.assertEqual((pin.version, pin.lockfile), ("1.2.0", "deno.lock"))

    def test_no_entry_and_no_lockfile_say_no_pinned_version(self):
        self.assertIn("no pinned version", lock.pin(self.project, "left-pad").reason)
        (self.project / "package-lock.json").unlink()
        pin = lock.pin(self.project, "plainpkg")
        self.assertFalse(pin)
        self.assertIn("no lockfile", pin.reason)

    def test_lock_hash_tracks_the_lockfile_bytes(self):
        first = lock.lock_hash(self.project)
        self.assertEqual(first, lock.lock_hash(self.project))
        (self.project / "package-lock.json").write_text((self.project / "package-lock.json").read_text() + "\n")
        self.assertNotEqual(first, lock.lock_hash(self.project))
        (self.project / "package-lock.json").unlink()
        self.assertIsNone(lock.lock_hash(self.project))

    def test_find_project_walks_up_to_the_lockfile(self):
        deep = self.project / "src" / "a"
        deep.mkdir(parents=True)
        self.assertEqual(lock.find_project(deep), self.project)


class GuardTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve() / "pkg"
        self.root.mkdir()
        self.outside = Path(temp.name).resolve() / "outside.d.ts"
        self.outside.write_text("export declare const secret: 1;\n")

    def test_allowlist_is_declaration_and_markdown_files(self):
        for good in ("a.d.ts", "dir/b.d.mts", "c.d.cts", "README.md"):
            (self.root / good).parent.mkdir(exist_ok=True)
            (self.root / good).write_text("x")
            self.assertEqual(guard.resolve_inside(self.root, good), self.root / good)
        for bad in ("a.ts", "a.js", "package.json", "notes.txt", "a.d.ts.map"):
            with self.assertRaises(guard.Rejected, msg=bad):
                guard.resolve_inside(self.root, bad)

    def test_env_components_are_never_read(self):
        for bad in (".env", ".env.md", ".env.production.d.ts", "dir/.env.d.ts", ".envrc/x.md"):
            with self.assertRaises(guard.Rejected, msg=bad):
                guard.resolve_inside(self.root, bad)

    def test_traversal_and_absolute_paths_are_refused(self):
        for bad in ("../outside.d.ts", "a/../../outside.d.ts", str(self.outside)):
            with self.assertRaises(guard.Rejected, msg=bad):
                guard.resolve_inside(self.root, bad)

    def test_symlink_leaving_the_root_is_refused_and_one_inside_is_followed(self):
        (self.root / "escape.d.ts").symlink_to(self.outside)
        with self.assertRaises(guard.Rejected):
            guard.resolve_inside(self.root, "escape.d.ts")
        (self.root / "real.d.ts").write_text("export {}\n")
        (self.root / "alias.d.ts").symlink_to(self.root / "real.d.ts")
        self.assertEqual(guard.resolve_inside(self.root, "alias.d.ts"), self.root / "real.d.ts")
        (self.root / "linkdir").symlink_to(self.outside.parent)
        with self.assertRaises(guard.Rejected):
            guard.resolve_inside(self.root, "linkdir/outside.d.ts")

    def test_a_link_with_an_allowed_name_to_an_env_file_inside_is_refused(self):
        (self.root / ".env").write_text("TOKEN=1\n")
        (self.root / "innocent.d.ts").symlink_to(self.root / ".env")
        with self.assertRaises(guard.Rejected):
            guard.resolve_inside(self.root, "innocent.d.ts")

    def test_read_text_refuses_binary_and_huge_files(self):
        (self.root / "bin.md").write_bytes(b"ab\0cd")
        (self.root / "big.md").write_text("x" * 50)
        with self.assertRaises(guard.Rejected):
            guard.read_text(self.root / "bin.md")
        with self.assertRaises(guard.Rejected):
            guard.read_text(self.root / "big.md", limit=10)
        self.assertIsNone(guard.read_text(self.root / "missing.md"))


class SignatureTests(ProjectCase):
    def test_dts_declaration_with_its_doc_comment_and_overloads(self):
        found = sig(self.project, "plainpkg", "connect")
        self.assertEqual(found.version, "1.2.3")
        self.assertIn("plainpkg@1.2.3 — index.d.ts", found.text)
        self.assertIn(" * Open a connection.\n", found.text)
        self.assertIn("export declare function connect(url: string, options?: ConnectOptions<{ id: string }>): Promise<Connection>;", found.text)
        self.assertIn("/** Overload taking a port. */\nexport declare function connect(port: number)", found.text)

    def test_generic_interface_and_unterminated_statements(self):
        text = sig(self.project, "plainpkg", "ConnectOptions").text
        self.assertIn("/** Options for {@link connect}. */", text)
        self.assertTrue(text.rstrip().endswith("onError(error: Error): void\n}"), text)
        self.assertIn("export type Handler = (event: string) => void", sig(self.project, "plainpkg", "Handler").text)
        self.assertIn("export declare const DEFAULT_TIMEOUT: number", sig(self.project, "plainpkg", "DEFAULT_TIMEOUT").text)
        self.assertNotIn("Mode", sig(self.project, "plainpkg", "DEFAULT_TIMEOUT").text)
        self.assertIn("export declare const enum Mode { Fast = 1, Slow = 2 }", sig(self.project, "plainpkg", "Mode").text)

    def test_dotted_symbols_find_members_of_namespaces_and_classes(self):
        self.assertIn("/** Join with a slash. */\nfunction join(...parts: string[]): string", sig(self.project, "plainpkg", "util.join").text)
        member = sig(self.project, "plainpkg", "Connection.close").text
        self.assertIn("(member Connection.close)", member)
        self.assertIn("/** Close it. */\nclose(): void", member)
        self.assertIn("static from(url: string): Connection", sig(self.project, "plainpkg", "Connection.from").text)

    def test_dmts_declaration_through_the_exports_types_condition(self):
        found = sig(self.project, "esmpkg", "parseDuration")
        self.assertIn("esmpkg@2.0.0 — dist/index.d.mts", found.text)
        self.assertIn('/** Parse a duration like "5m". */\nexport declare function parseDuration(text: string): number;', found.text)
        self.assertIn('export declare const unit: "ms" | "s";', sig(self.project, "esmpkg", "unit").text)

    def test_relative_reexports_are_followed_including_renames(self):
        self.assertIn("lib/things.d.ts", sig(self.project, "reexp", "makeThing").text)
        self.assertIn("export declare function internalName(): string", sig(self.project, "reexp", "renamed").text)
        self.assertIn("declare function Local(): void;", sig(self.project, "reexp", "Exposed").text)

    def test_types_package_is_used_when_the_package_ships_none_and_pins_on_its_own(self):
        found = sig(self.project, "untyped", "untyped")
        self.assertEqual((found.package, found.version), ("@types/untyped", "3.1.9"))
        self.assertIn("From @types.", found.text)
        self.assertIn("const flag: boolean", sig(self.project, "untyped", "untyped.flag").text)  # via `export =`

    def test_symbol_not_found_is_a_miss_that_names_what_was_read(self):
        with self.assertRaises(signature.Miss) as ctx:
            sig(self.project, "plainpkg", "doesNotExist")
        self.assertIn("doesNotExist not found in the plainpkg@1.2.3 declarations", str(ctx.exception))
        self.assertEqual(ctx.exception.version, "1.2.3")

    def test_bad_symbols_and_package_names_are_refused_before_any_read(self):
        for symbol in ("", "a b", "a;b", "../x", "x.", "1abc", "a" * 201):
            with self.assertRaises(guard.Rejected, msg=symbol):
                sig(self.project, "plainpkg", symbol)
        for package in ("", "../../etc", "a/../b", "/etc", ".", "..", "@scope", "a b", "~root", "jsr:@std/path", "https://x.test/a.ts", "file:///etc"):
            with self.assertRaises(guard.Rejected, msg=package):
                sig(self.project, package, "x")

    def test_package_dir_symlink_leaving_node_modules_is_rejected(self):
        outside = self.base / "elsewhere"
        outside.mkdir()
        (outside / "package.json").write_text('{"name":"evil","version":"1.0.0","types":"index.d.ts"}')
        (outside / "index.d.ts").write_text("export declare function stolen(): void;\n")
        (self.project / "node_modules" / "evil").symlink_to(outside)
        with self.assertRaises(guard.Rejected) as ctx:
            sig(self.project, "evil", "stolen")
        self.assertIn("outside", str(ctx.exception))

    def test_node_modules_that_is_itself_a_symlink_is_rejected(self):
        real = self.base / "real_modules"
        (self.project / "node_modules").rename(real)
        (self.project / "node_modules").symlink_to(real)
        with self.assertRaises(guard.Rejected):
            sig(self.project, "plainpkg", "connect")

    def test_declaration_file_symlink_leaving_the_package_is_rejected(self):
        outside = self.base / "outside.d.ts"
        outside.write_text("export declare function stolen(): void;\n")
        entry = self.project / "node_modules" / "plainpkg" / "index.d.ts"
        entry.unlink()
        entry.symlink_to(outside)
        with self.assertRaises(guard.Rejected) as ctx:
            sig(self.project, "plainpkg", "stolen")
        self.assertIn("outside", str(ctx.exception))

    def test_types_path_that_climbs_out_is_rejected_not_followed(self):
        pkg = self.project / "node_modules" / "@scope" / "scoped"
        (pkg / "index.d.ts").unlink()
        (self.base / "project" / "outside.d.ts").write_text("export declare function stolen(): void;\n")
        with self.assertRaises(guard.Rejected) as ctx:
            sig(self.project, "@scope/scoped", "stolen")
        self.assertIn("leaves the package", str(ctx.exception))

    def test_reexport_that_climbs_out_is_not_followed(self):
        pkg = self.project / "node_modules" / "reexp"
        (self.project / "node_modules" / "stolen.d.ts").write_text("export declare function stolen(): void;\n")
        (pkg / "index.d.ts").write_text("export * from '../stolen'\nexport declare function mine(): void;\n")
        with self.assertRaises(signature.Miss) as ctx:
            sig(self.project, "reexp", "stolen")
        self.assertIn("refused", str(ctx.exception))

    def test_env_and_non_allowlisted_entry_files_are_rejected(self):
        pkg = self.project / "node_modules" / "plainpkg"
        for target, message in ((".env", "environment file"), (".env.d.ts", "environment file"), ("src/index.ts", "not a declaration"),
                                ("types.json", "not a declaration")):
            (pkg / "package.json").write_text(json.dumps({"name": "plainpkg", "version": "1.2.3", "types": target}))
            (pkg / "index.d.ts").rename(pkg / "keep.d.ts")  # no fallback entry to hide the refusal
            try:
                with self.assertRaises(guard.Rejected, msg=target) as ctx:
                    sig(self.project, "plainpkg", "connect")
                self.assertIn(message, str(ctx.exception))
            finally:
                (pkg / "keep.d.ts").rename(pkg / "index.d.ts")

    def test_symlinked_declaration_to_an_env_file_inside_the_package_is_rejected(self):
        pkg = self.project / "node_modules" / "plainpkg"
        (pkg / ".env").write_text("export declare const TOKEN: 'hunter2';\n")
        (pkg / "index.d.ts").unlink()
        (pkg / "index.d.ts").symlink_to(pkg / ".env")
        with self.assertRaises(guard.Rejected):
            sig(self.project, "plainpkg", "TOKEN")

    def test_no_lockfile_entry_returns_no_pinned_version_and_does_not_read_node_modules(self):
        data = json.loads((self.project / "package-lock.json").read_text())
        del data["packages"]["node_modules/plainpkg"]
        self.write_lock(data)
        with mock.patch.object(guard, "read_text", side_effect=AssertionError("read without a pin")):
            with self.assertRaises(signature.Miss) as ctx:
                sig(self.project, "plainpkg", "connect")
        self.assertIn("no pinned version", str(ctx.exception))
        self.assertIsNone(ctx.exception.version)

    def test_project_without_a_lockfile_has_no_pinned_version(self):
        (self.project / "package-lock.json").unlink()
        with self.assertRaises(signature.Miss) as ctx:
            sig(self.project, "plainpkg", "connect")
        self.assertIn("no lockfile", str(ctx.exception))

    def test_installed_version_that_differs_from_the_pin_is_a_miss_naming_both(self):
        pkg = self.project / "node_modules" / "plainpkg" / "package.json"
        pkg.write_text(pkg.read_text().replace("1.2.3", "1.9.0"))
        with self.assertRaises(signature.Miss) as ctx:
            sig(self.project, "plainpkg", "connect")
        self.assertIn("1.9.0", str(ctx.exception))
        self.assertIn("pins 1.2.3", str(ctx.exception))

    def test_a_version_asked_for_must_be_the_pinned_one(self):
        self.assertIn("connect", sig(self.project, "plainpkg@1.2.3", "connect").text)
        with self.assertRaises(signature.Miss) as ctx:
            sig(self.project, "plainpkg@9.9.9", "connect")
        self.assertIn("pins 1.2.3", str(ctx.exception))

    def test_deno_npm_cache_serves_npm_specifiers_for_a_deno_lock_project(self):
        (self.project / "package-lock.json").unlink()
        shutil.copy(FIXTURES / "locks" / "deno.lock", self.project)
        deno = FIXTURES / "deno"
        found = sig(self.project, "npm:denopkg", "fromDeno", deno=deno)
        self.assertIn("denopkg@1.2.0 — mod.d.ts", found.text)
        self.assertIn("/** Deno-cached function. */", found.text)
        self.assertIn("fromDeno", sig(self.project, "denopkg", "fromDeno", deno=deno).text)  # bare name, absent from node_modules
        with self.assertRaises(signature.Miss) as ctx:
            sig(self.project, "npm:denopkg", "fromDeno", deno=self.base / "empty-cache")
        self.assertIn("not installed", str(ctx.exception))

    def test_deno_cache_version_must_be_the_pinned_one(self):
        (self.project / "package-lock.json").unlink()
        self.write_lock(json.dumps({"version": "4", "specifiers": {"npm:denopkg@1": "1.3.0"}}), "deno.lock")
        with self.assertRaises(signature.Miss):
            sig(self.project, "npm:denopkg", "fromDeno", deno=FIXTURES / "deno")

    def test_jsr_and_https_specifiers_are_refused_with_the_reason(self):
        for spec in ("jsr:@std/path", "https://deno.land/x/mod.ts", "npm:../../x"):
            with self.assertRaises(guard.Rejected, msg=spec):
                sig(self.project, spec, "join", deno=FIXTURES / "deno")

    def test_declaration_scanner_handles_comments_strings_and_missing_semicolons(self):
        text = ('declare const a: "}" // } in a comment\n/* export declare function nope(): void */\n'
                "export declare function f(\n  x: number,\n  y: { z: string }\n): void\nexport declare module 'm' {\n  export function inner(): void\n}\n"
                "export declare type U =\n  | 'a'\n  | 'b'\nexport interface I extends Base<{ k: 1 }> {\n  a: 1\n}\n")
        self.assertEqual(len(signature.find_declarations(text, "a")), 1)
        self.assertEqual(signature.find_declarations(text, "nope"), [])
        self.assertIn("y: { z: string }\n): void", signature.find_declarations(text, "f")[0])
        self.assertIn("function inner", signature.find_declarations(text, "inner")[0])
        self.assertTrue(signature.find_declarations(text, "U")[0].endswith("| 'b'"))
        self.assertTrue(signature.find_declarations(text, "I")[0].endswith("a: 1\n}"))


class IndexTests(ProjectCase):
    def setUp(self):
        super().setUp()
        self.docs = self.base / "docs"
        shutil.copytree(FIXTURES / "docs", self.docs)
        (self.docs / ".env.md").write_text("# secrets\nhunter2 token password\n")
        (self.docs / "notes.txt").write_text("hunter2 connect\n")
        (self.docs / "link.md").symlink_to(self.base / "outside.md")
        (self.base / "outside.md").write_text("# outside\nhunter2 connect\n")
        (self.docs / "node_modules").mkdir()
        (self.docs / "node_modules" / "dep.md").write_text("# dep\nhunter2\n")

    def build(self, **kw):
        return index.build_for(self.project, "plainpkg", source=kw.pop("source", self.docs), **kw)

    def test_build_indexes_only_allowlisted_markdown_under_the_source(self):
        meta = self.build()
        self.assertEqual((meta["lib"], meta["version"], meta["files"]), ("plainpkg", "1.2.3", 2))
        directory = index.index_root(self.project) / "plainpkg@1.2.3"
        self.assertTrue((directory / "index.json").is_file())
        self.assertNotIn("hunter2", (directory / "index.json").read_text())

    def test_search_ranks_the_matching_section_and_reports_file_and_line(self):
        self.build()
        hit = index.docs_search(self.project, "plainpkg", "timeout option connect")
        self.assertEqual(hit.version, "1.2.3")
        first = hit.text.split("\n\n")[1]
        self.assertTrue(first.startswith("[README.md:"), first)
        self.assertIn("Connecting", first)
        self.assertIn("streaming.md", index.docs_search(self.project, "plainpkg", "backpressure").text)
        self.assertIn("connectWithBackoff", index.docs_search(self.project, "plainpkg", "connect with backoff").text)

    def test_a_hash_in_a_code_fence_is_not_a_heading(self):
        chunks = index.chunk_markdown("# T\n\n```sh\n# not a heading\nls\n```\n\n## Next\ntext\n", "a.md")
        self.assertEqual([c["h"] for c in chunks], ["T", "Next"])

    def test_search_never_builds_and_says_what_to_run(self):
        with self.assertRaises(index.Miss) as ctx:
            index.docs_search(self.project, "plainpkg", "timeout")
        self.assertIn("no docs index for plainpkg@1.2.3", str(ctx.exception))
        self.assertIn("tp-docs build plainpkg", str(ctx.exception))
        self.assertFalse(index.index_root(self.project).exists())

    def test_search_for_a_library_with_no_pin_has_no_fallback_version(self):
        self.build()
        data = json.loads((self.project / "package-lock.json").read_text())
        del data["packages"]["node_modules/plainpkg"]
        self.write_lock(data)
        with self.assertRaises(index.Miss) as ctx:
            index.docs_search(self.project, "plainpkg", "timeout")
        self.assertIn("no pinned version", str(ctx.exception))

    def test_an_index_for_another_version_is_not_used_after_the_pin_moves(self):
        self.build()
        data = json.loads((self.project / "package-lock.json").read_text())
        data["packages"]["node_modules/plainpkg"]["version"] = "1.3.0"
        self.write_lock(data)
        with self.assertRaises(index.Miss) as ctx:
            index.docs_search(self.project, "plainpkg", "timeout")
        self.assertIn("plainpkg@1.3.0", str(ctx.exception))

    def test_refresh_rebuilds_when_the_lockfile_hash_changes_and_not_otherwise(self):
        before = self.build()
        self.assertEqual([a for _, a, _ in index.refresh(self.project)], ["unchanged"])
        self.assertEqual([a for _, a, _ in index.refresh(self.project)], ["unchanged"])
        meta_file = index.index_root(self.project) / "plainpkg@1.2.3" / "meta.json"
        self.assertEqual(json.loads(meta_file.read_text())["build_id"], before["build_id"])
        # an unrelated lockfile change moves the hash
        data = json.loads((self.project / "package-lock.json").read_text())
        data["packages"]["node_modules/esmpkg"]["version"] = "2.0.1"
        self.write_lock(data)
        self.assertEqual([a for _, a, _ in index.refresh(self.project)], ["rebuilt"])
        after = json.loads(meta_file.read_text())
        self.assertNotEqual(after["build_id"], before["build_id"])
        self.assertEqual(after["lock_hash"], lock.lock_hash(self.project))
        self.assertEqual([a for _, a, _ in index.refresh(self.project)], ["unchanged"])

    def test_refresh_follows_a_moved_pin_to_the_new_version_and_drops_the_old_index(self):
        self.build()
        data = json.loads((self.project / "package-lock.json").read_text())
        data["packages"]["node_modules/plainpkg"]["version"] = "1.3.0"
        self.write_lock(data)
        pkg = self.project / "node_modules" / "plainpkg" / "package.json"
        pkg.write_text(pkg.read_text().replace("1.2.3", "1.3.0"))
        (self.project / "node_modules" / "plainpkg" / "README.md").write_text("# plainpkg 1.3\n\nNew `timeout` docs.\n")
        # the recorded source was an explicit directory, which still exists and is reused; it indexes the old docs for the new pin
        self.assertEqual(index.refresh(self.project)[0][:2], ("plainpkg", "rebuilt"))
        self.assertFalse((index.index_root(self.project) / "plainpkg@1.2.3").exists())
        self.assertTrue((index.index_root(self.project) / "plainpkg@1.3.0").is_dir())

    def test_default_source_is_the_installed_package_only_when_it_is_the_pinned_version(self):
        (self.project / "node_modules" / "plainpkg" / "README.md").write_text("# plainpkg\n\nInstalled docs about retries.\n")
        meta = index.build_for(self.project, "plainpkg")
        self.assertEqual(meta["source"]["kind"], "package")
        self.assertIn("retries", index.docs_search(self.project, "plainpkg", "retries").text)
        pkg = self.project / "node_modules" / "plainpkg" / "package.json"
        pkg.write_text(pkg.read_text().replace("1.2.3", "1.9.0"))
        with self.assertRaises(index.Miss) as ctx:
            index.build_for(self.project, "plainpkg")
        self.assertIn("no docs source", str(ctx.exception))

    def test_a_stale_index_is_flagged_not_hidden(self):
        self.build()
        data = json.loads((self.project / "package-lock.json").read_text())
        data["packages"]["node_modules/esmpkg"]["version"] = "2.0.1"
        self.write_lock(data)
        self.assertIn("older lockfile", index.docs_search(self.project, "plainpkg", "timeout").text)
        row = index.status(self.project)["indexes"][0]
        self.assertTrue(row["lock_changed"])
        self.assertTrue(row["matches_pin"])

    def test_search_inputs_are_checked(self):
        self.build()
        for lib, query in (("../x", "q"), ("plainpkg", ""), ("plainpkg", "q" * 501)):
            with self.assertRaises(guard.Rejected, msg=(lib, query)):
                index.docs_search(self.project, lib, query)
        with self.assertRaises(guard.Rejected):
            index.docs_search(self.project, "plainpkg", "q", limit="many")

    def test_build_replaces_the_index_atomically_and_leaves_no_scratch_directories(self):
        self.build()
        self.build()
        names = sorted(p.name for p in index.index_root(self.project).iterdir())
        self.assertEqual(names, ["plainpkg@1.2.3"])

    def test_an_index_directory_that_is_a_symlink_is_not_read(self):
        self.build()
        directory = index.index_root(self.project) / "plainpkg@1.2.3"
        moved = self.base / "moved"
        directory.rename(moved)
        directory.symlink_to(moved)
        with self.assertRaises(index.Miss):
            index.docs_search(self.project, "plainpkg", "timeout")

    def test_scoped_library_names_get_a_flat_directory(self):
        self.assertEqual(index.key("@scope/scoped", "4.0.0"), "@scope__scoped@4.0.0")


class FetchTests(ProjectCase):
    def tarball(self, members):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as archive:
            for name, content, kind in members:
                info = tarfile.TarInfo(name)
                if kind == "file":
                    data = content.encode()
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
                else:
                    info.type = tarfile.SYMTYPE
                    info.linkname = content
                    archive.addfile(info)
        return buf.getvalue()

    def test_only_safe_markdown_members_are_kept(self):
        data = self.tarball([("package/README.md", "# r", "file"), ("package/docs/guide.md", "# g", "file"), ("package/index.js", "x", "file"),
                             ("package/.env.md", "t", "file"), ("package/docs/.env/x.md", "t", "file"), ("package/../evil.md", "t", "file"),
                             ("package/node_modules/dep/README.md", "t", "file"), ("package/link.md", "/etc/passwd", "link"),
                             ("/abs.md", "t", "file")])
        self.assertEqual(sorted(name for name, _ in fetch.docs_from_tarball(data)), ["README.md", "docs/guide.md"])

    def test_fetch_writes_docs_src_checked_against_the_lockfile_integrity(self):
        import base64
        import hashlib
        data = self.tarball([("package/README.md", "# plainpkg\n\nFetched docs about `retries`.\n", "file")])
        digest = base64.b64encode(hashlib.sha512(data).digest()).decode()
        lockdata = json.loads((self.project / "package-lock.json").read_text())
        lockdata["packages"]["node_modules/plainpkg"]["integrity"] = f"sha512-{digest}"
        self.write_lock(lockdata)
        urls = []
        done = fetch.fetch(self.project, "plainpkg", opener=lambda url: urls.append(url) or data, registry="https://registry.test")
        self.assertEqual(urls, ["https://registry.test/plainpkg/-/plainpkg-1.2.3.tgz"])
        self.assertEqual((done["version"], done["files"], done["integrity"]), ("1.2.3", 1, "verified sha512"))
        meta = index.build_for(self.project, "plainpkg")
        self.assertEqual(meta["source"]["kind"], "fetched")
        self.assertIn("retries", index.docs_search(self.project, "plainpkg", "retries").text)

    def test_integrity_mismatch_is_refused_and_writes_nothing(self):
        data = self.tarball([("package/README.md", "# r", "file")])
        with self.assertRaises(fetch.FetchError) as ctx:
            fetch.fetch(self.project, "plainpkg", opener=lambda url: data)  # the fixture lock says sha512-AAAA
        self.assertIn("integrity mismatch", str(ctx.exception))
        self.assertFalse((self.project / index.SOURCE_ROOT).exists())

    def test_fetch_refuses_without_a_pin(self):
        with self.assertRaises(fetch.FetchError) as ctx:
            fetch.fetch(self.project, "left-pad", opener=lambda url: b"")
        self.assertIn("no pinned version", str(ctx.exception))

    def test_tarball_url_for_scoped_packages(self):
        self.assertEqual(fetch.tarball_url("@scope/scoped", "4.0.0"), "https://registry.npmjs.org/@scope/scoped/-/scoped-4.0.0.tgz")


class CallTests(ProjectCase):
    def lines(self):
        path = self.task / "docs-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def call(self, tool, args, **kw):
        return calls.call(tool, args, project=self.project, env={"TP_TASK_DIR": str(self.task)}, **kw)

    def test_every_call_logs_exactly_one_line_with_its_outcome(self):
        shutil.copytree(FIXTURES / "docs", self.base / "docs")
        index.build_for(self.project, "plainpkg", source=self.base / "docs")
        cases = [("api_signature", {"package": "plainpkg", "symbol": "connect"}, "hit"),
                 ("api_signature", {"package": "plainpkg", "symbol": "nope"}, "miss"),
                 ("api_signature", {"package": "../../etc", "symbol": "x"}, "rejected"),
                 ("api_signature", {"package": "left-pad", "symbol": "x"}, "miss"),
                 ("docs_search", {"lib": "plainpkg", "query": "timeout"}, "hit"),
                 ("docs_search", {"lib": "plainpkg", "query": "zzzqqq"}, "miss"),
                 ("docs_search", {"lib": "esmpkg", "query": "x"}, "miss"),
                 ("nope", {}, "rejected")]
        for number, (tool, args, expected) in enumerate(cases, 1):
            reply = self.call(tool, args)
            self.assertEqual(reply.status, expected, (tool, args, reply.text))
            lines = self.lines()
            self.assertEqual(len(lines), number)
            entry = lines[-1]
            self.assertEqual((entry["tool"], entry["result"], entry["args"]), (tool, expected, {k: str(v) for k, v in args.items()}))
            self.assertEqual(entry["bytes"], len(reply.text.encode()))
            self.assertRegex(entry["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
            self.assertEqual(entry["index_version"], reply.version)
            if expected != "hit":
                self.assertTrue(entry["reason"])

    def test_unset_task_dir_fails_closed_before_anything_is_read(self):
        with mock.patch.object(guard, "read_text", side_effect=AssertionError("read before logging")):
            for env in ({}, {"TP_TASK_DIR": ""}, {"TP_TASK_DIR": str(self.base / "missing")}):
                with self.assertRaises(log.LogUnavailable, msg=env):
                    calls.call("api_signature", {"package": "plainpkg", "symbol": "connect"}, project=self.project, env=env)

    def test_a_log_that_cannot_be_written_withholds_the_answer(self):
        (self.task / "docs-calls.jsonl").symlink_to(self.base / "elsewhere.jsonl")
        with self.assertRaises(log.LogUnavailable):
            self.call("api_signature", {"package": "plainpkg", "symbol": "connect"})
        self.assertFalse((self.base / "elsewhere.jsonl").exists())  # the link was not followed

    def test_a_task_dir_that_is_a_symlink_is_refused(self):
        link = self.base / "tasklink"
        link.symlink_to(self.task)
        with self.assertRaises(log.LogUnavailable):
            calls.call("api_signature", {"package": "plainpkg", "symbol": "connect"}, project=self.project, env={"TP_TASK_DIR": str(link)})

    def test_explicit_log_file_overrides_the_task_dir(self):
        target = self.base / "mine.jsonl"
        calls.call("api_signature", {"package": "plainpkg", "symbol": "connect"}, project=self.project, log_file=target, env={})
        self.assertEqual(len(target.read_text().splitlines()), 1)

    def test_long_arguments_are_clipped_in_the_log(self):
        self.call("api_signature", {"package": "plainpkg", "symbol": "a" * 9000})
        self.assertLess(len(self.lines()[0]["args"]["symbol"]), 600)

    def test_responses_are_capped_at_8kb_with_a_truncation_marker(self):
        decls = "".join(f"export declare function big{n}(a: string): void;\n" for n in range(2000))
        (self.project / "node_modules" / "plainpkg" / "index.d.ts").write_text(
            "/** " + "long " * 3000 + "*/\nexport declare function huge(): void;\n" + decls)
        reply = self.call("api_signature", {"package": "plainpkg", "symbol": "huge"})
        size = len(reply.text.encode())
        self.assertLessEqual(size, calls.MAX_RESPONSE)
        self.assertRegex(reply.text, r"\n\[truncated: \d+ more bytes\]$")
        self.assertEqual(self.lines()[0]["bytes"], size)
        self.assertEqual(calls.cap("short"), "short")
        multi = calls.cap("é" * 9000)  # never cut inside a character
        self.assertLessEqual(len(multi.encode()), calls.MAX_RESPONSE)
        multi.encode("utf-8")

    def test_an_unexpected_exception_is_logged_as_error_without_a_traceback(self):
        with mock.patch.object(signature, "api_signature", side_effect=RuntimeError("boom")):
            reply = self.call("api_signature", {"package": "plainpkg", "symbol": "connect"})
        self.assertEqual(reply.status, "error")
        self.assertNotIn("boom", reply.text)
        self.assertEqual(self.lines()[0]["reason"], "RuntimeError('boom')")


class ProvenanceTests(ProjectCase):
    """Every answer says where it came from: source, version, whether that version is exactly the lockfile's, and when the
    content was retrieved. The same record is in the answer's first line and in the call log."""
    HEADER = r"^\[provenance\] source=(?P<source>\S+) version=(?P<version>\S+) exact=(?P<exact>yes|no) retrieved=(?P<retrieved>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)\n"

    def setUp(self):
        super().setUp()
        shutil.copytree(FIXTURES / "docs", self.base / "docs")

    def lines(self):
        path = self.task / "docs-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def call(self, tool, args, **kw):
        return calls.call(tool, args, project=self.project, env={"TP_TASK_DIR": str(self.task)}, **kw)

    def test_api_signature_records_source_version_exact_flag_and_retrieval_time(self):
        reply = self.call("api_signature", {"package": "plainpkg", "symbol": "connect"})
        found = re.match(self.HEADER, reply.text)
        self.assertTrue(found, reply.text)
        self.assertEqual((found["version"], found["exact"]), ("1.2.3", "yes"))
        self.assertEqual(found["source"], f"node_modules:{self.project / 'node_modules' / 'plainpkg'}")
        self.assertEqual(reply.provenance, {"source": found["source"], "version": "1.2.3", "exact": True, "retrieved": found["retrieved"]})
        self.assertEqual(self.lines()[0]["provenance"], reply.provenance)

    def test_deno_cache_answers_name_the_cache_as_the_source(self):
        (self.project / "package-lock.json").unlink()
        shutil.copy(FIXTURES / "locks" / "deno.lock", self.project)
        reply = self.call("api_signature", {"package": "npm:denopkg", "symbol": "fromDeno"}, deno=FIXTURES / "deno")
        self.assertEqual(reply.status, "hit", reply.text)
        self.assertTrue(reply.provenance["source"].startswith("deno-cache:"), reply.provenance)
        self.assertEqual((reply.provenance["version"], reply.provenance["exact"]), ("1.2.0", True))

    def test_a_search_answer_names_the_index_source_and_is_exact_only_for_installed_or_fetched_docs(self):
        index.build_for(self.project, "plainpkg", source=self.base / "docs")
        reply = self.call("docs_search", {"lib": "plainpkg", "query": "timeout"})
        found = re.match(self.HEADER, reply.text)
        self.assertTrue(found, reply.text)
        self.assertEqual(found["source"], f"dir:{self.base / 'docs'}")
        self.assertEqual((found["version"], found["exact"]), ("1.2.3", "no"), "a directory the user named is not known to be that version")
        (self.project / "node_modules" / "plainpkg" / "README.md").write_text("# plainpkg\n\nInstalled docs about retries.\n")
        index.build_for(self.project, "plainpkg")
        again = self.call("docs_search", {"lib": "plainpkg", "query": "retries"})
        self.assertEqual((again.provenance["exact"], again.provenance["source"].split(":")[0]), (True, "package"))

    def test_search_is_not_exact_once_the_lockfile_moved_since_the_build(self):
        (self.project / "node_modules" / "plainpkg" / "README.md").write_text("# plainpkg\n\nInstalled docs about retries.\n")
        index.build_for(self.project, "plainpkg")
        self.assertTrue(self.call("docs_search", {"lib": "plainpkg", "query": "retries"}).provenance["exact"])
        data = json.loads((self.project / "package-lock.json").read_text())
        data["packages"]["node_modules/esmpkg"]["version"] = "2.0.1"
        self.write_lock(data)
        reply = self.call("docs_search", {"lib": "plainpkg", "query": "retries"})
        self.assertFalse(reply.provenance["exact"])
        self.assertIn("exact=no", reply.text.split("\n")[0])

    def test_fetched_docs_carry_the_time_they_were_fetched_not_the_time_they_were_indexed(self):
        import base64
        import hashlib
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as archive:
            body = b"# plainpkg\n\nFetched docs about `retries`.\n"
            info = tarfile.TarInfo("package/README.md")
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
        data = buf.getvalue()
        lockdata = json.loads((self.project / "package-lock.json").read_text())
        lockdata["packages"]["node_modules/plainpkg"]["integrity"] = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
        self.write_lock(lockdata)
        with mock.patch("time.gmtime", return_value=time.gmtime(1_700_000_000)):
            done = fetch.fetch(self.project, "plainpkg", opener=lambda url: data)
        self.assertEqual(done["retrieved"], "2023-11-14T22:13:20Z")
        index.build_for(self.project, "plainpkg")
        reply = self.call("docs_search", {"lib": "plainpkg", "query": "retries"})
        self.assertEqual((reply.provenance["retrieved"], reply.provenance["exact"]), ("2023-11-14T22:13:20Z", True))
        self.assertTrue(reply.provenance["source"].startswith("fetched:"))

    def test_misses_and_refusals_carry_no_provenance(self):
        for tool, args in (("api_signature", {"package": "plainpkg", "symbol": "nope"}), ("docs_search", {"lib": "plainpkg", "query": "x"}),
                           ("api_signature", {"package": "../x", "symbol": "y"})):
            reply = self.call(tool, args)
            self.assertNotEqual(reply.status, "hit")
            self.assertIsNone(reply.provenance)
            self.assertNotIn("[provenance]", reply.text)
        self.assertTrue(all(line["provenance"] is None for line in self.lines()))

    def test_the_provenance_line_survives_the_size_cap(self):
        (self.base / "docs" / "big.md").write_text("# Big\n\n" + "\n\n".join(f"timeout section {n} " + "word " * 300 for n in range(40)))
        index.build_for(self.project, "plainpkg", source=self.base / "docs")
        reply = self.call("docs_search", {"lib": "plainpkg", "query": "timeout section word", "limit": 10})
        self.assertRegex(reply.text, self.HEADER)
        self.assertLessEqual(len(reply.text.encode()), calls.MAX_RESPONSE)
        self.assertIn("[truncated:", reply.text)


class SearchCacheTests(ProjectCase):
    """docs_search answers are cached under the task directory, keyed by lib, pinned version, index build and query."""

    def setUp(self):
        super().setUp()
        shutil.copytree(FIXTURES / "docs", self.base / "docs")
        index.build_for(self.project, "plainpkg", source=self.base / "docs")

    def search(self, query="timeout option connect", **kw):
        return calls.call("docs_search", {"lib": "plainpkg", "query": query, **kw}, project=self.project, env={"TP_TASK_DIR": str(self.task)})

    def lines(self):
        return [json.loads(line) for line in (self.task / "docs-calls.jsonl").read_text().splitlines()]

    def test_a_repeated_query_is_served_from_the_cache_without_loading_the_index(self):
        first = self.search()
        with mock.patch.object(index, "_load", side_effect=AssertionError("index loaded")):
            second = self.search()
        self.assertEqual(second.text.split("\n", 1)[1], first.text.split("\n", 1)[1])
        self.assertEqual([line["cache"] for line in self.lines()], ["miss", "hit"])
        self.assertEqual(len(self.lines()), 2, "a cached answer is still logged as a call")

    def test_the_key_is_version_plus_query_so_another_query_or_limit_is_not_served_from_it(self):
        self.search()
        self.search("backpressure")
        self.search(limit=2)
        self.assertEqual([line["cache"] for line in self.lines()], ["miss", "miss", "miss"])
        self.assertEqual(len(list((self.task / "docs-cache").iterdir())), 3)

    def test_a_cached_record_names_the_version_and_query_it_answers(self):
        self.search()
        (record,) = [json.loads(p.read_text()) for p in (self.task / "docs-cache").iterdir()]
        self.assertEqual((record["lib"], record["version"], record["query"]), ("plainpkg", "1.2.3", "timeout option connect"))

    def test_a_rebuilt_index_does_not_serve_the_old_build_s_answers(self):
        self.search()
        (self.base / "docs" / "README.md").write_text("# plainpkg\n\n## Connecting\n\nThe timeout option was renamed to deadline.\n")
        index.build_for(self.project, "plainpkg", source=self.base / "docs")
        again = self.search()
        self.assertIn("deadline", again.text)
        self.assertEqual([line["cache"] for line in self.lines()], ["miss", "miss"])

    def test_a_moved_pin_is_not_served_the_old_version_s_answer(self):
        self.search()
        data = json.loads((self.project / "package-lock.json").read_text())
        data["packages"]["node_modules/plainpkg"]["version"] = "1.2.4"
        self.write_lock(data)
        pkg = self.project / "node_modules" / "plainpkg" / "package.json"
        pkg.write_text(pkg.read_text().replace("1.2.3", "1.2.4"))
        reply = self.search()
        self.assertEqual(reply.status, "miss")
        self.assertIn("no docs index for plainpkg@1.2.4", reply.text)

    def test_staleness_is_judged_on_every_call_not_frozen_into_the_cache(self):
        self.search()
        data = json.loads((self.project / "package-lock.json").read_text())
        data["packages"]["node_modules/esmpkg"]["version"] = "2.0.1"
        self.write_lock(data)
        reply = self.search()
        self.assertIn("older lockfile", reply.text)
        self.assertFalse(reply.provenance["exact"])
        self.assertEqual(self.lines()[-1]["cache"], "hit")

    def test_a_tampered_or_mismatched_cache_record_is_ignored(self):
        self.search()
        (path,) = (self.task / "docs-cache").iterdir()
        record = json.loads(path.read_text())
        record["version"] = "9.9.9"
        record["body"] = "poisoned"
        path.write_text(json.dumps(record))
        self.assertNotIn("poisoned", self.search().text)
        path.write_text("not json")
        self.assertEqual(self.search().status, "hit")

    def test_a_symlinked_cache_directory_is_not_followed(self):
        outside = self.base / "outside"
        outside.mkdir()
        (self.task / "docs-cache").symlink_to(outside)
        self.assertEqual(self.search().status, "hit")
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(self.lines()[-1]["cache"], "off")

    def test_misses_are_not_cached(self):
        self.search("zzzqqq")
        self.assertFalse((self.task / "docs-cache").exists() and list((self.task / "docs-cache").iterdir()))


class OfflineTests(ProjectCase):
    """The query path opens no sockets. The CLI half of this is in test_docs_cli."""

    def test_queries_work_with_every_socket_operation_blocked(self):
        shutil.copytree(FIXTURES / "docs", self.base / "docs")
        index.build_for(self.project, "plainpkg", source=self.base / "docs")

        def blocked(*args, **kwargs):
            raise AssertionError("network used")

        with mock.patch.object(socket.socket, "connect", blocked), mock.patch.object(socket, "create_connection", blocked), \
                mock.patch.object(socket, "getaddrinfo", blocked), mock.patch.object(socket.socket, "bind", blocked):
            env = {"TP_TASK_DIR": str(self.task)}
            self.assertEqual(calls.call("api_signature", {"package": "plainpkg", "symbol": "connect"}, project=self.project, env=env).status, "hit")
            self.assertEqual(calls.call("docs_search", {"lib": "plainpkg", "query": "timeout"}, project=self.project, env=env).status, "hit")

    def test_the_query_modules_import_nothing_that_can_open_a_socket(self):
        network = {"socket", "ssl", "http", "urllib", "urllib3", "requests", "asyncio", "ftplib", "smtplib", "telnetlib", "xmlrpc", "socketserver"}
        for name in ("cache", "calls", "guard", "index", "lock", "log", "signature", "__init__"):
            tree = ast.parse((ROOT / "lib" / "localdocs" / f"{name}.py").read_text())
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    imported.add(node.module.split(".")[0])
                elif isinstance(node, ast.ImportFrom) and node.level:
                    imported.update(alias.name for alias in node.names if alias.name == "fetch")
            self.assertEqual(imported & (network | {"fetch"}), set(), name)

    def test_only_fetch_py_uses_the_network_and_only_the_fetch_command_imports_it(self):
        self.assertIn("urllib.request", (ROOT / "lib/localdocs/fetch.py").read_text())
        cli = (ROOT / "bin/tp-docs").read_text()
        self.assertEqual(cli.count("fetch as fetcher"), 1)
        self.assertNotIn("import fetch", cli.split("def main")[0])


if __name__ == "__main__":
    unittest.main()
