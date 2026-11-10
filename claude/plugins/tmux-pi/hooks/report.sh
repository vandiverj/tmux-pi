#!/usr/bin/env bash
# report.sh — Claude Code hook that writes a tmux-pi subagent's live state.
#
# Same files as the pi reporter extension (agent/extensions/reporter.ts):
#   $TP_TASK_DIR/state.json  state, current tool, turns, tool calls, failed tool
#                            calls in a row (toolErrors), tokens, model
#   $TP_TASK_DIR/result.md   last assistant reply, written on Stop; a reply without RESULT_STATUS never
#                            replaces one with it (see below)
#   $TP_TASK_DIR/results/    each reply or result.md the hook replaced or held back, never overwritten
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
# Every reply is kept and the report is kept by what the reply says, never by who started the turn (RO1):
# - A reply with a valid RESULT_STATUS line (lib/result_status.py, the one reader tp-review and tp-agent use) replaces
#   result.md. A reply without one (none, malformed, partial, empty) never replaces a result.md that has one, whoever
#   started the turn; it goes to results/ and state.json counts it (laterReplies, laterReplyPath). A result.md without a
#   RESULT_STATUS line is replaced as before. Packets ask every final reply to end with RESULT_STATUS, so a turn that
#   follows the packet still updates the report.
# - The next prompt still withdraws result.md while the worker works: it is copied to results/ first and, when it has a
#   status, recorded as heldReport so the Stop can put it back.
# - An archive write never overwrites: each copy takes the first free results/<n>.md from the turn it belongs to upward,
#   created exclusively (a hard link, else a noclobber write), so old state, a missing field or two close submits cannot
#   destroy a kept reply. Where nothing can be created under results/, result.md and the reply both stay on disk.
# - A Stop with no open prompt changes neither result.md nor the turn state, but a reply it carries that is not already
#   kept is archived and counted.
results_dir="$TP_TASK_DIR/results"
status_py="$(dirname "$0")/../../../../lib/result_status.py"
has_status() { [[ -s "$1" && -f "$status_py" ]] && python3 "$status_py" "$1" >/dev/null 2>&1; }
archived=""
archive() { # archive FILE N: copy FILE to the first free results/<m>.md, m >= N; sets $archived to where the copy is
  # A name that exists is never written: a hard link creates the name or fails, and where there are no hard links a
  # noclobber write does the same; a clash takes the next number. If neither can create the name, the copy stays at its
  # temporary name and $archived points there. Returns 1 only when no copy could be made (then the caller keeps FILE).
  archived=""
  mkdir -p "$results_dir" 2>/dev/null || return 1
  local tmp name n="$2"; tmp="$(mktemp "$results_dir/.keep.XXXXXXXX")" || return 1
  if ! cp -p "$1" "$tmp" 2>/dev/null; then rm -f "$tmp"; return 1; fi
  [[ "$n" =~ ^[0-9]+$ ]] || n=0
  while :; do
    name="$results_dir/$n.md"
    if [[ ! -e "$name" && ! -L "$name" ]]; then
      if ln "$tmp" "$name" 2>/dev/null; then rm -f "$tmp"; archived="results/$n.md"; return 0; fi
      if [[ ! -e "$name" && ! -L "$name" ]]; then  # ln failed with no clash: no hard links here
        if ( set -o noclobber; cat "$tmp" > "$name" ) 2>/dev/null; then rm -f "$tmp"; archived="results/$n.md"; return 0; fi
        archived="results/${tmp##*/}"; return 0  # no name can be created: the copy stays where it is, never deleted
      fi
    fi
    n=$((n + 1))
  done
}
keep_reply() { # archive the reply; if that is impossible the reply file itself stays in the task dir and $archived names it
  if archive "$reply_tmp" "$prompt_turn"; then rm -f "$reply_tmp"; else archived="${reply_tmp##*/}"; fi
  reply_tmp=""
}
same() { [[ -n "$2" && -f "$TP_TASK_DIR/$2" ]] && cmp -s "$1" "$TP_TASK_DIR/$2"; }
field() { jq -r ".$1 // empty | tostring" <<<"$prev" 2>/dev/null; }
prompt_turn="$(field promptTurn)"; prompt_turn="${prompt_turn:-0}"
settled_turn="$(field settledTurn)"; settled_turn="${settled_turn:-0}"
later_path="$(field laterReplyPath)"
later_count() { local n; n="$(jq -r '.laterReplies // 0 | if type == "number" then floor else 0 end' <<<"$prev" 2>/dev/null)"; echo "${n:-0}"; }
ext='{}'
if [[ "$event" == UserPromptSubmit ]]; then
  if [[ -f "$TP_TASK_DIR/result.md" ]]; then
    kept="$(field resultArchive)"
    if same "$TP_TASK_DIR/result.md" "$kept"; then archived="$kept"; else archive "$TP_TASK_DIR/result.md" "$settled_turn"; fi
    if [[ -n "$archived" ]]; then
      held=""
      if has_status "$TP_TASK_DIR/result.md"; then held="$archived"; fi
      ext="$(jq -nc --arg h "$held" '{heldReport: $h, resultArchive: ""}')"
      rm -f "$TP_TASK_DIR/result.md"
    fi  # a result.md that could not be archived stays in place rather than be lost
  fi
