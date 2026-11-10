# PD-B: persistence and deterministic helpers (lane product-design, 2026-11-09)

Phase B of the product design plan (see `pd-brief.md`): `bin/tp-product` (`init`, `status`, `validate`, `apply`),
changesets with a lock, an expected revision and direct-edit detection, semantic fingerprints, feedback-ingestion
primitives, impact propagation, and brief decision 9 in the readiness code. The contract is in
`docs/product-design.md` (The `tp-product` command, Persistence, Changesets, Feedback ingestion, Slice readiness).

## What was added

| Path | Holds |
| --- | --- |
| `lib/product_design/store.py` | Lock (flock, 30 s), journaled commit (`txn/`, commit point `txn/txn.json`), recovery, commit record, `inspect` / `trust_problems`, `init` |
| `lib/product_design/changes.py` | Changeset check and operations, the transaction (`apply`), adoption of external edits, `impact`, `check_bundle` / `ingest` / `anchor` |
| `lib/product_design/cli.py`, `bin/tp-product` | The command; the wrapper only puts `lib/` on the path |
| `lib/product_design/package.py` | `acceptance_problems` (decision 9, used by validation and readiness), `content_fingerprint`, `stale_cases`, `AUTHORITY`, `authority_map` |
| Schema | Slice `readiness.acceptance.evidence` (required, at least one), `readiness.delivery`, `$defs/feedback` |
| Fixture | The accepted slice's acceptance names its decision evidence; `.tp-product/commit.json` (revision 3) so the fixture is a committed package |
| `tests/test_product_design_store.py` | Phase B tests; decision-9 rows added to `tests/test_product_design_package.py` |

## Deviations from the plan, with reasons

- **No `import-feedback` or `build` command.** The brief defers both to the review-build phase; ingestion is the
  library function `changes.ingest`, tested in process.
- **Anchor remapping and a target registry are not built.** A note is anchored `current`, `changed-context` or
  `unresolved`; `remapped` stays in the schema for the review phase, which knows the build's targets.
- **`stale` and `blocked` stay derived.** The plan's impact propagation "marks" dependent slices stale; here a slice is
  stale when its acceptance fingerprint differs from its current fingerprint, computed on every read, and each commit
  records which slices became stale in its history entry. Storing it would let a stored flag and the content disagree.
- **Acceptance fields are authority fields.** Plan section 10 says direct edits are detected and must be adopted; this
  phase also refuses to adopt an edit that changes an acceptance, a readiness state, an input's reconciliation or an
  accepted uncertainty, so a hand edit cannot manufacture an acceptance (decision 9).
- **The fixture keeps its PD-A formatting** and ships a generated `commit.json`; digests are of canonical JSON, so the
  formatting is not an edit. The first `apply` rewrites the files with two-space indentation.
- **Withdrawing a blocking question** (`set` on a question's status) unblocks a slice without an acceptance event.
  It cannot make a slice ready (that still needs a human acceptance), so it is left as an ordinary edit, recorded in
  history.

## Round 2: hardening (2026-11-09)

- **Read-back.** Every file a commit writes is checked against the reader's limits (`package.MAX_FILE_BYTES`,
  `MAX_DEPTH`, each owned by one function in `package.py`) before the commit point (`result.unreadable`). Bundles and
  changesets have the same size and depth limits whether they arrive as bytes, text or a parsed value
  (`changes.load_json_input`).
- **Other writers' bytes.** Just before the commit point, and per file in roll-forward, the live files, `commit.json`
  and the new history slot must still match what the change was computed from. Choice: withdraw the commit and keep
  the other writer's bytes (`commit.external-edit`, exit 3) rather than merge. History entries are created with a
  link, so an existing one is never replaced (`history.exists`).
- **Control records** are shape-checked (`commit_problems`, `history_problems`, `txn_problems`) and must agree with
  each other; disagreement is `package.control-mismatch` (untrusted, exit 3) and every writer, adoption included,
  refuses. `init` now writes a complete history entry.
- **Originals.** `commit.json` records a digest of each original note, input and review. Choice: an adoption that
  removes or changes one is refused (`adopt.original`); a re-import with other content stays a `bundle.conflict`.
- **Operation shapes** are checked against `changes.SPECS` before use (`op.missing`, `op.shape`); under `--json`
  every refusal, usage and an unreadable changeset file included, is the JSON error object.

Deviations and limits:

- **Reopening an accepted uncertainty** (a `set` that withdraws or rewrites an accepted question) is not changed here;
  it is owed to PD-E with the rest of the uncertainty workflow.
- (Superseded by round 3: every history entry is now validated, and files are displaced rather than replaced.)
- Deleting an original input or review by hand is refused by the earlier checks (`adopt.authority` for an input's
  status, `adopt.invalid` when a slice's readiness names the review), not by `adopt.original`; either way the
  original is kept.
- Round 1's attack report miscounted its mismatches: the final run had none, and the runs before it had 5 and then
  1 (two expectation-ordering errors, one readiness expectation, two TypeErrors, then one adoption ordering).

## Round 3: a commit never loses data or trust (2026-11-09)

- **Displacement, not replacement (F1).** `store._place` renames each live file into `txn/displaced/NAME~TOKEN`,
  checks the displaced bytes against the old (or new) digest, and links the new file in place with `os.link`, which
  fails if the name has reappeared. Someone else's displaced bytes are linked back (unless the name was saved again,
  when the newer save stays) and kept; a file that appeared at the name stays; after the link the displaced copy is
  checked again. Each is `txn.conflict` naming the kept copy (`details.kept`); the journal stays, so status reports
  `package.interrupted` and `package.displaced` until `--recover`. `_finish_txn` removes only copies that hold the
  old or new content, checked just before. `recover` and roll-forward use the same steps. A file whose live content is
  already the new content is not rewritten (it used to be replaced with identical bytes).
