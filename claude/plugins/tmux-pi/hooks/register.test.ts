import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

const ROOT = '/opt/tmux-pi'
const AGENT = `${ROOT}/bin/tp-agent`
const MAIN = { TP_ROOT: ROOT, TP_MODE: 'orchestrator' }
const BAND = { hasSurvey: false, isWorking: false, maxRows: 5, bodyColumns: 80 }

type Row = { id: string; status: string; role?: string; capability_request?: unknown; result_status?: string | null }

// Stands for the engine beneath the plugin: answers the environment, the clock, the
// session and every host command from memory, and records what the module did. The kit
// has no stand-in for a plugin's own `$.session.append`, so the index row appended at
// start is not asserted here; the text it carries is, through the compaction route.
function world(on: On, env: Record<string, string>) {
  const w = {
    rows: [] as Row[],
    isFailing: false,
    isIndexFailing: false,
    collectStatus: 'completed',
    collectResult: undefined as string | undefined,
    objective: 'ship it',
    paused: undefined as Record<string, unknown>[] | undefined,
    unstatused: undefined as Record<string, unknown>[] | undefined,
    isRejecting: false,
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
    if (tool !== AGENT) stdout = `viewing ${verb}`
    else if (verb === 'list') stdout = w.listOut ?? JSON.stringify(w.rows)
    else if (verb === 'index') stdout = JSON.stringify({ objective: w.objective, active: w.rows, pending: [], paused: w.paused ?? [], unstatused: w.unstatused ?? [], recent: [] })
    else if (verb === 'collect') stdout = JSON.stringify({ id, status: w.collectStatus, result_status: w.collectResult })
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
  expect(w.ran.slice(0, 2)).toEqual([[AGENT, 'index', '--no-tick'], [AGENT, 'list']])
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

// The kit cannot store a plugin's own session append, so here the index never reaches
// the conversation; that is the refused-append path, where waiting tasks must be announced.
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
