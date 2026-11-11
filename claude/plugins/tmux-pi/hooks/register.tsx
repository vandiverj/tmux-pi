// Main-agent module for the tmux-pi plugin. The classic command hooks in hooks.json
// (report.sh, sensitive-read.sh) stay the cross-harness contract; this module only
// adds main-side conveniences on top of bin/tp-agent and never replaces them.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, SessionStartInput } from 'claude-code'

import type { AnswerRoute, NotifierState, OtherRow, RequestEvent, TaskRow, TaskSummary } from '../types'

const TP_AGENT_TIMEOUT_MS = 20000
const POLL_MS = 5000
const INDEX_CHARS = 6000
const NOTABLE = new Set(['needs_input', 'settled', 'paused', 'failed'])
const WAKE_TRIES = 3
// After a failed submit the rows stay held and the next try waits this long, doubling per failure in a row (to 8x): a main that
// cannot take a turn is not asked every poll, and is asked again for as long as the row is still waiting.
const WAKE_BACKOFF_MS = 15000
// Failed reads of `tp-agent list` in a row before the band says the notices are paused.
const READ_FAILURES_PAUSED = 2
const OTHER_IDS_SHOWN = 5
const UNREAD_EPOCH = 1e15
// tp-agent's own id pattern (IDENT in bin/tp-agent): no leading dash, so never an option.
const TASK_ID = /^[A-Za-z0-9][A-Za-z0-9._-]*$/
// tp-agent's MAIN_ID pattern.
const MAIN_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/
// lib/hub_records.py REQUEST_ID: req- and a ULID.
const REQUEST_ID = /^req-[0-9A-HJKMNP-TV-Z]{26}$/

// The one gate. Workers, headless runs, legacy mains and tp-claude-bootstrap load this
// plugin too; every hook returns next(e) untouched while this is false.
let isActive = false

// Notifier state.
// - `delivered`: `id:status` keys whose submit SUCCEEDED (or that the injected index showed), dropped when the task leaves that
//   status, so a second permission prompt on the same task notifies again. A key is never in it before its submit resolved.
// - `held`: rows found but not yet delivered, kept and retried (WAKE_BACKOFF_MS) for as long as the row is still in that status;
//   `inFlight`: the batch of a submit that has not resolved (it resolves when its turn starts, which waits for the main to be idle).
// The rows the main actually read in the index appended at start are the baseline and wake
// nobody. Anything it was not shown (no index, a refused append, rows cut for length)
// stays eligible and is announced by the first poll.
// All of it is mirrored into ONE session-state value, written in one piece, because a hot
// reload resets module variables: the next module takes it up, so a reload neither repeats
// an acknowledged wake nor loses one still waiting. Each fresh module claims the value
// with a higher `epoch`; a module that finds a higher one stored has been replaced and
// goes quiet without overwriting its successor. An unresolved submit of the old module
// may still land, so each reload can repeat at most the wake that was in flight then.
// - `cursors`: per task (`<state_dir>#<id>`), the count of its request transitions (request_log seq, a count of request events,
//   not a byte offset) already queued as notices. Taken by the startup snapshot, just before the startup index is read, so a
//   transition after it is delivered once and none before it is replayed; undefined until then (the first good poll takes it if
//   the startup read failed). A request notice queued past the cursor stays in `held` until a submit that rendered it succeeded:
//   it is never dropped because its row changed. Request keys (`req:<id>:open`, `req:<id>:resolved`) are never in `delivered`.
let delivered = new Set<string>()
let held: TaskRow[] = []
let inFlight: TaskRow[] = []
let cursors: Map<string, number> | undefined
let wakeFailures = 0
let retryAt = 0
let isStuck = false
let readFailures = 0
let readFailure: string | undefined
let hasToastedPause = false
let other: OtherRow[] = []
let seenOther = new Set<string>()
let lastSummary: TaskSummary | undefined
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

// The identity tp-agent records on every task this main dispatches (main_id in bin/tp-agent): the launcher's TP_MAIN_ID,
// else the Claude session id. Read at each poll, because the main exports a different TP_STATE_DIR per command and this
// is the one thing that follows its tasks into whichever state directory they live in.
async function readMainId($: EngineInterface): Promise<string | undefined> {
  const id = (await $.env.get('TP_MAIN_ID')) || (await $.env.get('CLAUDE_CODE_SESSION_ID'))
  return id !== undefined && MAIN_ID.test(id) ? id : undefined
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

// What one read of `tp-agent list` came to: the rows (and the rows of this session another main owns), or why it failed.
type Read = { rows: TaskRow[]; other: OtherRow[] } | { failure: string }

// `tp-agent list` is read-only: no tick, no index write. `--main ID` adds this main's tasks in the other sessions of the
// workspace and leaves out another main's tasks in this one, reporting those left out beside the rows (`other_main`); a main
// with no identity lists its own session as before. A failure is never silent: poll counts it and the band says so.
async function readTasks($: EngineInterface): Promise<Read> {
  const main = await readMainId($)
  const ran = await runTool($, 'tp-agent', main === undefined ? ['list'] : ['list', '--main', main])
  if (ran === undefined) return { failure: 'timed out' }
  if (ran.exitCode !== 0) return { failure: `exit ${ran.exitCode}` }
  let parsed: any
  try {
    parsed = JSON.parse(ran.stdout)
  } catch {
    return { failure: 'bad output' }
  }
  // An array is an older tp-agent (or no --main); an object carries the rows and what was left out.
  const rows = Array.isArray(parsed) ? parsed : Array.isArray(parsed?.rows) ? parsed.rows : undefined
  if (rows === undefined) return { failure: 'bad output' }
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
      reason: typeof row.reason === 'string' ? row.reason : null,
      result_status: typeof row.result_status === 'string' ? row.result_status : null,
      ...(typeof row.state_dir === 'string' && row.state_dir.startsWith('/') ? { state_dir: row.state_dir } : {}),
      ...(typeof row.failing_tests === 'object' && row.failing_tests !== null ? { failing_tests: true as const } : {}),
      ...requestFields(row),
    })
  }
  const left = Array.isArray(parsed?.other_main?.rows) ? parsed.other_main.rows : []
  const others: OtherRow[] = []
  for (const row of left) {
    if (typeof row?.id !== 'string') continue
    others.push({ id: row.id, status: typeof row.status === 'string' ? row.status : '?', main: typeof row.main === 'string' ? row.main : '?' })
  }
  return { rows: found, other: others }
}

