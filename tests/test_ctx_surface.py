"""CX4 ctx-tools: the folded-in CX1 review notes (N1-N8), the surface (argument checks, the 6 KB answer, the status vocabulary),
the deterministic brief, the five operations on stubbed and on fake-real navigators, and the call log. No model, no network,
no real navigator: the stubs and small fakes that copy the real answer shapes stand in."""
import inspect
import json
import os
import pty
import random
import re
import select
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
TP_CTX = ROOT / "bin" / "tp-ctx"
sys.path.insert(0, str(ROOT / "lib"))
from ctxengine import calls  # noqa: E402


def clean_env(extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("TP_")}
    env.update(extra or {})
    return env


def run_pty(argv, env, cwd):
    """Run argv with a pseudo-terminal as stdin and stdout. Returns (exit code, terminal output, stderr text)."""
    master, slave = pty.openpty()
    errors = tempfile.TemporaryFile()
    proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=errors, env=env, cwd=cwd, close_fds=True)
    os.close(slave)
    out = b""
    try:
        while True:
            ready, _, _ = select.select([master], [], [], 30)
            if not ready:
                proc.kill()
                break
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        code = proc.wait(timeout=30)
    finally:
        os.close(master)
        errors.seek(0)
        err = errors.read().decode("utf-8", "replace")
        errors.close()
    return code, out.decode("utf-8", "replace"), err


