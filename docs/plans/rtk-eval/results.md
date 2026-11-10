# RTK evaluation: results

Run 2026-11-08 against `plan.md` (same directory). RTK v1.1.0, full edition, `~/.local/rtk-eval/bin/rtk`, telemetry off.
Raw data is in `data/` and the scripts are in `scripts/`. Every number below comes from those files.

## Verdict

**Don't adopt, on either harness, in full or in part.** All four decision-rule items fail.

| # | rule | verdict | why, in one line |
|---|---|---|---|
| 1 | Fidelity | **FAIL** | 19 of 98 replayed commands lost information: grep, git diff/show, git log, ls/find, git branch, wc. The lossless families save about 0%. |
| 2 | Guards unchanged | **FAIL** | Guard decisions on rewritten forms hold, but (a) redaction misses secrets that RTK cuts mid-line (11–19 chars of a dummy key id leak, live on Pi); (b) RTK's `recall.db` keeps full unredacted output for 30 days; (c) agent-typed `rtk run` and `rtk bypass` get past the kill guard; (d) on Claude, RTK's hook either is silently overridden (`TP_REDACTION=on`) or gets every rewritten command refused (shadow mode, worker without `--allowedTools`). |
| 3 | Accuracy non-inferior | **FAIL** | Pi T4 correctness fell from 5/5 to 1/5 (git log subjects truncated). Claude kept 12/12 but needed 1–2 extra turns on T4 to get around the truncation, which doubled that task's input. |
| 4 | ≥10% billed-input reduction | **FAIL** | Median per-task change in billed input, RTK on vs off: Claude **+2.2%**, Pi **+14.6%** (both are increases). The Phase 0 ceiling is about 13% of Claude input. With the Phase 1 byte reduction of about 16%, the best case is roughly 2%. |

Partial adoption does not rescue it. Excluding every lossy family through `[hooks] exclude_commands` (grep/rg, git diff/show/log/branch, ls, find, wc) leaves git status, `cat`→`rtk read` and test runners. Those measured 1.0%, −0.2% and 0% byte reduction, so nothing is left to save. Pi-only does not help either: Pi showed the accuracy drop and the live redaction leak.

## Phase 0: ceiling (`scripts/phase0_ceiling.py`, `data/phase0.{json,md}`)

Each Bash result is weighted by the API calls that re-read it, up to the end of its context segment (compaction).
Tokens are estimated as chars/R, with R calibrated per harness from context growth after results larger than 20k chars.

| harness | sessions | API calls | billed input | R (chars/token) | Bash output share | share RTK would rewrite at all |
|---|---:|---:|---:|---:|---:|---:|
| Claude (tmux-pi config dir) | 622 | 19,963 | 2,066.8M | 2.51 | 25.3% | **13.0%** |
| Pi (task transcripts) | 48 | 627 | 29.8M | 3.70 | 24.9% | **18.4%** |

Families by share of billed input (share RTK would rewrite in brackets):

- Claude: cat/sed/head/tail 9.58% (4.78%), grep/rg 4.17% (2.78%), tmux-pi tools 2.65% (1.13%), other 2.47% (0.65%), python/node 1.65% (0.05%), ls/find 1.14% (1.03%), git diff/show 1.07% (0.91%), wc 0.61% (0.58%), git status 0.48% (0.45%), git log 0.17% (0.16%), everything else under 0.7% each.
- Pi: git diff/show 13.00% (12.98%), other 5.12% (0.85%), grep/rg 3.37% (2.94%), ls/find 1.50% (0.39%), git status 0.95% (0.95%), cat/sed/head/tail 0.47% (0.07%), tests 0.31% (0.23%).
- Non-Bash tool results, which RTK's PreToolUse hook never touches: Claude Read 16.9%, Grep tool 1.4%; Pi read 38.8%, safe_read 3.0%, safe_grep 1.4%.

Ceiling arithmetic: 13.0% rewritable × 16.4% mean byte reduction (Phase 1) ≈ 2.1% of Claude billed input. For Pi, where git diff/show (28% reduction) dominates, it is 18.4% × about 25% ≈ 4–5%. Both are far below the 10% bar, even before any loss.

