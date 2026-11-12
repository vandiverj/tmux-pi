# HB6c: a status view, per open lane: where it waits and the next valid action (lane hub-status, slice 1)

Owner (2026-11-10): "My priorities are understanding where work waits and avoiding repeated verification." HUB target
(`hub/owner-request.md`): "leave and return, and receive current completion evidence without repeatedly reminding Main how to
operate". HB6's scope (`hub/README.md`, HB6) names "a status view: objective, progress, blocker, next valid action, required
decision". HB6a/HB6b1 built the per-task timeline (`tools/waits/timeline.py`), and a live measure shows 0% unknown. Nothing joins
the lanes, their tasks and their reviews into one answer to "what is waiting, on whom, and what happens next". This slice does
that, read-only. Bar: realistic use (`docs/plans/realistic-use-bar.md`). Kind: mechanical against the table below (Sonnet).

## What to build
`python3 tools/waits/status.py SESSION_TASKS_DIR [--repo R] [--json] [--now ISO]`. It is read-only: it opens files for reading
only and writes nothing. It prints no transcript text and no file content beyond ids, names, states, verdict words and times.
A missing or malformed record makes that row's field `unknown` and is listed under `problems`; it never raises.

One row per open lane of the repo (the lane records `tp-agent lanes` reads; find its reader in lib/ and import it, never parse
lane.json a second way). Each row holds:
- `lane`;
- `objective` (the lane's domain);
- `step` (open step);
- `age`;
- `task` (the newest task of that lane and step, by the task records' lane/step fields);
- `state` (that task's current state and since when; use timeline.py's `build_task`/`build` for it, never a copy of its rules);
- `review` (the newest review of that task, and its verdict word, or none);
- `requests` (that task's open requests with ids and kinds, from `lib/hub_records.open_requests`);
- `next` (the next valid action, from the table below);
- `who` (main, owner, worker or reviewer).

Then one line per row in text mode, sorted by `who` (owner first, then main, then worker and reviewer), then by age.

## The next-action table (the rule; report.md restates it with file:line, then code to it; first match wins)
| # | Condition | next | who |
|---|---|---|---|
| 1 | an open request whose kind is permission | answer the permission prompt at the pane (Main never approves) | owner |
| 2 | another open request (capability, input, question) | `tp-agent message TASK --resolves REQ --text ...` | main |
| 3 | no task for the open step | dispatch the step, or remove the lane | main |
| 4 | the task is running (any non-final state) | wait | worker |
| 5 | a review of the task is running | wait | reviewer |
| 6 | final result, no review | Main's checks, then `tp-agent review TASK` | main |
| 7 | the newest review says REQUEST_CHANGES | diagnose, then `tp-agent retry TASK --reason BRIEF` | main |
| 8 | the newest review approves, and the step is not closed | `tp-agent lane close LANE STEP` | main |
| 9 | the step is closed, and the lane branch is not merged into main | merge, then `tp-agent lane remove LANE` | main |
| 10 | anything else | unknown, with the reason | unknown |

Say in report.md how each condition is read (which record and field), and what "final" and "running" mean in the task state
records (list the states). If a condition cannot be read reliably from records, say so. Give that row `unknown`; never guess.

## Attack table (one table in report.md; extend existing fixtures and do not add one test per row)
Cover every table row, plus:
- two tasks in one step (the newest wins);
- a retry chain (the retry is the newest);
- a review of an older task in the chain;
- a lane with a malformed lane record;
- a task dir with no meta.json;
- a request record that is malformed;
- a lane of another repo (left out).

For each row, give the expected `next` and `who`, and the test row that proves it. Put the tests in the timeline module's test
file if one exists (find it with safe_grep), as one table-driven method over fixture dirs built in a temp dir. Show no
fail-before: the tool is new.

## Case 2 (read-only, real data)
Run it on this Mac's real session tasks dir and repo. Say what it shows in counts and `next`/`who` words only. For example, lane
tq1-review is open with step Q4c and no recent task. Quote no log or transcript text.

## Verification
Run the new or extended test module and `tests.test_session_env` once each through `tools/agent/test-run.sh`:
- use `/usr/bin/python3` outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`);
- use a fresh short `TMUX_TMPDIR` under /tmp;
- run in the foreground.

Touch only tools/waits/status.py (new), its tests and fixtures, and tools/waits/timeline.py, but there only to expose a
function the view needs, with no rule change. Do not edit bin/, config/ or claude/. Never write to the session dir or the
work store.

Do not use the network. Send nothing to 127.0.0.1:1234. Never touch the default tmux server. Approve no prompt. If you need
something outside these paths, use `tp-agent request` and stop. Commit nothing. No git notes. No background shells.
`RESULT_STATUS: complete` as the last line.
