"""Shared fixtures for the model-router tests: catalog loaders, stub `claude` and `pi` executables for PATH, a stub of the
local chat endpoint, and a guard that fails when anything connects to the Laya endpoint (127.0.0.1:11436) or the real
LM Studio port (1234). No live model is ever called.

Tests of chain mechanics run on a fixture catalog (fixture_catalog): the shipped one with the router chain in an order
the test sets, so reordering the shipped chain never breaks them. Stubs writes it and points TP_MODEL_CATALOG at it, so
the commands (tp-route, tp-agent) and in-process calls read it without a flag. Only tests about the shipped chain use
shipped_catalog()."""
import copy
import json
import os
import stat
import sys
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import router_catalog  # noqa: E402

CATALOG_PATH = ROOT / "config" / "model-catalog.json"
HAIKU = "claude-haiku-4-5-20251001"
LUNA = "openai-codex/gpt-6-luna"
QWEN = "lmstudio/qwen/qwen3.6-35b-a3b"
SONNET = "claude-sonnet-5-5"
OPUS = "claude-opus-5-5"
SOL = "openai-codex/gpt-6-sol"
ASTRA = "openai-codex/gpt-6-astra"
LAYA_PORT = 11436
LMSTUDIO_PORT = 1234
# The chain order the mechanics tests are written against; a test that wants another passes order=.
DEFAULT_ORDER = (HAIKU, LUNA, QWEN)


def shipped_catalog():
    return router_catalog.load_catalog(CATALOG_PATH)


def fixture_catalog(order=DEFAULT_ORDER, confirm=False):
    """The shipped catalog with its router chain reordered to `order` (model names). Each entry keeps its shipped transport,
    limits, cap and thinking setting; its confirm rule is dropped unless confirm=True, so a test adds the rule it exercises."""
    cat = copy.deepcopy(json.loads(CATALOG_PATH.read_text(encoding="utf-8")))
    by_model = {e["model"]: e for e in cat["router"]["chain"]}
    chain = [by_model[m] for m in order]
    if not confirm:
        for entry in chain:
            entry.pop("confirm", None)
    cat["router"]["chain"] = chain
    return router_catalog.validate_catalog(cat)


def catalog(order=DEFAULT_ORDER):
    return fixture_catalog(order)


def write_catalog(path, order=DEFAULT_ORDER, change=None):
    """Write a fixture catalog (optionally edited by change(cat)) to path and return the path."""
    cat = fixture_catalog(order)
    if change:
        change(cat)
    Path(path).write_text(json.dumps(cat))
    return Path(path)


def reply(level=3, effort="medium", review="optional", shape=None, reason="stub"):
    return json.dumps({"level": level, "effort": effort, "review": review, "shape": shape, "reason": reason})


STUB = textwrap.dedent('''\
    #!/usr/bin/env python3
    """Stub of the {name} CLI. STUB_SCRIPT is a JSON file: {{key: [step, ...]}}; a step is {{"text": ...}}, {{"exit": N}} or
    {{"sleep": S, "text": ...}}. The next unused step answers each call (the last repeats). Calls are logged to STUB_LOG."""
    import json, os, sys, time
    argv = sys.argv[1:]
    key = {key}
    log = os.environ["STUB_LOG"]
    script = json.load(open(os.environ["STUB_SCRIPT"]))
    steps = script.get(key) or [{{"exit": 1, "text": "no script for " + key}}]
    counter = os.path.join(os.path.dirname(log), "count-" + key.replace("/", "_"))
    n = int(open(counter).read()) if os.path.exists(counter) else 0
    open(counter, "w").write(str(n + 1))
    step = steps[min(n, len(steps) - 1)]
    with open(log, "a") as f:
        f.write(json.dumps({{"tool": "{name}", "key": key, "argv": argv, "cwd": os.getcwd(), "stdin_tty": sys.stdin.isatty(),
                            "max_thinking_tokens": os.environ.get("MAX_THINKING_TOKENS"), "call": n + 1}}) + "\\n")
    if step.get("sleep"):
        time.sleep(step["sleep"])
    if step.get("exit"):
        sys.stderr.write(step.get("text", "failed"))
        sys.exit(step["exit"])
    {emit}
''')

CLAUDE_EMIT = '''usage = {"output_tokens": 57}
if not step.get("no_count"):
    usage["output_tokens_details"] = {"thinking_tokens": step.get("thinking", 0)}
print(json.dumps({"result": step["text"], "is_error": False, "usage": usage}))'''
PI_EMIT = '''print(json.dumps({"type": "message_end", "message": {"role": "assistant", "stopReason": "stop",
          "content": [{"type": "text", "text": step["text"]}], "usage": {"reasoning": step.get("thinking", 0)}}}))'''


def key_of(name):
    if name == "claude":
        return '"claude"'
    return '"pi/" + argv[argv.index("--model") + 1]'


