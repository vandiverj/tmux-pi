// Main-agent module for the tmux-pi plugin. The classic command hooks in hooks.json
// (report.sh, sensitive-read.sh) stay the cross-harness contract; this module only
// adds main-side conveniences on top of bin/tp-agent and never replaces them.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, SessionStartInput } from 'claude-code'

import type { NotifierState, TaskRow, TaskSummary } from '../types'

const TP_AGENT_TIMEOUT_MS = 20000
const POLL_MS = 5000
const INDEX_CHARS = 6000
const NOTABLE = new Set(['needs_input', 'settled', 'paused', 'failed'])
const WAKE_TRIES = 3
const UNREAD_EPOCH = 1e15
// tp-agent's own id pattern (IDENT in bin/tp-agent): no leading dash, so never an option.
const TASK_ID = /^[A-Za-z0-9][A-Za-z0-9._-]*$/

// The one gate. Workers, headless runs, legacy mains and tp-claude-bootstrap load this
// plugin too; every hook returns next(e) untouched while this is false.
let isActive = false

// Notifier state.
// - `delivered`: `id:status` keys already announced, dropped when the task leaves that
//   status, so a second permission prompt on the same task notifies again.
// - `held`: rows found but not yet in a submit; `inFlight`: the batch of a submit that has
//   not resolved (it resolves when its turn starts, which waits for the main to be idle).
// The rows the main actually read in the index appended at start are the baseline and wake
// nobody. Anything it was not shown (no index, a refused append, rows cut for length)
// stays eligible and is announced by the first poll.
// All of it is mirrored into ONE session-state value, written in one piece, because a hot
// reload resets module variables: the next module takes it up, so a reload neither repeats
// an acknowledged wake nor loses one still waiting. Each fresh module claims the value
// with a higher `epoch`; a module that finds a higher one stored has been replaced and
// goes quiet without overwriting its successor. An unresolved submit of the old module
// may still land, so each reload can repeat at most the wake that was in flight then.
let delivered = new Set<string>()
let held: TaskRow[] = []
let inFlight: TaskRow[] = []
let wakeFailures = 0
let isSubmitting = false
let isPolling = false
let isLoaded = false
let isIndexed = false
let isSuperseded = false
let epoch = 0
let saved = ''
let timer: { cancel: () => void } | undefined

// Written by every poll (and primed once at an active start); read by the band.
const tasks = atom({ plugin: 'tmux-pi', key: 'tasks' } as const, null)
// Session state survives a hot reload; module variables do not.
const notifier = atom({ plugin: 'tmux-pi', key: 'notifier' } as const, { epoch: 0, isIndexed: false, delivered: [], pending: [] })

// Helpers that take `$` must be top-level function declarations (validate refuses others),
// and `$.env.get` takes string literals only.
async function readGate($: EngineInterface, e: SessionStartInput): Promise<boolean> {
  if (!e.isInteractive) return false
  if ((await $.env.get('TP_TASK_DIR')) !== undefined) return false
  if (!(await $.env.get('TP_ROOT'))) return false
  return (await $.env.get('TP_MODE')) === 'orchestrator'
}

// Runs ${TP_ROOT}/bin/<tool> by argv (no shell). A timeout or a command that cannot
// start rejects; that reads as undefined.
async function runTool($: EngineInterface, tool: 'tp-agent' | 'tp-view', args: readonly string[]) {
  const root = await $.env.get('TP_ROOT')
  if (!root) return undefined
  try {
    return await $.process.run([`${root}/bin/${tool}`, ...args], { timeoutMs: TP_AGENT_TIMEOUT_MS })
  } catch {
    return undefined
  }
}

// Answers tp-agent's parsed JSON, or undefined on a non-zero exit, a timeout or output
// that is not JSON.
async function runTpAgent($: EngineInterface, args: readonly string[]): Promise<unknown> {
  const ran = await runTool($, 'tp-agent', args)
  if (ran === undefined || ran.exitCode !== 0) return undefined
  try {
    return JSON.parse(ran.stdout)
  } catch {
    return undefined
  }
}

// `tp-agent list` is read-only: no tick, no index write.
async function readTasks($: EngineInterface): Promise<TaskRow[] | undefined> {
  const rows = await runTpAgent($, ['list'])
  if (!Array.isArray(rows)) return undefined
  const found: TaskRow[] = []
  for (const row of rows) {
    if (typeof row?.id !== 'string' || typeof row.status !== 'string') continue
    found.push({
      id: row.id,
      status: row.status,
      role: typeof row.role === 'string' ? row.role : null,
      permission: row.permission ?? null,
      capability_request: row.capability_request ?? null,
      capability_pending: row.capability_pending === true,
      result_status: typeof row.result_status === 'string' ? row.result_status : null,
    })
  }
  return found
}

