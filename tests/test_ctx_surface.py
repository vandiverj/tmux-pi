"""CX4 ctx-tools: the folded-in CX1 review notes (N1-N8), the surface (argument checks, the 6 KB answer, the status vocabulary),
the deterministic brief, the five operations on stubbed and on fake-real navigators, and the call log. No model, no network,
no real navigator: the stubs and small fakes that copy the real answer shapes stand in."""
import fcntl
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
        self.assertTrue(any("importers" in item and "cut" in item for item in answer.data["left_out"]), answer.data["left_out"])
        many = self.brief(paths=(MANY,))
        self.assertIn("s20", many.text)
        self.assertNotIn("s21", many.text)
        self.assertTrue(any("exports" in item for item in many.data["left_out"]))
        consumers = re.findall(r"^  (?:apps|lib)/\S+:\d+$", text.split("== contracts")[1].split("== knowledge")[0], re.M)
        self.assertLessEqual(len(consumers), 8)
        for ident in range(10):
            add_approved(self.layout, f"kn-2026-10-1{ident}-more", f"More {ident}", ["apps/launchpad/api/**"], "More.")
        self.assertEqual(len(self.brief(paths=(ORDERS,)).notes_returned), 5)

    def test_notes_are_ranked_by_specificity_and_the_body_is_cut_at_300_characters_after_the_title(self):
        answer = self.brief(paths=(ORDERS,))
        self.assertEqual(answer.notes_returned, ["kn-2026-10-01-parse", "kn-2026-10-02-routes", "kn-2026-10-03-repo"])
        line = [row for row in answer.text.splitlines() if "kn-2026-10-01-parse" in row][0]
        self.assertIn("Parse input before use", line)
        title, body = line.split(" — ", 1)[1].split(": ", 1)
        self.assertEqual(title, "Parse input before use")
        self.assertEqual(body, "Always parse. " + "p" * 286 + "…")  # the first 300 characters of the body, whatever the title's length
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
        self.assertIn("references of addToCart (5 of 7)", answer.text)
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
        self.assertIn("none can be shown (1 withheld, candidate notes", hidden.text)
        self.assertNotIn("No matching notes", hidden.text)
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
        self.assertEqual(answer.data["result"]["status"], "candidate")
        ident = answer.data["result"]["id"]
        written = self.layout.path("candidate", ident)
        self.assertTrue(written.is_file())
        self.assertFalse(self.layout.path("approved", ident).exists())
        text = written.read_text()
        self.assertIn("worker:coder-x", text)
        self.assertIn("evidence_hashes", text)
        self.assertEqual(answer.notes_returned, [ident])
        again = engine.run("ctx_note", VALID["ctx_note"])  # the same text never overwrites
        self.assertEqual(again.status, "ok")
        self.assertNotEqual(again.data["result"]["id"], ident)
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


# ------------------------------------------------------------------------------------------------------- Review 1 (F1-F11)

API_SRC = "apps/launchpad/api/src"
WEB_SRC = "apps/launchpad/web/src"
SPEC = f"{WEB_SRC}/cart.spec.ts"
GOOD = f"{API_SRC}/routes/r01.ts"
REFUSED_PATHS = ["config/local.json", "../outside.ts", ".env", "apps/link.ts"]  # sensitive, outside, sensitive, a link out of the project
LEAKS = ("local.json", "outside.ts", ".env", "do-not-read", "apps/link.ts")


def put(project, rel, text):
    path = project / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return rel


def api_importers(paths, target=ORDERS, **extra):
    rows = [{"path": p, "line": 1, "spec": "../orders.ts"} for p in paths]
    return {"status": "ok", "target": target, "total": len(rows), "truncated": False, "importers": rows, "index_age_s": 3, "unresolved_total": 0,
            "files_indexed": 40, "scanned": 0, **extra}


def web_edges(file, direction, others, **extra):
    """A web `imports` answer: `others` import `file` (importers), or `file` imports `others` (imports)."""
    edges = [{"from": o, "line": 1, "specifier": "x", "to": file, "depth": 1} if direction == "importers"
             else {"from": file, "line": n, "specifier": "x", "to": o, "depth": 1} for n, o in enumerate(others, 1)]
    return {"status": "ok", "direction": direction, "target": file, "edges": edges, "total": len(edges), "truncated": False, "unresolved": 0, **extra}


class ReviewOne(SurfaceCase):
    def put(self, rel, text):
        return put(self.project, rel, text)


class NavigatorPaths(ReviewOne):
    """F1: every path a navigator returns passes the path policy before it is shown; a refused one is counted, never named."""

    def setUp(self):
        super().setUp()
        outside = self.tmp / "outside.ts"
        outside.write_text("export const x = 1;\n")
        (self.project / "apps" / "link.ts").symlink_to(outside)

    def assertClean(self, answer, counted=None):
        self.assertEqual(answer.status, "ok", answer.text)
        for word in LEAKS:
            self.assertNotIn(word, answer.text, word)
            self.assertNotIn(word, json.dumps(answer.data), word)
        if counted is not None:
            self.assertTrue(any(f"{counted} withheld" in item for item in answer.data["left_out"]), answer.data["left_out"])

    def test_api_importers_are_screened_in_relations_and_in_the_brief(self):
        fake_navs.BEHAVIOUR[("api", "importers")] = lambda a: api_importers([GOOD, *REFUSED_PATHS])
        relations = self.run_tool("ctx_relations", {"target": ORDERS, "direction": "importers"})
        self.assertClean(relations, counted=4)
        self.assertIn(f"{GOOD}:1 -> {ORDERS}", relations.text)
        brief = self.run_tool("ctx_brief", {"paths": [ORDERS]})
        self.assertClean(brief, counted=4)
        self.assertIn(GOOD, brief.text)

    def test_web_importers_and_imports_are_screened_in_relations_and_in_the_brief(self):
        fake_navs.BEHAVIOUR[("web", "imports")] = lambda a: web_edges(a["file"], a["direction"],
                                                                      [SPEC if a["direction"] == "importers" else ORDER, *REFUSED_PATHS])
        for direction in ("importers", "imports", "contracts"):
            self.assertClean(self.run_tool("ctx_relations", {"target": CART, "direction": direction}), counted=4 if direction != "contracts" else None)
        self.assertClean(self.run_tool("ctx_brief", {"paths": [CART]}), counted=4)

    def test_api_imports_are_screened(self):
        files = [{"path": p, "line": n, "spec": "x"} for n, p in enumerate([ORDER, *REFUSED_PATHS], 1)]
        fake_navs.BEHAVIOUR[("api", "imports")] = {"status": "ok", "target": ORDERS, "source": "index", "files": files, "external": [], "unresolved": [],
                                                   "index_age_s": 3, "unresolved_total": 0, "files_indexed": 40, "scanned": 0}
        self.assertClean(self.run_tool("ctx_relations", {"target": ORDERS, "direction": "imports"}), counted=4)
        self.assertClean(self.run_tool("ctx_brief", {"paths": [ORDERS]}), counted=4)

    def test_contract_consumers_are_screened(self):
        fake_navs.BEHAVIOUR[("web", "imports")] = lambda a: web_edges(a["file"], a["direction"], [CART, *REFUSED_PATHS])
        self.assertClean(self.run_tool("ctx_relations", {"target": ORDERS, "direction": "contracts"}))

    def test_a_placeholder_and_the_navigators_own_withheld_count_are_counted_not_shown(self):
        fake_navs.BEHAVIOUR[("api", "importers")] = lambda a: api_importers([GOOD, "<withheld>"])
        answer = self.run_tool("ctx_relations", {"target": ORDERS, "direction": "importers"})
        self.assertNotIn("<withheld>", answer.text)
        self.assertTrue(any("1 withheld" in item for item in answer.data["left_out"]), answer.data["left_out"])
        fake_navs.BEHAVIOUR[("web", "imports")] = lambda a: web_edges(a["file"], "importers", [SPEC], withheld=3)
        answer = self.run_tool("ctx_relations", {"target": CART, "direction": "importers"})
        self.assertTrue(any("3 withheld" in item for item in answer.data["left_out"]), answer.data["left_out"])
        self.assertIn(SPEC, answer.text)

    def test_definitions_and_references_of_a_symbol_are_screened(self):
        rows = [{"path": p, "line": 5, "column": 1, "name": "addToCart", "kind": "function", "container": "", "text": f"function addToCart() // {p}"}
                for p in (CART, *REFUSED_PATHS)]
        fake_navs.BEHAVIOUR[("web", "definition")] = {"status": "ok", "definitions": rows, "signature": "function addToCart()", "doc": ""}
        refs = [{"path": p, "line": 2, "column": 1, "text": "addToCart(x)", "is_definition": False, "is_write": False} for p in (SPEC, *REFUSED_PATHS)]
        fake_navs.BEHAVIOUR[("web", "references")] = {"status": "ok", "definitions": [], "references": refs, "total": len(refs), "truncated": False}
        answer = self.run_tool("ctx_symbol", {"name": "addToCart", "path": CART})
        self.assertEqual(answer.status, "ok", answer.text)
        for word in ("local.json", "outside.ts", ".env", "apps/link.ts"):
            self.assertNotIn(word, answer.text, word)
        self.assertIn(f"{CART}:5", answer.text)
        self.assertIn(SPEC, answer.text)
        self.assertTrue(any("withheld" in item and "definitions" in item for item in answer.data["left_out"]), answer.data["left_out"])
        self.assertTrue(any("withheld" in item and "references" in item for item in answer.data["left_out"]), answer.data["left_out"])

    def test_an_api_symbol_listed_at_a_refused_path_is_not_named(self):
        symbols = [{"name": "createOrder", "kind": "function", "path": p, "line": 5, "signature": "export function createOrder()", "doc": ""}
                   for p in (ORDERS, *REFUSED_PATHS)]
        fake_navs.BEHAVIOUR[("api", "symbols")] = {"status": "ok", "path": ORDERS, "source": "deno_doc", "total": 5, "truncated": False, "symbols": symbols}
        answer = self.run_tool("ctx_symbol", {"name": "createOrder", "path": ORDERS})
        self.assertClean(answer)
        self.assertIn(f"{ORDERS}:5", answer.text)


