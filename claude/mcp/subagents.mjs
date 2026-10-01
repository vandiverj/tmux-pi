#!/usr/bin/env node
// subagents MCP server — the Claude-main equivalent of agent/extensions/subagents.ts.
// Exposes spawn/list/send/collect/review/kill over the bin/tp-* commands. Exposes no
// tools in a tp-spawn worker (TP_TASK_DIR) or outside tmux, like the Pi extension.
import { execFile } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createInterface } from "node:readline";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const bin = (n) => join(ROOT, "bin", n);
const active = !process.env.TP_TASK_DIR && !!process.env.TMUX;
const THINKING = ["off", "minimal", "low", "medium", "high", "xhigh", "max"];
const lastInput = new Map();

const run = (cmd, args, input) =>
  new Promise((resolve) => {
    const child = execFile(cmd, args, { timeout: 120000 }, (err, stdout, stderr) =>
      resolve({ code: err ? (typeof err.code === "number" ? err.code : 1) : 0, stdout: String(stdout), stderr: String(stderr) }));
    if (input !== undefined) child.stdin?.end(input);
  });
const must = async (cmd, args, input) => {
  const r = await run(cmd, args, input);
  if (r.code !== 0) throw new Error((r.stderr || r.stdout).trim() || `${cmd} exited with ${r.code}`);
  return r.stdout.trim();
};
const readText = (f) => { try { return readFileSync(f, "utf8").trim(); } catch { return ""; } };
const readJson = (f) => { try { return JSON.parse(readFileSync(f, "utf8")); } catch { return {}; } };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const tmuxTarget = process.env.TMUX_PANE ? ["-t", process.env.TMUX_PANE] : [];
const stateDir = () => must("tmux", ["display-message", "-p", ...tmuxTarget, "#{@tp_state_dir}"]);
const taskDir = async (id) => {
  if (!/^[\w.-]+$/.test(id)) throw new Error(`bad subagent id: ${id}`);
  const dir = join(await stateDir(), "tasks", id);
  if (!existsSync(join(dir, "meta.json"))) throw new Error(`no subagent '${id}' (see list_subagents)`);
  return dir;
};
const paneState = async (dir) => {
  const pane = readJson(join(dir, "meta.json")).pane;
  const panes = await must("tmux", ["list-panes", "-a", "-F", "#{pane_id} #{pane_dead}"]);
  const line = panes.split("\n").find((l) => l.split(" ")[0] === pane);
  if (!line) return "killed";
  if (line.endsWith(" 0")) return "running";
  const code = readText(join(dir, "exit")) || "?";
  return code === "0" ? "done" : `failed:${code}`;
};

