# <CASE-ID> case handoff inputs (follow the case-handoff skill)

Dispatch as a researcher whose task starts with `/case-handoff`.

**Skill:** `~/projects/tmux-pi/catalog/skills/case-handoff/SKILL.md`. Read it first and follow it exactly. Drafts
only.

- **Case:** <CASE-ID>, <one sentence on what it delivers>. The MR closes <CASE-ID>.
  - Read the `<tracker>` text with read tools only (`get_issue`, `get_issue_comments`).
  - Touched cases that each get a handoff comment: <CASE-ID (why)>, …
- **Branch, base and worktree:** `feature/<CASE-ID>-<slug>`, base `<origin/develop, or the local parent branch>`.
  Worktree <wt> is read-only for you. Tip `<sha>`; synced with the base at `<sha>`. Staged locally and not pushed, so
  write `!NNNN (pending)`; the MR will be a full MR, not a Draft, and it targets develop.
- **Base for the skill's steps (this brief wins over the skill where they differ).** The skill runs
  `git fetch origin <base>`, `git log --oneline HEAD..origin/<base>` and `git diff --shortstat origin/<base>...HEAD`.
  - **Staged (MR-ready) time:** a case on develop uses `origin/develop` as the skill says. A stacked child's base is
    the local parent branch (`<parent branch>`, a local ref in the parent lane's worktree): skip the fetch and use
    that branch in place of `origin/<base>` in the log and the shortstat.
  - **Send time** (rerun of the drafts' counts): every case, stacked or not, uses `origin/develop` only, after its
    sync merge. Never a parent branch, remote or local.
  - If `git log --oneline HEAD..<that base>` is not empty, say so and stop, as the skill says; Main merges the base
    in a sync-merge step and dispatches the handoff again.
- **Output folder:** <C>/handoff/<case-id>/: `mr-title.txt`, `mr-description.md`, `<tracker>-comments/<CASE-ID>.md`
  (done), and one handoff comment file per touched case.
- **Decisions:** <decision log path>, IDs <list>, all signed off (or name the ones still provisional). The ids are
  for you to find the text. In every draft write each as "decided by `<owner>`" plus what, never the id.
- **Step history:** every <CASE-ID> line in <C>/briefs/<batch>-queue.md; result files under
  <repo>/.tmux-pi/sessions/<session>/tasks/<id>/ (or `archive/`).
- **Test results to cite, each with its commit and log path:** case-end `<program>` suite; send-gate step; full repo
  suite and the develop comparison (failures shared with develop by name, with their case ID); flakes rerun. If the
  send gate has not run yet, write those lines as "pending the send gate" for Main to fill in.
- **Screenshots:** <C>/screenshots/<case-id>/ (list them), or "Not done: <reason>".
- **Follow-ups and known limits:** <list with case IDs>.
- **Merge order (for "Before merging"):** <stack order, or "targets develop directly">. Migrations: <none / which, and
  which local DBs need a reset>.
- **Rules:** no `.env*` or credential reads; no SSN data or identifiers in any text; `safe_grep` and `safe_read`
  outside the worktree; create files only with the Write tool. End with `RESULT_STATUS: complete`.
