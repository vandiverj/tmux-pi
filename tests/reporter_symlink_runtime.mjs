// Pi reporters (reporter.ts, sensitive.ts, escalate.ts) and their shared state-write helper against planted symlinks.
// A link at a state file or at a directory below the trusted root, pointing outside it, is replaced or refused and
// never written through: the outside file is byte-for-byte unchanged. Synthetic data only.
// Usage: node --experimental-strip-types tests/reporter_symlink_runtime.mjs
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import assert from 'node:assert/strict';

const root = path.resolve(import.meta.dirname, '..');
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tp-symlink-'));
fs.writeFileSync(path.join(tmp, 'package.json'), '{"type":"module"}\n');
for (const name of ['reporter', 'sensitive', 'escalate']) fs.copyFileSync(path.join(root, `agent/extensions/${name}.ts`), path.join(tmp, `${name}.ts`));
fs.cpSync(path.join(root, 'agent/extensions/lib'), path.join(tmp, 'lib'), { recursive: true });
const load = async (name) => (await import(pathToFileURL(path.join(tmp, `${name}.ts`)).href)).default;
const helper = await import(pathToFileURL(path.join(tmp, 'lib/state-write.ts')).href);

const SENTINEL = 'OUTSIDE-MUST-NOT-CHANGE\n';
const outside = path.join(tmp, 'outside');
fs.mkdirSync(outside);
// A fresh outside file per plant, so each check is on its own bytes.
let planted = 0;
const outsideFile = () => {
  const file = path.join(outside, `target-${planted++}`);
  fs.writeFileSync(file, SENTINEL);
  return file;
};
const unchanged = (file) => assert.equal(fs.readFileSync(file, 'utf8'), SENTINEL, `${file} was written through a symlink`);
const isLink = (p) => fs.lstatSync(p).isSymbolicLink();
const plant = (link) => { const target = outsideFile(); fs.symlinkSync(target, link); return target; };
const freshDir = (name) => { const dir = path.join(tmp, name); fs.mkdirSync(dir); return dir; };
// A task dir in the layout tp-spawn makes: <name>/tasks/<id>.
const freshTask = (name, id = 'w1') => { const dir = path.join(tmp, name, 'tasks', id); fs.mkdirSync(dir, { recursive: true }); return dir; };

