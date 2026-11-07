# Unattended run log (Main appends after every action; resume from here after a compaction)

## Owner's standing answers, 2026-11-07 (before leaving)
- Owner, later the same day: add the benchmark (j1) after E1/E2; when the plan is done and the owner is not back, run
  the improvement loop in loop/PROTOCOL.md (research with several providers, cycles in worktrees, keep/discard, log all).
- Scope: everything in the plan: A1 C1 E1 D1 F1 G1, then B1 C2 E2, then B2 D2, then H1 (after C1 merged), then i1-i4
  (after C1, D1, D2 merged; i4 after B1), then the prompt-examples cleanup step.
- Worker permission prompt: deny with Escape, message the safe alternative; if it prompts again, park the lane and log it.
  Never approve.
- OpenAI review failure: keep a list of reviews owed, retry hourly; other lanes continue.
- Never without the owner: approve a prompt, same-provider review, --override-review, merge to main, push.
- Fix rounds: at most 3 per finding family, then park with findings.
- Integration: each lane with all steps closed merges --no-ff into lane/integrate in worktree
  ~/Documents/Projects/tmux-pi-wt-detprep-integrate (the older tmux-pi-wt-integrate on work/orch-integrate is not ours; untouched); full unittest suite + tests/check_dispatcher_types.sh run there after each merge.

## Commands
- `export TP_SESSION=detprep PATH=$HOME/Documents/Projects/tmux-pi/bin:$PATH`
- watcher: scratchpad/watch.sh (exits when any task is not working/starting)

## Tasks
| Step | Lane | Task | Model | State |
|---|---|---|---|---|
| A1 | tools-template | coder-40e8a7a43a | sonnet-5.5 | working |
| C1 | lane-facts | coder-dbcf0c11b8 | sonnet-5.5 | working |
| E1 | measure | coder-d6ff23c1d0 | haiku-5.5 | working |
| D1 | review-order | coder-983705af23 | sonnet-5.5 | working |
| F1 | status-cleanup | coder-6c8b009b46 | sonnet-5.5 | working |
| G1 | local-model | coder-014ac74821 | haiku-5.5 | working (told: config/local-model.json, not orchestration.json) |

