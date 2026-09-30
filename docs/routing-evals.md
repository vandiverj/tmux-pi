# Experimental Laya model/effort selection and routing evals

Status: experimental, on by default (2026-09-29). Evidence only: nothing here grants dispatch,
edit or review authority, enables an automation cell, or changes a main-owned contract.

## What was already live, and what this adds

Already live before this change:

- `config/router.json` selects the `laya` backend. For every main-agent request, the dispatcher
  runs `tp-route --dispatch`: Laya's typed-decisions answers feed the learned heads, which pick
  a route (and so a validated roster model and effort), an intent, a review level and a placement.
- Delegated workers already run on that decision's model and effort: `tp-spawn --model/--thinking`
  in `dispatcher.ts` `delegate()`. A main-placed Claude route runs as a foreground Claude worker
  on the decided model.
- The main agent does **not** switch models in place. R5 (docs/plan.md, 2026-09-28) removed the
  M8 in-place switch. The main model also frames the Orient/Frame contract, so swapping it per
  request would put the gate's framing on whatever model the route picked. That limitation stands:
  a main-placed Pi route is a recommendation only, and the log records `main_model: "unknown"`
  and `main_model_switched: false` for it.

This change adds:

1. **Claude-branch preference** (`claude_preference` in `config/router.json`). When Laya's decision
   is already a Claude worker (today the `expert` route), it re-picks among Claude models from
   Laya's bounded traits:
   - **heavy** is `claude-opus-5-5`/`high` when any of these hold: difficulty ≥ 2.5 (of 0–3),
     `is_high_risk` ≥ 0.5 or `is_sensitive` ≥ 0.5. Multi-step scope alone does not justify Opus;
   - **ordinary** is `claude-sonnet-5-5`/`medium` otherwise.

   It never moves a Pi/GPT-6 route to Claude, and never changes intent, review, placement or
   overrides; tests assert this. Local `rules` still apply after it, so a machine's known-failure
   redirects win. The OpenAI fallback backend is unchanged. `claude-sonnet-5-5` joined the roster;
   `claude-sonnet-5` remains as a compatibility roster entry; default independent review, research and packet-file selection now use `claude-sonnet-5-5`.
2. **Selection records.** Every `tp-route --dispatch` and plain `tp-route` call appends one record to
   `<state dir>/evals/routing-selections.jsonl`, the session's project-local `.tmux-pi`.
3. **Human outcomes and a joined export** via `bin/tp-evals`.

## Selection record (`tmux-pi-routing-selection:v1`)

| Field | Content |
|---|---|
| `selection_id` | `sel-<32 hex>`; also added to `--dispatch` output, so it flows into the handoff scaffold and `meta.json` `decision` of spawned tasks (escalations copy it) |
| `request` | `sha256` (same digest as the handshake's `request_sha256`), `task_sha256`, length/word/line counts, markers, has code fence/URL/path. **No text** unless `TP_ROUTING_EVALS_TEXT=1` (then ≤ 2000 chars) |
| `versions` | hashes of the questions, feature questions, classifier request, router config, local config, Laya model file, each learned head, the Claude preference and `lib/router.py` |
| `classifier` | backend, Laya's validated answers (choice, score, noul, confidence, probabilities, rounded), and the heads' probabilities (route, intent, review gate, required level, subagent placement) |
| `recommendation` | Laya route, route-table target, Claude preference (tier, reasons, changed), rule applied, final model/effort, intent, review, scope, placement, overrides |
| `actual` | main never switched; worker model/effort is joined at export |
| `status`/`error` | `router_error` records a failed classification (error type and a bounded message) |

Records are capped at 16 KiB: optional text, then heads, then scores are dropped first, and the drops
are listed in `truncated`. Appends use one `O_APPEND` write under an exclusive `flock`, with
`O_NOFOLLOW`, mode 0600 and a 64 MiB log cap. Readers count a torn line as malformed and skip it.
Raw transcripts are never collected, and nothing is written outside the session state dir.

**Failure is inert.** Any logging error prints `tp-route: routing eval not logged: …` to stderr. The
decision, exit status and policy stay identical, and `selection_id` is omitted (tests compare the
automation policy with and without logging). With no session state dir (outside tmux, no
`TP_STATE_DIR`), nothing is logged, silently. `TP_ROUTING_EVALS=0` turns logging off, and
`TP_ROUTING_EVAL_DIR` redirects it.

Not logged: `+research` (fixed `config/research.json` route, no Laya selection), follow-up
judgments, `--review`, and `--pick-files`.

## Outcomes and export

```sh
bin/tp-evals list                                 # recent selections
bin/tp-evals rate --selection sel-… --acceptance accepted_with_changes --sizing oversized \
                  --correct-model claude-sonnet-5-5 --correct-thinking low --note "…"
bin/tp-evals rate --task auto-123 --rating poor
bin/tp-evals export --out rows.jsonl              # summary counts on stderr
```

Ratings are `good|acceptable|poor|wrong`. Acceptance is `accepted|accepted_with_changes|rejected|unknown`.
Sizing is `right_sized|oversized|undersized|unknown`. Corrections must be valid roster model/effort pairs,
and notes are at most 1000 characters. Outcomes go to `routing-outcomes.jsonl` with `authority: "none"`.

Each export row (`tmux-pi-routing-eval-row:v1`) joins one selection to:

- tasks, by `meta.decision.selection_id`. Older tasks fall back to `decision.task` hash, used only when
  exactly one selection has that hash. The row records `link` either way.
- escalation targets, one hop.
- handshakes, from `handoffs/*/scaffold.json`.
- human outcomes.

Per task, it carries the actual worker/model/effort, state, exit, turns, tool errors, tokens, cost,
duration, the reviewer's verdict line, the `review-required.json` status, escalation ids and the names
of evidence files. Request text is never exported, including from scaffolds or opt-in records.

Labels are conservative:

- `acceptance` is `unknown` unless a human recorded it. A review verdict is listed separately and
  does not count as acceptance.
- `sizing` is `unknown` unless a human judged it. Oversizing needs a comparison run.
- An escalation signals a model or tool failure. It is not by itself a correctness label.

Selections and exports are raw material for later evals, instruction tuning, fine-tuning or an
alternative router. Nothing here retrains the learned layer or reads the frozen qualification set.
Retraining stays `bin/tp-calibrate` behind its tune/holdout gate.
