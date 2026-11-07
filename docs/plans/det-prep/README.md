# Deterministic preparation, completion evidence and reviewer independence: implementation plan

Written 2026-11-07. Executed on this Mac against tmux-pi `main`; brought to the work machine afterwards (it is busy running
work cases, so nothing here depends on it). Primary goal: quality of the code workers produce. Second: fewer wasted tokens.
Both within the owner's AI credits, so the local Qwen model does every step it can do, and no step uses the `top` level.

Source of the gaps: the 2026-11-06 research session (seven scouts over the code, all claims re-verified by Main):

| Gap | Where | Lane |
|---|---|---|
| Projects start with no `.tmux-pi/agent-tools.json`; the Tools block never renders; workers run `tp-agent --help` (86 tasks) | lib/agent_tools.py, docs/agent-tools.md | A |
| `LANE_BASE` is read by lane-info.sh and pre-commit-gate.sh but never set; packet has no branch/HEAD/base/changed files (438 tasks orient with git) | bin/tp-agent `launch()`, bin/tp-spawn `# Context` | C |
| Context is line ranges only; whole prototype docs get re-read (1.0M tokens) | bin/tp-spawn `read_context` | C |
| coder.md cites a `tp-agent test-run` record that does not exist; no machine record of tests run, on which tree | tools/agent, agent/roles/coder.md | B |
| Gate: missing tool skips silently, no result record, no tree fingerprint, gitleaks ignores dirty files | tools/agent/pre-commit-gate.sh | B |
| Lane close needs an approved review of the tree but no gate or test record | lib/lane_steps.py `evaluate` | B |
| Reviewer reads Main's Frame, the author's packet and the author's result before any diff; round 1 has no diff | bin/tp-review ctx order, lib/review_rounds.py | D |
| Coverage is enforced only as "a section with three words"; affected areas are model recall | lib/review_verdict.py, code-reviewer.md | D |
| routines report double counts overlapping probes, mislabels, uses len/4, misses Claude main and Claude re-reads | lib/routines.py, bin/tp-improve | E |

## Test bed

`~/Documents/Projects/battle-soccer`: Vite + React + TypeScript 6 + vitest, `tsconfig.json`, no jest, no `.tmux-pi/`. It is
disposable: workers may add files, a `.tmux-pi/` directory and throwaway branches. It proves the changes on a real
TypeScript project the way the work monorepo would see them (tsc typecheck, a test command, a lane worktree). Jest-specific
behaviour stays covered by tmux-pi's own fixtures (`tests/fixtures/`), not by battle-soccer.

tmux-pi's own tests are the reference: `python3 -m unittest discover -s tests -p 'test_*.py'` (serial) and
`tests/check_dispatcher_types.sh`. Every lane adds unit tests in the existing module for the code it touches. battle-soccer
runs are the live proof and are recorded in `docs/plans/det-prep/evidence.md` by Main (task id, command, last lines).

## Lanes, ownership and order

Each lane is a tmux-pi lane (`tp-agent dispatch --lane NAME --step N --domain ... --touches ...`), one writer at a time, a
cross-provider review before each step closes. `--touches` are exclusive; a worker that needs a file another lane owns sends
`tp-agent request`, it does not edit.

| Lane | Touches | Steps (brief file) | Depends on |
|---|---|---|---|
| A `tools-template` | `templates/agent-tools.example.json`, `docs/agent-tools.md`, `lib/agent_tools.py`, `tests/test_preamble.py`, `tests/fixtures/agent_tools/` | A1 `a1-template-and-cheatsheet.md` | none |
| C `lane-facts` | `bin/tp-agent`, `bin/tp-spawn`, `lib/lane_commands.py`, `tests/test_lane_create.py`, `tests/test_lane_lease.py`, `tests/test_retry_packet.py` | C1 `c1-lane-facts.md`, C2 `c2-heading-context.md` | none |
| E `measure` | `lib/routines.py`, `bin/tp-improve`, `tests/test_routines.py` | E1 `e1-routines-accuracy.md`, E2 `e2-claude-coverage.md` | none |
| B `evidence` | `tools/agent/`, `lib/gate_record.py` (new), `lib/lane_steps.py`, `agent/roles/coder.md`, `agent/roles/tester.md`, `config/orchestration.json`, `tests/test_agent_tools.py`, `tests/test_lane_steps.py`, `tests/test_gate_record.py` (new) | B1 `b1-gate-record-and-test-run.md`, B2 `b2-lane-close-evidence.md` | A1 merged (B1 names the test-run usage line A1 ships) |
| D `review-order` | `bin/tp-review`, `lib/review_rounds.py`, `lib/review_verdict.py`, `agent/roles/code-reviewer.md`, `tests/test_tp_review.py`, `tests/test_review_rounds.py`, `tests/test_review_verdict.py`, `tests/test_review_coverage.py` | D1 `d1-claims-last.md`, D2 `d2-affected-areas.md` | none; D2 after C1 is merged is nicer (lane facts give the base) but not required |

