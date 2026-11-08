# M1 (eval, queued): model-specific worker profiles

Owner, 2026-11-08: "this could lead to version specific subagents depending on the model being used … as specific as sonnet versus opus or anthropic versus openai as a group. we would definitely want one specific to qwen local model that would likely be a lot more specific on what to do than the frontier models … teaching a young child versus a field expert."

Source: Fan et al., *An Empirical Study of Harness Design for Coding Agents*, arXiv:2609.20804 (2026-09-17). There are 176 matched settings on SWE-Bench Verified and Terminal-Bench 2.1, with four models. Findings that matter here:
- Planning is an accuracy scaffold for weaker models and a cost saver for stronger ones, with little accuracy change. This supports a step-by-step profile for the local Qwen and a terse one for frontier models.
- Predefined tools help models that are weak at bash. Bash-capable models can work bash-only at much lower cost. So the profile should set the tool surface, not only the prose.
- Context management pays off more as the budget tightens. Rule-based elision before LLM summarization was the most efficient. Recoverable elision added machinery models rarely used and gained no accuracy (relevant to the RTK eval, `rtk-eval` branch).
- The authors call for "model- and budget-aware harness design".

## What a profile is
A profile is chosen per worker from the dispatch's resolved model: a small layer over the existing role file (`agent/roles/<role>.md`). It sets:
1. **Instruction granularity:**
   - expert: goal, constraints and done-means only;
   - guided: numbered steps, an explicit plan-first step, a checklist before reporting, and worked examples of the report format.
2. **Planning scaffold:** none, or plan required before edits.
3. **Tool surface:** bash-first, or named tools (safe_grep, safe_read, test-run) preferred.
4. **Context budget:** packet size cap, and how much of the brief is inlined versus linked.

## Groups to compare (coarse to fine; stop at the coarsest that captures the difference)
- G0: one profile for all models (today).
- G1: frontier vs local (Claude/OpenAI vs Qwen).
- G2: provider (Anthropic / OpenAI / Qwen).
- G3: model (Opus / Sonnet / Haiku / gpt-6-sol / gpt-6-luna / Qwen 3.6 35B).

## Eval design
- Tasks: the same known-answer task set as the RTK live A/B (diagnosis, seeded bug fix with tests, review with a seeded defect, codebase question), plus 2 real det-prep briefs replayed at a fixed commit.
- Arms per model: expert profile vs guided profile, crossed with planning off/on for Qwen and Haiku. At least 3 reps per cell.
- Metrics:
  - correctness, by a deterministic checker;
  - review verdict on first submission;
  - retries to approval;
  - tokens (input/cache/output), turns, wall time;
  - parked or prompted runs.
- Decision: adopt the finer grouping only where it beats the coarser one on correctness or on retries to approval, without costing more tokens per approved result. Quality first, then efficiency.

## Order
After the RTK eval, so that the token baseline is settled first, and before the J1 benchmark, so that J1 runs with the chosen profiles. Owner to confirm the order.
