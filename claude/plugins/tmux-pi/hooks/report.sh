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
input="$(cat)"
state="$TP_TASK_DIR/state.json"
now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

prev='{}'
if [[ -f "$state" ]]; then prev="$(cat "$state")"; fi

event="$(jq -r '.hook_event_name // empty' <<<"$input")"

# The reply comes with the Stop event.
if [[ "$event" == Stop ]]; then
  jq -r '.last_assistant_message // empty' <<<"$input" > "$TP_TASK_DIR/result.md.tmp"
  if [[ -s "$TP_TASK_DIR/result.md.tmp" ]]; then
    mv "$TP_TASK_DIR/result.md.tmp" "$TP_TASK_DIR/result.md"
  else
    rm -f "$TP_TASK_DIR/result.md.tmp"
  fi
fi

# Token totals come from the transcript. At Stop it can still lack the final
# message, so totals are recomputed on the next prompt too. Streaming writes one
# line per content block with the same message id and usage, so dedupe by id.
usage='null'
transcript="$(jq -r '.transcript_path // empty' <<<"$input")"
if [[ ( "$event" == Stop || "$event" == UserPromptSubmit ) && -f "$transcript" ]]; then
  usage="$(jq -s '
    [ .[] | select(.type == "assistant" and .message.usage != null) ]
    | { turns: ([ .[].message.id ] | unique | length),
        tokens: (group_by(.message.id) | map(last.message.usage) | {
          input: (map(.input_tokens // 0) | add // 0),
          output: (map(.output_tokens // 0) | add // 0),
          cacheRead: (map(.cache_read_input_tokens // 0) | add // 0),
          cacheWrite: (map(.cache_creation_input_tokens // 0) | add // 0) }),
        model: (last.message.model // "") }
  ' "$transcript" 2>/dev/null)" || usage='null'
  if [[ -z "$usage" ]]; then usage='null'; fi
fi

jq -n --argjson prev "$prev" --argjson in "$input" --argjson usage "$usage" \
  --arg id "${TP_TASK_ID:-}" --arg now "$now" '
  def hint: ((.tool_input.file_path // .tool_input.path // .tool_input.command // .tool_input.pattern // "") | tostring);
  def clip: gsub("\\s+"; " ") | if length > 80 then .[0:79] + "…" else . end;
  { id: $id, harness: "claude", state: "starting", tool: "", model: "", turns: 0, tools: 0, toolErrors: 0,
    tokens: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, cost: 0, error: "" }
  + $prev
  + ( if $in.hook_event_name == "SessionStart" then { state: "starting", model: ($in.model // $prev.model // "") }
      elif $in.hook_event_name == "UserPromptSubmit" then { state: "working", error: "" }
      elif $in.hook_event_name == "PreToolUse" then
        { state: "tool", tool: ("\($in.tool_name) \($in | hint)" | clip), tools: (($prev.tools // 0) + 1) }
      elif $in.hook_event_name == "PostToolUse" then { state: "working", tool: "", toolErrors: 0 }
      elif $in.hook_event_name == "PostToolUseFailure" then
        { state: "working", tool: "", toolErrors: (($prev.toolErrors // 0) + 1) }
      elif $in.hook_event_name == "Notification" then
        (if ($in.message // "" | test("permission"; "i")) then { state: "permission", tool: ($in.message | clip) } else {} end)
      elif $in.hook_event_name == "Stop" then { state: "waiting", tool: "" }
      else {} end )
  + ( if $usage != null then $usage | if .model == "" then del(.model) else . end else {} end )
  + { updated: $now }
' > "$state.tmp" 2>/dev/null && mv "$state.tmp" "$state"
rm -f "$state.tmp"
exit 0