class ChatServer:
    """A stub of the local server's chat-completions endpoint on a loopback port. Steps (script key "chat"): {"text"},
    {"text", "reasoning_tokens"}, {"text", "no_usage": true}, {"status": N}, {"sleep": S, "text"}; the next unused step
    answers each call (the last repeats). Every request body is logged, so tests can check temperature, seed and reasoning."""

    def __init__(self, stubs):
        self.stubs = stubs
        self.calls_made = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                steps = json.loads(outer.stubs.script.read_text()).get("chat") or [{"status": 500}]
                step = steps[min(outer.calls_made, len(steps) - 1)]
                outer.calls_made += 1
                with outer.stubs.log.open("a") as f:
                    f.write(json.dumps({"tool": "chat", "key": "chat", "path": self.path, "body": body, "call": outer.calls_made}) + "\n")
                if step.get("sleep"):
                    time.sleep(step["sleep"])
                if step.get("status"):
                    self.send_response(step["status"])
                    self.end_headers()
                    return
                message = {"role": "assistant", "content": step["text"]}
                usage = {} if step.get("no_usage") else {"completion_tokens": 40, "completion_tokens_details": {
                    "reasoning_tokens": step.get("reasoning_tokens", 0)}}
                data = json.dumps({"choices": [{"index": 0, "finish_reason": step.get("finish", "stop"), "message": message}],
                                   "usage": usage}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:{}/v1".format(self.server.server_address[1])
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def reset(self):
        self.calls_made = 0

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Stubs:
    """Stub claude and pi on a private bin dir, and the local chat endpoint. script maps "claude", "pi/<model>" or
    "chat" (the direct local router) to steps."""

    def __init__(self, base, script, order=DEFAULT_ORDER):
        self.base = Path(base)
        self.catalog_path = write_catalog(self.base / "fixture-catalog.json", order)
        self.bin = self.base / "stubbin"
        self.bin.mkdir(parents=True, exist_ok=True)
        self.log = self.base / "stub.log"
        self.script = self.base / "stub-script.json"
        self.set(script)
        self.chat = ChatServer(self)
        for name, emit in (("claude", CLAUDE_EMIT), ("pi", PI_EMIT)):
            path = self.bin / name
            path.write_text(STUB.format(name=name, key=key_of(name), emit=emit))
            path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def set(self, script):
        self.script.write_text(json.dumps(script))
        for counter in self.base.glob("count-*"):
            counter.unlink()
        if hasattr(self, "chat"):
            self.chat.reset()

    def close(self):
        self.chat.close()

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE", "TP_ROUTER_BACKEND", "TP_ROUTING_EVALS", "TP_MODEL_CATALOG")}
        env.update(PATH=str(self.bin) + os.pathsep + os.environ["PATH"], STUB_LOG=str(self.log), STUB_SCRIPT=str(self.script),
                   TP_LOCAL_CONFIG=str(self.base / "no-local.json"), TP_CLAUDE_CONFIG_DIR=str(self.base / "no-claude-profile"), TP_ROUTER_LLM_URL=self.chat.url,
                   TP_MODEL_CATALOG=str(self.catalog_path))
        env.update(extra)
        return env

    def calls(self, tool=None):
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]
        return [r for r in rows if tool in (None, r["tool"], r["key"])]


GUARD = '''\
import os, socket
_real = socket.socket.connect
_ports = {ports}
def _guard(self, address, *a, **k):
    if isinstance(address, tuple) and len(address) > 1 and address[1] in _ports:
        open(os.environ["LAYA_HIT_LOG"], "a").write(repr(address) + "\\n")
        raise ConnectionRefusedError("a real model endpoint must not be contacted on this path")
    return _real(self, address, *a, **k)
socket.socket.connect = _guard
'''


def laya_guard(base):
    """(env additions, hit log path): every Python process started with these fails and logs a connect to the Laya
    port (11436) or the real LM Studio port (1234). The stubbed chat endpoint is on another port."""
    folder = Path(base) / "guard"
    folder.mkdir(exist_ok=True)
    (folder / "sitecustomize.py").write_text(GUARD.format(ports=repr({LAYA_PORT, LMSTUDIO_PORT})))
    log = Path(base) / "laya-hits.log"
    return {"PYTHONPATH": str(folder) + os.pathsep + os.environ.get("PYTHONPATH", ""), "LAYA_HIT_LOG": str(log)}, log


def block_real_models(test):
    """For in-process tests: connecting to the Laya or real LM Studio port raises, and the test fails at cleanup."""
    import socket
    from unittest import mock
    hits = []
    real = socket.socket.connect

    def guarded(self, address, *a, **k):
        if isinstance(address, tuple) and len(address) > 1 and address[1] in (LAYA_PORT, LMSTUDIO_PORT):
            hits.append(address)
            raise ConnectionRefusedError("a real model endpoint must not be contacted on this path")
        return real(self, address, *a, **k)

    patch = mock.patch.object(socket.socket, "connect", guarded)
    patch.start()
    test.addCleanup(lambda: test.assertEqual(hits, [], "a real model endpoint was contacted"))
    test.addCleanup(patch.stop)
