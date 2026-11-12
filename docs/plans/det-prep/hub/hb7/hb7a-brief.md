# HB7a: friction records from recorded corrections (lane hub-friction, first slice of HB7 / H40)

Owner target (owner-request.md): "I provide a goal, correct the process once if necessary, leave and return, and receive current
completion evidence without repeatedly reminding Main how to operate." HB7 (README.md "HB7") turns a correction the owner had to
give into a scoped durable improvement, and measures whether the same point comes back. This first slice is a **read-only report**:
it derives friction records from the places corrections are held today, groups them, proposes a durable home, and measures
recurrence and contract size. It writes no contract, prompt, role file, memory or record. **Adoption is the owner's.** Bar: realistic
use (`docs/plans/realistic-use-bar.md`). Later slices: HB7b the prune pass (each contract line carries its evidence), HB7c the
model-profile arm (H15, `../../m1-model-profiles.md`).

## Where corrections are held today (measured by Main, 2026-11-12)
1. **HB3 decision records of kind `correction`** (lib/work.py:645-800, `all_records`; interfaces.md section 5). This is the designed
   source. On this Mac the store holds **0 records of any kind**: Main has not been writing them. The report must read them, and must
   say plainly when there are none.
2. **H48 labels of owner messages** (`.tmux-pi/il1/labels-A.json`, `labels-B.json`; untracked, they quote private project content):
   a list of `{i, sid, at, label, confidence, why, quote[, proj]}`; `label == "process"` rows are corrections of how Main operates.
   The kind and the repeat flag live only in the summary files (`labels-*-summary.md`), not per row: say how you join them, or that
   you cannot.
3. **Main's feedback memories**: one markdown file per fact with frontmatter `name`, `description`, `metadata.type`; `type: feedback`
   files quote the owner and carry `**Why:**` / `**How to apply:**` lines. Directory passed as an argument; never a default path.
4. **Brief-rule signals from review rounds** (Main's diagnosis, not the owner's words): H12 (`../../loop/ideas.md` "H12"), H22
   ("H22"), H26 ("H26"). These are HB7's first records.

## Rules first (write this table in report.md before code)
One row per source: path, the fields used, provenance (`by owner` with an exact quote / `main` / `worker`), the timestamp used for
ordering, and what it cannot know. Then the derivation rules:
- **Friction record** = `{key, trigger, correction, successful_action, scope, evidence[], provenance, status, proposed_home,
  occurrences[], recurrence}`. `evidence[]` are pointers (record id, label file + index, memory file name, ideas.md heading), never
  copied text.
- **Grouping is by an explicit key, never by text similarity.** A key comes from (a) a `friction:` key on the correction record,
  (b) a tracked mapping file `docs/plans/det-prep/hub/hb7/frictions.json` that Main writes and the owner can correct
  (key → trigger, scope, proposed home, source pointers). A source item with no key is listed as `ungrouped`, never guessed into a
  group. Write the mapping for the case-2 data yourself (pointers only, no quotes), and mark each key `by main` (inferred).
- **Status**: `active` only when at least one occurrence is an owner correction with an exact quote (an HB3 record by owner with
  quote, an H48 `process` row, or a feedback memory whose body quotes the owner) **and** the scope is stated; otherwise
  `candidate`. Worker text (task results, reports, reviewer findings) is never a source of policy: if a worker-authored source
  appears, it is listed as evidence only and cannot raise status. Note: `active` here is the report's label for "the owner said it";
  it is not an HB3 record and does not enter `tp-agent brief`.
- **Durable home** (proposed, one per key): `contract line` (claude/main-prompt.md or agent/roles/<role>.md), `tool default`
  (a code default, e.g. safe_grep), `adapter` (per-model or per-provider layer), `retrieved procedure` (a doc the brief points to,
  e.g. a memory or plan file). Say for each key why that home and what would show it worked.
- **Recurrence after correction** = occurrences of the same key with a timestamp after the first owner correction of that key, per
  source and total. Items with no timestamp count as `undated` and are never placed before or after.
- **Contract size** = lines and words of claude/main-prompt.md and each agent/roles/*.md, and how many of those lines carry an
  evidence pointer (expect 0 today; HB7b builds on this number).

## Build
- `tools/friction/report.py`: `python3 tools/friction/report.py --map docs/plans/det-prep/hub/hb7/frictions.json [--store DIR]
  [--labels FILE ...] [--memory DIR] [--json]`. HB3 records through `lib.work` (`--store` sets TP_WORK_DIR for lib.work, as its tests do; no
  second parser). Output per key: status, scope, proposed home, occurrences by source (dated / undated), recurrence after
  correction, evidence pointers; then ungrouped items by source; then contract size. Read-only. Prints no quote text from labels or
  memories (pointers and counts only), nothing from `.env*` or credential files. Malformed or missing inputs are listed and skipped,
  never a crash.
- `docs/plans/det-prep/hub/hb7/frictions.json`: keys for at least H12, H22, H26 (scope `role:coder` or brief writing; home `contract
  line` or `retrieved procedure`: decide and say why) and the H48 repeats (A: model choice, tools are for target projects, safe
  search; B: state saving/restart, run only affected tests). Pointers only.

## Case 1, planted (ground truth)
A temp work store with HB3 records (one owner correction with quote and scope, one by main, one worker-provenance, one withdrawn,
one superseded), a labels file with process and non-process rows (one undated), a memory dir (one feedback with a quote, one
project-type file, one malformed frontmatter), and a mapping with two keys plus one source item left unmapped. Expected per key:
status, recurrence after correction, ungrouped list. Score every expected cell.

## Case 2, real data (read-only)
Run the report on this Mac: `--store` = the live work store (expect 0 records: say so), `--labels .tmux-pi/il1/labels-A.json
.tmux-pi/il1/labels-B.json`, and the mapping you wrote. Do not pass `--memory` on the real directory (it is outside the repo;
Main runs that part). Report per key: status, occurrences, recurrence, proposed home. Quote nothing from the label files in
report.md (counts and pointers only).

## Attack table (one table in report.md, not one test per row)
Input space: source {HB3 record, label row, memory file, mapping} x field {present, missing, malformed, wrong type} x provenance
{owner+quote, owner without quote, main, worker} x status {active, provisional, superseded, withdrawn} x time {dated before, dated
after, undated} x key {mapped, unmapped, mapped twice, key absent from mapping}. Expected output and the row that proves it.
Fail-before where a rule replaces a naive reading: counting a worker source toward `active`, grouping by text, counting an undated
item as a recurrence, counting a withdrawn/superseded record.

## Tests and verification
One new module `tests/test_friction_report.py` (a new tool needs one): one table-driven method for the rules plus the case-1
scoring, with temp dirs. No ritual tests. Run it and `tests.test_session_env` and `tests.test_docs_wiring` once through
`tools/agent/test-run.sh` with `TMUX_TMPDIR=$(mktemp -d /tmp/h7a.XXXX)`. Main runs the cells and the full suite.

Report in report.md (rules first, then case 1 score, case 2 table, attack table, and which next step the numbers point to: Main
writing `correction` records when the owner corrects it, the prune pass, or a contract line). Do not commit. Do not add git notes.
