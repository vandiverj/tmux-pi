# HB8a round 5: score only the shell shapes we can read; everything else is uncertain (Main, 2026-11-11)

Review review-coder-e916dee3e2-044600 (REQUEST_CHANGES), report, `probes.py`, `probes.log`, `sweep-rows.json`:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-e916dee3e2-044600/`.
The HB8a brief and rounds 2-4 still apply. Bar: realistic use. Route rule-first; keep the change local to tools/episodes and its tests.

## Why the rule changes shape again
Rounds 2-4 each widened what the scorer reads out of shell text (cd, subshells, `sh -c`, `||`, wrappers, compound records), and each
review found new sites in the same families (command-evidence-identity, verification-tree-identity, completion-evidence). This round
stops chasing shell grammar. The scorer reads a small, stated grammar exactly and treats everything outside it as uncertain. An
uncertain execution never counts as a verification or a write-free interval, and it makes any measure or check whose answer depends
on it `unknown` with the reason. Ordinary worker commands (`npm test`, `cd packages/a && npm test`, `npm run build; npm test`,
`npm test 2>&1 | tail -20`, `tools/agent/test-run.sh . -- npm test`) stay inside the grammar.

## The rules
1. **A stated grammar.** A Bash call is read only if, after comment stripping, it is a list of simple commands joined by newline,
   `;` or `&&`, where each command is optional env assignments, a word list, optional redirects, and optionally a pipeline whose
   first stage is the command. `cd DIR` as its own command changes the cwd for the following commands of that call only when it ran.
   Anything else in the call (`||`, `if`/`case`/loops, `( … )`, `{ … }`, `sh -c`/`bash -c`/`eval`, `$( … )` containing a runner
   or a write, background `&`) makes the whole call uncertain. Write the grammar in score.py's header and in the report.
   Closes F5 (all three paths) by replacing the per-shape handling.
2. **One ordered point per stage.** Every stage of a read call, and the write and check inside it, gets its own order point in the
   stream (writes before the check when one stage does both). Closes F3.
3. **A structured record speaks for its own `cmd` only.** A test-run record's exit backs a verification only when its `cmd` is a
   single runner command inside the grammar (no shell wrapper). A record whose `cmd` is a shell (`sh -c …`) or a compound list is an
   uncertain execution: its exit is the shell's, not any stage's. Its cwd is the record's cwd; inner `cd` never moves it. Closes F2
   and F5.1.
4. **A result exists from its own time.** An unstructured check's result is available at the tool result's time (`result_at`),
   never at the call start; an undated or missing result backs nothing. Closes F4.
5. **A write happened only if the tool says so.** Edit/Write with an error or blocked result is no write; with no result it is an
   uncertain write (the tree after it is unknown). Closes F9.
6. **No silent fallback for the scoring specification.** A missing or empty `run/episode.json` makes the run `incomplete-records`,
   unless the original episode file is read and its hash equals the hash the run recorded (then say so in the run's reasons). Closes F6.
7. **Every object the run says exists is read.** Request ids come from the union of `requests/` files, `request.opened` events and
   any notice/index that names an id; an id without a readable file is missing, and `requests_distinct` and other request measures go
   unknown. The test-run wrapper count comes from executed stages under rule 1, never from raw text (comments and quoted arguments
   are not calls). Closes F7, F8.
8. **The dead-pane test kills only its own worker.** Kill the process that belongs to the private pane (its `pane_pid` from the
   private socket, or a pid file the fake writes in its own run dir), never `pkill -f` by pattern. Keep the bounded pane-death wait.
   Run the test 8 times in parallel and 4 cells in parallel; report both counts. Closes F1.
- **F10:** diagnostic note only; list it under known limits.
- **F11 (check):** correct the report's closure claims as the review lists. Also: Main's round-4 evidence said 6 of 8 parallel copies
  failed at `coder-done` under load; those copies shared the host-wide `pkill`, so that count is evidence of F1, not of load timing.

## Report
Fail-before against 4d69181 for every probe in the review's `probes.py`, and a test per rule. A grammar table: shape → read or
uncertain, with one test per row, including every shape the review used. Keep the round-4 differential oracle (its fixtures plus the
review's probe runs) and add an oracle case per rule: no measure goes from one known value to a different known value; report counts
and every mismatch. Rerun all 14 episodes fake and compare with round 4 (a measure that is now unknown because of the grammar must
be listed with the shape that caused it; the good-Main fake runs should stay known). The four cells as before, each log starting
with `date -u`, `TMUX_TMPDIR` (short /tmp path) and `git add -A && git write-tree`, then `git reset -q`. No `--live` runs; no Claude
or pi start; private tmux socket only; nothing to 127.0.0.1:1234 or any network. Search with safe_grep, read with Read/safe_read;
never read claude/mcp.json, .env* or auth files. Commit nothing. No background shells. `RESULT_STATUS: complete` as the last line.
