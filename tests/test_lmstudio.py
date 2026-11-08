import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import json
import shutil
import signal
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from router import load_config, provider_models  # noqa: E402

QWEN = "qwen/qwen3.6-35b-a3b"

# A stand-in lms: loaded models live in a JSON file; every call is logged. A load takes --context-length (262144 when
# none is given, the size this Mac's model came up at); unload drops the identifier.
FAKE_LMS = """#!/usr/bin/env bash
state="$(dirname "$0")/loaded.json"
[[ -f "$state" ]] || echo '[]' > "$state"
echo "$*" >> "$(dirname "$0")/calls.log"
case "$1" in
  ps) cat "$state" ;;
  load)
    ctx=262144; ttl=null; prev=""
    for arg in "$@"; do
      [[ "$prev" == --context-length ]] && ctx="$arg"
      [[ "$prev" == --ttl ]] && ttl=$((arg * 1000))
      prev="$arg"
    done
    jq --arg id "$2" --argjson ctx "$ctx" --argjson ttl "$ttl" '. + [{identifier: $id, contextLength: $ctx, ttlMs: $ttl}]' "$state" > "$state.tmp" && mv "$state.tmp" "$state" ;;
  unload) jq --arg id "$2" '[.[] | select(.identifier != $id)]' "$state" > "$state.tmp" && mv "$state.tmp" "$state" ;;
  server) ;;
esac
"""

# A stand-in tp-agent: `list` prints the rows the test wrote to rows.json, whatever state directory it is given.
FAKE_AGENT = """#!/usr/bin/env bash
cat "$(dirname "$0")/rows.json"
"""


