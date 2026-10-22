// pi safe_read / safe_grep (agent/extensions/safe.ts) and the sensitive.ts gate around them, against the real tp-redact,
// tp-trust-guard (under a scratch home) and tp-sensitive-read. The key is built at runtime.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import assert from 'node:assert/strict';

const root = path.resolve(import.meta.dirname, '..');
const pkg = process.env.PI_CODING_AGENT_PACKAGE;
const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'tp-safe-')));
// The extensions run from a copy beside a node_modules that holds the installed Pi packages.
fs.mkdirSync(path.join(tmp, 'node_modules/@earendil-works'), { recursive: true });
fs.symlinkSync(pkg, path.join(tmp, 'node_modules/@earendil-works/pi-coding-agent'));
fs.symlinkSync(path.join(pkg, 'node_modules/@earendil-works/pi-ai'), path.join(tmp, 'node_modules/@earendil-works/pi-ai'));
fs.writeFileSync(path.join(tmp, 'package.json'), '{"type":"module"}\n');
fs.mkdirSync(path.join(tmp, 'ext'));
for (const name of ['safe.ts', 'sensitive.ts']) fs.copyFileSync(path.join(root, 'agent/extensions', name), path.join(tmp, 'ext', name));
fs.cpSync(path.join(root, 'agent/extensions/lib'), path.join(tmp, 'ext/lib'), { recursive: true });

// A scratch tool root: the real redactor and classifier, and the real guard placed over a scratch home (the guard's own
// `--home`), so no call here looks at the owner's trust store. `guard` is what the stub guard runs.
const toolRoot = (name) => {
  const base = path.join(tmp, name);
  fs.mkdirSync(path.join(base, 'agent'), { recursive: true });
  fs.mkdirSync(path.join(base, 'bin'));
  return base;
};
const home = path.join(tmp, 'home');
fs.mkdirSync(path.join(home, '.config/tmux-pi/trust'), { recursive: true });
const good = toolRoot('good');
for (const name of ['tp-redact', 'tp-sensitive-read']) fs.symlinkSync(path.join(root, 'bin', name), path.join(good, 'bin', name));
const guard = path.join(good, 'bin/tp-trust-guard');
const setGuard = (body) => fs.writeFileSync(guard, `#!/bin/sh\n${body}\n`, { mode: 0o755 });
setGuard(`exec python3 "${path.join(root, 'bin/tp-trust-guard')}" --home "${home}"`);

Object.assign(process.env, {
  PI_CODING_AGENT_DIR: path.join(good, 'agent'), TP_REDACTION: 'on', TP_REDACTION_LLM_URL: 'http://127.0.0.1:9',
  TP_REDACTION_DIR: path.join(tmp, 'store'), TP_REDACTION_SESSION: 'pi-safe-test-' + process.pid,
  TP_TRUST_GUARD_LOG_DIR: path.join(tmp, 'guard-log'),
});
for (const key of ['TP_TASK_DIR', 'TP_TASK_ID', 'TP_MODE']) delete process.env[key];

const alphabet = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
const secret = 'sk_live_' + Array.from({ length: 32 }, (_, i) => alphabet[(i * 17 + 5) % alphabet.length]).join('');
const work = path.join(tmp, 'work');
const outside = path.join(tmp, 'outside');
for (const dir of [work, outside]) fs.mkdirSync(dir);
const env = path.join(outside, '.env.production');
fs.writeFileSync(env, `API_URL=https://example.com\nCLERK_SECRET_KEY=${secret}\n`);
fs.writeFileSync(path.join(outside, 'design.md'), 'plain notes\nsee CLERK_SECRET above\n');

const safeMod = await import(pathToFileURL(path.join(tmp, 'ext/safe.ts')).href);
const gateMod = await import(pathToFileURL(path.join(tmp, 'ext/sensitive.ts')).href);
// One session of both extensions, as `pi` loads them.
const load = () => {
  const tools = {}, handlers = {};
  const api = { on(name, fn) { handlers[name] = fn; }, registerTool(tool) { tools[tool.name] = tool; } };
  safeMod.default(api);
  gateMod.default(api);
  return { tools, handlers };
};
const ctx = { cwd: work };
// Relative to ctx.cwd, as a worker writes it. (The scratch directory's random name in the output may itself be redacted.)
const near = '../outside';
const text = (result) => result.content.map((block) => block.text).join('\n');
const read = (tools, input) => tools.safe_read.execute('id', input, undefined, undefined, ctx).then(text);
const grep = (tools, input) => tools.safe_grep.execute('id', input, undefined, undefined, ctx).then(text);