class ContractMarks(ReviewOne):
    """F2: contract consumers say whether they read or write the contract."""

    def setUp(self):
        super().setUp()
        imp = 'import { OrderSchema } from "../../../../lib/launchpad/types/order";\n'
        self.files = {
            "reader": self.put(f"{WEB_SRC}/reader.ts", imp + "export const read = (x: unknown) => OrderSchema.parse(x);\n"),
            "writer": self.put(f"{WEB_SRC}/writer.ts", imp + "export const Draft = OrderSchema.extend({ note: z.string() });\n"),
            "both": self.put(f"{WEB_SRC}/both.ts", imp + "export const A = OrderSchema.pick({ id: true });\nexport const f = (x: unknown) => OrderSchema.safeParse(x);\n"),
            "uses": self.put(f"{WEB_SRC}/uses.ts", imp + "console.log(OrderSchema);\n"),
            "types": self.put(f"{WEB_SRC}/types.ts", 'import type { Order } from "../../../../lib/launchpad/types/order";\nexport type O = Order;\n'),
            "reexport": self.put(f"{WEB_SRC}/reexport.ts", 'export * from "../../../../lib/launchpad/types/order";\n'),
            "alias": self.put(f"{WEB_SRC}/alias.ts", 'import { OrderSchema as S } from "../../../../lib/launchpad/types/order";\nexport const x = S.omit({ id: true });\n'),
            "namespace": self.put(f"{WEB_SRC}/ns.ts", 'import * as O from "../../../../lib/launchpad/types/order";\nexport const x = O.OrderSchema.parse(1);\n'),
        }
        fake_navs.BEHAVIOUR[("web", "imports")] = lambda a: web_edges(a["file"], a["direction"], list(self.files.values()) if a["direction"] == "importers"
                                                                      else [ORDER])

    def marks(self, text):
        found = {}
        for name, rel in self.files.items():
            hit = re.search(rf"^  {re.escape(rel)}:\d+(?: -> \S+)? \[([^\]]+)\]$", text, re.M)
            found[name] = hit.group(1) if hit else "absent"
        return found

    def test_each_consumer_of_a_contract_is_marked_reader_writer_both_or_uses(self):
        text = self.run_tool("ctx_relations", {"target": CART, "direction": "contracts"}).text
        self.assertEqual(self.marks(text), {"reader": "reader", "writer": "writer", "both": "reader, writer", "uses": "uses", "types": "reader",
                                            "reexport": "writer", "alias": "writer", "namespace": "reader"}, text)

    def test_importers_of_a_lib_contract_are_marked_and_a_file_that_cannot_be_read_says_so(self):
        text = self.run_tool("ctx_relations", {"target": ORDER, "direction": "importers"}).text
        self.assertEqual(self.marks(text)["writer"], "writer", text)
        (self.project / self.files["writer"]).unlink()
        self.assertEqual(self.marks(self.run_tool("ctx_relations", {"target": ORDER, "direction": "importers"}).text)["writer"], "unread")

    def test_the_brief_marks_its_contract_consumers_too(self):
        text = self.run_tool("ctx_brief", {"paths": [CART]}).text
        contracts = text.split("== contracts")[1].split("== knowledge")[0]
        self.assertEqual(self.marks(contracts)["reader"], "reader", contracts)
        self.assertIn("[", contracts)


def web_listing(total, contract_at, cap):
    """A web navigator that has `total` imports of the file, returns at most `cap` of them and reports `total` and `truncated`."""
    def answer(args):
        everything = [ORDER if n == contract_at else f"{WEB_SRC}/p{n:03d}.ts" for n in range(1, total + 1)]
        shown = web_edges(args["file"], "imports", everything)
        wanted = min(args["limit"], cap)
        shown["edges"] = shown["edges"][:wanted]
        shown["truncated"] = total > wanted
        return shown
    return answer


def importing(total, contract_at, cap):
    listing = web_listing(total, contract_at, cap)
    return lambda a: listing(a) if a["direction"] == "imports" else web_edges(a["file"], "importers", [SPEC])


class TruncatedImports(ReviewOne):
    """F3: an imports list the navigator cut is reported as cut, and never as the whole picture of the file's contracts."""

    def test_a_26th_import_is_not_hidden_behind_a_25_of_25_claim(self):
        fake_navs.BEHAVIOUR[("web", "imports")] = importing(26, 26, 200)
        answer = self.run_tool("ctx_relations", {"target": CART, "direction": "imports", "limit": 25})
        self.assertNotIn("25 of 25", answer.text)
        self.assertIn("25 of 26", answer.text)
        self.assertTrue(any("imports" in item and "1 more" in item for item in answer.data["left_out"]), answer.data["left_out"])

    def test_the_contract_that_is_import_26_is_found_in_the_brief_and_in_relations(self):
        fake_navs.BEHAVIOUR[("web", "imports")] = importing(26, 26, 200)
        brief = self.run_tool("ctx_brief", {"paths": [CART]})
        self.assertNotIn("no shared contracts", brief.text)
        self.assertIn(ORDER, brief.text.split("== contracts")[1].split("== knowledge")[0])
        self.assertIn(ORDER, self.run_tool("ctx_relations", {"target": CART, "direction": "contracts"}).text)

    def test_when_the_navigator_itself_cuts_the_list_the_contracts_view_says_it_may_be_incomplete(self):
        fake_navs.BEHAVIOUR[("web", "imports")] = importing(250, 250, 200)
        for answer in (self.run_tool("ctx_relations", {"target": CART, "direction": "contracts"}), self.run_tool("ctx_brief", {"paths": [CART]})):
            self.assertNotIn("no shared contracts", answer.text)
            self.assertIn("only 200 imports were read, the navigator cut its list", answer.text)
            self.assertNotIn("250", answer.text)
            self.assertTrue(any("imports" in item for item in answer.data["left_out"]), answer.data["left_out"])

    def test_a_complete_list_makes_no_such_claim(self):
        fake_navs.BEHAVIOUR[("web", "imports")] = importing(5, 3, 200)
        answer = self.run_tool("ctx_relations", {"target": CART, "direction": "contracts"})
        self.assertNotIn("were read", answer.text)
        self.assertIn(ORDER, answer.text)


def symbol_listing(total, cap, path=MANY):
    def answer(args):
        found = [{"name": f"e{n:03d}", "kind": "variable", "path": path, "line": n, "signature": f"export const e{n:03d} = {n};", "doc": ""}
                 for n in range(1, total + 1)]
        wanted = min(args["limit"], cap)
        return {"status": "ok", "path": path, "source": "deno_doc", "cached": False, "total": total, "truncated": total > wanted, "symbols": found[:wanted]}
    return answer


class PartialSymbolListing(ReviewOne):
    """F4: ctx_symbol does not conclude that an api symbol is absent from a listing the navigator cut."""

    def test_a_name_beyond_the_listing_is_not_declared_absent(self):
        fake_navs.BEHAVIOUR[("api", "symbols")] = symbol_listing(120, 100)
        answer = self.run_tool("ctx_symbol", {"name": "e110", "path": MANY})
        self.assertNotIn("lists none", answer.text)
        self.assertNotIn("no definition", answer.text)
        self.assertIn("does not show that it is not defined", answer.text)
        self.assertRegex(answer.text, r"Next: \S")
        self.assertTrue(any("not listed" in item for item in answer.data["left_out"]), answer.data["left_out"])

    def test_a_name_in_the_first_hundred_is_found_where_fifty_used_to_be_the_limit(self):
        fake_navs.BEHAVIOUR[("api", "symbols")] = symbol_listing(120, 100)
        answer = self.run_tool("ctx_symbol", {"name": "e060", "path": MANY})
        self.assertIn(f"{MANY}:60", answer.text)
        self.assertEqual(max(c[2]["limit"] for c in fake_navs.CALLS if c[1] == "symbols"), 100)

    def test_a_name_missing_from_a_complete_listing_is_still_said_to_be_absent(self):
        fake_navs.BEHAVIOUR[("api", "symbols")] = symbol_listing(30, 100)
        answer = self.run_tool("ctx_symbol", {"name": "nothere", "path": MANY})
        self.assertIn("no definition of nothere", answer.text)
        self.assertNotIn("does not show", answer.text)


def letters(count):
    return "".join(chr(97 + n % 26) for n in range(count))


