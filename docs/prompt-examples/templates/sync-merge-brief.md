# <CASE-ID> sync merge: bring <origin/develop or parent branch> into the staged lane (lane <lane>)

**Read first:** <C>/briefs/brief-common.md. Same tool rule and never-list. Merge only; never rebase, amend, reset or
cherry-pick. Don't push. **Run no tests.**

**Worktree:** <wt>, branch `feature/<CASE-ID>-<slug>`, HEAD `<sha>` (clean). If it is dirty, stop and report.
**Why:** <develop moved to <sha> / <CASE-ID> merged as !NNNN>.

## Do
1. `git -C <repo> fetch origin`. If `git -C <wt> log --oneline HEAD..<merge source>` is empty, say so and stop.
2. `git -C <wt> merge --no-ff <merge source> -m "Merge <merge source> into feature/<CASE-ID>-<slug>"` plus the trailer.
   The source is `origin/develop`, or for a stacked lane the parent's merged state (`origin/develop` once the parent
   has merged).
3. Resolve conflicts only, following `<repo>/.claude/skills/merge-develop/SKILL.md`. Keep both sides' behaviour. If
   a conflict needs a product decision, `git merge --abort` and report the files and both intents.
4. Migrations: if the merge brought journal entries that clash, renumber this branch's migration with `db:generate`
   (`when` above develop's latest) and say so.
5. Check: no conflict markers (`git -C <wt> grep -n -E '^(<<<<<<<|>>>>>>>)' -- apps lib docs` is empty) and
   `gitleaks git -m --log-opts="<old HEAD>..HEAD" --no-banner` scans more than 0 commits and finds nothing.
   `deno check` and `tsc` are not required.

## Report
Merge sha; each conflicted file with how it was resolved and why (file:line); **every hand-fixed hunk listed**, since
a merge with hand fixes gets a quick independent review; migration changes; gitleaks result; final sha; clean
worktree. State that no tests were run. End with `RESULT_STATUS: complete`.
