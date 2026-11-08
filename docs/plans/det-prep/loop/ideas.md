# Candidate hypotheses

Status: open | running | kept | discarded | refuted (by data, no run). Fingerprint = files + mechanism in one line.

| id | idea | metric it should move | source | cost | status |
|---|---|---|---|---|---|
| H1 | **Local doc lookup for coders.** Index framework manuals and the project's own docs (React, Vite, vitest, TS handbook pages actually used; `docs/agent-tools.md`; worker rules) into a local search the worker calls as a tool; the local model returns the 3 to 8 most relevant lines with `path:line` refs, never a summary, so the frontier worker reads only those ranges and can open more by ref. | code_pass, tokens_per_accepted, orient_waste | owner | medium | open |
| H2 | **Local scout before dispatch.** Before a coder starts, the local model runs the brief's searches (symbols named in the brief, callers, tests that name the files) and attaches a `## Where to look` block of `path:line` refs with one-line reasons; the coder is told to start there. Measures whether pre-located refs replace frontier grep/read loops. | orient_waste, tokens_per_accepted | owner | low | open |
| H3 | **Local model as the search tool.** A worker tool `ask_local "question" PATHS` that greps and reads with the local model and returns refs plus quoted lines, used instead of repeated `sed -n`/grep calls in frontier context. | tokens_per_accepted | owner | medium | open |
| H4 | **Tool usage manual injected into the local model.** Give the local model a short manual for tmux-pi tools and git/grep forms (the safe forms from the worker rules) and test whether its run_named_checks and locate_quote accuracy rises. | local_exact | owner | low | open |
| H5 | **Syntax cards for the local coder.** For `small_edit` local tasks, inject a compact card of the exact APIs the file imports (signatures pulled by the ctx engine from node_modules types) and measure local small-edit pass rate. | code_pass (local subset) | owner | medium | open |
| H6 | **Affected-areas block sourced by the local model** instead of grep only (D2), for languages the navigator cannot read. | review_catch | Main | medium | open |
| H7 | **Blind second reviewer** (no author result at all) on steps whose first review approved, local model or small level, only to raise disagreements. | review_catch, review_fp | Main (earlier research) | medium | open |
| H8 | **Report-template autofill**: the runner fills counts, log paths and commit from B1 records into the worker's report so workers stop spending turns on it. | tokens_per_accepted | Main | low | open |

| H9 | **Start check.** tp-spawn (or the status poller) confirms a worker actually took its first prompt (transcript has a user turn, or the context counter rises) within N minutes, and flags the task `stalled` with the pane's last lines (for example "Please run /login") instead of reporting "working". Found live: 6 coders idle 80 min behind an expired login. | wall_s, deterministic failure detection | Main (observed 2026-11-07) | low | open |

| H10 | **Haiku and the redaction hook.** With redaction on, lib/redact_hook.py rewrites every Bash command (appends `|| echo exit-code: nonzero`); Claude Haiku workers treat the rewrite notice as untrusted and stop running Bash, so their steps return incomplete. Fix options: do not rewrite (use PostToolUse only), word the notice so it reads as the harness, or route Claude coders above Haiku when redaction is on. Found live 2026-11-07 (E1, G1). | code_pass, wall_s | Main (observed) | low | open |

| H11 | **Reviewers stop on the sensitive-read prompt.** Four of eight reviewers so far asked for a recursive search of agent/, config/ or tools/agent and waited on "Authorize sensitive read?" until Main denied it. Tell reviewers in the packet which paths are secret stores and to search named files or subfolders, or make the guard exclude known secret files from the search instead of asking. | wall_s, Main turns | Main (observed 2026-11-07) | low | open |

| H13 | **Structural sweep with ast-grep.** When a review finding names a family, Main or the coder writes an ast-grep rule (pattern with metavariables, or YAML rule) for the defect shape; `ast-grep scan` lists every site in `bin/`, `lib/`, `agent/extensions/`, `tests/` before the fix, the same rule runs after the fix and must return zero (or only allowlisted) hits, and the rule file is kept as the parity check. Targets the F1/D1/K1 pattern of 5–8 rounds where each round fixed only the cited site. | rounds_to_approve, review_catch, tokens_per_accepted | owner (2026-11-08) | medium | open |
| H14 | **RTK command-output compression** (`rtk-eval` branch; eval running 2026-11-08, plan `docs/plans/rtk-eval/plan.md`). | tokens_per_accepted at equal code_pass | owner (2026-11-08) | low | running |
| H15 | **Model-specific worker profiles** (guided vs expert instructions, planning scaffold, tool surface per model group; `m1-model-profiles.md`, arXiv:2609.20804). | code_pass, rounds_to_approve, tokens_per_accepted | owner (2026-11-08) | medium | open |

