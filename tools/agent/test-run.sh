#!/usr/bin/env bash
# test-run.sh WORKTREE [--timeout S] [--label NAME] -- CMD ARGS...
#
# Runs CMD in WORKTREE to completion or to a hard timeout (default 900 s), in the foreground: it never backgrounds and
# needs no polling. The output goes to a log; stdout gets the last 20 lines, a `record:` line, and the last line
# `exit=N log=PATH`. N is the exit code of CMD, or 124 when the timeout killed it (the summary says so).
#
# Record: a `tp-test-run:v1` JSON file TP_TASK_DIR/test-runs/<n>.json (n counts up from 1), or, when TP_TASK_DIR is not
# set, beside the log (same name, .json). Fields: label, cmd (array), cwd, head, tree (the fingerprint of the working tree
# before CMD started, see tp_tree_fingerprint), exit, timed_out, seconds, log, and summary: the last line of the output
# that a known runner prints (`Ran N tests in …`, `Tests: …`, `Test Files …`, vitest's `Tests …`), colours removed.

set -uo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

USAGE="usage: test-run.sh WORKTREE [--timeout S] [--label NAME] -- CMD ARGS..."
[ $# -ge 1 ] || tp_fail "$USAGE"
WT_ARG="$1"
shift
TIMEOUT=900
LABEL=""
while [ $# -gt 0 ]; do
  case "$1" in
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

if [ -n "${TP_TASK_DIR:-}" ]; then
  RUNS="$TP_TASK_DIR/test-runs"
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
printf '%s\0' "${TOK[@]}" | tp_json_write "$RECORD" ||
  tp_fail "cannot write $RECORD"
[ -z "${LOCK:-}" ] || rmdir "$LOCK"
printf 'record: %s\n' "$RECORD"
exit "$RC"