// ---- notice rules: begin ----
// From here to "notice rules: end" is pure (no `$`, no JSX): what the main is told, and when a notice counts as delivered.
// tests/test_hub_records.py runs this very text under node, fed by the real Python request writers.

// A row's request transition log and answer route (bin/tp-agent record `request_log`, `answer_route`; lib/hub_records.py
// request_log): only well-formed entries, and no key at all for a task that has none, so its row is what it was before.
// The row's `requests` (row_requests) is a display window and is never read here: nothing that delivers notices reads it.
function requestFields(row: unknown): { requestLog?: RequestEvent[]; answerRoute?: AnswerRoute } {
  const { request_log: log, answer_route: route } = (row ?? {}) as Record<string, unknown>
  const events: RequestEvent[] = []
  for (const item of Array.isArray(log) ? log : []) {
    const { seq, id, change, kind, summary, how } = (item ?? {}) as Record<string, unknown>
    if (typeof seq !== 'number' || !Number.isInteger(seq) || seq < 1 || typeof id !== 'string' || !REQUEST_ID.test(id)) continue
    if (change !== 'opened' && change !== 'closed') continue
    events.push({
      seq,
      id,
      change,
      kind: typeof kind === 'string' ? kind : '',
      summary: typeof summary === 'string' ? summary : '',
      ...(typeof how === 'string' && how !== '' ? { how } : {}),
    })
  }
  const { by, why } = (typeof route === 'object' && route !== null ? route : {}) as Record<string, unknown>
  const isRoute = by === 'message' || by === 'retry' || by === 'none'
  return {
    ...(events.length === 0 ? {} : { requestLog: events }),
    ...(isRoute ? { answerRoute: { by, ...(typeof why === 'string' && why !== '' ? { why } : {}) } } : {}),
  }
}

function summarize(rows: readonly TaskRow[]): TaskSummary {
  const summary: TaskSummary = { active: 0, toCollect: 0, paused: 0, pausedWithStatus: 0, failed: 0, refused: 0, needsInput: [], notes: [] }
  for (const row of rows) {
    if (row.status === 'needs_input') summary.needsInput.push(row.id)
    else if (row.status === 'settled') summary.toCollect += 1
    else if (row.status === 'paused') {
      if (row.result_status === null) summary.paused += 1
      else summary.pausedWithStatus += 1
    }
    else if (row.status === 'failed') summary.failed += 1
    else if (isRefused(row)) summary.refused += 1  // never starts, so it is not active work
    else if (row.status === 'queued' || row.status === 'starting' || row.status === 'working') summary.active += 1
  }
  return summary
}

function isQuiet(summary: TaskSummary): boolean {
  return summary.active + summary.toCollect + summary.paused + summary.pausedWithStatus + summary.failed + summary.refused + summary.needsInput.length === 0
}

function bandText(summary: TaskSummary): string {
  const parts = [`${summary.active} active`, `${summary.toCollect} to collect`, `${summary.failed} failed`]
  if (summary.paused > 0) parts.push(`${summary.paused} replied without a result status`)
  if (summary.pausedWithStatus > 0) parts.push(`${summary.pausedWithStatus} paused`)
  if (summary.refused > 0) parts.push(`${summary.refused} refused`)
  if (summary.needsInput.length > 0) parts.push(`needs input: ${summary.needsInput.join(', ')}`)
  return `workers: ${parts.join(' · ')}`
}

function clip(text: string, columns: number): string {
  if (columns <= 0 || text.length <= columns) return text
  return columns === 1 ? '…' : `${text.slice(0, columns - 1)}…`
}

// A queue entry tp-agent could not start: `tp-agent list` gives it status queued and the reason "start refused: …"
// (bin/tp-agent queue_entry), which tests/test_orchestration.py pins.
function isRefused(row: unknown): boolean {
  const { status, reason } = (row ?? {}) as Record<string, unknown>
  return status === 'queued' && typeof reason === 'string' && reason.startsWith('start refused: ')
}

// What a worker that waits for the main is waiting on, beyond its state: a pending capability request
// (capability-request.json), a capability awaiting human approval (pending-capability.json, exported
// as capability_pending) or a pending permission prompt. tp-agent keeps returning an answered request, so only pending counts.
function awaiting(row: { permission?: unknown; capability_request?: unknown; capability_pending?: unknown }): string {
  const pending = (value: unknown) => typeof value === 'object' && value !== null && (value as { status?: unknown }).status === 'pending'
  return [pending(row.capability_request) ? 'capability request pending' : '', row.capability_pending === true ? 'capability approval pending' : '', pending(row.permission) ? 'permission prompt pending' : ''].filter(Boolean).join(', ')
}

// A task in another session's state directory is reached with that directory exported, as the main dispatched it.
function agent(row: TaskRow, verb: string): string {
  return `${row.state_dir === undefined ? '' : `TP_STATE_DIR='${row.state_dir.replace(/'/g, `'\\''`)}' `}tp-agent ${verb} ${row.id}`
}

