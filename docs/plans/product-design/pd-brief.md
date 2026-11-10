# PD: product design workflow, lane product-design (Main, 2026-11-09)

Owner request (2026-11-09): "use this doc to make a separate parallel implementation of the product managment capbilities
in the plan, it should not stop the current checkpoint 1".

The plan is `/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/plans/product-design-plan.md` (sha256
0eceee10…26318). Read it in full before each phase; it is the specification. It is kept out of Git on purpose (it names
private work products), so do not copy it, or any of its work-product names or paths, into tracked files. This brief records
Main's decisions where the plan leaves a choice, and how the work runs in this repo. Where the brief and the plan differ, the
brief wins.

## How the work runs
- One lane, `product-design`, one step per plan phase: PD-A … PD-F. Each dispatch names the phase. Do only that phase, then
  report and stop. Main checkpoint-commits each phase, has it reviewed (a different provider), closes the step and sends
  the next phase. That gives the plan's "coherent commit per phase".
- Commit nothing yourself. `git add` new files so the session-env guard sees them.
- This lane runs beside checkpoint-1. Do not edit `bin/tp-agent`, `bin/tp-main`, `bin/tp-review`, `lib/pane_send.py`,
  `claude/plugins/tmux-pi/hooks/*`, `lib/family_sweep.py`, `lib/route_resolve.py`, `lib/model_router.py` or
  `agent/roles/*`. If a phase seems to need one of them, stop and send `tp-agent request` naming the change and why the
  existing mechanism cannot carry it (plan section 3 asks for that justification too).

## Main's decisions
1. **Language and layout.** `bin/tp-product` and `lib/product_design/` in Python 3 standard library only, runnable by
   `/usr/bin/python3` (3.9) and `/opt/homebrew/bin/python3`. A few cohesive modules, not one per record type.
   Tests are flat files `tests/test_product_design_*.py` (the repo discovers `tests/test_*.py`). Runtime assets under
   `templates/product-design/`. The synthetic fixture lives under `templates/product-design/examples/` (or a test fixtures
   directory if you find a better existing convention; say which).
2. **No installs, no downloads.** No npm/pip install, no CDN, no framework, no build tool. The review runtime is plain
   JavaScript and CSS inlined by `tp-product build`.
3. **Browser checks without installing anything.** Drive the system Chrome
   (`/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`) headless through the DevTools protocol, using Node's
   built-in `WebSocket` (Node 24 is on PATH) or Python's standard library. Always pass a fresh
   `--user-data-dir` from `mktemp -d` and never touch the owner's Chrome profile. When Chrome or Node is missing the browser
   tests skip with a stated reason, so no machine is required to have them. Never require a browser for the Python helpers.
4. **Synthetic only.** Use the plan's generic review-queue fixture. The real-product pilot (plan sections 1 and 12F) is out
   of scope for this lane: its sources live on the owner's work machine. Report it as the remaining operational step.
5. **Config.** The only `config/` file you may edit is `config/skill-catalog.json`, in PD-D, to add the two skill entries.
   `max_selected` stays 3. No other `config/` file, no model routing, provider policy, approval rule or role floor.
6. **Recovery state.** Use `tp-agent objective` and the session index for package path, revision, slice and next action.
   The package's own manifest is the durable record.
7. **Review profile (PD-D/PD-F).** Prefer `tp-review --contract FILE` with a product-review contract file over any new
   reviewer profile. Ordinary code review and provider independence must not change.
8. **Harness entry points (PD-D).** Claude: a command file under `claude/plugins/tmux-pi/commands/` like the existing
   `product-fit.md`. Pi: read how pi loads skills and prompts in this repo and use that path, or document the explicit
   prompt path if there is no alias parity. Do not document a command that does not exist.

## Every phase
- **Attack the rule before you report.** For each rule the phase adds (validation, changeset apply, import dedupe and
  conflict, readiness and staleness, anchor remapping, case blocking, packet scope, note capture, reset/back replay),
  name its input space from an authoritative source (the schema's record types and fields, the status enums, the changeset
  operations, prior states × events, concurrent writer interleavings), print the list by code, run every case against the
  real code with the expected outcome and the reason, and report counts (cases, matches, mismatches fixed). Script and log
  under `evidence/`. Fix mismatches by the rule; keep the cases worth keeping as test rows.
- **Fail-before** for each test that guards changed shared behaviour (catalog selection, command discovery), against the
  lane base. New-only modules need no fail-before; say so.
- **Security.** Note text, sources and imported data are inert: escaped when rendered, never `eval`-ed or run. No network
  calls from any command. Never read `.env*` and never write that string in a shell command. Never search `bin/` as a
  directory; never search `claude/`, `agent/` or `config/` recursively; name the files.
- **tmux.** Any test that starts tmux sets `TMUX_TMPDIR` to a fresh `mktemp -d /tmp/pd.XXXX` and uses a private `-L`
  socket. Never the default server, never `kill-server` without `-L`. No live model, no Claude or pi start, nothing sent
  to 127.0.0.1:1234.

## Done means (each phase)
1. The phase's acceptance in plan section 12 is met, shown by tests or by a script and log under `evidence/`.
2. The new tests plus `tests.test_session_env`, `tests.test_skill_catalog` (if it exists) and any test touching a shared
   file you changed pass with `/usr/bin/python3` and `/opt/homebrew/bin/python3`, both outside tmux (`env -u TMUX -u
   TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`) and inside (`TMUX=/tmp/x,1,1 TP_SESSION=dummy
   TP_TASK_ID=dummy`). Rerun after your last edit and list each file's time per cell. Respect SLOW_TESTS.
3. The attack table counts, Self-review, the list of shared files changed, and the plan deviations you chose with reasons.
4. `RESULT_STATUS` on its own line as the last line. No background shells left running.
