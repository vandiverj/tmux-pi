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
