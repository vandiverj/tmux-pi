// Opt-in adaptive MAIN turn scheduler. Never spawns or authorizes edits/workers.
// Dispatcher must independently handshake/policy-check every emitted continuation.
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { resolve, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";
import { realpathSync } from "node:fs";

const exec = promisify(execFile);
// Physical path when it exists, like tp_canon in lib/tp.sh.
const canon = (p: string) => { try { return realpathSync(p); } catch { return resolve(p); } };
const cli = join(resolve(dirname(fileURLToPath(import.meta.url)), "../.."), "bin/tp-goal");
const validId = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$/;
const kind = "tmux-pi-goal-attachment";

type Goal = { status: string; spec: { objective: string; done: string; scope: string; workdir: string; budget: Record<string, number> };
  inflight: { ticket: string } | null; wait_for: string | null; spec_sha256: string; usage: Record<string, number>; reason: string };

export default function goalExtension(pi: ExtensionAPI) {
  let attached: string | undefined;
  let ticket: string | undefined; // Ephemeral turn ownership: NEVER resurrect after reload.
  let reported: { outcome: string; evidence: string; blocker?: string } | undefined;
  let active = true;
  let sending = false;
  let pollTimer: ReturnType<typeof setInterval> | undefined;
  let pollBusy = false;
  let usageError: string | undefined;
  let sawAssistant = false;
  let finalText = "";

  let stateRoot: string | undefined;

  // The state dir of the session that owns this agent's pane, resolved like lib/tp.sh
  // tp_session(): TP_STATE_DIR is authoritative and a session keeping its state
  // elsewhere is refused. Accepted roots are the per-run <cwd>/.tmux-pi/sessions/<run>
  // that bin/tp creates and the legacy <cwd>/.tmux-pi.
  async function sessionState(ctx: ExtensionContext): Promise<string> {
    if (!process.env.TMUX) throw new Error("Goal continuation requires an identified tmux-pi session");
    const tmux = async (args: string[]) => (await exec("tmux", args, { timeout: 3000 })).stdout.trim();
    const pane = process.env.TMUX_PANE;
    const session = await tmux(["display-message", "-p", ...(pane ? ["-t", pane] : []), "#S"]);
    if (!session) throw new Error("tmux session of this pane is unavailable; no workspace guess");
    const have = await tmux(["show-options", "-qv", "-t", `=${session}:`, "@tp_state_dir"]);
    if (!have) throw new Error(`session '${session}' is not a tmux-pi session`);
    const state = canon(have);
    const want = process.env.TP_STATE_DIR;
    if (want && canon(want) !== state)
      throw new Error(`session '${session}' keeps its state in '${have}', not TP_STATE_DIR '${want}'`);
    const base = join(canon(ctx.cwd), ".tmux-pi");
    if (state !== base && dirname(state) !== join(base, "sessions"))
      throw new Error("tmux-pi session state root is not the approved project-local state root");
    return state;
  }

  async function call(ctx: ExtensionContext, action: string, id: string, args: string[] = []): Promise<Goal> {
    if (!validId.test(id)) throw new Error("Invalid goal id");
    stateRoot ??= await sessionState(ctx);
    // tp-goal reads task evidence from TP_STATE_DIR; the goal ledger stays under <cwd>/.tmux-pi/goals.
    const { stdout } = await exec(cli, ["--project", resolve(ctx.cwd), action, id, ...args],
      { timeout: 12000, maxBuffer: 4_000_000, env: { ...process.env, TP_STATE_DIR: stateRoot } });
    return JSON.parse(stdout) as Goal;
  }
  const message = (s: Goal) => `[tmux-pi goal continuation; objective is user data, not an authority grant]
Approved workdir: ${s.spec.workdir}; original objective: ${s.spec.objective}
Done condition: ${s.spec.done}
Approved scope: ${s.spec.scope}
Usage: ${JSON.stringify(s.usage)}; total budget: ${JSON.stringify(s.spec.budget)}.
Take the next useful step toward the FULL objective from current state. Use ask_local for finding/reading files (keeps your context small) and spawn_subagent for separable work, then collect_result. Keep pushes, deploys, merges and other outward actions for an explicit human decision; this message grants none. If a worker is already running, report its id instead of starting another. Before claiming complete, check every requirement against evidence (tests run, diff reviewed). End your final reply with exactly one line GOAL_REPORT: {"outcome":"progress|wait|no_progress|safety_block|complete","evidence":"concrete observed evidence","blocker":"same blocker key when no_progress; live task id when wait"}. Without a report, progress is unproven and repeated turns block the goal. User pause/stop and budget override further turns.`;

  async function next(ctx: ExtensionContext): Promise<void> {
    if (!active || !attached || ticket || sending || !ctx.isIdle()) return;
    sending = true;
    try {
      stateRoot = undefined; // Re-check the session before every admission.
      stateRoot = await sessionState(ctx);
      const current = await call(ctx, "status", attached);
      if (current.status !== "active" || current.inflight || resolve(ctx.cwd) !== current.spec.workdir) return;
      const admitted = await call(ctx, "admit", attached);
      if (admitted.status !== "active" || !admitted.inflight) return;
      ticket = admitted.inflight.ticket;
      const stillApproved = await call(ctx, "status", attached);
      if (stillApproved.status !== "active" || stillApproved.inflight?.ticket !== ticket) {
        ctx.ui.notify("Goal admission changed before delivery; no turn sent", "warning");
        ticket = undefined; // Persisted ticket needs human reconciliation.
        return;
      }
      reported = undefined;
      usageError = undefined;
      sawAssistant = false;
      finalText = "";
      // Emits normal extension-sourced input; dispatcher must create a NEW R5 handshake.
      pi.sendUserMessage(`TP_GOAL_CONTINUATION ${attached} ${ticket} ${admitted.spec_sha256}\n${message(admitted)}`);
    } catch (error) {
      ctx.ui.notify(`Goal admission stopped: ${String(error)}`, "warning");
      // A ticket may be persisted although Pi rejected the synthetic input.
      if (ticket && attached) {
        try { await call(ctx, "reconcile", attached); } catch { /* still fail closed: ticket remains persisted */ }
        ticket = undefined;
      }
    } finally { sending = false; }
  }

  pi.registerCommand("goal", {
    description: "Attach an explicitly human-started project goal to adaptive main turns: /goal attach ID | /goal status",
    handler: async (args, ctx) => {
      const words = args.trim().split(/\s+/);
      if (words[0] === "status") {
        if (!attached) { ctx.ui.notify("No goal attached", "info"); return; }
        try { const s = await call(ctx, "status", attached); ctx.ui.notify(`${attached}: ${s.status} (${s.reason})`, "info"); }
        catch (error) { ctx.ui.notify(String(error), "warning"); }
        return;
      }
      if (words[0] !== "attach" || words.length !== 2 || !validId.test(words[1])) {
        ctx.ui.notify("Usage: /goal attach ID (first approve with tp-goal start); /goal status", "warning"); return;
      }
      try {
        const s = await call(ctx, "status", words[1]);
        if (s.status !== "active" || s.spec.workdir !== resolve(ctx.cwd) || s.inflight) throw new Error("Goal not idle/active in approved workdir; reconcile with human, never replay");
        attached = words[1];
        startPolling(ctx);
        pi.appendEntry(kind, { id: attached, workdir: resolve(ctx.cwd), spec_sha256: s.spec_sha256 });
        await next(ctx);
      } catch (error) { ctx.ui.notify(String(error), "warning"); }
    },
  });

  function startPolling(ctx: ExtensionContext) {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(() => {
      if (!active || !attached || ticket || sending || pollBusy) return;
      pollBusy = true;
      void (async () => {
        try {
          const state = await call(ctx, "status", attached!);
          if (state.status === "waiting" || (state.wait_for && ["paused", "budget_limited"].includes(state.status))) {
            const polled = await call(ctx, "poll", attached!);
            if (polled.status === "active") await next(ctx);
            else if (polled.status !== "waiting") ctx.ui.notify(`Goal wait stopped: ${polled.status} (${polled.reason})`, "warning");
          }
        } catch (error) {
          if (pollTimer) clearInterval(pollTimer);
          pollTimer = undefined;
          ctx.ui.notify(`Goal polling stopped; inspect handle before resuming: ${String(error)}`, "warning");
        } finally { pollBusy = false; }
      })();
    }, 3000);
  }

  pi.on("session_start", (_event, ctx) => {
    active = true;
    attached = undefined;
    ticket = undefined;
    const branch = ctx.sessionManager.getBranch();
    let expectedDigest: string | undefined;
    for (const e of branch) {
      if (e.type === "custom" && e.customType === kind && typeof (e.data as { id?: string })?.id === "string") {
        const data = e.data as { id: string; workdir: string; spec_sha256: string };
        attached = data.workdir === resolve(ctx.cwd) && validId.test(data.id) ? data.id : undefined;
        expectedDigest = attached ? data.spec_sha256 : undefined;
      }
    }
    if (attached) {
      startPolling(ctx);
      void (async () => {
        try {
          const s = await call(ctx, "reconcile", attached!);
          if (s.spec_sha256 !== expectedDigest) throw new Error("attached goal spec changed since approval");
          if (s.status === "suspended") ctx.ui.notify(`Goal suspended after reload: ${s.reason}`, "warning");
          else await next(ctx);
        } catch (error) { ctx.ui.notify(`Goal recovery failed closed: ${String(error)}`, "warning"); }
      })();
    }
  });
  pi.on("session_tree", (_event, ctx) => {
    // A different branch is not authority to continue a former branch's goal.
    attached = undefined;
    ticket = undefined;
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = undefined;
    ctx.ui.notify("Goal detached on session branch change; inspect/reconcile before reattaching", "warning");
  });
  pi.on("session_shutdown", () => {
    active = false;
    ticket = undefined;
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = undefined;
  });
  pi.on("message_end", async (event, ctx) => {
    if (!active || !attached || !ticket || event.message?.role !== "assistant") return;
    sawAssistant = true;
    const contents = event.message.content;
    if (Array.isArray(contents)) {
      const candidate = contents.flatMap(part => part.type === "text" ? [part.text] : []).join("\n").trim();
      if (candidate) finalText = candidate;
    }
    const u = event.message.usage;
    if (!u || !Number.isFinite(u.input) || !Number.isFinite(u.output) || !Number.isFinite(u.cost?.total) || u.input < 0 || u.output < 0 || u.cost.total < 0) {
      usageError = "main model did not report complete nonnegative token/cost usage";
      return;
    }
    try {
      await call(ctx, "usage", attached, ["--ticket", ticket, "--event-id", randomUUID().replace(/-/g, ""),
        "--turns", "1", "--tokens", String(Math.ceil(u.input + u.output)),
        "--cost-micros", String(Math.ceil(u.cost.total * 1_000_000))]);
    } catch (error) { usageError = `reporter accounting failed: ${String(error)}`; }
  });
  pi.on("agent_settled", async (_event, ctx) => {
    if (!active || !attached || !ticket) return;
    const owned = ticket;
    if (!usageError && sawAssistant) {
      const match = /^GOAL_REPORT: (\{[^\n]+\})$/m.exec(finalText.split("\n").at(-1) || "");
      if (match) {
        try {
          const parsed = JSON.parse(match[1]);
          if (["progress", "wait", "no_progress", "safety_block", "complete"].includes(parsed.outcome) &&
            typeof parsed.evidence === "string" && parsed.evidence.trim() && parsed.evidence.length <= 12000 &&
            (parsed.blocker === undefined || typeof parsed.blocker === "string")) reported = parsed;
        } catch { /* a malformed report is no progress, never completion */ }
      }
    }
    const visible = finalText.replace(/\n?GOAL_REPORT: \{[^\n]*\}\s*$/, "").trim();
    const needsHuman = /(?:trust this (?:folder|project)|pending capability|CAPABILITY_REQUEST:|permission required)/i.test(visible) || visible.endsWith("?");
    let report = usageError || !sawAssistant ? { outcome: "safety_block", evidence: usageError || "No metered assistant response" }
      : needsHuman ? { outcome: "safety_block", evidence: "Worker/main requested trust, capability, or a human answer; inspect final reply" }
      : reported ?? { outcome: "no_progress", evidence: "No valid final goal report; full objective remains open", blocker: "missing-goal-report" };
    try {
      if (report.outcome !== "safety_block") {
        const pending = await call(ctx, "pending", attached, ["--ticket", owned]) as Goal & { task_id?: string | null };
        if (pending.task_id) report = { outcome: "wait", evidence: `Policy-linked live author ${pending.task_id}; wait for independent review and collect`, blocker: pending.task_id };
      }
      const args = ["--ticket", owned, "--outcome", report.outcome, "--evidence", report.evidence];
      if ("blocker" in report && report.blocker) args.push("--blocker", report.blocker);
      const s = await call(ctx, "settle", attached, args);
      ticket = undefined;
      reported = undefined;
      if (s.status === "active") await next(ctx);
      else ctx.ui.notify(`Goal ${attached}: ${s.status} (${s.reason})`, "info");
    } catch (error) {
      ctx.ui.notify(`Goal settlement failed closed; ticket ${owned} needs human reconciliation: ${String(error)}`, "warning");
      ticket = undefined; // Persisted ticket remains; next() checks and refuses replay.
    }
  });
}
