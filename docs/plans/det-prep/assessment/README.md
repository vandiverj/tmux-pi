# Independent assessment (Fable, 2026-11-10): what it adds to the queue

Source: `fable-assessment-2026-11-10.md` (owner-provided, verbatim; inspected commit 5ac86c6, code and docs only, no transcripts or
runs). Owner request: "read this document and add any additional eval or work queues that make sense for tmux-pi". Main mapped
every proposal to an existing queue row or HUB item, or added a row (H43-H55 in `../loop/ideas.md`). Nothing here changes the
HUB order (`../hub/README.md`); new rows are queued behind it unless marked otherwise.

## Claims Main re-checked on main 5c9d7f5 (2026-11-11)
- `lib/gate_record.status` (fresh/stale/invalid) has callers only in `tests/test_agent_tools.py` and `tests/test_gate_record.py`;
  nothing in `bin/` or `lib/` reads it. Confirms "records without readers" (→ HB5).
- `--deny-prompt` (h1-main-guards.md) has 0 matches in `bin/` files. Confirms the missing message guard (→ HB4, lane hub-correct).
- README.md:49 says `dispatcher.ts`/`subagents.ts` load only under `TP_MODE=strict`, which nothing in `bin/` sets (→ H49).
- `gh repo view`: vandiverj/tmux-pi is **PUBLIC**. The assessment says `claude/mcp.json` carries a work tracker hostname and a
  private network address. Main did not open that file (standing rule) (→ H55, owner decision).

## Mapping
| Assessment proposal | Where it goes | Note |
|---|---|---|
| P1 deny-with-redirect in worker sessions | SG1 (lane safe-search, in review) | SG1 already refuses worker shell searches/sensitive reads and names the safe tool. Its fixture gains the replay below |
| P1 replay fixture of past blocked commands (17 review prompts, H27 coder prompts) | H37 HB4 + SG1 follow-up | Baseline before/after; each must get a deny whose reason is a runnable safe call |
| P1 standing tool lines for every worker, with or without a manifest | H35 HB2 | Already HB2 rule 1 |
| P1 `message` refuses at an open prompt; `redirect` = Escape, wait, send; wake notice names it; fix `orchestrator-claude.md` "do not act" sentence | H37 HB4 (HB4a running, HB4b next) | Added the wake-notice and orchestrator-claude.md points to HB4b |
| P2 bar in every brief, finding labels, sweep at first cause-stating rejection, round-3 design choice | **H43 RC1** (new) | Extends the 2026-11-10 realistic-use decision repo-wide; hostile bar only for trust/redaction/sensitive-read |
| P2 property-based and stateful tests | **H44 PB1** (new) | Hypothesis is a new dependency: owner approval, pinned isolated, 3.9.6 check |
| P2 ast-grep sweep | H13 (exists) | Unchanged |
| P3 holder line, stall wake, holder-change events | H39 HB6 | Added the historical wait derivation and the 95%-agreement check |
| P4 `tp-agent evidence`, packet facts file, close reports before refusing, test map | H38 HB5 (+H28, H33) | Added the M2 selection replay and duplicate-run count |
| P4 time the ten slowest test files | **H45 TS1** (new) | Answers the assessment's question 7; decides selection vs speed-up |
| P5 harness preamble, typed standing orders, procedure cards by state, triggers per eval tool | H35 HB2, H36 HB3, H40 HB7 | Cards capped in count and size; each needs a must-not-fire case |
| P5 four-arm ablation, removal arms, E8 negative controls, held-out variation, at least 5 runs per cell | **H46 AB1** (new, extends H41 HB8) | HB8a builds the harness these arms run on |
| Idea A: process mining over an event log | H39 HB6 | `events.jsonl` (interfaces.md §3) is the log; first test is after-the-fact derivation |
| Idea B: return brief | H36 HB3 (`tp-agent brief`) + H39 HB6 view | Add timing: attach to first correct instruction, with and without |
| Idea C: decision-point replay | **H47 DR1** (new) | 15 hand-built snapshots first |
| Q1: share of interventions that are process reminders | **H48 IL1** (new, priority high, cheap) | Re-ranks everything; audit C F6 has 8 quoted corrections as a start |
| Remove legacy Main and strict mode; one copy of each prose rule | **H49 LG1** (new; owner decision) + HB2 | Removal is the owner's call |
| README drift from the catalog; generate config-derived parts | **H50 DD1** (new) | README says Haiku first; the catalog says local first |
| Router value for strict-floor roles; catalog's 5-12% expected local share | **H51 RT1** (new) | Read the selection log only; any config change is the owner's |
| Local model as a checked single-call function; requalify by model build; fallback rate | **H52 LQ1** (new) | Never a gate, reviewer vote or authorization |
| `ask_local` for Claude Main | **H53 AL1** (new) | Pi Main has it; Claude Main does not |
| Deterministic commit trailers; local drafting of Main's commit messages | **H54 CM1** (new) | Trailer from the resolved model is code, no model needed |
| Side note: work endpoints in the tracked `claude/mcp.json` of a public repo | **H55 PH1** (new; owner decision) | Matches the HUB rule "keep machine-specific settings separate" (HB9) |
| Work-Mac sequence: version manifest, five canaries, a week in observe mode, a switch per feature, Fallow report-only | H42 HB9 | Added as HB9 requirements |
| Episode kind inside tp-bench (no second runner) | H41 HB8: decision at HB8a review | HB8a builds `tools/episodes`. Fold into tp-bench as an `episode` kind if it shares the sealed scorer cleanly |
| Reject: bigger memory store, more role text, blocking quality gates, DRY/SOLID checks | Agreed; no row | Matches the owner's "keep advisory" (code-quality eval) |
| Defer: OTel (H16), summarizer on a critical path, goal mode, freeze guard, hard refusal at close (B2) | H16 stays deferred; others as stated | H7 blind second reviewer: the assessment advises against it. Left open for the owner |
| Watch: native agent teams | No row | Keep files and records as the contract |

## Questions for the owner (from the assessment's list, still open)
1. ~~Which Main is the target?~~ **Answered 2026-11-10:** "lets only worry about claude main for now and bring pi main up once we
   have the processes all working". HB4b/HB6 and H53 target Claude Main; pi Main parity is a later follow-up, not a blocker.
2. Where is the hostile bar required? The assessment proposes trust, redaction and sensitive-read code only (H43).
3. May session metadata (event files, decision snapshots, never repository content) leave the work Mac? H47 and the HB9 tuning loop
   depend on it.
4. Which parts do you want to own in six months? This decides H49 and how much goes into the Claude plugin.
5. H55: move the work endpoints out of the tracked `claude/mcp.json` (and the routines.json:42 names you said to leave), and
   whether to rewrite history.
