# PD-B round 4: nothing is published that its own reader would refuse (Main, 2026-11-09)

Review of coder-54e75bf58e (REQUEST_CHANGES). Report, `probes_r3.py` and both interpreters' results are in the newest
`review-coder-54e75bf58e-*` directory under
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/`.
Run `probes_r3.py` on both interpreters before you change anything. Round 3's displacement rule, history validation and the
public-input text check were confirmed; do not rework them.

## The defects
- **F1:** interrupt an apply after its commit point, set the staged `history.json` `base_revision` to 999, run `--recover`: it
  succeeds, deletes the journal, and the next `status` refuses the history it just published. The staged commit record has the
  same gap (store.py ~669-683).
- **F2:** a document titled `chr(0xd800)` passes `validate_docs`, then `T.commit` raises `UnicodeEncodeError`: `commit_record`
  hashes content before the representability check (store.py ~509-520).
- **F3:** the report says adoption places only `commit.json`; it also replaces `package.json`. Correct it.

## Main's decisions
1. **A command never publishes a state its own reader refuses.** Before any commit or recovery makes records live, it validates
   the prospective state as a whole (the history with the new entry, the commit record, the manifest, the package documents) with
   the same functions `status`/`validate` use. A staged record that fails is not published: recovery refuses with the structured
   control error, keeps the journal and the staged files, and names them. One function builds and checks the prospective state;
   apply, adopt, ingest and recover all call it.
2. **Text representability is checked at the one place bytes are produced.** The canonical render/encode function itself refuses a
   value that is not representable as UTF-8 with the documented structured error, so nothing can hash, digest or write such a
   value by any route: public input, the transaction API (`T.commit`), recovery. Remove separate pre-checks that this makes
   redundant, or keep them only as earlier, friendlier messages that call the same function.

## Attack the rule before you report
- Rule 1: each staged control file (history, commit record, manifest, each document) × each corruption (missing, empty, wrong
  JSON type, wrong field types, wrong revision, wrong `base_revision`, digest mismatch, huge and negative numbers) × interrupt
  point (before and after the commit point) × apply, adopt, ingest, recover. Expected: nothing published that `status` then
  refuses; the journal kept when refused.
- Rule 2: lone high and low surrogates, an escaped pair, a valid astral character × every field of every document type × entry
  route (CLI text, bytes, dict, file on disk, `T.commit` directly, recovery of a staged file). Expected: a structured refusal or a
  clean write, never an exception.
Print each input space by code, run every case on both interpreters, report counts. Keep worth-keeping rows as tests and fold
`probes_r3.py` in.

## Report
- Fail-before for each new test against 3da157e (the round-3 checkpoint).
- The four-cell run (`tests.test_product_design_package tests.test_product_design_store tests.test_skill_catalog
  tests.test_session_env`): /usr/bin/python3 and /opt/homebrew/bin/python3, each outside (`env -u TMUX -u TMUX_PANE -u TP_SESSION
  -u TP_TASK_ID -u TP_STATE_DIR`) and inside (`env TMUX=/tmp/x,1,1 TP_SESSION=dummy TP_TASK_ID=dummy`), after your last edit. Each
  log starts with `date -u` and the working-tree hash; each file's time per cell.
- Attack counts per rule, the review's probes rerun, Self-review, deviations.
- Commit nothing; `git add` new files. Brief rules (pd-brief.md) still apply. `RESULT_STATUS: complete` as the last line.
