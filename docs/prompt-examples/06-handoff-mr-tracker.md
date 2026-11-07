# Handoff, MR and `<tracker>` comments

Everything here is drafted by Main or a `/case-handoff` researcher, checked against the branch tip, and shown to
`<owner>`. Nothing is pushed or posted without their OK for that case. The auto-mode classifier also denies
`<tracker>` comments until `<owner>` says OK in the session: wait for their message, then post.

## Drafts (`<C>/handoff/<case-id>/`)

Rules for every draft:
- Facts come from the tip. Say if the base moved: staged, a case on develop is checked against `origin/develop` and
  a stacked child against the local parent branch; at send time every case is checked against `origin/develop`
  after its sync merge. Check every file:line with grep at the tip. Each test result names its commit. Write
  `!NNNN (pending)` until the MR exists.
- Plain words. Say that `<owner>` decided and what; never the decision id (S1D1 and the like). Ids are local to
  `<owner>`'s campaign log and deck, and mean nothing to a reader of the case or MR.
- Don't copy any one past MR as a model (one earlier MR was three times the normal length).
- There is no tool to edit a posted `<tracker>` comment, so check the drafts before posting.

### MR title and description

- Title: `<CASE-ID>: <user-facing outcome>`. The MR targets `develop`; a stacked case names its merged parent in
  "Before merging".
- Description, 250–700 words, following `.<git-host>/merge_request_templates/Default.md`:
  1. **Description**: ends "Branch tip <sha>, synced with <base> at <sha>." (`origin/develop` at send time.)
  2. **Related Issue**: Closes / Relates to.
  3. **Type of Change**.
  4. **Reviewer Focus**: 2–5 bullets, each with file:line.
  5. **Before merging**: merge and deploy order, rollback, env and flags, behaviour changes, not-done items; or
     "Nothing to note."
  6. **Migrations**: journal `when`; whether a local DB reset is needed.
  7. **Notes for sign-off**: what `<owner>` decided (the call in words, no decision id), then numbered open
     questions.
  8. **Testing**: AC table (criterion | covered by file:line | status); what ran, with counts and commit; the full
     repo result; failures shared with develop, by name, with their case ID; manual check and screenshots, or "Not
     done: <reason>"; maintenance commits.
  9. **Config / Deployment Impact**.
  10. `%{all_commits}` only for 5 or fewer commits.
  11. Ends with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.

### `<tracker>` comments (`<tracker>-comments/<CASE-ID>.md`)

- First line `# <CASE-ID> <done|handoff> comment (draft, not posted)`; last line `*Draft <date>. Not posted.*` (both
  removed when posting).
- **Done comment** (300–700 words), written so a newcomer can follow it: header; Built; How to verify (set-up,
  steps, "Should be refused"); Decisions and changes from the case; Tests and checks; Known limits and follow-ups;
  Handed off / New cases.
- **Handoff comment** on each touched case (100–300 words): "Handoff from <CASE-ID> (MR !NNNN, tip <sha>):"; what is
  now true; what this case must do; known gaps; corrected AC wording if needed.

## Outward steps, after `<owner>`'s OK

One case at a time: the final sync and full gate (the `review-pre-mr` build and test steps, full API, web Jest and
`<admin-app>` Jest suites, lint, typecheck and the rest) pass first (`02-case-lifecycle.md`, step 10), and the next
case is not sent until this MR has merged. Workers never push. Each action below needs `<owner>`'s OK for that case.

1. Push the branch (`<owner>` pushes, or Main pushes when they say so).
2. `<mr-cli> mr create`, a full MR (no `--draft`), source `feature/<CASE-ID>-<slug>`, target `develop` always (a
   stacked case's parent has merged first and the lane has had its sync merge), assignee `<owner>`, reviewer
   `<reviewer>`. Then `<mr-cli> mr update` with the description. Run `<mr-cli>` from inside the repo (zsh has no
   `timeout`; do not wrap it). Post anchored (line) MR comments with a JSON body, not bracket fields: bracket
   fields posted them as general comments and the copies had to be deleted.
3. Post the screenshots as MR notes.
4. `<tracker>`: post the done comment and the handoff comments; set State to **In Review**; set the assignee to
   `<reviewer>`. Story points can be written only on a User Story; Task and Spike types are read-only (a Spike
   carries none). Sprint values read "`<program>` Sprint N". A "Dev Testing" state exists too.
5. Log time when `<owner>` asks: the total they name, split across the cases as they say, with no description.
6. Follow-up cases: create and link them only with their OK.
7. `tp-agent work note add` with the MR number and tip.
8. Watch the MR pipeline (`<mr-cli> ci list`). If `mr_pipeline_type_guard` fails on a detached pipeline while the MR
   has no conflicts, `<owner>` cancels it and runs a new MR pipeline.
9. Review notes from `<reviewer>`: fix them on the branch through a lane step, rerun step 5, draft replies for
   `<owner>`.
10. When the MR merges, sync-merge the other staged lanes (`02-case-lifecycle.md`, step 3), then send the next case.
