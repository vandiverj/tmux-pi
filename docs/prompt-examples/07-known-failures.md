# Known failures and the rules that prevent them

From early campaign logs (`<prev-campaign>/briefs/round3-queue.md` and later rounds) and session history. When a new
failure recurs, add a row.

| What went wrong | Rule now |
|---|---|
| Workers stopped at permission prompts nobody answered (repo-root `rg`, heredocs, `python3 -`, `node -e`, shell arrays, `$(...)`, reads of sensitive paths) | The tool rules in `templates/brief-common.md`, in every brief before dispatch. Scoped `git -C <wt> grep -- apps lib docs`; `safe_grep` / `safe_read` outside the worktree. |
| Main approved a worker's prompt by messaging it (twice) | Never message a worker with an open prompt. Deny with Escape, then message. Never approve; escalate to `<owner>`. |
| A review asked for changes only because the report was incomplete (no failing-first quote, no per-file counts, no log paths) | Report requirements spelled out in every brief; every count cites its log. |
| Failures from stale fixtures in an upstream lane were blamed on the step | Classify as not caused by the step; the fix arrives by merge. Check develop first (boy-scout rule). |
| Shared test DB: stale lock and collisions across lanes | One DB per worktree through `bin/api-db-test.sh` (decisions `S1D5`, `S1D6`); exit 75 means retry. |
| A worker started the full suite in the background and finished; the run died and its lock stayed, so Main's next run waited 30 minutes and gave up | Workers run tests in the foreground (worker preamble). Before Main reruns, check the lock owner: if it is a finished worker of this session and no `api-db-test`/`deno test` process is alive for that worktree, remove that lock and log it. |
| `deno.lock` and `deno.test.json` changed after test runs | The runner restores them; workers check `git status` before committing. |
| A rebase broke lane tracking | Merge, never rebase. Fixes are new commits, never amends. |
| Main committed on a lane outside a step; the next dispatch was refused | Every commit on a lane, including merges, is a dispatched step. |
| The close guard refused a step after REQUEST_CHANGES on an unchanged tree | Fix and re-review, or `<owner>`'s owner override (`04-tmux-pi-operations.md`). |
| gitleaks scanned 0 commits on a merge range | Use `-m`; require more than 0 commits scanned. |
| Migration number clash at merge | Renumber with `db:generate`; compare SQL; check journal order and prevId; `when` above develop's latest. |
| Wrong model in the commit trailer | The trailer names the model the worker actually runs as. |
| Test cleanup deleted or truncated globally | Test cleanup deletes only its own rows (`S1D4`). |
| Date bugs: UTC versus the business-time-zone day, DST, jest failing in a non-UTC zone | Show the business-time-zone day; web jest with `TZ=UTC`; test evening, midday and both DST changes. |
| Podman VM clock drift after sleep caused time-based test failures | `chronyc makestep` in the VM before trusting a time failure. |
| Another provider's usage limits stopped reviews | Same-provider review only with `<owner>`'s quoted OK; keep a list of reviews owed; retry hourly. |
| Main ran git in the wrong checkout | Absolute paths and `git -C` everywhere. |
| "Request has been terminated" in full runs. Cause: a test calls `superoak(app)` directly; superoak picks a random port and about 1 in 2,500 is one Deno's `fetch` refuses. Develop has the same flake. | Rerun the file once; if it passes, record a flake. New and changed route tests use `openTestClient` from `routes/tests/helpers.ts`, never `superoak(app)`. The older files are a separate maintenance case (patch: `<C>/research/superoak-open-test-client-fix.patch`). |
| An early campaign added timer waits in 14 test files (`later()` sleeps for a later `now()`; a 200 ms sleep to make two requests overlap; a 50 ms sleep before assuming a row exists, which became a pipeline flake after a reviewer filed it as a note) | No fixed waits (`03-security-and-quality.md`); the timer grep in every lane's checks; a timer in a test the diff adds is a defect, never a note. |
| New test files and helpers where an existing table or helper fitted (briefs said "new file, pattern X"; `later` written four times) | Find the existing test and helper first; briefs name the test to extend. |
| Fix chains of 6–11 rounds (a cancel flow, an eligibility gate, a relink): each round the reviewer found the next case of a rule the brief never stated; the cancel guarantee level was settled only at round 9 | Rule, case table and guarantee level in the step brief; one rule per step; round budget per finding family (`04`). |
| Reviews asked for changes the coder could have seen in its own diff (UI offered what the API rejects, readers not updated, stale text, wrong counts) | The Self-review section in every report, spot-checked by the reviewer. |
| A worker started the full API suite in the background, then settled; the run was killed ("Terminated: 15") and the report said "incomplete" | Workers run tests in the foreground, split by folder if long. Main runs the full suite. |
| REQUEST_CHANGES for reporting gaps (counts, log paths, commit count) blocked lane close; coder rounds were dispatched only to change the tree | Prevent it: Main gates the report before dispatching the review and has missing evidence added with no code change; the reviewer contract makes report gaps `check`; tmux-pi P1–P3 (proposed) make a no-code REQUEST_CHANGES unusable instead of a rejection. Log any that still get through (`04`). |
| Notes became chains of notes-only coder steps, and the same three notes were repeated in 7 rounds of one step | Note dispositions (fold, decide, follow-up, drop) in `04`; the "Already decided" list in the next brief. |
| Step reviews covered the whole lane (37 files for one step, 150+ for a merge), so closed steps were re-reviewed every round | The brief scopes the review to the step diff (`For the reviewer`). |
| UI text described unbuilt behaviour, or a wrong status ("Roster complete") | Text-truth pass before the MR. |
| Scope surfaced late at the MR (an extra card, an extra history table) | Raise anything not in the prototype or case at the design step. |
| 19 new tables had no validators | The plan includes `validators.ts`; CI `migration_test` checks it. |
| Posted `<tracker>` comments had errors | Check drafts against the tip before posting; there is no edit tool. |
| Two branches passed alone and failed together once combined | Each case ships as its own MR after the previous one merged; the send gate runs the full suite on the latest develop; fix in new commits naming both cases. |
| A dispatch to an occupied lane was refused, not queued | Wait for the worker to settle, then dispatch. |
| Bulk tmux pane closes were denied by the safety classifier | Give `<owner>` a script to run themselves; don't work around the denial. |
| Built-in Agent tool used for research instead of tmux-pi | All subagents go through `tp-agent dispatch` in visible panes. |
| Main merged develop into a lane and fixed CI by hand; tp-agent refused to dispatch ("changes since step closed") and proposed `reset --hard` on a pushed branch | Sync merges and post-MR fixes are steps, or a child lane (`04`). Never reset a pushed branch. |
| Worktrees and branches removed by hand left lane records in `.git/tmux-pi/lanes/` | `tp-agent lane remove`; archive leftovers (`04`). |
| Two develop merges 9 s apart deployed the admin app together; one failed `UPDATE_IN_PROGRESS`; the deploy jobs have no `resource_group` | Check for a later deploy; never retry an older one (`02` step 10). |
| Spot reclaims failed jobs that were not code failures; root `default.retry` is not applied inside the child pipelines | Read `failure_reason` before touching code (`02` step 10). Retry gaps go to a DevOps case, drafted for `<owner>`'s OK. |
| Prettier moved a `// timer-ok:` comment off its line; a stale allowlist entry failed `repo_guards` | One-line timer call; shrink the allowlist in the same commit (`03`). |
| Workflow-engine tests could not boot without `.env.test`; a worker stopped `incomplete` | Main runs the runner, or the brief names it; pass `--env-file=.env.test` without opening it (`04`). |
| A reviewer from another provider asked to read `.env.test` and was denied | The reviewer brief names the boot outcome and pass counts as the evidence (`04`). |
| A reviewer asked for an existing test to be extended where the coder had added a new one | "Existing tests" line in the reviewer contract (`templates/lane-step-brief.md`). |
| A commit lacked the Co-Authored-By trailer; the coder cannot amend | Put the trailer text in the brief's commit instruction; a miss gets a note, never an amend. |
| A worker dropped a stash by an off-by-one index | Workers do not use `git stash`; the brief says so. |
| Read-only reviewers on a lane diff from the main checkout got sensitive-read denials | Run as a lane step or give the diff as a file (`04`). |
| The plugin's `failing_tests` flag fired on prose ("none.", "does not own") several times | Write the "Failing tests" section as a literal list or the single word `none`. |
| `<mr-cli>` bracket fields posted anchored MR comments as general ones | JSON body (`06` outward step 2). |
| Story-point write refused on a Task and a Spike | Points only on a User Story (`06` outward step 4). |
| Auto-mode classifier denied `<tracker>` comments until `<owner>` said OK | Wait for their OK in the session, then post (`06`). |
| The heavy e2e job is the merge-train critical path (about 1,800 s); about 900 s sits in a loading-indicator probe in the e2e helpers; `retries: 2` hid a flaky test | A test-speed change is proved by a CI e2e run with no retries. No local Playwright runs. |
| A decision id (D-number) was quoted in outward text and meant nothing to the reader; numbering was global and went stale (the template quoted a number the log had long passed) | Ids are local to `<owner>` and restart per campaign as `S<sprint>D<n>`; outward text says what was decided (`05`, `06`). |
| A push during a develop freeze | Ask about a freeze before every send; during one, work stays local (`02` step 10). |
