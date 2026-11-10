# Checkpoint 1: port notes (Main, 2026-11-10)

Checkpoint 1 is the first cut after d8a18a7 (the commit on the work Mac) meant for production use. It fixes the owner's production
issues without depending on the local summarizer. Gates: release-gates.md, "Checkpoint 1". Tag `checkpoint-1` (local only). Push
only with the owner's approval; the work Mac is not touched by Main.

Cut commit: the commit tagged `checkpoint-1`. Its code is that of a417c82 (main after the SW1 merge and its README fix); later
commits change only files under docs/ (`git diff --stat a417c82 checkpoint-1 -- ':!docs'` is empty). Results of G1 and G5 are at the end of this file.

## What changes for the owner

The fixes for the issues the owner hits on the work Mac:

| Issue on the work Mac | Fix | Merge |
|---|---|---|
| Messages to workers not getting sent | MS1: each worker message carries a delivery token and counts as delivered only when the worker's prompt shows that token. | 627fdf8 |
| Long messages arrive cut off at the start | MS2: a message counts as delivered only when the whole text arrived; long text goes through a paste buffer. | 876aa57 |
| Workers no longer send their ack to the main agent, after several side tasks (subagents) | MN1: the main keeps one stable id in `$TP_STATE_DIR/main-id` that survives `/clear`, resume, compaction and relaunch, so worker notices still reach it. | fef8aec |
| A worker's report is replaced by a later turn the worker took on its own | RO1: a reply with no prompt from outside cannot overwrite the report already kept. | a81d7da |
| Workers find one or two things and stop | SW1: worker briefs require an attack over the whole scope; routing has a built-in policy when the model catalog is unusable, and an explicit level stops instead of falling back. | e41af57 |

Other merges since d8a18a7 (first-parent): TV1 folder-trust dialog shown as `needs_input` (e1e1b4f); AT1 no tmux server is an empty
pane table (57611f6); DS1 a late answer to a declined request keeps every field (9915fd8); SE1 TP_TRUST_* hook classification
(d63f08c); FC1 review diffs name frozen copies (234225d); G3 end-to-end smoke and its fixes (256e332, b6b43c4, 36b870d, 5fd7630);
G2 lanes (Python 3.9 support, handover, Deno optional, trial run ids, lane cap: 16a9eb1 to 838589b); FG-N1 wrapper floor
(a924a5b, 2fac25a); gate record once (4713181); retry digest (8748ee2); LS1 summarizer lanes (6ec27a8, 711cd79, b77ad33, db29594,
f503858), which are not used unless a worker is routed to `summarize_files`.

`git log --first-parent --merges d8a18a7..checkpoint-1` lists them all.

Runtime files changed: bin/ (tp-agent, tp-claude, tp-main, tp-spawn, tp-review, tp-redact, tp-sensitive-read, tp-lmstudio,
tp-exit, tp-task, tp-contract, tp-improve; new tp-bench), claude/plugins/tmux-pi (report.sh, register.tsx, hooks.json),
claude/settings.json (one more pre-allowed command), claude/mcp/safe.mjs, agent/extensions (safe, sensitive, subagents),
agent/roles, agent/models.json (qwen context 65536), config/ (local-model.json new; local.example.json, model-catalog.json,
router.json), lib/ and tools/.

## Setup on the work Mac

Nothing new is required. G5 rehearsed this on a fresh clone with no `config/local.json`, Apple Python 3.9 first on PATH and no
`lms` on PATH.

1. Exit every running tmux-pi workspace (`tp-exit` in each), so no worker runs old code against new records.
2. In the tmux-pi checkout, note the current commit (`git rev-parse HEAD`, expected d8a18a7), then fetch and check out
   `checkpoint-1` once it is pushed.
