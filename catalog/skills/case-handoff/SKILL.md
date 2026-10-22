---
name: case-handoff
description: Draft the merge request description and the YouTrack done and handoff comments for a finished case, from facts checked at the branch tip. Drafts only; nothing is pushed, created or posted.
---

# Case handoff: MR description and YouTrack drafts

**Drafts only.** Write files. Never push, run `glab mr create/update/note`, call a YouTrack write tool, or edit an
issue. The main agent posts after the user approves the exact text. YouTrack has no comment edit, so a wrong posted
comment costs a correction comment.

Write for two readers: a reviewer who has the diff but not your context, and the user, who signs off. The diff shows
what changed line by line. The text says what the diff cannot: what a user sees, what must merge or deploy first,
what was decided or left out, and how you know it works. Past reviewer questions in this kind of repo were about
merge order, deploy order and rollback, migration ordering, and behaviour changes nobody flagged, not about the diff.

## Inputs (from the task brief; stop and ask with `tp-agent request` if one is missing)
Case id(s) and title; branch, base branch and lane worktree; the output folder; the step reports and review results;
the decisions record; the cases the work touches. Read the case text (cached copy or YouTrack read tools) for the
acceptance criteria.

## Facts come from the tip
1. `git fetch origin <base>` and `git log --oneline HEAD..origin/<base>`. If the base moved, say so in your reply and
   stop; merging it is a separate step.
2. Write the tip commit and the base commit it is synced with. Count with `git diff --shortstat origin/<base>...HEAD`.
3. Make every `file:line` with `grep -n` at the tip. Never copy a hash, line number or count from an earlier draft,
   work note or report.
4. Take test results from the reports, with their commit. Say which commit each run was at; a run before the last
   change is "at <sha>, not rerun", not "passed".
5. A check not run, a failure not investigated, or a decision still open is written down as such, with the reason.

## Style
Plain language: what a person sees or what the system now does. Name things by their product name; a code name only
where a developer must find it. Short sentences, no unexplained abbreviations or internal decision ids, no review
history ("F1 fixed in abc123"), no praise or hedging. Never put a vendor, company or private person's data in the text.

## MR title
`CASE-nn: <what a user or operator gets, in plain words>`. Several cases: list them. If it must follow another MR,
end with `(merge after !NNNN)`.

## MR description
Use the repo's MR template (for example `.gitlab/merge_request_templates/Default.md`) for the heading order. Fill
it; do not leave placeholder sentences, unticked box lists or the generic checklist. Delete a section whose content
would be "none" except where noted. Target 250-700 words; a multi-case MR up to about 1,200. If it runs long, move
detail to the case's done comment and point to the case.

1. **Description** (3-8 lines, then bullets by layer if several: API, web, admin app, migration, emails). Line one:
   what the change does for a user. Then why (the case). End with `Branch tip <sha>, synced with <base> at <sha>.`
   Leave out formulas, long `file:line` lists, and anything with no visible effect.
2. **Related Issue**: `Closes CASE-nn` per case completed; `Relates to CASE-nn` for partial ones.
3. **Type of Change**: tick what applies; delete the rest.
4. **Reviewer Focus** (2-5 bullets): where a mistake would hurt (data writes, migrations, permissions and tenant
   isolation, concurrency, compliance rules). One line each: `file:line` at the tip and the one thing to check.
5. **Before merging** (keep the heading; write `Nothing to note.` if all are false). One bullet each:
   merge order or dependency on another MR; deploy order between apps and what breaks if it is wrong; anything to
   roll back or rerun; new or changed environment variable, schedule, queue or flag, and what happens when unset;
   behaviour that changes for existing users that the case did not ask for; what was deliberately not done and the
   case that tracks it.
6. **Migrations** (only if the branch adds or changes one): number and name, the tables touched, that its ordering
   key (for drizzle, the journal `when`) is above the base's latest and not forward-dated, and which local databases
   need a reset (any that ran an earlier build of the branch or a migration with a higher ordering key).
7. **Notes for sign-off**: decisions made while building, with who made them; open questions, numbered, each with
   the person who must answer.
8. **Testing** (in place of the template's Manual Testing and Test Impact lists):
   - *Acceptance criteria*: a table, one row per criterion in the case: criterion, the test (`file:line`) or manual
     step that covers it, covered / partly / not / out of scope. A "break-it" note only where a change was broken on
     purpose and the test failed.
   - *What was run*: commands or project names, pass/fail counts, the commit, and how the scope was chosen.
     Failures: name each, and say whether it fails on the base (yes only if reproduced there).
   - *Manual check*: what was done in the running app, as which user, and the result; screenshots for UI changes.
     Otherwise `No UI change.` or `Not done: <reason>.`
   - *Maintenance*: fixes the branch made to failures that also fail on the base, in files the branch touches
     (formatting, a broken test, a shared skill), one line each with the commit. A failure left alone gets its case.
   - *Not done*: every skipped check, with the reason.
9. **Config / Deployment Impact**: `No config, CI or deployment impact.`, or point to "Before merging".
10. Keep `%{all_commits}` only for about five commits or fewer.
11. End with the attribution line the session gives for pull requests, if there is one.

## YouTrack drafts
One file per case in `<output folder>/youtrack-comments/CASE-nn.md`. First line
`# CASE-nn <done|handoff> comment (draft, not posted)`; last line `*Draft <date>. Not posted.*`. Leave the MR number
as `!NNNN (pending)` until it exists.

### Done comment on the case built (300-700 words)
1. One line: branch, tip, base sync, MR, and the state asked for (for example Ready for review).
2. **Built**, in user terms: bullets of 1-2 sentences; `file:line` only where the next developer starts (about 8 at
   most).
3. **How to verify**: set-up (app, user, data), numbered steps with what to expect, then **Should be refused**.
   For a change with no screen, say so and point to the tests.
4. **Decisions and changes from the case**: each with who decided.
5. **Tests and checks**: test files, run commands and counts at their commit; what was not run.
6. **Known limits and follow-ups**, with case numbers.
7. **Handed off** and **New cases**, by number.

### Handoff comment on a touched case (100-300 words)
Written so its owner can act without opening the MR. One per case; not a copy of the done comment.
1. `Handoff from CASE-nn (MR !NNNN, tip <sha>):` and one sentence on why it matters to this case.
2. **What is now true** in the code, with `file:line` at the tip.
3. **What this case must do or watch for**: actions, each testable.
4. **Known gaps**, with where.
5. If the case text or acceptance criteria are now wrong, the corrected wording (to replace, not to append).

## Before handing over
- Every claim has a command, a test or a `file:line` at the tip behind it; nothing was copied from an old draft.
- The sync line names the base commit actually merged in.
- Every skipped check, known failure and open decision is written down.
- The user can answer each sign-off question without opening the code.
- Nothing was pushed, created or posted.

Reply with the list of draft files, the tip and base commits used, anything you could not confirm, and the questions
for the user.
