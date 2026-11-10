// Headless system Chrome over the DevTools protocol (a pipe: no port), Node built-ins only. Used by
// tests/test_product_design_review.py; not a unittest module.
//
//   node tests/product_design_browser.mjs acceptance BUILD.html WORKDIR [--keep DIR]
//
// Prints one JSON object: {checks: [[name, ok, detail]...], failures: [...], exported: path, requests: [...]}.
// Every wait is on an event (a CDP event, a page event, the runtime's settled() promise) with a deadline, never a
// fixed sleep. Chrome runs with a fresh --user-data-dir under WORKDIR, host resolution mapped to nothing, and every
// network request the pages make is recorded: only file:, blob: and data: URLs are allowed.
import {spawn} from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import {pathToFileURL} from "node:url";

export const CHROME = process.env.TP_CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const DEADLINE = 8000;

export class Browser {
  static async launch(workDir) {
    const profile = fs.mkdtempSync(path.join(workDir, "profile."));
    const args = ["--headless=new", "--remote-debugging-pipe", `--user-data-dir=${profile}`, "--no-first-run",
      "--no-default-browser-check", "--disable-background-networking", "--disable-component-update", "--disable-sync",
      "--disable-extensions", "--disable-default-apps",
      // Fewer processes: the GPU and network services in the browser process, no spare renderer, one renderer limit.
      "--disable-features=Translate,OptimizationHints,MediaRouter,SpareRendererForSitePerProcess",
      "--enable-features=NetworkServiceInProcess2", "--in-process-gpu", "--disable-site-isolation-trials",
      "--renderer-process-limit=1",
      "--host-resolver-rules=MAP * ~NOTFOUND", "--no-proxy-server", "--metrics-recording-only", "--mute-audio",
      "--password-store=basic", "--use-mock-keychain", "--window-size=1280,1000",
      "about:blank"];
    const proc = spawn(CHROME, args, {stdio: ["ignore", "ignore", "pipe", "pipe", "pipe"]});
    const b = new Browser(proc, profile);
    await b.send("Target.setDiscoverTargets", {discover: false});
    for (const t of (await b.send("Target.getTargets")).targetInfos) {  // the startup tab: its renderer is not needed
      if (t.type === "page") await b.send("Target.closeTarget", {targetId: t.targetId});
    }
    return b;
  }
  constructor(proc, profile) {
    this.proc = proc;
    this.profile = profile;
    this.id = 0;
    this.pending = new Map();
    this.listeners = new Set();
    this.stderr = "";
    this.requests = [];
    this.logs = [];
    let buf = Buffer.alloc(0);
    proc.stdio[2].on("data", (d) => { if (this.stderr.length < 20000) this.stderr += d; });
    proc.stdio[4].on("data", (d) => {
      buf = Buffer.concat([buf, d]);
      let i;
      while ((i = buf.indexOf(0)) >= 0) {
        const msg = JSON.parse(buf.subarray(0, i).toString("utf8"));
        buf = buf.subarray(i + 1);
        this.dispatch(msg);
      }
    });
    this.exited = new Promise((resolve) => proc.on("exit", resolve));
  }
  dispatch(msg) {
    if (msg.id !== undefined && this.pending.has(msg.id)) {
      const {resolve, reject, method} = this.pending.get(msg.id);
      this.pending.delete(msg.id);
      if (msg.error) reject(new Error(`${method}: ${msg.error.message}`)); else resolve(msg.result);
      return;
    }
    if (msg.method === "Network.requestWillBeSent") this.requests.push(msg.params.request.url);
    if (msg.method === "Log.entryAdded" && msg.params.entry.level === "error") this.logs.push(msg.params.entry.text);
    if (msg.method === "Runtime.exceptionThrown") this.logs.push("exception: " + JSON.stringify(msg.params.exceptionDetails.exception?.description || msg.params.exceptionDetails.text));
    for (const l of [...this.listeners]) l(msg);
  }
  send(method, params = {}, sessionId) {
    const id = ++this.id;
    const msg = {id, method, params};
    if (sessionId) msg.sessionId = sessionId;
    return new Promise((resolve, reject) => {
      this.pending.set(id, {resolve, reject, method});
      this.proc.stdio[3].write(JSON.stringify(msg) + "\0");
    });
  }
  // Resolve with the params of the first `method` event (optionally for `sessionId`) that satisfies `pred`. Arm it
  // before causing the event.
  waitFor(method, pred = () => true, sessionId = undefined, ms = DEADLINE) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.listeners.delete(l); reject(new Error(`timed out waiting for ${method}`)); }, ms);
      const l = (msg) => {
        if (msg.method === method && (sessionId === undefined || msg.sessionId === sessionId) && pred(msg.params)) {
          clearTimeout(timer);
          this.listeners.delete(l);
          resolve(msg.params);
        }
      };
      this.listeners.add(l);
    });
  }
  async context() { return (await this.send("Target.createBrowserContext", {disposeOnDetach: true})).browserContextId; }
  async page(contextId, {beforeLoad} = {}) {
    const {targetId} = await this.send("Target.createTarget", {url: "about:blank", browserContextId: contextId});
    const {sessionId} = await this.send("Target.attachToTarget", {targetId, flatten: true});
    const p = new Page(this, sessionId, targetId, contextId);
    for (const m of ["Page.enable", "Runtime.enable", "Network.enable", "Log.enable"]) await p.send(m);
    await p.send("Network.setBlockedURLs", {urls: ["http://*", "https://*", "ws://*", "wss://*", "ftp://*"]});
    if (beforeLoad) await p.send("Page.addScriptToEvaluateOnNewDocument", {source: beforeLoad});
    return p;
  }
  // Close a context and its pages (their renderer processes exit with them).
  async dispose(contextId) { await this.send("Target.disposeBrowserContext", {browserContextId: contextId}); }
  async close() {
    try { await Promise.race([this.send("Browser.close"), this.exited]); } catch (e) { /* already gone */ }
    const t = setTimeout(() => this.proc.kill("SIGKILL"), 3000);
    await this.exited;
    clearTimeout(t);
    fs.rmSync(this.profile, {recursive: true, force: true});
  }
}

