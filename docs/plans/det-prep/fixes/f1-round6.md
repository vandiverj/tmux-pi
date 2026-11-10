# F1 round 6: no rollback; atomic, repeatable reconcile, then one move (route: design)

Review: /Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-c556d93f24-171919/result.md and report.md beside it.

Five rounds of rollback (a hand-kept list, then a snapshot, then a whole-directory copy) each missed something: symlinks, mode, FIFOs, hard links, writes that land after the copy. Restoring a directory exactly is not achievable in general, so stop doing it.

## Rule
`archive_task` never restores. It works in two phases:
1. **Reconcile in place, in `tasks/<id>`.** Every write is atomic (write a temp file in the same directory, then `os.replace`). Every write is idempotent and yields a valid live task record, so a failure part-way leaves a task that is still correct in `tasks/` and can be archived again later. Write through no symlink: refuse, and leave the task, when a target is a symlink or not a regular file.
2. **Move:** a single `os.rename` of the directory into `archive/` as the very last step. Nothing touches the task after it.

Reserve the archive name only at the move. Remove the copy, the snapshot, and the reservation from the earlier rounds.

**Parents (F4):** a task whose `parent` chain loops (A→B→A, or longer) is not archived. It stays in place with a reason, as the other ordering failures do. The ordering function detects the loop and doesn't rely on depth.

## Tests (one table)
- each reconcile writer failing in turn (the task stays in `tasks/`, still valid, and archives on a second run);
- a symlinked `task.md` (refused, the outside file unchanged);
- a FIFO in the task (archives, because rename works);
- a hard-linked file (the inode is unchanged after both failure and success);
- a concurrent write between reconcile and move, simulated with a hook (the write survives);
- a 2-cycle and a 3-cycle of parents (neither archived, each with a reason);
- a failure of the move itself (nothing left in `archive/`).

## Report
A Site / Disposition / Evidence sweep table over every writer in reconcile and every caller of `archive_task`, with the exact test commands and their log paths.
