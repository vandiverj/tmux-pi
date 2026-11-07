# <CASE-ID> <case title>, step <step>: <what this step does> (lane <lane>)

**Read first:** <C>/briefs/brief-common.md. The tool rule and never-list apply in full: Write/Edit only; no
heredocs, inline scripts, shell arrays or `$(...)`; scoped `git -C <wt> grep -- apps lib docs`; no push, MR, posts,
amend or rebase; no `.env*`, dev DB, `npm ci` or `mise trust`.

**Worktree:** <wt>, branch `feature/<CASE-ID>-<slug>`, lane `<lane>`, base `<base sha>`, HEAD `<sha>` (clean).
**Design:** <C>/research/<case-id>-design.md. **Decisions:** S<N>D<n>, S<N>D<n> in <decision log>.
**Prototype:** <screen and state>. **Regulation:** <REG> §<section>, or none.

## Done means
<One to three sentences: the observable behaviour when this step is finished.> Done when every AC row and every
case-table row has a passing test that failed first, and the Self-review section of your report is filled.

## Acceptance criteria for this step
| AC | What must be true | Test (file to add or extend) |
|---|---|---|
| AC1 | … | `apps/<api>/src/.../x_test.ts` (extend the "…" table) |

## Rule this step enforces
(Required when the step opens, closes, blocks, clears, enrolls, cancels or corrects anything. Otherwise delete.)
<The rule in one sentence.>

| Starting state | Trigger | Expected result | Test |
|---|---|---|---|
| … | … | … | … |

The rows include the opposite action (open/close, apply/correct/undo, block/unblock), a repeat of the trigger, a
later contradicting event, a person outside the usual state (applicant, rehire, never enrolled, ended), and both the
API and the UI for anything the UI offers. If the change needs a row this table doesn't have, stop and send
`tp-agent request` before writing code. Add one test that runs sequences of the operations and checks the rule
after each (an `assertStanding(world)`-style helper, as one earlier step used).

## Guarantee level
(Required for any email, outside service or background job. Otherwise delete.)
<"Best effort, bounded by <deadline>, failure recorded as <event>" or "Guaranteed: <mechanism>".> Anything stronger
than this line is out of scope. For a side effect, write the record first and send after, unless the line says
otherwise.

## Out of scope for this step
- <what later steps or other cases do; name them>
- <anything tempting that is not asked for>

**Owned by another lane or step in flight:** <lane, branch, files or symbols it owns, and the decision that moved
them>, or "none". Don't edit these.

**Stop and send `tp-agent request` if** you must edit a file or symbol listed above, the case table needs a row it
doesn't have, or the change reaches more than <N> files outside "Existing code and tests" below.

## Existing code and tests to read first
- `<file>`: <why>
- `<existing test file>`: <the test or table to extend rather than duplicate>

## Do
1. Tests first: <tests to add or change, as rows in the existing tables named above>. Run them and quote the failure.
2. Change: <files and the change, specifically>.
3. Run the tests again, then the checks in brief-common.

## Tests to run
`<C>/bin/api-db-test.sh <wt> <task id> <files>`; `<jest paths>`.

## Already decided (do not re-raise)
- <decision id or earlier review note: the ruling, in one line. The id is for this brief; code and commit text state the rule in words>

## Commit
`<CASE-ID>: <subject>` with the trailer.

## For the reviewer
- **Scope:** this step's change, `git -C <wt> diff <last closed step sha or lane base>..HEAD` (for a merge step, the
  first parent). Earlier steps' files only where this change reaches them (callers, readers, writers of the same
  state); name that area in Coverage. A defect in earlier code this step didn't cause is a note naming that step.
- **Contract:** judge against AC1..ACn, the case table and decisions <IDs>. A finding outside that list is a note
  or a decision request, unless it breaks something that worked before this step. Items under "Already decided"
  are closed; don't list them again unless the change altered them.
- **Return:** a table `AC or case row | met / unmet / not checked | evidence you checked yourself (file:line, test,
  command)`. For the state rule, mark each cell `ran`, `traced` or `not checked`; a defect names its cell and every
  neighbouring cell with the same cause. Spot-check two lines of the author's Self-review.
- **Closes by:** give each finding `Closes by: code`, `check` (a command, count or log the author must show),
  `decision` or `none`. A missing or wrong item in the author's report, or a command the author didn't run, is
  never a defect: it is `check`, under "Owed before close", with APPROVED_WITH_NOTES. Run it yourself when you can.
  REQUEST_CHANGES needs at least one `code` finding.
- **Existing tests:** flag any new test file or case that could extend an existing one in the same module
  (`Closes by: code`).
- **Run what you can:** `<C>/bin/api-db-test.sh <wt> <your task id> <file>` reaches a suspect cell; say when you
  traced instead of ran.

## Report
As brief-common's Report section, including the Self-review. End with `RESULT_STATUS: complete`.
