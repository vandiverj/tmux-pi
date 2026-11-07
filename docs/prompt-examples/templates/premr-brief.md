# <CASE-ID> send gate: final sync with origin/develop and full recheck (lane <lane>)

**Read first:** <C>/briefs/brief-common.md. Same tool rule and never-list. Never rebase. Don't push.

**This brief overrides /review-pre-mr** only for the staged run (02 §8), which is a review only: (1) it skips the
skill's builds and full test suites for affected projects (API, e2e, web/admin); (2) it stops at the review report and
does not hand off to `/ship-mr`; (3) diff base: for a stacked child whose parent has not merged, compare against the
local parent branch, not origin/develop; the skill's origin/develop comparison applies only at send time. This
send-time brief runs the skill's build and test steps in full, and `/ship-mr` follows only with `<owner>`'s OK.

Dispatch this only for the case about to be pushed; other staged lanes get `templates/sync-merge-brief.md`.

**Worktree:** <wt>, HEAD `<sha>` (clean). Base: `origin/develop` for every step below, including a stacked case
(its parent has merged and the lane already had a sync merge). Never compare against a parent branch, remote or
local.

## Do
1. `git -C <repo> fetch origin`. If `git -C <wt> log --oneline HEAD..origin/develop` is empty, say so and skip to
   step 3.
2. `git -C <wt> merge origin/develop --no-ff -m "Merge origin/develop into feature/<CASE-ID>-<slug> before MR"` plus
   the trailer. Resolve conflicts following `<repo>/.claude/skills/merge-develop/SKILL.md`. If a conflict needs a
   product decision, `git merge --abort` and report.
3. Migrations: if develop added journal entries, renumber this branch's migration with `db:generate`, keep `when`
   above develop's latest, run the drift check, and run `src/db/tests/migration_journal_test.ts`.
4. Builds and tests: the `review-pre-mr` skill's affected-project builds (API, web, admin); every `<program>` and
   sensitive-identifier API test file, through the runner; the **full** web Jest suite with `TZ=UTC` and the **full**
   admin-app Jest suite (not only `<program>` tests); `npx tsc -b` (web) and `npx tsc --noEmit -p .` (admin). Full
   e2e runs in CI; run locally only the e2e specs that touch this case's files, and say so in the report.
5. Checks: brief-common's list (lint, Prettier), with `gitleaks git -m --log-opts="origin/develop..HEAD" --no-banner` (it must scan
   more than 0 commits) and Semgrep with `--baseline-commit origin/develop`.
6. `git -C <wt> diff --stat origin/develop...HEAD`: list any shared path outside the `<program>` areas.

## Report
Merge sha and conflicts (file, how resolved, why); migration changes; test counts per file with log paths; checks;
shared paths; final sha; clean worktree. End with `RESULT_STATUS: complete`.

Main then runs the full API suite at the tip and on `origin/develop`, compares failures by name, and runs the browser check
where the case has a UI.
