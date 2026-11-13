# HB7c slice 1 (design): model profiles, built and ready to measure on the bench

Owner, 2026-11-12: "HB7c approved". Plan: docs/plans/det-prep/m1-model-profiles.md (read all of it, including the owner's
words). This is the "simpler workflows for newer models" arm of HB7 (H15).

The owner's limits apply. Tests do not use frontier models, and frontier tokens are a concern. **This slice builds and dry-runs
the arms. It starts no live worker and sends nothing to LM Studio.** Main runs the live pilot after review, with the run count
you propose and Main approves.

## Step 1 (read only)
Read bin/tp-bench (`run`: arms are checkouts; `--env`; `--tasks`; `--repeat`; `--max-worker-runs`; records),
bench/tasks/*.json, docs/plans/rtk-eval/plan.md and results.md (the token baseline M1 waits on), and how a dispatch resolves a
worker's model and role file into the packet (bin/tp-agent dispatch, the packet builder). Report:
- how an arm can carry a profile with no change to today's default dispatch: a checkout whose packet adds a profile layer, an
  `--env` the packet reads, or another way;
- which bench tasks have a deterministic checker, and so fit M1's correctness metric;
- what tp-bench already records of the M1 metrics: correctness, review verdict, retries, tokens, turns, wall time,
  parked/prompted runs.

## Step 2: the design (in report.md before code)
- **Two profiles** as M1 defines them: `expert` (goal, constraints, done-means only) and `guided` (numbered steps,
  plan-first, a checklist before reporting, a worked report example). Each is a small layer over the role file. Never edit
  agent/roles/*.md: each role line carries owner evidence (HB7b ledger).
- **How a profile is chosen:** per dispatch, from the resolved model or an explicit setting. Default = none = today's packet,
  byte-identical.
- **The bench arms for a first pilot:** frontier only. Haiku and Sonnet × {expert, guided}, on the deterministic-checker
  tasks. Propose the task subset and reps, and the worker-run count with an estimate of its tokens (from rtk-eval results). Keep
  the pilot at or under 24 worker runs.
- **The Qwen (local) arm is slice 2.** Write what it would need under the LM Studio cap (opt-in, 300 s per call, one at a
  time, 20 per run) and whether a local worker task fits it. Do not run it.
- **The decision rule,** from M1: adopt a finer grouping only where it beats the coarser one on correctness or retries to
  approval, without costing more tokens per approved result.

## Build
- The profile files.
- The smallest wiring that lets an arm or a dispatch use them.
- A dry run of the pilot arms that prints the packet each arm would send, with no worker started. Use it to show the default
  packet is unchanged: a byte diff against main.

Tests: extend the packet/dispatch tests that pin the default packet; add rows for each profile and for "no profile = byte
identical". Fail-before as usual, with a `.py` in evidence/ run with `python3 -I` against main in /tmp. Run once through
`tools/agent/test-run.sh` on /usr/bin/python3, outside tmux, with a fresh TMUX_TMPDIR: the modules you touch plus
tests.test_session_env.

Touch only new files under agent/profiles/ and tools/profiles/, plus at most the packet builder and dispatch wiring you name
in step 1. Ask Main before editing any existing file and name it. If a guard refuses an edit, ask Main; do not route around
it. Do not edit config/, agent/roles/ or claude/main-prompt.md. Do not commit. No git notes. No background shells.
`RESULT_STATUS: complete` as the last line.