## Phase 1: replay fidelity (`scripts/phase1_replay.py`, `data/phase1.json`)

- Sample: 98 command segments, drawn by family (seed 20261108) from the transcripts.
  - Pool sizes: grep 1636, cat 1750, ls/find 389, git diff 189, tests 167, wc 119, git other 81, git log 46, git status 34.
- Each segment ran raw and as `rtk rewrite` produced it, in this worktree, and was compared by a family-specific deterministic checker.

| family | lossless | byte reduction | recall hint shown |
|---|---:|---:|---:|
| cat/sed/head/tail | 17/19 | −0.2% | 0 |
| git diff/show | 17/20 | 28.0% | 0 |
| git log | 6/8 | −3.9% | 0 |
| git other (branch, ls-files) | 2/4 | 19.7% | 0 |
| git status | 6/6 | 1.0% | 0 |
| grep/rg | 17/20 | 28.3% | 2 |
| ls/find | 6/12 | 38.8% | 4 |
| tests (unittest) | 3/3 | 0% (not rewritten) | 0 |
| wc | 5/6 | 7.3% | 0 |
| **all** | **79/98** | **16.4%** | 6 |

Exit codes were preserved in all 98 cases. Commands RTK does not rewrite at all: `sed -n`, `git grep`, `python3 -m unittest`, `tp-*`, `nl`, `cat a | head`, and most pipelines ending in `wc`/`sort`.

### Every loss, with an example

- **git diff/show:** large hunks are cut, with no recall hash offered.
  - `git diff --cached c37a8feb… -- agent/roles/coder.md …` lost 528 of 1016 hunk/changed lines ("... (90 additions truncated)").
  - Two other `git diff --cached abf72de…` replays lost 35 lines each.
  - The `diff --git`, `index` and `new file mode` headers are dropped.
  - Live example: `rtk git show 074a518 -- lib/gate_record.py` shows 8 of the 14 added `def` lines.
- **grep/rg:** at most 25 matches per file, and lines cut at about 80 chars with `...`.
  - `grep -n "def \|^class \|^[A-Z_]* =" lib/task_record.py | head -120` lost 29 of 54 matches.
  - `grep -n "def write_record\|…\|archive" lib/task_record.py` lost 19 of 44, and 18 more had their text cut.
  - `grep -n "test-run\|stub" tests/test_agent_tools.py | head` lost 2 of 10, and 8 were cut.
  - Recursive grep over `lib` showed about 20 of 2318 matches, ending "+1909 hidden: rtk recall …".
- **git log:** `--oneline` subjects are cut at about 80 chars ("…a shell reads them (env -S, cat pipe stage unwrapped)" became "…commands only when a..").
  - `git log --oneline | head -8` lost 4 of 8 subjects; `head -3` lost 2 of 3.
  - It also cuts `--format='%h %s'` output; that caused the Phase 3 T4 failures.
- **ls/find:**
  - Entries are hidden ("+103 hidden"): `ls bin lib tests` lost 210 of 260 names; `ls lib | head -80` lost 20 of 70; `ls tests/*.py tests/*.sh` lost 30 of 80.
  - Per-directory headers are lost.
  - `ls -l` drops owner and mtime.
  - `find` regroups output by directory and hides entries (`find claude -type f | head -50` lost 1 of 30).
- **git branch -a:** remote branches are dropped (4 of 17 lines).
- **stdin grep:** `git ls-files | grep -Ev '^tests/'` lost 76 of 276 lines.
- **wc:** paths are cut to their basename and the total is shown as `Σ` (`wc -l tests/test_lane_remove.py`).
- **cat/tail on a missing file:** the error text changes (no file content is involved). This affected 2 cases.
- Lossless but with no saving: `rtk read` (cat/head/tail), and `rtk git status`. A status with 30 untracked files kept all 30.

## Phase 2: guard compatibility (`scripts/phase2_guards.py`, `scripts/phase2_live_*.sh`, `data/phase2.{json,md}`)

