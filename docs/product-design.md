# Product design packages

Status: the package contract (schema version 1), its validator, a synthetic fixture, and `bin/tp-product` with `init`,
`status`, `validate` and `apply` exist, with atomic commits, external-edit detection and feedback-ingestion primitives
in `lib/product_design/changes.py`. The review HTML build, the `import-feedback` command, the skills and case export
are not built yet; nothing below describes them as available.

A product design package is the durable record of one product design effort: facts and their sources, scenarios, rules,
questions, decisions, proposed API and data contracts, delivery slices and the reviews of them. It lives in the
product's own workspace (for example `product-design/<package-id>/` in the product or context repository), not in
tmux-pi. The compiled review HTML is an output of a package, never its editable source.

## Package contract (schema version 1)

### Layout

| File | Schema name | Holds |
| --- | --- | --- |
| `package.json` | `tp-product-package` | Manifest: id, title, owner, objective, source roots, revision, activity, next action, retained builds |
| `design.json` | `tp-product-design` | Semantic records: sources, claims, actors, journeys, rules, scenarios, questions, decisions, contracts, slices, cases, measurements |
| `reviews.json` | `tp-product-reviews` | Review bookkeeping: notes (feedback), inputs (reviewer answers and proposals), reviews |

Each file names its schema and `schema_version` (the integer `1`; `1.0`, `"1"` and `true` are refused). Every
`package` field, and every note's `package`, must equal the manifest `id`. The JSON Schema is
`templates/product-design/schema/package-v1.schema.json`; the rules it cannot express are in
`lib/product_design/package.py`. A synthetic package is in `templates/product-design/examples/review-queue/`.

| Record (plan name) | Collection | Id prefix |
| --- | --- | --- |
| Source | `sources` | `source.` |
| Claim | `claims` | `claim.` |
| Actor and journey | `actors`, `journeys` | `actor.`, `journey.` |
| Rule | `rules` | `rule.` |
| Scenario | `scenarios` | `scenario.` |
| Question | `questions` | `question.` |
| Decision | `decisions` | `decision.` |
| Contract (API, data, integration) | `contracts` | `contract.` |
| Slice | `slices` | `slice.` |
| Case | `cases` | `case.` |
| Measurement | `measurements` | `measurement.` |
| Feedback | `notes` | `note.` |
| Review answer or proposal | `inputs` | `input.` |
| Review | `reviews` | `review.` |
| Source root (in the manifest) | `source_roots` | `root.` |

### Identifiers

- A record id is `<kind>.<slug>[.<slug>...]`: the kind is the collection's prefix above; a slug is lower-case letters
  and digits in words joined by single hyphens. At most 96 characters. Examples: `rule.assigned-reviewer`,
  `note.session-b.12`. The package id is a single slug of at most 64 characters.
- Ids are unique across the whole package (all three files).
- An id is the record's identity. It is never derived from the title, and editing a title, label or other bookkeeping
  field changes no identity and no fingerprint.
- Ids are never reused or renamed. A record that is replaced is superseded (rules, decisions) or withdrawn (questions),
  and stays in the package so older notes, reviews and cases remain intelligible.
- Every reference names an existing record of an allowed kind. Two kinds of field are exempt because they describe the
  build a note was captured from, not the current design: a note's `journey`, `scenario` and `target.id`. Their
  match to the current design is recorded in the note's `anchor` (`current`, `changed-context`, `unresolved`,
  `remapped`).

### Revisions and fingerprints

- `package.json` `revision` is an integer that goes up by one with every committed change to the package, semantic or
  bookkeeping. It is the concurrency token (a writer states the revision it expects); it is not used for readiness.
- Every record type marks some fields as bookkeeping (`x-bookkeeping` in the schema): titles, a slice's `readiness`, a
  decision's `acceptance`, a note's `anchor` and `disposition`, an input's `status` and `reconciliation`, a case's
  `publication`, a claim's `note`. Everything else is semantic content.
- A record fingerprint is `sha256:` + the SHA-256 of the canonical JSON (keys sorted, no spaces, UTF-8) of
  `{"kind": KIND, "record": RECORD without its bookkeeping fields}`.
