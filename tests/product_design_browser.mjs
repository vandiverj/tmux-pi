// Headless system Chrome over the DevTools protocol (a pipe: no port), Node built-ins only. Used by
// tests/test_product_design_review.py; not a unittest module.
//
//   node tests/product_design_browser.mjs acceptance BUILD.html WORKDIR [--keep DIR]
//
// Prints one JSON object: {checks: [[name, ok, detail]...], failures: [...], exported: path, requests: [...]}.
// Every wait is on an event (a CDP event, a page event, the runtime's settled() promise) with a deadline, never a
// fixed sleep. Chrome runs with a fresh --user-data-dir under WORKDIR, host resolution mapped to nothing, and every
// network request the pages make is recorded: only file:, blob: and data: URLs are allowed.
import {execFileSync, spawn} from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import {fileURLToPath, pathToFileURL} from "node:url";

export const CHROME = process.env.TP_CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const DEADLINE = 8000;
const REPO = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

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
    // a note opened after the last step: its context separates the row's cells, and it names no step past the end
    await a.key("n");
    await a.click('[data-tp-target="queue.row.s-1"]');
    d = await a.dbg();
    check("a note's context separates a table row's cells", d.pending && d.pending.target.id === "queue.row.s-1" && d.pending.target.label === "Queue row S-1" &&
          d.pending.target.context.startsWith('"S-1 · Laptop request · Hardware · Waiting · -" on prototype page page.queue'), d.pending && d.pending.target);
    const panel = await a.text("#tp-note-panel");
    check("a note after the last step is recorded and shown as all steps done", d.pending && d.pending.mode === "guided" && d.pending.step === null &&
          /scenario\.submit-complete-record, all steps done/.test(panel) && !/step \d/.test(panel), [d.pending && d.pending.step, panel]);
    await a.key("Escape");
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
    const cards = await a.ev(`[...document.querySelectorAll(".tp-note")].map((e) => e.textContent).join("\\n")`);
    check("the Feedback list shows the notes after the last step as all steps done", (cards.match(/all steps done/g) || []).length === 2 && !/step \d/.test(cards), cards);
    check("the hostile note text renders as text", (await a.ev(`[...document.querySelectorAll(".tp-note-text")].some((e) => e.textContent.includes(${JSON.stringify(HOSTILE)}))`))
          && (await a.count("#tp-root img, #tp-root script")) === 0 && (await a.ev("window.__pwned === undefined")));
    const stateA = (await a.dbg()).state;
    const runA = (await a.dbg()).run;
    const dl = await a.download(downloads, () => a.click("#tp-export"));
    exported = path.join(workDir, "exported-feedback.json");
    fs.copyFileSync(dl.file, exported);
    const bundle = JSON.parse(fs.readFileSync(exported, "utf8"));
    check("export downloads the bundle", bundle.schema === "tp-product-feedback" && bundle.notes.length === 2 && bundle.inputs.length === 1 && bundle.sessions.length === 1 && /\.json$/.test(dl.suggested), dl.suggested);
    check("the exported notes after the last step carry no step", bundle.notes.every((n) => n.mode === "guided" && n.scenario === "scenario.submit-complete-record" && n.step === null),
          bundle.notes.map((n) => [n.mode, n.scenario, n.step]));
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
    // an older export holds the step count itself after the last step: it imports and shows as all steps done
    const past = JSON.parse(JSON.stringify(bundle));
    past.notes = [Object.assign({}, bundle.notes[1], {id: bundle.notes[1].id.replace(/\.\d+$/, ".91"), step: 5})];
    past.inputs = [];
    past.sessions = [];
    const pastFile = path.join(workDir, "past-last-step.json");
    fs.writeFileSync(pastFile, JSON.stringify(past));
    rep = await b.setFile("#tp-import-file", pastFile, "tp-imported");
    const pastCard = await b.ev(`[...document.querySelectorAll('[data-note-id="${past.notes[0].id}"]')].map((e) => e.textContent).join("\\n")`);
    check("an older note recorded past the last step imports and shows as all steps done", rep.ok && rep.added.notes.length === 1 &&
          /all steps done/.test(pastCard) && !/step \d/.test(pastCard), [rep, pastCard]);
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

    // ---- context F: a note keeps the task position it recorded when the scenario is later revised. A note at step 4
    // of 5 is exported; a copy of the package cuts that scenario to 3 steps (tp-product apply) and is rebuilt; the
    // untouched bundles import into the new build: the note still reads step 4, a note taken after the last step
    // still reads all steps done, and no imported note changes.
    const ctxF = await browser.context();
    const g = await browser.page(ctxF);
    await g.load(url + "#journeys");
    await g.type("#tp-reviewer", "Di (reviewer-4)");
    await g.click('[data-tp-start="guided:scenario.submit-complete-record"]');
    for (let i = 0; i < 3; i++) await g.click("#tp-assist");
    await g.click('[data-tp-target="submit.send"]', "right");
    await g.type("#tp-note-text", "What happens if I submit now?", {leave: false});
    await g.click("#tp-note-save");
    const step4Dl = await g.download(downloads, () => g.click('[data-tp-view="feedback"]').then(() => g.click("#tp-export")));
    const step4File = path.join(workDir, "step-4-of-5.json");
    fs.copyFileSync(step4Dl.file, step4File);
    const step4 = JSON.parse(fs.readFileSync(step4File, "utf8"));
    check("a note at step 4 of 5 is exported with that step, its task in progress", step4.notes.length === 1 && step4.notes[0].step === 3 &&
          step4.sessions[0].tasks[0].outcome === "in-progress", [step4.notes.map((n) => n.step), step4.sessions[0].tasks]);
    await browser.dispose(ctxF);
    const pkgCopy = path.join(workDir, "revised-package");
    fs.cpSync(path.dirname(path.dirname(htmlPath)), pkgCopy, {recursive: true});
    const product = (...args) => JSON.parse(execFileSync(process.env.TP_PYTHON || "python3",
      [path.join(REPO, "bin/tp-product"), ...args, "--json"], {encoding: "utf8", env: Object.assign({}, process.env, {PYTHONDONTWRITEBYTECODE: "1"})}));
    const design = JSON.parse(fs.readFileSync(path.join(pkgCopy, "design.json"), "utf8"));
    const tour = design.scenarios.find((s) => s.id === "scenario.submit-complete-record");
    const change = {schema: "tp-product-changeset", schema_version: 1, actor: "Main", summary: "Combine field entry with submission",
                    operations: [{op: "set", id: tour.id, fields: {actions: tour.actions.filter((x, i) => i === 0 || i >= 3)}}]};
    const changeFile = path.join(workDir, "three-steps.json");
    fs.writeFileSync(changeFile, JSON.stringify(change));
    product("apply", pkgCopy, changeFile, "--expected-revision", String(product("status", pkgCopy).revision));
    const revised = product("build", pkgCopy);
    const h2 = await browser.page(await browser.context());
    await h2.load(pathToFileURL(path.join(pkgCopy, revised.file)).href + "#feedback");
    const rep4 = await h2.setFile("#tp-import-file", step4File, "tp-imported");
    const repDone = await h2.setFile("#tp-import-file", exported, "tp-imported");
    const cardOf = (id) => h2.ev(`[...document.querySelectorAll('[data-note-id="${id}"]')].map((e) => e.textContent).join("\\n")`);
    const card4 = await cardOf(step4.notes[0].id);
    const summary4 = (await h2.ev("TPReview.summary()")).split("\n");
    const line4 = summary4[summary4.findIndex((l) => l.includes(step4.notes[0].id)) + 1] || "";
    check("after the scenario is cut to 3 steps the imported note still reads step 4, never all steps done",
          rep4.ok && rep4.added.notes.length === 1 && /scenario\.submit-complete-record step 4/.test(card4) && !/all steps done/.test(card4) &&
          /scenario\.submit-complete-record step 4/.test(line4) && !/all steps done/.test(line4), [rep4.errors, card4, line4]);
    const doneIds = JSON.parse(fs.readFileSync(exported, "utf8")).notes.map((n) => n.id);
    const doneCards = await Promise.all(doneIds.map(cardOf));
    const doneLines = doneIds.map((id) => summary4[summary4.findIndex((l) => l.includes(id)) + 1] || "");
    check("notes taken after the last step still read all steps done after the same revision", repDone.ok && repDone.added.notes.length === 2 &&
          doneCards.concat(doneLines).every((t) => /scenario\.submit-complete-record, all steps done/.test(t) && !/step \d/.test(t)), [repDone.errors, doneCards, doneLines]);
    const stored = JSON.parse(await h2.ev(`localStorage.getItem("tp-product-review:v1:review-queue-demo")`)).notes;
    const sent = step4.notes.concat(JSON.parse(fs.readFileSync(exported, "utf8")).notes);
    check("import changes no note", sent.every((n) => canonical(stored.find((s) => s.id === n.id)) === canonical(n)), stored.map((n) => [n.id, n.step]));

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

