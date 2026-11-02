// Gives a scratch extension directory the imports pi gives an extension at run time. Pi 1.5.0 maps `Type` from "@earendil-works/pi-ai"
// to the TypeBox it bundles, because pi-ai itself no longer exports it (pi docs/extensions.md, "Available Imports"); `StringEnum` is
// still pi-ai's own. Outside pi that mapping is missing, so this builds it: node_modules/@earendil-works/pi-ai is a shim that
// re-exports the installed pi-ai and adds Type from the installed typebox. With a pi that still exports Type from pi-ai it links pi-ai as is.
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

function firstExisting(candidates) {
  return candidates.find((candidate) => fs.existsSync(candidate)) ?? null;
}

export function linkPiHost(tmp, pkg) {
  const scope = path.join(tmp, 'node_modules/@earendil-works');
  fs.mkdirSync(scope, { recursive: true });
  fs.symlinkSync(pkg, path.join(scope, 'pi-coding-agent'));
  // pi-ai is nested under the package or installed beside it, depending on the install layout.
  const ai = firstExisting([path.join(pkg, 'node_modules/@earendil-works/pi-ai'), path.join(path.dirname(pkg), 'pi-ai')]);
  if (!ai) throw new Error(`pi-ai not found for ${pkg}`);
  const typebox = firstExisting([path.join(pkg, 'node_modules/typebox'), path.join(path.dirname(path.dirname(pkg)), 'typebox')]);
  const aiIndex = path.join(fs.realpathSync(ai), 'dist/index.js');
  if (!typebox) {
    fs.symlinkSync(ai, path.join(scope, 'pi-ai'));
    return;
  }
  const shim = path.join(scope, 'pi-ai');
  fs.mkdirSync(shim);
  fs.writeFileSync(path.join(shim, 'package.json'), '{"name":"@earendil-works/pi-ai","type":"module","main":"index.js"}\n');
  const typeboxEntry = pathToFileURL(path.join(fs.realpathSync(typebox), 'build/index.mjs')).href;
  fs.writeFileSync(path.join(shim, 'index.js'),
    `export * from ${JSON.stringify(pathToFileURL(aiIndex).href)};\nexport { Type } from ${JSON.stringify(typeboxEntry)};\n`);
}