- **Every control record is validated (F2-F4, F6).** `store.check_history` reads every `NNNNNN.json`: shape, types,
  revision equal to the name, and each `base_revision` equal to the entry before it, gaps found by comparing
  neighbours (no range is built, so any integer is a structured refusal). Any `control.invalid` makes status and
  validate untrusted (`package.control-invalid`). `commit`, roll-forward and `init` (which also refuses a leftover
  history) refuse to write a revision on a history that fails. An adoption with `commit.json` missing continues from
  the newest history entry, or is refused (`revision.conflict`, exit 4) when `--expected-revision` is the manifest's.
- **One representability rule (F5).** `package.text_problem` (a lone surrogate) is called by `file_problem` (every
  file a commit writes), `read_json_file` (every file read) and `changes.load_json_input` (bytes, text, parsed
  values and changeset files), so each form is a structured `encoding` problem; `--json` prints the error object.
- **Report correction (F8).** Round 2's report said `read_json_file` went through `file_problem`; it calls
  `size_problem` and `depth_problem` directly (now also `text_problem`).

Limits, stated:

- A writer that holds the old file open and writes to it after `_finish_txn`'s last check (between the check and the
  unlink of a displaced copy) still loses that write: the window is one `unlink` long, and only a writer that keeps
  writing to a file that is no longer at its name can hit it. The same holds for the instant between the final check
  in `_place` and the next step.
- Copies are compared by canonical-JSON digest: an editor's save that only re-indents the old content counts as the
  old content, and its copy is removed.
- An editor that saves by rename after the new file is linked wins: its file is live and the package reports
  `package.external-edit`, as for any edit after a commit.
- `init` writes `.gitignore` only when absent; the changeset path given on the command line is not checked for
  symlinks (it is read, never written).
- F7 (reopening an accepted uncertainty) stays owed to PD-E.

## Round 4: no command publishes a state its reader refuses (2026-11-09)

- **One prospective check (F1).** `store.prospective(root, docs, entry, record=None, revision=None)` builds what a
  commit would publish: the package files, the new history entry after the existing history, and the commit record.
  It checks that state with the reader's own functions: `file_problem`, `validate_docs`, `commit_problems`,
  `history_problems`, `check_history`, and `chain_problems` and `agreement_problems` (both taken out of `check_history`
  and `inspect`, so status and the writers share them). `commit` (behind `apply`, adoption, `ingest` and `init`) calls
  it before the journal exists. `_staged`, run by roll-forward and so by `recover`, calls it on the staged files before
  anything is replaced. A refusal names the file. On recovery it keeps the journal and its staged files
  (`details.journal`, `details.staged`). Choices: recovery reports `result.*` problems as `control.invalid`, because the
  staged files are control data the user did not write in this command; a disagreement stays
  `package.control-mismatch` (exit 3), the code status gives. `history.exists` and `txn.conflict` are still checked
  first, so a foreign file keeps its own code.
- **One encode function (F2).** `package.encode` is the only place a value becomes bytes (`render`, `canonical`,
  `digest`). It turns a lone surrogate into `TextError`. `file_problem` gets the encoding problem by rendering, with
  no separate pre-check. `load_json_input` gets it for a parsed value by measuring its canonical size. `text_problem`
  calls `encode` and remains as the earlier, friendlier message in `read_json_file` and for text input.
- **Report correction (F3).** Round 3's report said adoption replaces `commit.json`. It also replaces `package.json`:
  adoption commits the edited files as the next revision, so the manifest's revision is incremented and
  `package.json` is rewritten along with `commit.json` and the new history entry.

## Round 5: a staged record that is present is validated as it is (2026-11-10)

- **Absent is decided by the file (F1).** Recovery published a staged `commit.json` holding JSON `null` when the
  journal's digest matched it: `prospective(..., record=None)` meant "build the record", so a parsed `null` was taken
  as "no record" and a record built from the documents was checked instead. `prospective(root, docs, entry,
  staged=None)` now takes recovery's staged record as `staged=(record, revision)`, so any staged value, `null`
  included, is checked by `commit_problems`, the function `status` uses.
- **One reader.** `package.read_file(path)` returns `package.ABSENT` when no file is there, else the parsed value
  under the reader's limits. `ABSENT` refuses truth-testing (`bool(ABSENT)` raises), so callers must use `is`. It is
  used for every document (`read_docs`), every control file (`store._read_json`, so `_load` for `commit.json`, history
  entries and `txn.json`, and `_live_digest`) and every staged file (`_staged` reads each staged file once through it;
  a missing staged file is `control.invalid`).
- **No reader turns a falsy value into absence.** Every `or {}`, `if commit` and `revision or ...` on a control value
  was replaced with an explicit `is None` or `is ABSENT` test (`inspect`, `_expected`, `history_problems`,
  `changes._open`, `cli.status_report`). `prospective` refuses a commit whose documents lack a key instead of
  rendering that key as `null`. The task report lists each site and how it was handled.
