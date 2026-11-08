# LS1 round 2 (review-coder-891eb3cf5e-133721: REQUEST_CHANGES)

Same lane `local-summarizer`, step S1, same touches. Read the review: tasks/review-coder-891eb3cf5e-133721/result.md and report.md.

Fix:
1. **F1 route-only shape.** `tp-route` output has no `shape` field (model_router._finish omits it) and a candidate-shape stop goes to stderr. Record what the real tool gives: level, model, source, reason, and for a stop the stderr text and exit code (classify "outside the qualified local shapes" as `shape_not_qualified`). If the shape itself is needed, get it from the router's selection log or an existing `--json`/debug path if one exists; do not change lib/ here. Replace the stub's invented `shape` interface with the real output format, and test against a captured real output sample (save one in the test, no live call).
2. **F2 lenient facts.** Audit every `required` entry in answers.jsonl: each list/sequence chore (steps, states, exports, usages) must require every item, not an alternation that one item satisfies. Add a test that, for each chore, a reply with only the first required fact fails.
3. **F3 citations.** Any `path:line` (any extension or none, also bare `file:line`) is a citation; a citation of a file outside `cite_files`, missing, or out of range is bad.
4. **F4 (Main's decision): citations are required.** A reply passes only if each required fact is supported by a citation on the same line or bullet: `path:line`/`path:a-b`, or `Line N`/`Lines a-b` under the nearest preceding heading or bullet that names a cited file (resolve it and check it like any citation). A fact without a citation is `missing_citation`; any bad citation fails the reply. Main's smoke reply (tasks/coder-891eb3cf5e/main-evidence/smoke.jsonl) is a test case: score it under the new rules and report what it gets, including whether its heading-relative line numbers are right.
5. **F5 accepted** (TP_LOCAL_CONFIG and TP_PI_NODE kept; documented). F6 no change.
6. Same verification as before, both interpreters, both modes. Do not run the live model.