- Fields marked `x-dep` are semantic dependencies: a slice's scenarios, rules, decisions and contracts; a scenario's
  rules and contracts; a rule's decisions; a contract's rules. A slice's dependency closure is everything reachable
  through them. Claims, sources, journeys, questions and other slices are not followed.
- A slice fingerprint covers the slice's own semantic content and the record fingerprint of every record in its
  closure. Changing a rule, scenario, decision or contract changes the fingerprint of every slice that reaches it;
  adding a note, a review, an input or a disposition changes none.
- Impact through links that are not recorded (a claim, a source revision, an unlinked rule) is not detected. It is for
  human or model review; the package does not claim complete semantic impact detection.

### Workflow state

The manifest's `activity` says where the work is: `frame`, `investigate`, `shape`, `build-inspect`, `review-reconcile`,
`prepare-cases` or `learn`. It is a description for resuming work, not a gate. `next_action` (text, owner, date set) is
updated at every meaningful pause; with the open questions it is what a new session resumes from. Pending questions
are not stored separately: they are the questions whose status is `open` (or `answered` by a decision that is not
accepted).

### Statuses

- **Claim**: `observed-current` (what the current system does), `documented-requirement`, `proposed`, `assumption`,
  `unresolved`. An observed or documented claim cites at least one source. Current behaviour is not desired behaviour.
- **Source**: pinned by a `revision`, a content `digest`, or both.
- **Rule**: `proposed`, `accepted`, `superseded`. A superseded rule names its successor in `superseded_by`, and the
  successor names it back in `supersedes`. Decisions follow the same three statuses and supersession rule.
- **Decision acceptance**: an `accepted` decision carries `acceptance`: who accepted it locally, when, the evidence
  (at least one item), the decision's record fingerprint at that moment, and `origin: "local"`. A decision whose
  content no longer matches its acceptance fingerprint counts as not accepted. A `proposed` decision carries no
  acceptance.
- **Question**: `open`, `answered` (names `decision`), `accepted-uncertainty` (records scope, rationale, revisit
  trigger, who accepted it and when), `withdrawn` (records the reason). A question is resolved when it is
  accepted-uncertainty, withdrawn, or answered by a decision that is accepted. A `blocking` question names the slices it
  blocks.
- **Input** (a reviewer's answer or proposed choice): `pending` until a local reconciliation event (who, when, outcome,
  optionally the decision, `origin: "local"`) makes it `reconciled` or `declined`. Its text never accepts a decision,
  answers a question or makes a slice ready, whatever it says; a reviewer label is descriptive, not authority.
- **Review**: scope (record ids), the fingerprint reviewed, reviewer label and type (`human` or `model`), verdict
  (`accept`, `request-changes`, `comment`), findings, evidence.

### Slice readiness

A slice's stored `readiness.state` is one of:

| State | Meaning |
| --- | --- |
| `draft` | Being shaped |
| `ready-for-human-review` | Structurally complete; waiting for a human review |
| `ready-for-case-preparation` | Accepted: carries `acceptance` (the responsible person, when, the accepting review, the decision evidence, the slice fingerprint, `origin: "local"`) |
| `implemented-verified` | Built and verified; keeps the acceptance and adds `delivery` (who, when, at least one item of delivery evidence: `commit`, `merge-request`, `release`, `ci-run` or `ticket`, `origin: "local"`) |

Who can make a slice ready (brief decision 9; `package.acceptance_problems`, applied both by validation and by
readiness):

- Any review, a model review included, can bring a slice at most to `ready-for-human-review`. Those states carry no
  acceptance and no delivery.
- `ready-for-case-preparation` needs the local acceptance event above, and its review must be a `human` review with
  verdict `accept` that includes the slice in its scope and reviewed the same fingerprint. A model review there is
  refused (`acceptance.not-human`); the slice reads as blocked, not ready.
- `implemented-verified` needs delivery evidence (`delivery.missing` otherwise). A ref naming one of the package's
  review builds (`manifest.builds[].file`) is refused (`delivery.prototype`): a prototype run is not delivery.
