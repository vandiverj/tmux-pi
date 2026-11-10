---
name: prepare-cases
description: Turn an accepted product design slice into a few small implementation case drafts of observable behaviour, with acceptance examples, dependencies and package provenance; drafts only, nothing published.
---

# Prepare cases from a slice (explicitly invoked)

**Drafts only.** This skill grants no ticket, publication, push, edit or approval authority. Creating tickets in a tracker
is a separate step after the human authorizes it. This is incoming case preparation, before work starts; completion-side MR and
handoff drafts are `/case-handoff`.

Use when the human asks to prepare cases (`/prepare-cases`, or "prepare cases for slice X").

## Before writing
1. Run `tp-product status DIR`. Work only on a slice whose effective readiness is `ready-for-case-preparation`. A `blocked` or
   `stale` slice, or one still in `ready-for-human-review`, gets no cases. Report its actual blockers (the question, the
   decision or the review that is missing), and say who must act.
2. Read the slice and its closure: its scenarios, rules with examples, decisions, contracts and `depends_on`. Do not read the whole
   package or the whole prototype.

## Shape the cases
- **Slice by observable behaviour.** A case is a small piece of behaviour a user or a client can observe. It crosses the layers it
  needs (UI, API, data, integration), with technical subtasks where they help. Never write one ticket per screen, per endpoint or
  per table.
- **Keep it small.** A case one person can finish and demonstrate on its own, usually one scenario plus its exception paths. Split
  when the acceptance examples describe two outcomes a user would notice separately.
- **Proposals stay proposals.** A contract or schema marked `proposed` is a design input, not a final interface. Say what is
  tentative and what the implementer must confirm. Do not assume every schema proposal is final.
- **No whole-product completeness.** Prepare cases for this slice only. List the matters that do not block it as unresolved, but
  nonblocking.
- A case is **blocked** when a material question it depends on is open, a required review is stale, an accepted rule lacks a usable
  example, or a contract contradicts known evidence. Record the blocker instead of writing around it. A human may accept bounded
  uncertainty (`accept-uncertainty`, with scope and rationale); there is no force flag.

## Each case draft
- A stable local key (`case.<slug>`) and a concise title.
- The actor, the user outcome, the scope and the exclusions.
- The package revision and slice fingerprint it was written from, both from `tp-product status DIR --json`.
- Its rule, scenario, decision and source references.
- Concrete acceptance examples: given, when, then observable result. Include the material exceptions.
- The relevant contracts and their confidence; the dependencies; any feasibility findings.
- The QA approach, the required fixtures and the evidence that will show it is done.
- Suggested implementation areas, each labelled observed (seen in code) or tentative.

Commit the case definitions with an `add` changeset to the `cases` collection, through `tp-product apply DIR CHANGESET
--expected-revision N`. Then run `tp-product validate DIR`. Never edit `design.json` by hand.

## Export and hand over
- `tp-product cases DIR --slice ID --out OUTDIR` writes one Markdown file per case and `cases.json`. It refuses a slice
  or case that is blocked or stale, naming the missing decision; report that, do not work around it. It never
  overwrites a file someone edited.
- `tp-product packet DIR --case ID --out FILE` writes the worker packet for one case: only its scope, with links for
  the rest. Give the worker that file, not the package. It holds no Git facts; the worker reads them live.
- Publishing to a tracker is the human's step. After it, record the external id with a `record-publication`
  changeset (the `digest` and revision from `cases.json`). A later export proposes `update` for that id: never
  create a second ticket, and check the ticket was not edited there before replacing its text.
- When an answer turns out wrong, reopen its question with a `reopen-question` changeset (`question`, `reason`, `by`,
  `date`). Its cases go stale and keep their published ids. `tp-product impact DIR` lists what must be reviewed
  and prepared again.

## Output
The cases, each with its key, outcome and acceptance examples; the slices that got no cases, with their blockers; and the next action,
which you also save in the manifest and in `tp-agent objective` as the product-design skill describes.

Optional reference: `catalog/skills/prepare-cases/references/case-shape.md` in the tmux-pi checkout (the folder above the `bin/`
holding `tp-product`). It shows one slice split well and one split badly, with a case changeset.