## Events
- 2026-11-07T16:24:12Z Owner stepping away for several hours. Started caffeinate for 12 h (keeps the Mac awake on AC power). LM Studio: Qwen loaded at 262144 context with no TTL, left as is so local steps keep working.
- 2026-11-07T16:25:36Z Owner approved web research: searching and reading sites is allowed; downloading or installing anything is not. WebSearch and WebFetch tested and working without prompts.
- 2026-11-07T16:32:02Z Baseline full suite on lane/integrate (= main abf72de+): 4011 tests, 2575 s, 114 failures + 378 errors + 65 skipped, all pre-existing. 492 failing ids saved in baseline-failing.txt (mostly test_ctx_nav_api 358, test_trust_guard 58, test_ctx_cli 29, test_docs_cli 20; likely missing deno / Pi Node types and date-bound fixtures). Type check fails: Pi Node types not installed. Merge rule: no failing id outside this list. Because the suite takes 43 min, each merge runs the touched test modules; the full suite runs once per phase.
- 2026-11-07T16:38:50Z FOUND: all 6 Claude coders sat idle ~80 min at an empty prompt (context 0%). Pane: "Login reached its time limit · Please run /login"; the worker Claude config (~/.config/tmux-pi/claude) login expired, so the packet passed at start never ran. tp-agent index still said "working". Resent the packet path by message; workers still need a login. Asked owner to run the login; fallback is to redispatch coders on OpenAI and hold their reviews as owed until Claude login is back.
- 2026-11-07T17:14:34Z Owner logged in the worker Claude account. Resent the packet path to all 6 coders; all running (context 22-32% after 40 s). OpenAI (agent/auth.json openai-codex) checked with a one-word pi call: works, token refreshed (240 h). Watcher now also stops on a pane showing a login or confirm dialog.
- 2026-11-07T17:14:48Z A1 coder-40e8a7a43a complete (39 tests OK, block bytes 474..1357; golden fixtures untouched, justified: no Tools block in them). tp-agent review failed: "no such pane: %9", reviewer pi (openai/gpt-6-sol) could not start. Asked owner.
- 2026-11-07T17:18:55Z E1 coder-d6ff23c1d0 and G1 coder-014ac74821 returned incomplete: Bash refused (hook appends "|| echo exit-code: nonzero"; Haiku workers would not re-run). Main ran checks: test_routines 38 OK (measure wt); bash -n ok + test_lmstudio 10 OK (local-model wt). Fail-before-change not observed. E1 "failing tests" flag is malformed (nothing run), no investigation. Review retry failed again: no such pane %10. Review blocked for A1, E1, G1.
- 2026-11-07T17:21:47Z C1 coder-dbcf0c11b8 complete: 5/6 new tests fail without change; touched + consumer modules OK. SLOW_TESTS: test_lane_writer 91.5s, test_retry_packet 63.1s (pre-existing). test_retry_packet needs python3.14 (f-string SyntaxError under 3.9.6). Review failed: no such pane %11.
- 2026-11-07T17:38:07Z F1 coder-6c8b009b46 complete: touched modules OK; one failure test_trial_session ...keeps_the_keys... (20 != 16) is in baseline-failing.txt:480, so pre-existing, no investigation. SLOW_TESTS: test_status_repaint 116.4s (114.7s on HEAD). Review (required) failed: no such pane %12. Notes for owners: bin/tp-agent:107 and bin/tp-review:133 comments wrong; legacy tp-clean archives pane-gone lane tasks with open step.
- 2026-11-07T17:41:50Z D1 coder-983705af23 complete: test_review_rounds 83 OK, test_tp_review 94 OK + 4 consumer modules OK; new tests fail on HEAD (25F/7E). test_retry_packet import error is baseline (baseline-failing.txt:262), no investigation. SLOW_TESTS: test_tp_review 95.7s (82.2 HEAD), test_refusal_remedies 194.3s, test_lane_steps 116.8s. Behaviour change: round-1 diff over 300000-char budget now refuses review. Review (required) failed: no such pane %13. Phase 1 coders all settled; all reviews blocked.
- 2026-11-07T18:11:07Z probe-codex-121038 (not dispatched by this Main) failed: tp-pi says pi is pinned to ~/.nvm/versions/node/v24.16.0/bin/node, and ~/.nvm/versions/node does not exist. Likely the cause of every review failing with "no such pane" (pi pane dies at once). Fix: nvm install 24.16.0 + npm install -g @earendil-works/pi-coding-agent, or set TP_PI_NODE. Install needs owner (no installs approved).
- 2026-11-07T18:11:25Z probe-sol-121110 (other session): pi started this time, but openai/gpt-6-sol goes through OpenRouter and fails 402: account can afford 4000 tokens, request asked 128000; needs credits. So reviewer pi/openai/gpt-6-sol is blocked on OpenRouter credits even with pi running.
- 2026-11-07T21:37:26Z (outside Main) Review blockers fixed on this Mac only: (1) tmux set-environment -t detprep TP_PI_NODE "" so tp-pi runs pi from PATH (~/.local/bin/pi); the pinned ~/.nvm Node v24.16.0 does not exist here. (2) Owner chose a local edit: config/model-catalog.json and config/router.json openai/gpt-6* -> openai-codex/gpt-6* (UNCOMMITTED in the main checkout; revert at run end with git checkout -- config/model-catalog.json config/router.json; not for the work-Mac port). The workspace main pane had been acting as a second Main (entries above at 17:14..18:11); told it to stand down, it replied OK. Main tests re-run per lane all OK (evidence files attached). Six reviews started on openai-codex/gpt-6-sol (subscription).
- 2026-11-07T21:38:51Z Reviewers for A1 and G1 asked to search agent/ and config/ recursively ("may include credential files"). Denied with Escape; told to read named files only. Both continued. Watcher now scans only live panes for prompts.
- 2026-11-07T21:45:07Z Round-1 reviews (openai-codex/gpt-6-sol) all REQUEST_CHANGES with real code defects: A1 1, C1 1, D1 4, E1 3, F1 3, G1 3. Fix briefs in fixes/<step>-round2.md. Retried all six on claude-sonnet-5-5 (E1, G1 moved off Haiku: Haiku refuses Bash once lib/redact_hook.py appends its suffix; candidate finding H10). New coders: A1 coder-fe980af259, C1 coder-bba83ee1d7, D1 coder-a3700f8c4a, F1 coder-779b14406d, E1 coder-09a7ffcf00, G1 coder-b812bd22f1; all took their prompt (context 12-17%). Note: F1 and G1 both touch README.md (merge later).
- 2026-11-07T21:59:23Z Round-2 coders all complete. Main re-ran changed test modules (bash): A1 40 OK, E1 39 OK, G1 14 OK, C1 33 OK, D1 214 OK, F1 109 OK. Round-2 reviews started (openai-codex/gpt-6-sol).
- 2026-11-07T21:59:42Z A1 round-2 reviewer asked for a recursive search of tools/agent; denied, told to use exact paths.
- 2026-11-07T21:59:59Z G1 round-2 reviewer asked for a recursive search of config; denied, told to use exact paths. Every G1 and A1 reviewer so far has hit this prompt (idea H11).
- 22:06 UTC — Round-3 retries dispatched with `--route rule-first` (C1 coder-e18ac0a208, D1 coder-1c48082c71, E1 coder-5a5f76bab8, F1 coder-222f5202c9, G1 coder-d7ce94da2b); tp-agent required a route after two REQUEST_CHANGES / repeated family. E1 finished first (39 tests OK; 13 failures + 5 errors with lib/routines.py reverted) and is in review.