function hint(row: TaskRow): string {
  const notice = row.notice
  if (notice !== undefined) {
    const about = `${notice.kind} request ${notice.id}${notice.summary === '' ? '' : ` (${notice.summary})`}`
    if (notice.state === 'resolved') return `${row.id}: ${about} is closed${notice.how === undefined ? '' : ` (${notice.how})`}`
    // An opening told after its request already closed (both happened between two polls): said, with nothing to answer.
    if (notice.isClosed === true) return `${row.id}: ${about} was opened`
    // The answer route is tp-agent's own check (bin/tp-agent answer_route, the one `tp-agent message` makes): a message is
    // suggested only while the worker can take one. A finished task's request is said to be left open, with the command that
    // works on that task (retry), or none.
    const why = row.answerRoute?.why === undefined ? '' : ` (${row.answerRoute.why})`
    if (row.answerRoute?.by === 'retry') {
      return `${row.id}: ${about} is left open by a finished task${why}: ${agent(row, 'retry')} --reason "..." starts a new attempt, or ${agent(row, 'inspect')}`
    }
    if (row.answerRoute?.by === 'none') return `${row.id}: ${about} is open, but no command can answer it now${why}: ${agent(row, 'inspect')}`
    // tp-agent message takes its text only as --text (bin/tp-agent, the `message` parser).
    const answer = notice.kind === 'permission' ? 'answer it in its pane' : `answer with ${agent(row, 'message')} --resolves ${notice.id} --text "..."`
    return `${row.id} has an open ${about}: ${answer}, or ${agent(row, 'inspect')}`
  }
  if (isRefused(row)) return `${row.id} will not start (${row.reason}): fix or remove the queue file, or ${agent(row, 'stop')}`
  if (row.status === 'settled') {
    return row.failing_tests === true
      ? `${row.id} settled and reports failing tests it does not own: ${agent(row, 'collect')}, then tp-agent dispatch --investigate-failure ${row.id}`
      : `${row.id} settled: ${agent(row, 'collect')}`
  }
  if (row.status === 'paused') {
    const why = row.result_status === null ? 'replied without a result status' : 'is paused'
    const awaits = awaiting(row)
    return `${row.id} ${why}${awaits === '' ? '' : ` (${awaits})`}: ${agent(row, 'inspect')}`
  }
  if (row.status === 'failed') return `${row.id} failed: ${agent(row, 'inspect')}`
  // tp-agent keeps returning a capability request after it is answered: only a pending
  // one is the reason the worker waits.
  const request = row.capability_request as { status?: unknown } | null
  const why = request?.status === 'pending' ? 'has a capability request' : 'is waiting at a permission prompt in its pane'
  return `${row.id} needs input (${why}): ${agent(row, 'inspect')}`
}

// The key a row is announced under, or undefined when it is not news: `id:status` for a notable status, and
// `id:refused` for a queue entry tp-agent will not start (it keeps status queued, so the status alone cannot say).
// A notice row made for one request transition (requestNotice) is keyed on the request id, `req:<id>:open` or `req:<id>:resolved`.
function noticeKey(row: unknown): string | undefined {
  const { id, status, notice } = (row ?? {}) as Record<string, unknown>
  if (typeof notice === 'object' && notice !== null) {
    const { id: req, state } = notice as Record<string, unknown>
    return typeof req === 'string' && (state === 'open' || state === 'resolved') ? `req:${req}:${state}` : undefined
  }
  if (typeof id !== 'string' || typeof status !== 'string') return undefined
  if (NOTABLE.has(status)) return `${id}:${status}`
  return isRefused(row) ? `${id}:refused` : undefined
}

// What the injected index showed the main wakes nobody: the status of each row it listed. A request is never marked here: the
// index does not render request ids, and delivered means rendered; the startup snapshot queues every request still open.
function baseline(rows: readonly unknown[]) {
  for (const row of rows) {
    const key = noticeKey(row)
    if (key !== undefined && !key.startsWith('req:')) delivered.add(key)
  }
}

// The task a cursor counts for: the same id may live in two state directories.
function taskKey(row: { id: string; state_dir?: string }): string {
  return `${row.state_dir ?? ''}#${row.id}`
}

function lastSeq(log: readonly RequestEvent[] | undefined): number {
  return (log ?? []).reduce((last, event) => Math.max(last, event.seq), 0)
}

// The requests a log leaves open: opened and not closed since.
function openIds(log: readonly RequestEvent[] | undefined): Set<string> {
  const open = new Set<string>()
  for (const event of log ?? []) {
    if (event.change === 'opened') open.add(event.id)
    else open.delete(event.id)
  }
  return open
}

// A row as a notice carries it: never its log (a held row is persisted, and its log is read afresh each poll).
function plainRow(row: TaskRow): TaskRow {
  const { requestLog, notice, ...rest } = row
  return rest
}

// The notice row for one transition: an opening (`req:<id>:open`, isClosed when the same log already closes it) or a closing
// (`req:<id>:resolved`, resolved, superseded or withdrawn, with how).
function requestNotice(row: TaskRow, event: RequestEvent): TaskRow {
  const about = { id: event.id, kind: event.kind, summary: event.summary }
  const isClosed = event.change === 'opened' && (row.requestLog ?? []).some(each => each.id === event.id && each.change === 'closed')
  const notice = event.change === 'opened'
    ? { ...about, state: 'open' as const, ...(isClosed ? { isClosed: true } : {}) }
    : { ...about, state: 'resolved' as const, how: event.how ?? 'closed' }
  return { ...plainRow(row), notice }
}

