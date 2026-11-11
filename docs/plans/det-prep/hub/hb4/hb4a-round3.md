# HB4a round 3: notices from the transition log, not from windowed rows; pause after the result baseline (Main, 2026-11-11)

Review review-coder-7f81d72c91-070903 (REQUEST_CHANGES): `result.md`, `report.md`, `probes.py`, `probes.log` in
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-7f81d72c91-070903/`.
The HB4a brief and round 2 (`hb4a-round2.md`, this dir) still apply. Bar: realistic use. Lane base for fail-before: cb5353a. Kind
of round: **design** for request notices (the family `request-notice-loss` came back in a second round), mechanical for the rest.

## Main's diagnosis of round 2 (owner rule: every REQUEST_CHANGES is diagnosed)
- **Design fault, not a missed case.** Rounds 1 and 2 built notice delivery on reading request *rows* (`row_requests`, newest 20
  closed by id order) and on treating whatever is closed at the notifier's first poll as history. Each round fixed the cases
  found; F2 and F3 are two more of the same kind. A window over current state cannot report every transition. Rule 1 changes
  the source.
- F1: an ordering defect in `bin/tp-contract` (state read before the result baseline). Mechanical.
- F4: the notice suggests a command without checking the task can take it. Round-2 rule 6 stated it; the completed-task state was
  missed. Mechanical.
- F5 is a report correction; F6 is Main's (full discovery on the preview).

## The rules
1. **Notices come from the transition log (F2, F3).** Every request transition (opened, resolved, superseded, closed) is already
   written as an event (`lib/hub_records.py` `event()`; or add one per transition if any is missing). The notifier delivers
   transitions from that append-only log with a persisted cursor per task (a count or byte offset; your choice, stated), never
   from the windowed rows. Startup: the cursor is what the notifier recorded when it took its startup index, so a transition
   between the startup index and the first poll is delivered, once. A transition is marked delivered only when rendered
   (round-2 rule 1 unchanged). `row_requests` stays a display window; nothing that delivers notices reads it. If the closed window
   is kept for display, order it by close time so a just-closed old request shows.
2. **Pause after the baseline (F1).** `bin/tp-contract` takes the result baseline before it reads the worker's working state, so
   a completion that lands between the two reads is seen as a change and never becomes a pause. Test the interleaving with the
   review's probe.
3. **Suggested commands fit the task's state (F4).** A notice suggests `tp-agent message <id> --resolves <req> --text ...` only
   when the task's worker can take a message (the same check `bin/tp-agent` makes before sending). For a collected or finished
   task with an open request, the notice says the request is left open by a finished task and names the command that does work
   (e.g. retry, or closing the request), or names none. A test runs each suggested command against a task in each state.
4. **Report (F5).** File count 15; the compatibility-writer/inspect sweep corrected; the network statement says which test uses
   loopback HTTP (`test_lmstudio`, a local fake server) instead of "no network".

## Tests (tests earn their place)
Extend the existing attack table and notifier tests: one row per review probe (F1-F4), fail-before against cb5353a. Replace
row-window delivery tests that the new source makes meaningless rather than keeping both; say which. Report added vs extended vs
replaced.

## Verification (the worker tests once)
Run the touched and importing Python modules (the round-2 list, minus the nonexistent `tests.test_redact_hook`) plus
`tests.test_session_env` once, and the plugin tests once, with `/usr/bin/python3` outside tmux
(`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`), under `perl -e 'alarm 900; exec @ARGV'`, the log
starting with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`, then `git reset -q`. Tests that need tmux use a private
`-L` socket and TMUX_TMPDIR. Main runs the four cells and the full discovery. No Claude or pi start; nothing to 127.0.0.1:1234.
Search with safe_grep, read with Read/safe_read; never read claude/mcp.json, .env* or auth files. Commit nothing. No background
shells. `RESULT_STATUS: complete` as the last line.
