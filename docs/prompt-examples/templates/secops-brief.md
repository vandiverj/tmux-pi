# <CASE-ID> security review (secops <fast lane | full sweep>, report only)

**Worktree:** <wt>, branch `feature/<CASE-ID>-<slug>`. Diff: `git -C <wt> diff <base>...HEAD`.
**Why this mode:** <fast lane: the diff touches X / full sweep: the case changes the `<program>` data model (new
table Y, shared helper Z)>.
**Context:** design <C>/research/<case-id>-design.md; decisions <IDs> in <decision log>; lane briefs <paths>.

## Do
Run the `secops` skill (`<repo>/.claude/skills/secops/SKILL.md`) over the branch diff and the code it reaches. Load
`.claude/secops-baseline.yml` first. Pay particular attention to:
- **Identifiers:** can a full SSN-like identifier (outside the reveal route behind `reveal_full_id`), date of birth,
  ID number or name reach a response, log, email, audit or history row, fixture or export? (`S1D1`, `S1D2`)
- **Cross-customer isolation:** can one customer see another's people, results, notices or counts? NULL
  `organizationId`? A combined pool (`<REG> §<section>`)?
- **Permissions:** each new route's `hasPermission`; `<contact-role>`-only actions; `<company>` system admins
  view-only.
- **Email:** recipients and content (log-in prompt only; no names, counts or results).
- **Append-only and integrity:** triggers, the registry entry, no update or delete path.
- **Failure isolation:** can a side effect (email, vendor call) change an HTTP result or roll back a record?
- **Feature flag:** with `<FEATURE_FLAG>` off, nothing is sent or written.
- **Test data:** no real-looking SSNs, names or addresses.
<case-specific items>

## Rules
Report only: don't edit, stage or commit. No push, MR or posts. No MCP database tools, credentials or `.env*`.
`safe_grep` and `safe_read` outside the worktree. No dev servers. No heredocs or inline scripts.

## Deliverable
<C>/research/<case-id>-secops.md: findings ranked most severe first (MUST-FIX, SHOULD-FIX, COSMETIC), each confirmed
with the concrete input or state and the wrong result traced through the code, or marked as a note; file:line and
a suggested fix for each; a Coverage section listing what you read. End with `RESULT_STATUS: complete`.
