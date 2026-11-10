// The browser half of tools/smoke/product_design_demo.py (plan section 13, steps 1 to 5): two reviewers use the offline
// review page in two fresh browser contexts of one headless Chrome (a fresh --user-data-dir under WORKDIR), each exports
// a feedback bundle. Driven through tests/product_design_browser.mjs (the PD-C driver): real mouse and key events, no
// network (every request is file:, blob: or data:).
//
//   node tools/smoke/product_design_demo_browser.mjs BUILD.html WORKDIR
//
// Writes WORKDIR/feedback/reviewer-a.json and reviewer-b.json (the downloaded bundles) and WORKDIR/screens/*.png, and
// prints one JSON object: {checks: [[step, name, ok, detail]], failures, bundles, screens, observed}. Exit 0 when every
// check passed, 1 otherwise.
import fs from "node:fs";
import path from "node:path";
import {pathToFileURL} from "node:url";
import {Browser, allowedRequest} from "../../tests/product_design_browser.mjs";

export const REVIEWER_A = "Ana (reviewer A, synthetic)";
export const REVIEWER_B = "Ben (reviewer B, synthetic)";
export const NOTE_A = "Which submission do I take first? Nothing on the queue says it is oldest first.";
export const NOTE_B = "If this is reassigned while I am halfway through, what happens to my notes and draft decision?";

