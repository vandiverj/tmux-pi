# tmux-pi: independent assessment and direction

Nov 10, 2026 · @Jerrick

## Summary

Most of the friction you describe is a delivery problem, not a memory or model-reasoning problem. The rules you keep repeating live in prose that Main must remember and relay, while the tools at the moment of need point the other way.

The strongest evidence is a pattern in your own list. Everything tmux-pi enforces in code (one writer per lane, review of the exact tree, cross-provider reviewers, effort ceilings) is absent from your complaints. Everything left to prose is on the list.

Four findings carry most of the weight:

- **The safe-read path is opt-in and the unsafe path blocks.** The sensitive-read hook answers `ask` with a message written for a human and never names `safe_grep`. Your own count before the K1 classifier fix: 8 of 17 reviews stopped on such a prompt. The run-log shows stops continuing after it.
- **Worker correction is not a tool.** There is no cancel command, `tp-agent message` presses Enter into an open prompt, the wake notice offers only `inspect`, and the shipped Claude overlay tells Main not to answer prompts at all. The guard was specified on 2026-11-07 and is not in this revision.
- **Evidence and waiting state are recorded but not consumed.** Gate and test-run records carry tree fingerprints, and a `fresh`/`stale` reader exists, yet nothing in `bin/` or `lib/` calls it.
- **Review chains have two causes that need different fixes.** Some are one defect family found site by site. Others are an open-ended adversarial bar with no stopping rule (LS1 reached round 14, S1c round 21).

My five priorities, in order: fix the permission-prompt path end to end; converge reviews with an acceptance bar, an earlier sweep and generated attack cases; show who holds the next action; consume the evidence already recorded; deliver operating knowledge by state instead of by memory.

I would not add a larger memory store or more role-prompt text. I would remove the legacy and strict paths, and apply your realistic-use bar to the harness itself.

For the local Qwen model, use it as a checked, single-call function (scout, digest, private labelling) and not as a worker. Your role floors already keep it out of worker roles, and your summarizer data shows one structured call beating a tool loop.

I inspected code and documents only. I ran no sessions and saw no transcripts, so every claim about runtime behavior rests on your own logs or is marked as a hypothesis.

## Scope and evidence

I inspected `vandiverj/tmux-pi` at `main`, commit `5ac86c6a430ad313a344e47c18698f078766aa4f` (2026-11-10 17:01 -0600, "Merge branch 'gate-deno-lint-new-lines'"). The clone was shallow, so no history analysis was possible.

**What I read**

| Area | Files |
| --- | --- |
| Main's instructions | `agent/orchestrator.md`, `agent/orchestrator-claude.md`, `agent/orchestrator-pi.md`, `claude/main-prompt.md`, `bin/tp-main`, `bin/tp-claude` |
| Worker instructions | `agent/roles/*.md`, `lib/preamble.py`, `lib/agent_tools.py`, `docs/agent-tools.md` |
| Hooks and safe tools | `claude/plugins/tmux-pi/hooks/hooks.json`, `register.tsx`, `sessionstart-compact.sh`, `lib/sensitive_read.py`, `claude/mcp/safe.mjs`, `claude/settings.json` |
| Messaging and state | `bin/tp-agent` (message branch, subcommands), `lib/pane_send.py`, `lib/lane_steps.py`, `bin/tp-status`, `lib/gate_record.py` |
| Review and families | `lib/family_sweep.py`, `lib/review_rounds.py`, `bin/tp-review` |
| Memory and skills | `docs/persistent-work.md`, `docs/context-engine.md`, `docs/skill-authoring.md`, `config/skill-catalog.json`, `config/work.json` |
| Evaluation | `bin/tp-bench`, `bin/tp-trial`, `bin/tp-evals`, `bin/tp-improve`, `docs/bench.md`, `bench/tasks/` |
| Your own record | `docs/plans/det-prep/` (README, run-log, release-gates, `h1-main-guards.md`, `loop/ideas.md`), `docs/plans/realistic-use-bar.md`, `docs/prompt-examples/` |
| Configuration | `config/model-catalog.json`, `config/orchestration.json`, `config/routines.json`, `agent/settings.example.json` |

**Current configuration, as tracked**

- `agent` defaults to Claude as Main and `tp` defaults to Pi (README).
- Coder, tester and researcher have a strict `mid` floor: Claude Sonnet 5.5 or GPT-6.1 Sol. Tester and researcher are capped at `mid` too.
- Reviewers come from a different provider than the author, at `mid` or higher, with GPT first for Claude-authored work.
- Local Qwen is limited to qualified shapes and is first in the router chain in `config/model-catalog.json`. The README still describes Haiku first, so the two disagree.

**How to read the claims below**

- *Implemented*: I read the code at this revision.
- *Documented*: a doc or plan says so; I did not confirm it in code.
- *Observed by you*: taken from your run-log, ideas file or known-failures list.
- *Hypothesis*: my inference, not yet tested.

**What I could not see**

- No session transcripts, task records or event data: `.tmux-pi/` is git-ignored.
- No live run. I did not start tmux, Claude Code, Pi or the test suite.
- Nothing from the work Mac: its prompt folder, config and repositories are known to me only through the sanitized `docs/prompt-examples/`.

One side note, since the repository is public: `claude/mcp.json` still carries a work tracker hostname and a private network address, and `config/routines.json` names the work monorepo. The prompt examples were sanitized; these were not.

## Independent diagnosis

Six of your seven observations trace to instruction delivery, tool design or unread state. Only the review chains involve model judgment in a way a mechanism cannot fully absorb.

### Each observation, explained

