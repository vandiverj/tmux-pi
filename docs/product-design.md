# Product design packages

Status: the package contract (schema version 1), its validator, a synthetic fixture, and `bin/tp-product` with `init`,
`status`, `validate`, `apply`, `build` and `import-feedback` exist, with atomic commits, external-edit detection,
feedback ingestion, and the offline review page (`lib/product_design/review.py`,
`templates/product-design/runtime/`), and the `product-design` and `prepare-cases` workflow skills with their entry
points, the product review contract and session resume (see "Workflow skills"). Case export (`tp-product cases`,
`packet`) is not built yet; nothing below describes it as available.

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
tp-product build DIR [--expected-revision N] [--json]
                                    write the offline review page builds/ID-rREV.html and record it in the manifest
tp-product import-feedback DIR BUNDLE [--expected-revision N] [--json]
                                    import a feedback bundle exported by a review page; all or nothing
tp-product remap-anchor DIR NOTE TARGET --reason TEXT [--by LABEL] [--expected-revision N] [--json]
                                    anchor a note to another target of the review build, keeping its original
```

| Exit | Meaning |
| --- | --- |
| 0 | Done (or the changeset changed nothing: "no change", same revision) |
| 1 | Invalid package, changeset, result, bundle or control record; nothing was written |
| 2 | Usage |
| 3 | The package cannot be trusted yet, or another writer's bytes are in the way: an interrupted commit, an uncommitted external edit, no commit record, control records that disagree, or a file changed under the commit. `status` and `validate` still report, with a warning |
| 4 | Revision conflict: `--expected-revision` is not the current revision; nothing was written |
| 5 | Another writer holds the lock (waited 30 s) |

Errors print `tp-product: CODE: message`; with `--json`, a `{"ok": false, "code", "message", "details"}` object.
Every refusal has one, a usage error (`usage`, exit 2) and an unreadable changeset file included; no command ends in a
traceback. Reads never write. No command makes a network call or starts anything.

| Code | Exit | Meaning |
| --- | --- | --- |
| `changeset.unreadable` | 1 | The changeset file is missing, a directory or not readable |
| `changeset.invalid` | 1 | Not UTF-8 JSON, wrong format, too large, too deep, or text that is not valid Unicode (`details.problem`: `size`, `depth`, `unreadable`, `encoding`) |
| `op.missing`, `op.shape` | 1 | An operation lacks a field, has an unknown one, or one of the wrong type (`details.operation`) |
| `result.invalid`, `result.unreadable` | 1 | The result does not validate, or a file it would write exceeds what a reader accepts (`details.file`) |
| `control.invalid` | 1 | `commit.json`, a history entry or the journal is malformed, the journal does not match its files, or its staged files would not be read back as committed (the journal is kept; `details.staged` names its files) |
| `io.error` | 1 | The package path cannot be read or written as a directory |
| `package.control-mismatch` | 3 | Control records disagree (commit revision, a history entry's file digests or `base_revision`, a gap in history, journal revision), or would disagree after a commit or recovery (nothing written; on recovery the journal is kept) |
| `commit.external-edit` | 3 | A file changed between inspection and the commit point; the commit is withdrawn and the edit kept |
| `txn.conflict` | 3 | A file was neither the old nor the new content when a commit or recovery came to replace it, or someone wrote to it while it was being replaced; their bytes are kept (`details.kept` names a copy kept in `txn/displaced/`) |
| `history.exists` | 3 | The history entry for the new revision already exists; it is never replaced |
| `adopt.original` | 1 | An adoption would remove or change an original note, input or review |
| `adapter.invalid` | 1 | `build`: `prototype/adapter.json` is unreadable or outside its schema, names an unknown page, contract or operation, repeats an id, or its script or style cannot be inlined (a symlink, outside the package, over 2 MiB, not UTF-8, or holding `</script`, `</style` or `<!--`) |
| `build.exists` | 3 | `build`: the build file exists with other bytes (or is not a regular file), including any recorded build whose bytes are not its recorded digest; it is never replaced and never reported up to date. Also when `builds/` is not a folder (a file) or is a symbolic link: nothing is written through it |
| `build.conflict` | 3 | `build`: the commit failed before its commit point and the build file had been replaced by someone else meanwhile; their bytes are kept |
| `package.build-missing` | 3 | a journal (or `apply --recover`) would record a build whose file is missing or holds other bytes than its digest; the commit record is not written and the journal is kept; `status` reports it as a trust problem. Also a `status` trust problem when a build the manifest records is missing, and a `build` refusal when that build cannot be written back (the package no longer renders bytes that every entry naming that file records, for example two entries that disagree): nothing is written |
| `package.build-changed` | 3 | `status`: a build the manifest records is not a regular file holding its recorded digest (for an entry from before digests: the page the committed package renders for it) (truncated, rewritten, a directory, a symlink). `tp-product build` refuses to replace it (`build.exists`); once it is moved aside, build writes back every recorded build the committed package still renders byte for byte, and refuses (`package.build-missing`) for one it does not |
| `build.unproved`, `package.build-unproved` | 3 | `build` refuses, and `status` does not trust, a recorded build from before digests (no `digest` in its entry) whose bytes cannot be proved: the committed package no longer renders it (its entry records another source), cannot render it (an invalid adapter or asset), or (`status`) a package file differs from the commit record. Check the file against version control, then record its sha256 as the entry's `digest` (or remove the entry) with an `adopt-external-edit` changeset. Nothing is written |
| `bundle.unreadable` | 1 | `import-feedback`: the bundle file cannot be read |
| `bundle.invalid`, `bundle.conflict` | 1 | `import-feedback`: see Feedback ingestion; nothing was written |

## Persistence

Control state is in `DIR/.tp-product/`:

| Path | Tracked with the package | Holds |
| --- | --- | --- |
| `commit.json` | yes | The commit record: revision, the canonical digest of each package file, the digest of each record's acceptance (authority) fields, and the digest of each original note, input and review |
| `history/NNNNNN.json` | yes | One entry per committed revision: actor, summary, evidence, operations, base revision, impact |
| `lock` | no | The writer lock (flock) |
| `txn/` | no | The journal of a commit in progress; present only during, or after an interrupted, commit. `txn/displaced/` holds the live files the commit moved aside |
| `.gitignore` | yes | Written by `init`: ignores `lock` and `txn/` |

A commit takes the lock, checks `--expected-revision`, validates the whole result, writes every new file into `txn/`,
then writes `txn/txn.json` last: that is the commit point. It then replaces the live files, writes the history entry
and the commit record, and removes `txn/`. A crash before the commit point leaves the live package untouched (the next
writer discards the partial journal). A crash after it leaves every file whole (old or new, never partial, or moved
aside into `txn/displaced/`); readers report `package.interrupted`, writers refuse, and `apply --recover` rolls the
commit forward the same way. Recovery refuses (`txn.conflict`) when a live file is neither the old nor the new content:
someone edited it during the interruption.

**No command publishes a state its own reader refuses.** Before anything is written, a commit builds the whole state it
would publish (the package files, the new history entry with the history before it, the commit record) and checks it
with the functions `status` and `validate` use: each file's limits, the package's validity, the shape of the entry and
the record, the history chain, and that the record, the entry and the manifest agree. Any refusal is the code the
reader would report (`result.unreadable`, `result.invalid`, `control.invalid`, `package.control-mismatch`), with
`details.file` naming the file, and nothing is written. `apply`, adoption, `ingest` and `apply --recover` all use this
check. Recovery runs it on the staged files before it replaces anything. If the staged files differ from `txn.json`,
are unreadable or would not be read back as committed, recovery refuses with `control.invalid` or
`package.control-mismatch`. It replaces nothing and keeps the journal and its staged files (`details.journal`,
`details.staged`), so `status` keeps reporting `package.interrupted`.

**A commit never destroys bytes it did not write.** A file is replaced in three steps: the live file is renamed into
`txn/displaced/NAME~TOKEN` (never unlinked), the displaced bytes are checked against what the change was computed from,
and the new file is linked into place, which fails if anything has appeared at the name since. If the displaced bytes
are someone else's, they are put back (unless the name was saved again meanwhile, when the newer save stays) and the
copy is kept; if a file appeared at the name, it stays; after the link the displaced copy is checked once more, for a
writer that held the old file open. Each case is `txn.conflict` with the kept copy named in `details.kept`; the journal
stays, so `status` reports `package.interrupted` and `package.displaced` until the owner moves the copy out of
`.tp-product/txn/` and runs `apply --recover`. Displaced copies that hold the old (or new) content are removed when the
commit completes; nothing else is removed without a user's command. A file the commit does not change is not rewritten.

No write replaces bytes it has not seen. Just before the commit point every live file, `commit.json` and the new
history slot are compared with what the change was computed from; a difference withdraws the commit
(`commit.external-edit`), keeps the other writer's bytes and leaves the package reporting the edit. Roll-forward checks
each file again before replacing it. A history entry is created, never replaced (`history.exists`).

Every file a commit writes is checked against the reader's limits first (8 MiB, nesting depth 64, text that is valid
Unicode), with the same functions the reader uses, so what is committed can always be read back (`result.unreadable`,
nothing written). One function turns a value into bytes (`package.encode`, behind every render and digest), and it
refuses text that is not valid Unicode: a lone surrogate, which JSON's `\ud800` escape can carry. So such text is
refused in every input form: bytes, text, a parsed value, a file, or a value given to the store directly
(`changeset.invalid` or `bundle.encoding` on input, `result.unreadable` on a commit, `control.invalid` on recovery). A
package file holding it is `file.unreadable`. An escaped pair (`\ud83d\ude00`) is one valid character and is kept.

The control records are checked before they are believed: a malformed `commit.json`, history entry or journal is
`control.invalid`; a `commit.json` whose revision is not the newest history entry's, a gap in history, a top entry
whose file digests differ from `commit.json`, or a journal for an unexpected revision makes the package untrusted
(`package.control-mismatch`). Every history entry is checked: its fields and types, its revision against its file name
(`NNNNNN.json`, six digits or more; other names are ignored), and its `base_revision` against the entry before it. Any
`control.invalid` makes the package untrusted in every output (`package.control-invalid`). Writers, adoption included,
refuse until the owner repairs it, and no command writes a revision on top of a history that fails these checks. History
may start after revision 1 (an imported package) when its chain is complete from there. Whether a package or control
file (a document, `commit.json`, a history entry, the journal or a staged file) is absent is decided by the file,
never by its value: one reader (`package.read_file`) returns "absent" only when there is no file, and any JSON value a
file holds (`null`, `false`, `0`, `""`, `[]`, `{}`) is checked as that record and, if it is not one, is malformed. A package whose `commit.json`
is missing but whose history is whole is adopted from the newest history entry (`--expected-revision` is that entry's
revision, whatever the manifest says).

Digests are of canonical JSON, so re-indenting a file is not an edit. Files are written with two-space indentation.

**Direct edits.** A package file whose content differs from the commit record is an external edit: `status` and
`validate` warn (`package.external-edit`, exit 3) and `apply` refuses everything except an `adopt-external-edit`
changeset. Adoption commits the edited files as the next revision when they are valid and change no acceptance
(authority) field and keep every original note, input and review (local bookkeeping, such as a note's `anchor` and
`disposition`, is not part of the original); otherwise it is refused (`adopt.invalid`, `adopt.authority`, `adopt.original`). Impact is not computed for an adopted
edit (the previous content is not kept), but readiness is derived from fingerprints, so an edited dependency still
shows its slices as stale. A package with no commit record can be adopted only while it holds no acceptance record.

## Changesets

```json
{"schema": "tp-product-changeset", "schema_version": 1, "actor": "Main", "summary": "why",
 "evidence": ["optional"], "expected_revision": 3, "operations": [{"op": "set", "id": "rule.x", "fields": {}}]}
