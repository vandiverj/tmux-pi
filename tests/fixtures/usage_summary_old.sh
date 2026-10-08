# The usage_summary bin/tp-status ran (one jq over every state.json) before lib/tp_window.py took the summary over with a per-task cache,
# kept verbatim as usage_summary_old so tests/test_tp_window.py can compare old and new output. Delete with that test.
# Needs $tasks (the tasks directory) set by the caller.
# usage_summary — honest session token usage, one line per output line.
# New states carry byModel (counted per message); states without it, and any
# tokens byModel does not account for, go to an "unattributed" bucket rather
# than being assigned to the last-reported model. Cache reads/writes stay apart
# from uncached input. Claude cost is unknown (costKnown:false), never $0.
usage_summary_old() {
  local files=() f nt=0
  for f in "$tasks"/*/state.json; do [[ -f "$f" ]] && files+=("$f"); done
  nt=$(find "$tasks" -mindepth 2 -maxdepth 2 -name meta.json 2>/dev/null | wc -l)
  main_state="$(dirname "$tasks")/main/state.json"
  [[ -f "$main_state" ]] && files+=("$main_state")
  (( ${#files[@]} )) || return 0
  jq -nr --argjson nt "$nt" --arg main "$main_state" '
    def k: if . >= 1000000 then "\(. / 100000 | floor / 10)M"
           elif . >= 1000 then "\(. / 100 | floor / 10)k" else tostring end;
    def num: if type == "number" then . else 0 end;
    def tk: {input: (.input|num), output: (.output|num), cacheRead: (.cacheRead|num), cacheWrite: (.cacheWrite|num)};
    def zero: {input:0, output:0, cacheRead:0, cacheWrite:0};
    def plus($b): {input: (.input + $b.input), output: (.output + $b.output), cacheRead: (.cacheRead + $b.cacheRead), cacheWrite: (.cacheWrite + $b.cacheWrite)};
    def minus0($b): {input: ([.input - $b.input, 0] | max), output: ([.output - $b.output, 0] | max), cacheRead: ([.cacheRead - $b.cacheRead, 0] | max), cacheWrite: ([.cacheWrite - $b.cacheWrite, 0] | max)};
    def sum: reduce .[] as $t (zero; plus($t));
    def empty4: (.input + .output + .cacheRead + .cacheWrite) == 0;
    def costtxt: (map(select(.cost != null) | .cost) | add // 0) as $c
      | (map(select(.cost == null)) | length > 0) as $u
      | if $u then (if $c > 0 then "$\($c * 1000 | round / 1000)+?" else "$?" end) else "$\($c * 1000 | round / 1000)" end;
    def counts: "  in \(.input|k)  cache-read \(.cacheRead|k)  cache-write \(.cacheWrite|k)  out \(.output|k)";
    [inputs | select(type == "object") | {f: input_filename, s: .}] as $all
    | [ $all[] | select((.s.tokens | type) == "object" or (.s.byModel | type) == "object") ] as $used
    | [ $used[] | .s as $s
        | (($s.harness == "claude") or ($s.costKnown == false)) as $unk
        | (if ($s.byModel | type) == "object" then $s.byModel else {} end) as $bm
        | ($bm | to_entries | map({model: .key, t: (.value | tk), cost: (if $unk then null else (.value.cost | if type == "number" then . else null end) end)})) as $att
        | ($s.tokens | if type == "object" then tk else zero end) as $tot
        | ($att | map(.t) | sum) as $got
        | ($tot | minus0($got)) as $rem
        | ((($s.cost | num) - ($att | map(.cost // 0) | add // 0)) | [., 0] | max) as $remcost
        | $att
          + (if ($rem | empty4) and ($att | length) > 0 then []
             else [{model: null, t: (if ($att | length) > 0 then $rem else $tot end),
                    cost: (if $unk then null else $remcost end), last: ($s.model // ""), partial: (($att | length) > 0)}] end)
      ] | flatten as $ents
    | ($ents | map(select(.model != null)) | group_by(.model) | map({model: .[0].model, t: (map(.t) | sum), cost: map({cost})})) as $models
    | ($ents | map(select(.model == null and (.t | empty4 | not)))) as $leg
    | ($used | length) as $nrec
    | ([ $used[] | {f, zero: (.s | (if (.tokens | type) == "object" then (.tokens | tk) else ((.byModel // {}) | [.[] | tk] | sum) end) | empty4)} ]) as $recs
    | ([ $recs[] | select(.f != $main) ] | length) as $nwork
    | ([ $recs[] | select(.zero) ] | length) as $nzero
    | (if ($recs | map(select(.f == $main)) | length) > 0 then 1 else 0 end) as $hasmain
    | "session tokens (per-message by model; cache separate)",
      ($models[] | "\(.model)", "\(.t | counts)  \(.cost | costtxt)"),
      (if ($leg | length) > 0 then
         ($leg | map(.last | select(. != "")) | unique) as $lasts
         | "unattributed (legacy/partial; model switches not ruled out)",
           "\($leg | map(.t) | sum | counts)  \($leg | map({cost}) | costtxt)",
           (if ($lasts | length) > 0 then "  last reported model: \($lasts | join(", ")) (not an attribution)" else empty end)
       else empty end),
      ([$models[].t] + [$leg[].t] | sum | "total\(counts)"),
      "records \($nrec) (main \($hasmain), workers \($nwork)); zero-token \($nzero); workers with no usage data \([$nt - $nwork, 0] | max)"
  ' "${files[@]}" 2>/dev/null
}