function summarize(rows: readonly TaskRow[]): TaskSummary {
  const summary: TaskSummary = { active: 0, toCollect: 0, paused: 0, pausedWithStatus: 0, failed: 0, needsInput: [] }
  for (const row of rows) {
    if (row.status === 'needs_input') summary.needsInput.push(row.id)
    else if (row.status === 'settled') summary.toCollect += 1
    else if (row.status === 'paused') {
      if (row.result_status === null) summary.paused += 1
      else summary.pausedWithStatus += 1
    }
    else if (row.status === 'failed') summary.failed += 1
    else if (row.status === 'queued' || row.status === 'starting' || row.status === 'working') summary.active += 1
  }
  return summary
}

function isQuiet(summary: TaskSummary): boolean {
  return summary.active + summary.toCollect + summary.paused + summary.pausedWithStatus + summary.failed + summary.needsInput.length === 0
}

function bandText(summary: TaskSummary): string {
  const parts = [`${summary.active} active`, `${summary.toCollect} to collect`, `${summary.failed} failed`]
  if (summary.paused > 0) parts.push(`${summary.paused} replied without a result status`)
  if (summary.pausedWithStatus > 0) parts.push(`${summary.pausedWithStatus} paused`)
  if (summary.needsInput.length > 0) parts.push(`needs input: ${summary.needsInput.join(', ')}`)
  return `workers: ${parts.join(' · ')}`
}

function clip(text: string, columns: number): string {
  if (columns <= 0 || text.length <= columns) return text
  return columns === 1 ? '…' : `${text.slice(0, columns - 1)}…`
}

// What a worker that waits for the main is waiting on, beyond its state: a pending capability request
// (capability-request.json), a capability awaiting human approval (pending-capability.json, exported
// as capability_pending) or a pending permission prompt. tp-agent keeps returning an answered request, so only pending counts.
function awaiting(row: { permission?: unknown; capability_request?: unknown; capability_pending?: unknown }): string {
  const pending = (value: unknown) => typeof value === 'object' && value !== null && (value as { status?: unknown }).status === 'pending'
  return [pending(row.capability_request) ? 'capability request pending' : '', row.capability_pending === true ? 'capability approval pending' : '', pending(row.permission) ? 'permission prompt pending' : ''].filter(Boolean).join(', ')
}

function hint(row: TaskRow): string {
  if (row.status === 'settled') return `${row.id} settled: tp-agent collect ${row.id}`
  if (row.status === 'paused') {
    const why = row.result_status === null ? 'replied without a result status' : 'is paused'
    const awaits = awaiting(row)
    return `${row.id} ${why}${awaits === '' ? '' : ` (${awaits})`}: tp-agent inspect ${row.id}`
  }
  if (row.status === 'failed') return `${row.id} failed: tp-agent inspect ${row.id}`
  // tp-agent keeps returning a capability request after it is answered: only a pending
  // one is the reason the worker waits.
  const request = row.capability_request as { status?: unknown } | null
  const why = request?.status === 'pending' ? 'has a capability request' : 'is waiting at a permission prompt in its pane'
  return `${row.id} needs input (${why}): tp-agent inspect ${row.id}`
}

// What the injected index already told the main is waiting wakes nobody.
function baseline(rows: readonly unknown[]) {
  for (const row of rows) {
    const { id, status } = (row ?? {}) as Record<string, unknown>
    if (typeof id === 'string' && typeof status === 'string' && NOTABLE.has(status)) delivered.add(`${id}:${status}`)
  }
}

// The rows that entered a notable status since the last read, each marked delivered
// before anything is sent. A key leaves `delivered` when its task leaves that status,
// so a second permission prompt on the same task notifies again.
function transitions(rows: readonly TaskRow[]): TaskRow[] {
  const now = new Set(rows.map(row => `${row.id}:${row.status}`))
  for (const key of delivered) if (!now.has(key)) delivered.delete(key)
  const fresh: TaskRow[] = []
  for (const row of rows) {
    const key = `${row.id}:${row.status}`
    if (!NOTABLE.has(row.status) || delivered.has(key)) continue
    delivered.add(key)
    fresh.push(row)
  }
  return fresh
}

