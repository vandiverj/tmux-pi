#!/usr/bin/env bash
# pre-commit-gate.sh WORKTREE [BASE_REF] [--require NAME...]
#
# The checks a worker runs by hand before it commits, as one plain command:
#   prettier --check and eslint on the changed files, the typecheck of each touched app, gitleaks over BASE..HEAD.
# Changed files = BASE...HEAD plus uncommitted and untracked files. Deleted files are not checked themselves, but a
# deletion (also the old path of a rename or move) touches its app, because files that import it can break.
# BASE_REF defaults to env LANE_BASE, then the upstream branch of the current branch.
# An app is the nearest directory above a file with a package.json, deno.json or deno.jsonc. Its typecheck is
# `deno check` when deno.json(c) is there, else `tsc --noEmit` when tsconfig.json is there; nothing else is guessed.
# prettier and eslint run per app and are recorded per app (`prettier:APP`, `eslint:APP`, like `typecheck:APP`): an app
# without an eslint config is not_applicable on its own and never hides behind another app's pass.
# Prints one `name exit` line per check (`name skipped (reason)` for a tool that is not installed, a project without an
# eslint config, or no matching files; a skip does not fail the gate), then `exit=N log=PATH`. N is 1 when a check failed.
# gitleaks scans commits (BASE..HEAD), so commit first and run the gate again if you want the new commit covered. A second
# check, gitleaks-dirty, runs `gitleaks dir` over the uncommitted and untracked changed files.
#
# Record: a `tp-gate:v1` JSON file beside the log (same name, .json) and, when env TP_TASK_DIR is set, TP_TASK_DIR/gate.json
# (the previous one is kept as gate-<timestamp>.json). It lists every check with status passed|failed|missing|not_applicable
# (missing = tool not installed; not_applicable = no config or no matching files), plus head and the tree fingerprint of
# the working tree the checks saw. --require NAME... makes a `missing` check of that name fail the gate (exit 1); NAME
# `typecheck` stands for every `typecheck:APP`. Without --require a missing tool never fails the gate. When no check
# passed or failed the summary says `no check applied`.

set -uo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

POS=()
REQUIRED=()
in_require=0
for arg in "$@"; do
  if [ "$arg" = "--require" ]; then
    in_require=1
  elif [ "$in_require" = 1 ] && [[ "$arg" != --* ]]; then
    REQUIRED+=("$arg")
  elif [[ "$arg" = --* ]]; then
    tp_fail "unknown flag: $arg"
  else
    in_require=0
    POS+=("$arg")
  fi
done
USAGE="usage: pre-commit-gate.sh WORKTREE [BASE_REF] [--require NAME...]"
[ "${#POS[@]}" -ge 1 ] && [ "${#POS[@]}" -le 2 ] || tp_fail "$USAGE"
if [ "$in_require" = 1 ] && [ "${#REQUIRED[@]}" = 0 ]; then tp_fail "--require needs at least one NAME"; fi
WT="$(tp_worktree "${POS[0]}")" || tp_fail "not a git worktree: ${POS[0]}"
BASE="${POS[1]:-${LANE_BASE:-}}"
if [ -z "$BASE" ]; then
  BASE="$(git -C "$WT" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null)" || BASE=""
fi
[ -n "$BASE" ] || tp_fail "no base: pass BASE_REF, set LANE_BASE, or give the branch an upstream"
BASE_SHA="$(git -C "$WT" rev-parse --verify --quiet "$BASE^{commit}")" || tp_fail "base ref not found: $BASE"
tp_log_init pre-commit-gate "$WT"
STARTED="$(tp_now)"

# The gate fails closed: a step it could not complete (a listing, a copy, a fingerprint) is an error, and the errors
# make the check `collect` fail, with the reason. Nothing is ever judged on a partial set.
ERRORS=()
gate_error() { ERRORS+=("$1"); }
HEAD_SHA="$(git -C "$WT" rev-parse --verify --quiet HEAD 2>/dev/null)" || HEAD_SHA=""
[ -n "$HEAD_SHA" ] || gate_error "cannot read HEAD"
TREE="$(tp_tree_fingerprint "$WT")" || TREE=""
[ -n "$TREE" ] || gate_error "cannot fingerprint the working tree"

