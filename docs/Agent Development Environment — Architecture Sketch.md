# Agent Development Environment — Architecture Sketch

Sep 30, 2026 · @Jerrick

## Constraints and principles

This is a solo setup. Configuration lives in a repository so it can move between the machines you work on, which rules out anything carrying heavy per-machine state.

Telemetry stays local. Traces, prompts and logs must not land on a third-party observability service, because the setup has to be usable for work as well as personal projects. Model inference itself goes out over the wire as normal — that distinction matters, and it is the only thing that crosses the boundary.

Cost is a live constraint. You are already spending over $200 a month on model access. Since April 2026 Anthropic has not permitted Claude Pro or Max subscription credentials to drive third-party harnesses, so reaching Claude models from anything other than Claude Code means paying API rates on top of the subscription. This is the single fact that shapes the harness decision more than any feature comparison.

### Two lessons already paid for

**Do not front-load the quality gates.** An earlier iteration encoded quality and security checks up front. They did not merely add friction — they blocked work entirely. Guardrails are added once there is something worth guarding, not before.

**Do not encode fixed steps.** Mandatory sequences assume every task has the same shape, and development work does not. This is thought work, not a production line. The orchestrator selects from an available roster based on the task in front of it; nothing is a compulsory stage. Current models are capable enough to make that choice, which was not true when pipeline-style designs became common.

**Degrade, never halt.** When something the orchestrator relies on is unavailable, it carries on with a worse version rather than stopping. The classifier is down, so it picks the tier itself. The local slots are full, so it waits or routes to a frontier model. This is the direct inverse of the gates that made the last iteration unusable: a gate stops everything when a condition is not met, a fallback keeps going less well. Degraded work you can see and correct beats no work at all.

## Architecture at a glance

&#91;embedded content: agent architecture · orchestrator, classifier, 8 subagents\]

The orchestrator decides what kind of work is needed; the classifier, sitting below it, decides how much horsepower that work gets. Because the classifier reads a machine-generated context payload rather than your phrasing, you are not asked to label your own tasks.

## The orchestrator

The main agent is a distinct role with its own specification, not simply a subagent that happens to run first. Its job is to decide what kind of work comes next and dispatch it.

It is explicitly prohibited from doing subagent work itself. Left unconstrained, an orchestrator that hits a subagent failure tends to take over and finish the job — which defeats the architecture and consumes exactly the context the design was protecting.

### Surviving compaction

The orchestrator's real enemy is context compaction. Compaction is lossy in ways that are hard to predict, and what tends to disappear is not the record of a decision but the reasoning behind it.

The answer is that the orchestrator holds as little as possible. Analysis and architectural thinking go to subagents whose output is written to the git-ignored artifact folder already used for session artifacts. That folder is the orchestrator's memory; compaction stops being catastrophic because the ledger survives it.

Re-reading the whole ledger after a compaction recreates the original problem, so the orchestrator holds an index rather than contents: a short resident state file naming the current task, what is done, what is pending, and pointers to the detailed artifacts. Full artifacts are opened only when that specific one is needed.

## The classifier

A small trained classifier — BERT-style — decides model tier and effort level. It does not decide which subagent handles the work; the orchestrator does that.

This split matters. An earlier framing had the classifier sitting in front of everything, reading how you phrased a request. That version has a circularity problem: if the signal lives in your phrasing, you are doing the classification yourself, and the whole point was to avoid typing "delegate to coding subagent" before every task.

Putting the classifier below the orchestrator dissolves that. Its input is the context payload the orchestrator is about to send down — machine-generated, not authored by you. It answers a narrower question: given this payload heading to this kind of subagent, what tier and effort does it warrant? The result is that the coding subagent is not permanently pinned to one model; the decision is made per dispatch.

### Why the orchestrator keeps the routing decision

Delegation choice stays with the main agent for now. It already holds conversational context, and models shift often enough that a classifier trained on delegation patterns would need retraining every time the landscape moves. Tier routing is the more stable target. Learned delegation is a later optimisation, once the patterns are settled enough to be worth capturing.

