# Shared by the scripts in this directory (sourced, never run by itself). Bash 3.2 compatible.
#
# Contract of every script here: one plain command with absolute-path arguments, a short summary on stdout, and a
# LAST line `exit=N log=PATH` (log=- when the script stopped before it made a log). The full output goes to the log,
# under ${TMPDIR:-/tmp}/tp-tools/. The last line comes from an EXIT trap, so it is printed on every path, including
# a usage error and a script that dies halfway.

TP_LOG="-"

tp_exit_line() {
  local code=$?
  printf 'exit=%s log=%s\n' "$code" "$TP_LOG"
}
trap tp_exit_line EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# tp_fail MESSAGE [CODE] — say why on stdout (so the message sits above the last line) and stop.
tp_fail() {
  printf '%s: %s\n' "${0##*/}" "$1"
  exit "${2:-2}"
}

# tp_log_init NAME WORKTREE — create the log file and set TP_LOG.
tp_log_init() {
  local dir="${TMPDIR:-/tmp}"
  dir="${dir%/}/tp-tools"
  mkdir -p "$dir" || tp_fail "cannot create $dir"
  TP_LOG="$dir/$1-${2##*/}-$(date +%Y%m%d-%H%M%S)-$$.log"
  : >"$TP_LOG" || tp_fail "cannot write $TP_LOG"
}

# tp_worktree PATH — print the top of the git working tree at PATH (physical path), or fail.
tp_worktree() {
  local top
  [ -d "$1" ] || return 1
  top="$(git -C "$1" rev-parse --show-toplevel 2>/dev/null)" || return 1
  (cd "$top" && pwd -P)
}

# tp_find_tool NAME START_DIR WORKTREE — node_modules/.bin/NAME from START_DIR up to WORKTREE, else NAME on PATH.
tp_find_tool() {
  local name="$1" dir="$2" wt="$3"
  while :; do
    if [ -x "$dir/node_modules/.bin/$name" ]; then
      printf '%s\n' "$dir/node_modules/.bin/$name"
      return 0
    fi
    if [ "$dir" = "$wt" ] || [ "$dir" = "/" ]; then
      break
    fi
    dir="${dir%/*}"
    [ -n "$dir" ] || dir=/
  done
  command -v "$name"
}

# tp_find_deno WORKTREE — deno on PATH, else `mise which deno` run in the worktree, else the newest mise-installed one.
tp_find_deno() {
  local found data
  found="$(command -v deno 2>/dev/null)" && { printf '%s\n' "$found"; return 0; }
  if command -v mise >/dev/null 2>&1; then
    found="$(cd "$1" && mise which deno 2>/dev/null)" && [ -x "$found" ] && { printf '%s\n' "$found"; return 0; }
  fi
  data="${MISE_DATA_DIR:-${HOME:-/nonexistent}/.local/share/mise}"
  found="$(ls -d "$data"/installs/deno/*/bin/deno 2>/dev/null | sort | tail -n 1)"
  if [ -n "$found" ] && [ -x "$found" ]; then
    printf '%s\n' "$found"
    return 0
  fi
  return 1
}

# tp_dirname PATH — like dirname, without starting a process; sets TP_DIR.
tp_dirname() {
  case "$1" in
    */*) TP_DIR="${1%/*}"; [ -n "$TP_DIR" ] || TP_DIR=/ ;;
    *) TP_DIR=. ;;
  esac
}

# tp_app_of WORKTREE RELPATH — the directory (relative to the worktree, "." for the root) of the nearest package.json,
# deno.json or deno.jsonc above RELPATH; "." when there is none.
tp_app_of() {
  local dir
  tp_dirname "$2"
  dir="$TP_DIR"
  while [ "$dir" != "." ] && [ "$dir" != "/" ]; do
    if [ -e "$1/$dir/package.json" ] || [ -e "$1/$dir/deno.json" ] || [ -e "$1/$dir/deno.jsonc" ]; then
      printf '%s\n' "$dir"
      return 0
    fi
    tp_dirname "$dir"
    dir="$TP_DIR"
  done
  printf '.\n'
}
