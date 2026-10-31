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
  console.log(JSON.stringify(p.includes('dummy-secret') ? { sensitive: true, target: p } : { sensitive: false }));
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
assert(excerpt.length <= 24_000 && reply.startsWith(excerpt));
assert(reply[excerpt.length] === '\n', 'cut at a line boundary');
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

// A hash reflects an uncommitted edit.
fs.appendFileSync(path.join(work, 'src/alpha.py'), '# edited, not committed\n');
const again = await ask(long);
const alpha = again.details.manifest.paths.find((p) => p.path === 'src/alpha.py');
assert.equal(alpha.content_hash, sha(path.join(work, 'src/alpha.py')));
assert.notEqual(alpha.content_hash, m.paths[0].content_hash);
assert.equal(evidence().length, 2, 'each overflow gets its own file');

// A classifier that cannot answer fails closed: everything cited is withheld.
process.env.TP_SENSITIVE_READ_BIN = path.join(tmp, 'no-such-classifier');
const closed = await ask(long);
assert.deepEqual(closed.details.manifest.paths, []);
assert.equal(closed.details.manifest.withheld, 4);
console.log('ok');