### The training data problem

There is already a corpus, which changes the starting point. A 300-task labelled set exists, with Laya's recorded answers, split into 180 tuning tasks and 120 holdout. The learned layer measures roughly 55% tier match and 66% model match on holdout. That is a real baseline to improve on rather than a cold start, and the labels are one person's judgement, which is a known limit on how far it can go.

Work tasks still cannot leave the work machine, so the corpus stays drawn from personal-project work — a thinner and differently shaped stream than the full range of tasks the system will see.

That is survivable because the target is task *shape*, not task *content*: what counts as a review, a piece of exploratory research, a mechanical edit. That distinction transfers across domains far better than anything content-specific.

The honest first build is crude on purpose: route, log, retry, and watch what the retries say. The cost of a misroute cannot be specified before the system exists — it defines itself from that data.

## Subagent roster

A starting set, not a fixed roster. These are the roles that recur across most software work; which ones actually earn their keep is something the build will reveal.

| Subagent | Role |
| --- | --- |
| Coder | Writes and edits code. |
| Researcher | Reads documentation, searches, gathers context. Likely a cheap tier most of the time. |
| Code reviewer | Does this work? Correctness, structure, maintainability. |
| Tester | Runs the suite, interprets failures. |
| Vulnerability reviewer | Is this exploitable? Reads code for security flaws. |
| PII / data-flow reviewer | What leaks where? Reads data movement: logging, boundaries, traces. |
| Architect | Called when a task warrants design thinking, not on every flow. |
| Infrastructure | Called when a task warrants it. |

The two review roles are deliberately separate. "Does this work" and "is this exploitable" are different reading modes, and vulnerability review and data-flow review are different again — the first reads code for exploitable flaws, the second follows data across boundaries. Given that this setup keeps telemetry local partly for that reason, it deserves its own pass.

Architect and infrastructure sit outside the default set. The orchestrator calls them when the work warrants it, which keeps them available rather than mandatory — the same instinct as not front-loading the gates.

Most of the work this will handle is brownfield, against existing systems, not greenfield. Roles that assume a blank slate are worth less here than roles that can read what already exists.

### Role and harness are independent

A role is not a harness. The coder is not "Claude Code" — a coder subagent could be GPT or a local model running through Pi just as easily. The same is true of every other role: Claude Code can be the researcher, the reviewer or the coder, because it is a harness that can wear any of them.

What makes Claude Code different is only how it is invoked. Other roles run as a model behind Pi; Claude Code has to be called as its own CLI, because that is the only way to reach Claude models on the subscription. So it is used for what it is good at, reached by a different mechanism than the rest.

The orchestrator therefore picks two things at dispatch: which role the work needs, and which harness that role runs in.

## What a subagent returns

Everything a subagent does is written down. Conversations are recorded to a transcript on disk as a matter of course, and findings land in the artifact folder. Nothing is lost, and any agent that needs the full detail later can go and read it.

What comes back up to the orchestrator is deliberately thin. If the orchestrator has to read everything the subagent did, the delegation bought nothing — the context it was protecting gets spent anyway. So the return carries what the orchestrator needs to decide the next move, and no more.

What that amounts to is situation-dependent:

- A researcher returns a pointer — the work is done, here is where the findings are stored. The orchestrator knows the information exists and can decide whether it actually needs to read it.
- A file search returns the results themselves, names and line numbers, because those *are* the answer and they are small.
- A coder returns something closer to a completion signal: the assigned task is done, enough for the orchestrator to move forward.

The orchestrator is not barred from reading a subagent's full output. The question is only whether it needs to, and the default answer is no.

### Who writes what

Subagents write their own transcripts and their own findings. The orchestrator writes the state file — its task list and its pointers — and little else. It is handling references, not content, which is what keeps it thin across a long session.

### Dropping into a running subagent

The third tmux window exists so you can enter a running subagent's session. In practice that is for approving permissions, watching that it is doing the right thing, and occasionally nudging it toward the right answer.