FILES=()
DIRTY=()
DELETED=()
add_file() {
  local f
  [ -f "$WT/$1" ] || return 0  # a committed file that is gone from the working tree is listed as a deletion
  for f in ${FILES[@]+"${FILES[@]}"}; do
    [ "$f" = "$1" ] && return 0
  done
  FILES+=("$1")
}
# add_dirty PATH — a changed file that is not committed yet (what BASE..HEAD gitleaks cannot see).
add_dirty() {
  local f
  [ -f "$WT/$1" ] || { gate_error "changed file vanished while collecting: $1"; return 0; }
  for f in ${DIRTY[@]+"${DIRTY[@]}"}; do
    [ "$f" = "$1" ] && return 0
  done
  DIRTY+=("$1")
}
add_deleted() {
  local f
  [ -e "$WT/$1" ] && return 0
  for f in ${DELETED[@]+"${DELETED[@]}"}; do
    [ "$f" = "$1" ] && return 0
  done
  DELETED+=("$1")
}
# list_z CALLBACK DESCRIPTION GIT-ARGS... — call CALLBACK with every NUL-separated name git prints. A failing git is an
# error, not an empty list.
list_z() {
  local cb="$1" desc="$2" out f
  shift 2
  out="$(mktemp "${TMPDIR:-/tmp}/tp-gate-list.XXXXXX")" || { gate_error "cannot list $desc: no temp file"; return 1; }
  if ! git -C "$WT" "$@" >"$out" 2>>"$TP_LOG"; then
    rm -f "$out"
    gate_error "cannot list $desc: git $* failed"
    return 1
  fi
  while IFS= read -r -d '' f; do "$cb" "$f"; done <"$out"
  rm -f "$out"
}
add_file_dirty() { add_file "$1"; add_dirty "$1"; }
list_z add_file "files changed since the base" diff -z --name-only --diff-filter=ACMR "$BASE_SHA...HEAD"
list_z add_file_dirty "uncommitted files" diff -z --name-only --diff-filter=ACMR HEAD
list_z add_file_dirty "untracked files" ls-files -z --others --exclude-standard
# --no-renames: the old path of a moved file is a deletion too.
list_z add_deleted "files deleted since the base" diff -z --name-only --no-renames --diff-filter=D "$BASE_SHA...HEAD"
list_z add_deleted "uncommitted deletions" diff -z --name-only --no-renames --diff-filter=D HEAD

APPS=()
FILE_APP=()
DELETED_APP=()
add_app() {
  local known
  for known in ${APPS[@]+"${APPS[@]}"}; do
    [ "$known" = "$1" ] && return 0
  done
  APPS+=("$1")
}
for f in ${FILES[@]+"${FILES[@]}"}; do
  a="$(tp_app_of "$WT" "$f")"
  FILE_APP+=("$a")
  add_app "$a"
done
for f in ${DELETED[@]+"${DELETED[@]}"}; do
  a="$(tp_app_of "$WT" "$f")"
  DELETED_APP+=("$a")
  add_app "$a"
done

{
  printf 'worktree: %s\nbase: %s (%s)\nchanged files: %s\n' "$WT" "$BASE" "$BASE_SHA" "${#FILES[@]}"
  for f in ${FILES[@]+"${FILES[@]}"}; do printf '  %s\n' "$f"; done
  printf 'deleted files: %s\n' "${#DELETED[@]}"
  for f in ${DELETED[@]+"${DELETED[@]}"}; do printf '  %s\n' "$f"; done
} >>"$TP_LOG"
printf 'worktree: %s\nbase: %s (%s)\nchanged files: %s\n' "$WT" "$BASE" "${BASE_SHA:0:10}" "${#FILES[@]}"
[ "${#DELETED[@]}" = 0 ] || printf 'deleted files: %s\n' "${#DELETED[@]}"

FAILED=0
CHECK_TOKENS=()  # name status exit seconds, four tokens per check of the record, added as the checks finish
RUN_SECS=0     # wall seconds spent in run_in since the caller last set it to 0
APPLIED=0      # checks that passed or failed
MISSING_NAMES=()

# rec NAME STATUS EXIT — add a check to the record. EXIT is empty for a check that did not run (JSON null). A check
# is never an aggregate: a per-app tool is recorded once per app, as NAME:APP.
rec() {
  CHECK_TOKENS+=("$1" "$2" "$3" "$RUN_SECS")
  case "$2" in
    passed | failed) APPLIED=$((APPLIED + 1)) ;;
    missing) MISSING_NAMES+=("$1") ;;
  esac
}