// The startup snapshot: each task's cursor is its last transition now, and every request still open is queued once (the index
// does not render request ids). Closed history is not replayed. Taken just before the startup index is read.
function snapshot(rows: readonly TaskRow[]): TaskRow[] {
  cursors = new Map()
  const out: TaskRow[] = []
  for (const row of rows) {
    const last = lastSeq(row.requestLog)
    if (last > 0) cursors.set(taskKey(row), last)
    const open = openIds(row.requestLog)
    for (const event of row.requestLog ?? []) if (event.change === 'opened' && open.has(event.id)) out.push(requestNotice(row, event))
  }
  return out
}

// The request transitions past each task's cursor, in the order the log has them, and the cursor moved to the last. A task
// not seen before starts at 0. A log that shrank sets the cursor to its last transition and replays nothing; a log that reads
// empty leaves the cursor alone (a failed read of events.jsonl must not replay the task's whole history next poll).
function logged(rows: readonly TaskRow[]): TaskRow[] {
  if (cursors === undefined) return snapshot(rows)
  const out: TaskRow[] = []
  for (const row of rows) {
    const key = taskKey(row)
    const last = lastSeq(row.requestLog)
    const cursor = cursors.get(key) ?? 0
    if (last === 0 || last === cursor) continue
    for (const event of row.requestLog ?? []) if (event.seq > cursor) out.push(requestNotice(row, event))
    cursors.set(key, last)
  }
  return out
}

// The notices that are new since the last read and are not already waiting in `held` or `inFlight` (`fresh`), the status
// notices that are current, by key, and the rows read, by task. Request notices come from the log (logged): each transition
// once, past the cursor. A status key is NOT marked delivered here: only a submit that succeeded does that (acknowledge),
// and it leaves `delivered` when its task leaves that status, so a second permission prompt on the same task notifies again.
// The row's own needs_input/paused notice is left out while its log has a request open: the request says more.
function transitions(rows: readonly TaskRow[]): { fresh: TaskRow[]; current: Map<string, TaskRow>; listed: Map<string, TaskRow> } {
  const told = logged(rows)
  const current = new Map<string, TaskRow>()
  for (const row of rows) {
    if (openIds(row.requestLog).size > 0 && (row.status === 'needs_input' || row.status === 'paused')) continue
    const plain = plainRow(row)
    const key = noticeKey(plain)
    if (key !== undefined) current.set(key, plain)
  }
  for (const key of delivered) if (!current.has(key)) delivered.delete(key)
  const waiting = new Set([...held, ...inFlight].map(noticeKey))
  const fresh: TaskRow[] = []
  for (const row of [...told, ...current.values()]) {
    const key = noticeKey(row)
    if (key === undefined || delivered.has(key) || waiting.has(key)) continue
    waiting.add(key)
    fresh.push(row)
  }
  return { fresh, current, listed: new Map(rows.map(row => [taskKey(row), row])) }
}

// One good read: the fresh notices queue behind those already held. A held request notice is kept until rendered, whatever its
// row does since, and takes its row's current fields (an opening whose request has since closed says so; the answer route
// follows the task). A held status notice that is no longer current is dropped (its task left that status).
function announce(rows: readonly TaskRow[]): TaskRow[] {
  const { fresh, current, listed } = transitions(rows)
  held = [...held, ...fresh].flatMap(row => {
    if (row.notice !== undefined) {
      const now = listed.get(taskKey(row))
      if (now === undefined) return [row]
      const isClosed = row.notice.state === 'open' && (row.notice.isClosed === true || (now.requestLog ?? []).some(each => each.id === row.notice?.id && each.change === 'closed'))
      return [{ ...plainRow(now), notice: { ...row.notice, ...(isClosed ? { isClosed: true } : {}) } }]
    }
    const now = current.get(noticeKey(row) ?? '')
    return now === undefined ? [] : [now]
  })
  return fresh
}

// The text one submit gives the main: one hint per notice row.
function wakeText(batch: readonly TaskRow[]): string {
  return `tmux-pi workers changed state:\n${batch.map(hint).join('\n')}`
}

// A submit of `batch` succeeded: each row in it was rendered by hint. A status row marks its status key delivered; a request
// notice needs no mark: its transition is behind the cursor, and leaving `held` (rendered) is its delivery.
function acknowledge(batch: readonly TaskRow[]) {
  for (const row of batch) {
    const key = noticeKey(row)
    if (key !== undefined && !key.startsWith('req:')) delivered.add(key)
  }
}

// ---- notice rules: end ----

// The lines under the counts in the band: what the main would otherwise not learn. None while all is well.
function bandNotes(): string[] {
  const notes: string[] = []
  if (readFailures >= READ_FAILURES_PAUSED) notes.push(`worker notices paused: tp-agent list ${readFailure}`)
  if (isStuck) {
    const ids = [...inFlight, ...held].map(row => row.id)
    notes.push(`main could not be woken: ${ids.length} worker notice${ids.length === 1 ? '' : 's'} held (${ids.join(', ')}); retrying`)
  }
  if (other.length > 0) {
    const ids = other.slice(0, OTHER_IDS_SHOWN).map(row => `${row.id} (${row.main})`).join(', ')
    notes.push(`${other.length} worker${other.length === 1 ? ' in this session belongs' : 's in this session belong'} to another main: ${ids}${other.length > OTHER_IDS_SHOWN ? ', …' : ''}; tp-agent list`)
  }
  return notes
}

// The band's value: the counts of the last good read and the notes now. Never throws.
async function publish($: EngineInterface) {
  const summary: TaskSummary = { ...(lastSummary ?? summarize([])), notes: bandNotes() }
  await update($, tasks, () => summary).catch(() => undefined)
}

