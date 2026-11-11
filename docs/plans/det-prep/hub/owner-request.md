# Owner request: coordinated improvement workstream (pasted 2026-11-11, verbatim)

Add a coordinated improvement workstream to the existing tmux-pi work queue. Reconcile it with work already completed, underway, or queued; merge overlapping items and preserve active lanes. Execute it through the existing queue and integration process.

The goal is to make tmux-pi a dependable personal software development hub. Most implementation and evaluation should happen on this personal Mac before transferring changes to the work Mac. Reserve work-Mac validation for environment differences, work-only integrations, and real usage.

The recurring problems are that Main needs reminders to give workers the appropriate safe_grep/safe_read guidance, run relevant evaluation tools, and cancel a worker's pending input prompt before sending a corrective instruction. It also sometimes loses the purpose and operating expectations of the ongoing work.

Optimize for correct outcomes, fewer repeated reminders, reliable recovery, and less active supervision. Token savings are secondary. Preserve flexibility in models and implementation strategy while making operating capabilities and durable state dependable.

1. **Audit the current local state and establish a baseline.**

   A previous static review examined GitHub commit `5ac86c6a430ad313a344e47c18698f078766aa4f`. Treat its findings as investigation leads, not confirmed defects in the current checkout:

   - `agent/roles/coder.md` explicitly recommended `grep -rn`, while standing guidance allowed either ordinary Read or safe_read.
   - `lib/agent_tools.py` omitted its entire Tools block, including standing instructions, when the optional project manifest was absent.
   - `docs/persistent-work.md` described decision-recording instructions that were missing from the active Main prompt.
   - Main loaded generic orchestrator instructions and a provider overlay, while project preambles and newer context tools primarily served workers.
   - Worker messaging and watchdog Escape handling existed separately, without a verified cancel-and-redirect operation.
   - Pause state could be recorded before successful interruption, and notification identity used task/status rather than individual request identity.
   - `tp-bench` directly exercised workers rather than Main managing a continuing session.
   - Some telemetry coverage differed between Claude/Pi and between ordinary and safe tool names.

   Inspect the current queue, launch paths, effective prompts, hooks, tool registrations, installed versions, local configuration, and representative transcripts. Include every delegation route actually used, including native subagents if applicable. Identify which findings remain relevant.

   Capture reproducible examples and a baseline before changing behavior. Distinguish observed failures, source-confirmed gaps, and hypotheses.

2. **Make instruction and capability delivery consistent and observable.**

   Define the preferred reading/searching behavior once and reconcile conflicting role instructions, examples, tool descriptions, and retry guidance. Account for actual tool availability and legitimate fallbacks.

   Always deliver the small universal operating contract. Keep optional project tool registration separate so a missing manifest cannot silently remove basic guidance.

   Record the effective launch package: instruction versions, relevant preferences, exposed tools, capabilities, role, and task context. Verify delivery through real dispatch, retry, and recovery paths.

   Keep shared workflow behavior consistent across providers. Provider adapters should describe mechanics and supported capabilities. Make unsupported capabilities visible.

   Expected improvement: fewer tool-choice reminders and a clear explanation when a rule was unavailable, missing, contradictory, or ignored.

3. **Complete the durable-memory lifecycle and give Main a current working brief.**

   Reuse existing workspaces, decisions, task records, checkpoints, and context knowledge. Identify their authoritative responsibilities before adding storage.

   Distinguish persistent operating preferences, project purpose/design decisions, current task intent, and temporary pending obligations. Preserve scope, provenance, and supersession.

   Give Main a compact working brief containing the current outcome, acceptance conditions, relevant preferences, active work, unresolved decisions, pending checks, and pointers to supporting knowledge. Reconstruct it at startup, restart, compaction recovery, and meaningful transitions.

   Record explicit decisions and corrections when they occur. A checkpoint cannot recover something never written. Preserve the user's meaning; keep inferred preferences provisional.

   Deliver relevant intentions when they become actionable: worker guidance at dispatch, outstanding evidence near completion, and unresolved interventions during recovery. Provide Main with an appropriate knowledge lookup path. Surface useful lane-local knowledge before cleanup.

   Support continuity across research, product decisions, implementation, review, and follow-through. Reuse the existing product-design traceability where useful without requiring a product-design package for ordinary fixes.

   Expected improvement: a correction remains effective later, and returning to a session does not require reconstructing its purpose.

4. **Implement and evaluate a supported worker correction operation.**

   Investigate a state-aware cancel-and-redirect operation; its command name is an implementation choice.

   It should identify the current interaction, cancel the pending action where supported, verify readiness, deliver the correction, confirm receipt, and report whether execution resumed. Handle ordinary input, permission prompts, busy tools, queued messages, completed workers, and unknown states explicitly.

   Cancellation must not grant permission or bypass a genuine access denial. Prefer supported runtime interfaces where available; verify installed-version behavior and document limitations of terminal fallbacks.

   Separate requested, confirmed, failed, and unverified actions. Preserve dependency holds when interruption is uncertain without falsely claiming the worker stopped.

   Give requests/events stable identities. Separate message delivery from resolution of a particular request. Address repeated input requests between polling intervals and restart during intervention. Bound retries and prevent duplicate corrections or accidental loss of queued instructions.

   Expected improvement: Main can perform routine corrections reliably without requiring the user to remember terminal procedures.

