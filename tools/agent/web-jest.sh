#!/usr/bin/env bash
# web-jest.sh WORKTREE [--repeat N] [--config-hook PATH] TEST_PATH...
#
# Runs jest on the given test files, from the package that owns each one (the nearest directory above it whose
# package.json mentions jest, or that has a jest.config.*), with `--runTestsByPath --ci` and TZ=UTC.
# TEST_PATH is absolute or relative to WORKTREE. Quote a path that contains parentheses, as in
# "/wt/apps/web/app/(protected)/page.test.ts": the script passes every path on to jest as one argument, and it
# prints the command it ran with such paths quoted.
# Prints per-file counts (`PASS path passed=P failed=F skipped=S`, from jest's --verbose list) and jest's `Tests:` line.
# With --repeat N it runs the whole thing N times and prints one `run I EXIT` line per run with its per-file counts under it.
# --config-hook PATH: a JS file of the project (absolute, kept outside the tracked tree, e.g. in .tmux-pi/) that exports
#   function (config, { worktree, packageDir }) returning the jest config to use. The script wraps the package's own
#   jest config (jest.config.js|cjs|mjs, or the "jest" key of package.json; an async config function is awaited) in a scratch
#   config under the log directory and passes that with --config. Nothing project-specific is built in.
# The last line is `exit=N log=PATH`; N is 0 when every run passed, else the first nonzero exit of jest.

set -uo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

USAGE="usage: web-jest.sh WORKTREE [--repeat N] [--config-hook PATH] TEST_PATH..."
[ $# -ge 1 ] || tp_fail "$USAGE"
WT="$(tp_worktree "$1")" || tp_fail "not a git worktree: $1"
shift
REPEAT=1
HOOK=""
TESTS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --repeat)
      [ $# -ge 2 ] || tp_fail "--repeat needs a number. $USAGE"
      case "$2" in
        '' | *[!0-9]*) tp_fail "--repeat needs a whole number, got '$2'" ;;
      esac
      [ "$2" -ge 1 ] && [ "$2" -le 50 ] || tp_fail "--repeat must be 1 to 50, got $2"
      REPEAT="$2"
      shift 2
      ;;
    --config-hook)
      [ $# -ge 2 ] || tp_fail "--config-hook needs a path. $USAGE"
      HOOK="$2"
      shift 2
      ;;
    --) shift; while [ $# -gt 0 ]; do TESTS+=("$1"); shift; done ;;
    -*) tp_fail "unknown option $1. $USAGE" ;;
    *) TESTS+=("$1"); shift ;;
  esac
