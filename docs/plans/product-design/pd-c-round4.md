# PD-C round 4: the page exports only what the CLI imports; a build rebuilds what was removed; every refused state has its recovery (Main, 2026-11-10)

Review review-coder-7239b69ac6-035042 (REQUEST_CHANGES): F1-F3 defects, F4 note, F5-F6 checks. Report, probes and evidence:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-7239b69ac6-035042/`.
Rerun its probes first (`evidence/probe.mjs` for F1 and F3, the restore sequence in `evidence/restore-probe.json` for F2) and record
each failing before your change. The review confirmed round 3's fixes and attack rows (864, 205 and 155 rows replayed with 0 failing);
keep them. Main's decisions from rounds 2 and 3 stand, including every accepted round-3 deviation.

## Fix (all `Closes by: code`), each as a rule with its input space built by code, counts before and after
- **F1 The page exports only what the CLI imports.** Export validation uses the same rule set as the page's import classification and
  the CLI's `check_bundle`, conflicting ids included, applied to each exported part and to the parts together. Use one shared rule
  path in the page, not a second copy of the conflict check. Attack: a parity table over saved state built by code, crossing:
  - every rule the CLI's ingest applies (list them from the code, print the list): conflicts (same id, different content; same
    content), duplicates within one part and across parts, schema, encoding, depth, bytes, record counts;
  - record kinds (notes, inputs, any other kind saved);
  - single-part and multipart export.

  For each row: page export verdict vs. the real CLI ingest of every exported part. Expected: 0 rows where the page exports something
  the CLI refuses, and 0 rows marked exported that are not importable. Report the page's choice for a conflict (refuse the export with
  a reason, or export without the conflicting records and say so) and keep it consistent with import.
- **F2 A build rebuilds what was removed.** Restoring a recorded build creates whatever parent directories are missing, the same way
  the normal build path does. Attack: after a clean build, remove each generated piece by code (the last HTML, an older HTML, the
  whole `builds/` folder, `builds/` replaced by a file or a symlink, any other generated output the package records), then `build`
  and `status`. Expected: build restores or rebuilds when the bytes are reproducible, otherwise refuses with a named reason; never
  `io.error`; status agrees afterwards. Run it against b253478 and after.
- **F3 Every refused saved state offers its importable records.** Any refusal on load (before or after parsing: depth, size,
  encoding, schema, a bad field) shows the recovery control whenever at least one record in the stored bytes is importable on its
  own, without accepting the refused part into state and without lifting the depth refusal. Attack: each refusal branch of the loader
  (list them from the code) × one valid note present / none present / several. Expected: control present exactly when an importable
  record exists, and the recovered parts import with the CLI.

## Notes and checks
- **F4:** a restored build must not print "nothing was written" (`lib/product_design/cli.py:110-113`). Fix the message to say it was
  restored.
- **F5:** correct the round-3 report: the external attack script, not the retained unittest, wraps the writes and both exception types.
- **F6 (Main):** Main supplies the preview log paths and the private-name scan output with the next evidence; nothing for you.

## Report
- Fail-before for each new test against b253478.
- tests.test_product_design_review, tests.test_product_design_store, tests.test_product_design_package, tests.test_skill_catalog,
  tests.test_session_env in all four cells, one after another, as round 3 (fresh `TMUX_TMPDIR`; each log starts with `date -u` and
  `git add -A && git write-tree`, followed by `git reset -q`; each module's time per cell).
- The attack table for every rule (input space printed by code, counts before and after), the sweep of every place each rule applies,
  Self-review, deviations. Rerun round 3's F1, F2 and F6 attacks on the final tree and report their counts.
- Synthetic fixtures only; no private product names; nothing sent to 127.0.0.1:1234 or any network address. Do not change
  `docs/plans/product-design/pd-b-note.md`. Commit nothing. `RESULT_STATUS: complete` as the last line of your final reply, exactly.
  No background shells or Chrome processes left running.
