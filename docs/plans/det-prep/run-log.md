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
