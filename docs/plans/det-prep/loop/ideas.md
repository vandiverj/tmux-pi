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

Research runs append below this table with their ids.
