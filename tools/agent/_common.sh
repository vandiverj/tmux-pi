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

# tp_tree_fingerprint WORKTREE — print the tree sha of everything in the working tree that git would add: every tracked
# file (even one that matches an ignore pattern) and untracked files that are not ignored (deletions are simply absent).
# It is `git write-tree` on a temporary index: seed HEAD, then `git add -A` (handles tracked files and type changes),
# then refresh staged-new paths individually (including ignored ones). Rejected paths, e.g. descendants now beyond a
# symlink, are skipped. Never copy the real index's assume-unchanged/skip-worktree flags or touch that index. If the
# richer snapshot fails, retry the original empty-index add -A/write-tree so a valid type change cannot stop a command.
# The same content gives the same sha. HEAD is separate. lib/gate_record.py:fingerprint takes the same steps.
tp_tree_fingerprint() {
  local dir idx tree path rc=0
  local head=()
  dir="$(mktemp -d "${TMPDIR:-/tmp}/tp-fp.XXXXXX")" || return 1
  idx="$dir/index"
  if git -C "$1" rev-parse --verify --quiet HEAD >/dev/null 2>&1; then
    head=(HEAD)
    GIT_INDEX_FILE="$idx" git -C "$1" read-tree HEAD >/dev/null 2>&1 || rc=1
  fi
  if [ "$rc" = 0 ]; then (cd "$1" && GIT_INDEX_FILE="$idx" git add -A >/dev/null 2>&1) || rc=1; fi
  if [ "$rc" = 0 ]; then
    # No renames: a renamed destination is new membership too. Include intent-to-add entries; keep names NUL-delimited.
    git -C "$1" diff --cached --no-renames --ita-visible-in-index --diff-filter=A --name-only -z ${head[@]+"${head[@]}"} -- >"$dir/paths" 2>/dev/null || rc=1
    while IFS= read -r -d '' path; do
      GIT_INDEX_FILE="$idx" git -C "$1" update-index --add --remove -- "$path" >/dev/null 2>&1 || :
    done <"$dir/paths"
  fi
  if [ "$rc" = 0 ]; then
    tree="$(GIT_INDEX_FILE="$idx" git -C "$1" write-tree 2>/dev/null)" || rc=1
  fi
  if [ "$rc" != 0 ]; then
    rm -f "$idx" "$idx.lock"
    rc=0
    (cd "$1" && GIT_INDEX_FILE="$idx" git add -A >/dev/null 2>&1) || rc=1
    if [ "$rc" = 0 ]; then tree="$(GIT_INDEX_FILE="$idx" git -C "$1" write-tree 2>/dev/null)" || rc=1; fi
  fi
  rm -rf "$dir"
  [ "$rc" = 0 ] || return 1
  printf '%s\n' "$tree"
}

# tp_json_write OUTPUT — write the JSON document described by the NUL-separated tokens on stdin to OUTPUT (atomically).
# This is the only way a script writes JSON: values go to python3 json.dumps as bytes, never as text in a command line,
# code or hand-built JSON, so every byte of a name or an argument survives. The token format is in json_record.py.
# Use it as: printf '%s\0' "${TOKENS[@]}" | tp_json_write PATH
TP_TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tp_json_write() {
  python3 -I "$TP_TOOLS_DIR/json_record.py" "$1"
}

# tp_session_dir — print the session directory, the way bin/tp-agent state_dir finds it: TP_STATE_DIR, else the
# @tp_state_dir option of the tmux session (this pane's, or TP_SESSION, default tmux-pi). Fails, printing nothing, when there
# is none or it is not a directory holding tasks/.
tp_session_dir() {
  local dir="${TP_STATE_DIR:-}" session target=()
  if [ -z "$dir" ]; then
    if [ -n "${TMUX:-}" ]; then
      [ -z "${TMUX_PANE:-}" ] || target=(-t "$TMUX_PANE")
      session="$(tmux display-message -p ${target[@]+"${target[@]}"} '#S' 2>/dev/null)" || return 1
    else
      session="${TP_SESSION:-tmux-pi}"
    fi
    dir="$(tmux show-options -qv -t "=$session:" @tp_state_dir 2>/dev/null)" || return 1
  fi
  [ -n "$dir" ] && [ -d "$dir/tasks" ] || return 1
  (cd "$dir" && pwd -P)
}

# tp_now — UTC time, ISO 8601.
tp_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# tp_tsc_conf_refs — the `path` of each project reference in the `tsc --showConfig` output held in $TP_TSC_CONF, as
# written, one per line.
tp_tsc_conf_refs() {
  printf '%s\n' "$TP_TSC_CONF" | sed -n '/^    "references": \[/,/^    \]/p' | sed -n 's/^ *"path": "\(.*\)",\{0,1\}$/\1/p'
}
