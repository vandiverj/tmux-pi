// Fake Pi event boundary; no session, tmux, model, or real CLI is started.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import assert from 'node:assert/strict';
const source = fs.readFileSync(path.resolve(import.meta.dirname, '../agent/extensions/goal.ts'), 'utf8');
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tp-goal-extension-'));
fs.writeFileSync(path.join(tmp, 'goal.ts'), source
  .replace('import { execFile } from "node:child_process";', 'const execFile = globalThis.__execFile;'));
process.env.TMUX = 'fake';
fs.mkdirSync(path.join(tmp, '.tmux-pi')); // A session's state root always exists (bin/tp creates it).
let state = { status: 'active', spec: { objective: 'Fix parser fully', done: 'All requirements checked', scope: 'parser only', workdir: tmp,
  budget: { turns: 3, tokens: 100, seconds: 300, attempts: 3, cost_micros: 100000 } }, spec_sha256: 'a'.repeat(64), usage: { turns: 0, tokens: 0, cost_micros: 0 }, reason: 'active', inflight: null };
const calls = [];
let settlementArgs = [];
globalThis.__execFile = (_bin, args, opts, cb) => {
  if (_bin === 'tmux') { queueMicrotask(() => cb(null, { stdout: path.join(tmp, '.tmux-pi') + '\n', stderr: '' })); return; }
  const action = args[2]; calls.push(action);
  if (action === 'admit') state.inflight = { ticket: `t-${calls.length}` };
  if (action === 'usage') { state.usage.turns++; state.usage.tokens += 5; state.usage.cost_micros += 10; }
  if (action === 'settle') { settlementArgs = args; state.inflight = null; state.status = 'paused'; }
  if (action === 'reconcile' && state.inflight) state.status = 'suspended';
  queueMicrotask(() => cb(null, { stdout: JSON.stringify(state), stderr: '' }));
};
const factory = (await import(pathToFileURL(path.join(tmp, 'goal.ts')).href)).default;
const handlers = {}, commands = {}, tools = {}, messages = [], entries = [], notices = [];
const pi = { on: (event, cb) => { handlers[event] = cb; }, registerCommand: (name, d) => { commands[name] = d; },
  registerTool: d => { tools[d.name] = d; }, appendEntry: (name, data) => entries.push({ type: 'custom', customType: name, data }),
  sendUserMessage: x => messages.push(x) };
factory(pi);
const ctx = { cwd: tmp, isIdle: () => true, sessionManager: { getBranch: () => entries },
  ui: { notify: (s) => notices.push(s) } };
await commands.goal.handler('attach repair', ctx);
assert.equal(messages.length, 1);
assert.match(messages[0], /Fix parser fully/);
assert.equal(calls.filter(c => c === 'admit').length, 1);
await handlers.message_end({ message: { role: 'assistant', content: [{type: 'text', text: 'Inspected parser.\nGOAL_REPORT: {"outcome":"progress","evidence":"Inspected parser"}'}], usage: { input: 3, output: 2, cost: { total: .00001 } } } }, ctx);
assert.equal(state.usage.tokens, 5);
await handlers.agent_settled({}, ctx);
assert.equal(state.status, 'paused');
assert.deepEqual(settlementArgs.slice(settlementArgs.indexOf('--outcome'), settlementArgs.indexOf('--outcome') + 2), ['--outcome', 'progress']);
assert.equal(messages.length, 1);
state.status = 'active';
await commands.goal.handler('attach repair', ctx);
assert.equal(messages.length, 2);
await handlers.message_end({ message: { role: 'assistant', content: [{ type: 'text', text: 'GOAL_REPORT: {"outcome":"complete","evidence":"done"}' }] } }, ctx);
await handlers.agent_settled({}, ctx);
assert.deepEqual(settlementArgs.slice(settlementArgs.indexOf('--outcome'), settlementArgs.indexOf('--outcome') + 2), ['--outcome', 'safety_block']);
await handlers.session_shutdown();
state.status = 'active'; state.inflight = { ticket: 'orphaned' };
factory({ ...pi, on: (event, cb) => { handlers[event] = cb; } });
handlers.session_start({}, ctx);
await new Promise(r => setTimeout(r, 25));
assert.equal(state.status, 'suspended');
assert.equal(messages.length, 2, 'no restart of an orphaned turn');
handlers.session_shutdown();
fs.rmSync(tmp, { recursive: true, force: true });
console.log('goal runtime fake boundary ok');
