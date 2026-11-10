# Product design packages

Status: the package contract (schema version 1), its validator and a synthetic fixture exist. The `tp-product` command,
the review runtime, the skills and case export are not built yet; nothing below describes them as available.

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
| `ready-for-case-preparation` | Accepted: carries `acceptance` (who, when, the accepting review, the slice fingerprint, `origin: "local"`) |
| `implemented-verified` | Built and verified; also carries the acceptance. Never inferred from a working prototype |

The acceptance's review must have verdict `accept`, include the slice in its scope, and have reviewed the same
fingerprint. Readiness is derived per slice, never for the package as a whole:

- **Blockers** (any state): a blocking, unresolved question that names the slice; a superseded rule or decision in the
  closure. In addition, for the two accepted states: a rule in the closure that is not accepted or is accepted without
  examples, and a decision in the closure that is not accepted (or changed after its acceptance).
- **Stale**: the slice has an acceptance and its fingerprint differs from the slice's current fingerprint.
- **Effective** readiness is `blocked` if there is any blocker, else `stale` if stale, else the stored state.

One slice can be blocked while another is ready. A structurally valid, ready slice says only that the recorded checks
pass; it does not establish that the business rules are right.

### Text and safety

Note text, source excerpts and imported data are stored verbatim and treated as data: never evaluated, executed or
rendered unescaped. Notes carry only synthetic, relevant application state, never cookies, credentials or browser
profiles. Validation reads only the three package files, makes no network call and starts nothing.