Research runs append below this table with their ids.

## H12: fix briefs ask for the rule, not the sample (2026-11-07)
E1 and G1 each took 3 rounds in one finding family. Both coders patched only the site or form the reviewer cited: E1 kept extending a regex for git option forms, and G1 moved the unload past one kill step at a time. Idea: when a finding names a family, the fix brief must state the rule that closes it (a parser, or "the unload goes before anything that can end the caller"). It must also require a test that proves the rule, either a parity table or a stub that kills the caller, not one example. Eval: rounds-to-approve on review-loop tasks in tp-bench.

## H11 update (2026-11-07): the cause and two fixes
The cause is a gap in the instruction plus a conservative classifier.
- Instruction: `bin/tp-review:219` scopes safe_grep to files outside the worktree, so reviewers use bash `git grep` inside the worktree.
- Classifier: `lib/sensitive_read.py:61` (CREDENTIAL_DIRS) flags agent/, config/ and claude/ as search roots even for `git grep`, which can only read tracked files.

Fix A (instruction, bin/tp-review): search the repository with safe_grep, or use `git grep` with roots outside agent/, config/ and claude/. Read files in those directories by exact path.

Fix B (system, lib/sensitive_read.py): a plain `git grep` clears a credential-dir root when `git ls-files` under that root lists no credential-named file. Plain means no --untracked, --no-index, --recurse-submodules or revision. This is security code, so it gets its own lane and a security review.

Eval: reviews that stop on a prompt, per review. 8 of 17 before the fix.

## H13: ast-grep for whole-family sweeps (owner, 2026-11-08)
Owner: "this could possibly help with the issue of not finding all the places a defect happens and then we have to do 5 rounds of single file changes instead of one".
Evidence from det-prep:
- F1 took 8 rounds and D1 took 7. K1 and C2 took 3 each.
- Several rejections were a "new missed sweep site" in the same family. One example is F1 r7, where `tp-contract` called `write_record` without `relative=True`.
- Coders' sweep tables come from text grep. Text grep misses call forms, for example a call wrapped across lines or a keyword passed through `**kwargs`. It also floods results with comments and strings.

How ast-grep could close this:
- ast-grep (https://ast-grep.github.io/) matches tree-sitter syntax, not text. It has built-in Python, TypeScript/JavaScript, Bash, JSON and YAML, which covers everything tmux-pi has.
- `bin/tp-*` scripts have no extension. `sgconfig.yml` therefore needs `languageGlobs` to map them to python or bash by shebang group, and the eval must check this.
- Patterns use metavariables, for example `write_record($$$ARGS)` with a `not: has: relative=True` constraint. YAML rules support `inside`/`has`/`not` relations and `--rewrite` fixes. `ast-grep test` checks rules against valid/invalid snippets.

Eval design:
1. **Replay (no new runs).** For each det-prep finding family that needed more than one round (F1 retirement-dependency-order, D1 diff-scope states, K1 option allowlist / committed check, C2 heading parse, E1 git option forms, G1 unload order), write the rule a reviewer could have written from the round-1 finding. Run it at the round-1 commit.
   - Measure: did it list the sites later rounds found (recall), and how many false hits did it return (precision)? Compare with the text-grep sweep the coder actually produced.
   - Rule-writing cost: time and tokens per rule, written by Sonnet and separately by the local Qwen.
2. **Live A/B (tp-bench review-loop tasks).** Fix-brief arm A is the H12 rule-first brief, which is what we use today. Arm B is the H12 brief plus an ast-grep rule file that the reviewer or Main supplies. The coder must run it before and after, and attach both outputs. The parity test then runs the rule.
   - Metrics: rounds_to_approve, review_catch, tokens per accepted step.
3. **Where it fails.** Note families that ast-grep cannot express, such as ordering across processes, concurrency races and semantic invariants. Those still need H12 tests.

Install for the eval: one pinned release binary, checksum-verified, in an isolated dir, the same way as RTK. Don't use global installs without the owner.
Adopt if: replay recall beats the text-grep sweep on at least 4 of the 6 families, live rounds_to_approve drops, and tokens per accepted step don't rise.
