# RR1: a gated retry starts a fresh findings window in the review packet (Main, 2026-11-11)

**Owner decision, 2026-11-11:** "Fix tp-review rounds". HB8a round 8 (a design change the owner chose, "Records only") passed
four cells but `tp-review` refused its packet: ~432k chars, limit 400k. Parts: the diff since round 7 (239k), the earlier
findings of all 7 rounds (50k), 8 briefs (40k), the author's packet (32k). Rounds 1-7 were about reading Bash, a design round 8
replaced. HB4a and HB3a are on the same path. **The 400k limit stays; nothing here raises or bypasses it.**

## The rule
1. When a retry was made with `tp-agent retry --route rule-first` or `--route design` (a gated retry: the brief changed the rule
   or the design), the reviews **before** that retry are an older window. The review packet attaches in full only the findings of
   the reviews from the last gated retry on (the review that triggered the gate is the first of them, so the reviewer still sees
   what the design answers). It names every older review on one line each: round, review id, verdict, the family names and the
   finding headings (`F1 — ...`), no bodies. A reviewer can still name a family that recurs from an older round.
2. Where the gate is recorded: find it (bin/tp-agent retry, around lines 1540-1600: `sweep.md` and the Notes; `parked.md` for
   park). If no durable per-task record says "this retry was gated with route X", add one to the new task's `meta.json` (for
   example `"gate": "design"`), written by retry, read by `lib/review_rounds.py`. A task without the field behaves as today.
3. Nothing else in the packet changes: the diff since the last reviewed state, briefs, author packet and result, Main's evidence
   and the 400k check stay as they are. A packet that is still too big is refused as today.
4. The review task text says which rounds are summarised and why (one sentence), so the reviewer knows the older bodies were
   left out on purpose and where they are (their result.md paths).

## Tests (tests earn their place)
Extend the existing review-rounds tests (find them with safe_grep: `lib/review_rounds.py` `prepare`, `chain_labels`; e.g.
tests/test_family_sweep.py, tests/test_review_coverage.py, tests/fixtures/task_family/). Cases: a chain with no gate (unchanged
output, byte for byte against today); a chain with one design gate (older rounds summarised, later ones in full); two gates (the
last one wins); a summary line keeps family names and headings. Fail-before on main for the gate cases. Then measure: run
`python3 lib/review_rounds.py prepare` (read-only) for coder-c24113fdaa (HB8a, lane hub-episodes) and report the before/after
size of its earlier findings. Do not start a review.

## Verification (the worker tests once)
Run the affected modules plus tests.test_tp_review and tests.test_session_env once, through `tools/agent/test-run.sh`, with
`/usr/bin/python3` outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`), short
fresh `TMUX_TMPDIR` under /tmp, the log starting with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`, then
`git reset -q`. Main runs the four cells and the discovery. No Claude or pi start; nothing to 127.0.0.1:1234 or any network;
private tmux `-L` sockets only. Do not edit config/. Search with safe_grep, read with Read/safe_read; never read claude/mcp.json,
.env*, auth files or config/local.json. Commit nothing. No background shells. `RESULT_STATUS: complete` as the last line.