The fixture repo has fake credential-named files containing dummy values only (a cloud key id `AKIA…FIXTURE…`, a `ghp_fixture…` token and a password).
`TP_REDACTION=on` was set, with a scratch redaction store.

### Offline decision matrix

The real Claude hook scripts and Pi gates were run on 40 commands C and on their rewrites R(C). The full table is in `data/phase2.md`.

- **Same decision on C and R(C):** benign, `.env`/`secrets.json` reads (`rtk read .env` still asks on Pi), the trust store (denied in both forms), and placeholder denials.
  - Kill commands are never rewritten.
  - In the actual chains the guards see the *original* command anyway. Claude runs PreToolUse hooks in parallel on the original input; Pi loads `rtk.ts` last.
- **Weaker on the rewritten form (Pi sensitive-read):**
  - `grep -rn AKIA .` asks, but `rtk grep -rn AKIA .` passes. The same holds for `rg -n PASSWORD`.
  - `grep -rn owner ~/.config/tmux-pi/state/trust/` loses its sensitive ask, though the trust guard still blocks it.
  - This matters only when an agent types the `rtk` form itself.
- **Agent-typed rtk commands that escape guards:**
  - `rtk run 'pkill -f fixture-proc'` and `rtk bypass pkill -f fixture-proc` pass the kill guard on Claude and on Pi.
  - `rtk grep -rn AKIA .` passes Pi sensitive-read.
  - Caught: `rtk bypass cat .env` and `rtk run 'cat .env'` (Pi asks), and `rtk read`/`rtk bypass cat` on the trust store (denied).
- **Redaction after RTK line-cutting leaks secret prefixes.** RTK cuts grep lines at about 80 columns, which cuts a secret in half; the redactor no longer recognises the fragment.
  - Secret at column 70: 11/20 chars of the key id and 11/40 of the token stay visible.
  - Secret at column 62: 19/20 and 19/40 stay visible.
  - Raw output was fully redacted in every case.
- **`recall.db`:** RTK stores the full raw output of every elided command, unredacted, for 30 days.
  - It lives in `~/Library/Application Support/rtk/recall.db`.
  - `rtk recall <hash> --full` returned 30 fixture lines containing the dummy key.
  - `rtk recall --list` exposes hashes across sessions.

### Live runs

**Claude**, `claude-haiku-5-5`, worker mode, `--tools Agent,Bash,Edit,Glob,Grep,Read,Write`, no `--allowedTools`. The prompt ran `git status --short`, `ls no-such-dir-xyz`, a grep over the fixture's secret-bearing file, `cat README.md` and `git log --oneline -3`.

| redaction mode | arm | result |
|---|---|---|
| on | off | 6 turns; redact suffix `exit-code: nonzero` present; grep fully redacted |
| on | on (×2) | Identical to off: the suffix is present, the grep is not truncated (6230 chars). RTK's `history.db` logged decision `ask` with a `rewritten_cmd` for every call. **redact.sh's `updatedInput` won both times, so RTK was a silent no-op.** |
| shadow (default) | off | DONE, 0 denials |
| shadow | on | **All 5 Bash calls refused: "This command requires approval."** RTK's rewrite won, and `rtk git status --short` etc. match none of tmux-pi's `Bash(git status:*)`-style allow rules. An interactive worker would prompt instead. |

Both hooks return `updatedInput` for the same call, so whichever finishes last wins and the other is discarded.
RTK gives no `permissionDecision` because it cannot see tmux-pi's `--settings` allow list.

**Pi**, local Qwen, `TP_RTK=1`, `TP_REDACTION=on`, worker mode.

- Both arms ran all five commands. `cat .env` was stopped in both ("needs user authorization").
- RTK on: the grep result was cut to 29 lines with elision markers, and **12 key-id fragments and 13 token fragments stayed visible with 0 redaction markers**.
- RTK off: 61 lines, 0 visible fragments, 60 redaction markers.

The redaction leak is real on the live Pi chain.

### Environment note found along the way (not an RTK issue)

