# PD-C round 6: every recorded entry is proved; recovery decides by validation, not position, and keeps the original text (Main, 2026-11-10)

Review review-coder-980ff7195f-053141 (REQUEST_CHANGES): F1-F3 defects, F4 report check. Report, probes and evidence:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-980ff7195f-053141/`.
Rerun its probes first (`evidence/duplicate-build.py` for F1, `evidence/recovery-probe.mjs` and `evidence/recovery-oracle.py` for F2 and
F3) and record each failing before your change. The review confirmed round 5's tables (32/0 proof, 38,783/0 recovery, 864/980/0 calls,
88/0 parity) and every accepted deviation; keep them. Main's decisions from rounds 2-5 stand.

## Fix (all `Closes by: code`), each as a rule with its input space built by code, counts before and after
- **F1 Every recorded entry is its own proof obligation.** When several build entries name the same path, `build` restores or reports
  `up_to_date` only when the bytes it would leave satisfy every one of those entries (digest match, or a legacy render match). Entries that
  disagree with each other are refused with a named reason, never resolved by picking one. Keep the entry's identity from
  `recorded_build_problems` through to the restore decision (`review.py:470-486`); do not reduce problems to `(file, why)`.
  Attack: 1, 2 and 3 entries per path × each entry digest / legacy × agreeing / disagreeing (wrong digest, other revision, other source)
  × file present unchanged / changed / missing × last and older position, then `build` and `status`. Expected: 0 rows where `build` says
  `up_to_date` or restores while `status` then reports a problem, and 0 rows where any entry's digest goes unchecked.
- **F2 Recovery decides a record by validation, not by its position or key.** Every complete JSON object in the stored text is a
  candidate. A candidate is offered when the CLI's ingest would accept it on its own as a record of its kind; then its nested objects are
  its content, not separate candidates. A candidate that is not accepted (a wrapper, a saved-state object, an invalid record, an array
  element of a nested array) hides nothing: the scan looks inside it. Decide this once, in the scanner, for every kind; no per-wrapper
  special case.
- **F3 Recovery keeps the record's original text.** An offered record is exported from its exact bytes in the stored text, so a number
  the CLI accepts (`1.0`, `9007199254740993`) is neither dropped nor rounded. The normal browser importer's numeric refusal (Main's
  earlier decision) is unchanged. Depth, size and schema refusals stay.

  Attack for F2 and F3, built by code from valid saved states holding 0, 1 and several records of each kind:
  - wrap the state, or each record, in every reserved key (`{"notes":[`, `{"inputs":[`, `{"sessions":[`, `{"session":`, and every other
    key the saved state uses, listed from the code) and in nested arrays (`[[record]]`) at depths 1-3;
  - records carrying numbers whose JSON round trip changes the token (decimals with trailing zeros, integers above 2^53, exponents,
    negative zero) beside unknown fields;
  - round 5's truncation, surround and nest sets.

  For each row compare the offered records, by exact bytes, with an oracle that runs the real CLI ingest on each complete object in the
  text. Expected: 0 missed, 0 extra, 0 changed bytes. Round 5's excluded unclosed-string prefixes stay excluded.

## Check
- **F4:** in the sweep, use only the dispositions `fixed`, `already correct`, `out of scope`, `unresolved`. Correct the row for the
  historical `changes.py:667-675`: `changes.ingest` reanchors and commits; it does not trigger a build.

## Report
- Fail-before for each new test against 901077e.
- tests.test_product_design_review, tests.test_product_design_store, tests.test_product_design_package, tests.test_skill_catalog,
  tests.test_session_env in all four cells, one after another (fresh `TMUX_TMPDIR`; each log starts with `date -u`, `TMUX_TMPDIR` and
  `git add -A && git write-tree`, followed by `git reset -q`; each module's time per cell).
- The attack table for every rule (input space printed by code, counts before and after, on both interpreters), the sweep of every
  place each rule applies, Self-review, deviations. Rerun rounds 3-5's attacks on the final tree on both interpreters.
- Synthetic fixtures only; no private product names; nothing sent to 127.0.0.1:1234 or any network address. Do not change
  `docs/plans/product-design/pd-b-note.md`. Commit nothing. `RESULT_STATUS: complete` as the last line of your final reply, exactly.
  No background shells or Chrome processes left running.
