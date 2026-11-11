# HB0: shared interfaces for HB2-HB8 (Main draft, 2026-11-11)

Written once so the lanes (hub-correct, hub-memory, hub-delivery, hub-episodes) implement against the same shapes. Every
record is JSON or JSONL written with `lib/safe_write`; every field a source cannot supply is absent or `null`, never guessed or
zeroed. Schema names carry a version; readers refuse an unknown major version with a clear message. A lane that needs a change
here proposes it in its report; Main updates this file before the next brief.

## 1. Universal worker contract (HB2)
`agent/contract/worker.md` with a first line `contract: worker vN`. Short (target ≤ 20 lines): stay in the brief's scope; the one
read/search rule; what to do at a permission prompt (stop, report, never work around a denial); report to `report.md`, final
reply ends with `RESULT_STATUS:`; how to reach Main (`tp-agent request`). Tool names come from a per-harness table in the adapter
(Claude: `mcp__tmux-pi-safe__safe_grep`/`safe_read` and Read/Grep; pi: `safe_grep`/`safe_read`/`read`), never hard-coded in role
files. Role files keep only role-specific method. The project tools manifest stays optional and is rendered as its own block.

## 2. Launch record (HB2): `TASK_DIR/launch.json`, schema `tp-launch:v1`
`{schema, task, role, harness, harness_version, model, effort, tmux_pi_commit, tmux_pi_dirty,
 instructions: [{name, path, sha256, version}],            // contract, role file, preamble, manifest, overlays, injected rules
 tools: {exposed: [...], denied: [...]},                     // as passed on the command line / registered by the adapter
 capabilities: {supported: [...], unsupported: [...]},       // e.g. interrupt.rpc, permission.state, prompt.confirm
 preferences: [decision ids applied],                        // from HB3, in-scope only
 context: {brief, parent, lane, step, base_commit, evidence: [...]},
 route: spawn|retry|review|lane|contract|bench|main|subagent}`
Main's own record: `.tmux-pi/sessions/<sid>/main-launch.json` (same schema, `role: main`), rewritten at each start/recovery.

## 3. Event journal (HB4, HB5, HB6): `TASK_DIR/events.jsonl`, one line per event, schema `tp-event:v1`
`{v: 1, id: "ev-<ulid>", at: ISO-8601 UTC, task, actor: main|worker|reviewer|hook|watchdog|owner|test,
  kind, ref?: "req-…"|"cor-…"|"obl-…", state?, detail?, source: file|hook|rpc|pane|transcript, confidence: exact|derived|inferred}`
Kinds (closed list, extended only here): `state` (working, tool, waiting, permission, input, done, failed),
`request.opened`, `request.resolved`, `correction.requested`, `correction.cancelled`, `correction.delivered`,
`correction.confirmed`, `correction.failed`, `correction.unverified`, `pause.requested`, `pause.confirmed`, `hold.kept`,
`obligation.changed`, `check.started`, `check.finished`, `queue.reason`, `lease.taken`, `lease.released`.
Tool names are normalised at write time by one shared table (`Bash|bash` → `shell`, `Read|read|safe_read|mcp__tmux-pi-safe__safe_read`
→ `read` with `safe: true|false`, likewise grep), the raw name kept in `detail.raw_tool`.

## 4. Request identity (HB4)
Every permission prompt, input wait, capability request and question to Main gets `req-<ulid>` when it is first seen, written by
the hook or reporter that sees it: `TASK_DIR/requests/<req-id>.json` `{id, task, kind: permission|input|capability|question,
opened, text_sha256, summary, status: open|resolved|withdrawn|superseded, resolved?, resolution?: {by, how, ref}}`.
A message delivery never resolves a request by itself; resolution names the request id. Notifications to Main key on the request
id, so two requests between polls are two notices and a resolved request never re-notifies after a restart.
A correction has `cor-<ulid>` and an idempotency key `sha256(task, text, intended request id)`; a second `correct` with the same
key while the first is unconfirmed reports the first instead of sending again.

## 5. Decisions and preferences (HB3): extend the existing work store, no new store
Use `tp-agent work decide` / `work decision add|answer` (`lib/work_commands.py`, `docs/persistent-work.md`). Added fields per
decision line/record: `kind: preference|design|task-intent|obligation|correction`, `scope` (`global`, `project:<repo>`,
`workspace`, `lane:<n>`, `task:<id>`, `role:<r>`), `provenance: {by: owner|main|worker, quote?, at, source?}`,
`status: active|provisional|superseded|withdrawn`, `supersedes?`. Rules: only `by: owner` with a quote becomes `active`
directly; anything inferred is `provisional` until the owner confirms; worker text never creates an active record. The brief and
dispatch read only `active` records whose scope covers the target.
Working brief (`tp-agent brief`, ≤ ~60 lines): outcome, acceptance, active preferences in scope, active work (tasks, lanes,
reviews with state), open decisions, pending obligations (from 6), last verified evidence, pointers. Built from records only;
it states the record time of each part so a stale part is visible.

## 6. Obligations (HB5): `TASK_DIR/obligations.json` (lane-level copy in the lane dir), schema `tp-obligations:v1`
`[{id: "obl-<ulid>", check, class: project|harness, level: required|advisory, why, applies_by: rule id from the catalogue,
   status: pending|satisfied|inapplicable|deferred, reason?, evidence?: {record (VR1 key or log path), tree, at, fresh: bool}}]`
`inapplicable` and `deferred` need a reason; `satisfied` needs evidence whose tree matches the task's current tree under the
shared fingerprint (VR1). Completion and lane close list every `pending` required obligation.

## Ownership
- hub-correct (HB4) writes 3 and 4 first; hub-memory (HB3) writes 5; hub-delivery (HB2) writes 1 and 2; HB5 writes 6.
- One shared module per schema in `lib/` (e.g. `lib/hub_records.py`) created by the first lane that needs it; later lanes extend
  it. Lanes that run at the same time (hub-correct, hub-memory) agree by this file, not by editing the same module.