`/usr/bin/python3` (3.9) comes first on this shell's PATH. Under it, `lib/trust_guard.py` raises on any glob (`glob.iglob(include_hidden=…)` needs Python ≥ 3.11) and denies the call as "could not be analysed".
The first Phase 2 matrix and the first 4 A/B runs hit this. They were rerun with `/opt/homebrew/bin` (Python 3.14) first, and the discarded runs are not counted.
Worth a separate fix, either a version check or a shebang pin.

## Phase 3: live A/B (`scripts/phase3_ab.py`, `data/phase3_runs.jsonl`, `data/phase3.md`)

Setup:

- Commit `25e2de2`. Each run used a fresh throwaway detached worktree and its own `TP_TASK_DIR` (worker mode).
- `TP_REDACTION=shadow` was set explicitly in both arms. With `on`, RTK is a no-op on Claude (see above).
- Arms alternated within each rep.
- Claude: `claude-sonnet-5-5`, `--allowedTools Bash` (without it every rewritten command is refused), 3 reps per arm per task.
- Pi: `lmstudio/qwen/qwen3.6-35b-a3b`, run serially, 5 reps per arm per task.

Tasks, all read-only, each with a deterministic checker:

| task | question | known answer |
|---|---|---|
| T1 grep-count | lines matching `^\s*def ` in lib/**/*.py: the total, plus the top file and its count | 2316, lib/trial_shadow.py, 130 |
| T2 ls-tests | `tests/test_*.py` files at top level: how many, and which sorts last | 129, test_worker_workdir.py |
| T3 diff-defs | module-level functions that commit 074a518 adds to lib/gate_record.py, in order | 14 names |
| T4 log-subjects | every commit touching lib/kill_guard.py, with the full subject | 4 commits, 3 subjects over 80 chars |

| harness | task | arm | n | correct | median billed input | median output | median turns | bypass / recall / agent-typed rtk | median wall s |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| claude | T1 | off | 3 | 3/3 | 35,636 | 294 | 2 | 0 / 0 / 0 | 35 |
| claude | T1 | on | 3 | 3/3 | 35,415 | 267 | 2 | 0 / 0 / 0 | 35 |
| claude | T2 | off | 3 | 3/3 | 35,395 | 331 | 2 | 0 / 0 / 0 | 35 |
| claude | T2 | on | 3 | 3/3 | 36,750 | 281 | 2 | 0 / 0 / 0 | 34 |
| claude | T3 | off | 3 | 3/3 | 35,546 | 223 | 2 | 0 / 0 / 0 | 34 |
| claude | T3 | on | 3 | 3/3 | 35,747 | 269 | 2 | 0 / 0 / 0 | 34 |
| claude | T4 | off | 3 | 3/3 | 35,681 | 290 | 2 | 0 / 0 / 0 | 34 |
| claude | T4 | on | 3 | 3/3 | **72,445** | 594 | **4** | 0 / 0 / 0 | 39 |
| pi | T1 | off | 5 | 5/5 | 25,138 | 758 | 8 | 0 / 0 / 0 | 24 |
| pi | T1 | on | 5 | 4/5 | 32,595 | 891 | 9 | 0 / 0 / 0 | 28 |
| pi | T2 | off | 5 | 5/5 | 7,576 | 278 | 3 | 0 / 0 / 0 | 8 |
| pi | T2 | on | 5 | 5/5 | 18,023 | 459 | 6 | 0 / 0 / 0 | 16 |
| pi | T3 | off | 5 | 5/5 | 12,176 | 485 | 3 | 0 / 0 / 0 | 10 |
| pi | T3 | on | 5 | 5/5 | 10,067 | 377 | 3 | 0 / 0 / 0 | 11 |
| pi | T4 | off | 5 | 5/5 | 4,554 | 225 | 2 | 0 / 0 / 0 | 4 |
| pi | T4 | on | 5 | **1/5** | 4,535 | 215 | 2 | 0 / 0 / 0 | 8 |

Billed input is input + cache read + cache write. Median change in billed input, RTK on vs off:

