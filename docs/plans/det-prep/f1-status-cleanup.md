# F1: finished workers leave the status list and free their panes

Lane `status-cleanup`, step 1. Level: `mid` coder. Touches: `lib/tp_window.py`, `bin/tp-status`, `bin/tp-clean`,
`lib/task_record.py`, `lib/worker_process.py`, `config/orchestration.json`, `tests/test_tp_window.py`,
`tests/test_tp_clean.py` (new if absent), `README.md` (the status pane paragraph only).

## Why
The owner sees 200+ rows in the status pane on the work machine. Every task stays in `tasks/` until the session ends:
`collect` and `review` end the process but never archive, and `tp-clean` archives only tasks whose pane is gone. Each
finished worker also keeps a dead tmux window (`remain-on-exit on`, bin/tp-spawn:704) that nothing reclaims mid-session.
The window code already pages to the newest 60 rows plus active ones (lib/tp_window.py:42, bin/tp-status:56), but
scrolling still reaches every old row.

Moving a task directory is not free: `task_dir()` refuses archived ids, so review, retry lineage, queued `after`
dependencies and `lane close` (which needs a settled writer in `tasks/`, lib/lane_steps.py `_author`) break if the wrong
task moves. The rule below archives only tasks nothing can still ask for.

## Change
1. `lib/task_record.py retired(state_dir) -> list[(id, reason)]`: a task is retired when ALL hold:
   - lifecycle is `completed` (collected.json present), `stopped`, `killed` or `failed`; never `settled`, never ACTIVE;
   - `meta.worker != "main"`; no pending `capability-request.json`; no `process.json` awaiting reap; record readable;
   - no queued item names it in `after`; no task in `tasks/` has it as `parent` (retry lineage) unless that child is
     itself retired; no unfinished review has it as `review-of`;
   - for a lane task: its step is closed in the lane record, or the lane is removed; else not retired;
   - for a non-lane task: finished more than `status.retire_after_minutes` ago (config, default 30).
   Each non-retired finished task gets the reason it is kept (`open lane step a1`, `parent of coder-x`, ...).
2. `tp-clean --retired` (and the `c` key in tp-status, which keeps its current behaviour and adds this): for each retired
   task, kill its pane the way `tp-kill` does (dead panes only; a live process is never killed here), then archive it with
   `archive_task`. Prints `archived N, kept M (reasons: ...)`.
3. Automatic: when `status.auto_retire` is true in `config/orchestration.json` (default true), `tp-status` runs the same
   retire pass at most once a minute, off its draw path (a failure is shown in the footer, never stops the pane).
4. Display cap: `status.max_rows` (default 50). Rows are active tasks first, then the newest finished ones up to the cap;
   a footer line says `N finished not shown (tp-agent find / tp-task list --all)`. No row that is ACTIVE, `settled` or
   needs input is ever hidden, even past the cap.
5. Fix the stale comments that say stop and collect move a task to the archive (bin/tp-agent:107, bin/tp-review:133) by
   naming them in the report for Main; those files belong to other lanes.

## Done means
1. A fixture state dir with every case in item 1 gives exactly the expected retired set and the kept reasons.
2. `tp-clean --retired` archives the retired set, kills only dead panes of those tasks (tmux calls stubbed), and leaves the
   rest; a second run is a no-op.
3. After archiving, `tp-agent lane close` on a closed step, `tp-agent review` of a kept author, and a queued `after` item
   behave as before (tests drive one of each through the existing helpers).
4. With 120 finished and 3 active fixture tasks, the window shows the 3 active plus 47 newest finished and the footer line.
5. `auto_retire` false disables item 3; config keys are documented in README.
6. Tests for 1, 2, 4 fail without the change.

## Verify on battle-soccer (V-F)
Dispatch 6 short `--local-only` researcher tasks there, collect them, wait the retire time (set 1 minute in the test
workspace's config), and check the pane shows only active rows plus a footer, `tmux list-windows` lost the `sa:` windows,
and `.tmux-pi/sessions/<id>/archive/` holds the six.
