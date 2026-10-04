# Product fit and observable contract (planning guidance)

**Planning only.** Do not implement, edit public contracts, or treat this skill as approval to do so. It is a bounded Orient/Frame aid, not a mandatory SDLC phase: tmux-pi's main agent owns framing and the human owns consequential product decisions.

Use for an explicitly requested product-fit or contract check, especially before a substantial user-workflow, public API/CLI, data-ownership, permissions, or AI-authority change.

**Source trust:** Start from the real task and existing product evidence read in this task. Separate facts, safe assumptions, and unknowns; label anything else an assumption.

**Output** (scale down for tiny work; no PRD or new file by default):
1. Actor and outcome
2. Product-fit finding
3. Affected boundary
4. Key scenarios
5. Unresolved decisions, each with a recommended default and tradeoff
6. Recommendation: proceed, revise, split, or defer

**How:**
- Identify the primary actor, others affected, their job and the expected outcome. Check where the change fits the surrounding journey: setup, error/empty states, permissions, persisted data, nearby consumers.
- When an observable boundary changes, sketch its smallest useful contract: inputs, outputs, validation and missing-data behavior, side effects, error states, compatibility, and one concrete example. For AI/automation, make recommendation-versus-action authority explicit.
- Walk a happy path and one relevant ambiguous/failure path; add a cross-feature or existing-data path when it can change the decision.
- Ask only decision-bearing questions. If an actor, source of truth, public behavior, or permission rule is unclear, propose a stop/next evidence step rather than inventing it.
