// Worker/main secret redaction in the pi tool_result handler, against the real tp-redact. The key is built at runtime.
import path from 'node:path';
import os from 'node:os';
import fs from 'node:fs';
import { execFileSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';
import assert from 'node:assert/strict';

const root = path.resolve(import.meta.dirname, '..');
const store = fs.mkdtempSync(path.join(os.tmpdir(), 'tp-sens-'));
Object.assign(process.env, { PI_CODING_AGENT_DIR: path.join(root, 'agent'), TP_REDACTION: 'on', TP_REDACTION_LLM_URL: 'http://127.0.0.1:9', TP_REDACTION_SESSION: 'pi-test-' + process.pid });
delete process.env.TP_TASK_DIR;
const secret = 'ghp_' + Array.from({ length: 36 }, (_, i) => 'aZ3kQ9xT7mB2vL5nR8wC1yD4fG6hJ0sPeUiO'[(i * 7 + 3) % 36]).join('');
const mod = await import(pathToFileURL(path.join(root, 'agent/extensions/sensitive.ts')).href);
const handlers = {};
mod.default({ on(name, fn) { handlers[name] = fn; } });
assert.deepEqual(Object.keys(handlers).sort(), ['tool_call', 'tool_result']);
const event = (text, extra = {}) => ({ type: 'tool_result', toolName: 'read', input: { path: 'src/app.env' }, isError: false,
  content: [{ type: 'text', text }, { type: 'image', data: 'AA==', mimeType: 'image/png' }], ...extra });

const out = handlers.tool_result(event(`TOKEN=${secret}\nname=demo\n`, { structuredContent: { token: secret } }));
const text = out.content[0].text;
assert.ok(!text.includes(secret), 'secret leaked');
assert.match(text, /<REDACTED:[a-z_0-9]+>/);
assert.ok(text.includes('name=demo'));
assert.equal(out.content[1].type, 'image');
assert.equal(out.structuredContent, null);
// Same secret in a second result maps to the same placeholder.
const again = handlers.tool_result(event(`x ${secret}\n`)).content[0].text;
assert.equal(text.match(/<REDACTED:[a-z_0-9]+>/)[0], again.match(/<REDACTED:[a-z_0-9]+>/)[0]);
// Redactor unavailable: withheld, never the raw text.
const broken = mod.default;
process.env.PI_CODING_AGENT_DIR = path.join(store, 'agent');
const h2 = {};
broken({ on(name, fn) { h2[name] = fn; } });
const failed = h2.tool_result(event(`TOKEN=${secret}`)).content[0].text;
assert.ok(!failed.includes(secret));
assert.match(failed, /withheld/);
// Off mode leaves results alone.
process.env.PI_CODING_AGENT_DIR = path.join(root, 'agent');
process.env.TP_REDACTION = 'off';
const h3 = {};
mod.default({ on(name, fn) { h3[name] = fn; } });
assert.equal(h3.tool_result(event('plain')), undefined);

// tool_call sends the session's directory to the classifier. A stub records what it receives.
const stubRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'tp-sens-stub-'));
fs.mkdirSync(path.join(stubRoot, 'agent')); fs.mkdirSync(path.join(stubRoot, 'bin'));
const seen = path.join(stubRoot, 'seen.json');
const stub = path.join(stubRoot, 'bin/tp-sensitive-read');
const seenArgs = path.join(stubRoot, 'seen-args');
fs.writeFileSync(stub, `#!/bin/sh\nprintf '%s\\n' "$*" > "${seenArgs}"\ncat > "${seen}"\necho '{"sensitive":false}'\n`, { mode: 0o755 });
// A stub trust-store guard: records what it receives and answers with the contents of `verdict`.
const guardSeen = path.join(stubRoot, 'guard-seen.json');
const verdict = path.join(stubRoot, 'verdict');
fs.writeFileSync(verdict, '{"deny":false,"reason":null}');
fs.writeFileSync(path.join(stubRoot, 'bin/tp-trust-guard'), `#!/bin/sh\ncat > "${guardSeen}"\ncat "${verdict}"\n`, { mode: 0o755 });
process.env.PI_CODING_AGENT_DIR = path.join(stubRoot, 'agent');
const h4 = {};
mod.default({ on(name, fn) { h4[name] = fn; } });
const call = (toolName, input) => ({ type: 'tool_call', toolName, input });
await h4.tool_call(call('bash', { command: 'git diff' }), { cwd: '/some/session/dir', hasUI: false });
assert.equal(JSON.parse(fs.readFileSync(seen, 'utf8')).cwd, '/some/session/dir');
// The worker gate prompts the owner, so it asks for the trusted lifts (T7 slice 4); offload.ts, which does not, never does.
assert.equal(fs.readFileSync(seenArgs, 'utf8'), '--trusted\n');
// A missing or odd cwd is left out, so the classifier assumes nothing.
for (const odd of [undefined, '', 42, null]) {
  fs.rmSync(seen, { force: true });
  await h4.tool_call(call('bash', { command: 'git diff' }), { cwd: odd, hasUI: false });
  assert.ok(!('cwd' in JSON.parse(fs.readFileSync(seen, 'utf8'))), `cwd ${String(odd)} sent`);
}

