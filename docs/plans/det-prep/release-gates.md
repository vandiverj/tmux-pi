# det-prep release gates

Owner 2026-11-08: "where do we need to make it to before we can confidently feel the changes improve the production
use of tmux-pi and does not introduce a new failing". Answer: these five gates. Per-lane module runs plus a
cross-provider review prove a lane does what its brief says; they do not prove the whole system. The 2026-11-08
full-suite gate (run-log) found 387 off-baseline failures in 20 modules that M2's own run and review missed.

Claims each gate allows:
- G1 + G2: "no failing tests at all" (G1 alone: "no new failing").
- G3 + G4: "improves production use".
- G5: "safe to port to the work Mac".

## G1: full suite on main
- **When:** after the open lanes merge (M2-F1, FG-N1, LS1); again after every later merge batch.
- **What:** all test modules, `/usr/bin/python3` (3.9.6) and `/opt/homebrew/bin/python3` (3.14), session keys unset and
  dummy TMUX/TP_*, in a clean worktree of the `main` commit (no local config edits). One module per process with a
  time bound; a bound hit is re-run solo with a longer bound before it counts.
- **Done when:** zero FAIL/ERROR names outside `baseline-failing.txt` in all four runs. Every off-baseline name is
  fixed or traced to a cause before the gate passes. Record the commit, counts and logs in the run-log.

## G2: fix every failure (baseline to zero)
- **Owner 2026-11-08:** "I want all failures fixed in the end so they should be determined and fixed".
- **What:** every name in `baseline-failing.txt` (13 on 2026-11-08), plus anything G1 finds, gets a found cause and a
  fix. A test that is itself wrong is fixed as a test, with the cause shown; nothing is skipped, deleted or left
  "explained" to pass.
- **How:** group names by cause, one lane per group, the normal lane flow (repro on main, fix, both interpreters and
  modes, cross-provider review). Each fixed name is removed from `baseline-failing.txt` in the same lane.
- **Done when:** `baseline-failing.txt` is empty and a G1 run on the resulting `main` has zero FAIL/ERROR names in all
  four runs.

## G3: end-to-end smoke
- **What:** one scripted run on the battle-soccer test bed through the real tools: dispatch, a permission prompt that is
  declined and redirected, retry, cross-provider review, `lane close`, merge, `collect`, `lane remove`.
- **Done when:** the run completes; afterwards no leftover worktree, tmux session or pane; task records, lane records
  and the review verdict are correct; repeatable from a script.

## G4: before/after measurement
- **What:** the same small task set on `d8a18a7` (last pushed commit before this campaign) and on the new `main`.
  Measure first-try approval rate, review rounds, tokens per task, and answer completeness checked by a deterministic
  checker (reuse LS1's `tools/qual/summarize_check.py` pattern).
- **Needs first:** LS1 flipped (`summarize_files` qualified per ls1-local-summarizer.md "S2 additions").
- **J1-D1, decided 2026-11-09:**
  - (a) bench local tasks dispatch with a role that has no strict mid floor. The catalog floor stays as it is.
  - (b) each task is reworded to the form its shape was qualified with.
  - (c) summarize-importers is replaced by a task of a qualified shape for now, then re-added in the qualified summarize
    form once LS1 is flipped. G4 runs with it included.
- **Done when:** the numbers are recorded with the task set and commits; "improves" is claimed only where the numbers
  show it.

## G5: port rehearsal
- **What:** a fresh clone on this Mac set up like the work Mac (no `config/local.json`, no LM Studio, Apple Python 3.9),
  then G1 and G3 there. The work machine itself stays untouched.
- **Done when:** G1 and G3 pass in the fresh clone; any setup step it needed is written into the port notes.

## Order
G1 after the merges; G2 and G3 in parallel as lanes; G4 after the LS1 flip (J1-D1 decided); G5 last.

## Checkpoint 1: a production cut without the local summarizer (owner 2026-11-09)
Owner: "can we get to a point for improvements to use on production without the sumarrizer ... a checkpoint we can use and
you would continue the other work".

- **Scope:** main as it stands once MS1, MN1, RO1 and G3 round 6 are merged. `summarize_files` stays `candidate` in
  config/model-catalog.json, so nothing routes summaries to the local model. The LS1c lanes (S1c, MG1) may merge before
  the cut. They only add tools/qual, lib/summarize_structured.py and tests, and nothing on the production path calls them.
- **Gates for the cut (in order):**
  1. **MS1 merged.** `tp-agent message` submits and confirms. This is the cause of G3 live runs 4 and 5, and a production
     defect. It is also the owner's first named production issue ("messages to workers not getting sent").
  1b. **MN1 merged** (docs/plans/det-prep/fixes/mn1-main-notify.md). Worker state changes reach the Claude main: a stable
     main id, no silent hiding of rows, and no silently dropped notice. This is the owner's second named production issue
     ("workers no longer sending messages ack to main agent", 2026-11-09).
  1c. **RO1 merged** (docs/plans/det-prep/fixes/ro1-result-kept.md). A worker's report is not replaced by a later turn it did
     not get from outside (for example its own background task finishing). Found by Main on 2026-11-09, when MS1's report
     was lost this way. `report.sh` is unchanged since d8a18a7, so production has it, and it is one way the owner's second
     issue shows up. The owner confirmed their main is Claude Code and their side tasks are subagents.
  2. **G3 round 6 merged and a live run passing.** This is the end-to-end flow on battle-soccer: dispatch, permission,
     decline plus redirect, retry, cross-provider review, close, merge, collect, remove.
  3. **G1 on the cut commit:** the full suite in 4 cells, zero failures (the baseline is empty).
  4. **G5 port rehearsal:** a fresh clone set up like the work Mac (no config/local.json, no LM Studio, Apple Python
     3.9), then G1 and G3 in it. The work Mac itself stays untouched.
- **Then:** tag the commit `checkpoint-1` (local tag). Write port notes: what changed since d8a18a7, setup steps, how to
  roll back to d8a18a7. Push only with the owner's approval.
- **G4 (before/after numbers)** is not a gate for this cut. It can run on `checkpoint-1` with the J1-D1 task set (no
  summarize task), so any "improves" claim comes with numbers.
