// Real extensions with a real child-process boundary: tmux is the fake on PATH, the
// environment (TMUX, TMUX_PANE, TP_STATE_DIR, PI_CODING_AGENT_DIR) comes from the caller.
// Usage: node --experimental-strip-types tests/extension_session_runtime.mjs goal|dispatcher|subagents <project>
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const [scenario, project] = process.argv.slice(2);
const root = path.resolve(import.meta.dirname, '..');
const extension = name => path.join(root, 'agent/extensions', name);
const handlers = {}, commands = {}, tools = {}, messages = [], notices = [];
const pi = { on: (event, cb) => { handlers[event] = cb; }, registerCommand: (name, d) => { commands[name] = d; },
  registerTool: d => { tools[d.name] = d; }, appendEntry() {}, sendMessage: m => messages.push(m.content),
  sendUserMessage: m => messages.push(m) };
const ctx = { cwd: project, hasUI: true, isIdle: () => true, sessionManager: { getBranch: () => [] },
  ui: { notify: s => notices.push(s), setStatus() {}, setEditorText() {} } };
const finish = () => { console.log(JSON.stringify({ messages, notices })); process.exit(0); };

if (scenario === 'goal') {
  (await import(pathToFileURL(extension('goal.ts')).href)).default(pi);
  await commands.goal.handler('attach repair', ctx);
  finish();
}
if (scenario === 'dispatcher') {
  (await import(pathToFileURL(extension('dispatcher.ts')).href)).default(pi);
  try { await handlers.input({ text: '+followup probe hello', source: 'interactive' }, ctx); }
  catch (error) { notices.push(String(error)); }
  finish();
}
if (scenario === 'subagents') {
  // Only the schema helpers come from pi-ai; the tool bodies under test do not use them.
  const stub = 'const StringEnum = (values: readonly string[]) => ({ values }); const Type: any = new Proxy({}, { get: () => () => ({}) });';
  const source = fs.readFileSync(extension('subagents.ts'), 'utf8');
  const line = 'import { StringEnum, Type } from "@earendil-works/pi-ai";';
  if (!source.includes(line)) throw new Error('subagents pi-ai import changed; update the harness stub');
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tp-subagents-module-'));
  fs.writeFileSync(path.join(tmp, 'subagents.ts'), source.replace(line, stub));
  (await import(pathToFileURL(path.join(tmp, 'subagents.ts')).href)).default(pi);
  fs.rmSync(tmp, { recursive: true, force: true });
  try { await tools.send_to_subagent.execute('call', { id: 'probe', message: 'hello' }); }
  catch (error) { notices.push(String(error)); }
  finish();
}
throw new Error(`unknown scenario: ${scenario}`);
