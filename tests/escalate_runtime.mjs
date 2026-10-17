// Isolated tool-error handoff against the actual extension, fake CLI and temporary task dirs.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import assert from 'node:assert/strict';

const root = path.resolve(import.meta.dirname, '..');
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tp-escalate-'));
const sourceDir = path.join(tmp, 'tasks', 'old');
const targetDir = path.join(tmp, 'tasks', 'new');
fs.mkdirSync(sourceDir, { recursive: true });
fs.mkdirSync(targetDir);
const original = { id: 'old', worker: 'pi', workdir: tmp, decision: { review: 'required', scope: 'brief' },
  blocked_by: ['task:prior'], contract: { kind: 'task', artifact: '/old/report.md' },
  preflight: { schema: 'preflight', no_edits: true }, handshake_id: 'h1' };
fs.writeFileSync(path.join(sourceDir, 'meta.json'), JSON.stringify(original));
fs.writeFileSync(path.join(sourceDir, 'request.md'), 'Original task');
for (const name of ['packet.md', 'main-contract.json', 'policy-decision.json', 'context-manifest.json'])
  fs.writeFileSync(path.join(sourceDir, name), name === 'main-contract.json' ? '{"no_edits":false}' : '{}');
fs.writeFileSync(path.join(targetDir, 'meta.json'), JSON.stringify({ id: 'new', worker: 'claude', pane: '%1', contract: { artifact: '/new/report.md' } }));
const config = path.join(tmp, 'local.json');
fs.writeFileSync(config, JSON.stringify({ escalation: { models: ['local/qwen'], after_consecutive_errors: 0 } }));
process.env.TP_TASK_DIR = sourceDir;
process.env.TP_TASK_ID = 'old';
process.env.PI_CODING_AGENT_DIR = path.join(tmp, 'agent');
process.env.TP_LOCAL_CONFIG = config;
const calls = [];
const source = fs.readFileSync(path.join(root, 'agent/extensions/escalate.ts'), 'utf8');
fs.cpSync(path.join(root, 'agent/extensions/lib'), path.join(tmp, 'lib'), { recursive: true });
fs.writeFileSync(path.join(tmp, 'escalate.ts'), source.replace('import { execFileSync } from "node:child_process";', 'const execFileSync = globalThis.__fakeExecFileSync;'));
globalThis.__fakeExecFileSync = (cmd, args) => {
  calls.push({ cmd: path.basename(cmd), args });
  switch (path.basename(cmd)) {
    case 'tp-contract': return args[0] === 'escalate' ? JSON.stringify({ target: { worker: 'claude', model: 'claude-sonnet-5-5', thinking: 'low' }, record: {} }) : '';
    case 'tp-spawn': return 'new\n';
    default: return '';
  }
};
const handlers = {};
const extension = (await import(pathToFileURL(path.join(tmp, 'escalate.ts')).href)).default;
extension({ on(name, fn) { handlers[name] = fn; } });
let aborted = false;
handlers.model_select({ model: { provider: 'local', id: 'qwen' } });
handlers.tool_execution_start({ toolCallId: '1', toolName: 'read', args: {} });
handlers.tool_execution_end({ toolCallId: '1', toolName: 'read', isError: true, result: 'failed' },
  { model: { provider: 'local', id: 'qwen' }, abort() { aborted = true; }, shutdown() {} });
assert.equal(aborted, true);
const spawn = calls.find(c => c.cmd === 'tp-spawn').args;
for (const flag of ['--preflight', '--main-contract', '--policy-decision', '--context-manifest', '--kind', '--decision-json']) assert(spawn.includes(flag), flag);
assert.equal(JSON.parse(fs.readFileSync(spawn[spawn.indexOf('--preflight') + 1], 'utf8')).no_edits, true);
assert.equal(JSON.parse(fs.readFileSync(path.join(sourceDir, 'preflight.json'), 'utf8')).no_edits, true);
assert.equal(spawn.at(-1), 'Original task');
assert.equal(spawn[spawn.indexOf('--workdir') + 1], tmp);
const target = JSON.parse(fs.readFileSync(path.join(targetDir, 'meta.json')));
assert.equal(target.decision.review, 'required');
assert.deepEqual(target.blocked_by, ['task:prior']);
assert.deepEqual(target.preflight, { schema: 'preflight', no_edits: true });
assert.equal(target.contract.artifact, '/new/report.md');
assert.equal(target.escalated_from.id, 'old');
assert.equal(JSON.parse(fs.readFileSync(path.join(sourceDir, 'meta.json'))).escalated_to.id, 'new');
assert.equal(fs.readdirSync(targetDir).some(name => name.endsWith('.tmp')), false);
// A required-review foreground worker without a contract must gate the target at spawn.
fs.writeFileSync(path.join(sourceDir, 'meta.json'), JSON.stringify({ ...original, contract: undefined }));
fs.writeFileSync(path.join(targetDir, 'meta.json'), JSON.stringify({ id: 'new', worker: 'claude', pane: '%1' }));
const plain = {};
(await import(pathToFileURL(path.join(tmp, 'escalate.ts')).href + '?plain')).default({ on(name, fn) { plain[name] = fn; } });
plain.model_select({ model: { provider: 'local', id: 'qwen' } });
plain.tool_execution_start({ toolCallId: 'plain', toolName: 'read', args: {} });
plain.tool_execution_end({ toolCallId: 'plain', toolName: 'read', isError: true, result: 'failed' },
  { model: { provider: 'local', id: 'qwen' }, abort() {}, shutdown() {} });
