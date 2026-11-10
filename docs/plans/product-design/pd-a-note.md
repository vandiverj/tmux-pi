# PD-A: source assessment and implementation note (lane product-design, 2026-11-09)

Phase A of the product design plan (see `pd-brief.md`): inspect the checkout, define the smallest package contract,
ship a synthetic fixture and a validator that shows the fixture meets the phase A acceptance.

## Base

- Lane `product-design`, branch `lane/product-design`, worktree `tmux-pi-wt-product-design`.
- Base: `main` at `6662ed1eae6fbc7ee951def469626a75e26d7cba` (the brief commit). The plan was written against an older
  public commit (`624cc3c7`); everything below is from this checkout, not from the plan.
- Python: `/usr/bin/python3` 3.9.6 and `/opt/homebrew/bin/python3` 3.14.5, both used for every check.

## Existing capabilities (inspected)

| Area | What exists | Use in this lane |
| --- | --- | --- |
| `catalog/skills/product-fit/SKILL.md` | Bounded planning guidance: actor and outcome, boundary, scenarios, unresolved decisions with recommended defaults. Planning only. | The product-design skill (PD-D) points to it for the Frame step; it is not copied or changed. |
| `config/skill-catalog.json`, `lib/skill_catalog.py` | `version: 1`, `max_selected: 3`; explicit `/marker` matches rank first; phrases are narrow. | PD-D adds two entries (`product-design`, `prepare-cases`), with markers; `max_selected` stays 3. |
| `catalog/skills/case-handoff/SKILL.md` | Completion-side MR and tracker drafts from the branch tip. | Kept distinct: case preparation (PD-E) is incoming, before work starts. |
| `claude/plugins/tmux-pi/commands/*.md` | Each alias calls `get_skill` with the catalogue id and follows it. | PD-D adds `product-design.md` and `prepare-cases.md` in the same form. |
| `agent/prompts/*.md`, `bin/tp-pi` | Pi starts with `--no-skills`; the catalogue is reached through the `tmux_pi_find_skill` tool (`agent/extensions/catalogue.ts`); prompt files exist for the current aliases. | PD-D checks how pi loads `agent/prompts/` and either adds the same prompt files or documents the explicit prompt path. |
| `bin/tp-agent objective` | Writes `session-objective.md` in the session state; the index and the SessionStart hook show it. | Records package path, revision, slice and next action (brief decision 6). No core change. |
| `bin/tp-review --contract FILE` | Attaches a reviewer contract file; provider independence unchanged. | Product review (PD-D/F) uses a contract file, not a new reviewer profile (brief decision 7). |
| `lib/safe_write.py` | `write_atomic` (O_EXCL temp file, `os.replace`, symlink refusal); stdlib only. | PD-B uses it for atomic package writes instead of a new writer. |
| Roles (`agent/roles/`) | researcher, architect, coder, tester, code-reviewer and others. | Reused unchanged. |
| Lanes (`docs/lanes.md`) | Lease, one writer, exact-tree review, ordered step closure. | This work runs as lane steps PD-A to PD-F. |
| Campaign, package or decision records | None in `lib/`: "campaign" appears only as file-path patterns in `lib/routines.py`; `lib/contract.py` is worker contracts (budgets, checkpoints), unrelated despite the name. | Nothing to reuse; the package contract is new. |
| Tests | `tests/test_*.py`, `import session_env` first, `sys.path` insert of `lib/`; README commands `python3 -m unittest discover -s tests -p 'test_*.py'` and `tests/check_dispatcher_types.sh` are current. | New tests are flat `tests/test_product_design_*.py`. |
| `AGENTS.md` / `CLAUDE.md` | None tracked in this repository. | Nothing to forward to workers. |

## What phase A adds (all new files; no shared file changed)

- `templates/product-design/schema/package-v1.schema.json`: the versioned JSON Schema (draft 2020-12 subset) for the
  three package documents, with `x-id`, `x-ref`, `x-dep` and `x-bookkeeping` extensions.
- `lib/product_design/schema.py`: the subset interpreter; it refuses any keyword it does not implement.
- `lib/product_design/package.py`: read, validate (schema, ids, references, invariants), record and slice
  fingerprints, derived slice readiness. Read-only.
