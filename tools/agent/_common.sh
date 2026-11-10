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

# tp_tree_fingerprint WORKTREE — print the tree sha of everything in the working tree that git would add: tracked
# changes and untracked files that are not ignored (deletions are simply absent). It is `git write-tree` on a temporary
# index filled by `git add -A`, so the real index is not touched. The same content gives the same sha. HEAD is separate
# (`git rev-parse HEAD`). lib/gate_record.py:fingerprint takes the same steps and a test holds the two equal.
tp_tree_fingerprint() {
  local dir idx tree rc=0
  dir="$(mktemp -d "${TMPDIR:-/tmp}/tp-fp.XXXXXX")" || return 1
  idx="$dir/index"
  (cd "$1" && GIT_INDEX_FILE="$idx" git add -A >/dev/null 2>&1) || rc=1
  if [ "$rc" = 0 ]; then
    tree="$(GIT_INDEX_FILE="$idx" git -C "$1" write-tree 2>/dev/null)" || rc=1
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

# tp_now — UTC time, ISO 8601.
tp_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# tp_tsc_conf_refs — the `path` of each project reference in the `tsc --showConfig` output held in $TP_TSC_CONF, as
# written, one per line.
tp_tsc_conf_refs() {
  printf '%s\n' "$TP_TSC_CONF" | sed -n '/^    "references": \[/,/^    \]/p' | sed -n 's/^ *"path": "\(.*\)",\{0,1\}$/\1/p'
}

# tp_tsc_project CONFIG TSC WORKTREE — what `tsc -b` of the project CONFIG (an absolute tsconfig path) would write, and
# whether git ignores all of it. Sets TP_TSC_WHY (empty when every output is an ignored, untracked path, else a short
# reason) and TP_TSC_REFS (the absolute tsconfig path of each project it references). The outputs, from --showConfig:
#   - JS (and its map) unless emitDeclarationOnly: into outDir, else next to each source file;
#   - declarations (and their map) when declaration or composite: into declarationDir, else outDir, else next to each
#     source file (the files come from --listFilesOnly);
#   - the .tsbuildinfo: tsBuildInfoFile, else inside outDir, else next to the config.
# A directory output is checked with a probe path inside it and for tracked files; a file output with git check-ignore
# (a tracked file is never reported as ignored, so a tracked a.js that the build would overwrite is unsafe).
tp_tsc_project() {
  local cfg="$1" bin="$2" wt="$3" dir line key val ref out="" decl="" bi="" decl_on=0 decl_only=0 smap=0 dmap=0 jsx=""
  local jsext dtsext stem f src bad="" d
  local dirs=() paths=()
  TP_TSC_WHY=""
  TP_TSC_REFS=()
  dir="${cfg%/*}"
  TP_TSC_CONF="$(cd "$dir" && "$bin" --showConfig -p "$cfg" 2>>"$TP_LOG")" || { TP_TSC_WHY="its config cannot be read"; return 0; }
  while IFS= read -r line; do
    key="${line%%=*}"
    val="${line#*=}"
    val="${val%,}"
    val="${val#\"}"
    val="${val%\"}"
    case "$key" in
      outDir) out="$val" ;;
      declarationDir) decl="$val" ;;
      tsBuildInfoFile) bi="$val" ;;
      jsx) jsx="$val" ;;
      composite | declaration) if [ "$val" = true ]; then decl_on=1; fi ;;
      emitDeclarationOnly) if [ "$val" = true ]; then decl_only=1; fi ;;
      sourceMap) if [ "$val" = true ]; then smap=1; fi ;;
      declarationMap) if [ "$val" = true ]; then dmap=1; fi ;;
    esac
  done < <(printf '%s\n' "$TP_TSC_CONF" | sed -n 's/^        "\([A-Za-z]*\)": \(.*\)$/\1=\2/p')
  out="${out#./}"
  decl="${decl#./}"
  bi="${bi#./}"
  while IFS= read -r ref; do
    [ -n "$ref" ] || continue
    if [ -d "$dir/$ref" ]; then
      ref="$(cd "$dir/$ref" && pwd -P)/tsconfig.json"
    else
      ref="$(cd "$dir/$(dirname "$ref")" 2>/dev/null && pwd -P)/$(basename "$ref")"
    fi
    TP_TSC_REFS+=("$ref")
  done < <(tp_tsc_conf_refs)

  # Where the outputs go. 1 = next to the sources.
  local js_src=0 dts_src=0
  if [ "$decl_only" = 0 ]; then
    if [ -n "$out" ]; then dirs+=("$out"); else js_src=1; fi
  fi
  if [ "$decl_on" = 1 ]; then
    if [ -n "$decl" ]; then dirs+=("$decl"); elif [ -n "$out" ]; then dirs+=("$out"); else dts_src=1; fi
  fi
  if [ -n "$bi" ]; then
    paths+=("$bi")
  elif [ -n "$out" ]; then
    dirs+=("$out")
  else
    f="${cfg##*/}"
    paths+=("${f%.json}.tsbuildinfo")
  fi
  # The relative outputs above are relative to the config's directory.
  local rel=()
  for f in ${paths[@]+"${paths[@]}"}; do
    case "$f" in /*) rel+=("$f") ;; *) rel+=("$dir/$f") ;; esac
  done
  paths=(${rel[@]+"${rel[@]}"})
  for d in ${dirs[@]+"${dirs[@]}"}; do
    case "$d" in /*) ;; *) d="$dir/$d" ;; esac
    if [ -n "$(git -C "$wt" ls-files -- "$d" 2>/dev/null)" ]; then
      TP_TSC_WHY="writes into ${d#"$wt"/}, which holds tracked files"
      return 0
    fi
    paths+=("$d/.tp-gate-probe")
  done
  if [ "$js_src" = 1 ] || [ "$dts_src" = 1 ]; then
    src="$(cd "$dir" && "$bin" --listFilesOnly -p "$cfg" 2>>"$TP_LOG")" || { TP_TSC_WHY="its file list cannot be read"; return 0; }
    while IFS= read -r f; do
      case "$f" in
        "$wt"/node_modules/* | "$wt"/*/node_modules/*) continue ;;
        "$wt"/*) ;;
        *) continue ;;
      esac
      case "$f" in
        *.d.ts | *.d.mts | *.d.cts) continue ;;
        *.ts) jsext=js dtsext=d.ts ;;
        *.tsx) jsext=js dtsext=d.ts; if [ "$jsx" = preserve ]; then jsext=jsx; fi ;;
        *.mts) jsext=mjs dtsext=d.mts ;;
        *.cts) jsext=cjs dtsext=d.cts ;;
        *) continue ;;
      esac
      stem="${f%.*}"
      if [ "$js_src" = 1 ]; then
        paths+=("$stem.$jsext")
        if [ "$smap" = 1 ]; then paths+=("$stem.$jsext.map"); fi
      fi
      if [ "$dts_src" = 1 ]; then
        paths+=("$stem.$dtsext")
        if [ "$dmap" = 1 ]; then paths+=("$stem.$dtsext.map"); fi
      fi
    done <<<"$src"
  fi
  # `-v -n` prints a path no pattern matches as `::<TAB>path`.
  bad="$(printf '%s\n' ${paths[@]+"${paths[@]}"} | git -C "$wt" check-ignore --stdin -v -n 2>>"$TP_LOG" | sed -n 's/^::	//p' | head -n 1)"
  if [ -n "$bad" ]; then
    bad="${bad#"$wt"/}"
    TP_TSC_WHY="would write ${bad%/.tp-gate-probe}, which git does not ignore (or tracks)"
  fi
  return 0
}

# tp_tsc_unsafe_refs WORKTREE TSC APP_DIR REF... — REF are the project references of the app in APP_DIR (as written).
# Prints `<project>: <reason>` for the first project, among them and the projects they reference (transitively), whose
# `tsc -b` would write a path git does not ignore; prints nothing when every build writes only ignored files.
tp_tsc_unsafe_refs() {
  local wt="$1" bin="$2" app="$3" queue=() seen=$'\n' i=0 cfg ref
  shift 3
  for ref in "$@"; do
    if [ -d "$app/$ref" ]; then
      queue+=("$(cd "$app/$ref" && pwd -P)/tsconfig.json")
    else
      queue+=("$(cd "$app/$(dirname "$ref")" 2>/dev/null && pwd -P)/$(basename "$ref")")
    fi
  done
  while [ "$i" -lt "${#queue[@]}" ]; do
    cfg="${queue[$i]}"
    i=$((i + 1))
    case "$seen" in *$'\n'"$cfg"$'\n'*) continue ;; esac
    seen="$seen$cfg"$'\n'
    tp_tsc_project "$cfg" "$bin" "$wt"
    if [ -n "$TP_TSC_WHY" ]; then
      printf '%s: %s\n' "${cfg#"$wt"/}" "$TP_TSC_WHY"
      return 0
    fi
    queue+=(${TP_TSC_REFS[@]+"${TP_TSC_REFS[@]}"})
  done
  return 0
}