const plainSpawn = calls.filter(c => c.cmd === 'tp-spawn').at(-1).args;
assert(!plainSpawn.includes('--kind'));
assert.equal(JSON.parse(plainSpawn[plainSpawn.indexOf('--decision-json') + 1]).review, 'required');
assert(!plainSpawn.includes('--lane') && !plainSpawn.includes('--lane-replaces'));
// A lane worker hands the lane to its target: lane, step, preamble source and role are carried, and the target replaces this task.
fs.writeFileSync(path.join(sourceDir, 'meta.json'), JSON.stringify({ ...original, contract: undefined, lane: 'alpha', lane_step: 's1', preamble_from: '/repo', role: 'coder' }));
fs.writeFileSync(path.join(targetDir, 'meta.json'), JSON.stringify({ id: 'new', worker: 'claude', pane: '%1' }));
const laneHandlers = {};
(await import(pathToFileURL(path.join(tmp, 'escalate.ts')).href + '?lane')).default({ on(name, fn) { laneHandlers[name] = fn; } });
laneHandlers.model_select({ model: { provider: 'local', id: 'qwen' } });
laneHandlers.tool_execution_start({ toolCallId: 'lane', toolName: 'read', args: {} });
laneHandlers.tool_execution_end({ toolCallId: 'lane', toolName: 'read', isError: true, result: 'failed' },
  { model: { provider: 'local', id: 'qwen' }, abort() {}, shutdown() {} });
const laneSpawn = calls.filter(c => c.cmd === 'tp-spawn').at(-1).args;
const after = (flag) => laneSpawn[laneSpawn.indexOf(flag) + 1];
assert.deepEqual([after('--lane'), after('--lane-step'), after('--lane-replaces'), after('--preamble-from'), after('--role')],
  ['alpha', 's1', 'old', '/repo', 'coder']);
// A rename failure must leave the original target JSON intact and kill the orphan.
fs.writeFileSync(path.join(sourceDir, 'meta.json'), JSON.stringify(original));
const targetBefore = JSON.stringify({ id: 'new', worker: 'claude', pane: '%1' });
fs.writeFileSync(path.join(targetDir, 'meta.json'), targetBefore);
// The rename lives in the shared state-write helper, so the failing variant gets its own patched copy of it.
const failDir = path.join(tmp, 'fail');
fs.mkdirSync(path.join(failDir, 'lib'), { recursive: true });
const helper = fs.readFileSync(path.join(root, 'agent/extensions/lib/state-write.ts'), 'utf8');
assert(helper.includes('renameSync(temp, file);'), 'state-write rename call changed; update the failure harness');
fs.writeFileSync(path.join(failDir, 'lib/state-write.ts'), helper.replace('renameSync(temp, file);', 'globalThis.__failRename(temp, file);'));
fs.writeFileSync(path.join(failDir, 'escalate-fail.ts'), source
  .replace('import { execFileSync } from "node:child_process";', 'const execFileSync = globalThis.__fakeExecFileSync;'));
globalThis.__failRename = (temp, file) => {
  if (file.endsWith('/meta.json')) throw new Error('simulated metadata rename failure');
  fs.renameSync(temp, file);
};
const failed = {};
(await import(pathToFileURL(path.join(failDir, 'escalate-fail.ts')).href)).default({ on(name, fn) { failed[name] = fn; } });
failed.model_select({ model: { provider: 'local', id: 'qwen' } });
failed.tool_execution_start({ toolCallId: '2', toolName: 'read', args: {} });
failed.tool_execution_end({ toolCallId: '2', toolName: 'read', isError: true, result: 'failed' },
  { model: { provider: 'local', id: 'qwen' }, abort() { throw new Error('must not abort'); }, shutdown() {} });
assert.equal(fs.readFileSync(path.join(targetDir, 'meta.json'), 'utf8'), targetBefore);
assert(calls.some(c => c.cmd === 'tp-kill' && c.args[0] === 'new'));
assert.equal(fs.readdirSync(targetDir).some(name => name.endsWith('.tmp')), false);
// Goal lineage is not transferable after the target pane starts: pause BEFORE spawn.
fs.writeFileSync(path.join(sourceDir, 'meta.json'), JSON.stringify({ ...original,
  goal: { id: 'approved-goal', ticket: 'owned-turn', spec_sha256: 'a'.repeat(64), workdir: tmp } }));
const beforeGoalSpawns = calls.filter(c => c.cmd === 'tp-spawn').length;
const goalHandlers = {};
(await import(pathToFileURL(path.join(tmp, 'escalate.ts')).href + '?goal')).default({ on(name, fn) { goalHandlers[name] = fn; } });
goalHandlers.model_select({ model: { provider: 'local', id: 'qwen' } });
goalHandlers.tool_execution_start({ toolCallId: 'goal', toolName: 'read', args: {} });
goalHandlers.tool_execution_end({ toolCallId: 'goal', toolName: 'read', isError: true, result: 'failed' },
  { model: { provider: 'local', id: 'qwen' }, abort() { throw new Error('goal must not abort'); }, shutdown() {} });
assert.equal(calls.filter(c => c.cmd === 'tp-spawn').length, beforeGoalSpawns);
assert(calls.some(c => c.cmd === 'tp-goal' && c.args.includes('pause') && c.args.includes('approved-goal')));
assert.match(fs.readFileSync(path.join(sourceDir, 'escalation-error.txt'), 'utf8'), /Goal escalation requires human/);
fs.rmSync(tmp, { recursive: true, force: true });
console.log('escalate handoff OK');