// The trust-store guard runs first, in every redaction mode; a deny, a crash or an unreadable answer blocks and never reaches the classifier.
assert.equal(JSON.parse(fs.readFileSync(guardSeen, 'utf8')).cwd, undefined, 'guard saw the last (cwd-less) call');
for (const mode of ['on', 'shadow', 'off']) {
  process.env.TP_REDACTION = mode;
  fs.rmSync(seen, { force: true }); fs.rmSync(guardSeen, { force: true });
  await h4.tool_call(call('bash', { command: 'git diff' }), { cwd: '/some/session/dir', hasUI: false });
  const sent = JSON.parse(fs.readFileSync(guardSeen, 'utf8'));
  assert.deepEqual([sent.tool_name, sent.cwd], ['bash', '/some/session/dir']);
  assert.ok(fs.existsSync(seen), `classifier skipped when the guard allowed (${mode})`);
  fs.writeFileSync(verdict, '{"deny":true,"reason":"trust store"}');
  fs.rmSync(seen, { force: true });
  const denied = await h4.tool_call(call('write', { path: '/x' }), { cwd: '/some/session/dir', hasUI: true });
  assert.equal(denied.block, true, `guard deny did not block (${mode})`);
  assert.match(denied.reason, /trust store/);
  assert.ok(!fs.existsSync(seen), `classifier ran after a guard deny (${mode})`);
  for (const bad of ['not json', '{}', '{"deny":"no"}', '']) {
    fs.writeFileSync(verdict, bad);
    assert.equal((await h4.tool_call(call('bash', { command: 'ls' }), { cwd: '/some/session/dir', hasUI: true }))?.block, true, `guard answer ${JSON.stringify(bad)} let the call through (${mode})`);
    assert.ok(!fs.existsSync(seen), 'classifier ran after an unreadable guard answer');
  }
  fs.writeFileSync(verdict, '{"deny":false,"reason":null}');
}
// The guard's ask (a tree, glob or brace list too large to check, far from the store) goes to the owner as the classifier's prompts do; it is sent with
// deny: true as well, and every other shape blocks. The classifier's own answer never lifts it, and it is not asked about when denied.
{
  const asked = '{"deny":true,"ask":true,"reason":"too large to check"}';
  const taskDir = fs.mkdtempSync(path.join(os.tmpdir(), 'tp-sens-task-'));
  process.env.TP_TASK_DIR = taskDir;
  const hAsk = {};
  mod.default({ on(name, fn) { hAsk[name] = fn; } });
  delete process.env.TP_TASK_DIR;
  const request = () => JSON.parse(fs.readFileSync(path.join(taskDir, 'permission-request.json'), 'utf8'));
  const prompts = [];
  const ui = (answer) => ({ cwd: '/some/session/dir', hasUI: true, ui: { confirm: async (title, message) => { prompts.push([title, message]); return answer; } } });
  const run = (verdictText, ctx) => { fs.writeFileSync(verdict, verdictText); fs.rmSync(seen, { force: true }); prompts.length = 0; return hAsk.tool_call(call('bash', { command: 'grep -R foo .' }), ctx); };
  for (const mode of ['on', 'shadow', 'off']) {
    process.env.TP_REDACTION = mode;
    let result = await run(asked, { cwd: '/some/session/dir', hasUI: false });
    assert.equal(result.block, true, `ask without a terminal let the call through (${mode})`);
    assert.match(result.reason, /too large to check/);
    assert.equal(request().status, 'pending');
    assert.ok(!fs.existsSync(seen), 'classifier ran after an unanswered guard ask');
    result = await run(asked, ui(false));
    assert.equal(result.block, true, `ask answered no let the call through (${mode})`);
    assert.equal(request().status, 'denied');
    assert.equal(prompts.length, 1);
    assert.match(prompts[0][1], /too large to check/);
    // The wording fits a brace list or a glob as well as a tree.
    assert.match(prompts[0][0], /could not fully check/);
    assert.match(request().target, /could not fully check/);
    assert.match(result.reason, /could not fully check/);
    assert.doesNotMatch(prompts[0][0] + request().target + result.reason, /tree/);
    assert.ok(!fs.existsSync(seen), 'classifier ran after the owner refused');
    result = await run(asked, ui(true));
    assert.equal(result, undefined, `ask answered yes still blocked (${mode})`);
    assert.equal(request().status, 'authorized');
    assert.ok(fs.existsSync(seen), 'classifier skipped after the owner approved');
    // Anything else blocks without asking: a plain deny, an ask that is not marked as a deny, odd values.
    for (const other of ['{"deny":true,"reason":"trust store"}', '{"deny":true,"ask":false,"reason":"x"}', '{"deny":false,"ask":true}',
                         '{"ask":true}', '{"deny":"yes","ask":true}', '{"deny":true,"ask":"yes"}', '{"deny":true,"ask":1}']) {
      result = await run(other, ui(true));
      assert.equal(result?.block, true, `${other} let the call through (${mode})`);
      assert.equal(prompts.length, 0, `${other} asked the owner`);
      assert.ok(!fs.existsSync(seen), `classifier ran after ${other}`);
    }
  }
  // The classifier still asks on its own after the guard's ask is approved, and a refusal there still blocks.
  fs.writeFileSync(path.join(stubRoot, 'bin/tp-sensitive-read'), '#!/bin/sh\ncat > /dev/null\necho \'{"sensitive":true,"target":"~/.aws","reason":"r"}\'\n', { mode: 0o755 });
  let result = await run(asked, ui(true));
  assert.equal(result, undefined);
  assert.equal(prompts.length, 2);
  fs.writeFileSync(stub, `#!/bin/sh\ncat > "${seen}"\necho '{"sensitive":false}'\n`, { mode: 0o755 });
  process.env.TP_REDACTION = 'off';
  fs.writeFileSync(verdict, '{"deny":false,"reason":null}');
}
fs.writeFileSync(path.join(stubRoot, 'bin/tp-trust-guard'), '#!/bin/sh\nexit 3\n', { mode: 0o755 });
assert.equal((await h4.tool_call(call('bash', { command: 'ls' }), { cwd: '/some/session/dir', hasUI: true }))?.block, true, 'a crashed guard let the call through');
fs.rmSync(path.join(stubRoot, 'bin/tp-trust-guard'));
assert.equal((await h4.tool_call(call('bash', { command: 'ls' }), { cwd: '/some/session/dir', hasUI: true }))?.block, true, 'a missing guard let the call through');
process.env.TP_REDACTION = 'off';

