# Security and quality gates

## Domain rules

These are an example set for a regulated program that holds sensitive personal data. Replace them with your own
program's rules; keep the shape (each rule is short, has a decision label, and names the test that guards it).

**Sensitive identifier (SSN-like) and identity**
- The identifier is encrypted with the program's crypto module (`apps/<api>/src/crypto/<program>.ts`). The browser
  sees the last four digits by default. The one exception is the authorized reveal (decision `S1D3`): a
  `<program>:sensitive:reveal_full_id` permission held by a short list of roles (not managers, not `<company>`
  system admins), through the reveal route and the shared `Revealable` component, which hides the value after 60
  seconds and caches nothing. The full identifier never appears in any other response, or in a log, email, fixture
  or screenshot.
- Server-side decrypt is allowed only for import, bounded dedupe and the reveal (one record per request, after the
  permission check). No bulk decrypt. No blind index without a secops decision.
- **`S1D1`:** no per-reveal access log.
- Test data uses fake `9xx` identifiers; screenshots use the existing fake. Never real people.

**Audit and history**
- **`S1D2`:** audit and history rows that link to the person never copy identifiers the record already holds.
- One generic append-only table, `<program>_audit_events`, written by `recordAudit(tx, …)` in the same
  transaction, with before and after values through the redaction allowlist (`db/<program>Audit.ts`). Don't add
  a second audit table.
- Append-only tables are protected by row triggers and BEFORE TRUNCATE triggers, through the shared registry
  and `withAppendOnlyTruncateAllowed`.
- Corrections are new records with a reason and how they were verified. No document upload.
- **`S1D4`:** test cleanup deletes only its own rows.

**Access**
- Roles: `<program>::{super_admin, admin, manager, <contact-role>}` (`apps/<api>/src/permissions.ts`).
  `OWNER_ONLY_PERMISSIONS` belong to the `<contact-role>`. `<company>` system admins are view-only in a customer's
  program.
- Every view and query is scoped to the customer organization; NULL `organizationId` must never widen it; no
  visibility across customer groups (`<REG> §<section>`).
- Each endpoint has `[ACCEPTS]` and `[REJECTS]` tests: reject admin, manager and another org's super admin where
  the rule says so.

**Email**
- Notices to the `<contact-role>` only ask them to log in: no names, counts or results. Pool alerts go to `<company>`
  only. No emails to the people the program covers. The from address is configuration. The email log stores nothing
  sensitive.

**Regulation**
- Results are write-once; nothing is altered or deleted before its retention floor. `<company>` staff never make
  the customer's decisions. Only verified results count. Selections are never announced in advance.

## Security review

| Tool | When | How |
|---|---|---|
| `secops` repo skill, fast lane | Every case, on the branch diff | `templates/secops-brief.md`. Engages when the diff touches a sensitive column, a jsonb/text blob holding a domain type, object storage, an outbound payload, a body log, or a masking or permission helper. Loads `.claude/secops-baseline.yml` first. |
| `secops` full sweep | The case changes the `<program>` data model | Same brief, full sweep: inventory, propagation ("find the second copy"), egress. |
| `/threat-model` (tmux-pi) | Auth, permissions, private data or an outside integration | Report only. It doesn't replace the independent review. |
| Reviewer roles `vulnerability-reviewer`, `data-flow-reviewer` | Critical-risk steps | `tp-agent dispatch --role vulnerability-reviewer` (or `data-flow-reviewer`) with the step's commit range, in addition to `tp-agent review`. |

Severity is MUST-FIX, SHOULD-FIX or COSMETIC, plus how reachable it is. MUST-FIX blocks the MR. Known gap
(CONVENTIONS §8.1): `sendErrorResponse` serializes the raw error and user object; don't widen it.

## Manual gates (CI doesn't run all of these, and lanes run with the commit hook off)

Run them on the changed files or the branch range. Each must pass, or every finding must be explained, before a
step closes and again in the send gate.

