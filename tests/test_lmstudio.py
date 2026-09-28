import json
import shutil
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from router import load_config, provider_models  # noqa: E402

QWEN = "qwen/qwen3.6-35b-a3b"

# A stand-in lms: loaded models live in a JSON file; every call is logged.
FAKE_LMS = """#!/usr/bin/env bash
state="$(dirname "$0")/loaded.json"
[[ -f "$state" ]] || echo '[]' > "$state"
echo "$*" >> "$(dirname "$0")/calls.log"
case "$1" in
  ps) cat "$state" ;;
  load) jq --arg id "$2" '. + [{identifier: $id, contextLength: 262144}]' "$state" > "$state.tmp" && mv "$state.tmp" "$state" ;;
  server) ;;
esac
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
        for name in ("router.json", "laya-model.json"):
            shutil.copy(ROOT / "config" / name, self.root / "config" / name)
        (self.root / "agent").mkdir()
        (self.root / "agent/models.json").write_text(json.dumps({"providers": {"lmstudio": {
            "baseUrl": self.base_url, "models": [{"id": QWEN, "contextWindow": 65536}]}}}))
        self.lms = self.root / "lms" / "lms"
        self.lms.parent.mkdir()
        self.lms.write_text(FAKE_LMS)
        self.lms.chmod(0o755)

    def local(self, value):
        (self.root / "config/local.json").write_text(json.dumps(value))

    def run_tool(self, *args):
        return subprocess.run([str(self.root / "bin/tp-lmstudio"), *args], capture_output=True, text=True,
                              env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "TP_LMS": str(self.lms),
                                   "TP_LOCAL_CONFIG": str(self.root / "config/local.json")})

    def calls(self):
        log = self.lms.parent / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

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
        self.assertIn("load {} -y --context-length 65536".format(QWEN), self.calls())
        self.run_tool()
        self.assertEqual(sum(call.startswith("load") for call in self.calls()), 1)
        self.assertEqual(self.run_tool("status").returncode, 0)

    def test_nothing_to_do_without_lmstudio_models(self):
        self.local({})
        result = self.run_tool("ensure")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(self.calls(), [])

    def test_explicit_model_and_unreachable_server(self):
        self.local({})
        self.assertEqual(self.run_tool("ensure", "other/model").returncode, 0)
        self.assertIn("load other/model -y", self.calls())
        self.server.shutdown()
        self.server.server_close()
        result = self.run_tool("status", "other/model")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not answering", result.stderr)


if __name__ == "__main__":
    unittest.main()
