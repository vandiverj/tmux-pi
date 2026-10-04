# Agent Development Environment — Product Requirements

**Status:** Implementation baseline; acceptance validation in progress  
**Date:** 2026-09-30  
**Owner:** Jerrick  
**Basis:** [Architecture Sketch](Agent%20Development%20Environment%20%E2%80%94%20Architecture%20Sketch.md), project inventory, and the 2026-09-30 direction to favor capable agent judgment over prescribed process.

**Implementation note (2026-09-30):** The repository now starts either main harness, dispatches role-aware tasks through `tp-agent`, records task artifacts and a session index, applies concurrency limits and visible fallbacks, and asks before supported sensitive reads. A Pi main and Pi worker have been exercised in live tmux sessions; the full repository suite passed 232 tests, with later focused orchestration and status checks passing. Claude main reached the isolated profile's first-use login screen, so its authenticated worker path still needs a user sign-in and live acceptance run. The sensitive-read hook covers built-in reads and common shell paths, but arbitrary computed shell paths remain outside its enforcement boundary. The outcome comparison in acceptance criterion 8 also remains to be collected from real project use. These are validation gaps, not changes to the requirements below.

## Product definition

A portable, local tmux workspace in which a main agent coordinates visible specialist agents, chooses an appropriate model and harness for each task, and keeps enough durable context to complete software work without repeatedly fighting the workflow.

This is a solo development environment for personal and work projects. Its purpose is to improve the quality and throughput of real development work while controlling model spend. It is not a process enforcement system.

## Problem

The current workspace succeeds at showing and controlling live subagents, but its control plane grew into fixed contracts, preflight steps, capability gates, and mandatory review paths. Those mechanisms can consume a turn before useful work starts, block ordinary work, and make the main agent manage ceremony rather than outcomes. Workers are generic model sessions rather than defined roles, the main experience is tied to Pi, and useful findings can consume the main agent's context when gathered back into it. A model router exists, but its current measurements do not establish that it improves completed work.

The replacement must make it easy to start the right work, see what is happening, correct it, and finish it. Agent decisions should be visible and revisable, without requiring the user to spell out a workflow or model choice for every request.

## Goals

1. **Get useful work done with low friction.** A normal request can move from intent to action without a mandatory contract, stage sequence, or approval ritual.
2. **Delegate by task need.** The main agent decides which specialist role, if any, should work next; it can choose sequential or parallel work based on dependencies.
3. **Route by expected value.** Select model tier and effort per dispatch using task context, cost, capability, latency, and availability; recover when the classifier or a preferred model is unavailable.
4. **Keep the user in control.** The user can see active agents, inspect their task and output, enter a live session, grant required permissions, and steer the overall work through the main agent.
5. **Preserve useful context.** Task artifacts and an indexed session state survive compaction. The main agent reads detailed work only when a decision requires it.
6. **Work through either main harness.** Pi and Claude Code can each run the same orchestrator behavior and dispatch the same worker roles through repository-owned interfaces.
7. **Stay portable and private.** Shared configuration lives in the repository; session artifacts and telemetry remain local, and work-project data is not sent to an observability service.

## Non-goals

- Reinstating the strict dispatcher, universal preflight, contract handshake, capability registry, automation cells, or goal mode as prerequisites to ordinary work.
- A fixed sequence of research, design, coding, testing, and review for every task.
- A new terminal multiplexer, web interface, or custom TUI replacing tmux.
- Training a delegation classifier in the first release. The main agent chooses roles.
- Automatically training models on work-project prompts or exporting work telemetry.
- Making cooperative agent instructions equivalent to an operating-system security sandbox.
- Mandating cross-provider review on every task.

## Users and workflows

The primary user is Jerrick, working alone across machines and repositories. A worker may use local or remote model inference, subject to the selected harness and the data boundary below.

### W1. Start and orient

The user starts the workspace in a project and chooses Pi or Claude Code for the main agent. The familiar main, status, and viewer layout appears. The main agent can read the current project and any indexed session state, explain what it understands, answer a simple question directly, and start useful work without requiring a process declaration.

### W2. Delegate work

The user asks for a change, investigation, review, or explanation. The main agent identifies the needed outcome and chooses a role. It sends a focused task packet containing the assignment, relevant context, constraints, and expected deliverable. Routing recommends tier and effort; harness selection accounts for model access and availability. The user can see the choice and its reason in the task record.

### W3. Observe and intervene

The status pane shows each task's role, harness, model, current activity, state, elapsed time, and deliverable location. The user can select a task, inspect its packet and result, or enter its live terminal for a permission prompt or observation. A material change in assignment goes through the main agent and updates its task state. Direct terminal interaction remains possible and is visible as an intervention.

### W4. Complete and continue

