# PD-B round 2: a commit never loses data or trust it cannot account for (Main, 2026-11-09)

Review review-coder-19a00f8082-201623 (REQUEST_CHANGES). Its report, `probes.py` and `probes.log` are in
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-19a00f8082-201623/`.
Read the report and run every probe before you change anything. The decision-9 rules, dedupe, conflicts and staleness were
confirmed; do not rework them.

## The defects, grouped by the rule each one breaks
1. **What is committed can always be read back (F1, F6).** Every limit a reader enforces (file size, bundle size, any other)
   is checked on the result before the commit point, with the same constant, whatever form the input came in (bytes, str,
   dict). One function owns each limit; the reader and the writer call it. A refused commit writes nothing.
2. **No write replaces bytes it has not seen (F2).** At the commit point and in roll-forward, every file about to be replaced
   must still match the digest the change was computed from. If not, refuse (a revision conflict or an external-edit refusal,
   your choice, documented), keep the other writer's bytes, and leave the package readable and trusted state accurate.
3. **Control records are checked before they are believed (F3).** `commit.json`, the journal and history entries are
   validated for shape and type, and their revision must agree with history (the highest history entry, the files'
   recorded digests). Disagreement means untrusted, with a reason; no command writes over an existing history entry, ever.
   Malformed control data gives a structured refusal, never a traceback.
4. **Original feedback is never lost (F4).** Adopting a hand edit that removes or changes an imported note (or any record the
   plan says is preserved as original) is refused like an authority change, or the original is kept and the re-import is a
   conflict. Pick one, state it, test both orders (delete+adopt then re-import; re-import then delete+adopt).
5. **Every expected bad input is a structured refusal (F5).** Validate operation shapes (op, id, field paths, set/unset
   values) before use. Under `--json`, every refusal, including a missing or unreadable changeset file, prints the JSON
   error object and the documented exit code. No `TypeError`, `KeyError` or `AttributeError` reaches the user.

F7 (accepted uncertainty cannot be reopened) belongs to PD-E; do not change it now, list it in the note as owed there.
F8: correct the round-1 mismatch accounting in your report (count, list and log of development mismatches).

## Attack the rule before you report
For each of rules 1-5, name its input space from the code and schema, print it by code, run every case against the real
code and report counts. At least:
- Rule 1: every reader limit × each input form (bytes, str, dict, file path) × under, at, and over the limit × single
  bundle and the sum of several commits.
- Rule 2: an external edit to each package file × each moment (before inspect, between inspect and the commit point, during
  roll-forward via `--recover`) × each command that writes (init, apply, adopt, ingest).
- Rule 3: each control file × missing, empty, wrong JSON type, wrong field types, revision lower/higher/equal to history,
  history gap × each command (status, validate, apply, recover, ingest).
- Rule 4: each preserved record type × delete/change × adopt × re-import with same/different content, both orders.
- Rule 5: each operation × each field × wrong type (list, dict, number, null, bool) × `--json` on and off, plus missing,
  unreadable, empty and non-JSON changeset files.
Keep the cases worth keeping as test rows. Fold the reviewer's probes into the tests.

## Report
- Fail-before for each new test against e944d02 (the PD-B checkpoint).
- The four-cell run (`tests.test_product_design_package tests.test_product_design_store tests.test_skill_catalog
  tests.test_session_env`, both interpreters, outside and inside tmux) after your last edit, each file's time per cell.
- Attack table counts per rule, the reviewer's probes rerun (each now refused or preserved as stated), Self-review, deviations.
- Commit nothing; `git add` new files. Brief rules (pd-brief.md) still apply. `RESULT_STATUS` as the last line.
