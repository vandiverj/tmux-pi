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
console.log('ok');
