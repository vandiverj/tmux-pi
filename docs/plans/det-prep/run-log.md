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
- 22:09 UTC — G1 round 3 done: 14 tests OK. Live engine_mb on this Mac gives 21623 MB (the LM Studio engine node process; its inline script contains "llmworker"). In review. G1 reviewer asked for a recursive config search; denied with Escape and redirected to exact paths (H11 again). C1 round 3 done: 16 + 20 tests OK, and both new tests fail with tp-spawn and lane_commands.py at HEAD. In review.
- 22:12 UTC — E1 and G1 round-3 reviews: REQUEST_CHANGES, the same family again (E1: `git -c k=v fetch`; G1: tp-exit from a worker pane dies in tp-reset before the unload, plus new TTL-in-status). Both parked per the 3-round limit with `--route park`; notes in fixes/e1-park.md and fixes/g1-park.md. F1 round 3 done: 28 + 40 + 45 tests OK. In review.
- 22:13 UTC — Phase 2: B1 dispatched as coder-2dc7b5f969 on lane evidence, based on lane/integrate (which includes A1), Sonnet. Overlap warnings: config/orchestration.json with status-cleanup (resolve at merge), tools/agent/test-run.sh with tools-template (merged). C2 waits on C1; E2 waits on E1, which is parked.
- 22:18 UTC — The owner asked for a recommendation on parked E1 and G1. Two Sonnet researchers ran, read-only.
  - E1: the loop came from handling git's option grammar with a regex. Each round patched only the cited form. No real transcripts contain a flagged fetch. Fix by rule: a git global-option parser, about 35 lines, with a table of forms.
  - G1: the unload is ordered after steps that can kill the caller (reset at tp-exit:28, kill at :54). Tests run outside tmux, so a caller dying mid-run is never exercised. Fix: make the unload decision first and run the unload detached (nohup; this Mac has no setsid), with a test whose reset stub kills the caller. F2: `lms ps --json` gives ttlMs (null for Qwen here), so read it.