- `templates/product-design/examples/review-queue/`: the synthetic review-queue package.
- `docs/product-design.md`: the package contract (ids, revisions, fingerprints, statuses, workflow state, readiness).
- `tests/test_product_design_package.py`; the attack script and log in the PD-A task evidence (kept out of the tree).

## Phase A acceptance in the fixture

| Element | Where |
| --- | --- |
| Normal workflow | `journey.submit-and-review`; `scenario.submit-complete-record` (kind `normal`) |
| Relevant exception | `scenario.submit-missing-category` (kind `exception`), linked from the normal scenario's `exceptions` |
| Unanswered question | `question.reassignment-during-review` (`open`, blocking `slice.reassignment`); a pending reviewer answer `input.session-b.answer-1` does not resolve it |
| Accepted decision | `decision.missing-field-rejects` with local acceptance at its current fingerprint |
| API / data proposal | `contract.submissions-api` (api, proposed, with failure paths), `contract.submissions-table` (data, proposed) |
| Ready slice | `slice.submit-and-queue`: `ready-for-case-preparation`, accepted by `review.submit-and-queue-1` at the current fingerprint, no blockers |
| Blocked slice | `slice.reassignment`: effective `blocked`, blocker `question.unresolved` on the open question |

Product-fit is unchanged (no catalogue, skill or alias file touched); `tests/test_skill_catalog.py` passes unchanged.

## Planned touchpoints (later phases)

- PD-B: `bin/tp-product` (init, status, validate, apply, import-feedback, impact) and a transaction module in
  `lib/product_design/` using `lib/safe_write.py`; lock file in the package directory; changesets.
- PD-C: `templates/product-design/runtime/` (inlined JS/CSS), `tp-product build`; `builds` entries in the manifest.
- PD-D: `config/skill-catalog.json` (two entries), `catalog/skills/product-design/`, `catalog/skills/prepare-cases/`,
  `claude/plugins/tmux-pi/commands/` (two files), pi prompt path, a product-review contract file. README link.
- PD-E: case definitions in `design.json` `cases`, `tp-product cases` and `packet`.
- PD-F: demo script, user guide sections in `docs/product-design.md`.

## Deviations from the plan, with reasons

1. **Layout.** Tests are flat `tests/test_product_design_*.py` (the repository discovers `tests/test_*.py`; brief
   decision 1), not `tests/product_design/`. The fixture is under `templates/product-design/examples/` rather than
   `tests/fixtures/`, because it is also the demo package for PD-C and PD-F.
2. **Fixed file names.** The package is exactly `package.json`, `design.json`, `reviews.json` (plus the product's own
   source files). The plan's "prototype source directory and generated outputs" are added by PD-C.
3. **Fingerprints and readiness are in phase A.** The plan lists semantic fingerprints under phase B. The ready slice
   must carry an acceptance fingerprint that can be checked, so the fingerprint and the derived readiness are defined
   and implemented now. Phase B still owns locking, atomic apply, import and impact propagation.
4. **No jsonschema library.** The brief forbids installs, so the schema is interpreted by a small subset interpreter
   that refuses unimplemented keywords. Conditional rules (status-dependent fields) are Python invariants, not
   `if/then` schema.
5. **Readiness representation.** Stored states are `draft`, `ready-for-human-review`, `ready-for-case-preparation`,
   `implemented-verified`; `blocked` and `stale` are derived and never stored, so a file cannot claim them falsely.
6. **Contract status.** `proposed` or `existing-verified`; "simulated" is how the runtime will label a proposed API.
   The plan's case blocker "a required contract contradicts known evidence" has no field yet; PD-E adds one when it
   implements case blocking.
7. **Note anchors are not reference-checked.** A note's journey, scenario and target describe the build it came from;
   its `anchor` field records the match to the current design (plan section 8: never drop missing targets).
8. **Real-product pilot.** Out of scope for this lane (brief decision 4). It remains the operational step after PD-F.

## Open for Main

- **Who may be the accepting review for a slice?** Phase A requires the slice acceptance to be recorded locally with a
  person (`acceptance.by`), and the accepting review to have verdict `accept` at the same fingerprint. It does not
  restrict that review to `reviewer.type: human`. Plan section 9 says a model reviewer cannot assert customer
  validation; a rule that only a human review can accept a slice is one line in `_invariants`. Not decided here
  because it is an authority rule.