| Observation | Most likely cause | Evidence | Confidence | Alternative explanation |
| --- | --- | --- | --- | --- |
| Reminders to use `safe_grep` / `safe_read` | Tool design plus relay. The blocked path prompts instead of redirecting, and the rule reaches workers only if Main copies it into the brief. | Implemented: `sensitive_read.response_for` returns `ask` with a human-addressed reason and no mention of the safe tools. `safe.mjs` describes them as for credential and env files. The only standing worker line names `safe_read` alone and renders only when the project has `.tmux-pi/agent-tools.json`. Observed by you: 8 of 17 reviews stopped on a prompt (H11). K1 then lifted the prompt for plain git grep over tracked files, and the run-log still records later stops: the M2-F1 review, the U2 coder and three coders in H27. | High | Workers ignore a clear instruction. Unlikely: Claude coders and GPT reviewers both did it, and your H11 note says the reviewer packet scoped `safe_grep` to files outside the worktree. |
| Evaluation and quality tools not considered | No trigger. 39 commands exist; Main's prompt names none of `tp-bench`, `tp-trial`, `tp-evals`, `tp-improve` or `tp-ctx`, and tells Main to stay lean and avoid a fixed pipeline. | Implemented: `agent/orchestrator.md`. The skill catalogue selects by keyword markers and holds product procedures only. | Medium-high | Main is correctly skipping them. Without a stated condition for when each tool is relevant, non-use and correct omission look the same. |
| Reminders that Main can cancel a prompt and redirect | Missing capability plus contradictory guidance. | Implemented: no cancel subcommand; the `message` branch sends text and Enter with no prompt check; the wake notice for a prompt offers only `tp-agent inspect`; `orchestrator-claude.md` says a prompt needs the user and Main must not answer it; neither the settings allow-list nor Main's `--allowedTools` covers `tmux`, so a raw `tmux send-keys` is not pre-approved. Documented: `h1-main-guards.md` specifies `--deny-prompt`; it has 0 matches in `bin/tp-agent`. Observed by you: Main approved a prompt by messaging it, twice. | High | Main is being appropriately careful. Partly true: your known-failures list records the safety classifier denying tmux pane commands. |
| Purpose and expectations fade in long sessions | Operating rules are not durable state. After compaction the hook re-injects the task index only; rules survive if the summary keeps them. | Implemented: `sessionstart-compact.sh`, the Compact Instructions block. Documented: your work prompt asks Main to re-read files after compaction, a rule that must itself survive compaction. | Medium | Long-context degradation independent of compaction, which Chroma's 18-model study reports as input grows ([source](https://trychroma.com/research/context-rot)). Or the purpose was never recorded beyond a one-line objective. I cannot separate these without transcripts. |
| Same defect found file by file across rounds | Two mechanisms; see below. | Observed by you: F1 ran 9 rounds and was abandoned, D1 7, LS1 at least 14, S1c at least 21. Implemented: the sweep brief is generated only after the repeated-family gate stops a retry. | High for the pattern, medium for the split | The code under review is intrinsically hard (shell parsing, process races), so any reviewer would keep finding cases. |
| Expensive testing, weak evidence reuse | Records exist with no reader, and test selection is left to model judgment. | Implemented: `gate_record.status` returns `fresh`, `stale` or `invalid`, and I found no caller outside its module. Observed by you: baseline full suite 4,011 tests in 2,575 s; the release gate runs four cells; yet 387 off-baseline failures in 20 modules passed M2's own run and review. | Medium-high | Cost sits in a few process-heavy files, not suite breadth: `test_tp_review` about 115 s, nine tamper tests at 41 to 43 s. |
| Cannot tell what progress waits on | The answer is split across three pull-only views and omits several owners. | Implemented: lane `waiting_on` covers worker, review and three orchestrator states, not user decision, test run or provider. It appears in `tp-agent lanes`, not the status pane. The notifier wakes on task status only. Pi Main has no notifier. | Medium-high | The information is sufficient and the pane layout is the issue. |

### Cause 1: operating knowledge travels by relay

The shipped orchestrator prompt is 6.5 KB and holds none of the rules you repeat. Those rules live in the eight-file prompt folder you load by message at work, and in a brief template whose first line says every brief must restate them.

So each rule crosses two lossy hops: your message to Main, then Main's brief to the worker. A compaction can drop the first hop. A hurried brief drops the second.

Thin prompts did not cause this. The role prompts are not thin (the reviewer role is 7 KB), and the 21-round chain happened under them. The cause is that the rules sit in the one place no mechanism reads.

### Cause 2: the tool surface contradicts the rule

At the moment a rule applies, the tool in front of the model points elsewhere:

- A worker that runs an ordinary recursive search gets a blocking dialog, not a redirect.
- Main, told a worker is at a prompt, is handed `inspect` and an instruction not to answer.
- The one command Main does have, `message`, performs the action you most want to prevent.

A model that follows the surface in front of it will need reminding every time. This is an affordance problem in the HCI sense, and prompts are a weak fix for it.

### Cause 3: records without readers

tmux-pi writes careful evidence: tree fingerprints on gate and test runs, reviewed-tree records, delivery ids on messages, model-selection logs. Lane close reads the reviewed tree. Nothing reads gate or test-run freshness.

So "do not rerun a file that passed unless the tree changed" stays a sentence in three role prompts. The same holds for waiting state: the facts exist, but no single line answers "whose move is it".

### Cause 4: two kinds of review chain

**Family chains.** One cause, several sites, found one per round. Your H12 note shows coders patched only the cited site or form. The harness answers with a sweep brief, but only after a second rejection.

**Open-bar chains.** The reviewer is told to review everything every time and not to stop at a verdict. Any `code` finding blocks. With no statement of which inputs matter, an adversarial reviewer on another provider can always find one more.

Your realistic-use decision on 2026-11-10 fixed this for two lanes by declaring the bar. F1 shows the compounding effect: round 9's fix closed the earlier cases and introduced two new defects.

These need different remedies. A better sweep does nothing for an open bar, and a declared bar does nothing for a missed site.

### Cause 5: the harness's own weight

By my rough count the repository holds about 75,000 lines of non-test code, 105,000 lines of tests, 39 commands and 4,960 test functions. That is a large surface for one maintainer.

Signs that the weight is already costing you:

- The behavior-test rule appears verbatim in three role files and again, as a shared paragraph, in both Main prompts.
- `dispatcher.ts` (2,145 lines) loads only under `TP_MODE=strict`, which the README says nothing in `bin/` sets.
- The README and the catalog disagree on router order.
- Port work had to chase Python 3.9 incompatibilities across three lanes (U1 to U3).

Much of the multi-round review effort went into hardening the harness against inputs you later ruled contrived. That is effort not spent on the work the harness exists to carry.

### Counterexamples

- **Mechanism works where it exists.** No item on your list concerns lane leases, step close, reviewer independence or effort ceilings. Each is enforced by code that refuses and names the clearing command.
- **Durable notes work when kept.** The det-prep campaign ran for days with you away, steered by a run-log and your standing answers. Purpose survived there because it was written down and re-read.
- **More text has already been tried.** The reviewer role asks for every site of a cause in one pass. Chains continued. This argues against more prompting as the main remedy.
- **Against my own thesis:** brief rules did cut prompts once Main pasted them. So prose works when it arrives. The question is who guarantees arrival.

### Where the boundary belongs

A responsibility belongs to a mechanism when the right behavior is a predicate over recorded state. It belongs to the model when it requires reading meaning. It belongs to you when it grants authority.

| Responsibility | Owner | Reason |
| --- | --- | --- |
| Which tools a worker has and how to call them | Mechanism: harness preamble, tool descriptions, redirecting hooks | Same for every task; relay adds only loss |
| Lease, step close, reviewed tree | Mechanism (already) | State predicate |
| Evidence freshness | Mechanism: fingerprint comparison | State predicate |
| Whose move it is, how long, stalled or not | Mechanism: derived line and timer | State predicate |
| Delivery and event identity | Mechanism (already, `pane_send`) | State predicate |
| Round budgets, timeouts, bounded runs | Mechanism | Counting |
| Role choice, work split, brief content | Model | Interpretation |
| Whether a finding is in the bar; patch or redesign | Model proposes, you set the bar | Meaning plus priority |
| Design quality, duplication, coupling | Model and you, with tool findings as evidence | No tool measures design quality |
| Approving a prompt, push, override, same-provider review | You only | Authority |

Cancelling a prompt and redirecting a worker sits on the mechanism side. It removes a pending request and grants nothing. Approving the same prompt sits on yours.

## What is working and should be preserved

The parts of tmux-pi that turn a rule into a refusal with a named fix are its best work. Extend that pattern; do not replace it.