# rec_ran NAME RC — record a check that ran.
rec_ran() {
  if [ "$2" = 0 ]; then rec "$1" passed "$2"; else rec "$1" failed "$2"; fi
}

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

# count_in APP — how many changed files are in APP.
count_in() {
  local i=0 n=0 f
  for f in ${FILES[@]+"${FILES[@]}"}; do
    [ "${FILE_APP[$i]}" = "$1" ] && n=$((n + 1))
    i=$((i + 1))
  done
  printf '%s\n' "$n"
}

# deleted_in APP [EXT_REGEX] — how many deleted files were in APP (only those whose name matches EXT_REGEX, when given).
deleted_in() {
  local i=0 n=0 f re="\\.(${2:-})\$"
  for f in ${DELETED[@]+"${DELETED[@]}"}; do
    if [ "${DELETED_APP[$i]}" = "$1" ] && { [ -z "${2:-}" ] || [[ "$f" =~ $re ]]; }; then n=$((n + 1)); fi
    i=$((i + 1))
  done
  printf '%s\n' "$n"
}

# all_ts_of APP — set ALL_TS to every TS file of APP (tracked or untracked, not ignored), relative to APP's directory.
# Fails when git cannot list them. No pathspec on purpose: GIT_LITERAL_PATHSPECS=1 turns every pathspec, `:(glob)`
# magic included, into a literal name.
all_ts_of() {
  local f out re="\\.($TS_EXT)\$"
  ALL_TS=()
  out="$(mktemp "${TMPDIR:-/tmp}/tp-gate-list.XXXXXX")" || return 1
  if ! git -C "$WT/$1" ls-files -z --cached --others --exclude-standard >"$out" 2>>"$TP_LOG"; then
    rm -f "$out"
    return 1
  fi
  while IFS= read -r -d '' f; do
    if [[ "$f" =~ $re ]] && [ -f "$WT/$1/$f" ]; then ALL_TS+=("$f"); fi
  done <"$out"
  rm -f "$out"
}

# run_in APP NAME CMD... — run in APP's directory, output appended to the log under a heading. Sets RC and, when it
# failed, a few lines of the failure for the summary.
run_in() {
  local app="$1" name="$2" start t0="$SECONDS"
  shift 2
  start="$(wc -l <"$TP_LOG")"
  printf '== %s: %s\n' "$name" "$*" >>"$TP_LOG"
  (cd "$WT/$app" && "$@") >>"$TP_LOG" 2>&1
  RC=$?
  RUN_SECS=$((RUN_SECS + SECONDS - t0))
  printf '== %s exit %s\n' "$name" "$RC" >>"$TP_LOG"
  TAIL=""
  [ "$RC" = 0 ] || TAIL="$(sed -n "$((start + 2)),$((start + 9))p" "$TP_LOG" | sed 's/^/    /')"
}

