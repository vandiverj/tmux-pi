import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

const ROOT = '/opt/tmux-pi'
const AGENT = `${ROOT}/bin/tp-agent`
const MAIN = { TP_ROOT: ROOT, TP_MODE: 'orchestrator' }
const BAND = { hasSurvey: false, isWorking: false, maxRows: 5, bodyColumns: 80 }

type Row = { id: string; status: string; role?: string; capability_request?: unknown; result_status?: string | null }

// Stands for the engine beneath the plugin: answers the environment, the clock, the
// session and every host command from memory, and records what the module did. A plugin's
// own `$.session.append` is refused here (a deny from the test's hook; newer kits
// store the row when nothing refuses it), so the index row appended at start is not asserted;
// the text it carries is, through the compaction route.
function world(on: On, env: Record<string, string>) {
  const w = {
    rows: [] as Row[],
    isFailing: false,
    isIndexFailing: false,
    collectStatus: 'completed',
    collectResult: undefined as string | undefined,
    collectReport: undefined as { report_path: string; report_bytes: number } | undefined,
    objective: 'ship it',
    paused: undefined as Record<string, unknown>[] | undefined,
    unstatused: undefined as Record<string, unknown>[] | undefined,
    indexWrites: 0, // `tp-agent index` runs that, unlike `index --no-write`, rewrite the index file
    work: undefined as Record<string, unknown> | undefined,
    workError: undefined as string | undefined,
    isRejecting: false,
    acceptsAppend: false,
    listOut: undefined as string | undefined,
    ran: [] as string[][],
    submitted: [] as string[],
    toasts: [] as string[],
    commands: [] as string[],
    release: undefined as (() => void) | undefined,
    isHolding: false,
    clock: mock.clock(on),
  }
  mock.env(on, env)
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('command.register', (_$, e) => {
    w.commands.push(e.name)
    return { value: { command: e.name } }
  })
  on('session.append', (_$, e, next) => (w.acceptsAppend ? next(e) : ({ deny: 'the test refuses the index row' } as never)))
  on('command.run', () => ({ text: 'engine' }))
  on('classic.SessionStart', () => ({}))
  on('ui.toast', (_$, e) => {
    w.toasts.push(e.text)
    return { value: undefined }
  })
  on('prompt.submit', async (_$, e) => {
    w.submitted.push(e.text)
    if (w.isRejecting) throw new Error('refused')
    if (w.isHolding) await new Promise<void>(resolve => (w.release = resolve))
    return { text: e.text }
  })
  on('process.run', (_$, e) => {
    w.ran.push([...e.argv])
    const [tool, verb, id] = e.argv
    let stdout = ''
    if (tool === AGENT && verb === 'index' && !e.argv.includes('--no-write')) w.indexWrites += 1
    if (tool !== AGENT) stdout = `viewing ${verb}`
    else if (verb === 'list') stdout = w.listOut ?? JSON.stringify(w.rows)
    else if (verb === 'index') stdout = JSON.stringify({ objective: w.objective, active: w.rows, pending: [], paused: w.paused ?? [], unstatused: w.unstatused ?? [], recent: [], ...(w.work === undefined ? {} : { work: w.work }), ...(w.workError === undefined ? {} : { work_error: w.workError }) })
    else if (verb === 'collect') stdout = JSON.stringify({ id, status: w.collectStatus, result_status: w.collectResult, ...w.collectReport })
    const exitCode = w.isFailing || (verb === 'index' && w.isIndexFailing) ? 1 : 0
    return { value: { exitCode, stdout: exitCode === 0 ? stdout : '', stderr: exitCode === 0 ? '' : 'boom', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  return w
}

const start = (isInteractive: boolean) => ({ cwd: '/work', surface: isInteractive ? ('terminal' as const) : null, isInteractive })
const verbs = (ran: string[][]) => ran.map(argv => argv[1])
const command = (name: string, args = '') => ({ command: name, args }) as never

// ---- gate ----

test('an interactive orchestrator main injects the index, registers commands and polls', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = [{ id: 'coder-1', status: 'working', role: 'coder' }]
  expect(await $.session.start(start(true))).toEqual({ cwd: '/work' })
  expect(w.ran.slice(0, 2)).toEqual([[AGENT, 'index', '--no-write'], [AGENT, 'list']])
  expect(w.commands).toEqual(['workers', 'worker-view', 'worker-collect'])
})

for (const [name, env, isInteractive] of [
  ['a worker (TP_TASK_DIR set)', { ...MAIN, TP_TASK_DIR: '/state/tasks/coder-1' }, true],
  ['a headless run', MAIN, false],
  ['a legacy (non-orchestrator) main', { TP_ROOT: ROOT, TP_MODE: 'legacy' }, true],
  ['a session without TP_ROOT', { TP_MODE: 'orchestrator' }, true],
] as const) {
  test(`${name} does nothing: no command, timer or slash command`, async ($, on) => {
    const w = world(on, env)
    w.rows = [{ id: 'coder-1', status: 'settled' }]
    expect(await $.session.start(start(isInteractive))).toEqual({ cwd: '/work' })
    await w.clock.advance(30000)
    expect(w.ran).toEqual([])
    expect(w.commands).toEqual([])
    expect(w.submitted).toEqual([])
    expect(await $.command.run(command('workers'))).toEqual({ text: 'engine' })
  })
}

test('a failing tp-agent leaves the session start untouched', async ($, on) => {
  const w = world(on, MAIN)
  w.isFailing = true
  expect(await $.session.start(start(true))).toEqual({ cwd: '/work' })
  expect(w.submitted).toEqual([])
})

// ---- notifier ----

// The world refuses the module's session append, so the index never reaches the
// conversation; that is the refused-append path, where waiting tasks must be announced.
test('when the index append is refused, what is already waiting is announced once', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = [{ id: 'coder-1', status: 'settled' }, { id: 'coder-2', status: 'needs_input' }]
  await $.session.start(start(true))
  await w.clock.advance(15000)
  expect(w.submitted.length).toBe(1)
  expect(w.toasts.length).toBe(2)
})

test('after a reload, delivered tasks stay quiet and a task that changed meanwhile wakes', async ($, on) => {
  const w = world(on, MAIN)
  // What the module before the reload left in session state.
  const kept: Record<string, unknown> = { notifier: { epoch: 1, isIndexed: true, delivered: ['coder-1:settled'], pending: [] } }
  on('state.get', { plugin: 'tmux-pi' }, (_$, e, next) => (e.key in kept ? { value: { value: kept[e.key], version: 1 } } : next(e)))
  on('state.set', { plugin: 'tmux-pi' }, (_$, e, next) => {
    if (!(e.key in kept)) return next(e)
    kept[e.key] = e.value
    return { value: { isSet: true, version: 2 } }
  })
  w.rows = [{ id: 'coder-1', status: 'settled' }, { id: 'coder-2', status: 'needs_input' }]
  await $.session.start(start(true))
  await w.clock.advance(15000)
  expect(verbs(w.ran).includes('index')).toBe(false)
  expect(w.submitted.length).toBe(1)
  expect(w.submitted[0]).toContain('coder-2 needs input')
  expect(w.submitted[0]).not.toContain('coder-1')
})

test('a wake still unacknowledged when the module reloaded is sent again, not lost', async ($, on) => {
  const w = world(on, MAIN)
  // The module before the reload had marked coder-2 delivered but its submit never resolved.
  const kept: Record<string, any> = {
    notifier: {
      epoch: 4,
      isIndexed: true,
      delivered: ['coder-1:settled', 'coder-2:failed'],
      pending: [{ id: 'coder-2', status: 'failed', role: 'coder', permission: null, capability_request: null }],
    },
  }
  on('state.get', { plugin: 'tmux-pi' }, (_$, e, next) => (e.key in kept ? { value: { value: kept[e.key], version: 1 } } : next(e)))
  on('state.set', { plugin: 'tmux-pi' }, (_$, e, next) => {
    if (!(e.key in kept)) return next(e)
    kept[e.key] = e.value
    return { value: { isSet: true, version: 2 } }
  })
  w.rows = [{ id: 'coder-1', status: 'settled' }, { id: 'coder-2', status: 'failed' }]
  await $.session.start(start(true))
  await w.clock.advance(15000)
  expect(w.submitted.length).toBe(1)
  expect(w.submitted[0]).toContain('coder-2 failed')
  expect(w.submitted[0]).not.toContain('coder-1')
  expect(w.toasts).toEqual([])
  expect(kept.notifier.pending).toEqual([])
  expect(kept.notifier.epoch).toBe(5)
  expect(kept.notifier.delivered).toEqual(['coder-1:settled', 'coder-2:failed'])
})

test('a store that could not be read at start does not leave the module superseded for good', async ($, on) => {
  const w = world(on, MAIN)
  const kept: Record<string, any> = { notifier: { epoch: 7, isIndexed: true, delivered: [], pending: [] } }
  let isDown = true
  on('state.get', { plugin: 'tmux-pi' }, (_$, e, next) => {
    if (!(e.key in kept)) return next(e)
    if (isDown) return { deny: 'state is down' }
    return { value: { value: kept[e.key], version: 1 } }
  })
  on('state.set', { plugin: 'tmux-pi' }, (_$, e, next) => {
    if (!(e.key in kept)) return next(e)
    if (isDown) return { deny: 'state is down' }
    kept[e.key] = e.value
    return { value: { isSet: true, version: 2 } }
  })
  w.rows = [{ id: 'coder-1', status: 'working' }]
  await $.session.start(start(true))
  isDown = false
  w.rows = [{ id: 'coder-1', status: 'settled' }]
  await w.clock.advance(20000)
  expect(w.submitted.length).toBe(1)
  // Only the failed-read path claims an epoch this high: proof that it ran.
  expect(kept.notifier.epoch >= 1e15).toBe(true)
  expect(kept.notifier.delivered).toEqual(['coder-1:settled'])
})

test('a module replaced by a newer one goes quiet and does not overwrite its successor', async ($, on) => {
  const w = world(on, MAIN)
  const kept: Record<string, any> = { notifier: { epoch: 0, isIndexed: false, delivered: [], pending: [] } }
  on('state.get', { plugin: 'tmux-pi' }, (_$, e, next) => (e.key in kept ? { value: { value: kept[e.key], version: 1 } } : next(e)))
  on('state.set', { plugin: 'tmux-pi' }, (_$, e, next) => {
    if (!(e.key in kept)) return next(e)
    kept[e.key] = e.value
    return { value: { isSet: true, version: 2 } }
  })
  w.rows = [{ id: 'coder-1', status: 'working' }]
  await $.session.start(start(true))
  // A newer module claims the state.
  const successor = { epoch: 99, isIndexed: true, delivered: ['x-9:settled'], pending: [] }
  kept.notifier = successor
  w.rows = [{ id: 'coder-1', status: 'settled' }]
  await w.clock.advance(20000)
  expect(kept.notifier).toEqual(successor)
  expect(w.submitted).toEqual([])
})

test('a failing state write does not stop the wake, and does not repeat it', async ($, on) => {
  const w = world(on, MAIN)
  on('state.set', { plugin: 'tmux-pi' }, (_$, e, next) => {
    if (e.key === 'notifier') return { deny: 'state is down' }
    return next(e)
  })
  w.rows = [{ id: 'coder-1', status: 'working' }]
  await $.session.start(start(true))
  w.rows = [{ id: 'coder-1', status: 'settled' }]
  await w.clock.advance(20000)
  expect(w.submitted.length).toBe(1)
  expect(w.submitted[0]).toContain('coder-1 settled')
})

test('a repeated session start after a refused append does not announce the same task twice', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = [{ id: 'coder-1', status: 'settled' }]
  await $.session.start(start(true))
  await w.clock.advance(5000)
  expect(w.submitted.length).toBe(1)
  await $.session.start(start(true))
  await w.clock.advance(15000)
  expect(w.submitted.length).toBe(1)
})

test('a start with the gate closed stops the timer of an earlier active start', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = [{ id: 'coder-1', status: 'working' }]
  await $.session.start(start(true))
  await $.session.start(start(false))
  const before = w.ran.length
  w.rows = [{ id: 'coder-1', status: 'settled' }]
  await w.clock.advance(20000)
  expect(w.ran.length).toBe(before)
  expect(w.submitted).toEqual([])
})

