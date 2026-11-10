# F1 round 8 (owner-approved extra round): every task record is location-free

Owner, 2026-11-08: "use your recommendation for F1". F1 was parked after round 7, and this round was added on the owner's decision. If it is rejected, F1 parks again; there is no round 9.

## The finding (review-coder-627bab8253-175516 F1)
`lib/task_record.py:837` reconciles `task.md` with `relative=True` before the rename. Every other `write_record` caller still writes absolute paths: `bin/tp-contract` (`:226,263,294,375,387,431`), `bin/tp-task`, `bin/tp-spawn`, `lib/preflight.py`, and `lib/task_record.py:776,910,939`. A `tp-contract status` that lands between the reconcile and the rename leaves the archived `task.md` pointing at the old, nonexistent location. Family: retirement-dependency-order.

## Rule (closes the family by construction)
There is one way to write a task record, and it is location-free.
1. Remove the `relative` parameter from `write_record` and `_links`. Every path to a file **inside the task directory** (result, request, packet, meta, state, transcript, run.sh, report and any other link `_links` emits, plus the "Result file" line) is written by its name relative to the task directory, always.
2. Paths **outside** the task directory (the workdir, external research artifacts, an absolute `contract.artifact` that does not lie under the task directory) keep their current form. `:821-834` already makes that split for the contract artifact; reuse it and don't add a second test for "inside".
3. Every reader of `task.md` links resolves a relative link against the record's own directory. Round 7 added this. Confirm every reader does it (`parse_record`, `tp-status`, `tp-task`, `tp-review`, `tp-clean`, `lib/task_archive.py`, and any other caller found with `git grep -n "parse_record\|task\.md"`), and fix any reader that does not.
4. With no absolute in-task path left to write, a writer that races the rename can't produce a stale record. The reconcile at `:837` stays and simply calls `write_record` like everyone else. Don't add a lock.
5. Old records already on disk with absolute links must still read correctly: an absolute link is used as-is.

## Tests (table + parity)
- **Parity test:** collect every `write_record(` call in `bin/` and `lib/` with `git grep` over fixed literal roots, then require all of these:
  - the signature has no `relative` parameter, and no call passes it;
  - for a fixture task directory, each writer path that can be driven from a test produces a `task.md` with no occurrence of the task directory's absolute path. Cover at least `tp-contract status`, `tp-task`, `preflight`, the reconcile, and `tp-spawn`'s starting record.
  - List the callers in a table in the test, with a parity assertion that the table equals the grep result, so a new caller fails the test until it is added.
- **Race test:** reconcile, then run `tp-contract status` on the same task (the round-6 interleaving), then rename. The archived `task.md` resolves every link to an existing file in the archive.
- **Reader table:** one row per reader from rule 3, each run against a record with relative links and a legacy record with absolute links; both resolve.
- Keep all earlier F1 tests passing. Run `test_tp_clean`, `test_state_symlinks`, `test_tp_window`, `test_process_end`, `test_task_record`, `test_status_repaint`, and any module that covers `tp-contract` or `tp-task` you touch, each separately, and report command, count and seconds.

## Report
- The sweep table: every caller and reader, file:line, and its disposition.
- State plainly that `relative` no longer exists and that the parity test enforces it.