```

`expected_revision` is optional; when present it must equal `--expected-revision`. Operations apply in order to a
copy; the result must validate as a whole, or nothing is written. A changeset that changes nothing is not committed.
A changeset has the same limits as a bundle (4 MiB, nesting depth 64) whether it arrives as a file, text or a parsed
value. Each operation's fields are checked against the table below before the package is read: a missing field is
`op.missing`, an unknown field or a wrong type (`id` and other names are text; `record` and `fields` are objects;
`evidence` and `unset` are lists of text; `delivery` is a list) is `op.shape`.

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

`tp-product import-feedback DIR BUNDLE` (`changes.ingest`) imports it in one transaction. A bundle may also carry
`sessions` (review sessions, so a review can be restored in another browser); they are validated and not stored.

- The bundle is refused whole (`bundle.invalid`, nothing written) when its format, version, package, size (4 MiB,
  5000 records) or nesting depth (64) is wrong (the same limits for bytes, text and a parsed value), any record fails the schema, an input targets a record that does not exist, a note carries
  local bookkeeping (`anchor`, `disposition`), or an input is not `pending` or carries a reconciliation.
- Records are deduplicated by id on their semantic content: an id already present with the same content is a
  duplicate, and importing the same bundle again is a no-op (no new revision). The same id with different content, in
  the package or twice in the bundle, refuses the bundle (`bundle.conflict`); the original is never overwritten, and
  an external edit that removed or changed it is not adopted (`adopt.original`), so a re-import cannot replace it.
- Each new note gets an `anchor`: `unresolved` when its journey or scenario no longer exists or its target is not in
  the target registry (below), `current` when its build fingerprint equals the package's content fingerprint, else
  `changed-context`. Its original fields are kept verbatim; a note is never dropped for its anchor. The command lists
  the unresolved notes and says the inputs stay pending until reconciled.
- Anchors are re-evaluated on every import and every build, for every stored note, duplicates included: a note whose
  target, journey or scenario has left the build's registry becomes `unresolved` (its original fields are kept), and
  one whose target is back is anchored again. A change is committed as a `re-anchor` operation; `status` lists the
  unresolved notes from the live evaluation.
- `tp-product remap-anchor DIR NOTE TARGET --reason TEXT` anchors a note to another target of the registry. It goes
  through the same transaction as every change (`remap-anchor` operation): the note keeps its original `target`, and
  `remap` records the new target, the reason, who decided it and the date; the anchor becomes `remapped` (or
  `unresolved` if that target later leaves the registry). The same target and reason again is a no-op; a target outside
  the registry is refused. A bundle may not carry `remap`. `status` lists remapped notes with both targets, and the
  review page shows them the same way.
- JSON is read as RFC 8259 defines it, by the CLI and the page alike: a byte order mark, `NaN`, `Infinity` and
  invalid UTF-8 are refused (`bundle.unreadable` issue), and nothing writes `NaN` or an infinity. Schema patterns
  have their ECMA-262 meaning in both validators: `$` does not match before a final line break.
- Importing notes and inputs changes no design fingerprint and no readiness.

The content fingerprint (`package.content_fingerprint`) is the digest of every design record's fingerprint by id; a
review build records it so notes can be anchored.

### Text and safety

Note text, source excerpts and imported data are stored verbatim and treated as data: never evaluated, executed or
rendered unescaped. Notes carry only synthetic, relevant application state, never cookies, credentials or browser
profiles. Validation reads only the three package files, makes no network call and starts nothing.

## The review build

`tp-product build DIR` renders the committed, trusted package into one HTML file a reviewer opens from disk
(`file://`), writes it as `builds/<id>-r<revision>.html` and records it in the manifest's `builds` list (`revision`,
`fingerprint`, `source`, `file`, `digest`: the sha256 of the file's bytes) through the same transaction as every
other change, so the build moves the package to the next revision.