// --- helper: whole-file write replaces a link at the target, and refuses a link above it.
{
  const dir = freshDir('h-replace');
  const target = plant(path.join(dir, 'state.json'));
  helper.writeFileAtomic(dir, path.join(dir, 'state.json'), 'new\n');
  unchanged(target);
  assert.equal(isLink(path.join(dir, 'state.json')), false);
  assert.equal(fs.readFileSync(path.join(dir, 'state.json'), 'utf8'), 'new\n');
  assert.deepEqual(fs.readdirSync(dir), ['state.json'], 'a temp file was left behind');

  // A link at the file's temp name would be followed by a plain write; the exclusive, no-follow open never reuses a name.
  const trusted = freshDir('h-ancestor');
  fs.symlinkSync(outside, path.join(trusted, 'tasks'));
  const before = fs.readdirSync(outside).sort();
  assert.throws(() => helper.writeFileAtomic(trusted, path.join(trusted, 'tasks', 'x', 'meta.json'), 'x'), /symlink/);
  assert.throws(() => helper.ensureDir(trusted, path.join(trusted, 'tasks', 'x')), /symlink/);
  assert.deepEqual(fs.readdirSync(outside).sort(), before, 'a file or dir was created through an ancestor link');

  // The mode argument still sets the file mode.
  helper.writeFileAtomic(dir, path.join(dir, 'private.json'), '{}\n', 0o600);
  assert.equal(fs.statSync(path.join(dir, 'private.json')).mode & 0o777, 0o600);
  // An explicit mode wins over an existing file's mode: escalate.ts asks 0600 for meta.json, which tp-spawn may leave 0644.
  const oldMask = process.umask(0o022);
  try {
    const meta = path.join(dir, 'meta-0644.json');
    fs.writeFileSync(meta, 'old\n', { mode: 0o644 });
    helper.writeFileAtomic(dir, meta, 'new\n', 0o600);
    assert.equal(fs.statSync(meta).mode & 0o7777, 0o600, 'an explicit 0600 left an existing 0644 file at 0644');
    assert.equal(fs.readFileSync(meta, 'utf8'), 'new\n');
  } finally { process.umask(oldMask); }
  // Each caller gets the mode its old writer gave, for an existing file at several modes, a new file and a replaced link, under
  // several umasks. Explicit mode (0600 escalate metadata; 0666 reporter, sensitive, dispatcher JSON) = the old temp+rename;
  // no mode (dispatcher queue, escalation.md, escalation-error.txt) = the old in-place writeFileSync.
  const oldTempRename = (file, mode) => {
    const temp = `${file}.old.tmp`;
    fs.writeFileSync(temp, 'new\n', mode === undefined ? { flag: 'wx' } : { flag: 'wx', mode });
    fs.renameSync(temp, file);
  };
  const oldInPlace = (file) => fs.writeFileSync(file, 'new\n');
  const cases = [[0o600, oldTempRename], [0o666, oldTempRename], [undefined, oldInPlace]];
  let n = 0;
  for (const mask of [0o022, 0o077, 0o002]) {
    const saved = process.umask(mask);
    try {
      for (const [mode, oldWriter] of cases) {
        for (const start of [0o600, 0o644, 0o664, 0o640, 'missing', 'link']) {
          const modeAfter = (write, old = false) => {
            const file = path.join(freshDir(`h-mode-${n++}`), 'f.json');
            let target;
            if (start === 'link') { target = plant(file); fs.chmodSync(target, 0o666); }
            else if (start !== 'missing') { fs.writeFileSync(file, 'old\n'); fs.chmodSync(file, start); }
            // The old in-place writer wrote through a link; what it should have done is write a new file there.
            if (start === 'link' && old && oldWriter === oldInPlace) fs.rmSync(file);
            write(file);
            if (target) unchanged(target);
            assert.equal(fs.readFileSync(file, 'utf8'), 'new\n');
            return fs.statSync(file).mode & 0o7777;
          };
          const label = `mode ${mode?.toString(8)} over ${typeof start === 'number' ? start.toString(8) : start} under umask ${mask.toString(8)}`;
          assert.equal(modeAfter((file) => helper.writeFileAtomic(path.dirname(file), file, 'new\n', mode)), modeAfter((file) => oldWriter(file, mode), true), label);
        }
      }
    } finally { process.umask(saved); }
  }
  // A link's target mode is not copied onto the replacement.
  const loose = plant(path.join(dir, 'linked-mode.json'));
  fs.chmodSync(loose, 0o640);
  helper.writeFileAtomic(dir, path.join(dir, 'linked-mode.json'), 'x\n');
  assert.equal(fs.statSync(path.join(dir, 'linked-mode.json')).mode & 0o777, 0o666 & ~process.umask());
  unchanged(loose);
  // A root that is itself a link is trusted: it is the session state dir the caller was handed, above every task dir.
  const rootLink = path.join(tmp, 'h-root-link');
  fs.symlinkSync(dir, rootLink);
  helper.writeFileAtomic(rootLink, path.join(rootLink, 'ok.json'), 'ok\n');
  assert.equal(fs.readFileSync(path.join(dir, 'ok.json'), 'utf8'), 'ok\n');
  // A path outside the trusted root is refused.
  assert.throws(() => helper.writeFileAtomic(dir, path.join(outside, 'x'), 'x'), /outside/);
  assert.throws(() => helper.writeFileAtomic(dir, path.join(dir, '..', 'escape'), 'x'), /outside/);
}

// --- helper: append refuses a link at the target and above it, creates a missing file, and appends.
{
  const dir = freshDir('h-append');
  helper.appendFileSafe(dir, path.join(dir, 'followups.md'), '- one\n');
  helper.appendFileSafe(dir, path.join(dir, 'followups.md'), '- two\n');
  assert.equal(fs.readFileSync(path.join(dir, 'followups.md'), 'utf8'), '- one\n- two\n');
  const target = plant(path.join(dir, 'linked.md'));
  assert.throws(() => helper.appendFileSafe(dir, path.join(dir, 'linked.md'), 'evil\n'));
  unchanged(target);
  fs.symlinkSync(outside, path.join(dir, 'sub'));
  const before = fs.readdirSync(outside).sort();
  assert.throws(() => helper.appendFileSafe(dir, path.join(dir, 'sub', 'followups.md'), 'evil\n'), /symlink/);
  assert.deepEqual(fs.readdirSync(outside).sort(), before);
}