// Mirrors the notifier state into session state in one write. Never throws: a failed
// write is retried by the next call, and a wake never waits on it.
async function persist($: EngineInterface) {
  if (isSuperseded) return
  const next: NotifierState = { epoch, isIndexed, delivered: [...delivered].sort(), pending: [...inFlight, ...held] }
  const text = JSON.stringify(next)
  if (text === saved) return
  try {
    let isMine = true
    await update($, notifier, stored => {
      isMine = stored.epoch <= epoch
      return isMine ? next : stored
    })
    if (isMine) saved = text
    else isSuperseded = true
  } catch {
    saved = ''
  }
}

// Takes up what the module before a reload left and claims the value. Rows it had not
// had acknowledged go back into `held`, unless their task has since left that status.
async function restore($: EngineInterface) {
  try {
    const stored = await read($, notifier)
    epoch = stored.epoch + 1
    isIndexed = stored.isIndexed
    delivered = new Set(stored.delivered)
    held = stored.pending.filter(row => delivered.has(`${row.id}:${row.status}`))
  } catch {
    // The saved value could not be read, so its epoch is unknown. Claim far above any counter
    // a readable store holds, plus the time, so this module is never taken for a replaced one
    // when the store answers again, and a later module that also cannot read still claims
    // above this one. A module that can read claims above whatever is stored with + 1.
    epoch = UNREAD_EPOCH + (await $.clock.now().catch(() => 0))
    isIndexed = false
    delivered = new Set()
    held = []
  }
  inFlight = []
  await persist($)
}

// One wake at a time: `$.prompt.submit` resolves when its turn starts, and the engine
// does not dedupe, so items found meanwhile are held and ride the next submit.
async function wake($: EngineInterface) {
  if (isSubmitting || isSuperseded || held.length === 0) return
  inFlight = held
  held = []
  isSubmitting = true
  try {
    await $.prompt.submit({ text: `tmux-pi workers changed state:\n${inFlight.map(hint).join('\n')}` })
    wakeFailures = 0
  } catch {
    // Put the batch back for the next tick, a few times at most: a missed wake, never a loop.
    wakeFailures += 1
    if (wakeFailures < WAKE_TRIES && !isSuperseded) held = [...inFlight, ...held]
    else wakeFailures = 0
  } finally {
    inFlight = []
    isSubmitting = false
    await persist($)
  }
}

async function poll($: EngineInterface) {
  if (!isActive || isPolling || isSuperseded) return
  isPolling = true
  try {
    const rows = await readTasks($)
    // The gate can close while the read is out (a later session.start): do nothing then.
    if (rows === undefined || !isActive || isSuperseded) return
    const summary = summarize(rows)
    await update($, tasks, () => summary).catch(() => undefined)
    const fresh = transitions(rows)
    // A row whose task left its status while waiting for a submit is no longer news.
    held = [...held, ...fresh].filter(row => delivered.has(`${row.id}:${row.status}`))
    for (const row of fresh) $.ui.toast(hint(row))
    await persist($)
    // Not awaited: the submit resolves only when its turn starts, and polling (the band,
    // toasts, the next transitions) must go on while the main is busy.
    void wake($)
  } catch {
    // Skip the tick.
  } finally {
    isPolling = false
  }
}

function indexLine(row: unknown): string | undefined {
  if (typeof row !== 'object' || row === null) return undefined
  const { id, role, harness, status, result_status, result_path, reason } = row as Record<string, unknown>
  if (typeof id !== 'string') return undefined
  const fields = [id, role, harness, status].filter(field => typeof field === 'string' && field !== '')
  if (typeof result_status === 'string') fields.push(`result=${result_status}`)
  if (typeof result_path === 'string') fields.push(result_path)
  if (typeof reason === 'string' && reason !== '') fields.push(`(${reason})`)
  const awaits = awaiting(row as Record<string, unknown>)
  if (awaits !== '') fields.push(`[awaits: ${awaits}]`)
  return `- ${fields.join(' ')}`
}

// The protected part of the index, within `room` characters: workers that wait for the main (paused,
// replied without a result status, needs input). Their wait is a decision only the main can make and no
// other signal reaches it, so they are never dropped silently: full rows when
// they all fit, else their ids only, else as many ids as fit and a line counting the rest.
// An id in the ids-only tiers keeps a short mark of what the worker awaits.
function idMark(row: Record<string, unknown>): string {
  const awaits = awaiting(row)
  return awaits === '' ? String(row.id) : `${row.id} [${awaits}]`
}

