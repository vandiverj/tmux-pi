# tmux-pi

A tmux workspace for running a main [pi](https://github.com/earendil-works/pi) agent
that delegates to subagents you can watch live.

```
┌──────────────┬──────────────┐
│              │ status       │
│ main pi      ├──────────────┤
│              │ viewer       │
└──────────────┴──────────────┘
```

## Requirements

- tmux 3.3+ (`brew install tmux`)
- pi

## Run

```sh
bin/tp            # workspace for the current directory
bin/tp ~/code/x   # workspace for another directory
```

To launch it as `agent` from any directory:

```sh
ln -s "$PWD/bin/tp" ~/.local/bin/agent
```

Re-running `tp` reattaches to the existing session. Set `TP_SESSION` to run more than one.

tmux-pi is self-contained: pi (`bin/tp-pi`) and Claude Code workers (`bin/tp-claude`) load only
this repo's `agent/`, `claude/` and `skills/`, never `~/.pi`, `~/.agents` or `~/.claude`.
Run `/login` once inside the workspace for pi's subscription providers.

Before launching Claude workers in a new target directory, complete Claude Code's
one-time folder trust with the same isolated configuration used by workers:

```sh
bin/tp-claude-bootstrap /path/to/target-project
# approve trust if prompted, then type /exit
```

## Subagents

Ask the main agent to delegate. Its subagents extension
(`agent/extensions/subagents.ts`) gives it these tools:

| Tool | Does |
|---|---|
| `spawn_subagent` | Builds the context packet from the task, `files` (whole or line ranges) and `notes`; routes the task with `tp-route` unless a model is given; then `tp-spawn`s it |
| `list_subagents` | `tp-list` |
| `send_to_subagent` | Types a follow-up message into the subagent's session |
| `collect_result` | State and last reply; `wait_seconds` waits until it finishes. A subagent stuck at a prompt (Claude's folder trust, a permission request) comes back with its screen |
| `kill_subagent` | `tp-kill` |

Inside a tmux-pi session the `tp-*` commands are on PATH (for you and the agents):

```sh
tp-spawn -w claude -m sonnet -t high "Refactor the auth module"   # prints an id
tp-spawn -m slotstream/qwen3.8-flash-next:4bit -f task.md -v       # pi worker, view now
tp-spawn -c src/auth.ts -c src/db.ts:40-90 -N "tokens expire after 1h" "Fix the refresh bug"
tp-list            # subagents, state, tokens, cost, current tool
tp-view <id>       # swap its live session into the viewer (click in to type)
tp-view -          # put the placeholder back
tp-kill <id>       # stop it; task files stay in .tmux-pi/tasks/<id>/
tp-clean           # archive killed subagents to .tmux-pi/archive/
```

The status pane (top right) is the same list, live. Click it, then:

| Key | Action |
|---|---|
| `j` / `k`, `↑` / `↓` | move |
| `enter` | show the selected subagent in the viewer |
| `o` | show it and move focus into it, to type to it |
| `-` | clear the viewer |
| `r` / `t` | read its last reply / its context packet in a popup |
| `x` | kill it (asks first) |
| `c` | archive killed subagents |

States come from each worker's reporter: `starting`, `working`, `tool` (the tool and
its argument show below the row), `waiting` (finished its turn, ready for more),
`permission` (Claude is asking to use a tool), `error`. Once the process ends:
`done`, `failed:<code>` or `killed`.

### Context packets

A worker's first message is its `packet.md`: the task, then each file passed with
`-c path` or `-c path:start-end` (in a code fence, labelled with its path and range),
then the `-N` notes. Workers load no `AGENTS.md` or `CLAUDE.md` and never see
main's conversation, so the packet is all the project context they start with;
they can still read other files with their tools. Limits: 60k characters per file
and 200k per packet (use line ranges for big files). Credential files (`.env*`,
keys, `auth.json`, `*secret*` and similar) are refused.

Each task dir holds `task.md`, `packet.md`, `meta.json`, the generated `run.sh`, and the reporter's `state.json`
and `result.md` (the last reply). pi workers report through
`agent/extensions/reporter.ts`, Claude workers through the tmux-pi plugin's hooks
(`claude/plugins/tmux-pi/hooks/`). Token counts include cached input; cost is shown
only for pi providers that price tokens.

See [docs/plan.md](docs/plan.md) for the design and milestones.

## Local model router

`bin/tp-route` uses Laya's `typed-decisions` checkpoint at
`127.0.0.1:11436/v1/systemone` by default. It asks six bounded questions: the
task's intent (a six-way choice), difficulty on a four-level scale, and as
yes/no probabilities whether it needs tools, is sensitive, is high-risk, and
changes files, systems or data. A small learned layer,
[`config/laya-model.json`](config/laya-model.json), reads all of Laya's
probabilities and picks a route from
[`config/router.json`](config/router.json), whose target must be in the allowed
`tp-spawn` roster. The output is exactly `worker`, `model`, and `thinking`;
classification alone never spawns a worker.

Bootstrap once when `.laya/src` and `.laya/venv` are absent:

```sh
mkdir -p .laya
git clone https://github.com/NandhaKishorM/laya.git .laya/src
git -C .laya/src checkout 4066d5d5fbf08b66c6757ddeedbd797bd7655bc0 # verified Laya 0.3.20
python3.11 -m venv .laya/venv # Python 3.10+ required; use your compatible interpreter
.laya/venv/bin/python -m pip install -e '.laya/src[serve]'
.laya/venv/bin/python -c 'from laya import Router; Router().preload(["typed-decisions"])'
```

The final bootstrap command fetches the checkpoint into the local cache.
`bin/tp-laya` itself runs offline: it never installs packages or downloads
weights. It binds only `127.0.0.1`, preloads only `typed-decisions`, chooses
MPS when available (otherwise CPU), and logs under ignored `.laya/`. Set
`TP_LAYA_DEVICE=cpu` or another supported torch device to override selection.
It refuses to start if its venv is missing or port 11436 already has a listener.

```sh
bin/tp-laya start
bin/tp-laya status
curl --noproxy '*' http://127.0.0.1:11436/health
bin/tp-route --check
bin/tp-route 'Review the authentication flow'
bin/tp-route --file task.md
bin/tp-laya stop                 # stops only a process started by tp-laya
```

An independently started Laya service can be used on the same endpoint.
`tp-laya stop` leaves such a service alone; stop it with its own process manager.

The routes are `simple` (Slotstream/off), `standard` (Slotstream/low),
`coding` (GPT-5.6 Terra/medium), `complex` (GPT-6 Sol/high) and `expert`
(Claude Opus/high, also used for sensitive or risky work). The learned layer is
multinomial logistic regression over Laya's answer probabilities, trained on
labeled tasks (see [Calibration](#calibration)); inference is plain Python.
Laya's answers carry limited information about how hard a task is: the route
matches the labeled tier for about 55% of held-out tasks and the labeled model
for about 66%, and it sends about 22% of tasks to a cheaper tier than labeled.
Pass an explicit model to `spawn_subagent` or `tp-spawn` when that matters.

The roster also permits Claude Haiku/Sonnet/Opus, GPT-5.3 Codex Spark,
GPT-5.5, GPT-5.6 Luna/Terra/Sol, and GPT-6 Luna/Sol/Astra for deliberate
policy changes. The installed Pi provider data lists 128k context and
$1.75/$14 input/output per million tokens for Spark; the remaining Codex
models have 272k context. Within 5.6, Luna ($0.20/$1.20), Terra ($2/$12), and
Sol ($4/$20) form the cost ladder; within 6, Luna ($0.10/$0.50), Sol ($2/$10),
and Astra ($10/$50) do likewise. Terra is the current coding tier and Sol the
current complex tier; the more expensive or less suitable entries are allowed
but not selected automatically. There is no GPT-6 Terra entry in the installed
provider data.

The `models` array is the allowed spawn roster; `routes` must point into it.
It lists eligible targets, not a guarantee that those providers are available.
The adapter accepts only unauthenticated loopback HTTP, disables proxies and
redirects, validates responses and route targets, and fails clearly on server
or data errors.

## Cross-provider review loop

After a subagent successfully exits, run `bin/tp-review <task-id>` (or use the
main agent's `review_subagent` tool). It creates a reviewer on the other
provider: Pi-authored work is reviewed by Claude Sonnet, and Claude-authored
work by Pi/Codex. The reviewer receives the original task, packet, and result,
then independently checks the working tree. It is instructed not to edit and
returns `APPROVED` or actionable `REQUEST_CHANGES`. Review linkage is recorded
in the author task's `meta.json`; use `collect_result` on the printed review id.

Before starting a reviewer, `tp-review` asks the review policy about the author's
task (`bin/tp-route --review --file task.md`), which returns `none`, `optional`
or `required` from the same Laya answers:

- research is never reviewed (it changes nothing); the learned layer reads the
  intent from all of Laya's answers, which is more accurate than Laya's own
  intent choice (0.78 against 0.74 on held-out tasks)
- implementation and configuration/operations work, and anything Laya calls
  sensitive (≥ 0.5), is always reviewed
- otherwise a learned review/no-review classifier decides
- a reviewed task is `required` unless a second classifier is confident it is
  only `optional`

On held-out tasks this never skipped a review labeled required, decided
review-or-not correctly for 92%, and ran 6 reviews in 120 that were not needed.

On `none`, `tp-review` starts nothing and exits 3. `tp-review --force <id>` (or
`review_subagent` with `force`) reviews regardless; if Laya is unreachable the
review runs anyway. The level used is recorded as `review_policy` in the author's
`meta.json`. Reviews still start only when asked; nothing hands off to review
automatically yet.

## Calibration

`tests/calibration/corpus.jsonl` holds 300 labeled tasks: for each, the
intent, route and review level it *should* get, following
[`LABELS.md`](tests/calibration/LABELS.md), and a split. The learned layer is
trained on the 180 `tune` tasks only; the 120 `holdout` tasks measure it.
`tests/calibration/answers.jsonl` records Laya's replies (typed-decisions is
deterministic, so a recording is a stable baseline), tagged with a digest of
the questions asked. `bin/tp-calibrate` scores the model per split without
contacting Laya:

```sh
bin/tp-calibrate               # score the model offline
bin/tp-calibrate --mismatches  # also list every wrong decision
bin/tp-calibrate --record      # ask Laya about new or edited tasks
bin/tp-calibrate --refresh     # ask Laya about every task again (e.g. after upgrading it)
bin/tp-calibrate --fit         # retrain config/laya-model.json (needs numpy)
```

Changing `QUESTIONS` in `lib/router.py` invalidates the recording and the model:
run `--record --fit`. Every change tried so far, with its tune and holdout
scores and whether it was kept, is in
[`experiments.md`](tests/calibration/experiments.md); check it before trying
another. The tests fail if a task has no current recorded answer, if the model
skips a review labeled required, or if `--fit` would not reproduce the shipped
model.

The previous Slotstream OpenAI-compatible chat classifier remains available
only by explicit selection:

```sh
bin/tp-route --backend openai --file task.md
# For another already-running local chat endpoint:
bin/tp-route --backend openai --base-url http://127.0.0.1:1234/v1 \
  --router-model your-loaded-model-id --file task.md
```

`TP_ROUTER_BACKEND`, `TP_ROUTER_BASE_URL`, and `TP_ROUTER_MODEL` are equivalent
environment overrides. The OpenAI-compatible endpoint must return a complete
`choices[0].message.content` JSON object with `model` and `thinking` in the
roster. The Laya backend never asks the checkpoint to emit raw model IDs.

`spawn_subagent` does this handoff. From a shell:

```sh
decision="$(bin/tp-route --file task.md)" || exit 1
bin/tp-spawn --worker "$(jq -r .worker <<<"$decision")" \
  --model "$(jq -r .model <<<"$decision")" \
  --thinking "$(jq -r .thinking <<<"$decision")" --file task.md
```