async function run(htmlPath, workDir) {
  const checks = [];
  let step = 0;
  const check = (name, ok, detail) => { checks.push([step, name, !!ok, detail === undefined ? null : detail]); return ok; };
  const url = pathToFileURL(htmlPath).href;
  const feedback = path.join(workDir, "feedback"), screens = path.join(workDir, "screens");
  fs.mkdirSync(feedback, {recursive: true});
  fs.mkdirSync(screens, {recursive: true});
  const downloads = fs.mkdtempSync(path.join(workDir, "downloads."));
  const out = {bundles: {}, screens: {}, observed: {}};
  const shot = async (page, name) => { const f = path.join(screens, name + ".png"); await page.screenshot(f); out.screens[name] = f; };
  const exportBundle = async (page, name) => {
    await page.click('[data-tp-view="feedback"]');
    const dl = await page.download(downloads, () => page.click("#tp-export"));
    const file = path.join(feedback, name + ".json");
    fs.copyFileSync(dl.file, file);
    out.bundles[name] = file;
    return JSON.parse(fs.readFileSync(file, "utf8"));
  };
  const browser = await Browser.launch(workDir);
  try {
    // ---- reviewer A, in its own browser context
    const ctxA = await browser.context();
    const a = await browser.page(ctxA);
    step = 1;
    await a.load(url + "#journeys");
    let d = await a.dbg();
    check("the page loads offline, storage available, no load error", d.storage.ok && d.errors.length === 0, d.errors);
    await a.type("#tp-reviewer", REVIEWER_A);
    await a.click('[data-tp-start="guided:scenario.submit-complete-record"]');
    check("the guided task highlights its first target", (await a.count('.tp-highlight[data-tp-target="page.submit"]')) === 1);
    await shot(a, "1-guided-highlight");
    await a.click('[data-tp-target="page.submit"]');
    await a.type('[data-tp-target="submit.title"]', "Laptop request");
    await a.choose('[data-tp-target="submit.category"]', "Hardware");
    await a.click('[data-tp-target="submit.send"]');
    d = await a.dbg();
    check("the requester's submit is one simulated request (201, the submissions contract)",
          d.events.length === 1 && d.events[0].status === "201" && d.events[0].contract === "contract.submissions-api", d.events);
    await a.click('[data-tp-target="page.queue"]');
    const row = await a.text('[data-tp-target="queue.row.s-1"]');
    check("the reviewer's queue page lists the new record S-1", /S-1/.test(row || "") && /Laptop request/.test(row || ""), row);
    d = await a.dbg();
    check("the guided task completes on its observed condition", d.session.tasks[0].outcome === "completed", d.session.tasks);
    await a.click('[data-tp-target="page.summary"]');
    check("the summary page counts it: Waiting: 1", (await a.text('[data-tp-target="summary.waiting"]')) === "Waiting: 1");

    step = 4;   // reviewer A, on the queue the record just reached: a note on the confusing choice of what to take first
    await a.click('[data-tp-target="page.queue"]');
    const acted = (await a.dbg()).run.actions.length;
    await a.key("n");
    await a.click('[data-tp-target="queue.row.s-1"]');
    d = await a.dbg();
    out.observed.noteFocus = await a.ev("document.activeElement && (document.activeElement.id || document.activeElement.tagName)");
    check("note mode picks the queue row as the target and does not open the record",
          d.pending && d.pending.target.id === "queue.row.s-1" && d.run.actions.length === acted && d.state.page === "page.queue", d.pending);
    await a.type("#tp-note-text", NOTE_A, {leave: false});
    await a.choose("#tp-note-category", "confusing");
    await shot(a, "4-note-open");
    await a.click("#tp-note-save");
    d = await a.dbg();
    check("reviewer A's note is saved with the state it was written in", d.notes === 1 && d.pending === null);

    step = 2;   // reviewer A again: the guided exception path, then its request and data consequence
    await a.click('[data-tp-view="journeys"]');
    await a.click('[data-tp-start="guided:scenario.submit-missing-category"]');
    await a.click('[data-tp-target="page.submit"]');
    await a.type('[data-tp-target="submit.title"]', "Laptop request");
    await a.click('[data-tp-target="submit.send"]');
    d = await a.dbg();
    check("submit without a category is refused (one 422 event) and nothing reaches the queue",
          d.events.length === 1 && d.events[0].status === "422" && d.state.submissions.length === 0, d.events);
    check("the guided exception task completes", d.session.tasks[1] && d.session.tasks[1].outcome === "completed", d.session.tasks);
    await a.click('[data-tp-view="engineering"]');
    await a.click('[data-tp-event="1"]');
    const detail = (await a.text("#tp-event-detail")) || "";
    out.observed.missingFieldDetail = detail.slice(0, 600);
    check("the engineering view shows the proposed request, the 422 response and its consequence",
          /Create submission/.test(detail) && /422/.test(detail) && /names each missing field/.test(detail) &&
          /Simulated: proposed contract/.test(detail), detail.slice(0, 400));
    await shot(a, "2-missing-field-engineering");

    step = 5;
    const bundleA = await exportBundle(a, "reviewer-a");
    check("reviewer A exports one bundle: the note, the session with both guided tasks",
          bundleA.schema === "tp-product-feedback" && bundleA.notes.length === 1 && bundleA.notes[0].text === NOTE_A &&
          bundleA.notes[0].reviewer === REVIEWER_A && bundleA.sessions.length === 1 &&
          bundleA.sessions[0].tasks.map((t) => t.mode + ":" + t.outcome).join() === "guided:completed,guided:completed",
          {notes: bundleA.notes.length, sessions: bundleA.sessions.map((s) => s.tasks)});
    await shot(a, "5-feedback-exported");
    await browser.dispose(ctxA);

    // ---- reviewer B, a fresh context: nothing of A's is there
    const ctxB = await browser.context();
    const b = await browser.page(ctxB);
    step = 3;
    await b.load(url + "#journeys");
    d = await b.dbg();
    check("a second reviewer's fresh context starts empty", d.notes === 0 && d.sessions === 0);
    await b.type("#tp-reviewer", REVIEWER_B);
    await b.click('[data-tp-start="unguided:scenario.resolve-assigned-submission"]');
    d = await b.dbg();
    const hintsShown = async () => (await b.count(".tp-highlight")) + (await b.count(".tp-hint")) + (await b.count("#tp-step"));
    check("the unguided task starts on its seed (S-1 assigned) with a goal and no click hints",
          d.unguided && d.run.seed === "one-assigned" && /Resolve a submission assigned to you/.test(await b.text("#tp-task")) &&
          (await hintsShown()) === 0, d.unguided);
    await shot(b, "3-unguided-task");
    await b.click('[data-tp-target="page.queue"]');
    await b.click('[data-tp-target="queue.row.s-1"]');
    check("no hint appears while the reviewer finds the record", (await hintsShown()) === 0);

    step = 4;   // reviewer B, on the record: asks what happens when the assignment changes, without acting
    const before = (await b.dbg()).run.actions.length;
    await b.click('[data-tp-target="record.reassign"]', "right");
    d = await b.dbg();
    check("right-click on Reassign opens a note and does not reassign", d.pending && d.pending.target.id === "record.reassign" &&
          d.run.actions.length === before, d.pending && d.pending.target);
    await b.type("#tp-note-text", NOTE_B, {leave: false});
    await b.choose("#tp-note-category", "question");
    await b.click("#tp-note-save");
    d = await b.dbg();
    check("reviewer B's question is saved as a note", d.notes === 1 && d.pending === null);

    step = 3;
    await b.click('[data-tp-target="record.approve"]');   // the task completes when its goal is observed: no "I'm done" needed
    d = await b.dbg();
    const task = d.session.tasks[0];
    check("the unguided reviewer resolves the record: completed on the observed condition, no help asked",
          task.mode === "unguided" && task.outcome === "completed" && task.help_requested === 0 && task.assisted_steps === 0 &&
          d.state.submissions[0].status === "approved", task);
    check("the task panel shows the outcome", /Status: completed\. Help requested: 0\./.test(await b.text("#tp-task-outcome")));
    await shot(b, "3-unguided-completed");

    step = 5;
    const bundleB = await exportBundle(b, "reviewer-b");
    check("reviewer B exports one bundle: the question note, the unguided session",
          bundleB.notes.length === 1 && bundleB.notes[0].text === NOTE_B && bundleB.notes[0].mode === "unguided" &&
          bundleB.sessions.length === 1 && bundleB.sessions[0].tasks[0].outcome === "completed", bundleB.notes.map((n) => n.mode));
    check("the two bundles share no note id", !bundleA.notes.some((n) => bundleB.notes.some((m) => m.id === n.id)));
    await browser.dispose(ctxB);
  } catch (e) {
    check("browser flow ran to the end", false, String(e && e.stack || e));
  } finally {
    await browser.close();
  }
  step = 5;
  check("no request left file:, blob: or data:", browser.requests.every(allowedRequest), browser.requests.filter((u) => !allowedRequest(u)));
  check("no console error or CSP violation", browser.logs.length === 0, browser.logs);
  return Object.assign(out, {checks, failures: checks.filter((c) => !c[2])});
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  const [html, work] = process.argv.slice(2);
  if (!html || !work) {
    console.error("usage: product_design_demo_browser.mjs BUILD.html WORKDIR");
    process.exit(2);
  }
  const out = await run(path.resolve(html), path.resolve(work));
  process.stdout.write(JSON.stringify(out, null, 1) + "\n", () => process.exit(out.failures.length ? 1 : 0));
}
