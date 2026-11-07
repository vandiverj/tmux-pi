# Sources of truth

## Which source wins

1. **`<owner>`'s decisions**, newest first. Quoted in the campaign decision log, the `tp-agent work note`s, or the
   chat. A later decision replaces an earlier one on the same point.
2. **`<ctx>/<program>-implementation-roadmap.context.md`**: the product source of truth when documents disagree.
3. **Regulation** (`<REG>` and any agency- or sector-specific parts that apply to the program). Where regulation sets
   a rule, the product follows it unless a decision says `<company>` goes further.
4. **The prototype** (`<ctx>/<program>-prototype/`): screens, flows and wording. The roadmap wins over it.
5. **The `<tracker>` case text** and its comments, including handoff comments from other cases. Decisions win over
   the case text; when they do, draft a case-text amendment for `<owner>` rather than silently diverging.
6. **Existing code** on develop. It shows what is built, not what is right.

`<ctx>/<program>-handoff-<reviewer>.context.md` is for process (how `<reviewer>` reviews), not product rules.

Record any disagreement between these as a decision (`05-decisions-and-deck.md`).

## Product and domain documents (`<ctx>/`)

Read at least these for every case; read the rest when the case touches their area.

| Document | Use it for |
|---|---|
| `<program>-implementation-roadmap.context.md` | What to build and in what form. Wins disagreements. |
| `<program>-rules-model.context.md` | Program, pool, rate and rule versions. |
| `<program>-api-and-schema.context.md`, `<program>-api-schema-handoff.context.md` | Tables, routes, record shapes. |
| `<program>-gap-backlog.context.md`, `legacy-feature-parity.context.md` | What the old system did; known gaps. |
| `regulatory-requirements.context.md` | Regulation by topic; the MUST NOT list (a section near the end). |
| `<program>-exceptions-by-agency.context.md` | Exception rules per agency. |
| `<program>-config.context.md` | What is configuration rather than code. |
| `case-review.context.md` | Earlier case review notes. |
| `<phase>-decisions-<date>.md` | Early-phase decisions (D1 onward). |
| The campaign decision logs (`<C>/briefs/*decisions*.md`) | Later decisions, one log per campaign (`<campaign>-decisions-<date>.md`; labels `S<N>D<n>`, or bare `D<n>` in the first campaign). |

## Prototype (`<ctx>/<program>-prototype/`)

- Open `<program>-prototype.html`, or the build at `dist/index.html`. `README.md` explains the walkthroughs. The
  prototype's own `DECISIONS.md`, `GAPS-FOUND.md`, `REGULATORY-PROFILE.md` and `COMPLIANCE-REVIEW.md` record its
  reasoning.
- Source: routes in `src/App.tsx`, screens in `src/screens/`, sample data in `src/data/`. Find a screen by
  searching `src/screens` for the case's wording.
- Screenshots: `npm run screenshots` (`scripts/capture-screens.mjs`) writes to `screenshots/`. Its node_modules
  are already installed; don't install anything.
- For each AC, note the prototype screen and state that shows it. Note any difference from the roadmap (the
  roadmap wins), and anything the case asks for that the prototype doesn't show (a decision).
- Use prototype screenshots on deck slides and next to app screenshots when the build differs on purpose.

## Regulations

- Start with `regulatory-requirements.context.md`. Its MUST NOT list is a hard gate. Typical items for a
  compliance program of this kind: results are write-once, with no alteration or deletion before the retention
  floor; no visibility across customer groups; `<company>` staff never make the customer's decisions; nothing
  unverified counts as a result; nothing is announced in advance; and the rest.
- Retention floors are set per record type by the regulation (`<REG> §<section>`). For example: several years for
  adverse outcomes, a shorter period for routine records, and the shortest for plain negative outcomes. Take the
  real figures from the current regulation text, not from a note.
- When a rule matters to a decision, re-read the current text on the official regulation site and quote the section
  in the decision. The context doc can lag.
- Rates, periods, thresholds and limits are configuration with regulation-sourced defaults. Never invent a limit
  the regulation doesn't set. If the regulation is silent, record that and make it a decision.
- If a case seems to raise a regulatory area the repo and the context docs have no guidance on (a privacy law, for
  example), record it as a decision for `<owner>` rather than guessing.

## Repo standards (`<repo>/`)

| File | Use it for |
|---|---|
| `CLAUDE.md` | Hard rules: imports, Prettier, `nx sync`, simplest thing, no new dependencies. Workers don't load it, so briefs restate it. |
| `.claude/CONVENTIONS.md` | Patterns. §7 is the list reviewers raise again and again; §8 lists known gaps. |
| `.claude/skills/{refine-story,build-tdd,review-pre-mr,secops,merge-develop,ship-mr}/SKILL.md` | Story pipeline skills. |
| `.claude/secops-baseline.yml` | Known security findings, so they aren't re-reported. |
| `docs/testing/TEST-QUALITY-AUDIT.md`, `docs/testing/ESCAPED-BUGS.md` | Test quality rubric and past escapes. |
| `.<git-host>/merge_request_templates/Default.md` | MR template. |
| App READMEs (`apps/<api>/README.md`, `apps/<web>/README.md`, `apps/<admin-app>/README.md`) | App-specific commands. |

## `<tracker>`

- Read with `get_issue` and `get_issue_comments` only. Writes need `<owner>`'s OK (`06-handoff-mr-tracker.md`).
- Snapshot each case into `<C>/cases/<CASE-ID>.md` when you start it and list it in `<C>/cases/INDEX.md`, so workers
  read the file rather than the tracker.
- Case keys are `<CASE-ID>`. Older documents may use a variant of the project key; treat it as the same project.
