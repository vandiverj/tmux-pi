// /refresh-status against the actual extension with a mock ExtensionAPI and stub scripts; no tmux, model, or session.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import assert from 'node:assert/strict';

const root = path.resolve(import.meta.dirname, '..');
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tp-orch-refresh-'));
fs.writeFileSync(path.join(tmp, 'package.json'), '{"type":"module"}\n');
const source = fs.readFileSync(path.join(root, 'agent/extensions/orchestration.ts'), 'utf8');
const typeImport = 'import { Type } from "@earendil-works/pi-ai";';
const limit = 'const REFRESH_TIMEOUT_MS = 15_000;';
assert(source.includes(typeImport));
assert(source.includes(limit), 'refresh timeout is 15s');
assert(source.includes('{ timeout: 180_000') === false && source.includes('timeout = 180_000'), 'other calls keep 180s');
const stubType = 'const Type = new Proxy({}, { get: () => (...a) => a });';
fs.writeFileSync(path.join(tmp, 'orchestration.ts'), source.replace(typeImport, stubType));
fs.writeFileSync(path.join(tmp, 'orchestration-fast.ts'),
  source.replace(typeImport, stubType).replace(limit, 'const REFRESH_TIMEOUT_MS = 300;'));
const factory = (await import(pathToFileURL(path.join(tmp, 'orchestration.ts')).href)).default;
const fast = (await import(pathToFileURL(path.join(tmp, 'orchestration-fast.ts')).href)).default;

const bin = path.join(tmp, 'bin');
fs.mkdirSync(bin);
const script = path.join(bin, 'tp-refresh-status');
const log = path.join(tmp, 'calls.log');
const stub = body => { fs.writeFileSync(script, `#!/bin/sh\necho "argc=$#" >> '${log}'\n${body}\n`); fs.chmodSync(script, 0o755); };
const calls = () => fs.existsSync(log) ? fs.readFileSync(log, 'utf8').trim().split('\n') : [];

const model = [];
function load(make = factory) {
  const commands = {}, tools = {};
  make({ registerCommand: (name, d) => { commands[name] = d; }, registerTool: d => { tools[d.name] = d; },
    on: () => model.push('on'), sendUserMessage: x => model.push(x), sendMessage: x => model.push(x),
    appendEntry: x => model.push(x) });
  return { commands, tools };
}
const main = { TMUX: 'fake', PI_CODING_AGENT_DIR: path.join(tmp, 'agent') };
function env(values) {
  for (const key of ['TP_TASK_DIR', 'TMUX', 'PI_CODING_AGENT_DIR']) delete process.env[key];
  Object.assign(process.env, values);
}
async function invoke(commands, args = '') {
  const notices = [];
  const ui = new Proxy({}, { get: (_t, name) => name === 'notify' ? (text, type) => notices.push([text, type])
    : () => { throw new Error(`unexpected ui.${String(name)}`); } });
  await commands['refresh-status'].handler(args, { ui, hasUI: true, cwd: tmp });
  return notices;
}

// Main only: a worker, a session outside tmux, or one without the agent dir registers nothing.
for (const values of [{ ...main, TP_TASK_DIR: path.join(tmp, 'task') }, { PI_CODING_AGENT_DIR: main.PI_CODING_AGENT_DIR }, { TMUX: 'fake' }]) {
  env(values);
  const { commands, tools } = load();
  assert.deepEqual([Object.keys(commands), Object.keys(tools)], [[], []], JSON.stringify(values));
}
env(main);
const { commands, tools } = load();
assert.deepEqual(Object.keys(commands), ['refresh-status']);
assert.deepEqual(Object.keys(tools).sort(), ['dispatch_role', 'task_collect', 'task_index']);
assert.equal(typeof commands['refresh-status'].description, 'string');

// Success: the script next to the agent dir runs once with no arguments; its output is the notice.
stub("echo \"refreshed status pane %2 in session 'tp'\"");
assert.deepEqual(await invoke(commands), [["refreshed status pane %2 in session 'tp'", 'info']]);
// Command arguments are never forwarded (no shell, fixed argv).
await invoke(commands, '; touch pwned $(id) --flag');
assert.deepEqual(calls(), ['argc=0', 'argc=0']);
assert.equal(fs.existsSync(path.join(tmp, 'pwned')), false);
stub('exit 0');
assert.deepEqual(await invoke(commands), [['Status pane refreshed', 'info']]);

// Failure: the script's stderr is shown as an error and the handler does not throw.
stub("echo \"tp: session 'x' has no recorded status pane\" >&2; exit 1");
assert.deepEqual(await invoke(commands), [["Status refresh failed: tp: session 'x' has no recorded status pane", 'error']]);
fs.rmSync(script);
const [[missing, kind]] = await invoke(commands);
assert.equal(kind, 'error');
assert.match(missing, /^Status refresh failed: .*ENOENT/);

// Timeout: a hung script is killed and reported (limit shortened only in this copy).
stub('exec sleep 20');
const started = Date.now();
assert.deepEqual(await invoke(load(fast).commands), [['Status refresh failed: timed out after 0.3s', 'error']]);
assert(Date.now() - started < 5000, 'hung script was not awaited');

assert.deepEqual(model, [], 'no model-facing message, entry, or event hook');
fs.rmSync(tmp, { recursive: true, force: true });
console.log('ok');
