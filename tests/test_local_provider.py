"""LP1: a machine-local provider alias (config/local.json "provider_aliases") applied only where pi is launched.

The shared id (roster, catalog, routes, meta.json model, review provider checks) stays openai/...; only the --model handed to
pi is the local provider's name. Real tp-spawn launches run once for the class on a private tmux server whose new-window does
not start the worker (tests/lane_fixture.py); each launch's run.sh is then run with fake tp-pi / tp-claude that log their argv."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

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
