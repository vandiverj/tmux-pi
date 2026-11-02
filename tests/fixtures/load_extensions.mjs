// Imports every agent/extensions/*.ts against the installed pi (through pi_host.mjs) and checks each has a default factory.
// Input: argv = [pkg, root]. Prints {loaded: [...], failed: {file: message}} as JSON. No model, no network; factories are not called.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { linkPiHost } from './pi_host.mjs';

const [pkg, root] = process.argv.slice(2);
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tp-ext-load-'));
linkPiHost(tmp, pkg);
fs.writeFileSync(path.join(tmp, 'package.json'), '{"type":"module"}\n');
const source = path.join(root, 'agent/extensions');
fs.cpSync(source, tmp, { recursive: true });
const loaded = [];
const failed = {};
for (const name of fs.readdirSync(source).filter((n) => n.endsWith('.ts')).sort()) {
  try {
    const mod = await import(pathToFileURL(path.join(tmp, name)).href);
    if (typeof mod.default !== 'function') throw new Error('no default factory');
    loaded.push(name);
  } catch (error) {
    failed[name] = String(error.message ?? error);
  }
}
fs.rmSync(tmp, { recursive: true, force: true });
console.log(JSON.stringify({ loaded, failed }));
