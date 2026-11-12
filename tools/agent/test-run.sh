#!/usr/bin/env bash
# test-run.sh WORKTREE [--timeout S] [--label NAME] [--main [--lane L] [--step S]] -- CMD ARGS...
#
# Runs CMD in WORKTREE to completion or to a hard timeout (default 900 s), in the foreground: it never backgrounds and
# needs no polling. The output goes to a log; stdout gets the last 20 lines, a `record:` line, and the last line
# `exit=N log=PATH`. N is the exit code of CMD, or 124 when the timeout killed it (the summary says so).
#
# Record: a `tp-test-run:v1` JSON file TP_TASK_DIR/test-runs/<n>.json (n counts up from 1), or, when TP_TASK_DIR is not
# set, beside the log (same name, .json). Fields: label, cmd (array), cwd, head, tree (the fingerprint of the working tree
# before CMD started, see tp_tree_fingerprint), exit, timed_out, seconds, log, and summary: the last line of the output
# that a known runner prints (`Ran N tests in …`, `Tests: …`, `Test Files …`, vitest's `Tests …`), colours removed, and
# interpreter: {path, version} of the python CMD starts (tools/agent/interpreter.py), or null.
#
# --main (Main's own run, no TP_TASK_DIR): the record goes to SESSION/main-runs/<n>.json, SESSION being the session
# directory (TP_STATE_DIR, else the tmux session's @tp_state_dir, as bin/tp-agent finds it), with role "main" and the
# --lane / --step given. A worker (TP_TASK_DIR set) is refused, and so is a run with no session; nothing runs then.

set -uo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

USAGE="usage: test-run.sh WORKTREE [--timeout S] [--label NAME] [--main [--lane L] [--step S]] -- CMD ARGS..."
[ $# -ge 1 ] || tp_fail "$USAGE"
WT_ARG="$1"
shift
TIMEOUT=900
LABEL=""
MAIN=0
LANE=""
STEP=""
while [ $# -gt 0 ]; do
  case "$1" in
    --main)
      MAIN=1
      shift
      ;;
    --lane)
      [ $# -ge 2 ] || tp_fail "$USAGE"
      LANE="$2"
      shift 2
      ;;
    --step)
      [ $# -ge 2 ] || tp_fail "$USAGE"
      STEP="$2"
      shift 2
      ;;
    --timeout)
      [ $# -ge 2 ] || tp_fail "$USAGE"
      TIMEOUT="$2"
      shift 2
      ;;
    --label)
      [ $# -ge 2 ] || tp_fail "$USAGE"
      LABEL="$2"
      shift 2
      ;;
    --)
      shift
      break
      ;;
    *) tp_fail "$USAGE" ;;
  esac
done
[ $# -ge 1 ] || tp_fail "$USAGE"
case "$TIMEOUT" in
  '' | *[!0-9]* | 0) tp_fail "--timeout needs a positive number of seconds: $TIMEOUT" ;;
esac
[ "$MAIN" = 1 ] || { [ -z "$LANE" ] && [ -z "$STEP" ]; } || tp_fail "--lane and --step go with --main"
SESSION_DIR=""
if [ "$MAIN" = 1 ]; then
  [ -z "${TP_TASK_DIR:-}" ] || tp_fail "--main is for Main: TP_TASK_DIR is set, so this is a worker and its record belongs to its task"
  SESSION_DIR="$(tp_session_dir)" || tp_fail "--main needs the session directory (one with a tasks/ dir): set TP_STATE_DIR or run in the tmux-pi session; no record can be written"
fi
WT="$(tp_worktree "$WT_ARG")" || tp_fail "not a git worktree: $WT_ARG"
tp_log_init test-run "$WT"

CMD=("$@")
# A record without the tree it ran on proves nothing, so a field that cannot be filled stops the run before CMD starts:
# no record is written, whatever CMD would have returned.
HEAD_SHA="$(git -C "$WT" rev-parse --verify --quiet HEAD 2>/dev/null)" || HEAD_SHA=""
[ -n "$HEAD_SHA" ] || tp_fail "cannot read HEAD of $WT: no record can be written"
TREE="$(tp_tree_fingerprint "$WT")" || TREE=""
[ -n "$TREE" ] || tp_fail "cannot fingerprint the working tree of $WT: no record can be written"
STATUS_FILE="$TP_LOG.status"
# Which python CMD starts, read before CMD runs: one start of that python per run (about 30 ms). Empty file = null.
python3 -I "$TP_TOOLS_DIR/interpreter.py" "$WT" -- "${CMD[@]}" >"$TP_LOG.interp" 2>/dev/null || : >"$TP_LOG.interp"

