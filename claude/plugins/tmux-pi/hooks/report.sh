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
# Every report is kept: before result.md is removed or replaced it is copied to results/<turn>.md (the turn that wrote it).
# A turn the worker did not start itself (a task notification from its own background shell, a hook-driven turn) must not
# replace a report that carries RESULT_STATUS with a reply that does not. Only a prompt from outside the worker (origin
# "human": a tp-agent message, a retry, a capability answer) may. The origin comes from the hook input when it carries one
# (origin.kind, turnOrigin, promptSource; a prompt that opens with <task-notification>), else, at Stop, from the newest
# origin-bearing user entry of the transcript; with neither it is "unknown" and the simpler rule holds: a reply without
# RESULT_STATUS never replaces one with it.
results_dir="$TP_TASK_DIR/results"
status_py="$(dirname "$0")/../../../../lib/result_status.py"  # the one RESULT_STATUS reader
has_status() { [[ -s "$1" && -f "$status_py" ]] && python3 "$status_py" "$1" >/dev/null 2>&1; }
norm_origin() { case "$1" in human|typed) echo human;; "") echo unknown;; *) echo "$1";; esac; }
archive() { # archive FILE TURN: copy FILE to results/TURN.md atomically
  mkdir -p "$results_dir" 2>/dev/null || return 0
  local tmp; tmp="$(mktemp "$results_dir/.keep.XXXXXXXX")" || return 0
  if cp "$1" "$tmp" 2>/dev/null; then mv "$tmp" "$results_dir/$2.md"; else rm -f "$tmp"; fi
}
prev_turn="$(jq -r '.promptTurn // 0' <<<"$prev" 2>/dev/null)" || prev_turn=0
result_turn="$(jq -r '.resultTurn // .promptTurn // 0' <<<"$prev" 2>/dev/null)" || result_turn=0
ext='{}'
if [[ "$event" == UserPromptSubmit ]]; then
  origin="$(jq -r '(try (.origin | objects | .kind) catch null) // .turnOrigin // .promptSource // empty' <<<"$input" 2>/dev/null)" || origin=""
  if [[ -z "$origin" ]] && [[ "$(jq -r '(.prompt // "" | tostring) | ltrimstr("\n") | startswith("<task-notification>")' <<<"$input" 2>/dev/null)" == true ]]; then
    origin=task-notification
  fi
  origin="$(norm_origin "$origin")"
  ext="$(jq -nc --arg o "$origin" '{promptOrigin: $o}')"
  if [[ -f "$TP_TASK_DIR/result.md" ]]; then
    archive "$TP_TASK_DIR/result.md" "$result_turn"
    if has_status "$TP_TASK_DIR/result.md" && [[ "$origin" != human && "$origin" != unknown ]]; then
      :  # an unprompted turn leaves the report in place
    else
      if has_status "$TP_TASK_DIR/result.md"; then held="results/$result_turn.md"; else held=""; fi
      ext="$(jq -c --arg h "$held" '. + {heldReport: $h}' <<<"$ext")"
      rm -f "$TP_TASK_DIR/result.md"
    fi
  fi