// Mirrors the notifier state into session state in one write. Never throws: a failed
// write is retried by the next call, and a wake never waits on it.
async function persist($: EngineInterface) {
  if (isSuperseded) return
  const next: NotifierState = {
    epoch,
    isIndexed,
    delivered: [...delivered].sort(),
    pending: [...inFlight, ...held],
    ...(cursors === undefined ? {} : { cursors: Object.fromEntries([...cursors].sort(([a], [b]) => a.localeCompare(b))) }),
  }
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
    // Rows its submit had not acknowledged; poll drops those whose task has left that status.
    held = stored.pending
    // The cursors are saved in the same write as `pending`, so a reload neither repeats a queued transition nor loses one. A
    // value with none (a module before the startup snapshot, or one from before cursors) snapshots again at this start.
    const kept = typeof stored.cursors === 'object' && stored.cursors !== null ? Object.entries(stored.cursors) : undefined
    cursors = kept === undefined ? undefined : new Map(kept.filter(([, seq]) => typeof seq === 'number' && Number.isInteger(seq) && seq >= 0))
  } catch {
    // The saved value could not be read, so its epoch is unknown. Claim far above any counter
    // a readable store holds, plus the time, so this module is never taken for a replaced one
    // when the store answers again, and a later module that also cannot read still claims
    // above this one. A module that can read claims above whatever is stored with + 1.
    epoch = UNREAD_EPOCH + (await $.clock.now().catch(() => 0))
    isIndexed = false
    delivered = new Set()
    held = []
    cursors = undefined
  }
  inFlight = []
  await persist($)
}

// Takes the startup snapshot from one read of `tp-agent list`: the cursors, and the requests still open queued (and toasted)
// as the first poll would. A failed read leaves `cursors` undefined, and the first good poll takes the snapshot instead (a
// transition between this start and that poll is then counted as before the snapshot: the known limit).
async function startSnapshot($: EngineInterface) {
  const read = await readTasks($)
  if ('failure' in read || !isActive || isSuperseded || cursors !== undefined) return
  const waiting = new Set([...held, ...inFlight].map(noticeKey))
  const fresh = snapshot(read.rows).filter(row => !waiting.has(noticeKey(row)))
  held = [...held, ...fresh]
  for (const row of fresh) $.ui.toast(hint(row))
  await persist($)
}

async function nowMs($: EngineInterface): Promise<number> {
  return $.clock.now().catch(() => 0)
}

// One wake at a time: `$.prompt.submit` resolves when its turn starts, and the engine
// does not dedupe, so items found meanwhile are held and ride the next submit. A row is marked
// delivered only when its submit succeeded. A failed one keeps its rows held and is tried again
// after WAKE_BACKOFF_MS (doubling), for as long as the row is still in that status; after
// WAKE_TRIES failures in a row it says so once (toast and band) and goes on trying.
async function wake($: EngineInterface) {
  if (isSubmitting || isSuperseded || held.length === 0) return
  if ((await nowMs($)) < retryAt) return
  inFlight = held
  held = []
  isSubmitting = true
  try {
    await $.prompt.submit({ text: wakeText(inFlight) })
    acknowledge(inFlight)
    wakeFailures = 0
    isStuck = false
  } catch {
    wakeFailures += 1
    if (!isSuperseded) held = [...inFlight, ...held]
    retryAt = (await nowMs($)) + WAKE_BACKOFF_MS * 2 ** Math.min(wakeFailures - 1, 3)
    if (wakeFailures === WAKE_TRIES && !isSuperseded) {
      isStuck = true
      $.ui.toast(`tmux-pi could not wake main with ${held.length} worker notice${held.length === 1 ? '' : 's'} (${held.map(row => row.id).join(', ')}); they stay held and are retried`)
    }
  } finally {
    inFlight = []
    isSubmitting = false
    await persist($)
    await publish($)
  }
}

// A read of `tp-agent list` that failed: counted, never silent. From the second in a row the band says why and one toast is shown;
// the next good read clears it. Polling goes on.
async function readFailed($: EngineInterface, why: string) {
  readFailures += 1
  readFailure = why
  if (readFailures >= READ_FAILURES_PAUSED && !hasToastedPause) {
    hasToastedPause = true
    $.ui.toast(`worker notices paused: tp-agent list ${why}`)
  }
  await publish($)
}

