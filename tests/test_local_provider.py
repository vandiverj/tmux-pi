"""LP1: a machine-local provider alias (config/local.json "provider_aliases") applied only where pi is launched.

The shared id (roster, catalog, routes, meta.json model, review provider checks) stays openai/...; only the --model handed to
pi is the local provider's name. Real tp-spawn launches run once for the class on a private tmux server whose new-window does
not start the worker (tests/lane_fixture.py); each launch's run.sh is then run with fake tp-pi / tp-claude that log their argv."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from session_env import outside_session  # noqa: E402
from lane_fixture import TMUX, write_wrappers  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import llm_router  # noqa: E402
import model_router  # noqa: E402
import router  # noqa: E402

SOL, CODEX_SOL = "openai/gpt-6.1-sol", "openai-codex/gpt-6.1-sol"
SONNET = "claude-sonnet-5-5"
SLOT = "slotstream/qwen3.8-flash-next:4bit"
ALIAS = {"provider_aliases": {"openai": "openai-codex"}}
TASK = "coder-lp1launch01"
# The launches' machine config also gives each model a standing prompt, as "prompts" does: tp-spawn reads both in one start.
PROMPTS = {SOL: "SOL PROMPT line 1\n  line 2 {\"x\": 1}", SONNET: "SONNET PROMPT", SLOT: "SLOT PROMPT"}
LOCAL = dict(ALIAS, prompts=PROMPTS)


BAD = {"provider_aliases": {"openai": "openai"}}  # the review's probe: an alias to itself
MESSAGE = "provider_aliases 'openai' aliases itself"


def shared_config():
    return router.load_config(ROOT / "config" / "router.json", local_path=False, laya_model=False)


class ProviderAliasSettingTests(unittest.TestCase):
    """The setting's input table (load_local): each row is local.json's content and the expected result."""

    def load(self, local):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "local.json"
            if local is not None:
                path.write_text(json.dumps(local))
            return router.load_config(ROOT / "config" / "router.json", local_path=str(path), laya_model=False)

    def test_each_row_of_the_setting_table(self):
        refused = "refused"
        rows = [
            ("no file", None, {}),
            ("no key", {"routes": {}}, {}),
            ("empty mapping", {"provider_aliases": {}}, {}),
            ("this Mac", ALIAS, {"openai": "openai-codex"}),
            ("a provider not in the roster is allowed", {"provider_aliases": {"anthropic": "anthropic-work"}},
             {"anthropic": "anthropic-work"}),
            ("two names to one provider", {"provider_aliases": {"openai": "codex", "azure": "codex"}},
             {"openai": "codex", "azure": "codex"}),
            ("a list", {"provider_aliases": [["openai", "openai-codex"]]}, refused),
            ("a string", {"provider_aliases": "openai-codex"}, refused),
            ("null", {"provider_aliases": None}, refused),
            ("a number", {"provider_aliases": 1}, refused),
            ("a boolean", {"provider_aliases": True}, refused),
            ("a number target", {"provider_aliases": {"openai": 1}}, refused),
            ("a boolean target", {"provider_aliases": {"openai": True}}, refused),
            ("a list target", {"provider_aliases": {"openai": ["openai-codex"]}}, refused),
            ("a null target", {"provider_aliases": {"openai": None}}, refused),
            ("an object target", {"provider_aliases": {"openai": {"name": "openai-codex"}}}, refused),
            ("empty target", {"provider_aliases": {"openai": ""}}, refused),
            ("empty source", {"provider_aliases": {"": "openai-codex"}}, refused),
            ("a model id as target", {"provider_aliases": {"openai": "openai-codex/gpt-6.1-sol"}}, refused),
            ("a model id as source", {"provider_aliases": {"openai/gpt-6.1-sol": "openai-codex"}}, refused),
            ("upper case", {"provider_aliases": {"OpenAI": "openai-codex"}}, refused),
            ("a space", {"provider_aliases": {"openai": "openai codex"}}, refused),
            ("a trailing newline", {"provider_aliases": {"openai": "openai-codex\n"}}, refused),
            ("self-alias", {"provider_aliases": {"openai": "openai"}}, refused),
            ("chain", {"provider_aliases": {"openai": "openai-codex", "openai-codex": "codex2"}}, refused),
            ("cycle", {"provider_aliases": {"openai": "codex", "codex": "openai"}}, refused),
            ("misspelt key (unknown local key)", {"provider_alias": {"openai": "openai-codex"}}, refused),
        ]
        for name, local, expected in rows:
            with self.subTest(name):
                if expected == refused:
                    with self.assertRaises(router.RouterError) as caught:
                        self.load(local)
                    self.assertIn("provider_alias", str(caught.exception))
                else:
                    self.assertEqual(self.load(local)["local"].get("provider_aliases", {}), expected)

    def test_the_rewrite_changes_only_a_matching_provider_prefix(self):
        config = {"local": {"provider_aliases": {"openai": "openai-codex"}}}
        rows = [(SOL, CODEX_SOL), ("openai/gpt-6-astra", "openai-codex/gpt-6-astra"), (SLOT, SLOT),
                ("openai-codex/gpt-6.1-sol", "openai-codex/gpt-6.1-sol"), ("openaix/m", "openaix/m"), (SONNET, SONNET),
                ("openai", "openai"), ("", "")]
        for model, launched in rows:
            with self.subTest(model):
                self.assertEqual(router.launch_model(model, config), launched)
                self.assertEqual(router.launch_model(model, None), model)
                self.assertEqual(router.launch_model(model, {"local": {}}), model)


class RouterLaunchTests(unittest.TestCase):
    """The pi router call (lib/llm_router.py) is a pi launch too; claude routers are untouched."""

    def test_pi_router_argv_uses_the_alias_and_claude_router_argv_does_not(self):
        config = shared_config()
        aliased = dict(config, local=dict(ALIAS))
        pi, claude = {"worker": "pi", "model": SOL}, {"worker": "claude", "model": SONNET}
        model = lambda argv: argv[argv.index("--model") + 1]
        self.assertEqual(model(llm_router.command(pi, "low", "s", "u", aliased)), CODEX_SOL)
        plain = llm_router.command(pi, "low", "s", "u", config)
        self.assertEqual(model(plain), SOL)
        self.assertEqual(llm_router.command(pi, "low", "s", "u"), plain)
        self.assertEqual(llm_router.command(claude, "low", "s", "u", aliased), llm_router.command(claude, "low", "s", "u", config))
        seen = []
        llm_router.call(pi, "low", "s", "u", 5, runner=lambda argv, **kw: seen.append(argv) or
                        subprocess.CompletedProcess(argv, 1, "", "stub"), config=aliased)
        self.assertEqual(model(seen[0]), CODEX_SOL)


@unittest.skipUnless(TMUX, "tmux not installed")
class SpawnLaunchTests(unittest.TestCase):
    """Real tp-spawn launches: (worker, model, local config) -> the argv a fake harness receives and meta.json."""

    LAUNCHES = {"alias": ("pi", SOL, LOCAL), "none": ("pi", SOL, None), "claude": ("claude", SONNET, LOCAL),
                "other": ("pi", SLOT, LOCAL)}

    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        cls.base = Path(temp.name).resolve()
        cls.sock = "tp-lp1-{}".format(os.getpid())
        fake = cls.base / "bin"
        fake.mkdir()
        write_wrappers(fake, cls.sock)
        cls.harness = cls.base / "harness"
        cls.harness.write_text('#!/bin/sh\nprintf \'%s\\0\' "$@" > "$TP_TASK_DIR/argv.log"\n')
        cls.harness.chmod(0o755)
        subprocess.run([TMUX, "-L", cls.sock, "new-session", "-d", "-s", "anchor", "sleep 300"], check=True, capture_output=True)
        cls.addClassCleanup(lambda: subprocess.run([TMUX, "-L", cls.sock, "kill-server"], capture_output=True))
        cls.path = str(fake) + os.pathsep + os.environ["PATH"]
        cls.out = {name: cls.launch(name, *spec) for name, spec in cls.LAUNCHES.items()}

    @classmethod
    def launch(cls, name, worker, model, local):
        root = cls.base / name
        project, state = root / "project", root / "project" / ".tmux-pi"
        (state / "tasks").mkdir(parents=True)
        local_path = root / "local.json"
        if local is not None:
            local_path.write_text(json.dumps(local))
        env = {**outside_session(), "TP_STATE_DIR": str(state), "TP_SESSION": "fixture", "TP_WORK_DIR": str(root / "work-store"),
               "TP_LOCAL_CONFIG": str(local_path), "PATH": cls.path}
        run = subprocess.run([str(ROOT / "bin/tp-spawn"), "--task-id", TASK, "--role", "coder", "--worker", worker,
                              "--model", model, "--thinking", "medium", "--tests", "none", "--workdir", str(project), "--", "do it"],
                             env=env, text=True, capture_output=True, timeout=60)
        assert run.returncode == 0, run.stderr
        task = state / "tasks" / TASK
        script = (task / "run.sh").read_text()
        for name_ in ("tp-pi", "tp-claude"):
            script = script.replace(str(ROOT / "bin" / name_) + " ", str(cls.harness) + " ")
        (root / "run-fake.sh").write_text(script)
        subprocess.run(["bash", str(root / "run-fake.sh")], env=env, capture_output=True, timeout=60, check=True)
        argv = (task / "argv.log").read_text().split("\0")[:-1]
        system = task / "system-prompt.md"
        return {"root": root, "task": task, "argv": [a.replace(str(root), "<root>") for a in argv],
                "meta": json.loads((task / "meta.json").read_text()), "system": system.read_text() if system.exists() else ""}

    @staticmethod
    def model(argv):
        return argv[argv.index("--model") + 1]

    def test_the_alias_is_applied_at_the_pi_launch_and_nowhere_else_in_argv(self):
        alias, none = self.out["alias"]["argv"], self.out["none"]["argv"]
        self.assertEqual(self.model(alias), CODEX_SOL)
        self.assertEqual(self.model(none), SOL)
        at, prompt = alias.index("--model") + 1, alias.index("--append-system-prompt") + 1
        # the aliased launch's config also adds SOL's standing prompt at the end of the system prompt; nothing else differs
        self.assertEqual(alias[prompt], none[prompt] + "\n\n" + PROMPTS[SOL])
        self.assertEqual(alias[:at] + alias[at + 1:prompt] + alias[prompt + 1:], none[:at] + none[at + 1:prompt] + none[prompt + 1:])

    def test_each_launch_gets_its_models_standing_prompt_from_the_same_config(self):
        for name in ("alias", "claude", "other"):
            with self.subTest(name):
                model = self.LAUNCHES[name][1]
                self.assertTrue(self.out[name]["system"].endswith("\n\n" + PROMPTS[model]), self.out[name]["system"][-200:])
        self.assertNotIn("PROMPT", self.out["none"]["system"])

    def test_meta_keeps_the_shared_model_and_records_the_launched_one(self):
        meta = self.out["alias"]["meta"]
        self.assertEqual((meta["worker"], meta["model"], meta["launch_model"]), ("pi", SOL, CODEX_SOL))
        for name in ("none", "claude", "other"):
            with self.subTest(name):
                self.assertNotIn("launch_model", self.out[name]["meta"])
                self.assertEqual(self.out[name]["meta"]["model"], self.LAUNCHES[name][1])

    def test_a_bad_alias_refuses_the_spawn_before_a_task_is_launched(self):
        root = self.base / "bad"
        project, state = root / "project", root / "project" / ".tmux-pi"
        (state / "tasks").mkdir(parents=True)
        (root / "local.json").write_text(json.dumps(BAD))
        env = {**outside_session(), "TP_STATE_DIR": str(state), "TP_SESSION": "fixture",
               "TP_LOCAL_CONFIG": str(root / "local.json"), "PATH": self.path}
        run = subprocess.run([str(ROOT / "bin/tp-spawn"), "--task-id", TASK, "--role", "coder", "--worker", "pi", "--model", SOL,
                              "--thinking", "medium", "--tests", "none", "--workdir", str(project), "--", "do it"],
                             env=env, text=True, capture_output=True, timeout=60)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn(MESSAGE, run.stderr)
        self.assertFalse((state / "tasks" / TASK / "run.sh").exists())

    def test_claude_workers_and_other_providers_are_untouched(self):
        self.assertEqual(self.model(self.out["claude"]["argv"]), SONNET)
        self.assertEqual(self.model(self.out["other"]["argv"]), SLOT)

    def test_review_provider_diversity_is_unchanged_with_the_alias_set(self):
        plans = {}
        for name in ("alias", "none"):
            task = self.out[name]["task"]
            # the pi reporter records the provider pi ran with
            (task / "state.json").write_text(json.dumps({"state": "waiting", "model": self.model(self.out[name]["argv"])}))
            plans[name] = model_router.review_plan(task, base=self.out[name]["root"] / "project" / ".tmux-pi", log=False,
                                                   files_touched=1, force=True)
        self.assertEqual(plans["alias"], plans["none"])
        self.assertEqual(plans["alias"]["author"]["model"], SOL)
        self.assertEqual(plans["alias"]["reviewer"]["worker"], "claude")


class BadAliasRefusesOnEveryRouteTests(unittest.TestCase):
    """Rule 1: a bad provider_aliases refuses, with the loader's text, before any pi is started, on every entry that can start pi."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.local = self.base / "local.json"
        self.local.write_text(json.dumps(BAD))
        fake = self.base / "bin"
        fake.mkdir()
        self.log = self.base / "pi-started"
        pi = fake / "pi"
        pi.write_text('#!/bin/sh\necho "$@" >> "{}"\n'.format(self.log))
        pi.chmod(0o755)
        self.env = {**outside_session(), "TP_LOCAL_CONFIG": str(self.local), "TP_PI_NODE": "",
                    "PATH": str(fake) + os.pathsep + os.environ["PATH"]}

    def test_the_loader_text_is_the_reference(self):
        with mock.patch.dict(os.environ, {"TP_LOCAL_CONFIG": str(self.local)}):
            with self.assertRaises(router.RouterError) as caught:
                router.load_config(ROOT / "config" / "router.json", laya_model=False)
        self.assertEqual(str(caught.exception), MESSAGE)

    def test_model_router_select_stops_before_any_launch(self):
        import route_fixture as fx
        started = []
        runner = lambda argv, **kw: started.append(argv) or subprocess.CompletedProcess(argv, 1, "", "no")
        requests = [("router chain", {"task": "Add pagination with tests", "role": "coder"}),
                    ("explicit model", {"task": "Add pagination", "role": "coder", "model": fx.LUNA})]
        with mock.patch.dict(os.environ, {"TP_LOCAL_CONFIG": str(self.local)}):
            for name, request in requests:
                with self.subTest(name):
                    with self.assertRaises(model_router.RouteStop) as caught:
                        model_router.select(request, runner=runner, log=False, catalog=fx.fixture_catalog(order=(fx.LUNA, fx.QWEN)))
                    self.assertEqual(str(caught.exception), MESSAGE)
        self.assertEqual(started, [])

    def test_tp_route_cli_stops_with_the_text(self):
        run = subprocess.run([str(ROOT / "bin/tp-route"), "--no-log", "--model", SOL, "do the thing"], env=self.env,
                             text=True, capture_output=True, timeout=60)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn(MESSAGE, run.stderr)
        self.assertFalse(self.log.exists())

    def test_tp_pi_refuses_before_starting_pi(self):
        run = subprocess.run([str(ROOT / "bin/tp-pi"), "--model", SOL, "-p", "x"], env=self.env, text=True,
                             capture_output=True, timeout=30)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn(MESSAGE, run.stderr)
        self.assertFalse(self.log.exists())


