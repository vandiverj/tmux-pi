# The per-task shell row builder tp_rows had before the one-pass rewrite (lib/tp_rows.py), kept
# verbatim as tp_rows_old so tests/test_tp_rows.py can compare old and new output. Delete with that test.
# tp_rows <session> — one line per subagent, oldest first, fields separated by
# \x1f (a tab would collapse empty fields in `read`):
#   id worker status tool tokens cost model
# tokens is ↑uncached-input cCACHE(read+write) ↓output; cost is $? when unknown (Claude).
# status merges the reporter's state.json with the pane: a dead pane is done or
# failed:<code> from run.sh's exit file, a missing pane is killed.
tp_rows_old() {
  local tasks panes meta d pane pstate code row
  tasks="$(tp_opt "$1" @tp_state_dir)/tasks"
  panes="$(tmux list-panes -a -F '#{pane_id} #{pane_dead}')"
  while IFS= read -r -d '' meta; do
    d="$(dirname "$meta")"
    row="$( { cat "$meta"; cat "$d/state.json" 2>/dev/null || echo '{}'; } | jq -rs '
      .[0] as $m | .[1] as $s
      | def k: if . >= 1000000 then "\(. / 100000 | floor / 10)M"
               elif . >= 1000 then "\(. / 100 | floor / 10)k" else tostring end;
      [ $m.id, $m.worker, $m.pane, ($s.state // "starting"), ($s.tool // ""),
        (if $s.tokens then "↑\($s.tokens.input | k) c\((($s.tokens.cacheRead // 0) + ($s.tokens.cacheWrite // 0)) | k) ↓\($s.tokens.output | k)" else "" end),
        (if $s.costKnown == false or ($s.harness == "claude" and ($s.cost // 0) == 0) then "$?"
         elif ($s.cost // 0) > 0 then "$\($s.cost * 1000 | round / 1000)" else "" end),
        ([ ($s.model // (if $m.model == "" then "default" else $m.model end)),
           (if $m.thinking == "" then empty else $m.thinking end) ] | join(":")) ]
      | join("\u001f")' 2>/dev/null)" || continue
    pane="$(cut -d $'\x1f' -f3 <<<"$row")"
    pstate="$(awk -v p="$pane" '$1 == p { print $2 }' <<<"$panes")"
    if [[ "$pstate" == 1 ]]; then
      code="$(cat "$d/exit" 2>/dev/null || echo "?")"
      if [[ "$code" == 0 ]]; then pstate=done; else pstate="failed:$code"; fi
    elif [[ "$pstate" != 0 ]]; then
      pstate=killed
    else
      pstate=""
    fi
    # Drop the pane field; a dead or missing pane overrides the reported state.
    awk -F $'\x1f' -v OFS=$'\x1f' -v ps="$pstate" \
      '{ if (ps != "") { $4 = ps; $5 = "" } print $1, $2, $4, $5, $6, $7, $8 }' <<<"$row"
  done < <(python3 -c '
import os, pathlib, sys
paths = pathlib.Path(sys.argv[1]).glob("*/meta.json")
for path in sorted(paths, key=lambda p: p.stat().st_mtime):
    sys.stdout.buffer.write(os.fsencode(path) + b"\0")
' "$tasks")
}

