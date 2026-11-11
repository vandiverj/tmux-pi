# RM: project rules set up once, referenced on every task (Main, 2026-11-11)

**Owner (2026-11-11):** the failure is "consistent regulatory and business rule understandings and enforcing those across the
tasks". "I do not want another job to maintain this and get everything setup on initiated everytime. it should be something the
main agent could setup once and then reference to the project as needed." The owner approved this brief's direction ("Yes, brief
it") after Main's assessment of an outside proposal. Nothing here starts until the owner approves the slices and their place in
the queue. **Order:** after test speed (PR1, Q4) and HB3a merge.

## What goes wrong today
1. A rule is applied outside its scope (a rule for one program, activity or actor is reused in another).
2. An earlier finding is lost, so the owner asks for the same investigation again.
3. The rule check is skipped: a change to regulated behaviour is reviewed as code only.

## Where things live
- **tmux-pi (public repo):** only the generic mechanism, its tests and its docs. No rule, source document, finding, path or name
  from a work project, ever. The private-name grep runs before every commit as today.
- **The target project, on its own machine:** the rule map, the source documents and the recorded rules. They are committed in
  that project, so an approval of the project's tree also covers the map.

## The slices (each under ~20 min of worker time; each feeds the next)
### RM1: the rule map, written once by Main
- A file in the target project, `.tmux-pi/rules.md` (format fixed by this slice, plain Markdown with one table per area).
- Each row gives a business area, the source documents that govern it (path in the project and section), and the code paths that
  implement it (globs), and states whether it is `regulated`.
- Main writes it once by reading the project and its documents, and the owner confirms it once. After that it changes only when
  the owner corrects something.
- Delivery: when present, the per-project worker preamble (`lib/preamble.py`) and Main's session-start brief name the file and the
  areas. They name it; they do not paste it.
- Source documents stay in a folder of the project, with their dates in the file names. Replacing a file is the whole update
  process: no ingestion, hashes or refresh job.

### RM2: every recorded rule says where it applies
- Extend HB3a's decision records (lib/work.py, "Decisions") with optional fields: area, program, activity, actor, effective date,
  source (document + section), and a status of `owner-confirmed` or `agent-reviewed`.
- A field that is not known is `unknown`; the agent never fills one in by guessing.
- A rule with no applicability shows in the brief as "scope unknown", never as a general convention.
- Two in-force rules with overlapping applicability that disagree show as `CONFLICT` (HB3a's existing mechanism).
- No new store.

### RM3: the trigger is the changed files, and the review must answer it
- If a lane's diff touches a path the map marks `regulated`, the lane needs a rule verdict. The trigger is deterministic: path
  globs, re-evaluated on the final diff, and no model decides it.
- The existing cross-provider reviewer gets the map rows that fired and must return a separate section: is each cited rule correct
  and applicable, and was anything missed (another program, an exception, another affected workflow)?
- Statements of a requirement without a cited source are `provisional`, and the reviewer flags them.
- Lane close requires this verdict when the trigger fired, on the exact tree, as it already requires the code review.
- "Missing evidence / applicability unresolved" is blocking, and `review_verdict` must not treat it as closable because it has no
  code fix.
- One reviewer run with two verdicts, not a second specialist run.
- When Main answers a design question itself, the same map and rules are in its brief (session start and compaction), so Main is
  held to the same scope.

### RM4: workers may read more than they may edit
- The worker contract separates edit scope (as today) from read scope. When RM3's trigger fires, read scope covers the project's
  source-document folder and the map.
- A worker that needs a document says which one in its result instead of stopping (owner's SW1 complaint: workers "find one or
  two things and quit").

### RM5: regression cases from real corrections
- Each time the owner corrects a rule, Main adds a case (generic in tmux-pi, specific in the project): the input, the wrong
  answer, the right answer and the source.
- Constructed cases to start with: rule from the wrong program; shared helper used by two programs; exception outside the first
  section read; approval made stale by a later change.
- These cases are run by the product-design lane's prepare-cases, not by a new suite.

## Left out on purpose (would be a second job)
- Source ingestion, version hashes and freshness jobs.
- A knowledge graph, model fine-tuning or local-model routing.
- A separate findings store.
- An applicability record for every task at intake.
- A separate investigation run per task.
- Model classification of every task.

## Owner decisions before RM1 starts
1. Approve the slices and their order (RM1 → RM2 → RM3 → RM4 → RM5), or change them.
2. Which project gets the first map (the work project, on the work Mac; Main never writes to a target project without the owner's
   word for that project).