3. Keep `config/local.json`, `agent/settings.json`, `agent/auth.json` and `claude/mcp.local.env` as they are. They are not
   tracked and are not overwritten. `agent/auth.json` is pi's login: tmux-pi points pi at the checkout's own `agent/` folder
   (`PI_CODING_AGENT_DIR`), so a fresh clone has no login, and a pi worker or reviewer there sits at `starting` until its
   waiter times out (G5's first live run). Update the existing checkout in place rather than cloning anew; on a new clone,
   log in to pi once from it (`bin/tp-pi`, then `/login`). `agent/settings.json` keeps its own `defaultModel`; the template now says `gpt-6.1-sol`, so change it by hand if
   wanted.
4. Reviewer models: the committed catalog names `openai/...` models for pi, as d8a18a7 did, so the work Mac's pi login keeps
   working unchanged. When pi has no key for the provider it asks for, it can answer from another provider it has a key for
   (in G5's second live run it went to OpenRouter and was refused for credits; tmux-pi still recorded `openai/gpt-6.1-sol`).
   This Mac's pi login uses `openai-codex`, so G5's passing live run used a local, uncommitted copy of that mapping.
5. Pi workers still use `TP_PI_NODE` (default `~/.nvm/versions/node/v24.16.0/bin/node`; set it empty to use `pi` from PATH).
   Unchanged since d8a18a7.
6. Start a new workspace (`agent` or `tp --main claude`). The Claude plugin loads from the checkout, so the new hooks take effect
   on launch. The first launch writes `$TP_STATE_DIR/main-id`.
7. Optional check: `claude plugin test claude/plugins/tmux-pi` and the quick module run in "Validation" in README.md.

## Rollback to d8a18a7

1. Exit every running workspace (`tp-exit`).
2. `git checkout d8a18a7` in the checkout (or the branch that held it). Untracked local files are untouched.
3. Start a new workspace. Records written by checkpoint-1 stay under `.tmux-pi/sessions/<run-id>/`; d8a18a7 reads them: G5 ran its `tp-task list`, `tp-agent list`, `inspect` and `collect` on a copy of the records from G5 live run 4, all with exit 0.

## Gate results

G1 (2026-11-10, started 08:06:53Z): a clean worktree at a417c82 (tree 7417f3f, 0 dirty files), all 157 test modules, each run on its own,
in four cells: Apple Python 3.9.6 and Homebrew Python 3.14 with and without a tmux-pi session in the environment, under a private
TMUX_TMPDIR. Each cell: 5191 tests, 0 modules failed. The baseline of known failures is empty.

G5 (2026-11-10): a fresh clone of a417c82 on this Mac set up like the work Mac: no `config/local.json`, no `lms` on PATH,
`/usr/bin/python3` (Apple Python 3.9.6) first on PATH, other tools (tmux, rg, node v24.16.0, claude, pi) from a shim folder.
- G1 in the clone (started 08:58:18Z, TMUX_TMPDIR /tmp/g5cp1.A49M, clone clean): all 157 modules, each on its own, with and without a
  tmux-pi session in the environment. Each cell: 5191 tests, 0 modules failed.
- G3 live runs in the clone, in order:
  1. Failed: the reviewer stayed `starting` for 600 s. A fresh clone has no pi login (`agent/auth.json` empty; pi: "No API key
     found for openai"). Setup step 3 covers this. Fix for the rehearsal: the clone used this Mac's existing login.
  2. Failed in 31 s: with no OpenAI key on this Mac, pi answered through OpenRouter, which refused for credits (402). Setup step 4
     covers this. Fix for the rehearsal: this Mac's `openai-codex` mapping, applied to the clone and removed afterwards.
  3. Failed: the reviewer stayed `needs_input` for 600 s. It ran a broad hidden-file search, and the sensitive-read guard stopped it
     for approval, which is correct; the smoke answers only the coder's prompts. Nothing was changed before run 4. This is a known
     follow-up (pi reviewers' recursive searches); on the work Mac such a prompt waits for the owner.
  4. Passed: 11 steps in 77.13 s (g5-g3-20261110084354), cleanup complete, no leftover branches, worktree or tmux session.
- Rollback check (08:45:37Z): a d8a18a7 worktree ran `tp-task list`, `tp-agent list`, `inspect` and `collect` on a copy of run 4's
  records; all exited 0.

G2 (the live run on the main checkout) passed earlier: .tmux-pi/g3/live9-20261110035055.json, ok, 11 steps.

Known issues carried into checkpoint 1 (follow-ups, not blockers):
- A pi worker or reviewer with no login shows `starting` until its waiter times out, instead of an error.
- When pi lacks a key for the requested provider it can answer from another; tmux-pi still records the requested model.
- A pi reviewer's broad search can stop at a sensitive-read prompt; answer it (or decline) in the reviewer's pane.