function pausedLines(groups: readonly (readonly [string, unknown])[], room: number): { lines: string[]; rows: unknown[] } {
  const named = groups.map(([name, group]) => ({
    name,
    rows: (Array.isArray(group) ? group : []).filter(row => indexLine(row) !== undefined) as Record<string, unknown>[],
  }))
  const all = named.flatMap(group => group.rows)
  const full = named.flatMap(({ name, rows }) => [`${name}:${rows.length === 0 ? ' none' : ''}`, ...rows.map(row => indexLine(row) as string)])
  if (full.join('\n').length <= room) return { lines: full, rows: all }
  const idsHeader = (name: string) => `${name} (ids only; tp-agent inspect <id>):`
  const ids = named.map(({ name, rows }) => (rows.length === 0 ? `${name}: none` : `${idsHeader(name)} ${rows.map(idMark).join(', ')}`))
  if (ids.join('\n').length <= room) return { lines: ids, rows: all }
  const rest = (hidden: number) => `… ${hidden} more workers waiting for you not shown; run tp-agent list to see them`
  const lines: string[] = []
  const shown: unknown[] = []
  let size = rest(all.length).length
  let isFull = false
  for (const { name, rows } of named) {
    let line = idsHeader(name)
    let count = 0
    for (const row of rows) {
      const piece = `${count === 0 ? ' ' : ', '}${idMark(row)}`
      // A group's first id also pays for its header and the newline before it.
      const cost = piece.length + (count === 0 ? line.length + 1 : 0)
      if (isFull || size + cost > room) {
        isFull = true
        break
      }
      size += cost
      line += piece
      count += 1
      shown.push(row)
    }
    if (count > 0) lines.push(line)
  }
  lines.push(rest(all.length - shown.length))
  return { lines, rows: shown }
}

// The session index as the main reads it: objective, paused, active and pending (not recent),
// never longer than INDEX_CHARS.
// --no-tick: injection only reads; it never launches queued work.
// `rows` are only the rows shown in `text` (as a line, or by id): a row cut for length was
// not shown to the main, so it must stay eligible for a wake.
async function readIndex($: EngineInterface): Promise<{ text: string; rows: unknown[] } | undefined> {
  const index = await runTpAgent($, ['index', '--no-tick'])
  if (typeof index !== 'object' || index === null) return undefined
  const { objective, active, pending, paused, unstatused } = index as Record<string, unknown>
  const lines = ['tmux-pi task index (injected; run `tp-agent index` only if this is missing):']
  lines.push(`objective: ${(typeof objective === 'string' && objective.trim() !== '' ? objective.trim() : '(none set)').slice(0, 1000)}`)
  // Room kept for the active and pending headers and the omission line, so the whole text fits.
  const limit = INDEX_CHARS - 120
  // Workers that await the main come first (paused, unstatused, and needs_input: a request with no pause);
  // the other active rows and the pending ones take what room is left.
  const activeRows = Array.isArray(active) ? active : []
  const isAsking = (row: unknown) => typeof row === 'object' && row !== null && (row as { status?: unknown }).status === 'needs_input'
  const first = pausedLines(
    [['replied without a result status', unstatused], ['paused', paused], ['needs input', activeRows.filter(isAsking)]],
    limit - lines.join('\n').length - 1,
  )
  lines.push(...first.lines)
  const shown = [...first.rows]
  let size = lines.join('\n').length
  let cut = 0
  for (const [name, group] of [['active', activeRows.filter(row => !isAsking(row))], ['pending', pending]] as const) {
    const rows = Array.isArray(group) ? group : []
    lines.push(`${name}:${rows.length === 0 ? ' none' : ''}`)
    for (const row of rows) {
      const line = indexLine(row)
      if (line === undefined) continue
      if (size + line.length + 1 > limit) {
        cut += 1
        continue
      }
      size += line.length + 1
      lines.push(line)
      shown.push(row)
    }
  }
  if (cut > 0) lines.push(`… ${cut} more not shown; run tp-agent index for all of them`)
  return { text: lines.join('\n'), rows: shown }
}