// With the real classifier in a scratch repository: an unscoped diff is judged by the files it touches.
process.env.PI_CODING_AGENT_DIR = path.join(root, 'agent');
const h5 = {};
mod.default({ on(name, fn) { h5[name] = fn; } });
const repo = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'tp-sens-repo-')));
const git = (...args) => execFileSync('git', ['-C', repo, ...args], { stdio: 'pipe' });
git('init', '-q', '-b', 'main', '.');
git('config', 'user.email', 't@example.invalid'); git('config', 'user.name', 't');
for (const name of ['README.md', 'config/local.json']) {
  fs.mkdirSync(path.dirname(path.join(repo, name)), { recursive: true });
  fs.writeFileSync(path.join(repo, name), 'one\n');
}
git('add', '-f', '.'); git('commit', '-q', '-m', 'first');
const diff = (cwd) => h5.tool_call(call('bash', { command: 'git diff' }), { cwd, hasUI: false });
fs.appendFileSync(path.join(repo, 'README.md'), 'more\n');
assert.equal(await diff(repo), undefined, 'plain diff prompted');
assert.equal((await diff(undefined))?.block, true, 'no cwd must prompt');
assert.equal((await diff(path.join(repo, 'missing')))?.block, true, 'odd cwd must prompt');
fs.appendFileSync(path.join(repo, 'config/local.json'), 'more\n');
assert.equal((await diff(repo))?.block, true, 'diff touching config/local.json must prompt');
console.log('ok');