// --- helper: writeTaskFile checks from the state dir (two levels above the task dir) down, so a link at tasks/ or at
// the task dir itself is refused; a link at the file is replaced; a linked state dir is trusted.
{
  const state = freshDir('h-task-state');
  assert.equal(helper.taskStateRoot(path.join(state, 'tasks', 'w1')), state);
  // state/tasks -> outside, holding a task dir with a file the write would hit.
  const outTasks = freshDir('h-out-tasks');
  fs.mkdirSync(path.join(outTasks, 'w1'));
  fs.writeFileSync(path.join(outTasks, 'w1', 'state.json'), SENTINEL);
  fs.symlinkSync(outTasks, path.join(state, 'tasks'));
  assert.throws(() => helper.writeTaskFile(path.join(state, 'tasks', 'w1'), 'state.json', 'x\n', 0o666), /symlink/);
  unchanged(path.join(outTasks, 'w1', 'state.json'));
  assert.deepEqual(fs.readdirSync(path.join(outTasks, 'w1')), ['state.json']);
  // state/tasks/w1 -> outside.
  const state2 = freshDir('h-task-state2');
  fs.mkdirSync(path.join(state2, 'tasks'));
  const outTask = freshDir('h-out-task');
  fs.writeFileSync(path.join(outTask, 'state.json'), SENTINEL);
  fs.symlinkSync(outTask, path.join(state2, 'tasks', 'w1'));
  assert.throws(() => helper.writeTaskFile(path.join(state2, 'tasks', 'w1'), 'state.json', 'x\n'), /symlink/);
  unchanged(path.join(outTask, 'state.json'));
  // A link at the file is replaced.
  const task = freshTask('h-task-state3');
  const target = plant(path.join(task, 'state.json'));
  helper.writeTaskFile(task, 'state.json', 'new\n');
  unchanged(target);
  assert.equal(isLink(path.join(task, 'state.json')), false);
  assert.equal(fs.readFileSync(path.join(task, 'state.json'), 'utf8'), 'new\n');
  // A linked state dir is trusted.
  fs.symlinkSync(path.join(tmp, 'h-task-state3'), path.join(tmp, 'h-state-link'));
  helper.writeTaskFile(path.join(tmp, 'h-state-link', 'tasks', 'w1'), 'ok.json', 'ok\n');
  assert.equal(fs.readFileSync(path.join(task, 'ok.json'), 'utf8'), 'ok\n');
}