async function poll($: EngineInterface) {
  if (!isActive || isPolling || isSuperseded) return
  isPolling = true
  try {
    const read = await readTasks($)
    // The gate can close while the read is out (a later session.start): do nothing then.
    if (!isActive || isSuperseded) return
    if ('failure' in read) return await readFailed($, read.failure)
    const { rows } = read
    readFailures = 0
    readFailure = undefined
    hasToastedPause = false
    other = read.other
    // Another main's rows are shown, never woken on: a live second main may own them. One toast per newly seen row.
    const now = new Set(other.map(row => `${row.id}:${row.main}`))
    for (const row of other) {
      if (!seenOther.has(`${row.id}:${row.main}`)) $.ui.toast(`${row.id} (${row.status}) belongs to another main (${row.main}); not woken: tp-agent list`)
    }
    seenOther = now
    lastSummary = summarize(rows)
    // A row whose task left its status while waiting for a submit is no longer news (announce drops it from held).
    const fresh = announce(rows)
    // Nothing is waiting any more (the rows left that status): the warning about a main that could not be woken is over.
    if (held.length === 0 && inFlight.length === 0) {
      isStuck = false
      wakeFailures = 0
    }
    await publish($)
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
  const { id, role, harness, status, result_status, result_path, reason, verdict, review_complete, review_unusable, family_missing } = row as Record<string, unknown>
  if (typeof id !== 'string') return undefined
  const fields = [id, role, harness, status].filter(field => typeof field === 'string' && field !== '')
  if (typeof result_status === 'string') fields.push(`result=${result_status}`)
  // a review's stated verdict never reads as an approval unless its Coverage section was there (tp-agent: review_complete),
  // and a REQUEST_CHANGES with no Closes by: code finding (review_unusable) is neither a rejection nor an approval;
  // a round 3+ REQUEST_CHANGES with no Family: line (family_missing) is flagged beside the verdict, as lib/index_text.py does
  if (typeof verdict === 'string') {
    const note = review_complete !== true ? ' [review incomplete: not an approval]'
      : typeof review_unusable === 'string' && review_unusable !== '' ? ' [review unusable: neither a rejection nor an approval]' : ''
    const missing = typeof family_missing === 'string' && family_missing !== '' ? ' [no Family: line on a round 3+ REQUEST_CHANGES]' : ''
    fields.push(`verdict=${verdict}${note}${missing}`)
  }
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

// The queue entries tp-agent refused to start, after the rows above, in the room those left (`room` counts the newline
// before each line). A refused entry has no pane, no state and no worker, never starts, and holds back every --after
// dependent until `tp-agent stop <id>` removes it, so the main has to act on it; but it takes only what the workers
// that wait for the main left over, never an id they would have had without it. Full rows when they fit, else ids
// only, else as many ids as fit. Rows not shown are counted on a short line that may use the index's reserve beyond
// `room`, and they stay eligible for a wake (the poll announces what the index did not show).
function refusedLines(rows: Record<string, unknown>[], room: number): { lines: string[]; rows: unknown[] } {
  if (rows.length === 0) return { lines: [], rows: [] }
  const name = 'queue entries that will not start'
  const fits = (lines: readonly string[]) => lines.join('\n').length + 1 <= room
  const full = [`${name}:`, ...rows.map(row => indexLine(row) as string)]
  if (fits(full)) return { lines: full, rows }
  const header = `${name} (ids only; tp-agent stop <id> removes one):`
  const ids = [`${header} ${rows.map(row => row.id).join(', ')}`]
  if (fits(ids)) return { lines: ids, rows }
  const shown: Record<string, unknown>[] = []
  for (const row of rows) {
    if (!fits([`${header} ${[...shown, row].map(each => each.id).join(', ')}`])) break
    shown.push(row)
  }
  const lines = shown.length === 0 ? [] : [`${header} ${shown.map(row => row.id).join(', ')}`]
  lines.push(`… ${rows.length - shown.length} refused not shown`)
  return { lines, rows: shown }
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

// ---- the durable `work` block of the index (tp-agent work ...) ----
// Per open decision the index carries only id, workspace, priority and blocking: never the question text. The injected text
// shows the counts, the first ids that fit `chars`, and the command that lists them all, so it cannot grow with the backlog.
const WORK_COMMAND = 'tp-agent work decisions'
const PRIORITY_RANK: Record<string, number> = { high: 0, normal: 1, low: 2 }
const DEFAULT_DECISION_CHARS = 1200

// The first `size` UTF-16 code units, except that a surrogate pair the cut would split is left out whole (lib/index_text.py _head does the same).
function cutUnits(text: string, size: number): string {
  const high = text.charCodeAt(size - 1)
  const low = text.charCodeAt(size)
  return high >= 0xd800 && high <= 0xdbff && low >= 0xdc00 && low <= 0xdfff ? text.slice(0, size - 1) : text.slice(0, size)
}

// The C0 controls other than newline and tab dropped, then every lone surrogate replaced by U+FFFD (lib/index_text.py _clean does the
// same, in the same order). Run on the finished text: it never lengthens it, so the budget still holds.
function wellFormed(text: string): string {
  return text
    .replace(/[\x00-\x08\x0b-\x1f]/g, '')
    .replace(/[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/g, '\ufffd')
}

function plain(text: unknown, size: number): string {
  const one = (typeof text === 'string' ? text : '').replace(/\s+/g, ' ').trim()
  return one.length <= size ? one : `${cutUnits(one, size - 1)}…`
}

function decisionTrailer(more: number, moreBlocking: number): string {
  return more > 0 ? `+${more} more (${moreBlocking} blocking): ${WORK_COMMAND}` : `${WORK_COMMAND} (every question and detail)`
}

// Same text as lib/work_index.py decision_summary: header, then blocking-first ids (one line each) while they fit, then the trailer.
// The trailer is reserved first. `chars` of 0 gives the minimum: the header and the trailer.
function decisionSummary(decisions: unknown, chars: number): string {
  if (typeof decisions !== 'object' || decisions === null) return ''
  const d = decisions as { open?: unknown; blocking?: unknown; items?: unknown }
  const items = (Array.isArray(d.items) ? d.items : []).filter(
    (item): item is { id: string; priority?: unknown; blocking?: unknown } =>
      typeof item === 'object' && item !== null && typeof (item as { id?: unknown }).id === 'string',
  )
  const opened = typeof d.open === 'number' ? d.open : items.length
  if (opened <= 0) return ''
  const blocking = typeof d.blocking === 'number' ? d.blocking : items.filter(item => item.blocking === true).length
  const rank = (item: { priority?: unknown }) => PRIORITY_RANK[String(item.priority)] ?? 9
  const ordered = [...items].sort((a, b) => Number(b.blocking === true) - Number(a.blocking === true) || rank(a) - rank(b))
  const header = `decisions: ${opened} open (${blocking} blocking)`
  let size = header.length + 1 + Math.max(decisionTrailer(opened, blocking).length, decisionTrailer(0, 0).length)
  const lines: string[] = []
  let shownBlocking = 0
  for (const item of ordered) {
    const line = `- ${plain(item.id, 80)} [${plain(item.priority, 10)}]${item.blocking === true ? ' blocking' : ''}`
    if (chars <= 0 || size + line.length + 1 > chars) break
    size += line.length + 1
    lines.push(line)
    shownBlocking += item.blocking === true ? 1 : 0
  }
  return [header, ...lines, decisionTrailer(opened - lines.length, blocking - shownBlocking)].join('\n')
}

function decisionChars(work: Record<string, unknown>): number {
  const chars = (work.decisions as { chars?: unknown } | undefined)?.chars
  return typeof chars === 'number' && chars > 0 ? chars : DEFAULT_DECISION_CHARS
}

// What the decisions block costs at least, so the protected tiers leave it room.
function workReserve(index: Record<string, unknown>): number {
  const work = index.work
  if (typeof work !== 'object' || work === null) return 0
  const text = decisionSummary((work as Record<string, unknown>).decisions, 0)
  return text === '' ? 0 : text.length + 1
}

function workRow(row: unknown): string | undefined {
  if (typeof row !== 'object' || row === null) return undefined
  const r = row as Record<string, unknown>
  if (typeof r.id !== 'string') return undefined
  // An attached workspace whose file cannot be used is still named, with why (the index carries its health).
  if (typeof r.health === 'string' && r.health !== '' && r.health !== 'ok') return `- ${plain(r.id, 60)} (${plain(r.health, 60)})`
  const status = typeof r.status === 'string' && r.status !== '' && r.status !== 'active' ? ` [${plain(r.status, 12)}]` : ''
  const next = plain(r.next, 100)
  const sessions = (Array.isArray(r.sessions) ? r.sessions : []).filter((x): x is string => typeof x === 'string')
  return (
    `- ${plain(r.id, 60)} "${plain(r.title, 80)}"${status}` +
    (next === '' ? '' : ` — next: ${next}`) +
    (sessions.length === 0 ? '' : ` (${sessions.slice(-3).map(x => plain(x, 40)).join(', ')})`)
  )
}

function noteList(label: string, notes: unknown): string | undefined {
  const rows = (Array.isArray(notes) ? notes : []).filter(
    (n): n is { id: string; title?: unknown } => typeof n === 'object' && n !== null && typeof (n as { id?: unknown }).id === 'string',
  )
  if (rows.length === 0) return undefined
  return `${label}: ${rows.map(n => `${plain(n.id, 60)} "${plain(n.title, 80)}"`).join('; ')}`
}

// The `work` lines, most useful first, never more than `room` characters (each line counts its newline). Every line, the error
// line and the decisions header and trailer included, goes through the one `append` below, which adds it only if it fits;
// the optional groups also leave the decisions minimum its room. No block and no error: nothing, so the text is unchanged.
function workLines(index: Record<string, unknown>, room: number): string[] {
  const lines: string[] = []
  let size = 0
  // A lead line and its rows: the rows that fit with `after` characters kept free, and none of it if no row fits (never a lead alone).
  const append = (lead: string, rest: string[], after: number) => {
    let cost = lead.length + 1
    if (size + cost + after > room) return
    const fit = rest.filter(line => {
      if (size + cost + line.length + 1 + after > room) return false
      cost += line.length + 1
      return true
    })
    if (rest.length > 0 && fit.length === 0) return
    size += cost
    lines.push(lead, ...fit)
  }
  const error = index.work_error
  const work = index.work
  if (typeof work !== 'object' || work === null) {
    if (typeof error === 'string' && error !== '') append(`work context not read: ${plain(error, 160)}`, [], 0)
    return lines
  }
  const w = work as Record<string, unknown>
  const reserve = workReserve(index)
  const rows = (group: unknown) => (Array.isArray(group) ? group : []).map(workRow).filter((x): x is string => x !== undefined)
  const attached = rows(w.attached)
  const candidates = rows(w.candidates)
  const others = typeof w.others === 'number' && w.others > 0 ? w.others : 0
  const notes = (typeof w.notes === 'object' && w.notes !== null ? w.notes : {}) as Record<string, unknown>
  if (attached.length > 0) append('work attached to this session (tp-agent work show ID):', attached, reserve)
  // Checkpoint and recovery (Phase C), only for an attached workspace: the index carries the exact sentences, read-only.
  const checkpoint = typeof w.checkpoint === 'object' && w.checkpoint !== null ? (w.checkpoint as Record<string, unknown>) : undefined
  const recovery = typeof w.recovery === 'object' && w.recovery !== null ? (w.recovery as Record<string, unknown>) : undefined
  if (recovery !== undefined && recovery.pending === true) {
    append(
      `recovery pending (${recovery.reason === 'attach' ? 'workspace attached' : 'compaction'} at ${plain(recovery.since, 30)}): run tp-agent work recover; re-verify any "done" claim against live records first`,
      [],
      reserve,
    )
  }
  // Autonomy (Phase D1), only while it is on: what is blocked and since when, as the index words it.
  const autonomy = typeof w.autonomy === 'object' && w.autonomy !== null ? (w.autonomy as Record<string, unknown>) : undefined
  if (autonomy !== undefined && typeof autonomy.line === 'string' && autonomy.line !== '') append(plain(autonomy.line, 300), [], reserve)
  if (checkpoint !== undefined && typeof checkpoint.line === 'string' && checkpoint.line !== '') append(plain(checkpoint.line, 300), [], reserve)
  if (recovery !== undefined && recovery.pending === true && typeof checkpoint?.caution === 'string' && checkpoint.caution !== '') {
    append(plain(checkpoint.caution, 300), [], reserve)
  }
  if (candidates.length > 0) append('work in this directory, not attached (tp-agent work attach ID):', candidates, reserve)
  if (others > 0) append(`+${others} other active workspaces: tp-agent work list`, [], reserve)
  for (const line of [noteList('notes', notes.recent), noteList('important notes', notes.important)]) if (line !== undefined) append(line, [], reserve)
  // Files that could not be read are counted by the index, one short line here; the rest of the block is unaffected.
  const unreadable = (w.skipped as { count?: unknown } | null | undefined)?.count
  if (typeof unreadable === 'number' && unreadable > 0) append(`${Math.floor(unreadable)} unreadable work files; tp-agent work list`, [], reserve)
  const decisions = decisionSummary(w.decisions, Math.min(decisionChars(w), Math.max(0, room - size)))
  if (decisions !== '') {
    const [head, ...tail] = decisions.split('\n')
    // all or nothing: a decisions block that does not fit whole is left out, never cut mid-way
    if (size + decisions.length + 1 <= room) append(head, tail, 0)
  }
  return lines
}

// The session index as the main reads it: objective, work, paused, active and pending (not recent),
// never longer than INDEX_CHARS.
// --no-write: injection only reads; it neither launches queued work nor rewrites the index file.
// `rows` are only the rows shown in `text` (as a line, or by id): a row cut for length was
// not shown to the main, so it must stay eligible for a wake.
async function readIndex($: EngineInterface): Promise<{ text: string; rows: unknown[] } | undefined> {
  const index = await runTpAgent($, ['index', '--no-write'])
  if (typeof index !== 'object' || index === null) return undefined
  const { objective, active, pending, paused, unstatused } = index as Record<string, unknown>
  const lines = ['tmux-pi task index (injected; run `tp-agent index` only if this is missing):']
  lines.push(`objective: ${cutUnits(typeof objective === 'string' && objective.trim() !== '' ? objective.trim() : '(none set)', 1000)}`)
  // Room kept for the active and pending headers and the omission line, so the whole text fits.
  const limit = INDEX_CHARS - 120
  // Workers that await the main come first (paused, unstatused, and needs_input: a request with no pause), then the
  // queue entries that will never start, in what room those left; the other active rows and the pending ones take the rest.
  const activeRows = Array.isArray(active) ? active : []
  const isAsking = (row: unknown) => typeof row === 'object' && row !== null && (row as { status?: unknown }).status === 'needs_input'
  // The protected tiers are sized first and hold back only the decisions minimum; the work block takes what they leave and is
  // placed ahead of them.
  const whole = index as Record<string, unknown>
  const reserve = workReserve(whole)
  const head = lines.length
  const first = pausedLines(
    [['replied without a result status', unstatused], ['paused', paused], ['needs input', activeRows.filter(isAsking)]],
    limit - lines.join('\n').length - 1 - reserve,
  )
  lines.push(...first.lines)
  // Listed only when there is one: unlike the others it is not a state every session has a line for.
  const refused = refusedLines(
    activeRows.filter(row => isRefused(row) && indexLine(row) !== undefined),
    limit - lines.join('\n').length - reserve,
  )
  lines.push(...refused.lines)
  // workLines counts each line with its newline, so what is left is exactly `limit` less the text so far.
  lines.splice(head, 0, ...workLines(whole, limit - lines.join('\n').length))
  const shown = [...first.rows, ...refused.rows]
  let size = lines.join('\n').length
  let cut = 0
  for (const [name, group] of [['active', activeRows.filter(row => !isAsking(row) && !isRefused(row))], ['pending', pending]] as const) {
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
  return { text: wellFormed(lines.join('\n')), rows: shown }
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
    // The startup snapshot, just before the startup index: a request transition after it is delivered once, by the first poll.
    if (cursors === undefined) await startSnapshot($)
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

  // The index after a compaction is not injected here. A `classic.SessionStart` handler was written for it, but the engine never
  // delivered its answer (no hook-context record after the compaction boundary, in a live test and across 7 compactions of a
  // long session), and an append made while compacting lands before the boundary, which the model never reads. The command
  // hook hooks/sessionstart-compact.sh (SessionStart, matcher compact in hooks.json) marks recovery pending and delivers the
  // index as additionalContext, from `tp-agent index --no-write --hook-json`. The startup append above stays.

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (!isActive || e.props.hasSurvey) return next(e)
    const summary = await read($, tasks)
    if (summary === null) return next(e)
    const lines = [...(isQuiet(summary) ? [] : [{ key: 'tp-band', text: bandText(summary) }]), ...(summary.notes ?? []).map((text, i) => ({ key: `tp-note-${i}`, text }))]
    if (lines.length === 0) return next(e)
    const { Box, Text } = $.ui.resolve(e)

    return (
      <Box flexDirection="column">
        {lines.map(line => (
          <Text key={line.key} dimColor>
            {clip(line.text, e.props.bodyColumns)}
          </Text>
        ))}
      </Box>
    )
  })

  on('command.run', { command: 'workers' }, async ($, e, next) => {
    if (!isActive) return next(e)
    const read = await readTasks($)
    if ('failure' in read) return { text: `tp-agent list failed: ${read.failure}.` }
    const left = read.other.length === 0 ? [] : [`${read.other.length} in this session belong to another main: ${read.other.map(row => `${row.id} (${row.main})`).join(', ')}`]
    if (read.rows.length === 0) return { text: ['No tasks in this session.', ...left].join('\n') }
    return { text: ['id  role  status', ...read.rows.map(row => `${row.id}  ${row.role ?? '-'}  ${row.status}`), ...left].join('\n') }
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
    // The worker's own report.md is kept apart from the last reply: say where it is and how big.
    let report = ''
    try {
      const { report_path: path, report_bytes: bytes, review_notice: notice } = JSON.parse(ran.stdout)
      if (typeof path === 'string' && typeof bytes === 'number') report = ` Report: ${path} (${bytes} bytes).`
      if (typeof notice === 'string') report += ` NOT AN APPROVAL: ${notice}`
    } catch {
      report = ''
    }
    return { text: `Collected ${id}.${report}`, context: [`tp-agent collect ${id}:\n${ran.stdout}`] }
  })
}
