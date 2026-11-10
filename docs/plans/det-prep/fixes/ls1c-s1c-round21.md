# LS1c S1c round 21: close under the realistic-use bar (Main, 2026-11-10)

The owner set a new bar for this lane: `docs/plans/realistic-use-bar.md` (read it first). The summarizer runs only on the owner's own
repos and work code. A finding blocks only when it can happen in code written and run normally; contrived inputs become known limits.

Review review-coder-df3aae6de1-072402 (REQUEST_CHANGES) and its report are in
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-df3aae6de1-072402/`.
Keep everything the review confirmed from round 20 (rules 1 and 2, guard 9/3/0, probe 0, zero inventory change, full-file and
use-lrucache lines, 232 tests in four cells).

## Main's decisions
- **F1 (`import hp.x; y = hp.x.v.w` where `v = 1`) is contrived:** the false credit exists only because the program always raises
  `AttributeError` on the int before the credited line. When the attribute exists the program runs and the credit is right. Do not
  change the resolver for it; do not make member chains opaque (real code reads `pkg.mod.CONST.attr`). It becomes a known limit.
- The round-20 open classes (programs that fail on a non-module value or an unbound name before the line; 60 + 1,536 + 80 rows) and
  the rule-1 cost (two imports of different modules under one name are unknown) are known limits too.
- No new attack campaigns this round. No code change to `lib/summarize_structured.py` unless a realistic defect turns up while you
  write the limits; if one does, stop and report it instead of fixing it.

## Do
1. Write `docs/plans/det-prep/ls1c-known-limits.md`: one line per known limit, with the input shape, what the scan does (false credit,
   opaque file, lost line) and the round that found it. Cover F1 above, the round-20 open classes, the rule-1 cost, round 19's F2
   (`loaded_before` counts top-level imports only), round 20's F3 note (conservative class-body and `rebound` checks) and any other
   limit an earlier round accepted as "Closes by: Main decision". Open the file with a link to the bar.
2. F2 report corrections, from existing logs and the tree: sweep row 24 (`_members` is consumed by `use_scan` at its current line);
   repro20's 26 runtime errors per interpreter; the reverse-order controls lose credit by design (say so); 15 read forms, not 16;
   the `_STATE_ATTRS` examples; and say plainly which historical inventories cannot be reconstructed.
3. F3: qualify the attack evidence. Bindings triples cover import kinds only; the reads generator stops at the member. State both.
4. Rerun the seven modules (tests.test_qual_summarize_code_facts, tests.test_qual_summarize, tests.test_qual_summarize_oracle,
   tests.test_qual_summarize_structured, tests.test_qual_summarize_structured_v2, tests.test_session_env,
   tests.test_qual_summarize_meaning) in all four cells, one after another. Each log starts with `date -u`, `TMUX_TMPDIR` and
   `git add -A && git write-tree`; record each file's time per cell; run `git reset -q` afterwards.

## Report
The limits file, the corrected F2/F3 statements, the matrix. Commit nothing. Make `RESULT_STATUS: complete` the last line, exactly.
Leave no background shells running. Never send a request to 127.0.0.1:1234 or any other address. Use a private `TMUX_TMPDIR` for
anything that touches tmux.
