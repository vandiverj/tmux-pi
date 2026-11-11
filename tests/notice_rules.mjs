// Runs the Claude plugin notifier's own notice rules (claude/plugins/tmux-pi/hooks/register.tsx, the text between
// "notice rules: begin" and "notice rules: end", with the file's top-level constants) under node, with no port: the
// TypeScript is stripped of its types (node:module stripTypeScriptTypes) and evaluated as is.
// stdin: {"source": <path of register.tsx>, "scenarios": {name: [step, ...]}}. A step is {"rows": [...], "submit": bool,
// "index"?: [...], "start"?: [...] | "failed", "stored"?: {...} | "unreadable"}: `rows` are tp-agent list rows (their
// `request_log` and `answer_route` go through the plugin's own requestFields, as readTasks does), "start" the rows the startup
// list read gave the start cutoff (session.start, before the index; "failed" when that read failed: startCut), "stored" the
// notifier value a new module takes up first (restore: storedCursors, `delivered`, `pending`), "index" the rows of an injected
// index (baseline), and "submit" whether this poll's wake succeeds.
// stdout: {name: [{"toasts": [...], "submitted": text | null, "cursors": {...}}, ...]}: per step, the hints toasted (the
// snapshot's first), the text submitted and the cursors after it.
import { readFileSync } from 'node:fs'
import { stripTypeScriptTypes } from 'node:module'
import vm from 'node:vm'

const input = JSON.parse(readFileSync(0, 'utf8'))
const text = readFileSync(input.source, 'utf8')
const begin = text.indexOf('// ---- notice rules: begin ----')
const end = text.indexOf('// ---- notice rules: end ----')
if (begin < 0 || end < begin) throw new Error('notice rules markers not found')
const constants = text.split('\n').filter(line => /^const [A-Z_]+ = (\/|new Set\()/.test(line)).join('\n')
const body = `${constants}\nlet delivered = new Set(), held = [], inFlight = [], cursors\n${text.slice(begin, end)}`
const code = stripTypeScriptTypes(body, { mode: 'strip' })

const out = {}
for (const [name, steps] of Object.entries(input.scenarios)) {
  // A fresh notifier per scenario: what a new session's module starts with.
  const context = vm.createContext({})
  vm.runInContext(`${code}
const parse = rows => rows.map(row => {
  const { request_log, answer_route, requests, ...rest } = row
  return { ...rest, ...requestFields(row) }
})
globalThis.step = (rows, submit, index, start, stored) => {
  const toasts = []
  if (stored !== undefined) {
    cursors = storedCursors(stored === 'unreadable' ? undefined : stored)
    delivered = new Set(stored === 'unreadable' ? [] : stored.delivered)
    held = stored === 'unreadable' ? [] : stored.pending
  }
  if (start !== undefined) {
    const waiting = new Set([...held, ...inFlight].map(noticeKey))
    const first = startCut(start === 'failed' ? undefined : parse(start)).filter(row => !waiting.has(noticeKey(row)))
    held = [...held, ...first]
    toasts.push(...first.map(hint))
  }
  if (index !== undefined) baseline(index)
  const fresh = announce(parse(rows))
  toasts.push(...fresh.map(hint))
  let submitted = null
  if (held.length > 0) {
    inFlight = held
    held = []
    submitted = wakeText(inFlight)
    if (submit) acknowledge(inFlight)
    else held = [...inFlight, ...held]
    inFlight = []
  }
  return { toasts, submitted, cursors: cursors === undefined ? null : Object.fromEntries(cursors) }
}`, context)
  out[name] = steps.map(s => context.step(s.rows, s.submit !== false, s.index, s.start, s.stored))
}
process.stdout.write(JSON.stringify(out))
