# Common rules for lane workers (copy to <C>/briefs/brief-common.md and fill in the paths)

Every brief links this file and restates the tool rule and the never-list, because review packets snapshot the
brief. tmux-pi workers don't load the repo's CLAUDE.md, so the repo rules are here.

## Scope
- Work only in your worktree `<wt>`, on its branch. Commit there. Never push, open an MR, post to `<tracker>` or
  `<git-host>`, amend, rebase, reset or cherry-pick. Don't touch any other worktree.
- Before editing, restate the assignment and list its acceptance criteria and the cases they imply.
- Do what the brief asks. Stop and send `tp-agent request` with what you found and the options when: you must edit
  a file or symbol the brief lists as out of scope or owned by another lane; the rule needs a case the brief's
  table doesn't have; or the change reaches files the brief didn't name. Don't widen the scope, and don't decide a
  rule that affects compliance or data handling. Report anything else you notice as a note, not a change.
- Fix a cause, not a site: when you fix a defect, find every writer, reader and caller that shares it, and fix or
  list each one.

## Tools (every Bash call is plain)
- Write and edit files only with the Write and Edit tools.
- No heredocs or inline scripts: no `cat <<EOF`, `python3 - <<`, `node -e`, `deno eval`. No shell arrays, no
  `$(...)`. Each one stops at a permission prompt nobody will answer, and the task is lost.
- Search with `git -C <wt> grep -n '<pattern>' -- apps lib docs`, never a repo-root `rg`.
- Outside the worktree, use the `safe_grep` and `safe_read` tools or the Read tool on an exact path.
- Use `git -C <wt>` and absolute paths. Never `mise trust`.

## Never
- Read, grep, cat or source `.env*` files or credential values (running the campaign runner as written is fine;
  it loads them inside the script). If you need a fact from one, ask with
  `tp-agent request` and accept yes/no or names only.
- Use the dev database, create or connect to a database yourself, run `db:checksum`, or use MCP database tools.
- Run `npm ci` or `npm install`; node_modules are symlinks into the main checkout. If `package.json` or a lockfile
  would change, stop and report.
- Put a real or real-looking SSN, name, email or address in code, tests, logs or screenshots. Use `9xx` SSNs.

## Product rules in force
- `S1D1`: no access or reveal log for the sensitive identifier, ever. The browser sees the last four digits, except
  the authorized reveal (`S1D3`: `reveal_full_id` permission, the shared `Revealable` component, 60-second hide,
  nothing cached).
- `S1D2`: audit and history rows never copy identifiers the record already holds.
- `S1D7`: no document upload. Corrections carry a written reason and how they were verified.
- `S1D4`: test cleanup deletes only its own rows.
- Notices to the `<contact-role>` only ask them to log in: no names, counts or results. No emails to the people the
  program covers.
- Every view is scoped to the customer organization.
- Decisions: `<C>/briefs/<batch>-decisions-<date>.md` (and earlier logs it names). A decision wins over the case
  text. Decision ids (`S1D1`…) are for the briefs, the log and your report to Main. In code, comments, test names
  and commit messages state the rule in words: the ids are local to `<owner>`'s campaign and mean nothing outside it.

## Repo rules (from CLAUDE.md and .claude/CONVENTIONS.md)
- Deno imports: prefix alias plus explicit `.ts`; no barrels. Run `npx nx sync` after adding a lib import.
- Route → domain → db through `routeWrapper`; Zod at input boundaries; no `as` or `any`; use the passed-in `tx`; no
  `console.log`, dead code or floating promises; every TODO carries a case ID.
- `[ACCEPTS]` and `[REJECTS]` permission tests for each endpoint, run as an org admin.
- Simplest thing that works. No new dependencies. Seeded PRNG, not faker.
- No behaviour change for other products.
- Comments only for why, outside behaviour, a regulation or a case handoff. Never restate code, history or plans.
- UI text true of the code now: names not IDs, no unbuilt behaviour, no vendor names.

## Migrations
None unless the brief says so. If you find you need one, stop and report. When the brief asks for one:
`deno task db:generate` (`--custom` for triggers); never hand-edit `_journal.json` or an existing migration; `when`
above develop's latest and not in the future; after committing, `deno task db:check-drift` passes and
`git -C <wt> status --short -- apps/<api>/src/db/drizzle` is empty (on drift, delete only the files it
listed and restore `meta/_journal.json`, then stop and report); table registered in
`apps/<api>/src/db/validators.ts`; SQL, snapshot, journal and schema in one commit.

## Tests
- **Find the existing test before writing one.** `git -C <wt> grep -l '<function, route or component>' --
  '*_test.ts' '*.test.ts' '*.test.tsx' '*.spec.ts'`. If a table, loop or `describe` covers it, add a row or case
  there. Start a new file only when none covers it (or the file is over 3,000 lines), and say what you searched.
  Grep for a helper (`waitFor`, `later`, fake clock, date stub, world builder) before writing one.
