// Pi reporter by-model usage against the actual extension with a mock ExtensionAPI; synthetic data only.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import assert from 'node:assert/strict';

const root = path.resolve(import.meta.dirname, '..');
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tp-reporter-'));
fs.writeFileSync(path.join(tmp, 'package.json'), '{"type":"module"}\n');
fs.copyFileSync(path.join(root, 'agent/extensions/reporter.ts'), path.join(tmp, 'reporter.ts'));
fs.cpSync(path.join(root, 'agent/extensions/lib'), path.join(tmp, 'lib'), { recursive: true });
const mod = await import(pathToFileURL(path.join(tmp, 'reporter.ts')).href);

// --drive TASK_DIR TASK_ID (tests/test_contract.py): the real reporter, driven one event per stdin line, each a JSON
// {"on": name, "event": {...}} or {"on": "message_end", "text": "..."} for an assistant reply; "ok" is printed after each,
// once its writes are on disk. The process is the worker: a session_start line restarts it (a new extension instance).
if (process.argv[2] === '--drive') {
  process.env.TP_TASK_DIR = process.argv[3];
  process.env.TP_TASK_ID = process.argv[4];
  delete process.env.TP_STATE_DIR;
  let handlers = {};
  const readline = await import('node:readline');
  for await (const line of readline.createInterface({ input: process.stdin })) {
    if (!line.trim()) continue;
    const { on, event, text } = JSON.parse(line);
    if (on === 'session_start') {
      handlers = {};
      mod.default({ on(name, fn) { handlers[name] = fn; } });
    }
    const arg = on === 'message_end' ? { message: { role: 'assistant', content: [{ type: 'text', text }] } } : event ?? {};
    handlers[on]?.(arg, {});
    process.stdout.write('ok\n');
  }
  fs.rmSync(tmp, { recursive: true, force: true });
  process.exit(0);
}
const task = path.join(tmp, 'task');
fs.mkdirSync(task);
const state = () => JSON.parse(fs.readFileSync(path.join(task, 'state.json'), 'utf8'));

function start(model) {
  delete process.env.TP_STATE_DIR;
  process.env.TP_TASK_DIR = task;
  process.env.TP_TASK_ID = 'w1';
  const h = {};
  mod.default({ on(name, fn) { h[name] = fn; } });
  h.session_start({}, { model });
  return h;
}
const msg = (provider, model, input, output, cost) => ({ message: { role: 'assistant', provider, model, content: [], usage: { input, output, cacheRead: input * 10, cacheWrite: 1, cost: { total: cost } } } });

let h = start({ provider: 'p', id: 'a' });
h.message_end(msg('p', 'a', 1, 5, 0.5));
h.model_select({ model: { provider: 'q', id: 'b' } });
h.message_end(msg('q', 'b', 2, 7, 0.25));
// Event model wins over the selected one (a message finalized after a switch keeps its producer).
h.model_select({ model: { provider: 'p', id: 'a' } });
h.message_end(msg('q', 'b', 3, 1, 0.25));
h.message_end({ message: { role: 'user', content: 'x' } });
let s = state();
assert.equal(s.turns, 3);
assert.deepEqual(s.tokens, { input: 6, output: 13, cacheRead: 60, cacheWrite: 3 });
assert.equal(s.cost, 1);
assert.deepEqual(s.byModel, {
  'p/a': { input: 1, output: 5, cacheRead: 10, cacheWrite: 1, cost: 0.5 },
  'q/b': { input: 5, output: 8, cacheRead: 50, cacheWrite: 2, cost: 0.5 } });
assert.equal(s.model, 'p/a');

// Message without model falls back to the selected model; with neither, only overall totals grow.
h.message_end({ message: { role: 'assistant', content: [], usage: { input: 1, output: 1 } } });
assert.equal(state().byModel['p/a'].input, 2);

// Restart in the same task dir restores history instead of resetting.
h = start({ provider: 'q', id: 'b' });
assert.equal(state().turns, 4);
h.message_end(msg('q', 'b', 1, 1, 0));
s = state();
assert.equal(s.tokens.input, 8);
assert.equal(s.byModel['q/b'].input, 6);
assert.equal(s.byModel['p/a'].input, 2);

// A different task id or corrupt state is not inherited.
fs.writeFileSync(path.join(task, 'state.json'), '{not json');
h = start(undefined);
assert.equal(state().turns, 0);
fs.writeFileSync(path.join(task, 'state.json'), JSON.stringify({ ...s, id: 'other' }));
h = start(undefined);
assert.equal(state().tokens.input, 0);

// HB4a: a pi worker that settles waiting with a question (no RESULT_STATUS line) opens one input request through
// lib/hub_records.py; the same reply seen again is the same id; its next start resolves it (left-state); a reply that
// ends the task opens none.
process.env.TP_HUB_RECORDS_PY = path.join(root, 'lib/hub_records.py');
const said = (text) => ({ message: { role: 'assistant', content: [{ type: 'text', text }] } });
const reqs = () => {
  const d = path.join(task, 'requests');
  return fs.existsSync(d) ? fs.readdirSync(d).filter((f) => f.endsWith('.json')).map((f) => JSON.parse(fs.readFileSync(path.join(d, f), 'utf8'))) : [];
};
h = start(undefined);
h.agent_start();
assert.equal(fs.existsSync(path.join(task, 'requests')), false); // nothing to resolve: no process, no directory
h.message_end(said('Which of the two files should I change?'));
h.agent_settled();
assert.equal(reqs().length, 1);
const [asked] = reqs();
assert.match(asked.id, /^req-[0-9A-HJKMNP-TV-Z]{26}$/);
assert.deepEqual([asked.kind, asked.status, asked.summary], ['input', 'open', 'Which of the two files should I change?']);
h.agent_settled(); // the same reply again: the same request
assert.deepEqual(reqs().map((r) => r.id), [asked.id]);
h.agent_start();
assert.deepEqual(reqs().map((r) => [r.id, r.status, r.resolution.how]), [[asked.id, 'resolved', 'left-state']]);
h.message_end(said('Done.\nRESULT_STATUS: complete'));
h.agent_settled();
assert.deepEqual(reqs().map((r) => r.status), ['resolved']); // a finished task waits for nobody

const kinds = fs.readFileSync(path.join(task, 'events.jsonl'), 'utf8').trim().split('\n').map((l) => JSON.parse(l).kind);
assert.deepEqual(kinds, ['request.opened', 'request.resolved']);
// HB4a round 5 rule 1: promptTurn counts agent_start; completedTurn is the highest turn whose own settle wrote a statused
// reply. Above: turn 1 asked a question (not completed), turn 2 completed.
assert.deepEqual([state().promptTurn, state().completedTurn], [2, 2]);
h.agent_start(); // turn 3 withdraws result.md, never lowers completedTurn
assert.deepEqual([state().promptTurn, state().completedTurn, fs.existsSync(path.join(task, 'result.md'))], [3, 2, false]);
h.message_end(said('Still looking.'));
h.agent_settled(); // an unstatused settle leaves it
assert.deepEqual([state().promptTurn, state().completedTurn], [3, 2]);
h.agent_settled(); // a second settle of the same turn too
assert.equal(state().completedTurn, 2);
h = start(undefined); // a restart carries both
assert.deepEqual([state().promptTurn, state().completedTurn], [3, 2]);
h.agent_start();
h.message_end(said('Now done.\n**RESULT_STATUS:** complete'));
h.agent_settled();
assert.deepEqual([state().promptTurn, state().completedTurn], [4, 4]);
console.log('ok');
