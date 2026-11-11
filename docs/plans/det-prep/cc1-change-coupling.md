# CC1: git change coupling for review (loop idea H29)

Owner, 2026-11-10: when a reviewer finds a defect that may recur in other modules, give it a short list of files that
historically changed alongside the affected file, with the number of shared commits and a few representative commit ids so
the reviewer can check why they changed together. Advisory only: the owner decides on adoption. Status: **queued**.


**Retargeted (owner, 2026-11-10).** These tools are for tmux-pi to check the project its agents work on (battle-soccer here,
work projects on the work Mac), not tmux-pi itself. The tmux-pi cases below only show the tool installs, runs and reports; the
cases that decide adoption come from a target project (battle-soccer first: TS/React, Vite, 21 modules, 48 commits), run on a
scratch clone. The reviewer is the gate, not the only place: score the tool at each stage (task start, after an edit, coder
before finish, review, lane close), run from a hook where possible, skipped with a note when the tool is not installed, never
a crash or a block. Stage map: the code-quality eval's "Where the checks run".

## Expected gain
Affected files surface earlier, including sibling implementations that share a business rule but have different names or code
structure, which text search and ast-grep (H13) miss by construction.

## Arms (same reviewer, packet, settings and budget; fresh session per run)
- **A, current:** today's review search workflow (`tp-review`, safe_grep/safe_read, ctx tools).
- **B, + git co-change:** A plus a ranked list from plain `git log`, attached with `tp-review --evidence FILE` (the handoff point
  found for the code-quality eval; no code change). Per seed file: the top **K = 8** files by shared commits, each with its
  shared-commit count, coupling share (shared / seed commits) and up to 3 commit ids with subjects.
- **C, + fallow hotspots (replaces Code Maat, owner 2026-11-10):** A plus `fallow health --hotspots --since <window>
  --format json` computed at the reviewed change's base, listing which of the seed's candidate files are hotspots (churn x
  complexity). Main's trial on main (1c689ec): 23 TS/JS files ranked in 0.35 s. It has **no co-change pairs**, so it cannot
  replace B's list; it tests whether a hotspot mark helps the reviewer choose which listed files to open first. It sees only
  TS/JS, never `lib/` or `bin/` Python. Code Maat comes back only if B adds coverage and B's ranking looks too crude.

## History rules
- History is `git log --no-merges <base>` where `<base>` is the reviewed change's base: nothing after the reviewed change.
- Excluded commits: more than 30 files changed (bulk moves and formatting), and dependency-only bumps. Excluded files: generated
  data (for example `claude/plugins/tmux-pi/hooks/index-text-cases.ts`, check before excluding), lockfiles, `docs/plans/**`
  run logs and briefs (they co-change with everything in a campaign).
- Merge and squash history: lanes merge with `--no-ff`, so each lane commit stays visible and `--no-merges` avoids counting a
  merge's whole diff twice. A squashed lane collapses many commits into one wide commit, which inflates coupling between
  everything it touched; the report records how many squash-like commits (single parent, more than 15 files) fell in range and
  reruns B without them.

## Cases
1. **F1 status-cleanup (cross-file, one site per round).** Rounds 6-8 reviews found new sweep sites outside the seed file, for
   example `bin/tp-contract` calling `write_record` without `relative=True` after the fix in `lib/task_record.py`. Reviews:
   `.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-{222f5202c9-161245,627bab8253-175516,dd5117f3c0-183947}/`
   (local, untracked); briefs `fixes/f1-round*.md`. **Fixture needed:** the F1 lane branch no longer exists; each round's diff
   is rebuilt from the review packet onto its recorded base, and the base must be found in `lane/integrate` history.
2. **dc59477** (2026-10-27, "Count a Closes by label only where it ends a finding…"): one rule changed in
   `agent/extensions/subagents.ts`, `claude/mcp/subagents.mjs`, `claude/plugins/tmux-pi/hooks/register.tsx`, `bin/tp-agent`
   and `lib/index_text.py`. Replay: seed = `subagents.ts` only, history before dc59477; ground truth = the other four.
3. **Two or three more historical multi-file fixes** found with `git log --stat` (a fix commit whose message names one rule and
   touches sibling implementations in different languages or dirs). Ground truth is written in `expected.md` before any arm
   runs.
4. **Control:** SG1 rounds 4-8 (`lane/safe-search`): every same-cause site is inside `lib/sensitive_read.py`, so co-change
   should add nothing; it measures noise on a single-file family.

## Measures (per case, per arm)
Confirmed affected sites found on the first pass; ground-truth sites missed; irrelevant files the reviewer opened because of
the list; elapsed time including building the list; reviewer tokens. Review rounds only if the repair loop is replayed. Also
the ranking's own recall at K = 8 without a reviewer (cheap, deterministic).

## Decision
Recommend the simplest approach that adds coverage: B alone unless the hotspot mark in C cuts irrelevant opens or finds a
ground-truth file sooner. Name
the trigger for the reviewer (for example "a finding whose cause is a rule, not a line, in a file with at least N commits").
Adopt as on-demand only if B finds at least one ground-truth file A missed in two or more cases, and irrelevant opens stay
under 3 per case.

## Missing before it can run
- Nothing to install: fallow 3.10.0 is already in the npx cache. (Code Maat would need a Java runtime, which this Mac lacks;
  dropped unless B shows promise.)
- The F1 fixture (case 1) and the extra historical cases (case 3).