// --- reporter: a link at state.json or result.md in the task dir is replaced; a link at main/ keeps it off.
const reporter = await load('reporter');
const startReporter = (env) => {
  for (const key of ['TP_TASK_DIR', 'TP_STATE_DIR', 'TP_TASK_ID', 'TMUX']) delete process.env[key];
  Object.assign(process.env, env);
  const handlers = {};
  reporter({ on(name, fn) { handlers[name] = fn; } });
  return handlers;
};
{
  const task = freshTask('r-task');
  const stateTarget = plant(path.join(task, 'state.json'));
  const resultTarget = plant(path.join(task, 'result.md'));
  const h = startReporter({ TP_TASK_DIR: task, TP_TASK_ID: 'w1' });
  h.session_start({}, { model: { provider: 'p', id: 'a' } });
  h.agent_start({});
  h.message_end({ message: { role: 'assistant', provider: 'p', model: 'a', content: [{ type: 'text', text: 'hello' }], usage: { input: 1, output: 1 } } });
  h.agent_settled({});
  unchanged(stateTarget);
  unchanged(resultTarget);
  assert.equal(isLink(path.join(task, 'state.json')), false);
  assert.equal(isLink(path.join(task, 'result.md')), false);
  assert.equal(JSON.parse(fs.readFileSync(path.join(task, 'state.json'), 'utf8')).state, 'waiting');
  assert.equal(fs.readFileSync(path.join(task, 'result.md'), 'utf8'), 'hello\n');
  assert.deepEqual(fs.readdirSync(task).sort(), ['result.md', 'state.json']);

  // The old fixed temp name (state.json.tmp) is no longer a way in.
  const task2 = freshTask('r-task-tmp', 'w2');
  const tmpTarget = plant(path.join(task2, 'state.json.tmp'));
  startReporter({ TP_TASK_DIR: task2, TP_TASK_ID: 'w2' }).session_start({}, {});
  unchanged(tmpTarget);
  assert.equal(JSON.parse(fs.readFileSync(path.join(task2, 'state.json'), 'utf8')).id, 'w2');

  // state/tasks -> outside, and state/tasks/w3 -> outside: nothing is written or deleted through the link.
  for (const [name, link] of [['r-tasks-link', 'tasks'], ['r-taskdir-link', 'task']]) {
    const state = freshDir(name);
    const out = freshDir(`${name}-out`);
    const outTask = link === 'tasks' ? path.join(out, 'w3') : out;
    fs.mkdirSync(outTask, { recursive: true });
    for (const file of ['state.json', 'result.md']) fs.writeFileSync(path.join(outTask, file), SENTINEL);
    if (link === 'tasks') fs.symlinkSync(out, path.join(state, 'tasks'));
    else { fs.mkdirSync(path.join(state, 'tasks')); fs.symlinkSync(out, path.join(state, 'tasks', 'w3')); }
    const h = startReporter({ TP_TASK_DIR: path.join(state, 'tasks', 'w3'), TP_TASK_ID: 'w3' });
    h.session_start({}, { model: { provider: 'p', id: 'a' } });
    h.agent_start({});
    h.message_end({ message: { role: 'assistant', provider: 'p', model: 'a', content: [{ type: 'text', text: 'hello' }], usage: { input: 1, output: 1 } } });
    h.agent_settled({});
    for (const file of ['state.json', 'result.md']) unchanged(path.join(outTask, file));
    assert.deepEqual(fs.readdirSync(outTask).sort(), ['result.md', 'state.json'], `${name}: a file was created through the link`);
  }
}
{
  // Main run: TP_STATE_DIR/main is a link out of the state dir. The reporter stays off and writes nothing.
  const state = freshDir('r-state');
  const outDir = path.join(tmp, 'r-main-out');
  fs.mkdirSync(outDir);
  fs.symlinkSync(outDir, path.join(state, 'main'));
  const h = startReporter({ TP_STATE_DIR: state });
  assert.deepEqual(Object.keys(h), [], 'reporter registered handlers behind a symlinked main/');
  assert.deepEqual(fs.readdirSync(outDir), []);
  // Without the link it works as before.
  const clean = freshDir('r-state-clean');
  startReporter({ TP_STATE_DIR: clean }).session_start({}, {});
  assert.equal(JSON.parse(fs.readFileSync(path.join(clean, 'main', 'state.json'), 'utf8')).id, 'main');
  // A link at state.json inside main/ is replaced.
  const target = plant(path.join(clean, 'main', 'state.json.x'));
  fs.rmSync(path.join(clean, 'main', 'state.json'));
  fs.symlinkSync(target, path.join(clean, 'main', 'state.json'));
  startReporter({ TP_STATE_DIR: clean }).session_start({}, {});
  unchanged(target);
  assert.equal(isLink(path.join(clean, 'main', 'state.json')), false);
}

// --- sensitive: a link at permission-request.json is replaced; a link at tasks/ is refused, not written through.
{
  const stubRoot = freshDir('s-stub');
  fs.mkdirSync(path.join(stubRoot, 'agent'));
  fs.mkdirSync(path.join(stubRoot, 'bin'));
  fs.writeFileSync(path.join(stubRoot, 'bin/tp-trust-guard'), '#!/bin/sh\ncat >/dev/null\necho \'{"deny":false,"reason":null}\'\n', { mode: 0o755 });
  fs.writeFileSync(path.join(stubRoot, 'bin/tp-sensitive-read'), '#!/bin/sh\ncat >/dev/null\necho \'{"sensitive":true,"target":"a stub target","reason":"stub"}\'\n', { mode: 0o755 });
  fs.writeFileSync(path.join(stubRoot, 'bin/tp-redact'), '#!/bin/sh\necho off\n', { mode: 0o755 });
  const task = freshTask('s-task');
  const target = plant(path.join(task, 'permission-request.json'));
  Object.assign(process.env, { PI_CODING_AGENT_DIR: path.join(stubRoot, 'agent'), TP_TASK_DIR: task, TP_REDACTION: 'off' });
  const handlers = {};
  (await load('sensitive'))({ on(name, fn) { handlers[name] = fn; } });
  const result = await handlers.tool_call({ type: 'tool_call', toolName: 'bash', input: { command: 'cat x' } }, { cwd: '/some/session/dir', hasUI: false });
  assert.equal(result.block, true);
  unchanged(target);
  assert.equal(isLink(path.join(task, 'permission-request.json')), false);
  assert.equal(JSON.parse(fs.readFileSync(path.join(task, 'permission-request.json'), 'utf8')).status, 'pending');

  // state/tasks -> outside: the request is not written there, and the read is still not let through.
  const state = freshDir('s-tasks-link');
  const out = freshDir('s-tasks-link-out');
  fs.mkdirSync(path.join(out, 'w1'));
  fs.writeFileSync(path.join(out, 'w1', 'permission-request.json'), SENTINEL);
  fs.symlinkSync(out, path.join(state, 'tasks'));
  process.env.TP_TASK_DIR = path.join(state, 'tasks', 'w1');
  const linked = {};
  (await load('sensitive'))({ on(name, fn) { linked[name] = fn; } });
  let outcome;
  try { outcome = await linked.tool_call({ type: 'tool_call', toolName: 'bash', input: { command: 'cat x' } }, { cwd: '/some/session/dir', hasUI: false }); }
  catch (error) { outcome = { threw: String(error) }; }
  assert.ok(outcome?.block === true || /symlink/.test(outcome?.threw ?? ''), `a sensitive read got through behind a linked tasks dir: ${JSON.stringify(outcome)}`);
  unchanged(path.join(out, 'w1', 'permission-request.json'));
  assert.deepEqual(fs.readdirSync(path.join(out, 'w1')), ['permission-request.json']);
  delete process.env.TP_TASK_DIR;
}

