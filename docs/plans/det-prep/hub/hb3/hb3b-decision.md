# HB3b: owner decision (adopt rules in worker packets?), lane hub-packet head 0f07089

The brief says "Adoption is the owner's", so Main stopped before the close and merge. Nothing from this lane is in main.

## What it adds
Every worker that `tp-agent` starts (dispatch in or out of a lane, queue tick, retry, investigate) gets a packet section
`# Rules in force`: the owner's active records (`tp-agent work decide ... --by owner --quote ...`) whose scope covers that worker.
- **Scopes:** global; project (the worker's checkout or a worktree of it); role; lane; task (and the task it retries or investigates).
  An out-of-scope, provisional, superseded, withdrawn or worker-written record never appears.
- **Each line:** kind, scope, the whole rule text, the owner's quote (clipped to 120), the record id. Conflicting active records in
  one scope are shown as a conflict, never resolved.
- **Cap:** 20 lines and 4000 characters; whole lines only, newest first, then "+N more left out" with the listing command.
- **Fail open:** a broken store never blocks a start; the section says the rules could not be read. A machine with no store gets one
  calm line ("no work store ..., so no owner rules are recorded").
- Records are data: shell text, headings and `RESULT_STATUS:` lines in a record cannot change the packet.

## What it shows on this Mac
The store holds 2 active owner records (1 global: committed files never depend on machine-local state; 1 project: the HB7b
decision). A coder in lane hub-packet gets a 5-line section. Cost per start: one extra `git rev-parse` process.

## Rounds
| Round | Worker | Review | Result |
|---|---|---|---|
| 1 (Opus) | coder-9c299c1e6e, 41bc936 | review-coder-9c299c1e6e-115737 | REQUEST_CHANGES: rule text clipped at 240 chars (lost a prohibition); separate-git-dir worktrees missed project rules; missing store silent; omission count included conflict lines |
| 2 (Sonnet) | coder-8c0089d198, f749c16 | review-coder-8c0089d198-121035 | APPROVED, no new defects |
| 3 (Main) | 0f07089, comment only | review-coder-8c0089d198-134156 | APPROVED (the full suite's refusal audit read "tp-agent omits" in a new tp-spawn comment as a command) |

**Checks:** cells on main + f749c16: test_session_env, test_work_brief, test_lane_other_paths, test_work_decisions,
test_worker_env, 82 tests x 4, 0 failures. Every new row fails on the base. Private-name check: 1 line, config/routines.json:42.
Full suite on main + PF1c + HB3b (2026-11-12, 5546 tests): 2 failures, one from this lane (the comment, fixed in round 3; cells
141 tests x 4, 0 failures) and one already on main (test_local_provider, fixed by LP2, merged 0f82f94).

## Not in this lane (follow-ups)
1. Reviewers get no section yet: `bin/tp-review` was lane perf-review's (PF1c, merged ab722a0); one call passes `--rules-file`. Main
   recommends reviewers get it (some owner rules are about reviews).
2. Shadow trials and other direct `tp-spawn` callers get no section.
3. `lib/launch_record.py:271` still says owner records are not applied at launch (stale comment).
4. Some older tests dispatch without `TP_WORK_DIR` and read the real store read-only; a shared fixture should point it at a temp dir.
5. Known limit: conflict lines sit above the rules, so a store full of conflicting pairs pushes rules into the closing count.

## Options (Main recommends A)
- **A. Adopt:** close and merge after the batched full suite; then follow-ups 1, 3 and 4 as one small slice.
- **B. Park:** keep the lane; nothing changes for workers.
