// ask_local over-cap handling against the actual extension, a stub tp-pi and a stub sensitive classifier. No model calls.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { pathToFileURL } from 'node:url';
import assert from 'node:assert/strict';

const root = path.resolve(import.meta.dirname, '..');
const pkg = process.env.PI_CODING_AGENT_PACKAGE;
const fixtures = path.join(root, 'tests/fixtures/assistant');
const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'tp-assistant-')));
fs.mkdirSync(path.join(tmp, 'ext/node_modules/@earendil-works'), { recursive: true });
fs.symlinkSync(pkg, path.join(tmp, 'ext/node_modules/@earendil-works/pi-coding-agent'));
fs.symlinkSync(path.join(pkg, 'node_modules/@earendil-works/pi-ai'), path.join(tmp, 'ext/node_modules/@earendil-works/pi-ai'));
fs.writeFileSync(path.join(tmp, 'ext/package.json'), '{"type":"module"}\n');
fs.copyFileSync(path.join(root, 'agent/extensions/assistant.ts'), path.join(tmp, 'ext/assistant.ts'));
fs.cpSync(path.join(root, 'agent/extensions/lib'), path.join(tmp, 'ext/lib'), { recursive: true });

// Layout the extension expects: <root>/agent (PI_CODING_AGENT_DIR), <root>/bin/tp-pi.
const home = path.join(tmp, 'home');
fs.mkdirSync(path.join(home, 'agent'), { recursive: true });
fs.mkdirSync(path.join(home, 'bin'));
const script = (name, body) => { const f = path.join(home, 'bin', name); fs.writeFileSync(f, body); fs.chmodSync(f, 0o755); return f; };
script('tp-pi', '#!/bin/sh\ncat "$ASK_FIXTURE"\n');
const classifier = script('classifier', `#!/usr/bin/env node
let d = ''; process.stdin.on('data', (c) => d += c).on('end', () => {
  const p = String(JSON.parse(d).tool_input?.path ?? '');
  console.log(JSON.stringify(p.includes('dummy-secret') || p.includes('secret files') || p.endsWith('/.env') ? { sensitive: true, target: p } : { sensitive: false }));
});
`);
const work = path.join(tmp, 'work');
fs.mkdirSync(path.join(work, 'src'), { recursive: true });
fs.mkdirSync(path.join(work, 'dummy-secret'));
fs.writeFileSync(path.join(work, 'src/alpha.py'), 'def alpha():\n    return 1\n');
fs.writeFileSync(path.join(work, 'src/beta.py'), 'from alpha import alpha\n');
fs.writeFileSync(path.join(work, 'dummy-secret/token.txt'), 'not-a-real-credential\n');
const state = path.join(tmp, 'state');
fs.mkdirSync(state);

for (const key of ['TP_TASK_DIR', 'TP_MODE']) delete process.env[key];
Object.assign(process.env, {
  TMUX: '/tmp/stub,1,0', PI_CODING_AGENT_DIR: path.join(home, 'agent'), TP_STATE_DIR: state, TP_SENSITIVE_READ_BIN: classifier,
});
const tools = {};
(await import(pathToFileURL(path.join(tmp, 'ext/assistant.ts')).href)).default({ on() {}, registerTool(t) { tools[t.name] = t; } });
const ask = async (file) => {
  process.env.ASK_FIXTURE = file;
  const result = await tools.ask_local.execute('1', { task: 'find alpha' }, undefined, undefined, { cwd: work });
  return { text: result.content[0].text, details: result.details };
};
const sha = (file) => 'sha256:' + crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
const evidence = () => fs.existsSync(path.join(state, 'evidence')) ? fs.readdirSync(path.join(state, 'evidence')) : [];

// Under the cap: byte-for-byte what the model said (trimmed, as before), no evidence file.
const under = path.join(fixtures, 'under_cap.md');
const small = await ask(under);
assert.equal(small.text, fs.readFileSync(under, 'utf8').trim());
assert.equal(small.details.manifest, undefined);
assert.deepEqual(evidence(), []);
// Exactly at the cap is still under it.
const exact = path.join(tmp, 'exact.md');
fs.writeFileSync(exact, 'x'.repeat(24_000));
assert.equal((await ask(exact)).text.length, 24_000);
assert.deepEqual(evidence(), []);

