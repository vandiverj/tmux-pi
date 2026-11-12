# HB6b1: record the waits arm A could not see (lane hub-waits-b, WT1 arm B, first slice)

Owner (2026-11-10): "My priorities are understanding where work waits and avoiding repeated verification." HB6a (merged
bf76186, `tools/waits/timeline.py`) measured arm A: case-1 attribution 74.2% against the 90% bar. Its two misses are a queued
`--after` child's wait (the queue file is deleted on start, so nothing dates the wait) and owner input recorded as `by: main`
(nothing says who answered). Review notes added: a permission record can stay open past approval, into the tool's run
(report.sh:266,283-293), so its span is not approval latency. Plan: `../../wt1-wait-timeline.md` (arm B items, measures,
decision). Bar: realistic use (`docs/plans/realistic-use-bar.md`).

**Adoption is the owner's** (WT1: "Advisory: the owner decides on adoption"). This lane is built, reviewed and measured, then
Main stops before merge with the measures for the owner. Write nothing that changes a worker's behaviour beyond appending records.

## Change (producers emit kinds that already exist in interfaces.md section 3; no schema change, no new kind)
Read `docs/plans/det-prep/hub/interfaces.md` sections 3-4 and `lib/hub_records.py` (`event`, `_close`) first; write every record
through those helpers (safe_write), never a second writer.
1. **Queue wait.** When tp-agent queues a task behind `--after` (or any queue reason), record `queued_at` in meta.json and a
   `queue.reason` event with the reason; when it leaves the queue, a `queue.reason` event with state `released`. The timeline
   paints `waiting:dependency` (or the reason's state) from these, exact.
2. **Who answered.** A request resolved on the owner's word must say so: the answering command takes `--by owner` with a
   required `--quote` (the owner's words, as in interfaces.md section 5's provenance rule); without it `by` stays `main`. Never
   inferred. The timeline paints owner input as `waiting:owner-input` only when `by: owner`.
3. **Approval instant.** Where report.sh / reporter.ts already see a permission prompt end (the next PreToolUse/PostToolUse or
   the state leaving `permission`), resolve the request at that instant (`how: approved|denied|left`), so the request span ends
   at the answer, not after the tool ran. Name the producer lines you rely on; if a harness gives no such instant, say so and
   leave it (that is arm C's question, not this slice).
4. **Timeline reads them.** tools/waits/timeline.py prefers these exact records over its inferred rules; with them absent it
   behaves exactly as today (arm A is the fallback, every old row still passes).

Not in this slice: per-state `state` events on every save, lane `lease.*`, test-run `started`/`finished`, Main's own turns.
List any you find you need, with why; do not build them.

## Measure (case 1 again, with the real producers)
Rebuild HB6a's case-1 schedule so items 1-3 are written by the real code paths (tp-agent queue and answer commands run
against a temp session with TMUX_TMPDIR plus a private `-L` socket; report.sh fed producer-shaped hook JSON on stdin), not
hand-written files; say which parts are still hand-built and why. Report: attribution accuracy (bar 90%), stall visibility,
overhead (added records and bytes per task, added seconds per producer call), and the WT1 decision it points to. Rerun case 2
on the real task dirs: arm B records do not exist there yet, so it must equal HB6a's output exactly (prove it with a diff).

## Attack table (one table in report.md, not one test per row)
Input space: harness {claude, pi} x record {queue: --after child released / cancelled / parent failed; answer: by owner with
quote / by owner without quote (refused) / by main; permission: approved / denied / left open / two prompts in one tool call}
x record present/missing/malformed x arm-A fallback still holds. Expected output and the row that proves it; fail-before where
a rule replaces an inferred one.

## Tests and verification
Extend `tests/test_wait_timeline.py`'s existing tables and the existing tests of the producers you change (find them; a new
test module only if none covers that producer). No ritual tests. Run the affected modules plus `tests.test_session_env` once
through `tools/agent/test-run.sh` with `TMUX_TMPDIR=$(mktemp -d /tmp/h6b.XXXX)`. Never touch the default tmux server.

Report in report.md (rules first: one row per new record and the producer line that writes it). Do not commit. Do not add git
notes.