class PiLauncherAliasTests(unittest.TestCase):
    """Rule 3: bin/tp-pi is the common boundary of every pi start with a model: tp-main, the qualification runners (tools/qual),
    ask_local (agent/extensions/assistant.ts runs bin/tp-pi) and tp-spawn's run.sh. A fake pi logs its argv."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        fake = self.base / "bin"
        fake.mkdir()
        self.record = self.base / "argv.json"
        pi = fake / "pi"
        pi.write_text('#!/usr/bin/env python3\nimport json, os, sys\n'
                      'open(os.environ["TEST_PI_ARGV"], "w").write(json.dumps(sys.argv[1:]))\nprint("stub reply")\n')
        pi.chmod(0o755)
        self.local = self.base / "local.json"
        self.local.write_text(json.dumps(ALIAS))
        self.env = {**outside_session(), "PATH": str(fake) + os.pathsep + os.environ["PATH"], "TEST_PI_ARGV": str(self.record),
                    "TP_PI_NODE": "", "TP_LOCAL_CONFIG": str(self.local), "TP_STATE_DIR": str(self.base / "state")}

    def argv(self, command, args, **env):
        run = subprocess.run([*command, *args], env={**self.env, **env}, text=True, capture_output=True, timeout=30,
                             cwd=str(self.base))
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(self.record.read_text())

    @staticmethod
    def models(argv):
        """Every model value before any "--"."""
        head = argv[:argv.index("--")] if "--" in argv else argv
        return [a for i, a in enumerate(head) if i and head[i - 1] == "--model"] + [a for a in head if a.startswith("--model=")]

    def test_tp_pi_rewrites_each_model_form_once(self):
        tp_pi = [str(ROOT / "bin/tp-pi")]
        rows = [("separate value", ["--model", SOL, "-p", "x"], [CODEX_SOL]),
                ("equals form", ["--model=" + SOL, "-p", "x"], ["--model=" + CODEX_SOL]),
                ("already local", ["--model", CODEX_SOL], [CODEX_SOL]),
                ("other provider", ["--model", SLOT], [SLOT]),
                ("task text after --", ["--model", SOL, "--", "--model", SOL], [CODEX_SOL]),
                ("no model", ["-p", "x"], [])]
        for name, args, expected in rows:
            with self.subTest(name):
                argv = self.argv(tp_pi, args)
                self.assertEqual(self.models(argv), expected)
                if "--" in args:
                    self.assertEqual(argv[argv.index("--"):], args[args.index("--"):])

    def test_no_alias_leaves_the_model_as_it_was(self):
        self.local.write_text("{}")
        self.assertEqual(self.models(self.argv([str(ROOT / "bin/tp-pi")], ["--model", SOL])), [SOL])

    def test_tp_main_pi_launch(self):
        self.assertEqual(self.models(self.argv([str(ROOT / "bin/tp-main")], ["--model", SOL], TP_MAIN_HARNESS="pi")), [CODEX_SOL])

    def test_qualification_runners_pass_through_tp_pi(self):
        qual = str(ROOT / "tools" / "qual")
        sys.path.insert(0, qual)
        self.addCleanup(sys.path.remove, qual)
        for name in ("summarize_run", "summarize_meaning"):
            spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / "qual" / (name + ".py"))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with self.subTest(name):
                self.record.unlink(missing_ok=True)
                if name == "summarize_run":
                    module.run_pi("task", str(self.base), SOL, "", "medium", 20, str(ROOT / "bin/tp-pi"), self.env)
                else:
                    module.judge_once("p", {}, [], [], model=SOL, timeout=20, pi_command=str(ROOT / "bin/tp-pi"), env=self.env)
                self.assertEqual(self.models(json.loads(self.record.read_text())), [CODEX_SOL])


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ReporterSharedNameTests(unittest.TestCase):
    """Rule 2: with the alias, state.json model and byModel carry the shared name; the launched name sits in launched_model.
    The real reporter code runs on synthetic pi events (no model); it learns the shared name from the task's meta.json."""

    SCRIPT = r"""
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
const [root, meta] = process.argv.slice(1);
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'lp1-rep-'));
try {
  fs.writeFileSync(path.join(tmp, 'package.json'), '{"type":"module"}');
  fs.copyFileSync(path.join(root, 'agent/extensions/reporter.ts'), path.join(tmp, 'reporter.ts'));
  fs.cpSync(path.join(root, 'agent/extensions/lib'), path.join(tmp, 'lib'), { recursive: true });
  const mod = await import(pathToFileURL(path.join(tmp, 'reporter.ts')).href);
  const task = path.join(tmp, 'tasks', 'coder-lp1rep');
  fs.mkdirSync(task, { recursive: true });
  fs.writeFileSync(path.join(task, 'meta.json'), meta);
  process.env.TP_TASK_DIR = task;
  process.env.TP_TASK_ID = 'coder-lp1rep';
  const h = {};
  mod.default({ on(n, f) { h[n] = f; } });
  h.session_start({}, { model: { provider: 'openai-codex', id: 'gpt-6.1-sol' } });
  const use = { input: 10, output: 2, cost: { total: 0.1 } };
  h.message_end({ message: { role: 'assistant', provider: 'openai-codex', model: 'gpt-6.1-sol', content: [], usage: use } });
  h.message_end({ message: { role: 'assistant', provider: 'other', model: 'm', content: [], usage: use } });
  console.log(fs.readFileSync(path.join(task, 'state.json'), 'utf8'));
} finally { fs.rmSync(tmp, { recursive: true, force: true }); }
"""

    def state(self, meta):
        run = subprocess.run(["node", "--input-type=module", "-e", self.SCRIPT, str(ROOT), json.dumps(meta)], text=True,
                             capture_output=True, timeout=60, cwd=str(ROOT))
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def test_aliased_task_records_the_shared_name_and_keeps_the_launched_one_beside_it(self):
        state = self.state({"model": SOL, "launch_model": CODEX_SOL})
        self.assertEqual(state["model"], SOL)
        self.assertEqual(set(state["byModel"]), {SOL, "other/m"})
        self.assertEqual(state["byModel"][SOL]["cost"], 0.1)
        self.assertEqual(state["launched_model"], CODEX_SOL)

    def test_no_alias_records_are_as_before(self):
        state = self.state({"model": CODEX_SOL})
        self.assertEqual(state["model"], CODEX_SOL)
        self.assertEqual(set(state["byModel"]), {CODEX_SOL, "other/m"})
        self.assertNotIn("launched_model", state)

    def test_usage_buckets_follow_the_shared_name(self):
        import tp_usage
        state = self.state({"model": SOL, "launch_model": CODEX_SOL})
        text = "\n".join(tp_usage.lines([(False, tp_usage.contribution([state])[0])], 1))
        self.assertIn(SOL, text.splitlines())
        self.assertNotIn(CODEX_SOL, text)


class DiagnosticsTests(unittest.TestCase):
    def test_route_check_lists_the_active_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "local.json"
            local.write_text(json.dumps(ALIAS))
            run = subprocess.run([str(ROOT / "bin/tp-route"), "--check"], text=True, capture_output=True, timeout=60,
                                 env={**outside_session(), "TP_LOCAL_CONFIG": str(local)})
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("provider aliases (pi launch only): openai -> openai-codex", run.stdout)


if __name__ == "__main__":
    unittest.main()