class FoldedCx1Notes(unittest.TestCase):
    """CX1 review 2: N1 (a `..` after a symlink), N2/N3 (operator commands and status need a terminal to run unlogged), N7 (--log help)."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name).resolve()
        self.project = self.tmp / "project"
        (self.project / ".tmux-pi").mkdir(parents=True)
        self.task = self.tmp / "task"
        self.task.mkdir()

    def test_n1_a_task_dir_with_dot_dot_after_a_symlink_is_refused_and_nothing_is_written_through_it(self):
        other = self.tmp / "other"
        (other / "task").mkdir(parents=True)
        (self.tmp / "lnk").symlink_to(other)
        (self.tmp / "inner").mkdir()
        for value in (f"{self.tmp}/lnk/../task", f"{self.tmp}/./task", f"{self.tmp}//task", f"{self.task}/", f"{self.tmp}/inner/../task"):
            ran = []
            with self.assertRaisesRegex(calls.LogUnavailable, "normalized", msg=value):
                calls.call("t", {}, lambda: ran.append(1) or calls.Answer("ok", "x"), env={"TP_TASK_DIR": value})
            self.assertEqual(ran, [], value)
        self.assertEqual(sorted(p.name for p in other.glob("**/*")), ["task"])
        self.assertFalse((self.task / calls.LOG_NAME).exists())

    def test_n1_a_normalized_task_dir_is_still_served_and_logged_at_the_path_that_was_checked(self):
        calls.call("t", {}, lambda: calls.Answer("ok", "x"), env={"TP_TASK_DIR": str(self.task)})
        self.assertEqual(len((self.task / calls.LOG_NAME).read_text().splitlines()), 1)

    def test_n1_the_command_line_gives_no_answer_for_a_dot_dot_task_dir(self):
        other = self.tmp / "other"
        (other / "task").mkdir(parents=True)
        (self.tmp / "lnk").symlink_to(other)
        done = subprocess.run([sys.executable, str(TP_CTX), "--project", str(self.project), "status"], capture_output=True, text=True,
                              env=clean_env({"TP_TASK_DIR": f"{self.tmp}/lnk/../task"}), timeout=60)
        self.assertEqual((done.returncode, done.stdout), (3, ""))
        self.assertFalse((other / "task" / calls.LOG_NAME).exists())

    def test_n2_n3_an_operator_command_runs_unlogged_only_at_a_terminal(self):
        ran = []
        for tty, outcome in ((True, "runs"), (False, "refused")):
            with mock.patch.object(calls, "on_terminal", return_value=tty):
                if outcome == "runs":
                    got = calls.call("t", {}, lambda: ran.append(1) or calls.Answer("ok", "x"), operator=True, env={})
                    self.assertEqual(got.status, "ok")
                else:
                    with self.assertRaisesRegex(calls.LogUnavailable, "terminal"):
                        calls.call("t", {}, lambda: ran.append(2), operator=True, env={})
        self.assertEqual(ran, [1])

    def test_n2_n3_list_show_audit_and_status_give_no_answer_without_a_terminal_and_without_a_task_dir(self):
        base = [sys.executable, str(TP_CTX), "--project", str(self.project)]
        for words in (("knowledge", "list"), ("knowledge", "show", "kn-2026-10-31-x"), ("knowledge", "audit"), ("status",)):
            done = subprocess.run(base + list(words), capture_output=True, text=True, env=clean_env(), timeout=60, stdin=subprocess.DEVNULL)
            self.assertEqual((done.returncode, done.stdout), (3, ""), (words, done.stderr))
            self.assertIn("terminal", done.stderr, words)
        self.assertFalse((self.project / ".tmux-pi" / "knowledge").exists())

    def test_n3_status_is_an_operator_command_that_a_person_at_a_terminal_may_run_unlogged(self):
        code, shown, errors = run_pty([sys.executable, str(TP_CTX), "--project", str(self.project), "status"], clean_env(), str(self.tmp))
        self.assertEqual(code, 0, errors)
        self.assertIn("engine: 1", shown)

    def test_n2_a_person_at_a_terminal_still_runs_list_unlogged(self):
        code, shown, errors = run_pty([sys.executable, str(TP_CTX), "--project", str(self.project), "knowledge", "list"], clean_env(),
                                      str(self.tmp))
        self.assertEqual((code, shown.strip()), (0, "no notes"), errors)

    def test_n7_the_log_option_says_it_is_for_an_operator_at_a_terminal(self):
        done = subprocess.run([sys.executable, str(TP_CTX), "--help"], capture_output=True, text=True, env=clean_env(), timeout=60)
        flat = " ".join(done.stdout.split())
        self.assertRegex(flat, r"--log LOG\s+call log file \(default.*\)[^-]*operator[^-]*terminal")


# ====================================================================================================== the surface itself

FIX = ROOT / "tests" / "fixtures" / "ctx_surface"
sys.path.insert(0, str(FIX))
import fake_navs  # noqa: E402

try:
    from ctxengine import store, surface  # noqa: E402
except ImportError:  # the failing-first run: the module does not exist yet
    from ctxengine import store
    surface = None

AWS = "AKIA" + "IOSFODNN7EXAMPLE"
ORDERS, MANY, CART, ORDER = fake_navs.ORDERS, fake_navs.MANY, fake_navs.CART, fake_navs.ORDER
FAKE_MODULES = {"web": "fake_navs", "api": "fake_navs"}
PINS = {"zod": "4.6.7"}


def copy_project(tmp):
    project = tmp / "project"
    shutil.copytree(FIX / "project", project)
    (project / ".tmux-pi").mkdir()
    (project / ".env").write_text("SECRET=do-not-read\n")
    (project / "config").mkdir()
    (project / "config" / "local.json").write_text("{}")
    return project


def add_approved(layout, ident, title, scope, body, tags=(), evidence=(f"{ORDERS}:5",)):
    note = store.Note(id=ident, kind="convention", scope=list(scope), source="human", body=body, title=title, tags=list(tags),
                      evidence=list(evidence), status="approved", created="2026-10-01", last_reviewed="2026-10-31")
    note.evidence_hashes = {}
    directory = layout.directory("approved")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{ident}.md").write_text(store.render(note))


class SurfaceCase(unittest.TestCase):
    modules = FAKE_MODULES
    task_env = False

    def setUp(self):
        self.assertIsNotNone(surface, "lib/ctxengine/surface.py does not exist yet")
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name).resolve()
        self.project = copy_project(self.tmp)
        self.layout = store.Layout(self.project)
        fake_navs.reset()
        self.addCleanup(fake_navs.reset)

    def engine(self, modules="default", pin=None, env=None):
        modules = self.modules if modules == "default" else modules
        navs = surface.Navigators(self.layout, modules=modules)
        env = {"TP_CTX_TODAY": "2026-10-31", **(env or {})}
        engine = surface.Engine(self.layout, navigators=navs, pin=pin, env=env)
        self.addCleanup(engine.close)
        return engine

    def run_tool(self, tool, args, **kw):
        return self.engine(**kw).run(tool, args)


VALID = {
    "ctx_brief": {"paths": [ORDERS]},
    "ctx_symbol": {"name": "createOrder"},
    "ctx_relations": {"target": ORDERS, "direction": "importers"},
    "ctx_knowledge": {},
    "ctx_note": {"kind": "lesson", "scope": ["apps/**"], "text": "Always parse input.", "evidence": [f"{ORDERS}:6"]},
}
ARGUMENT_CASES = [  # (tool, arguments, the problem that must be named, or None for valid)
    ("ctx_brief", {"paths": [ORDERS]}, None),
    ("ctx_brief", {"paths": [ORDERS] * 5}, None),
    ("ctx_brief", {}, "paths is required"),
    ("ctx_brief", {"paths": []}, "paths needs 1 to 5"),
    ("ctx_brief", {"paths": [ORDERS] * 6}, "paths needs 1 to 5"),
    ("ctx_brief", {"paths": ORDERS}, "paths must be a list"),
    ("ctx_brief", {"paths": [1]}, "paths must be a list of strings"),
    ("ctx_brief", {"paths": [""]}, "paths must be a list of strings"),
    ("ctx_brief", {"paths": [ORDERS], "extra": 1}, "unknown argument 'extra'"),
    ("ctx_symbol", {"name": "x"}, None),
    ("ctx_symbol", {"name": "x", "path": ORDERS, "package": "api"}, None),
    ("ctx_symbol", {}, "name is required"),
    ("ctx_symbol", {"name": ""}, "name must be 1 to 100"),
    ("ctx_symbol", {"name": "a b"}, "name must be a bare identifier"),
    ("ctx_symbol", {"name": "x", "package": "cobol"}, "package must be one of web, api, mc, lib"),
    ("ctx_symbol", {"name": "x", "path": 5}, "path must be a string"),
    ("ctx_symbol", {"name": 7}, "name must be a string"),
    ("ctx_relations", {"target": ORDERS, "direction": "imports", "limit": 25}, None),
    ("ctx_relations", {"target": ORDERS}, "direction is required"),
    ("ctx_relations", {"direction": "imports"}, "target is required"),
    ("ctx_relations", {"target": ORDERS, "direction": "up"}, "direction must be one of importers, imports, contracts"),
    ("ctx_relations", {"target": ORDERS, "direction": "imports", "limit": 0}, "limit must be a whole number from 1 to 25"),
    ("ctx_relations", {"target": ORDERS, "direction": "imports", "limit": 26}, "limit must be a whole number from 1 to 25"),
    ("ctx_relations", {"target": ORDERS, "direction": "imports", "limit": True}, "limit must be a whole number from 1 to 25"),
    ("ctx_relations", {"target": ORDERS, "direction": "imports", "limit": "5"}, "limit must be a whole number from 1 to 25"),
    ("ctx_relations", {"target": ORDERS, "direction": "imports", "limit": 2.5}, "limit must be a whole number from 1 to 25"),
    ("ctx_knowledge", {}, None),
    ("ctx_knowledge", {"query": "q", "paths": [ORDERS], "tags": ["t"], "include_candidates": True, "limit": 10}, None),
    ("ctx_knowledge", {"limit": 11}, "limit must be a whole number from 1 to 10"),
    ("ctx_knowledge", {"include_candidates": "yes"}, "include_candidates must be true or false"),
    ("ctx_knowledge", {"tags": ["a"] * 6}, "tags needs at most 5"),
    ("ctx_knowledge", {"query": "x" * 201}, "query must be at most 200"),
    ("ctx_knowledge", {"paths": [ORDERS] * 6}, "paths needs at most 5"),
    ("ctx_note", VALID["ctx_note"], None),
    ("ctx_note", {**VALID["ctx_note"], "text": "x" * 801}, "text must be 1 to 800"),
    ("ctx_note", {**VALID["ctx_note"], "text": ""}, "text must be 1 to 800"),
    ("ctx_note", {**VALID["ctx_note"], "evidence": []}, "evidence needs 1 to 10"),
    ("ctx_note", {**VALID["ctx_note"], "kind": "poem"}, "kind must be one of convention, framework, relation, decision, lesson"),
    ("ctx_note", {**VALID["ctx_note"], "scope": []}, "scope needs 1 to 10"),
    ("ctx_note", {k: v for k, v in VALID["ctx_note"].items() if k != "kind"}, "kind is required"),
]


class Arguments(SurfaceCase):
    def test_every_row_of_the_argument_table_is_accepted_or_names_its_problem(self):
        for tool, args, problem in ARGUMENT_CASES:
            clean, problems = surface.validate(tool, args)
            if problem is None:
                self.assertEqual(problems, [], (tool, args))
            else:
                self.assertTrue(any(problem in p for p in problems), (tool, args, problems))

    def test_a_bad_argument_gives_status_invalid_the_problem_and_a_next_step_and_runs_no_navigator(self):
        for tool, args, problem in ARGUMENT_CASES:
            if problem is None:
                continue
            answer = self.run_tool(tool, args)
            self.assertEqual(answer.status, "invalid", (tool, args))
            self.assertIn(problem, answer.text)
            self.assertRegex(answer.text, r"Next: ", (tool, args))
            self.assertIn(tool + "(", answer.text)  # the usage line
        self.assertEqual(fake_navs.CALLS, [])

    def test_an_unknown_tool_or_a_non_object_input_is_invalid_not_an_error(self):
        engine = self.engine()
        self.assertEqual(engine.run("ctx_nope", {}).status, "invalid")
        self.assertEqual(engine.run("ctx_brief", ["a"]).status, "invalid")

    def test_the_schema_table_names_the_five_tools_and_their_limits(self):
        self.assertEqual(surface.TOOLS, ("ctx_brief", "ctx_symbol", "ctx_relations", "ctx_knowledge", "ctx_note"))
        self.assertEqual(surface.CAP, 6144)
        self.assertEqual(surface.SPECS["ctx_relations"]["limit"]["max"], 25)
        self.assertEqual(surface.SPECS["ctx_brief"]["paths"]["max"], 5)


class Boundaries(SurfaceCase):
    def test_a_path_outside_the_project_or_a_sensitive_one_is_refused_and_no_navigator_hears_of_it(self):
        for value in (".env", "config/local.json", "../outside.ts", "/etc/passwd", "apps/../../x.ts"):
            for tool, args in (("ctx_brief", {"paths": [value]}), ("ctx_symbol", {"name": "x", "path": value}),
                               ("ctx_relations", {"target": value, "direction": "imports"}), ("ctx_knowledge", {"paths": [value]}),
                               ("ctx_note", {**VALID["ctx_note"], "evidence": [f"{value}:1"]})):
                answer = self.run_tool(tool, args)
                self.assertEqual(answer.status, "refused", (tool, value, answer.text))
                self.assertIn("refused", answer.text)
                self.assertNotIn("do-not-read", answer.text)
        self.assertEqual(fake_navs.CALLS, [])

    def test_a_symlink_that_leaves_the_project_is_refused(self):
        outside = self.tmp / "outside.ts"
        outside.write_text("export const x = 1;\n")
        (self.project / "apps" / "link.ts").symlink_to(outside)
        answer = self.run_tool("ctx_brief", {"paths": ["apps/link.ts"]})
        self.assertEqual(answer.status, "refused")
        self.assertEqual(fake_navs.CALLS, [])

    def test_output_passes_the_redaction_screen(self):
        add_approved(self.layout, "kn-2026-10-01-leaky", "Leaky note", ["apps/**"], f"The key is {AWS} here.")
        answer = self.run_tool("ctx_knowledge", {"query": "leaky"})
        self.assertNotIn(AWS, answer.text)


class Navigators(SurfaceCase):
    def test_stubbed_navigators_answer_unavailable_with_a_next_step_for_every_navigator_tool(self):
        for tool, args in (("ctx_symbol", {"name": "createOrder", "path": ORDERS}),
                           ("ctx_relations", {"target": ORDERS, "direction": "importers"}),
                           ("ctx_relations", {"target": CART, "direction": "imports"})):
            answer = self.run_tool(tool, args, modules={})
            self.assertEqual(answer.status, "unavailable", (tool, args))
            self.assertRegex(answer.text, r"Next: \S", answer.text)
            self.assertIn("stub", answer.text)

    def test_the_brief_with_stubbed_navigators_is_unavailable_but_still_carries_what_needs_no_navigator(self):
        add_approved(self.layout, "kn-2026-10-01-parse", "Parse input first", ["apps/launchpad/api/**"], "Always parse.")
        answer = self.run_tool("ctx_brief", {"paths": [ORDERS]}, modules={}, pin=PINS.get)
        self.assertEqual(answer.status, "unavailable")
        for expected in ("orders.test.ts", "kn-2026-10-01-parse", "zod 4.6.7", "api_signature(zod", "lines: 13", "Next: "):
            self.assertIn(expected, answer.text)

    def test_the_knowledge_tools_work_with_stubbed_navigators(self):
        self.assertEqual(self.run_tool("ctx_knowledge", {}, modules={}).status, "ok")

    def test_a_navigator_refusal_is_surfaced_with_its_reason_as_given(self):
        fake_navs.BEHAVIOUR[("api", "importers")] = {"status": "refused", "reason": "reaches_refused_path", "next_step": "ask about a file that imports nothing refused"}
        fake_navs.BEHAVIOUR[("web", "imports")] = {"status": "refused", "message": "path refused (sensitive_path)", "next": "ask about a file inside the project"}
        fake_navs.BEHAVIOUR[("api", "symbols")] = {"status": "refused", "reason": "sandbox_unavailable", "detail": "Deno is not run without the sandbox",
                                                   "next_step": "run on macOS"}
        api = self.run_tool("ctx_relations", {"target": ORDERS, "direction": "importers"})
        self.assertEqual(api.status, "refused")
        self.assertIn("reaches_refused_path", api.text)
        self.assertIn("ask about a file that imports nothing refused", api.text)
        web = self.run_tool("ctx_relations", {"target": CART, "direction": "imports"})
        self.assertEqual(web.status, "refused")
        self.assertIn("path refused (sensitive_path)", web.text)
        symbols = self.run_tool("ctx_symbol", {"name": "createOrder", "path": ORDERS})
        self.assertEqual(symbols.status, "refused")
        self.assertIn("sandbox_unavailable", symbols.text)
        self.assertIn("Deno is not run without the sandbox", symbols.text)

    def test_each_navigator_status_maps_to_the_vocabulary_and_keeps_its_own_word(self):
        table = [("warming", "unavailable"), ("timeout", "unavailable"), ("crashed", "unavailable"), ("unavailable", "unavailable"),
                 ("no_typescript", "no_project"), ("not_in_project", "no_project"), ("no_project", "no_project"),
                 ("bad_request", "invalid"), ("bad_argument", "invalid"), ("stale_index", "stale_index"), ("error", "error"),
                 ("something_new", "error")]
        for raw, expected in table:
            fake_navs.BEHAVIOUR[("web", "imports")] = {"status": raw, "message": f"the {raw} message", "next": f"the {raw} next step"}
            answer = self.run_tool("ctx_relations", {"target": CART, "direction": "imports"})
            self.assertEqual(answer.status, expected, raw)
            self.assertIn(f"the {raw} message", answer.text, raw)
            self.assertIn(f"the {raw} next step", answer.text, raw)
            self.assertIn(raw, answer.text, raw)
        fake_navs.BEHAVIOUR[("web", "imports")] = {"status": "stale_index"}
        self.assertRegex(self.run_tool("ctx_relations", {"target": CART, "direction": "imports"}).text, r"Next: \S")  # a default step

    def test_a_navigator_that_raises_is_unavailable_not_a_traceback(self):
        def boom(args):
            raise RuntimeError("helper exploded")
        fake_navs.BEHAVIOUR[("web", "imports")] = boom
        answer = self.run_tool("ctx_relations", {"target": CART, "direction": "imports"})
        self.assertIn(answer.status, ("unavailable", "error"))
        self.assertNotIn("Traceback", answer.text)
        self.assertRegex(answer.text, r"Next: \S")

    def test_a_real_navigator_module_drops_in_with_no_change_to_the_surface(self):
        engine = self.engine()
        answer = engine.run("ctx_relations", {"target": ORDERS, "direction": "importers", "limit": 3})
        self.assertEqual(answer.status, "ok")
        kinds = {kind: args for kind, args in fake_navs.INSTANCES}
        self.assertEqual(os.path.realpath(kinds["api"]["project_root"]), os.path.realpath(self.project))
        self.assertTrue(kinds["api"]["root"].endswith("apps/launchpad/api"))
        self.assertIs(kinds["api"]["check_path"], surface.redact.check_path)
        self.assertIs(kinds["api"]["guard"], surface.redact.guard)
        engine.run("ctx_relations", {"target": CART, "direction": "imports"})
        web = [args for kind, args in fake_navs.INSTANCES if kind == "web"][0]
        self.assertTrue(web["tsconfig"].endswith("apps/launchpad/web/tsconfig.json"))
        self.assertIsNotNone(web["guard"].check_path(str(self.project / ".env"), str(self.project)))  # the CX2 guard shape: a reason
        self.assertIsNone(web["guard"].check_path(str(self.project / CART), str(self.project)))
        self.assertNotIn(AWS, web["guard"].screen(f"key {AWS}"))
        engine.close()
        self.assertTrue(all(args["self"].stopped for _, args in fake_navs.INSTANCES))

    def test_a_module_that_is_not_there_or_will_not_start_is_a_stub_not_a_crash(self):
        answer = self.run_tool("ctx_relations", {"target": ORDERS, "direction": "importers"}, modules={"web": "no_such_module_here", "api": "no_such_module_here"})
        self.assertEqual(answer.status, "unavailable")

    def test_the_stub_file_has_the_interfaces_of_the_real_navigators(self):
        from ctxengine import nav_stub
        for name in ("NavWeb", "NavApi"):
            stub, fake = getattr(nav_stub, name), getattr(fake_navs, name)
            for method in {"NavWeb": ("definition", "references", "exports", "imports", "stats"),
                           "NavApi": ("importers", "imports", "symbols", "forward_graph", "status")}[name]:
                self.assertTrue(hasattr(stub, method), (name, method))
            for method in ("exports", "imports", "definition", "references", "importers", "symbols"):
                if hasattr(fake, method) and hasattr(stub, method):
                    self.assertEqual(list(inspect.signature(getattr(stub, method)).parameters), list(inspect.signature(getattr(fake, method)).parameters),
                                     (name, method))
        answer = nav_stub.NavWeb("/x", "tsconfig.json").exports("a.ts")
        self.assertEqual(answer["status"], "unavailable")
        self.assertTrue(answer["next"])
        self.assertTrue(nav_stub.NavApi("/x").importers("a.ts")["next_step"])


class Brief(SurfaceCase):
    def setUp(self):
        super().setUp()
        add_approved(self.layout, "kn-2026-10-01-parse", "Parse input before use", ["apps/launchpad/api/src/orders.ts"], "Always parse. " + "p" * 400,
                     tags=["zod"])
        add_approved(self.layout, "kn-2026-10-02-routes", "Routes are thin", ["apps/launchpad/api/**"], "Keep routes thin.")
        add_approved(self.layout, "kn-2026-10-03-repo", "Repo-wide habit", ["**/*.ts"], "Use strict mode.")
        add_approved(self.layout, "kn-2026-10-04-elsewhere", "Not for orders", ["apps/launchpad/web/**"], "Web only.")

    def brief(self, paths=(ORDERS, CART), **kw):
        return self.run_tool("ctx_brief", {"paths": list(paths)}, pin=PINS.get, **kw)

    def test_the_brief_has_the_five_parts_in_a_fixed_order(self):
        answer = self.brief()
        self.assertEqual(answer.status, "ok", answer.text)
        text = answer.text
        order = [text.index(x) for x in (f"== {ORDERS}", f"== {CART}", "== contracts", "== knowledge", "== libraries")]
        self.assertEqual(order, sorted(order))
        for expected in ("createOrder", "cancelOrder", "orders.test.ts", "cart.spec.ts", "lines: 13", "kind: source"):
            self.assertIn(expected, text)

    def test_the_brief_is_byte_identical_across_runs_and_engines(self):
        first, second = self.brief().text, self.brief().text
        self.assertEqual(first.encode(), second.encode())
        fake_navs.reset()
        self.assertEqual(self.brief(paths=(CART, ORDERS)).text.encode(), self.brief(paths=(CART, ORDERS)).text.encode())

    def test_the_brief_matches_the_golden_answer(self):
        text = self.brief().text
        golden = FIX / "golden" / "brief_orders_cart.txt"
        if os.environ.get("CTX_UPDATE_GOLDEN") == "1":
            golden.write_text(text)
        self.assertEqual(text, golden.read_text())

    def test_the_brief_caps_importers_imports_exports_contracts_and_notes_and_says_what_it_left_out(self):
        answer = self.brief(paths=(ORDERS,))
        text = answer.text
        self.assertEqual(len(re.findall(r"routes/r\d\d\.ts:3", text)), 10)
        self.assertIn("r10.ts:3", text)
        self.assertNotIn("r11.ts", text)
        self.assertTrue(any("importers" in item and "4" in item for item in answer.data["left_out"]), answer.data["left_out"])
        many = self.brief(paths=(MANY,))
        self.assertIn("s20", many.text)
        self.assertNotIn("s21", many.text)
        self.assertTrue(any("exports" in item for item in many.data["left_out"]))
        consumers = re.findall(r"^  (?:apps|lib)/\S+:\d+$", text.split("== contracts")[1].split("== knowledge")[0], re.M)
        self.assertLessEqual(len(consumers), 8)
        for ident in range(10):
            add_approved(self.layout, f"kn-2026-10-1{ident}-more", f"More {ident}", ["apps/launchpad/api/**"], "More.")
        self.assertEqual(len(self.brief(paths=(ORDERS,)).notes_returned), 5)

    def test_notes_are_ranked_by_specificity_and_cut_at_300_characters_with_their_titles(self):
        answer = self.brief(paths=(ORDERS,))
        self.assertEqual(answer.notes_returned, ["kn-2026-10-01-parse", "kn-2026-10-02-routes", "kn-2026-10-03-repo"])
        line = [row for row in answer.text.splitlines() if "kn-2026-10-01-parse" in row][0]
        self.assertIn("Parse input before use", line)
        self.assertLessEqual(len(line.split(" — ", 1)[1]), 300 + 3)
        self.assertNotIn("kn-2026-10-04-elsewhere", answer.text)

    def test_library_hints_name_the_pinned_version_or_say_it_could_not_be_read(self):
        text = self.brief().text
        self.assertIn("zod 4.6.7", text)
        self.assertIn("use api_signature(zod, <symbol>)", text)
        self.assertRegex(text, r"react \(version not read[^)]*\)")
        self.assertNotIn("slug", text.split("== libraries")[1])  # a relative import is not a library

    def test_a_file_the_navigator_does_not_cover_still_gets_a_brief(self):
        (self.project / "bin").mkdir()
        (self.project / "bin" / "tool.py").write_text("print('hi')\n")
        answer = self.brief(paths=("bin/tool.py",))
        self.assertIn("lines: 1", answer.text)
        self.assertIn("no package covers this path", answer.text)

    def test_a_missing_file_is_named_not_guessed(self):
        answer = self.brief(paths=("apps/launchpad/api/src/ghost.ts",))
        self.assertIn("no such file", answer.text)

    def test_stale_notes_carry_their_flags(self):
        add_approved(self.layout, "kn-2026-10-05-gone", "Dead scope", ["apps/launchpad/api/src/orders.ts", "gone/**"], "Dead.")
        self.assertIn("stale_by_path", self.brief(paths=(ORDERS,)).text)


class Operations(SurfaceCase):
    def test_symbol_with_a_path_gives_definition_signature_trimmed_doc_and_five_sample_references(self):
        answer = self.run_tool("ctx_symbol", {"name": "addToCart", "path": CART})
        self.assertEqual(answer.status, "ok", answer.text)
        self.assertIn(f"{CART}:5", answer.text)
        self.assertIn("function addToCart(item: unknown)", answer.text)
        doc = [row for row in answer.text.splitlines() if row.strip().startswith("doc:")][0]
        self.assertLessEqual(len(doc.split("doc:", 1)[1].strip()), 400)
        self.assertIn("7 references", answer.text)
        self.assertEqual(len(re.findall(r"apps/launchpad/web/src/p\d\d\.ts:1", answer.text)), 5)

    def test_symbol_without_a_path_is_located_by_a_deterministic_text_scan(self):
        first = self.run_tool("ctx_symbol", {"name": "addToCart"})
        self.assertEqual(first.status, "ok", first.text)
        self.assertIn(f"{CART}:5", first.text)
        self.assertEqual(first.text, self.run_tool("ctx_symbol", {"name": "addToCart"}).text)
        self.assertIn("no definition", self.run_tool("ctx_symbol", {"name": "addToCart", "package": "api"}).text)
        self.assertIn("no definition", self.run_tool("ctx_symbol", {"name": "neverDefined"}).text)

    def test_symbol_on_an_api_file_comes_from_the_symbol_list_and_says_references_come_from_relations(self):
        answer = self.run_tool("ctx_symbol", {"name": "createOrder", "path": ORDERS})
        self.assertIn(f"{ORDERS}:5", answer.text)
        self.assertIn("export function createOrder(input: unknown) {", answer.text)
        self.assertIn("ctx_relations", answer.text)

    def test_symbol_shows_at_most_five_definitions(self):
        rows = [{"path": CART, "line": n, "column": 1, "name": "dup", "kind": "function", "container": "", "text": f"function dup{n}"} for n in range(1, 8)]
        fake_navs.BEHAVIOUR[("web", "definition")] = {"status": "ok", "definitions": rows, "signature": "", "doc": ""}
        answer = self.run_tool("ctx_symbol", {"name": "dup", "path": CART})
        self.assertEqual(len(re.findall(rf"^{re.escape(CART)}:\d+ ", answer.text, re.M)), 5)
        self.assertTrue(any("definitions" in item for item in answer.data["left_out"]))

    def test_relations_importers_imports_and_contracts(self):
        importers = self.run_tool("ctx_relations", {"target": ORDERS, "direction": "importers", "limit": 3})
        self.assertEqual(len(re.findall(r"^  \S+:\d+ -> \S+$", importers.text, re.M)), 3, importers.text)
        self.assertIn("apps/launchpad/api/src/routes/r01.ts:3 -> " + ORDERS, importers.text)
        self.assertTrue(any("importers" in item for item in importers.data["left_out"]))
        imports = self.run_tool("ctx_relations", {"target": ORDERS, "direction": "imports"})
        self.assertIn(f"{ORDERS}:2 -> {ORDER}", imports.text)
        self.assertIn("zod", imports.text)
        contracts = self.run_tool("ctx_relations", {"target": ORDERS, "direction": "contracts"})
        self.assertIn(ORDER, contracts.text)
        self.assertIn(CART, contracts.text)
        self.assertNotIn(f"{ORDERS}:2 -> {ORDER}\n", contracts.text.split(ORDER, 1)[1])  # the target itself is not its own consumer

    def test_relations_groups_by_package_and_orders_edges_stably(self):
        text = self.run_tool("ctx_relations", {"target": ORDER, "direction": "importers"}).text
        self.assertLess(text.index("[web]"), text.index("[api]"))
        self.assertEqual(text, self.run_tool("ctx_relations", {"target": ORDER, "direction": "importers"}).text)

    def test_relations_takes_a_symbol_and_names_the_file_it_resolved_to(self):
        answer = self.run_tool("ctx_relations", {"target": "addToCart", "direction": "importers"})
        self.assertEqual(answer.status, "ok", answer.text)
        self.assertIn(f"target resolved to {CART}", answer.text)

    def test_knowledge_filters_ranks_limits_and_leaves_superseded_notes_out(self):
        add_approved(self.layout, "kn-2026-10-01-a", "Orders parse", ["apps/launchpad/api/src/orders.ts"], "Parse orders.", tags=["zod"])
        add_approved(self.layout, "kn-2026-10-02-b", "Routes", ["apps/launchpad/api/**"], "Thin routes.", tags=["routes"])
        by_path = self.run_tool("ctx_knowledge", {"paths": [ORDERS]})
        self.assertEqual(by_path.notes_returned, ["kn-2026-10-01-a", "kn-2026-10-02-b"])
        self.assertEqual(self.run_tool("ctx_knowledge", {"tags": ["routes"]}).notes_returned, ["kn-2026-10-02-b"])
        self.assertEqual(self.run_tool("ctx_knowledge", {"query": "parse"}).notes_returned, ["kn-2026-10-01-a"])
        self.assertEqual(len(self.run_tool("ctx_knowledge", {"limit": 1}).notes_returned), 1)
        self.assertIn("No matching notes", self.run_tool("ctx_knowledge", {"query": "zzzz"}).text)

    def test_knowledge_shows_candidates_only_when_asked_and_counts_the_ones_it_hid(self):
        candidate = store.Note(id="kn-2026-10-06-cand", kind="lesson", scope=["apps/**"], source="worker:x", body="Candidate wisdom.",
                               evidence=[f"{ORDERS}:5"], status="candidate", created="2026-10-31")
        directory = self.layout.directory("candidate")
        directory.mkdir(parents=True)
        (directory / "kn-2026-10-06-cand.md").write_text(store.render(candidate))
        hidden = self.run_tool("ctx_knowledge", {"query": "wisdom"})
        self.assertEqual(hidden.notes_returned, [])
        self.assertIn("1 candidate", hidden.text)
        shown = self.run_tool("ctx_knowledge", {"query": "wisdom", "include_candidates": True})
        self.assertEqual(shown.notes_returned, ["kn-2026-10-06-cand"])
        self.assertIn("candidate", shown.text)

    def test_knowledge_flags_a_note_whose_pinned_version_no_longer_matches(self):
        note = store.Note(id="kn-2026-10-07-ver", kind="framework", scope=["apps/**"], source="human", body="Zod 3 only.", versions={"zod": ">=3 <4"},
                          evidence=[f"{ORDERS}:1"], status="approved", created="2026-10-01", last_reviewed="2026-10-31")
        self.layout.directory("approved").mkdir(parents=True, exist_ok=True)
        (self.layout.directory("approved") / "kn-2026-10-07-ver.md").write_text(store.render(note))
        self.assertIn("stale_by_version", self.run_tool("ctx_knowledge", {}, pin=PINS.get).text)
        self.assertIn("version not checked", self.run_tool("ctx_knowledge", {}).text)

    def test_note_writes_a_candidate_only_with_hashed_evidence_and_a_worker_source(self):
        engine = self.engine(env={"TP_TASK_ID": "coder-x"})
        answer = engine.run("ctx_note", VALID["ctx_note"])
        self.assertEqual(answer.status, "ok", answer.text)
        self.assertEqual(answer.data["status"], "candidate")
        ident = answer.data["id"]
        written = self.layout.path("candidate", ident)
        self.assertTrue(written.is_file())
        self.assertFalse(self.layout.path("approved", ident).exists())
        text = written.read_text()
        self.assertIn("worker:coder-x", text)
        self.assertIn("evidence_hashes", text)
        self.assertEqual(answer.notes_returned, [ident])
        again = engine.run("ctx_note", VALID["ctx_note"])  # the same text never overwrites
        self.assertEqual(again.status, "ok")
        self.assertNotEqual(again.data["id"], ident)
        self.assertEqual(written.read_text(), text)

    def test_note_refuses_a_secret_and_rejects_bad_evidence_and_scope(self):
        before = list(self.layout.knowledge.glob("**/*.md")) if self.layout.knowledge.exists() else []
        secret = self.run_tool("ctx_note", {**VALID["ctx_note"], "text": f"Use {AWS} always."})
        self.assertEqual(secret.status, "refused")
        self.assertNotIn(AWS, secret.text)
        for evidence in (["no-line-number"], ["apps/launchpad/api/src/orders.ts:999"], ["apps/launchpad/api/src/ghost.ts:1"]):
            answer = self.run_tool("ctx_note", {**VALID["ctx_note"], "evidence": evidence})
            self.assertEqual(answer.status, "invalid", evidence)
            self.assertRegex(answer.text, r"Next: \S")
        bad_scope = self.run_tool("ctx_note", {**VALID["ctx_note"], "scope": ["../x"]})
        self.assertEqual(bad_scope.status, "invalid")
        after = list(self.layout.knowledge.glob("**/*.md")) if self.layout.knowledge.exists() else []
        self.assertEqual(before, after)

    def test_note_allows_five_per_task_and_the_sixth_is_refused_with_a_next_step(self):
        task = self.tmp / "task"
        task.mkdir()
        env = {"TP_TASK_DIR": str(task), "TP_TASK_ID": "coder-x"}
        for number in range(5):
            done = run_cli(self.project, "ctx_note", {**VALID["ctx_note"], "text": f"Rule number {number}."}, env)
            self.assertEqual(done.returncode, 0, done.stderr)
        sixth = run_cli(self.project, "ctx_note", {**VALID["ctx_note"], "text": "Rule number 6."}, env)
        payload = json.loads(sixth.stdout)
        self.assertEqual((sixth.returncode, payload["status"]), (2, "refused"))
        self.assertIn("5 notes per task", payload["text"])
        self.assertRegex(payload["text"], r"Next: \S")
        self.assertEqual(len(list(self.layout.directory("candidate").glob("*.md"))), 5)
        other = run_cli(self.project, "ctx_note", {**VALID["ctx_note"], "text": "Another task."}, {**env, "TP_TASK_DIR": str(self.tmp / "task2")})
        self.assertEqual(other.returncode, 3)  # that task directory does not exist


class Cap(SurfaceCase):
    def test_a_hundred_random_inputs_never_exceed_the_cap_and_big_answers_say_what_was_cut(self):
        for number in range(30):
            add_approved(self.layout, f"kn-2026-10-{number % 28 + 1:02d}-big-{number}", f"Big note {number}", ["apps/**"], ("word " * 240).strip(),
                         tags=["big"])
        rng = random.Random(4)
        paths = [ORDERS, CART, ORDER, MANY, "apps/launchpad/api/src/util.ts", "apps/launchpad/api/src/ghost.ts", "bin/none.py"]
        names = ["createOrder", "addToCart", "OrderSchema", "nothing", "s01"]
        truncated = 0
        for case in range(100):
            tool = rng.choice(surface.TOOLS[:4])
            if tool == "ctx_brief":
                args = {"paths": rng.sample(paths, rng.randint(1, 5))}
            elif tool == "ctx_symbol":
                args = {"name": rng.choice(names), **({"path": rng.choice(paths)} if rng.random() < 0.5 else {})}
            elif tool == "ctx_relations":
                args = {"target": rng.choice(paths + names), "direction": rng.choice(["importers", "imports", "contracts"]),
                        **({"limit": rng.randint(1, 25)} if rng.random() < 0.7 else {})}
            else:
                args = {"limit": rng.randint(1, 10), **({"query": rng.choice(["word", "big", "note"])} if rng.random() < 0.7 else {})}
            answer = self.run_tool(tool, args)
            self.assertLessEqual(len(answer.text.encode("utf-8")), surface.CAP, (case, tool, args))
            self.assertIn(answer.status, surface.calls.STATUSES)
            if answer.data["truncated"]:
                truncated += 1
                self.assertTrue(answer.data["left_out"], (tool, args))
                self.assertIn("left out", answer.text)
        self.assertGreater(truncated, 3)

    def test_the_cap_holds_for_a_single_enormous_line_and_for_multibyte_text(self):
        add_approved(self.layout, "kn-2026-10-01-wide", "日本語のノート", ["apps/**"], "語" * 400)
        for number in range(12):
            add_approved(self.layout, f"kn-2026-10-02-wide{number}", "語" * 90, ["apps/**"], "語" * 400)
        answer = self.run_tool("ctx_knowledge", {"limit": 10})
        self.assertLessEqual(len(answer.text.encode("utf-8")), 6144)
        answer.text.encode("utf-8").decode("utf-8")
        self.assertTrue(answer.data["truncated"])

    def test_the_cap_measure_survives_the_command_line_json(self):
        for number in range(30):
            add_approved(self.layout, f"kn-2026-10-03-cli{number}", f"Cli note {number}", ["apps/**"], ("word " * 240).strip())
        task = self.tmp / "task"
        task.mkdir()
        done = run_cli(self.project, "ctx_knowledge", {"limit": 10}, {"TP_TASK_DIR": str(task)})
        payload = json.loads(done.stdout)
        self.assertLessEqual(len(payload["text"].encode("utf-8")), 6144)
        self.assertEqual(sorted(payload), ["left_out", "status", "text", "truncated"])


def run_cli(project, tool, args, env, raw=None):
    full = clean_env({"TP_CTX_TODAY": "2026-10-31", "TP_CTX_NAV_MODULES": "fake_navs", "PYTHONPATH": str(FIX), **env})
    return subprocess.run([sys.executable, str(TP_CTX), "--project", str(project), "--json", tool, "--input", raw if raw is not None else json.dumps(args)],
                          capture_output=True, text=True, env=full, timeout=120, stdin=subprocess.DEVNULL)


class CommandLine(SurfaceCase):
    def test_every_tool_gives_no_answer_without_a_task_dir_even_at_a_terminal(self):
        for tool, args in VALID.items():
            done = run_cli(self.project, tool, args, {})
            self.assertEqual((done.returncode, done.stdout), (3, ""), (tool, done.stderr))
            self.assertIn("TP_TASK_DIR", done.stderr)
        code, shown, errors = run_pty([sys.executable, str(TP_CTX), "--project", str(self.project), "--json", "ctx_knowledge", "--input", "{}"], clean_env(),
                                      str(self.tmp))
        self.assertEqual(code, 3, errors)
        self.assertNotIn('"status"', shown)
        self.assertFalse(self.layout.knowledge.exists())

    def test_every_call_is_logged_with_its_status_bytes_and_notes_and_goes_through_the_logged_path(self):
        add_approved(self.layout, "kn-2026-10-01-parse", "Parse", ["apps/**"], "Parse.")
        task = self.tmp / "task"
        task.mkdir()
        for tool, args in VALID.items():
            if tool != "ctx_note":
                done = run_cli(self.project, tool, args, {"TP_TASK_DIR": str(task)})
                self.assertIn(done.returncode, (0, 1), (tool, done.stderr))
        lines = [json.loads(line) for line in (task / "ctx-calls.jsonl").read_text().splitlines()]
        self.assertEqual([line["tool"] for line in lines], ["ctx_brief", "ctx_symbol", "ctx_relations", "ctx_knowledge"])
        knowledge = lines[-1]
        self.assertEqual((knowledge["status"], knowledge["notes_returned"]), ("ok", ["kn-2026-10-01-parse"]))
        self.assertGreater(knowledge["bytes"], 0)

    def test_a_bad_argument_or_bad_json_is_answered_and_logged_with_exit_1(self):
        task = self.tmp / "task"
        task.mkdir()
        env = {"TP_TASK_DIR": str(task)}
        bad = run_cli(self.project, "ctx_relations", {"target": ORDERS, "direction": "sideways"}, env)
        payload = json.loads(bad.stdout)
        self.assertEqual((bad.returncode, payload["status"]), (1, "invalid"))
        self.assertIn("Next: ", payload["text"])
        broken = run_cli(self.project, "ctx_knowledge", None, env, raw="{")
        self.assertEqual((broken.returncode, json.loads(broken.stdout)["status"]), (1, "invalid"))
        statuses = [json.loads(line)["status"] for line in (task / "ctx-calls.jsonl").read_text().splitlines()]
        self.assertEqual(statuses, ["invalid", "invalid"])

    def test_with_the_fake_navigator_module_the_command_line_answers_from_it(self):
        task = self.tmp / "task"
        task.mkdir()
        done = run_cli(self.project, "ctx_relations", {"target": ORDERS, "direction": "importers", "limit": 2}, {"TP_TASK_DIR": str(task)})
        payload = json.loads(done.stdout)
        self.assertEqual(payload["status"], "ok", payload)
        self.assertIn("routes/r01.ts:3 -> " + ORDERS, payload["text"])

    def test_the_command_line_with_no_navigator_module_is_unavailable_and_exits_1(self):
        task = self.tmp / "task"
        task.mkdir()
        full = clean_env({"TP_TASK_DIR": str(task)})
        done = subprocess.run([sys.executable, str(TP_CTX), "--project", str(self.project), "--json", "ctx_relations", "--input",
                               json.dumps({"target": ORDERS, "direction": "importers"})], capture_output=True, text=True, env=full, timeout=120,
                              stdin=subprocess.DEVNULL)
        payload = json.loads(done.stdout)
        self.assertEqual((done.returncode, payload["status"]), (1, "unavailable"))
        self.assertRegex(payload["text"], r"Next: \S")

    def test_the_schema_command_prints_the_specs_the_wrappers_must_match(self):
        task = self.tmp / "task"
        task.mkdir()
        done = run_cli(self.project, "ctx_schema", {}, {"TP_TASK_DIR": str(task)})
        schema = json.loads(json.loads(done.stdout)["text"])
        self.assertEqual(sorted(schema["tools"]), sorted(surface.TOOLS))
        self.assertEqual(schema["cap"], 6144)


if __name__ == "__main__":
    unittest.main()