# tool_check NAME TOOL EXT_REGEX ARGS... — run TOOL on the matching changed files of each app, and record the result
# per app as NAME:APP (passed, failed, missing, or not_applicable). No app's result stands for another's.
tool_check() {
  local name="$1" tool="$2" ext="$3" app bin any=0 rels label
  shift 3
  for app in ${APPS[@]+"${APPS[@]}"}; do
    rels=()
    while IFS= read -r line; do [ -z "$line" ] || rels+=("$line"); done < <(files_of "$app" "$ext")
    [ "${#rels[@]}" -gt 0 ] || continue
    any=1
    label="$name:$app"
    RUN_SECS=0
    if [ "$name" = eslint ] && ! has_eslint_config "$app"; then
      printf '%s skipped (no eslint config)\n' "$label"
      rec "$label" not_applicable ""
    elif ! bin="$(tp_find_tool "$tool" "$WT/$app" "$WT")"; then
      printf '%s skipped (not found)\n' "$label"
      rec "$label" missing ""
    else
      run_in "$app" "$label" "$bin" "$@" ${rels[@]+"${rels[@]}"}
      printf '%s %s\n' "$label" "$RC"
      rec_ran "$label" "$RC"
      if [ "$RC" != 0 ]; then FAILED=1; printf '%s\n' "$TAIL"; fi
    fi
  done
  if [ "$any" = 0 ]; then
    RUN_SECS=0
    printf '%s skipped (no files)\n' "$name"
    rec "$name" not_applicable ""
  fi
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

# Typecheck, one line per touched app that has a typecheck config (deno.json(c) or tsconfig.json). Any changed or
# deleted file in the app counts: a package.json, tsconfig, deno.json or .js change, or a deleted file that others
# import, can break the typecheck as well as a .ts one.
# `tsc --noEmit` checks the whole app. `deno check` needs files: the changed TS files when only TS changed (and nothing
# was deleted), else every TS file of the app. An app without a config is reported only when TS changed or was deleted in it.
TS_EXT='ts|tsx|mts|cts'
typed=0
for app in ${APPS[@]+"${APPS[@]}"}; do
  rels=()
  while IFS= read -r line; do [ -z "$line" ] || rels+=("$line"); done < <(files_of "$app" "$TS_EXT")
  label="typecheck:$app"
  RUN_SECS=0
  deleted_here="$(deleted_in "$app")"
  if [ -e "$WT/$app/deno.json" ] || [ -e "$WT/$app/deno.jsonc" ]; then
    typed=1
    if [ "$deleted_here" -gt 0 ] || [ "$(count_in "$app")" -gt "${#rels[@]}" ]; then
      if all_ts_of "$app"; then
        rels=(${ALL_TS[@]+"${ALL_TS[@]}"})
      else
        printf '%s 1\n  cannot list the ts files of %s\n' "$label" "$app"
        rec "$label" failed 1
        FAILED=1
        continue
      fi
    fi
    if [ "${#rels[@]}" = 0 ]; then
      if [ "$deleted_here" -gt 0 ]; then
        printf '%s skipped (nothing left to check after the deletions)\n' "$label"
      else
        printf '%s skipped (no ts files)\n' "$label"
      fi
      rec "$label" not_applicable ""
    elif bin="$(tp_find_deno "$WT")"; then
      run_in "$app" "$label" "$bin" check --allow-import "${rels[@]}"
      printf '%s %s\n' "$label" "$RC"
      rec_ran "$label" "$RC"
      if [ "$RC" != 0 ]; then FAILED=1; printf '%s\n' "$TAIL"; fi
    else
      printf '%s skipped (not found)\n' "$label"
      rec "$label" missing ""
    fi
  elif [ -e "$WT/$app/tsconfig.json" ]; then
    typed=1
    if bin="$(tp_find_tool tsc "$WT/$app" "$WT")"; then
      run_in "$app" "$label" "$bin" --noEmit
      printf '%s %s\n' "$label" "$RC"
      rec_ran "$label" "$RC"
      if [ "$RC" != 0 ]; then FAILED=1; printf '%s\n' "$TAIL"; fi
    else
      printf '%s skipped (not found)\n' "$label"
      rec "$label" missing ""
    fi
  elif [ "${#rels[@]}" -gt 0 ] || [ "$(deleted_in "$app" "$TS_EXT")" -gt 0 ]; then
    typed=1
    printf '%s skipped (no deno.json or tsconfig.json)\n' "$label"
    rec "$label" not_applicable ""
  fi
done
RUN_SECS=0
if [ "$typed" != 1 ]; then
  printf 'typecheck skipped (no ts files)\n'
  rec typecheck not_applicable ""
fi

RUN_SECS=0
if GL="$(command -v gitleaks)"; then
  run_in . gitleaks "$GL" git --no-banner --redact "--log-opts=$BASE_SHA..HEAD" .
  printf 'gitleaks %s\n' "$RC"
  rec_ran gitleaks "$RC"
  if [ "$RC" != 0 ]; then FAILED=1; printf '%s\n' "$TAIL"; fi
else
  printf 'gitleaks skipped (not found)\n'
  rec gitleaks missing ""
fi

# gitleaks-dirty: the uncommitted and untracked changed files, copied to a scratch directory (`gitleaks dir` scans one
# directory, and the worktree holds more than this change).
RUN_SECS=0
if [ "${#DIRTY[@]}" = 0 ]; then
  printf 'gitleaks-dirty skipped (no uncommitted files)\n'
  rec gitleaks-dirty not_applicable ""
elif [ -z "$GL" ]; then
  printf 'gitleaks-dirty skipped (not found)\n'
  rec gitleaks-dirty missing ""
else
  DIRTY_DIR="$(mktemp -d "${TMPDIR:-/tmp}/tp-gate-dirty.XXXXXX")" || tp_fail "cannot make a temp directory"
  COPY_ERR=""
  for f in "${DIRTY[@]}"; do
    if ! mkdir -p "$DIRTY_DIR/$(dirname "$f")" 2>>"$TP_LOG" || ! cp "$WT/$f" "$DIRTY_DIR/$f" 2>>"$TP_LOG" || [ ! -f "$DIRTY_DIR/$f" ]; then
      COPY_ERR="$f"
      break
    fi
  done
  if [ -n "$COPY_ERR" ]; then
    # A partial copy would scan only some of the files: the check fails without scanning.
    rm -rf "$DIRTY_DIR"
    printf 'gitleaks-dirty 1\n    cannot copy %s for the scan\n' "$COPY_ERR"
    rec gitleaks-dirty failed 1
    FAILED=1
  else
    run_in . gitleaks-dirty "$GL" dir --no-banner --redact "$DIRTY_DIR"
    rm -rf "$DIRTY_DIR"
    printf 'gitleaks-dirty %s\n' "$RC"
    rec_ran gitleaks-dirty "$RC"
    if [ "$RC" != 0 ]; then FAILED=1; printf '%s\n' "$TAIL"; fi
  fi
fi

# Steps that could not complete: one failed check, with every reason.
if [ "${#ERRORS[@]}" -gt 0 ]; then
  RUN_SECS=0
  printf 'collect 1\n'
  for e in "${ERRORS[@]}"; do printf '    %s\n' "$e"; printf '== collect: %s\n' "$e" >>"$TP_LOG"; done
  rec collect failed 1
  FAILED=1
fi

# --require: a required check that is missing fails the gate. NAME `typecheck` also covers every `typecheck:APP`.
for r in ${REQUIRED[@]+"${REQUIRED[@]}"}; do
  for m in ${MISSING_NAMES[@]+"${MISSING_NAMES[@]}"}; do
    if [ "$m" = "$r" ] || [[ "$m" == "$r":* ]]; then
      printf 'required check missing: %s\n' "$m"
      FAILED=1
    fi
  done
done

[ "$APPLIED" != 0 ] || printf 'no check applied\n'

# write_record PATH — the tp-gate:v1 record, written by tp_json_write (python json.dumps; atomic).
write_record() {
  local tok
  tok=(s schema tp-gate:v1 s worktree "$WT" s base.ref "$BASE" s base.sha "$BASE_SHA" s head "$HEAD_SHA" s tree "$TREE")
  tok+=(a files "${#FILES[@]}" ${FILES[@]+"${FILES[@]}"})
  tok+=(k checks "$((${#CHECK_TOKENS[@]} / 4))" ${CHECK_TOKENS[@]+"${CHECK_TOKENS[@]}"})
  tok+=(r exit "$FINAL" s started "$STARTED" s finished "$(tp_now)" s log "$TP_LOG")
  printf '%s\0' "${tok[@]}" | tp_json_write "$1"
}

FINAL=0
[ "$FAILED" = 0 ] || FINAL=1
write_record "${TP_LOG%.log}.json" || tp_fail "cannot write the record beside the log" 2
printf 'record: %s\n' "${TP_LOG%.log}.json"
if [ -n "${TP_TASK_DIR:-}" ]; then
  mkdir -p "$TP_TASK_DIR" || tp_fail "cannot create TP_TASK_DIR: $TP_TASK_DIR"
  if [ -e "$TP_TASK_DIR/gate.json" ]; then
    mv "$TP_TASK_DIR/gate.json" "$TP_TASK_DIR/gate-$(date +%Y%m%d-%H%M%S)-$$.json" ||
      tp_fail "cannot keep the previous $TP_TASK_DIR/gate.json" 2
  fi
  write_record "$TP_TASK_DIR/gate.json" || tp_fail "cannot write $TP_TASK_DIR/gate.json" 2
  printf 'record: %s\n' "$TP_TASK_DIR/gate.json"
fi

exit "$FINAL"