{
  const { tools, handlers } = load();
  assert.deepEqual(Object.keys(tools).sort(), ['safe_grep', 'safe_read']); // no reveal
  assert.deepEqual(Object.keys(handlers).sort(), ['tool_call', 'tool_result']);
  for (const tool of Object.values(tools)) assert.equal(tool.parameters.type, 'object');

  // A secret in a .env-like file outside the worktree comes back as a placeholder, with line numbers.
  const shown = await read(tools, { path: env });
  assert.ok(!shown.includes(secret), 'secret leaked through safe_read');
  assert.match(shown, /CLERK_SECRET_KEY=<REDACTED:[a-z_0-9]+>/);
  assert.match(shown.split('\n')[1], /^\s+2\tCLERK_SECRET_KEY=/);
  assert.ok(shown.includes('API_URL=https://example.com'));
  const limited = await read(tools, { path: env, offset: 2, limit: 1 });
  assert.match(limited, /^\s+2\tCLERK_SECRET_KEY=<REDACTED:[a-z_0-9]+>$/);
  assert.equal(await read(tools, { path: '../outside/.env.production', offset: 2, limit: 1 }), limited); // relative to ctx.cwd
  await assert.rejects(read(tools, { path: outside }), /not a file/);
  await assert.rejects(read(tools, { path: path.join(outside, 'missing') }));

  const found = await grep(tools, { pattern: 'CLERK_SECRET', path: near });
  assert.ok(!found.includes(secret), 'secret leaked through safe_grep');
  assert.match(found, /\.env\.production:2:CLERK_SECRET_KEY=<REDACTED:[a-z_0-9]+>/); // hidden file found
  assert.match(found, /\.md:2:see CLERK_SECRET above/);
  assert.equal(await grep(tools, { pattern: 'zzz-no-such-text', path: near }), '(no matches)');
  assert.equal((await grep(tools, { pattern: '=', path: near, max_lines: 1 })).split('\n').length, 2); // one match and the "more lines" note
  assert.match(await grep(tools, { pattern: 'clerk_secret', path: near, ignore_case: true, glob: '*.md' }), /\.md:2:/);
  await assert.rejects(grep(tools, { pattern: '' }), /pattern is required/);

  // The placeholder is the same in both tools, and in the session's other results.
  const name = (out) => out.match(/CLERK_SECRET_KEY=(<REDACTED:[a-z_0-9]+>)/)[1];
  assert.equal(name(found), name(shown));

  // tool_result redacts the output a second time (on mode). That changes nothing: no double wrap, same placeholders, same lines.
  for (const [toolName, input, out] of [['safe_read', { path: env }, shown], ['safe_grep', { pattern: 'CLERK_SECRET', path: near }, found]]) {
    const again = handlers.tool_result({ type: 'tool_result', toolName, input, isError: false, content: [{ type: 'text', text: out }] });
    assert.equal(text(again), out, `${toolName}: a second redaction pass changed the output`);
  }
}

// Redactor failure withholds everything: a redactor that fails (even after printing the file), a missing one, a hung-up one.
for (const body of ['cat\nexit 3', 'exit 1', null]) {
  const bad = toolRoot('bad' + String(body).length);
  if (body !== null) fs.writeFileSync(path.join(bad, 'bin/tp-redact'), `#!/bin/sh\n${body}\n`, { mode: 0o755 });
  process.env.PI_CODING_AGENT_DIR = path.join(bad, 'agent');
  const { tools } = load();
  await assert.rejects(read(tools, { path: env }), (e) => /withheld/.test(e.message) && !e.message.includes(secret));
  await assert.rejects(grep(tools, { pattern: 'CLERK', path: near }), (e) => /withheld/.test(e.message) && !e.message.includes(secret));
}
process.env.PI_CODING_AGENT_DIR = path.join(good, 'agent');