- 22:23 UTC — C1 APPROVED_WITH_NOTES in round 3 (note: the golden covers only the # Context suffix). Committed a58e23c, lane closed, merged into lane/integrate as 7eaded5. Touched modules on integrate: worker_env 16, lane_other_paths 20, lane_create 45, lane_lease 25 and preamble 40, all OK. test_retry_packet has a Python 3.9 f-string SyntaxError that is in the baseline.
- F1 round 3: REQUEST_CHANGES (rollback is not a full snapshot; the %N check is a prefix check only; g/G go by display position). Parked; see fixes/f1-park.md.
- The owner chose round 4 for E1 and G1 (--route design). Briefs are fixes/e1-round4.md (git global-option parser and a parity table) and fixes/g1-round4.md (decide first, detached nohup unload, a reset stub that kills the caller; ttlMs in status). E1 is coder-169502cc17 and G1 is coder-b1a1f0afad.
- D1 round 3 done (85, 36 and 96 tests OK) and B1 done (4, 63 and 40 OK); both in review. C2 dispatched on lane-facts.
- 22:23 UTC — The D1 and B1 reviewers asked for a recursive agent/ search. Denied and redirected to exact files (H11).
- 22:28 UTC — The owner chose round 4 for F1 (coder-fd0d9b14db; fixes/f1-round4.md). D1 round 3 REQUEST_CHANGES on one finding (staged-diff-clarity); round 4 dispatched as coder-eade871e2c (fixes/d1-round4.md), the same choice the owner made for E1, G1 and F1. B1 round 1 REQUEST_CHANGES with 4 code findings (gate fails open on copy errors; multi-app checks; required fields per status; record-number race); round 2 is coder-88290d3d23 (fixes/b1-round2.md, written H12-style). C2 blocked on bin/tp-spawn ownership; Main told it to proceed inside read_context only.
- E1 round 4 is a git global-option parser; 41 tests OK. G1 round 4 is a detached nohup unload decided first, with ttlMs in status; 16 tests OK, and the coder showed the caller-kill test fails on the round-3 order. Live status: "ttl none, engine rss 21619 MB"; it also warns that Qwen is loaded at 262144 context against a recommended 65536. Both in review.
- 22:28 UTC — The G1 round-4 reviewer asked for a recursive config/ search. Denied and redirected (H11; about the seventh time).
- 22:33 UTC — With the owner's OK, reloaded Qwen between tasks (no local worker active; model idle, queue 0). Was context 262144 with no ttl at 21619 MB; now context 65536, ttl 1800 s, 20345 MB. Saving about 1.3 GB: the weights dominate, not the KV cache.
- The owner asked why reviewers keep hitting "Authorize sensitive read?". Diagnosis:
  - 8 of 17 reviews stopped on it, all on `git grep` over agent/, config/ or tools/agent inside the lane worktree.
  - tp-review's outside_rule (bin/tp-review:219) tells reviewers to use safe_read/safe_grep only for files OUTSIDE the worktree.
  - lib/sensitive_read.py treats agent/, config/ and claude/ (CREDENTIAL_DIRS, line 61) as credential dirs, even for plain git grep.
  - Plain git grep reads tracked files only. The credential files (agent/auth.json, agent/sessions/, config/local.json) are gitignored, and none are tracked in any lane worktree.
- 22:35 UTC — The owner approved Fix B. Lane sensitive-grep K1 dispatched as coder-5c2f8af2df (Opus, base lane/integrate). Fix A interim: the search note is appended to review evidence from now on; the real change goes into D2 (bin/tp-review).

## 2026-11-07 23:00Z: G1 merged; E1 round 4 rejected; five reviews out
- G1 round 4: APPROVED_WITH_NOTES (the note asks only for the missing sweep table). Committed as `e854161`, lane closed, merged into lane/integrate as `46e44b0`. `test_lmstudio` passes 16 of 16 there.
- E1 round 4: REQUEST_CHANGES. One new site in the probe-specificity family: `git_subcommands` treats a shell comment as a command, so `echo ok # x && git fetch` counts as a fetch. All eight swept rows were confirmed. Waiting on the owner: a round 5 or a merge with the limit documented.
- C2, F1 r4, D1 r4, B1 r2, K1: Main ran each coder's modules in its own worktree. The only failures were 4 in `test_orchestration` (C2) and 1 in `test_sensitive_trust` (K1), all listed in the baseline. Reviews went out with the search note. C2 was held because its status line was mid-sentence; it has been asked to restate.
- K1 open owner decision: tracked `agent/mcp.json`, `claude/mcp.json` and `bin/tp-secret-exec` keep the prompt for `agent/`, `claude/` and the repo root.

## 2026-11-07 23:10Z: four reviews rejected; rule-shaped retries out
- F1 r4 REQUEST_CHANGES: the rollback misses symlinks, writes through a planted link, loses file mode, and leaves the archive reservation. Retry `coder-c556d93f24` (design): `fixes/f1-round5.md`, a whole-directory copy swapped back on failure.
- D1 r4 REQUEST_CHANGES: the staged section compares against the base, not HEAD. Retry `coder-3464a832e0` (design): `fixes/d1-round5.md`, the four-state rule with a 15-partition table.
- B1 r2 REQUEST_CHANGES: an ESLint aggregate across apps, a TypeError on a wrong-typed field, and a record written without a tree. Retry `coder-a7350b4c4b` (design): `fixes/b1-round3.md`, per-app checks, a type table, and fail closed.
- K1 r1 REQUEST_CHANGES: `-f FILE` and `--textconv` pass as plain. Retry `coder-3a6e22f84f`: `fixes/k1-round2.md`, an option allowlist checked by a parity test against `git grep -h`.
- C2 is in review (`review-coder-86b4ed5fe2-170237`).

## 2026-11-07 23:40Z: owner decisions applied (E1 accepted limit, K1 option 2)
- Owner: "use your recommendations for E1 and K1 questions".
- E1: the comment limit is documented in the `_simple_commands` docstring and carried into `e2-claude-coverage.md`. Committed in the worktree as `6fbb8c9`. `lane close` needs a review of this tree, and `--override-review` is not allowed, so a fresh review is out with evidence that the owner accepted the limit.
- K1: option 2 is added to `fixes/k1-round2.md`: a tracked credential-named file that matches the committed version no longer blocks the lift. Before adding it, Main checked the committed files by structure only, with no values printed. Every credential field in both `mcp.json` files is a `${VAR}` reference, and `tp-secret-exec` has no token-like strings. The running K1 coder was messaged.
- C2 r1 REQUEST_CHANGES (four-backtick fence, trailing `#`, empty heading). Retry `coder-52ad5a71e4`: `fixes/c2-round2.md`, CommonMark fence and heading rules.
- F1 r5, D1 r5 and B1 r3 coders finished; Main is running their tests.

## 2026-11-08 00:00Z: E1 merged; F1/D1/B1 rejected again with new rules
- E1 re-review: APPROVED_WITH_NOTES (the only note is the accepted comment limit, owned by E2). Lane closed and merged into lane/integrate as `f63faae`; `test_routines` OK.
- F1 r5 REQUEST_CHANGES: a write after the copy is lost, a FIFO can't be copied, a hard link is split, and a parent cycle is mishandled. The rollback approach is dropped. Retry `coder-3ebc82a9fa` (`fixes/f1-round6.md`): an atomic, repeatable reconcile in place, then one rename, and parent cycles are skipped.
- D1 r5 REQUEST_CHANGES: a prior-HEAD filter hides base→HEAD. Retry `coder-c905fc8013` (`fixes/d1-round6.md`): no filter but the four states.
- B1 r3 REQUEST_CHANGES: `tp_json_str` drops control bytes. Retry `coder-7fb9a831b4` (`fixes/b1-round4.md`): a single Python serializer and a byte round-trip table. Main accepted the base ImportError as the before-state for the new module.
- K1 r2 (allowlist plus option 2) and C2 r2 (CommonMark) are done; Main is running their tests.

## 2026-11-08 00:30Z: K1 r2 and C2 r2 rejected; F1/D1/B1 coders done
- The K1 r2 reviewer hit "Authorize sensitive read?" (a recursive search of a temp fixture). It was denied with Escape and redirected to literal paths and direct classifier calls, and it finished without a second prompt.
- K1 r2 REQUEST_CHANGES: `--assume-unchanged`, `--skip-worktree` and `core.filemode=false` hide edits from `git diff --raw`. Retry `coder-805ee335ab` (`fixes/k1-round3.md`): hash the bytes on disk to a git blob id and compare it with HEAD; refuse flagged entries.
- C2 r2 REQUEST_CHANGES: `#Rules:2-3` is parsed as a range, trailing whitespace handling, and the parity test is not on the table fixtures. Retry `coder-17ff9d75bb` (`fixes/c2-round3.md`): one parse order.
- F1 r6, D1 r6 and B1 r4 coders are done; Main is running their tests.

## 2026-11-08 00:45Z: B1 merged; F1/D1 final rounds
- B1 r4: APPROVED_WITH_NOTES. The before-state note was already ruled on by Main in `b1-round4.md`: the base's ImportError counts because the module is new. Committed as `074a518`, lane closed, merged into lane/integrate as `4fe780a`. Main is running the touched modules there.
- F1 r6 REQUEST_CHANGES: the reconcile writes future archive paths, and an empty-directory rename race. Retry `coder-627bab8253` (`fixes/f1-round7.md`): location-free paths and an owned mkdir reservation. This is the final round; F1 parks if it is rejected.
- D1 r6 REQUEST_CHANGES: a later-round scope recomputed against HEAD, and commands that fail with no commits. Retry `coder-858a228fd2` (`fixes/d1-round7.md`): the scope comes from the attached diffs, the empty tree stands in for HEAD, and every printed command is executed. This is the final round.
- K1 r3 and C2 r3 coders are done; Main is running their tests.

## 2026-11-08 01:00Z: K1 and C2 merged; F1/D1 final rounds in test
- K1 r3: APPROVED_WITH_NOTES (the only note is test cost, over the five-process budget). Committed as `63ef110`, lane closed, merged into lane/integrate as `dedc6ce`.
- C2 r3: APPROVED_WITH_NOTES (the only note is that the 4 orchestration failures weren't reproduced on the base; Main's diagnosis traces them to the session env leaking into the tests). Committed as `1263473`, lane closed, merged as `1abfb88`.
- Owner: "send an agent to figure out why the 4 failures are occuring". Cause: `tests/test_orchestration.py` passes `CLAUDE_CODE_SESSION_ID`/`TP_MAIN_ID` through to `tp-agent`, so "main missing" is filled in from the env (since `03065fe`). The tests pass when those variables are unset. Owner: "yes, open the lane and rerun the baseline list". The baseline modules are being rerun without the session env; the lane brief follows from the result.
- F1 r7 and D1 r7 (final) coders are done; Main is running their tests.

## 2026-11-08 01:30Z: D1 merged; F1 parked to the owner
- D1 r7 (final): APPROVED_WITH_NOTES (report-evidence gaps only). Committed as `cbf045b`, lane closed, merged into lane/integrate as `5ea21ba`.
- F1 r7 (final): REQUEST_CHANGES on a new sweep site. `bin/tp-contract:138-140,226` writes `task.md` through `write_record` with absolute paths, without `relative=True`. If a `tp-contract status` lands between the reconcile and the rename in `lib/task_record.py:837-868`, the archived record links to the old location. Family: retirement-dependency-order. As planned, F1 is **parked for the owner** after round 7. The worktree `tmux-pi-wt-status-cleanup` is left uncommitted and intact. Options for the owner: (a) one more round that applies `relative=True` to every `write_record` caller, enforced by a parity test over the callers, or (b) take the rename under the same task lock that `tp-contract` holds.
- Battle-soccer V-F waits on F1.

## 2026-11-08 02:10Z: baseline rerun without the session env; T1 lane opened
- All 15 baseline modules were rerun in lane/integrate with `CLAUDE_CODE_SESSION_ID`, `TP_MAIN_ID` and `TP_TASK_DIR` unset. An earlier attempt in the main checkout is void: Main's local catalog edit (`openai/gpt-6` → `openai-codex/gpt-6`) made `openai/gpt-6-sol` fixtures fail.
- Session-env-caused: only the 4 `test_orchestration` entries. That module passes 40/40 without the session env.
- The 5 `unittest.loader._FailedTest` entries are not session-env-caused. They come from a full-suite discover run and don't show up when modules run by name.
- Real failures, unchanged without the session env: 483. Among them are the ctx/docs modules (test_ctx_nav_api 357 errors, test_ctx_cli, test_docs_cli, test_docs_wiring), test_trust_guard 58, and one each in test_sensitive_trust (`test_rg_…`), test_lane_recovery, test_lane_records, test_run_bounded, test_trial_session, test_trust_record and the trial_shadow modules.
- T1 lane `test-env` opened from lane/integrate (`5ea21ba`). Brief `t1-test-session-env.md`: a shared `tests/session_env.py` helper, a parity test over modules that copy `os.environ`, and a positive optional-main case. Coder `coder-47ae1f2cac` (claude-sonnet-5-5).

## 2026-11-08 02:20Z: F1 unparked for one owner-approved round
- Owner: "use your recommendation for F1". Recommendation: make records location-free by construction, not a lock. Remove `write_record`'s `relative` parameter so every in-task path is written by name. Readers resolve relative links against the record directory, and legacy absolute links still read. A parity test checks the caller table against `git grep`.
- Retry `coder-dd5117f3c0` (rule-first): `fixes/f1-round8.md`. If it is rejected, F1 parks again; there is no round 9.

## 2026-11-08 02:40Z: RTK eval started; M1 queued
- Owner: "run an eval on whether RTK would be useful … set it up for claude and pi in the test branch".
- RTK v1.1.0 release binary, sha256-verified, is at `~/.local/rtk-eval/bin`. It is not on PATH; no `-g` init and no brew install were used, and telemetry is off.
- On branch `rtk-eval` (worktree `tmux-pi-wt-rtk-eval`, from `5ea21ba`), `TP_RTK=1` adds RTK's hook to `tp-claude` and its extension, loaded last, to `tp-pi` (`24d7bc5`). Both smoke tests rewrote commands, and both answers were correct.
- Plan and decision rule are in `docs/plans/rtk-eval/plan.md` (`25e2de2`). A background agent is running phases 0–3 (ceiling, replay fidelity, guard compatibility, live A/B).
- Owner proposed model-specific worker profiles, citing arXiv:2609.20804. The brief is `m1-model-profiles.md`, queued after the RTK eval and before J1.

- 2026-11-08: owner added ast-grep structural sweeps as loop idea H13 (replay on F1/D1/K1/C2/E1/G1 families, then A/B on review-loop tasks); RTK eval and M1 registered as H14/H15.
- 2026-11-08: owner added OpenTelemetry usage monitoring as loop idea H16 (Claude built-in OTel + Pi extension, local stdlib receiver, overhead/fidelity/insight eval before adoption).

## 2026-11-08 01:05Z: F1 round 8 rejected, F1 parked
- Main's 8 modules all passed (evidence13). Review `review-coder-dd5117f3c0-183947`: REQUEST_CHANGES, rounds 1–7 cases confirmed fixed.
  - F1 (retirement-dependency-order, still open): an in-flight `tp-contract status` that writes after `archive_task` renames the task recreates `tasks/<id>` as a ghost dir without `meta.json`. The round-8 race test only covers a write before the rename. Location-free paths do not stop this; it needs either a writer that refuses to create a missing task dir, or a lock.
  - F2 (new): `lib/tp_window.py:190-207` footer counts escalated/other omitted tasks as "finished".
- Per the agreed rule there is no round 9. Worktree `tmux-pi-wt-status-cleanup` is kept uncommitted for the owner. Owner decision needed: (a) one more round with "write_record never creates a task dir" (closes the ghost-dir family by construction) + F2 fix, (b) a lock, or (c) leave F1 out of this campaign. V-F stays blocked.
- Process note: another missed-site round in the same family; a candidate replay case for H13 (ast-grep cannot express the cross-process ordering, so this one also tests H13's limits).
- 2026-11-08: owner added experiment X1 (strong-model traces → smaller-model harness aids; A/B/C/D arms, sealed holdout, stop before adoption). Requirements recorded in `x1-trace-distillation.md`; loop idea H17. Design pending harness inventory.

## 2026-11-08: F1 round 9 (owner: "use your recommendation for F1"), final round
- Recommendation taken: (a). Only the task creator creates a task dir; every other writer writes through a directory fd and refuses if the dir is gone (late writes land in the archive, which is valid because records are location-free). The footer names omitted rows by their real state. No lock.
- Retry `coder-32886e6158` (pane %94), brief `fixes/f1-round9.md`. If rejected, F1 is left out of this campaign.
- 2026-11-08: X1 design written (inventory: tp-bench not built, so X1 is blocked on J1; tmux-pi has no battle-soccer runs yet, so friction is mined from pi-improvement-workbench + det-prep; acceptance/budgets/promotion frozen in the plan).
- 2026-11-08: owner added eval HL1 (pi-hashline-readmap for Pi coder + Claude portability), loop idea H18. Phases 0–1 (source review, guard parity, edit micro-bench) can run before J1; phase 2 needs J1. Install isolated, never `pi install`.
- 2026-11-08: owner: "run phase 0 and 1 of HL1 now". Background agent started: worktree `tmux-pi-wt-hl1-eval` (branch hl1-eval from lane/integrate), extension pinned under `~/.local/hl1-eval`, results in `docs/plans/hl1-eval/results.md` on that branch. Haiku capped at 24 runs.
- 2026-11-08 01:15Z: owner: "start building J1 tp-bench now". Brief amended (start before E2: null tokens where unavailable; transcript paths, `--env`, runtime versions, random order for X1/HL1). Lane `bench` created from lane/integrate; coder `coder-c28f7b48f3` (claude-sonnet-5-5). Overlap warning: `tests/test_bench.py` vs lane test-env (T1's parity test will need it converted or exempt; resolve at merge).
- 2026-11-08: owner's future experiments recorded as loop ideas H19 requirement change, H20 semantic integration conflicts, H21 external-state reconciliation (bench task kinds after J1).

## 2026-11-08: F1 round 9 rejected; F1 out of this campaign
- Main ran 15 modules, all passing (evidence14). Review `review-coder-32886e6158-192417`: REQUEST_CHANGES. All earlier cases (rounds 1–8) are confirmed fixed, and the ghost-dir race and footer from round 8 are closed.
- Two new defects were introduced by round 9's single-creator change (family retirement-dependency-order):
  - F1: when two spawns race on one id, `create_task_dir` returns False but `write-many` still prints `owned`, so `tp-spawn:613` adopts, overwrites and can delete the other task.
  - F2: `tp-contract build` no longer creates a nested parent for a custom artifact path (`<task_dir>/reports/out.md`).
- Per the owner-approved rule there is no round 10. F1 is left out of this campaign. Worktree `tmux-pi-wt-status-cleanup` is kept uncommitted (rounds 1–9) as evidence and as a starting point if the owner revisits it. V-F stays unrun.
- Lesson for H13/X1: 9 rounds, each fixing the cited site while the family stayed open. Round 9's fix opened new sites in the creator path. Candidate replay case for H13 and an X1 "incomplete scope" example.
- 2026-11-08: T1 review `review-coder-47ae1f2cac-192701` REQUEST_CHANGES (F1 baseline edit = Main at merge; F2 regex parity satisfied by any mention; F3 exemption without run; N1 in-process TMUX). Round 2 `coder-91e90cad04`, brief `fixes/t1-round2.md`: `tests/__init__.py` scrubs session keys at package import (closes subprocess and in-process routes), runtime parity replaces regex + EXEMPT. Main ruling on criterion 5: identical baseline-listed failures in both modes are accepted.

## 2026-11-08: RTK eval (H14) verdict: do not adopt
Results are in `tmux-pi-wt-rtk-eval/docs/plans/rtk-eval/results.md` (branch rtk-eval, commit f846a00, not merged). There were 28 Sonnet runs, 4 of them discarded. The rest of the 71 runs were on the local Qwen via Pi.
1. **Fidelity fails.** 19 of 98 replayed commands lost information: diffs lost hunks, grep capped matches per file at 25, git log subjects were cut, and ls/find/branch -a hid entries.
2. **Guards fail.**
   - Redaction misses secrets that RTK cuts mid-line; a dummy key leaked live on Pi.
   - `recall.db` keeps raw output for 30 days.
   - `rtk run`/`rtk bypass` gets past kill-guard.
   - On Claude, RTK and the redact hook overwrite each other's rewrite.
3. **Accuracy fails.** The Pi git-log task fell from 5/5 to 1/5. Claude needed extra turns.
4. **Savings fail.** Median billed input went up: Claude +2.2%, Pi +14.6%. RTK could only touch 13–18% of input.
- Side finding: under macOS `/usr/bin/python3` (3.9) the trust guard denies every glob. This may explain part of the `test_trust_guard` baseline failures; check before E2.
- Open owner questions:
  - Remove `~/.local/rtk-eval` and RTK's local databases, which still hold raw output from these runs including dummy secrets?
  - Keep or remove the rtk-eval worktree and branch?

## 2026-11-08 01:31Z: incident, J1 live run stopped the detprep workers
- **What happened.** The J1 coder (`coder-c28f7b48f3`) ran a live `tp-bench run --label control` from its worker pane.
  - `lib/bench/shell.py` merges the full `os.environ` into every child process, so the pane's `TMUX`/`TMUX_PANE` reached `tp-exit`.
  - `tp_session` in `lib/tp.sh` checks `TMUX` before `TP_SESSION`, so `tp-exit` and `tp-reset` targeted **detprep** instead of `bench-control-1`.
  - `tp-reset` stopped every running detprep worker: J1 (%95) and T1 round 2 (`coder-91e90cad04`, %98).
  - The script died with its pane before `kill-session`. Otherwise the whole detprep session would have been killed, Main included.
- **Second problem.** The bench workspace's Claude worker stopped at Claude Code's "trust this folder" dialog for the temp copy, and all 4 tasks errored. Session `bench-control-1` is still open on that dialog and nothing answered it. Main's `tmux kill-session` was denied by the auto-mode classifier, so it is left for the owner.
- **Recovery.**
  - Stopped both dead records.
  - Retried both in their kept worktrees, with uncommitted work intact:
    - T1 is now `coder-cb6b537bac` (%102), restart brief only.
    - J1 is now `coder-550a178817` (%103). Its brief adds: no live runs from a worker pane; one env builder that strips the session keys from every child, with a parity test that must fail without it; a session-ownership guard before stopping; folder trust done the supported way, never by answering the dialog.
- **New finding F-SESS.** When `TMUX` is set, `tp_session` ignores an explicit `TP_SESSION`. Any tmux-pi script run from a pane with `TP_SESSION=<other>` acts on the caller's own session. The candidate rule: an explicit `TP_SESSION` wins, or the script refuses when they disagree. This touches every bin script, so it needs its own lane after T1. It is added to the fix queue.

## 2026-11-08: RTK cleanup (owner-approved)
- **Kept.** `plan.md` and `results.md` were copied to `docs/plans/rtk-eval/` on main. The raw `data/` and `scripts/` were not kept.
  - Earlier references to branch rtk-eval at commit f846a00 now point to that copy.
  - The `TP_RTK=1` hook commit (24d7bc5) is dropped with the branch.
- **Removed.**
  - worktree `tmux-pi-wt-rtk-eval`;
  - branch `rtk-eval`;
  - `~/.local/rtk-eval`, the binary and downloads;
  - `~/Library/Application Support/rtk`, holding `history.db` and `recall.db`, with raw run output including the dummy fixture values.

## 2026-11-08: J1 review, round 1: REQUEST_CHANGES
- **Tests.** Main ran all four bench modules in both modes: 45 tests, all OK. `tp-bench verify` checked 15 tasks.
- **Review findings** (`review-coder-550a178817-194749`):
  - F1: the fallback `kill-session` runs without a fresh ownership check.
  - F2: transcripts the record cites are deleted along with the project copy.
  - F3: a terminal result seen after the deadline is still scored.
  - F4: a review that times out drops out of the catch-rate denominator.
- **Round 2** (`coder-3a0d4cbc7c`, brief `j1-r2.md`) applies family rules:
  - one ownership-checked helper for every destructive tmux action;
  - every path a record names survives cleanup (artifacts are copied into the results dir);
  - the budget is checked on every observation;
  - denominators come from the specs.
- **Owed.** The owner runs `bin/tp-claude-bootstrap ~/.local/share/tp-bench/work`, then Main does the live control runs from a plain terminal.
- 2026-11-08: the owner ran `tp-claude-bootstrap` on `~/.local/share/tp-bench/work` and approved trust. Main created the folder first, because bootstrap refuses a missing dir. Running it through `!` failed: with no TTY, Claude fell into `--print` mode. Both points were sent to the J1 coder for `docs/bench.md`.

## 2026-11-08: J1 review, round 2: REQUEST_CHANGES (session-ownership family)
- **Tests.** Main ran 52 tests in both modes, all OK. `verify` checked 15 tasks.
- **Accepted.** The round-1 fixes for F1–F4.
- **New findings:**
  - F1: a failed `has-session` call is treated as "gone", which skips the stop and the copy cleanup.
  - F2: the exit codes of `git reset`/`clean` between tasks are ignored.
  - F3 (note): the orient rows are missing from the denominator table.
- **Round 3** (`coder-fe9731bcf3`, `--route rule-first`, brief `j1-r3.md`):
  - absence is proven by `list-sessions`, and any other outcome is `unknown`, so nothing is stopped or removed;
  - `Shell.run` checks the exit code by default, and every opt-out carries a reason;
  - after each reset the tree is checked positively, and if that fails the remaining tasks are recorded `not_run: reset-failed`.

## 2026-11-08: N2, owner report: `FAILING_TESTS: none` starts false investigations
- **Cause.** `failing_tests.parse` only recognises an exact inline `none`. Variants such as `none.`, `None (all pass)`, `none (nothing was run).`, `none` on the next line and `**none**` all parse as a block. The hook then tells Main to dispatch `--investigate-failure`.
- **Rule** (brief `fixes/n2-failing-tests-none.md`): a report exists only if it has a bulleted entry that names a test. The header text is a note. The worker prompt says to leave the block out when nothing fails. There are negative and positive table tests plus an end-to-end index test.
- **Dispatched** as lane failing-tests (`coder-151123d131`, Sonnet).

## 2026-11-08 ~02:15Z: status
- **J1 round 3** (`coder-fe9731bcf3`):
  - added `probe_session` (absence must be proven, otherwise `unknown`);
  - `Shell.run` raises on a non-zero exit unless the call carries `tolerate="reason"`;
  - after each reset the tree is checked, and if that fails later tasks are recorded `reset-failed`.
  - Main ran 60 tests in both modes, all OK. No `has-session` remains. Review `review-coder-fe9731bcf3-200950` is running.
- **T1 round 2** (`coder-cb6b537bac`) is complete:
  - `tests/__init__.py` scrubs the session keys;
  - runtime checks plus a mutation check (3 failures with `__init__` removed);
  - `test_docs_wiring` (6 failures) and `test_trial_session` (1 failure) fail the same in both modes, with names matching the baseline;
  - `test_retry_packet` has its baseline import error.
  - Main's own two-mode run is in progress; the review follows.
- **N2** (`coder-151123d131`) stopped on lane ownership and asked for context. That was the correct behaviour.
  - Main's ruling: lane-facts and local-model have no open step and are merged; status-cleanup is parked; T1 does not touch `test_failing_tests`, `fixtures` or `tp-spawn`.
  - Edits granted; the merge order is T1, then N2. Retried as `coder-deee9cfdd0`.
- **J1 review round 3: REQUEST_CHANGES.**
  - F1: a second failed attempt overwrites `<n>.cleanup.json` (session-ownership family);
  - F2: a failed arm `git status` is recorded as `dirty: false`;
  - F3: a test waits on `sleep(5)`.
  - Round 4 (`coder-75b8f67dfe`, rule-first): exclusive-create records numbered across every record kind; a tolerated failure feeding a record becomes null, with every site listed; tests wait on events.
- **N2 review: REQUEST_CHANGES** (entry-validation family):
  - unbulleted `test:` fields still create an entry;
  - `- test: none` and `- command: pytest` count as entries;
  - `- no.test_x fails` is dropped;
  - `- all tests passed` counts as an entry.
  - Root cause: negative-word matching. Round 2 (`coder-45e00e9e96`) removes the negative-word set. An entry now needs a bullet plus a `TEST_ID` token (a dotted unittest id, `test_x`, `TestX`, a test path, or a pytest `::` id).

## 2026-11-08 02:30Z — J1 r5 and N2 r3 dispatched

- J1 round 4 review (review-coder-75b8f67dfe-202132): REQUEST_CHANGES with one new defect. `lmstudio_context` crashes on a successful but malformed reply (family metadata-unknown). All earlier findings are fixed at their sites. The live control run is still owed by Main. Retried as coder-379e83dc48 (rule-first): every metadata reader turns any reply it does not fully understand into null, with a table of malformed shapes.
- N2 round 2 review (review-coder-45e00e9e96-202316): REQUEST_CHANGES (family entry-validation). A repeated unbulleted `test:` field starts a second entry, and a `note:` value naming a test counts as an entry. Retried as coder-22c8e8dfa7 (rule-first). New rule: only a bullet starts an entry; the id comes only from `test:` or else the bullet's first token; a second `test:` field makes the entry malformed. The end-to-end run against the base parser is owed.
- Main slip: a stray `tp-agent retry` with an empty reason was refused. No duplicate record was created (checked).

## 2026-11-08 03:05Z — HL1 phases 0–1 done; J1 r5 and N2 r3 in review

- HL1 (agent a80e35ac229990d3d): the Phase 1 gate FAILS for both hashline arms against stock Pi on Qwen.
  - Pass rates: P0 100% / P1 79% / P2 82%.
  - Silent wrong edits: 0 / 7 / 2.
  - First-try edit success fell about 31 pp; edit tokens rose 247–308%.
  - Phase 2 is not worth running on LM Studio Qwen with Pi 1.1.0.
  - Branch `hl1-eval` (104f879, b30c387, 7a4e08e), not merged; write-up in `docs/plans/hl1-eval/results.md` on that branch.
  - The isolated install `~/.local/hl1-eval/src` is still present; ask the owner about cleanup at the end.
  - Found a real bug on main: the safe_grep `.env` leak, logged as F-GREP in loop/ideas.md.
- J1 r5 coder done. Main's two-mode run: shell 8, runner 22, score 28, cli 7, all OK; verify checks 15. Under review as review-coder-379e83dc48-202806.
- N2 r3 coder done. Main's two-mode run: test_failing_tests 24 OK, test_preamble 40 OK; 11 spot shapes correct. Under review as review-coder-22c8e8dfa7-203045.

## 2026-11-08 03:35Z — J1 r6 in review, N2 r4 dispatched

- J1 r5 review: REQUEST_CHANGES. A loaded model with an unreadable context length was recorded as {} instead of null. Fixed as r6 (coder-13239f5437) with the rule "partly understood means null, never empty or partial". Main's two-mode run is all OK and verify checks 15. Under review as review-coder-13239f5437-203344.
- N2 r3 review: REQUEST_CHANGES with two more entry-validation cases (a `test_case:` label; explicit `test:` taking precedence over the bullet token). Also found that `docs/prompt-examples/07-known-failures.md:54` tells workers to write "none", which is the likely source of the owner's issue. N2 r4 (coder-2103c620cb) implements an exact line grammar, fixes that doc, sweeps for similar text, and produces a scripted before/after table. Loop signal H22 logged.

## 2026-11-08 03:55Z — J1 approved and merged into lane/integrate

- J1 r6 review (review-coder-13239f5437-203344): APPROVED_WITH_NOTES. All three notes are checks: the live run, without-change coverage for acceptance items 2–5, and the historical sweep rows; the reviewer's report.md covers that sweep.
- Committed as `f1c341d` (39 files, the exact reviewed set). Lane step j1 closed; merged into lane/integrate as `3a1bcb4`. In integrate, the four bench modules pass in both modes (8/22/28/7) and verify checks 15.
- Owed by Main: the live local-only control run (main and lane/integrate, `--repeat 2`) with before/after `tmux ls` and `git worktree list`. It is held until no worker pane is live, because the previous live run killed two coder panes.

## 2026-11-08 04:50Z — N2 r4–r6, T1 r2 in review

- N2 r4 (exact grammar plus the docs that taught "none"): REQUEST_CHANGES. The author added a continuation-line rule outside the grammar, which let `test: none` + an indented id become an entry.
- N2 r5 (continuation lines never touch ids): REQUEST_CHANGES. The parser inferred the previous field from the last dict key, so a continuation after `test:` reached `command:`. That is a correctness gap, not a false trigger.
- N2 r6 (coder-eaeccd4686, escalated to Opus): the parser is now an explicit transition table, with an exhaustive test over every block of 1–4 lines checking invariants I1–I5, and the exhaustive failure count shown on r5. Signal for H22: in a free-text parser, state must be explicit, and the test must enumerate combinations, not list cases.
- T1 r2 two-mode run done: 44 modules OK in both modes. The only failures are the baseline 6 docs_wiring and 1 trial_session, matched by name. test_orchestration is 41/41, so baseline lines 422–424 and 442 are to be removed at merge. Under review as review-coder-cb6b537bac-204849.
- T1 r2 review: REQUEST_CHANGES (family inherited-tmux). The scrub runs only when the `tests` package is imported, so `discover -s tests` and `python3 tests/test_x.py` still inherit TMUX. Reproduced on test_lane_writer. T1 r3 (coder-eead542ff6) must isolate under every runner: a runner-matrix test plus an import-first check over every test file.

## 2026-11-08 05:05Z — N2 approved (round 6)

- review-coder-eaeccd4686-210047: APPROVED_WITH_NOTES. The notes: the exhaustive test does not sample the END and BULLET_TEXT_ID kinds by name, and its table check does not compare the action column (the actions match on inspection); merge coordination with test-env. Main ruled on I5: a note closes the open entry.
- Committed in the lane as `63bb424`; lane step N2 closed. The merge into lane/integrate waits for T1 (merge order T1 then N2), because both touch tests/test_failing_tests.py.
- N2 took 6 review rounds, 5 of them in the entry-validation family. H22 records the lesson.

## 2026-11-08 03:29Z (from date -u; the 05:05Z heading above is mislabelled) — T1 r3 returned incomplete; P1 found and dispatched

- coder-eead542ff6 (T1 r3): `tests/session_env.py` scrubs on import, `tests/__init__.py` aliases it, and 108 test modules import it first. A runner-matrix test over five runners and the reviewer's lane_writer repro pass. Fail-before is shown. RESULT_STATUS incomplete: 22 modules owned by other open lanes did not get the import line. They are listed in `NOT_YET_TAKEN` in test_session_env.py, and the check fails if that list goes stale.
- The coder's `tp-agent request` for those 22 was denied nine times: "trust store guard ... could not be analysed". Main reproduced it with the hook: `lib/trust_guard.py:532` `glob.iglob(..., include_hidden=True)` raises TypeError on the system Python 3.9.6, so any call with a glob word is denied. This confirms the E2-noted 3.9 risk. About 60 baseline lines are this defect (H23).
- P1 brief `docs/plans/det-prep/p1-guard-python39.md`. Dispatched coder-256b4dd5bc (Opus, lane guard-py39, step P1, pane %130).
- T1 r3: full two-mode run under way. Main's ruling for the review: the 22 modules get their import line once their lanes merge (a follow-up step in integrate). The stale-list check enforces it.

## 2026-11-08 03:55Z — P1 round 1 reviewed, round 2 dispatched

- coder-256b4dd5bc: `_iglob` replaces `glob.iglob(include_hidden=True)`; `_report_fault` puts a single line on stderr; `lib/trust.py` `_parse_expiry` accepts `Z` (sweep row 2). The capability request to edit tests/test_trust_guard.py (lane test-env owns tests/) was granted by Main, who copied the coder's proposed file in unchanged. Main's two-mode run: trust_guard 310, sensitive_trust 192, trust_record 69, all OK. Hook spot checks under /usr/bin/python3: globs allowed, literal store paths denied. The coder maps 58 of the 60 baseline lines in these modules to passing, and all 60 with the tests applied.
- Main slip: answering the request by message made the worker's "ack" overwrite result.md; restored by asking again (H25).
- review-coder-256b4dd5bc-215052: REQUEST_CHANGES F1. The glob budget counts matches, not entries visited, as the brief's wording asked. Main's ruling (in the r2 brief, for the reviewer to judge): a glob that matches nothing cannot name the store; the deadline, now checked per entry, bounds the walk; the budget counts matches, as it did on 3.11. Owed check: the intermittent test_sensitive_trust `git grep` subtests. Main is running the unmodified integrate tree 3× per mode.
- Round 2: coder-ff55f5cdc7 (Opus, pane %132), brief p1-r2.md.

## 2026-11-08 04:03Z — P1 round 2 in review

- coder-ff55f5cdc7: comments state the rule (the budget counts matches; the deadline is checked per visited entry) and one test row pins a nonmatching glob as allowed while time remains. A mutation that spends the budget per entry fails it. Main's two-mode run: trust_guard 310 OK and OK, trust_record 69 OK and OK, sensitive_trust OK outside and 2 git grep subtests inside.
- Owed check done: test_sensitive_trust on unmodified integrate 3x per mode. The git grep subtests failed in runs 2 and 3 in both modes, so they predate P1 (F-FLAKE). Baseline line 446 (test_rg_is_taken...) fails on every base run and passes with P1.
- Review: review-coder-ff55f5cdc7-220259.

## 2026-11-08 04:08Z — P1 approved and merged into lane/integrate

- review-coder-ff55f5cdc7-220259: APPROVED, no findings. It accepts Main's ruling on F1 and the base evidence for the git grep flake.
- Committed in the lane as b4dc3cd (3 files); lane step P1 closed; merged into integrate as 0cb5400. In integrate, both modes: trust_guard 310, sensitive_trust 192, trust_record 69, all OK.
- Still owed: the baseline lines P1 fixes (about 60, test_trust_guard, test_trust_record 425, test_sensitive_trust 446). Main removes them after T1 merges, because test-env owns the baseline file.
- The live hook runs from the main checkout's bin/, so workers keep hitting the 3.9 denial until P1 reaches main. That needs the owner.
- 04:09Z F-GREP dispatched: brief fg-safe-grep-source.md, coder-51390d04c9 (Opus, lane safe-source, step FGREP, pane %134). Main granted the tests/ paths up front, given test-env's claim on tests/, to avoid P1's blocked request.

## 2026-11-08 04:19Z — P1 on main (owner request)

- Owner: "merge P1 to main so the live hook is fixed". Cherry-picked the P1 commit b4dc3cd onto main as 8eaa6f2 (-x). It is not a merge of lane/guard-py39, which would also bring the 19 integrate commits. The three files were identical on main and at P1's base 3a1bcb4, so it applied clean.
- On main, both modes: trust_guard 310, sensitive_trust 184, trust_record 69, all OK. The live hook bin/tp-trust-guard under /usr/bin/python3 allows `ls tests/test_*.py` and the T1 r3 request shape, and still denies a literal store path. Not pushed.
- When integrate later merges to main, git sees the same change on both sides (no conflict expected).

## 2026-11-08 04:40Z–05:05Z — F-GREP and T1 r3 to review

- F-GREP coder-51390d04c9 complete. Main's run of the 9 modules on the uncommitted tree, both modes, all OK (Pi tests with PI_CODING_AGENT_PACKAGE). Out-of-touch files flagged: agent/extensions/sensitive.ts, bin/tp-redact, lib/redact.py. Review review-coder-51390d04c9-223858 (gpt-6-sol).
- The F-GREP reviewer hit "Authorize sensitive read?" on a probe that builds a temp `.env`. Main pressed Escape, ran the probe itself and sent the output: body redacted, a token in the path is shown (the coder's listed "path parts" risk). The reviewer had already moved to a non-.env path.
- T1 r3 coder-eead542ff6 two-mode run of 118 modules done: 482 distinct failing names, 0 outside the baseline once the `tests.` prefix is dropped. The 4 test_orchestration lines pass. test_trial_shadow_end_path is intermittent in both modes (Main reran 3x per mode: one outside failure), baseline-listed. Four modules (test_ctx_calls, test_ctx_surface, test_ctx_tools_wiring, test_localdocs) crash at import on Python 3.9 (PY39-UNION). Main's ruling on the 22 NOT_YET_TAKEN modules: follow-up after their lanes merge. Review review-coder-eead542ff6-225801.
- review-coder-51390d04c9-223858 (F-GREP r1): REQUEST_CHANGES. F1 path text shown unredacted (source-path-redaction); F2 path resolved twice, symlink swap between search and redaction (source-identity-race). Round 2 coder-0836e64d32, brief: resolve once before the read, hand tp-redact the resolved path, withhold on change, redact path text.
- review-coder-eead542ff6-225801 (T1 r3): REQUEST_CHANGES. F1 filtered discover of an other-lane module inherits the session (inherited-tmux, repeat of r2 F1); F2 import-first check tests first import, not first statement. Main withdrew the 22-module follow-up ruling and granted the one-line import now. The recurrence guard required a route; round 4 coder-c51414607f with --route rule-first (every module scrubs itself; AST first-statement check; matrix over all modules).
- F-GREP r2 coder-0836e64d32: Main's 9-module two-mode run all OK. Review review-coder-0836e64d32-232548: REQUEST_CHANGES F1 (round-2 narrowing left secret-shaped path segments in additionalContext) plus an owed before-fix reconstruction. r2 F2 (identity race) fixed. Round 3 coder-b59d089c64 --route rule-first: PostRedactor.shown() is the one source of displayed path text.
- F-GREP r3: coder asked about Glob/Grep `filenames`; Main ruled no for this lane (F-PATHNAMES, owner decision), answered by message and had the full report re-sent (H25). Main's two-mode run all OK (test_safe_source_redaction 10). Review review-coder-b59d089c64-234251.
- T1 r4 coder-c51414607f complete: 22 one-line imports, AST first-statement check (18 rows), filtered-discover matrix over every module. Main's two-mode run over all changed modules started.
- F-GREP r3 review review-coder-b59d089c64-234251: REQUEST_CHANGES F1 (plain-text heads with ':' or >300 chars fall to ordinary rules); F2 filenames = note under F-PATHNAMES. Round 4 coder-4a28b45a70 --route design: unsourced plain-text lines fail closed to STRICTEST_RULES; heads split at every ':'/'-'. Main granted and applied the coder's docs patch. Main's 9-module two-mode run all OK (test_safe_source_redaction 11).
- F-GREP r4 review review-coder-4a28b45a70-000515: REQUEST_CHANGES F1 (head accepted without a line number), F2 (`git -C . grep` not detected). Earlier rounds confirmed fixed. Round 5 coder-5365a5da2b --route design: heads need a line-number field; command detection by shlex parsing with wrappers and git global options, unparseable commands count as search. Main's rule: another site in this family after round 5 parks the lane for the owner (H26 candidate: a detector over display or command text needs a fail-closed default from round 1, not per-site fixes).
- F-GREP r5 coder-5365a5da2b: Main's 9-module two-mode run all OK (test_safe_source_redaction 14). Review review-coder-5365a5da2b-002800: REQUEST_CHANGES F1, the wrapper operand cap at 64 fails open (6th site in source-path-redaction). Per Main's pre-set rule the lane is PARKED for the owner (tp-agent retry --route park; docs/plans/det-prep/fg-parked.md). Recommendation: one small round (any parser limit → treat as search). All earlier findings confirmed fixed. Work is uncommitted in tmux-pi-wt-safe-source.

## 2026-11-08 U1 (PY39-UNION) dispatched
- Brief `u1-py39-union.md` (eac6300). Lane `py39-union`, step U1, base lane/integrate; coder-62fe70c08d (claude, level mid -> claude-sonnet-5-5).
- T1 round 4 two-mode run still going when U1 started; no overlap (U1 touches lib/, T1 touches tests/ only, except the new tests/test_lib_imports_py39.py).
- U1 coder-62fe70c08d PARKED after two auto-mode prompts (first: a long chain reverting lib/ for the before-fix run, redirected to a git-archive export; second: `mkdir -p $TP_TASK_DIR/base`). Both say a hook changed the call after auto mode reviewed it: the H10 rewrite (lib/redact_hook.py:27 SUFFIX). Escape both times, never approved; task stopped.
  State left: tmux-pi-wt-py39-union has `from __future__ import annotations` in lib/ctxengine/{calls,surface}.py and lib/localdocs/{calls,lock}.py plus new tests/test_lib_imports_py39.py, uncommitted. Task dir has before.log (9 FAIL lines, TypeError on `|`), sweep.txt; after.log empty. No before/after test run, no review.
  H10 raised to high: it blocks Claude workers in auto mode. Owner decision on the H10 fix (stop rewriting, or exempt workers) before more Claude coders are dispatched.

## 2026-11-08 T1 merged
- Round-4 review review-coder-c51414607f-011734 (gpt-6-sol): APPROVED_WITH_NOTES. The only owed item was Main's baseline edit.
- Main's two-mode run: 140 modules x 2 = 280 runs; 246 OK, 24 FAILED, 10 no result (U1 imports, test_retry_packet). 481 distinct failing names, 0 off-baseline. Evidence in scratch evidence-t1-r4.txt.
- Committed 4d2108d in the lane, `lane close test-env t1` recorded, merged into integrate 155ae8d.
- Baseline: removed exactly the 4 test_orchestration lines (old 422-424, 442); 492 -> 488 lines.

## 2026-11-08 Owner decisions (morning)
- H10: stop rewriting Bash; failing output handled in PostToolUse only. Done as a normal lane. It goes first, because the rewrite blocks Claude workers. Author: Pi worker (local Qwen if it can, else OpenAI). Review: a different provider.
- F-GREP: one small round. Fail closed when any parser limit is reached; add 65 and 10,000 assignment rows. After H10.
- U1: wait for H10, then retry on Claude.
- Pushed main 2e0e19c..d8a18a7 (docs only). Deleted ~/.local/hl1-eval (360M, nothing running from it).
- Merge integrate->main approved. Main merges after the P1 baseline check and N2, shows the diff summary, and does not push that merge without asking.

## 2026-11-08 P1 baseline check
- Integrate 155ae8d (P1 + T1): the 14 baseline modules, run twice in both modes (4 runs per module).
- 60 baseline lines passed in all 4 runs: 58 test_trust_guard, test_trust_record.test_expiry, and the test_sensitive_trust rg lift. Removed 59; kept the rg lift because it is intermittent in T1's runs (F-FLAKE). Baseline went from 488 to 429 lines.
- Off-baseline: 2 test_sensitive_trust UnscopedPatchTests_unrelated `git grep` lift subtests failed in 1 of 4 runs (inside, second pass). Same intermittent family. Logged under F-FLAKE, not added to the baseline.
- H10 re-decided with the owner: dropping the suffix reopens design row 8 (PostToolUseFailure still cannot replace output, per the current hooks docs). The owner chose to investigate first: why do workers get the auto-mode denial when the docs exempt plugin command hooks that change Bash?

## 2026-11-08 N2 merged into integrate
- 68973cb merges lane/failing-tests 63bb424. No conflict: git combined T1's first-line `import session_env` in tests/test_failing_tests.py with N2's changes (still line 4, first import).
- Both modes: test_failing_tests 25 OK, test_preamble 40 OK. test_docs_wiring has 6 failures, the same as the baseline.
- test_session_env: 3 failures on the merged tree, which also happen on a git-archive export of 155ae8d (T1 merge, before N2). So they come from T1 and J1 together, not from N2. T1's lane branched before J1 (f1c341d) landed, so T1's guards never saw tests/test_bench_{cli,runner,score,shell}.py (no `import session_env` first) or the 7 TP_BENCH_* names that lib/bin read (not classified). Follow-up T1b: a tests-only fix. It is not on the baseline because it is a real regression on integrate.

## 2026-11-08 H10 repros (Main, Sonnet 5.5, --permission-mode auto, tmux session h10)
- A, redaction on, normal tp-claude: `pwd` (allow-listed) denied, "auto mode's check had no usable answer"; `mkdir -p` ran.
- B, redaction off: both ran. The rewrite is the trigger.
- C, redact hook moved from the plugin into --settings: `pwd` still denied: "The server-side auto mode classifier gave no verdict for Bash: a hook changed this call's input after auto mode reviewed it, so the review does not cover what would run." The docs' exemption for command hooks in settings did not hold for us.
- D, A without the inherited CLAUDE_* env: same denial.
- E, order swapped (mkdir, pwd, git status): mkdir ran; pwd and git status (both allow-listed) denied. So the denials follow the allow rules, not the call order: the rewritten `X || echo exit-code: nonzero` no longer fully matches the allow rule for X.
- F, claude/settings.json plus allow rule `Bash(echo exit-code: nonzero)`: pwd, git status, mkdir ran; a second trial (pwd, ls config, git log, wc -l) all ran. A call that still needs the classifier (`ls /nonexistent-h10-dir`, a path outside the project) was denied, twice, while `ls config` beside it ran.
- G, the model sends the wrapped form itself (`ls /nonexistent-h10-dir || echo exit-code: nonzero`): ran. The hook leaves an already-wrapped command alone (no updatedInput), so the classifier reviewed exactly what ran. The output arrived through PostToolUse (exit 0), so redaction still covers it.
- Fix proposal, keeping the suffix and so the no-leak property of design row 8: (1) add `Bash(echo exit-code: nonzero)` to claude/settings.json allow; (2) tell Claude workers and Main in the preamble to append ` || echo exit-code: nonzero` to Bash commands themselves, so the hook rewrites nothing and the classifier sees the real command; the hook rewrite stays as the fallback. Open: whether to deny-with-instruction instead of rewrite for calls that need the classifier.
- Cleanup: tmux session h10 killed; .tmux-pi/h10-A..F removed. Launchers in scratch h10c/, h10f/.

## 2026-11-08 T1b and M2 unblocked
- Both gpt-6.1-sol coders returned blocked: closed lanes test-env (claimed all of tests/) and bench still owned their paths. Both lanes' tips are in lane/integrate; collected their finished reviewers, then `lane remove test-env` and `lane remove bench` (archived; restore: `git branch lane/test-env 4d2108d`, `git branch lane/bench f1c341d`). Messaged both coders: ownership granted, two-mode run defined.

## 2026-11-08 H10 fixed and merged into integrate
- Lane h10-fix: coder-f2f7dc5d9d (gpt-6.1-sol) wrote 16 tests first (failed before), then: allow `Bash(echo exit-code: nonzero)`; bin/tp-claude merges caller --append-system-prompt / -file values into one inline prompt plus a standing instruction to append the suffix (wrap()'s exclusions spelled out); golden_argv claude entries; design-doc paragraph. No lib/ changes (py39-union owns lib/).
- Escaped one worker prompt (authorize `git diff` patch output) and redirected; no second prompt.
- Main: git diff --check clean; both modes: test_tp_claude_suffix 16 OK, test_claude_launcher_settings 8 OK, test_settings_template 2 OK, test_claude_redact_hook 16 OK, test_redact 13 OK, test_docs_wiring 6 baseline failures.
- Review review-coder-f2f7dc5d9d-031022 (Sonnet 5.5): APPROVED_WITH_NOTES, no defects; notes: instruction text must track wrap() if it changes (F3).
- Committed df63b5e, `lane close h10-fix h1`, merged into integrate 908b535. Integrate: same results; test_session_env 3 failures (the T1b gap).
- Live check from integrate's bin/tp-claude, Sonnet 5.5 auto mode: pwd, git status, `ls /nonexistent-h10-dir` (denied in every earlier repro) and `ls /tmp/claude-501` all ran; Sonnet appended the suffix itself; redaction still masked 6 lines.
- Lanes removed after integration (stale path claims blocked T1b, M2, H10): test-env, bench, sensitive-grep, failing-tests (restore commands in their archives).
- Local config: my 6.1 sed listed gpt-6.1-sol at mid and strong; router_catalog refuses duplicates, which broke review dispatch. Local mid restored to gpt-6-sol; M2 must settle it.

## 2026-11-08 M2 and T1b merged into integrate
- M2 (lane gpt61, coder-0fef98c95d, gpt-6.1-sol): gpt-6.1-sol on mid [low, medium] and strong [high]; validate_catalog allows one model at several levels when provider/worker/context_window/max_output agree and efforts are disjoint and ascending; level_of/provider_of take an optional effort; _local_rule compares (model, effort); escalate moves up a level when the model has no higher effort. coding/complex routes use gpt-6.1-sol. Fail-before in the task dir (HEAD validator "listed twice"; HEAD level_of rejects an effort).
- The worker's 60s cap timed out test_model_routing (69s full file); it split by class. Its capability request (run both modes) answered by Main's runs: router_catalog 24, routing_evals 29, route_resolve 80, llm_router 45, model_routing 75, all OK in both modes.
- Review review-coder-0fef98c95d-073102 (Sonnet 5.5): APPROVED_WITH_NOTES, no defects. F1 (Closes by: decision): strong is shown:false and its entry in_roster:false, so the complex route (6.1@high) is inert: explicit 6.1@high is refused and the local-rule redirect is dropped; before, gpt-6-sol@high ran on mid. F2: 6.1 on mid now refuses off/minimal/high/xhigh/max (as specified).
- Committed c920585, `lane close gpt61 M2`, merged into integrate 2b9d1e1.
- T1b (lane test-env-bench, coder-7ca71a3733, gpt-6.1-sol): the four bench test modules import session_env first; CONFIG lists TP_BENCH_{ANSWERS,HOME,PROJECT,RESULTS,WORK}, TP_HL1, TP_X1_ARM (path overrides and arm gates; readers checked by Main). Before: test_session_env 3 failures both modes; after OK.
- Review review-coder-7ca71a3733-073229 (Sonnet 5.5): APPROVED, no notes. Committed 8c143f6, `lane close test-env-bench T1b`, merged into integrate d496bdd.
- Integrate d496bdd, both modes: the five routing modules, test_tp_claude_suffix 16, test_claude_launcher_settings 8, test_session_env 13 (1 skip), bench cli/runner/score/shell 7/22/28/8: all OK. The test_session_env T1 gap is closed.
- F-GREP r6 (coder-4d766446d8) asked for lib/redact_hook.py (py39-union claims lib/) and three safe-source test files (gpt61 claims tests): Main authorized the cap logic only (U1 touches only import headers) and those three test files.
- Full suite on d496bdd, both modes, started for the integrate->main gate.

## 2026-11-08 F-GREP round 6 and merge into integrate
- coder-4d766446d8 (gpt-6-sol): the wrapper operand cap returns "search" past 64 operands instead of slicing; 65/10,000-assignment rows on Claude post_result and Pi tp-redact --command, plus a direct cap/depth test. Fail-before: 2 tests, 7 failures.
- Worker hit a second permission prompt (rg over its own after.log read as a recursive sensitive search): escaped, task parked, worker wrote its report without running more.
- Main's nine-module both-mode run: all OK except the new test's "under the limit" row (63 assignments = 65 operands, an off-by-one in the test, not the fix). Main changed it to 62 and added 63 as a fail-closed row; module OK both modes.
- Review review-coder-4d766446d8-074323 (Sonnet 5.5): APPROVED_WITH_NOTES, no defects. Round-5 F1 fixed; depth limit, shlex failure and UNREADABLE all fail closed; no other cap in the detector. N1 (later): wrappers missing from WRAPPERS (setsid, busybox, ssh, script -c, flock) are not detected. N2: >64-operand non-search wrapper commands are over-redacted (accepted).
- Committed 2a1e56f (rounds 1-6), `lane close safe-source FGREP`, merged into integrate 421b4c1 with no conflict. test_session_env then caught test_safe_source_redaction (written before T1) without the scrub import; Main added it on integrate df8753e.
- Integrate df8753e, both modes: test_session_env 13, the nine F-GREP modules, test_tp_claude_suffix 16: all OK. test_docs_wiring 6 failures = baseline.

## 2026-11-08 Full-suite gate before integrate->main: M2 regression found
- Main ran all 136 test modules in both modes on a detached copy of integrate d496bdd (M2 + T1b + H10), 3 in parallel, 900s bound per module. 448 distinct failing names; 387 outside the baseline in 20 modules.
- Cause of all but the F-FLAKE pair: fixtures dispatch through the committed catalog with `openai/gpt-6-sol` (tests/lane_fixture.py:18 and others), which M2 removed. M2's worker and review covered only the five routing modules. The integrate->main merge is held.
- F-FLAKE again: test_tracked_credential_names_lift_only_as_committed in test_sensitive_read and test_sensitive_trust (outside mode only).
- test_ctx_nav_api hit the 900s bound in both modes (its 358 baseline names unconfirmed this run; likely slower under 3-way parallelism).
- Follow-up M2b (docs/plans/det-prep/m2b-fixtures.md, list m2b-off-baseline.txt): lane gpt61, coder-f6352fbb8b. Main pre-authorized model-id/effort literals in files other lanes own.

## 2026-11-08 M2b merged into integrate; off-baseline modules clean
- M2b (lane gpt61, coder-f6352fbb8b, gpt-6.1-sol): fixture/example model ids gpt-6-sol -> gpt-6.1-sol (tests/lane_fixture.py, local MODEL constants in main_notify/orchestration/session_resolution, t9 dispatch, contract escalation, router_floor SOL; README, subagents.ts description, settings.example.json). Worker ended incomplete with three leftovers; Main classified them: lane_recovery hand-over (baseline-failing.txt:378) and trial_session shipped config (:426) are baseline.
- The third was real: router_floor `test_strong_available_and_unavailable...` (anthropic, unavailable). Health is keyed provider:model and strong's model is mid's gpt-6.1-sol at high, so a strong outage also removes mid's only other-provider reviewer; stop_and_ask is the correct answer. Main changed that row with a comment. Consequence of M2's shared model, worth knowing for the strong decision (M2 F1).
- Main's both-mode run of the 19 named modules: all OK except the two baseline names. Remaining gpt-6-sol ids are synthetic or absence assertions, left on purpose.
- Review review-coder-f6352fbb8b-085811 (Sonnet 5.5): APPROVED_WITH_NOTES, no defects (N1 two SOL constants could drift; N3 synthetic ids). Committed 5ac69b9, `lane close gpt61 M2b`, merged into integrate 46704c9.
- Re-run of the gate's 20 off-baseline modules on 46704c9, both modes, 4 in parallel: all OK except lane_recovery's baseline name. F-FLAKE did not recur. test_ctx_nav_api again hit 900s (dots almost all E, as in the baseline); a solo run with a 2400s bound checks its names.

## 2026-11-08 F-FLAKE fixed; integrate merged into main (local, not pushed)
- test_ctx_nav_api, solo with a 2400s bound on 46704c9: ~955s per mode, 441 tests, 358 failing = the 358 baseline names, none new. The 900s timeouts were only the bound.
- Trial merge of main + 46704c9 (merge-tree: no conflicts; main had touched lib/trust.py, lib/trust_guard.py, tests/test_trust_guard.py): trust/sensitive/routing/session_env modules OK both modes except F-FLAKE.
- F-FLAKE root cause: K1's test_tracked_credential_names_lift_only_as_committed (test_sensitive_read, also run by test_sensitive_trust) made a same-size, same-mtime edit to agent/mcp.json and "restored" it with `git checkout --`. When the index entry is not racily clean, git trusts the matching stat and leaves the edit on disk; K1 then rightly keeps the prompt and judge(False) fails. Instrumented runs: every failure at that step had `bar\n` on disk. main (without K1) 12/12 clean; integrate 5/12 failing runs.
- Fix (Main, integrate a0aa3e6, test only): write the committed bytes back and assert them. 40/40 isolated, 3x both modes of both modules OK. Not independently reviewed: tp-review takes only a worker task id; the change is four test lines with the mechanism shown above.
- Main's local config edits (openai-codex/ prefix) saved to the job dir, reset, `git merge --no-ff a0aa3e6` -> main 259f87b (228 files, +9456/-625), then the same prefix substitution re-applied (mid now openai-codex/gpt-6.1-sol; validate_catalog passes).
- 259f87b in a clean worktree, both modes: session_env, the five routing modules, router_floor, contract, docs_wiring (6 = baseline), sensitive_read, trust_guard, tp_claude_suffix, claude_launcher_settings, safe_source_redaction, lane_writer, orchestration: no failure outside the baseline. Not pushed.

## 2026-11-08 U1 (Python 3.9 imports) merged into main
- coder-b41c9f8728 (Claude Sonnet 5.5, lane py39-union): `from __future__ import annotations` in lib/ctxengine/calls.py, surface.py, lib/localdocs/calls.py, lock.py; new tests/test_lib_imports_py39.py imports every lib module; 59 now-passing names removed from baseline-failing.txt.
- Main's both-mode runs on the lane: the ctx/docs modules all OK; test_ctx_nav_api 358 failing = baseline, none of the 59 removed names fails, nothing outside the baseline. Logs kept in tasks/coder-b41c9f8728/main-evidence/ (review F2).
- First review dispatch refused (packet ~825k > 300k): the lane record's base was still 0cb5400 (lane/integrate) after Main fast-forwarded the lane to 75b5610, and tp-agent has no lane rebase. Main backed up lane.json and set base to main 75b5610 under the lane lock (load_lane validation passes: base is an ancestor of the tip). Tool gap: a supported `lane rebase` (or base refresh on fast-forward).
- Review review-coder-b41c9f8728-103457 (openai-codex/gpt-6.1-sol): APPROVED_WITH_NOTES, no defects; ran the probe on 3.9.6 and 3.14.5, 614 affected tests pass. F1 (author missed 3.14.5) discharged by the reviewer; F2 logs preserved as above.
- Committed 8fff34e, `lane close py39-union U1`, merged into main 3b71013 (tree identical to the reviewed tip). Not pushed.
- U2 found: 347 of nav_api's 358 failures on 3.9 are `shutil.rmtree(name, dir_fd=fd)` (lib/ctxengine/graph.py:1409; dir_fd is 3.11+), in the link-safe remove_own_folder. Other 11: NotADirectoryError on scratch, assertNoLogs (3.10+), one empty open-list assertion.

## 2026-11-08 U2 (rmtree dir_fd on 3.9) merged into main; U3 found
- First worker coder-5340bf74b1 (gpt-6.1-sol) stopped correctly: all four paths were still claimed by the finished lanes py39-union and gpt61. Main checked both merged into main and clean, collected U1's finished review process, and ran `tp-agent lane remove` on both (archived, restore commands printed). Eleven more merged lanes are still open with claims (evidence, h10-fix, safe-source, ...): same blocker for later work.
- Retry coder-e0e9e46dea (router picked Claude Sonnet 5.5): below 3.11 remove_own_folder uses a descriptor-relative _rmtree_at (O_NOFOLLOW per level, links unlinked never followed, depth 64); 3.11+ keeps shutil.rmtree(dir_fd=). New tests/test_remove_own_folder.py (3 of 4 cases fail before); test_ctx_nav_api no_logs helper for 3.9; 355 baseline lines removed.
- Main's runs (459 tests: remove_own_folder, ctx_nav_api, lib_imports_py39, session_env), 3.9.6 and 3.14.5, both modes: only baseline names left (3.9: two r8 link tests + r9 deno; 3.14: r9 deno). ~107 s per run on 3.9, down from ~955 s. Logs in tasks/coder-e0e9e46dea/main-evidence/.
- Main corrected the worker: Homebrew 3.14.5 has os.O_NOFOLLOW_ANY; Apple's 3.9.6 (python3 on PATH) does not, so graph.NOFOLLOW_ANY falls back to O_NOFOLLOW there and an intermediate link is followed. Follow-up U3 (u3-nofollow-any.md): use the SDK value 0x20000000 on darwin.
- Review review-coder-e0e9e46dea-105254 (gpt-6.1-sol): APPROVED_WITH_NOTES, no defects (F1 = U3; F2 timing provenance, closed by Main's per-interpreter logs). Committed 11d38b8, `lane close py39-rmtree U2`, merged into main def18d6 (tree identical to the reviewed tip). Not pushed.

## 2026-11-08 U3 (O_NOFOLLOW_ANY on Apple's 3.9) merged into main
- coder-653e1a5433 (gpt-6.1-sol, lane nofollow-any): on darwin without os.O_NOFOLLOW_ANY, graph.NOFOLLOW_ANY = 0x20000000 (SDK sys/fcntl.h) after an import-time probe of "/" (EINVAL -> O_NOFOLLOW; other errors raised). New tests/test_nofollow_any.py; FsTracer and allowlists updated.
- Worker hit two sensitive-read prompts (recursive grep over tests/; git diff --check), both cancelled, not approved; redirected after the first, parked after the second. It ended incomplete (own nav_api run bounded at 60 s, baseline untouched).
- Main: before the fix (main f5ac3f8 + new test) 3.9.6 fails the kernel test ("followed an intermediate link"), 3.14.5 passes it. After: 829 tests, 3.9.6 and 3.14.5, both modes: only r9 (deno) fails; both r8 PathRuleCase link tests pass on 3.9. Main removed those two baseline lines. Logs in tasks/coder-653e1a5433/main-evidence/.
- Review review-coder-653e1a5433-111035 (Claude Sonnet 5.5): APPROVED_WITH_NOTES, no defects (N1 pre-macOS-11 kernel not covered, out of scope; N2 non-EINVAL probe error makes graph unimportable, fail-closed by design; N3 covered by Main's runs). All other NOFOLLOW_ANY users checked; ELOOP/ENOTDIR mappings already accept both.
- Committed on the lane, `lane close nofollow-any U3`, merged into main (tree identical to the reviewed tip). Not pushed.

## 2026-11-08 Finished lanes removed
- Main checked every open lane: merged into main, worktree clean, steps closed. Removed (`tp-agent lane remove`, records archived under .git/tmux-pi/lanes/archive/ with restore commands): evidence, guard-py39, h10-fix, lane-facts, local-model, measure, review-order, safe-source, test-env-bench, tools-template (plus py39-union, gpt61, py39-rmtree, nofollow-any earlier). Four needed their finished, uncollected reviewers collected first.
- Left open: status-cleanup (F1): no closed step, 21 uncommitted files in its worktree on base f1a0ad3. Unfinished work; owner/Main to decide whether to resume or abandon.

## 2026-11-08 J1 live local-only control run: harness clean, all tasks refused at routing
- `tp-bench run --arm main --label control --repeat 2 --tasks 'local-*' --order random`, from Main's shell with TMUX/TP_SESSION unset, answers sha256-checked, LM Studio up. Exit 0; records bench-results/control/{1,2}.json (schema tp-bench-run:v1, cleanup complete, leftover null). `tmux ls`, `git worktree list` and the bench work root identical before and after (done-means 1 and 5 hold for the harness).
- But 8/8 tasks status=error at dispatch, so local_exact=0.0 measures nothing. Not recorded in loop/results.tsv.
  - local-count-exports, local-docs-states, local-summarize-importers: "local-only task outside the qualified local shapes". `tp-route --local-only` reproduces it for all three, with role researcher or none: the local router does not tag them with a qualified shape. summarize_files is only a *candidate* shape in the catalog, so that task can never route local as written; the two count_enumerate questions lack the "run a counting command" form the shape was qualified with ("77/80 with the counting prompt").
  - local-locate-chant: run 1/2 stopped on "the mid floor for role researcher allows no level below it" (catalog roles.researcher strict_floor mid, 86f52c0, predates the bench f1c341d). `tp-route --local-only --role researcher` on the same text routed local (locate_quote) when Main reran it, so the two paths disagree or the router answer varies; not yet settled.
- Owner decision needed (J1-D1): bench local tasks should (a) dispatch with a role that has no strict mid floor, or the catalog should exempt --local-only from it; (b) be reworded to the qualified shapes' forms, or carry an explicit shape; (c) drop or replace summarize-importers until summarize_files is qualified.

## 2026-11-08 Owner decisions: push, status-cleanup abandoned, local summarizer
- Owner: "push". Main pushed main to origin (d8a18a7..dbb101c, 66 commits; config local edits not committed).
- Owner: "resume it if it is needed work and abandon it if it was left because it was determined failed". F1 status-cleanup was left out after round 9 was rejected (no round 10 by the agreed rule): abandoned. Rounds 1-9 diff saved as fixes/f1-status-cleanup-r9.patch and in stash@{0} ("lane status-cleanup: before remove"); `tp-agent lane remove status-cleanup` archived the record (restore: branch lane/status-cleanup f1a0ad3). The need (200+ status rows) stays open as an idea, not as this design.
- Owner: "can't we make a summarizer for local models so it can work". Brief ls1-local-summarizer.md: 24 chores, deterministic checker, runner; Main runs the live qualification (S2); summarize_files flips to qualified only at >= 95% holdout with no fabricated citation.

## 2026-11-08 Release gates added (owner-approved)
- Owner asked what it takes to be confident the changes improve production use without a new failure. Gates G1-G5 in
  docs/plans/det-prep/release-gates.md. Owner: "yes add the gates and start G1 after the merges".
- FG-N1 round 2 in review (review-coder-50e09c1158-143214); LS1 round 3 worker (coder-2d4df36f35); M2-F1 review
  (review-coder-76b4f649f5-135407). G1 runs on main after these merge.

## 2026-11-08 G2 raised to zero failures (owner)
- Owner: "I want all failures fixed in the end so they should be determined and fixed". G2 now means
  baseline-failing.txt goes to empty with each cause found and fixed; no skips or "explained" entries.
- Plan: G1 first (it fixes the full failing list on merged main), then G2 lanes grouped by cause.

## 2026-11-08 ~14:00-18:30 CST: four hours lost to broken watchers; three lanes redispatched
- Main's watcher loops were written for bash but ran in zsh (no word splitting), so they never fired. Fixed with a
  bash script that checks its arguments and exits if a pane disappears.
- LS1: the "round 3" retry (coder-2d4df36f35) actually ran round 2 again. Main passed `--reason` as a relative
  path; tp-agent makes a brief the instruction only when the path is absolute (recorded as a note otherwise). Re-sent
  with the absolute path: coder-ed25d9ee58.
- FG-N1 round 2 review (review-coder-50e09c1158-143214): REQUEST_CHANGES, families stacked-wrapper-strings and
  remote-command-uncertainty (option values hide stacked strings; nested sh -c loses remote uncertainty).
  tp-agent refused a plain retry (second REQUEST_CHANGES); round 3 sent as `--route design`
  (fixes/fg-n1-round3.md: a table per wrapper, fail closed on unknown options, flag kept through nesting, generated
  test): coder-b9c961eade.
- M2-F1 review: second credential-directory prompt (`config/`), so it was parked as the rule says. Re-dispatched with an
  evidence note to read config files by exact path and never search config/ or agent/ recursively:
  review-coder-76b4f649f5-182739.

## 2026-11-08 M2-F1 (strong shown) merged into main
- coder-76b4f649f5 (lane strong-shown): strong `shown: true`, gpt-6.1-sol `in_roster: true` at efforts ["high"]. The
  router prompt now offers 5 levels (4 = strong, 5 = top); a top author's reviewer walks down to gpt-6.1-sol at
  strong/high. tests/route_fixture.py defaults strong_shown=False (scope deviation, accepted by review).
- Main's four-mode run (10 modules): 433 OK in each. Review review-coder-76b4f649f5-182739 (gpt-6.1-sol):
  APPROVED_WITH_NOTES, no defects. F1 note: test_model_routing enables strong before its redirect test instead of
  using the shipped flags (other tests pin them). F2 owed: the worker reported aggregate durations only; per-file
  timings were not collected (acknowledged).
- Committed 4e01e1f, `lane close strong-shown F1`, merged 2798974. Main's local openai-codex/ prefix edits saved,
  reverted, re-applied after the merge (one conflict on the strong row, resolved to openai-codex/gpt-6.1-sol with
  in_roster true); validate_catalog passes. Lane removed.
- With strong in the roster, a strong outage also takes out mid's gpt-6.1-sol (health keyed provider:model).

## 2026-11-08 LS1 round 3 done; Main found unsupported-citation gap, round 4 sent before review
- coder-ed25d9ee58: heading file resolution (one allowed file, whole name), all 24 answers regenerated with a hand-written
  reference per chore and every-fact-needed tests, order check for steps-states/steps-deploy. 128 tests OK in 4 modes.
- Main's check: use-jobqueue's reference with quotes stripped and all six line numbers changed to wrong in-range lines
  still passes (no bad citations). The worker had disclosed it. S2's "zero fabricated citations" bar needs this, so
  round 4 (fixes/ls1-round4.md: per-fact `support` regex on the cited lines, range limit 5) went out before a
  review: coder-636cd4b784.

## 2026-11-08 ~19:10 CST: G2 started in parallel with the FG-N1/LS1 reviews
- Owner asked whether failing tests can be worked in parallel in other areas. Yes: G2 lanes touching areas FG-N1 and
  LS1 do not.
- Main reproduced every baseline name on a clean detached checkout of 1b1e9fb (each test alone, keys unset, 3.9 and 3.14).
  Causes: test_retry_packet f-string backslash (3.9 SyntaxError, test-only); run_bounded passes unittest `--durations`
  (3.12+); trial_record deep-JSON RecursionError escapes `routing_evals._read_json` on 3.9; lane cap tests expect 16
  but owner's 899551c set 20; ctx_nav_api r9 needs Deno (not installed); sensitive_trust rg lift,
  trial_shadow_branches HEAD, trial_shadow_end_path inconclusive and lane_recovery hand-over pass alone.
- FG-N1 r3 evidence (734 tests x4 OK; 26-command false-positive probe identical to main) -> review-coder-b9c961eade-185926.
- LS1 r4 evidence (159 tests x4 OK; Main probe: 0/144 shifted-line replies pass, 24/24 right-line pass) ->
  review-coder-636cd4b784-185851.
- Dispatched G2-A lane g2-py39 (coder-be0acf43be, brief g2/g2a-py39-compat.md) and G2-B lane g2-lane-cap
  (coder-ff2c8ee9c3, brief g2/g2b-lane-cap.md).
- Held: sensitive_trust rg lift (FG-N1's area, after merge); ctx_nav_api r9 (owner decision on Deno); the
  order-dependent group (whole-module repeat runs in progress).
- ~19:20 CST: G2-A/G2-B first dispatch started from stale lane/integrate (a0aa3e6; Main omitted --base main).
  Stopped, collected, lanes abandoned (G2-A's 3 changed files stashed in its worktree, then removed). Briefs updated:
  base main; no lane edits baseline-failing.txt (Main removes the lines at merge). Re-dispatched with --base main:
  G2-A g2-py39 coder-f32fa5e4d8, G2-B g2-lane-cap coder-f7f5d0d555, G2-C g2-trial-runid coder-d2f1cdad52 (default
  trial run id stamped to the second collides; branches whole-module fails 2/2 on 3.14), G2-D g2-handover
  coder-c8a5cd505f (lane_recovery whole-module fails 4/4: a second writer without --lane-replaces is not refused).
  trial_shadow_end_path passed 4/4 whole-module; folded into G2-C to find its cause.
- LS1 r4 review (review-coder-636cd4b784-185851): REQUEST_CHANGES F1 completeness (signature-only passes), F2 unknown
  extension heading, F3 body/docstring citations rejected, F4 table separator. Main's probe confirmed by the reviewer
  (24/24, 0/144). Round 5 by route design (fixes/ls1-round5.md): support as line sets with anchors, behaviour regexes
  matched with citations removed, any filename-like heading sets context. coder-10744b0248.
- FG-N1 r3 review (review-coder-b9c961eade-185926): REQUEST_CHANGES F1 sudo -h / script -F arity, F2 env -S drops
  trailing args, F3 five remote-uncertainty shapes, F4 generated product sampled (1,296 of 46,656 triples). Round 4
  by route design (fixes/fg-n1-round4.md): bag-of-words floor over wrapper tails; uncertain handed-on text is
  sensitive; 41-command false-positive check; full product at depth 1-2, triples with option none. coder-8b20c9cf68.
- G2-B merged (838589b): review-coder-f7f5d0d555-190752 APPROVED_WITH_NOTES (owed F1 reporting gap closed by Main
  in main-evidence). baseline-failing.txt 13 -> 11 lines.
- Owner asked why Deno at all: tmux-pi runs on Python; Deno is only used by the context engine's navigation of a
  Deno/TypeScript target project (nav_api: `deno doc`/`deno info`) and by pre-commit-gate's `deno check` when the
  target has deno.json. Without Deno those answer `unavailable`; importers/imports still work.

## 2026-11-08 — Deno decision; G2-E dispatched
- Owner: "just do not require it on machines not using it." Deno is used only when the target project has deno.json/deno.lock,
  to improve checking the work projects; tmux-pi never needs Deno to run or to pass its own suite. Product code already
  works this way (pre-commit-gate, lane-info, lane_deps, localdocs, ctxengine all trigger on the project's Deno files).
- The one breach is a test: ctx_nav_api r9 reach-walk calls the real deno with no NEEDS_DENO gate. G2-E (brief g2/g2e-deno-optional.md)
  fakes it so both branches are still proven, sweeps the five Deno-touching test modules, and adds a guard so no test
  reaches a real deno ungated.

## 2026-11-08 — G2-C merged (6aa2480); baseline 11 -> 9
- Cause: default trial run id stamped to the second; two default prepares of one task in one second collided ("already exists").
  Fix: a default id moves on to -2, -3 (also on a lost mkdir race); an explicit --run is still refused when taken.
- Review 1 APPROVED_WITH_NOTES (docstring said task part is cut; it is the stamp). Main fixed the docstring, then by mistake merged
  before the edited tree had a review (`lane close` refused; the && chain did not stop because of `| tail`). Review 2 of fe47bd9:
  APPROVED; step closed after. Lesson: never pipe a gating command into tail inside an && chain.
- Main's 4-way matrix: 274 tests; py3.14 OK both modes; py3.9 only the 4 record_files deep-JSON baseline errors (G2-A).
- Removed lines: trial_shadow_branches HEAD-garbage subtest; trial_shadow_end_path inconclusive first review.
- Open: worker stash `g2c-tmp` (identical to the merged diff) left for the owner to drop; auto-mode denied Main's drop.

## 2026-11-08 — LS1 round 5 REQUEST_CHANGES -> round 6 by design
- Same three families for the 4th time (fact completeness, citation support coverage, citation file validation).
- Design change (fixes/ls1-round6.md): answers are fact lines `- claim [path:line]`, parsed exactly; no headings or neighbours supply
  files or citations; behaviour facts matched with fixture identifiers removed. Retry coder-d50afe8142.
- Tool bug found: review_rounds' round diff counted the lane's never-committed files twice (as added and as deleted), doubling the
  packet (434k > 300k). Worked around by committing the lane (dec49d3). Follow-up lane: token waste.

## Follow-ups recorded
- Over-deep JSON family: 150 json.load(s) sites / 71 files, 29 catch RecursionError. One shared reader + guard, after open lanes merge.
- review_rounds untracked-files double count (above).
- test_retry_packet RepeatedFamilyGateTests: 1 in 6 flake (state digest coder-1 vs coder-0), checking on clean main.
- retry_packet flake confirmed on clean main 64cc2f9: 4/18 fail. Added to baseline (9 -> 10) as pre-existing; brief g2/g2f-retry-packet-digest.md,
  dispatched after G2-A merges (same test file). Suspected: state_digest names tied tasks in glob order.

## 2026-11-08 — LS1 round 6 done; review packet over the limit; RV-1
- LS1 r6 (coder-d50afe8142): fact-line parser, old prose code deleted. Main matrix 170 x4 OK. Main mutation probe
  (ls1probe6.py): reference 24/24; shift, next-line cite, heading file, basename, generic claim, one-line, drop-line,
  wide range, extra bad cite, star bullets all 0 passes; 3 passes inspected, none a defect. Committed c249ce3 on the lane.
- tp-agent review refused: packet ~341k > 300k. Round diff 230k, of which answers.jsonl 101k (rewritten ~2k-char lines).
  Smaller of diff/file per file = 145k. No bypass used (no --override-review). Product fix: RV-1 (brief rv/rv1-rewrite-whole-file.md),
  dispatched coder-2d2c9997c1. LS1 review waits for RV-1's merge.

## 2026-11-08 — RV-1 merged; LS1 r6 review started
- RV-1 (coder-2d2c9997c1): review 1 APPROVED_WITH_NOTES (F1: keep raw size and fail-before evidence); Main kept both prepare
  outputs (229931 -> 148555) and re-ran fail-before itself (6/8 fail on unfixed code); review 2 APPROVED. Worker's deviation
  accepted: compare the sections as attached, tie keeps hunks (the literal rule would make small edits whole).
  Main probe: a forged `diff --git` line and two secret-like values in a rewritten file stay `+` lines and are redacted.
  Closed (lane close records review 1's verdict, not the later APPROVED; noted), merged d352a8b, lane removed.
- LS1 r6 review now fits and started (review-coder-d50afe8142-204331).
- G2-E review 1 APPROVED_WITH_NOTES (F1 names table, F2 probe logs: Main's first probe log was empty, a redirect-order slip);
  re-run with -v, table of 24 gated tests (14 deno + 10 sandbox), all 9 broken-deno failures gated. Re-review running.
- G2-A verify on its old base: 3 failures, all fixed on main or baselined (retry_packet flake; cap 16 vs 20 from G2-B;
  run-id collision from G2-C). G2-A committed ff1c47b, main merged into the lane, full set queued on the merged tree.
- FG-N1 r4 verify OK x4; 3.14 prints an unclosed-socket ResourceWarning at exit, being traced on main vs lane.
- G2-E review 2 APPROVED. Committed 814d669, closed (step id is `1` for this lane, not G2E), merged 1d38997, lane removed.
  Baseline 10 -> 9: test_r9 reach-walk line removed (now ungated and passes, even with a broken deno on PATH).
  Deno is now optional for tmux-pi's own suite: no test needs a real deno unless marked (14 NEEDS_DENO + 10 NEEDS_SANDBOX).

## 2026-11-08 — LS1 round 7; FG-N1 r4 in review
- LS1 r6 review REQUEST_CHANGES: F1 subject swap passes, F2 intro line hides order, F3 double-backtick span, F4 bare `- `.
  Five rounds in required-fact-completeness. Round 7 brief (fixes/ls1-round7.md): subject rule, order on assigned lines,
  CommonMark code spans, every list item graded, and a GENERATED attack test over all 24 chores (fail-before shown).
  Retry coder-07bbf506d8.
- FG-N1 r4 review started. Its 3.14 ResourceWarning (unclosed 127.0.0.1 socket at exit) is pre-existing on main.
- Follow-ups added: (1) unclosed socket at exit in the FG-N1 module set on 3.14 (main too); (2) conservative false
  positives on main: `timeout 5 echo rg` (search), `nohup sh -c "$CMD" &` (search), `xargs -I{} sh -c 'echo {}'` (sensitive);
  (3) lane close names a lane's steps `1` when dispatched without a session step name: check with `tp-agent lanes NAME`.

## 2026-11-08 — G2-D merged; FG-N1 round 5
- G2-D F1 (owed check) closed. Every LaneCase user was run x4: 186 tests (first run), 300 tests (remove, refusal_remedies,
  autonomy, failing_tests, other_paths, deps, state_symlinks) and 21 tests (decisions_gate, a transitive user). Re-review
  APPROVED. Committed 5a2bf44, closed (step G2D), merged f48c51b, lane removed.
  Baseline 9 -> 8: test_lane_recovery hand-over line removed.
- FG-N1 r4 review REQUEST_CHANGES. Main reproduced F1-F3 on 3.9:
  - a `sudo -h HOST` table error hides `su -c '$X'`;
  - string splitting drops the following words;
  - `$1`/`$@`/`$?` are not treated as unresolved.
  F4: the depth-two product is still partial and 3,290 read assertions are disabled.
  Fourth round in the same two families, so round 5 (fixes/fg-n1-round5.md) makes the uncertainty floor table-free,
  keeps word order when flattening, and requires fail-before on a real base worktree.
  Retry coder-d68351c236.

## 2026-11-08 — LS1 round 8; G2-A re-review
- LS1 r7 verify: 177 tests OK x4. Main's probes:
  - round-6 kinds: only the same 3 harmless passes as round 6;
  - new probe ls1probe7: 0/975 subject swaps pass;
  - ref + reversed copy PASSES in order chores, while reversed + ref fails. The verdict depends on the assignment.
  Review 7 (OpenAI reviewer) REQUEST_CHANGES:
  - F1: Unicode-indented bullets escape rule 9.
  - F2 (duplicate order): a decision for Main.
  The reviewer asked once to search config/ recursively. Main declined and redirected it to config/local.example.json.
- Main found the wider hole behind F1: a fabricated citation on any non-list line (prose, heading, quote, `•`, `–`) is ignored,
  and summarize_run's fabricated count misses it. That would blind S2's zero-fabricated bar.
  Round 8 (fixes/ls1-round8.md):
  - every citation anywhere is checked (rule 10);
  - list items by any Unicode space, blockquote and bullet (rule 11);
  - Main's ruling on F2: order counts every matching line, so repeats and reversals fail (rule 12).
  Retry coder-1e9a5b5bb0.
- G2-A re-review APPROVED_WITH_NOTES: F2 closed. F1 asks for the raw clean-main log of the retry_packet flake (the 4/18 run
  kept only tail lines). Rerunning 18 repeats on clean main f9a719f into rp-clean18.log.
- G2-A re-review 2: APPROVED_WITH_NOTES. It asked for the raw clean-main log, and Main reran 18 repeats on clean f9a719f:
  8/18 FAIL with the same coder-1/coder-0 digest (rp-clean18.log in G2-A's main-evidence). Re-review 3: APPROVED.
  Closed (step G2A), merged 16a9eb1, lane removed. Baseline 8 -> 2: G2-A's six lines are removed (the retry_packet
  loader error, 4 record_files deep reads, run_bounded --durations). Left: the sensitive_trust rg flake and the retry_packet
  digest flake (G2-F).
- F-GATE-TS (fixes/f-gate-ts.md, lane gate-record-once, coder-c13cfc9e4c). G2-F's full-suite matrix found it: the gate record's
  `finished` was read once per write, so the two copies could differ by a second. Fix: the token list is built once and written
  twice. Fail-before: main's script fails `:03` vs `:02` with a stepping date stub; the lane passes. Matrix 160 OK x 4.
  Review review-coder-c13cfc9e4c-000113: APPROVED_WITH_NOTES. The first reviewer was parked after two .env prompts; the
  restart had named-file guidance. F1 (owed): sweep coverage of lib/ctxengine, localdocs, bench and redact*. Main did the
  sweep in the evidence, and no site writes one record twice. Closed F1, merged 4713181; GateTests 36 OK on main (py3.9);
  lane removed. Follow-ups: work_checkpoint (2 clock reads) and task_record (3).
- FG-N1 round 7 (fixes/fg-n1-round7.md, coder-ee3a424f60). Main's rulings: 12 sg UNKNOWN cells accepted; 4 sudo over-reads
  accepted, with each one traced; the 14 cells where a wrong table turned True into False were not accepted. The worker found
  the real cause (a doubled line) and fixed it, and the test now asserts `lost == {}`. `sudo su -c 'git grep foo' nobody`
  reads True (over-read, fails closed): accepted, follow-up. Main's re-run: the F1 reproducer is True with and without the
  table; fgn1r6probe gives the same output on py3.9 and 3.14; matrix 762 OK x 4 (13 modules). On 3.14, the socket warning
  is pre-existing F4. Review review-coder-ee3a424f60-001830: APPROVED_WITH_NOTES. F1 (sudo is not in the persisted
  wrong-table product; the reviewer's 4,752-case sudo product passed) is a follow-up. F2 (owed: which commit each copied
  tree is) was answered by blob-hash comparison: base-tree = dbb101c, r6-tree = 019e07e, main-tree = 7014c87. Committed
  93013ee, closed N1, merged a924a5b.
- Baseline: the sensitive_trust rg line was removed. Its cause (F-FLAKE: K1's same-stat restore of agent/mcp.json) was fixed
  in a0aa3e6, and the line was kept only because T1's runs were intermittent. On main 00a7883, test_sensitive_trust as a whole
  module: 12/12 OK alone (6 per interpreter); 24/24 OK under 4-way concurrent load (3 rounds x 4 per interpreter). It also did
  not fail in the 3 completed G2-F full-suite runs (4678 tests each, main-based tree). The G1 full suite on main is the final check.
  Baseline 2 -> 1 (the retry_packet digest line goes with G2-F).
- G2-F (g2/g2f-retry-packet-digest.md, lane g2-retry-digest, coder-3e7e4cb2b7). Main's runs: repeats 40/40 on py3.9 and 40/40 on
  py3.14. Full suite on the lane tree, 4678 tests x 4: three OK; py39-out had 1 failure, the gate-record timestamp bug, which
  became F-GATE-TS and is merged (4713181). RepeatedFamilyGateTests passed in all 4. Review review-coder-3e7e4cb2b7-011254:
  APPROVED_WITH_NOTES. F1 (owed: sweep rows left out, work_find/trial_shadow_case/state_symlinks/tp_window/lane_writer
  snapshots) was answered in Main's evidence: all are sorted unique paths, no tie. Committed 0b75268, closed G2F, merged
  8748ee2, lane removed. Clean worktree at 8748ee2: test_retry_packet (both modes) and test_agent_tools OK on 3.9 and 3.14.
  In the main checkout, test_retry_packet fails 54 subtests only because of Main's uncommitted openai-codex/ config diff (the
  fixtures dispatch openai/gpt-6.1-sol). It is not a code regression, and the end-of-session config revert clears it.
- Baseline 1 -> 0: baseline-failing.txt is now empty. The G1 full suite on main is the check that nothing outside it fails.
- Follow-ups: bin/tp-agent:287 index tie-break; test_retry_packet ~65 s per file (over the 60 s budget, pre-existing).

## 2026-11-09 — G1 full suite on main; FG-N1b dispatched; LS1 round 13
- G1 full suite, clean worktree at dc4ad17, 4729 tests per cell. Every cell finished so far (py39-out, py39-in, py314-out)
  has the same 25 FAIL + 1 ERROR, all in tests/test_tp_review.py. The baseline is empty, so all of them are new.
  - Cause: FG-N1 (a924a5b) made lib/sensitive_read.py import the new lib/command_wrappers.py. TpReviewTests.TEMPLATE_LIBS copies a
    fixed lib/ list into a temp install and does not have it, so bin/tp-review exits 1 ("No module named 'command_wrappers'").
  - Proof: with command_wrappers.py added to the tuple (scratch, reverted), test_tp_review gave 96 OK.
  - Why it was missed: FG-N1's post-merge check ran only the affected modules (425 tests), not the full suite.
  - Second gap from the same split: config/model-catalog.json protected_paths has lib/sensitive_read.py but not lib/command_wrappers.py.
- FG-N1b (fg-n1b-wrappers-followup.md, lane fgn1b-followup F1, coder-019dc91a62): the template fix, an import-closure test so
  the class fails loudly, a sweep of other partial-install templates, and the protected path plus a routing test.
- test_tp_review took 116 s alone under load: over the 60 s budget. Added to the budget follow-ups.
- LS1 round 12 review (review-coder-e245072fca-015025) was REQUEST_CHANGES. F1: the known-file shortcut rewrote delimited paths,
  e.g. `(junk src/cli.py:6)` was read as src/cli.py. Main reproduced it and checkpointed round 12 as c8f96f5. Round 13 is
  coder-b69bae86e7 (fixes/ls1-round13.md, then ls1-round13-rulings.md):
  - rule 27: inside a pair, the pair decides;
  - rule 29: one list separator splits off a lead-in;
  - rule 30: no fallback outside a pair;
  - flag 1 (HTML comment -> unknown_file) accepted.
  F2 (provenance): the deleted 1.10 s experiment is marked unaudited and nothing relies on it.

## 2026-11-09 — G1 done; FG-N1b merged; LS1 round 14
- G1 final, all 4 cells at dc4ad17: 4729 tests per cell. The only failures are the same 25 FAIL + 1 ERROR in test_tp_review, in
  every cell. Nothing else fails.
- FG-N1b (coder-019dc91a62) adds:
  - `command_wrappers.py` in `TEMPLATE_LIBS`;
  - tests/lib_closure.py and test_lib_closure.py (the import closure of a partial install: names the missing module and its importer);
  - lib/command_wrappers.py in protected_paths, with the sensitive_read floor;
  - routing tests in test_route_resolve and test_model_routing.

  Sweep: 14 sites checked; only test_tp_review needed a fix.

  Main's checks:
  - Fail-before on a dc4ad17 export (3 tests fail; the closure test names command_wrappers <- sensitive_read).
  - Matrix: 489 tests, OK in all 4 cells.

  Review review-coder-019dc91a62-035915: APPROVED_WITH_NOTES.
  - F1 (follow-up): the closure reduces `import pkg.child` to `pkg`, so a package installed without its child is not caught.
  - F2: the report says 28 libs; it is 29.

  Committed 4a8537c, closed F1, merged 2fac25a, lane removed. Full suite on clean 2fac25a started (G1 rerun).
- Budget follow-ups: test_tp_review takes about 115 s alone; test_model_routing about 71 s under load.
- LS1 round 13 review (review-coder-b69bae86e7-032459): REQUEST_CHANGES.
  - F1: nested reverse references are quadratic. Main reproduced it with ls1probe17: 1 MB, k=8000 nested `line 1 of (` pairs
    took 4.06 s CPU at round 13, 0.12 s at c8f96f5 and 2.48 s at 9e2a680.
  - F2: report rows 6/13/21/32.

  Main checkpointed round 13 as 8560cf6 (worktree). Round 14 (fixes/ls1-round14.md) asks for:
  - a linear reader;
  - a nesting family in the rule-20 tests, with a linearity ratio;
  - fail-before on 8560cf6.

  The old pane was dead, so this is a retry: coder-3b1dfbedab, pane %252.
- Incident, about 04:22. The G3 worker's fake-mode cleanup in tools/smoke/g3_e2e.py ran `tmux kill-server` while `tmux_cmd` was still the
  bare default (its `-L` socket is set later, in the fake setup). That stopped the default tmux server, and every pane died:
  - LS1 round 14 (coder-3b1dfbedab);
  - G3 (coder-e9abf0b28a);
  - the detprep session.

  Edits survived in both worktrees. Main recreated detprep with bin/tp and pointed @tp_state_dir / TP_STATE_DIR back to the
  dbe2f4f8 state dir; `tp-agent stop` was run on both workers, then each was retried:
  - LS1: coder-f9e817eb92, fixes/ls1-round14-resume.md;
  - G3: coder-1469cd1f98, g3/g3-resume-1.md.

  G3 must now isolate tmux by construction: TMUX_TMPDIR set to a temp dir, a private `-L` from the constructor, kill-server
  guarded, and a PATH-shim test that asserts no call escapes. Follow-up: audit other tests for the same bare-tmux risk; the
  thousands of stale tp-t3-* sockets under /private/tmp/tmux-501 are already on the list.
- LS1 round 14 (coder-f9e817eb92) reviewed by review-coder-f9e817eb92-045736: APPROVED, no findings.
  - Nested pairs are read with boundary tables and an in-order merge. At 1 MB, k=16000 now takes 0.140 s CPU (round 13: over 8 s).
  - 20,000 differential texts and 12,744 oracle samples give the same readings as 8560cf6.
  - The 4 new rule-20 tests fail on 8560cf6 and pass now. The matrix is green: py39 and py3.14, inside and outside tmux.

  Committed 11b038b, closed S1, merged 6ec27a8, lane removed (archive local-summarizer-20261109T110935Z).
  Follow-up: the rule-20 margin is thin. The worst shape takes 1.66–1.73 s against a 2 s limit.
  Next for LS1: S2 live qualification (at least 57/60, fabricated 0, unparsed 0, every hit inspected by hand), then the flip lane.
- G1 rerun on clean 2fac25a: py39-out ran 4735 tests, OK (skipped 77). The other three cells are still running.

## 2026-11-09 Owner decision J1-D1
- Owner: "go with your recommendations as long as they ensure we get a local model summarization built well and fully
  tested with best chance of success to be used on real world work".
- Decided:
  - (a) bench local tasks dispatch with a role that has no strict floor. The catalog floor is unchanged.
  - (b) each task is reworded to the qualified form of its shape.
  - (c) summarize-importers is replaced for now, then re-added after the LS1 flip.
- LS1 S2 is tightened so the qualification reflects real work (ls1-local-summarizer.md, "S2 additions"):
  - a real-work split of 12 chores over full-size tmux-pi files (lane step S1b);
  - at least 57/60 on both holdout and real, with no fabricated citations and no unparsed replies;
  - every real reply inspected by hand;
  - routing deterministic and in agreement, at least 34 of 36 chores tagged summarize_files;
  - 3 tasks through the full `tp-agent dispatch` path after the flip.
- G4 now waits for the LS1 flip and includes the summarize task. The locate-chant routing disagreement is settled under
  S2 step 3.

## 2026-11-09 G1 rerun on clean 2fac25a (after FG-N1b): green
- Four cells, 4735 tests each, 0 failures and 0 errors. Logs are in Main's job tmp g1-main2/.

  | cell | tests | time | skipped |
  |---|---|---|---|
  | py3.9 out | 4735 | 2168 s | 77 |
  | py3.9 in | 4735 | 2293 s | 77 |
  | py3.14 out | 4735 | 2261 s | 78 |
  | py3.14 in | 4735 | 2339 s | 78 |

- The baseline stays empty. G1 holds on 2fac25a.
- The next full run is on main after the G3 merge. That run also covers the LS1 merge 6ec27a8.
- Follow-ups found in the warnings. They are not failures, but are to be fixed under "all failures fixed in the end":
  - **py3.14 only, 9 ResourceWarnings per run.** Most are "Implicitly cleaning up <HTTPError 500 ...>": a test or the code
    under test leaves an HTTPError unclosed. One is an unclosed 127.0.0.1 socket at exit, the known test_redact_llm item.
  - **Leaked shell loops.** tests/test_process_end.py leaves orphaned `trap '' HUP TERM; while true; do sleep 1; done`
    loops (ppid 1) running after full-suite runs.
  - **Stale tmux sockets.** Full-suite runs leave tp-t3-<pid>-* and tp-ts-<pid>-* sockets in /private/tmp/tmux-501.

## 2026-11-09 G3 smoke script merged (fake mode); live run still owed
- Lane g3-smoke, five rounds:
  - every review was cross-provider (openai-codex);
  - round 5 coder-2797d9c567, review review-coder-2797d9c567-072159: APPROVED_WITH_NOTES;
  - closed at d0bb8b6, merged into main as 256e332, lane removed (archive g3-smoke-20261109T132934Z).
- Round 5 fixes:
  - Rule A: the lane config is loaded in a bounded `python -I` child, so the script imports nothing from lib/. `bounded()`
    kills the child's whole process group.
  - Rule B: collision and leftover checks use the exact worktree path from `lanes.lane_worktree`. Look-alike names are
    reported only. The nested `{repo}-{name}/checkout` pattern is supported; only empty directories the run made are
    removed.
- Main's isolated four-cell verify: 56 tests per cell, all OK. Default tmux sessions, sockets (7237 entries), worktrees and
  `lane/*` branches were identical before and after; the private TMUX_TMPDIR was empty afterwards.
- The review notes are answered in Main's evidence addendum (task coder-2797d9c567/main-evidence):
  - F1: the per-test timing file is attached and the historical mapping corrected.
  - F3: socket equality shown, and the orphan loops dated to before the run.
  - F2: the live battle-soccer run is G3's remaining done condition. Main runs it.
- Over 5 s alone: 9 TamperTest tests at 41-43 s (one shared batch of 56 tamper runs), and 9 test_g3_e2e tests at 26-27 s
  (shared fake-run batches). Each file is under 60 s.
- New follow-up: /private/tmp/tmux-501 holds 7237 sockets. Most are stale, from test runs, and are part of the stale-socket
  follow-up.

## 2026-11-09 G3 live run on battle-soccer: blocked by Claude's folder-trust dialog
- Two live runs (`tools/smoke/g3_e2e.py --repo ~/Documents/Projects/battle-soccer`; the second with `--keep` and pane
  captures). Both failed at step `prompt`: "no permission prompt within 240 s (last seen: starting)". Cleanup was complete
  both times: no errors, no leftovers, no session, branch or worktree left. Main removed the kept temp state dir.
- Cause, from the pane captures at 30, 60, 120 and 200 s:
  - the coder sat at Claude Code's folder-trust screen for the new worktree `battle-soccer-wt-g3-<run id>`;
  - tmux-pi's Claude profile trusts tmux-pi but not battle-soccer. That is why tmux-pi lane worktrees never ask; Main
    confirms this once battle-soccer is trusted.
- Main did not answer the dialog. Folder trust is the owner's decision: `bin/tp-claude-bootstrap ~/Documents/Projects/battle-soccer`
  from a real terminal, once.
- Product gap: `tp-agent inspect` shows `starting` for a worker stuck at the trust screen, so every waiter times out
  without a reason. The bench detects that screen; task inspection does not. Fix lane TV1 (g3/trust-visible.md).
- G3 stays open until a live run passes after the owner trusts battle-soccer.
- LS1b: round 2 (step-count bound) is checkpointed as eb8661a. Main's verify found test_qual_summarize_oracle failing with
  21 errors in every cell. The oracle generator assumes a file extension, and the real split cites `bin/tp-spawn`.
  Round 3 brief: fixes/ls1b-round3.md.

## 2026-11-09 G3 live run 3: trust fixed; the decline does not settle the request
- The owner trusted battle-soccer (`tp-claude-bootstrap`, from a terminal). `hasTrustDialogAccepted` is now true in
  tmux-pi's Claude profile.
- Steps setup, dispatch and prompt passed: the ask was raised 4 s after dispatch.
- Step `decline` failed. After Escape the pane read "Interrupted · What should Claude do instead?", but
  permission-request.json stayed `pending` for 240 s. Cleanup was complete.
- Cause: Claude Code fires no PermissionDenied event when the owner rejects the dialog. `redact_hook` drops pending grants
  on Stop and UserPromptSubmit, but leaves the task's request file pending. So every real decline leaves a task showing
  `needs_input`. Fix lane DS1 (fixes/ds1-decline-settles.md).
- The G3 script waits for the request to settle before it sends the redirect, so it must send the redirect first. That
  change comes in a G3 round after TV1 and DS1 merge, along with the README's PermissionDenied claim.

## 2026-11-09 LS1b round 3 verified; review packet over the limit; FC1
- Main's four cells for LS1b round 3 (all test_qual_summarize* modules, test_router_catalog, test_route_resolve): 228 OK in
  every cell. Checkpointed as 9889bd4 in tmux-pi-wt-ls1-real.
- `tp-agent review coder-622ab09870` refused: the packet is ~480k against the 300k limit. LS1b had not been reviewed before
  (rounds 1–3 were retries). Of the 420k lane diff, 288k is the ten frozen fixtures under fixtures/real/. All ten are
  byte-identical to `109948a:<src>` (cmp), and 109948a is an ancestor of main.
- No limit raise and no override. Product fix FC1 (fixes/fc1-frozen-copies.md): a `frozen-copies.json` manifest. Verified
  copies are named in the review diff with their blob id and their contents left out; anything not verified keeps its full
  diff. After FC1 merges, LS1b adds the manifest and goes to review.

## 2026-11-09 TV1 merged (folder-trust dialog shown as needs_input)
- Round 2 (tv1-round2.md) fixed review 1's F1 (the bench again gives `error: trust-dialog` and stops the worker), F2 (a per-capture
  timeout plus a 15 s scan budget; rows not looked at say "not checked") and F3 (the G3 script quotes the bootstrap folder
  with a local helper, because test_g3_e2e's allow-list forbids `shlex`). F5's provenance logs are in the task dir.
- Main's four cells over round 1's 13 files: 320 OK in three cells. In py39-in, `test_status_repaint.test_mouse_tracking_lifecycle`
  failed once (an empty tail within its fixed 5 s wait after SIGTERM). TV1 cannot reach it: the trust scan runs only for
  claude rows and that test's jobs are pi. Alone in the same cell it passed 5/5 and the whole file 40 OK. Follow-up: a
  load-sensitive test to fix.
- Review review-coder-b8fd822d83-100331: APPROVED_WITH_NOTES. The reviewer ran 103 tests and checked 22,621 quoting cases
  against `shlex.quote`. Its one note (files over 60 s) is the tracked slow-file follow-up.
- During the review Main declined two sensitive-read prompts with Escape (both recorded `denied`): a recursive rg that
  included agent/, then an rg whose exclusion glob `-g '!*.env*'` alone raised the prompt. Follow-up: the classifier treats
  an exclusion glob as a read.
- Checkpoint ce4caf5, `lane close trust-visible TV1`, merged e1e1b4f (code identical to the reviewed tip), lane removed.
  Not pushed. F4 (index and notifier do not show the trust needs_input) stays a follow-up.

## 2026-11-09 DS1 rounds 3 and 4; FC1 round 1 reviewed
- DS1 round 3 (251656b) scoped `settle_declined` by `agent_id` and added `SubagentStop`. Review: REQUEST_CHANGES. F1: in
  shadow and off a late `PostToolUseFailure` after `declined` was not recorded. F2: after 200 newer main-agent calls, the
  trimmed `calls` let the main agent's Stop delete a subagent's live grant (approving it then exits 126). F3: the fail-before
  runs were not kept. The reviewer's rg over `bin/` raised the sensitive-read prompt (tp-secret-exec); Main declined it with
  Escape and redirected.
- DS1 round 4 (fixes/ds1-round4.md, checkpoint 289e2ba): the late failure settles in shadow and off; `trim_calls` keeps
  calls with a recorded run and the newest 200 that use a pending name (calls/agents ≤ 600, runs ≤ 200); a grant with an
  unknown owner is kept. Fail-before: the round 3 tests fail 5/6 on 620764d (one guard), the round 4 tests 7/7 on 251656b.
  Main's four cells over the same 13 modules: 489 OK in each. In review (review-coder-51f93f6064-115631).
- FC1 round 1 (checkpoint 321568c). Main's four cells showed 11 failures per cell, all from tests that need an ambient tmux
  server under an empty `TMUX_TMPDIR`; they passed with a private server (environment, not FC1). Review: REQUEST_CHANGES.
  F1: `gate()` read the hunk lines `-- a/copy` / `++ b/copy` as a file header pair and refused an ordinary edit. F2 note:
  Git calls per copy and a 73 s test file. F3: the logs were named without their paths. Round 2 is running
  (fixes/fc1-round2.md, coder-6b53f61929).
- S2 started from a plain shell. LM Studio was up the whole time (probed). Earlier estimates wrongly listed it as something the
  owner had to start. Runs: route-only ×3 on all 24, tune ×3 with the live prompt and ×3 with the draft from
  config/local.example.json (bench-results/s2-2026-11-09/).

## 2026-11-09 LS1c S1 merged; live tuning; SE1, S1b and AT1 under way
- LS1c S1 (checkpoint 48d603b). The review review-coder-dc15a6aff4-134510 returned APPROVED_WITH_NOTES: 163 scoped tests
  were rerun independently under 3.9 and 3.14. Its F1 note says SE1 still owes evidence that the session-env failures
  reproduce on the base. Main then closed the lane step, merged it as 711cd79 and removed the lane.
- Main's run of test_session_env on main fails 4 cases. The log is
  /Users/jerrickvandiver/.claude/jobs/6061f696/tmp/main-session-env.log. The causes:
  - TP_TRUST_CAPTURE_TIMEOUT and TP_TRUST_SCAN_BUDGET were never classified;
  - test_g3_e2e and test_g3_e2e_tamper lack `import session_env`;
  - test_lane_writer needs a tmux server, so the filtered-discover test and reviewers_repro both trip on it.

  TV1 and G3 merged without running this guard. Rule from now on: every lane verify adds tests.test_session_env. SE1
  (fixes/se1-session-env.md, coder-295d3a9910) fixes the first three causes. AT1 fixes the test_lane_writer part.
- AT1's four-cell rerun used a short private TMUX_TMPDIR. The first run's 51 setUpClass errors came from a socket path
  over macOS's limit. The rerun shows only 3 test_session_env failures, the same as main minus the lane_writer one, which
  AT1 fixes.
- Live structured-path tuning ran on the tune split, ×1 each, deterministic at temperature 0. Prompts and runs are in
  bench-results/ls1c-2026-11-09/.

  | Run | Passed | Notes |
  |---|---|---|
  | S1 base (×3) | 6/36 | 60 range_too_long drops |
  | t1 (wording) | 3/12 | |
  | t2 (`lines` list of 1-3) | 3/12 | 0 drops, 34 missing |
  | t3 (+ subject field) | 2/12 | |
  | t4 | 1/12 | |
  | t5 (+ ast name outline in the prompt) | 1/12 | 41 failures |
  | t6 (+ name index in the output) | 2/12 | 36 failures |
  | t7 (+ parameters in the outline) | 3/12 | 28 failures |

  - **What helped**: forcing structure through the schema and adding the list of names from `ast` (deterministic)
    helped more than prompt wording did.
  - **What remains**: compound claims ("waits 1 s, 5 s and 30 s"); the class-vs-method subject; "use" chores that skip
    what the caller does; and absent-use or negative facts.
  - The bar is 57/60 on the holdout and real splits. It is far off.
  - S1b (fixes/ls1c-s1b-structured-v2.md, coder-52af2c36fb) builds the v2 fact form, the name outline and the name index
    properly, with tests, so tuning continues on the real module rather than the scratch copy.

## 2026-11-09 LS1c tune runs re-scored strict vs meaning; the bar moves to meaning
- Owner asked whether the failures are format rather than missing information, then: "re-score the runs both ways, then
  grade on meaning". Each tune chore's 9 replies (base run 1, t1-t7, the Pi path) were shuffled, anonymized (key seed
  20261109) and graded blind by 6 Claude subagents against reference facts, one rubric (plus amendment 1: absence
  claims graded on truth). Everything is in bench-results/ls1c-2026-11-09/meaning/ (packets, key, grades, scores.txt).

  | run | strict | meaning (all facts, 0 wrong/unsup) | meaning >=80% | recall | wrong | unsup |
  |---|---|---|---|---|---|---|
  | base | 2/12 | 4/12 | 4/12 | 64% | 0 | 0 |
  | t1 | 3/12 | 4/12 | 4/12 | 56% | 2 | 0 |
  | t2 | 3/12 | 5/12 | 6/12 | 76% | 4 | 1 |
  | t3 | 2/12 | 7/12 | 8/12 | 85% | 0 | 0 |
  | t4 | 1/12 | 6/12 | 8/12 | 85% | 0 | 0 |
  | t5 | 1/12 | 5/12 | 8/12 | 85% | 0 | 0 |
  | t6 | 2/12 | 3/12 | 7/12 | 84% | 0 | 2 |
  | t7 | 3/12 | 4/12 | 8/12 | 82% | 3 | 0 |
  | pi | 0/12 | 1/12 | 1/12 | 45% | 0 | 40 |

  - Format explains much of the strict gap: t3-t5 have zero wrong and zero unsupported lines and 85% recall while
    strict passed 1-2.
  - The real gap is cross-file reasoning. steps-*, sum-* and exports-* are near-solved from t3 on; the misses sit in
    diff-settings (2-4/5: adds/drops), use-jobqueue (3-4/6), use-lrucache (who does not use it) and diff-order F5/F8
    (lower priority first; JobQueue never evicts). t7's 3 wrong lines are false "not in the files" claims.
  - The name index (t6/t7) raised strict but not meaning recall; it is true and harmless, not a recall lever.
  - The Pi path's 40 unsupported lines are off-by-n citations; the structured path's numbered input fixes them.
  - Caveats: one run per config, one grader per chore, n=12; t3-t5 differences are inside the noise.
- The bar in ls1c-structured-summarize.md is now the meaning bar: zero wrong, zero unsupported, recall >= 90% overall
  and >= 80% per chore, judged by a calibrated non-qwen judge. The best run today (t3) misses it on recall (85%;
  diff-order, diff-settings, use-jobqueue under 80%).
- Next levers, all deterministic prep: an ast diff of defined names and values for diff chores; a reference scan
  (imports and call sites per name, and named files with none) for use chores; a check that drops a "not in the files"
  claim whose subject is defined in a named file. A repeatable meaning grader lane follows.

## 2026-11-09 (later): merges AT1, FC1, SE1, S1b, DS1; S1c dispatched
Each lane: checkpoint, Main's evidence, openai-codex review, notes closed, `lane close` run alone, merge, collect.
- AT1 (ambient-tmux) 57611f6, APPROVED_WITH_NOTES: no tmux server reads as an empty pane table. Notes closed with the
  liveness qualification and a fail-before script plus log.
- FC1 (frozen-copies) 234225d, APPROVED_WITH_NOTES: review diffs name verified frozen copies; the gate reads hunks with
  bounded git work. The truncated-hunk test is a fail-before test.
- SE1 (session-env) d63f08c, APPROVED_WITH_NOTES: TP_TRUST_* test hooks classified; g3 tests scrub before import.
  Preview 148 OK in all four cells; the provenance note closed with preview-provenance.txt. test_session_env passes on main.
- S1b (structured-v2) b77ad33, APPROVED after round 2 (fixes/ls1c-s1b-round2.md: *args/**kwargs in the outline). Merged
  tree equals the verified preview tree (d200d6a); 190 OK (skipped=1) in all four cells.
- DS1 (decline-settles) 9915fd8, APPROVED at round 5: a late answer to a declined request keeps every field of the
  prior record. Preview (d63f08c + lane) 144 OK in all four cells; the merged DS1 files equal the preview's blobs.
- S1c (lane code-facts, task coder-f8f92bfdf7) dispatched on b77ad33 with fixes/ls1c-s1c-code-facts.md: version diff,
  use scan and the false-absence drop computed by code, aimed at the meaning bar's recall misses.

## 2026-11-09 (evening): LS1b and MG1 merged; G3 decline cause found; checkpoint-1; owner's production issues
- LS1b (ls1-real) db29594, APPROVED: real-work split, frozen copies with manifest, oracle extensionless targets.
- G3 live runs 4 and 5 (battle-soccer) failed at `decline`. Run 5's pane captures showed the redirect typed but never
  submitted: `tp-agent message` sends Enter straight after the text, and Claude Code takes the burst as a paste. tp-contract
  and tp-task wait 0.3 s first, but none of the three confirms the submit, and tp-agent still reports `sent: true`.
  Lane msg-submit (MS1, fixes/ms1-message-submit.md) is building a shared sender that confirms the submit. Lane g3-decline
  waits for it.
- Checkpoint 1 (release-gates.md): a production cut without the local summarizer. Gates: MS1 and MN1 merged, a G3 live run
  passing, G1 on the cut, G5 port rehearsal, then a local tag and port notes.
- Owner's production issues: (1) "messages to workers not getting sent" is MS1. (2) "workers no longer sending messages
  ack to main agent", which the owner thinks happens when several side tasks break the linking.
  - Traced with two read-only agents and reproduced by Main: the Claude-main notifier links tasks by
    CLAUDE_CODE_SESSION_ID. `tp-agent list --main` silently drops same-session tasks tagged with another id, so workers
    started from another Claude session never reach main.
  - Failed wakes and failed polls also drop notices silently.
  - A pi orchestrator main has no notifier at all, by design.
  - Lane main-notify (MN1, fixes/mn1-main-notify.md) dispatched.
- MG1 (meaning-grader) f503858, APPROVED at round 2: packets chosen by shape, and a fact mismatch is an invalid grade. The
  merged tree equals the preview; 184 OK in all four cells. Live calibration against the 108 hand grades is running.
- S1c (code-facts) round 2 closed F1-F7 and F9. Its preview with main failed one guard test on the real split rows that
  LS1b added, so round 2b covered every row.
  - The review packet was then 762k, then 1.2M after a rebuild, because a later round diffs against the previous round's
    tree, which predates main's merges.
  - The lane was rebuilt on f503858 as one commit, b1f06fc (equal to 3245236, kept as backup/code-facts-3245236). A
    fresh check task (fixes/ls1c-s1c-check.md) gives it a first review against main.
  - Follow-up: a later-round packet should leave out what came from main (diff against the previous round's tree merged
    with the new base).

## 2026-11-11 (midday): PD1, LP1, Q4a, LM1, PR1 merged; owner away, Main decides by its recommendation
- PD1 (process-decode) and LP1 (local-provider) merged (c547fae, c372c71); main's code equals the serial-tested tree 07ae0e8
  (5321 tests, OK). Post-LP1: config/local.json has `provider_aliases` openai -> openai-codex (owner-authorized); the owner's
  local rename of config/model-catalog.json and config/router.json is saved untracked in .tmux-pi/plans/ and reverted;
  `tp-route --check` passes. d4f2003: lib/trial_shadow.py pid_is_run reads ps with surrogateescape (PD1 follow-up).
- PR1 (test-speed), round 2 APPROVED_WITH_NOTES. Same code, serial vs parallel: 3442 s vs 791 s, the same 5325 ids, 77 skips
  both. Known limits from the review:
  - F1: the serial tree 07ae0e8 and the parallel tree 5eb1b98 differ in PR1's four files, so this is before/after evidence and
    not a same-tree run; the comparison script did not wrap the run in the 5,400 s alarm.
  - F2: the author's report says logging needs `--log`, but tests/run_bounded.py writes its default log without it.
  - The children keep the caller's TMPDIR (Main approved the exception; TMUX_TMPDIR is private per child).
  - The 110-pty figure is a sampled-window peak.
- Q4a (tq1-setup), LM1 (lmstudio-cap) and PR1 merged (01747c7, d5faa23, d0bf9cc). The combined discovery on the merged tree
  da25880 (parallel runner, -j 8): 5331 tests in 601 s, OK (skipped=78). LM1's cap is now in force: live LM Studio tests are
  opt-in (TP_LIVE_LMSTUDIO=1), 300 s, one at a time, 20 per run.
- HB3a: the last-round review found the family again (F1: the rules overflow command `--status active` missed pending and
  provisional records). Closing over it needs the owner's terminal override, and the owner was away and had said to decide by
  recommendation, so Main fixed it (4f20d56: the remedy lists every status; the campaign runs each remedy). The focused review
  was APPROVED_WITH_NOTES. Known limits:
  - 18 or more attached workspaces can exceed the 60-line brief (64 lines). Automatic startup attaches one.
  - Round 6's report needs qualifying: the attack/environment history lacks retained logs, the replacement fixture drops the
    task lane fields, and the inventory has 37 call sites, not 38.
- RR1 (review-rounds): Sonnet worker plus Main fix e04cc16. Tasks from before the meta.json `gate` field count as gated by the
  sweep.md that the gated retry wrote into the replaced author. HB8a's earlier findings drop from 49.6k to 13.9k chars (packet
  ~396k).
- 2026-11-11 afternoon. HB3a merged (54af2a2): full parallel discovery on the merged tree, 5349 tests in 636 s, OK
  (skipped=78).
- RR1 round 1 got REQUEST_CHANGES (retry-window-provenance). Both faults were Main's:
  - F1: e04cc16 inferred the gate from the parent's sweep.md, so a sibling retry could gate an ordinary one.
  - F2: the brief never said to record the triggering review.
  Round 2 (Sonnet, brief hub/rr1-round2.md) records `gate_review`; a legacy child is gated by its own dispatch.json.
  APPROVED_WITH_NOTES. Merged 16b8d4d; discovery 5376 tests in 577 s, OK.
- HB4a round 5 (last) got REQUEST_CHANGES; both families recurred. Known limits are in hub/hb4/hb4b-known-limits.md.
  - Closing needs the owner's `--override-review`.
  - Main + lane with two "keep both" conflict resolutions (tree fa5e9d8): 5401 tests, OK.
  - Main merged main into the lane between rounds, and the packet hit 1.04M chars. The merge was taken back off the lane. Rule
    from now on: never merge main into a lane between review rounds.
  - The worker got around guard refusals of edit commands with scratch scripts (review F5). The work was kept; the owner is to
    see it.
- HB8a round 8 got REQUEST_CHANGES; three families recurred at new sites. Under the owner's delegation, Main sent one
  rule-first last round on Opus (hub/hb8/hb8a-round9.md): one function per record fact. A recurrence merges as is with HB8b
  known limits. The retry recorded gate_review (RR1 in use).
- 2026-11-11 evening, owner away (delegation: decide by recommendation).
  - Merged into local main after combined runs: HB4b (81bb48e), Q3 (8313708), PF1b (7bb49ad); combo8 5524 tests, the 3
    test_g3_e2e_tamper failures were "fork failed: Device not configured" and the module passed alone. HB2b round 6
    (rule-first, Sonnet: state_digest compares an in-tree file's text, not launch.json's hash of it) approved with notes and
    merged (c42bc97); combo9 5530 tests, OK. Nothing pushed.
  - Pty exhaustion: 415 orphaned `trap '' HUP TERM; while true; do sleep 1; done` loops from tests/test_process_end.py
    (test_stop_ends_a_process_that_ignores_hangup, HUP_PROOF) held 439 of 511 ptys (kern.tty.ptmx_max). Main killed them
    (exact command, ppid 1); 24 ttys after. The combo10 failures (test_pane_send, test_trial_shadow_outcomes,
    test_lane_recovery) passed on rerun (51 tests, OK). Follow-up: test_process_end cleanup must kill the pane's process
    group; until then full runs leak these loops (see "Leaked shell loops" above).
  - HB8b rounds 3-5 (design, Opus) each got REQUEST_CHANGES on verification-tree-identity. Main stopped the lane at cb7e57f
    (cells 314 x 4 OK) for the owner's decision: hub/hb8/hb8b-decision.md.
  - TL1 (lane test-leak) merged dec415e: TmuxCase.pane() registers a cleanup that SIGKILLs the pane's process group (only
    while the pid still leads its group and has the recorded start time). Review review-coder-ce153756f1-212530
    APPROVED_WITH_NOTES; Main's dispositions for the other launch sites are in the task evidence (each has its own cleanup
    or a bound). Cells 164 x 4 OK, 0 leftover loops; on merged main test_process_end + test_session_env 57 OK, 0 loops.
  - HB6a (lane hub-waits) tool gaps seen: (1) the round-1 worker's lane-ownership request sat unseen ~20 min because
    Main's watcher only waited for result.md (Main now also watches for a pending capability-request.json); (2) merged
    lanes still open (perf-spawn, hub-correct, others) keep broad touches (tools/, tests/) that overlap every new lane;
    (3) a request from a worker that has ended can only be answered by a retry; (4) review panes stay after collect.
    Candidates for HUB work, not fixed here.
  - HB6a (lane hub-waits) merged bf76186: tools/waits/timeline.py, where work waits from existing task records (WT1 arm A).
    Three rounds (Opus, Opus, Sonnet rule-first); review-coder-248931aa5b-231733 APPROVED_WITH_NOTES. Full run on the
    merged tree 7bdba44: 5533 tests OK (skipped 78). Measures: case-1 attribution 74.2% (bar 90%: not met, misses are the
    --after queue wait and owner input recorded as by: main); real runs 1.2% unknown on critical paths. Decision: arm B
    (HB6b) needed, recording who answered and the approval instant. Smoke run on HB6a's own rounds: 0% unknown inside tasks,
    28% of the critical path is Main's time between tasks (verification, diagnosis), which no record covers yet.
  - HB6b1 (lane hub-waits-b, WT1 arm B first slice) approved at 6515b5c after 3 rounds (Opus, Opus, Sonnet rule-first);
    review-coder-52842bfcd1-020016 APPROVED_WITH_NOTES. Cells 227 x 4 OK; full run on the merged tree 31b1284 5533 OK
    (skipped 78). Case-1 attribution 100% (arm A 74.2%). Stopped before merge for the owner (hub/hb6/hb6b1-decision.md).
  - HB5a (lane hub-verify, VR1 arm A measure, tools/checks/repeats.py): rounds 1-3 REQUEST_CHANGES on the same families
    (which commands count as the same check; shell text taken as paths; masking). Round 3's inverted rule (eligible only
    when fully understood) cut real-session repeats to 5 duplicates + 2 covered, 87.2 s (0.5% of recorded test time);
    Main's own reruns stay invisible. Round 4 (Sonnet rule-first, coder-68f2fb8255) validates a strict grammar on the
    original words before any normalisation.
  - Tool gap: the trust-store guard refuses read-only analysis and edits whose text contains "tmux" or session paths; HB5a
    workers worked around it once (round 1 rewrote refused analyses as scripts; round 3 edited through a heredoc), both
    disclosed. Main authorised one narrow case (the reviewer's in-process probes from its own evidence dir).
  - HB5a approved at 7a97277 after 6 rounds (rounds 3-6 Sonnet rule-first); review-coder-abcd34fd0b-034623
    APPROVED_WITH_NOTES. The rule that held: one strict grammar over every original command word in every role before any
    recognition (round 4's gap was Main's brief wording). Full run on the merged tree 8d4af09: 5535 OK (skipped 78), 546 s.
    Real session: 7 duplicates + 2 covered = 92.5 s (0.5%); Main's reruns invisible. Stopped before merge for the owner
    (hub/hb5/hb5a-decision.md). HB7 waits on HB8 (hb8b-decision), so the HUB queue now waits on the owner.

## 2026-11-12 (morning): owner "use recommendations"
- HB6b1 (A, adopt) and HB5a (A, adopt): `tp-agent lane close hub-waits-b HB6b1` and `tp-agent lane close hub-verify HB5a`
  passed (approved reviews of the exact trees); merged into main 278cfdd and 84b54ed. Full run on main 84b54ed (tree
  10ff356): 5535 tests OK (skipped 78), 568 s. To answer a worker on the owner's behalf: `tp-agent message ID --resolves R
  --by owner --quote "..."`. Report: `python3 tools/checks/repeats.py .tmux-pi/sessions/<id>/tasks`.
- HB8b (A2): round 7 (Sonnet, rule-first, coder-c1a7ce3072) reverts round 6's two additions (mtime window, stable window).
- HB8b (A2) rounds 7-8 (Sonnet, rule-first): round 7 reverted the mtime and stable-window rules (17 of 28 stored fake runs'
  claims went backed -> unknown); review-coder-c1a7ce3072-073558 found the runner test's false_claims expectation held for
  one interleaving only; round 8 derived it from the run's recorded stamps. review-coder-6c12ed2ab4-075154
  APPROVED_WITH_NOTES (stale comment at tests/test_episode_harness.py:223 and report wording still describe the removed
  continuous-presence rule; note only). `tp-agent lane close hub-episodes2 HB8b` passed; merged e731aa8. Full run on main
  e731aa8 (tree f9facdd): 5535 tests OK (skipped 78), 553 s. Effect for live E-runs: a claim not near a poll or Read scores
  unknown, so "0 false claims" needs direct evidence. HB7 (H40) is now unblocked.

## 2026-11-12 (afternoon): HB7a waits on the owner's adoption decision
- HB7a (H40, lane hub-friction, rounds 1-4): read-only report `tools/friction/report.py` of the owner's process corrections
  by key (H48 labels, Main's feedback memories, HB3 records), recurrence by session, contract size. Rounds 1 and 3
  REQUEST_CHANGES were Main's brief wording (quote marks as owner authority; key-level worker mark); round 2's py3.14 failure
  was `Path.glob` skipping an unreadable folder silently. review-coder-b5a566b6cc-085938 APPROVED_WITH_NOTES (list-then-read
  window a known limit). Full run on preview 76aa79e (main 3ffa8cf + lane 584d109, tree c2d663b): 5538 tests OK (skipped 78),
  550 s. Not merged: the brief says adoption is the owner's; options in docs/plans/det-prep/hub/hb7/hb7a-decision.md.
- HB7a adopted (owner "use recommendation" = A): `tp-agent lane close hub-friction HB7a` passed; merged 0427722. Key file
  filled: 24 keys (16 active on the owner's own words, 8 candidate from Main's memories and ideas); 12 label rows left
  ungrouped on purpose (7 one-off task instructions; labels-B #89, #91, #185-187 are Main's instructions to workers that H48
  labeled as owner messages). Recurrence by session: state-saved-for-restart 5, model-choice 2, dispatch-via-tmux-pi-workers 2.
  Next: the owner's OK for a claude/main-prompt.md line so Main records corrections as HB3 records.
- HB7b (H40, lane hub-prune, rounds 1-2): contract ledger check and skeleton in tools/friction/report.py. Round 1
  REQUEST_CHANGES (code against stated rules: duplicate identity, problem totals, stale pointers); round 2 (Sonnet)
  review-coder-1653d423f5-100210 APPROVED_WITH_NOTES. Real data: 97 contract lines need a reason, 0 have one (all provenance
  only). Cleanup: 23 merged lanes removed with `tp-agent lane remove` after collecting 16 finished reviewers; 7 HB7a preview
  worktrees removed. Not merged: options in docs/plans/det-prep/hub/hb7/hb7b-decision.md.
- HB7b adopted (owner "use recommendation for decision" = A): `tp-agent lane close hub-prune HB7b` passed; merged 00bee5c
  (after pulling the work-Mac tp-pi long-argv fix as 05c3aa6). Full run on preview be588cd: 5539 tests OK (skipped 78), 553 s;
  after the merge test_friction_report, test_session_env, test_docs_wiring 42 OK. Ledger written:
  docs/plans/det-prep/hub/hb7/contract-ledger.json, 97 entries, 0 problems, 0 unresolved pointers: 53 justified on the owner's
  words, 20 on Main's docs/keys, 24 provenance only (owner list in hb7b-decision.md "Ledger result"). No contract line changed.
- HB7b ledger, owner decision (2026-11-12, "use recommendation for decision"): keep the 24 provenance-only lines; HB3 record
  det-prep/R1 (first record in the work store; workspace det-prep created). Ledger: 97 justified (77 owner-stated / 20 Main),
  0 problems with `--store ~/.config/tmux-pi/work`.
- HB7b ledger correction (owner question 2026-11-12: why copy a record between machines): the committed ledger must not
  point at a machine-local HB3 record. The 24 entries now point to hb7b-decision.md "Owner decision on the 24 lines"; the
  check resolves with no `--store` (97 justified: 53 owner-stated / 44 Main, 0 problems). Gap noted for HB7: a doc pointer
  counts as Main even when it quotes the owner, so portable owner-stated reasons exist only as H48 label rows today.

## 2026-11-12 (late afternoon): PF1c, LP2, GB1, TR1, TR2 merged; HB3b waits on the owner
- PF1c (lane perf-review, round 2 on Sonnet, coder-9628d382cb): split_sections counts an empty line as blank context
  (diff.suppressBlankEmpty); blob_patches falls back to per-file blob_patch on any Refused. A survey of 38 diff/core settings
  found only suppressBlankEmpty differs. review-coder-9628d382cb-124549 APPROVED; merged ab722a0. Round-1 F2 (identical cell
  wall times) was settled as separate processes: separate CPU, different end times when staggered; cells now log user CPU.
- Batched full run on main + PF1c + HB3b (9b23173): 5546 tests, 2 failures: an HB3b comment (fixed in HB3b round 3, lane
  0f07089, review-coder-8c0089d198-134156 APPROVED, still the owner's A/B) and test_local_provider:190, stale since e97296d.
- LP2 (lane lp-test): the LP1 alias launch test reads pi's system prompt from its file. Main's `--tests none` made the worker
  skip tests (memory: dispatch-tests-flag); Main ran the cells. review-coder-22c2364fd8-134141 APPROVED_WITH_NOTES; merged 0f82f94.
- GB1 (lane gate-blank): the review gate's outside_hunks counts blank context under diff.suppressBlankEmpty, never the end piece
  after the final newline. review-coder-8fc2571f65-135739 APPROVED_WITH_NOTES; merged 6120340.
- TR1 (lane task-record-utf8, 3 rounds): task_record never raises on non-UTF-8 task files (display reads replace; verbatim fields
  round-trip with surrogateescape and newline=""). Round 1 F1 CRLF/CR translation, F2 artifact rows; round 2 F1 fallback rows;
  round 3 retried `--route rule-first` (family verbatim-newline-loss twice) with a kept-field x reader table.
  review-coder-b9add0ae87-140827 APPROVED_WITH_NOTES; merged 4d21709.
- TR2 (lane task-reader-utf8, 2 rounds, from TR1 review F3): tp-agent record/retry, model_router review_plan, contract ledger_row
  read with utf-8/replace. Round 1 F1: retry's fix had no row. review-coder-bd259bdc7e-141312 APPROVED_WITH_NOTES; merged 20af558.
  Not fixed: lib/bench/runner.py:381,422 read result.md strictly (bench harness).
- Full run on main 20af558 (tree 0e10c42, python 3.9.6, outside tmux): 5545 tests OK (skipped 78), 2789 s. The two failures from
  the 9b23173 run are gone on main: the stale test_local_provider row (LP2); the HB3b comment is still on its unmerged lane.

## 2026-11-12 (evening): safe-search abandoned, HB3b adopted, HB3c merged; HB5b and SN1 started
- safe-search abandoned on the owner's word ("Abandon lane safe-search"): `tp-agent lane remove --abandon safe-search` after
  collecting its worker. Tip 8cc889c (restore: `git branch lane/safe-search 8cc889c`).
- HB3b adopted on the owner's word ("adopt HB3b"): `tp-agent lane close hub-packet HB3b` passed; merged b21a31c; decision note
  c8deb43. Pre-push full run on c8deb43 started (the owner: "push changes").
- HB3c (lane hub-rules-review, HB3b follow-ups 1, 3, 4; Sonnet, coder-c0fef53014): reviewers get the owner's active in-scope
  rules (`work_brief.rules_for_task`, `bin/tp-review --rules-file`); the stale launch_record.py comment; tests/session_env.py
  points TP_WORK_DIR at an empty temp store by default. Cells 230 x 4 OK (preview bde7a47). review-coder-c0fef53014-160954
  APPROVED_WITH_NOTES (F1 isolation regression coverage, F2 +20 s in test_lane_writer, F3 canary proof, F4 Main's cells not
  re-verified). F3 checked by Main: the one flagged file in test_orchestration (base and lane alike) was the canary's own
  sitecustomize .pyc; control row True. Lane close passed; merged 4866ff1. Needs a full run (session_env reaches every module).
- HB6 measure on a live lane with HB6b1's records (timeline.py, coder-c0fef53014): 21m48s, 0% unknown. tool 11m02s (one 10m03s
  shell call: the canary script running 24 module runs), test 4m12s, waiting:main 3m34s, model 2m58s.
- PF1a proposal 6 (fewer tp-agent starts): measured 0.06 s per `tp-agent --help`, of which python start 0.01 s and imports
  ~0.025 s (autonomy/contract 7 ms, work_commands 4 ms, argparse 3 ms). Lazy imports save at most ~0.03 s per call (~2% of a
  parallel full run, estimated); medium risk across every tp-agent command. Parked: not worth a lane now.
- HB5b (lane hub-main-runs, brief hub/hb5/hb5b-brief.md, Sonnet coder-56c0c62fe6): Main's runs recorded through test-run.sh
  with the interpreter, read by repeats.py as role main.
- SN1 (lane small-notes, brief fixes/sn1-brief.md, Sonnet coder-21c3cc49a8): lib/bench/runner.py result readers tolerate
  non-UTF-8 (TR2 note); HB8b stale comments in tests/test_episode_harness.py.
- Pushed c8deb43 on the owner's word ("push changes") after a full run on it: 5548 tests OK (skipped 78), 2982 s; origin had
  nothing new (837c31c..c8deb43). HB3c 4866ff1 and later merges stay local until a batched full run and a new owner word.
- SN1 round 1 (a3c0424): F1, the transcript-path reader (:281) had no row. Round 2 (coder-e36c24e8f3, d53cf0f) cells 79 x 4 OK;
  review-coder-e36c24e8f3-162958 APPROVED_WITH_NOTES; merged 4cb62e1.
- FL1 (lane episode-flake, Sonnet coder-b0a7770546): SN1's py39-out cell failed test_fake_pass_plays_scores_and_tears_down
  once under load (main: 0 of 20). Origin: the test's oracle said 0 when the scorer says unknown (claim strictly between two
  polls, result write time known only to the whole second). The oracle now states the scorer's rule; the scorer is unchanged
  (owner option A2). Cells 136 x 4 OK; review-coder-b0a7770546-163623 APPROVED_WITH_NOTES; merged 76ee86f.
- HB5b round 1 (0fd8e0e) REQUEST_CHANGES: Main reruns undercounted, relative PATH resolved against the caller's cwd, no real
  overlap in the concurrency test. Round 2 (ee7cbef) fixed F2/F3; F1 recurred: equal end times broke by task name, so Main was
  charged a rerun on a tie. Round 3 rule-first (hb5b-round3.md): strictly earlier end time only, coder-5243a36008.