// Over the cap.
const long = path.join(tmp, 'long.md');
const lines = Array.from({ length: 2000 }, (_, i) => `line ${i} filler text to push the reply past the cap`).join('\n');
const reply = (fs.readFileSync(path.join(fixtures, 'over_cap_head.md'), 'utf8') + lines).trim();
assert(reply.length > 24_000);
fs.writeFileSync(long, reply);
const big = await ask(long);
const files = evidence();
assert.equal(files.length, 1);
const saved = path.join(state, 'evidence', files[0]);
assert.equal(fs.readFileSync(saved, 'utf8'), reply, 'the full reply is on disk');
assert.equal(fs.statSync(saved).mode & 0o777, 0o600);
const m = big.details.manifest;
assert.equal(m.artifact, saved);
assert.equal(m.truncated, true);
assert.equal(m.bytes_total, Buffer.byteLength(reply));
// The returned text is bounded, cut at a line boundary, and names the artifact and the manifest.
const excerpt = big.text.slice(0, big.text.indexOf('\n[truncated at a line boundary'));
const cutAt = reply.lastIndexOf('\n', 24_000);
assert(reply[cutAt] === '\n' && cutAt <= 24_000 && cutAt > 20_000, 'cut at a line boundary');
assert.equal(excerpt, reply.slice(0, cutAt).replace('dummy-secret/token.txt:1', '[withheld path]'));
assert(big.text.length < 24_000 + 2000);
assert(big.text.includes(saved));
assert(big.text.includes('evidence manifest: ' + JSON.stringify(m)));
// Cited files: line ranges merged per path, hashes of the files as they are on disk.
assert.deepEqual(m.paths, [
  { path: 'src/alpha.py', lines: ['3-5', '4'], content_hash: sha(path.join(work, 'src/alpha.py')) },
  { path: 'src/beta.py', lines: ['10', '20-22'], content_hash: sha(path.join(work, 'src/beta.py')) },
]);
// A sensitive path is counted as withheld: not hashed, not named anywhere in the manifest.
assert.equal(m.withheld, 1);
assert(!JSON.stringify(m).includes('dummy-secret'));
// ...and not in the returned excerpt either: masked with a fixed placeholder (the saved file keeps the original).
assert(!big.text.includes('dummy-secret'), 'withheld path is masked in the excerpt');
assert(big.text.includes('[withheld path]:1 holds a credential.'.replace(':1', '')), 'placeholder stands in');

// A hash reflects an uncommitted edit.
fs.appendFileSync(path.join(work, 'src/alpha.py'), '# edited, not committed\n');
const again = await ask(long);
const alpha = again.details.manifest.paths.find((p) => p.path === 'src/alpha.py');
assert.equal(alpha.content_hash, sha(path.join(work, 'src/alpha.py')));
assert.notEqual(alpha.content_hash, m.paths[0].content_hash);
assert.equal(evidence().length, 2, 'each overflow gets its own file');

// More over-cap replies, each with its own head; the filler pushes the reply past the cap.
const overflow = async (head, tail = lines) => {
  const f = path.join(tmp, `case-${Math.random().toString(16).slice(2)}.md`);
  fs.writeFileSync(f, (head + '\n' + tail).trim());
  const r = await ask(f);
  return { text: r.text, m: r.details.manifest };
};
// F2: a cited name that is not on disk is still classified before it is skipped.
const noEnv = await overflow('Config lives in .env:1 and nothing else.');
assert.equal(noEnv.m.withheld, 1);
assert(!noEnv.text.includes('.env:1'), 'nonexistent sensitive name is masked');
assert.deepEqual(noEnv.m.paths, []);
// F3: the cap on listed paths stays, and what it leaves out is counted.
fs.mkdirSync(path.join(work, 'many'));
const names = Array.from({ length: 55 }, (_, i) => `many/f${i}.txt`);
for (const n of names) fs.writeFileSync(path.join(work, n), n);
const many = await overflow(names.join('\n'));
assert.equal(many.m.paths.length, 50);
assert.equal(many.m.omitted, 5);
assert.equal(many.m.withheld, 0);
// F4: a file past 64 MiB is hashed by streaming, not given a null hash.
const hugeFile = path.join(work, 'src/huge.bin');
fs.writeFileSync(hugeFile, '');
fs.truncateSync(hugeFile, 65 * 1024 * 1024);
fs.appendFileSync(hugeFile, 'tail');
const huge = await overflow('See src/huge.bin:1 for the blob.');
assert.deepEqual(huge.m.paths, [{ path: 'src/huge.bin', lines: ['1'], content_hash: sha(hugeFile) }]);
assert.equal(huge.m.omitted, 0);
fs.rmSync(hugeFile);
// F5: a path with spaces is taken when quoted or backticked, or when it exists; a quoted one that is not there is unresolved.
fs.writeFileSync(path.join(work, 'src/my file.py'), 'x = 1\n');
fs.mkdirSync(path.join(work, 'my dir'));
fs.writeFileSync(path.join(work, 'my dir/b c.py'), 'y = 2\n');
const spaced = await overflow('Bare: see src/my file.py:8 here.\nQuoted: "my dir/b c.py:3-4" and `src/gone file.py:2`.');
assert.deepEqual(spaced.m.paths, [
  { path: 'src/my file.py', lines: ['8'], content_hash: sha(path.join(work, 'src/my file.py')) },
  { path: 'my dir/b c.py', lines: ['3-4'], content_hash: sha(path.join(work, 'my dir/b c.py')) },
]);
assert.equal(spaced.m.unresolved, 1);
// F6: one line longer than the cap is cut at the cap and labelled so.
const oneLine = await overflow('y'.repeat(30_000), '');
assert(oneLine.text.includes('truncated, cut mid-line'));
assert(!oneLine.text.includes('line boundary'));
assert(oneLine.text.startsWith('y'.repeat(24_000) + '\n['));