async function registerCommands($: EngineInterface) {
  // The engine refuses a built-in's name (/tasks is one), and /tp is the built-in alias of
  // /teleport, so the names start with `worker`.
  await $.command.register({ name: 'workers', description: 'List tmux-pi worker tasks' })
  await $.command.register({ name: 'worker-view', description: 'Show a tmux-pi worker pane beside main', argumentHint: '<id>|-' })
  await $.command.register({ name: 'worker-collect', description: 'Collect a settled tmux-pi task', argumentHint: '<id>' })
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    isActive = await readGate($, e)
    // session.start fires again on a hot reload and may fire twice in one module: one timer,
    // and none at all once the gate is closed.
    timer?.cancel()
    timer = undefined
    if (!isActive) return next(e)

    // Each step stands alone: a refused append or registration must not stop the poll.
    // A fresh module (first start, or a hot reload) takes up the session's saved state;
    // a repeated start in the same module keeps what it has.
    if (!isLoaded) {
      await restore($)
      isLoaded = true
    }
    if (!isIndexed) {
      const index = await readIndex($)
      const appended =
        index === undefined
          ? undefined
          : await $.session
              .append({ message: { type: 'user', content: [{ type: 'text', text: index.text }] } })
              .catch(() => undefined)
      if (index !== undefined && appended?.uuid !== undefined) {
        // The baseline and "indexed" are saved together, in the one write.
        baseline(index.rows)
        isIndexed = true
        await persist($)
      }
    }
    await registerCommands($).catch(() => undefined)
    await poll($)
    timer = $.clock.every(POLL_MS, () => {
      void poll($)
    })

    return next(e)
  }).catch(($, e, next) => next(e))

  // After a compaction the index goes in as SessionStart context: an append made while
  // compacting lands before the boundary and the model never reads it.
  on('classic.SessionStart', async ($, e, next) => {
    const answer = await next(e)
    if (!isActive || e.source !== 'compact') return answer
    const index = await readIndex($)
    if (index === undefined) return answer
    return { ...answer, additionalContext: [...(answer.additionalContext ?? []), index.text] }
  }).catch(($, e, next) => next(e))

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (!isActive || e.props.hasSurvey) return next(e)
    const summary = await read($, tasks)
    if (summary === null || isQuiet(summary)) return next(e)
    const { Box, Text } = $.ui.resolve(e)

    return (
      <Box>
        <Text key="tp-band" dimColor>
          {clip(bandText(summary), e.props.bodyColumns)}
        </Text>
      </Box>
    )
  })

  on('command.run', { command: 'workers' }, async ($, e, next) => {
    if (!isActive) return next(e)
    const rows = await readTasks($)
    if (rows === undefined) return { text: 'tp-agent list failed.' }
    if (rows.length === 0) return { text: 'No tasks in this session.' }
    return { text: ['id  role  status', ...rows.map(row => `${row.id}  ${row.role ?? '-'}  ${row.status}`)].join('\n') }
  })

  on('command.run', { command: 'worker-view' }, async ($, e, next) => {
    if (!isActive) return next(e)
    const id = e.args.trim()
    if (id !== '-' && !TASK_ID.test(id)) return { text: 'Usage: /worker-view <id>|-' }
    const ran = await runTool($, 'tp-view', [id])
    if (ran === undefined) return { text: 'tp-view did not run.' }
    const out = (ran.exitCode === 0 ? ran.stdout : ran.stderr || ran.stdout).trim()
    if (out !== '') return { text: out }
    if (ran.exitCode !== 0) return { text: `tp-view ${id}: exit ${ran.exitCode}` }
    return { text: id === '-' ? 'Viewer cleared.' : `Showing ${id} in the viewer.` }
  })

  on('command.run', { command: 'worker-collect' }, async ($, e, next) => {
    if (!isActive) return next(e)
    const id = e.args.trim()
    if (!TASK_ID.test(id)) return { text: 'Usage: /worker-collect <id>' }
    const ran = await runTool($, 'tp-agent', ['collect', id])
    if (ran === undefined) return { text: 'tp-agent collect did not run.' }
    if (ran.exitCode !== 0) return { text: (ran.stderr || ran.stdout).trim() || `tp-agent collect ${id}: exit ${ran.exitCode}` }
    // tp-agent exits 0 for a task that is not settled yet: only `completed` was collected.
    let status: unknown
    try {
      status = JSON.parse(ran.stdout)?.status
    } catch {
      status = undefined
    }
    if (status === 'paused') {
      let resultStatus: unknown
      try {
        resultStatus = JSON.parse(ran.stdout)?.result_status
      } catch {
        resultStatus = undefined
      }
      const why = typeof resultStatus === 'string' ? 'is paused' : 'replied without a result status (paused)'
      return { text: `${id} ${why}; nothing was collected.` }
    }
    if (status !== 'completed') return { text: `${id} is ${typeof status === 'string' ? status : 'in an unknown state'}; nothing was collected.` }
    return { text: `Collected ${id}.`, context: [`tp-agent collect ${id}:\n${ran.stdout}`] }
  })
}
