# Test a skill or selection rule (internal, bounded)

Use when explicitly evaluating a tmux-pi skill, catalogue trigger, or workflow-rule change **before relying on it**. This is a development aid, not an automatic test campaign or permission to change routing policy. Any model probe is run by a worker with no write authority.

1. Define the intended behavior and authority boundary.
2. Select a small, predeclared set of positive, negative and near-miss cases: at least one ordinary task that must not load the skill, one request that must, one ambiguous/missing-fact case, and one attempted authority overreach if relevant.
3. **Deterministic first:** run `echo "<request>" | bin/tp-skills --mode select` for each case and `bin/tp-skill-check <skill-dir>` for the skill (use `--legacy` for an existing catalogue skill, which predates frontmatter; use `--single-file` for a new skill meant to stay portable). Per docs/skill-authoring.md, a new or changed trigger needs one positive and one negative case in tests/test_skill_catalog.py.
4. Only for changed model behavior, use a bounded read-only probe, with no real ticket write, external action or destructive command. Keep test prompts separate from frozen qualification/eval sets and report untested paths honestly.

**Output** a table, then a summary:

| request | expected skill/behavior | observed | pass/fail/partial | ordinary-turn overhead |
|---|---|---|---|---|

A failed or ambiguous case is evidence to revise or leave the route disabled, not a reason to weaken checks or retrofit labels. Give the next smallest fix. Do not promote a single synthetic success into unattended routing, enable automation, alter frozen qualification/eval labels, or grant new capabilities.