const str = (description) => ({ type: "string", description });
const TOOLS = {
  spawn_subagent: {
    description:
      "Start a subagent in its own tmux-pi session to work on a task, and return its id. The subagent gets a context packet " +
      "and nothing else: no CLAUDE.md or this conversation. The packet is the task, then `files` (whole, or just the lines that " +
      "matter), then `notes`. Pick only what the task needs, and say what to report back. Leave model and thinking unset to let " +
      "the local router pick them. Runs in the background; use collect_result to wait for its reply.",
    schema: {
      type: "object", required: ["task"],
      properties: {
        task: str("Complete, self-contained instructions for the subagent"),
        worker: { type: "string", enum: ["pi", "claude"], description: "Harness; implied by the router or the model" },
        model: str("pi: provider/id; claude: alias (sonnet, opus, haiku)"),
        thinking: { type: "string", enum: THINKING },
        name: str("Short id prefix, e.g. 'tests' or 'review'"),
        files: { type: "array", items: { type: "object", required: ["path"], properties: { path: str("Path relative to the working directory"), lines: str("Line range like '40-120'") } } },
        notes: { type: "array", items: { type: "string" } },
        view: { type: "boolean", description: "Show it in the viewer slot right away" },
      },
    },
    async run(p) {
      let { worker, model, thinking } = p;
      let routed = "";
      if (!model) {
        const d = JSON.parse(await must(bin("tp-route"), [], p.task));
        worker = d.worker; model = d.model; thinking = thinking ?? d.thinking;
        routed = ` (routed: ${worker} ${model} ${thinking})`;
      }
      if (!worker) worker = model && !model.includes("/") ? "claude" : "pi";
      const args = ["--worker", worker];
      if (model) args.push("--model", model);
      if (thinking) args.push("--thinking", thinking);
      if (p.name) args.push("--name", p.name.replace(/[^\w.-]+/g, "-"));
      for (const f of p.files ?? []) {
        if (f.lines && !/^\d+-\d+$/.test(f.lines)) throw new Error(`bad line range for ${f.path}: ${f.lines} (use start-end)`);
        args.push("--context", f.lines ? `${f.path}:${f.lines}` : f.path);
      }
      for (const n of p.notes ?? []) args.push("--note", n);
      if (p.view) args.push("--view");
      const id = (await must(bin("tp-spawn"), args, p.task)).split("\n")[0];
      lastInput.set(id, Date.now());
      const packet = readText(join(await stateDir(), "tasks", id, "packet.md"));
      return `started ${id}${routed}; packet ${packet.length} chars`;
    },
  },
  list_subagents: {
    description: "List subagents with state (working, tool, waiting, done, failed, killed), tokens and model.",
    schema: { type: "object", properties: {} },
    run: () => must(bin("tp-list"), []),
  },
  send_to_subagent: {
    description: "Send a follow-up message to a subagent that is waiting for input, as if typed into its session. Use collect_result afterwards.",
    schema: { type: "object", required: ["id", "message"], properties: { id: str("Subagent id"), message: str("Message to send") } },
    async run(p) {
      const dir = await taskDir(p.id);
      const ps = await paneState(dir);
      if (ps !== "running") throw new Error(`subagent '${p.id}' is ${ps}; it cannot take input`);
      const pane = readJson(join(dir, "meta.json")).pane;
      await must("tmux", ["send-keys", "-t", pane, "-l", p.message.replace(/\r?\n/g, " ")]);
      await sleep(300);
      await must("tmux", ["send-keys", "-t", pane, "Enter"]);
      lastInput.set(p.id, Date.now());
      return `sent to ${p.id}`;
    },
  },
  collect_result: {
    description: "Get a subagent's state and last reply. With wait_seconds, wait until it has finished its current work or time runs out.",
    schema: { type: "object", required: ["id"], properties: { id: str("Subagent id"), wait_seconds: { type: "number", description: "Max seconds to wait (0-1800, default 0)" } } },
    async run(p) {
      const dir = await taskDir(p.id);
      const deadline = Date.now() + Math.min(Math.max(p.wait_seconds ?? 0, 0), 1800) * 1000;
      const since = lastInput.get(p.id) ?? 0;
      let state = {}, ps = "running";
      for (;;) {
        const esc = readText(join(dir, "escalated"));
        if (esc) {
          const why = readJson(join(dir, "meta.json")).escalated_to ?? {};
          return `${p.id} escalated to ${esc} (${why.model ?? "a stronger model"}) after ${why.after_errors ?? "repeated"} failed tool calls. Use collect_result on ${esc}; attempt log: ${join(dir, "escalation.md")}.`;
        }
        state = readJson(join(dir, "state.json"));
        ps = await paneState(dir);
        const fresh = Date.parse(state.updated ?? "") >= since - 1000;
        if (ps !== "running" || (fresh && ["waiting", "error"].includes(state.state)) || Date.now() >= deadline) break;
        await sleep(2000);
      }
      const raw = ps === "running" ? (state.state ?? "starting") : ps;
      const status = (await run(bin("tp-contract"), ["collect", p.id])).stdout.trim() || raw;
      const t = state.tokens;
      const lines = [
        `id: ${p.id}`,
        `status: ${status}${state.tool ? ` (${state.tool})` : ""}`,
        state.model ? `model: ${state.model}` : "",
        t ? `tokens: in ${t.input + t.cacheRead + t.cacheWrite}, out ${t.output}; turns ${state.turns}` : "",
        state.error ? `error: ${state.error}` : "",
      ].filter(Boolean);
      if (ps === "running" && ["starting", "permission"].includes(raw)) {
        const pane = readJson(join(dir, "meta.json")).pane;
        const screen = (await run("tmux", ["capture-pane", "-p", "-t", pane])).stdout.split("\n").filter((l) => l.trim()).slice(-15).join("\n");
        lines.push(`${p.id} may be waiting for the user (enter in the status pane to view it). Its screen:`, screen);
      }
      const reply = readText(join(dir, "result.md"));
      const body = reply.length > 20000 ? `${reply.slice(0, 20000)}\n…(truncated; full reply in ${join(dir, "result.md")})` : reply;
      return `${lines.join("\n")}\n\n${body || "(no reply yet)"}`;
    },
  },
  review_subagent: {
    description:
      "Start an independent reviewer on the other provider for a successfully completed subagent. It reports APPROVED or REQUEST_CHANGES and never edits files. " +
      "Use collect_result on the returned id. Local policy decides first; set force only when the user asked for a review.",
    schema: { type: "object", required: ["id"], properties: { id: str("Completed subagent id"), force: { type: "boolean" } } },
    async run(p) {
      const r = await run(bin("tp-review"), p.force ? ["--force", p.id] : [p.id]);
      if (r.code === 3) return r.stderr.trim();
      if (r.code !== 0) throw new Error((r.stderr || r.stdout).trim() || `tp-review exited with ${r.code}`);
      const rid = r.stdout.trim();
      lastInput.set(rid, Date.now());
      return `started cross-provider review ${rid} for ${p.id}`;
    },
  },
  kill_subagent: {
    description: "Stop a subagent and close its session. Its task files and last reply are kept.",
    schema: { type: "object", required: ["id"], properties: { id: str("Subagent id") } },
    async run(p) { await must(bin("tp-kill"), [p.id]); lastInput.delete(p.id); return `killed ${p.id}`; },
  },
};

const send = (m) => process.stdout.write(JSON.stringify({ jsonrpc: "2.0", ...m }) + "\n");
async function handle(msg) {
  const { id, method, params } = msg;
  if (id === undefined) return; // notifications
  if (method === "initialize")
    return send({ id, result: { protocolVersion: params?.protocolVersion ?? "2025-06-18", capabilities: { tools: {} }, serverInfo: { name: "tmux-pi-subagents", version: "0.1.0" } } });
  if (method === "ping") return send({ id, result: {} });
  if (method === "tools/list")
    return send({ id, result: { tools: active ? Object.entries(TOOLS).map(([name, t]) => ({ name, description: t.description, inputSchema: t.schema })) : [] } });
  if (method === "tools/call") {
    const tool = active && TOOLS[params?.name];
    if (!tool) return send({ id, result: { isError: true, content: [{ type: "text", text: `unknown tool ${params?.name}` }] } });
    try {
      return send({ id, result: { content: [{ type: "text", text: await tool.run(params.arguments ?? {}) }] } });
    } catch (e) {
      return send({ id, result: { isError: true, content: [{ type: "text", text: String(e.message ?? e) }] } });
    }
  }
  send({ id, error: { code: -32601, message: `method not found: ${method}` } });
}
createInterface({ input: process.stdin }).on("line", (l) => { try { handle(JSON.parse(l)); } catch {} });
