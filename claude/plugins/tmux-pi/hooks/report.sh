#!/usr/bin/env bash
# report.sh — Claude Code hook that writes a tmux-pi subagent's live state.
#
# Same files as the pi reporter extension (agent/extensions/reporter.ts):
#   $TP_TASK_DIR/state.json  state, current tool, turns, tool calls, failed tool
#                            calls in a row (toolErrors), tokens, model
#   $TP_TASK_DIR/result.md   last assistant reply, written on Stop
#
# Does nothing outside a tp-spawn worker (no TP_TASK_DIR). Never fails the hook:
# reporting must not block or change what Claude does.
[[ -n "${TP_TASK_DIR:-}" && -d "$TP_TASK_DIR" ]] || exit 0
# Hold a per-task advisory lock across the read, result and state writes. Python's
# fcntl lock works on macOS (unlike flock(1)) and releases on process death. Hooks
# can overlap; a shared .tmp name or an unlocked read/modify/write loses Stop.
# The lock file intentionally remains in the task directory after each invocation.
if [[ "${1:-}" != --locked ]]; then
  python3 -c '
import fcntl, subprocess, sys
with open(sys.argv[2], "a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    subprocess.run(["bash", sys.argv[1], "--locked"], input=sys.stdin.buffer.read())
' "$0" "$TP_TASK_DIR/.report.lock" 2>/dev/null || true
  exit 0
fi
input="$(cat)"
state="$TP_TASK_DIR/state.json"
now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
prev='{}'
if [[ -f "$state" ]]; then prev="$(cat "$state")"; fi
# Malformed hook input must not turn an old result into a new settled reply.
event="$(jq -r '.hook_event_name // empty' <<<"$input" 2>/dev/null)" || exit 0

# A new prompt invalidates the prior reply BEFORE changing state; reviewers
# cannot see a waiting state alongside a previous turn's result. A Stop only
# publishes a reply for an open prompt. Duplicate/late Stop cannot republish it.
active="$(jq -rn --argjson prev "$prev" '
  def turn: ($prev.promptTurn // (if $prev.state == "working" or $prev.state == "tool" or $prev.state == "permission" then 1 else 0 end));
  def settled: ($prev.settledTurn // (if $prev.state == "waiting" then turn else 0 end));
  if turn > settled then "yes" else "no" end' 2>/dev/null)" || exit 0
if [[ "$event" == UserPromptSubmit ]]; then
  rm -f "$TP_TASK_DIR/result.md"
elif [[ "$event" == Stop && "$active" == yes ]]; then
  result_tmp="$(mktemp "$TP_TASK_DIR/.result.XXXXXXXX")" || exit 0
  if jq -er '.last_assistant_message // empty | select(length > 0)' <<<"$input" > "$result_tmp" && [[ -s "$result_tmp" ]]; then
    mv "$result_tmp" "$TP_TASK_DIR/result.md" || exit 0
  else
    rm -f "$result_tmp" "$TP_TASK_DIR/result.md"
  fi
fi

# Token totals come from the transcript. At Stop it can still lack the final
# message, so totals are recomputed on the next prompt too. Streaming writes one
# line per content block with the same message id and usage, so dedupe by id (the
# last line per id wins). byModel groups those unique messages by message.model;
# empty and <synthetic> (Claude Code's zero-usage error/interrupt placeholder) are
# left out of byModel and never become state.model (the last real model is kept).
# Only UserPromptSubmit and Stop recompute: reading the whole transcript on every
# tool event would be O(n^2). Caveat: Stop can fire before the final message is
# flushed, so the last turn may be missing until the next prompt, which a one-shot
# worker may never get; totals are a lower bound mid-turn and possibly after Stop.
usage='null'
transcript="$(jq -r '.transcript_path // empty' <<<"$input")"
if [[ -n "$transcript" ]]; then
  printf '%s\n' "$transcript" > "$TP_TASK_DIR/transcript-path.tmp"
  mv "$TP_TASK_DIR/transcript-path.tmp" "$TP_TASK_DIR/transcript-path"
fi
if [[ ( "$event" == Stop || "$event" == UserPromptSubmit ) && -f "$transcript" ]]; then
  usage="$(jq -s '
    def tok: { input: (map(.input_tokens // 0) | add // 0),
               output: (map(.output_tokens // 0) | add // 0),
               cacheRead: (map(.cache_read_input_tokens // 0) | add // 0),
               cacheWrite: (map(.cache_creation_input_tokens // 0) | add // 0) };
    def real: . != null and . != "" and . != "<synthetic>";
    [ .[] | select(.type == "assistant" and .message.usage != null) ] as $a
    | ($a | group_by(.message.id) | map(last)) as $m
    | { turns: ($m | length),
        tokens: ($m | map(.message.usage) | tok),
        byModel: ($m | map(select(.message.model | real)) | group_by(.message.model)
                  | map({ key: .[0].message.model, value: (map(.message.usage) | tok) }) | from_entries),
        model: ([ $a[].message.model | select(real) ] | last // "") }
  ' "$transcript" 2>/dev/null)" || usage='null'
  if [[ -z "$usage" ]]; then usage='null'; fi
fi

state_tmp="$(mktemp "$TP_TASK_DIR/.state.XXXXXXXX")" || exit 0
jq -n --argjson prev "$prev" --argjson in "$input" --argjson usage "$usage" \
  --arg id "${TP_TASK_ID:-}" --arg now "$now" '
  def turn: ($prev.promptTurn // (if $prev.state == "working" or $prev.state == "tool" or $prev.state == "permission" then 1 else 0 end));
  def settled: ($prev.settledTurn // (if $prev.state == "waiting" then turn else 0 end));
  def active: turn > settled;
  def hint: ((.tool_input.file_path // .tool_input.path // .tool_input.command // .tool_input.pattern // "") | tostring);
  def clip: gsub("\\s+"; " ") | if length > 80 then .[0:79] + "…" else . end;
  # Transcripts carry no cost. cost stays numeric 0 because lib/goal.py treats a
  # non-numeric cost as unmetered and fails closed; costKnown:false marks it unknown.
  { id: $id, harness: "claude", state: "starting", tool: "", model: "", turns: 0, tools: 0, toolErrors: 0,
    tokens: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, cost: 0, costKnown: false, error: "" }
  + $prev
  + ( if $in.hook_event_name == "SessionStart" then { state: "starting", model: ($in.model // $prev.model // "") }
      elif $in.hook_event_name == "UserPromptSubmit" then
        { state: "working", tool: "", error: "", promptTurn: (turn + 1), settledTurn: settled }
      elif $in.hook_event_name == "PreToolUse" and active then
        { state: "tool", tool: ("\($in.tool_name) \($in | hint)" | clip), tools: (($prev.tools // 0) + 1) }
      elif $in.hook_event_name == "PostToolUse" and active then { state: "working", tool: "", toolErrors: 0 }
      elif $in.hook_event_name == "PostToolUseFailure" and active then
        { state: "working", tool: "", toolErrors: (($prev.toolErrors // 0) + 1) }
      elif $in.hook_event_name == "Notification" and active then
        (if ($in.message // "" | test("permission"; "i")) then { state: "permission", tool: ($in.message | clip) } else {} end)
      elif $in.hook_event_name == "Stop" and active then
        { state: "waiting", tool: "", promptTurn: turn, settledTurn: turn }
      else {} end )
  + ( if ($in.hook_event_name == "UserPromptSubmit" or ($in.hook_event_name == "Stop" and active)) and $usage != null
      then $usage | if .model == "" then del(.model) else . end else {} end )
  + { updated: $now }
' > "$state_tmp" 2>/dev/null && mv "$state_tmp" "$state"
rm -f "$state_tmp"
exit 0