// The gate. `confirms` counts the owner's prompts; the same input is sent to a tool that still asks and to the two that do not.
const gate = async (handlers, toolName, input, { ui = true } = {}) => {
  let confirms = 0;
  const answer = await handlers.tool_call({ type: 'tool_call', toolName, input },
    { cwd: work, hasUI: ui, ui: { confirm: async () => { confirms++; return true; } } });
  return { answer, confirms };
};
for (const mode of ['on', 'shadow', 'off']) {
  process.env.TP_REDACTION = mode;
  const { tools, handlers } = load();
  const homeSearch = { pattern: 'design', path: os.homedir() }; // outside the working tree, and may include credential files
  const envPath = { path: env };
  // Every other tool keeps the confirm, and is blocked where nobody can answer it.
  for (const [toolName, input] of [['grep', homeSearch], ['read', envPath], ['bash', { command: `cat ${env}` }]]) {
    assert.deepEqual(await gate(handlers, toolName, input), { answer: undefined, confirms: 1 }, `${toolName} no longer confirms (${mode})`);
    const unattended = await gate(handlers, toolName, input, { ui: false });
    assert.equal(unattended.answer?.block, true, `${toolName} unattended was not blocked (${mode})`);
    assert.equal(unattended.confirms, 0);
  }
  // safe_read and safe_grep need no prompt, with or without a terminal.
  for (const [toolName, input] of [['safe_grep', homeSearch], ['safe_grep', { pattern: 'x', path: outside, include_ignored: true }], ['safe_read', envPath]]) {
    for (const ui of [true, false]) assert.deepEqual(await gate(handlers, toolName, input, { ui }), { answer: undefined, confirms: 0 }, `${toolName} prompted (${mode})`);
  }
  // Whatever the mode, what they return is redacted at the source.
  assert.ok(!(await read(tools, { path: env })).includes(secret), `safe_read leaked in ${mode} mode`);
  assert.ok(!(await grep(tools, { pattern: 'CLERK', path: near })).includes(secret), `safe_grep leaked in ${mode} mode`);
}
process.env.TP_REDACTION = 'on';

// The trust-store guard still judges both tools first, in every mode, exactly as it does read, grep and bash.
const store = path.join(home, '.config/tmux-pi/trust');
const denied = [
  ['safe_read', { path: path.join(store, 'a.json') }], ['read', { path: path.join(store, 'a.json') }],
  ['safe_read', { path: '~/.config/tmux-pi/trust/a.json' }], ['safe_read', { path: path.join(home, '.CONFIG/Tmux-Pi/TRUST/a.json') }],
  ['safe_grep', { pattern: 'x', path: store }], ['grep', { pattern: 'x', path: store }],
  ['safe_grep', { pattern: 'x', path: home }], ['grep', { pattern: 'x', path: home }], // a search that would walk into the store
  ['safe_grep', { pattern: 'x', path: path.join(home, '.config') }],
  ['safe_grep', { pattern: 'x', glob: path.join(store, '*.json') }],
  ['bash', { command: `cat ${path.join(store, 'a.json')}` }],
];
const allowed = [
  ['safe_read', { path: env }], ['safe_grep', { pattern: 'tmux-pi/trust', path: outside }], ['safe_grep', { pattern: 'x', path: outside, glob: '*.env*' }],
  ['safe_grep', { pattern: 'x' }], ['safe_read', { path: path.join(home, '.config/nvim/init.lua') }],
];
for (const mode of ['on', 'shadow', 'off']) {
  process.env.TP_REDACTION = mode;
  const { handlers } = load();
  for (const [toolName, input] of denied) {
    for (const ui of [true, false]) {
      const { answer, confirms } = await gate(handlers, toolName, input, { ui });
      assert.equal(answer?.block, true, `${toolName} ${JSON.stringify(input)} was not denied (${mode})`);
      assert.match(answer.reason, /trust/i);
      assert.equal(confirms, 0, `${toolName} asked the owner instead of denying (${mode})`);
    }
  }
  for (const [toolName, input] of allowed) assert.equal((await gate(handlers, toolName, input)).answer, undefined, `${toolName} ${JSON.stringify(input)} was denied (${mode})`);
}
process.env.TP_REDACTION = 'on';
// A guard that fails, or is gone, blocks both tools.
{
  const { handlers } = load();
  for (const body of ['exit 3', 'echo not json', 'echo {}']) {
    setGuard(body);
    for (const toolName of ['safe_read', 'safe_grep'])
      assert.equal((await gate(handlers, toolName, toolName === 'safe_read' ? { path: env } : { pattern: 'x', path: outside })).answer?.block, true, `${toolName}: guard answer ${body} let the call through`);
  }
  fs.rmSync(guard);
  for (const toolName of ['safe_read', 'safe_grep'])
    assert.equal((await gate(handlers, toolName, { path: env, pattern: 'x' })).answer?.block, true, `${toolName}: a missing guard let the call through`);
}
console.log('ok');
