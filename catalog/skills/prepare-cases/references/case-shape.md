# Case shape (optional reference)

These examples use the synthetic review-queue package in `templates/product-design/examples/review-queue/`.
`slice.submit-and-queue` lets a requester submit a complete record and see it queued, and rejects an incomplete one.

## Split well: by observable behaviour
| Key | Outcome a user can see | Crosses |
| --- | --- | --- |
| `case.submit-complete-record` | A complete submission is accepted and appears in the reviewer queue as Waiting, oldest first | form, submissions API, submissions table, queue view |
| `case.reject-missing-field` | A submission missing its title or category is rejected with a message naming each field, and the queue is unchanged | form validation, API 422 path, no write |

Each case is demonstrable on its own and carries its exception path. The schema proposal (`contract.submissions-table`) is an
input to the first case: the implementer confirms the column types, and the case says so.

## Split badly
- "Build the submission form", "Build the submissions endpoint", "Create the submissions table". This is one ticket per layer or
  screen. None of them can be demonstrated to a user, and the rejection rule falls between them.
- "Implement the review queue". This is the whole product. It cannot be accepted until every slice is, including the blocked
  reassignment slice.

## A case changeset
Fill `package_revision` and `slice_fingerprint` from `tp-product status DIR --json` (`revision`, and `slices.<id>.fingerprint`).
Then commit the changeset with `tp-product apply DIR CHANGESET --expected-revision <revision>`.

```json
{"schema": "tp-product-changeset", "schema_version": 1, "actor": "Main",
 "summary": "Case drafts for slice.submit-and-queue",
 "operations": [
  {"op": "add", "collection": "cases", "record": {
    "id": "case.reject-missing-field", "title": "Reject a submission missing a required field",
    "slice": "slice.submit-and-queue",
    "acceptance_examples": [
      {"situation": "Title 'Laptop request', no category.", "outcome": "Rejected: 'Category is required'. The queue is unchanged."},
      {"situation": "No title, category 'Hardware'.", "outcome": "Rejected: 'Title is required'. The queue is unchanged."}
    ],
    "dependencies": ["rule.required-fields", "decision.missing-field-rejects", "scenario.submit-missing-category"],
    "actor": "actor.requester",
    "outcome": "A requester who leaves out a required field is told which one, and nothing is queued.",
    "scope": ["Server-side check of title and category", "A message naming each missing field"],
    "exclusions": ["Saving drafts"],
    "feasibility": ["The handler already refuses a missing title; the category check is new"],
    "nonblocking": ["The exact message wording; the owner reviews it on the build"],
    "qa": {"approach": "One API test per missing field, plus one form test", "fixtures": ["An empty queue"]},
    "completion_evidence": ["A test run showing the 422 response for each missing field"],
    "implementation_areas": [{"area": "Submission request handler", "basis": "tentative"}],
    "source": {"package_revision": 3, "slice_fingerprint": "sha256:<slices.slice.submit-and-queue.fingerprint>"}}}
 ]}
```

`actor` through `implementation_areas` are optional (`feasibility` and `nonblocking` are lists of text); per area,
`"basis": "observed"` takes the `source` it was seen in. Contracts and their confidence come from the dependencies: the
export and the packet carry every rule, scenario, decision and contract the dependencies reach, in full. A dependency on
another slice is a delivery dependency: a link with that slice's readiness, never its scope.

Then export and hand over:

```
tp-product cases DIR --slice slice.submit-and-queue --out OUTDIR
tp-product packet DIR --case case.reject-missing-field --out OUTDIR/packets/case.reject-missing-field.md
```
