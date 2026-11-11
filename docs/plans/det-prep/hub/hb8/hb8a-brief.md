# HB8a: complete-episode harness through Main, with deterministic scoring (Main, 2026-11-11)

Owner workstream: `/Users/jerrickvandiver/Documents/Projects/tmux-pi/docs/plans/det-prep/hub/owner-request.md` item 8 (read its
episode table and measures). Plan: `.../hub/README.md` (HB8, baseline table) and `.../hub/interfaces.md`. Bar:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/docs/plans/realistic-use-bar.md`. This step builds the harness and its
deterministic scoring; Main runs the live episodes (you start no Claude or pi). Keep `bin/tp-bench` and `lib/bench/` worker tasks
working as they are.

## What to build
1. **Runner** `tools/episodes/run.py` (Python 3.9 stdlib; reuse what fits from `tools/smoke/g3_e2e.py` and `lib/bench/`, and say what
   you reused). It:
   - makes a scratch clone of a target project (default `~/Documents/Projects/battle-soccer`, given by `--project`) under a run dir
     it creates in `--out` (never writes to the original project);
   - starts Main through the normal entry point (`bin/tp` / `bin/tp-main` with `TP_MAIN_HARNESS=claude`, the way the owner does) in a
     **private tmux server** (fresh `TMUX_TMPDIR` and `-L <unique>`), with its own `TP_STATE_DIR`/session so it never sees or touches
     the detprep session or the default tmux server;
   - plays an episode file: owner turns (text, sent whole: paste-buffer `-p`, never send-keys for long text, and never a leading `!`),
     waits on conditions read from records (task states, result.md, lane records, request files when present) with timeouts, and
     forced events (`/compact` in Main's pane, Main restart by killing and relaunching tp-main in the same private server, a worker
     left at a prompt); every action and wait is logged with a UTC time to `run/episode-log.jsonl`;
   - has `--dry` that prints the plan and a `--fake` mode in which Main and workers are scripted stand-ins (shell scripts that write
     the same records), so the runner and scorer are tested without any model.
   - always tears down its own private server and leaves the scratch clone and records for scoring.
2. **Episodes** `tools/episodes/episodes/*.json`, one per row of the owner's table, E1-E7, each with a negative variant (a reminder,
   tool or intervention that must **not** apply). Small battle-soccer tasks (a real, checkable change: a rule tweak with a unit
   test, a bug fix); the owner turns written as the owner would type them; an `expected` block per episode naming the evidence the
   scorer looks for. E2 (worker needs correction) plants a worker at a permission prompt and at queued text; E4 forces a compaction
   and a restart mid-episode; E5 states a process correction once, then a later step where it must hold without a reminder, plus a
   negative step out of its scope.
3. **Scorer** `tools/episodes/score.py`: reads only records and logs (task dirs, messages.jsonl, events/requests when present,
   transcripts by path, git in the scratch clone), never asks a model. Per episode: correctly completed (the project's tests pass
   and the expected change is present; semantic success is left as `needs_human` with the evidence listed), process corrections
   (owner turns tagged `process` in the episode file) vs product decisions (tagged `product`), recurrence after correction, recovery
   success and time, avoidable waiting (a worker done or prompting while Main did nothing, from timestamps; unknown stays unknown),
   false completion claims (Main says done while a check or result says otherwise), irrelevant checks, stale or redundant
   verification (same command on the same tree twice), safe-tool use (shell grep/sed/cat on project files vs safe tools, per worker,
   with tool names normalised for both harnesses), messages sent into a prompt. Output JSON + a short Markdown table; a
   baseline/candidate comparison mode over several runs (`score.py compare A/*.json B/*.json`) that reports per-measure counts and
   says plainly that small pilots are diagnostic.
4. **Deterministic checks** that need no live run, as tests: runner plan and teardown (fake mode, private server only), scorer on
   hand-made record fixtures for every measure including the negative variants and missing-telemetry cases (must be `unknown`, not 0).

## Report
Tests with fail-before where a test checks behaviour that exists on the base (most here are new; say which). The four cells as in
the HB4a brief (`/Users/jerrickvandiver/Documents/Projects/tmux-pi/docs/plans/det-prep/hub/hb4/hb4a-brief.md`, "Report"), plus
tests.test_session_env. A fake-mode run of every episode with its scorer output. For each episode: what Main would need to do to pass,
and which baseline finding (README table row) it is expected to show. Known limits. Then the exact command Main runs for a live
baseline of each episode (Claude Main, Claude workers, review by pi per the review policy), with expected duration.
Search with safe_grep and read with Read/safe_read; never search `bin/` as a directory or `config/`, `agent/`, `claude/`
recursively, name the files; never read `.env*`, auth files or `claude/mcp.json`. Any test that starts tmux uses a fresh
`TMUX_TMPDIR` and a private `-L` socket, never the default server, never kill-server without `-L`. Nothing sent to 127.0.0.1:1234
or any network address; no Claude or pi start. `git add` new files. Commit nothing. No background shells. `RESULT_STATUS: complete`
as the last line, exactly.