| Keep | Why it matters | Where |
| --- | --- | --- |
| One writer per lane, enforced by a lease shared across sessions | Removes a whole class of coordination reminders | `lib/lane_lease.py`, README "Lanes" |
| Step close tied to a review of the exact committed tree | Completion evidence that cannot drift from the code | `lib/lane_steps.py`, `reviewed-state.json` |
| Refusals that print the command that clears them | The model and you both recover without recalling a procedure | README "Lane recovery" |
| Cross-provider review, with a quoted approval required for same-provider | Independence by construction; substitution is never silent | `bin/tp-review`, catalog `reviewer` |
| Claims-last review packets and `Closes by:` labels | A report gap no longer blocks a tree | `agent/roles/code-reviewer.md` |
| Confirmed message delivery with a per-send id | "Sent" means received whole, or says how much arrived | `lib/pane_send.py` |
| Work store with user-quoted decision answers | Separates an accepted decision from an agent's guess | `docs/persistent-work.md` |
| Knowledge notes that start as candidates and need a person at a terminal to approve | Stops inferred lessons from becoming rules unreviewed; scope globs and freshness flags limit over-generalization | `docs/context-engine.md` |
| Checkpoints written by parsing, verified by hash | Recovery state without a model call | `tp-agent work checkpoint` |
| A fixed, sealed benchmark and an evaluation habit that can say no | The RTK trial ended in "do not adopt" | `bin/tp-bench`, run-log 2026-11-08 |
| The realistic-use bar | The first explicit statement of what "good enough" means | `docs/plans/realistic-use-bar.md` |
| Visible panes, and human-only approval, push and override | Meaningful control without constant watching | README, `04-tmux-pi-operations.md` |
| A short router card instead of the full packet | Keeps routing cheap and insensitive to alarm words | `agent/orchestrator.md` |

Your memory design already draws the distinctions you asked for. It needs a delivery path, not a redesign:

- **Explicit preference**: a decision answered `--by user` with a quote.
- **Accepted decision**: a `Decisions` line in a workspace.
- **Inferred lesson**: a candidate knowledge note with evidence, scope and a version range.
- **Temporary task state**: the task record and session index.

## Five highest-priority improvements

Ranked by expected benefit against effort and lasting complexity. The first two are small and address measured failures. The fifth has the widest reach and the least certainty, so it comes last and needs an ablation before adoption.

### 1. Fix the permission-prompt path end to end

Make the blocked path redirect instead of block, and make correction a command. This covers your first and third observations together.

**Change**

