// Real dispatcher module under Node's TS stripping; tmux/spawn/handoff are fake,
// while preflight and automation decisions run through the real tp-task Python CLI.
// Usage: node --experimental-strip-types tests/dispatcher_runtime.mjs <scenario>
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';
const scenario = process.argv[2];
// A worker's default reply: it carries a result status, or the task reads paused.
// A review ends with a Coverage section or it is incomplete (lib/review_verdict.py).
const COV = '\nCoverage\nreviewed every changed file.';
const DONE = 'Done.\nRESULT_STATUS: complete\n';
const root = path.resolve(import.meta.dirname, '..');
const state = fs.mkdtempSync(path.join(os.tmpdir(), 'tp-r5-runtime-'));
const moduleDir = fs.mkdtempSync(path.join(os.tmpdir(), 'tp-r5-module-'));
const source = fs.readFileSync(path.join(root, 'agent/extensions/dispatcher.ts'), 'utf8');
fs.cpSync(path.join(root, 'agent/extensions/lib'), path.join(moduleDir, 'lib'), { recursive: true });
// The production watcher/scanner poll every 3000ms; compress only that cadence so
// settlement is noticed quickly. Other sleeps (2s, 15s contract scan) stay real.
const POLL_MS = 3000, FAST_POLL_MS = Number(process.env.TP_TEST_POLL_MS || 150);
const sleepLine = 'const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));';
if (!source.includes(sleepLine)) throw new Error('dispatcher sleep helper changed; update the harness poll compression');
// Only the two intended 3000ms cadences may be compressed: the task watcher (after its
// stuck-warning check) and the prompt scanner (guarded by `active`). Fail if either moves
// or a third appears, and if any sleep() call uses a non-literal delay we cannot audit.
const pollSleeps = [...source.matchAll(/sleep\(3000\)/g)];
const watcherPoll = /Date\.now\(\) - started > 45_000[\s\S]{0,400}?\n\t\t\tawait sleep\(3000\);\n\t\t\}\n\t\};/;
const scannerPoll = /await scanPrompts\(\)\.catch\(\(\) => undefined\);\n\t+if \(active\) await sleep\(3000\);/;
if (pollSleeps.length !== 2 || !watcherPoll.test(source) || !scannerPoll.test(source)
  || [...source.matchAll(/sleep\(([^)]*)\)/g)].some(m => m[1] !== 'ms' && !/^[\d_]+$/.test(m[1]))) {
  throw new Error(`dispatcher 3000ms poll sites changed (found ${pollSleeps.length}); update the harness poll compression`);
}
fs.writeFileSync(path.join(moduleDir, 'dispatcher.ts'), source.replace('import { execFile } from "node:child_process";', 'const execFile = globalThis.__fakeExecFile;')
  .replace(sleepLine, `const sleep = (ms: number) => new Promise((resolve) => setTimeout(() => { if (ms === ${POLL_MS}) (globalThis.__pollTicks ||= []).push(Date.now()); resolve(undefined); }, ms === ${POLL_MS} ? ${FAST_POLL_MS} : ms));`));
