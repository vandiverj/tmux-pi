"""Call the router chain: a small model reads the fixed-form card and returns a level and an effort.

Each router gets no tools, no repository context, no session and no standard input, and runs from a temporary
directory, so it sees only the system prompt generated from the catalog and the card. The chain is in the
catalog (its order, each router's maximum input, transport, thinking expectation and confirm rule are data); an
entry's effort "lowest" means the lowest the model allows. A call that fails or times out moves to the next router;
a reply that does not parse, or that reports thinking tokens from a router that must not think, is retried on the
same router, then the next router is tried.

Sampling: the local router is called directly on the local server's chat-completions endpoint (transport direct_chat) with
temperature 0, a fixed seed and reasoning off, the way the follow-up judge does, taking its base URL and model from
the same local configuration (lib/router.py load_config, local.followup). The claude and pi CLIs have no temperature
or seed option, so the other routers cannot be set to temperature 0 through them: claude has no such flag, and pi
has none either (its cloud reasoning models do not accept a temperature at all).
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import router  # noqa: E402
import router_catalog as rc  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PI_FLAGS = ["-p", "--mode", "json", "--no-tools", "--no-session", "--no-context-files", "--no-extensions",
            "--no-skills", "--no-prompt-templates", "--no-mcp"]
CLAUDE_FLAGS = ["-p", "--setting-sources", "", "--strict-mcp-config", "--tools", "", "--no-session-persistence",
                "--output-format", "json"]


def _environment(entry):
    env = dict(os.environ)
    env.update(entry.get("env", {}))
    if entry["worker"] == "claude" and "CLAUDE_CONFIG_DIR" not in env:
        # The profile the tmux-pi Claude workers use, so the router call is signed in the same way.
        profile = Path(env.get("TP_CLAUDE_CONFIG_DIR") or Path.home() / ".config" / "tmux-pi" / "claude")
        if profile.is_dir():
            env["CLAUDE_CONFIG_DIR"] = str(profile)
    if entry["worker"] == "pi" and "PI_CODING_AGENT_DIR" not in env and (ROOT / "agent").is_dir():
        env["PI_CODING_AGENT_DIR"] = str(ROOT / "agent")
    return env


def command(entry, effort, system, user):
    if entry["worker"] == "claude":
        return ["claude", *CLAUDE_FLAGS, "--model", entry["model"], "--effort", effort, "--system-prompt", system, user]
    return ["pi", *PI_FLAGS, "--system-prompt", system, "--model", entry["model"], "--thinking", effort, user]


def _claude_reply(out):
    data = json.loads(out)
    if data.get("is_error"):
        raise RuntimeError(str(data.get("result", "error"))[:200])
    usage = data.get("usage") or {}
    return data.get("result", ""), (usage.get("output_tokens_details") or {}).get("thinking_tokens")


def _pi_reply(out):
    last = None
    for line in out.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "message_end" and event.get("message", {}).get("role") == "assistant":
            last = event["message"]
    if not last:
        raise RuntimeError("no assistant message")
    if last.get("stopReason") != "stop":
        raise RuntimeError(str(last.get("errorMessage") or last.get("stopReason"))[:200])
    return "".join(c.get("text", "") for c in last.get("content", []) if c.get("type") == "text"), (last.get("usage") or {}).get("reasoning")


MAX_TOKENS = 300
PROTECTED = {"model", "temperature", "seed", "max_tokens", "messages", "stream"}


def local_endpoint(entry, config=None):
    """(base_url, model, extra_body) for a direct chat router, from the local configuration the follow-up judge uses
    (config["local"]["followup"]: base_url, model, extra_body), else the local server's entry in agent/models.json
    (keyed by the model name's provider prefix, as the redaction reviewer reads it) and the model name without that
    prefix. TP_ROUTER_LLM_URL overrides the base URL (the redaction reviewer has the same kind of override). The URL
    must be a loopback one."""
    prefix, _, bare = entry["model"].partition("/")
    judge = ((config or {}).get("local") or {}).get("followup") or {}
    base_url = os.environ.get("TP_ROUTER_LLM_URL") or judge.get("base_url")
    if not base_url:
        base_url = json.loads((ROOT / "agent" / "models.json").read_text(encoding="utf-8"))["providers"][prefix]["baseUrl"]
    extra = {k: v for k, v in (judge.get("extra_body") or {}).items() if k not in PROTECTED}
    return router.local_base_url(base_url), judge.get("model") or bare, extra


def _chat_reply(data):
    choice = data["choices"][0]
    message = choice.get("message") or {}
    if choice.get("finish_reason") not in (None, "stop"):
        raise RuntimeError("finish_reason {}".format(choice.get("finish_reason")))
    thinking = ((data.get("usage") or {}).get("completion_tokens_details") or {}).get("reasoning_tokens")
    reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
    if not isinstance(thinking, int) and reasoning.strip():
        thinking = max(1, len(reasoning) // 4)
    return message.get("content") or "", thinking


def _chat_call(entry, system, user, timeout, config, opener):
    base_url, model, extra = local_endpoint(entry, config)
    body = dict(extra, model=model, temperature=entry.get("temperature", 0), max_tokens=MAX_TOKENS, stream=False,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    if "seed" in entry:
        body["seed"] = entry["seed"]
    if entry.get("thinking_off"):
        # Forced, not defaulted: the follow-up judge's extra_body may set a reasoning effort of its own.
        body["reasoning_effort"] = "none"
    request = urllib.request.Request(base_url + "/chat/completions", json.dumps(body).encode("utf-8"),
                                     {"Content-Type": "application/json"})
    with (opener or router.local_open)(request, timeout) as reply:
        return _chat_reply(json.loads(reply.read()))


def call(entry, effort, system, user, timeout, runner=subprocess.run, config=None, opener=None):
    """One router call. Returns {"ok", "text", "seconds", "thinking_tokens", "error"}; never raises."""
    started = time.monotonic()
    try:
        if entry.get("transport", "cli") == "direct_chat":
            text, thinking = _chat_call(entry, system, user, timeout, config, opener)
        else:
            done = runner(command(entry, effort, system, user), capture_output=True, text=True, timeout=timeout,
                          cwd=tempfile.gettempdir(), stdin=subprocess.DEVNULL, env=_environment(entry))
            if done.returncode != 0:
                raise RuntimeError("exit {}: {}".format(done.returncode, (done.stderr or done.stdout).strip()[:200]))
            text, thinking = (_claude_reply if entry["worker"] == "claude" else _pi_reply)(done.stdout)
        return {"ok": True, "text": text, "seconds": round(time.monotonic() - started, 2), "thinking_tokens": thinking, "error": None}
    except (TimeoutError, subprocess.TimeoutExpired):
        error = "timed out after {:g}s".format(timeout)
    except (OSError, RuntimeError, ValueError, KeyError, IndexError, TypeError, urllib.error.URLError, router.RouterError) as exc:
        error = "{}: {}".format(type(exc).__name__, exc)[:240]
    return {"ok": False, "text": "", "seconds": round(time.monotonic() - started, 2), "thinking_tokens": None, "error": error}


def thinking_problem(entry, thinking):
    """For a router that must not think (catalog thinking_off): ("fail", reason) when the reply reports thinking tokens,
    ("note", text) when the CLI reports no count (accepted, and recorded), else None."""
    if not entry.get("thinking_off"):
        return None
    if thinking is None:
        return "note", "thinking tokens not reported by the {}; accepted unverified".format(
            "endpoint" if entry.get("transport") == "direct_chat" else entry["worker"] + " CLI")
    if thinking > 0:
        return "fail", "reported {} thinking tokens; thinking is meant to be off".format(thinking)
    return None


def run_chain(cat, card, health=None, local_only=False, runner=subprocess.run, clock=time.monotonic, config=None, opener=None):
    """Ask the chain about a card. Returns {"decision", "decided_by", "attempts", "reason", "prompt_sha", "confirmation"}.

    decision is None when no router gave a usable reply; reason then says why each attempt failed. A router with a
    catalog confirm rule whose pick triggers it hands the card to the next router, whose reply then stands.
    """
    from route_resolve import choose_router, confirm_reason
    system = rc.build_system(cat)
    user = rc.user_message(card)
    size = (len(system) + len(user)) // 4
    settings = cat["router"]
    deadline = clock() + settings["budget_seconds"]
    attempts, tried = [], []
    out = {"decision": None, "decided_by": None, "attempts": attempts, "prompt_sha": rc.prompt_sha(system), "reason": "",
           "confirmation": None, "input_tokens": size}

    def ask(entry):
        """(decision, decided_by) from one router, retrying a bad or thinking reply; None when it gave nothing usable."""
        effort = rc.lowest_effort(cat, entry)
        for attempt in range(1 + settings["retries_on_invalid"]):
            remaining = deadline - clock()
            if remaining <= 0:
                attempts.append({"model": entry["model"], "ok": False, "error": "router budget used up"})
                return None
            result = call(entry, effort, system, user, min(entry["timeout_seconds"], remaining), runner, config, opener)
            note = {"model": entry["model"], "effort": effort, "ok": result["ok"], "seconds": result["seconds"],
                    "attempt": attempt + 1, "thinking_tokens": result["thinking_tokens"], "error": result["error"]}
            attempts.append(note)
            if not result["ok"]:
                return None
            problem = thinking_problem(entry, result["thinking_tokens"])
            if problem and problem[0] == "fail":
                note.update(ok=False, error=problem[1])
                continue
            try:
                decision = rc.parse_decision(result["text"], cat)
            except (ValueError, TypeError) as exc:
                note.update(ok=False, error="invalid reply: {}".format(str(exc)[:120]))
                continue
            if problem:
                note["note"] = problem[1]
            return decision, {"provider": entry["provider"], "worker": entry["worker"], "model": entry["model"],
                              "effort": effort, "max_level": entry.get("max_level")}
        return None

    pending = None  # (router that asked, why): its pick waits for the next router's confirmation
    while True:
        entry = choose_router(cat, health, local_only, skip=tried)
        if entry is None:
            break
        tried.append(entry["model"])
        if entry.get("max_input_tokens") and size > entry["max_input_tokens"]:
            attempts.append({"model": entry["model"], "ok": False, "skipped": True,
                             "error": "input about {} tokens is over this router's maximum of {}".format(size, entry["max_input_tokens"])})
            continue
        got = ask(entry)
        if got is None:
            if clock() >= deadline:
                break
            continue
        if pending:
            out["confirmation"] = {"asked": pending[0], "why": pending[1], "confirmed_by": entry["model"],
                                   "first_level": out["decision"]["level"], "level": got[0]["level"]}
            pending = None
        out["decision"], out["decided_by"] = got
        why = confirm_reason(cat, entry, got[0])
        if not why:
            return out
        pending = (entry["model"], why)
    if pending:
        out["confirmation"] = {"asked": pending[0], "why": pending[1], "confirmed_by": None,
                               "note": "no further router answered; the pick stands"}
    if out["decision"] is None:
        out["reason"] = "; ".join("{}: {}".format(a["model"], a["error"]) for a in attempts if a.get("error")) or "no router available"
    return out
