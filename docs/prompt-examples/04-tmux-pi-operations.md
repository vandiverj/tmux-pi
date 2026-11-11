# tmux-pi operations

Main runs everything through `tp-agent` (in `~/projects/tmux-pi/bin`). Workers run in visible tmux panes. Outside a
tmux-pi pane, prefix commands with `TP_STATE_DIR=<repo>/.tmux-pi/sessions/<session id>`.

## Lanes and steps

- A **lane** is one branch in its own worktree, with a declared set of paths it touches. Create it with
  `tp-agent lane create <lane> --base <base> --branch feature/<CASE-ID>-<slug> --touches <paths> --domain "<why it is
  independent>"`, or let the first `tp-agent dispatch --lane <lane> ...` create it with the same flags.
- A **step** is one slice of the lane's work: `tp-agent dispatch --lane <lane> --step <step> --role coder --task
  '<one line>' --context <brief> --deliverable '<what to report>'`. Put the task text in single quotes; keep the
  detail in the brief.
- One writer per lane at a time. A dispatch to an occupied lane is refused, not queued; wait for the worker to
  settle.
- Lanes that overlap on paths get a warning. Rethink the split unless the overlap is deliberate.
- Every commit on a lane is made by a dispatched step, including forward merges and fixes. Main never commits on a
  lane directly; the lane guard refuses the next dispatch if it does. A merge of develop into a lane and any fix
  after the MR exists are dispatched steps too. If Main already did one by hand, tp-agent refuses to dispatch
  ("changes since step closed") and may propose `reset --hard`; never reset a pushed branch. Route:
  `tp-agent lane create <name> --base <lane branch>`, dispatch the work there, then fast-forward the parent branch to
  the child's tip. Reason: an earlier case's review fixes ran this way (`lane/<case-id>-review-fixes`, based on the
  case branch, closed at `<sha>`).
- **Staged lanes and sync merges.** Every case is prepared in its own lane and stays local until it is sent. When
  develop moves or a case merges, dispatch a sync-merge step (`templates/sync-merge-brief.md`) on every staged lane:
  `git merge --no-ff` of `origin/develop` (a stacked lane merges its parent's merged state), never a rebase. It only
  resolves conflicts and runs no tests; the full gate runs once, on the case about to be pushed, against
  `origin/develop` only (`02-case-lifecycle.md` step 3 has the base rule). A sync merge that
  needed hand-made conflict fixes gets a quick independent review. Staged `/review-pre-mr` is a review only (no
  builds or full suites, no `/ship-mr`); the skill's full steps run at send time. Workers never push.
- Close a step with `tp-agent lane close <lane> <step>`. It needs a review that approved the exact committed
  tree. It finds lanes in the current directory's repo, so close a lane in another repo (for example tmux-pi)
  with `cd <that lane's worktree> && tp-agent lane close …`.
- Lane records live in `<repo>/.git/tmux-pi/lanes/<lane>/`, not in `.tmux-pi/lanes.json`. Remove a lane with
  `tp-agent lane remove`. If a worktree and branch were already removed by hand, archive the leftover record
  afterwards (it moves to `lanes/archive/`): a hand removal once left a stale record behind.

## Reviews

- `tp-agent review ID` picks a reviewer from a different provider (GPT for Claude work). Use `--force` to review
  when policy says none is needed (designs, docs that set rules).
- If no other provider is available, stop. A same-provider review needs
  `--same-provider --approved-by-user "<owner's exact words>"`. Keep a list of other-provider reviews owed and run
  them when that provider is back.
- The reviewer's first line is the verdict: `APPROVED`, `APPROVED_WITH_NOTES` or `REQUEST_CHANGES`, and the reply
  must have a Coverage section.
- The step brief's "For the reviewer" section is the reviewer's contract (the packet snapshots the brief): scope
  is the step's own diff, judge against the AC list and case table, return the AC/cell table, label each finding
  `Closes by: code | check | decision | none`, and ask for changes only for `code`.
