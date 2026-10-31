// The reporter's result.md drops a leading <think> block but keeps the answer; synthetic data, mock ExtensionAPI.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import assert from 'node:assert/strict';

const root = path.resolve(import.meta.dirname, '..');
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tp-reporter-think-'));
fs.writeFileSync(path.join(tmp, 'package.json'), '{"type":"module"}\n');
fs.copyFileSync(path.join(root, 'agent/extensions/reporter.ts'), path.join(tmp, 'reporter.ts'));
fs.cpSync(path.join(root, 'agent/extensions/lib'), path.join(tmp, 'lib'), { recursive: true });
const mod = await import(pathToFileURL(path.join(tmp, 'reporter.ts')).href);

function result(text, n) {
  const task = path.join(tmp, 'task' + n);
  fs.mkdirSync(task);
  delete process.env.TP_STATE_DIR;
  process.env.TP_TASK_DIR = task;
  process.env.TP_TASK_ID = 'w' + n;
  const h = {};
  mod.default({ on(name, fn) { h[name] = fn; } });
  h.session_start({}, { model: { provider: 'p', id: 'a' } });
  h.message_end({ message: { role: 'assistant', provider: 'p', model: 'a', content: [{ type: 'text', text }], usage: { input: 1, output: 1 } } });
  h.agent_settled({});
  return fs.readFileSync(path.join(task, 'result.md'), 'utf8');
}

assert.equal(result('<think>plan it\nstep</think>\n\nDone: changed a.py', 1), 'Done: changed a.py\n');
assert.equal(result('  <think>x</think>Answer', 2), 'Answer\n');
assert.equal(result('Answer first <think>kept</think> and more', 3), 'Answer first <think>kept</think> and more\n');
assert.equal(result('<think>a</think>One <think>b</think> two', 4), 'One <think>b</think> two\n');
assert.equal(result('No tags here', 5), 'No tags here\n');
console.log('ok');