export class Page {
  constructor(browser, sessionId, targetId, contextId) {
    Object.assign(this, {browser, sessionId, targetId, contextId});
  }
  send(method, params = {}) { return this.browser.send(method, params, this.sessionId); }
  async ev(expression) {
    const r = await this.send("Runtime.evaluate", {expression, returnByValue: true, awaitPromise: true});
    if (r.exceptionDetails) throw new Error("page: " + (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
    return r.result.value;
  }
  async load(url) {
    const loaded = this.browser.waitFor("Page.loadEventFired", () => true, this.sessionId);
    await this.send("Page.navigate", {url});
    await loaded;
    return this.ready();
  }
  async reload() {
    const loaded = this.browser.waitFor("Page.loadEventFired", () => true, this.sessionId);
    await this.send("Page.reload", {ignoreCache: true});
    await loaded;
    return this.ready();
  }
  ready() {
    return this.ev(`document.documentElement.getAttribute("data-tp-ready") ? 1 :
      new Promise((r) => document.addEventListener("tp-ready", () => r(1), {once: true}))`);
  }
  settle() { return this.ev("TPReview.settled().then(() => 1)"); }
  dbg() { return this.ev("TPReview.debug()"); }
  text(sel) { return this.ev(`(document.querySelector(${JSON.stringify(sel)}) || {}).textContent ?? null`); }
  count(sel) { return this.ev(`document.querySelectorAll(${JSON.stringify(sel)}).length`); }
  async point(sel) {
    await this.settle();
    const p = await this.ev(`(() => { const e = document.querySelector(${JSON.stringify(sel)}); if (!e) return null;
      e.scrollIntoView({block: "center"}); const r = e.getBoundingClientRect(); return {x: r.x + r.width / 2, y: r.y + r.height / 2}; })()`);
    if (!p) throw new Error("no element " + sel);
    return p;
  }
  async click(sel, button = "left") {
    const {x, y} = await this.point(sel);
    await this.send("Input.dispatchMouseEvent", {type: "mouseMoved", x, y});
    await this.send("Input.dispatchMouseEvent", {type: "mousePressed", x, y, button, clickCount: 1});
    await this.send("Input.dispatchMouseEvent", {type: "mouseReleased", x, y, button, clickCount: 1});
    await this.settle();
  }
  // Two presses as one double click (the second carries clickCount 2), with no wait between them.
  async dblclick(sel) {
    const {x, y} = await this.point(sel);
    await this.send("Input.dispatchMouseEvent", {type: "mouseMoved", x, y});
    for (const clickCount of [1, 2]) {
      await this.send("Input.dispatchMouseEvent", {type: "mousePressed", x, y, button: "left", clickCount});
      await this.send("Input.dispatchMouseEvent", {type: "mouseReleased", x, y, button: "left", clickCount});
    }
    await this.settle();
  }
  async key(key) {
    const codes = {Escape: 27, Enter: 13, " ": 32, Tab: 9, n: 78, N: 78};
    const text = key === "Enter" ? "\r" : key.length === 1 ? key : undefined;
    const base = {key, windowsVirtualKeyCode: codes[key], code: key === " " ? "Space" : key.length === 1 ? "Key" + key.toUpperCase() : key};
    await this.send("Input.dispatchKeyEvent", Object.assign({type: text ? "keyDown" : "rawKeyDown", text}, base));
    await this.send("Input.dispatchKeyEvent", Object.assign({type: "keyUp"}, base));
    await this.settle();
  }
  // Focus a field, type into it as a user would, and leave it (its change event fires on blur).
  async type(sel, text, {leave = true} = {}) {
    await this.settle();
    await this.ev(`document.querySelector(${JSON.stringify(sel)}).focus()`);
    await this.send("Input.insertText", {text});
    if (leave) await this.ev("document.activeElement.blur()");
    await this.settle();
  }
  async choose(sel, value) {
    await this.settle();
    await this.ev(`(() => { const e = document.querySelector(${JSON.stringify(sel)}); e.value = ${JSON.stringify(value)};
      e.dispatchEvent(new Event("change", {bubbles: true})); })()`);
    await this.settle();
  }
  async historyBack() {
    const hash = await this.ev(`new Promise((r) => { addEventListener("hashchange", () => r(location.hash), {once: true}); history.back(); })`);
    await this.settle();
    return hash;
  }
  async setFile(sel, file, eventName) {
    await this.settle();
    const armed = this.ev(`new Promise((r) => document.addEventListener(${JSON.stringify(eventName)}, (e) => r(e.detail), {once: true}))`);
    const {root} = await this.send("DOM.getDocument", {depth: 0});
    const {nodeId} = await this.send("DOM.querySelector", {nodeId: root.nodeId, selector: sel});
    await this.send("DOM.setFileInputFiles", {nodeId, files: [file]});
    const detail = await armed;
    await this.settle();
    return detail;
  }
  async download(dir, action) {
    await this.browser.send("Browser.setDownloadBehavior", {behavior: "allowAndName", downloadPath: dir, eventsEnabled: true,
                                                            browserContextId: this.contextId});
    const begin = this.browser.waitFor("Browser.downloadWillBegin");
    const done = this.browser.waitFor("Browser.downloadProgress", (p) => p.state === "completed" || p.state === "canceled");
    await action();
    const b = await begin;
    const p = await done;
    if (p.state !== "completed") throw new Error("download " + p.state);
    return {file: path.join(dir, b.guid), suggested: b.suggestedFilename};
  }
  async screenshot(file) {
    const {data} = await this.send("Page.captureScreenshot", {format: "png", captureBeyondViewport: true});
    fs.writeFileSync(file, Buffer.from(data, "base64"));
  }
}

export function canonical(v) {
  if (Array.isArray(v)) return "[" + v.map(canonical).join(",") + "]";
  if (v && typeof v === "object") return "{" + Object.keys(v).sort().map((k) => JSON.stringify(k) + ":" + canonical(v[k])).join(",") + "}";
  return JSON.stringify(v);
}

export const HOSTILE = `<img src=x onerror="window.__pwned=1"><script>window.__pwned=2</script>&lt;b&gt; "q" 'a' \u2028\u2029 </textarea>`;
export const STORAGE_DENIED = `Object.defineProperty(window, "localStorage", {configurable: true,
  get() { throw new DOMException("The user denied storage for this page", "SecurityError"); }});`;

export function allowedRequest(url) { return /^(file|blob|data):/.test(url); }

// ------------------------------------------------------------------ the acceptance flow

async function acceptance(htmlPath, workDir, keep) {
  const checks = [];
  const check = (name, ok, detail) => { checks.push([name, !!ok, detail === undefined ? null : detail]); return ok; };
  const url = pathToFileURL(htmlPath).href;
  const downloads = fs.mkdtempSync(path.join(workDir, "downloads."));
  const browser = await Browser.launch(workDir);
  let exported = null;
  try {
    // ---- context A: review, act, note, answer, export
    const ctxA = await browser.context();
    const a = await browser.page(ctxA);
    await a.load(url + "#overview");
    let d = await a.dbg();
    check("loads offline with storage available", d.storage.ok && d.errors.length === 0, d.storage);
    await a.type("#tp-reviewer", "Ana (reviewer-1)");
    await a.click('[data-tp-view="journeys"]');
    await a.click('[data-tp-start="guided:scenario.submit-complete-record"]');
    d = await a.dbg();
    check("guided task starts on the scenario seed", d.view === "experience" && d.run.seed === "empty-queue" && d.run.actions.length === 0 && d.guide.step === 0, d.run);
    check("step 1 target is highlighted", await a.count('.tp-highlight[data-tp-target="page.submit"]') === 1);
    await a.click('[data-tp-target="page.submit"]');
    await a.type('[data-tp-target="submit.title"]', "Laptop request");
    await a.choose('[data-tp-target="submit.category"]', "Hardware");
    await a.click('[data-tp-target="submit.send"]');
    d = await a.dbg();
    check("submit is one simulated event (201)", d.events.length === 1 && d.events[0].status === "201" && d.events[0].contract === "contract.submissions-api", d.events);
    await a.click('[data-tp-target="page.queue"]');
    d = await a.dbg();
    check("guided task completes on its condition", d.guide.step === 5 && d.session.tasks[0].outcome === "completed", d.session.tasks);
    await a.click('[data-tp-target="page.summary"]');
    check("the consequence shows on another page (summary)", (await a.text('[data-tp-target="summary.waiting"]')) === "Waiting: 1");

    // engineering: select the matching event and note on it
    await a.click('[data-tp-view="engineering"]');
    await a.click('[data-tp-event="1"]');
    const detail = await a.text("#tp-event-detail");
    check("the selected event shows its contract, request and response",
          /Create submission/.test(detail) && /Submissions API|submissions/i.test(detail) && /201/.test(detail) && /Simulated: proposed contract/.test(detail), detail.slice(0, 300));
    const actionsBefore = (await a.dbg()).run.actions.length;
    await a.key("n");
    check("n enters note mode", (await a.dbg()).noteMode === true);
    await a.click('[data-tp-event="1"]');
    d = await a.dbg();
    check("note mode picks the event row as the note target", d.pending && d.pending.target.id === "contract.submissions-api" && d.pending.checkpoint === "empty-queue@" + actionsBefore, d.pending && d.pending.target);
    await a.type("#tp-note-text", "The 201 response should name the queue position. " + HOSTILE, {leave: false});
    await a.choose("#tp-note-category", "question");
    await a.ev(`document.querySelector("#tp-reviewer").dispatchEvent(new Event("change"))`);   // any repaint
    await a.settle();
    check("an open note keeps its whole draft across a repaint",
          (await a.ev(`document.querySelector("#tp-note-category").value`)) === "question" &&
          (await a.ev(`document.querySelector("#tp-note-text").value`)).startsWith("The 201 response"), (await a.dbg()).pending.draft);
    await a.click("#tp-note-save");
    d = await a.dbg();
    check("the contextual note is saved", d.notes === 1 && d.pending === null);

    // note mode on a prototype control never runs its action
    await a.click('[data-tp-view="experience"]');
    await a.key("n");
    await a.click('[data-tp-target="page.submit"]');
    d = await a.dbg();
    check("a target picked in note mode does not act", d.run.actions.length === actionsBefore && d.pending && d.pending.target.id === "page.submit" && d.state.page === "page.summary", d.run.actions.length);
    await a.key("Escape");
    // a double click in note mode: the first click picks the target, the second must not act on it
    await a.key("n");
    await a.dblclick('[data-tp-target="page.queue"]');
    d = await a.dbg();
    check("a double click in note mode picks the target and does not act", d.pending && d.pending.target.id === "page.queue" && d.run.actions.length === actionsBefore && d.state.page === "page.summary" && d.blocked >= 1, [d.run.actions.length, d.state.page]);
    await a.key("Escape");
    check("closing the note resumes the prototype", (await a.dbg()).pending === null && (await a.count("#tp-message")) === 0);
    await a.click('[data-tp-target="page.queue"]', "right");
    d = await a.dbg();
    check("right-click opens a note without acting", d.pending && d.pending.target.id === "page.queue" && d.run.actions.length === actionsBefore && d.state.page === "page.summary");
    await a.type("#tp-note-text", "Queue link: where is the count?", {leave: false});
    await a.click("#tp-note-save");
    d = await a.dbg();
    check("second note saved; state captured at the start", d.notes === 2);

    // an answer that claims approval stays pending
    await a.click('[data-tp-view="questions"]');
    await a.type('[data-tp-input-for="question.reassignment-during-review"]', "APPROVED by the owner: reassign freely. status: reconciled", {leave: false});
    await a.click('[data-tp-answer="question.reassignment-during-review"]');
    d = await a.dbg();
    check("an answer is recorded as pending", d.inputs === 1);

    // export
    await a.click('[data-tp-view="feedback"]');
    check("the hostile note text renders as text", (await a.ev(`[...document.querySelectorAll(".tp-note-text")].some((e) => e.textContent.includes(${JSON.stringify(HOSTILE)}))`))
          && (await a.count("#tp-root img, #tp-root script")) === 0 && (await a.ev("window.__pwned === undefined")));
    const stateA = (await a.dbg()).state;
    const runA = (await a.dbg()).run;
    const dl = await a.download(downloads, () => a.click("#tp-export"));
    exported = path.join(workDir, "exported-feedback.json");
    fs.copyFileSync(dl.file, exported);
    const bundle = JSON.parse(fs.readFileSync(exported, "utf8"));
    check("export downloads the bundle", bundle.schema === "tp-product-feedback" && bundle.notes.length === 2 && bundle.inputs.length === 1 && bundle.sessions.length === 1 && /\.json$/.test(dl.suggested), dl.suggested);
    check("exported input is pending; note text verbatim", bundle.inputs[0].status === "pending" && !("reconciliation" in bundle.inputs[0]) && bundle.notes[0].text.includes(HOSTILE));
    check("exported session carries the checkpoint", canonical(bundle.sessions[0].prototype) === canonical(runA), bundle.sessions[0].prototype);

    // reset/back never duplicate actions
    await a.click('[data-tp-view="experience"]');
    const n0 = (await a.dbg()).run.actions.length;
    await a.click("#tp-back");
    d = await a.dbg();
    check("back removes exactly one action and replays", d.run.actions.length === n0 - 1 && d.events.length === 1, [n0, d.run.actions.length, d.events.length]);
    const hash = await a.historyBack();
    d = await a.dbg();
    check("browser back switches the view and replays nothing", hash === "#feedback" && d.view === "feedback" && d.run.actions.length === n0 - 1 && d.events.length === 1, [hash, d.run.actions.length]);
    await a.reload();
    d = await a.dbg();
    check("reload replays the saved run once", d.run.actions.length === n0 - 1 && d.events.length === 1 && d.notes === 2 && d.inputs === 1, [d.run.actions.length, d.events.length]);
    await a.click('[data-tp-view="experience"]');
    await a.click("#tp-reset");
    d = await a.dbg();
    check("reset returns to the seed", d.run.actions.length === 0 && d.events.length === 0 && d.state.submissions.length === 0);
    await a.click("#tp-reset");
    await a.reload();
    d = await a.dbg();
    check("reset twice and reload stay at the seed", d.run.actions.length === 0 && d.events.length === 0);
    if (keep) await a.screenshot(path.join(keep, "context-a-experience.png"));
    await browser.dispose(ctxA);

    // ---- context B: a fresh browser context restores from the export
    const ctxB = await browser.context();
    const b = await browser.page(ctxB);
    await b.load(url + "#feedback");
    d = await b.dbg();
    check("a fresh context starts empty", d.notes === 0 && d.inputs === 0 && d.sessions === 0);
    // the CLI refuses a file that starts with a byte order mark; so does the page (it once stripped it and accepted)
    const withBom = path.join(workDir, "exported-feedback-bom.json");
    fs.writeFileSync(withBom, Buffer.concat([Buffer.from([0xef, 0xbb, 0xbf]), fs.readFileSync(exported)]));
    let rep = await b.setFile("#tp-import-file", withBom, "tp-imported");
    d = await b.dbg();
    check("a bundle with a byte order mark is refused, as by the CLI", !rep.ok && d.notes === 0 && d.inputs === 0 && d.sessions === 0, rep.errors);
    rep = await b.setFile("#tp-import-file", exported, "tp-imported");
    check("import restores the notes, inputs and session", rep.ok && rep.added.notes.length === 2 && rep.added.inputs.length === 1 && rep.added.sessions.length === 1, rep);
    check("restored hostile text is inert", (await b.ev(`[...document.querySelectorAll(".tp-note-text")].some((e) => e.textContent.includes(${JSON.stringify(HOSTILE)}))`))
          && (await b.count("#tp-root img, #tp-root script")) === 0 && (await b.ev("window.__pwned === undefined")));
    await b.click(`[data-tp-resume="${bundle.sessions[0].id}"]`);
    d = await b.dbg();
    check("resume replays the exported checkpoint to the same state", canonical(d.run) === canonical(runA) && canonical(d.state) === canonical(stateA) && d.events.length === 1, d.run);
    check("resume adopts the session (reviewer, tasks)", d.session.id === bundle.sessions[0].id && d.session.reviewer === "Ana (reviewer-1)" && d.session.tasks.length === 1);
    await b.click('[data-tp-view="feedback"]');
    rep = await b.setFile("#tp-import-file", exported, "tp-imported");
    check("importing the same bundle again adds nothing", rep.ok && rep.added.notes.length === 0 && rep.added.inputs.length === 0 && rep.duplicates.length >= 3, rep);
    // a note from an older build on a target this build does not have: listed as unresolved, never dropped
    const old = JSON.parse(JSON.stringify(bundle));
    old.notes = [Object.assign({}, bundle.notes[1], {id: bundle.notes[1].id.replace(/\.\d+$/, ".90"), target: {id: "record.escalate", label: "Escalate button", context: "old build"},
                                                       build: {revision: 1, fingerprint: "sha256:" + "0".repeat(64)}})];
    old.inputs = [];
    old.sessions = [];
    const oldFile = path.join(workDir, "old-build.json");
    fs.writeFileSync(oldFile, JSON.stringify(old));
    rep = await b.setFile("#tp-import-file", oldFile, "tp-imported");
    d = await b.dbg();
    check("an old-build note with an unknown target is imported and listed unresolved", rep.ok && d.notes === 3 && /record\.escalate/.test(await b.text("#tp-unresolved")), rep);
    // ids that are also Object.prototype names are ids like any other; an unknown field with such a name is refused
    const proto = JSON.parse(JSON.stringify(bundle));
    proto.sessions = [Object.assign({}, bundle.sessions[0], {id: "constructor", prototype: null})];
    proto.notes = [Object.assign({}, bundle.notes[1], {id: "note.constructor.1", review_session: "constructor"})];
    proto.inputs = [];
    const protoFile = path.join(workDir, "proto-ids.json");
    fs.writeFileSync(protoFile, JSON.stringify(proto));
    rep = await b.setFile("#tp-import-file", protoFile, "tp-imported");
    check("ids such as constructor import like any id", rep.ok && rep.added.notes.length === 1 && rep.added.sessions.length === 1, rep);
    fs.writeFileSync(protoFile, JSON.stringify(proto).replace('"review_session"', '"constructor":1,"review_session"').replace("note.constructor.1", "note.constructor.2"));
    rep = await b.setFile("#tp-import-file", protoFile, "tp-imported");
    check("an unknown field named constructor is refused, as by the CLI", !rep.ok && /constructor/.test(rep.errors.join(" ")), rep.errors);
    // numbers this page could not export as written are refused, visibly, before anything is imported
    fs.writeFileSync(protoFile, JSON.stringify(old).replace('"revision":1', '"revision":9007199254740993').replace(/\.90"/, '.91"'));
    rep = await b.setFile("#tp-import-file", protoFile, "tp-imported");
    check("a number that would re-export differently is refused", !rep.ok && /9007199254740993/.test(rep.errors.join(" ")) &&
          /9007199254740993/.test(await b.text("#tp-import-report")), rep.errors);
    // any depth: the 64-level refusal in the import report, never a stack overflow
    fs.writeFileSync(protoFile, "[".repeat(200000) + "]".repeat(200000));
    rep = await b.setFile("#tp-import-file", protoFile, "tp-imported");
    check("a bundle nested 200000 deep gets the 64-level refusal", !rep.ok && /deeper than 64/.test(await b.text("#tp-import-report")), rep.errors);
    fs.writeFileSync(protoFile, "[".repeat(2200000) + "]".repeat(2200000));   // over the byte limit too: both are reported
    rep = await b.setFile("#tp-import-file", protoFile, "tp-imported");
    check("a bundle nested 2200000 deep and over 4 MiB gets the 64-level refusal too", !rep.ok &&
          /deeper than 64/.test(await b.text("#tp-import-report")) && /larger than/.test(rep.errors.join(" ")), rep.errors);
    if (keep) await b.screenshot(path.join(keep, "context-b-feedback.png"));
    await browser.dispose(ctxB);

    // ---- context D: a session saved mid-task resumes the task (scenario, step, assistance) and notes carry it
    const ctxD = await browser.context();
    const e = await browser.page(ctxD);
    await e.load(url + "#journeys");
    await e.type("#tp-reviewer", "Ben (reviewer-2)");
    await e.click('[data-tp-start="guided:scenario.submit-complete-record"]');
    await e.click("#tp-assist");
    d = await e.dbg();
    const midTask = {guide: d.guide, task: d.session.tasks[0]};
    const midDl = await e.download(downloads, () => e.click('[data-tp-view="feedback"]').then(() => e.click("#tp-export")));
    const midFile = path.join(workDir, "mid-task.json");
    fs.copyFileSync(midDl.file, midFile);
    // a stored value of any shape: the page starts, says so, and leaves the value as it was
    for (const raw of ["null", "0", "false", "[]", '{"v":1}', '{"v":1,"ui":{"guide":{"task":7}}}']) {
      await e.ev(`localStorage.setItem("tp-product-review:v1:review-queue-demo", ${JSON.stringify(raw)})`);
      await e.reload();
      d = await e.dbg();
      check("stored value " + raw + " gives a working page, a notice and an untouched value",
            d.notes === 0 && d.errors.length === 0 && /could not be read/.test(d.storageNotice || "") &&
            (await e.ev(`localStorage.getItem("tp-product-review:v1:review-queue-demo")`)) === raw, d.storageNotice);
    }
    await browser.dispose(ctxD);
    const ctxE = await browser.context();
    const f = await browser.page(ctxE);
    await f.load(url + "#feedback");
    rep = await f.setFile("#tp-import-file", midFile, "tp-imported");
    await f.type("#tp-reviewer", "Cy (reviewer-3)");
    await f.click(`[data-tp-resume="${midTask.task && (JSON.parse(fs.readFileSync(midFile, "utf8")).sessions[0].id)}"]`);
    d = await f.dbg();
    check("restoring a session resumes its task in progress", rep.ok && d.guide && d.guide.scenario === midTask.guide.scenario &&
          d.guide.step === midTask.guide.step && d.guide.step === 1 && d.session.tasks[0].outcome === "in-progress" &&
          d.session.tasks[0].assisted_steps === 1, [d.guide, midTask.guide]);
    await f.click('[data-tp-view="experience"]');
    check("the resumed task's next step is highlighted", (await f.count('.tp-highlight[data-tp-target="submit.title"]')) === 1);
    await f.click('[data-tp-target="submit.title"]', "right");
    await f.type("#tp-note-text", "Title field: what length is allowed?", {leave: false});
    await f.click("#tp-note-save");
    const resumedNote = await f.ev(`TPReview.summary()`);
    d = await f.dbg();
    check("the next note carries the resumed task's context", d.notes === 1 &&
          /guided, assisted-advance, scenario\.submit-complete-record step 2/.test(resumedNote), resumedNote.split("\n").filter((l) => /by Ana|by Ben|by Cy/.test(l)));
    await browser.dispose(ctxE);

    // ---- context C: storage denied is visible, and the page still works in the tab
    const c = await browser.page(await browser.context(), {beforeLoad: STORAGE_DENIED});
    await c.load(url + "#experience");
    d = await c.dbg();
    const banner = await c.text("#tp-storage");
    check("storage denied is shown on the page", !d.storage.ok && /only in this tab/.test(banner) && (await c.ev(`document.querySelector("#tp-storage").classList.contains("tp-error")`)), banner);
    await c.click('[data-tp-target="page.submit"]');
    check("the prototype still works without storage", (await c.dbg()).run.actions.length === 1);
    if (keep) await c.screenshot(path.join(keep, "context-c-storage-denied.png"));
  } catch (e) {
    check("flow ran to the end", false, String(e && e.stack || e));
  } finally {
    await browser.close();
  }
  const requests = browser.requests;
  check("no request left file:, blob: or data:", requests.every(allowedRequest), requests.filter((u) => !allowedRequest(u)));
  check("no console error or CSP violation", browser.logs.length === 0, browser.logs);
  return {checks, failures: checks.filter((c) => !c[1]), exported, requests};
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  const [mode, html, work, ...rest] = process.argv.slice(2);
  const keep = rest[0] === "--keep" ? rest[1] : null;
  if (mode !== "acceptance" || !html || !work) {
    console.error("usage: product_design_browser.mjs acceptance BUILD.html WORKDIR [--keep DIR]");
    process.exit(2);
  }
  const out = await acceptance(path.resolve(html), path.resolve(work), keep && path.resolve(keep));
  process.stdout.write(JSON.stringify(out, null, 1) + "\n");
  process.exit(out.failures.length ? 1 : 0);
}