class Models(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/v1/models" else 404)
        self.end_headers()
        self.wfile.write(b'{"data": []}')

    def log_message(self, *args):
        pass


class LmStudioTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Models)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base_url = "http://127.0.0.1:{}/v1".format(self.server.server_port)
        (self.root / "bin").mkdir()
        shutil.copy(ROOT / "bin/tp-lmstudio", self.root / "bin/tp-lmstudio")
        (self.root / "lib").symlink_to(ROOT / "lib")
        (self.root / "config").mkdir()
        for name in ("router.json", "laya-model.json", "local-model.json"):
            shutil.copy(ROOT / "config" / name, self.root / "config" / name)
        (self.root / "agent").mkdir()
        (self.root / "agent/models.json").write_text(json.dumps({"providers": {"lmstudio": {
            "baseUrl": self.base_url, "models": [{"id": QWEN, "contextWindow": 131072}]}}}))
        self.lms = self.root / "lms" / "lms"
        self.lms.parent.mkdir()
        self.lms.write_text(FAKE_LMS)
        self.lms.chmod(0o755)
        self.agent = self.root / "tp-agent"
        self.agent.write_text(FAKE_AGENT)
        self.agent.chmod(0o755)
        self.workers([])

    def local(self, value):
        (self.root / "config/local.json").write_text(json.dumps(value))

    def local_model(self, value):
        (self.root / "config/local-model.json").write_text(json.dumps({"local_model": value}))

    def workers(self, rows):
        (self.root / "rows.json").write_text(rows if isinstance(rows, str) else json.dumps(rows))

    def loaded(self, context):
        (self.lms.parent / "loaded.json").write_text(json.dumps([{"identifier": QWEN, "contextLength": context}]))

    def run_tool(self, *args):
        return subprocess.run([str(self.root / "bin/tp-lmstudio"), *args], capture_output=True, text=True,
                              env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "TP_LMS": str(self.lms),
                                   "TP_AGENT": str(self.agent), "TP_STATE_DIR": str(self.root / "state"),
                                   "TP_LOCAL_CONFIG": str(self.root / "config/local.json")})

    def calls(self):
        log = self.lms.parent / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def changes(self):
        return [call for call in self.calls() if call.startswith(("load", "unload"))]

    def test_provider_models(self):
        self.local({"routes": {"standard": {"model": "lmstudio/" + QWEN, "thinking": "off"}},
                    "followup": {"base_url": self.base_url + "/", "model": "judge", "ask_between": [0.25, 0.9]}})
        config = load_config(self.root / "config/router.json")
        self.assertEqual(provider_models(config, "lmstudio", self.base_url), [QWEN, "judge"])
        self.assertEqual(provider_models(config, "lmstudio"), [QWEN])
        self.assertEqual(provider_models(config, "slotstream"), ["qwen3.8-flash-next:4bit"])

    def test_ensure_loads_a_configured_model_once(self):
        self.local({"routes": {"standard": {"model": "lmstudio/" + QWEN, "thinking": "off"}}})
        status = self.run_tool("status")
        self.assertEqual((status.returncode, status.stdout.strip()), (1, QWEN + ": not loaded"))
        first = self.run_tool("ensure")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("load {} -y --context-length 131072 --ttl 1800".format(QWEN), self.calls())
        self.run_tool()
        self.assertEqual(sum(call.startswith("load") for call in self.calls()), 1)
        status = self.run_tool("status")
        self.assertEqual(status.returncode, 0)
        self.assertIn("context 131072", status.stdout)
        self.assertIn("ttl 1800 s", status.stdout)
        self.assertIn("engine rss", status.stdout)

    def test_fresh_load_takes_the_context_and_ttl_from_local_model(self):
        self.local({"routes": {"standard": {"model": "lmstudio/" + QWEN, "thinking": "off"}}})
        self.local_model({"context": 65536, "ttl_seconds": 600})
        self.assertEqual(self.run_tool("ensure").returncode, 0)
        self.assertEqual(self.changes(), ["load {} -y --context-length 65536 --ttl 600".format(QWEN)])

    def test_model_loaded_above_its_context_is_unloaded_and_reloaded(self):
        self.local({"routes": {"standard": {"model": "lmstudio/" + QWEN, "thinking": "off"}}})
        self.loaded(262144)
        result = self.run_tool("ensure")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.changes(), ["unload " + QWEN, "load {} -y --context-length 131072 --ttl 1800".format(QWEN)])
        self.run_tool()
        self.assertEqual(len(self.changes()), 2)

    def test_a_loaded_model_within_ten_percent_is_left_alone(self):
        self.local({"routes": {"standard": {"model": "lmstudio/" + QWEN, "thinking": "off"}}})
        self.loaded(144179)  # 131072 * 1.1 = 144179.2
        self.assertEqual(self.run_tool("ensure").returncode, 0)
        self.assertEqual(self.changes(), [])

    def test_a_live_local_worker_of_this_session_only_warns(self):
        self.local({"routes": {"standard": {"model": "lmstudio/" + QWEN, "thinking": "off"}}})
        live = {"status": "working", "harness": "pi", "model": "lmstudio/" + QWEN}
        cases = [
            ("live local worker", [live], True),
            ("worker that finished", [dict(live, status="done")], False),
            ("worker on another provider", [dict(live, model="openai/gpt")], False),
            ("unreadable listing", "not json", True),
        ]
        for label, rows, blocked in cases:
            with self.subTest(label):
                self.loaded(262144)
                self.workers(rows)
                (self.lms.parent / "calls.log").unlink(missing_ok=True)
                result = self.run_tool("ensure")
                self.assertEqual(result.returncode, 0, result.stderr)
                if blocked:
                    self.assertEqual(self.changes(), [])
                    self.assertIn("unload " + QWEN, result.stderr)
                    self.assertIn("--context-length 131072 --ttl 1800", result.stderr)
                else:
                    self.assertEqual(self.changes(), ["unload " + QWEN, "load {} -y --context-length 131072 --ttl 1800".format(QWEN)])

    def test_reload_if_larger_false_only_warns(self):
        self.local({"routes": {"standard": {"model": "lmstudio/" + QWEN, "thinking": "off"}}})
        self.local_model({"reload_if_larger": False})
        self.loaded(262144)
        result = self.run_tool("ensure")
        self.assertEqual(self.changes(), [])
        self.assertIn("unload " + QWEN, result.stderr)

    def test_unload_only_loaded_models_and_only_when_idle(self):
        self.local({"routes": {"standard": {"model": "lmstudio/" + QWEN, "thinking": "off"}}})
        self.run_tool("unload")
        self.assertEqual(self.changes(), [])
        self.loaded(131072)
        self.workers([{"status": "needs_input", "harness": "pi", "model": "lmstudio/" + QWEN}])
        self.assertEqual(self.run_tool("unload", "--if-idle", str(self.root / "other")).returncode, 0)
        self.assertEqual(self.changes(), [])
        self.workers([{"status": "done", "harness": "pi", "model": "lmstudio/" + QWEN}])
        self.assertEqual(self.run_tool("unload", "--if-idle", str(self.root / "other")).returncode, 0)
        self.assertEqual(self.changes(), ["unload " + QWEN])

    def test_nothing_to_do_without_lmstudio_models(self):
        self.local({})
        result = self.run_tool("ensure")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(self.calls(), [])

    def test_explicit_model_and_unreachable_server(self):
        self.local({})
        self.assertEqual(self.run_tool("ensure", "other/model").returncode, 0)
        self.assertIn("load other/model -y --ttl 1800", self.calls())
        self.server.shutdown()
        self.server.server_close()
        result = self.run_tool("status", "other/model")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not answering", result.stderr)

    def test_status_shows_the_loaded_models_own_ttl(self):
        # F2: the TTL is the loaded model's ttlMs, not the configured one: null is "none", 600000 is 600 s.
        self.local({"routes": {"standard": {"model": "lmstudio/" + QWEN, "thinking": "off"}}})
        for ttl_ms, shown in ((None, "ttl none"), (600000, "ttl 600 s")):
            (self.lms.parent / "loaded.json").write_text(json.dumps(
                [{"identifier": QWEN, "contextLength": 131072, "ttlMs": ttl_ms}]))
            out = self.run_tool("status").stdout
            self.assertIn(", " + shown + ",", out)
            self.assertNotIn("ttl 1800", out)

    def test_spawn_passes_its_state_dir_to_ensure(self):
        # F1: with the workspace picked by TP_SESSION, TP_STATE_DIR is not exported; tp-spawn must hand it over, or
        # lms_busy("") counts the session as busy and an oversized model is never reloaded.
        spawn = (ROOT / "bin/tp-spawn").read_text()
        # (A source check: a spawn run needs the whole session fixture; the reload with a state dir is covered above.)
        self.assertIn('TP_STATE_DIR="$state_dir" "$ROOT/bin/tp-lmstudio" ensure', spawn)

    def test_status_counts_only_lm_studio_engine_processes(self):
        # F3: an unrelated train_mlx.py is not LM Studio.
        ps = self.root / "ps"
        ps.write_text("#!/bin/sh\ncat \"$(dirname \"$0\")/ps.out\"\n")
        ps.chmod(0o755)
        (self.root / "ps.out").write_text(
            " 1048576 /Applications/LM Studio.app/Contents/MacOS/LM Studio\n"
            " 1048576 /Users/me/.lmstudio/bin/lms daemon up\n"
            " 2097152 /Users/me/.lmstudio/extensions/backends/mlx-llm/bin/llmworker --model x\n"
            " 4194304 python3 /Users/me/train_mlx.py --lm studio\n"
            " 8388608 /usr/bin/python3 mlx_lm.server\n")
        self.local({"routes": {"standard": {"model": "lmstudio/" + QWEN, "thinking": "off"}}})
        self.loaded(131072)
        env = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "TP_LMS": str(self.lms), "TP_PS": str(ps),
               "TP_LOCAL_CONFIG": str(self.root / "config/local.json")}
        out = subprocess.run([str(self.root / "bin/tp-lmstudio"), "status"], capture_output=True, text=True, env=env)
        # only the llmworker (2 GB): not the 1 GB UI, the 1 GB lms daemon, train_mlx.py or mlx_lm.server
        self.assertIn("engine rss 2048 MB", out.stdout)


