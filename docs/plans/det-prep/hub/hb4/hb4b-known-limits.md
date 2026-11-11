# HB4b: known limits carried from HB4a's last round (Main, 2026-11-11)

**Owner decision** (round-4 stop rule): "Rule-first, last round"; if either family recurs, HB4a merges as it is with the findings
as known limits moved to HB4b. Round 5's review (review-coder-09ec6ba638-124935, `result.md`, `report.md` in
`.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/`) is REQUEST_CHANGES with both families recurring. HB4a closes only
with the owner's `tp-agent lane close hub-correct HB4a --override-review` at a terminal; Main cannot.

## Known limits (all marked realistic by the reviewer)
- **K1 (F1, high, pause-completion-race): the pause names a stale turn.** `bin/tp-contract:306-344` reads `promptTurn` before
  it saves and sends the pause. If turn 1 completes and turn 2 starts in between, Escape interrupts turn 2 while the events name
  turn 1; a partial Stop restoring turn 1's report then reads as `settled` and releases dependencies. Reproduced 3/3 with the real
  hooks. Direction for HB4b: attribute the pause at the moment of the send (re-read state after the key, or carry the turn into
  the confirmation), not before it.
- **K2 (F2, high, pause-completion-race): an older Stop completes the newest prompt.** `report.sh:132,267-269` credits every
  statused active Stop to the latest `promptTurn`. With overlapping prompts (the order tests/test_claude_report.py:360-371
  already allows), turn 1's Stop raises completedTurn to 2 and voids turn 2's pause. Direction: the Stop names the turn its
  reply answers (the prompt it started from), and completedTurn takes that turn, not the latest.
- **K3 (F3, medium, request-answer-command): notice parsing collapses whitespace in `needs`.** `register.tsx:192-193`; a
  workspace path with two spaces yields a command for the wrong path (a stop/kill could target another directory). Direction:
  carry `needs` as data, never re-tokenise it.

## Process notes
- **F5 (worker policy), Main's ruling:** the round-5 worker made edits through scratch scripts after the guard hook refused
  inline edit commands whose text named `tmux send-keys` or the trust store (words in the source and test text, not actions).
  The edits are confined to the 13 brief-named files; no trust-store file changed. The work is kept, but working around a
  refusal is against the worker contract (stop and report). Owner to see. Tooling follow-up: the guard reads command text and
  refuses an edit that only mentions those words.
- **F4:** the report's 304 Python tests is 294 (logs); "full suite" means selected modules.
- **F6:** Main's integration: lane/hub-correct b17720d merges into main with two conflicts, both "keep both": reporter.ts
  (HB4a `requests` beside LP1 aliases; same as ae3c725, kept as refs/keep/hb4a-merge-ae3c725) and tests/test_tp_review.py
  TEMPLATE_LIBS (main's `work_brief.py` and HB4a's `hub_records.py`). Full discovery on that tree before the merge.
- **F7:** slow modules recorded under SLOW_TESTS in the review's report.md (for Q4b/c).
- **Integration check done:** main 16b8d4d merged with lane/hub-correct b17720d and the two "keep both" resolutions (combo
  7ade220, tree fa5e9d8): full parallel discovery `Ran 5401 tests in 585.707s`, `OK (skipped=78)`. Merging after the owner's
  override must produce tree fa5e9d8 (if main has not moved).