test('a second session start in the same module keeps one timer', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = [{ id: 'coder-1', status: 'working' }]
  await $.session.start(start(true))
  await $.session.start(start(true))
  const before = verbs(w.ran).filter(verb => verb === 'list').length
  await w.clock.advance(10000)
  expect(verbs(w.ran).filter(verb => verb === 'list').length).toBe(before + 2)
})

test('a settled task that reports failing tests it does not own says so and names the investigation', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = [{ id: 'coder-1', status: 'working' }]
  await $.session.start(start(true))
  w.rows = [{ id: 'coder-1', status: 'settled', failing_tests: { entries: [{ test: 'tests.test_x' }] } }]
  await w.clock.advance(5000)
  expect(w.submitted.length).toBe(1)
  expect(w.submitted[0]).toContain('reports failing tests it does not own')
  expect(w.submitted[0]).toContain('tp-agent dispatch --investigate-failure coder-1')
})

test('one transition gives one toast and one wake; the same status later gives none', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = [{ id: 'coder-1', status: 'working' }]
  await $.session.start(start(true))
  w.rows = [{ id: 'coder-1', status: 'settled' }]
  await w.clock.advance(5000)
  expect(w.submitted.length).toBe(1)
  expect(w.submitted[0]).toContain('tp-agent collect coder-1')
  expect(w.toasts.length).toBe(1)
  await w.clock.advance(20000)
  expect(w.submitted.length).toBe(1)
  expect(w.toasts.length).toBe(1)
})

test('a paused worker wakes with "replied without a result status" and no collect hint', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = [{ id: 'coder-1', status: 'working' }]
  await $.session.start(start(true))
  w.rows = [{ id: 'coder-1', status: 'paused' }]
  await w.clock.advance(5000)
  expect(w.submitted.length).toBe(1)
  expect(w.submitted[0]).toContain('coder-1 replied without a result status')
  expect(w.submitted[0]).not.toContain('collect')
})

test('every notable status wakes: needs_input, settled and failed', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = [{ id: 'a-1', status: 'working' }, { id: 'b-2', status: 'working' }, { id: 'c-3', status: 'working' }]
  await $.session.start(start(true))
  w.rows = [{ id: 'a-1', status: 'needs_input' }, { id: 'b-2', status: 'settled' }, { id: 'c-3', status: 'failed' }]
  await w.clock.advance(5000)
  expect(w.submitted.length).toBe(1)
  expect(w.submitted[0]).toContain('a-1 needs input')
  expect(w.submitted[0]).toContain('b-2 settled')
  expect(w.submitted[0]).toContain('c-3 failed')
})

test('polling goes on while a wake waits for the main to go idle', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = [{ id: 'a-1', status: 'working' }, { id: 'b-2', status: 'working' }]
  await $.session.start(start(true))
  w.isHolding = true
  w.rows = [{ id: 'a-1', status: 'settled' }, { id: 'b-2', status: 'working' }]
  await w.clock.advance(5000)
  const lists = verbs(w.ran).filter(verb => verb === 'list').length
  w.rows = [{ id: 'a-1', status: 'settled' }, { id: 'b-2', status: 'needs_input' }]
  await w.clock.advance(10000)
  expect(verbs(w.ran).filter(verb => verb === 'list').length).toBe(lists + 2)
  expect(w.toasts.length).toBe(2)
  const ui = await $.ui.mount({ plugin: 'tmux-pi', surface: 'terminal', component: 'AbovePrompt', props: BAND as never })
  expect((await ui.find({ type: 'Text', text: /workers:/ }))?.text).toContain('needs input: b-2')
  await ui.unmount()
  expect(w.submitted.length).toBe(1)
  w.isHolding = false
  w.release?.()
})

