# HB7b: the contract ledger (lane hub-prune, second slice of HB7 / H40), rule-first

HB7 (README.md "HB7"): "A prune pass: each contract/role line carries the evidence that justified it, and removal is evaluated like
addition." HB7a's report (tools/friction/report.py, merged 0427722) measured Main's prompt + roles at 147 lines / 4403 words with
**0 lines carrying their evidence**. Evidence written into each line would make the prompt longer, and Main pays for it every turn.
So the evidence lives in a **ledger file** beside the contract, and the report checks the ledger against the files. This slice builds
the check and a skeleton generator. Main fills the ledger afterwards; the owner decides any removal. Read-only, like HB7a. Bar:
realistic use (`docs/plans/realistic-use-bar.md`). **Adoption is the owner's.**

Read first: tools/friction/report.py (whole file: `contract_size`, `EVIDENCE_RE`, the loaders, provenance rules, `--help` epilog),
tests/test_friction_report.py, docs/plans/det-prep/hub/hb7/frictions.json (the `key:` targets), hb7a-round2.md..round4.md (rules
that stay in force: provenance from authorship, every filesystem error a listed problem, `os.listdir` probes).

## Rules (write them as tables in report.md first, then code)
1. **Population.** The contract is the files `contract_size` already reads (claude/main-prompt.md, agent/roles/*.md,
   agent/contract/worker.md). A line needs a reason when it is non-blank and not a Markdown heading (`#`..`######`). Fenced code
   lines count like prose.
2. **Ledger file** (JSON, `schema: tp-contract-ledger:v1`, `entries: [...]`). Each entry: `file` (repo path), `sha` (first 12 hex
   of sha256 of the line with leading/trailing whitespace stripped), `prefix` (first 8 words, for a human reading the file; never
   used to match), `reasons` (list of pointers), `by` (`main` | `owner` | `worker`), `status` (`keep` | `remove-candidate`), and,
   for `remove-candidate`, `removal_reasons` (list of pointers). Matching is by (`file`, `sha`) only. Identical lines in one file share
   one entry; the report says how many lines it covers.
3. **Pointers.** The forms report.py already uses (`record:`, `label:`, `memory:`, `doc:<path>#<heading>`) plus `key:<friction key>`
   (a key in the `--map` file) and `commit:<hex, 7-40>`. Every pointer is **resolved**: the record exists in the store, the label row
   exists in a `--labels` file, the memory file exists, the doc exists and has that heading, the key exists in the map, the commit
   exists (`git cat-file -e <sha>^{commit}` run read-only with the repo as cwd). An unresolved pointer counts for nothing and is
   listed with why. When the source a pointer needs was not given (no `--store`, no `--labels`), the pointer is `unchecked`, listed
   apart, and counts for nothing.
4. **Reason vs provenance.** `commit:` only says when a line came in, not why: a line whose only resolved pointers are `commit:` is
   `provenance only`, not justified. Any other resolved pointer makes it `justified`.
5. **Who stands behind it.** A line is `owner-stated` only when a resolved reason is a `label:` row or an HB3 record by the owner
   with a quote (HB7a round 2 rule 1), and neither the entry's `by` nor anything else says `worker`. Otherwise `main`. An entry with
   `by: worker` is never `justified`; it is listed as `worker text, evidence only`.
6. **Removal is evaluated like addition.** `remove-candidate` needs at least one resolved `removal_reasons` pointer that is not
   `commit:` (for example `key:` whose home moved to a tool default, or a `doc:` heading recording the change). Without one it is
   listed as `removal without a reason`. The report never edits a contract file and never says a line should go; it lists.
7. **Drift.** An entry whose (`file`, `sha`) matches no current line is `stale` (the line was edited, moved to another file or
   removed). An entry for a file outside the population, or a missing file, is a listed problem. Two entries with one (`file`,
   `sha`) are a listed problem and neither counts.
8. **Errors.** An unreadable or malformed ledger, a missing field, a wrong type or an unknown `status`/`by` is a listed problem for
   the ledger source (the entry, or the whole ledger when it cannot be parsed); the rest of the report still runs. `git` missing or
   failing is a listed problem for `commit:` pointers only.
9. **Counting.** `evidence_lines` in `contract_size` becomes: lines with an inline pointer (today's `EVIDENCE_RE`) **or** a
   `justified` ledger entry. Without `--ledger` the output is unchanged from today. With it, per file and in total: justified
   (owner-stated / main), provenance only, no entry, stale entries, removal candidates (with / without a reason), problems.
10. **Skeleton.** `--ledger-skeleton` prints to stdout (writes nothing) a ledger JSON holding an entry for every population line that
    has no entry in `--ledger` (or every line, without `--ledger`): `file`, `sha`, `prefix`, `reasons: ["commit:<sha>"]` from
    `git blame --porcelain` of that line at HEAD (uncommitted lines get `reasons: []`), `by: main`, `status: keep`. Main fills it.

CLI: `python3 tools/friction/report.py --map FILE [--ledger FILE] [--ledger-skeleton] [--labels ...] [--memory DIR] [--store DIR]
[--json]`. Put the rules' short form in the `--help` epilog (where round 4 put the deferral).

## Input-space attack table (required in report.md, one row per cell, each row -> the test row that proves it)
Line side: plain line; heading; blank; fenced line; leading/trailing whitespace changed only (same sha); one word changed (stale);
line moved within the file (still matches); line moved to another population file (stale + no entry); duplicate identical lines;
file removed; file unreadable. Ledger side: missing file; unreadable; not JSON; wrong schema; entry missing each field; wrong type per
field; unknown status/by; duplicate (file, sha); entry for a file outside the population. Pointer side: each form resolved,
unresolved, unchecked (source not given); `commit:` alone; `commit:` + another reason; malformed pointer; `by: worker`;
`remove-candidate` with no removal reason, with only `commit:`, with a resolved reason. Skeleton: committed line, uncommitted line,
not a git repo.

## Tests
Extend tests/test_friction_report.py: one table-driven method for the matrix above where the rows share a fixture (a temp repo with
a tiny population and a ledger), plus the skeleton cases; extend the existing CLI and permission tests rather than adding methods
for them. Skip the unreadable rows as root. No live LM Studio, no network, no default tmux server: run every test through
`tools/agent/test-run.sh` with a fresh `TMUX_TMPDIR`. Run `tests.test_friction_report` under python3.9 and python3.14 (find them
with `command -v`) and `tests.test_session_env tests.test_docs_wiring` once. Report the runtime of the new rows.

## Real-data check (read-only)
Run `--ledger-skeleton` against the lane worktree and report only the counts: population lines, lines with a blamed commit,
uncommitted lines. Do not write the ledger file into the repo; Main fills it.

Do not edit claude/main-prompt.md, agent/roles, agent/contract, config/ or lib/. Do not commit. Do not add git notes. Use safe_grep /
safe_read for searching; if a guard refuses a read-only analysis, write the script as a `.py` file in your evidence/ dir and run it as
`cd "$TP_TASK_DIR" && python3 -I evidence/<f>.py`; if that is refused too, stop and ask.
