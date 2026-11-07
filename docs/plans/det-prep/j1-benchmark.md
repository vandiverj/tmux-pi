# J1: tp-bench, a fixed benchmark that scores one tmux-pi checkout against another

Lane `bench`, step j1 (j2 if the reviewer splits it). Level: `mid` coder. Touches: `bin/tp-bench` (new), `lib/bench/`
(new), `bench/` (new: task specs, never answers), `tests/test_bench.py` (new), `docs/bench.md` (new). Starts after E1 and E2
are merged (it reads their token figures). Hidden answers live OUTSIDE the repo (see 3).

## Why
Every step so far is proved to work as specified. Nothing shows that tmux-pi gets more tasks right, catches more defects
or spends fewer tokens. The improvement loop (`loop/PROTOCOL.md`) needs one command that turns a tmux-pi checkout into a
score, the same way every time, cheap enough to run many times: the autoresearch pattern (fixed budget, one score,
keep or discard, log every run).

## Change
1. `tp-bench run --arm DIR --label NAME [--tasks GLOB] [--repeat N] [--budget-minutes M]`: DIR is a tmux-pi checkout
   (a worktree). For each repeat: make a fresh copy of the bench project (git worktree of battle-soccer at tag
   `bench-base-v1`, created by this step from dfea0c8 plus a committed `.tmux-pi/agent-tools.json` from A1's template),
   start a detached workspace from DIR's `bin/tp` (`TP_NO_ATTACH=1`, its own `TP_SESSION=bench-<label>-<n>`), run every
   task through DIR's `tp-agent`, wait (no sleep polling in agents; the runner polls), score, stop the workspace
   (`tp-exit`), and remove the copy. Every task has a wall budget; a task over it scores as failed.
2. Task kinds, one JSON spec each in `bench/tasks/` (`schema tp-bench-task:v1`):
   - `code`: a brief for a coder at a FIXED level (`--level small` default, so both arms use the same model) in a lane.
     Scored by HIDDEN vitest tests copied in after the worker settles: pass/fail per test. Six tasks over `src/game`
     (pure functions, one UI component prop, one bug fix in existing code, one task that needs an existing helper found
     rather than rewritten, one that needs reading two files to get the rule right, one with a trap in an adjacent file).
   - `review`: a prepared diff with planted defects (2 to 3 each: off-by-one, missed caller, wrong text, test that
     cannot fail) and one clean diff. Dispatched through `tp-agent review` of a stub author task. Scored per planted defect
     by file plus line window plus keyword, matched deterministically against the findings; the clean diff scores false
     positives (`Closes by: code` findings on it).
   - `local`: locate_quote, count_enumerate and summarize_files questions with exact answers about battle-soccer and the
     tmux-pi docs, run `--local-only`. Scored exact-match (normalised whitespace) per answer.
   - `orient`: a coder task whose score is only its tool trace: calls to `--help`, re-reads of the same range, base-branch
     lookups, `sleep` polls (from E1/E2's routines on that task's transcript).
3. Answers (hidden tests, planted-defect keys, exact answers) live in `~/.local/share/tp-bench/answers/` with a sha256 of
   each recorded in the task spec, so no worker can grep them from the repo and a changed answer is detected. `tp-bench
   seal` writes them from `bench/answers-src/` (never committed; `.gitignore`d) and prints the hashes.
4. Score record per run `bench-results/<label>/<n>.json` (`schema tp-bench-run:v1`): per task pass/fail or catch counts,
   tokens (worker, reviewer, Main where known, from E2), tool counts, wall seconds, arm commit, task-set hash, models used.
   `tp-bench compare A B` prints per-metric means, the spread of each arm's repeats, and a verdict line by the rule in
   `loop/PROTOCOL.md` ("keep", "discard" or "inconclusive").
5. Cost guard: `--max-worker-runs N` (default 40) refuses before starting when tasks x repeats would exceed it.

## Done means
1. `tp-bench run --arm <main checkout> --label control --repeat 1 --tasks 'local-*'` completes on this machine and writes a valid run record (local tasks only: no frontier spend).
2. A fixture run record pair gives the expected `compare` table and verdict for keep, discard and inconclusive cases.
3. Answer files outside the repo are checked by hash; a changed answer refuses with the task id.
4. Planted-defect matching: a fixture review text with 2 of 3 defects named scores 2/3, and a finding on the wrong line window does not count.
5. The workspace and project copy are removed after a run, also after a task times out (tmux and git stubbed in tests; the live run in 1 shows `tmux ls` and `git worktree list` back to before).
6. `docs/bench.md` says how to add a task, seal answers, run, compare, and what the score means.
7. Tests for 2 to 5 fail without the change.

## First real use (Main, after merge)
Control run of `main` and `lane/integrate` with `--repeat 2` on all tasks. Record both in `loop/results.tsv`. This is the
first answer to "did Phases 1 to 3 help", and the control spread becomes the noise floor for the loop.
