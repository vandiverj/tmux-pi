# F1 round 5: rollback by whole-directory copy (route: design)

Review: /Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-fd0d9b14db-165828/result.md and report.md beside it.

Round 4 rebuilt the task directory from a snapshot of names, bytes and mtimes. The review found four more things the snapshot missed: a symlink that was replaced, a write through an intervening symlink, the file mode, and an archive reservation left behind when the scan fails. The list approach will keep missing attributes, so replace it.

## Rule
Before reconciling, `archive_task` makes a full copy of the task directory beside it (same filesystem). Use `shutil.copytree(src, tmp, symlinks=True, copy_function=shutil.copy2)` plus `os.chmod` and `os.utime` on directories, or an equivalent that copies links as links and keeps mode and mtime. On any failure, it deletes the damaged directory and renames the copy into place with `os.replace`/`os.rename`. It never writes into files inside the damaged tree. On success it deletes the copy.

Every step that creates something (the archive reservation, the copy, the temp names) is undone in one `try/finally` that covers every exit path, including a failure before the snapshot.

## Tests (one table)
Rows: each reconcile writer failing in turn; `task.md` that started as a symlink; a symlink planted mid-reconcile pointing at an outside file holding `PRIVATE` (the outside file must be unchanged); a file at mode 0644 (still 0644); an unreadable task directory (no archive entry left); a copy failure (task untouched, no temp left).

Each row asserts the task directory is identical afterwards (names, file types, link targets, bytes, mode, mtime), and that nothing new exists in `archive/` or beside the task.

## Report
Include a Site / Disposition / Evidence sweep table covering every caller and writer of `archive_task`. Also include the new table's failure count against the round-4 code.
