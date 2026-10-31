#!/usr/bin/env bash
# lane-info.sh WORKTREE [BASE_REF] [--fetch]
#
# The orientation a worker does by hand at the start of a task, as one plain command: branch, HEAD, base ref and sha,
# merge-base, ahead/behind, `git diff --name-status BASE...HEAD`, uncommitted files, whether node_modules is a symlink
# (the root and every directory with a package.json), and where deno is.
# BASE_REF defaults to env LANE_BASE, then the upstream branch; with neither, the base lines say so and are left out.
# It runs `git fetch` only with --fetch (the only network use), and says what the fetch returned.
# Project facts are printed by built-in hooks when their files exist: the newest file in a drizzle/ folder.
# The full output is also written to the log. The last line is `exit=N log=PATH`.

set -uo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

FETCH=0
POS=()
for arg in "$@"; do
  case "$arg" in
    --fetch) FETCH=1 ;;
    -*) tp_fail "unknown option $arg. usage: lane-info.sh WORKTREE [BASE_REF] [--fetch]" ;;
    *) POS+=("$arg") ;;
  esac
done
[ "${#POS[@]}" -ge 1 ] && [ "${#POS[@]}" -le 2 ] || tp_fail "usage: lane-info.sh WORKTREE [BASE_REF] [--fetch]"
WT="$(tp_worktree "${POS[0]}")" || tp_fail "not a git worktree: ${POS[0]}"
BASE="${POS[1]:-${LANE_BASE:-}}"
if [ -z "$BASE" ]; then
  BASE="$(git -C "$WT" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null)" || BASE=""
fi
tp_log_init lane-info "$WT"

g() { git -c core.quotepath=off -C "$WT" "$@"; }

{
  printf 'worktree: %s\n' "$WT"
  printf 'branch: %s\n' "$(g rev-parse --abbrev-ref HEAD 2>/dev/null)"
  printf 'head: %s\n' "$(g log -1 --format='%h %s' 2>/dev/null)"

  if [ "$FETCH" = 1 ]; then
    if out="$(g fetch 2>&1)"; then
      printf 'fetch: done%s\n' "${out:+ ($(printf '%s' "$out" | tail -n 1))}"
    else
      printf 'fetch: failed (%s)\n' "$(printf '%s' "$out" | tail -n 1)"
    fi
  else
    printf 'fetch: not run (pass --fetch)\n'
  fi

  if [ -z "$BASE" ]; then
    printf 'base: none (pass BASE_REF, set LANE_BASE, or give the branch an upstream)\n'
  elif ! BASE_SHA="$(g rev-parse --verify --quiet "$BASE^{commit}")"; then
    printf 'base: %s (not found)\n' "$BASE"
  else
    printf 'base: %s %s\n' "$BASE" "$(printf '%s' "$BASE_SHA" | cut -c1-10)"
    mb="$(g merge-base "$BASE_SHA" HEAD 2>/dev/null)" || mb=""
    printf 'merge-base: %s\n' "${mb:-none (unrelated histories)}"
    printf 'ahead/behind: %s\n' "$(g rev-list --left-right --count "HEAD...$BASE_SHA" 2>/dev/null | awk '{print $1 "/" $2}')"
    printf 'changed against base (git diff --name-status %s...HEAD):\n' "$BASE"
    changed="$(g diff --name-status "$BASE_SHA...HEAD" 2>/dev/null)"
    if [ -n "$changed" ]; then printf '%s\n' "$changed" | sed 's/^/  /'; else printf '  none\n'; fi
  fi

  printf 'uncommitted:\n'
  dirty="$(g status --porcelain 2>/dev/null)"
  if [ -n "$dirty" ]; then printf '%s\n' "$dirty" | sed 's/^/  /'; else printf '  none\n'; fi

  printf 'node_modules:\n'
  found=0
  dirs="$(cd "$WT" && { printf '.\n'; find . -maxdepth 4 -name node_modules -prune -o -name package.json -print 2>/dev/null \
    | sed -e 's|^\./||' -e 's|/package.json$||' -e '/^package.json$/d'; } | sort -u)"
  while IFS= read -r d; do
    [ -n "$d" ] || continue
    [ -e "$WT/$d/package.json" ] || continue
    found=1
    nm="$WT/$d/node_modules"
    if [ -L "$nm" ]; then
      printf '  %s: symlink -> %s\n' "$d" "$(readlink "$nm")"
    elif [ -d "$nm" ]; then
      printf '  %s: directory\n' "$d"
    else
      printf '  %s: missing\n' "$d"
    fi
  done <<<"$dirs"
  [ "$found" = 1 ] || printf '  no package.json\n'

  if deno="$(tp_find_deno "$WT")"; then printf 'deno: %s\n' "$deno"; else printf 'deno: not found\n'; fi

  # Hook: the newest file in each drizzle/ folder (highest name; migration files carry a number prefix).
  while IFS= read -r d; do
    [ -n "$d" ] || continue
    newest="$(ls -1 "$WT/$d" 2>/dev/null | grep -v '^meta$' | sort | tail -n 1)"
    [ -z "$newest" ] || printf 'drizzle: newest in %s is %s\n' "$d" "$newest"
  done < <(cd "$WT" && find . -maxdepth 5 -name node_modules -prune -o -type d -name drizzle -print 2>/dev/null | sed 's|^\./||' | sort)
} 2>&1 | tee -a "$TP_LOG"

exit 0