It is not for changing what the subagent was asked to do. The task stays what the orchestrator assigned, and the subagent's return is still the final word on it. Corrections, follow-up work and changes of direction go through the orchestrator, not through the subagent's window.

That distinction is what keeps the orchestrator's picture accurate. If tasks could be rewritten mid-flight from inside a session, the orchestrator's state file would quietly stop describing reality. Because steering does not change the assignment, it never goes stale.

### Concurrency, and two different ceilings

Subagents run in parallel — that is much of the point. But two separate limits apply, for entirely different reasons.

**A local-model cap, roughly two at once.** This is a hardware limit. Ten concurrent local models would simply overwhelm the machine. When the orchestrator wants another local subagent and both slots are busy, it has a genuine choice: wait for a slot, or send the work to a small capable frontier model instead if it needs the answer quickly. That is a routing decision, and it belongs with the classifier's tier logic rather than being a hard rule.

**A global cap, roughly four subagents at once.** This one is not about resources — thirty frontier calls would run fine. It is about coordination. Work happening in too many places at once duplicates effort and heads in directions that would have been avoided if the orchestrator had simply waited for an earlier result to come back. Past a point, more parallelism produces less accurate work, not faster work.

Both numbers are starting points, not findings. What matters architecturally is that the orchestrator understands waiting as a legitimate move — that a full slot table is a reason to hold, or to reroute, rather than a reason to push work out anyway.

### Two kinds of state: project and session

Project state is what you are building and what is left to do — the task list, the decisions made, the direction. It lives mostly in documentation and outlasts any individual sitting. Come back after a week and this is what you pick up.

Session state is what this particular run is doing and what it has learned about its own conditions. It is transient by design and does not survive into the next session.

The classifier's failure mode shows why the second exists. If the classifier is down, the orchestrator falls back to deciding tier itself — it is capable of that, just less consistently. After a few failed attempts it asks you to bring the classifier back up. If that does not happen, it sets a flag and stops asking, because an orchestrator that raises the same dead dependency every few minutes is worse than one that quietly carries on without it.

That flag is session state, not project state. It describes this run's conditions, not the work. A new session starts clean and tries the classifier again — which is the behaviour you want, since the usual reason a classifier is down is something you fixed between sittings.

## Harness and interface

### The harness is a slot, not a choice

The main window is not permanently Pi. Either CLI can sit in it: Claude Code when the work is Claude-flavoured and should draw on the subscription, Pi when the work wants a different model or a loop you can cut into. Both can dispatch the same subagents, so committing to one harness up front is a constraint the design does not actually need.

What makes that possible is that a subagent is not a harness feature. It is a tmux window running a command against a context payload. If dispatch is a script in the repository — take a role, take a payload, spawn the window — then Claude Code invokes it as a shell command and Pi invokes it as a shell command, and neither needs to know the other exists.

**This is the main structural change from what exists today.** Currently the main agent's behaviour lives in Pi extensions: the dispatcher, the subagent tools, the local-assistant mode. That ties the orchestrator to Pi. For Claude Code to sit in the main window as a first-class orchestrator, dispatch and the subagent tools have to move out of the extension layer and become repository scripts that either harness calls as ordinary commands. The extension becomes a thin binding to those scripts rather than the place the logic lives.

### The orchestrator prompt: common core, harness overlay

The dispatch script is the easy half. The hard half is the orchestrator's own behaviour: knowing to route rather than do the work, knowing to consult the classifier, knowing to keep its context thin. That is prompt, not code — which is what makes it portable, since a prompt is just text in the repository.

It is written in two layers rather than to a lowest common denominator, so that neither harness loses what it is actually good at.

**The core** holds everything about the role: the routing discipline, the prohibition on doing subagent work, the state file conventions, the roster and when to reach for each part of it. This is identical regardless of which CLI loads it.

**The overlay** is per-harness and carries mechanics only — how this particular CLI invokes the dispatch script, how it reads files, whatever it uniquely offers. Claude Code loads the core plus its overlay as a project instruction file; Pi loads the core plus its own overlay as a system prompt.

