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
