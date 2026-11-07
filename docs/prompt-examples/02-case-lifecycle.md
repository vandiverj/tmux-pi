# Case lifecycle

Steps 0–10 for each case. Each step lists what to do, what it produces, and when to stop. Commands for workers
live in the brief templates in `templates/`; worker rules live in `templates/brief-common.md`.

## 0. Batch set-up (once per batch)

- [ ] `tp-agent objective "<batch>: take <CASE-ID>, <CASE-ID>… to MR-ready"`. Run `tp-agent index` and read the work
      notes for earlier decisions and handoffs.
- [ ] Campaign folder `<C>` (new or reused) with:
  - `STATE.md`: cases, lanes, status (UNSTARTED, IMPLEMENTED, REVIEWED, INTEGRATED, VERIFIED, ACCEPTED, BLOCKED),
    tip shas, open items, the next decision label, and "rules worth repeating". Update it after every merge and
    every send (an early campaign's file went three days stale);
  - `briefs/<batch>-queue.md`: the work queue and a dated event log (every dispatch, verdict, close, failure);
  - `briefs/<batch>-decisions-<date>.md`: the decision log (`05-decisions-and-deck.md`); labels `S<sprint>D<n>`,
    starting at D1 in a new campaign;
  - `briefs/brief-common.md`: copied from `templates/brief-common.md`, with the campaign paths filled in;
  - `bin/*.sh` and `browser-check/`: copy them from an earlier campaign (`<prev-campaign>`) if the campaign is new:
    the scripts (`*.sh`, `check.mjs`) and `README.md` only, not logs, `results/`, `shots/` or `old-*`.
    `api-db-test.sh` is the runner for case work (named files); `api-db-test-sharded.sh` is the full-API-suite
    runner (`04-tmux-pi-operations.md`); `prototype-gate.sh` comes along with them. Then set
    `CAMPAIGN=<C>` in each runner and its header comment, and in the browser kit set `W=` (`start-api.sh`,
    `start-admin.sh`, `start-web.sh`) and `ADMIN_ROOT` (`check.mjs`) to the worktree under test. Rewrite the README's
    worktree line, `D=` and `cd` paths, and its check command to name `check-<case-id>.mjs`. Check:
    `grep -rn '<prev-campaign>\|wt-<prev-lane>' <C>/bin <C>/browser-check` prints nothing. `check.mjs` is a sample
    scenario; write `check-<case-id>.mjs` per case from it, reusing its sign-in, `go()` and shot helpers;
  - `cases/`, `research/`, `handoff/`, `screenshots/`, `test-runs/`;
  - the deck folder (`research/decision-deck-<round>/`, from `deck/`).
- [ ] Snapshot every case into `<C>/cases/<CASE-ID>.md` (title, description, AC, comments, links, assignee, state).
- [ ] Triage: order the cases by dependency, mark which can run in parallel, and note cases owned by someone else.
      If a case is assigned to someone else, or a branch for it already exists on origin (`git fetch` first), ask
      before starting it.

## 1. Understand the case

- [ ] Read the case snapshot, its comments, and handoff comments that other cases left on it.
- [ ] **Dependency check.** If the case is gated on a decision case (a spike, for example), check now whether its
      questions are answered, and proceed on the state at that time. Don't wait on an unanswered one: record the
      call you build on as a provisional decision (step 2).
- [ ] Read the documents in `01-sources-of-truth.md` that apply.
- [ ] Walk the prototype screens for the case. Note each screen and state for each AC, and every difference from
      the roadmap or the case.
- [ ] Note the regulation sections that apply and check them against the MUST NOT list.
- [ ] Map the existing code: what is built, what is missing, which other cases it touches. Dispatch an
      explorer or researcher when the map spans many files, in parallel with the reading.
- [ ] Run the repo `refine-story` skill on the case to get testable acceptance criteria. Start the **AC trace
      table** in `<C>/research/<case-id>-design.md`: criterion | test (file:line) | e2e/browser check | screenshot.
      It drives the tests, the reviews and the MR's Testing section.
- [ ] List the **shared surfaces** the case may touch: `lib/frontend`, `lib/<product>/*`, `lib/backend`, shared
      layouts and components, auth and permissions, the main web app, shared tables, `package.json` or lockfiles,
      the CI config. All products ship in one container (`<product>`, `<admin-app>` and the other apps in the
      monorepo).

## 2. Design and decisions

- [ ] For anything beyond a small change, dispatch a design researcher (`templates/design-brief.md`). It writes
      `<C>/research/<case-id>-design.md` with: AC status, hook points, mechanism and record shape, regulation and
      prototype per AC, dependencies, a lane plan of 3–5 steps, and open questions each with a recommendation.
- [ ] Force a review of the design by a different provider (`tp-agent review ID --force`). On REQUEST_CHANGES,
      dispatch a revision.
- [ ] Optional for consequential calls: `/counterpoint` on the plan, `/product-fit` for a workflow, API, data
      ownership or permission change, `/threat-model` for auth, permissions, private data or an outside
      integration.
- [ ] A decision is needed when:
  - the prototype, the roadmap and the case disagree;
  - the work adds something in neither the prototype nor the case;
  - a regulatory interpretation is involved;
  - the schema, or a data model shared with other cases, changes;
  - shared code other products use would change (default: add a `<program>`-specific piece instead);
  - the change is hard to reverse.
  Raise these now, not at the MR.
- [ ] Decide each one (standing order 1), record it in the decision log, and add a deck slide with screenshots.
      If it is on the "stop and ask" list in `case-work-prompt.md`, wait for `<owner>`.
- [ ] The plan includes registering every new table and every new or changed typed-JSON column in the schema
      validators file (`apps/<api>/src/db/validators.ts`).
- [ ] Classify the case's risk to set the verification depth:
  - **Low:** cosmetic, isolated, docs. Targeted checks and the normal review.
  - **Medium:** UI workflows, API handlers, new components. Review, meaningful tests, integration checks.
  - **High:** transactions, state transitions, outside-service adapters, cross-module changes. Add failure-case
    tests and an architecture check.
  - **Critical:** authorization, sensitive data, regulatory rules, audit and retention, data integrity. Add a
    focused secops pass, negative and boundary tests, and a decision for every unresolved policy question.
  Risk sets how deep the checks go. It doesn't override routing, model floors or permissions.

## 3. Lane and worktree

- [ ] `git -C <repo> fetch origin`. Pick the base: `origin/develop`, or the parent case branch when the case
      depends on unmerged work. A stacked parent must be develop-synced first.
- [ ] **Which base a case is compared against** (same words in every brief):
  - **Staged (MR-ready) time:** a stacked child's base is the local parent branch (a local ref, no fetch). A case on
    develop uses `origin/develop`.
  - **Send time:** every case, stacked or not, uses `origin/develop` only, after its sync merge. Never a parent
    branch, remote or local. A stacked case's MR always targets develop.
- [ ] `tp-agent lane create` for lane `<case-id>-<short>`, branch `feature/<CASE-ID>-<kebab-slug>`, worktree
      `<repo>-wt-<lane>`. Record the base sha in STATE.md.
- [ ] Always use `git -C <wt>` and absolute paths. A new worktree's `mise.toml` is untrusted; never `mise trust`.
- [ ] Every case is prepared now in its own lane and stays staged locally (not pushed). **Sync merges:** when
      develop moves or a case merges, merge develop into every staged lane (a stacked lane merges its parent's
      merged state) with `git merge --no-ff`, never rebase, as a dispatched step (`templates/sync-merge-brief.md`).
      The step only resolves conflicts and runs **no tests**. A sync merge that needed hand-made conflict fixes gets
      a quick independent review. A sync merge or a post-MR fix is a dispatched step or a child lane, never a hand
      commit (`04-tmux-pi-operations.md`).

## 4. Build through lane steps

- [ ] Split the work into steps (the design's lane plan). **A step owns one rule.** A step that changes the state
      of a person, test, violation or enrollment is its own step with a rule and case table; display, wiring and
      tidy-ups go in other steps. (One earlier step started as "display" and became an eligibility gate over 8
      rounds.) Review notes from one step become their own step or a named follow-up, not extra items in an
      unrelated step.
- [ ] One brief per step at `<C>/briefs/lane-<lane>-<step>.md` from `templates/lane-step-brief.md`. It links
      `brief-common.md` and restates the key rules, because review packets snapshot the brief. Fill in:
  - the rule and case table for any state change, and the guarantee level for any side effect (one earlier
    cancel step took 11 rounds because nobody had said cancel was best effort);
  - the tests to extend, by file and table (never "new file, pattern X");
  - the other lanes in flight that own nearby files or symbols (`tp-agent lane list`, `--touches`, the decisions
    that moved code), so the coder doesn't find out mid-task.
- [ ] Dispatch (`04-tmux-pi-operations.md`). Run independent lanes in parallel.
- [ ] Answer `tp-agent request`s with `tp-agent message`, but never while that worker has a permission prompt
      open.
- [ ] When a worker settles: `tp-agent collect ID`. Read the report, then check the diff, the commit and the test
      logs yourself.
- [ ] `tp-agent review ID` (a different provider). On REQUEST_CHANGES: write a fix brief
      (`templates/fix-brief.md`) with your ruling on each finding, then `tp-agent retry ID --reason ...`. Fix-round
      budget per finding family: see `04-tmux-pi-operations.md`.
- [ ] Before closing: run gitleaks over the step's commits, rerun the key tests if in doubt, then
      `tp-agent lane close`. Carry review notes into the next step's brief.
- [ ] Migrations, when the step needs one:
  - schema change, then `deno task db:generate` (`--custom` for triggers); never hand-edit `_journal.json` or an
    existing numbered migration;
  - number follows develop's latest; `when` above develop's latest and never in the future
    (`src/db/tests/migration_journal_test.ts` enforces both);
  - drift check after committing: `deno task db:check-drift` in `apps/<api>` passes, and
    `git -C <wt> status --short -- apps/<api>/src/db/drizzle` prints nothing. On drift the script leaves
    the generated files behind: delete only the files it listed and restore `meta/_journal.json`, then fix the
    schema or migration with `db:generate`. Never commit a check-generated file;
  - validators registered;
  - SQL, snapshot, journal and schema committed together.

## 5. Verify (Main, when the last step closes)

- [ ] `<program>` API suite at the tip through `bin/api-db-test.sh`, and the web and `<admin-app>` `<program>` jest
      suites.
- [ ] Typecheck, lint, Prettier, gitleaks and Semgrep (commands in `03-security-and-quality.md`).
- [ ] Browser check on throwaway databases (`<C>/browser-check/`): every AC, the error paths, and the screens shared
      with earlier cases. Screenshots to `<C>/screenshots/<case-id>/NN-<desc>.png`. Compare with the prototype.
- [ ] Other-product impact (`03-security-and-quality.md`).
- [ ] Test review against the test standards; repeat the break-it check yourself on one test per AC.
- [ ] Text-truth pass on every new or changed string and comment.
- [ ] Boy-scout rule for failures (`03-security-and-quality.md`).

## 6. Security

- [ ] Dispatch a secops researcher (`templates/secops-brief.md`): the fast lane on the branch diff, or a full
      sweep when the case changes the `<program>` data model.
- [ ] Fix confirmed findings in a lane step, or hand them off as a case (drafted; created only with `<owner>`'s OK).

## 7. Independent review of the whole case

- [ ] Each step already has an approving review. For a case with 3 or more steps, or Critical risk, run one more
      review over the whole branch diff. Give the reviewer the AC trace table, the shared-surface list and the
      decisions, and ask about other-product impact, text truth, unnecessary comments and test value.
- [ ] Anything committed after an approval gets a focused check; the MR says what was checked.
- [ ] Every 3–5 integrated cases, run a cumulative review across them for drift and duplication.

## 8. Pre-MR (staged)

A case is **MR-ready** when every step is closed and independently reviewed, the per-step tests and the case-end
`<program>` suite (with the web and `<admin-app>` `<program>` Jest tests) have passed, and the checks below are
done. It stays staged locally; the full web and `<admin-app>` Jest suites and the full API suite are the send gate
(step 10).

- [ ] Run `/review-pre-mr` as a **review only** (skip its inline security read; step 6 covers it). The staged pre-MR
      brief overrides the skill in three places: skip its builds and full test suites for affected projects (API,
      e2e, web/admin), because the staged case relies on its per-step tests and the `<program>` suite at case end;
      stop at the review report, with no hand-off to `/ship-mr`; and diff base: for a stacked child whose parent
      has not merged, compare against the local parent branch, not origin/develop; the skill's origin/develop
      comparison applies only at send time.
- [ ] Confirm secops (step 6) and the independent review (step 7) are done on the final tree, and the migration
      checks from step 4 pass.
- [ ] Fix anything found in a `pre-mr-fixes` step.
- [ ] Keep the lane current with develop only through sync merges (step 3), without tests.

## 9. Handoff drafts

- [ ] Dispatch a researcher whose task starts with `/case-handoff` (`templates/handoff-brief.md`). Output goes to
      `<C>/handoff/<case-id>/`: the MR text and the `<tracker>` comments, with `!NNNN (pending)` until the MR
      exists.
- [ ] Check every file:line, sha and count in the drafts against the tip before showing them, using the staged-time
      base from step 3. After a sync merge moves the tip, recheck the drafts that cite it; at send time the counts
      are rerun against `origin/develop`.
- [ ] If the branch adds or re-dates migrations, say which local databases need a reset.
- [ ] `tp-agent work note add` for the case: tip, MR (pending), handoffs, open decisions.
- [ ] Main recommends the order in which the cases are sent, by dependency and risk.

## 10. Send (one case at a time, each action with `<owner>`'s OK)

Each case gets its own full (non-draft) MR. The next case is not sent until the previous MR has merged.

- [ ] **Freeze check, first.** Before any push, ask `<owner>` whether a develop freeze is on. Freezes are announced
      ad hoc, so no file shows one. During a freeze nothing is pushed: the work stays local, committed in its lane,
      and the case waits staged until the freeze ends.

- [ ] **Final sync:** `git -C <wt> fetch origin`, then dispatch the send-gate step (`templates/premr-brief.md`): merge
      the latest `origin/develop` (`--no-ff`, never rebase, resolve with `.claude/skills/merge-develop/SKILL.md`),
      recheck migrations, then run the full gate: the `review-pre-mr` skill's build and test steps (not skipped
      now: the affected-project builds, API, web and admin), the `<program>` suite, the full web Jest suite and the
      full `<admin-app>` Jest suite, typecheck (`tsc -b`), lint, Prettier, gitleaks with `-m`, Semgrep, and the
      browser check where the case has a UI. Full e2e runs in CI (`04-tmux-pi-operations.md`); locally run only the
      e2e specs that touch the case's files. The base is `origin/develop` throughout (sync merge, comparisons, diff
      counts), never a parent branch. A stacked case's parent has merged first, and its lane got a sync merge of
      `origin/develop` (which now holds the parent) before this step.
- [ ] Main runs the **full API suite** at the tip and the same suite on develop, then compares the failures by
      name. A failure only on the branch is ours. A failure shared with develop gets a case ID (as one earlier
      shared failure did). One-off "Request has been terminated" timeouts: rerun the file once; if it passes,
      record it as a flake.
- [ ] If the gate moved the tip, recheck the drafts against it.
- [ ] Hand off to `/ship-mr` only with `<owner>`'s OK, after the gate passes.
- [ ] With `<owner>`'s OK: push, MR, screenshots as MR notes, `<tracker>` comments, In Review assigned to
      `<reviewer>`, time logging, follow-up cases (`06-handoff-mr-tracker.md`).
- [ ] Wait for the MR to merge. Then run sync merges into the other staged lanes (step 3) and send the next case.
- [ ] **Red develop.** After a merge, watch develop's pipeline. For a red job, read its `failure_reason` first: spot
      reclaim and `runner_system_failure` are infrastructure, not code. Check whether a later develop pipeline has
      already deployed, and never retry an older deploy over a newer one (two develop merges 9 s apart once deployed
      the admin app together and one failed on `UPDATE_IN_PROGRESS`).
- [ ] Update `STATE.md` after every merge and every send.