// --- escalate: links at escalation.md, meta.json and preflight.json in the source dir are replaced, and a link
// for the new task's dir is refused (the worker leaves a reason in escalation-error.txt and stays running).
{
  const stubRoot = freshDir('e-root');
  fs.mkdirSync(path.join(stubRoot, 'agent'));
  fs.mkdirSync(path.join(stubRoot, 'bin'));
  const config = path.join(stubRoot, 'local.json');
  fs.writeFileSync(config, JSON.stringify({ escalation: { models: ['local/qwen'], after_consecutive_errors: 0 } }));
  // state/tasks/<id> is the layout escalate.ts relies on; `tasksLink` makes state/tasks a link to that directory.
  const run = async (name, setup, tasksLink) => {
    const state = freshDir(name);
    const tasks = path.join(state, 'tasks');
    if (tasksLink) { fs.mkdirSync(tasksLink); fs.symlinkSync(tasksLink, tasks); } else fs.mkdirSync(tasks);
    const sourceDir = path.join(tasks, 'old');
    fs.mkdirSync(sourceDir);
    // tp-spawn writes meta.json by shell redirection, so it may start 0644.
    fs.writeFileSync(path.join(sourceDir, 'meta.json'), JSON.stringify({ id: 'old', worker: 'pi', workdir: tmp, preflight: { schema: 'preflight', no_edits: true } }), { mode: 0o644 });
    fs.chmodSync(path.join(sourceDir, 'meta.json'), 0o644);
    fs.writeFileSync(path.join(sourceDir, 'packet.md'), 'packet\n');
    fs.writeFileSync(path.join(sourceDir, 'request.md'), 'Original task\n');
    const targetDir = path.join(tasks, 'new');
    const fixtures = setup({ tasks, sourceDir, targetDir });
    const calls = [];
    globalThis.__fakeExecFileSync = (cmd, args) => {
      calls.push(path.basename(cmd));
      switch (path.basename(cmd)) {
        case 'tp-contract': return args[0] === 'escalate' ? JSON.stringify({ target: { worker: 'claude', model: 'claude-sonnet-5-5', thinking: 'low' }, record: {} }) : '';
        case 'tp-spawn':
          if (!fs.existsSync(targetDir)) fs.mkdirSync(targetDir);
          if (!isLink(targetDir) && !fs.existsSync(path.join(targetDir, 'meta.json'))) fs.writeFileSync(path.join(targetDir, 'meta.json'), JSON.stringify({ id: 'new', worker: 'claude', pane: '%1' }));
          return 'new\n';
        default: return '';
      }
    };
    Object.assign(process.env, { TP_TASK_DIR: sourceDir, TP_TASK_ID: 'old', PI_CODING_AGENT_DIR: path.join(stubRoot, 'agent'), TP_LOCAL_CONFIG: config });
    const source = fs.readFileSync(path.join(root, 'agent/extensions/escalate.ts'), 'utf8')
      .replace('import { execFileSync } from "node:child_process";', 'const execFileSync = globalThis.__fakeExecFileSync;');
    fs.writeFileSync(path.join(tmp, `escalate-${name}.ts`), source);
    const handlers = {};
    (await import(pathToFileURL(path.join(tmp, `escalate-${name}.ts`)).href)).default({ on(n, fn) { handlers[n] = fn; } });
    let aborted = false;
    handlers.model_select({ model: { provider: 'local', id: 'qwen' } });
    handlers.tool_execution_start({ toolCallId: '1', toolName: 'read', args: {} });
    handlers.tool_execution_end({ toolCallId: '1', toolName: 'read', isError: true, result: 'failed' },
      { model: { provider: 'local', id: 'qwen' }, abort() { aborted = true; }, shutdown() {} });
    return { state, tasks, sourceDir, targetDir, fixtures, calls, aborted };
  };

  // Links at the source dir's files are replaced and the handoff completes.
  const a = await run('links', ({ sourceDir }) => ({
    notes: plant(path.join(sourceDir, 'escalation.md')),
    preflight: plant(path.join(sourceDir, 'preflight.json')),
  }));
  assert.equal(a.aborted, true);
  unchanged(a.fixtures.notes);
  unchanged(a.fixtures.preflight);
  assert.equal(isLink(path.join(a.sourceDir, 'escalation.md')), false);
  assert.equal(isLink(path.join(a.sourceDir, 'preflight.json')), false);
  assert.match(fs.readFileSync(path.join(a.sourceDir, 'escalation.md'), 'utf8'), /Escalation from old/);
  assert.equal(JSON.parse(fs.readFileSync(path.join(a.sourceDir, 'meta.json'), 'utf8')).escalated_to.id, 'new');
  assert.equal(JSON.parse(fs.readFileSync(path.join(a.sourceDir, 'preflight.json'), 'utf8')).no_edits, true);
  // The task metadata is private, as before: 0600 even over an existing 0644 meta.json.
  for (const name of ['meta.json', 'preflight.json']) assert.equal(fs.statSync(path.join(a.sourceDir, name)).mode & 0o777, 0o600, name);

  // The new task's dir is a link out of the tasks dir: no metadata is written through it, and the handoff is undone.
  const outTask = path.join(tmp, 'e-out-task');
  fs.mkdirSync(outTask);
  // Valid metadata for the new id, so the handoff gets past reading it and reaches the write.
  const outMeta = JSON.stringify({ id: 'new', worker: 'claude', pane: '%1' });
  fs.writeFileSync(path.join(outTask, 'meta.json'), outMeta);
  const b = await run('target-link', ({ targetDir }) => { fs.symlinkSync(outTask, targetDir); return {}; });
  assert.equal(b.aborted, false, 'the handoff went on through a linked target dir');
  assert.equal(fs.readFileSync(path.join(outTask, 'meta.json'), 'utf8'), outMeta);
  assert.deepEqual(fs.readdirSync(outTask), ['meta.json']);
  assert(b.calls.includes('tp-kill'), 'the orphan was not killed');
  assert.match(fs.readFileSync(path.join(b.sourceDir, 'escalation-error.txt'), 'utf8'), /symlink/);

  // state/tasks is itself a link out of the state dir (the source task lives under the outside directory): the first
  // source write (escalation.md) is refused, so nothing is written through the link, no new task is spawned, and the
  // worker keeps running with no escalation-error.txt (there is nowhere safe to put one).
  const outTasks = path.join(tmp, 'e-out-tasks');
  const c = await run('tasks-link', () => ({}), outTasks);
  assert.equal(c.aborted, false, 'the handoff went on through a linked tasks dir');
  assert.deepEqual(fs.readdirSync(path.join(outTasks, 'old')).sort(), ['meta.json', 'packet.md', 'request.md'], 'a source file was written through the link');
  assert.equal(JSON.parse(fs.readFileSync(path.join(outTasks, 'old', 'meta.json'), 'utf8')).escalated_to, undefined);
  assert.equal(fs.statSync(path.join(outTasks, 'old', 'meta.json')).mode & 0o777, 0o644);
  assert.equal(c.calls.includes('tp-spawn'), false, 'a new task was spawned behind a linked tasks dir');
  assert.equal(fs.existsSync(path.join(outTasks, 'new')), false);

  // The source task dir itself is a link out of tasks/: likewise refused before any write.
  const outSource = path.join(tmp, 'e-out-source');
  const d = await run('source-link', ({ tasks, sourceDir }) => {
    fs.renameSync(sourceDir, outSource);
    fs.symlinkSync(outSource, sourceDir);
    return {};
  });
  assert.equal(d.aborted, false, 'the handoff went on through a linked source task dir');
  assert.deepEqual(fs.readdirSync(outSource).sort(), ['meta.json', 'packet.md', 'request.md'], 'a source file was written through the link');
  assert.equal(d.calls.includes('tp-spawn'), false);
}

fs.rmSync(tmp, { recursive: true, force: true });
console.log('reporter symlinks OK');