- The reviewer brief says: "Do not read `.env*` files. The evidence for DB-backed behaviour is the boot outcome and
  pass counts of the integration tests in the coder's report." A reviewer that asks for the file gets denied and the
  review stalls (`review-<task-id>`).
- REQUEST_CHANGES blocks that tree even if a later review approves it. Fix it with a new commit and review again.
- **Keep reporting gaps out of REQUEST_CHANGES.** In one early campaign about 37 reviews asked for changes only
  because a count, log path or command was missing from the coder's report. tmux-pi treats every REQUEST_CHANGES as
  a rejection of that tree, so these stalled lanes. Prevent them before the review starts:
  1. **Report gate before every review.** Before `tp-agent review`, Main checks the coder's report against
     brief-common's Report list: each AC mapped to a test, counts copied from the runner's `exit=... log=...` line
     with the log path, failing-first output, the timer grep, check results, the commit sha, the Self-review and
     `RESULT_STATUS: complete`. Where something is missing, Main runs the check itself (the runner, the grep, the
     linter) and sends the output to the coder with
     `tp-agent message ID 'Add this to your report under <section>; change no code: <path to output>'`. Main
     dispatches the review only when the report is complete.
  2. **The reviewer contract** in the brief: a missing or wrong report item is `Closes by: check`, never a defect,
     and REQUEST_CHANGES needs at least one `code` finding.
  3. **tmux-pi enforcement** (proposal P1–P3 in `<C>/research/process-improvement-proposals.md`, not built yet):
     a REQUEST_CHANGES without a `code` finding is read as an unusable review rather than a rejection, so a fresh
     review of the same tree can approve it.
  Until P1–P3 land, a REQUEST_CHANGES that has no `code` finding and still gets past 1 and 2 needs `<owner>`'s owner
  override (below). Log every one as a gate miss in the queue log, so the gate can be tightened. Never dispatch a
  coder only to change the tree so a new review passes (an earlier case's steps 7b–7d did exactly that).
- **Notes (APPROVED_WITH_NOTES).** Close the step, then give each note one line in the queue log:
  - `fold`: add it to the lane's next step brief (an existing step, not a new one);
  - `decide`: a decision entry and deck slide, no code now;
  - `follow-up`: a named follow-up case or the case-end review;
  - `drop`: why.
  Add the folded, decided and dropped notes to the next brief's "Already decided" list so the next reviewer
  doesn't raise them again. No coder step for notes alone unless a note shows a wrong result with a concrete
  case; then it is a defect, and goes back to the reviewer as one.
- Fix rounds: fix brief (`templates/fix-brief.md`) linking the review result path, with Main's ruling on each
  finding and "new commit on top of <sha>; never amend". `tp-agent retry ID --reason '...'`.
- **Round budget per finding family** (the reviewer names the family when a finding repeats one from an earlier
  round). Raising effort doesn't fix a family that keeps returning.
  - First return: a normal fix brief.
  - Second return: the fix brief's "rule first" form. The coder (or the reviewer, asked for the full matrix)
    writes the rule and full case table for the family with no code, Main checks it, then the coder fixes every row
    and adds the sequence test that checks the rule after each operation.
  - Third return: no fourth fix round on the same call sites. Change the design (an architect or researcher writes
    the model, as two decisions did for one earlier step) or park it for `<owner>` with the findings.
  `--escalate` only when the round failed for capacity (a large diff the model lost track of), not for a family.
- **Owner override.** When `<owner>` accepts a tree despite REQUEST_CHANGES (for example, the finding is a
  decision they made), they run this in their own terminal:
  `TP_STATE_DIR=... tp-agent lane close <lane> <step> --override-review --reason '<why>'`. It asks them to
  type a code back. Main can't run it; give them the exact command.

## Workers and prompts

- Never approve a worker's permission prompt. Bring it to `<owner>` with what it wants and your assessment.
- Never `tp-agent message` a worker whose prompt is open: the message's Enter approves it. To deny, send
  `tmux send-keys -t <pane> Escape`, then message the safe alternative.
- Most prompts come from shell forms. The brief rules prevent them: no heredocs or inline scripts, no shell
  arrays or `$(...)`, and content searches through `safe_grep` (pattern, path), reads through `safe_read` or
  the read tool on an exact path (a shell `grep`/`rg` only on exact, non-sensitive paths in the tree). The worker contract gives every worker this rule; a brief need not repeat it. A
  repo-root recursive `rg` once stopped a code reviewer (`code-reviewer-<task-id>`) at a prompt.
- A read-only worker started from the main checkout on a lane's diff (another worktree) hits sensitive-read prompts
  and is denied. Run that review or research as a lane step, or give it the diff as a file.
- `tp-agent request` from a worker: answer with `tp-agent message ID '<answer>'`. Facts from `.env` files are
  answered as yes/no or names only, never values.
- When a worker reports a failing test it doesn't own, tmux-pi asks for
  `tp-agent dispatch --investigate-failure ID`. It runs in its own scratch worktree on a clean base, so the lane
  stays free.
- `tp-agent collect ID` gives the report and result; check the claims yourself before closing.
- The router picks model and effort. Don't pass `--model` or `--thinking` without a reason; above `high` needs
  `<owner>`'s approval (`--approve-effort`).

## The API DB test runner

- `<C>/bin/api-db-test.sh <wt> <task-id> <test files relative to apps/<api>>`. One plain command per run.
  Use `.` for the full API suite.
- Each worktree gets its own database (`<db>_test_<lane>_test`) behind a lock; exit 75 means the lock is busy, so
  retry later. The runner applies the worktree's migrations, runs the files, drops the database, and restores
  `deno.lock`, `deno.json` and `deno.test.json`.
- Never run `deno task test` directly, never create or connect to a database yourself, never touch the dev DB.
- `<C>/bin/api-db-test-sharded.sh <wt> <task-id> [shards]` is the **full-API-suite** runner (case work uses
  `api-db-test.sh` on named files). It runs the whole suite as concurrent shards the way CI does (default 4, max 6;
  the repo's own partition from `scripts/run_sharded_tests.ts`), against per-worktree databases
  `<db>_test_<lane>_s<i>_test`, and takes the same per-worktree lock, so it never overlaps a single-file run there
  (exit 75: lock busy). It prints one result line per shard and ends with `exit=… log=…`. Use it for the send gate's
  full API run (`02-case-lifecycle.md`, step 10).
- A worker that cannot reach a DB or workflow-engine test runner does not read `.env.test`. Main runs the campaign
  runner for it, or the brief names the runner up front. Workflow-engine tests run with
  `deno test --env-file=.env.test …` (pass the path; never open it). One earlier coder (`coder-<task-id>`) ended
  `RESULT_STATUS: incomplete` for want of a runner.

## Browser checks

`<C>/browser-check/` (`up.sh`, a per-case `check-<case-id>.mjs`, `down.sh`; see its README) runs `<product>` and
`<admin-app>` on throwaway databases (`<db>_browser_a`, `<db>_browser_b`). Screenshots go to `shots/` and then to
`<C>/screenshots/<case-id>/`. Full e2e (Playwright with the auth provider) runs only in CI.

- Main runs the kit; workers never do. Point it at the worktree under test first (`02-case-lifecycle.md`, step 0).
- The start scripts load database URLs and API keys from `.env` files inside the script (dotenv-cli, or a `grep`
  into an `export`) and refuse any database but the throwaway ones. Main runs them as they are and never reads the
  files, echoes a value or edits the loading lines. A script that would print a value is a defect: stop and report.

## Housekeeping

- `tp-clean` archives tasks whose pane is gone. Lane gates still read the archive, so archiving is safe.
- Exited panes must be closed before `tp-clean` will archive them. Closing many at once is something `<owner>` runs
  themselves; give them a script.
- Never touch another session's workers, locks or lanes.