test('a task that settles after a failed first read is still announced', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = [{ id: 'coder-1', status: 'working' }]
  w.isFailing = true
  await $.session.start(start(true))
  w.isFailing = false
  w.rows = [{ id: 'coder-1', status: 'settled' }]
  await w.clock.advance(5000)
  expect(w.submitted.length).toBe(1)
  expect(w.submitted[0]).toContain('coder-1 settled')
})

test('with no index at start, what is already waiting is announced once', async ($, on) => {
  const w = world(on, MAIN)
  w.isIndexFailing = true
  w.rows = [{ id: 'coder-1', status: 'settled' }]
  await $.session.start(start(true))
  await w.clock.advance(15000)
  expect(w.submitted.length).toBe(1)
})

test('a rejected wake is retried, and given up after three tries', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = [{ id: 'coder-1', status: 'working' }]
  await $.session.start(start(true))
  w.isRejecting = true
  w.rows = [{ id: 'coder-1', status: 'failed' }]
  await w.clock.advance(60000)
  expect(w.submitted.length).toBe(3)
  expect(w.toasts.length).toBe(1)
})

test('an answered capability request does not relabel a permission prompt', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = [{ id: 'a-1', status: 'working' }, { id: 'b-2', status: 'working' }]
  await $.session.start(start(true))
  w.rows = [
    { id: 'a-1', status: 'needs_input', capability_request: { status: 'answered' } },
    { id: 'b-2', status: 'needs_input', capability_request: { status: 'pending' } },
  ]
  await w.clock.advance(5000)
  expect(w.submitted[0]).toContain('a-1 needs input (is waiting at a permission prompt')
  expect(w.submitted[0]).toContain('b-2 needs input (has a capability request)')
})

test('transitions found while a wake is unresolved ride one follow-up', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = [{ id: 'a-1', status: 'working' }, { id: 'b-2', status: 'working' }, { id: 'c-3', status: 'working' }]
  await $.session.start(start(true))
  w.isHolding = true
  w.rows = [{ id: 'a-1', status: 'settled' }, { id: 'b-2', status: 'working' }, { id: 'c-3', status: 'working' }]
  await w.clock.advance(5000)
  expect(w.submitted.length).toBe(1)
  w.rows = [{ id: 'a-1', status: 'settled' }, { id: 'b-2', status: 'failed' }, { id: 'c-3', status: 'working' }]
  await w.clock.advance(5000)
  w.rows = [{ id: 'a-1', status: 'settled' }, { id: 'b-2', status: 'failed' }, { id: 'c-3', status: 'settled' }]
  await w.clock.advance(5000)
  expect(w.submitted.length).toBe(1)
  w.isHolding = false
  w.release?.()
  await w.clock.advance(5000)
  expect(w.submitted.length).toBe(2)
  expect(w.submitted[1]).toContain('b-2 failed')
  expect(w.submitted[1]).toContain('c-3 settled')
})

test('leaving and re-entering needs_input wakes twice', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = [{ id: 'coder-1', status: 'working' }]
  await $.session.start(start(true))
  for (const status of ['needs_input', 'working', 'needs_input']) {
    w.rows = [{ id: 'coder-1', status }]
    await w.clock.advance(5000)
  }
  expect(w.submitted.length).toBe(2)
})

test('a failing or garbled list skips the tick without a wake', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = [{ id: 'coder-1', status: 'working' }]
  await $.session.start(start(true))
  w.listOut = 'not json'
  await w.clock.advance(5000)
  w.listOut = undefined
  w.isFailing = true
  await w.clock.advance(5000)
  expect(w.submitted).toEqual([])
  w.isFailing = false
  w.rows = [{ id: 'coder-1', status: 'failed' }]
  await w.clock.advance(5000)
  expect(w.submitted.length).toBe(1)
})

// ---- index after compaction ----

test('SessionStart after a compaction carries the index as context', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = [{ id: 'coder-1', status: 'working', role: 'coder' }]
  await $.session.start(start(true))
  const answer = await $.classic.SessionStart({ source: 'compact' } as never)
  expect(answer.additionalContext?.length).toBe(1)
  expect(answer.additionalContext?.[0]).toContain('tmux-pi task index')
  expect(answer.additionalContext?.[0]).toContain('ship it')
  expect(answer.additionalContext?.[0]).toContain('coder-1')
})

test('injecting the index, at start and after a compaction, never rewrites the index file', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = [{ id: 'coder-1', status: 'working', role: 'coder' }]
  await $.session.start(start(true))
  await $.classic.SessionStart({ source: 'compact' } as never)
  expect(w.ran.filter(argv => argv[1] === 'index').length).toBeGreaterThanOrEqual(2)
  expect(w.indexWrites).toBe(0)
})

test('the index carries why a queued task was refused', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = [{ id: 'shape01', status: 'queued', reason: 'start refused: the queue entry is not an object' }]
  await $.session.start(start(true))
  const text = (await $.classic.SessionStart({ source: 'compact' } as never)).additionalContext?.[0] ?? ''
  expect(text).toContain('- shape01 queued (start refused: the queue entry is not an object)')
})

test('an index too long to inject says how many rows it left out', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = Array.from({ length: 400 }, (_, i) => ({ id: `researcher-${String(i).padStart(4, '0')}-abcdef`, status: 'working', role: 'researcher' }))
  await $.session.start(start(true))
  const text = (await $.classic.SessionStart({ source: 'compact' } as never)).additionalContext?.[0] ?? ''
  expect(text.length <= 6000).toBe(true)
  expect(text).toContain('researcher-0000-abcdef')
  expect(text).toContain('more not shown; run tp-agent index')
  expect(text).not.toContain('researcher-0399-abcdef')
})

test('SessionStart from startup adds nothing, and neither does a failing index', async ($, on) => {
  const w = world(on, MAIN)
  await $.session.start(start(true))
  expect((await $.classic.SessionStart({ source: 'startup' } as never)).additionalContext ?? []).toEqual([])
  w.isFailing = true
  expect((await $.classic.SessionStart({ source: 'compact' } as never)).additionalContext ?? []).toEqual([])
})

test('a worker gets no index after a compaction', async ($, on) => {
  const w = world(on, { ...MAIN, TP_TASK_DIR: '/state/tasks/coder-1' })
  await $.session.start(start(true))
  expect((await $.classic.SessionStart({ source: 'compact' } as never)).additionalContext ?? []).toEqual([])
  expect(w.ran).toEqual([])
})

// ---- band ----

test('the band shows counts and who needs input, on each surface', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = [{ id: 'coder-1', status: 'working' }, { id: 'coder-2', status: 'needs_input' }, { id: 'coder-3', status: 'settled' }]
  await $.session.start(start(true))
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ plugin: 'tmux-pi', surface, component: 'AbovePrompt', props: BAND as never })
    const band = await ui.find({ type: 'Text', text: /workers:/ })
    expect(band?.text).toContain('1 active')
    expect(band?.text).toContain('1 to collect')
    expect(band?.text).toContain('needs input: coder-2')
    expect(band?.text).not.toContain('without a result status')
    await ui.unmount()
  }
})

