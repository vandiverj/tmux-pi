# F1 round 9 (owner-approved final round): no writer can recreate a task directory, and the footer counts by state

Owner, 2026-11-08: "use your recommendation for F1". This is the last round. If it is rejected, F1 is left out of this campaign.

Round 8 is confirmed fixed for every round 1–7 case and for location-free records. Keep all of it.

## Findings (review-coder-dd5117f3c0-183947)
- **F1 (family retirement-dependency-order, still open):** an in-flight `tp-contract status` holds the task's path and writes after `archive_task` renamed the directory (`bin/tp-contract:125-141,226`). `write_record` calls `safe_write.ensure_dir(d, ...)` (`lib/task_record.py:318`), which recreates `tasks/<id>`. The result is a ghost directory with a `task.md` and no `meta.json`. The round-8 race test only covers a write that lands before the rename.
- **F2 (new):** `lib/tp_window.py:190-207` labels every omitted non-active, non-settled row as "finished". With ten escalated tasks, no finished tasks and a five-row cap, the footer says `5 finished not shown`.

## Rule for F1 (closes the family by construction, no lock)
1. **Only the task's creator creates a task directory.** The creators are `tp-spawn`, the queue/dispatch path that makes a new task, and any other path that makes a new task id. Find them with the sweep below. They create it through one named function (for example `task_record.create_task_dir`). Nothing else in `bin/` or `lib/` may create `tasks/<id>`. That includes `ensure_dir`/`mkdir`/`makedirs`/`mkdir -p` on a task directory and writes that create missing parents.
2. **Every other write into a task directory works through a directory handle.** Open the directory once with `os.open(d, O_RDONLY | O_DIRECTORY)`; refuse links the same way `safe_write` does today. Then create the temp file and rename it relative to that fd (`dir_fd=`).
   - If the open fails because the directory is missing, the writer refuses with a clear "task <id> is no longer active (archived or removed)" message and a non-zero exit. No traceback and no directory is created.
   - If the rename happens after the open, the write lands in the directory where it now is (the archive). That is correct, because round 8 made the record location-free. No temp file is left behind on any path.
3. This applies to `write_record` and to every other writer into a task directory: `meta.json`, `state.json`, `result.md`, the contract files `tp-contract` writes, `retention.json`, `pinned`, and any others the sweep finds. Put the dir-fd write in one shared helper (in `safe_write` or `task_record`) and route all of them through it. Don't hand-roll it per caller.
4. `archive_task` is unchanged: reconcile, then one rename.

## Rule for F2
The footer names what it omits by state, using the same state classification the rows use. Examples: `3 finished, 2 escalated not shown (...)`, or a single label when there is only one kind. A kind with a zero count is not printed. The footer must never use a state word for rows that are not in that state.

## Tests (table + parity)
- **Writer parity:** sweep `bin/` and `lib/` with `git grep` over fixed literal roots for writes into a task directory (`write_record(`, `_write_atomic(`, `ensure_dir(`, `mkdir`, `makedirs`, `write_text(`, `open(... "w"`, shell `>`/`mkdir -p` in `bin/` scripts) and classify each hit as creator, dir-fd writer, or not a task-directory write. Put the table in the test. Assert that the table equals the grep result, and that only the named creator function creates a task directory.
- **Race tests (after the rename),** one row per writer that can be driven from a test, at least `tp-contract status`, `tp-task`, `preflight`, and the reconcile writer:
  - Open the writer's handle or start the write, rename the directory to the archive, then let the write finish. Check that the write is not under `tasks/<id>`, that the archived record resolves every link, and that no temp files are left.
  - Also cover a writer that starts after the rename. It refuses with the message and a non-zero exit, and `tasks/<id>` does not exist afterwards.
  - Use a deterministic hook or an injected step between open and write, not a sleep.
- **Creator test:** `tp-spawn` (and each other creator) still creates a working task.
- **F2 table:** omitted rows of finished only, escalated only, mixed, and zero omitted. Each gives the exact footer text.
- Keep every earlier F1 test passing. Run `test_tp_clean`, `test_state_symlinks`, `test_tp_window`, `test_process_end`, `test_task_record`, `test_task_record_r2`, `test_status_repaint`, `test_contract`, and any module covering a writer you touch. Run each separately and report the command, test count and seconds.

## Report
- The writer sweep table: every hit, with file:line and its classification.
- State plainly which function is the only creator, and that the parity test enforces it.