| Gate | Command | Notes |
|---|---|---|
| Prettier | `npx --no-install prettier --check <changed files>` | Never `deno fmt`. |
| ESLint (web, admin, libs) | `npx nx lint <web-project>`, `npx nx lint <admin-project>`, `npx nx lint <lib>` | Not in CI. React Compiler rules are warnings (`<CASE-ID>`); new code must not add new warnings in the files it touches. |
| Deno lint (API) | `deno lint <changed API files>` | `src/main.ts` has a pre-existing error. |
| Typecheck | API `deno check --allow-import --config apps/<api>/deno.json <files>`; web `npx tsc -b`; admin `npx tsc --noEmit -p .` | Without `--config`, a repo-root deno check gives import-map errors. |
| gitleaks | `gitleaks git --log-opts="<base>..HEAD" --no-banner` (add `-m` over merges; it must scan more than 0 commits) | Never add a real secret to `.gitleaks-baseline.json`. |
| Semgrep (code scan) | `semgrep scan --config p/typescript --config p/nodejsscan --config p/jwt --config p/secrets --metrics=off --baseline-commit <base> --json-output=<C>/test-runs/<lane>-semgrep.json apps lib` | Not in the repo or CI; installed locally. `--baseline-commit` reports only new findings. Triage each one in the report: real, or a false positive with the reason (literal regex flagged as ReDoS, fake keys in tests). |
| Dependency scan | `osv-scanner scan source -r <wt>` and `npm audit --omit=dev` | Only when `package.json` or a lockfile changes, which itself needs `<owner>`'s OK. |
| Migration journal | `src/db/tests/migration_journal_test.ts` through the DB runner | When a migration is added or re-dated. |

## Test standards

A test exists to fail when the behaviour it covers breaks. The number of tests is never the measure.

- **Every test traces** to an AC row or a named risk (boundary, permission, fixed bug). A test with neither is
  deleted or justified.
- **Assert the observable outcome:** status and body, DB rows, audit events; visible text and state in the UI. Not
  that a mock was called or a value the test set up.
- **Exact expected values**, not `toBeDefined()`, truthiness or snapshot dumps.
- **Test the edges the rule defines:** effective dates, empty sets, pending drafts, permission refusals, the
  business time zone's day boundary and both DST changes for anything dated.
- **Find the existing test first.** Before writing a test, search the test files with `safe_grep` (pattern
  `<function, route or component>`, path the test directory, glob `*.test.ts`). If a table, loop or `describe` covers it, add a row or case
  there. A new file only when none covers it (or the file is over 3,000 lines: then a sibling file named for the
  behaviour, importing the shared world helper). The report says "Existing test: file:line" or "None (searched:
  terms)" for each behaviour. Briefs never say "new file"; they name the test to extend.
- **Search (`safe_grep`) for a helper before writing one** (`waitFor`, `later`, fake clock, date stub, world builder). An early
  campaign wrote `later` four times.
- **Lowest layer that proves it, once.** Table-driven cases for one rule. One DB per file, not per test.
- **No fixed waits.** Never `setTimeout`, `sleep` or `waitForTimeout` to wait for something. Wait on the thing:
  - background work: await its promise, or poll a condition with a deadline (`waitFor` in
    `tests/helpers/waitFor.ts`) that gives up at the deadline;
  - UI: `findBy*` / `waitFor` on the visible result, Playwright locator assertions; debounces with fake timers;
  - time-dependent code: `FakeTime` / `jest.useFakeTimers` or the date stub;
  - "row B is later than row A": pass the timestamp or use the clock stub, never sleep for a later `now()`;
  - two requests that must overlap: hold the first on a latch (a promise the test resolves) until the second has
    started.
  If the code has no hook for this (injectable clock, resolvable promise), add one. A raised test timeout needs a
  comment saying what is slow. Check: `git -C <wt> diff <base> -U0 -- '*_test.ts' '*.test.ts' '*.test.tsx'
  '*.spec.ts' | grep -nE '^\+[^+].*(setTimeout\(|waitForTimeout\(|\bsleep\()' | grep -v 'timer-ok'` is empty,
  or each line carries `// timer-ok: <reason>` (mock latency, a `Promise.race` guard). Prettier can move a
  `// timer-ok: reason` comment off the line it excuses: write the timer call on one line so the comment stays on
  it (`.githooks/no_fixed_test_waits_test.ts` matches `// timer-ok: <text>`). The allowlist
  `.githooks/fixed_test_waits_allowlist.txt` only shrinks: when you remove a wait, lower or delete its entry in the
  same commit, because a stale entry fails `repo_guards`.