test('the band does not count a refused queue entry as active', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = [{ id: 'coder-1', status: 'working' }, { id: 'shape01', status: 'queued', reason: 'start refused: the queue entry is not an object' },
    { id: 'coder-9', status: 'queued', reason: 'waiting for coder-1' }]
  await $.session.start(start(true))
  const ui = await $.ui.mount({ plugin: 'tmux-pi', surface: 'terminal', component: 'AbovePrompt', props: BAND as never })
  const band = await ui.find({ type: 'Text', text: /workers:/ })
  expect(band?.text).toContain('2 active')
  expect(band?.text).toContain('1 refused')
  await ui.unmount()
})

test('a long id list is cut to the band width', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = Array.from({ length: 12 }, (_, i) => ({ id: `researcher-${i}abcdef`, status: 'needs_input' }))
  await $.session.start(start(true))
  const ui = await $.ui.mount({ plugin: 'tmux-pi', surface: 'terminal', component: 'AbovePrompt', props: { ...BAND, bodyColumns: 40 } as never })
  const band = await ui.find({ type: 'Text', text: /workers:/ })
  expect(band?.text.length).toBe(40)
  await ui.unmount()
})

// ---- slash commands ----

test('/workers lists the tasks', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = [{ id: 'coder-1', status: 'working', role: 'coder' }]
  await $.session.start(start(true))
  expect((await $.command.run(command('workers'))).text).toBe('id  role  status\ncoder-1  coder  working')
})

test('/worker-view and /worker-collect refuse a bad id without running anything', async ($, on) => {
  const w = world(on, MAIN)
  await $.session.start(start(true))
  const before = w.ran.length
  expect((await $.command.run(command('worker-view', 'a; rm -rf /'))).text).toContain('Usage')
  expect((await $.command.run(command('worker-collect', '../x'))).text).toContain('Usage')
  expect((await $.command.run(command('worker-collect', ''))).text).toContain('Usage')
  expect((await $.command.run(command('worker-collect', '--help'))).text).toContain('Usage')
  expect(w.ran.length).toBe(before)
})

test('/worker-view and /worker-collect pass the id as one argument', async ($, on) => {
  const w = world(on, MAIN)
  await $.session.start(start(true))
  expect((await $.command.run(command('worker-view', ' coder-1 '))).text).toBe('viewing coder-1')
  expect(w.ran.at(-1)).toEqual([`${ROOT}/bin/tp-view`, 'coder-1'])
  const collected = await $.command.run(command('worker-collect', 'coder-1'))
  expect(collected.text).toBe('Collected coder-1.')
  expect(collected.context?.[0]).toContain('completed')
  expect(w.ran.at(-1)).toEqual([AGENT, 'collect', 'coder-1'])
})

test('/worker-collect shows the worker report path and size when it wrote one', async ($, on) => {
  const w = world(on, MAIN)
  w.collectReport = { report_path: '/t/coder-1/report.md', report_bytes: 1234 }
  await $.session.start(start(true))
  expect((await $.command.run(command('worker-collect', 'coder-1'))).text).toBe('Collected coder-1. Report: /t/coder-1/report.md (1234 bytes).')
})

test('the band counts a paused worker, and a band of only paused workers is not quiet', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = [{ id: 'coder-1', status: 'paused' }]
  await $.session.start(start(true))
  const ui = await $.ui.mount({ plugin: 'tmux-pi', surface: 'terminal', component: 'AbovePrompt', props: BAND as never })
  const band = await ui.find({ type: 'Text', text: /workers:/ })
  expect(band?.text).toContain('1 replied without a result status')
  await ui.unmount()
})

test('/worker-collect on a paused task says it replied without a result status', async ($, on) => {
  const w = world(on, MAIN)
  await $.session.start(start(true))
  w.collectStatus = 'paused'
  const answer = await $.command.run(command('worker-collect', 'coder-1'))
  expect(answer.text).toBe('coder-1 replied without a result status (paused); nothing was collected.')
  expect(answer.context).toBeUndefined()
})

test('/worker-collect on a task that is not settled says nothing was collected', async ($, on) => {
  const w = world(on, MAIN)
  await $.session.start(start(true))
  w.collectStatus = 'working'
  const answer = await $.command.run(command('worker-collect', 'coder-1'))
  expect(answer.text).toBe('coder-1 is working; nothing was collected.')
  expect(answer.context).toBeUndefined()
})

test('a dotted task id is accepted', async ($, on) => {
  const w = world(on, MAIN)
  await $.session.start(start(true))
  expect((await $.command.run(command('worker-view', 'fix.parser'))).text).toBe('viewing fix.parser')
})

test('a failing tp-agent collect answers its error as text', async ($, on) => {
  const w = world(on, MAIN)
  await $.session.start(start(true))
  w.isFailing = true
  expect((await $.command.run(command('worker-collect', 'coder-1'))).text).toBe('boom')
})

test('a paused worker that has a result status is called paused, not unstatused', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = [{ id: 'coder-1', status: 'working' }]
  await $.session.start(start(true))
  w.rows = [{ id: 'coder-1', status: 'paused', result_status: 'blocked' }]
  await w.clock.advance(5000)
  expect(w.submitted.length).toBe(1)
  expect(w.submitted[0]).toContain('coder-1 is paused')
  expect(w.submitted[0]).not.toContain('without a result status')
  const ui = await $.ui.mount({ plugin: 'tmux-pi', surface: 'terminal', component: 'AbovePrompt', props: BAND as never })
  const band = await ui.find({ type: 'Text', text: /workers:/ })
  expect(band?.text).toContain('1 paused')
  expect(band?.text).not.toContain('without a result status')
  await ui.unmount()
  w.collectStatus = 'paused'
  w.collectResult = 'blocked'
  const answer = await $.command.run(command('worker-collect', 'coder-1'))
  expect(answer.text).toBe('coder-1 is paused; nothing was collected.')
})

test('the injected index lists a worker that replied without a result status', async ($, on) => {
  const w = world(on, MAIN)
  w.unstatused = [{ id: 'coder-9', role: 'coder', harness: 'pi', status: 'paused', result_status: null }]
  w.paused = [{ id: 'coder-8', role: 'coder', harness: 'pi', status: 'paused', result_status: 'blocked' }]
  await $.session.start(start(true))
  const answer = await $.classic.SessionStart({ source: 'compact' } as never)
  expect(answer.additionalContext?.[0]).toContain('replied without a result status:')
  expect(answer.additionalContext?.[0]).toContain('coder-9')
  expect(answer.additionalContext?.[0]).toContain('paused:')
  expect(answer.additionalContext?.[0]).toContain('coder-8')
})

test('paused and unstatused workers are injected even when active rows fill the index', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = Array.from({ length: 400 }, (_, i) => ({ id: `researcher-${String(i).padStart(4, '0')}-abcdef`, status: 'working', role: 'researcher' }))
  w.unstatused = Array.from({ length: 6 }, (_, i) => ({ id: `coder-u${i}`, role: 'coder', harness: 'pi', status: 'paused', result_status: null }))
  w.paused = Array.from({ length: 6 }, (_, i) => ({ id: `coder-p${i}`, role: 'coder', harness: 'pi', status: 'paused', result_status: 'blocked' }))
  await $.session.start(start(true))
  const text = (await $.classic.SessionStart({ source: 'compact' } as never)).additionalContext?.[0] ?? ''
  expect(text.length <= 6000).toBe(true)
  for (let i = 0; i < 6; i++) {
    expect(text).toContain(`coder-u${i}`)
    expect(text).toContain(`coder-p${i}`)
  }
  expect(text).toContain('more not shown; run tp-agent index')
  expect(text).not.toContain('researcher-0399-abcdef')
  expect(text.indexOf('coder-u0') < text.indexOf('researcher-0000-abcdef')).toBe(true)
})

