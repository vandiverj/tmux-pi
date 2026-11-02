// pi api_signature / docs_search (agent/extensions/docs.ts) loaded the way pi loads an extension, against the real bin/tp-docs
// and fixture projects. No model, no network. Environment: PI_CODING_AGENT_PACKAGE (installed pi), DOCS_ROOT (repo),
// DOCS_PROJECT (a project with an index for plainpkg), DOCS_JSR_PROJECT (a deno.lock project), DOCS_TASK_DIR, DENO_DIR.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import assert from 'node:assert/strict';
import { linkPiHost } from '../pi_host.mjs';

const root = process.env.DOCS_ROOT;
const pkg = process.env.PI_CODING_AGENT_PACKAGE;
const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'tp-docs-ext-')));
linkPiHost(tmp, pkg);
fs.writeFileSync(path.join(tmp, 'package.json'), '{"type":"module"}\n');
fs.copyFileSync(path.join(root, 'agent/extensions/docs.ts'), path.join(tmp, 'docs.ts'));

process.env.PI_CODING_AGENT_DIR = path.join(root, 'agent');
process.env.TP_TASK_DIR = process.env.DOCS_TASK_DIR;
const mod = await import(pathToFileURL(path.join(tmp, 'docs.ts')).href);
const tools = {};
mod.default({ on() {}, registerTool(tool) { tools[tool.name] = tool; } });

assert.deepEqual(Object.keys(tools).sort(), ['api_signature', 'docs_search']);
for (const tool of Object.values(tools)) assert.equal(tool.parameters.type, 'object');
const text = (result) => result.content.map((block) => block.text).join('\n');
const call = (name, input, cwd) => tools[name].execute('id', input, undefined, undefined, { cwd }).then(text);
const log = () => fs.readFileSync(path.join(process.env.DOCS_TASK_DIR, 'docs-calls.jsonl'), 'utf8').trim().split('\n').map((l) => JSON.parse(l));

const sig = await call('api_signature', { package: 'plainpkg', symbol: 'connect' }, process.env.DOCS_PROJECT);
assert.match(sig, /^\[provenance\] source=node_modules:/);
assert.match(sig, /export declare function connect\(url: string/);
const found = await call('docs_search', { lib: 'plainpkg', query: 'timeout', limit: 2 }, process.env.DOCS_PROJECT);
assert.match(found, /plainpkg@1\.2\.3 docs/);
const jsr = await call('api_signature', { package: 'jsr:@fx/strs', symbol: 'Counter.increment' }, process.env.DOCS_JSR_PROJECT);
assert.match(jsr, /source=deno-cache:/);
assert.match(jsr, /increment\(by: number = 1\): number;/);
assert.ok(!jsr.includes('BODY_MARKER'), 'a body came back');
const miss = await call('api_signature', { package: 'plainpkg', symbol: 'noSuchThing' }, process.env.DOCS_PROJECT);
assert.match(miss, /not found/);

const rows = log();
assert.deepEqual(rows.map((r) => r.tool), ['api_signature', 'docs_search', 'api_signature', 'api_signature']);
assert.deepEqual(rows.map((r) => r.result), ['hit', 'hit', 'hit', 'miss']);

// no task directory: the call fails closed and answers nothing
delete process.env.TP_TASK_DIR;
await assert.rejects(call('api_signature', { package: 'plainpkg', symbol: 'connect' }, process.env.DOCS_PROJECT), /TP_TASK_DIR|log/i);
assert.equal(log().length, 4);
fs.rmSync(tmp, { recursive: true, force: true });
console.log('docs extension runtime ok');