// ------------------------------------------------------------------ saved state, text and export parts at the limits

// What the page saves it reloads (past one bundle's record cap too); what it refuses to read it offers back as
// importable parts; it refuses text the CLI refuses at any depth; every export part has a save control and copyable
// JSON before the records count as exported. Writes each part's copied JSON to WORKDIR and lists the paths (`parts`,
// `recovered`) so the caller can check them with the CLI.
async function limits(htmlPath, workDir) {
  const checks = [];
  const check = (name, ok, detail) => { checks.push([name, !!ok, detail === undefined ? null : detail]); return ok; };
  const url = pathToFileURL(htmlPath).href;
  const KEY = "tp-product-review:v1:review-queue-demo";
  const stored = `localStorage.getItem(${JSON.stringify(KEY)})`;
  const browser = await Browser.launch(workDir);
  const out = {parts: [], recovered: [], noteIds: null, recoverable: [], textFiles: []};
  const save = (name, text) => { const f = path.join(workDir, name); fs.writeFileSync(f, text); return f; };
  // each phase stands alone: one that breaks fails its own check and the next phase still runs
  const phase = async (name, fn) => { try { await fn(); } catch (e) { check(name + ": ran to the end", false, String(e && e.stack || e).slice(0, 2000)); } };
  // String.prototype.isWellFormed made to call text holding MARK-UNREADABLE a lone surrogate: text no bundle can hold,
  // put in the page's state by typing (a real one cannot be typed)
  const unreadable = `(() => { const w = window.__isWellFormed = String.prototype.isWellFormed;
    String.prototype.isWellFormed = function () { return this.indexOf("MARK-UNREADABLE") < 0 && (w ? w.call(this) : true); }; return 1; })()`;
  let note = null, saved = null;
  try {
    const ctx = await browser.context();
    await browser.send("Browser.setDownloadBehavior", {behavior: "deny", browserContextId: ctx});
    const p = await browser.page(ctx);
    await p.load(url + "#experience");
    await p.type("#tp-reviewer", "Dee (reviewer-4)");
    await p.click('[data-tp-target="page.submit"]', "right");
    await p.type("#tp-note-text", "One note, copied below past the record cap.", {leave: false});
    await p.click("#tp-note-save");
    await p.settle();
    saved = JSON.parse(await p.ev(stored));
    note = saved.notes[0];

    await phase("text", async () => {   // a lone surrogate in a free-form key at depth 6 is refused as the CLI refuses it
      const bundle = (key, id) => `{"schema":"tp-product-feedback","schema_version":1,"package":"review-queue-demo","exported":"2026-11-10T09:00:00Z",` +
        `"notes":[${JSON.stringify(Object.assign({}, note, {id, state: {page: "page.queue", a: {b: {c: {}}}}})).replace('"c":{}', `"c":{"${key}":"v"}`)}],"inputs":[]}`;
      await p.click('[data-tp-view="feedback"]');
      out.textFiles = [save("text-ok.json", bundle("key", "note.text.1")), save("text-bad.json", bundle("key\\ud800", "note.text.2"))];
      let rep = await p.setFile("#tp-import-file", out.textFiles[0], "tp-imported");
      check("a free-form key deep in a note's state imports", rep.ok, rep.errors);
      rep = await p.setFile("#tp-import-file", out.textFiles[1], "tp-imported");
      check("a lone surrogate in that key is refused, as by the CLI", !rep.ok && /not valid Unicode: the key notes\.0\.state\.a\.b\.c\.key/.test(rep.errors.join(" ")), rep.errors);
    });

    await phase("unreadable state", async () => {   // not stored, and not exported
      const before = await p.ev(stored);
      await p.ev(unreadable);
      await p.click('[data-tp-view="experience"]');
      await p.type("#tp-reviewer", "MARK-UNREADABLE");
      const d = await p.dbg();
      check("state the page could not read back is not stored, and the page says so",
            !d.storage.ok && /could not read it back/.test(d.storage.reason) && /could not read it back/.test(await p.text("#tp-storage")) &&
            (await p.ev(stored)) === before, d.storage);
      await p.click('[data-tp-view="feedback"]');
      await p.ev(`(window.__event = new Promise((r) => document.addEventListener("tp-exported", (e) => r(e.detail), {once: true})), 1)`);
      await p.click("#tp-export");
      const detail = await p.ev("window.__event");
      await p.settle();
      const shown = (await p.text("#tp-export-error")) || "";
      check("an export holding text no bundle can hold is refused visibly, with nothing marked exported",
            !detail.ok && /not valid Unicode: sessions\.0\.reviewer/.test(shown) && !JSON.parse(await p.ev(stored)).exported, [detail, shown]);
      await p.ev("(String.prototype.isWellFormed = window.__isWellFormed, 1)");
    });

    await phase("saved state past the record cap", async () => {
      const many = [];
      for (let i = 1; i <= 5001; i++) many.push(Object.assign({}, note, {id: "note." + saved.session.id + "." + i}));
      const big = Object.assign({}, saved, {notes: many});
      out.noteIds = save("note-ids.json", JSON.stringify(many.map((n) => n.id)));
      await p.ev(`localStorage.setItem(${JSON.stringify(KEY)}, ${JSON.stringify(JSON.stringify(big))})`);
      await p.reload();
      const d = await p.dbg();
      check("5001 saved notes reload in full, with no notice", d.notes === 5001 && d.storage.ok && !d.storageNotice, [d.notes, d.storageNotice]);
    });

    await phase("split export", async () => {   // every part offered before the records count as exported
      await p.click('[data-tp-view="feedback"]');
      await p.ev(`(() => {
        const count = () => Array.from(document.querySelectorAll('[data-tp-part^="tp-export:"]')).filter((e) => e.querySelector("[data-tp-save]") && e.querySelector("[data-tp-show]")).length;
        window.__seen = [];
        window.__marks = [];
        new MutationObserver(() => window.__seen.push([/Not exported yet/.test(document.querySelector("#tp-storage").textContent), count()]))
          .observe(document.getElementById("tp-root"), {childList: true, subtree: true});
        const set = Storage.prototype.setItem;   // what was on the page when the export marker was stored
        Storage.prototype.setItem = function (k, v) { try { if (JSON.parse(v).exported) window.__marks.push(count()); } catch (e) {} return set.call(this, k, v); };
        window.__event = new Promise((r) => document.addEventListener("tp-exported", (e) => r(e.detail), {once: true}));
        return 1;
      })()`);
      await p.click("#tp-export");
      const ev = await p.ev("window.__event");
      await p.settle();
      check("5001 notes export as two parts, downloads denied", ev.ok && ev.parts === 2 && ev.offered === 2, ev);
      const seen = await p.ev("window.__seen"), marks = await p.ev("window.__marks");
      check("the records count as exported only once both parts are offered", seen.length > 0 && seen.every(([unsaved, n]) => unsaved || n === 2) &&
            !seen[seen.length - 1][0] && marks.length === 1 && marks[0] === 2, {seen: seen.slice(-6), marks});
      const now = await p.ev(stored), d = await p.dbg();
      check("the export marker is stored as a short hash, not a second copy of the records",
            d.storage.ok && typeof JSON.parse(now).exported === "string" && JSON.parse(now).exported.length < 64, [d.storage, now.length]);
      for (let i = 1; i <= 2; i++) {
        await p.click(`#tp-export-show-${i}`);
        await p.settle();
        const text = await p.ev(`(document.querySelector("#tp-export-text-${i}") || {}).value || ""`);
        check(`part ${i} of 2 has a save control and its JSON to copy`, (await p.count(`#tp-export-save-${i}`)) === 1 && text.length > 0, text.length);
        out.parts.push(save(`part-${i}.json`, text));
      }
    });

    await phase("refused saved data", async () => {   // left as it is, downloadable, its importable records offered as parts
      const refused = Object.assign({}, saved, {zz: 1});
      // a record no bundle can hold, an id held twice with other content (an import refuses both), the same record twice
      refused.notes = [1, 2, 3].map((i) => Object.assign({}, note, {id: "note.good." + i})).concat([Object.assign({}, note, {id: "note.bad.1", text: "lone \ud800"}),
        Object.assign({}, note, {id: "note.dup.1", text: "one"}), Object.assign({}, note, {id: "note.dup.1", text: "other"}),
        Object.assign({}, note, {id: "note.good.1"})]);
      out.recoverable = ["note.good.1", "note.good.2", "note.good.3"];
      const raw = JSON.stringify(refused);
      await p.ev(`localStorage.setItem(${JSON.stringify(KEY)}, ${JSON.stringify(raw)})`);
      await p.reload();
      const d = await p.dbg();
      check("refused saved data gives a notice with a download of it as it is and an export of its records",
            /could not be read/.test(d.storageNotice || "") && (await p.count("#tp-saved-download")) === 1 && (await p.count("#tp-saved-export")) === 1, d.storageNotice);
      await p.click("#tp-saved-export");
      await p.settle();
      const rec = await p.ev(`(document.querySelector("#tp-saved-text-1") || {}).value || ""`);
      check("its importable records are offered as one part, with a save control", rec.length > 0 && (await p.count("#tp-saved-save-1")) === 1);
      const left = (await p.text("#tp-saved-excluded")) || "";
      check("the record no bundle can hold is listed as left out", /note\.bad\.1: not valid Unicode/.test(left), left);
      check("both records of a conflicting id are left out, a repeat is exported once", (left.match(/note\.dup\.1: another saved record has this id/g) || []).length === 2 &&
            /note\.good\.1: the same record again/.test(left), left);
      check("the saved data stays as it was", (await p.ev(stored)) === raw);
      out.recovered.push(save("recovered-1.json", rec));
    });

    await phase("conflicting saved state", async () => {   // the saved state itself is refused for an id held twice, never exported
      const two = Object.assign({}, saved, {notes: [Object.assign({}, note, {id: "note.twice.1", text: "one"}), Object.assign({}, note, {id: "note.twice.1", text: "other"}),
                                                    Object.assign({}, note, {id: "note.alone.1"})]});
      out.recoverable.push("note.alone.1");
      await p.ev(`localStorage.setItem(${JSON.stringify(KEY)}, ${JSON.stringify(JSON.stringify(two))})`);
      await p.reload();
      const d = await p.dbg();
      check("saved state holding two different records with one id is refused on load, not exported",
            d.notes === 0 && /note\.twice\.1: the bundle holds two different records with this id/.test(d.storageNotice || ""), [d.notes, d.storageNotice]);
      await p.click("#tp-saved-export");
      await p.settle();
      out.recovered.push(save("recovered-2.json", await p.ev(`(document.querySelector("#tp-saved-text-1") || {}).value || ""`)));
    });

    await phase("saved data refused before it is parsed", async () => {   // too deep to parse: its records are still offered
      const deep = Object.assign({}, saved, {notes: [Object.assign({}, note, {id: "note.deep.1"})]});
      const nest = "[".repeat(70) + "]".repeat(70), raw = JSON.stringify(deep).replace(/}$/, `,"zz":${nest}}`);
      out.recoverable.push("note.deep.1");
      await p.ev(`localStorage.setItem(${JSON.stringify(KEY)}, ${JSON.stringify(raw)})`);
      await p.reload();
      let d = await p.dbg();
      check("saved data too deep to parse offers its importable records", /nested deeper than/.test(d.storageNotice || "") && d.notes === 0 &&
            (await p.count("#tp-saved-export")) === 1, d.storageNotice);
      await p.click("#tp-saved-export");
      await p.settle();
      out.recovered.push(save("recovered-3.json", await p.ev(`(document.querySelector("#tp-saved-text-1") || {}).value || ""`)));
      check("the deep saved data stays as it was", (await p.ev(stored)) === raw);
      // other bytes before the saved object (a BOM, an object around it, one named as a saved state's part), the text
      // cut right after a complete note, the note in nested arrays, or numbers JavaScript would rewrite (1.0, an
      // integer past 2^53, -0.0, 1E+2) beside an unknown field: that note is still offered, its text as it was
      const one = (id) => Object.assign({}, note, {id}), rec = (id) => id === "note.num.1"
        ? JSON.stringify(Object.assign(one(id), {state: {counter: 0}})).replace('"counter":0', '"counter":1.0,"big":9007199254740993,"neg":-0.0,"e":1E+2')
        : JSON.stringify(one(id));
      // Two stored texts (one reload each): every wrapper around one saved state that holds a plain note, a note in
      // nested arrays and a note with such numbers, beside an unknown field; and a state cut right after its note.
      const state = (items) => JSON.stringify(Object.assign({}, saved, {notes: items.map((x, i) => "@REC" + i + "@")}))
        .replace(/"@REC(\d+)@"/g, (m, i) => items[Number(i)]);
      const [wrapped, nested, num, cut] = ["note.wrap.1", "note.nested.1", "note.num.1", "note.cut.1"].map(rec);
      // The saved state's own session is a complete review session the CLI imports on its own: it is offered like any
      // record (the session is recovered as it was saved, never made up from the page). A note whose effective number
      // is finite (a shadowed 1e400 before a later member of the same name) is offered as the CLI reads it. A note and
      // the session, each importable alone but one byte over the limit together, are offered as two parts.
      const session = JSON.stringify(saved.session);
      const shadow = JSON.stringify(Object.assign(one("note.shadow.1"), {state: {x: 0}})).replace('"x":0', '"x":1e400,"x":1');
      const LIMIT = 4 * 1024 * 1024, pad = (r) => r.replace(/}$/, " ".repeat(LIMIT + 1 - Buffer.byteLength(
        '{\n  "schema": "tp-product-feedback",\n  "schema_version": 1,\n  "package": "review-queue-demo",\n  "exported": "2026-01-01T00:00:00Z",\n' +
        '  "notes": [\n    ' + r + '\n  ],\n  "inputs": [],\n  "sessions": [\n    ' + session + '\n  ]\n}')) + "}");
      const big = pad(rec("note.big.1"));
      const deep2 = (t) => t.replace(/}$/, `,"zz":${nest}}`);
      const texts = [
        {name: "wrapped", ids: ["note.wrap.1", "note.nested.1", "note.num.1"], recs: [wrapped, nested, num],
         text: '\ufeff[{"a":{"notes":[{"session":' + state([wrapped, "[[" + nested + "]]", num]).replace(/}$/, ',"zz":1}')},
        {name: "cut", ids: ["note.cut.1"], recs: [cut], text: ((t) => t.slice(0, t.indexOf(cut) + cut.length))(state([cut]))},
        {name: "shadowed", ids: ["note.shadow.1"], recs: [shadow], text: deep2(state([shadow]))},
        {name: "session only", ids: [], recs: [], text: deep2(state([]))},
        {name: "near the limit together", ids: ["note.big.1"], recs: [big], parts: 2, text: deep2(state([big]))}];
      out.exact = [];
      out.sessions = [];
      for (const {name, ids, recs, text, parts = 1} of texts) await phase("stored text " + name, async () => {   // each row stands alone
        out.recoverable.push(...ids);
        await p.ev(`localStorage.setItem(${JSON.stringify(KEY)}, ${JSON.stringify(text)})`);
        await p.reload();
        d = await p.dbg();
        check(`${name}: refused, and its ${ids.length} complete note(s) and the saved session offered`, d.notes === 0 &&
              (await p.count("#tp-saved-export")) === 1 && new RegExp(`export the ${ids.length + 1} record`).test(d.storageNotice || ""), d.storageNotice);
        await p.click("#tp-saved-export");
        await p.settle();
        const n = await p.count('[data-tp-part^="tp-saved:"]'), got = [];
        for (let k = 1; k <= n; k++) {
          if (n > 1) { await p.click(`#tp-saved-show-${k}`); await p.settle(); }
          got.push(await p.ev(`(document.querySelector("#tp-saved-text-${k}") || {}).value || ""`));
        }
        check(`${name}: offered as ${parts} part(s), each within one bundle`, n === parts && got.every((t) => t.length > 0 && Buffer.byteLength(t) <= LIMIT),
              got.map((t) => Buffer.byteLength(t)));
        const files = got.map((t, k) => save(`recovered-${name.replace(/ /g, "-")}-${k + 1}.json`, t));
        out.recovered.push(...files);
        out.sessions.push(...files);
        recs.forEach((r, i) => {
          const at = files.find((f, k) => got[k].indexOf(r) >= 0);
          out.exact.push([at || files[0], r]);
          check(`${ids[i]}: the record is exported as its exact text`, !!at, got.map((t) => t.slice(0, 200)));
        });
        check(`${name}: the saved session is exported as its exact text, once`, got.filter((t) => t.indexOf(session) >= 0).length === 1);
        check(`${name}: the saved data stays as it was`, (await p.ev(stored)) === text);
      });
      out.sessionId = saved.session.id;
      // with no importable record (the note unreadable, no session): no export is offered
      const none = JSON.stringify(Object.assign({}, saved, {session: null, notes: [Object.assign({}, note, {id: "note.bad.2", text: "lone \ud800"})]})).replace(/}$/, `,"zz":${nest}}`);
      await p.ev(`localStorage.setItem(${JSON.stringify(KEY)}, ${JSON.stringify(none)})`);
      await p.reload();
      d = await p.dbg();
      check("with no importable record in it, no export is offered (the download still is)", /could not be read/.test(d.storageNotice || "") &&
            (await p.count("#tp-saved-export")) === 0 && (await p.count("#tp-saved-download")) === 1, d.storageNotice);
    });
    await browser.dispose(ctx);
  } catch (e) {
    check("flow ran to the end", false, String(e && e.stack || e));
  } finally {
    await browser.close();
  }
  check("no request left file:, blob: or data:", browser.requests.every(allowedRequest), browser.requests.filter((u) => !allowedRequest(u)));
  check("no console error or CSP violation", browser.logs.length === 0, browser.logs);
  return Object.assign(out, {checks, failures: checks.filter((c) => !c[1]), requests: browser.requests});
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  const [mode, html, work, ...rest] = process.argv.slice(2);
  const keep = rest[0] === "--keep" ? rest[1] : null;
  if (!["acceptance", "limits"].includes(mode) || !html || !work) {
    console.error("usage: product_design_browser.mjs acceptance|limits BUILD.html WORKDIR [--keep DIR]");
    process.exit(2);
  }
  const out = mode === "limits" ? await limits(path.resolve(html), path.resolve(work))
                                : await acceptance(path.resolve(html), path.resolve(work), keep && path.resolve(keep));
  // exit once the output is written: a pipe takes 64 KiB at a time, and exiting earlier would cut it off
  process.stdout.write(JSON.stringify(out, null, 1) + "\n", () => process.exit(out.failures.length ? 1 : 0));
}