- **Deterministic:** no random values in assertions that search text; build the expected string in full. New tests
  pass alone, in any order, and five serialized runs in a row (report "5/5").
- **API route tests open their client with `openTestClient`** (`routes/tests/helpers.ts`), never `superoak(app)`
  directly. superoak's random port is sometimes one Deno's `fetch` refuses ("Request has been terminated").
- **Keep the suite current:** update the existing test when behaviour changes; remove obsolete tests.
- **Break-it check:** for each new or changed test the worker reports the one implementation change that makes it
  fail, runs it, confirms red, reverts. Reviewers repeat it for 2–3 tests.
- **Test first:** quote the failing run, then the passing run.
- **Scope:** per step, only the test files covering the change. Main runs the `<program>` suite at case end and the
  full repo suite in the send gate (`02-case-lifecycle.md`, step 10). Sync merges run no tests.
- Web jest runs with `TZ=UTC`.

## Code conventions (from `CLAUDE.md` and `.claude/CONVENTIONS.md`)

- Deno imports use the prefix alias with an explicit `.ts`; no barrels, no mixed forms. Run `npx nx sync` after
  adding a lib import.
- Request path: route, then domain, then db, through `routeWrapper({requestBodySchema, hasPermission, routeLogic})`.
  Zod at input boundaries; Zod is the source of types; shared error taxonomy.
- §7: no `as` or `any`; no `console.log` or dead code; no floating promises; use the passed-in `tx`; pagination in
  the query; status or enum changes ship with their migration; optional services guarded by their env var; buttons
  disabled while processing; lockfile and CI parity; DRY without over-abstraction.
- Simplest thing that works; no new dependencies; seeded PRNG, not faker.
- No behaviour change for other products unless a decision says so.

## Comments

- Only where needed: why a constraint exists, outside behaviour the code depends on, the regulation behind a rule,
  or the case a handoff goes to.
- Never restate the code, describe history ("previously…") or plans ("will…").
- Accurate to the code now and specific (function, file, case ID, regulation section). Every TODO carries a case ID.
  Match the surrounding comment density. When in doubt, leave it out and make the code clearer.

## Text truth

List every new or changed user-visible string and comment, and check each against the code now:
- no unbuilt behaviour; status text matches the real state;
- names, never IDs; no values the new site doesn't have; no vendor or company names in user-facing text.

## Other-product impact

- `git -C <wt> diff --stat origin/develop...HEAD` shows no shared path the plan didn't name.
- `npx nx show projects --affected --base=origin/develop`; build, lint and test **every** affected project. The API
  is tested only through `<C>/bin/api-db-test.sh`, never `nx test`.
- For each changed shared symbol, list its callers (the code-index `impact_of` or `find_symbol` tool, or a scoped
  grep) and check each still behaves the same.

## Failures we don't own (boy-scout rule)

- A failure is never waved off as "not ours" until it is checked on an unmodified base
  (`tp-agent dispatch --investigate-failure ID`).
- Fails on develop locally but passed develop's pipeline: suspect the machine first (DB state, `deno.lock`, clock
  drift: `chronyc makestep` in the Podman VM).
- In a file the branch touches or next to one: fix it in its own `<CASE-ID> maintenance:` commit and list it in the
  MR.
- Out of reach: name it in the MR's Testing section with a case ID.
- No refactors that aren't fixing a failure.
