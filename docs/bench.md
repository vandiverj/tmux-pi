# tp-bench

`bin/tp-bench` scores one tmux-pi checkout (an "arm") on a fixed set of tasks over the bench project (battle-soccer at tag
`bench-base-v1`). The scorer (`bin/tp-bench`, `lib/bench/`, `bench/`) is fixed: the improvement loop never edits it.

## One-time setup (the owner, in a terminal)
1. `tp-bench init` creates the tag `bench-base-v1` in the project (`~/Documents/Projects/battle-soccer`, or `--project` /
   `TP_BENCH_PROJECT`): commit `dfea0c8` plus a committed `.tmux-pi/agent-tools.json`. It uses git plumbing on a temporary
   index and never edits the project's working tree or index. A tag that exists but is not that commit is left alone.
2. `tp-bench seal` copies `bench/answers-src/` (git-ignored) to `~/.local/share/tp-bench/answers/` and writes each file's
   sha256 into its task spec. `tp-bench verify` re-checks them.
3. **Claude folder trust.** Project copies are made under the work root (`~/.local/share/tp-bench/work`, or `--work-root` /
   `TP_BENCH_WORK`). First create it: `mkdir -p ~/.local/share/tp-bench/work`, because the bootstrap refuses a missing directory.
   Then run `bin/tp-claude-bootstrap <work root>` once **from a real terminal** (with no TTY Claude falls into `--print` and
   exits without showing the prompt), approve the trust prompt, type `/exit`. That is tmux-pi's
   own supported way to settle trust (`tp-claude` keeps it in the profile under `~/.config/tmux-pi/claude`); the bench never
   edits that profile and never types into a dialog. If a worker still stops at the prompt, the runner sees it in the pane,
   stops the worker, records the task as `error` with `detail.error = "trust-dialog"` and goes on. Local tasks use the Pi
   worker and do not need trust.

## Run
```
tp-bench [--project DIR] [--results-dir DIR] [--work-root DIR] run --arm DIR --label NAME [--tasks GLOB] [--repeat N]
         [--budget-minutes M] [--max-worker-runs N] [--env KEY=VALUE]... [--order random|fixed] [--seed S]
```
Run it from a plain terminal, not from a tmux-pi worker pane, and not as an agent. Each repeat makes a fresh git worktree
of the project under the work root, starts a detached workspace from DIR's `bin/tp` in its own tmux session
`bench-<label>-<n>-<random>`, runs every task through DIR's `tp-agent`, scores, stops the workspace and removes the copy
(also after a timeout or an exception). The record is `bench-results/<label>/<n>.json` (`tp-bench-run:v1`). A run claims its number `<n>` by creating
`<label>/<n>/` (artifacts go under it); `<n>` is one more than the highest of `<n>.json`, `<n>.cleanup.json` and `<n>/`, so
a retry never reuses a failed run's number, and no file in a results dir is ever replaced. A value the bench could not read
(a failed `git status`, an unreachable LM Studio, a tool with no version) is `null`, never a value made from empty output.

Session safety: the runner refuses to start if its session name exists (or if tmux cannot say). One probe answers every
"is it there?": a session is **gone** only when `tmux list-sessions -F '#{session_name}'` exits 0 and the exact name is not
listed; a failed call, a timeout or an exception is **unknown**, never gone. It stops a session only if the probe finds it
present with the `@tp_state_dir` it recorded at start; a foreign owner, or a session already gone before the runner stopped
it, is recorded as `stop_refused` and nothing is stopped. A session proven gone after the runner's own `tp-exit` is recorded
`stop: stopped`. When the state is unknown the runner stops nothing and keeps the project copy and the work dir: the record
(or `<n>.cleanup.json` when the run raised) says `cleanup: incomplete`, with `stop_refused` giving the reason and `leftover`
the paths to clean by hand. Between tasks the copy is reset to `bench-base-v1` and verified (HEAD is the tag's commit; only
`node_modules` and `.tmux-pi` may be dirty); if that fails, the task and every later one in the repeat are `not_run` with
`detail.error = "reset-failed"` and still count in the denominators. No subprocess result is ignored: `Shell.run` raises
`ShellError` on a nonzero exit unless the caller passes `tolerate="<reason>"`. Every child process gets one
environment from `lib/bench/shell.py` `child_env`, which drops `TMUX`, `TMUX_PANE`, `TP_SESSION`, `TP_STATE_DIR`, `TP_TASK_*`,
`TP_MAIN_*`, `TP_TRIAL_ID`, `CLAUDE_CODE_SESSION_ID` and `TP_MODE` and adds back only what the runner sets (its own `TP_SESSION`
and `TP_STATE_DIR`). `--env` refuses those keys. `--env` values reach the workspace and are recorded; `TP_PI_NODE=` is passed
through.

## Add a task
Write `bench/tasks/<id>.json` (`schema tp-bench-task:v1`; the file name is the id): `kind` (`code`, `review`, `local`,
`orient`), `title`, `budget_s`, and by kind `brief` + `test_path` (code, under `src/`), `request` + `diff` (review; `clean: true`
for a clean diff), `question` (local), `brief` (orient), plus `answer.file` for all but orient. Put the answer in
`bench/answers-src/<answer.file>` (hidden vitest test, planted-defect key `{"defects": [{id, file, line_start, line_end,
keywords}]}`, or `{"answer": "..."}`), then `tp-bench seal`. Never put an answer in a spec or in the repo.

## Compare
`tp-bench compare A B` prints per-metric means and spreads of the two labels' repeats and a verdict by
`loop/PROTOCOL.md`: **keep** (correctness rose beyond A's spread, or correctness equal and tokens per accepted task down 10%
or orient waste down 25%), **discard** (correctness fell beyond the spread, a task passed in every A repeat and failed in
every B repeat, or false positives on the clean diff rose by more than 1), else **inconclusive**. Records with a different
task set are refused; so are records whose env differs in keys other than `TP_X1_ARM` / `TP_HL1` unless `--allow-env-diff`.

## What the score means
`code_pass` hidden tests all pass; `review_catch` planted defects named (file, line window, keyword); `review_fp` `Closes
by: code` findings on the clean diff; `local_exact` exact answers; `tokens_per_accepted` worker plus reviewer tokens per passing
task; `orient_waste` help calls, re-reads, base lookups and sleep polls; `wall_s`. A figure that is not recorded is `null`
with a note in `token_sources` (Main's tokens are not recorded until E2); it is never 0. A task over its budget, or not run
because `--budget-minutes` ran out, counts as failed.
