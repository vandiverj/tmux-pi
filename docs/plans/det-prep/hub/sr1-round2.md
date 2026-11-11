# SR1 round 2: the stop rule refuses unless it has every finding (Main, 2026-11-11)

Round 1's review (review-coder-3a6125b4cb-145141, `result.md`, `report.md` and `repro.py` in
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-3a6125b4cb-145141/`)
is REQUEST_CHANGES with F1 (P1, family incomplete-stop-findings): `code_findings` takes lib/family_sweep.py's output as the
complete list of findings. That parser quotes findings; it does not prove it saw all of them (empty on a malformed fence, skips a
first-line reason, stops at the first Verification/Coverage heading). A new family after a Verification section closed the step.
**Main's diagnosis: the brief's fault**: it said "use the existing parser", which made the stop rule only as complete as a
quoting helper. The SR1 brief (docs/plans/det-prep/hub/sr1-stop-rule.md) still applies except where a rule below replaces it.

## The rules
1. **Fail closed.** The stop rule acts only on a review whose findings it can account for completely; otherwise authorize-stop and
   close --stop-rule refuse, naming the review and what could not be accounted for, and the owner uses `--override-review`.
2. **Completeness is proved, not assumed.** Count the finding headings in the whole review text independently of the sweep
   parser: every line that starts a finding the way reviews in this session write them (a Markdown heading or bold lead whose
   text starts `F<number>`, e.g. `### F1 — ...`, `**F1 — ...**`; check the real `result.md` files under
   `.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-*/` for the forms in use), anywhere in the file, including
   after Verification/Coverage and inside or after a broken fence. Every counted finding must be classified: its own
   `Family:` (if it closes by code) and `Closes by:` lines within its section. Any counted finding the classification misses, a
   rejecting review with zero counted findings (a reason-only `CHANGES` or `REQUEST_CHANGES: <reason>`), an unclosed fence, or a
   code finding with no family refuses. Reuse family_sweep for the per-finding fields where it is right; the count is what makes
   the list complete.
3. **Reviewer ids, not ordinals (review F2).** Finding ids are the reviewer's own `F<number>` from the heading. Two findings
   with the same id refuse. The known-limits file names those ids.
4. **The same extraction everywhere.** `stop_plan` (the families the owner sees and authorizes) and every check in
   `stop_rule_checks` use the one complete extraction, so the owner never authorizes from a partial list.
5. **Keep** round 1's stricter choices (every rejecting review of the closed tree; any close drops the step's authorization)
   and say in the refusal for (e) which review a missing id belongs to (review F3).
6. **Docs (review F3).** docs/lanes.md: add the stop-rule exception where it describes `--override-review` (around lines 106
   and 153), in the file's existing style: what the owner runs, when (at the decision, never after the last review), what Main
   runs, and that anything incomplete falls back to `--override-review`.

## Tests (tests earn their place)
Extend round 1's attack table in `StopRuleTests` (same fixture; no new file, no new dispatch per row). Rows: new-family finding
after a Verification section (the review's repro); after Coverage; unclosed fence before a finding; reason-only CHANGES and
`REQUEST_CHANGES: reason` (zero findings); a counted finding with no `Closes by:`; duplicate id; reviewer ids not 1..n (`F3`,
`F7`) closing with a limits file naming them; plus review F4's two branches: limits file committed only at the lane HEAD, and
the authorization changed while the owner confirms. Run the authorize side for the extraction rows that apply. Fail-before on
2064295 for the F1 rows (they close today).

## Report
Changed files; the table `Case | Expected | Got`; tests added / extended by name; the heading forms found in this session's
real reviews (count of result.md files read, forms seen, any that the count would miss); fail-before output.

## Verification (the worker tests once)
Run tests.test_lane_steps, tests.test_lane_records and tests.test_session_env once through `tools/agent/test-run.sh`, with
`/usr/bin/python3` outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`), a
short fresh `TMUX_TMPDIR` under /tmp, the log starting with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`, then
`git reset -q`. Keep the log by glob as in round 1. Main runs the cells and the discovery. No Claude or pi start; nothing to
127.0.0.1:1234 or any network; private tmux `-L` sockets only. Never run the commands against this repository's real lanes.
Do not edit config/, interfaces.md or release-gates.md. You may edit bin/tp-agent with the Edit tool if needed. Search with
safe_grep, read with Read/safe_read; never read claude/mcp.json, .env*, auth files or config/local.json. If a guard hook
refuses a command, stop and report it; do not work around it. Do not approve any permission or placeholder prompt; stop and
report it. Commit nothing. No background shells. `RESULT_STATUS: complete` as the last line.
