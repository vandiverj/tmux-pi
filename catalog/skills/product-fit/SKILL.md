# Product fit and observable contract (planning guidance)

Use for an explicitly requested product-fit or contract check, especially before a substantial user-workflow, public API/CLI, data-ownership, permissions, or AI-authority change. This is a **bounded Orient/Frame aid**, not a separate mandatory SDLC phase. Main owns framing and the human owns consequential product decisions.

Start from the real task and existing product evidence. Identify the primary actor, others affected, their job and the expected outcome. Check where the change fits the surrounding journey, including setup, error/empty states, permissions, persisted data, and nearby consumers. When an observable boundary changes, sketch its smallest useful contract: inputs, outputs, validation and missing-data behavior, side effects, error states, compatibility, and one concrete example. For AI/automation, make recommendation-versus-action authority explicit.

Walk through a happy path and one relevant ambiguous/failure path; include a cross-feature or existing-data path when it can change the decision. Separate facts, safe assumptions, and unknowns. Surface only decision-bearing questions, each with a recommended default and tradeoff. If an actor, source of truth, public behavior, or permission rule is still unclear, propose a stop/next evidence step rather than inventing it.

Output: actor/outcome, product-fit finding, affected boundary, key scenarios, unresolved decisions with recommendations, and proceed/revise/split/defer. Scale down for tiny work; do not create a PRD or a new file by default. Do not implement, edit public contracts, or interpret this skill as approval to do so.