# The watchdog is perl (on every Mac and Linux): it starts CMD in its own process group, and when the time is up sends
# TERM to the group, then KILL two seconds later. It writes `timed_out seconds` to the status file and exits with the
# code of CMD (128+N for a signal, 124 for a timeout, 127 for a CMD that cannot be started).
(cd "$WT" && perl -e '
  use strict; use POSIX (); use Time::HiRes qw(time);
  my ($secs, $log, $status, @cmd) = @ARGV;
  my $t0 = time;
  my $pid = fork();
  die "fork: $!" unless defined $pid;
  if (!$pid) {
    POSIX::setpgid(0, 0);
    open(STDIN, "<", "/dev/null");
    open(STDOUT, ">>", $log) or die;
    open(STDERR, ">&", \*STDOUT) or die;
    { exec {$cmd[0]} @cmd; }
    print STDERR "test-run: cannot run $cmd[0]: $!\n";
    POSIX::_exit(127);
  }
  POSIX::setpgid($pid, $pid);
  my $timed = 0;
  $SIG{ALRM} = sub {
    if (!$timed) { $timed = 1; kill "TERM", -$pid; alarm 2; }
    else { kill "KILL", -$pid; }
  };
  alarm $secs;
  my $r;
  while (($r = waitpid($pid, 0)) < 0 && $!{EINTR}) { }
  alarm 0;
  kill "KILL", -$pid;
  my $code = $? & 127 ? 128 + ($? & 127) : $? >> 8;
  $code = 124 if $timed;
  open(my $s, ">", $status) or die;
  printf $s "%d %.2f\n", $timed, time - $t0;
  close $s;
  exit $code;
' "$TIMEOUT" "$TP_LOG" "$STATUS_FILE" "${CMD[@]}")
RC=$?
if ! { read -r TIMED_OUT SECONDS_TAKEN <"$STATUS_FILE"; } 2>/dev/null; then
  # The watchdog died before it wrote its status: nothing says CMD finished, so a 0 must not stand.
  TIMED_OUT=0
  SECONDS_TAKEN=0
  printf 'test-run: the watchdog left no status\n'
  [ "$RC" != 0 ] || RC=125
fi
rm -f "$STATUS_FILE"

ESC="$(printf '\033')"
SUMMARY="$(sed "s/${ESC}\\[[0-9;]*[A-Za-z]//g" "$TP_LOG" | tr -d '\r' |
  grep -E '^[[:space:]]*(Ran [0-9]+ tests? in|Tests:|Tests[[:space:]]|Test Files[[:space:]])' | tail -n 1 |
  sed 's/^[[:space:]]*//')"

tail -n 20 "$TP_LOG"
[ "$TIMED_OUT" = 0 ] || printf 'test-run: timed out after %s s, killed; exit 124\n' "$TIMEOUT"

RUNS=""
[ -z "${TP_TASK_DIR:-}" ] || RUNS="$TP_TASK_DIR/test-runs"
if [ "$MAIN" = 1 ]; then
  RUNS="$SESSION_DIR/main-runs"
  [ ! -L "$RUNS" ] || tp_fail "$RUNS is a link"
fi
if [ -n "$RUNS" ]; then
  mkdir -p "$RUNS" || tp_fail "cannot create $RUNS"
  # The number is claimed with mkdir, which creates a directory exactly once however many runs ask at the same time.
  # The lock is held until the record is renamed into place, so a number is never taken twice and never half written.
  n=1
  while :; do
    if mkdir "$RUNS/$n.lock" 2>/dev/null; then
      [ ! -e "$RUNS/$n.json" ] && break
      rmdir "$RUNS/$n.lock"  # taken earlier and released since
    elif [ ! -d "$RUNS/$n.lock" ] && [ ! -e "$RUNS/$n.json" ]; then
      tp_fail "cannot claim a record number in $RUNS"
    fi
    n=$((n + 1))
  done
  LOCK="$RUNS/$n.lock"
  RECORD="$RUNS/$n.json"
else
  RECORD="${TP_LOG%.log}.json"
fi
TIMED_JSON=false
[ "$TIMED_OUT" = 0 ] || TIMED_JSON=true
TOK=(s schema tp-test-run:v1 s label "$LABEL" a cmd "${#CMD[@]}" "${CMD[@]}" s cwd "$WT" s head "$HEAD_SHA" s tree "$TREE"
  r exit "$RC" r timed_out "$TIMED_JSON" r seconds "$SECONDS_TAKEN" s log "$TP_LOG" s summary "$SUMMARY")
if [ "$MAIN" = 1 ]; then
  TOK+=(s role main)
  [ -z "$LANE" ] || TOK+=(s lane "$LANE")
  [ -z "$STEP" ] || TOK+=(s step "$STEP")
fi
{ printf '%s\0' "${TOK[@]}"; if [ -s "$TP_LOG.interp" ]; then cat "$TP_LOG.interp"; else printf 'r\0interpreter\0null\0'; fi; } |
  tp_json_write "$RECORD" ||
  tp_fail "cannot write $RECORD"
[ -z "${LOCK:-}" ] || rmdir "$LOCK"
rm -f "$TP_LOG.interp"
printf 'record: %s\n' "$RECORD"
exit "$RC"