The discipline to hold: overlays stay thin and describe mechanics, never behaviour. The moment a harness fragment starts redefining how the orchestrator decides things, there are two orchestrators drifting apart, and the portability is gone.

### What this forces out of the harness and into the repository

Because either CLI may be driving, nothing that matters can live in harness-specific scaffolding. The state file, the artifact ledger conventions and the dispatch interface all become plain files in the repository that both agents read and write the same way.

This is a constraint worth welcoming — it was already the goal for portability across machines. Harness independence and machine independence turn out to be the same requirement.

### tmux

The interface already exists: three windows, holding the main agent, a list of available subagents, and a live session for the selected one that you can drop into and take over mid-run. That last capability is precisely what the Claude CLI does not offer, and it is hard to replicate.

Alternatives were weighed and rejected:

- **Zellij** — nicer defaults and easier copy, but the same model. Porting for marginal gain.
- **A custom TUI** — a read-only dashboard is a weekend's work, but you want live interaction, which means terminal multiplexing. That is reimplementing tmux.
- **A web UI** — pseudo-terminals piped over websockets into xterm.js would give proper copy-paste and layout control. A functional version is a weekend; reconnects, scrollback and session persistence are where the real time goes. tmux already does all of it, badly, for free.

tmux stands, with awkward copy-paste as a known and accepted cost.

### Observability, deferred

Local-only tracing tools were investigated — Arize Phoenix in particular, which runs entirely on your machine with no API keys and shows nested spans per model and tool call with token counts attached, ingesting standard OpenTelemetry so there is no lock-in.

It is not needed yet. The tmux setup already provides the visibility that was missing, and instrumenting a harness that is still being designed is decoration. Worth revisiting if per-subagent token accounting becomes a real gap.

## From what exists to this

This replaces an existing working project rather than starting clean, so most of the architecture above already has an implementation. The useful framing is what carries over, what is new, and what is deliberately left behind.

### Already built, keep as is

The workspace itself — the tmux session, the main pane, the live status pane, the viewer that swaps in a selected subagent's window — is the part that works and is the reason not to start over.

Dispatch already exists as a spawn script that builds a packet and a run script, opens a hidden window and starts the worker. **The context payload is the packet**: the task, selected files or line ranges, and notes. That answers what a payload contains, and it is already a repository script rather than a harness feature.

State already lives as plain files: a per-task directory holding the task, request, packet, metadata, state, result and context manifest. **Completion is already detectable** — reporters write a state file with working, waiting, permission, error, done, failed and killed, which is what makes waiting and parallelism implementable rather than theoretical.

Claude Code isolation is already solved the way this document describes: launched with no inherited setting sources, given only the repository's own settings, with a bootstrap handling the one-time folder trust.

The local-assistant mode is the return contract already working. A fresh local model does read-only search and reading and returns paths, line numbers and snippets — exactly the case where the results themselves are small enough to be the answer.

### What is genuinely new

**Roles.** The current system has workers and models but no roles. There is no coder, researcher, reviewer or tester as a defined thing with its own instructions and baseline skills. Everything is a generic worker distinguished only by which model it runs. Adding roles is the substantive change, and it is where the prompting layer lives: the instruction to state what the task was understood to be, and to distinguish a genuine empty result from a failed search.

**Role plus harness as two independent choices at dispatch.** This follows from roles existing at all.

**Dispatch and subagent tools as harness-neutral scripts**, so Claude Code can orchestrate as well as Pi.

### Left behind

The governance layer: strict-mode dispatcher, the contract handshake, preflight, the capability registry, automation gating, goal mode. It is the largest body of code and it encodes the front-loaded-gates lesson in the most literal way — no automation cell is enabled, and the default already moved to the lighter assistant mode. The principles it encodes are worth remembering; the machinery is not worth porting.

Worth preserving out of it is cross-provider review — an author's work checked by a different provider — because that is a real check rather than a ceremony. It returns as a role the orchestrator can call, not as a mandatory stage.

## Open questions

Deliberately unresolved. Detail before architecture is how the last iteration became unusable.

