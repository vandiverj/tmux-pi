#!/usr/bin/env bash
# pre-commit-gate.sh WORKTREE [BASE_REF]
#
# The checks a worker runs by hand before it commits, as one plain command:
#   prettier --check and eslint on the changed files, the typecheck of each touched app, gitleaks over BASE..HEAD.
# Changed files = BASE...HEAD plus uncommitted and untracked files (deleted files are left out).
# BASE_REF defaults to env LANE_BASE, then the upstream branch of the current branch.
# An app is the nearest directory above a file with a package.json, deno.json or deno.jsonc. Its typecheck is
# `deno check` when deno.json(c) is there, else `tsc --noEmit` when tsconfig.json is there; nothing else is guessed.
# Prints one `name exit` line per check (`name skipped (reason)` for a tool that is not installed, a project without an
# eslint config, or no matching files; a skip does not fail the gate), then `exit=N log=PATH`. N is 1 when a check failed.
# gitleaks scans commits, so commit first and run the gate again if you want the new commit covered.

set -uo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

[ $# -ge 1 ] && [ $# -le 2 ] || tp_fail "usage: pre-commit-gate.sh WORKTREE [BASE_REF]"
WT="$(tp_worktree "$1")" || tp_fail "not a git worktree: $1"
BASE="${2:-${LANE_BASE:-}}"
if [ -z "$BASE" ]; then
  BASE="$(git -C "$WT" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null)" || BASE=""
fi
[ -n "$BASE" ] || tp_fail "no base: pass BASE_REF, set LANE_BASE, or give the branch an upstream"
BASE_SHA="$(git -C "$WT" rev-parse --verify --quiet "$BASE^{commit}")" || tp_fail "base ref not found: $BASE"
tp_log_init pre-commit-gate "$WT"

FILES=()
add_file() {
  local f
  [ -f "$WT/$1" ] || return 0
  for f in ${FILES[@]+"${FILES[@]}"}; do
    [ "$f" = "$1" ] && return 0
  done
  FILES+=("$1")
}
while IFS= read -r -d '' f; do add_file "$f"; done < <(git -C "$WT" diff -z --name-only --diff-filter=ACMR "$BASE_SHA...HEAD" 2>/dev/null)
while IFS= read -r -d '' f; do add_file "$f"; done < <(git -C "$WT" diff -z --name-only --diff-filter=ACMR HEAD 2>/dev/null)
while IFS= read -r -d '' f; do add_file "$f"; done < <(git -C "$WT" ls-files -z --others --exclude-standard 2>/dev/null)

APPS=()
FILE_APP=()
for f in ${FILES[@]+"${FILES[@]}"}; do
  a="$(tp_app_of "$WT" "$f")"
  FILE_APP+=("$a")
  seen=0
  for known in ${APPS[@]+"${APPS[@]}"}; do
    [ "$known" = "$a" ] && seen=1
  done
  [ "$seen" = 1 ] || APPS+=("$a")
done

{
  printf 'worktree: %s\nbase: %s (%s)\nchanged files: %s\n' "$WT" "$BASE" "$BASE_SHA" "${#FILES[@]}"
  for f in ${FILES[@]+"${FILES[@]}"}; do printf '  %s\n' "$f"; done
} >>"$TP_LOG"
printf 'worktree: %s\nbase: %s (%s)\nchanged files: %s\n' "$WT" "$BASE" "${BASE_SHA:0:10}" "${#FILES[@]}"

FAILED=0

# files_of APP EXT_REGEX — the changed files of APP whose name matches, relative to APP's directory, one per line.
files_of() {
  local i f rel re
  re="\\.($2)\$"
  i=0
  for f in ${FILES[@]+"${FILES[@]}"}; do
    if [ "${FILE_APP[$i]}" = "$1" ] && [[ "$f" =~ $re ]]; then
      if [ "$1" = "." ]; then rel="$f"; else rel="${f#"$1"/}"; fi
      printf '%s\n' "$rel"
    fi
    i=$((i + 1))
  done
}

# run_in APP NAME CMD... — run in APP's directory, output appended to the log under a heading. Sets RC and, when it
# failed, a few lines of the failure for the summary.
run_in() {
  local app="$1" name="$2" start
  shift 2
  start="$(wc -l <"$TP_LOG")"
  printf '== %s: %s\n' "$name" "$*" >>"$TP_LOG"
  (cd "$WT/$app" && "$@") >>"$TP_LOG" 2>&1
  RC=$?
  printf '== %s exit %s\n' "$name" "$RC" >>"$TP_LOG"
  TAIL=""
  [ "$RC" = 0 ] || TAIL="$(sed -n "$((start + 2)),$((start + 9))p" "$TP_LOG" | sed 's/^/    /')"
}

# report NAME RAN RC MISSING NOFILES — print the `name exit` line.
report() {
  local name="$1" ran="$2" rc="$3" missing="$4" nofiles="$5"
  if [ "$ran" = 0 ]; then
    if [ -n "$missing" ]; then
      printf '%s skipped (not found)\n' "$name"
    else
      printf '%s skipped (%s)\n' "$name" "$nofiles"
    fi
  else
    printf '%s %s\n' "$name" "$rc"
    [ -z "$missing" ] || printf '  also skipped (not found) in:%s\n' "$missing"
    [ "$rc" = 0 ] || FAILED=1
  fi
}

# tool_check NAME TOOL EXT_REGEX ARGS... — run TOOL once per app on its matching changed files.
tool_check() {
  local name="$1" tool="$2" ext="$3" app bin ran=0 rc=0 missing="" had=0 rels
  shift 3
  for app in ${APPS[@]+"${APPS[@]}"}; do
    rels=()
    while IFS= read -r line; do [ -z "$line" ] || rels+=("$line"); done < <(files_of "$app" "$ext")
    [ "${#rels[@]}" -gt 0 ] || continue
    had=1
    if [ "$name" = eslint ] && ! has_eslint_config "$app"; then
      missing=""
      continue
    fi
    if ! bin="$(tp_find_tool "$tool" "$WT/$app" "$WT")"; then
      missing="$missing $app"
      continue
    fi
    run_in "$app" "$name" "$bin" "$@" ${rels[@]+"${rels[@]}"}
    ran=$((ran + 1))
    [ "$RC" = 0 ] || rc="$RC"
    [ -z "$TAIL" ] || printf '%s\n' "$TAIL" >>"$TP_TAILS"
  done
  if [ "$had" = 0 ]; then
    report "$name" 0 0 "" "no files"
  elif [ "$ran" = 0 ] && [ -z "$missing" ]; then
    report "$name" 0 0 "" "no eslint config"
  else
    report "$name" "$ran" "$rc" "$missing" ""
    if [ "$rc" != 0 ]; then flush_tails; else : >"$TP_TAILS"; fi
  fi
}

TP_TAILS="$(mktemp "${TMPDIR:-/tmp}/tp-gate-tails.XXXXXX")" || tp_fail "cannot make a temp file"
tp_gate_exit() {
  local code=$?
  rm -f "$TP_TAILS"
  (exit "$code")
  tp_exit_line
}
trap tp_gate_exit EXIT
flush_tails() {
  cat "$TP_TAILS"
  : >"$TP_TAILS"
}

# has_eslint_config APP — an eslint config in APP's directory or any directory above it, up to the worktree.
has_eslint_config() {
  local dir="$WT/$1" c
  while :; do
    for c in eslint.config.js eslint.config.mjs eslint.config.cjs eslint.config.ts eslint.config.mts eslint.config.cts \
      .eslintrc .eslintrc.js .eslintrc.cjs .eslintrc.mjs .eslintrc.json .eslintrc.yml .eslintrc.yaml; do
      [ -e "$dir/$c" ] && return 0
    done
    if [ -f "$dir/package.json" ] && grep -q '"eslintConfig"' "$dir/package.json"; then return 0; fi
    if [ "$dir" = "$WT" ] || [ "$dir" = "/" ]; then return 1; fi
    dir="$(dirname "$dir")"
  done
}

tool_check prettier prettier 'js|jsx|ts|tsx|mjs|cjs|mts|cts|json|css|scss|md|yml|yaml' --check --ignore-unknown
tool_check eslint eslint 'js|jsx|ts|tsx|mjs|cjs|mts|cts'

# Typecheck, one line per touched app that has changed TypeScript.
TS_EXT='ts|tsx|mts|cts'
typed=0
for app in ${APPS[@]+"${APPS[@]}"}; do
  rels=()
  while IFS= read -r line; do [ -z "$line" ] || rels+=("$line"); done < <(files_of "$app" "$TS_EXT")
  [ "${#rels[@]}" -gt 0 ] || continue
  typed=1
  label="typecheck:$app"
  if [ -e "$WT/$app/deno.json" ] || [ -e "$WT/$app/deno.jsonc" ]; then
    if bin="$(tp_find_deno "$WT")"; then
      run_in "$app" "$label" "$bin" check "${rels[@]}"
      printf '%s %s\n' "$label" "$RC"
      if [ "$RC" != 0 ]; then FAILED=1; printf '%s\n' "$TAIL"; fi
    else
      printf '%s skipped (not found)\n' "$label"
    fi
  elif [ -e "$WT/$app/tsconfig.json" ]; then
    if bin="$(tp_find_tool tsc "$WT/$app" "$WT")"; then
      run_in "$app" "$label" "$bin" --noEmit
      printf '%s %s\n' "$label" "$RC"
      if [ "$RC" != 0 ]; then FAILED=1; printf '%s\n' "$TAIL"; fi
    else
      printf '%s skipped (not found)\n' "$label"
    fi
  else
    printf '%s skipped (no deno.json or tsconfig.json)\n' "$label"
  fi
done
[ "$typed" = 1 ] || printf 'typecheck skipped (no ts files)\n'

if GL="$(command -v gitleaks)"; then
  run_in . gitleaks "$GL" git --no-banner --redact "--log-opts=$BASE_SHA..HEAD" .
  printf 'gitleaks %s\n' "$RC"
  if [ "$RC" != 0 ]; then FAILED=1; printf '%s\n' "$TAIL"; fi
else
  printf 'gitleaks skipped (not found)\n'
fi

[ "$FAILED" = 0 ] || exit 1
exit 0
