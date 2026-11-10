# PD-C round 5: a build is up to date only when its bytes are proved; recovery finds every complete record (Main, 2026-11-10)

Review review-coder-0ccf1fbaae-043721 (REQUEST_CHANGES): F1-F2 defects, F3-F5 checks. Report, probes and evidence:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-0ccf1fbaae-043721/`.
Rerun its probes first (`evidence/probe.py` for F1, `evidence/recovery-probe.mjs` for F2) and record each failing before your change.
The review replayed round 4's tables (48/0 parity, 19/0 removal, 51/0 recovery) and round 3's (864/0, 205/0, 155/0); keep them. Main's
decisions from rounds 2-4 stand.

## Fix (all `Closes by: code`), each as a rule with its input space built by code, counts before and after
- **F1 A build is up to date only when its bytes are proved.** For every recorded build, with or without a recorded digest, `build`
  says `up_to_date` and `status` trusts it only when the file's bytes are proved: the digest matches, or, for a legacy entry with no
  digest, the bytes equal a fresh render of the committed state that build records. Anything else is refused or rebuilt with a named
  reason, never trusted. This closes the round-2 follow-up on digest-less builds. Attack: legacy and digest-bearing entries × file
  unchanged / rewritten with other text / truncated / emptied / replaced by a directory or symlink / removed × last and older build,
  then `build` and `status`. Expected: 0 rows where changed bytes are trusted.
- **F2 Recovery finds every complete record the CLI would import.** Replace the hand-written scanner's edge cases with one rule: from
  stored text, recovery offers exactly the complete note and input records that the CLI's ingest would accept on their own, whatever
  surrounds them. Attack, built by code from valid saved states holding 0, 1 and several records of each kind:
  - truncate at every byte offset;
  - prefix a BOM, whitespace or other bytes; append whitespace or other bytes;
  - nest the refused part before, between and after the good records.

  For each row compare the offered records with an oracle that runs the real CLI ingest on each complete record object in the text.
  Expected: 0 missed and 0 extra. Keep the depth and size refusals; do not accept the refused part into state.

## Checks
- **F3:** correct the six historical-site mappings the review lists, and say the review module has 21 tests (120 is the five-module
  total). Give the sweep in the required columns.
- **F4:** fix the `f1_calls.py` oracle so it decides the commit point from what actually happened, not the injection index (Python
  3.14 calls `os.mkdir` for existing directories, and `exist_ok=True` can swallow an injected OSError). Rerun it on both interpreters
  and report both counts. Do not skip rows.
- **F5:** add sessions and checkpoints (every saved record kind) to the F1 parity table from round 4, or say which kinds the CLI does
  not import and why they are out of scope.

## Report
- Fail-before for each new test against 2801f82.
- tests.test_product_design_review, tests.test_product_design_store, tests.test_product_design_package, tests.test_skill_catalog,
  tests.test_session_env in all four cells, one after another (fresh `TMUX_TMPDIR`; each log starts with `date -u`, `TMUX_TMPDIR` and
  `git add -A && git write-tree`, followed by `git reset -q`; each module's time per cell).
- The attack table for every rule (input space printed by code, counts before and after, on both interpreters), the sweep of every
  place each rule applies, Self-review, deviations. Rerun rounds 3 and 4's attacks on the final tree on both interpreters.
- Synthetic fixtures only; no private product names; nothing sent to 127.0.0.1:1234 or any network address. Do not change
  `docs/plans/product-design/pd-b-note.md`. Commit nothing. `RESULT_STATUS: complete` as the last line of your final reply, exactly.
  No background shells or Chrome processes left running.