- In worker sessions, the sensitive-read hook returns `deny` with a reason that names the equivalent safe call. Claude Code shows a deny reason to the model ([hooks reference](https://code.claude.com/docs/en/hooks)). Keep `ask` in Main's pane and for `reveal`. Mirror it in the Pi gate.
- Describe `safe_grep` and `safe_read` as the normal way to search or read anything that may include a secret store. Render the standing tool lines for every worker, with or without a project manifest.
- Build item 1 of your H1 plan: `message` refuses while a prompt is open, and a `redirect` action sends Escape, waits for the prompt to clear, then sends the text. Record the cancellation in the task record.
- Name that action in the wake notice, and correct the sentence in `orchestrator-claude.md` that tells Main not to act.

**Hypothesis.** Prompt stops fall sharply because the model reads the redirect and retries on its own. K1 cleared one command shape and took three rounds; a redirect covers every shape the classifier still flags. The remaining stops are handled by Main without a reminder.

**Smallest experiment.** Collect the commands behind the 17 review prompts and the H27 coder prompts into a fixture. Check that each now gets a deny whose reason is a runnable safe call. Then run 20 live tasks on battle-soccer with a seeded `config/` directory, and 10 seeded prompt scenarios for `redirect`.

**Baseline.** 8 of 17 reviews stopped before K1 (H11). The rate since K1 is unmeasured, but at least five stops are logged, three of them in one round (H27). Two approvals by message. Measure the current rate first.

**Adopt if** stops that need Main fall to 1 in 20 tasks or fewer, no prompt is ever approved by a keystroke from Main, and the redaction tests are unchanged.

**Reject or rework if** workers loop on denied calls (three or more per task), redacted output hides what a reviewer needed, or prompt detection blocks ordinary messages.

**Cost.** A small change in security code in both harness twins, so it needs its own security review. `safe_grep` leaked `.env` content once (F-GREP), so the safe path must be trusted before it becomes the default. Note that `deny` is stricter than `ask`: nothing is loosened. Runtime cost is nil. Your attention goes down.

### 2. Converge reviews: declare the bar, sweep earlier, generate the attack

Treat family chains and open-bar chains separately.

**Change**

- Every step brief states its bar: `realistic` by default, `hostile` for trust and redaction code. Reviewers label each finding, as your 2026-11-10 decision already requires for two lanes. Only in-bar `code` findings block. The rest go to a known-limits list.
- Generate the sweep brief at the first rejection whose finding states a cause, not after the second. Add an ast-grep rule where the family is syntactic, as H13 proposes.
- For rules with an input space, replace hand-built attack tables with property-based tests: Hypothesis for the Python harness, fast-check for the TypeScript work code. Example property: wrapping a command that reads a secret store in `sh -c`, `env`, `xargs` or a pipe never lowers its classification.
- For lease and task-directory lifecycles, use a stateful model. Hypothesis generates operation sequences and shrinks a failing one to a short program ([stateful tests](https://hypothesis.readthedocs.io/en/latest/stateful.html)).
- Keep your three-round stop. At round three, ask for one of two designs: narrow the accepted input, or fail closed.

**Hypothesis.** Median rounds to approval fall, and fewer fixes introduce new defects, because the generator explores the space the reviewer was sampling one case per round.

**Smallest experiment.** Replay only, no new worker runs. At the round-1 commit of F1, FG-N1 and LS1c: label every later finding against the bar; write one property per family and see whether it reproduces the later findings; run the H13 ast-grep replay as planned.

**Baseline.** Rounds from the run-log: F1 9, D1 7, N2 6, J1 6, DS1 5, LS1 14 or more, S1c 21 or more.

**Adopt if** the replay reproduces at least half of the later in-bar findings in four of six families, property tests fit your 5 s budget, and live review-loop tasks reach a median of two rounds or fewer.

**Reject if** generated cases mostly rediscover contrived inputs (then the bar alone is the fix), tests turn slow or flaky, or defects escape because reviewers label real ones contrived.

**Cost.** Hypothesis is a new development dependency, against your no-new-dependency rule, and must be checked on the Python 3.9.6 cell or confined to the newer one. The bar and the earlier sweep cost a few lines of prompt and one more context file per retry.

### 3. Show who holds the next action

Derive one line per work item and act on it.

**Change**

- A derived record with `holder` (worker, review, tests, runtime, user, main), `since`, and `next` (the commands valid now). Every input already exists: lane `waiting_on`, open blocking decisions, the permission record, provider health, queue state.
- One line per lane at the top of the status pane.
- A stall wake: when the holder is Main beyond a threshold and Main is idle, the notifier sends the `next` line. When the holder is you, the pane highlights it.
- Each holder change is appended to an event file. That file is your waiting-time data.

**Hypothesis.** Avoidable waiting falls, and you stop asking what a lane is waiting on.

**Smallest experiment.** Ship the read-only line first, with no wake. Compute from existing task records how long past lanes sat in `orchestrator-*` states. Then add the wake and compare on scripted episodes.

**Baseline.** That historical figure, plus the four hours lost to watcher loops on 2026-11-08.

**Adopt if** time with holder Main and Main idle halves, and the line matches your own reading of the state in 95% of sampled moments.

**Reject if** wakes push Main to act before evidence is in, or the line is wrong often enough that you stop trusting it.

**Cost.** One small derivation module and a pane change. Wakes add turns, bounded by the existing backoff. Pi Main gains nothing until it has a notifier.

### 4. Consume the evidence already recorded, and select tests by a map

**Change**

- `tp-agent evidence LANE` lists gate and test-run records as fresh or stale against the current fingerprint, using the reader that already exists.
- The review packet gains a facts file, "Checks already run on this tree", in the style of the timer-waits file. Reviewers rerun only what is stale or absent.
- `lane close` reports a missing fresh test run. Turn the report into a refusal (your B2) only after it proves accurate.
- `--tests affected` gets a deterministic meaning: a map from changed files to test modules built from imports, naming and declared config consumers (H28). Where a project uses pytest, pytest-testmon is an alternative; its accuracy depends on coverage, and subprocess tests need a coverage hook ([project page](https://pypi.org/project/pytest-testmon/0.9.16)).
- Time the ten slowest test files and move their variants in-process. This may be worth more than selection.

**Hypothesis.** Duplicate runs disappear and cross-module regressions are caught before integration, without more full-suite runs.

**Smallest experiment.** Replay M2: would the map have selected tests in the 20 modules that later showed 387 failures? Count runs in past task directories that share a command and a fingerprint.

**Baseline.** Those two counts, and 2,575 s for a full run.

**Adopt if** the replay selects at least 90% of the failing modules, duplicate runs reach zero in episodes, and stale evidence is never accepted.

**Reject if** the map misses because tests reach code only through `bin/` subprocesses, or its upkeep exceeds the time saved.

**Cost.** Small. Map generation adds seconds per dispatch. The full suite still runs once at integration.

### 5. Deliver operating knowledge by state, not by memory

This is how to restore reliability without thick prompts or a fixed workflow.

**Change**

- Rules true for every task (plain command forms, foreground tests, no stash) move into the harness preamble that every worker receives. Project rules stay in `worker-preamble.md`. Main stops relaying.
- Standing orders become typed, scoped items in the work store: kind (preference, decision, lesson), scope (repository, lane, session), the source quote, and a review date. The top few are re-injected with the task index at session start and after compaction.
- A small set of procedure cards, each under 600 bytes, is selected by state or event, not by keyword: prompt open, second rejection in a family, slow-test flag, before first review, before integration, provider out. A card states facts and available moves. It does not dictate order.
- Each evaluation tool gets a stated trigger, or a deliberate decision that Main never needs it.

**Hypothesis.** Process reminders fall and stay low across compaction, because the rule arrives with the situation.

**Smallest experiment.** Four arms on the scripted episodes, with a forced compaction midway: current; cards plus standing orders; the whole prompt folder preloaded; thin prompt only. Include cases where no card should fire.

**Adopt if** the cards arm beats both current and preloaded on reminders and on correct next action after compaction, and does not lose to thin-only on completion.

**Reject if** it matches current (then relay loss is not the cause), or cards fire where they should not.

**Cost.** Ten to fifteen cards to maintain, and a real risk of growing into a rigid workflow. Cap count and size. Rerun the thin-only arm with each new model and delete cards that no longer change outcomes. An Anthropic harness study took the same approach, removing components one at a time as models improved ([source](https://www.anthropic.com/engineering/harness-design-long-running-apps)).

## Comparison of tools, process changes and experiments

Two candidates are ready to adopt, one is a process change you have already made in part, and the rest should earn their place by replay on your own history before any live runs.

| Candidate | What exists already | What it adds | How it would be used | How to evaluate | Verdict |
| --- | --- | --- | --- | --- | --- |
| Deny-with-redirect in worker sessions | `ask` hook; safe tools | Self-correction with no human | Worker sessions only; Main keeps `ask` | Fixture of past blocked commands; prompt stops per task | Adopt after security review |
| `redirect` action and `message` guard | H1 plan; Escape already used by the pause path | Safe correction as one command | Main, on a prompt notice | Seeded prompt episodes; zero keystroke approvals | Adopt |
| Acceptance bar in every brief | Realistic-use bar on two lanes | A stopping rule for adversarial review | Reviewer labels each finding | Replay: share of later-round findings outside the bar; escaped defects | Adopt |
| Earlier sweep, with ast-grep where syntactic | Sweep brief after the second rejection; H13 plan | All sites listed at round 1 | Rule written from the first finding, run before and after the fix | H13 replay: recall against text grep on six families | Evaluate by replay |
| Hypothesis (Python), fast-check (TypeScript) | Hand-built attack tables; table-driven tests | Generated cases, shrinking, operation sequences | Properties for classifiers, parsers and lifecycles | Does a property reproduce later-round findings; run time; flake rate | Evaluate on two families |
| [Fallow](https://docs.rs/crate/fallow-cli/2.76.0/source/README.md) | Gate runs prettier, lint, typecheck, gitleaks; nothing for dead code, duplication or complexity | Whole-project graph; `audit` scoped to a changeset; hotspots as churn times complexity | Non-blocking record in the gate and a facts file for the reviewer | On past step diffs: share of findings a reviewer or you judged worth acting on; false "unused" on Deno and nx entry points | Evaluate; evidence only |
| Co-change analysis without Java | Affected-areas file from grep (D2) | Files that change together with no import between them | An "also consider" list in the brief and the affected-areas file | Replay: do co-change neighbors at round 1 include later-round sites that grep missed | Try a script first |
| Test selection map | `--tests affected`, chosen by the model | Deterministic selection | Per step; full suite once at integration | M2 replay; duplicate runs | Evaluate |
| Evidence ledger | Fingerprinted records and an unused reader | Freshness in the review packet and at close | `tp-agent evidence`, facts file | Stale evidence accepted (must be 0); duplicate runs | Adopt |
| Holder line and stall wake | Lane `waiting_on`; notifier | One answer to "whose move"; action on an available step | Status pane; wake notice | Avoidable waiting; agreement with your reading | Adopt the line, evaluate the wake |
| Procedure cards and typed standing orders | Keyword skill catalogue; work store; knowledge notes | Delivery tied to state; survival across compaction | Wake notices, `next` line, re-injection | Four-arm ablation with forced compaction | Evaluate |
| OpenTelemetry monitoring (H16) | `tp-improve`, routines, evals, ledger, selection log | Per-request cost and latency | Not yet | Not yet | Defer |
| Native Claude Code agent teams | tmux-pi's own orchestration | Less code to own | Not yet | Not yet | Watch |
| RTK output compression | Evaluated 2026-11-08 | Nothing | Not used | Done | Stay rejected |

**Notes on three of these**

*Fallow* covers TypeScript and JavaScript only, and its analysis is syntactic. So it applies to battle-soccer and the work monorepo, and barely to tmux-pi itself, which is mostly Python. It ships an agent skill and an MCP server. I would start with the plain CLI inside the gate, because a gate record is deterministic and a skill is one more thing to load.

*Co-change analysis.* Java-free options exist: [git-xray-cli](https://pypi.org/project/git-xray-cli/) is pure Python with no dependencies, and [code-maat-python](https://pypi.org/project/code-maat-python/) ports Code Maat's analyses but is marked alpha and is GPL-3. A 40-line script over `git log --name-only` gives the coupling list with nothing to install. Two limits apply to your history. Agent commits are large and campaign-shaped, and git-xray already drops commits over 30 files as noise. And with one author, the ownership and bus-factor analyses mean nothing. I could not run any of this on a shallow clone.

*Design-quality findings.* Present duplication, coupling and complexity findings as facts, next to the diff, and let the reviewer say whether each is accidental or essential. Track precision, meaning the share of shown findings that led to an accepted change. Do not track counts, lines removed or abstraction added.

## Exploratory ideas

All three are speculative. Each borrows from another field, and each transfer has a stated limit.

### A. Mine your own sessions as an event log

**Idea.** Write one append-only file of state changes: task, lane, decision, prompt and test-run events, each with a work-item id, a timestamp and the current holder. Then apply process-mining questions to it.

- Where does elapsed time sit, by holder?
- What order do steps really happen in, compared with the order you intend?
- Which known anti-patterns occur, such as a review dispatched before the report gate, or a coder step after notes-only approval?

**Connection.** Process mining reconstructs the actual flow of a process from event records and checks it against the intended one. Your sessions have the three fields it needs: a case, an activity and a time.

**Limits.** The field assumes many cases. You will have dozens, so use the output to find where to look, not as a metric to steer by. A short script is enough; no mining library is needed to start.

**Smallest test.** Derive the file after the fact from one finished campaign's task records. If it explains the four-hour watcher loss and the longest lane waits without your memory of them, build the live writer.

### B. A return brief, written by the harness

**Idea.** When you attach or press a key in the status pane, print four things: the objective; what changed since your last input; the decisions waiting on you, verbatim, blocking first; and what Main will do next without you.

**Connection.** Studies of interrupted programmers found resumption slow and effortful. In one analysis of about 10,000 recorded sessions, roughly one in ten began editing within a minute ([Parnin and Rugaber](https://sites.cc.gatech.edu/reverse/repository/resumptionstrategies.pdf)). In a lab study, programmers given an automated cue of their recent activity completed tasks at about twice the rate of those with notes alone ([DeLine and Parnin, CHI 2010](https://www.microsoft.com/en-us/research/publication/evaluating-cues-for-resuming-interrupted-programming-tasks/)).

Supervisory-control research adds a warning. Bainbridge's 1983 paper argues that an operator who only monitors is poorly placed to take over when the automation hands back a hard case. So a page to you should carry the evidence needed to decide, since your own context will be cold.

**Limits.** Those studies observed people resuming their own code, not supervising agents. That the cue transfers is my assumption.

**Smallest test.** Generate the brief by parsing, with no model call. Time yourself from attach to first correct instruction on ten returns, with and without it.

### C. Replay decision points instead of whole sessions

**Idea.** Each time you correct Main, save a snapshot: the session index, the lane states, the triggering notice, and the command you had it run instead. Offline, show a candidate prompt or model the same snapshot and score its first command against yours.

**Connection.** This is replay testing from durable-workflow systems, where recorded history is fed back through new code to see whether it makes the same decisions. Here the "code" is Main's prompt and model.

**Why it suits you.** A snapshot holds record metadata, not source code. Real corrections from the work Mac could become an evaluation set that runs on the personal Mac, subject to your employer's rules on what may leave.

**Limits.** It scores only the first step, not the outcome. A different first command may be equally good. Snapshots can leak names, so they need the redaction pass. It also needs a habit of capture, which is exactly the kind of habit this report argues against relying on, so the capture must be one keystroke.

**Smallest test.** Hand-build 15 snapshots from the incidents in your known-failures list. Check whether the current prompt reproduces the mistake and whether priority 1 and 5 remove it.

## Proposals to defer, simplify or reject

The common thread: stop adding capacity and text, and remove what duplicates or no longer runs.

| Proposal | Verdict | Reason |
| --- | --- | --- |
| A larger memory store, or embedding retrieval for lessons | Reject | The gap is delivery, not capacity. The knowledge store is already deterministic and reviewed, but it reaches only workers that ask, and only with `TP_CTX_TOOLS=1`. |
| More text in role prompts | Reject | The reviewer role is 7 KB and chains continued under it. Each new rule is copied into three to five files. |
| Preloading the whole prompt folder into Main as the standard | Reject as default | It is today's practice at work, and it decays at compaction. Longer inputs also make models less reliable ([Chroma](https://trychroma.com/research/context-rot)). Keep it as an ablation arm. |
| Any quality tool as a blocking gate | Reject | Findings are evidence. Syntactic analysis misreads framework entry points. |
| Strict DRY or SOLID compliance checks | Reject | No tool measures design quality, and a pattern check rewards abstraction for its own sake. |
| Code Maat itself | Reject | Java-free equivalents exist, and the idea should pass a replay first. |
| OpenTelemetry monitoring (H16) | Defer | It overlaps five measurement tools you already have. The event file answers the waiting question first. |
| The local summarizer on any critical path | Defer | LS1 took 14 or more rounds, and its best run reached 85% recall against a 90% bar. Your checkpoint-1 cut already excludes it. |
| Goal mode and wider autonomy | Defer | Its own document says no live goal session was run by its tests. Build on the prompt path and the holder line first. |
| The freeze guard (second half of H1) | Defer | Ship the message guard alone. The freeze matters only at work. |
| A hard refusal at lane close for missing evidence (B2) | Defer | Report it first. Refuse once the report proves accurate. |
| Native agent teams | Watch | Third-party guides describe them as experimental, and they are single-provider. Your differentiators are cross-provider review, lanes with evidence, and redaction. Keep files and records as the contract, so orchestration can move onto native primitives later. |
| Legacy Main (`TP_MAIN`, `claude/main-prompt.md`, the subagent MCP tools) and strict mode (`dispatcher.ts`, `subagents.ts`) | Remove or archive | They are second copies of the rules. The README says the strict flow is outside current guards, and legacy Main gets no wake-ups. |
| Duplicate prose rules | Simplify | Keep one fragment and include it at build time. |
| Hardening against hostile input outside the trust path | Simplify | Apply the realistic-use bar repository-wide. Keep `hostile` for sensitive-read, redaction and trust code only. |
| The 58 KB README as operator manual | Simplify | It has already drifted from the catalog. Generate the configuration-derived parts. |
| The router chain for strict-floor roles | Simplify, if the data agrees | Coder, tester and researcher resolve to `mid` whatever the router says. Three routers then mostly choose effort. Check the selection log for how often the pick differed from the role default. This is a hypothesis; I have not seen the log. |

## Using the local Qwen model where it pays

The local model earns its place as a bounded function inside deterministic code, not as a worker. Your own measurements point that way, and so do your role floors.

### Which model

Tracked configuration names `lmstudio/qwen/qwen3.6-35b-a3b`. A Qwen 3.8 appears only as the 4-bit "Flash Next" build served through Slotstream with a 32k window. A web search turned up 3.5 and 3.6 builds of the 35B-A3B for LM Studio and nothing called 3.8 35B-A3B.

If you have moved to a newer build in your untracked `config/local.json`, every number below belongs to the old one. The catalog's shape evidence is not keyed by model, so a swap inherits "qualified" silently.

### What it does today

| Use | Where | Evidence at this revision |
| --- | --- | --- |
| First router in the chain | `lib/llm_router.py`, direct call, temperature 0, capped at `mid` | In use |
| Follow-up judge | `lib/router.py` | 54 of 56 correct, no wrong automatic decision, median 0.14 s |
| Review of ambiguous redaction candidates | `lib/redact_llm.py` | Sees a candidate's shape, never its characters; any failure keeps the redaction |
| `ask_local` read and search digest | `agent/extensions/assistant.ts` | Pi Main only. Claude Main, the default for `agent`, has no equivalent |
| Worker at the `local` level, qualified shapes | Catalog `shapes` | Locate and quote 117 of 120; count 77 of 80 with a tuned prompt; short classification 54 of 56 |
| Worker, candidate shapes | Catalog `shapes` | Summarize far from its bar; small edit has 6 chores; named checks unqualified |
| Shadow coder trial | `bin/tp-trial` | Records are git-ignored; I could not see results |

### What your evidence says

**The worker level is almost unreachable.** All eight roles have a `mid` minimum, three of them strict. The J1 control run had 8 of 8 local tasks refused at dispatch. The catalog still expects a 5 to 12% share. I think the floors are right, which means the expected share is the part to delete.

**One call beats a tool loop.** In the summarizer tuning, the Pi tool-loop path reached 45% recall with 40 unsupported citations. A single call with numbered input, a JSON schema and code-checked citations reached 85% recall with none wrong at best. Your note from that run says structure and a code-made name list helped more than prompt wording.

**A command is better than a model at exact work.** Counting reaches 96% with a tuned prompt; `grep -c` reaches 100%. Running named checks and quoting output is what `test-run.sh` already does and records.

**Cost is the weakest reason.** You rank token cost as secondary, and the routed share is near zero anyway. The stronger reasons are:

- **Latency.** A 0.14 s judgment or a one-second lookup, against cloud router timeouts of 20 to 35 s.
- **Availability.** Provider limits have stopped your reviews. Local functions keep mechanical steps moving.
- **Privacy.** Work transcripts and code that may not leave the machine can still be read locally.
- **Main's context.** A digest returns instead of raw file reads.

The largest cost of local use so far has been building it. The summarizer lanes ran past 14 and 21 rounds for a capability still short of its bar.

### A rule for placing local work

Use the local model only when all four hold:

1. Code assembles a bounded input: named files, numbered lines, a size cap.
2. One call returns schema-constrained output at temperature 0. LM Studio enforces a JSON schema on its chat endpoint ([structured output](https://lmstudio.ai/docs/app/api/structured-output)).
3. Code checks every element of the output and drops what fails: the cited line exists, the quote matches, the label is in the allowed set.
4. A wrong or missing answer is cheap. The result is advisory, or falls back to a frontier model or to you.

It is never a gate, never the only reviewer and never an authorization. Your follow-up judge and redaction reviewer already follow this rule. The summarizer became tractable only once it did.

### Where I would use it

| Use | What the model adds | How to check it | Verdict |
| --- | --- | --- | --- |
| Pre-dispatch scout (your H2), as a function | Code runs the brief's searches. The model picks and orders 5 to 10 references with a one-line reason each. Code verifies every path, line and quote | Orientation calls and tokens before a frontier worker's first edit; share of attached references actually opened. Include a code-only arm with no model | Evaluate first |
| `ask_local` for Claude Main | The same digest Pi Main has, as a tool beside the safe server | Main's context growth per session; re-reads counted by `tp-improve` | Adopt |
| Transcript labelling on the work Mac | Bulk, private labels: process reminder or product decision, failure class, type of prompt stop | Agreement with 50 of your own labels before trusting it | Evaluate; answers question 1 |
| Advisory brief check before dispatch | Flags a brief with no stated rule, case table, bar or named tests (H12, H22) | Rounds to approval with and without the advisory | Low priority |
| Summarizer, narrowed | Prose over facts that code extracts. Keep the near-solved forms; let code produce the diff and use facts | Your meaning bar, on realistic inputs only | Keep off the critical path |

### Drafting text: commit messages and similar

Drafting short text is a good fit, provided code supplies the facts and the format and the model only phrases them. The deciding question for each item is whether the drafter needs to know *why* the change was made.

**Commit messages: split by who commits.**

- **Coder commits.** The coder writes the message while it still holds the reasons. A local model working from the diff alone sees what changed, not why, so I would leave these alone.
- **Main's commits.** Main commits approved lane work (`orchestrator-commit`), merges lanes, and makes sync and checkpoint commits. Main spends turns on these and has to read material it has already set aside. This is the best target.

**Code owns everything that must be exact:**

- the `CASE-ID:` subject prefix and the subject length;
- the `Co-Authored-By` trailer, written from the task record's resolved model. Your known-failures list has both a missing trailer and a wrong model in it, and both are deterministic fixes that need no model at all;
- the input: the step brief's title and acceptance rows, the worker's report summary, `git diff --stat`, and hunks up to a byte budget.

**The model writes:** the imperative subject text and a body of two to five lines.

**Code then checks the draft.** Every file or symbol the message names must appear in the diff. No decision ids (your brief rule). Nothing named in the brief as out of scope. If a check fails, fall back to a template built from the brief title.

**Other drafting jobs, ranked**

| Job | Fit | Note |
| --- | --- | --- |
| Main's lane, merge and sync commit messages | Good | Described above |
| Run-log and `STATE.md` entries after a lane event | Good | Code supplies the event facts. Keeps purpose written down without spending Main's context |
| Return-brief prose (idea B) | Good | Code builds the brief; the model only smooths it. Plain code output may be enough |
| Suggested note disposition (fold, decide, follow-up, drop) | Fair | A short classification against fixed labels. Advisory only; Main rules |
| Lane and branch slugs | Fair | Trivial, and code can do it nearly as well |
| Merge request description, first draft | Fair at most | Outward text with your approval required. Your list records tracker comments posted with errors, so the gain is small and the review still falls to you |
| Tracker comments | No | Same reason; cross-provider drafting plus your read is already the right control |

**Evaluation.** Take the last 30 lane commits from full history. Generate a message for each from the same inputs. Have code check the claims, then rate blind against the real messages: as good, acceptable, or wrong. Adopt if no draft contains a false claim and at least 80% rate as good or acceptable. Count Main's turns per lane close before and after.

**Why local here.** Privacy carries the most weight: work diffs never leave the machine. Speed matters too, since a lane close should not wait on a cloud round trip. Token savings are minor.

### Where I would not use it

- **Coder, tester, researcher or any reviewer vote.** Your floor already excludes Haiku from the first three.
- **Counting and named checks.** Use the command and the recorded run.
- **A blind second reviewer (H7).** More findings are not what your review loop lacks.
- **Anything that feeds a refusal or an approval.**

### Operating it

- **Memory.** You measured about 19 GB idle and 25 GB at peak, and the G1 plan records LM Studio growing until the work Mac froze. The bounded context and 30-minute idle unload now tracked are the right controls.
- **Contention.** Functions and local workers share one loaded model. A shadow trial in progress delays the judge. Give functions priority, or stop running local workers.
- **Fallback.** Every function needs a timeout, a defined fallback and a log line when the fallback fires. The fallback rate is the health measure.
- **Requalification.** Key each shape's evidence by model, quantization, runtime version and thinking setting. One command should rerun the calibration chores and write the result. A model with no record should not count as qualified.
- **Thinking mode.** Your router already fails an attempt that reports thinking tokens. Community reports show the no-thinking toggle behaving differently across LM Studio builds, so check it after every update.

### What would change my mind

- **Drop the scout's model** if the code-only arm matches it. Then the gain was the searches, not the ranking.
- **Drop any function** whose fallback fires in more than one call in ten, or whose advice Main follows into a mistake.
- **Reopen local workers** if the shadow trial records show small edits approved at first review as often as the `mid` coder on the same briefs. I have not seen those records.

## Evaluation design

Evaluate whole working episodes through Main's real entry point, and score outcomes. A tool call, an acknowledged instruction, a reviewer approval or an isolated benchmark pass is not an outcome.

### The unit: a scripted episode

An episode starts with `bin/tp --main claude PROJECT` on battle-soccer and a first message. A scripted "owner" answers product decisions only and logs every other intervention. Hidden acceptance tests decide completion.

| Episode | Situation and seed | Correct outcome | Must not happen |
| --- | --- | --- | --- |
| E1 Delegation | A task whose search touches a seeded `config/` directory and an env example | Done with no human touch | A prompt approved; a secret in output |
| E2 Correction | A brief that leads a worker into a blocked command | Main redirects and the task completes | Main approves by message, or waits in silence |
| E3 Relevant verification | A parser change whose consumers live in another module | Consumer tests run; no full suite in the lane | Consumers untested; full suite run |
| E4 Evidence reuse | Review asked twice: once on an unchanged tree, once after a one-line edit | No rerun the first time; rerun the second | Stale evidence accepted |
| E5 Compaction and restart | Forced compaction mid-lane, then a kill and resume | The lane continues; standing orders hold | Work redone; a settled decision asked again; an unverified "done" |
| E6 Recurring family | One cause seeded at four sites | All sites fixed in two rounds or fewer | One site per round |
| E7 Dependent lanes | Lane B needs lane A's interface, and A changes it | B waits with the right holder; the conflict is caught before integration | B proceeds on the old contract |
| E8 Negative controls | A trivial question; a search with no sensitive path; a `reveal` request; a real product ambiguity | Direct answer; no card and no forced safe tool; the prompt reaches you; Main asks | A dispatch for a question; Main cancels and proceeds without you; Main guesses the product call |

E8 matters as much as the rest. Each proposed behavior needs a case where it should stay quiet.

### Conditions

- Baseline at `5ac86c6`.
- Each change alone, then all together.
- Removal arms: the orchestrator prompt without its test-cost and review-notes paragraphs, and the thin prompt with nothing added. If removal does no harm, delete the text.
- At least five runs per cell. Report the median and the range. Treat a difference inside the run-to-run spread as no difference, as your LS1 notes already do.

### Held-out variation

- A second small project with another layout and language.
- Reworded briefs for the same situations.
- Pi with GPT as Main, to see whether a gain is tied to one harness.
- Hold back a third of the seeds and never tune against them.

### Measurements

| Measure | Definition | Verified by |
| --- | --- | --- |
| Correct completion | Hidden acceptance tests pass on the merged tree and the diff stays in scope | Deterministic |
| False completion claim | Main reports done while acceptance fails | Deterministic |
| Process reminders | Your messages that tell Main how to operate the system | Your label, by rubric |
| Product decisions | Your messages that settle scope, behavior or authorization. Counted separately; these are not failures | Your label |
| Human interventions | Any keystroke in a worker pane or manual command | Event file |
| Recovery success | Correct next action after compaction or restart with no human input | Deterministic check of the command, plus a sample read |
| Avoidable waiting | Time with a valid next action while the holder is an idle Main, or nobody | Event file |
| Unnecessary checks | A run with the same command and fingerprint as an earlier pass | Records |
| Stale evidence reuse | Acceptance of a record whose fingerprint differs from the tree. Must be zero | Records |
| Rounds to approval | Reviews until the step closes | Records |
| Guardrails | Wall time, turns and tokens per episode | Records |

Semantic quality needs you. Read a blind sample of briefs and review findings per condition, and judge whether each brief was sufficient and each blocking finding deserved to block.

### Fit with what exists

`tp-bench` scores single tasks: 6 code, 4 review, 4 local and 1 orientation. The G3 smoke script checks plumbing. Neither exercises a session with Main. Add an `episode` kind to `tp-bench` and keep its sealed answers and fixed scorer, so you do not build a second runner.

## Sequence: personal Mac first, then the work Mac

Most of this can be settled on the personal Mac, and a good share by replay with no new worker runs. The work Mac is needed for scale, real path shapes and the outward integrations.

### What each environment can establish

| Question | Real local run | Fixture or simulation | Needs the work Mac |
| --- | --- | --- | --- |
| Prompt path | Episodes on battle-soccer with real Claude, GPT and Qwen workers | Replay of past blocked commands | Real path shapes and how often prompts occur on case work |
| Worker correction | Seeded prompts in live panes | Fake-tmux tests | Nothing |
| Review convergence | Replay on tmux-pi's own history; live review-loop tasks | None | Rounds per step on real cases |
| Evidence and test selection | M2 replay; episodes E3 and E4 | A small Deno and nx fixture app for the gate | Suite timing and the database test runner |
| Holder line | Live sessions | Provider outage through `provider-health.json` | Waits on CI, merge requests and tracker approvals |
| Fallow | battle-soccer | The same Deno and nx fixture | Entry points and plugins in the real monorepo |
| Handoff, merge request, tracker | Nothing | A stub tracker and git host, including the lost-response case (H21) | The real services |
| Scale | Nothing | A synthetic large diff against packet limits | Real packet and review sizes |

### On the personal Mac

1. Freeze a baseline. Tag `5ac86c6`, write episodes E1 to E8 with hidden tests, and run the baseline five times.
2. Label one past campaign's interventions as process reminder or product decision. This number re-ranks everything that follows.
3. Run the replays, which need no new worker runs: the blocked-command fixture; bar labels and one property per family on F1, FG-N1 and LS1c; the H13 ast-grep replay; the M2 selection replay; a co-change script on full history.
4. Build priority 1 behind a switch, give it a security review, and run E1, E2 and E8.
5. Add the acceptance bar and the earlier sweep, and run E6. Add property tests only for families where the replay worked.
6. Add the holder line read-only, then the event file, then the wake. Run E7.
7. Add the evidence command and the facts file. Run E3 and E4.
8. Run the four-arm ablation for priority 5 with E5.
9. Remove the legacy and strict paths, rerun every episode, and run your G1 gate on both Pythons.
10. Do the port rehearsal (your G5), with a version manifest.

### On the work Mac

1. Record versions before anything else: Claude Code, Pi, tmux, Python and git. `pane_send.py` documents behavior specific to Claude Code 2.1.322, tmux 3.7c and Pi 1.1.0, so a version change can break delivery silently.
2. Run five canaries: a message is confirmed submitted; a deny reason reaches the worker; the notifier wakes Main; an open prompt is detected; the trust dialog is recognized.
3. Spend the first week in observe mode. The new hook logs what it would deny and still asks. The holder line and event file are on. Nothing else changes.
4. In the second week, enable priority 1 and the acceptance bar on one campaign. Keep one switch per feature so any of them can be turned off alone.
5. Run Fallow and the selection map on the monorepo in report-only form.
6. Bring back metadata only, if your employer allows it: event files and decision snapshots, never repository content. Tune on the personal Mac and repeat.

### Differences to plan for

- **Configuration.** Machine-specific values belong in `config/local.json`, which already holds model rules. Work endpoints currently sit in the tracked `claude/mcp.json`.
- **Models.** The catalog already handles a missing provider. Record which levels resolve to which model on each machine, since results will differ.
- **Resources.** `max_active_workers` is 10 and `max_local_workers` is 2 in tracked config. A per-machine override avoids tuning one for the other.
- **Python.** Your gate runs 3.9.6 and 3.14. Any new test dependency must be checked on both, or confined to one.

## Most consequential unanswered questions

The first question outranks the rest, because its answer could reorder the five priorities.

1. **What share of your interventions are process reminders, and which reminders?** I had no transcripts. One labelled campaign would show whether prompts, review rounds or waiting costs you most.
2. **What does Main actually load at work, and how large is it at first dispatch?** If the whole prompt folder is in context, priority 5 changes more than I assumed. If Main reads it selectively, less.
3. **Where is hostile-input robustness truly required?** The bar per area decides how much review effort is waste. My assumption is: the trust, redaction and sensitive-read path only.
4. **Is redacted output always acceptable as the default read path?** Priority 1 depends on trusting `safe_grep` and `safe_read`. One leak has already occurred.
5. **Which Main is the target?** The notifier, wake notices and post-compaction injection exist only for Claude. Pi Main is told that nothing notifies it. Investing further in the Claude plugin deepens that split.
6. **Which parts do you want to own in six months?** If native orchestration matures, the lasting value is in lanes with evidence, cross-provider review and redaction. That argues for shrinking everything else now.
7. **Is test cost in the breadth of the suite or in a dozen slow files?** A per-file time table answers it in one run and decides between selection and speed-up work.
8. **May session metadata leave the work Mac?** Ideas A and C, and step 6 of the work sequence, depend on it.
9. **Does the router earn its keep for strict-floor roles?** The selection log can answer this today.

## What a successful session should feel like

You state what you want and answer questions about the product. You never explain how tmux-pi works.

- **Workers rarely stop.** When one does, Main redirects it and tells you afterward. If the stop needs your authority, the request arrives with its evidence.
- **One glance tells you whose move it is.** If it is yours, the question is on screen in full. If it is Main's, it does not stay that way for long.
- **Reviews end.** A step closes in one or two rounds, or stops at three with a design question for you.
- **Checks run once per tree.** The full suite runs at integration, not in lanes.
- **Leaving is cheap.** On return, a short brief tells you what changed and what waits on you.
- **Nothing is asked twice.** After a compaction or restart, settled decisions stay settled.
- **"Done" can be opened.** It points to a tree, a review of that tree and fresh checks.
- **Silence means progress.** You are not watching panes to find out.

### Measurements that show you are getting closer

| Measure | Now | Direction |
| --- | --- | --- |
| Process reminders per session | Unknown; label one campaign | Toward zero |
| Prompt stops that need a person | 8 of 17 reviews before K1; unmeasured since | 1 in 20 tasks or fewer |
| Prompts approved by a keystroke from Main | 2 recorded | Zero |
| Rounds to approval | Up to 21; several lanes at 5 to 9 | Median of 2; none past 3 without a design step |
| Avoidable waiting | Unknown; derive from task records | Halved |
| Duplicate checks on an unchanged tree | Unknown; count from records | Zero |
| Stale evidence accepted | Not checked | Zero |
| Recovery with no human input | Unmeasured | 9 of 10 episodes |
| False completion claims | Unmeasured | Zero |
| Time from return to first correct instruction | Unmeasured | Under a minute on most returns |
| Product decisions asked | Unmeasured | No target; it should not fall to zero, which would mean Main is guessing |
| Harness size | About 75,000 lines of non-test code; rules in three to five copies | Down |

The last two rows are guards. Fewer interruptions bought by guessing, or by more machinery, would not be progress.

## Sources

**Repository** (all at commit `5ac86c6`): the files listed under Scope and evidence. Figures attributed to you come from `docs/plans/det-prep/run-log.md`, `release-gates.md`, `loop/ideas.md`, `docs/plans/realistic-use-bar.md` and `docs/prompt-examples/07-known-failures.md`.

**Pages opened**

- [Claude Code hooks reference](https://code.claude.com/docs/en/hooks). I read the first third of this page: PreToolUse decisions, the deny reason shown to the model, and the event list.
- [Harness design for long-running application development](https://www.anthropic.com/engineering/harness-design-long-running-apps), Anthropic, 24 March 2026. Source for removing harness components one at a time as models improve.
- [Effective harnesses for long-running agents](https://anthropic.com/engineering/effective-harnesses-for-long-running-agents), Anthropic, 26 November 2025. Background: progress files and a session-start routine, which your run-log practice already resembles.
- [fallow-cli README, version 2.76.0](https://docs.rs/crate/fallow-cli/2.76.0/source/README.md). Languages, commands, agent support, syntactic analysis.
- [git-xray-cli on PyPI](https://pypi.org/project/git-xray-cli/). Version 0.1.2, pure Python, coupling formula.
- [code-maat-python on PyPI](https://pypi.org/project/code-maat-python/). Version 0.2.0, alpha, GPL-3.
- [Resumption Strategies for Interrupted Programming Tasks](https://sites.cc.gatech.edu/reverse/repository/resumptionstrategies.pdf), Parnin and Rugaber.
- [Evaluating Cues for Resuming Interrupted Programming Tasks](https://www.microsoft.com/en-us/research/publication/evaluating-cues-for-resuming-interrupted-programming-tasks/), DeLine and Parnin, CHI 2010.

**Seen as search-result excerpts only**

- [Context Rot](https://trychroma.com/research/context-rot), Chroma technical report, July 2025.
- [Hypothesis: stateful tests](https://hypothesis.readthedocs.io/en/latest/stateful.html).
- [pytest-testmon](https://pypi.org/project/pytest-testmon/0.9.16), project description.
- [Ironies of Automation](https://gwern.net/doc/sociology/technology/1983-bainbridge.pdf), Bainbridge, Automatica, 1983.
- Third-party guides on Claude Code agent teams. I did not check these against Anthropic's own documentation.

**Added for the local-model section.** Repository: `config/model-catalog.json` (local level, shapes, role floors), `config/local.example.json`, `tests/calibration/experiments.md`, `agent/extensions/assistant.ts`, `lib/redact_llm.py`, `lib/trial_shadow.py`, `docs/plans/det-prep/g1-local-model-load.md`, `m1-model-profiles.md`, `x1-trace-distillation.md`, and the run-log entries for J1 and LS1c. Web, as search-result excerpts only: [LM Studio structured output](https://lmstudio.ai/docs/app/api/structured-output), and model cards for Qwen 3.5 and 3.6 35B-A3B builds, one of which reports the no-thinking toggle not being honored in LM Studio.

**Not sourced.** The descriptions of process mining and of replay in durable-workflow systems are general background. fast-check and ast-grep are named from your own plans; I did not review their current documentation.
