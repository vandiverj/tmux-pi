# F1 round 7 (final before park): location-free reconcile, owned reservation (route: design)

Review: /Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-3ebc82a9fa-173547/result.md and report.md beside it.

Round 6's design stands: reconcile in place, then one rename. Two gaps remain.

## Rules
1. **The reconcile writes nothing that depends on where the task will end up.** Any path in `meta.json` or the other reconciled files is written relative to the task directory, or left for readers to resolve from the record's own location. After a reconcile with no rename, every contract reader resolves every artifact in `tasks/<id>`. After the rename, they resolve the same artifacts in `archive/<name>`. Sweep every reader of those fields and make it resolve relative to the record's directory. Old records with absolute paths keep working.
2. **The archive name is owned before the rename.**
   - `os.mkdir(archive/<name>)` creates the reservation; `FileExistsError` means try the next name.
   - The task directory is then renamed onto that empty directory it owns, which POSIX allows.
   - A `try/finally` removes the reservation (`os.rmdir`, only while it is empty) on any failure before or during the rename.
   - No other directory can be replaced, because the code never renames onto a name it didn't create.

## Tests (added to the round-6 table)
- the rename fails after a reconcile: every contract reader finds every artifact in `tasks/<id>`;
- a successful archive: the readers find them in `archive/<name>`;
- a third party creates an empty `archive/<name>` between selection and rename (a hook): it is not replaced, and the next name is used;
- the reservation succeeds but the rename fails: no reservation is left behind;
- an old record with absolute paths is still readable.

## Report
A Site / Disposition / Evidence sweep table with one row per reader of the path fields, plus the exact commands and log paths.

If this round is rejected, Main parks F1 for the owner.