class NoteBodies(ReviewOne):
    """F5: ctx_knowledge returns the whole note body; the brief returns the first 300 characters of it whatever the title's length."""

    def test_knowledge_returns_an_800_character_body_whole(self):
        add_approved(self.layout, "kn-2026-10-01-long", "T" * 100, ["apps/**"], letters(800))
        answer = self.run_tool("ctx_knowledge", {})
        self.assertIn(letters(800), answer.text)
        self.assertEqual((answer.status, answer.data["truncated"], answer.data["left_out"]), ("ok", False, []))

    def test_a_body_of_the_longest_size_a_note_can_have_is_shown_whole(self):
        add_approved(self.layout, "kn-2026-10-01-huge", "Huge", ["apps/**"], letters(store.BODY_MAX))
        answer = self.run_tool("ctx_knowledge", {})
        self.assertIn(": " + letters(store.BODY_MAX), answer.text)
        self.assertNotIn("…", answer.text)
        self.assertEqual((answer.data["truncated"], answer.data["left_out"]), (False, []))

    def test_the_brief_gives_300_characters_of_body_after_a_100_character_title(self):
        add_approved(self.layout, "kn-2026-10-01-titled", "H" * 100, [ORDERS], letters(500))
        answer = self.run_tool("ctx_brief", {"paths": [ORDERS]})
        line = [row for row in answer.text.splitlines() if "kn-2026-10-01-titled" in row][0]
        self.assertTrue(line.endswith(": " + letters(300) + "…"), line)
        self.assertTrue(any("kn-2026-10-01-titled" in item for item in answer.data["left_out"]), answer.data["left_out"])

    def test_a_short_body_is_shown_whole_in_both_views(self):
        add_approved(self.layout, "kn-2026-10-01-short", "Short", [ORDERS], "Just this.")
        for answer in (self.run_tool("ctx_brief", {"paths": [ORDERS]}), self.run_tool("ctx_knowledge", {})):
            self.assertIn("Short: Just this.", answer.text)
            self.assertFalse(any("kn-2026-10-01-short" in item for item in answer.data["left_out"]))


class NoteResult(SurfaceCase):
    """F6: the candidate's id and status are structured output, not prose to scrape."""

    def test_the_json_answer_carries_the_id_and_status_for_a_note_and_nothing_extra_for_other_tools(self):
        task = self.tmp / "task"
        task.mkdir()
        env = {"TP_TASK_DIR": str(task), "TP_TASK_ID": "coder-x"}
        done = run_cli(self.project, "ctx_note", VALID["ctx_note"], env)
        payload = json.loads(done.stdout)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(payload["result"]["status"], "candidate")
        self.assertRegex(payload["result"]["id"], r"^kn-\d{4}-\d\d-\d\d-")
        self.assertIn(payload["result"]["id"], payload["text"])
        self.assertTrue(self.layout.path("candidate", payload["result"]["id"]).is_file())
        for tool in ("ctx_knowledge", "ctx_brief"):
            self.assertNotIn("result", json.loads(run_cli(self.project, tool, VALID[tool], env).stdout), tool)
        refused = json.loads(run_cli(self.project, "ctx_note", {**VALID["ctx_note"], "text": f"Use {AWS}."}, env).stdout)
        self.assertNotIn("result", refused)


