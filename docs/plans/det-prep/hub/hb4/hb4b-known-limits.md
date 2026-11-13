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

## HB4c (merged 37e7a0e; review-coder-2d72f4c144-204159 APPROVED_WITH_NOTES)
K2 is fixed for two distinct Stops: a prompt that arrived while an earlier reply ran is owed (`owedTurn`), and a later
distinct Stop completes it (report.sh and reporter.ts). Step 1 found one Stop for all prompts in about 47 of 50 overlapping
groups (Claude Code injects the later prompt into the running turn) and two Stops in 1 certain case. In the one-Stop case turn
2 closes without its own completion; its pause stays live until a later turn completes (fails safe).
- **HB4c-L1 (review F1, medium, contrived, 0 seen): accepted.** P1 P2 S1 P3 S2 credits turn 3 at S2, before turn 3's reply
  exists. Up to S2 it is the same event order as the common "one Stop, then a new prompt", which must credit 3 there; the
  only hook-only alternative lags every later completion by one turn (F1's symptom). Direction if it is ever seen: the reply
  names the turn it answers (reply identity), not a hook rule. Main's decision, 2026-11-12.
- **HB4c-L2 (review F2, low, pre-existing): follow-up.** After a new prompt, a duplicate of an older statused Stop is taken
  as the open prompt's Stop and can complete the new turn (report.sh:127-149; traced, not run). Direction: check
  `already_kept` for an active Stop too. **Fixed by HB4d** (merged 2026-11-12, review-coder-e1560fb1e0-215351
  APPROVED_WITH_NOTES): rule 2', below.

## HB4d: a byte-equal Stop on the open prompt (rule 2')

Step 1 over 421 Claude workers / 560 Stops: 0 duplicate Stops (after a new prompt or otherwise); 1 legitimate reply to turn
N+1 that is byte-equal to an earlier kept reply (coder-61108a8b31, a re-sent statused report). The Stop input carries no
message or turn id, so the hook cannot tell the two apart. Rule 2': such a Stop ends the working state (state waiting, never
stuck), completes nothing, and leaves its turn owed (owedTurn = replyTurn); a later new Stop completes it (rule 3) or the
next prompt closes it (rule 1). pi (reporter.ts) is unchanged: it tells a new reply by message, not bytes.
- **HB4d-L1 (realistic, 1 of 560): accepted.** A legitimate byte-equal reply leaves turn N+1 without its own completion; a
  pause on it stays live until a later turn completes (fails safe, same cost as HB4c's one-Stop case). result.md holds the
  reply.
- **HB4d-L2 (contrived, 0 of 560): accepted.** After a real duplicate, the worker reads `waiting` until turn 2's own Stop;
  in that window tool events are not counted and an unconfirmed Escape hold clears. A confirmed pause and its dependency hold
  stay. Direction if ever seen: carry the reply's message id in the Stop input, not a hook rule.
- The trust-store guard again refused an edit whose text only named the trust store (the F5 tooling follow-up above); this
  worker asked first and used Edit on Main's grant.