elif [[ "$event" == Stop ]]; then
  reply_tmp="$(mktemp "$TP_TASK_DIR/.reply.XXXXXXXX")" || exit 0
  jq -r '.last_assistant_message // empty | select(length > 0)' <<<"$input" > "$reply_tmp" 2>/dev/null || : > "$reply_tmp"
  if [[ "$active" != yes ]]; then
    # no open prompt: duplicate, late, or the second Stop of two overlapping turns. Keep its reply unless already kept.
    if [[ -s "$reply_tmp" ]] && ! { [[ -f "$TP_TASK_DIR/result.md" ]] && cmp -s "$reply_tmp" "$TP_TASK_DIR/result.md"; } \
       && ! same "$reply_tmp" "$later_path"; then
      keep_reply
      ext="$(jq -nc --argjson n "$(( $(later_count) + 1 ))" --arg p "$archived" '{laterReplies: $n, laterReplyPath: $p}')"
    fi
    rm -f "$reply_tmp"
  else
    held_file=""
    held_rel="$(field heldReport)"
    if [[ -n "$held_rel" && -f "$TP_TASK_DIR/$held_rel" ]]; then held_file="$TP_TASK_DIR/$held_rel"; fi
    base=""
    if [[ -f "$TP_TASK_DIR/result.md" ]]; then
      if has_status "$TP_TASK_DIR/result.md"; then base="$TP_TASK_DIR/result.md"; fi
    elif [[ -n "$held_file" ]] && has_status "$held_file"; then base="$held_file"; fi
    if [[ -n "$base" ]] && ! has_status "$reply_tmp"; then
      # the report stays (or comes back); the reply goes to results/ and state.json says a later reply exists
      ext='{"heldReport": ""}'
      if [[ "$base" == "$held_file" ]]; then
        restore=""
        if restore="$(mktemp "$TP_TASK_DIR/.restore.XXXXXXXX")" && cp -p "$held_file" "$restore" \
           && mv "$restore" "$TP_TASK_DIR/result.md"; then
          ext="$(jq -nc --arg a "$held_rel" '{heldReport: "", resultArchive: $a}')"
        else
          rm -f "$restore"; ext='{}'  # the report is still at heldReport
        fi
      fi
      if [[ -s "$reply_tmp" ]]; then
        keep_reply
        ext="$(jq -c --argjson n "$(( $(later_count) + 1 ))" --arg p "$archived" '. + {laterReplies: $n, laterReplyPath: $p}' <<<"$ext")"
      fi
      rm -f "$reply_tmp"
    elif [[ -f "$TP_TASK_DIR/result.md" ]] && ! same "$TP_TASK_DIR/result.md" "$(field resultArchive)" \
         && ! archive "$TP_TASK_DIR/result.md" "$settled_turn"; then
      # result.md cannot be copied anywhere: it stays, and so does the reply, in its own file, recorded as the later reply
      if [[ -s "$reply_tmp" ]]; then
        ext="$(jq -nc --argjson n "$(( $(later_count) + 1 ))" --arg p "${reply_tmp##*/}" '{laterReplies: $n, laterReplyPath: $p}')"
      else
        rm -f "$reply_tmp"
      fi
    else
      if [[ -s "$reply_tmp" ]]; then
        mv "$reply_tmp" "$TP_TASK_DIR/result.md" || exit 0
      else
        rm -f "$reply_tmp" "$TP_TASK_DIR/result.md"
      fi
      ext='{"laterReplies": 0, "laterReplyPath": "", "heldReport": "", "resultArchive": ""}'
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