- Imported records never count: a note or input whose reviewer label or text claims approval changes nothing, and an
  imported input is always `pending` (see Feedback ingestion).

Readiness is derived per slice, never for the package as a whole:

- **Blockers** (any state): a blocking, unresolved question that names the slice; a superseded rule or decision in the
  closure. In addition, for the two accepted states: a rule in the closure that is not accepted or is accepted without
  examples, and a decision in the closure that is not accepted (or changed after its acceptance).
- **Stale**: the slice has an acceptance and its fingerprint differs from the slice's current fingerprint.
- **Effective** readiness is `blocked` if there is any blocker, else `stale` if stale, else the stored state.

One slice can be blocked while another is ready. A structurally valid, ready slice says only that the recorded checks
pass; it does not establish that the business rules are right.

## The `tp-product` command

```
tp-product init DIR --id ID --title TITLE [--owner NAME] [--objective TEXT]
tp-product status DIR [--json]      revision, activity, next action, per-slice readiness and blockers, stale cases,
                                    open questions, pending inputs, notes without a disposition, unresolved anchors,
                                    slices awaiting human review or needing re-review
tp-product validate DIR [--json]    schema, ids, references, invariants, versions, commit state
tp-product apply DIR CHANGESET --expected-revision N [--json]
tp-product apply DIR --recover      finish (or discard) an interrupted commit
```

| Exit | Meaning |
| --- | --- |
| 0 | Done (or the changeset changed nothing: "no change", same revision) |
| 1 | Invalid package, changeset, result or bundle; nothing was written |
| 2 | Usage |
| 3 | The package cannot be trusted yet: an interrupted commit, an uncommitted external edit, or no commit record. `status` and `validate` still report, with a warning |
| 4 | Revision conflict: `--expected-revision` is not the current revision; nothing was written |
| 5 | Another writer holds the lock (waited 30 s) |

Errors print `tp-product: CODE: message`; with `--json`, a `{"ok": false, "code", "message", "details"}` object.
Reads never write. No command makes a network call or starts anything.

## Persistence

Control state is in `DIR/.tp-product/`:

| Path | Tracked with the package | Holds |
| --- | --- | --- |
| `commit.json` | yes | The commit record: revision, the canonical digest of each package file, the digest of each record's acceptance (authority) fields |
| `history/NNNNNN.json` | yes | One entry per committed revision: actor, summary, evidence, operations, base revision, impact |
| `lock` | no | The writer lock (flock) |
| `txn/` | no | The journal of a commit in progress; present only during, or after an interrupted, commit |
| `.gitignore` | yes | Written by `init`: ignores `lock` and `txn/` |

A commit takes the lock, checks `--expected-revision`, validates the whole result, writes every new file into `txn/`,
then writes `txn/txn.json` last: that is the commit point. It then replaces the live files, writes the history entry
and the commit record, and removes `txn/`. A crash before the commit point leaves the live package untouched (the next
writer discards the partial journal). A crash after it leaves every file whole (old or new, never partial); readers
report `package.interrupted`, writers refuse, and `apply --recover` rolls the commit forward. Recovery refuses
(`txn.conflict`) when a live file is neither the old nor the new content: someone edited it during the interruption.

Digests are of canonical JSON, so re-indenting a file is not an edit. Files are written with two-space indentation.

**Direct edits.** A package file whose content differs from the commit record is an external edit: `status` and
`validate` warn (`package.external-edit`, exit 3) and `apply` refuses everything except an `adopt-external-edit`
changeset. Adoption commits the edited files as the next revision when they are valid and change no acceptance
(authority) field; otherwise it is refused (`adopt.invalid`, `adopt.authority`). Impact is not computed for an adopted
edit (the previous content is not kept), but readiness is derived from fingerprints, so an edited dependency still
shows its slices as stale. A package with no commit record can be adopted only while it holds no acceptance record.

## Changesets

```json
{"schema": "tp-product-changeset", "schema_version": 1, "actor": "Main", "summary": "why",
 "evidence": ["optional"], "expected_revision": 3, "operations": [{"op": "set", "id": "rule.x", "fields": {}}]}
```