// Retry cases. Each is recorded so one run shows every failure, not just the first.
const failures = [];
const check = async (name, fn) => { try { await fn(); console.log('pass', name); } catch (e) { failures.push(name); console.log('FAIL', name, '-', String(e.message).split('\n')[0]); } };
const withEnv = async (vars, fn) => {
  const saved = Object.fromEntries(Object.keys(vars).map((k) => [k, process.env[k]]));
  for (const [k, v] of Object.entries(vars)) v === undefined ? delete process.env[k] : (process.env[k] = v);
  try { return await fn(); } finally { for (const [k, v] of Object.entries(saved)) v === undefined ? delete process.env[k] : (process.env[k] = v); }
};
const secretHead = 'Token is in dummy-secret/token.txt:1 and nothing else.';
const stateFile = path.join(tmp, 'state-is-a-file');
fs.writeFileSync(stateFile, 'not a directory');
const fallbackRoot = path.join(tmp, 'fallback-tmp');
fs.mkdirSync(fallbackRoot);

// F1/F3: the state dir cannot be used (unset, or unwritable): the reply is saved under the OS temp dir, masked like any other.
for (const [label, vars] of [['unset', { TP_STATE_DIR: undefined }], ['unwritable', { TP_STATE_DIR: path.join(stateFile, 'x') }]]) {
  await check(`F1/F3 state dir ${label}: fallback artifact, masked`, () => withEnv({ ...vars, TMPDIR: fallbackRoot }, async () => {
    const r = await overflow(secretHead);
    assert(r.m, 'a manifest comes back');
    assert(!r.text.includes('dummy-secret'), 'withheld path masked');
    assert.equal(r.m.withheld, 1);
    assert(r.m.artifact.startsWith(fallbackRoot + path.sep), 'artifact is under the OS temp dir');
    assert.equal(fs.readFileSync(r.m.artifact, 'utf8').startsWith(secretHead), true, 'full reply saved');
    assert.equal(fs.statSync(r.m.artifact).mode & 0o777, 0o600);
    assert.equal(r.m.error, undefined);
    assert(r.text.includes(r.m.artifact));
  }));
}
// F1/F3: nothing can be written: masked excerpt, and an explicit error that the full reply is not saved.
await check('F1/F3 nothing writable: masked excerpt, explicit not-saved error', () => withEnv({ TP_STATE_DIR: undefined, TMPDIR: path.join(tmp, 'no-such-tmp') }, async () => {
  const r = await overflow(secretHead);
  assert(!r.text.includes('dummy-secret'), 'withheld path masked');
  assert(r.text.includes('[withheld path]'));
  assert(r.m, 'manifest still returned');
  assert.equal(r.m.artifact, undefined);
  assert.equal(r.m.error, 'full reply not saved');
  assert.equal(r.m.withheld, 1);
  assert(r.text.includes('full reply not saved'));
}));

// F2: a quoted name with a trailing :line or :line:col is classified as the full path, whether or not it exists.
for (const [label, head] of [
  ['double quotes :line', 'Look at "secret files/data.txt:8" now.'],
  ['backticks :line:col', 'Look at `secret files/data.txt:8:3` now.'],
  ['single quotes :line-line', "Look at 'secret files/data.txt:8-9' now."],
  ['no line number', 'Look at "secret files/data.txt" now.'],
]) {
  await check(`F2 ${label}: withheld, not spelled in the excerpt`, async () => {
    const r = await overflow(head);
    assert.equal(r.m.withheld, 1);
    assert(!r.text.includes('secret files'), 'full spelling absent');
    assert(!r.text.includes('files/data.txt'), 'tail spelling absent');
    assert.deepEqual(r.m.paths, []);
  });
}
await check('F2 quoted existing path keeps its line range', async () => {
  const r = await overflow('Quoted: "my dir/b c.py:3:5" here.');
  assert.deepEqual(r.m.paths, [{ path: 'my dir/b c.py', lines: ['3'], content_hash: sha(path.join(work, 'my dir/b c.py')) }]);
});
if (failures.length) { console.log('FAILED:', failures.join(' | ')); process.exit(1); }

// A classifier that cannot answer fails closed: everything cited is withheld.
process.env.TP_SENSITIVE_READ_BIN = path.join(tmp, 'no-such-classifier');
const closed = await ask(long);
assert.deepEqual(closed.details.manifest.paths, []);
// Every cited name is classified, prose-like ones ("e.g", "v1.2") and a missing file included, so all six are held back.
assert.equal(closed.details.manifest.withheld, 6);
console.log('ok');