// The cap is hard (6000 characters) and paused workers are never dropped silently: full rows
// while they all fit, then ids only, then as many ids as fit and a line counting the rest.
const pausedRows = (n: number, prefix: string, result: string | null, pad = '') =>
  Array.from({ length: n }, (_, i) => ({
    id: `${prefix}${String(i).padStart(4, '0')}${pad}`, role: 'coder', harness: 'pi', status: 'paused', result_status: result,
    result_path: `/state/sessions/0123456789abcdef/tasks/${prefix}${String(i).padStart(4, '0')}${pad}/result.md`,
  }))
const busyRows = (n: number) =>
  Array.from({ length: n }, (_, i) => ({ id: `researcher-${String(i).padStart(4, '0')}-abcdef`, status: 'working', role: 'researcher' }))

test('index tier 1: paused workers that fit are full rows, within the cap', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = busyRows(400)
  w.unstatused = pausedRows(10, 'coder-u', null)
  w.paused = pausedRows(10, 'coder-p', 'blocked')
  await $.session.start(start(true))
  const text = (await $.classic.SessionStart({ source: 'compact' } as never)).additionalContext?.[0] ?? ''
  expect(text.length <= 6000).toBe(true)
  for (const row of [...w.unstatused, ...w.paused]) expect(text).toContain(`- ${row.id} coder pi paused`)
  for (const row of w.unstatused) expect(text).toContain(row.result_path)
  expect(text).not.toContain('ids only')
  expect(text).not.toContain('workers waiting for you not shown')
  expect(text).toContain('more not shown; run tp-agent index')
})

test('index tier 2: paused workers too many for full rows are all listed by id, within the cap', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = busyRows(400)
  w.unstatused = pausedRows(60, 'coder-u', null)
  w.paused = pausedRows(60, 'coder-p', 'blocked')
  await $.session.start(start(true))
  const text = (await $.classic.SessionStart({ source: 'compact' } as never)).additionalContext?.[0] ?? ''
  expect(text.length <= 6000).toBe(true)
  expect(text).toContain('replied without a result status (ids only; tp-agent inspect <id>): coder-u0000, ')
  expect(text).toContain('paused (ids only; tp-agent inspect <id>): coder-p0000, ')
  for (const row of [...w.unstatused, ...w.paused]) expect(text).toContain(row.id)
  expect(text).not.toContain('result.md')
  expect(text).not.toContain('workers waiting for you not shown')
  // What room is left still goes to active rows, after the paused ones.
  expect(text.indexOf('coder-p0059') < text.indexOf('researcher-0000-abcdef')).toBe(true)
})

test('index tier 3: when even the ids do not fit, as many as fit are shown with a count of the rest', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = busyRows(400)
  w.unstatused = pausedRows(300, 'coder-u', null, '-0123456789abcdef')
  w.paused = pausedRows(300, 'coder-p', 'blocked', '-0123456789abcdef')
  await $.session.start(start(true))
  const text = (await $.classic.SessionStart({ source: 'compact' } as never)).additionalContext?.[0] ?? ''
  expect(text.length <= 6000).toBe(true)
  expect(text.length > 5000).toBe(true)
  const all = [...w.unstatused, ...w.paused]
  const listed = all.filter(row => text.includes(row.id))
  expect(listed.length > 100).toBe(true)
  expect(listed.length < all.length).toBe(true)
  // Ids are shown in order, with no gap, and the line counts exactly the ones left out.
  expect(listed.map(row => row.id)).toEqual(all.slice(0, listed.length).map(row => row.id))
  expect(text).toContain(`… ${all.length - listed.length} more workers waiting for you not shown; run tp-agent list to see them`)
})

test('index tier 3 with a long objective and one paused group only still fits and counts the rest', async ($, on) => {
  const w = world(on, MAIN)
  w.objective = 'o'.repeat(3000)
  w.rows = busyRows(50)
  w.paused = pausedRows(500, 'coder-p', 'blocked', '-0123456789abcdef')
  await $.session.start(start(true))
  const text = (await $.classic.SessionStart({ source: 'compact' } as never)).additionalContext?.[0] ?? ''
  expect(text.length <= 6000).toBe(true)
  const listed = w.paused.filter(row => text.includes(row.id))
  expect(listed.length > 50).toBe(true)
  expect(text).toContain(`… ${500 - listed.length} more workers waiting for you not shown; run tp-agent list to see them`)
  expect(text).toContain('more not shown; run tp-agent index')
})

test('a paused worker left out of a saturated index stays eligible for a wake', async ($, on) => {
  const w = world(on, MAIN)
  // This test is about an index that was read: the world stores the appended row here instead of refusing it,
  // so the rows the index named are the baseline and wake nobody.
  w.acceptsAppend = true
  w.paused = pausedRows(500, 'coder-p', 'blocked', '-0123456789abcdef')
  w.rows = w.paused
  await $.session.start(start(true))
  const text = (await $.classic.SessionStart({ source: 'compact' } as never)).additionalContext?.[0] ?? ''
  expect(text).not.toContain('coder-p0499')
  await w.clock.advance(5000)
  expect(w.submitted.join('\n')).toContain('coder-p0499-0123456789abcdef is paused')
  expect(w.submitted.join('\n')).not.toContain('coder-p0000-0123456789abcdef is paused')
})

test('at every size the index stays within the cap and names each paused worker or counts the rest', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = busyRows(200)
  await $.session.start(start(true))
  for (const n of [0, 1, 20, 25, 30, 35, 40, 100, 150, 200, 250, 300, 301, 302, 303, 304, 305, 320, 400, 900]) {
    w.unstatused = pausedRows(Math.ceil(n / 2), 'coder-u', null)
    w.paused = pausedRows(Math.floor(n / 2), 'coder-p', 'blocked')
    const text = (await $.classic.SessionStart({ source: 'compact' } as never)).additionalContext?.[0] ?? ''
    expect(text.length <= 6000).toBe(true)
    const missing = [...w.unstatused, ...w.paused].filter(row => !text.includes(row.id)).length
    if (missing > 0) expect(text).toContain(`… ${missing} more workers waiting for you not shown; run tp-agent list to see them`)
    else expect(text).not.toContain('workers waiting for you not shown')
  }
})

// Every worker that awaits the main is in the never-truncated tier, and its row says what it awaits.
// These read the injected TEXT, with 80 other rows around them.
test('workers awaiting the main are injected with what they await, among 80+ other rows', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = [
    ...busyRows(250),
    { id: 'coder-ask', role: 'coder', harness: 'pi', status: 'needs_input', capability_request: { status: 'pending' }, permission: null },
    { id: 'coder-perm', role: 'coder', harness: 'pi', status: 'needs_input', capability_request: null, permission: { status: 'pending' } },
    { id: 'coder-bare', role: 'coder', harness: 'pi', status: 'needs_input', capability_request: { status: 'answered' }, permission: null },
  ]
  w.paused = [
    { id: 'coder-pcap', role: 'coder', harness: 'pi', status: 'paused', result_status: 'blocked', capability_request: { status: 'pending' }, permission: null },
    // the shape tp-agent exports for a task dir holding only pending-capability.json (see test_pause_classification.py)
    { id: 'coder-pfile', role: 'coder', harness: 'pi', status: 'paused', result_status: 'blocked', capability_request: null, capability_pending: true, permission: null },
  ]
  w.unstatused = [
    { id: 'coder-uperm', role: 'coder', harness: 'pi', status: 'paused', result_status: null, capability_request: null, permission: { status: 'pending' } },
    { id: 'coder-uq', role: 'coder', harness: 'pi', status: 'paused', result_status: null, capability_request: null, permission: null },
  ]
  await $.session.start(start(true))
  const text = (await $.classic.SessionStart({ source: 'compact' } as never)).additionalContext?.[0] ?? ''
  const line = (id: string) => text.split('\n').find(l => l.startsWith(`- ${id} `)) ?? ''
  expect(text.length <= 6000).toBe(true)
  expect(line('coder-pcap')).toContain('[awaits: capability request pending]')
  expect(line('coder-pfile')).toContain('[awaits: capability approval pending]')
  expect(line('coder-uperm')).toContain('[awaits: permission prompt pending]')
  expect(line('coder-uq')).toContain('paused')
  expect(line('coder-uq')).not.toContain('awaits')
  expect(line('coder-ask')).toContain('needs_input')
  expect(line('coder-ask')).toContain('[awaits: capability request pending]')
  expect(line('coder-perm')).toContain('[awaits: permission prompt pending]')
  expect(line('coder-bare')).toContain('needs_input')
  expect(line('coder-bare')).not.toContain('awaits')
  expect(text).toContain('more not shown; run tp-agent index')
  // All of them come before the first ordinary active row.
  for (const id of ['coder-ask', 'coder-perm', 'coder-bare', 'coder-pcap', 'coder-pfile', 'coder-uperm', 'coder-uq'])
    expect(text.indexOf(`- ${id} `) < text.indexOf('researcher-0000-abcdef')).toBe(true)
  expect(text.indexOf('\nactive:') > text.indexOf('- coder-bare ')).toBe(true)
})