done
[ "${#TESTS[@]}" -gt 0 ] || tp_fail "no TEST_PATH. $USAGE"
if [ -n "$HOOK" ]; then
  case "$HOOK" in /*) ;; *) tp_fail "--config-hook must be an absolute path: $HOOK" ;; esac
  [ -f "$HOOK" ] || tp_fail "--config-hook file not found: $HOOK"
  command -v node >/dev/null 2>&1 || tp_fail "--config-hook needs node on PATH"
fi
tp_log_init web-jest "$WT"
LOGDIR="${TP_LOG%/*}"

# Each test: its absolute path (must exist, inside the worktree) and its owning package directory.
has_jest() {
  local d="$1" c
  for c in jest.config.js jest.config.cjs jest.config.mjs jest.config.ts jest.config.json; do
    [ -e "$d/$c" ] && return 0
  done
  [ -f "$d/package.json" ] && grep -q '"jest"' "$d/package.json"
}
owner_of() {
  local dir
  tp_dirname "$1"
  dir="$TP_DIR"
  while :; do
    if has_jest "$dir"; then printf '%s\n' "$dir"; return 0; fi
    if [ "$dir" = "$WT" ] || [ "$dir" = "/" ]; then return 1; fi
    tp_dirname "$dir"
    dir="$TP_DIR"
  done
}

PKGS=()
PKG_OF=()
ABS=()
for t in "${TESTS[@]}"; do
  case "$t" in /*) a="$t" ;; *) a="$WT/$t" ;; esac
  [ -f "$a" ] || tp_fail "test file not found: $a"
  tp_dirname "$a"
  a="$(cd "$TP_DIR" && pwd -P)/${a##*/}"
  case "$a" in "$WT"/*) ;; *) tp_fail "test file is outside the worktree: $a" ;; esac
  pkg="$(owner_of "$a")" || tp_fail "no package with jest above $a"
  ABS+=("$a")
  PKG_OF+=("$pkg")
  seen=0
  for p in ${PKGS[@]+"${PKGS[@]}"}; do
    [ "$p" = "$pkg" ] && seen=1
  done
  [ "$seen" = 1 ] || PKGS+=("$pkg")
done

# The scratch config that wraps a package's own jest config with the hook. It is the same text for every package and
# reads its inputs from the environment (TP_JEST_PACKAGE, TP_JEST_WORKTREE, TP_JEST_BASE, TP_JEST_HOOK), so no path is
# ever spliced into code.
write_wrapper() {
  WRAPPER="$LOGDIR/web-jest-wrapper-$$.config.js"
  cat >"$WRAPPER" <<'JS'
// scratch config made by web-jest.sh; not part of the project
module.exports = async () => {
  const pkg = process.env.TP_JEST_PACKAGE;
  let base = {};
  if (process.env.TP_JEST_BASE) {
    const loaded = await import(require("url").pathToFileURL(process.env.TP_JEST_BASE).href);
    base = loaded.default !== undefined ? loaded.default : loaded;
    if (typeof base === "function") base = await base();
  } else {
    try { base = require(pkg + "/package.json").jest || {}; } catch (e) { base = {}; }
  }
  const hook = require(process.env.TP_JEST_HOOK);
  const run = typeof hook === "function" ? hook : hook.default;
  const changed = (await run(base, { worktree: process.env.TP_JEST_WORKTREE, packageDir: pkg })) || base;
  return { rootDir: pkg, ...changed };
};
JS
}

# summarize — per-file counts from jest --verbose output on stdin.
summarize() {
  awk '
    /^(PASS|FAIL) / { if (file != "") flush(); status = $1; file = $2; for (i = 3; i <= NF; i++) file = file " " $i; p = f = s = 0; next }
    /^[ \t]*✓/ { p++; next }
    /^[ \t]*✕/ { f++; next }
    /^[ \t]*○/ { s++; next }
    function flush() { printf "%s %s passed=%d failed=%d skipped=%d\n", status, file, p, f, s }
    END { if (file != "") flush() }
  '
}

printf 'worktree: %s\n' "$WT"
FINAL=0
run=1
while [ "$run" -le "$REPEAT" ]; do
  RUN_RC=0
  RUN_OUT="$LOGDIR/.run-$$-$run.out"
  : >"$RUN_OUT"
  for pkg in "${PKGS[@]}"; do
    rels=()
    i=0
    for a in "${ABS[@]}"; do
      if [ "${PKG_OF[$i]}" = "$pkg" ]; then rels+=("${a#"$pkg"/}"); fi
      i=$((i + 1))
    done
    if ! JEST="$(tp_find_tool jest "$pkg" "$WT")"; then
      printf 'jest not found for %s\n' "$pkg" | tee -a "$TP_LOG" >>"$RUN_OUT"
      RUN_RC=127
      continue
    fi
    args=(--runTestsByPath --ci --verbose)
    base_cfg=""
    if [ -n "$HOOK" ]; then
      for c in jest.config.js jest.config.cjs jest.config.mjs; do
        if [ -e "$pkg/$c" ]; then base_cfg="$pkg/$c"; break; fi
      done
      if [ -z "$base_cfg" ] && [ -e "$pkg/jest.config.ts" ]; then
        printf 'cannot wrap jest.config.ts with --config-hook in %s; use a .js, .cjs or .mjs config\n' "$pkg" | tee -a "$TP_LOG" >>"$RUN_OUT"
        RUN_RC=2
        continue
      fi
      [ -n "${WRAPPER:-}" ] || write_wrapper
      args+=(--config "$WRAPPER")
    fi
    {
      printf '== run %s: cd %s && TZ=UTC' "$run" "$(printf '%q' "$pkg")"
      printf ' %q' "$JEST" "${args[@]}" "${rels[@]}"
      printf '\n'
    } >>"$TP_LOG"
    (cd "$pkg" && TZ=UTC TP_JEST_PACKAGE="$pkg" TP_JEST_WORKTREE="$WT" TP_JEST_BASE="$base_cfg" TP_JEST_HOOK="$HOOK" "$JEST" "${args[@]}" "${rels[@]}") >"$RUN_OUT.jest" 2>&1
    rc=$?
    cat "$RUN_OUT.jest" >>"$TP_LOG"
    cat "$RUN_OUT.jest" >>"$RUN_OUT"
    printf '== run %s jest exit %s\n' "$run" "$rc" >>"$TP_LOG"
    rm -f "$RUN_OUT.jest"
    if [ "$rc" != 0 ] && [ "$RUN_RC" = 0 ]; then RUN_RC="$rc"; fi
  done
  [ "$REPEAT" -le 1 ] || printf 'run %s %s\n' "$run" "$RUN_RC"
  summarize <"$RUN_OUT" | sed 's/^/  /'
  grep -E '^Tests:' "$RUN_OUT" | sed 's/^/  /'
  grep -E '^(jest not found|cannot wrap)' "$RUN_OUT" | sed 's/^/  /'
  rm -f "$RUN_OUT"
  if [ "$FINAL" = 0 ] && [ "$RUN_RC" != 0 ]; then FINAL="$RUN_RC"; fi
  run=$((run + 1))
done
exit "$FINAL"