- **The build file and the commit point.** The file is written before the commit point. If the commit fails before
  that point, the file is removed, but only while it is still this build's file with this build's bytes (someone
  else's bytes are kept). After the commit point the file is never removed. A commit, and recovery of an interrupted
  one, checks every build it records just before the commit record and refuses (`package.build-missing`, journal
  kept) when a file is absent or holds other bytes; `status` reports such a journal as a trust problem. The commit
  point is `txn/txn.json` landing: once it is in place nothing in the same process removes it or the build file,
  whatever is raised (an interrupt included); recovery decides from then on.
- **Every recorded build is checked.** `status` (`store.trust_problems`) verifies that each build in
  the manifest's `builds` list is a regular file whose bytes are proved: its recorded digest, or for an entry from
  before digests the page the committed package renders for it (`review.build_prover`, the same check for `status`
  and `build`). A missing one is `package.build-missing`, any other bytes `package.build-changed`, an entry from
  before digests that the package does not render (another source) or cannot render `package.build-unproved`, and
  status exits non-zero. `tp-product build` checks the same list first, with the same prover: a recorded build that is
  missing (its folder too) and that the committed package still
  renders byte for byte (same source, same digest) is written back in place, its folder made as for a new build
  (`restored`; the text says "restored", never "nothing was written"); one that is missing and no longer renders is
  refused by name (`package.build-missing`, exit 3), one that holds other bytes is refused (`build.exists`), and one
  whose bytes cannot be proved is refused (`build.unproved`): it is never reported up to date. A
  `builds/` that is a file or a symbolic link is refused (`build.exists`); a build is never written through a link.
  Each refusal writes no build file, so `status` and `build` agree afterwards. An older build that is lost for good
  must be put back from version control before the package builds again.
- **Every entry is its own proof.** When several entries in `builds` name one file, each entry must hold for its
  bytes: its digest, or (an entry from before digests) the page the committed package renders for that entry. The
  file is up to date only when it satisfies all of them, and a missing file is written back only with a page that
  satisfies all of them. Entries that disagree (another digest, revision or source) are refused by name: a present
  file is `build.exists` or `build.unproved`, and a missing one `package.build-missing` ("is named by N recorded
  entries ... that no single page satisfies"). After writing back, the list is checked again and anything still
  unproved is `build.exists`.
- **The whole plan is proved before any write** (`review._plan`). Entries are one file when their names are equal
  once each component is put in Unicode NFC form and case-folded, whatever the disk: such a group is written back
  once, with a page that satisfies every entry in it, or refused by name when they disagree. A plan in which one
  target would be a folder above another (an entry under a recorded build file), or a target lies below an existing
  file or a symbolic link, is refused (`build.exists`) before anything is written; so is a new build whose name is
  taken by other bytes or by a build the plan writes back with another page. A build that is refused writes nothing;
  one that succeeds leaves `status` clean.
- **Built from committed state.** A build first re-evaluates note anchors against the current registry; if that changes
  stored anchors, it commits that change as its own revision before it renders, so the page shows only committed
  state and building again reports up to date.

- **Offline.** The runtime (`templates/product-design/runtime/`), the prototype and the package data are inlined. The
  page's Content-Security-Policy is `default-src 'none'` (so no connection, image, frame or font), with the
  inlined scripts and style allowed only by their sha256 hashes, and `base-uri` and `form-action` set to `'none'`.
  Nothing is fetched and no CDN is used. Scripts and styles are inlined with their line breaks normalized to LF (as
  the HTML parser delivers CRLF and CR), so the policy's hashes match what the browser hashes; a NUL is refused.
- **Provenance.** The page records the package id, revision, generator, `content_fingerprint` (the design content
  that notes carry) and `source_fingerprint` (everything the page shows except the revision counter).
- **Stable.** Building an unchanged package again reports "up to date" and writes nothing, but only when the recorded
  build file holds exactly the recorded digest (an entry from before digests: exactly the page this content renders
  for it); otherwise it is `build.exists` (or `build.unproved` when there is nothing to compare with), never up to
  date. Importing notes changes no
  source fingerprint; a design change does. Identical bytes already at the build path are reused. A file there with
  other bytes is refused (`build.exists`, exit 3) and never replaced.
- **Inert data.** Package text reaches the page only inside one JSON block whose `<`, `>`, `&`, U+2028 and U+2029 are
  escaped. The page reads it with `JSON.parse` and renders it as text nodes, through one element builder that refuses
  event-handler, URL and style attributes. The title is HTML-escaped.

### The page

- **Views:** Overview, Journeys, Product experience (the prototype), Questions and decisions, Engineering (the simulated
  events each business action emitted, with the contract they belong to) and Feedback (summary, export, import). The
  URL hash names the view. Browser back and forward switch views and never replay or repeat an action.
- **Shared state:** the prototype state is the adapter's seed folded through its reducer with the session's action
  list. Reset (no actions), Back (one fewer), reload and restore all replay from the seed, so an action never runs
  twice and none is lost.
- **Guided and unguided tasks:** a scenario can be followed step by step (guided) or attempted without steps
  (unguided). Help requests and assisted steps are counted separately, and the outcome is recorded per task.
  Back and Reset in a guided task reopen it: undoing a step that completed it sets it to in progress again. A completed
  unguided task stays completed after Back or Reset: its outcome is an observation (the reviewer reached the result
  without steps), and undoing the actions afterwards does not unmake that observation.
- **Resuming:** resuming a session (from this browser or an imported bundle) resumes its task in progress: the
  scenario, the mode, a guided task's step (derived from the checkpoint's actions) and the help and assistance counts.
  The next note carries that context.
- **Notes:** `n`, the toolbar button or a right click starts a note. In note mode, a click (or Enter or Space on a
  focused target) picks a prototype target instead of acting. The note captures the page, journey, scenario, step,
  target, a copy of the state and the checkpoint at that moment. While a note is open the prototype is paused: an
  action is refused with a visible message until the note is saved or Escape closes it. An open note keeps its whole
  draft (text and category) across every redraw of the page.
- **Answers and proposals** on open questions and proposed rules and decisions are stored as `pending`. A label or
  text that claims approval changes nothing.
- **Storage:** everything is saved in the browser's `localStorage` under `tp-product-review:v1:<package id>`. When
  storage is denied, or the saved data cannot be read, the page says so and keeps working for the visit. It never
  overwrites data it could not read. Any stored value (not JSON, `null`, `0`, an array, a partial or inconsistent
  state, any depth) is checked field by field before use; one that fails is left untouched under its key, a notice
  says so, and the page starts fresh. Saved state has its own limits, not one bundle's: it may hold any number of
  records (an export splits them), and every other rule an import applies (text, schema, bookkeeping, depth). The page
  stores state only after checking that it would read that same text back; when it would not, it keeps the last state
  stored and says so. The notice for saved data it refused, for any reason (too deep to parse, not JSON, cut short,
  an unknown field, a bad record, a conflicting id), offers the stored bytes as a download as they are and, whenever
  at least one note, answer or review session in them is importable on its own, an export of those records as
  importable parts.
  The records are found in the stored text without parsing all of it, by one rule whatever surrounds them (other
  bytes before or after, a BOM, the text cut short, a refused part before, between or after them, any wrapping key
  or array): every complete JSON object in the text, in the order it starts, is a candidate, and it is offered when
  the CLI's import would accept it on its own as a note, an answer or a session. Its inner objects are then its
  content; a candidate that is not accepted hides nothing inside it. Position and key names decide nothing. Each is
  checked for depth and size before it is parsed and then against every rule the CLI's import applies to it on its
  own (numbers as the CLI reads them: `1.0` or an integer beyond 2^53 is kept; the value checked is the one the CLI
  keeps, the last member of a name, so a shadowed `1e400` before a finite member is offered and an infinite one that
  is kept is not). A record is offered only when every supported CLI accepts it: the supported CLIs run on Python 3.9
  and Python 3.14, and Python 3.14 refuses an integer of more than 4300 digits anywhere in the text (a shadowed member
  too), so such a record is never offered, by name; the others that look like records are listed by id with the reason, both records of a conflicting id among
  them. An offered record is exported as its exact stored text, never re-serialized. A review session counts like a
  note or answer: one the CLI accepts on its own is offered, counted and exported, alone or with others (the saved
  open session is such a candidate, recovered as it was stored; it is never made up from other fields). The offered
  records are packed, in order, into parts by measurement, never by estimate: a part is offered only once its exact
  text has been measured within one bundle's bytes and records and checked with every import rule; a part that is not
  is split again, down to one record, so a record that is importable alone is always offered, and only a record that
  is not importable alone is left out, by name. The refused data is never
  taken into the page's state. What was last exported is saved as a short hash, so exporting never doubles what is stored.
- **Export and import:** Export downloads the feedback bundle (notes, inputs and sessions) and lists every part on the
  page with a save control and its JSON to copy, in case a download was blocked; the records count as exported only
  once every part is listed and its download started. Import applies the CLI's rules (schema, package, version,
  limits, text that is valid Unicode in every string and key at any depth, pending inputs, no local bookkeeping,
  conflicting ids) and is all or nothing. Conflicting ids are one rule, shared by import, export, saved state and its
  recovery: an id held twice with different content refuses the whole bundle (the CLI's "the bundle holds two
  different records with this id"); the same record twice is one record. Saved state holding such an id is refused
  when it is loaded, so the page never exports it. The page cannot see the notes a package already holds: an id the
  package holds with other content is refused by the CLI on import (`bundle.conflict`), never by the page. Nesting depth is measured without recursion, so any depth
  gets the 64-level refusal. A number the page could not export as written (an integer beyond 2^53, `-0`, an
  exponent, a trailing zero, a long decimal) is refused visibly, so whatever the page exports imports into the CLI as
  it was. Ids are looked up as own properties only: `constructor`, `__proto__` or `toString` are ids like any other,
  and an unknown field with such a name is refused as the CLI refuses it. Export checks each bundle it writes with
  every import rule (bytes, records, depth, text, schema, bookkeeping, numbers, conflicting ids), and the parts
  together with the same rules but the size of one bundle; feedback larger than one bundle is
  exported as several bundles (`-part-N-of-M`), each importable on its own, packed the same way as a recovery (each
  part measured on its exact text, split again until it fits; sessions, notes and answers alike), and an export that
  cannot be made importable (a record no bundle can hold on its own, named) is refused with nothing marked exported. A note or answer that would not be importable in a bundle on its
  own is refused when it is saved. Importing a bundle into a fresh browser and resuming its
  session restores the same prototype state. A note whose target is not in this build is listed as unresolved, never
  dropped.

### The prototype adapter

A package may have `prototype/adapter.json` (`tp-product-adapter`, schema version 1), which declares:
- `pages`;
- `targets`: a target with `instances` is a prefix, so `queue.row` matches `queue.row.s-1`;
- `actions`: a business action names the contract operation it exercises;
- `seeds`;
- the `script`, plus an optional `style`.

The script calls `TPReview.registerPrototype({id, seeds, reduce, render, conditions})`:
- `seeds` maps each seed id to a function that returns the initial state.
- `reduce(state, action)` is pure. It returns `{state, ok, events}`, and is the only way state changes.
- `render(ui, state)` draws the current page. It builds elements with `ui.h`, marks review targets with
  `ui.target(el, id, label, onActivate)` and dispatches with `ui.act(action, target, input)`.
- `conditions` maps a scenario id to a check that the scenario's goal is observable in the state.

The target registry is the one list a note may be anchored to: the views, every design record id and the adapter's
targets. The page and `import-feedback` use the same rule.

## Workflow skills

Two selective catalogue skills (`config/skill-catalog.json`) guide Main. They are instructions only: they approve
nothing, open no ticket and publish nothing.

- **`product-design`** (`catalog/skills/product-design/SKILL.md`): frame (with `product-fit`), investigate with bounded
  workers, shape the package, build and inspect the review page, reconcile feedback, and persist the next action.
  Observed evidence (a claim with a source) is kept apart from generated hypotheses; AI role-play is never customer
  validation. Optional reference: `catalog/skills/product-design/references/techniques.md`.
- **`prepare-cases`** (`catalog/skills/prepare-cases/SKILL.md`): turns a slice whose effective readiness is
  `ready-for-case-preparation` into small case drafts, each an observable behaviour with its exception path, never one
  per screen, endpoint or table. Drafts are committed as `cases` records with `tp-product apply`. Reference with a
  worked changeset: `catalog/skills/prepare-cases/references/case-shape.md`.

Selection: an explicit marker (`/product-design`, `/prepare-cases`) or a narrow phrase ("start a product design
package", "resume the product design workflow", "do product discovery", "prepare cases for slice", "break the slice
into cases" and the others listed in the catalogue). Incidental words such as "product", "design" or "case" select
nothing. At most three skills are selected, explicit markers first.

Entry points:

| Harness | Command | How it resolves |
| --- | --- | --- |
| Claude | `/product-design`, `/prepare-cases` | `claude/plugins/tmux-pi/commands/<name>.md` calls the `get_skill` tool with the id; the id must be in `ON_DEMAND_SKILLS` in `claude/mcp/subagents.mjs` |
| Pi | `/product-design`, `/prepare-cases` | `tp-pi` sets `PI_CODING_AGENT_DIR` to `agent/`, so `agent/prompts/<name>.md` is a prompt template; the catalogue extension first selects the skill from the raw `/name …` input |
| Worker packet | (selected by the task text) | `tp-skills --mode packet` inlines the selected SKILL.md |

**Product review.** `templates/product-design/review-contract.md` is a contract for `tp-review --contract FILE` (or
`tp-agent review ID --contract FILE`). It adds the product criteria (value assumptions, journeys, rules, exceptions,
testability, scope, evidence). It asks the reviewer to compare claims with their sources and to exercise the built
prototype, and it keeps product findings and prototype defects apart, each with its own dispositions. It changes what
is judged, not who judges: the reviewer and its provider independence come from the usual review plan.

**Resume.** The manifest (`activity`, `next_action`, set with a `set-manifest` changeset) is the durable record. At
each pause Main also records `tp-agent objective "product-design package=<absolute DIR> revision=<N> slice=<id or
none> next: <text>"`. A new main in the same tmux session sees it in the session index. It then runs
`tp-product status DIR`, and the status wins where the two disagree. A different tmux session has its own state and
no objective: it resumes from the package path alone.

## Known limits

The tools run on packages and saved states they wrote themselves, changed through the documented commands
(`docs/plans/realistic-use-bar.md`). Inputs only a hand edit produces are known limits: one line each, the input shape
and what the tool does. They are not fixed round by round.

- **Manifest `builds` entries naming one file in two spellings** (case or Unicode form, by hand): treated as one file;
  agreeing entries are written back once, disagreeing ones refused by name, nothing written. On a disk that tells such
  names apart, the other spellings are written only while still absent. Tested on one case-insensitive disk (APFS).
- **Manifest `builds` entries where one is a folder above another, or below an existing file** (by hand): the build is
  refused (`build.exists`) before anything is written.
- **Saved data with duplicated JSON members**: recovery checks the value the CLI keeps (the last member); a refused
  record is listed by name, nothing is lost (the stored bytes stay and can be downloaded).
- **Numbers of more than 4300 digits in saved data**: the record is never offered, because Python 3.14's CLI refuses
  it (Python 3.9's accepts it); it is listed by name and stays in the stored bytes.
- **Records sized within a few bytes of one bundle's limit** (4 MiB): parts are measured on their exact text and split
  again until each fits; a record over the limit on its own is left out of a recovery by name, or refuses an export
  by name with nothing marked exported. Recovery packing was attacked at limit-2..+2; the normal export's packing
  uses the same measured packer but was not attacked at those byte boundaries.
- **The page's own import** refuses a number whose JavaScript round trip would change it (an integer beyond 2^53,
  `-0`, an exponent, a trailing zero), visibly; recovery keeps such numbers as their exact text.
- **A package path containing spaces** in the resume objective: the `package=` field ends at the first space. Give the
  path to `tp-product status` by hand; the manifest is the record either way.
