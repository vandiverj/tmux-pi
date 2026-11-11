# RR1 round 2: the gate and its triggering review belong to the retry, recorded when it is made (Main, 2026-11-11)

Review review-coder-03b81b9952-120436 (`result.md`, `report.md`, `test-runs/` in
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-03b81b9952-120436/`)
found F1 and F2, family `retry-window-provenance`. Read both and their reproductions first. The RR1 brief
(docs/plans/det-prep/hub/rr1-review-rounds.md) still applies except where this file replaces it. Bar: realistic use.

## Main's diagnosis
Main's fault, twice. F1 comes from Main's own fallback e04cc16: it infers a legacy child's gate from the parent's `sweep.md`,
which a later sibling retry (or a failed gated dispatch) can write. F2: the brief said the triggering review is the first in
the window but not to record which review that was, so `findings_window` takes "the parent's last review", which moves when the
parent is reviewed again.

## Rule 1: the retry records its gate and the review that triggered it (F2)
`tp-agent retry` with a gated route (`rule-first` or `design`) writes two fields, carried through retry admission, the queue
record's validation (`bin/tp-agent`, near the existing `gate` check, ~line 830), the spawn args (~line 702) and `tp-spawn`
(a new `--gate-review <review id>`, beside `--gate`) into the new task's `meta.json`: `gate` (as now) and `gate_review` = the
id of the parent's review that `repeated_family` judged (the parent's newest review with a result.md at retry time). The queue
validation refuses a `gate_review` that is not a task id, or one without `gate`. `findings_window` starts the window at the
round of `gate_review` (its round number from `chain_labels`), not at "the last parent review before the gated author's rows".
A `gate_review` that is not in the chain refuses (Refused), naming it.

## Rule 2: a legacy child's gate is its own dispatch record, never the parent's file (F1)
A task with no `gate` key (made before round 1 of RR1) is gated iff its own `dispatch.json` `context` lists a sweep.md
(`family_sweep.is_sweep`, as `dispatched_sweep` in bin/tp-agent already does) that is the parent's sweep path. Its triggering
review is the parent's newest review whose `meta.json` `created` is earlier than the child's `created` (both are
`%Y-%m-%dT%H:%M:%SZ` UTC strings; compare them as such) and that has a result.md. The parent's `sweep.md` existing on disk is
no longer read for this. A task with `"gate": null` or no `gate` and no such dispatch entry is not gated. Real check: HB8a's
coder-c24113fdaa (dispatched with coder-e4f1c2274f/sweep.md, created 16:26:44Z; parent review review-coder-e4f1c2274f-100152
created 16:01:54Z) must still be gated with that review as the trigger.

## Tests (tests earn their place)
Extend the existing gate tests in tests/test_review_rounds.py and the retry tests that already cover `gate` (find them with
safe_grep `gate`); no new files. Cases: (a) F1 sibling: ordinary retry w2 of P, then a design retry w3 of P writes P/sweep.md:
preparing w2 is unchanged (byte for byte against before w3); (b) F1 failed gated dispatch leaves P/sweep.md: an ordinary
retry is not gated; (c) F2: a design retry w2 after review r-c, then another review of the parent appended in the shape
bin/tp-review:497 writes: w2's window still starts at r-c, which stays in full; (d) legacy child with the sweep in its
dispatch.json and no `gate` key: gated, trigger by `created`; (e) queue validation refuses `gate_review` without `gate` and a
non-id value. Fail-before against a9ea06f for (a), (b), (c). Re-run the HB8a measurement (`python3 lib/review_rounds.py
prepare`, read-only) and report earlier-findings chars (round 1 gave 49,556 -> 13,875).

## Verification (the worker tests once)
Run tests.test_review_rounds, tests.test_retry_packet, tests.test_tp_review, tests.test_family_sweep, tests.test_review_coverage
and tests.test_session_env once, through `tools/agent/test-run.sh`, with `/usr/bin/python3` outside tmux (`env -u TMUX -u
TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`), short fresh `TMUX_TMPDIR` under /tmp, the log starting
with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`, then `git reset -q`. Main runs the four cells and the
discovery. No Claude or pi start; nothing to 127.0.0.1:1234 or any network; private tmux `-L` sockets only. Do not edit config/.
Search with safe_grep, read with Read/safe_read; never read claude/mcp.json, .env*, auth files or config/local.json. Commit
nothing. No background shells. Report: findings F1/F2 each fixed with file:line, tests added/extended by name, fail-before
output, the measurement. `RESULT_STATUS: complete` as the last line.