- Test first: write the failing test, run it, quote the failure; then the code; then the passing run.
- API DB tests only through the runner, one plain command per run:
  `<C>/bin/api-db-test.sh <wt> <your task id> <test files relative to apps/<api>>`. Read its last line
  (`exit=... log=...`). Exit 75: the lock is busy, retry. Never `deno task test`. Run tests in the foreground
  and wait (timeout up to 600000 ms; split a long run by folder). A background run dies when you finish.
- Web: `TZ=UTC npx jest --ci <paths>` and `npx tsc -b`. Admin app: `npx jest --ci <paths>` and
  `npx tsc --noEmit -p .`. Libs: `npx nx test <lib>`.
- Run only the test files covering your change (new tests plus files that import or seed through the changed
  modules), with per-file counts. Main runs the wider suites.
- Test standards (`03-security-and-quality.md` in this prompt folder): each test traces to an AC row or named
  risk; assert observable outcomes with exact values; test the rule's edges; lowest layer, once; update existing
  tests rather than adding parallel ones; route tests use `openTestClient`, never `superoak(app)`; break-it check
  for each new or changed test.
- **No fixed waits** (`setTimeout`, `sleep`, `waitForTimeout`). Wait on the thing: await the promise or poll a
  condition with a deadline (`waitFor`); UI `findBy*`; a fake clock or date stub for time; pass the timestamp
  explicitly for "B is later than A"; hold the first request on a latch for "two requests overlap". If the code
  lacks the hook, add it. Any remaining timer line carries `// timer-ok: <reason>`. A raised test timeout says
  what is slow.
- No random values in text assertions. New tests pass alone and five serialized runs in a row ("5/5").

## Checks before each commit (the commit hook is off in lanes)
- `deno check --allow-import --config apps/<api>/deno.json <changed API files>` and `deno lint <files>`.
- `npx --no-install prettier --check <changed files>` (never `deno fmt`).
- `npx nx lint <project>` for each web, admin or lib project you changed.
- `gitleaks git --log-opts="<base>..HEAD" --no-banner`.
- `semgrep scan --config p/typescript --config p/nodejsscan --config p/jwt --config p/secrets --metrics=off
  --baseline-commit <base> apps lib`; triage each new finding in the report.
- Timer waits: `git -C <wt> diff <base> -U0 -- '*_test.ts' '*.test.ts' '*.test.tsx' '*.spec.ts' | grep -nE
  '^\+[^+].*(setTimeout\(|waitForTimeout\(|\bsleep\()' | grep -v 'timer-ok'` prints nothing.
- `git -C <wt> status --short` is clean apart from your change; restore `deno.lock`, `deno.json` and
  `deno.test.json` if a run changed them.

## Commits
- One logical change per commit: `<CASE-ID>: <imperative subject>`; maintenance fixes
  `<CASE-ID> maintenance: <subject>`.
- `git -C <wt> -c core.hooksPath=/dev/null commit`, ending with `Co-Authored-By: <the model you run as>
  <noreply@anthropic.com>`.
- Never commit `.tmux-pi/` files or an untracked AGENTS.md.

## Report
- Each AC (or brief item) mapped to its test file:line, with "Existing test: file:line" or "None (searched:
  terms)".
- The timer-wait grep output, and "5/5" for the new tests.
- Changes with file:line and the key diffs quoted.
- Every command run, with per-file counts copied from the log line and the log path; how the tests were picked.
- The failing-first output pasted (not described) and the passing run.
- The break-it change and result for each new or changed test.
- Check results (lint, Prettier, typecheck, gitleaks, Semgrep with triage).
- Commit sha(s), clean worktree, anything not done and why.
- Any failing test you don't own, under `FAILING_TESTS:` as `- test:` bullets with the command and whether you checked it on the base. When none fails, leave the block out; do not write `FAILING_TESTS: none`.
- A **Self-review** section. Each line gives evidence, or "none found, checked by <command>":
  1. Symbols: for every function, type, route, field or file you changed, its writers, readers and callers
     (`git -C <wt> grep -n`, command shown) and what each does now.
  2. Pairs: for every action added or changed, its opposite or repeat (open/close, apply/correct/undo,
     create/cancel, block/unblock) and where it is handled and tested.
  3. Other paths to the same rule: other routes, jobs, imports or seeds that reach the same state, and that they
     apply the same check.
  4. Offered vs accepted: for every control, option or message the UI shows, the API check that accepts it.
  5. Text: each new or changed user-visible string and comment, and the code that makes it true now.
  6. Consumers: typed fixtures, mocks, READMEs or docs that describe the old behaviour, updated or named.
  7. AC and case-table rows: each mapped to its test file:line.
  The reviewer spot-checks two lines; a false line is a defect.
- Last line: `RESULT_STATUS: complete`.
