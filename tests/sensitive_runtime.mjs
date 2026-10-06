// Worker/main secret redaction in the pi tool_result handler, against the real tp-redact. The key is built at runtime.
import path from 'node:path';
import os from 'node:os';
import fs from 'node:fs';
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
console.log('ok');
