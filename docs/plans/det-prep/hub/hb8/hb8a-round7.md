# HB8a round 7: a closed classifier, one uncertainty gate, argv identity (Main, 2026-11-11)

Review review-coder-2b268e9a45-070851 (REQUEST_CHANGES), `report.md`, `probes.py`, `probes.log`, `sweep-rows.json`:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-2b268e9a45-070851/`.
The HB8a brief and rounds 2-6 still apply. Bar: realistic use. Keep the change local to tools/episodes and its tests. Route:
**design** (the family command-evidence-identity has come back in rounds 2-6).

## Main's diagnosis of round 6 (owner rule: every REQUEST_CHANGES is diagnosed)
The rules are right; the code shape keeps breaking them. Rounds 5-6 said "read a stated grammar; every token is modelled or the
call is uncertain; uncertainty dominates". The code still works by stripping what it recognises (`_unwrap`/`_unwrap_chain` drop
`command`, `-v`, `env -S`; an assignment-only stage is dropped; `$(( ))` is not looked into; `check_identity` joins words with
spaces) and by each measure checking uncertainty on its own (F6: two consumers missed it). Each strip is a place where an
unmodelled token becomes a known fact. F5 (`eval`) and F8 (ids in other text) are misses of round-5 rule 1 and round-6 rule 5.
So this round changes the shape of the code, not the rules: recognise, never strip; one gate for uncertainty.

## The rules
1. **Recognise, never strip (F1, F2, F5, F7).** Replace `_unwrap`/`_unwrap_chain` and the best-effort paths with a classifier over
   the token list. A stage is read only when every word is matched by a table entry: the runner/writer programs the scorer knows,
   and each wrapper (`env`, `timeout`, `nice`, `time`, `command`, `tools/agent/test-run.sh`) with the exact options it accepts
   (e.g. `env` only `NAME=value` words and `-i`/`-u NAME`; `command` with no option; `timeout` a duration and `-s SIG`/`-k D`).
   Any other option (`command -v`, `env -S`, ...) makes the call uncertain. An assignment-only stage, `$?`, `$(( ))`, `eval`,
   `source`/`.` make the call uncertain, and after an uncertain call in the same shell the cwd is unknown. One table, in score.py's
   header and in the report; the table *is* the grammar.
2. **Identity is the argv (F3).** A check's identity is the word list as a JSON array plus the cwd, never a space-joined string. A
   word holding an unresolved expansion (`$VAR`, `${...}`, a glob) makes the identity unknown: such a run cannot clear or repeat
   another run.
3. **Writes at the cwd where they happen (F4).** A redirect is a write whatever the command (`cd . > a.txt` included). A writer run
   inside `test-run.sh WT -- ...` writes at WT resolved against the stage cwd; an outer redirect writes at the outer cwd.
4. **One uncertainty gate (F6).** Every interval answer (absence, count, staleness, `final_tree_verified`, message counts and
   absences) is computed through one function that returns `unknown` with the reason when the interval holds an uncertain call,
   an uncertain write, or an uncertain message call. No measure reads interval facts any other way; a test fails if a measure in
   `score.py` reads the event list without the gate (a static check over the module is fine).
5. **Request ids from every text (F8).** Ids are taken from every transcript text of every role (user, assistant, tool result) and
   every record, with the producer's id pattern (round 6 rule 5).
- **F9:** known limit, as stated. **F10:** Main's (full discovery and the raw private-name search are in the next evidence).

## Stop rule for this lane (owner rule: round numbers mean a fault, a misread, or an approach that is not usable)
If the round-7 review finds a new *realistic* site in command-evidence-identity, Main does not write round 8 of the same kind. It
takes the narrower design to the owner instead: free-form Bash never backs a verification or a write-free interval; only
`tools/agent/test-run.sh` records (argv, cwd, tree, exit, written by the wrapper itself) do, and every Bash-derived answer is
`unknown`. Say in the report what that design would make unknown in the 14 fake episodes, so the owner can weigh it.

## Tests (tests earn their place)
Extend `GRAMMAR_ROWS` with a row per probe in the review's `probes.py` (fail-before against fb646f6); replace the strip-path tests
that the classifier makes meaningless and say which. Add one attack generator test: wrapper chains and options drawn from a fixed
list (including options not in the table) combined with each runner; every combination is either read with the argv the table
gives or uncertain, never a known execution of a different argv. Keep the oracle; report match/mismatch counts. Rerun the 14 fake
episodes, compare with round 6 (each new unknown listed with its cause; the good-Main runs stay known). Report added vs extended vs
replaced.

## Verification (the worker tests once)
Run `tests.test_episode_harness` and `tests.test_session_env` once with `/usr/bin/python3` outside tmux
(`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`, fresh short `TMUX_TMPDIR`), the log starting with
`date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`, then `git reset -q`. Main runs the four cells and the full discovery.
No `--live` runs; no Claude or pi start; private tmux socket only; nothing to 127.0.0.1:1234 or any network. Search with safe_grep,
read with Read/safe_read; never read claude/mcp.json, .env* or auth files. Do not overwrite another task's evidence. Commit
nothing. No background shells. `RESULT_STATUS: complete` as the last line.
