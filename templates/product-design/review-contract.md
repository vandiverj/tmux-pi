# Product review contract

Main attaches this file with `tp-review --contract <tmux-pi>/templates/product-design/review-contract.md <task-id>`, or with
`tp-agent review <task-id> --contract …`, for a task that produced or revised a product design package. It
does not change who reviews: the reviewer is chosen by the usual rules, from a different provider than the author unless the
user approved otherwise. Ordinary code review is unchanged. For code the task changed, the code-review rules apply as usual.
For the package itself, judge it against this contract. Tests or mutants are not demanded for package content: say "not
applicable: package content" where a code-review item does not fit.

## What to read
- The package directory the task names: `package.json`, `design.json`, `reviews.json`. Run `tp-product status DIR` and
  `tp-product validate DIR`. Both are read-only.
- **The underlying sources, not only the summaries.** Pick the sources behind the claims that carry the most weight: the cited
  file at its revision, the document excerpt, the case. Compare each claim with its source. A reviewer who reads only generated
  summaries inherits the author's mistakes.
- **The built prototype.** Open the newest `builds/<id>-r<revision>.html` from disk, or build one in a copy of the package. Exercise
  the important scenarios: the normal path, the exception path, and a change of state seen on another page. Do not only read it.

## Criteria
Judge each criterion for the slices in scope:
1. **Value assumptions.** The outcome and why it matters are stated. Each assumption about value is labelled an assumption, with
   the evidence that would confirm or refute it.
2. **Journeys.** The actor's journey is complete for the slice: entry, steps, end state, and where it meets other journeys.
3. **Rules.** Every rule is unambiguous. Every accepted rule has at least one concrete example, and the examples agree with the
   scenarios and the prototype.
4. **Exceptions.** The material failure, empty and permission paths are present as scenarios, with an observable result.
5. **Testability.** Each scenario's expected results can be observed and checked. A case written from the slice could be accepted
   or rejected from them.
6. **Scope.** The slice's includes and excludes are explicit, small, and of observable behaviour, not one per screen or layer.
   Unresolved matters are marked blocking or nonblocking correctly.
7. **Evidence.** Claims about current behaviour cite sources and match them. Proposals and assumptions are not presented as facts.
   Decisions name a responsible person and evidence.

## Rules for findings
- **AI role-play is never customer validation.** A model, yours included, playing a user or stakeholder produces hypotheses and
  questions, never evidence of what customers want or do. Flag any claim, review or decision that rests on one.
- Your review does not accept a decision or make a slice ready. Only a local reconciliation by the responsible person does.
- Keep the two kinds of finding separate, each with its own disposition:
  - **Product findings** are about the design: a wrong or unsupported claim, a missing rule or exception, a scope problem. For
    each, give the record id, the evidence and a suggested disposition: `accept-change`, `needs-investigation`, `needs-decision`
    (name the owner), `duplicate`, `decline` (with rationale) or `defer` (with impact). A code change alone does not resolve a
    product question.
  - **Prototype defects** are where the built page does not do what the package says: a broken action, wrong state, or a missing
    target. For each, give the steps, the expected and the observed result, and the disposition `fix-prototype` or `not-a-defect`
    (with the reason).
- Cite file and line, or record id and source, for every finding. Say what you could not check.

## Acceptance criteria
1. Each of the seven criteria above is judged for the slices in scope, with its evidence or "not checked" and why.
2. At least one claim per slice in scope was compared with its underlying source, and the source is named.
3. The built prototype was exercised for the normal and the exception scenario of each slice in scope, with what was done and
   seen.
4. Product findings and prototype defects are listed separately, each with its own disposition.
5. No finding, verdict or recommendation treats AI role-play or a model's answer as customer validation or as an accepted decision.
