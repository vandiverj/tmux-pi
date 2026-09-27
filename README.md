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

A Claude worker stopped at a choice prompt (Claude Code's one-time folder trust
in a new directory, or a tool permission) is shown in main as a dialog with the
prompt's own options; your pick is keyed into the worker. The dialog closes if
you answer in the worker's pane instead. To settle folder trust ahead of time:

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
| `/reset` | Stops all subagents, clears the viewer, and starts a fresh main Pi session |

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
tp-reset           # stop every subagent and restore the viewer (used by /reset)
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

## Automatic routing

The main agent routes every message itself (`agent/extensions/dispatcher.ts`).
Before the model sees a message, `bin/tp-route --dispatch` asks Laya once and
returns the route, intent, review level, execution scope and placement:

- **main:** brief work that changes nothing and needs no required review. A pi
  route switches the main model and thinking level in place; the footer shows
  `AUTO main · <model> · <route> · review <level>`.
- **subagent:** implementation, configuration or operations, required review,
  or sustained work. The dispatcher spawns the routed worker itself; the main
  model takes no turn. The worker's packet is the verbatim request, what was
  said since the last delegated task, and two standing instructions: ask before
  acting if a wrong guess about the intent would change files or waste
  significant work, and finish with a report of what was done and how it was
  checked.

Progress is posted into the main conversation as `tmux-pi-auto` messages
(delegated, a worker's question, the result), so main also sees them on its next
turn. When review is required, a cross-provider reviewer (`tp-review`) starts as
soon as the author reports. If it requests changes, its findings go back to the
author once, and the author is reviewed again. A worker that ends its reply
with a question is not reviewed until you answer.

While delegated tasks are still running, each new message is first checked
against them (`tp-route --follow-up`). A message for one of them is typed into
that worker and saved to its `followups.md`, which its reviewer also reads. When
the judge is unsure, or two tasks could claim the message, you pick. With the
`followup` key in `config/local.json`, the local MLX model judges; without it,
Laya does, and asks you more often.

Start a message with `+followup` to send it to a running task without the
judge: `+followup <id> …` names the task (any live subagent, including one main
started), otherwise it goes to the only running delegated task or you pick one.
Start a message with `+here`, `+delegate` or `+review` to override placement or
review (they can be combined; the route stays a validated roster target; a
marked message is never treated as a follow-up). `/auto off` turns routing off
for the session. If Laya is unavailable, nothing changes and the footer says so.
If a spawn fails, main is told to delegate by hand. A routed model whose context
window cannot hold the conversation is skipped for the home model (the default
in `agent/settings.json`). Claude routes placed in main stay on the home model
until they run as foreground workers.

## Per-machine models

Different machines have different local models, so what each tier runs on is
set per machine in `config/local.json` (git-ignored). Start from the committed
[`config/local.example.json`](config/local.example.json); it holds no
credentials. Every key is optional:

- `routes` overrides tier assignments from `config/router.json`
  (`simple`, `standard`, `coding`, `complex`, `expert`); each target must be in
  the `models` roster there.
- `mlx` is the local model `bin/tp-mlx` serves: its id, port and sampling.
- `escalation` names the models whose pi workers hand off after too many failed
  tool calls in a row, and what they hand off to.
- `rules` redirect a task type a model is known to get wrong: when the router
  picks a model in `from` and the task text matches `pattern`, it uses `to`.
- `prompts` add standing system-prompt text to a model's workers (pi or
  Claude), for a mistake it is known to make; `tp-spawn` appends it.
- `followup` names a fast local chat model (loopback OpenAI-compatible URL,
  model id, and the `ask_between` probability band where the user is asked)
  that decides whether a message belongs to a running delegated task.

On a machine without Slotstream or MLX, leave those out and point `routes` at
models it does have. `bin/tp-route --check` validates the result;
`TP_LOCAL_CONFIG` points at a different file.

### Local MLX server

`bin/tp-mlx` serves the `mlx` model with mlx-lm, bound to `127.0.0.1` and
offline (a request for any other model fails instead of downloading one). pi
reaches it as the `mlx` provider in [`agent/models.json`](agent/models.json),
whose model id must match. Bootstrap once:

```sh
uv venv .mlx/venv --python 3.12
VIRTUAL_ENV=.mlx/venv uv pip install "mlx-lm==0.31.3" "mlx==0.32.2"
.mlx/venv/bin/hf download mlx-community/Qwen3.5-4B-MLX-4bit
bin/tp-mlx start        # also: status, stop
```

Qwen3.5-4B (4-bit, thinking off) handles `standard`-tier tool chores on this
machine: on 40 repository chores it answered 92.5% correctly with a median of
1.5 s, against 13 s for Slotstream. Its recurring mistake is exact
counting: it reports a number without computing it. Its workers get a prompt to
answer counts only from a counting command it ran (76% → 90% correct on
counting chores), and a rule sends tasks that ask for a count to Slotstream
(10/10).

### Escalation

A pi worker on an `escalation` model counts failed tool calls; a success resets
the count. Past `after_consecutive_errors` (3), it writes `escalation.md` in
its task dir with every tool call it made (arguments, errors, results) and its
last reply, spawns the `to` model (Claude Haiku) with the original packet plus
that file, records the new id in `escalated` and `meta.json`, and exits.
`collect_result` on the old id points to the new one.

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