A worker writes its deliverable and a concise return. The main agent uses the return to decide whether to present the result, inspect evidence, run a test, request a review, revise the assignment, or ask the user for a decision. It does not need to ingest an entire transcript to continue. The user receives a clear account of what changed, how it was checked, and what remains uncertain.

### W5. Recover from trouble

If routing, a model, a tool, or a worker fails, the main agent identifies the failure type and chooses a proportionate next step: wait, retry with better instructions or tooling, switch model or harness, narrow the task, or ask for missing authority. It preserves partial work and does not silently treat an empty or failed result as success.

## Functional requirements

Priority labels: **P0** is required for a usable first release; **P1** can follow once the core works. A requirement describes observable behavior, not a compulsory stage for every task.

### Orchestration and roles

| ID | Priority | Requirement |
| --- | --- | --- |
| OR-1 | P0 | The main agent has an explicit orchestrator instruction set shared between Pi and Claude Code. It chooses tasks, roles, dependencies, and follow-up actions using the current request and task state. Harness overlays describe invocation mechanics only. |
| OR-2 | P0 | A worker role is independent of harness and model. Dispatch records role, harness, model, effort, assignment, workdir, and expected deliverable. |
| OR-3 | P0 | Role definitions give workers a purpose, expected output, and guidance for reporting uncertainty or failure. The initial usable roles are coder, researcher, code reviewer, and tester. This MVP roster is proposed for review; the role interface must allow the other roles in the sketch without changing dispatch semantics. |
| OR-4 | P0 | The main agent answers simple requests directly when they need no major work or research. It delegates substantial investigation, editing, testing, review, or architectural analysis to an appropriate worker. It judges the boundary from the task rather than a fixed length or keyword rule. |
| OR-5 | P0 | No role, review, test, or architecture step is automatically mandatory for every request. The main agent can explain why it called one, skipped one, or deferred one. |
| OR-6 | P1 | The roster can add vulnerability, PII/data-flow, architect, and infrastructure roles with role-specific outputs. Review specialties remain separate reading modes. |
| OR-7 | P0 | A worker's understood assignment is made visible early enough for the main agent or user to correct a misunderstanding before significant work proceeds. This need not be a blocking handshake. |

### Routing and resource use

| ID | Priority | Requirement |
| --- | --- | --- |
| RT-1 | P0 | The main agent selects the role. A routing component recommends model tier and effort for the dispatch using the worker's assignment/context, rather than requiring the user to label the task. |
| RT-2 | P0 | The main agent selects a compatible harness considering the recommended tier, available models, subscription/API cost, tool capability, and queue pressure. Role and harness are recorded as separate decisions. |
| RT-3 | P0 | If classification is unavailable or produces an invalid choice, dispatch remains possible through a visible main-agent choice. Failure does not trigger a repeated prompt loop. |
| RT-4 | P0 | Local-model and total active-worker limits are configurable starting policies, not universal task rules. When a slot is full, the main agent can wait or route to a suitable available model and records the reason. |
| RT-5 | P0 | Each dispatch records recommendation, actual selection, fallback or override reason, model/harness versions where available, and outcome data sufficient to evaluate usefulness, cost, latency, and retries. Logs omit task text by default. |
| RT-6 | P1 | Compare routing with and without role type as an input. Re-evaluate the classifier against real dispatch packets; the existing request-based holdout score is a historical baseline, not an acceptance target for the new input. |

### Task context, artifacts, and returns

| ID | Priority | Requirement |
| --- | --- | --- |
| CT-1 | P0 | A task packet contains the assignment, selected files or ranges, notes and constraints, expected output, and role instructions. It stays inspectable before and after dispatch. |
| CT-2 | P0 | Each task has a local record with identity, parent/dependency links where relevant, role, harness/model, assignment, state, deliverables, result summary, and pointers to detailed artifacts or transcript. |
| CT-3 | P0 | The main agent maintains a short session index: current objective, active tasks, pending decisions, and artifact pointers. It can reconstruct its working state after compaction or harness restart without rereading every transcript. |
| CT-4 | P0 | A worker writes or identifies its deliverable and returns a concise, role-appropriate result. A code result includes changed paths and verification outcome; a research result identifies its findings artifact and evidence limits; a search result may return small path/line/snippet results directly. |
| CT-5 | P0 | Returns distinguish completed work, verified empty results, incomplete/blocked work, and uncertainty. A completion claim includes enough evidence for the main agent to decide whether further inspection is needed. |
| CT-6 | P0 | Detailed transcripts and artifacts remain local and retrievable through the task record. The task record must point to the actual transcript location; it need not duplicate a harness-native transcript. |
| CT-7 | P0 | Project decisions and longer-lived direction are stored separately from transient session state. Starting a new session does not inherit stale outage flags or unfinished permission assumptions. |

