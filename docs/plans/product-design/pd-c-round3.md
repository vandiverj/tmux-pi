# PD-C round 3: the commit point holds under any interruption; what the page saves it can reload; page and CLI agree (Main, 2026-11-10)

Review review-coder-0ffa56c1fa-015007 (REQUEST_CHANGES), F1-F6 defects, F7-F8 checks. Report, reproducers and evidence:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-0ffa56c1fa-015007/`.
Rerun its reproducers first (rename-interruption.py and the probes behind evidence/probe.json, browser-probe.json, unicode*.json) and
record each failing before your change. Round 2's fixes and attack rows were confirmed (round 1 F1, F2, F4, F5, F7, F8, F9, F11, F12,
F13); keep them. Main's round-2 decisions stand (F1 by refusal, export splitting).

## Fix (all `Closes by: code`), each as a rule with its input space built by code, counts before and after
- **F1 The commit point survives any interruption.** Once the journal (`txn/txn.json`) is in place, nothing in the same process removes
  it or the build file, for any exception including `BaseException` (`KeyboardInterrupt`, `SystemExit`); only recovery decides. Attack
  at the real call level, not only the named fault hooks: wrap every `os.replace`/`os.rename`/`os.link`/`os.unlink`/`os.fsync`/file write
  that `build`, `commit` and recovery make, and raise `KeyboardInterrupt` and `OSError` immediately before and immediately after each
  call (the call having taken effect). Print the list of call sites found. Pass = after the commit point the file is kept and recovery
  completes or reports; before it, only this build's own bytes are removed.
- **F2 Status checks every recorded build, not only journals.** `status` (and `trust_problems`) verifies, for every build the committed
  state records, that the file exists as a regular file and its digest matches; otherwise a trust problem (`package.build-missing`, or a
  mismatch code) and a non-zero exit. This is the detection Main's round-2 acceptance relied on; it was not there. Attack: delete,
  truncate, rewrite, replace by a directory or symlink, at every fault point and after a clean build.
- **F3 What the page saves, it can reload.** Local state is checked against limits that fit local state, not the single-bundle import
  limit; any state the page stored successfully loads on reload with the same notes and inputs. If stored bytes are refused, the notice
  offers a way to recover them (export the retained bytes as importable parts). Attack: accumulated notes/inputs across many imports and
  added notes, up to and past each bundle limit, then reload.
- **F4 The page refuses exactly what the CLI refuses.** The page's bundle check and the CLI's `check_bundle` give the same verdict for
  the same bytes, including lone surrogates and other invalid Unicode at any depth of any field (state, inputs, ids, keys), and the
  page's export check uses the full rule set (encoding, schema, records, bytes, depth), not size/count/depth only. Attack: a generated
  parity table (every field path × invalid encodings, plus round 2's F8 and F11 rows), page verdict vs CLI verdict, 0 mismatches.
- **F5 Every export has a recovery path.** A multipart export offers the same fallback a single-part one does (copyable JSON or a
  per-part save control for each part) and does not mark anything exported before each part has been offered. Attack: 1..4 parts ×
  downloads allowed/denied.
- **F6 A build is a function of committed state.** Building twice with no change in between gives `up_to_date` on the second build after
  any sequence of operations (remap, removed target, re-import, rebuild). Re-anchoring that changes stored state commits before the
  fingerprint is taken, in the same transaction or a prior one. Attack: generated sequences of apply/import/remap/build/registry change
  (at least every pair and triple), each followed by build, build; the second must be up to date.

## Checks
- **F7:** correct the round-2 report: status did not detect the late-writer race; the 20 fault hooks are not every boundary. Show the
  corrected checks after F1/F2.
- **F8:** replay round 1's 4074-row campaign and the four fail-without mutations against the final tree; attach the logs.

## Report
- Fail-before for each new test against 91e908a.
- tests.test_product_design_review, tests.test_product_design_store, tests.test_product_design_package, tests.test_skill_catalog,
  tests.test_session_env in all four cells, one after another, as round 2 (fresh `TMUX_TMPDIR`; each log starts with `date -u` and
  `git add -A && git write-tree`; each module's time per cell).
- The attack table for every rule (input space printed by code, counts before and after), the sweep of every place each rule applies,
  Self-review, deviations.
- Synthetic fixtures only; no private product names; nothing sent to 127.0.0.1:1234 or any network address. Do not change
  `docs/plans/product-design/pd-b-note.md`. Commit nothing. `RESULT_STATUS: complete` as the last line of your final reply, exactly.
  No background shells or Chrome processes left running.
