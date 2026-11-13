#!/usr/bin/env bash
# main-turns.sh — UserPromptSubmit, Stop and SessionStart in Claude Main: one line per event in SESSION/main-turns.jsonl (HB6d),
# so tools/waits/timeline.py can tell, of the time a task waits on Main, when Main was busy (a turn running), idle (between
# turns) or not recorded.
#
# Orchestrator main only, the gate of sessionstart-compact.sh: TP_TASK_DIR and TP_TASK_ID unset (set but empty is a worker's, as
# in the plugin module and autonomy.env_problem), TP_MODE=orchestrator; and a
# session directory found as tools/agent/_common.sh tp_session_dir finds it (TP_STATE_DIR, else the @tp_state_dir option of the
# pane's tmux session, or of TP_SESSION, default tmux-pi), holding tasks/. _common.sh is not sourced: its EXIT trap prints on
# stdout, and a UserPromptSubmit hook's stdout reaches Claude. Anywhere else it exits 0 with no output.
#
# The line: {"at": UTC ISO with ms, "event", "session": the Claude session id or null[, "source": SessionStart's]}. Only those
# three fields are taken from the hook input, by fixed patterns; the prompt, the reply and every other field are never read
# into the record. A JSON string can hold `"session_id"` only as `\"session_id\"`, so the patterns match top-level keys only.
#
# Cost (F4, round 2): bash never reads the input (its byte-by-byte read of a pipe and its regex over a long prompt or reply
# grew with the message: about 245 ms at 1 MB). perl reads the whole input in large blocks and finds the three keys with
# fixed-string patterns, so a long prompt or reply adds well under a millisecond per megabyte. Nothing is refused or truncated.
#
# Concurrent appends stay whole lines: the file is opened O_APPEND and each line (about 110 bytes) is one write, which lands
# at the end of the file as a whole. The file is opened with O_NOFOLLOW|O_NONBLOCK (a symlink fails, a FIFO is never waited
# on) and written only when it is a regular file. Never fails or blocks the hook: every path exits 0, nothing is printed.
[[ -z "${TP_TASK_DIR+set}" && -z "${TP_TASK_ID+set}" && "${TP_MODE:-}" == orchestrator ]] || exit 0
dir="${TP_STATE_DIR:-}"
if [[ -z "$dir" ]]; then
  if [[ -n "${TMUX:-}" ]]; then session="$(tmux display-message -p ${TMUX_PANE:+-t "$TMUX_PANE"} '#S' 2>/dev/null)" || exit 0
  else session="${TP_SESSION:-tmux-pi}"; fi
  dir="$(tmux show-options -qv -t "=$session:" @tp_state_dir 2>/dev/null)" || exit 0
fi
[[ -n "$dir" && -d "$dir/tasks" ]] || exit 0
perl -e '
use strict; use Fcntl; use POSIX qw(strftime); use Time::HiRes qw(time);
binmode STDIN;
my $in = do { local $/ = \1048576; my ($all, $block) = (""); $all .= $block while defined($block = <STDIN>); $all };
$in =~ /"hook_event_name"\s*:\s*"(UserPromptSubmit|Stop|SessionStart)"/ or exit 0;
my $event = $1;
my $sid = $in =~ /"session_id"\s*:\s*"([A-Za-z0-9._:-]{1,128})"/ ? "\"$1\"" : "null";
my $source = ($event eq "SessionStart" && $in =~ /"source"\s*:\s*"(startup|resume|clear|compact)"/) ? ", \"source\": \"$1\"" : "";
my $t = time; my $ms = int(($t - int($t)) * 1000);
my $line = sprintf("{\"at\": \"%s.%03dZ\", \"event\": \"%s\", \"session\": %s%s}\n",
                   strftime("%Y-%m-%dT%H:%M:%S", gmtime(int $t)), $ms, $event, $sid, $source);
sysopen(my $fh, "$ARGV[0]/main-turns.jsonl", O_WRONLY | O_APPEND | O_CREAT | O_NOFOLLOW | O_NONBLOCK, 0600) or exit 0;
exit 0 unless -f $fh;
syswrite($fh, $line);
' "$dir" >/dev/null 2>&1
exit 0