### tmux interface and harnesses

| ID | Priority | Requirement |
| --- | --- | --- |
| UI-1 | P0 | Preserve the main pane, live status pane, and selected-worker viewer. A worker remains a real terminal session that the user can enter while it runs. |
| UI-2 | P0 | The status pane clearly distinguishes queued, working, waiting for input/permission, settled, failed, and stopped tasks; it shows the selected task's deliverable and next needed action. |
| UI-3 | P0 | The user can inspect a task packet and result, switch the viewer, enter the selected worker, stop a worker, and return to the main agent without losing the task record. |
| UI-4 | P0 | Both Pi and Claude Code can be selected as the main harness at startup and use the same repository-owned dispatch, task-state, and collection interfaces. |
| UI-5 | P0 | Configuration loaded by either harness is limited to the intended repository configuration plus a dedicated per-machine authentication/trust location. The main harness does not accidentally inherit unrelated global skills, hooks, plugins, or project instructions. |
| UI-6 | P0 | The workspace can recover its layout and reconnect to existing task state after a terminal detach, main-agent restart, or compaction. Active-task supervision has an explicit recovery path. |

### Authorization and data boundaries

| ID | Priority | Requirement |
| --- | --- | --- |
| AU-1 | P0 | Supported worker read paths pause before exposing a file likely to contain credentials or sensitive configuration until the user authorizes that read through the main agent. The request identifies the file and the minimum keys or information needed. Ordinary file reads do not trigger this prompt. |
| AU-2 | P0 | A permission request pauses only the dependent action. Unrelated authorized work may continue. A denial is recorded and the worker seeks a non-sensitive path when possible. |
| AU-3 | P0 | A worker that discovers it has crossed an authorization boundary stops that action and surfaces the event to the user; it does not retry through a different route. |
| AU-4 | P0 | Skills and role instructions do not themselves grant file, tool, network, or external-service authority. A role has a small baseline instruction/skill set; the main agent may add task-relevant material, and a worker may request more. |
| AU-5 | P0 | Traces, prompts, task records, and logs are kept locally. Remote model inference receives the task content required by that call; the product does not send telemetry to a third-party observability service. The UI makes a remote-model choice visible. |
| AU-6 | P0 | The product documents which sensitive-read paths are enforced and which remain cooperative. A common read path that can bypass AU-1 is a release blocker until restricted or explicitly removed from worker access. The product does not claim operating-system isolation it lacks. |

### Failure handling and review

| ID | Priority | Requirement |
| --- | --- | --- |
| FH-1 | P0 | The main agent distinguishes execution trouble, misunderstanding, a valid empty result, missing authority, and boundary violation when evidence permits. Unknown cause is reported as unknown. |
| FH-2 | P0 | A retry or reroute preserves the previous assignment, partial artifact, and reason for the new attempt. It does not silently overwrite evidence or launch unbounded repeated attempts. |
| FH-3 | P0 | A code review can be requested as an independent role. The main agent may use a different provider to review consequential work; review is a judgment call rather than a universal gate. |
| FH-4 | P0 | A settled task is not treated as correct solely because a worker reported completion. The main agent can inspect changed paths, tests, diff, or a second opinion according to task risk and uncertainty. |
| FH-5 | P0 | If a required dependency is unavailable, the main agent explains the degraded route and proceeds where an authorized useful path exists. Permission denials and boundary violations are not overridden by fallback behavior. |

## Minimum shared interface contract

Both main harnesses need one repository-owned, machine-readable interface for `dispatch`, `list`, `inspect`, `message`, `collect`, `stop`, and `resolve-permission`. Thin Pi and Claude adapters may present these as native tools or shell commands, but they must read and write the same task records. The interface must not require the old strict-mode handshake. Detailed command names and schema versions belong in a follow-on contract specification.

A dispatch request minimally identifies the role, assignment, workdir, selected context, expected deliverable, chosen harness/model/effort, and optional parent/dependency links. The resulting task ID and record are available before the worker begins. Role definitions specify their baseline instructions and result shape; they do not imply tool authority. A worker result minimally gives status, understood assignment, artifact pointers, evidence or verification, and unresolved issues. A result may be concise, but an empty claim of completion is insufficient for a coding task.

The shared lifecycle has distinguishable **queued**, **starting**, **working**, **needs input**, **settled**, **completed**, **failed**, and **stopped** states. `Settled` means the worker has returned a result for the current assignment; `completed` means the main agent has collected or handed it off. A new assignment or authorized follow-up makes a new observable work attempt. Process exit, permission wait, and a genuine empty finding must not all render as the same state. State changes and artifact writes must be recoverable after a process restart.

## Outcome measures