elif [[ "$event" == Stop && "$active" == yes ]]; then
  origin="$(jq -r '.promptOrigin // "unknown"' <<<"$prev" 2>/dev/null)" || origin=unknown
  t_path="$(jq -r '.transcript_path // empty' <<<"$input" 2>/dev/null)"
  t_origin=""
  if [[ -n "$t_path" && -f "$t_path" ]]; then
    t_origin="$(jq -rs --argjson need "$prev_turn" '[ .[] | select(type == "object" and .type == "user" and (.origin | type) == "object" and .origin.kind != null) ] as $u
      | if ($u | length) >= $need and ($u | length) > 0 then ($u | last | .origin.kind) else empty end' "$t_path" 2>/dev/null)" || t_origin=""
  fi
  if [[ -n "$t_origin" ]]; then origin="$(norm_origin "$t_origin")"; fi
  reply_tmp="$(mktemp "$TP_TASK_DIR/.reply.XXXXXXXX")" || exit 0
  jq -r '.last_assistant_message // empty | select(length > 0)' <<<"$input" > "$reply_tmp" 2>/dev/null || : > "$reply_tmp"
  held_file=""
  held_rel="$(jq -r '.heldReport // empty' <<<"$prev" 2>/dev/null)" || held_rel=""
  if [[ -n "$held_rel" && -f "$TP_TASK_DIR/$held_rel" ]]; then held_file="$TP_TASK_DIR/$held_rel"; fi
  base=""
  if [[ -f "$TP_TASK_DIR/result.md" ]]; then
    if has_status "$TP_TASK_DIR/result.md"; then base="$TP_TASK_DIR/result.md"; fi
  elif [[ -n "$held_file" ]] && has_status "$held_file"; then base="$held_file"; fi
  if [[ -n "$base" && "$origin" != human ]] && ! has_status "$reply_tmp"; then
    # protected: the reply goes to results/<turn>.md and state.json says a later reply exists
    if [[ -s "$reply_tmp" ]]; then
      archive "$reply_tmp" "$prev_turn"
      later="$(jq -r '(.laterReplies // 0) + 1' <<<"$prev")"
      ext="$(jq -nc --argjson n "$later" --arg p "results/$prev_turn.md" '{laterReplies: $n, laterReplyPath: $p, heldReport: ""}')"
    else
      ext='{"heldReport": ""}'
    fi
    if [[ "$base" == "$held_file" ]]; then
      cp "$held_file" "$TP_TASK_DIR/.restore.$$" && mv "$TP_TASK_DIR/.restore.$$" "$TP_TASK_DIR/result.md"
    fi
    rm -f "$reply_tmp"
  else
    if [[ -f "$TP_TASK_DIR/result.md" ]]; then archive "$TP_TASK_DIR/result.md" "$result_turn"; fi
    if [[ -s "$reply_tmp" ]]; then
      mv "$reply_tmp" "$TP_TASK_DIR/result.md" || exit 0
      ext="$(jq -nc --argjson t "$prev_turn" '{resultTurn: $t, laterReplies: 0, laterReplyPath: "", heldReport: ""}')"
    else
      rm -f "$reply_tmp" "$TP_TASK_DIR/result.md"
      ext='{"laterReplies": 0, "laterReplyPath": "", "heldReport": ""}'
    fi
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

# The submit record lib/pane_send.py waits on. Only a UserPromptSubmit writes it: promptSubmits (a running count, the
# source of n) and promptLog, the last 8 records {n, at, tokens} in order, so two quick submits cannot hide each other.
# `tokens` is every delivery id found in the hook's `prompt` (lib/pane_send.py send() types one at the end of each message;
# the pattern below is pane_send.TOKEN_PATTERN, and a test keeps them equal). The text itself is neither stored nor compared,
# so no whitespace rule has to agree between this hook and the sender. A prompt with no marker records tokens [].
marker_re='\[tp:[0-9a-f]{10}\]'
tokens='[]'
if [[ "$event" == UserPromptSubmit ]]; then
  tokens="$(jq -c --arg re "$marker_re" '[(.prompt // "" | tostring) | scan($re)]' <<<"$input" 2>/dev/null)" || tokens='[]'
  if [[ -z "$tokens" ]]; then tokens='[]'; fi
fi

state_tmp="$(mktemp "$TP_TASK_DIR/.state.XXXXXXXX")" || exit 0
jq -n --argjson prev "$prev" --argjson in "$input" --argjson usage "$usage" \
  --arg id "${TP_TASK_ID:-}" --arg now "$now" --argjson tokens "$tokens" --argjson ext "$ext" '
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
        { state: "working", tool: "", error: "", promptTurn: (turn + 1), settledTurn: settled,
          promptSubmits: (($prev.promptSubmits // 0) + 1),
          promptLog: ((($prev.promptLog // []) + [{ n: (($prev.promptSubmits // 0) + 1), at: $now, tokens: $tokens }]) | .[-8:]) }
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
  + $ext
  + { updated: $now }
' > "$state_tmp" 2>/dev/null && mv "$state_tmp" "$state"
rm -f "$state_tmp"
exit 0