EXIT_TMUX = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_DIR/tmux.log"
case "$1" in
  has-session) [ ! -e "$FAKE_DIR/killed" ] ;;
  kill-session) : > "$FAKE_DIR/killed" ;;
  list-sessions)
    # killing the last session shuts the server down
    if [ -e "$FAKE_DIR/killed" ] && [ -z "$FAKE_OTHER" ]; then exit 1; fi
    printf '%s\\n' "$FAKE_SESSION"; [ -z "$FAKE_OTHER" ] || printf 'other\\n' ;;
  show-options)
    case "$4$5" in
      "=$FAKE_SESSION:@tp_state_dir") printf '%s\\n' "$FAKE_DIR/mine" ;;
      "=other:@tp_state_dir") printf '%s\\n' "$FAKE_OTHER" ;;
    esac ;;
esac
"""


class ExitUnloadTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name).resolve()
        root = self.dir / "root"
        (root / "bin").mkdir(parents=True)
        (root / "config").mkdir()
        (root / "lib").symlink_to(ROOT / "lib")
        shutil.copy(ROOT / "bin/tp-exit", root / "bin/tp-exit")
        for name in ("tp-reset", "tp-task", "tp-agent"):
            (root / "bin" / name).write_text("#!/bin/sh\nexit 0\n")
            (root / "bin" / name).chmod(0o755)
        (root / "bin/tp-lmstudio").write_text('#!/bin/sh\necho "$*" >> "$FAKE_DIR/lms.log"\n')
        (root / "bin/tp-lmstudio").chmod(0o755)
        (root / "config/local-model.json").write_text(json.dumps({"local_model": {"unload_on_exit": True}}))
        (self.dir / "mine").mkdir()
        fake = self.dir / "fake-bin"
        fake.mkdir()
        (fake / "tmux").write_text(EXIT_TMUX)
        (fake / "tmux").chmod(0o755)
        self.root, self.fake = root, fake

    def run_exit(self, other="", expect=0):
        env = {"PATH": str(self.fake) + ":/usr/bin:/bin:/opt/homebrew/bin", "FAKE_DIR": str(self.dir),
               "FAKE_SESSION": "mine", "FAKE_OTHER": other, "TP_SESSION": "mine"}
        result = subprocess.run([str(self.root / "bin/tp-exit")], capture_output=True, text=True, env=env, timeout=25)
        self.assertEqual(result.returncode, expect, result.stderr)
        # The unload runs detached: wait (at most 5 s, every 50 ms) for the stub to record it.
        log = self.dir / "lms.log"
        for _ in range(100):
            if log.exists() and log.read_text().endswith("\n"):
                break
            time.sleep(0.05)
        return log.read_text().splitlines() if log.exists() else []

    def test_last_session_still_unloads(self):
        self.assertEqual(self.run_exit(), ["unload --if-idle"])

    def test_other_sessions_state_is_checked(self):
        other = self.dir / "other-state"
        other.mkdir()
        self.assertEqual(self.run_exit(str(other)), ["unload --if-idle " + str(other)])

    def test_unload_survives_the_caller_being_killed(self):
        # tp-exit may run in a pane that tp-reset kills: the stub kills its parent, the tp-exit process.
        reset = self.root / "bin/tp-reset"
        reset.write_text("#!/bin/sh\nkill -KILL $PPID\n")
        self.assertEqual(self.run_exit(expect=-signal.SIGKILL), ["unload --if-idle"])


if __name__ == "__main__":
    unittest.main()
