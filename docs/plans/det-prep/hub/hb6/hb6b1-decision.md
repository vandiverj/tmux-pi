# HB6b1: owner decision (adopt the arm-B wait records?), lane hub-waits-b head 6515b5c

WT1 says "Advisory: the owner decides on adoption", so Main stopped before merge. Nothing from this lane is in main.

## What it adds (records only; no worker behaviour change, no new event kind)
- **Queue wait:** `tp-agent dispatch --after` records `queued_at` and `queue.reason` events; leaving the queue (start, or
  `tp-agent stop` of a queued task, kept in `<state>/queue-cancelled/<id>/`) records the wait.
- **Who answered:** `tp-agent message --resolves R --by owner --quote "..."` records the owner's words against R whatever
  closed R (worker state change, supersession, withdrawal). Without `--by owner`, `by` stays `main`. Never inferred.
- **Permission outcome (Claude workers):** report.sh tracks calls in flight by `tool_use_id`; a prompt closes `approved` only
  on the asking call's PostToolUse, `denied` only on its PermissionDenied, else `left`. After the 16-call list drops an
  entry, every close is `left` until it empties. The approval instant itself is not recorded (no Claude hook fires then):
  the span ends at the call's end and is marked inferred.
- **tools/waits/timeline.py** prefers these records; without them it gives exactly HB6a's output (case 2 byte-identical).

## Measures (case 1 = planted schedule run through the real producers; case 2 = this Mac's real task dirs)
| measure | arm A (merged HB6a) | arm B (this lane) | bar |
|---|---|---|---|
| case-1 attribution | 775/1045 = 74.2% | 1045/1045 = 100% (97.2% with the permission task) | 90% |
| stall visible | 61 s | 61 s | 2 min |
| report.sh hook cost | 0.0643/0.0650 s | 0.0649/0.0649 s (noise) | <1% task time |
| added records | | queued task +2 events (532 B) +42 B meta; owner answer +37 B (+~535 B after another close); state.json <= 1.4 KB | |
| case 2 (real dirs) | | identical to arm A (the records do not exist there yet) | |

Not measured: a real `tp-agent stop` of a queued task (in-process 0.48 ms). Not built (brief): per-state events, `lease.*`,
test-run started/finished, Main's own turns; `lease.*` and Main's turns are what explains `waiting:main` on real runs (HB6a
smoke: 28% of the critical path is Main's time between tasks).

## Rounds
| Round | Worker | Review | Result |
|---|---|---|---|
| 1 (Opus) | coder-81064fbb60, 1f1bb74 | review-coder-81064fbb60-004745 | REQUEST_CHANGES: answer race, queue cancel, permission by name, confidence |
| 2 (Opus) | coder-451805a8e9, 8122a32 | review-coder-451805a8e9-013439 | REQUEST_CHANGES: superseded close, calls-list eviction |
| 3 (Sonnet, rule-first) | coder-52842bfcd1, 6515b5c | review-coder-52842bfcd1-020016 | APPROVED_WITH_NOTES (report wording: 7 race rows not 8; 0.33 vs 0.4 us) |

Cells on main 02e315f + 6515b5c (tree 31b1284): 227 tests x 4 (py3.9/3.14, outside tmux / simulated in-tmux env), 0 failures.
Full suite on the same merged tree 31b1284 (`tests/run_parallel.py`, private TMUX_TMPDIR): 5533 tests OK (skipped 78), 552 s;
same count as HB6a's run because round 2-3 extended existing table rows only.

## Options (Main recommends A)
- **A. Adopt this slice:** Main closes the step and merges (approved review of the exact tree). Then the next live lane step
  runs with these records on, and the full WT1 decision (keep arm B, add `lease.*`/Main's turns) follows from that run.
  To use it day to day: answer a worker on your behalf with `--by owner --quote "<your words>"`.
- **B. Keep arm A only:** park the lane; timeline stays at 74% on queue-heavy runs and shows owner input as Main's.
- **C. Adopt part:** e.g. queue wait and owner answers, leave report.sh unchanged (permission spans stay `left`/inferred as in
  arm A). Needs one mechanical round and a re-review.
