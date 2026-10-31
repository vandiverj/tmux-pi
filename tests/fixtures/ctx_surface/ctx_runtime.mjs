// Runs agent/extensions/ctx.ts against the installed pi package (for TypeBox and StringEnum) with a fake ExtensionAPI, and prints
// what it registered and what each tool answered, as JSON. Input: CTX_RUNTIME_SPEC = {pkg, root, project, calls: [{tool, input}]}.
// The extension runs the real bin/tp-ctx (PI_CODING_AGENT_DIR is set by the caller so that ../bin/tp-ctx is it). No model, no network.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const spec = JSON.parse(process.env.CTX_RUNTIME_SPEC);
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tp-ctx-ext-'));
fs.mkdirSync(path.join(tmp, 'node_modules/@earendil-works'), { recursive: true });
fs.symlinkSync(spec.pkg, path.join(tmp, 'node_modules/@earendil-works/pi-coding-agent'));
fs.symlinkSync(path.join(spec.pkg, 'node_modules/@earendil-works/pi-ai'), path.join(tmp, 'node_modules/@earendil-works/pi-ai'));
fs.writeFileSync(path.join(tmp, 'package.json'), '{"type":"module"}\n');
fs.copyFileSync(path.join(spec.root, 'agent/extensions/ctx.ts'), path.join(tmp, 'ctx.ts'));
const mod = await import(pathToFileURL(path.join(tmp, 'ctx.ts')).href);

function load(enabled) {
  if (enabled) process.env.TP_CTX_TOOLS = '1';
  else delete process.env.TP_CTX_TOOLS;
  const tools = [];
  mod.default({ registerTool(tool) { tools.push(tool); } });
  return tools;
}

const off = load(false).length;
const tools = load(true);
const answers = [];
for (const call of spec.calls) {
  const tool = tools.find((t) => t.name === call.tool);
  try {
    const result = await tool.execute('call-1', call.input, undefined, undefined, { cwd: spec.project });
    answers.push({ text: result.content[0].text, type: result.content[0].type });
  } catch (error) {
    answers.push({ error: String(error.message ?? error) });
  }
}
const registered = tools.map((t) => ({ name: t.name, description: t.description, parameters: JSON.parse(JSON.stringify(t.parameters)) }));
fs.rmSync(tmp, { recursive: true, force: true });
console.log(JSON.stringify({ off, registered, answers }));
