# WT1: where work waits, as a timeline (loop idea H32)

Owner, 2026-11-10: find where time goes across Main, workers, tests, reviews and lanes. The report answers, per task, "is this
waiting for tests, a prerequisite task, a worker slot, a lane lease, model output, or input from me?", and shows the useful work
that ran at the same time. **Priority: high** (owner's first priority with VR1). Token and cost tracking are out of scope here.
Status: **open**. Advisory: the owner decides on adoption.

Overlap: absorbs the measurement side of **H9** (start check: a stalled task must become visible; WT1 measures how long that
takes and H9's check is one of its arms). Decides whether **H16** (OpenTelemetry) is needed: WT1 starts with existing records and
small JSONL additions, and lists only the gaps that need a tracing dependency. Uses the bench's `wall_s` baseline unchanged.

## What exists today (inventory 2026-11-10, refs at main fd2321d)
- Per task: `meta.json` `created` (spawn time, bin/tp-spawn:806-823); `state.json` is a snapshot rewritten on every event
  (pi agent/extensions/reporter.ts:111-118; Claude claude/plugins/tmux-pi/hooks/report.sh:245-264, adds `permission`), so
  state history is lost; `promptLog` keeps the last 8 prompt submits with times. Lifecycle states are derived from files, never
  stored (lib/contract.py:426-506). One-time markers: `exit` (mtime), `completed` (epoch), `collected.json`, `stopped.json`,
  `result.md` mtime. `checkpoints.json` is append-only, contract tasks only. `messages.jsonl` holds Main's messages.
- Queue: `queue/<id>.json` has `created` and a `queue_reason` (lane writer, worker slots full, local slots full, waiting for
  parent, autonomy gate; bin/tp-agent:536-576) that is overwritten each tick and the file is deleted when the task starts
  (:1155-1159), so queue wait time and its reason are lost.
- Lanes: the lease stamps `taken` (lib/lane_lease.py:226); release keeps no time (:231-245). Step close keeps `closed_at`.
- Owner/Main input: `capability-request.json` keeps `requested`/`answered`; permission and trust-dialog waits leave only the
  `updated` time of the `permission` state.
- Tests: `tp-test-run:v1` records keep `seconds` and `exit` but no start/end (tools/agent/test-run.sh:111-134); the gate record
  has `started`/`finished` (lib/gate_record.py:48-50); tests run with plain Bash are visible only in the transcript.
- Model vs tool time: the Claude transcript JSONL has a `timestamp` on every line, and tool_use/tool_result pair by id, but
  `lib/routines.py` `parse_transcript` drops the times; pi's reporter sees `tool_execution_start/end` and `message_end` but keeps
  no times. Main's own turns are only in Claude Code's store.

## Smallest instrumentation (arm B), all JSONL appends with safe_write
1. Per-task `events.jsonl` `{at, state, tool, source}` written beside each state save (reporter.ts save, report.sh before `mv`).
2. Queue: copy `created` into meta.json as `queued_at`; append each `queue_reason` change with its time.
3. Lane `lease-log.jsonl` `{lane, task, role, taken, released}` on release.
4. `started`/`finished` on test-run records (schema in lib/gate_record.py).
5. Timestamped permission/trust-dialog wait start and end; `at` on `pending-capability.json`.
6. `parse_transcript` keeps tool_use/tool_result times (after the fact, no runtime change).
7. A report command builds the timeline from these plus existing markers; no tracing dependency.

## Report shape
Per task, ordered intervals `{task, actor (main|worker|reviewer|test|lane), state, start, end, wait_reason, source,
confidence}`. States: model (generating), tool (with test as a tool subtype), waiting:test | dependency | worker-slot |
lane-lease | owner-input | permission | Main, stalled, interrupted, and **unknown** for any gap no record explains. Two totals,
never mixed: elapsed (wall clock, overlap counted once) and accumulated worker time (sum over parallel workers). A critical-path
view: which interval delayed completion, and one suggested focus area. `confidence`: exact (written event), derived (file mtime,
transcript pairing), inferred (gap between known events).

## Arms (same tasks and conditions)
- **A, existing records only** (built after the fact from today's files and transcripts).
- **B, + small instrumentation** (items 1-7 above).
- **C, + OpenTelemetry** only for gaps B cannot close (per-request model latency, per-tool `duration_ms`); runs only if B leaves
  model/tool attribution below the threshold, and needs owner approval for a local collector (H16).

## Cases
1. **Controlled delays (ground truth known):** scripted fake workers in private tmux servers (TMUX_TMPDIR plus `-L`, no live
   models) with planted durations: a 90 s test, a queued `--after` dependency, a busy lane writer, simulated model generation
   (sleep between turns), two overlapping tasks, a worker that stalls before its first prompt (H9) and one killed mid-tool.
   Attribution accuracy is scored per second against the planted schedule.
2. **Real runs:** the SG1 rounds 1-9 and product-design PD-A..F task dirs in `.tmux-pi/sessions/` (arm A only, history), then
   the next live lane step and one bench pair with arm B on (the bench `wall_s` baseline is kept; arm B runs as a new label).

## Measures
Attribution accuracy (share of seconds in the right state on case 1); unexplained (`unknown`) time share on real runs; delay
from a stall to it showing in the report; instrumentation overhead (added seconds per task, events per task, bytes); examples
where the timeline changes the recommended next action (counted from real runs, each with the action before and after).

## Decision
Incorporate B if, on case 1, at least 90% of seconds are attributed correctly, unknown time on real runs falls below 15%, a
stall shows within 2 minutes, overhead stays under 1% of task time, and at least one real run yields a changed next action.
Keep on demand (report command only, no new events) if A alone meets the attribution bar. Defer C unless B fails on model/tool
attribution only.

## Missing before it can run
Fake-worker fixtures for case 1. A lane branched from `main` (lane/integrate is stale). No access needed beyond this Mac.