process.env.TMUX = 'test';
process.env.TP_MODE = 'strict';
process.env.PI_CODING_AGENT_DIR = path.join(root, 'agent');
delete process.env.TP_TASK_DIR;
delete process.env.TP_TASK_ID;
// A worker shell's own workspace must not leak in: the fake tmux below names the state dir.
delete process.env.TP_STATE_DIR;
delete process.env.TMUX_PANE;
const calls = [];
const messages = [];
const handlers = {};
const statuses = [];
// Pinned Laya-style decisions: implementation is subagent unless +here overrides;
// review-required work stays blocked in main even for a low-risk text request.
const direct = ['direct', 'direct-off', 'direct-review', 'main-pi', 'review-turn', 'resolved-turn', 'forged-resolved', 'wrong-id-resolved', 'task-blocker', 'archived-blocker', 'archived-renamed', 'unknown-marker', 'malformed-marker', 'approved-changed', 'resolved-changed', 'expanded', 'expanded-marker'].includes(scenario);
const request = scenario === 'delegate-omitted' ? '+delegate Read only fixture.txt and answer: what color is cedar? Do not edit files.' : scenario === 'research-gate' ? '+research +review Summarize cache history' : ['expanded', 'expanded-marker'].includes(scenario) ? (scenario === 'expanded-marker' ? '+here /pwhash' : '/pwhash') : scenario === 'direct-subagent' ? 'Explain tp-view' : scenario === 'followup-blocked' ? '+delegate Fix typo in README.md' : ['review', 'review-gate', 'midreview-followup', 'midreview-pane', 'midreview-nonce', 'midreview-cap', 'midreview-reload'].includes(scenario) ? '+delegate Update password hashing in auth.py' : ['confirm', 'decline', 'no-ui'].includes(scenario) ? 'Deploy production migration and delete old backups' : ['review-turn', 'resolved-turn'].includes(scenario) ? '+here Go ahead and apply the change we discussed' : scenario === 'direct-review' || scenario === 'main-pi' ? '+here +review Explain tp-view' : direct ? '+here Explain tp-view' : 'Fix typo in README.md';
let decision = { worker: 'pi', model: 'openai/gpt-6-sol', thinking: 'low', route: 'coding', intent: direct || scenario === 'direct-subagent' ? 'communication_or_documentation' : 'implementation', review: ['direct-review', 'main-pi', 'capability-resolved', 'capability-deleted', 'review-failed', 'review-invalid', 'formatted-verdict', 'unknown-verdict', 'notes-empty', 'notes-suffix', 'approved-suffix', 'no-coverage', 'empty-coverage', 'notes-quote', 'notes-verdict', 'review-changes', 'review-gate', 'midreview-followup', 'midreview-pane', 'midreview-nonce', 'midreview-cap', 'midreview-reload', 'kill'].includes(scenario) ? 'required' : 'none', scope: 'brief', placement: direct ? 'main' : 'subagent', overrides: direct ? ['placement', ...(scenario === 'direct-review' || scenario === 'main-pi' ? ['review'] : [])] : ['placement'], task: direct ? 'Explain tp-view' : request.replace(/^\+delegate\s+/, '') };
if (['direct', 'direct-off', 'direct-review', 'main-pi', 'review', 'review-gate', 'review-turn', 'resolved-turn', 'delegate-omitted'].includes(scenario)) {
  const fixture = spawnSync('python3', [path.join(root, 'tests/router_runtime_fixture.py'), request], { cwd: root, encoding: 'utf8' });
  if (fixture.status !== 0) throw new Error(fixture.stderr);
  decision = JSON.parse(fixture.stdout);
}
const pf = { mode: 'delegated', artifact_tier: 1, durable: true, no_edits: false, stop: false };
function actualTask(args, input) {
  const p = spawnSync(path.join(root, 'bin/tp-task'), args, { cwd: root, input: input ?? '', encoding: 'utf8', env: { ...process.env, TP_STATE_DIR: state } });
  if (p.status !== 0) throw new Error(p.stderr || p.stdout);
  return p.stdout;
}
let pane = true;
let seq = 0;
function dir(id) { return path.join(state, 'tasks', id); }
function task(id, meta = {}) {
  fs.mkdirSync(dir(id), { recursive: true });
  fs.writeFileSync(path.join(dir(id), 'meta.json'), JSON.stringify({ id, worker: 'pi', pane: `%${++seq}`, ...meta }));
  fs.writeFileSync(path.join(dir(id), 'state.json'), JSON.stringify({ state: 'waiting', updated: new Date().toISOString() }));
  fs.writeFileSync(path.join(dir(id), 'result.md'), DONE);
}
function command(cmd, args, input) {
  const name = path.basename(cmd);
  // the real shared Coverage check (lib/review_verdict.py), not a fake
  if (name === 'python3' && path.basename(args[0]) === 'review_verdict.py') {
    const p = spawnSync('python3', args, { encoding: 'utf8' });
    if (p.status !== 0) throw Object.assign(new Error(p.stderr), { code: p.status });
    return p.stdout;
  }
  calls.push({ name, args, input });
  if (name === 'tmux') {
    if (args[0] === 'display-message') {
      if (args.at(-1) === '#{@tp_main_pane}') return '%1';
      if (args.at(-1) === '#{pane_current_path}') return root;
      return state;
    }
    if (args[0] === 'list-panes') return pane ? Array.from({ length: seq }, (_, i) => `%${i + 1} 0`).join('\n') + '\n' : '';
    if (scenario === 'halted-dialog-continue' && args[0] === 'send-keys' && args.at(-1) === 'Enter')
      globalThis.markerAtContinue = JSON.parse(fs.readFileSync(path.join(dir('source-1'), 'review-required.json')));
    if (scenario === 'followup-blocked' && args[0] === 'send-keys' && !globalThis.upgradeAtSend) {
      const markerPath = path.join(dir('auto-1'), 'review-required.json');
      globalThis.upgradeAtSend = { marker: fs.existsSync(markerPath) ? JSON.parse(fs.readFileSync(markerPath)) : null,
        decision: JSON.parse(fs.readFileSync(path.join(dir('auto-1'), 'meta.json'))).decision };
    }
    if (scenario === 'rework-approved' && args[0] === 'send-keys' && args.at(-1) === 'Enter') {
      setTimeout(() => {
        fs.writeFileSync(path.join(dir('auto-1'), 'result.md'), 'Reworked result.');
        fs.writeFileSync(path.join(dir('auto-1'), 'state.json'), JSON.stringify({ state: 'waiting', updated: new Date().toISOString() }));
      }, 150);
    }
    if (scenario === 'resolved-inflight' && args[0] === 'send-keys' && args.at(-1) === 'Enter' && fs.existsSync(path.join(dir('review-live'), 'review-of'))) {
      globalThis.markerAtRework = JSON.parse(fs.readFileSync(path.join(dir('auto-1'), 'review-required.json')));
      fs.writeFileSync(path.join(dir('auto-1'), 'result.md'), 'UNREVIEWED rework.');
      fs.writeFileSync(path.join(dir('auto-1'), 'state.json'), JSON.stringify({ state: 'starting' }));
    }
    if (['review-changes', 'resolved-changes'].includes(scenario) && args[0] === 'send-keys' && args.at(-1) === 'Enter') {
      fs.writeFileSync(path.join(dir('auto-1'), 'result.md'), 'Revised after first review.');
      fs.writeFileSync(path.join(dir('auto-1'), 'state.json'), JSON.stringify({ state: 'waiting', updated: new Date().toISOString() }));
    }
    return '';
  }
  if (name === 'tp-goal') {
    if (args.includes('gate-input')) return JSON.stringify({ id: 'goal', ticket: args[args.indexOf('--ticket') + 1], spec_sha256: 'b'.repeat(64), workdir: root, scope: 'src only', objective: 'Fix parser fully', done: 'Tests and review pass', allowed_paths: ['src'] });
    if (args.includes('gate-attempt')) return JSON.stringify({ id: 'goal', ticket: 'a'.repeat(32), spec_sha256: 'b'.repeat(64), workdir: root, scope: 'src only', objective: 'Fix parser fully', done: 'Tests and review pass', allowed_paths: ['src'], risk: 'low', review: 'required', notice: 'Goal gate verified' });
    return '{}';
  }
  if (name === 'tp-route') return args[0] === '--dispatch' ? JSON.stringify({ ...decision, task: ['direct', 'direct-off', 'direct-review', 'main-pi', 'review', 'review-turn', 'resolved-turn'].includes(scenario) ? decision.task : input }) : JSON.stringify({ ask: [] });
  if (name === 'tp-task') {
    if (args[0] === 'preflight') {
      const selected = JSON.parse(actualTask(args.filter(a => a !== '--record'), input));
      if (args.includes('--record')) {
        task('stop-1');
        return JSON.stringify({ ...selected, record: { id: 'stop-1', dir: dir('stop-1'), record: path.join(dir('stop-1'), 'task.md') } });
      }
      return JSON.stringify(selected);
    }
    if (args[0] === 'review-list') return actualTask(args);
    if (args[0] === 'main-contract') return actualTask(args, input);
    if (args[0] === 'automation') return actualTask(args, input);
    if (args[0] === 'context') return JSON.stringify({});
    if (args[0] === 'capture-output') { fs.writeFileSync(path.join(dir(args[1]), 'captured.md'), input); return ''; }
  }
  if (name === 'tp-spawn') { task('auto-1', ['review-gate', 'research-gate', 'halted-restart', 'halted-marker-restart'].includes(scenario) ? { worker: scenario === 'review-gate' ? 'claude' : 'pi', decision: JSON.parse(args[args.indexOf('--decision-json') + 1]), ...(scenario === 'research-gate' ? { contract: { artifact: path.join(state, 'report.md') } } : {}) } : {}); if (['review-gate', 'research-gate'].includes(scenario)) fs.writeFileSync(path.join(dir('auto-1'), 'state.json'), JSON.stringify({ state: 'starting' })); if (['followup-blocked', 'halted-restart', 'halted-marker-restart'].includes(scenario)) fs.writeFileSync(path.join(dir('auto-1'), 'state.json'), JSON.stringify({ state: 'starting' })); if (scenario === 'kill') pane = false; if (['capability', 'capability-resolved', 'capability-deleted'].includes(scenario)) fs.writeFileSync(path.join(dir('auto-1'), 'result.md'), 'CAPABILITY_REQUEST: Can I use a new external service?'); return 'auto-1\n'; }
  if (name === 'tp-review') { if (['resolved-inflight', 'resolved-inflight-error'].includes(scenario)) {
    task('review-live', { worker: 'claude' });
    fs.writeFileSync(path.join(dir('review-live'), 'review-of'), 'auto-1');
    fs.writeFileSync(path.join(dir('review-live'), 'result.md'), 'REQUEST_CHANGES\nFix evidence.');
    fs.writeFileSync(path.join(dir('review-live'), 'state.json'), JSON.stringify({ state: 'starting' }));
    return 'review-live\n';
  } if (['midreview-followup', 'midreview-pane', 'midreview-nonce', 'midreview-cap', 'midreview-reload'].includes(scenario)) {
    const id = `review-${++globalThis.midRounds}`;
    task(id, { worker: 'claude' });
    fs.writeFileSync(path.join(dir(id), 'review-of'), 'auto-1');
    fs.writeFileSync(path.join(dir(id), 'result.md'), 'APPROVED\nold snapshot.' + COV);
    fs.writeFileSync(path.join(dir(id), 'state.json'), JSON.stringify({ state: globalThis.midRounds === 1 || scenario === 'midreview-cap' ? 'starting' : 'waiting', updated: new Date().toISOString() }));
    fs.writeFileSync(path.join(dir('auto-1'), 'meta.json'), JSON.stringify({ ...JSON.parse(fs.readFileSync(path.join(dir('auto-1'), 'meta.json'))), review: id }));
    globalThis.reviewSnapshots.push(fs.readFileSync(path.join(dir('auto-1'), 'result.md'), 'utf8'));
    return `${id}\n`;
  } if (scenario === 'rework-approved') {
    task('review-2', { worker: 'claude' });
    fs.writeFileSync(path.join(dir('review-2'), 'review-of'), 'auto-1');
    fs.writeFileSync(path.join(dir('review-2'), 'result.md'), 'APPROVED\nnew round.' + COV);
    fs.writeFileSync(path.join(dir('auto-1'), 'meta.json'), JSON.stringify({ ...JSON.parse(fs.readFileSync(path.join(dir('auto-1'), 'meta.json'))), review: 'review-2' }));
    return 'review-2\n';
  } if (['review-changes', 'resolved-changes'].includes(scenario)) (globalThis.reviewRoundMarkers ??= []).push(JSON.parse(fs.readFileSync(path.join(dir('auto-1'), 'review-required.json'))).status); if (['review-failed', 'resolved-failed'].includes(scenario)) throw new Error('review unavailable'); task('review-1', { worker: scenario === 'review-gate' ? 'pi' : 'claude' }); fs.writeFileSync(path.join(dir('review-1'), 'review-of'), args.at(-1)); fs.writeFileSync(path.join(dir('review-1'), 'result.md'), scenario === 'review-invalid' ? 'No verdict.' : scenario === 'formatted-verdict' ? '**APPROVED**: checked.' : scenario === 'unknown-verdict' ? 'APPROVED_PENDING: later.' : scenario === 'notes-verdict' ? 'APPROVED_WITH_NOTES\nRESULT_STATUS: complete\nN1 test gap: no test for the empty case.\nN2 wording: the error says "bad".' + COV : scenario === 'notes-empty' ? 'APPROVED_WITH_NOTES\nRESULT_STATUS: complete' : scenario === 'notes-suffix' ? 'APPROVED_WITH_NOTES: N1 wording gap.' : scenario === 'no-coverage' ? 'APPROVED\nchecked, no problems.' : scenario === 'empty-coverage' ? 'APPROVED_WITH_NOTES\nN1 wording.\nCoverage\n\n' : scenario === 'approved-suffix' ? 'APPROVED-OTHER\nRESULT_STATUS: complete' : scenario === 'notes-quote' ? 'APPROVED_WITH_NOTES\nRESULT_STATUS: complete\nRound 1 REQUEST_CHANGES F1 was fixed.\nN1 wording gap.' + COV : ['review-changes', 'resolved-changes'].includes(scenario) ? 'REQUEST_CHANGES: fix the evidence.' : 'APPROVED\nindependently checked.' + COV); fs.writeFileSync(path.join(dir(args.at(-1)), 'meta.json'), JSON.stringify({ ...JSON.parse(fs.readFileSync(path.join(dir(args.at(-1)), 'meta.json'))), review: 'review-1' })); return 'review-1\n'; }
  if (name === 'tp-contract') { if (args[0] === 'escalate' && scenario.startsWith('halted-dialog-')) throw new Error('automatic escalation unavailable'); if (args[0] === 'extend') return JSON.stringify({ message: 'Continue with bounded budget' }); if (args[0] === 'enforce' && (['halted-restart', 'halted-marker-restart'].includes(scenario) || scenario.startsWith('halted-dialog-')) && !globalThis.haltedEmitted) { globalThis.haltedEmitted = true; return JSON.stringify({ action: 'halted', id: 'source-1', reason: 'budget', trigger: 'budget', artifact: path.join(dir('source-1'), 'result.md'), kind: scenario.startsWith('halted-dialog-') ? 'research' : 'task' }); } if (args[0] === 'research') return JSON.stringify({ worker: 'pi', model: 'openai/gpt-6-sol', thinking: 'low', escalate: { worker: 'pi', model: 'openai/gpt-6-sol', thinking: 'high' } }); if (args[0] === 'slot') return JSON.stringify({ ok: true }); if (args[0] === 'escalate') return JSON.stringify({ target: { worker: 'pi', model: 'openai/gpt-6-sol', thinking: 'high' }, record: { trigger: 'budget' } }); return ''; }
  return '';
}
globalThis.__fakeExecFile = (cmd, args, opts, callback) => {
  const child = { stdin: { end(input) { queueMicrotask(() => { try { callback(null, command(cmd, args, input), ''); } catch (e) { callback(e, '', e.message); } }); } } };
  // run() calls stdin.end only for input-bearing commands.
  queueMicrotask(() => { if (!child.used && !['tp-route', 'tp-task'].includes(path.basename(cmd)) || (!child.used && path.basename(cmd) === 'tp-task' && !['main-contract', 'automation', 'preflight', 'context', 'capture-output'].includes(args[0]))) { try { callback(null, command(cmd, args), ''); } catch (e) { callback(e, '', e.message); } } });
  const end = child.stdin.end;
  child.stdin.end = (input) => { child.used = true; end(input); };
  // preflight/context/automation receive input; tmux and spawn may also receive input.
  if (path.basename(cmd) === 'tp-task' && ['preflight', 'context'].includes(args[0])) queueMicrotask(() => { if (!child.used) callback(null, command(cmd, args), ''); });
  return child;
};
const pi = { on(name, fn) { handlers[name] = fn; }, registerTool(t) { pi.tool = t; }, registerCommand(name, c) { pi.command = c; }, sendMessage(msg) { messages.push(msg); } };
const ctx = { cwd: root, hasUI: scenario !== 'no-ui', ui: { setStatus(...v) { statuses.push(v); }, notify() {}, setEditorText() {}, async input() { return 'A narrower question'; }, async select() { if (scenario.startsWith('halted-dialog-')) return ({ 'halted-dialog-continue': 'Continue with one more budget', 'halted-dialog-discard': 'Discard it', 'halted-dialog-narrow': 'Narrow the question (new research task)', 'halted-dialog-escalate': 'Escalate to openai/gpt-6-sol (new research task)' })[scenario]; return scenario === 'alignment' ? 'Answer pending alignment' : scenario === 'decline' ? 'Do not dispatch' : 'Confirm dispatch'; } }, sessionManager: { getBranch() { return []; } } };
try {
  const extension = (await import(pathToFileURL(path.join(moduleDir, 'dispatcher.ts')).href)).default;
  extension(pi);
  if (['approved-changed', 'resolved-changed'].includes(scenario)) {
    task('auto-1', { decision: { review: 'required' }, review: 'review-1' });
    const reviewer = path.join(state, 'archive', 'review-1-ended-1');
    fs.mkdirSync(reviewer, { recursive: true });
    fs.writeFileSync(path.join(reviewer, 'meta.json'), JSON.stringify({ id: 'review-1', worker: 'claude' }));
    fs.writeFileSync(path.join(reviewer, 'review-of'), 'auto-1');
    const verdict = 'APPROVED\nindependently checked.' + COV;
    fs.writeFileSync(path.join(reviewer, 'result.md'), verdict);
    fs.writeFileSync(path.join(dir('auto-1'), 'review-required.json'), JSON.stringify(scenario === 'resolved-changed' ?
      { required: true, status: 'resolved', task_id: 'auto-1', round_nonce: 'r1', resolution: { actor: 'human_cli', round_nonce: 'r1', author_sha256: crypto.createHash('sha256').update(DONE).digest('hex') } } :
      { required: true, status: 'approved', task_id: 'auto-1', round_nonce: 'r1', approval: {
        reviewer_id: 'review-1', round_nonce: 'r1', evidence_sha256: crypto.createHash('sha256').update(verdict).digest('hex'),
        author_sha256: crypto.createHash('sha256').update(DONE).digest('hex') } }));
    const check = async () => { await handlers.input({ text: '+here Explain tp-view', source: 'interactive' }, ctx);
      const a = calls.filter(c => c.name === 'tp-task' && c.args[0] === 'preflight').at(-1).args;
      return JSON.parse(a[a.indexOf('--decision-json') + 1]).review; };
    const before = await check();
    fs.writeFileSync(path.join(dir('auto-1'), 'result.md'), 'Unreviewed change.');
    const after = await check();
    console.log(JSON.stringify({ before, after }));
    process.exit(0);
  }
  if (['resolved-inflight', 'resolved-inflight-error'].includes(scenario)) {
    task('auto-1', { decision: { review: 'required' }, review: 'review-live' });
    fs.writeFileSync(path.join(dir('auto-1'), 'review-required.json'), JSON.stringify({ required: true, status: 'pending_review', task_id: 'auto-1', round_nonce: crypto.randomUUID() }));
    fs.writeFileSync(path.join(dir('auto-1'), 'state.json'), JSON.stringify({ state: 'starting' }));
    await handlers.input({ text: '+followup auto-1 Begin review', source: 'interactive' }, ctx);
    fs.writeFileSync(path.join(dir('auto-1'), 'state.json'), JSON.stringify({ state: 'waiting', updated: new Date().toISOString() }));
    for (let i = 0; i < 85 && !fs.existsSync(path.join(dir('review-live'), 'review-of')); i++) await new Promise(r => setTimeout(r, 100));
    if (!fs.existsSync(path.join(dir('review-live'), 'review-of'))) throw new Error('review did not start');
    const env = { ...process.env, TP_STATE_DIR: state };
    const resolve = spawnSync(path.join(root, 'bin/tp-task'), ['review-resolve', '--task-id', 'auto-1', '--outcome', 'approved', '--reason', 'Human waiver during review', '--confirm', 'RESOLVE-REVIEW-REQUIRED'], { env, encoding: 'utf8' });
    if (resolve.status) throw new Error(resolve.stderr);
    fs.writeFileSync(path.join(dir('review-live'), 'state.json'), JSON.stringify({ state: scenario === 'resolved-inflight-error' ? 'error' : 'waiting', updated: new Date().toISOString() }));
    for (let i = 0; i < 85 && !(scenario === 'resolved-inflight-error' ?
      JSON.parse(fs.readFileSync(path.join(dir('auto-1'), 'review-required.json'))).status === 'needs_human' : globalThis.markerAtRework); i++) await new Promise(r => setTimeout(r, 100));
    if (scenario === 'resolved-inflight' && !globalThis.markerAtRework) throw new Error('rework did not start');
    const marker = JSON.parse(fs.readFileSync(path.join(dir('auto-1'), 'review-required.json')));
    const blocked = JSON.parse(actualTask(['review-list', '--json'])).blockers.some(b => b.id === 'auto-1');
    let collectDenied;
    if (scenario === 'resolved-inflight') {
      fs.writeFileSync(path.join(dir('auto-1'), 'state.json'), JSON.stringify({ state: 'waiting', updated: new Date().toISOString() }));
      const fakeBin = path.join(state, 'fake-bin'); fs.mkdirSync(fakeBin);
      fs.writeFileSync(path.join(fakeBin, 'tmux'), '#!/bin/sh\necho "%1 1 99999999"\n', { mode: 0o755 });
      collectDenied = spawnSync(path.join(root, 'bin/tp-contract'), ['collect', 'auto-1'], { env: { ...env, PATH: `${fakeBin}:${process.env.PATH}` }, encoding: 'utf8' }).status !== 0;
    }
    console.log(JSON.stringify({ before: globalThis.markerAtRework, marker, blocked, collectDenied, collected: calls.filter(c => c.name === 'tp-contract' && c.args[0] === 'collect').length }));
    process.exit(0);
  }
  if (['midreview-followup', 'midreview-pane', 'midreview-nonce', 'midreview-cap', 'midreview-reload'].includes(scenario)) {
    globalThis.midRounds = 0; globalThis.reviewSnapshots = [];
    task('auto-1', { decision: { review: 'required' } });
    fs.writeFileSync(path.join(dir('auto-1'), 'review-required.json'), JSON.stringify({ required: true, status: 'pending_review', task_id: 'auto-1', round_nonce: crypto.randomUUID() }));
    fs.writeFileSync(path.join(dir('auto-1'), 'result.md'), 'Round 1 result.');
    fs.writeFileSync(path.join(dir('auto-1'), 'state.json'), JSON.stringify({ state: 'starting', updated: new Date().toISOString() }));
    // Rejoin a watched task via CONTINUE (which re-arms) then settle round 1.
    await handlers.input({ text: '+followup auto-1 First report', source: 'interactive' }, ctx);
    fs.writeFileSync(path.join(dir('auto-1'), 'state.json'), JSON.stringify({ state: 'waiting', updated: new Date().toISOString() }));
    for (let i = 0; i < 85 && !globalThis.midRounds; i++) await new Promise(r => setTimeout(r, 100));
    if (!globalThis.midRounds) throw new Error('review did not start');
    const firstNonce = JSON.parse(fs.readFileSync(path.join(dir('auto-1'), 'review-required.json'))).round_nonce;
    if (scenario === 'midreview-reload') {
      handlers.session_shutdown({}, ctx); // Simulate Pi replacing this extension runtime.
      // Late reviewer settlement must not use the captured Pi context or collect.
      pi.sendMessage = () => { throw new Error('This extension ctx is stale after session replacement or reload'); };
      fs.writeFileSync(path.join(dir('review-1'), 'state.json'), JSON.stringify({ state: 'waiting', updated: new Date().toISOString() }));
      // Wait for observed poll activity after the late settlement instead of a fixed delay:
      // a compressed poll sleep must resolve after the write (the loop then re-checks state).
      // Shutdown can cancel further polls, so require one observed wake, not more cycles.
      const settledAt = Date.now(), ticks = () => (globalThis.__pollTicks || []).filter(t => t >= settledAt).length;
      for (let i = 0; i < 200 && !ticks(); i++) await new Promise(r => setTimeout(r, 25));
      if (!ticks()) throw new Error('no watcher/scanner poll tick observed after late settlement');
      await new Promise(r => setTimeout(r, 50)); // let the woken loop body run
      const marker = JSON.parse(fs.readFileSync(path.join(dir('auto-1'), 'review-required.json')));
      console.log(JSON.stringify({ marker, collected: calls.filter(c => c.name === 'tp-contract' && c.args[0] === 'collect').length,
        messages: messages.map(m => m.details?.kind), reviewer: globalThis.midRounds }));
      process.exit(0);
    }
    if (['midreview-followup', 'midreview-nonce'].includes(scenario)) await handlers.input({ text: '+followup auto-1 Also change X', source: 'interactive' }, ctx);
    if (scenario !== 'midreview-nonce') fs.writeFileSync(path.join(dir('auto-1'), 'result.md'), 'Round 2 UNREVIEWED result.');
    fs.writeFileSync(path.join(dir('auto-1'), 'state.json'), JSON.stringify({ state: 'waiting', updated: new Date().toISOString() }));
    const midMarker = JSON.parse(fs.readFileSync(path.join(dir('auto-1'), 'review-required.json')));
    fs.writeFileSync(path.join(dir('review-1'), 'state.json'), JSON.stringify({ state: 'waiting', updated: new Date().toISOString() }));
    if (scenario === 'midreview-cap') {
      for (let i = 0; i < 85 && globalThis.midRounds !== 2; i++) await new Promise(r => setTimeout(r, 100));
      if (globalThis.midRounds !== 2) throw new Error('second review did not start');
      fs.writeFileSync(path.join(dir('auto-1'), 'result.md'), 'Round 3 UNREVIEWED result.');
      fs.writeFileSync(path.join(dir('review-2'), 'state.json'), JSON.stringify({ state: 'waiting', updated: new Date().toISOString() }));
    }
    for (let i = 0; i < 85 && !messages.some(m => m.details?.kind === 'result' || m.details?.kind === 'review-needs-human'); i++) await new Promise(r => setTimeout(r, 100));
    const final = JSON.parse(fs.readFileSync(path.join(dir('auto-1'), 'review-required.json')));
    console.log(JSON.stringify({ firstNonce, midMarker, final, reviewSnapshots: globalThis.reviewSnapshots, rounds: globalThis.midRounds,
      messages: messages.map(m => m.details?.kind), collected: calls.filter(c => c.name === 'tp-contract' && c.args[0] === 'collect').length }));
    process.exit(0);
  }
  if (['rework-restart', 'rework-approved', 'resolved-restart', 'resolved-failed', 'resolved-changes', 'needs-human-followup', 'followup-symlinks'].includes(scenario)) {
    task('auto-1', { decision: { review: 'required' }, review: 'review-1' });
    task('review-1', { worker: 'claude' });
    fs.writeFileSync(path.join(dir('review-1'), 'review-of'), 'auto-1');
    const verdict = 'APPROVED\nprevious round.' + COV;
    fs.writeFileSync(path.join(dir('review-1'), 'result.md'), verdict);
    fs.writeFileSync(path.join(dir('auto-1'), 'review-required.json'), JSON.stringify(['needs-human-followup', 'followup-symlinks'].includes(scenario) ? { required: true, status: 'needs_human', task_id: 'auto-1', reasons: ['Earlier failure'] } : ['resolved-restart', 'resolved-failed', 'resolved-changes'].includes(scenario) ? { required: true, status: 'resolved', task_id: 'auto-1', resolution: { actor: 'human_cli', reason: 'Older work' } } : { required: true, status: 'approved', task_id: 'auto-1', approval: { reviewer_id: 'review-1', evidence_sha256: crypto.createHash('sha256').update(verdict).digest('hex'), author_sha256: crypto.createHash('sha256').update(DONE).digest('hex') } }));
    fs.writeFileSync(path.join(dir('auto-1'), 'completed'), '1\n');
    fs.writeFileSync(path.join(dir('auto-1'), 'state.json'), JSON.stringify({ state: 'starting' }));
    if (scenario === 'followup-symlinks') {
      // A link at followups.md and at meta.json: the append is refused and meta.json is replaced; nothing outside the state dir is written.
      const outside = fs.mkdtempSync(path.join(os.tmpdir(), 'tp-r5-outside-'));
      const outFollow = path.join(outside, 'followups.md'), outMeta = path.join(outside, 'meta.json');
      fs.writeFileSync(outFollow, 'OUTSIDE\n');
      fs.copyFileSync(path.join(dir('auto-1'), 'meta.json'), outMeta);
      const metaBefore = fs.readFileSync(outMeta, 'utf8');
      fs.symlinkSync(outFollow, path.join(dir('auto-1'), 'followups.md'));
      fs.rmSync(path.join(dir('auto-1'), 'meta.json'));
      fs.symlinkSync(outMeta, path.join(dir('auto-1'), 'meta.json'));
      // The input handler reports a failed send as a notice, not a throw.
      const notices = [];
      ctx.ui.notify = (message) => notices.push(message);
      await handlers.input({ text: '+followup auto-1 Please rework this', source: 'interactive' }, ctx);
      const refused = notices.splice(0).join('\n');
      console.log(JSON.stringify({ refused, outFollow: fs.readFileSync(outFollow, 'utf8'), metaUnchanged: fs.readFileSync(outMeta, 'utf8') === metaBefore,
        metaIsLink: fs.lstatSync(path.join(dir('auto-1'), 'meta.json')).isSymbolicLink(), outsideFiles: fs.readdirSync(outside).sort() }));
      process.exit(0);
    }
    await handlers.input({ text: '+followup auto-1 Please rework this', source: 'interactive' }, ctx);
    const pending = JSON.parse(fs.readFileSync(path.join(dir('auto-1'), 'review-required.json')));
    const staleCompleted = fs.existsSync(path.join(dir('auto-1'), 'completed'));
    const fakeBin = path.join(state, 'fake-bin'); fs.mkdirSync(fakeBin);
    fs.writeFileSync(path.join(fakeBin, 'tmux'), '#!/bin/sh\necho "%1 1 99999999"\n', { mode: 0o755 });
    const env = { ...process.env, TP_STATE_DIR: state, PATH: `${fakeBin}:${process.env.PATH}` };
    const collect = () => spawnSync(path.join(root, 'bin/tp-contract'), ['collect', 'auto-1'], { env, encoding: 'utf8' });
    const denied = collect().status !== 0;
    let restartBlocked, humanUnblocked;
    if (['rework-restart', 'resolved-restart'].includes(scenario)) {
      const restarted = {};
      extension({ on(name, fn) { restarted[name] = fn; }, registerTool() {}, registerCommand() {}, sendMessage() {} });
      const visible = spawnSync(path.join(root, 'bin/tp-task'), ['--state-dir', state, 'review-list', '--json'], { env, encoding: 'utf8' });
      const listing = JSON.parse(visible.stdout);
      // A fresh dispatcher has an empty delegated map and must inherit the gate.
      await restarted.input({ text: '+here Explain tp-view', source: 'interactive' }, ctx);
      const preflight = calls.filter(c => c.name === 'tp-task' && c.args[0] === 'preflight').at(-1).args;
      const inherited = JSON.parse(preflight[preflight.indexOf('--decision-json') + 1]);
      restartBlocked = listing.blockers.some(x => x.id === 'auto-1' && x.status === 'pending_review') && inherited.review === 'required';
      const resolution = spawnSync(path.join(root, 'bin/tp-task'), ['--state-dir', state, 'review-resolve', '--task-id', 'auto-1', '--outcome', 'approved', '--reason', 'Human inspected rework', '--confirm', 'RESOLVE-REVIEW-REQUIRED'], { env, encoding: 'utf8' });
      if (resolution.status !== 0) throw new Error(resolution.stderr);
      const fresh = {};
      extension({ on(name, fn) { fresh[name] = fn; }, registerTool() {}, registerCommand() {}, sendMessage() {} });
      await fresh.input({ text: '+here Explain tp-view', source: 'interactive' }, ctx);
      const after = calls.filter(c => c.name === 'tp-task' && c.args[0] === 'preflight').at(-1).args;
      humanUnblocked = JSON.parse(after[after.indexOf('--decision-json') + 1]).review === 'none';
    }
    let approved;
    if (scenario === 'rework-approved') {
      for (let i = 0; i < 75 && JSON.parse(fs.readFileSync(path.join(dir('auto-1'), 'review-required.json'))).status !== 'approved'; i++) await new Promise(r => setTimeout(r, 100));
      approved = JSON.parse(fs.readFileSync(path.join(dir('auto-1'), 'review-required.json')));
    }
    const collected = approved ? collect().status === 0 : false;
    if (approved) fs.writeFileSync(path.join(dir('auto-1'), 'result.md'), 'Unreviewed edits after approval.');
    const changedResultDenied = approved ? collect().status !== 0 : undefined;
    if (['resolved-failed', 'resolved-changes'].includes(scenario)) {
      fs.writeFileSync(path.join(dir('auto-1'), 'result.md'), 'New follow-up result.');
      fs.writeFileSync(path.join(dir('auto-1'), 'state.json'), JSON.stringify({ state: 'waiting', updated: new Date().toISOString() }));
      for (let i = 0; i < 85 && JSON.parse(fs.readFileSync(path.join(dir('auto-1'), 'review-required.json'))).status !== 'needs_human'; i++) await new Promise(r => setTimeout(r, 100));
    }
    console.log(JSON.stringify({ pending, staleCompleted, denied, restartBlocked, humanUnblocked, approved, collected, changedResultDenied, final: JSON.parse(fs.readFileSync(path.join(dir('auto-1'), 'review-required.json'))), rounds: calls.filter(c => c.name === 'tp-review').length, messages: messages.map(m => m.details?.kind), decision: JSON.parse(fs.readFileSync(path.join(dir('auto-1'), 'meta.json'))).decision }));
    process.exit(0);
  }
  if (scenario === 'read-tools') {
    const allowed = {};
    for (const name of ['read', 'grep', 'find', 'ls', 'list_subagents', 'collect_result', 'powershell', 'mutating_extension']) {
      allowed[name] = !(await handlers.tool_call({ toolName: name, input: {} }))?.block;
    }
    console.log(JSON.stringify(allowed));
    process.exit(0);
  }
  if (scenario.startsWith('halted-dialog-')) {
    task('source-1', { decision: { review: 'required' }, contract: { kind: 'research', artifact: path.join(dir('source-1'), 'result.md') } });
    fs.writeFileSync(path.join(dir('source-1'), 'review-required.json'), JSON.stringify({ required: true, status: 'pending_review', task_id: 'source-1', round_nonce: 'halted-round' }));
    handlers.session_start({}, ctx);
    for (let i = 0; i < 85 && !messages.some(m => ['discarded', 'follow-up', 'halted'].includes(m.details?.kind)); i++) await new Promise(r => setTimeout(r, 100));
    if (scenario === 'halted-dialog-continue') for (let i = 0; i < 85 && !globalThis.markerAtContinue; i++) await new Promise(r => setTimeout(r, 100));
    if (['halted-dialog-narrow', 'halted-dialog-escalate'].includes(scenario)) for (let i = 0; i < 85 && !fs.existsSync(path.join(dir('auto-1'), 'review-required.json')); i++) await new Promise(r => setTimeout(r, 100));
    const sourceMarker = JSON.parse(fs.readFileSync(path.join(dir('source-1'), 'review-required.json')));
    const newMarker = fs.existsSync(path.join(dir('auto-1'), 'review-required.json')) ? JSON.parse(fs.readFileSync(path.join(dir('auto-1'), 'review-required.json'))) : undefined;
    console.log(JSON.stringify({ sourceMarker, newMarker, markerAtContinue: globalThis.markerAtContinue,
      sourceBlocked: JSON.parse(actualTask(['review-list', '--json'])).blockers.some(b => b.id === 'source-1'),
      messages: messages.map(m => m.details?.kind) }));
    process.exit(0);
  }
  if (['halted-restart', 'halted-marker-restart'].includes(scenario)) {
    task('source-1', { decision: { review: scenario === 'halted-restart' ? 'required' : 'none' } });
    fs.writeFileSync(path.join(dir('source-1'), 'review-required.json'), JSON.stringify({ required: true, status: 'pending_review', task_id: 'source-1' }));
    handlers.session_start({}, ctx);
    for (let i = 0; i < 50 && !fs.existsSync(path.join(dir('auto-1'), 'review-required.json')); i++) await new Promise(r => setTimeout(r, 100));
    for (let i = 0; i < 30 && !JSON.parse(fs.readFileSync(path.join(dir('source-1'), 'review-required.json'))).review_target; i++) await new Promise(r => setTimeout(r, 50));
    const marker = JSON.parse(fs.readFileSync(path.join(dir('auto-1'), 'review-required.json')));
    await handlers.input({ text: '+here Explain tp-view', source: 'interactive' }, ctx);
    const args = calls.filter(c => c.name === 'tp-task' && c.args[0] === 'preflight').at(-1).args;
    const inheritedWhileWatched = JSON.parse(args[args.indexOf('--decision-json') + 1]).review;
    console.log(JSON.stringify({ inheritedWhileWatched, target: marker, decision: JSON.parse(fs.readFileSync(path.join(dir('auto-1'), 'meta.json'))).decision, source: JSON.parse(fs.readFileSync(path.join(dir('source-1'), 'review-required.json'))), lineage: JSON.parse(fs.readFileSync(path.join(dir('auto-1'), 'meta.json'))).escalated_from }));
    process.exit(0);
  }
  const original = ['goal', 'goal-spoof'].includes(scenario) ? `TP_GOAL_CONTINUATION goal ${'a'.repeat(32)} ${'b'.repeat(64)}\nFix parser in src/parser.py` : ['stop', 'stop-no-commit', 'stop-retry'].includes(scenario) ? '+plan Plan the retry boundary' : ['slash', 'slash-off'].includes(scenario) ? '/template Fix typo in README.md' : request;
  const source = ['extension', 'extension-off', 'goal'].includes(scenario) ? 'extension' : 'interactive';
  if (['slash-off', 'extension-off'].includes(scenario)) await pi.command.handler('off', ctx);
  const noHand = async () => ({ edit: !!(await handlers.tool_call({ toolName: 'edit', input: {} }))?.block, bash: !!(await handlers.tool_call({ toolName: 'bash', input: { command: 'sed -i x README.md' } }))?.block, powershell: !!(await handlers.tool_call({ toolName: 'powershell', input: { command: 'Remove-Item x' } }))?.block, unknown: !!(await handlers.tool_call({ toolName: 'mutating_extension', input: {} }))?.block, read: !!(await handlers.tool_call({ toolName: 'read', input: { path: 'README.md' } }))?.block, contract: !!(await handlers.tool_call({ toolName: 'tmux_pi_commit_contract', input: {} }))?.block });
  if (['review-turn', 'resolved-turn', 'forged-resolved'].includes(scenario)) {
    const handoff = path.join(state, 'handoffs', 'earlier');
    fs.mkdirSync(handoff, { recursive: true });
    fs.writeFileSync(path.join(handoff, 'review-required.json'), JSON.stringify({ handshake_id: 'earlier', status: ['resolved-turn', 'forged-resolved'].includes(scenario) ? 'resolved' : 'needs_human', required: true, ...(['resolved-turn', 'forged-resolved'].includes(scenario) ? { resolution: { actor: scenario === 'resolved-turn' ? 'human_cli' : 'model' } } : {}) }));
  }
  if (['task-blocker', 'archived-blocker', 'archived-renamed', 'unknown-marker', 'wrong-id-resolved', 'malformed-marker'].includes(scenario)) {
    const base = path.join(state, ['archived-blocker', 'archived-renamed'].includes(scenario) ? 'archive' : 'tasks', scenario === 'archived-renamed' ? 'earlier-task-ended-1' : 'earlier-task');
    fs.mkdirSync(base, { recursive: true });
    fs.writeFileSync(path.join(base, 'meta.json'), JSON.stringify({ id: 'earlier-task', worker: 'pi' }));
    fs.writeFileSync(path.join(base, 'review-required.json'), scenario === 'malformed-marker' ? '{bad json' : JSON.stringify({ status: scenario === 'unknown-marker' ? 'unexpected' : scenario === 'wrong-id-resolved' ? 'resolved' : 'needs_human', required: true, task_id: scenario === 'wrong-id-resolved' ? 'another-task' : 'earlier-task', ...(scenario === 'wrong-id-resolved' ? { resolution: { actor: 'human_cli' } } : {}) }));
  }
  if (scenario === 'no-hand') { console.log(JSON.stringify({ guards: await noHand() })); process.exitCode = 0; }
  else {
  if (scenario === 'queued') {
    const queued = await handlers.input({ text: original, source, streamingBehavior: 'followUp' }, ctx);
    console.log(JSON.stringify({ queued, guards: await noHand() })); process.exitCode = 0;
  } else {
  const inputResult = await handlers.input({ text: original, source }, ctx);
  const before = await handlers.before_agent_start({ type: 'before_agent_start', prompt: ['expanded', 'expanded-marker'].includes(scenario) ? 'Update password hashing in auth.py' : inputResult.text ?? original });
  if (scenario === 'malformed-marker') {
    console.log(JSON.stringify({ inputResult, blocked: await noHand(), calls: calls.map(c => c.name) }));
    process.exitCode = 0;
  } else {
  const guard = async (toolName, command) => handlers.tool_call({ toolName, input: { command } });
  const preContractTools = await noHand();
  const guards = {};
  for (const [key, toolName, cmd] of [['send', 'send_to_subagent', ''], ['resolve', 'bash', 'bin/tp-task capability-resolve auto-1 --approve'], ['globalResolve', 'bash', 'bin/tp-task --state-dir /tmp/state capability-resolve auto-1 --approve'], ['globalReviewResolve', 'bash', 'bin/tp-task --state-dir /tmp/state review-resolve r5-test --outcome approved'], ['reviewResolve', 'bash', 'bin/tp-task review-resolve r5-test --outcome approved'], ['gitOutput', 'bash', 'git log -1 --format=pwned --output=README.md'], ['gitShowOutput', 'bash', 'git show --output=/tmp/x HEAD'], ['rgPre', 'bash', 'rg --pre sh x .'], ['rgPreEq', 'bash', 'rg -uuu --pre=bash . x'], ['extDiff', 'bash', 'git diff --ext-diff'], ['absolute', 'bash', '/tmp/tmux-pi/bin/tp-spawn --model x'], ['variable', 'bash', '"$ROOT/bin/tp-spawn" x'], ['tmux', 'bash', 'tmux neww'], ['shellwrite', 'bash', 'python3 -c "open(\"x\",\"w\")"'], ['read', 'bash', 'rg foo README.md'], ['edit', 'edit', '']]) guards[key] = !!(await guard(toolName, cmd))?.block;
  if (['slash', 'extension', 'slash-off', 'extension-off'].includes(scenario)) {
    console.log(JSON.stringify({ inputResult, hidden: before.message.content.includes('Handshake:'), guards, calls: calls.map(c => ({ name: c.name, args: c.args })) }));
  } else if (scenario === 'delegate-omitted') {
    // Exercise the real hidden-message event and a final answer with no contract tool call.
    await handlers.agent_end({ messages: [{ role: 'assistant', content: [{ type: 'text', text: 'Cedar is green.' }] }] }, ctx);
    await handlers.agent_settled({ type: 'agent_settled' }, ctx);
    console.log(JSON.stringify({ hidden: before.message, inputResult, preContractTools, after: await noHand(),
      calls: calls.map(c => ({ name: c.name, args: c.args })), messages }));
    process.exitCode = 0;
  } else if (scenario === 'stop-no-commit') {
    await handlers.agent_end({ messages: [{ role: 'assistant', content: [{ type: 'text', text: 'Plan without tool call.' }] }] }, ctx);
    await handlers.agent_settled({ type: 'agent_settled' }, ctx);
    console.log(JSON.stringify({ captured: fs.readFileSync(path.join(dir('stop-1'), 'captured.md'), 'utf8'), contractCalls: calls.filter(c => c.name === 'tp-task' && c.args[0] === 'main-contract').length }));
    process.exitCode = 0;
  } else {
  const match = /Handshake: (\S+)\nRequest SHA-256: (\S+)/.exec(before.message.content);
  const rawHash = crypto.createHash('sha256').update(original).digest('hex');
  let staleHashRejected = false;
  if (['expanded', 'expanded-marker'].includes(scenario)) {
    try { await pi.tool.execute('stale', { handshake_id: match[1], request_sha256: rawHash, action: 'direct', mode: 'direct', task_summary: 'stale', frame: 'stale', assumptions: [], no_edits: false, scope: 'brief', risk_notes: [], proof_path: [], done_when: [] }, null, null, ctx); } catch { staleHashRejected = true; }
  }
  const params = { handshake_id: match[1], request_sha256: match[2], action: scenario === 'goal' ? 'delegate' : ['stop', 'stop-retry'].includes(scenario) ? 'stop' : scenario === 'alignment' ? 'ask_alignment' : ['review', 'review-failed', 'followup-blocked', 'kill', 'capability', 'capability-resolved', 'capability-deleted', 'confirm', 'decline', 'auto-off', 'no-ui', 'main-pi', 'review-gate', 'review-invalid', 'formatted-verdict', 'unknown-verdict', 'notes-empty', 'notes-suffix', 'approved-suffix', 'no-coverage', 'empty-coverage', 'notes-quote', 'notes-verdict', 'review-changes', 'research-gate'].includes(scenario) ? 'delegate' : 'direct', mode: scenario === 'goal' ? 'delegated' : ['stop', 'stop-retry'].includes(scenario) ? 'plan' : ['review', 'review-failed', 'main-pi', 'followup-blocked', 'kill', 'capability', 'capability-resolved', 'capability-deleted', 'confirm', 'decline', 'auto-off', 'no-ui', 'review-gate', 'review-invalid', 'formatted-verdict', 'unknown-verdict', 'notes-empty', 'notes-suffix', 'approved-suffix', 'no-coverage', 'empty-coverage', 'notes-quote', 'notes-verdict', 'review-changes', 'research-gate'].includes(scenario) ? 'delegated' : 'direct', task_summary: scenario === 'goal' ? 'Fix parser in src/parser.py' : 'Scoped work', frame: 'Only requested work', assumptions: [], no_edits: ['stop', 'stop-retry'].includes(scenario), scope: 'brief', risk_notes: [], proof_path: ['diff'], done_when: ['done'] };
  let mismatch = false, wrongStop = false;
  try { await pi.tool.execute('1', { ...params, handshake_id: 'wrong' }, null, null, ctx); } catch { mismatch = true; }
  if (['stop', 'stop-retry'].includes(scenario)) { try { await pi.tool.execute('1', { ...params, action: 'direct', mode: 'direct' }, null, null, ctx); } catch { wrongStop = true; } }
  if (scenario === 'alignment') params.material_ambiguity = { question: 'Which boundary?', why_material: 'Changes scope.' };
  if (scenario === 'retry-contract') await handlers.agent_end({ messages: [] }, ctx);
  const committed = await pi.tool.execute('2', params, null, null, ctx);
  let duplicate = false;
  try { await pi.tool.execute('3', params, null, null, ctx); } catch { duplicate = true; }
  if (scenario === 'auto-off' || scenario === 'direct-off') await pi.command.handler('off', ctx);
  let continuation;
  if (scenario === 'alignment') continuation = await handlers.input({ text: 'Only the HTTP endpoint', source: 'interactive' }, ctx);
  const postContractResolve = !!(await guard('bash', 'bin/tp-task --state-dir /tmp/state capability-resolve auto-1 --approve'))?.block;
  const postContractReviewResolve = !!(await guard('bash', 'bin/tp-task --state-dir /tmp/state review-resolve r5-test --outcome approved'))?.block;
  const editAfter = !!(await guard('edit', ''))?.block;
  if (['goal', 'review', 'notes-verdict', 'notes-quote', 'notes-suffix', 'approved-suffix', 'no-coverage', 'empty-coverage', 'review-failed', 'followup-blocked', 'kill', 'capability', 'capability-resolved', 'capability-deleted', 'confirm', 'decline', 'auto-off', 'no-ui', 'review-gate', 'review-invalid', 'formatted-verdict', 'unknown-verdict', 'notes-empty', 'review-changes', 'research-gate'].includes(scenario)) {
    await handlers.agent_end({ messages: [] }, ctx);
    await new Promise(resolve => setTimeout(resolve, 80));
    if (scenario === 'followup-blocked') {
      const handoff = path.join(state, 'handoffs', 'earlier');
      fs.mkdirSync(handoff, { recursive: true });
      fs.writeFileSync(path.join(handoff, 'review-required.json'), JSON.stringify({ status: 'needs_human', required: true, handshake_id: 'earlier' }));
      await handlers.input({ text: '+followup auto-1 Please check the same change', source: 'interactive' }, ctx);
      fs.writeFileSync(path.join(dir('auto-1'), 'state.json'), JSON.stringify({ state: 'waiting', updated: new Date().toISOString() }));
      for (let i = 0; !calls.some(c => c.name === 'tp-review') && i < 40; i++) await new Promise(resolve => setTimeout(resolve, 100));
    }
    if (scenario === 'research-gate') {
      for (let i = 0; i < 40 && !fs.existsSync(path.join(dir('auto-1'), 'review-required.json')); i++) await new Promise(resolve => setTimeout(resolve, 100));
      globalThis.researchGate = { marker: JSON.parse(fs.readFileSync(path.join(dir('auto-1'), 'review-required.json'))), decision: JSON.parse(fs.readFileSync(path.join(dir('auto-1'), 'meta.json'))).decision };
    }
    if (scenario === 'goal') {
      for (let i = 0; i < 60 && !calls.some(c => c.name === 'tp-contract' && c.args[0] === 'collect'); i++) await new Promise(resolve => setTimeout(resolve, 100));
    }
    if (scenario === 'review-gate') {
      const fakeBin = path.join(state, 'fake-bin'); fs.mkdirSync(fakeBin);
      fs.writeFileSync(path.join(fakeBin, 'tmux'), '#!/bin/sh\necho "%1 1 99999999"\n', { mode: 0o755 });
      const env = { ...process.env, TP_STATE_DIR: state, PATH: `${fakeBin}:${process.env.PATH}` };
      const collect = () => spawnSync(path.join(root, 'bin/tp-contract'), ['collect', 'auto-1'], { env, encoding: 'utf8' });
      const marker = JSON.parse(fs.readFileSync(path.join(dir('auto-1'), 'review-required.json')));
      const preReviewCollectDenied = collect().status !== 0;
      const listing = spawnSync(path.join(root, 'bin/tp-task'), ['--state-dir', state, 'review-list'], { env, encoding: 'utf8' });
      const restartCollectDenied = collect().status !== 0 && listing.stdout.includes('auto-1');
      fs.writeFileSync(path.join(dir('auto-1'), 'exit'), '0\n');
      fs.writeFileSync(path.join(dir('auto-1'), 'state.json'), JSON.stringify({ state: 'waiting', updated: new Date().toISOString() }));
      for (let i = 0; !calls.some(c => c.name === 'tp-contract' && c.args[0] === 'collect') && i < 85; i++) await new Promise(resolve => setTimeout(resolve, 100));
      const approval = JSON.parse(fs.readFileSync(path.join(dir('auto-1'), 'review-required.json')));
      const approvedCollect = collect();
      globalThis.reviewGate = { marker, preReviewCollectDenied, restartCollectDenied, approval, approvedCollect: approvedCollect.status === 0 && approvedCollect.stdout.trim() === 'completed' };
    }
    if (scenario === 'review-changes') {
      for (let i = 0; i < 85 && (!fs.existsSync(path.join(dir('auto-1'), 'review-required.json')) || JSON.parse(fs.readFileSync(path.join(dir('auto-1'), 'review-required.json'))).status !== 'needs_human'); i++) await new Promise(resolve => setTimeout(resolve, 100));
    }
    if (['review-invalid', 'formatted-verdict', 'unknown-verdict', 'notes-empty', 'notes-suffix', 'approved-suffix', 'no-coverage', 'empty-coverage'].includes(scenario)) {
      for (let i = 0; i < 80 && (!fs.existsSync(path.join(dir('auto-1'), 'review-required.json')) || JSON.parse(fs.readFileSync(path.join(dir('auto-1'), 'review-required.json'))).status !== 'needs_human'); i++) await new Promise(resolve => setTimeout(resolve, 100));
    }
    if (['notes-verdict', 'notes-quote'].includes(scenario)) {
      for (let i = 0; i < 85 && !messages.some(m => m.details?.kind === 'result'); i++) await new Promise(resolve => setTimeout(resolve, 100));
    }
    if (scenario === 'review-failed') {
      for (let i = 0; !fs.existsSync(path.join(dir('auto-1'), 'review-required.json')) && i < 80; i++) await new Promise(resolve => setTimeout(resolve, 100));
    }
    if (scenario === 'capability-deleted') {
      const pending = path.join(dir('auto-1'), 'pending-capability.json');
      for (let i = 0; !fs.existsSync(pending) && i < 80; i++) await new Promise(resolve => setTimeout(resolve, 100));
      if (!fs.existsSync(pending)) throw new Error('capability gate missing');
      fs.rmSync(pending);
      fs.writeFileSync(path.join(dir('auto-1'), 'result.md'), 'Worker attempted to bypass gate.');
      await new Promise(resolve => setTimeout(resolve, 800));
    }
    if (scenario === 'capability-resolved') {
      const pending = path.join(dir('auto-1'), 'pending-capability.json');
      for (let i = 0; !fs.existsSync(pending) && i < 80; i++) await new Promise(resolve => setTimeout(resolve, 100));
      if (!fs.existsSync(pending)) throw new Error('capability gate was not recorded');
      const fakeBin = path.join(state, 'fake-bin');
      fs.mkdirSync(fakeBin);
      fs.writeFileSync(path.join(fakeBin, 'tmux'), '#!/bin/sh\nexit 0\n', { mode: 0o755 });
      // Real human CLI, but its tmux signal goes only to a fake executable.
      const env = { ...process.env, TP_TASK_DIR: '', TP_STATE_DIR: state, PATH: `${fakeBin}:${process.env.PATH}` };
      const resolved = spawnSync(path.join(root, 'bin/tp-task'), ['capability-resolve', 'auto-1', '--deny', '--json'], { cwd: root, env, encoding: 'utf8' });
      if (resolved.status !== 0) throw new Error(resolved.stderr || resolved.stdout);
      // A fresh result/state is necessary: the old waiting result was a request.
      await new Promise(resolve => setTimeout(resolve, 650));
      fs.writeFileSync(path.join(dir('auto-1'), 'result.md'), 'Done after human denial.');
      fs.writeFileSync(path.join(dir('auto-1'), 'state.json'), JSON.stringify({ state: 'waiting', updated: new Date().toISOString() }));
      for (let i = 0; !calls.some(c => c.name === 'tp-contract' && c.args[0] === 'collect') && i < 85; i++) await new Promise(resolve => setTimeout(resolve, 100));
    }
  } else await handlers.agent_end({ messages: [{ role: 'assistant', content: [{ type: 'text', text: scenario === 'stop-retry' ? 'Partial.' : 'A bounded plan.' }] }] }, ctx);
  const afterEndBeforeSettle = await noHand();
  const stopCapturedBeforeSettle = fs.existsSync(path.join(dir('stop-1'), 'captured.md'));
  if (scenario === 'stop-retry') await handlers.agent_end({ messages: [{ role: 'assistant', content: [{ type: 'text', text: 'Retried final plan.' }] }] }, ctx);
  await handlers.agent_settled({ type: 'agent_settled' }, ctx);
  if (scenario === 'goal') {
    const follow = `TP_GOAL_CONTINUATION goal ${'c'.repeat(32)} ${'b'.repeat(64)}\nVerify the parser acceptance evidence`;
    const next = await handlers.input({ text: follow, source: 'extension' }, ctx);
    const hidden = await handlers.before_agent_start({ type: 'before_agent_start', prompt: next.text ?? follow });
    globalThis.goalNextHandshake = hidden?.message?.content?.includes('Handshake:') || false;
  }
  let inheritedTaskReview = false;
  if (scenario === 'review-failed') {
    const next = await handlers.input({ text: '+here Explain tp-view', source: 'interactive' }, ctx);
    inheritedTaskReview = JSON.parse(calls.filter(c => c.name === 'tp-task' && c.args[0] === 'preflight').at(-1).args.at(-1)).review === 'required';
    await handlers.before_agent_start({ type: 'before_agent_start', prompt: next.text ?? 'Explain tp-view' });
  }
  console.log(JSON.stringify({ guards, preContractTools, mismatch, inheritedTaskReview, staleHashRejected, wrongStop, duplicate, continuation, postContractResolve, postContractReviewResolve, afterEndBeforeSettle, stopCapturedBeforeSettle, committed: committed.details, terminate: committed.terminate ?? false, editAfter, afterEnd: await noHand(), capabilityResolution: scenario === 'capability-resolved' ? JSON.parse(fs.readFileSync(path.join(dir('auto-1'), 'checkpoints.json'))).events.some(e => e.action === 'capability-resolve') : undefined, calls: calls.map(c => ({ name: c.name, args: c.args })), messages: messages.map(m => ({ kind: m.details?.kind, content: m.content })), stopCaptured: fs.existsSync(path.join(dir('stop-1'), 'captured.md')), capturedOutput: fs.existsSync(path.join(dir('stop-1'), 'captured.md')) ? fs.readFileSync(path.join(dir('stop-1'), 'captured.md'), 'utf8') : undefined, reviewBlocker: fs.readdirSync(path.join(state, 'handoffs')).some(id => fs.existsSync(path.join(state, 'handoffs', id, 'review-required.json'))), decisionAtContract: JSON.parse(calls.find(c => c.name === 'tp-task' && c.args[0] === 'main-contract').args[calls.find(c => c.name === 'tp-task' && c.args[0] === 'main-contract').args.indexOf('--decision-json') + 1]), scaffold: fs.readFileSync(path.join(state, 'handoffs', match[1], 'scaffold.json'), 'utf8'), mainContract: JSON.parse(fs.readFileSync(path.join(state, 'handoffs', match[1], 'main-contract.json'))), handoffs: fs.readdirSync(path.join(state, 'handoffs')).length, newReviewMarkers: fs.readdirSync(path.join(state, 'handoffs')).filter(id => fs.existsSync(path.join(state, 'handoffs', id, 'review-required.json'))), taskReviewMarker: fs.existsSync(path.join(dir('auto-1'), 'review-required.json')), taskReview: fs.existsSync(path.join(dir('auto-1'), 'review-required.json')) ? JSON.parse(fs.readFileSync(path.join(dir('auto-1'), 'review-required.json'))).status : undefined, reviewReasons: fs.existsSync(path.join(dir('auto-1'), 'review-required.json')) ? JSON.parse(fs.readFileSync(path.join(dir('auto-1'), 'review-required.json'))).reasons : undefined, reviewGate: globalThis.reviewGate, researchGate: globalThis.researchGate, goalNextHandshake: globalThis.goalNextHandshake, reviewRoundMarkers: globalThis.reviewRoundMarkers, upgradeAtSend: globalThis.upgradeAtSend, inheritedReview: JSON.parse((() => { const a = calls.find(c => c.name === 'tp-task' && c.args[0] === 'preflight').args; return a.includes('--decision-json') ? a[a.indexOf('--decision-json') + 1] : '{}'; })()).review }));
  }
  }
  }
  }
} finally { fs.rmSync(state, { recursive: true, force: true }); fs.rmSync(moduleDir, { recursive: true, force: true }); }