5. **Connect applicable verification to the work item.**

   Extend the existing tool/evidence machinery with applicability, purpose, required versus advisory status, expected evidence, and execution cost.

   Distinguish project checks, such as Fallow and targeted property tests, from harness evaluations for changes to prompts, routing, memory, or worker control.

   Track relevant obligations as pending, satisfied, inapplicable, or deferred with a reason. Surface unresolved obligations before declaring completion. Keep advisory findings available for judgment rather than automatically accepting every recommendation.

   Reuse valid existing results using the project's freshness rules. Record enough revision, configuration, and tool provenance to reject stale evidence. Avoid running every available tool or repeatedly running expensive suites.

   Expected improvement: applicable checks happen without reminders, while irrelevant checks and redundant verification decrease.

6. **Measure waiting and supervision through the existing state machinery.**

   Extend current task/lane state with a small structured event journal and dependency relationships. Begin with existing local storage and reporting.

   Capture enough information to distinguish tool/test execution, waiting for workers or review, waiting for Main to act, human decisions, recovery, and unknown time. Normalize observations across supported harnesses and tool names. Missing telemetry must remain unknown rather than becoming zero.

   Report end-to-end and critical-path delays without adding concurrent durations together. Do not infer model reasoning merely from a lack of tool activity.

   Make Main and the user able to see the current objective, meaningful progress, actual blocker, next valid action, and required decision.

   Expected improvement: we can determine whether the limiting factor is tests, another lane, model/runtime execution, or orchestration delay.

7. **Turn recurring corrections into scoped improvements and test whether guidance remains useful.**

   For recurring friction, retain the trigger, correction, successful action, scope, and supporting evidence. Decide whether the durable fix belongs in instructions, a tool default, an adapter, or a small retrieved procedure.

   Explicit user preferences can become active within their stated scope. Inferred lessons remain candidates. Avoid allowing incidental worker text to become authoritative policy.

   Evaluate both adding guidance and removing obsolete guidance. Keep operational guarantees stable while allowing newer models to use simpler planning and reasoning workflows.

   Expected improvement: the same mistake becomes less likely without accumulating an ever-growing compulsory prompt.

8. **Add evaluations of complete working episodes through Main's normal entry point.**

   Preserve useful worker benchmarks. Add coverage for Main carrying work across multiple steps with state retained:

   | Episode | Required evidence |
   |---|---|
   | Normal delegated change | Appropriate brief/tools, correct behavior, relevant verification |
   | Worker needs correction | State recognized, intervention delivered, recovery verified |
   | Check obligation | Relevant check performed or justified disposition recorded |
   | Compaction/restart | Intent, decisions, pending work, and next action survive |
   | Correction recurs later | Correct behavior without another reminder; no unrelated scope leakage |
   | Concurrent dependent lanes | Accurate blocker attribution and timely orchestration |
   | Control/event failure | No falsely confirmed pause, missed distinct request, or duplicate correction |

   Use deterministic checks for instruction delivery, state transitions, request identity, and evidence freshness. Use actual supported runtimes for integration behavior and human assessment for semantic task success.

   Compare baseline and candidate configurations with controlled fixtures and repeated runs, then held-out variations. Treat small pilots as diagnostic evidence, not statistical proof. Include negative cases where a reminder, tool, or intervention should not apply.

   Primary measures are correctly completed work, human process corrections per completed episode, recurrence after correction, recovery success/time, avoidable waiting, false completion claims, irrelevant checks, and stale or redundant verification. Separate legitimate product decisions from process reminders. Tool invocation alone does not establish success.

For each queue item, record the problem, hypothesis, dependencies, implementation scope, expected benefit, evaluation method, evidence location, and adopt/revise/defer decision. Prefer small integrated changes that can be attributed and reversed. Coordinate shared instruction, memory, and event interfaces before parallel implementation.

Before transferring to the work Mac, produce a reproducible handoff containing:

- Exact commit and supported runtime/configuration versions.
- Baseline and candidate results, including unresolved failures.
- Portable configuration changes and migration steps.
- Executable smoke checks for dispatch, safe-tool guidance, correction, memory recovery, and evidence reporting.
- Known limitations and a short work-Mac validation checklist.

Use personal-Mac fixtures to exercise as much real behavior as possible. Simulated GitLab or other work-only interactions can validate local contracts, but must be labelled as simulated; actual integration remains a work-Mac check. Keep machine-specific credentials and settings separate from portable changes.

Prioritize consistent instruction delivery, durable decisions, and verified worker correction, with episode evaluations developed alongside them. Then improve bottleneck reporting and broader learning.

The target acceptance experience is: I provide a goal, correct the process once if necessary, leave and return, and receive current completion evidence without repeatedly reminding Main how to operate.
