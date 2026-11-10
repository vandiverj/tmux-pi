# Product design techniques (optional reference)

Read a section only when a step of the product-design skill needs it.

## Evidence labels
| You have | Record it as | It needs |
| --- | --- | --- |
| What the running system does today (read in code, a prototype run, logs) | claim `observed-current` | a source with revision or digest and an excerpt or anchor |
| What a document, ticket or regulation says | claim `documented-requirement` | a source; quote the excerpt, do not paraphrase a rule into a stronger one |
| What a person decided | decision `accepted` via `accept-decision` | the responsible person, date and evidence (a meeting note, a message, the review) |
| What someone observed a real user do | claim `observed-current`, source naming the session or study | who observed it, when, and how many users |
| What you or a worker think should happen | claim `proposed`, or a rule or scenario still `proposed` | an owner and the question that would settle it |
| What you are taking as true for now | claim `assumption` | the reason, the risk if it is wrong, and the next evidence step |
| Two sources that disagree | claim `unresolved` plus a question | both sources linked |

Role-played users, synthetic interviews and model reviewers produce hypotheses and questions, never observed evidence. Say
"a model suggested", never "users want".

## Choosing what to investigate
- Start from the decision the design must make. Which unknown would change it? Read for that, not for completeness.
- Prefer primary sources: the code path, the schema, the API handler, the case history. Read summaries only to find those.
- Check the current behaviour before proposing new behaviour. Current behaviour is not desired behaviour, but changing it has a cost.
- Give each material risk (data loss, permissions, money, compliance) an owner and a next evidence step, even when it cannot be
  settled now. Ask before deciding a rule that affects compliance, security or data handling.

## Briefing a worker
Give one bounded question per task.
- **Question**: the decision it informs.
- **Read**: the named repositories, paths, documents or cases. Never "everything".
- **Return**: findings, each with its source (path and revision, or document and section), its excerpt, and the label from the
  table above. Contradictions are listed separately. A finding with no source is a hypothesis.
- **Do not**: edit the package, decide, or contact anyone.

Main is the only writer of the package. Workers return findings or a proposed changeset file; Main checks it, then commits it with
`tp-product apply`.

## Shaping
- A rule needs at least one concrete example (input, then the observable result) before it can be accepted.
- Every normal scenario that can fail gets its exception scenario.
- Contracts start `proposed`. Mark what is known (observed in code) and what is a guess.
- Slices are small pieces of observable user value that cross the layers they need, not one per screen or per table.

## Asking for feedback
- Ask about named slices, scenarios or questions, and send the built page. Do not ask "any thoughts?".
- Ask related questions together, each with the decision needed, why it matters, a recommended answer with its tradeoff, and the
  owner.
- Carry on with independent slices while one waits.
- To accept uncertainty, use `accept-uncertainty`: its scope, rationale, revisit trigger, who accepted it, and when.

## Independent product review
Start a reviewer for a finished product task with `tp-review --contract <tmux-pi>/templates/product-design/review-contract.md
<task-id>`. The reviewer compares claims with the sources and exercises the built page. Its findings come back as review records.
They are not decisions.