`expected_revision` is optional; when present it must equal `--expected-revision`. Operations apply in order to a
copy; the result must validate as a whole, or nothing is written. A changeset that changes nothing is not committed.

| Operation | Fields | Notes |
| --- | --- | --- |
| `add` | `collection`, `record` | New design records and reviews (append-only). A new slice starts `draft` or `ready-for-human-review`. Not notes or inputs |
| `set` | `id`, `fields` and/or `unset` | Design records only; never the id |
| `replace` | `id`, `record` | Keeps the record's acceptance fields |
| `supersede` | `id` (rule or decision), `record` | Adds the successor with `supersedes`; the old record becomes `superseded` with `superseded_by` |
| `accept-decision` | `id`, `by`, `date`, `evidence` | Records the local acceptance and the decision's fingerprint |
| `set-readiness` | `slice`, `state`, then per state: `review`/`evidence` (optional); `by`, `date`, `review`, `evidence`; `by`, `date`, `delivery` | Decision 9; see Slice readiness. `blocked` and `stale` are derived and refused |
| `reconcile-input` | `id`, `status` (`reconciled`/`declined`), `by`, `date`, `outcome`, optional `decision` | A link to a decision does not accept it |
| `accept-uncertainty` | `question`, `scope`, `rationale`, `revisit_trigger`, `by`, `date` | |
| `set-disposition` | `note`, `kind`, `rationale`, `by`, `date`, optional `link` | Bookkeeping only |
| `set-manifest` | `fields` | Only `title`, `owner`, `objective`, `source_roots`, `activity`, `next_action` |
| `adopt-external-edit` | `by`, `reason` | Must be the only operation |

**Authority fields** (a decision's `acceptance`, a slice's `readiness`, an input's `status` and `reconciliation`, a
question's `uncertainty`) are written only by the acceptance operations. `add`, `set` and `replace` refuse them
(`op.authority`), a bundle cannot carry them, and an external edit that changes them is not adopted. An acceptance made
earlier in a changeset is re-checked against the final result (`acceptance.blocked`).

**Impact.** Every commit reports, and records in history: the design records whose fingerprint changed; slices that
became stale (their accepted content changed: they need a new review); changed records no slice reaches through
dependency links, with the records that name them (`unlinked`, for review by hand); and draft cases whose slice
changed. Review bookkeeping (notes, inputs, reviews, dispositions, titles) changes no fingerprint and stales nothing.

## Feedback ingestion

A feedback bundle is what a review build exports:

```json
{"schema": "tp-product-feedback", "schema_version": 1, "package": "<package id>",
 "exported": "2026-11-10T09:00:00Z", "notes": [], "inputs": []}
```

`changes.ingest(DIR, bundle)` (the `import-feedback` command comes with the review build) imports it in one
transaction:

- The bundle is refused whole (`bundle.invalid`, nothing written) when its format, version, package or size (4 MiB,
  5000 records) is wrong, any record fails the schema, an input targets a record that does not exist, a note carries
  local bookkeeping (`anchor`, `disposition`), or an input is not `pending` or carries a reconciliation.
- Records are deduplicated by id on their semantic content: an id already present with the same content is a
  duplicate, and importing the same bundle again is a no-op (no new revision). The same id with different content, in
  the package or twice in the bundle, refuses the bundle (`bundle.conflict`); the original is never overwritten.
- Each new note gets an `anchor`: `unresolved` when its journey or scenario no longer exists, `current` when its build
  fingerprint equals the package's content fingerprint, else `changed-context`. Its original fields are kept verbatim.
- Importing notes and inputs changes no design fingerprint and no readiness.

The content fingerprint (`package.content_fingerprint`) is the digest of every design record's fingerprint by id; a
review build records it so notes can be anchored.

### Text and safety

Note text, source excerpts and imported data are stored verbatim and treated as data: never evaluated, executed or
rendered unescaped. Notes carry only synthetic, relevant application state, never cookies, credentials or browser
profiles. Validation reads only the three package files, makes no network call and starts nothing.