test('needs_input workers saturating the index are protected by the same cap and count line', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = [
    ...busyRows(100),
    ...Array.from({ length: 400 }, (_, i) => ({
      id: `coder-n${String(i).padStart(4, '0')}-0123456789abcdef`, role: 'coder', harness: 'pi', status: 'needs_input',
      capability_request: i % 2 === 0 ? { status: 'pending' } : null, permission: i % 2 === 1 ? { status: 'pending' } : null,
    })),
  ]
  await $.session.start(start(true))
  const text = (await $.classic.SessionStart({ source: 'compact' } as never)).additionalContext?.[0] ?? ''
  expect(text.length <= 6000).toBe(true)
  const asking = w.rows.filter(row => row.status === 'needs_input')
  const listed = asking.filter(row => text.includes(row.id))
  expect(listed.length > 50).toBe(true)
  expect(text).toContain(`… ${asking.length - listed.length} more workers waiting for you not shown; run tp-agent list to see them`)
  expect(text).toContain('coder-n0000-0123456789abcdef [capability request pending]')
  expect(text).toContain('coder-n0001-0123456789abcdef [permission prompt pending]')
  expect(text).not.toContain('researcher-0100-abcdef')
})

// A queue entry tp-agent refused to start never starts, has no pane and holds back its --after dependents: only the
// main can act on it (tp-agent stop <id>), so it sits in the protected part of the index beside paused and awaiting rows.
const refusedRow = (id: string) => ({ id, status: 'queued', reason: 'start refused: the queue entry is not an object' })

test('/worker-collect says in words that an incomplete review is not an approval', async ($, on) => {
  const w = world(on, MAIN)
  w.collectReport = { review_notice: 'review-1 review is incomplete: the review has no Coverage section.' }
  await $.session.start(start(true))
  const text = (await $.command.run(command('worker-collect', 'review-1'))).text
  expect(text).toContain('NOT AN APPROVAL: review-1 review is incomplete: the review has no Coverage section.')
})

test('the injected index marks an incomplete review and leaves a complete one plain', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = [
    { id: 'rv-bad', role: 'code-reviewer', status: 'settled', result_status: 'incomplete', verdict: 'APPROVED', review_complete: false },
    { id: 'rv-ok', role: 'code-reviewer', status: 'settled', result_status: 'complete', verdict: 'APPROVED', review_complete: true },
  ]
  await $.session.start(start(true))
  const text = (await $.classic.SessionStart({ source: 'compact' } as never)).additionalContext?.[0] ?? ''
  expect(text).toContain('verdict=APPROVED [review incomplete: not an approval]')
  expect(text).toContain('rv-ok code-reviewer settled result=complete verdict=APPROVED')
  expect(text).not.toContain('rv-ok code-reviewer settled result=complete verdict=APPROVED [')
})

test('refused queue entries sit with paused and awaiting rows, ahead of the active rows', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = [
    ...busyRows(3),
    { id: 'queued-ok', status: 'queued', role: 'coder', reason: 'waiting for coder-1' },
    refusedRow('shape01'),
    { id: 'coder-ask', role: 'coder', status: 'needs_input', capability_pending: true },
  ]
  w.paused = [{ id: 'coder-p', role: 'coder', status: 'paused', result_status: 'blocked', capability_pending: true }]
  w.unstatused = [{ id: 'coder-u', role: 'coder', status: 'paused', result_status: null }]
  await $.session.start(start(true))
  const text = (await $.classic.SessionStart({ source: 'compact' } as never)).additionalContext?.[0] ?? ''
  const at = (needle: string) => text.indexOf(needle)
  expect(text).toContain('queue entries that will not start:\n- shape01 queued (start refused: the queue entry is not an object)')
  expect(text).toContain('- coder-p coder paused result=blocked [awaits: capability approval pending]')
  expect(text).toContain('- coder-u coder paused')
  expect(text).toContain('- coder-ask coder needs_input [awaits: capability approval pending]')
  // protected group, before the active rows; an ordinary queued row stays an active row
  expect(at('- shape01') < at('\nactive:')).toBe(true)
  expect(at('\nactive:') < at('- queued-ok')).toBe(true)
  expect(text.slice(at('\nactive:'))).not.toContain('shape01')
  // a session with no refused entry has no such line
  w.rows = busyRows(3)
  const quiet = (await $.classic.SessionStart({ source: 'compact' } as never)).additionalContext?.[0] ?? ''
  expect(quiet).toContain('researcher-0000-abcdef')
  expect(quiet).not.toContain('queue entries that will not start')
})

test('refused queue entries saturating the index stay within the cap and are counted when cut', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = [...busyRows(100), ...Array.from({ length: 400 }, (_, i) => refusedRow(`shape${String(i).padStart(4, '0')}-0123456789abcdef`))]
  w.paused = [{ id: 'coder-p', role: 'coder', status: 'paused', result_status: 'blocked', capability_pending: true }]
  await $.session.start(start(true))
  const text = (await $.classic.SessionStart({ source: 'compact' } as never)).additionalContext?.[0] ?? ''
  expect(text.length <= 6000).toBe(true)
  const refused = w.rows.filter(row => row.status === 'queued')
  const listed = refused.filter(row => text.includes(row.id))
  expect(listed.length > 50).toBe(true)
  expect(listed.length < refused.length).toBe(true)
  expect(text).toContain(`… ${refused.length - listed.length} refused not shown`)
  expect(text).toContain('queue entries that will not start (ids only; tp-agent stop <id> removes one): shape0000-0123456789abcdef,')
  // the paused worker is ahead of them and keeps its full row
  expect(text).toContain('- coder-p coder paused result=blocked [awaits: capability approval pending]')
  expect(text).not.toContain('researcher-0100-abcdef')
})

