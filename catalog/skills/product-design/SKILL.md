---
name: product-design
description: Run the explicitly requested product design workflow - frame the outcome, gather evidence through bounded workers, keep the package with tp-product, request focused review, and persist the next action.
---

# Product design workflow (Main, explicitly invoked)

**Guidance only.** This skill grants no edit, tool, approval, ticket or publication authority. Use it when the human asks for
product design work (`/product-design`, or a product design package or workflow by name). Ordinary coding, review and QA tasks
keep their usual flow. Small or well-understood changes do not need this workflow; a product-fit check (`/product-fit`) is
often enough.

**Evidence comes first.** Keep two kinds of statement apart, and never let one pass as the other:
- **Observed evidence**: what a source shows. That means current system behaviour, a document, a recorded stakeholder decision, or
  observed user behaviour. Record it as a claim with status `observed-current` or `documented-requirement`, citing a source pinned
  by revision or digest.
- **Generated hypothesis**: anything you, a worker or a reviewer model produced. That includes proposed behaviour, assumptions,
  personas, role-played user reactions and predicted outcomes. Record it as `proposed` or `assumption`, and as `unresolved` when it
  conflicts with a source. AI role-play is never customer validation, and a model's answer never accepts a decision.

## Steps (resume at the step the package's `activity` names)
1. **Frame.** Name the actor, the problem, the desired outcome, the scope and the known constraints. Use the `product-fit` skill for
   this check rather than repeating it here. Ask only decision-bearing questions, each with a recommended answer.
2. **Investigate.** Choose the few unknowns that would change the design. Delegate each as a bounded task: a `researcher` to read
   named repositories, documents or cases and report sources with excerpts, or an `architect` for feasibility. Give each one a
   question, the sources to read and the expected output. Never send a worker the whole package or the whole evidence set.
3. **Shape.** Propose journeys, rules with examples, scenarios with an exception path, API and data contracts marked proposed, and
   small slices of observable behaviour.
4. **Build and inspect.** Write every semantic change as a changeset and commit it with `tp-product apply DIR CHANGESET
   --expected-revision N`. Never edit the package JSON by hand. Then run `tp-product validate DIR` and `tp-product build DIR`, and
   open the built page yourself before you ask anyone else to.
5. **Review and reconcile.** Ask for focused feedback on named slices or questions. Import what reviewers export with
   `tp-product import-feedback DIR BUNDLE`, which leaves imported answers pending. Record a decision only with its responsible
   person and evidence (`accept-decision`), and give every note a disposition (`set-disposition`). Contradictory reviewers stay
   visible until a decision is recorded. For an independent product review, use the product-review contract with `tp-review
   --contract` (see the reference).
6. **Prepare cases.** When a slice is ready for case preparation, use `/prepare-cases`.

## Persist on every meaningful pause
- Update the manifest with a `set-manifest` changeset: `activity` and `next_action` (text, owner, date). The manifest is the durable
  record.
- Record the pointer in the session objective:
  `tp-agent objective "product-design package=<absolute DIR> revision=<N> slice=<slice id or none> next: <next action text>"`.
  The package path runs up to the first ` revision=<N> slice=`, so a path with spaces is kept whole.
- On resume, in a new process or a new session, take the package path from the objective, or from the human. Run `tp-product status
  DIR`. Where the objective and the status disagree (revision, next action), the status wins; then refresh the objective. Compare the
  relevant source revisions before trusting older claims.

## Output each round
The current activity, what changed (revision), the open questions with recommendations, which slices are ready, blocked or stale
and why, and the next action you saved.

Optional references (read only when a step needs them) live in the tmux-pi checkout. That is the folder above the `bin/` holding
`tp-product`: `$(dirname "$(dirname "$(command -v tp-product)")")`. Read `catalog/skills/product-design/references/techniques.md`
for investigation techniques, worker briefs and evidence labels. Read `templates/product-design/review-contract.md` for the
product-review contract. Read `docs/product-design.md` for the package format and every `tp-product` command.
