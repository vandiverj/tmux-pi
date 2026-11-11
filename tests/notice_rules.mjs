// Runs the Claude plugin notifier's own notice rules (claude/plugins/tmux-pi/hooks/register.tsx, the text between
// "notice rules: begin" and "notice rules: end", with the file's top-level constants) under node, with no port: the
// TypeScript is stripped of its types (node:module stripTypeScriptTypes) and evaluated as is.
// stdin: {"source": <path of register.tsx>, "scenarios": {name: [step, ...]}}. A step is {"rows": [...], "submit": bool,
// "index"?: [...]}: `rows` are tp-agent list rows (their `requests` go through the plugin's own requestRefs, as readTasks
// does), "index" the rows of an injected index (baseline), and "submit" whether this poll's wake succeeds.
// stdout: {name: [{"toasts": [...], "submitted": text | null}, ...]}: per step, the hints toasted and the text submitted.
import { readFileSync } from 'node:fs'
import { stripTypeScriptTypes } from 'node:module'
import vm from 'node:vm'

const input = JSON.parse(readFileSync(0, 'utf8'))
const text = readFileSync(input.source, 'utf8')
const begin = text.indexOf('// ---- notice rules: begin ----')
const end = text.indexOf('// ---- notice rules: end ----')
if (begin < 0 || end < begin) throw new Error('notice rules markers not found')
const constants = text.split('\n').filter(line => /^const [A-Z_]+ = (\/|new Set\()/.test(line)).join('\n')
const body = `${constants}\nlet delivered = new Set(), held = [], inFlight = [], history\n${text.slice(begin, end)}`
const code = stripTypeScriptTypes(body, { mode: 'strip' })

const out = {}
for (const [name, steps] of Object.entries(input.scenarios)) {
  // A fresh notifier per scenario: what a new session's module starts with.
  const context = vm.createContext({})
  vm.runInContext(`${code}
globalThis.step = (rows, submit, index) => {
  if (index !== undefined) baseline(index)
  const read = rows.map(row => {
    const { requests, ...rest } = row
    return { ...rest, ...requestRefs(requests) }
  })
  const fresh = announce(read)
  let submitted = null
  if (held.length > 0) {
    inFlight = held
    held = []
    submitted = wakeText(inFlight)
    if (submit) acknowledge(inFlight)
    else held = [...inFlight, ...held]
    inFlight = []
  }
  return { toasts: fresh.map(hint), submitted }
}`, context)
  out[name] = steps.map(s => context.step(s.rows, s.submit !== false, s.index))
}
process.stdout.write(JSON.stringify(out))
