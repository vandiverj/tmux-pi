# Test a skill or selection rule (internal, bounded)

Use when explicitly evaluating a tmux-pi skill, catalogue trigger, or workflow-rule change **before relying on it**. This is a development aid, not an automatic test campaign or permission to change routing policy.

Define the intended behavior and authority boundary, then select a small, predeclared set of positive, negative and near-miss cases. Include at least one ordinary task that must not load the skill, one request that must, one ambiguous/missing-fact case, and one attempted authority overreach if relevant. Check deterministic selection first; for changed model behavior use a bounded read-only probe only when needed, with no real ticket write, external action or destructive command. Keep test prompts separate from frozen qualification sets and report untested paths honestly.

For each case record request, expected selected skill/behavior, observed outcome, gap, and whether the proposed change adds unwanted ordinary-turn overhead. A failed or ambiguous case is evidence to revise or leave the route disabled, not a reason to weaken checks or retrofit labels. Summarize pass/fail/partial and next smallest fix. Do not promote a single synthetic success into unattended routing, enable automation, alter frozen Laya labels, or grant new capabilities.