class NoteLimitIsAtomic(SurfaceCase):
    """F7: the five-notes-per-task limit holds when callers arrive together."""

    GATE = ("import os, runpy, sys\n"
            "os.read(int(sys.argv[1]), 1)  # wait for the parent to close its end: every caller starts together\n"
            "sys.argv = ['tp-ctx'] + sys.argv[2:]\n"
            "runpy.run_path(%r, run_name='__main__')\n")

    def test_six_callers_released_together_write_five_candidates_and_log_six_lines(self):
        task = self.tmp / "task"
        task.mkdir()
        env = clean_env({"TP_CTX_TODAY": "2026-10-31", "TP_TASK_DIR": str(task), "TP_TASK_ID": "coder-x"})
        gate_in, gate_out = os.pipe()
        procs = []
        for number in range(6):
            args = {**VALID["ctx_note"], "text": f"Rule number {number}."}
            procs.append(subprocess.Popen([sys.executable, "-c", self.GATE % str(TP_CTX), str(gate_in), "--project", str(self.project), "--json",
                                           "ctx_note", "--input", json.dumps(args)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                                          pass_fds=(gate_in,), stdin=subprocess.DEVNULL))
        os.close(gate_in)
        os.close(gate_out)  # EOF on the gate: all six go
        results = []
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            results.append((proc.returncode, json.loads(out)["status"] if out else err))
        self.assertEqual(sorted(results), [(0, "ok")] * 5 + [(2, "refused")], results)
        self.assertEqual(len(list(self.layout.directory("candidate").glob("*.md"))), 5)
        logged = [json.loads(line) for line in (task / "ctx-calls.jsonl").read_text().splitlines()]
        self.assertEqual(sorted(line["status"] for line in logged), ["ok"] * 5 + ["refused"])

    def test_the_task_lock_is_held_for_the_whole_of_a_note_call(self):
        task = self.tmp / "task"
        task.mkdir()
        held = []
        engine = self.engine(env={"TP_TASK_ID": "coder-x", "TP_TASK_DIR": str(task)})
        real = engine.op_ctx_note

        def probe(args):
            fd = os.open(task / calls.LOCK_NAME, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held.append(False)
            except BlockingIOError:
                held.append(True)
            finally:
                os.close(fd)
            return real(args)

        engine.op_ctx_note = probe
        calls.call("ctx_note", VALID["ctx_note"], lambda: engine.run("ctx_note", VALID["ctx_note"]), env={"TP_TASK_DIR": str(task)}, exclusive=True)
        self.assertEqual(held, [True])


class UnloggedNotes(SurfaceCase):
    """F8: a ctx_note whose log line cannot be written leaves no candidate."""

    def run_note(self, task):
        return run_cli(self.project, "ctx_note", VALID["ctx_note"], {"TP_TASK_DIR": str(task), "TP_TASK_ID": "coder-x"})

    def candidates(self):
        directory = self.layout.directory("candidate")
        return list(directory.glob("*.md")) if directory.exists() else []

    def test_a_log_that_is_a_directory_or_a_link_means_no_answer_and_no_candidate(self):
        task = self.tmp / "task"
        task.mkdir()
        log = task / "ctx-calls.jsonl"
        for make in (log.mkdir, lambda: log.symlink_to(self.tmp / "elsewhere.jsonl")):
            make()
            done = self.run_note(task)
            self.assertEqual((done.returncode, done.stdout), (3, ""), done.stderr)
            self.assertEqual(self.candidates(), [])
            log.rmdir() if log.is_dir() and not log.is_symlink() else log.unlink()
        self.assertFalse((self.tmp / "elsewhere.jsonl").exists())
        self.assertEqual(self.run_note(task).returncode, 0)  # the retry writes exactly one
        self.assertEqual(len(self.candidates()), 1)

    def test_a_line_that_fails_after_the_write_takes_the_candidate_back(self):
        task = self.tmp / "task"
        task.mkdir()
        engine = self.engine(env={"TP_TASK_ID": "coder-x", "TP_TASK_DIR": str(task)})
        with mock.patch.object(calls, "record", side_effect=calls.LogUnavailable("disk full")):
            with self.assertRaises(calls.LogUnavailable):
                calls.call("ctx_note", VALID["ctx_note"], lambda: engine.run("ctx_note", VALID["ctx_note"]), env={"TP_TASK_DIR": str(task)},
                           exclusive=True)
        self.assertEqual(self.candidates(), [])


class WritingCallsShareTheLock(unittest.TestCase):
    """F7/F8 at their shared cause (calls.call, used by the surface and by CX1's `knowledge add`): a call that writes holds the task's
    lock from before it runs until its line is written, checks the log can be appended first, and takes its write back if the line
    still cannot be written."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name).resolve()
        self.task = self.tmp / "task"
        self.task.mkdir()
        self.env = {"TP_TASK_DIR": str(self.task)}

    def lock_is_free(self):
        fd = os.open(self.task / calls.LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
        finally:
            os.close(fd)

    def test_an_exclusive_call_holds_the_task_lock_while_it_runs_and_releases_it_after(self):
        seen = []
        calls.call("t", {}, lambda: seen.append(self.lock_is_free()) or calls.Answer("ok", "x"), env=self.env, exclusive=True)
        calls.call("t", {}, lambda: seen.append(self.lock_is_free()) or calls.Answer("ok", "x"), env=self.env)
        self.assertEqual(seen, [False, True])  # held during the exclusive run, not taken by an ordinary one
        self.assertTrue(self.lock_is_free())
        self.assertEqual(len((self.task / calls.LOG_NAME).read_text().splitlines()), 2)

    def test_the_lock_is_released_when_the_call_cannot_be_logged(self):
        with mock.patch.object(calls, "record", side_effect=calls.LogUnavailable("no")):
            with self.assertRaises(calls.LogUnavailable):
                calls.call("t", {}, lambda: calls.Answer("ok", "x"), env=self.env, exclusive=True)
        self.assertTrue(self.lock_is_free())

    def test_a_write_is_taken_back_when_its_line_cannot_be_written(self):
        written = self.tmp / "candidate.md"

        def run():
            written.write_text("a candidate")
            return calls.Answer("ok", "written", undo=written.unlink)

        with mock.patch.object(calls, "record", side_effect=calls.LogUnavailable("disk full")):
            with self.assertRaises(calls.LogUnavailable):
                calls.call("t", {}, run, env=self.env, exclusive=True)
        self.assertFalse(written.exists())

    def test_a_log_that_cannot_be_appended_stops_an_exclusive_call_before_it_runs(self):
        ran = []
        log = self.task / calls.LOG_NAME
        for make in (log.mkdir, lambda: log.symlink_to(self.tmp / "elsewhere.jsonl")):
            make()
            with self.assertRaises(calls.LogUnavailable):
                calls.call("t", {}, lambda: ran.append(1) or calls.Answer("ok", "x"), env=self.env, exclusive=True)
            self.assertEqual(ran, [])
            log.rmdir() if log.is_dir() and not log.is_symlink() else log.unlink()
        self.assertFalse((self.tmp / "elsewhere.jsonl").exists())

    def test_cx1_knowledge_add_leaves_no_candidate_when_its_log_cannot_be_written(self):
        project = self.tmp / "project"
        (project / ".tmux-pi").mkdir(parents=True)
        put(project, "apps/a.test.ts", "one\ntwo\n")
        notes = project / ".tmux-pi" / "knowledge"
        log = self.task / calls.LOG_NAME

        def add():
            return subprocess.run([sys.executable, str(TP_CTX), "--project", str(project), "knowledge", "add", "--kind", "convention", "--scope",
                                   "apps/**/*.test.ts", "--evidence", "apps/a.test.ts:2", "--text", "Clean up only the rows a test created."],
                                  capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                                  env=clean_env({"TP_TASK_DIR": str(self.task), "TP_TASK_ID": "coder-x", "TP_CTX_TODAY": "2026-10-31"}))

        for make in (log.mkdir, lambda: log.symlink_to(self.tmp / "elsewhere.jsonl")):
            make()
            done = add()
            self.assertEqual((done.returncode, done.stdout), (3, ""), done.stderr)
            self.assertEqual(list(notes.glob("**/*.md")), [], done.stderr)
            log.rmdir() if log.is_dir() and not log.is_symlink() else log.unlink()
        self.assertEqual(add().returncode, 0)
        self.assertEqual(len(list(notes.glob("candidates/*.md"))), 1)
        self.assertEqual(len(log.read_text().splitlines()), 1)


class EveryDefinition(ReviewOne):
    """F9: each matching definition gets its own detail, not only the first."""

    def setUp(self):
        super().setUp()
        self.other = self.put(f"{API_SRC}/other.ts", "export function createOrder(count: number) {\n  return count;\n}\n")

        def symbols(args):
            if args["path"] != self.other:
                return None
            return {"status": "ok", "path": self.other, "source": "deno_doc", "total": 1, "truncated": False,
                    "symbols": [{"name": "createOrder", "kind": "function", "path": self.other, "line": 1,
                                 "signature": "export function createOrder(count: number) {", "doc": "The other one."}]}
        fake_navs.BEHAVIOUR[("api", "symbols")] = symbols

    def test_two_files_that_export_the_name_each_get_signature_and_doc(self):
        answer = self.run_tool("ctx_symbol", {"name": "createOrder"})
        self.assertEqual(answer.status, "ok", answer.text)
        self.assertIn(f"== {ORDERS}", answer.text)
        self.assertIn(f"== {self.other}", answer.text)
        self.assertIn("export function createOrder(input: unknown) {", answer.text)
        self.assertIn("export function createOrder(count: number) {", answer.text)
        self.assertIn("Creates an order.", answer.text)
        self.assertIn("The other one.", answer.text)

    def test_details_are_given_for_at_most_five_files_and_the_rest_are_named(self):
        for n in range(6):
            self.put(f"{API_SRC}/extra{n}.ts", "export function createOrder() {}\n")
        answer = self.run_tool("ctx_symbol", {"name": "createOrder"})
        self.assertEqual(len(re.findall(r"^== ", answer.text, re.M)), 5)
        self.assertTrue(any("details for 3 more file(s)" in item for item in answer.data["left_out"]), answer.data["left_out"])

    def test_a_path_narrows_it_back_to_one_file(self):
        answer = self.run_tool("ctx_symbol", {"name": "createOrder", "path": self.other})
        self.assertNotIn("== ", answer.text)
        self.assertNotIn("input: unknown", answer.text)


class NothingToBrief(ReviewOne):
    """F10: a brief where no requested path exists is a bad argument with a next step, not a success."""

    GHOST = f"{API_SRC}/ghost.ts"

    def test_every_path_missing_is_invalid_with_a_next_step(self):
        answer = self.run_tool("ctx_brief", {"paths": [self.GHOST, f"{API_SRC}/phantom.ts"]})
        self.assertEqual(answer.status, "invalid", answer.text)
        for path in (self.GHOST, f"{API_SRC}/phantom.ts"):
            self.assertIn(f"== {path}\nno such file", answer.text)
        self.assertRegex(answer.text, r"Next: \S")

    def test_some_paths_missing_is_still_a_brief_with_a_next_step_for_the_missing_ones(self):
        answer = self.run_tool("ctx_brief", {"paths": [ORDERS, self.GHOST]})
        self.assertEqual(answer.status, "ok", answer.text)
        self.assertIn(f"== {ORDERS}", answer.text)
        self.assertRegex(answer.text, rf"Next: [^\n]*{re.escape(self.GHOST)}")

    def test_the_logged_status_is_invalid(self):
        task = self.tmp / "task"
        task.mkdir()
        done = run_cli(self.project, "ctx_brief", {"paths": [self.GHOST]}, {"TP_TASK_DIR": str(task)})
        self.assertEqual((done.returncode, json.loads(done.stdout)["status"]), (1, "invalid"))
        self.assertEqual(json.loads((task / "ctx-calls.jsonl").read_text())["status"], "invalid")


class AmbiguousSymbols(ReviewOne):
    """F11: ctx_relations does not pick one of several same-named symbols."""

    def setUp(self):
        super().setUp()
        self.a = self.put(f"{API_SRC}/handlers/a.ts", "export function handler() {}\n")
        self.b = self.put(f"{API_SRC}/handlers/b.ts", "export const handler = () => 1;\n")

    def test_a_symbol_declared_in_two_files_is_rejected_with_the_files_and_a_next_step(self):
        for direction in ("importers", "imports", "contracts"):
            answer = self.run_tool("ctx_relations", {"target": "handler", "direction": direction})
            self.assertEqual(answer.status, "invalid", (direction, answer.text))
            self.assertIn(self.a, answer.text)
            self.assertIn(self.b, answer.text)
            self.assertRegex(answer.text, r"Next: [^\n]*path")
        self.assertEqual(fake_navs.CALLS, [])

    def test_a_symbol_declared_once_still_resolves_and_a_path_still_works(self):
        answer = self.run_tool("ctx_relations", {"target": "addToCart", "direction": "importers"})
        self.assertEqual(answer.status, "ok", answer.text)
        self.assertIn(f"target resolved to {CART}", answer.text)
        self.assertEqual(self.run_tool("ctx_relations", {"target": self.a, "direction": "importers"}).status, "ok")

    def test_two_declarations_in_the_same_file_are_still_one_file(self):
        self.put(f"{API_SRC}/handlers/c.ts", "export function twice() {}\nexport type twice = number;\n")
        answer = self.run_tool("ctx_relations", {"target": "twice", "direction": "importers"})
        self.assertEqual(answer.status, "ok", answer.text)


# ------------------------------------------------------------------------------------------------------- Review 2 (F1-F4)

class OneOutputGate(ReviewOne):
    """Every path that reaches an answer, whoever produced it, passes the guard and the project boundary; a refused one is counted
    in left_out and never named. Losses a navigator reports (truncated, withheld, omitted_outside_root) all reach left_out."""

    def outside_test_link(self):
        src = self.project / API_SRC
        outside = self.tmp / "elsewhere.test.ts"
        outside.write_text("test('x', () => {});\n")
        (src / "orders.test.ts").unlink()
        (src / "orders.test.ts").symlink_to(outside)

    def test_f1_a_symlinked_test_file_is_counted_not_named(self):
        self.outside_test_link()
        answer = self.run_tool("ctx_brief", {"paths": [ORDERS]})
        self.assertNotIn("orders.test.ts", answer.text)
        self.assertNotIn("elsewhere", answer.text)
        self.assertTrue(any("tests" in item and "1 withheld" in item for item in answer.data["left_out"]), answer.data["left_out"])

    def test_the_sources_do_not_enumerate_files_outside_the_gate(self):
        import ast
        gated = ("allowed", "check_path", "self.path", "engine.path")
        reaches = {"is_file", "exists", "iterdir", "listdir", "glob", "rglob", "scandir", "walk", "is_dir", "isfile"}
        # these only test arguments that were checked by Engine.path, read a checked path, or name an ancestor package directory
        argument_checked = {"build", "op_ctx_symbol", "op_ctx_relations", "main", "run", "read", "package_for"}
        bare = []
        for name in ("brief.py", "surface.py"):
            source = (ROOT / "lib" / "ctxengine" / name).read_text()
            for node in ast.walk(ast.parse(source)):
                if not isinstance(node, ast.FunctionDef):
                    continue
                body = ast.get_source_segment(source, node)
                touches = any(isinstance(n, ast.Attribute) and n.attr in reaches for n in ast.walk(node))
                if touches and node.name not in argument_checked and not any(g in body for g in gated):
                    bare.append(f"{name}:{node.name}")
        self.assertEqual(bare, [])

    def test_f2_paths_a_web_navigator_omitted_outside_the_root_are_counted(self):
        fake_navs.BEHAVIOUR[("web", "definition")] = lambda a: {
            "status": "ok", "definitions": [{"path": CART, "line": 5, "name": "addToCart", "kind": "function", "text": "function addToCart()"}],
            "signature": "", "doc": "", "omitted_outside_root": 2}
        fake_navs.BEHAVIOUR[("web", "references")] = lambda a: {
            "status": "ok", "definitions": [], "references": [], "total": 0, "truncated": False, "omitted_outside_root": 3}
        answer = self.run_tool("ctx_symbol", {"name": "addToCart", "path": CART})
        left = " | ".join(answer.data["left_out"])
        self.assertIn("2 withheld", left)
        self.assertIn("3 withheld", left)

    def test_f2_omitted_outside_root_is_counted_for_importers_and_imports_too(self):
        fake_navs.BEHAVIOUR[("web", "imports")] = lambda a: web_edges(a["file"], a["direction"],
                                                                      [SPEC if a["direction"] == "importers" else ORDER], omitted_outside_root=4)
        for direction in ("importers", "imports"):
            answer = self.run_tool("ctx_relations", {"target": CART, "direction": direction})
            self.assertTrue(any("4 withheld" in item for item in answer.data["left_out"]), (direction, answer.data["left_out"]))

    def test_f3_a_truncated_empty_reference_list_is_not_zero_references(self):
        fake_navs.BEHAVIOUR[("web", "references")] = lambda a: {"status": "ok", "definitions": [], "references": [], "total": 0, "truncated": True}
        answer = self.run_tool("ctx_symbol", {"name": "addToCart", "path": CART})
        self.assertTrue(any("references" in item and "cut" in item for item in answer.data["left_out"]), answer.data["left_out"])

    def test_f4_a_typed_annotation_makes_the_consumer_a_reader(self):
        rel = self.put(f"{WEB_SRC}/annotated.ts", 'import { Order } from "../../../../lib/launchpad/types/order";\nconst order: Order = load();\n')
        from ctxengine import brief
        engine = surface.Engine(surface.store.Layout(self.project), navigators=surface.Navigators(surface.store.Layout(self.project), {}))
        self.assertEqual(brief.contract_use(engine, ORDER, rel), "reader")
        self.put(f"{WEB_SRC}/unused.ts", 'import { Order } from "../../../../lib/launchpad/types/order";\nconsole.log(1);\n')
        self.assertEqual(brief.contract_use(engine, ORDER, f"{WEB_SRC}/unused.ts"), "uses")


# ------------------------------------------------------------------------------------------------------- Review 3 (F1, F2)

TOTAL = 913   # a total no fixture list has: if it appears in an answer, a navigator's count of a cut list was repeated


class CutLists(ReviewOne):
    """F1, F2 and the family: when Engine.loss says a navigator cut a list, the answer text for that list never states a total, a
    zero or an absence; it says "at least N shown, list cut by the navigator" or "not found in the part the navigator returned
    (list cut)". One table covers every operation and every list."""

    SHOWN = re.compile(r"at least \d+ shown, list cut by the navigator")
    NOT_FOUND = "not found in the part the navigator returned (list cut)"
    ABSENCES = ("no definition", "nothing imports", "imports no ", "no shared", "none")
    ZERO = re.compile(r"(?<![\w.:/-])0(?![\w.])")

    # each setup installs a navigator answer that cuts one list: (empty, total) -> None
    def web_exports(self, empty, total):
        rows = [] if empty else [{"name": "addToCart", "kind": "function", "line": 5, "reexport": False, "defined_at": None}]
        fake_navs.BEHAVIOUR[("web", "exports")] = {"status": "ok", "is_module": True, "exports": rows, "total": total, "truncated": True}

    def web_importers(self, empty, total):
        fake_navs.BEHAVIOUR[("web", "imports")] = lambda a: (
            web_edges(a["file"], "importers", [] if empty else [SPEC], total=total, truncated=True) if a["direction"] == "importers" and a["file"] == CART
            else web_edges(a["file"], a["direction"], [SPEC] if a["direction"] == "importers" else [ORDER]))

    def web_imports(self, empty, total):
        fake_navs.BEHAVIOUR[("web", "imports")] = lambda a: (
            web_edges(a["file"], "imports", [] if empty else [f"{WEB_SRC}/x.ts"], total=total, truncated=True) if a["direction"] == "imports"
            else web_edges(a["file"], "importers", [SPEC]))

    def web_consumers(self, empty, total):
        fake_navs.BEHAVIOUR[("web", "imports")] = lambda a: (
            web_edges(a["file"], "importers", [] if empty else [f"{WEB_SRC}/p01.ts"], total=total, truncated=True)
            if a["direction"] == "importers" and a["file"] == ORDER
            else web_edges(a["file"], a["direction"], [SPEC] if a["direction"] == "importers" else [ORDER]))

    def api_importers_cut(self, empty, total):
        fake_navs.BEHAVIOUR[("api", "importers")] = api_importers([] if empty else [f"{API_SRC}/routes/r01.ts"], total=total, truncated=True)

    def api_imports_cut(self, empty, total):
        files = [] if empty else [{"path": f"{API_SRC}/util.ts", "line": 3, "spec": "./util.ts"}]
        fake_navs.BEHAVIOUR[("api", "imports")] = {"status": "ok", "target": ORDERS, "source": "index", "files": files, "external": [], "unresolved": [],
                                                   "index_age_s": 3, "unresolved_total": 0, "files_indexed": 40, "scanned": 0, "truncated": True,
                                                   "total": total}

    def api_symbols_cut(self, empty, total):
        symbols = [] if empty else [{"name": "other", "kind": "function", "path": ORDERS, "line": 7, "signature": "export function other() {}", "doc": ""}]
        fake_navs.BEHAVIOUR[("api", "symbols")] = {"status": "ok", "path": ORDERS, "source": "deno_doc", "total": total, "truncated": True, "symbols": symbols}

    def api_symbols_named_cut(self, empty, total):
        name = "createOrder"
        symbols = [] if empty else [{"name": name, "kind": "function", "path": ORDERS, "line": 5, "signature": "export function createOrder() {}", "doc": ""}]
        fake_navs.BEHAVIOUR[("api", "symbols")] = {"status": "ok", "path": ORDERS, "source": "deno_doc", "total": total, "truncated": True, "symbols": symbols}

    def web_definitions(self, empty, total):
        rows = [] if empty else [{"path": CART, "line": 5, "name": "addToCart", "kind": "function", "text": "function addToCart()"}]
        fake_navs.BEHAVIOUR[("web", "definition")] = {"status": "ok", "definitions": rows, "signature": "", "doc": "", "truncated": True}

    def web_references(self, empty, total):
        rows = [] if empty else [{"path": SPEC, "line": 2, "column": 1, "text": "addToCart(x)", "is_definition": False, "is_write": False}]
        fake_navs.BEHAVIOUR[("web", "references")] = {"status": "ok", "definitions": [], "references": rows, "total": total, "truncated": True}

    # (operation, arguments, list, setup, regex that selects the lines that open that list)
    TABLE = [
        ("ctx_brief", {"paths": [CART]}, "web exports", "web_exports", r"^exports\b"),
        ("ctx_brief", {"paths": [CART]}, "web importers", "web_importers", r"^importers\b"),
        ("ctx_brief", {"paths": [CART]}, "web imports", "web_imports", r"^imports\b"),
        ("ctx_brief", {"paths": [CART]}, "web contract consumers", "web_consumers", r"^\s+consumers\b"),
        ("ctx_brief", {"paths": [CART]}, "web imports (contracts absent)", "web_imports", r"^shared \(lib\) contracts\b"),
        ("ctx_brief", {"paths": [ORDERS]}, "api exports", "api_symbols_cut", r"^exports\b"),
        ("ctx_brief", {"paths": [ORDERS]}, "api importers", "api_importers_cut", r"^importers\b"),
        ("ctx_brief", {"paths": [ORDERS]}, "api imports", "api_imports_cut", r"^imports\b"),
        ("ctx_relations", {"target": CART, "direction": "importers"}, "web importers", "web_importers", r"^importers of\b"),
        ("ctx_relations", {"target": CART, "direction": "imports"}, "web imports", "web_imports", r"^\S+ imports\b"),
        ("ctx_relations", {"target": CART, "direction": "contracts"}, "web imports (contracts absent)", "web_imports", r"^shared \(lib\) contracts\b"),
        ("ctx_relations", {"target": CART, "direction": "contracts"}, "web contract consumers", "web_consumers", r"^\s+consumers\b"),
        ("ctx_relations", {"target": ORDERS, "direction": "importers"}, "api importers", "api_importers_cut", r"^importers of\b"),
        ("ctx_relations", {"target": ORDERS, "direction": "imports"}, "api imports", "api_imports_cut", r"^\S+ imports\b"),
        ("ctx_symbol", {"name": "addToCart", "path": CART}, "web definitions", "web_definitions", r"^definitions of\b"),
        ("ctx_symbol", {"name": "addToCart", "path": CART}, "web references", "web_references", r"^references of\b"),
        ("ctx_symbol", {"name": "createOrder", "path": ORDERS}, "api symbols (definitions)", "api_symbols_named_cut", r"^definitions of\b|^createOrder in\b"),
    ]

    def check(self, tool, args, label, setup, heads, empty, total):
        getattr(self, setup)(empty, total)
        if label.startswith("web definitions") and not empty:
            self.web_references(True, 0)
        answer = self.run_tool(tool, args)
        where = f"{tool} {label} empty={empty} total={total}"
        self.assertIn(answer.status, ("ok",), (where, answer.text))
        head = [line for line in answer.text.splitlines() if re.search(heads, line)]
        self.assertTrue(head, (where, answer.text))
        cut_lines = [line for line in head if "list cut" in line]
        self.assertTrue(cut_lines, (where, head))
        for line in head:
            self.assertNotRegex(line, self.ZERO, (where, line))
            self.assertNotIn(str(TOTAL), line, (where, line))
        if empty or "absent" in label:  # an absence derived from a cut list is always "not found in the part returned"
            self.assertTrue(any(self.NOT_FOUND in line for line in cut_lines), (where, cut_lines))
        else:
            self.assertTrue(any(self.SHOWN.search(line) for line in cut_lines), (where, cut_lines))
        # nothing anywhere in the answer, its footer or its left_out repeats the navigator's total for the cut list
        self.assertNotIn(str(TOTAL), answer.text, (where, answer.text))
        self.assertNotIn(str(TOTAL), " ".join(answer.data["left_out"]), (where, answer.data["left_out"]))
        for line in head:
            for word in self.ABSENCES:
                self.assertNotIn(word, line, (where, line))
        self.assertTrue(any("cut" in item for item in answer.data["left_out"]), (where, answer.data["left_out"]))
        return answer

    def test_every_cut_list_in_every_operation_is_said_to_be_cut_and_states_no_total_zero_or_absence(self):
        for tool, args, label, setup, heads in self.TABLE:
            for empty, total in ((False, TOTAL), (True, TOTAL), (True, 0)):
                with self.subTest(tool=tool, list=label, empty=empty, total=total):
                    fake_navs.reset()
                    self.check(tool, args, label, setup, heads, empty, total)

    def test_f1_f2_the_reported_cases(self):
        fake_navs.BEHAVIOUR[("web", "references")] = {"status": "ok", "definitions": [], "references": [], "total": 0, "truncated": True}
        text = self.run_tool("ctx_symbol", {"name": "addToCart", "path": CART}).text
        self.assertNotIn("0 references", text)
        self.assertIn("references of addToCart: " + self.NOT_FOUND, text)
        fake_navs.BEHAVIOUR[("web", "definition")] = {"status": "ok", "definitions": [], "truncated": True}
        text = self.run_tool("ctx_symbol", {"name": "addToCart", "path": CART}).text
        self.assertNotIn("no definition", text)
        self.assertNotIn("found none", text)
        self.assertIn("definitions of addToCart in " + CART + ": " + self.NOT_FOUND, text)

    def test_a_list_that_was_not_cut_still_states_its_total_and_its_absences(self):
        text = self.run_tool("ctx_symbol", {"name": "addToCart", "path": CART}).text
        self.assertRegex(text, r"references of addToCart \(5 of 7\)")
        self.assertNotIn("list cut", text)
        util = f"{API_SRC}/util.ts"
        self.assertIn(f"nothing imports {util}", self.run_tool("ctx_relations", {"target": util, "direction": "importers"}).text)
        fake_navs.BEHAVIOUR[("web", "references")] = {"status": "ok", "definitions": [], "references": [], "total": 0, "truncated": False}
        self.assertRegex(self.run_tool("ctx_symbol", {"name": "addToCart", "path": CART}).text, r"references of addToCart \(0\)")


# ------------------------------------------------------------------------------------------------------- Review 4 (F1)

class AllWithheld(ReviewOne):
    """F1: when every item of a list was refused or withheld and the navigator did not flag a cut, the list head says none can be
    shown and how many were withheld; it never claims an absence ("nothing imports X", "imports no ...", "(0)", "no shared ...")."""

    def setUp(self):
        super().setUp()
        outside = self.tmp / "outside.ts"
        outside.write_text("export const x = 1;\n")
        (self.project / "apps" / "link.ts").symlink_to(outside)

    NONE_SHOWN = re.compile(r"none can be shown \((\d+) withheld")
    ABSENCES = ("no definition", "nothing imports", "imports no ", "no shared", "none:", "found none", "(0)", "(0 of")

    def web_importers(self, mode):
        fake_navs.BEHAVIOUR[("web", "imports")] = lambda a: (
            self.edges(a["file"], "importers", mode) if a["direction"] == "importers" and a["file"] == CART
            else web_edges(a["file"], a["direction"], [SPEC] if a["direction"] == "importers" else [ORDER]))

    def web_imports(self, mode):
        fake_navs.BEHAVIOUR[("web", "imports")] = lambda a: (
            self.edges(a["file"], "imports", mode) if a["direction"] == "imports" else web_edges(a["file"], "importers", [SPEC]))

    def web_consumers(self, mode):
        fake_navs.BEHAVIOUR[("web", "imports")] = lambda a: (
            self.edges(a["file"], "importers", mode) if a["direction"] == "importers" and a["file"] == ORDER
            else web_edges(a["file"], a["direction"], [SPEC] if a["direction"] == "importers" else [ORDER]))

    def edges(self, file, direction, mode):
        return web_edges(file, direction, REFUSED_PATHS if mode == "paths" else [], **({} if mode == "paths" else {"withheld": 4, "total": 4}))

    def api_importers_all(self, mode):
        fake_navs.BEHAVIOUR[("api", "importers")] = api_importers(REFUSED_PATHS if mode == "paths" else [], **({} if mode == "paths" else {"withheld": 4, "total": 4}))

    def api_imports_all(self, mode):
        files = [{"path": p, "line": n, "spec": "x"} for n, p in enumerate(REFUSED_PATHS, 1)] if mode == "paths" else []
        fake_navs.BEHAVIOUR[("api", "imports")] = {"status": "ok", "target": ORDERS, "source": "index", "files": files, "external": [], "unresolved": [],
                                                   "index_age_s": 3, "unresolved_total": 0, "files_indexed": 40, "scanned": 0,
                                                   **({} if mode == "paths" else {"withheld": 4})}

    def web_exports_all(self, mode):
        fake_navs.BEHAVIOUR[("web", "exports")] = {"status": "ok", "is_module": True, "exports": [], "total": 4, "truncated": False, "withheld": 4}

    def web_references_all(self, mode):
        rows = [{"path": p, "line": 2, "column": 1, "text": "addToCart(x)", "is_definition": False, "is_write": False} for p in REFUSED_PATHS]
        fake_navs.BEHAVIOUR[("web", "references")] = (
            {"status": "ok", "definitions": [], "references": rows, "total": 4, "truncated": False} if mode == "paths"
            else {"status": "ok", "definitions": [], "references": [], "total": 4, "truncated": False, "withheld": 4})

    def web_definitions_all(self, mode):
        rows = [{"path": p, "line": 5, "name": "addToCart", "kind": "function", "text": "function addToCart()"} for p in REFUSED_PATHS]
        fake_navs.BEHAVIOUR[("web", "definition")] = (
            {"status": "ok", "definitions": rows, "signature": "", "doc": ""} if mode == "paths"
            else {"status": "ok", "definitions": [], "signature": "", "doc": "", "withheld": 4})

    # (operation, arguments, list, setup, regex that selects the lines that open that list)
    TABLE = [
        ("ctx_relations", {"target": CART, "direction": "importers"}, "web importers", "web_importers", r"^importers of\b"),
        ("ctx_brief", {"paths": [CART]}, "web importers", "web_importers", r"^importers\b"),
        ("ctx_relations", {"target": ORDERS, "direction": "importers"}, "api importers", "api_importers_all", r"^importers of\b"),
        ("ctx_brief", {"paths": [ORDERS]}, "api importers", "api_importers_all", r"^importers\b"),
        ("ctx_relations", {"target": CART, "direction": "imports"}, "web imports", "web_imports", r"^\S+ imports\b"),
        ("ctx_brief", {"paths": [CART]}, "web imports", "web_imports", r"^imports\b"),
        ("ctx_relations", {"target": ORDERS, "direction": "imports"}, "api imports", "api_imports_all", r"^\S+ imports\b"),
        ("ctx_brief", {"paths": [ORDERS]}, "api imports", "api_imports_all", r"^imports\b"),
        ("ctx_relations", {"target": CART, "direction": "contracts"}, "web imports (contracts)", "web_imports", r"^shared \(lib\) contracts\b"),
        ("ctx_brief", {"paths": [CART]}, "web imports (contracts)", "web_imports", r"^shared \(lib\) contracts\b|^none: the files import"),
        ("ctx_relations", {"target": CART, "direction": "contracts"}, "web contract consumers", "web_consumers", r"^\s+consumers\b"),
        ("ctx_brief", {"paths": [CART]}, "web contract consumers", "web_consumers", r"^\s+consumers\b"),
        ("ctx_brief", {"paths": [CART]}, "web exports", "web_exports_all", r"^exports\b"),
        ("ctx_symbol", {"name": "addToCart", "path": CART}, "web references", "web_references_all", r"^references of\b"),
        ("ctx_symbol", {"name": "addToCart", "path": CART}, "web definitions", "web_definitions_all", r"^definitions of\b|^addToCart in\b"),
    ]

    def check(self, tool, args, label, setup, heads, mode):
        getattr(self, setup)(mode)
        if label == "web references":
            fake_navs.BEHAVIOUR.setdefault(("web", "definition"), {"status": "ok", "definitions": [
                {"path": CART, "line": 5, "name": "addToCart", "kind": "function", "text": "function addToCart()"}], "signature": "", "doc": ""})
        answer = self.run_tool(tool, args)
        where = f"{tool} {label} {mode}"
        self.assertEqual(answer.status, "ok", (where, answer.text))
        head = [line for line in answer.text.splitlines() if re.search(heads, line)]
        self.assertTrue(head, (where, answer.text))
        for line in head:
            for word in self.ABSENCES:
                self.assertNotIn(word, line, (where, line))
        if label == "web definitions":
            self.assertTrue(any("refused path" in line for line in head), (where, head))
        else:
            self.assertTrue(any(self.NONE_SHOWN.search(line) for line in head), (where, head))
        self.assertTrue(any("4 withheld" in item for item in answer.data["left_out"]), (where, answer.data["left_out"]))
        for word in LEAKS:
            self.assertNotIn(word, answer.text, (where, word))

    def test_a_list_whose_every_item_was_withheld_says_none_can_be_shown_and_never_claims_absence(self):
        for tool, args, label, setup, heads in self.TABLE:
            for mode in ("paths", "counted"):
                with self.subTest(tool=tool, list=label, mode=mode):
                    fake_navs.reset()
                    self.check(tool, args, label, setup, heads, mode)

    def test_a_list_with_nothing_withheld_still_states_its_absence(self):
        util = f"{API_SRC}/util.ts"
        self.assertIn(f"nothing imports {util}", self.run_tool("ctx_relations", {"target": util, "direction": "importers"}).text)


class AbsenceWording(ReviewOne):
    """Review 5 F1 (and the family it repeats): no output line claims an absence (none, no X, nothing, 0, not found) unless the
    withheld count for that item is 0. Every absence wording goes through `surface.absent` (list_head calls it), which takes the count."""

    def link_over(self, rel, text="test('x', () => {});\n"):
        outside = self.tmp / ("elsewhere-" + Path(rel).name)
        outside.write_text(text)
        (self.project / rel).unlink()
        (self.project / rel).symlink_to(outside)

    def brief_tests_lines(self, answer):
        return [line for line in answer.text.splitlines() if line.startswith("tests:")]

    def test_a_brief_whose_only_test_is_withheld_says_none_can_be_shown_never_none_found(self):
        for rel, source in ((f"{API_SRC}/orders.test.ts", ORDERS), (SPEC, CART)):
            with self.subTest(test=rel):
                self.link_over(rel)
                answer = self.run_tool("ctx_brief", {"paths": [source]})
                (line,) = self.brief_tests_lines(answer)
                self.assertRegex(line, r"none can be shown \(1 withheld")
                for word in ("none found", "no test", "nothing"):
                    self.assertNotIn(word, line)
                for word in ("orders.test", "cart.spec", "elsewhere"):
                    self.assertNotIn(word, answer.text)
                self.assertTrue(any("tests" in item and "1 withheld" in item for item in answer.data["left_out"]), answer.data["left_out"])

    def test_a_brief_with_no_test_file_at_all_still_states_that_none_was_found(self):
        (self.project / API_SRC / "orders.test.ts").unlink()
        (line,) = self.brief_tests_lines(self.run_tool("ctx_brief", {"paths": [ORDERS]}))
        self.assertEqual(line, "tests: none found next to it")

    def test_a_brief_with_one_test_shown_and_one_withheld_lists_the_shown_one(self):
        self.put(f"{API_SRC}/__tests__/orders.test.ts", "test('y', () => {});\n")
        self.link_over(f"{API_SRC}/__tests__/orders.test.ts")
        answer = self.run_tool("ctx_brief", {"paths": [ORDERS]})
        (line,) = self.brief_tests_lines(answer)
        self.assertIn("orders.test.ts", line)
        self.assertNotIn("none", line)
        self.assertTrue(any("tests" in item and "1 withheld" in item for item in answer.data["left_out"]), answer.data["left_out"])

    def hidden_declaration(self):
        outside = self.tmp / "hidden.ts"
        outside.write_text("export function hiddenThing() { return 1; }\n")
        (self.project / "apps" / "hidden.ts").symlink_to(outside)

    def test_a_name_search_that_skipped_a_refused_file_never_says_no_definition_was_found(self):
        self.hidden_declaration()
        answer = self.run_tool("ctx_symbol", {"name": "hiddenThing"})
        self.assertRegex(answer.text, r"none can be shown \(1 withheld")
        for word in ("no definition", "hidden.ts", "outside"):
            self.assertNotIn(word, answer.text)
        relations = self.run_tool("ctx_relations", {"target": "hiddenThing", "direction": "importers"})
        self.assertRegex(relations.text, r"none can be shown \(1 withheld")
        self.assertNotIn("no declaration", relations.text)
        self.assertNotIn("hidden.ts", relations.text)

    def test_a_name_search_that_skipped_nothing_still_says_no_definition_was_found(self):
        self.assertIn("no definition of nothingHere found", self.run_tool("ctx_symbol", {"name": "nothingHere"}).text)
        self.assertIn("no declaration of that name was found", self.run_tool("ctx_relations", {"target": "nothingHere", "direction": "importers"}).text)

    def test_a_brief_whose_only_note_is_a_candidate_never_says_no_note_covers_the_files(self):
        candidate = store.Note(id="kn-2026-10-06-cand", kind="lesson", scope=["apps/**"], source="worker:x", body="Candidate wisdom.",
                               evidence=[f"{ORDERS}:5"], status="candidate", created="2026-10-31")
        directory = self.layout.directory("candidate")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "kn-2026-10-06-cand.md").write_text(store.render(candidate))
        text = self.run_tool("ctx_brief", {"paths": [ORDERS]}).text
        knowledge = text.split("== knowledge\n")[1].split("\n==")[0]
        self.assertRegex(knowledge, r"none can be shown \(1 withheld")
        self.assertNotIn("no approved note", knowledge)
        self.assertNotIn("Candidate wisdom", text)

    def test_a_brief_with_no_candidate_still_states_that_no_note_covers_the_files(self):
        text = self.run_tool("ctx_brief", {"paths": [f"{API_SRC}/util.ts"]}).text
        self.assertIn("none: no approved note covers these files", text)

    def test_a_brief_of_a_file_too_large_to_read_never_says_libraries_none(self):
        self.put(f"{API_SRC}/big.ts", "import { z } from 'zod';\n" + "// pad\n" * 160000)
        text = self.run_tool("ctx_brief", {"paths": [f"{API_SRC}/big.ts"]}).text
        libraries = text.split("== libraries\n")[1]
        self.assertNotRegex(libraries, r"^\s*none\s*$")
        self.assertRegex(libraries, r"none can be shown \(1 withheld")

    def test_a_brief_whose_files_were_all_read_still_states_that_no_library_is_imported(self):
        text = self.run_tool("ctx_brief", {"paths": [f"{API_SRC}/util.ts"]}).text
        self.assertRegex(text.split("== libraries\n")[1], r"^\s*none\s*$")


    # ---- the scanner: no absence string outside the helper, and every use of the helper passes a withheld count

    FILES = ("brief.py", "surface.py")
    WORDS = re.compile(r"\b(none|no|nothing|not found|zero|empty)\b|\(0\)|\(0 of", re.I)
    HELPERS = {"absent": 1, "list_head": 3}  # name -> position of the completeness argument (the keyword is `read`)
    OWNED = {"absent", "list_head"}  # function bodies that are the helper
    OWNED_NAMES = {"NONE_SHOWN", "CUT_NOTHING", "PATH_WHY", "NOT_AVAILABLE"}  # constants that are the helper's wording
    BUILDERS = {"Completeness", "of", "completeness", "notes_read"}  # make a record from a read result (the first two from their arguments)
    FROM_ARGUMENTS = {"Completeness", "of"}  # these only count when at least one argument is not a literal
    PRODUCERS = {"locate", "tests_for"}  # functions that read a source and hand back its record as their last result
    # Whole strings (an f-string's placeholders are `{}`), never fragments: a new string that merely contains one of these words must
    # be reviewed, not waved through.
    ALLOWED = {
        "{} must be a bare identifier (letters, digits, _ and $; no spaces or dots)": "argument check text: a rule for the caller's argument",
        "{} must be a list of strings (none empty)": "argument check text: a rule for the caller's argument",
        "no such file": "a briefed path that does not exist: the caller's own argument (a refused path is refused earlier, by Engine.path)",
        "no such file: ": "same, the start of the Next: line that names the missing paths",
        "Next: no such file: ": "same",
        "no such file: {}": "same, for ctx_symbol and ctx_relations",
        "no project: pass --project DIR or run inside a directory with .tmux-pi or .git": "a refusal of the command line: no project directory was given",
        "no package covers this path (no deno.json or tsconfig.json above it), so there is no export or import graph for it":
            "the path has no deno.json/tsconfig.json above it: a fact about directories, not a list a withheld item can hide in",
        "no package covers this path ({}): no deno.json or tsconfig.json above it\nNext: {}": "same fact, as a navigator failure",
        "no lockfile reader": "states why a library version was not read, not a missing item",
        "the {} navigator did not start ({}); using no navigator": "states that the navigator did not start; no list was asked for",
        "references: api files have none here; ctx_relations(target={}, direction='importers') lists who imports this file":
            "a fixed fact about the api navigator (it lists no references)",
        "none": "the package fact line of a brief (`package: none`): a fact about one file, not a list",
    }

    def violations(self, source, name="x.py", used=None):
        import ast
        tree = ast.parse(source)
        parent = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

        def text_of(node):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            if isinstance(node, ast.JoinedStr):
                return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
            return None

        def inside(node):
            while node in parent:
                node = parent[node]
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in self.OWNED:
                    return True
                if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in self.OWNED_NAMES for t in node.targets):
                    return True
            return False

        def callee(call):
            return call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", None)

        def helper_call(node):
            """The helper call that `node` is the absence text argument of, or None."""
            call = parent.get(node)
            if isinstance(call, ast.keyword):
                keyword, call = call.arg, parent.get(call)
            else:
                keyword = None
            if not isinstance(call, ast.Call):
                return None
            func = callee(call)
            if func == "absent" and keyword is None and call.args and call.args[0] is node:
                return call, func
            if func == "list_head" and keyword == "empty":
                return call, func
            return None

        def scope_of(node):
            while node in parent:
                node = parent[node]
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
                    return node
            return tree

        def names_in(target):
            if isinstance(target, ast.Name):
                return [target.id]
            if isinstance(target, (ast.Tuple, ast.List)):
                return [n for elt in target.elts for n in names_in(elt)]
            return []

        def from_read(expr, scope, seen=()):
            """Whether `expr` is a completeness record built from a read result: a builder call that takes something other than literals,
            a producer call, or a name that the enclosing function binds to one of those."""
            if isinstance(expr, ast.Call):
                func = callee(expr)
                if func in self.FROM_ARGUMENTS:
                    def real(arg):  # not a literal, and not a record that is itself made of literals
                        arg = arg.value if isinstance(arg, (ast.Starred, ast.keyword)) else arg
                        if isinstance(arg, ast.Call) and callee(arg) in self.FROM_ARGUMENTS:
                            return from_read(arg, scope, seen)
                        return not isinstance(arg, ast.Constant)
                    return any(real(a) for a in [*expr.args, *expr.keywords])
                return func in self.BUILDERS or func in self.PRODUCERS
            if isinstance(expr, ast.Name) and expr.id not in seen:
                bound = [n.value for n in ast.walk(scope) if isinstance(n, ast.Assign) and expr.id in [x for t in n.targets for x in names_in(t)]]
                return bool(bound) and all(from_read(v, scope, seen + (expr.id,)) for v in bound)
            return False

        def read_argument(call, func):
            where = self.HELPERS[func]
            given = [k.value for k in call.keywords if k.arg == "read"]
            if len(call.args) > where:
                given.append(call.args[where])
            return given[0] if given else None

        def helper_problem(call, func):
            given = read_argument(call, func)
            if given is None:
                return "the helper is called without the completeness of the read"
            if not from_read(given, scope_of(call)):
                return "the completeness argument is not built from a read result"
            return None

        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and callee(node) in self.HELPERS and not inside(node):
                problem = helper_problem(node, callee(node))
                if problem:
                    found.append((name, node.lineno, callee(node), problem))
            text = text_of(node)
            if text is None or isinstance(parent.get(node), ast.JoinedStr):
                continue
            if isinstance(parent.get(node), ast.Expr) or not self.WORDS.search(text) or inside(node):
                continue
            if helper_call(node):
                continue
            hit = [f for f in self.ALLOWED if f == text]
            if used is not None:
                used.update(hit)
            if not hit:
                found.append((name, node.lineno, text, "an absence string outside the helper"))
        return found

    def test_the_scanner_finds_a_bare_absence_and_a_helper_call_without_a_read_record(self):
        self.assertTrue(self.violations('lines.append("tests: none found")'))
        self.assertTrue(self.violations('lines.append(f"nothing imports {rel}")'))
        self.assertTrue(self.violations('x = surface.absent("none found")'))
        self.assertTrue(self.violations('x = surface.absent("none found", 0)'))
        self.assertTrue(self.violations('x = surface.absent("none found", refused)'))
        self.assertTrue(self.violations('x = surface.absent("none found", got.refused)'))
        self.assertTrue(self.violations('x = surface.absent("none found", Completeness())'))
        self.assertTrue(self.violations('x = surface.absent("none found", Completeness(withheld=0, cut=False))'))
        self.assertTrue(self.violations('x = surface.absent("none found", Completeness.of(Completeness(), Completeness()))'))
        self.assertTrue(self.violations('def f():\n    read = 3\n    return surface.absent("none found", read)'))
        self.assertTrue(self.violations('def f(read):\n    return surface.absent("none found", read)'))
        self.assertTrue(self.violations('x = surface.list_head("a", 0, 0, False, empty="nothing imports")'))
        self.assertTrue(self.violations('x = surface.list_head("a", 0, 0, empty="nothing imports", withheld=got.refused)'))
        self.assertTrue(self.violations('x = surface.list_head("a", 0, 0, got.cut, empty="nothing imports")'))

    def test_the_scanner_accepts_a_record_that_is_built_from_a_read_result(self):
        for source in ('x = surface.absent("none found", got.completeness())',
                       'x = surface.absent("none found", surface.Completeness(withheld=refused))',
                       'x = surface.absent("none found", Completeness(unread=len(unread), why=WHY))',
                       'x = surface.absent("none found", Completeness.of(*reads))',
                       'x = surface.absent("none found", surface.notes_read(result), label="x")',
                       'def f():\n    read = got.completeness()\n    return surface.absent("none found", read)',
                       'def f():\n    where, read = self.locate(name)\n    return surface.absent("none found", read)',
                       'x = surface.list_head("a", 0, 0, got.completeness(), empty="nothing imports")',
                       'x = surface.list_head("a", 0, 0, read=Completeness.of(*reads), empty="nothing imports")'):
            with self.subTest(source=source):
                self.assertEqual(self.violations(source), [])
        self.assertFalse(self.violations('"""no x"""\nx = 1'))

    def test_the_scanner_allows_a_whole_string_only_never_a_fragment_of_one(self):
        """Review 6 N3: a new bare absence that merely contains an allowed phrase is still a violation."""
        for text in ("no navigator is running", "no project file imports it", "none: no package covers this path", "no such file: x and none found",
                     "no lockfile reader, so none"):
            with self.subTest(text=text):
                self.assertTrue(self.violations(f"lines.append({text!r})"))
        for text in ("no lockfile reader", "none", "no such file: "):
            self.assertEqual(self.violations(f"lines.append({text!r})"), [], text)

    def test_the_sources_contain_no_absence_string_outside_the_one_helper(self):
        found, used = [], set()
        for name in self.FILES:
            found += self.violations((ROOT / "lib" / "ctxengine" / name).read_text(), name, used)
        self.assertEqual(found, [])
        self.assertEqual(sorted(set(self.ALLOWED) - used), [], "an allowlist entry that no string uses")