// A refusal takes only the room that the workers waiting for the main leave. 189 paused ids fit the index (ids only)
// with 15 characters to spare; one refused entry must not cost the last of them its place.
test('one refused queue entry never displaces a paused id that fits without it', async ($, on) => {
  const idsIn = async (w: ReturnType<typeof world>) => {
    const text = (await $.classic.SessionStart({ source: 'compact' } as never)).additionalContext?.[0] ?? ''
    expect(text.length <= 6000).toBe(true)
    return text
  }
  const w = world(on, MAIN)
  w.paused = pausedRows(189, 'coder-p', 'blocked', '-0123456789abcdef')
  w.rows = []
  await $.session.start(start(true))
  const without = await idsIn(w)
  for (const row of w.paused) expect(without).toContain(row.id)
  expect(without).not.toContain('workers waiting for you not shown')
  w.rows = [refusedRow('shape01')]
  const withOne = await idsIn(w)
  for (const row of w.paused) expect(withOne).toContain(row.id)
  expect(withOne).not.toContain('workers waiting for you not shown')
  // the refusal is counted, not silently dropped: its id does not fit, so a short count line says so
  expect(withOne).toContain('… 1 refused not shown')
  // and at every size around the boundary, whatever fits without a refusal fits with it
  for (const n of [150, 180, 185, 187, 188, 189, 190, 191, 195, 250]) {
    w.paused = pausedRows(n, 'coder-p', 'blocked', '-0123456789abcdef')
    w.rows = []
    const before = await idsIn(w)
    w.rows = [refusedRow('shape01'), refusedRow('shape02')]
    const after = await idsIn(w)
    for (const row of w.paused) if (before.includes(row.id)) expect(after).toContain(row.id)
    const hidden = w.paused.filter(row => !after.includes(row.id)).length
    if (hidden > 0) expect(after).toContain(`… ${hidden} more workers waiting for you not shown`)
    if (!after.includes('shape02')) expect(after).toMatch(/… \d+ refused not shown/)
  }
})

// ---- a queue entry refused after the session started ----

test('a queue entry that becomes refused after session start wakes the main once, with its id and reason', async ($, on) => {
  const w = world(on, MAIN)
  w.acceptsAppend = true
  w.rows = [{ id: 'coder-1', status: 'working', role: 'coder' }]
  await $.session.start(start(true))
  await w.clock.advance(5000)
  expect(w.submitted).toEqual([])
  w.rows = [...w.rows, refusedRow('shape01')]
  await w.clock.advance(5000)
  expect(w.submitted.length).toBe(1)
  expect(w.submitted[0]).toContain('shape01 will not start (start refused: the queue entry is not an object)')
  expect(w.submitted[0]).toContain('tp-agent stop shape01')
  expect(w.toasts.length).toBe(1)
  // deduplicated: the same refusal is not announced again
  await w.clock.advance(15000)
  expect(w.submitted.length).toBe(1)
  expect(w.toasts.length).toBe(1)
  // an ordinary queued row is not news
  w.rows = [...w.rows, { id: 'queued-ok', status: 'queued', role: 'coder' }]
  await w.clock.advance(10000)
  expect(w.submitted.length).toBe(1)
  // fixed, then refused again: that is a new refusal
  w.rows = w.rows.filter(row => row.id !== 'shape01')
  await w.clock.advance(5000)
  w.rows = [...w.rows, refusedRow('shape01')]
  await w.clock.advance(5000)
  expect(w.submitted.length).toBe(2)
})

test('a refused row already in the injected index does not alert again; one the index cut does', async ($, on) => {
  const w = world(on, MAIN)
  w.acceptsAppend = true
  w.rows = [refusedRow('shape01'), ...Array.from({ length: 400 }, (_, i) => refusedRow(`cut${String(i).padStart(4, '0')}-0123456789abcdef`))]
  await $.session.start(start(true))
  const text = (await $.classic.SessionStart({ source: 'compact' } as never)).additionalContext?.[0] ?? ''
  expect(text).toContain('shape01')
  expect(text).not.toContain('cut0399-0123456789abcdef')
  await w.clock.advance(5000)
  const woken = w.submitted.join('\n')
  expect(woken).not.toContain('shape01 will not start')
  expect(woken).toContain('cut0399-0123456789abcdef will not start (start refused:')
  expect(woken).not.toContain('cut0000-0123456789abcdef will not start')
})

test('a refusal is delivered after a reload only when it was not acknowledged', async ($, on) => {
  const w = world(on, MAIN)
  const kept: Record<string, unknown> = { notifier: { epoch: 1, isIndexed: true, delivered: ['shape01:refused'], pending: [] } }
  on('state.get', { plugin: 'tmux-pi' }, (_$, e, next) => (e.key in kept ? { value: { value: kept[e.key], version: 1 } } : next(e)))
  on('state.set', { plugin: 'tmux-pi' }, (_$, e, next) => {
    if (!(e.key in kept)) return next(e)
    kept[e.key] = e.value
    return { value: { isSet: true, version: 2 } }
  })
  w.rows = [refusedRow('shape01'), refusedRow('shape02')]
  await $.session.start(start(true))
  await w.clock.advance(5000)
  expect(w.submitted.length).toBe(1)
  expect(w.submitted[0]).toContain('shape02 will not start')
  expect(w.submitted[0]).not.toContain('shape01')
})

test('the wake hint for a paused worker names a pending request', async ($, on) => {
  const w = world(on, MAIN)
  await $.session.start(start(true))
  w.rows = [
    { id: 'coder-h1', role: 'coder', status: 'paused', result_status: null, capability_request: { status: 'pending' }, permission: null },
    { id: 'coder-h2', role: 'coder', status: 'paused', result_status: 'blocked', capability_request: null, permission: { status: 'pending' } },
    { id: 'coder-h3', role: 'coder', status: 'paused', result_status: null, capability_request: { status: 'answered' }, permission: null },
    { id: 'coder-h4', role: 'coder', status: 'paused', result_status: 'blocked', capability_request: null, capability_pending: true, permission: null },
  ]
  await w.clock.advance(5000)
  const sent = w.submitted.join('\n')
  expect(sent).toContain('coder-h1 replied without a result status (capability request pending): tp-agent inspect coder-h1')
  expect(sent).toContain('coder-h2 is paused (permission prompt pending): tp-agent inspect coder-h2')
  expect(sent).toContain('coder-h4 is paused (capability approval pending): tp-agent inspect coder-h4')
  expect(sent).toContain('coder-h3 replied without a result status: tp-agent inspect coder-h3')
})

// Ids-only tier: a saturated protected group keeps the mark of a pending-capability.json task.
test('ids-only rows keep the capability approval mark', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = busyRows(100)
  w.paused = Array.from({ length: 300 }, (_, i) => ({
    id: `coder-f${String(i).padStart(4, '0')}-0123456789abcdef`, role: 'coder', harness: 'pi', status: 'paused', result_status: 'blocked',
    capability_request: null, capability_pending: true, permission: null,
  }))
  await $.session.start(start(true))
  const text = (await $.classic.SessionStart({ source: 'compact' } as never)).additionalContext?.[0] ?? ''
  expect(text.length <= 6000).toBe(true)
  expect(text).toContain('coder-f0000-0123456789abcdef [capability approval pending]')
})

// ---- the durable work block (tp-agent work ...) ----
const summary = (id: string, extra: Record<string, unknown> = {}) => ({ id, title: `Title of ${id}`, status: 'active', updated: '2026-10-15T10:00:00Z', next: '', sessions: [], ...extra })
const workBlock = (extra: Record<string, unknown> = {}) => ({
  attached: [], candidates: [], others: 0, decisions: { open: 0, blocking: 0, chars: 1200, items: [] }, notes: { recent: [], important: [] }, checkpoint: null, ...extra,
})
const decisionItems = (n: number, blockingEvery = 0) =>
  Array.from({ length: n }, (_, i) => ({ id: `ws-${String(i).padStart(3, '0')}/D1`, workspace: `ws-${String(i).padStart(3, '0')}`, priority: i % 3 === 0 ? 'high' : 'normal', blocking: blockingEvery > 0 && i % blockingEvery === 0 }))
const injected = async ($: never) => ((await ($ as { classic: { SessionStart: (e: never) => Promise<{ additionalContext?: string[] }> } }).classic.SessionStart({ source: 'compact' } as never)).additionalContext?.[0] ?? '')