| task | Claude | Pi |
|---|---:|---:|
| T1 | −0.6% | +29.7% |
| T2 | +3.8% | +137.9% |
| T3 | +0.6% | −17.3% |
| T4 | +103.0% | −0.4% |
| **median** | **+2.2%** | **+14.6%** |

Totals: Claude off 445,690 vs on 541,697 billed (12/12 vs 12/12 correct; $0.265 vs $0.298). Pi off 457,866 vs on 995,422 (20/20 vs 15/20).

Why these results:

- **Claude T4:** `rtk git log --format='%h %s'` cut the long subjects.
  - In 3/3 runs Sonnet noticed the `..` and re-queried with `awk length`, `git cat-file commit` or `git log -1 --format=%B`. The re-query is not rewritten.
  - It stayed correct, but each run took 1–2 extra turns and up to twice the input.
- **Pi T4:** in 4 of 5 runs Qwen reported the truncated subjects (`… | xarg...`) as the answer. In the one correct run, Qwen re-ran it five ways first.
- **Pi T1:** the one wrong answer is model error, not RTK. The `grep -rc … | sort` output it read was not rewritten and did contain `lib/trial_shadow.py:130`.
- **Pi T2:** the RTK arm took more `ls`/`find` calls (median 5 vs 3) because `rtk ls`/`rtk find` regroup and hide entries.
- **Claude per-run input is dominated by the roughly 35k-token system and tool prompt.** On short read-only tasks, Bash output is a small slice of context, which matches the Phase 0 ceiling.

### Bypass and recall use (the arXiv:2609.20804 question)

- Across 64 A/B runs and 7 live guard runs, agents typed `rtk bypass` 0 times, `rtk recall` 0 times, and any `rtk …` command 0 times.
- Neither model used RTK's recovery path, even when an output said "+N hidden: rtk recall …". Instead they re-ran the query in another form (Claude) or accepted the truncated output (Qwen).
- So recoverable elision added machinery that went unused, cost turns where the model noticed the loss, and cost accuracy where it did not. This matches the paper's finding.

## Licence and edition

- The full edition is Apache-2.0 + BUSL-1.1, and the BUSL-1.1 part is the compression engine (`engine/`, `pipeline.toml`).
- `rtk-oss` is Apache-2.0 only: "Hook core and every command filter. No engine."
- Per `docs/guide/resources/editions.md` (via `gh api repos/rtk-ai/rtk/contents/...`), rtk-oss lacks `rtk pipeline`, `rtk methods`, `rtk tasks`, PostToolUse compression of Read/MCP output, and pipeline files. It keeps the PreToolUse rewrite hook, every command filter, `rtk recall`, `rtk gain`, `rtk session` and `rtk bypass`.
- **This eval used only rtk-oss features:** the PreToolUse Bash rewrite hook (`rtk hook claude`, `rtk rewrite`), the command filters, and recall/bypass. It never used PostToolUse compression or a pipeline file.
- So the results should carry over to `rtk-oss`. That is inferred from the docs; the oss binary was not run.

## What was not measured

- **The exclude list was never applied as a config.** `[hooks] exclude_commands` was not written to RTK's config and re-run. The partial-adoption conclusion rests on the per-family Phase 1 numbers.
- **Claude Code's interactive permission prompt was not exercised.** Interactive Claude workers (spawned by tp-spawn) with RTK in shadow mode were not observed directly. The headless refusal implies a prompt per rewritten command.
- **Phase 0 token counts are estimates.** They come from chars/R calibrated on large results, not from a tokenizer.
- **Small A/B samples.** n=3 per arm for Claude and n=5 for Pi, on short tasks. Long multi-turn worker sessions, where cache-read weighting favours RTK most, were not run live. Phase 0 bounds them at about 2–4%.
- **Sonnet usage:** 28 runs (24 counted, 4 discarded because of the Python 3.9 guard issue). The Phase 2 live runs used Haiku (5 runs).
- **RTK's own state was left where it defaults.** `history.db` and `recall.db` are under `~/Library/Application Support/rtk/` and now hold raw output from these runs, including the dummy fixture values. Delete them when RTK is removed.