**Claude Code config isolation — decided.** The re-prompting for folder permission on every invocation came from isolating Claude Code too bluntly: cutting it off from global settings to keep your other skills out also cut it off from where directory trust is recorded. The fix is a dedicated config location rather than an absent one — it carries the permissions block so trust persists, and explicitly declares empty skills, hooks and plugins so nothing global bleeds in. This matters more now than it did: as a first-class main agent, Claude Code gives inherited configuration far more surface to affect orchestrator behaviour.

**Which skills reach a subagent — decided.** Skills arrive in three layers. Each role carries a **baseline set** defined in the repository, always available to it — the tools that role obviously needs. The orchestrator can **add to that at dispatch** when it can see from the task that more will be required. And the subagent itself can **request more while working**, when it hits something nobody anticipated; those requests go to the orchestrator, which grants routine ones and escalates to you only when it judges the request dangerous.

The layering is what makes this better than a static allowlist. The baseline covers the predictable, the orchestrator covers what is visible at dispatch, and the requests reveal what neither of you thought of. Still open: what counts as dangerous enough to escalate.

**Does the classifier need subagent type as a feature?** Or is the context payload alone sufficient? This is an experiment rather than a design decision: try it both ways and see whether the type label earns its place.

**Failure taxonomy — roughed out.** "Failure" covers several unlike things, and the useful question is not how bad it was but what kind it was.

**Execution trouble.** The subagent understands the task but is fighting its tools — it cannot get a grep to work, a command keeps erroring. It is still on task. The answer is more attempts, better tooling, or a different approach to the same goal, not a different subagent.

**Comprehension failure.** It worked competently on the wrong thing — asked to change one file, it changed another. Retrying identically reproduces the same wrong answer, so this needs a tighter instruction or a stronger model.

**An empty result, which may not be failure.** A researcher that finds nothing may have discovered that nothing is there, which is a real finding. The actual failure is a subagent that cannot distinguish "I searched properly and it is not there" from "I could not search properly." What matters is whether it reports the difference honestly.

**Boundary violation.** Categorically separate. A subagent that goes looking for credentials on disk and uses them to read a database it was never granted is not failing at its task — it is doing something it should not have been able to do at all. The response is to stop and surface it to you. Retrying is exactly the wrong reflex, because the task outcome is not the concerning part.

That last category is the skill-escalation design seen from the other side: if the way to get more access is to ask the orchestrator, then obtaining access independently is the boundary being bypassed.

**The orchestrator has to judge all of this, which is the hard part.** None of these categories is enforced by the system; they are read off whatever comes back. But the return is deliberately thin, and a subagent reporting "task complete" looks the same whether it did the work or did the wrong work competently. The orchestrator can trust the summary, spend context checking the artifacts, or use the classifier's expectations as a smell test — this was rated easy and it took four attempts. The thinner the return, the cheaper delegation is and the less the orchestrator can tell. That tradeoff is real and does not resolve on paper.

Two layers reduce how much judgement has to carry.

**Prompting, for honest self-reporting.** How a subagent is defined determines how legible its failures are. One instructed to distinguish "I searched properly and found nothing" from "I could not search" gives the orchestrator something real to judge. One that states what it understood the task to be before starting makes comprehension failures visible at the beginning rather than at the end. This is where most of the ordinary cases get caught.

**Config files are ask-first.** Environment files, web config, appsettings JSON — a subagent can read them when it genuinely needs to, but it asks first and takes only the keys it needs rather than the whole file. This is the same escalation path as any other tooling request: routine ones the orchestrator grants, this class comes to you. It is a permission prompt, not a block. That is a change from the flat credential-file denylist in packet building today, and the reason is that a flat deny is unusable against real brownfield work.

Beyond that, security is the roster's job. The vulnerability reviewer and the PII reviewer are the layer that catches what slipped through — which is how the work gets checked anyway, rather than restricting what agents can touch while they are working.

**Orchestrator state file.** What format, and what stays resident versus fetched on demand. Easier to decide now than to retrofit.

**Which subagents actually earn their place.** The roster above is a starting set. Expect it to change once there is usage to judge it against.
