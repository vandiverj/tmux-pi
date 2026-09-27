# Calibration labels

Each task in `corpus.jsonl` is labeled with what it *should* get. Label from the
task text alone, as the router sees it.

## intent

The primary requested outcome, even when other work is needed on the way.

| label | the task asks for |
|---|---|
| research | facts, findings, comparisons or answers; nothing is planned or changed |
| planning | a plan, design, recommendation or decision about future work |
| implementation | code or project files written, changed, fixed, refactored or tested |
| configuration_or_operations | systems operated or configured: services, infra, deploys, data, credentials, repo settings |
| review | a judgement of existing work: code, a plan, a result, a design |
| communication_or_documentation | prose for people: explanations, summaries, messages, notes, docs |

## route

The cheapest tier that should reliably do the task. `expert` and `escalated`
share a target (Opus/high) and are scored as the same route.

| label | when |
|---|---|
| simple | answerable from general knowledge; no tools or project files; short |
| standard | routine and small, but needs tools or project files: look something up, a tiny edit, run a command, summarize a file |
| coding | ordinary software work: a feature, bug fix, tests, a script, a focused investigation or design |
| complex | multi-part work needing careful reasoning across several components |
| expert | frontier-hard: concurrency or distributed correctness, novel algorithms, deep security architecture |
| escalated | touches secrets, credentials, security, private or production data, or is destructive or irreversible, whatever its difficulty |

## review

| intent | label |
|---|---|
| research | none |
| communication_or_documentation | none; optional when about security or an incident |
| review | none; optional when the review is security-critical |
| planning | none when trivial, optional when routine, required when complex or risky |
| implementation | required; optional for purely cosmetic edits |
| configuration_or_operations | required |

## split

`tune` tasks drive changes. `holdout` tasks are only checked, never used to
choose a change, so a change that merely fits the tune set shows up as a
holdout regression.

## placement

Where the main agent should run the request.

| label | when |
|---|---|
| subagent | implementation or configuration/operations; required review; sustained work (an investigation, profiling, an audit, a full review of an artifact, a whole document, a multi-part plan); or anything that writes project files |
| main | a quick answer, lookup, short explanation, short draft, a quick review of something small, or a simple recommendation |

## follow-ups (`followups.jsonl`)

`follow_up` is true when the message is meant for the worker on the active task:
it answers the worker's question, corrects it, asks about its work, or extends
or continues the same task. It is false for a separate question or piece of
work, even in the same project or about the same files.