Phase 1 (parallel): A1, C1, E1, D1. Phase 2: B1, C2, E2. Phase 3: B2, D2. Phase 1 is where the quality gain per credit is
highest: D1 changes what every reviewer sees, C1 removes the biggest orientation cost, A1 makes the tools reach workers at all.

Integration: each lane merges into `lane/integrate` when its steps are closed, then `main` once battle-soccer verification
passes. Nothing is pushed or committed to `main` without the owner.

## Model levels per kind of step

Levels are the keys of `config/model-catalog.json`. The coder, tester and researcher roles have a `mid` floor in the
catalog, so a frontier coder step is `mid` (Sonnet 5.5 or GPT-6 Sol at the router's choice) and never `top`. `strong` has
no roster model. The reviewer is the other provider at `mid`, default effort. Local Qwen takes a step only when the request
names the files or the exact command and the output can be checked by reading it (the catalog's `local` rule), and is never
the only reviewer.

| Step kind | Level / flags | Examples in this plan |
|---|---|---|
| Code change with tests, more than one file | `mid` coder (router default) | A1, B1, B2, C1, C2, D1, D2, E2 |
| One-file edit with a named check | `small` coder (`--level small`) | E1 |
| Review of a step | cross-provider `mid` (tp-agent review) | every step |
| Run named checks on battle-soccer and quote the output | local Qwen, `--local-only`, shape `run_named_checks` | V-A, V-B, V-C below |
| Count or list things in files | local Qwen, shape `count_enumerate` | the per-role byte count in A1's check, the probe table in E1's check |
| Pre-review self-check of a diff for a named rule (e.g. "every new JSON key is documented") | local Qwen, shape `locate_quote` | optional, before each review to save a round |

Expected frontier worker runs if nothing is retried: 9 coder steps + 9 reviews. Local runs: about 6 to 8. Main (this
session) stays on short turns: dispatch, collect, run the three verification scripts, record evidence.

## Verification on battle-soccer (Main runs; local Qwen quotes)

- V-A (after A1): copy `templates/agent-tools.example.json` to `battle-soccer/.tmux-pi/agent-tools.json`; render the coder
  preamble (`python3 lib/preamble.py --workdir ~/Documents/Projects/battle-soccer --role coder` or the equivalent the brief
  names); the Tools block lists lane-info, pre-commit-gate, test-run and the standing cheat-sheet lines; byte total under 8,000
  for every role.
- V-C (after C1 and C2): `tp-agent lane create bs --repo ~/Documents/Projects/battle-soccer ...`; dispatch a trivial coder
  task (`--local-only` is not possible for coder; use `--level small`) into step 1 with `--context README.md#Rules` (or the
  heading battle-soccer actually has); `tp-agent inspect` shows the packet's `## Lane` section with branch, HEAD, base sha,
  changed files, and the heading-resolved context with its line range; `run.sh` exports `LANE_BASE`; inside the worker,
  `lane-info.sh WORKTREE` prints a base without arguments.
- V-B (after B1 and B2): in that lane, `pre-commit-gate.sh WORKTREE` writes `gate.json` beside the log and in the task dir,
  with `tsc --noEmit` as a ran check and gitleaks either ran or `missing`; `test-run.sh WORKTREE -- npx vitest run` writes
  a test-run record with exit, wall seconds and the tree fingerprint; after one more edit, `tp-agent lane close bs 1` refuses
  naming the stale gate; after re-running the gate it closes.
- V-D (after D1): review a battle-soccer step; `tp-agent inspect REVIEW` shows the context order brief, scope, diff,
  then author packet and author result last; the reviewer's reply has a `Coverage` section naming every changed file. After
  D2: the packet has an `Affected areas` file with ids and the reply marks each id.
- V-E (after E1 and E2): `tp-improve routines --project ~/Documents/Projects/battle-soccer` over the tasks above prints
  exclusive and overlapping columns, per-role denominators, and a `main` row from the Claude main transcript of this session.

## Rules for every brief

- Stay inside `--touches`. Needed file elsewhere: `tp-agent request --kind context`.
- Every behaviour change has a test that fails without it, in the module the lane owns (coder.md rule).
- Snapshots that can go stale say so in their own text (timestamp, sha) and name the command that refreshes them.
- A record a machine will read is JSON with a `schema` field (`tp-gate:v1`, `tp-test-run:v1`, ...), written atomically
  (temp file then rename), never trimmed silently: when it cannot be written, the command says so and exits non-zero.
- No new dependency. Bash scripts keep the `tools/agent/_common.sh` conventions (`exit=N log=PATH` last line).
- `git diff --check` clean. Docs updated in the same step for every user-visible flag or file.

## Phase 1 dispatch (run from the tmux-pi main checkout)

Local model check first: `curl -s localhost:1234/v1/models` must list `qwen/qwen3.6-35b-a3b` (verified 2026-11-07).

```
P=docs/plans/det-prep
tp-agent dispatch --lane tools-template --step a1 --domain "agent-tools template and cheat sheet" \
  --touches templates docs/agent-tools.md lib/agent_tools.py tests/test_preamble.py tests/fixtures/agent_tools tools/agent/test-run.sh \
  --role coder --tests affected --context $P/a1-template-and-cheatsheet.md \
  --task "Implement $P/a1-template-and-cheatsheet.md; Done means is the acceptance list."
tp-agent dispatch --lane lane-facts --step c1 --domain "lane facts in the worker packet" \
  --touches bin/tp-agent bin/tp-spawn lib/lane_commands.py tests/test_lane_create.py tests/test_lane_lease.py tests/test_retry_packet.py docs/lanes.md \
  --role coder --tests affected --context $P/c1-lane-facts.md \
  --task "Implement $P/c1-lane-facts.md; Done means is the acceptance list."
tp-agent dispatch --lane measure --step e1 --domain "routines report accuracy" \
  --touches lib/routines.py bin/tp-improve tests/test_routines.py \
  --role coder --level small --tests affected --context $P/e1-routines-accuracy.md \
  --task "Implement $P/e1-routines-accuracy.md; Done means is the acceptance list."
tp-agent dispatch --lane review-order --step d1 --domain "reviewer packet order" \
  --touches bin/tp-review lib/review_rounds.py lib/review_verdict.py agent/roles/code-reviewer.md tests/test_tp_review.py tests/test_review_rounds.py tests/test_review_verdict.py tests/test_review_coverage.py \
  --role coder --tests affected --context $P/d1-claims-last.md \
  --task "Implement $P/d1-claims-last.md; Done means is the acceptance list."
```

Then for each: `tp-agent collect TASK`, `tp-agent review TASK` (router picks the other provider at `mid`), `tp-agent lane
close LANE STEP`. Phase 2 and 3 steps use the same shape with the next brief; B1 waits for A1 to be merged to
`lane/integrate`. Local verification runs are `tp-agent dispatch --local-only --role tester --task "Run CMD in DIR and
quote the last 20 lines and the exit line" --tests none`.

## Added 2026-11-07: from the owner's campaign prompts, status list and LM Studio

| Lane | Steps | Brief | Waits for | Level |
|---|---|---|---|---|
| F `status-cleanup` | F1 | `f1-status-cleanup.md` | none | mid |
| G `local-model` | G1 | `g1-local-model-load.md` | none | small |
| H `main-guards` | H1 | `h1-main-guards.md` | C1 merged | mid |
| I `campaign` | i1, i2, i3, i4 | `i1-campaign-mechanics.md` | C1, D1, D2 merged; i4 after B1 | mid |

F1 and G1 can start now beside Phase 1 (no shared files). After I lands, `docs/prompt-examples/` is rewritten to drop the
rules tmux-pi now enforces; that edit is a `small` coder step with the list of rules as its input. One rule there is
already stale: `04-tmux-pi-operations.md` says a REQUEST_CHANGES with no `code` finding is "not built yet", but
lib/review_verdict.py already treats it as unusable.

## Added 2026-11-07 (2): benchmark and improvement loop

| Lane | Steps | Brief | Waits for | Level |
|---|---|---|---|---|
| J `bench` | j1 | `j1-benchmark.md` | E1, E2 merged | mid |

Order of the remaining work: Phase 2 (B1 C2 E2), Phase 3 (B2 D2), J1 and its control runs, H1, i1 to i4, the
prompt-examples cleanup, then the improvement loop in `loop/PROTOCOL.md` (research run first, then cycles). The loop's
state is entirely in `loop/`; read `loop/PROTOCOL.md` after any compaction.
