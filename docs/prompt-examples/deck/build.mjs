// Builds ../index.html: embeds decision text (the call, copied verbatim from the decision log) and screenshots
// (converted to JPEG with macOS sips, downscaled) into template.html. Run in this folder: node build.mjs
// Set the four values below for the batch. Paths are relative to the campaign folder, two levels above ../.
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { execFileSync } from 'node:child_process';
import { DECISIONS, SHOTS } from './data.mjs';

const DECISIONS_MD = 'briefs/<batch>-decisions-<date>.md';
const DECK_TITLE = '<Batch> decisions (S<N>D1 onward)';
const DECK_KEY = '<batch>-decision-deck-v1';
// Every logged decision from this number up must have a DECISIONS entry, so none is left out of the sign-off.
// Ids are `S<sprint>D<n>` (numbering restarts in each campaign); the older bare `D<n>` form still works.
// FIRST_ID is the n of the first decision in this deck, within the campaign's own ids.
const FIRST_ID = 1;

const here = path.dirname(new URL(import.meta.url).pathname);
const deck = path.join(here, '..');
const camp = path.join(deck, '../..');
const md = fs.readFileSync(path.join(camp, DECISIONS_MD), 'utf8').split('\n');
const blocks = {};
for (let i = 0; i < md.length; i++) {
    const m = md[i].match(/^- \*\*((?:S\d+)?D\d+)/);
    if (!m) continue;
    let t = md[i].replace(/^- /, '');
    let j = i + 1;
    while (j < md.length && /^\s+-?\s*\S/.test(md[j]) && !/^- /.test(md[j])) {
        t += '\n' + md[j].trim();
        j++;
    }
    (blocks[m[1]] ||= []).push(t);
}
const tmp = fs.mkdtempSync(path.join(process.env.DECK_TMP || os.tmpdir(), 'deckjpg-'));
const shots = {};
for (const [id, [rel, caption]] of Object.entries(SHOTS)) {
    const src = rel.startsWith('shots/') ? path.join(deck, rel) : path.join(camp, rel);
    const out = path.join(tmp, id + '.jpg');
    execFileSync('sips', ['-s', 'format', 'jpeg', '-s', 'formatOptions', '72', '--resampleWidth', '960', src, '--out', out], {
        stdio: 'ignore',
    });
    shots[id] = { caption, uri: 'data:image/jpeg;base64,' + fs.readFileSync(out).toString('base64') };
}
const decisions = DECISIONS.map((d) => {
    const call = (blocks[d.id] || []).join('\n\n');
    if (!call) throw new Error('no source text for ' + d.id + ' in ' + DECISIONS_MD);
    return { ...d, call: call.replace(/\*\*/g, '') };
});
const inDeck = new Set(DECISIONS.map((d) => d.id));
const missing = Object.keys(blocks).filter((id) => Number(id.match(/D(\d+)$/)[1]) >= FIRST_ID && !inDeck.has(id));
if (missing.length) throw new Error('logged but not in data.mjs: ' + missing.join(', '));
for (const d of DECISIONS)
    for (const r of d.reg || [])
        if (!r.cite || !r.quote || !r.src) throw new Error(d.id + ': each reg entry needs cite, quote and src');
const esc = (o) => JSON.stringify(o).replace(/</g, '\\u003c').replace(/\u2028/g, '\\u2028').replace(/\u2029/g, '\\u2029');
const text = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;');
let html = fs.readFileSync(path.join(here, 'template.html'), 'utf8');
html = html
    .replace('/*DECISIONS*/', () => esc(decisions))
    .replace('/*SHOTS*/', () => esc(shots))
    .replace(/<title>DECK_TITLE<\/title>/, () => '<title>' + text(DECK_TITLE) + '</title>')
    .replace(/<h1>DECK_TITLE<\/h1>/, () => '<h1>' + text(DECK_TITLE) + '</h1>')
    .replace("'DECK_TITLE (deck answers, '", () => JSON.stringify(DECK_TITLE + ' (deck answers, ').replace(/</g, '\\u003c'))
    .replace("var KEY='DECK_KEY';", () => 'var KEY=' + JSON.stringify(DECK_KEY).replace(/</g, '\\u003c') + ';');
fs.writeFileSync(path.join(deck, 'index.html'), html);
console.log('wrote index.html', (html.length / 1e6).toFixed(2) + ' MB', decisions.length, 'decisions');