class UnreadSources(ReviewOne):
    """Review 6: an absence is stated only when the source was read completely (the read succeeded, nothing was withheld, nothing was
    cut). F1: the brief's contracts section after a failed or never-run imports read. N1: notes that failed to parse count as unread."""

    CONTRACTS_NONE = "the files import no shared (lib) contract"

    def contracts(self, answer):
        return answer.text.split("== contracts\n")[1].split("\n==")[0]

    def assertNoClaim(self, section):
        self.assertNotIn(self.CONTRACTS_NONE, section)
        self.assertNotRegex(section, r"(?m)^none\b")

    def test_f1_a_brief_whose_imports_read_failed_never_says_the_files_import_no_contract(self):
        for kind, path, answer_for in (("web", CART, {"status": "unavailable", "message": "navigator down"}),
                                       ("api", ORDERS, {"status": "unavailable", "message": "navigator down"}),
                                       ("web", CART, {"status": "refused", "reason": "outside_project", "message": "no"})):
            with self.subTest(kind=kind, answer=answer_for["status"]):
                fake_navs.reset()
                fake_navs.BEHAVIOUR[(kind, "imports")] = answer_for
                answer = self.run_tool("ctx_brief", {"paths": [path]})
                section = self.contracts(answer)
                self.assertNoClaim(section)
                self.assertIn("shared (lib) contracts: not available", section)
                self.assertTrue(any("contracts" in item and path in item for item in answer.data["left_out"]), answer.data["left_out"])

    def test_f1_a_brief_of_a_file_no_package_covers_never_says_the_files_import_no_contract(self):
        self.put("loose/tool.ts", "export const x = 1;\n")
        section = self.contracts(self.run_tool("ctx_brief", {"paths": ["loose/tool.ts"]}))
        self.assertNoClaim(section)
        self.assertIn("shared (lib) contracts: not available", section)

    def test_f1_one_unread_file_among_read_ones_is_enough_to_withhold_the_claim(self):
        self.put("loose/tool.ts", "export const x = 1;\n")
        section = self.contracts(self.run_tool("ctx_brief", {"paths": [f"{API_SRC}/util.ts", "loose/tool.ts"]}))
        self.assertNoClaim(section)
        self.assertIn("not available", section)

    def test_f1_a_contract_that_is_found_still_carries_a_warning_when_another_file_was_not_read(self):
        self.put("loose/tool.ts", "export const x = 1;\n")
        text = self.run_tool("ctx_brief", {"paths": [CART, "loose/tool.ts"]}).text
        section = text.split("== contracts\n")[1].split("\n== knowledge")[0]
        self.assertIn(ORDER, section)
        self.assertRegex(section, r"warning: .*loose/tool\.ts.*may be missing")

    def test_f1_control_a_brief_whose_imports_were_all_read_still_states_that_no_contract_is_imported(self):
        section = self.contracts(self.run_tool("ctx_brief", {"paths": [f"{API_SRC}/util.ts"]}))
        self.assertIn("none: " + self.CONTRACTS_NONE, section)

    def invalid_note(self):
        directory = self.layout.directory("approved")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "kn-2026-10-01-broken.md").write_text("---\nid: [this is not closed\nkind: ???\n---\nBroken body.\n")

    def test_n1_an_approved_note_that_failed_to_parse_means_no_matching_notes_is_never_said(self):
        self.invalid_note()
        knowledge = self.run_tool("ctx_knowledge", {"query": "zzzz"})
        self.assertNotIn("No matching notes", knowledge.text)
        self.assertRegex(knowledge.text, r"none can be shown \(1 withheld")
        brief = self.run_tool("ctx_brief", {"paths": [f"{API_SRC}/util.ts"]}).text
        section = brief.split("== knowledge\n")[1].split("\n==")[0]
        self.assertNotIn("none: no approved note", section)
        self.assertRegex(section, r"none can be shown \(1 withheld")
        for text in (knowledge.text, brief):
            self.assertNotIn("kn-2026-10-01-broken", text)
            self.assertNotIn("Broken body", text)

    def test_n1_notes_that_matched_are_shown_and_the_unreadable_one_is_counted_in_left_out(self):
        self.invalid_note()
        add_approved(self.layout, "kn-2026-10-02-good", "Good rule", ["apps/**"], "Parse every input.")
        for tool, args in (("ctx_knowledge", {"paths": [ORDERS]}), ("ctx_brief", {"paths": [ORDERS]})):
            answer = self.run_tool(tool, args)
            self.assertIn("kn-2026-10-02-good", answer.text, tool)
            self.assertTrue(any("1 withheld" in item and "note" in item for item in answer.data["left_out"]), (tool, answer.data["left_out"]))

    def test_n1_control_with_every_note_readable_no_matching_notes_is_still_said(self):
        self.assertIn("No matching notes", self.run_tool("ctx_knowledge", {"query": "zzzz"}).text)


if __name__ == "__main__":
    unittest.main()
