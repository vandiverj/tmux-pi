# <CASE-ID> step <step>, fix round <n> (lane <lane>)

**Read first:** <C>/briefs/brief-common.md and the original step brief <C>/briefs/lane-<lane>-<step>.md. Same tool
rule and never-list.

**Worktree:** <wt>, HEAD `<sha>`. Make **new commits on top of `<sha>`; never amend**.
**Review:** <path to the review task's result.md> (verdict REQUEST_CHANGES on `<sha>`).

## Main's ruling on each finding
| # | Finding (short) | Cause (rule or assumption broken) | Ruling | What to do |
|---|---|---|---|---|
| 1 | … | <from the reviewer's diagnosis> | Fix | <specific change and the test that proves it> |
| 2 | … | … | Not a defect: <reason, decision ID> | Nothing; mention it in the report |
| 3 | … | … | Out of scope: handed to <case/step> | Nothing |

Notes (the reviewer's N-items) need no production change: list them in the report, don't act on them.
Findings labelled `Closes by: check` are Main's to run, not this round's.

**Already decided (copy into the re-review's brief so it isn't raised again):** <rulings from this and earlier
rounds>.

**Why the earlier fix missed it** (round 2 and later, from the reviewer's statement): <…>

Fix the cause, not the cited site: check for the **same kind of defect elsewhere** in the files this lane changed
(name the pattern), and fix every instance, not only the one the reviewer cited.

<Second return of a finding family: replace "Do" with: "First write the rule and the full case table for the
<family> family (starting state × trigger → result) into your report, with no code. Main checks it. Then fix every
row." Third return: don't send this brief; change the design or park it for `<owner>` (`04-tmux-pi-operations.md`).>

## Tests and checks
Rerun the step's tests plus a test for each fix (failing first), then brief-common's checks.

## Report
Each finding: the commit that fixes it and the test, or why nothing changed. Then brief-common's Report section,
including the Self-review. End with `RESULT_STATUS: complete`.