Quality takes priority over classifier match rate or raw delegation count. For each representative task, record whether the requested outcome was met, whether the main agent caught scope or correctness errors, what verification supports the result, how much user repair was needed, and whether the workflow itself blocked progress. Also record elapsed time, model usage/cost when available, number of retries/reroutes, and main-agent context pressure. The comparison set should include tasks that previously triggered the contract machinery, as well as ordinary new work. The first release is accepted on observed task outcomes and usability, not on a target number of agents spawned.

## Acceptance criteria

The first release is usable when the following can be demonstrated in a project checkout without invoking the old strict-mode flow:

1. Start the same three-pane workspace with Pi as main, then with Claude Code as main. From either, assign a coder and a researcher task using the same dispatch interface and inspect both in the viewer.
2. Dispatch a role on a local model and on a remote model. The task record shows the role, harness, routing recommendation, actual model/effort, and reason for a fallback or override.
3. Run two independent tasks in parallel, hold a dependent task until its input is available, and show a clear queued/waiting state when a concurrency limit is reached.
4. Complete a small code change. The main agent can report the changed paths, verification result, and uncertainty from the worker's concise return and task record, opening details only when needed.
5. Demonstrate a misunderstood task, an unavailable classifier, and an unavailable preferred worker. The main agent preserves evidence and takes a useful, visible recovery action without a mandatory new contract.
6. Request access to a credential-bearing config file through each supported read path. No file bytes reach the worker before authorization; denial is honored and recorded. Test common shell and search routes as well as built-in read tools, and document any inaccessible route or remaining boundary limit.
7. Compact or restart the main agent. It can resume from the session index and task records, including an active task and a finished artifact, without loading all transcripts.
8. Run a representative set of real development tasks spanning coding, investigation, testing, and review. Record outcome quality, user interventions, elapsed time, model cost/usage where available, and any work blocked by the harness. Compare with the previous workflow before declaring the replacement successful; set numeric targets after collecting a baseline.

## Validation expectations

- Test repository-owned task/dispatch/state interfaces with both harness adapters and simulated process failures.
- Exercise the tmux workflow in a live session; fake process tests alone do not prove the interactive viewer or permission flow.
- Review a sample of real task artifacts for correct assignment understanding, truthful empty/blocked results, and concise but useful returns.
- Verify that local logs contain no unintended remote telemetry endpoint, and verify the actual behavior of the credential ask-first mechanism at its enforcement boundary.
- Inspect cost, time, completion, and intervention records. A classifier match score alone is not a product-quality measure.

## Existing assets and dependencies

The existing tmux layout, viewer, worker launch script, reporters, local task files, read-only local assistant, cross-provider review command, and router/calibration corpus are assets to evaluate and reuse where they satisfy these requirements. They are not requirements on the replacement architecture. The strict dispatcher, preflight/capability/automation stack, and goal mode are historical evidence of friction and should not determine the new user workflow.

The first release depends on installed tmux, at least one available main harness, a working worker harness, repository-local configuration, and local storage for task records. Local models and the classifier are optional at runtime because documented fallbacks exist. Remote inference uses the selected provider's normal model service; subscription and API access remain a per-machine concern.

## Risks and open decisions

1. **MVP roles.** Confirm whether coder, researcher, code reviewer, and tester are the initial set; the eight-role sketch remains the intended roster to test over time.
2. **Credential enforcement.** Specify the smallest workable mechanism for identifying and pausing sensitive reads in both harnesses. Cooperative prompts alone cannot make a hard guarantee.
3. **Claude configuration and trust.** Validate the proposed dedicated config location against actual CLI behavior before treating trust persistence and isolation as implemented.
4. **Transcript portability.** Identify where each harness stores transcripts and how task records retain durable pointers without copying unnecessary sensitive content.
5. **Review judgment.** Define examples where an independent or cross-provider review is worth its cost, without turning the examples into a mandatory classification table.
6. **Router evidence.** Gather real packet-level outcomes before deciding whether the learned layer beats a simple heuristic or main-agent choice.

## Proposed release boundary

The first release covers the common orchestrator role, selectable main harness, role-aware worker dispatch, task records and session index, live tmux controls, visible routing/fallback, concise returns, credential ask-first path, and recovery from ordinary failure. It can launch with a small role roster and grow it from actual use. Special-purpose research contracts, automatic escalation chains, broad capability catalogs, goal continuation, and tracing UI are outside that release.

## Related material

- [Architecture Sketch](Agent%20Development%20Environment%20%E2%80%94%20Architecture%20Sketch.md): intended principles and proposed specialist roster.
- [Project Map](project-map.md): inventory of the current implementation and lessons; not a target specification.
- [README](../README.md): current user-facing behavior, to be rewritten as the replacement takes shape.
