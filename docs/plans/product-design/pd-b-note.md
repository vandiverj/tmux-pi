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
