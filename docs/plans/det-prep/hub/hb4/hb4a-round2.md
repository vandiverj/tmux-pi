# HB4a round 2: a request notice is delivered only when Main was shown it (Main, 2026-11-11)

Review review-coder-4c88637abd-034613 (REQUEST_CHANGES), report, `probes.py`, `notifier-probe.mjs` and logs:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-4c88637abd-034613/`.
The HB4a brief (`hb4a-brief.md` here) still applies. Bar: realistic use. Lane hub-correct. Route rule-first: F2-F5 are four sites of
one family (request-notice-loss); fix the rule, not the four sites.

## The rules
1. **Delivered means rendered.** A request transition (opened, resolved, superseded, closed) is marked delivered only when the text
   Main actually received names that request id and transition. Startup baselines, index tiers and generic rows that do not render
   the id never mark it. Within one notifier lifetime every transition not yet delivered is sent, in order, even when several
   happened between polls (A opened then superseded by B → Main hears both; opened and resolved between polls → Main hears the
   resolution with its id). A fresh session does not replay old resolved history: only open requests and transitions after the
   notifier started. Closes F2, F3.
2. **Open requests are never windowed.** Every open request on a task is visible to list, inspect, the index and the notifier,
   regardless of how many resolved records exist; bounding applies only to resolved history. Closes F4.
3. **A request notice does not depend on the task row's lifecycle.** An open capability/input request is announced even when the
   task row is settled or completed (a worker that files `tp-agent request` and ends with `RESULT_STATUS: blocked`). Closes F5.
4. **A pause never overrides a finished turn.** If, after the Escape, the worker's result shows a completed turn (a statused reply
   written after the pause began), the pause is not installed: record it (e.g. `pause.unneeded`, with why) and leave the task's
   lifecycle as the result says. Only an interrupted partial turn becomes paused. Closes F1.
5. **The compatibility file mirrors one request.** `capability-request.json` carries the newest capability request's own id, what and
   status; aggregate "anything outstanding" readers use `requests/`. Closes F6.
6. **Every suggested command runs.** Notice text says `tp-agent message <id> --resolves <req id> --text "..."`; a test parses each
   suggested command with the real argparse. Closes F7.

## Decisions (Main)
- F9: accepted as the Claude interrupt evidence rule for HB4a: an unchanged tool/working state plus a prompt-looking pane without
  `esc to interrupt` counts as confirmed, because Claude emits no Stop on interrupt. Document it in the report's known limits and in
  code comments at the check; HB4b may replace it with a supported observation.
- F8: HB4b (correlating a pi worker's left-state closure with a `--resolves` delivery). Not this round.
- F10 (check): correct the process-count claim for `tests/test_pane_send.py` (count or estimate honestly).

## Report
Fail-before against 15c6622 for each review probe (`probes.py` rows and the real-TypeScript `notifier-probe.mjs` cases), and a test
per rule using the real writers and the real TypeScript notifier functions (not a Python port) for the notice cases. Rebuild the
attack table so its expectations come from the rules above, not from the current code: two distinct requests between polls → two
opening notices plus the supersession; opened+resolved between polls → the resolution; startup with N open → each id rendered or
still pending; >20 resolved + 1 open → the open one visible; settled/completed task with open request → announced; pause with a
finished turn → not paused. Writer/reader sweep updated. The four cells as before (py3.9/3.14 × inside/outside tmux) on the final
tree, each log starting with `date -u`, `TMUX_TMPDIR` (short /tmp path) and `git add -A && git write-tree`, then `git reset -q`.
Private tmux socket only. No live Claude/pi; nothing to 127.0.0.1:1234 or any network. Search with safe_grep, read with
Read/safe_read; never read claude/mcp.json, .env* or auth files. Commit nothing. No background shells. `RESULT_STATUS: complete`
as the last line.