test('work block: attached and candidate workspaces show id, title and next action, and no old objective is needed', async ($, on) => {
  const w = world(on, MAIN)
  w.objective = ''
  w.work = workBlock({
    attached: [summary('import-dmp', { next: 'rerun the loader', sessions: ['run1/coder-1'] })],
    candidates: [summary('other-thing', { status: 'active' })],
    others: 2,
    notes: { recent: [{ id: 'loader-note', title: 'Loader quirks' }], important: [{ id: 'hard-rule', title: 'Never reload twice' }] },
  })
  await $.session.start(start(true))
  const text = await injected($ as never)
  expect(text).toContain('work attached to this session')
  expect(text).toContain('- import-dmp "Title of import-dmp" — next: rerun the loader (run1/coder-1)')
  expect(text).toContain('work in this directory, not attached (tp-agent work attach ID):')
  expect(text).toContain('- other-thing "Title of other-thing"')
  expect(text).toContain('+2 other active workspaces: tp-agent work list')
  expect(text).toContain('notes: loader-note "Loader quirks"')
  expect(text).toContain('important notes: hard-rule "Never reload twice"')
  expect(text.indexOf('work attached') < text.indexOf('active:')).toBe(true)
})

test('no work key leaves the index text exactly as it was', async ($, on) => {
  const w = world(on, MAIN)
  await $.session.start(start(true))
  expect(await injected($ as never)).toBe(
    [
      'tmux-pi task index (injected; run `tp-agent index` only if this is missing):',
      'objective: ship it',
      'replied without a result status: none',
      'paused: none',
      'needs input: none',
      'active: none',
      'pending: none',
    ].join('\n'),
  )
  expect(w.ran.length > 0).toBe(true)
})

test('work block: a work_error is one line and the rest of the index is intact', async ($, on) => {
  const w = world(on, MAIN)
  w.workError = 'cannot read the store'
  await $.session.start(start(true))
  const text = await injected($ as never)
  expect(text).toContain('work context not read: cannot read the store')
  expect(text).toContain('objective: ship it')
})

test('work block: unreadable work files are one short line beside the valid workspaces', async ($, on) => {
  const w = world(on, MAIN)
  w.work = workBlock({ attached: [summary('good-one')], skipped: { count: 2 } })
  await $.session.start(start(true))
  const text = await injected($ as never)
  expect(text).toContain('- good-one "Title of good-one"')
  expect(text).toContain('2 unreadable work files; tp-agent work list')
})

test('work block: a work_error never pushes a saturated index past the cap or takes room from the paused rows', async ($, on) => {
  const w = world(on, MAIN)
  w.objective = 'o'.repeat(3000)
  w.rows = busyRows(400)
  w.workError = 'e'.repeat(400)
  await $.session.start(start(true))
  let shownAtLeastOnce = false
  let leftOutAtLeastOnce = false
  // Sizes around where the protected tiers use up the whole room: the error line is kept whole when it fits and left out when it does not.
  for (let n = 0; n <= 700; n += 7) {
    w.unstatused = pausedRows(Math.ceil(n / 2), 'coder-u', null, '-0123456789abcdef')
    w.paused = pausedRows(Math.floor(n / 2), 'coder-p', 'blocked', '-0123456789abcdef')
    const text = await injected($ as never)
    expect(text.length <= 6000).toBe(true)
    const error = `work context not read: ${'e'.repeat(159)}…`
    const lines = text.split('\n').filter(line => line.startsWith('work context not read'))
    expect(lines.every(line => line === error)).toBe(true)
    if (lines.length > 0) shownAtLeastOnce = true
    else leftOutAtLeastOnce = true
    // the error line is optional: every paused worker is still named or counted
    const missing = [...w.unstatused, ...w.paused].filter(row => !text.includes(row.id)).length
    if (missing > 0) expect(text).toContain(`… ${missing} more workers waiting for you not shown; run tp-agent list to see them`)
  }
  expect(shownAtLeastOnce && leftOutAtLeastOnce).toBe(true)
})

test('work decisions: three open are all listed, blocking first, with the command trailer and no question text', async ($, on) => {
  const w = world(on, MAIN)
  const items = [
    { id: 'a/D1', workspace: 'a', priority: 'low', blocking: false },
    { id: 'b/D2', workspace: 'b', priority: 'normal', blocking: true },
    { id: 'c/D1', workspace: 'c', priority: 'high', blocking: false },
  ]
  w.work = workBlock({ decisions: { open: 3, blocking: 1, chars: 1200, items: items.map(item => ({ ...item, question: 'SECRET QUESTION TEXT' })) } })
  await $.session.start(start(true))
  const text = await injected($ as never)
  expect(text).toContain('decisions: 3 open (1 blocking)\n- b/D2 [normal] blocking\n- c/D1 [high]\n- a/D1 [low]\ntp-agent work decisions (every question and detail)')
  expect(text).not.toContain('SECRET QUESTION TEXT')
})

test('work decisions: 300 open give blocking-first ids that fit, a +N more count and the command, within both caps', async ($, on) => {
  const w = world(on, MAIN)
  w.rows = busyRows(400)
  const items = decisionItems(300, 50)
  w.work = workBlock({ decisions: { open: 300, blocking: 6, chars: 600, items } })
  await $.session.start(start(true))
  const text = await injected($ as never)
  expect(text.length <= 6000).toBe(true)
  const start_ = text.indexOf('decisions: 300 open (6 blocking)')
  expect(start_ >= 0).toBe(true)
  const block = text.slice(start_, text.indexOf('\n', text.indexOf('tp-agent work decisions', start_)))
  expect(block.length <= 600).toBe(true)
  const ids = block.split('\n').filter(line => line.startsWith('- ')).map(line => line.split(' ')[1])
  expect(ids.length > 5 && ids.length < 300).toBe(true)
  // the six blocking decisions come first
  expect(ids.slice(0, 6).sort()).toEqual(items.filter(item => item.blocking).map(item => item.id).sort())
  expect(block.endsWith(`+${300 - ids.length} more (${0} blocking): tp-agent work decisions`)).toBe(true)
  expect(text).not.toContain(items[299].id)
})

test('work decisions with 300 open and the default room: lines stay within inject_decisions_chars and the text within the index cap', async ($, on) => {
  const w = world(on, MAIN)
  w.objective = 'o'.repeat(1000)
  w.rows = busyRows(400)
  w.paused = pausedRows(60, 'coder-p', 'blocked')
  w.work = workBlock({ attached: [summary('big', { next: 'n'.repeat(300) })], decisions: { open: 300, blocking: 0, chars: 1200, items: decisionItems(300) } })
  await $.session.start(start(true))
  const text = await injected($ as never)
  expect(text.length <= 6000).toBe(true)
  const from = text.indexOf('decisions: 300 open')
  const block = text.slice(from, text.indexOf('\n', text.indexOf('tp-agent work decisions', from)))
  expect(block.length <= 1200).toBe(true)
  expect(block).toContain('more (0 blocking): tp-agent work decisions')
  // paused workers keep their priority over the work block's optional lines
  for (const row of w.paused) expect(text).toContain(row.id)
})

test('work block: paused workers keep their rows when the work block is large, and the decisions minimum still shows', async ($, on) => {
  const w = world(on, MAIN)
  w.objective = 'o'.repeat(3000)
  w.paused = pausedRows(500, 'coder-p', 'blocked', '-0123456789abcdef')
  w.work = workBlock({
    attached: [summary('big', { next: 'n'.repeat(300) })],
    notes: { recent: Array.from({ length: 3 }, (_, i) => ({ id: `note-${i}`, title: 't'.repeat(100) })), important: [] },
    decisions: { open: 300, blocking: 300, chars: 1200, items: decisionItems(300, 1) },
  })
  await $.session.start(start(true))
  const text = await injected($ as never)
  expect(text.length <= 6000).toBe(true)
  expect(text).toContain('decisions: 300 open (300 blocking)')
  expect(text).toContain('tp-agent work decisions')
  expect(w.paused.filter(row => text.includes(row.id)).length > 50).toBe(true)
  expect(text).toMatch(/… \d+ more workers waiting for you not shown/)
})
