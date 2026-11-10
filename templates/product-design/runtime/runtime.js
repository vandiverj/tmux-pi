/* tp-product review runtime (docs/product-design.md, "The review build"). Inlined into one offline HTML file by
 * `tp-product build`; plain JavaScript, no framework, no network: the page's Content-Security-Policy has
 * connect-src 'none' and no 'unsafe-eval', and nothing here fetches, imports or evaluates text.
 *
 * Safety rule: every piece of package or feedback data reaches the page as a text node or an attribute value set by
 * h() below, which refuses event-handler, style and URL attributes. No markup is parsed from text and no text is run as code.
 *
 * Prototype state has one source: S.run = {seed, actions}. The state is the adapter's seed folded through its reducer
 * with those actions, so reset (no actions), back (one fewer), reload and restore (the saved list) all replay from the
 * seed and can never apply an action twice. */
(function () {
  "use strict";

  var DATA = null;          // the build's embedded data (tp-product-review-build)
  var PROTO = null;         // the registered prototype adapter
  var LOAD_ERRORS = [];     // script errors seen before start (a prototype that failed to load)
  var KEY = "";             // the browser storage key, namespaced by package id

  // ------------------------------------------------------------------ safe DOM

  // Attributes that carry no URL, no script and no style. Everything else (on*, href, src, style, srcdoc, action...)
  // is refused, so neither the runtime nor a prototype can turn data into markup, a request or code.
  var ALLOWED_ATTR = /^(class|id|type|value|role|tabindex|title|placeholder|name|for|rows|cols|colspan|rowspan|scope|headers|accept|disabled|readonly|checked|selected|multiple|hidden|open|lang|dir|alt|min|max|step|maxlength|size|spellcheck|autocomplete|aria-[a-z-]+|data-[a-z-]+)$/;

  function h(tag, attrs, children) {
    var el = document.createElement(tag);
    attrs = attrs || {};
    Object.keys(attrs).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) return;
      if (!ALLOWED_ATTR.test(k)) throw new Error("h(): attribute " + k + " is not allowed");
      if (k === "value" && (tag === "input" || tag === "textarea" || tag === "select")) el.value = String(v);
      else el.setAttribute(k, v === true ? "" : String(v));
    });
    append(el, children);
    return el;
  }
  function append(el, c) {
    if (c === null || c === undefined || c === false) return;
    if (Array.isArray(c)) { c.forEach(function (x) { append(el, x); }); return; }
    if (c instanceof Node) { el.appendChild(c); return; }
    el.appendChild(document.createTextNode(String(c)));
  }
  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }
  function button(label, onClick, attrs) {
    var b = h("button", Object.assign({"type": "button"}, attrs || {}), label);
    b.addEventListener("click", onClick);
    return b;
  }

  // ------------------------------------------------------------------ small helpers

  function clone(v) { return v === undefined ? undefined : JSON.parse(JSON.stringify(v)); }
  // Data-keyed lookups use own properties only: an id such as constructor, __proto__ or toString is just an id.
  function own(o, k) { return o !== null && typeof o === "object" && Object.prototype.hasOwnProperty.call(o, k); }
  function dict() { return Object.create(null); }
  function canonical(v) {
    if (Array.isArray(v)) return "[" + v.map(canonical).join(",") + "]";
    if (v && typeof v === "object") {
      return "{" + Object.keys(v).sort().map(function (k) { return JSON.stringify(k) + ":" + canonical(v[k]); }).join(",") + "}";
    }
    return JSON.stringify(v);
  }
  function now() { return new Date().toISOString().replace(/\.\d+Z$/, "Z"); }
  function randomId() {
    var b = new Uint8Array(5);
    crypto.getRandomValues(b);
    return "r" + Array.prototype.map.call(b, function (x) { return ("0" + x.toString(16)).slice(-2); }).join("");
  }
  function utf8Length(s) { return new TextEncoder().encode(s).length; }
  function short(fp) { return fp ? fp.replace(/^sha256:/, "").slice(0, 12) : "none"; }
  function byId(coll) {
    var out = dict();
    (DATA.design[coll] || []).forEach(function (r) { out[r.id] = r; });
    return out;
  }
  function record(id) {
    var kind = String(id).split(".")[0];
    var colls = {source: "sources", claim: "claims", actor: "actors", journey: "journeys", rule: "rules",
                 scenario: "scenarios", question: "questions", decision: "decisions", contract: "contracts",
                 slice: "slices", "case": "cases", measurement: "measurements"};
    if (!own(colls, kind)) return null;
    var coll = colls[kind];
    return (DATA.design[coll] || []).filter(function (r) { return r.id === id; })[0] || null;
  }
  function title(id) { var r = record(id); return r && r.title ? r.title : id; }

  // ------------------------------------------------------------------ schema subset (the same schema file Python uses)

  function isType(t, v) {
    if (t === "object") return v !== null && typeof v === "object" && !Array.isArray(v);
    if (t === "array") return Array.isArray(v);
    if (t === "string") return typeof v === "string";
    if (t === "integer") return typeof v === "number" && Number.isInteger(v);
    if (t === "number") return typeof v === "number";
    if (t === "boolean") return typeof v === "boolean";
    if (t === "null") return v === null;
    return false;
  }
  function wellFormed(s) {
    return typeof s.isWellFormed === "function" ? s.isWellFormed()
      : !/[\ud800-\udbff](?![\udc00-\udfff])|(?:^|[^\ud800-\udbff])[\udc00-\udfff]/.test(s);
  }
  function readable(s) {   // a string fit to show in a message: lone surrogates become U+FFFD
    return typeof s.toWellFormed === "function" ? s.toWellFormed() : s.replace(/[\ud800-\udfff]/g, "\ufffd");
  }
  // Every string and every key, at any depth (iterative, like depth()): the first one that is not valid Unicode, as
  // package.text_problem refuses it (the CLI cannot write a lone surrogate as UTF-8). null when there is none.
  function textProblem(v) {
    var stack = [{x: v, up: null, key: null}];
    function where(n, key) {
      var at = key === null ? [] : [key];
      for (; n && n.key !== null; n = n.up) at.unshift(n.key);
      return at.length ? at.map(function (k) { return readable(String(k)); }).join(".") : "(bundle)";
    }
    while (stack.length) {
      var n = stack.pop(), x = n.x;
      if (typeof x === "string") {
        if (!wellFormed(x)) return "not valid Unicode: " + where(n, null) + " holds a lone surrogate";
        continue;
      }
      if (x === null || typeof x !== "object") continue;
      if (Array.isArray(x)) { for (var i = x.length - 1; i >= 0; i--) stack.push({x: x[i], up: n, key: i}); continue; }
      var ks = Object.keys(x);
      for (var j = ks.length - 1; j >= 0; j--) {
        if (!wellFormed(ks[j])) return "not valid Unicode: the key " + where(n, ks[j]) + " holds a lone surrogate";
        stack.push({x: x[ks[j]], up: n, key: ks[j]});
      }
    }
    return null;
  }
  // Nesting depth (a top-level object is 1, as package.depth_problem counts it), without recursion: any depth is
  // measured, and the walk stops as soon as it is past `stopAt`.
  function depth(v, stopAt) {
    var max = 0, stack = [v, 1];
    while (stack.length) {
      var d = stack.pop(), x = stack.pop();
      if (x === null || typeof x !== "object") continue;
      if (d > max) { max = d; if (stopAt !== undefined && max > stopAt) return max; }
      if (Array.isArray(x)) for (var i = 0; i < x.length; i++) stack.push(x[i], d + 1);
      else Object.keys(x).forEach(function (k) { stack.push(x[k], d + 1); });
    }
    return max;
  }
  // The nesting depth of JSON text, counted on the text before it is parsed (strings skipped). The parser and
  // everything after it then only ever see values within the limit.
  function textDepth(text, stopAt) {
    var d = 0, max = 0, inStr = false;
    for (var i = 0; i < text.length; i++) {
      var c = text.charCodeAt(i);
      if (inStr) { if (c === 92) i += 1; else if (c === 34) inStr = false; continue; }
      if (c === 34) inStr = true;
      else if (c === 123 || c === 91) { d += 1; if (d > max) { max = d; if (max > stopAt) return max; } }
      else if (c === 125 || c === 93) d -= 1;
    }
    return max;
  }
  // textDepth over raw UTF-8 bytes, resumable: st carries the depth and string state from one chunk to the next
  function bytesDepth(st, u8, stopAt) {
    for (var i = 0; i < u8.length; i++) {
      var c = u8[i];
      if (st.inStr) { if (st.esc) st.esc = false; else if (c === 92) st.esc = true; else if (c === 34) st.inStr = false; continue; }
      if (c === 34) st.inStr = true;
      else if (c === 123 || c === 91) { st.d += 1; if (st.d > st.max) { st.max = st.d; if (st.max > stopAt) return; } }
      else if (c === 125 || c === 93) st.d -= 1;
    }
  }
  // Numbers the page could not export as they were written: JSON.parse reads a number into a double, and
  // JSON.stringify writes that double back. A token whose text would change (an integer beyond 2^53, -0, 1.0, 1e5, a
  // long decimal) would be re-exported as another value than the CLI holds, so it is refused, never rewritten.
  function numberProblems(text) {
    var re = /"(?:[^"\\]|\\.)*"|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/g, m, out = [];
    while ((m = re.exec(text)) !== null) {
      var tok = m[0];
      if (tok.charCodeAt(0) === 34) continue;
      var back = JSON.stringify(Number(tok));
      if (back !== tok) {
        out.push("the number " + tok + " would be exported as " + back + " (this page keeps numbers as JavaScript does), so the bundle cannot round-trip");
        if (out.length >= 5) break;
      }
    }
    return out;
  }
  function schemaIssues(entry, value) {
    var defs = DATA.schema.$defs, issues = [];
    function add(path, msg) { if (issues.length < 50) issues.push((path.length ? path.join(".") : "(bundle)") + ": " + msg); }
    function walk(s, v, path) {
      if (s === true) return;
      if (s === false) { add(path, "no value is allowed here"); return; }
      if (s.$ref) walk(defs[s.$ref.replace("#/$defs/", "")], v, path);   // the build's own schema: its keys are ours
      if (s.type) {
        var ts = Array.isArray(s.type) ? s.type : [s.type];
        if (!ts.some(function (t) { return isType(t, v); })) { add(path, "expected " + ts.join(" or ")); return; }
      }
      if (own(s, "const") && canonical(v) !== canonical(s["const"])) add(path, "must be " + JSON.stringify(s["const"]));
      if (s["enum"] && !s["enum"].some(function (o) { return canonical(o) === canonical(v); })) add(path, "is not one of " + s["enum"].join(", "));
      if (typeof v === "string") {
        var n = Array.from(v).length;
        if (!wellFormed(v)) add(path, "is not valid Unicode text");
        if (own(s, "minLength") && n < s.minLength) add(path, "shorter than " + s.minLength);
        if (own(s, "maxLength") && n > s.maxLength) add(path, "longer than " + s.maxLength);
        if (s.pattern && !new RegExp(s.pattern).test(v)) add(path, "does not match " + s.pattern);
        if (s["x-id"]) {   // schema.id_kind: <kind>.<slug>[.<slug>...], at most 96 characters
          var m = /^([a-z0-9]+(?:-[a-z0-9]+)*)\.[a-z0-9]+(?:-[a-z0-9]+)*(?:\.[a-z0-9]+(?:-[a-z0-9]+)*)*$/.exec(v);
          if (!m || n > 96 || m[1] !== s["x-id"]) add(path, JSON.stringify(v) + " is not a " + s["x-id"] + ".<slug> id");
        }
      }
      if (typeof v === "number" && own(s, "minimum") && v < s.minimum) add(path, "less than " + s.minimum);
      if (Array.isArray(v)) {
        if (own(s, "minItems") && v.length < s.minItems) add(path, "needs at least " + s.minItems + " item(s)");
        if (s.items) v.forEach(function (x, i) { walk(s.items, x, path.concat([i])); });
      }
      if (isType("object", v)) {
        var props = s.properties || {};
        (s.required || []).forEach(function (k) { if (!own(v, k)) add(path.concat([k]), "required field is missing"); });
        Object.keys(v).forEach(function (k) {
          if (!wellFormed(k)) add(path, "a field name is not valid Unicode text");
          if (own(props, k)) walk(props[k], v[k], path.concat([k]));
          else if (s.additionalProperties === false) add(path.concat([k]), "unknown field");
          else if (s.additionalProperties && typeof s.additionalProperties === "object") walk(s.additionalProperties, v[k], path.concat([k]));
        });
      }
    }
    walk(defs[entry], value, []);
    return issues;
  }

  // ------------------------------------------------------------------ review targets

  var REG = {exact: dict(), prefixes: dict()};
  function buildRegistry() {
    DATA.view_targets.forEach(function (t) { REG.exact[t.id] = t.label; });
    Object.keys(DATA.design).forEach(function (coll) {
      if (Array.isArray(DATA.design[coll])) DATA.design[coll].forEach(function (r) { if (r && r.id) REG.exact[r.id] = r.title || r.id; });
    });
    if (DATA.adapter) DATA.adapter.targets.forEach(function (t) {
      if (t.instances) REG.prefixes[t.id] = t.label; else REG.exact[t.id] = t.label;
    });
  }
  function knownTarget(id) {
    if (REG.exact[id] !== undefined) return true;
    return Object.keys(REG.prefixes).some(function (p) { return id.length > p.length + 1 && id.indexOf(p + ".") === 0; });
  }
  function tgt(el, id, label, context) {
    el.setAttribute("data-tp-target", id);
    el.setAttribute("data-tp-label", String(label || id).slice(0, 200));
    if (context) el.setAttribute("data-tp-context", String(context).slice(0, 1000));
    if (!knownTarget(id)) {
      el.classList.add("tp-undeclared");
      RT.undeclared[id] = true;
    }
    return el;
  }

  // ------------------------------------------------------------------ state

  var S = null;   // what is saved and exported: the current session, other sessions, notes, inputs, ui
  var RT = {      // derived or transient: never saved
    state: null, events: [], last: null, view: "overview", noteMode: false, pending: null, blocked: 0,
    selectedEvent: null, storage: {ok: false, reason: "not checked"}, storageNotice: null, message: null,
    stepError: null, importReport: null, summary: null, exportParts: null, exportError: null,
    savedRefused: null, salvage: null, showPart: dict(),
    undeclared: dict(), questionFilter: "open",
    hint: false, engScenario: null, holding: false, renderPending: false, focusNote: false, paints: 0
  };

  function newSession(reviewer) {
    return {id: randomId(), reviewer: reviewer || null, build: buildRef(), visited: [], tasks: [], prototype: null};
  }
  function buildRef() { return {revision: DATA.build.revision, fingerprint: DATA.build.content_fingerprint}; }
  function freshState() {
    var seed = DATA.adapter ? DATA.adapter.seeds[0].id : null;
    return {v: 1, session: newSession(null), sessions: [], notes: [], inputs: [],
            run: seed ? {seed: seed, actions: []} : null, ui: {guide: null, unguided: null}, exported: null};
  }

  // ------------------------------------------------------------------ storage (best effort)

  function storageProbe() {
    try {
      var ls = window.localStorage;
      ls.setItem(KEY + ":probe", "1");
      ls.removeItem(KEY + ":probe");
      return {ok: true, reason: null};
    } catch (e) {
      return {ok: false, reason: (e && e.name ? e.name + ": " : "") + (e && e.message ? e.message : String(e))};
    }
  }
  // Saved state is stored only when this page would read it back: the same check load() makes, on the same text.
  function save() {
    if (!RT.storage.ok) return;
    try {
      var text = JSON.stringify(S), back = storedProblems(text);
      if (back.problems.length) {
        RT.storage = {ok: false, reason: "not saved, because this page could not read it back (" + back.problems.slice(0, 3).join("; ") +
                                         "); the last state saved is kept"};
        return;
      }
      window.localStorage.setItem(KEY, text);
    } catch (e) {
      RT.storage = {ok: false, reason: "saving failed: " + (e && e.name ? e.name : "") + " " + (e && e.message ? e.message : "")};
    }
  }
  function load() {
    RT.storage = storageProbe();
    if (!RT.storage.ok) return freshState();
    var raw;
    try { raw = window.localStorage.getItem(KEY); } catch (e) { RT.storage = {ok: false, reason: String(e)}; return freshState(); }
    if (raw === null) return freshState();
    var got = storedProblems(raw);
    if (got.problems.length) {
      RT.storageNotice = "Review data saved in this browser for this package could not be read and was ignored (" +
        got.problems.slice(0, 3).join("; ") + "). It is left untouched under the browser storage key " + KEY + "; " +
        "nothing is saved over it from this tab. Download it as it is, or export the records in it that can still be imported.";
      RT.storage = {ok: false, reason: "saved data unreadable; not overwriting it"};
      RT.savedRefused = {raw: typeof raw === "string" ? raw : String(raw), value: got.value};
      return freshState();
    }
    return got.value;
  }
  // {value, problems}: the stored text parsed, and why this page would not use it ([] when it would). Whatever is
  // stored, the page starts: a check that throws is one more reason to ignore it.
  function storedProblems(raw) {
    var value, problems = [];
    try {
      if (typeof raw !== "string") problems.push("not text");
      else if (textDepth(raw, DATA.limits.depth) > DATA.limits.depth) problems.push("nested deeper than " + DATA.limits.depth + " levels");
      else {
        value = JSON.parse(raw);
        problems = savedProblems(value);
      }
    } catch (e) {
      problems.push((e && e.name ? e.name + ": " : "") + (e && e.message ? e.message : String(e)));
    }
    return {value: value, problems: problems};
  }
  // Every field the page reads from saved state is checked here, so a value that passes cannot throw later.
  function savedProblems(saved) {
    if (!isType("object", saved) || saved.v !== 1) return ["unknown format"];
    var want = ["v", "session", "sessions", "notes", "inputs", "run", "ui", "exported"];
    var extra = Object.keys(saved).filter(function (k) { return want.indexOf(k) < 0; });
    if (extra.length) return ["unknown fields " + extra.slice(0, 3).join(", ")];
    if (!isType("object", saved.session) || !Array.isArray(saved.sessions) || !Array.isArray(saved.notes) || !Array.isArray(saved.inputs)) return ["missing parts"];
    if (saved.run !== null && !(isType("object", saved.run) && typeof saved.run.seed === "string" && Array.isArray(saved.run.actions))) return ["bad prototype checkpoint"];
    if (saved.exported !== null && typeof saved.exported !== "string") return ["bad export marker"];
    // the session, sessions, notes, inputs and checkpoint: every rule a bundle has except its record cap (local state
    // is not one bundle; an export splits it into bundles that each fit)
    var p = bundleProblems(asBundle(saved), {local: true});
    if (p.length) return p;
    if (!isType("object", saved.ui) || Object.keys(saved.ui).sort().join() !== "guide,unguided") return ["bad ui state"];
    var tasks = saved.session.tasks;
    function ref(x, guided) {
      if (x === null) return true;
      if (!isType("object", x) || Object.keys(x).sort().join() !== (guided ? "advancedAt,scenario,step,task" : "scenario,task")) return false;
      var sc = typeof x.scenario === "string" ? record(x.scenario) : null;
      if (!sc || x.scenario.indexOf("scenario.") !== 0) return false;
      if (!isType("integer", x.task) || x.task < 0 || x.task >= tasks.length || tasks[x.task].scenario !== x.scenario) return false;
      if (!guided) return true;
      return isType("integer", x.step) && x.step >= 0 && x.step <= (sc.actions || []).length && Array.isArray(x.advancedAt) &&
             x.advancedAt.length === x.step && x.advancedAt.every(function (i) { return isType("integer", i) && i >= 0; });
    }
    if (!ref(saved.ui.guide, true) || !ref(saved.ui.unguided, false)) return ["bad task state"];
    return [];
  }
  function asBundle(s) {
    return {schema: "tp-product-feedback", schema_version: 1, package: DATA.package.id, exported: now(),
            notes: s.notes, inputs: s.inputs, sessions: [sessionRecord(s.session, s.run)].concat(s.sessions)};
  }
  function sessionRecord(sess, run) {
    var r = clone(sess);
    r.prototype = run ? clone(run) : null;
    return r;
  }

  // ------------------------------------------------------------------ prototype engine

  function adapterAction(id) {
    return DATA.adapter ? DATA.adapter.actions.filter(function (a) { return a.id === id; })[0] || null : null;
  }
  function enrich(ev, act, idx) {
    var a = adapterAction(act.action), contract = a && a.contract ? record(a.contract) : null;
    return {
      seq: 0, index: idx, action: act.action, target: act.target, operation: String(ev.operation || (a && a.operation) || act.action),
      contract: contract ? contract.id : null,
      label: contract ? (contract.status === "existing-verified" ? "Existing verified contract" : "Simulated: proposed contract")
                      : "Simulated locally: no contract proposed",
      request: String(ev.request || ""), response: String(ev.response || ""), status: String(ev.status || ""),
      records: (ev.records || []).map(String), consequence: String(ev.consequence || "")
    };
  }
  function reduceOne(state, act, idx) {
    var r = PROTO.reduce(clone(state), clone(act)) || {};
    if (!r.state) throw new Error("the prototype's reducer returned no state for " + act.action);
    return {state: r.state, ok: r.ok !== false, message: r.message || null,
            events: (r.events || []).map(function (e) { return enrich(e, act, idx); })};
  }
  function replay() {
    RT.state = null; RT.events = []; RT.last = null;
    if (!PROTO || !S.run) return;
    var seedFn = own(PROTO.seeds, S.run.seed) ? PROTO.seeds[S.run.seed] : null;
    if (!seedFn) { RT.message = {kind: "error", text: "The prototype has no seed named " + S.run.seed + "."}; return; }
    var state = clone(seedFn());
    S.run.actions.forEach(function (act, i) {
      var r = reduceOne(state, act, i);
      state = r.state;
      r.events.forEach(function (e) { e.seq = RT.events.length + 1; RT.events.push(e); });
      RT.last = r;
    });
    RT.state = state;
  }
  function dispatch(action, target, input, opts) {
    opts = opts || {};
    if (RT.noteMode) { RT.blocked += 1; return; }   // a target picked in note mode never runs its business action
    // While a note is open the prototype is paused: the note holds the state it was started on, and the second click
    // of a double click that picked the target must not act on it.
    if (RT.pending) {
      RT.blocked += 1;
      RT.message = {kind: "info", paused: true, text: "The prototype is paused while a note is open: save the note or press Escape to continue."};
      render();
      return;
    }
    if (!PROTO || !S.run || !RT.state) return;
    var act = {action: String(action), target: String(target)};
    if (input && Object.keys(input).length) act.input = clone(input);
    var idx = S.run.actions.length, r;
    try { r = reduceOne(RT.state, act, idx); } catch (e) { RT.message = {kind: "error", text: "Prototype error: " + e.message}; render(); return; }
    S.run.actions.push(act);
    RT.state = r.state;
    r.events.forEach(function (e) { e.seq = RT.events.length + 1; RT.events.push(e); });
    RT.last = r;
    RT.message = null;
    afterAction(act, idx, !!opts.assisted);
    save();
    render();
  }
  function back() {
    if (!S.run || !S.run.actions.length) return;
    var idx = S.run.actions.length - 1;
    S.run.actions.pop();
    var g = S.ui.guide;
    if (g && g.advancedAt.length && g.advancedAt[g.advancedAt.length - 1] === idx) {
      g.advancedAt.pop();
      g.step -= 1;
      var t = S.session.tasks[g.task];
      if (t && t.outcome !== "in-progress") { t.outcome = "in-progress"; t.ended = null; }
    }
    replay();
    save();
    render();
  }
  function reset(seed) {
    if (!S.run) return;
    if (seed && seed !== S.run.seed) { endTasks("abandoned"); S.run.seed = seed; }
    S.run.actions = [];
    if (S.ui.guide) { S.ui.guide.step = 0; S.ui.guide.advancedAt = []; var t = S.session.tasks[S.ui.guide.task]; if (t) { t.outcome = "in-progress"; t.ended = null; } }
    replay();
    save();
    render();
  }

  // ------------------------------------------------------------------ guided and unguided tasks

  function scenarioJourney(sid) {
    var j = (DATA.design.journeys || []).filter(function (j) {
      return j.steps.some(function (s) { return (s.scenarios || []).indexOf(sid) >= 0; });
    })[0];
    return j ? j.id : null;
  }
  function endTasks(outcome) {
    [S.ui.guide, S.ui.unguided].forEach(function (x) {
      if (!x) return;
      var t = S.session.tasks[x.task];
      if (t && t.outcome === "in-progress") { t.outcome = outcome; t.ended = now(); }
    });
    S.ui.guide = null;
    S.ui.unguided = null;
    RT.hint = false;
  }
  function startTask(sid, mode) {
    var sc = record(sid);
    if (!sc || !PROTO) return;
    if (!own(PROTO.seeds, sc.seed)) {
      RT.message = {kind: "error", text: sc.id + " starts from seed '" + sc.seed + "', which this prototype does not provide."};
      render();
      return;
    }
    endTasks("abandoned");
    S.run = {seed: sc.seed, actions: []};
    replay();
    var prior = S.session.tasks.some(function (t) { return t.mode === "guided"; });
    S.session.tasks.push({scenario: sid, mode: mode, outcome: "in-progress", help_requested: 0, assisted_steps: 0,
                          prior_guided: prior, started: now(), ended: null});
    if (S.session.visited.indexOf(sid) < 0) S.session.visited.push(sid);
    var ref = {scenario: sid, task: S.session.tasks.length - 1};
    if (mode === "guided") { ref.step = 0; ref.advancedAt = []; S.ui.guide = ref; } else { S.ui.unguided = ref; }
    RT.message = null;
    save();
    go("experience");
  }
  function afterAction(act, idx, assisted) {
    var g = S.ui.guide;
    if (g) {
      var sc = record(g.scenario), t = S.session.tasks[g.task];
      var st = sc && sc.actions[g.step];
      if (st && st.action === act.action && st.target === act.target) {
        g.advancedAt.push(idx);
        g.step += 1;
        if (assisted && t) t.assisted_steps += 1;
        if (g.step === sc.actions.length && t) {
          var met = condition(g.scenario);
          if (met === true) { t.outcome = "completed"; t.ended = now(); }
          else RT.message = {kind: "warn", text: met === null ? "All steps done. This prototype has no completion check for this scenario: mark it done if the expected result shows."
                                                           : "All steps done, but the expected result is not observed. Use Back or Restart."};
        }
      } else if (st) {
        RT.message = {kind: "warn", text: "That was not step " + (g.step + 1) + ". The action stays in the prototype; use Back to undo it."};
      }
    }
    var u = S.ui.unguided;
    if (u) {
      var tu = S.session.tasks[u.task];
      if (tu && tu.outcome === "in-progress" && condition(u.scenario) === true) { tu.outcome = "completed"; tu.ended = now(); }
    }
  }
  function condition(sid) {
    var f = PROTO && own(PROTO.conditions, sid) ? PROTO.conditions[sid] : null;
    if (typeof f !== "function" || !RT.state) return null;
    try { return !!f(clone(RT.state)); } catch (e) { return false; }
  }
  function assist() {
    var g = S.ui.guide, sc = g && record(g.scenario), st = sc && sc.actions[g.step];
    if (!st) return;
    dispatch(st.action, st.target, st.input || null, {assisted: true});
  }

  // ------------------------------------------------------------------ notes and inputs

  function currentPage() {
    if (RT.view === "experience" && RT.state && typeof RT.state.page === "string") return RT.state.page.slice(0, 200);
    return "view." + RT.view;
  }
  function activeTask() {
    var g = S.ui.guide, u = S.ui.unguided;
    if (g) return {mode: "guided", ref: g, task: S.session.tasks[g.task]};
    if (u) return {mode: "unguided", ref: u, task: S.session.tasks[u.task]};
    return {mode: "free", ref: null, task: null};
  }
  function contextOf(el) {
    var c = el.getAttribute("data-tp-context");
    if (c) return c;
    var text = (el.textContent || el.value || "").replace(/\s+/g, " ").trim().slice(0, 300);
    var where = RT.view === "experience" && RT.state ? "prototype page " + RT.state.page : "view " + RT.view;
    return (text ? "\"" + text + "\" on " : "On ") + where + (S.run ? ", checkpoint " + S.run.seed + "@" + S.run.actions.length : "");
  }
  function closeNote() {   // every way a note ends (saved, cancelled, Escape, note mode toggled) resumes the prototype
    RT.pending = null;
    if (RT.message && RT.message.paused) RT.message = null;
  }
  function beginNote(el) {
    var id = el.getAttribute("data-tp-target");
    RT.noteMode = false;
    if (!knownTarget(id)) {
      RT.message = {kind: "error", text: "This element's target '" + id + "' is not declared in this build, so a note on it could not be anchored. Pick another element."};
      render();
      return;
    }
    var a = activeTask();
    RT.pending = {   // captured now: what happens after this does not change what the note refers to
      target: {id: id, label: el.getAttribute("data-tp-label") || id, context: contextOf(el).slice(0, 1000)},
      page: currentPage(),
      journey: a.ref ? scenarioJourney(a.ref.scenario) : null,
      scenario: a.ref ? a.ref.scenario : null,
      step: a.mode === "guided" ? a.ref.step : null,
      state: RT.state ? clone(RT.state) : {},
      checkpoint: S.run ? S.run.seed + "@" + S.run.actions.length : null,
      mode: a.mode,
      assistance: a.task ? (a.task.assisted_steps > 0 ? "assisted-advance" : a.task.help_requested > 0 ? "help-requested" : "none") : "none"
    };
    RT.focusNote = true;
    render();
  }
  function nextNumber(prefix) {
    var n = 0, re = new RegExp("^" + prefix.replace(/\./g, "\\.") + "(\\d+)$");
    S.notes.concat(S.inputs).forEach(function (r) { var m = re.exec(r.id); if (m) n = Math.max(n, +m[1]); });
    return n + 1;
  }
  function saveNote(text, category) {
    var p = RT.pending;
    if (!p) return "no note is open";
    if (!S.session.reviewer) return "Enter your reviewer label first (top of the page).";
    if (!text.trim()) return "The note is empty.";
    var note = {
      id: "note." + S.session.id + "." + nextNumber("note." + S.session.id + "."),
      review_session: S.session.id, package: DATA.package.id, build: buildRef(), reviewer: S.session.reviewer,
      created: now(), page: p.page, journey: p.journey, scenario: p.scenario, step: p.step, target: p.target,
      state: p.state, checkpoint: p.checkpoint, text: text, category: category || null, mode: p.mode, assistance: p.assistance
    };
    var issues = schemaIssues("note", note);
    if (issues.length) return "The note cannot be saved: " + issues.join("; ");
    var big = recordFits("notes", note);
    if (big) return "The note cannot be saved: " + big + ".";
    S.notes.push(note);
    closeNote();
    save();
    render();
    return null;
  }
  function addInput(type, target, text) {
    if (!S.session.reviewer) return "Enter your reviewer label first (top of the page).";
    if (!text.trim()) return "The answer is empty.";
    var input = {id: "input." + S.session.id + "." + nextNumber("input." + S.session.id + "."), type: type, target: target,
                 text: text, reviewer: S.session.reviewer, package_revision: DATA.build.revision, status: "pending"};
    var issues = schemaIssues("input", input);
    if (issues.length) return "Cannot record it: " + issues.join("; ");
    var big = recordFits("inputs", input);
    if (big) return "Cannot record it: " + big + ".";
    S.inputs.push(input);
    save();
    render();
    return null;
  }

  // ------------------------------------------------------------------ anchors (the same rule as tp-product import-feedback)

  function remapOf(id) {
    var r = (DATA.remaps || []).filter(function (x) { return x.note === id; });
    return r.length ? r[0] : null;
  }
  function anchorOf(note) {
    var rm = remapOf(note.id);
    if (rm) return knownTarget(rm.target) ? "remapped" : "unresolved";
    if (note.journey !== null && !(record(note.journey) && note.journey.indexOf("journey.") === 0)) return "unresolved";
    if (note.scenario !== null && !(record(note.scenario) && note.scenario.indexOf("scenario.") === 0)) return "unresolved";
    if (!knownTarget(note.target.id)) return "unresolved";
    return note.build.fingerprint === DATA.build.content_fingerprint ? "current" : "changed-context";
  }

  // ------------------------------------------------------------------ export, import, summary

  function allSessions() {
    return [sessionRecord(S.session, S.run)].concat(S.sessions).filter(function (s) {
      return s.tasks.length || s.visited.length || S.notes.some(function (n) { return n.review_session === s.id; }) ||
             (s.prototype && s.prototype.actions.length);
    });
  }
  function bundle() {
    return {schema: "tp-product-feedback", schema_version: 1, package: DATA.package.id, exported: now(),
            notes: clone(S.notes), inputs: clone(S.inputs), sessions: clone(allSessions())};
  }
  // What was last exported, as a short hash of the records' canonical text (two 32-bit FNV-1a passes with different
  // seeds): saved state holds the hash, not a second copy of every record, so exporting never doubles what is stored.
  function exportDigest() {
    var t = canonical({notes: S.notes, inputs: S.inputs}), a = 0x811c9dc5, b = 0x050c5d1f;
    for (var i = 0; i < t.length; i++) {
      var c = t.charCodeAt(i);
      a = Math.imul(a ^ c, 0x01000193);
      b = Math.imul(b ^ c, 0x01000193) ^ (b >>> 13);
    }
    return "fnv:" + ("0000000" + (a >>> 0).toString(16)).slice(-8) + ("0000000" + (b >>> 0).toString(16)).slice(-8) + ":" + t.length;
  }
  function download(name, text, type) {
    var url = URL.createObjectURL(new Blob([text], {type: type}));
    var a = document.createElement("a");
    a.href = url;            // a blob: URL made here, never data from the package
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 60000);
  }
  // What one bundle may hold: the limits tp-product import-feedback and this page's import enforce.
  function bundleFits(text, b) {
    var p = [];
    if (utf8Length(text) > DATA.limits.bytes) p.push(utf8Length(text) + " bytes; at most " + DATA.limits.bytes);
    if (b.notes.length + b.inputs.length > DATA.limits.records) p.push((b.notes.length + b.inputs.length) + " records; at most " + DATA.limits.records);
    if (depth(b, DATA.limits.depth) > DATA.limits.depth) p.push("nested deeper than " + DATA.limits.depth + " levels");
    return p;
  }
  function bundleText(b) { return JSON.stringify(b, null, 2); }
  // Why the CLI (and this page's import) would refuse these bytes, [] when it would take them: the limits of one bundle
  // and every rule an import applies (text, schema, ids, bookkeeping, numbers), on the text as it would be read back.
  function exportProblems(text, b) {
    var p = bundleFits(text, b);
    if (p.length) return p;
    p = bundleProblems(JSON.parse(text));
    return p.length ? p : numberProblems(text);
  }
  function emptyBundle(sessions) {
    return {schema: "tp-product-feedback", schema_version: 1, package: DATA.package.id, exported: now(), notes: [], inputs: [], sessions: sessions || []};
  }
  // The bundles an export of `whole` writes. One when it is importable as it is; otherwise the notes and inputs are
  // split, in order, into bundles that each fit (the sessions go with the first), and every part must be importable on
  // its own. Returns {parts: [{bundle, text}]} or {error}.
  function exportParts(whole) {
    var text = bundleText(whole);
    if (!exportProblems(text, whole).length) return {parts: [{bundle: whole, text: text}]};
    var head = {schema: whole.schema, schema_version: whole.schema_version, package: whole.package, exported: whole.exported};
    function part(sessions) { return {schema: head.schema, schema_version: head.schema_version, package: head.package, exported: head.exported, notes: [], inputs: [], sessions: sessions}; }
    var first = part(whole.sessions), firstText = bundleText(first), firstProblems = bundleFits(firstText, first);
    if (firstProblems.length) return {error: "the review sessions alone exceed one bundle (" + firstProblems.join("; ") + ")"};
    var parts = [], cur = first, size = utf8Length(firstText);
    var recs = whole.notes.map(function (r) { return ["notes", r]; }).concat(whole.inputs.map(function (r) { return ["inputs", r]; }));
    for (var i = 0; i < recs.length; i++) {
      var coll = recs[i][0], r = recs[i][1];
      // the record's size inside a bundle: its pretty text indented two more levels, a separator and a line break
      var rt = JSON.stringify(r, null, 2), cost = utf8Length(rt) + 4 * (rt.split("\n").length) + 2;
      if (depth(r, DATA.limits.depth) + 2 > DATA.limits.depth) return {error: r.id + " is nested deeper than one bundle allows (" + DATA.limits.depth + " levels)"};
      if (cur.notes.length + cur.inputs.length > 0 && (size + cost > DATA.limits.bytes || cur.notes.length + cur.inputs.length + 1 > DATA.limits.records)) {
        parts.push(cur);
        cur = part([]);
        size = utf8Length(bundleText(cur));
      }
      cur[coll].push(r);
      size += cost;
    }
    parts.push(cur);
    var out = [];
    for (var j = 0; j < parts.length; j++) {
      var t = bundleText(parts[j]), problems = exportProblems(t, parts[j]);
      if (problems.length) return {error: (parts.length === 1 ? "the bundle" : "part " + (j + 1) + " of " + parts.length) +
                                          " would not be importable (" + problems.slice(0, 3).join("; ") + ")"};
      out.push({bundle: parts[j], text: t});
    }
    return {parts: out};
  }
  // Each part as it is offered: its file name, text and counts.
  function offered(parts, base) {
    var n = parts.length;
    return parts.map(function (x, i) {
      return {name: base + (n === 1 ? "" : "-part-" + (i + 1) + "-of-" + n) + ".json", text: x.text, bytes: utf8Length(x.text),
              notes: x.bundle.notes.length, inputs: x.bundle.inputs.length};
    });
  }
  // Exporting offers every part twice over: a download, and on the page a save control and the JSON to copy (a browser
  // may block downloads, several downloads at once above all). The records count as exported only once every part is
  // on the page and its download has been started.
  function doExport() {
    var got = exportParts(bundle());
    if (got.error) {   // nothing is marked exported: the records stay counted as unsaved
      RT.exportError = "Export refused: " + got.error + ". Nothing was exported.";
      RT.exportParts = null;
      render();
      document.dispatchEvent(new CustomEvent("tp-exported", {detail: {ok: false, error: RT.exportError}}));
      return;
    }
    var parts = offered(got.parts, DATA.package.id + "-feedback-" + S.session.id), digest = exportDigest();
    RT.exportError = null;
    RT.exportParts = parts;
    RT.showPart = dict();
    render();
    settled().then(function () {
      parts.forEach(function (x) { download(x.name, x.text, "application/json"); });
      S.exported = digest;
      save();
      render();
      return settled();
    }).then(function () {
      document.dispatchEvent(new CustomEvent("tp-exported", {detail: {ok: true, parts: parts.length, offered: parts.length,
        notes: parts.reduce(function (n, x) { return n + x.notes; }, 0), inputs: parts.reduce(function (n, x) { return n + x.inputs; }, 0)}}));
    });
  }
  // Why one record cannot be exported (it would not be importable even alone in a bundle), or null.
  function recordProblem(coll, r) {
    var b = emptyBundle();
    b[coll].push(r);
    var p = exportProblems(bundleText(b), b);
    return p.length ? p.slice(0, 3).join("; ") : null;
  }
  // A record is kept only if it can be exported: on its own it is an importable bundle.
  function recordFits(coll, r) {
    var p = recordProblem(coll, r);
    return p ? "it would not be importable in an export bundle (" + p + ")" : null;
  }
  // Saved data this page refused to read: the records in it that are importable on their own, as importable parts.
  // Records that are not are listed by id with the reason; the stored bytes stay untouched and downloadable as they are.
  function salvage() {
    var v = RT.savedRefused && RT.savedRefused.value, excluded = [], ids = dict(), keep = {notes: [], inputs: []}, sessions = [];
    if (!isType("object", v)) return;
    ["notes", "inputs"].forEach(function (coll) {
      (Array.isArray(v[coll]) ? v[coll] : []).forEach(function (r, i) {
        var label = isType("object", r) && typeof r.id === "string" ? readable(r.id) : coll + "." + i;
        var why = recordProblem(coll, r);
        if (!why && ids[r.id] !== undefined) why = "another record here has this id";
        if (why) { excluded.push(label + ": " + why); return; }
        ids[r.id] = 1;
        keep[coll].push(r);
      });
    });
    var cands = [];
    try { if (isType("object", v.session)) cands.push(sessionRecord(v.session, isType("object", v.run) ? v.run : null)); } catch (e) { /* not a session */ }
    (Array.isArray(v.sessions) ? v.sessions : []).forEach(function (x) { cands.push(x); });
    var sids = dict();
    cands.forEach(function (x, i) {
      var b = emptyBundle([x]);
      var why = null;
      try { why = exportProblems(bundleText(b), b); } catch (e) { why = [String(e)]; }
      if (why.length || sids[x.id] !== undefined) { excluded.push("session " + (isType("object", x) && typeof x.id === "string" ? readable(x.id) : i) + ": " + (why.length ? why.slice(0, 2).join("; ") : "another session here has this id")); return; }
      sids[x.id] = 1;
      sessions.push(x);
    });
    var whole = emptyBundle(sessions);
    whole.notes = keep.notes;
    whole.inputs = keep.inputs;
    var got = exportParts(whole);
    RT.salvage = {parts: got.parts ? offered(got.parts, DATA.package.id + "-stored-records") : null, error: got.error || null, excluded: excluded};
    RT.showPart = dict();
    render();
    if (RT.salvage.parts) settled().then(function () { RT.salvage.parts.forEach(function (x) { download(x.name, x.text, "application/json"); }); });
  }
  // Every part with its own save control and, on request (at once for a single part), its JSON to copy.
  function partsBlock(prefix, parts) {
    var n = parts.length;
    return h("ol", {"class": "tp-parts", "id": prefix + "-list"}, parts.map(function (x, i) {
      var k = i + 1, key = prefix + ":" + k, shown = n === 1 || !!RT.showPart[key];
      return h("li", {"data-tp-part": key}, [
        (n === 1 ? "The bundle" : "Part " + k + " of " + n) + ": " + x.notes + " note(s), " + x.inputs + " answer(s)/proposal(s), " + x.bytes + " bytes. ",
        button(n === 1 ? "Save it again" : "Save part " + k, function () { download(x.name, x.text, "application/json"); },
               {"id": prefix + "-save-" + k, "data-tp-save": "1"}),
        n === 1 ? null : button(shown ? "Hide its JSON" : "Show its JSON to copy", function () { RT.showPart[key] = !shown; render(); },
                                {"id": prefix + "-show-" + k, "data-tp-show": "1", "aria-expanded": shown ? "true" : "false"}),
        shown ? h("textarea", {"readonly": true, "rows": 8, "aria-label": (n === 1 ? "Bundle" : "Part " + k) + " JSON",
                               "id": n === 1 && prefix === "tp-export" ? "tp-export-text" : prefix + "-text-" + k}, x.text) : null
      ]);
    }));
  }

  function bundleProblems(b, opts) {
    opts = opts || {};
    var p = [];
    if (!isType("object", b)) return ["a feedback bundle is a JSON object"];
    if (depth(b) > DATA.limits.depth) return ["nested deeper than " + DATA.limits.depth + " levels"];
    var bad = textProblem(b);   // the CLI's order: size, JSON, depth, then text, then the schema
    if (bad) return [bad];
    if (b.schema !== "tp-product-feedback") p.push("schema must be tp-product-feedback");
    if (b.schema_version !== 1) p.push("schema_version " + JSON.stringify(b.schema_version) + " is not supported");
    if (b.package !== DATA.package.id) p.push("the bundle is for package " + JSON.stringify(b.package) + ", not " + DATA.package.id);
    if (p.length) return p;
    var count = (Array.isArray(b.notes) ? b.notes.length : 0) + (Array.isArray(b.inputs) ? b.inputs.length : 0);
    if (!opts.local && count > DATA.limits.records) return [count + " records; at most " + DATA.limits.records];
    p = schemaIssues("feedback", b);
    if (p.length) return p;
    b.notes.forEach(function (n, i) {
      if (n.package !== DATA.package.id) p.push("notes." + i + ": belongs to package " + JSON.stringify(n.package));
      ["anchor", "disposition", "remap"].forEach(function (f) { if (own(n, f)) p.push("notes." + i + "." + f + ": set locally by the package owner, never imported"); });
    });
    b.inputs.forEach(function (x, i) {
      if (x.status !== "pending" || own(x, "reconciliation")) p.push("inputs." + i + ".status: an answer or proposal is pending until the package owner reconciles it");
      var t = record(x.target), kind = String(x.target).split(".")[0];
      if (!t || ["question", "rule", "decision"].indexOf(kind) < 0) p.push("inputs." + i + ".target: " + x.target + " is not a question, rule or decision of this package");
    });
    return p;
  }
  function importText(text) {
    var report = {ok: false, errors: [], added: {notes: [], inputs: [], sessions: []}, duplicates: [], conflicts: []};
    // both limits are reported together (the depth scan stops one level past the limit, so it is cheap at any size)
    if (utf8Length(text) > DATA.limits.bytes) report.errors.push("larger than " + DATA.limits.bytes + " bytes");
    if (textDepth(text, DATA.limits.depth) > DATA.limits.depth) report.errors.push("nested deeper than " + DATA.limits.depth + " levels");
    if (report.errors.length) return report;
    var b;
    try { b = JSON.parse(text); } catch (e) { report.errors.push("not JSON: " + e.message); return report; }
    report.errors = bundleProblems(b);
    if (report.errors.length) return report;
    report.errors = numberProblems(text);
    if (report.errors.length) { report.errors.push("nothing was imported"); return report; }
    var have = dict();
    S.notes.concat(S.inputs).forEach(function (r) { have[r.id] = canonical(r); });
    var sessions = dict();
    [sessionRecord(S.session, S.run)].concat(S.sessions).forEach(function (s) { sessions[s.id] = canonical(s); });
    var seen = dict(), adds = {notes: [], inputs: [], sessions: []};
    function classify(coll, r, pool) {
      var c = canonical(r);
      if (seen[coll + r.id] !== undefined) { if (seen[coll + r.id] !== c) report.conflicts.push(r.id + " (the bundle holds two different records with this id)"); return; }
      seen[coll + r.id] = c;
      if (pool[r.id] === undefined) adds[coll].push(r);
      else if (pool[r.id] === c) report.duplicates.push(r.id);
      else report.conflicts.push(r.id + " (this browser holds a different record with this id)");
    }
    b.notes.forEach(function (r) { classify("notes", r, have); });
    b.inputs.forEach(function (r) { classify("inputs", r, have); });
    (b.sessions || []).forEach(function (s) {
      if (s.id === S.session.id && sessions[s.id] !== canonical(s)) {
        report.conflicts.push(s.id + " (the session open in this browser has this id; its record differs)");
        return;
      }
      classify("sessions", s, sessions);
    });
    if (report.conflicts.length) { report.errors.push("conflicting ids; nothing was imported and the records here are unchanged"); return report; }
    S.notes = S.notes.concat(adds.notes);
    S.inputs = S.inputs.concat(adds.inputs);
    S.sessions = S.sessions.concat(adds.sessions);
    report.added = {notes: adds.notes.map(function (r) { return r.id; }), inputs: adds.inputs.map(function (r) { return r.id; }),
                    sessions: adds.sessions.map(function (r) { return r.id; })};
    report.ok = true;
    save();
    return report;
  }
  function importFile(file) {
    function done(report) {
      RT.importReport = report;
      render();
      document.dispatchEvent(new CustomEvent("tp-imported", {detail: report}));
    }
    if (file.size > DATA.limits.bytes) {
      // never parsed; the depth is still measured, chunk by chunk over the bytes (brackets and quotes are ASCII, so
      // they never occur inside a UTF-8 sequence), and the scan stops one level past the limit
      var st = {d: 0, max: 0, inStr: false, esc: false}, at = 0, CHUNK = 1 << 20;
      var errors = ["larger than " + DATA.limits.bytes + " bytes"];
      var finish = function () {
        if (st.max > DATA.limits.depth) errors.push("nested deeper than " + DATA.limits.depth + " levels");
        done({ok: false, errors: errors, added: {notes: [], inputs: [], sessions: []}, duplicates: [], conflicts: []});
      };
      var step = function () {
        if (at >= file.size || st.max > DATA.limits.depth) { finish(); return; }
        file.slice(at, at + CHUNK).arrayBuffer().then(function (buf) {
          bytesDepth(st, new Uint8Array(buf), DATA.limits.depth);
          at += CHUNK;
          step();
        }, finish);
      };
      step();
      return;
    }
    var fr = new FileReader();
    fr.onload = function () {
      var text;
      // ignoreBOM keeps a byte order mark, which JSON.parse refuses: the same file is refused here and by the CLI
      try { text = new TextDecoder("utf-8", {fatal: true, ignoreBOM: true}).decode(fr.result); }
      catch (e) { done({ok: false, errors: ["not UTF-8 text"], added: {notes: [], inputs: [], sessions: []}, duplicates: [], conflicts: []}); return; }
      done(importText(text));
    };
    fr.onerror = function () { done({ok: false, errors: ["the file could not be read"], added: {notes: [], inputs: [], sessions: []}, duplicates: [], conflicts: []}); };
    fr.readAsArrayBuffer(file);
  }
  function resume(sid) {
    var i = S.sessions.map(function (s) { return s.id; }).indexOf(sid);
    if (i < 0) return;
    var rec = S.sessions[i];
    if (rec.prototype && PROTO && !own(PROTO.seeds, rec.prototype.seed)) {
      RT.message = {kind: "error", text: "Session " + sid + " was recorded on seed " + rec.prototype.seed + ", which this prototype does not have."};
      render();
      return;
    }
    endTasks("in-progress");
    S.sessions.splice(i, 1);
    var cur = sessionRecord(S.session, S.run);
    if (allSessionsHas(cur)) S.sessions.push(cur);
    S.run = rec.prototype ? clone(rec.prototype) : S.run;
    var s = clone(rec);
    delete s.prototype;
    S.session = s;
    resumeTask();
    replay();
    save();
    render();
  }
  // The session's last task, if still in progress, is open again: its scenario, mode and assistance counts come from
  // the task record, and a guided task's step from the checkpoint's actions (startTask begins it with an empty run).
  function resumeTask() {
    var n = S.session.tasks.length - 1, t = S.session.tasks[n];
    if (!t || t.outcome !== "in-progress") return;
    var sc = record(t.scenario);
    if (!sc || !S.run || S.run.seed !== sc.seed) return;
    if (t.mode !== "guided") { S.ui.unguided = {scenario: t.scenario, task: n}; return; }
    var g = {scenario: t.scenario, task: n, step: 0, advancedAt: []};
    S.run.actions.forEach(function (act, idx) {
      var st = sc.actions[g.step];
      if (st && act && st.action === act.action && st.target === act.target) { g.advancedAt.push(idx); g.step += 1; }
    });
    S.ui.guide = g;
  }
  function allSessionsHas(s) {
    return s.tasks.length || s.visited.length || S.notes.some(function (n) { return n.review_session === s.id; }) ||
           (s.prototype && s.prototype.actions.length);
  }

  function summaryText() {
    var L = [], notes = S.notes.slice(), inputs = S.inputs.slice();
    function cmp(a, b) { return a < b ? -1 : a > b ? 1 : 0; }
    function quote(t) { return String(t).split(/\r?\n/).map(function (l) { return "    > " + l; }); }
    L.push("Review summary: " + DATA.package.title + " (" + DATA.package.id + ")");
    L.push("Build: revision " + DATA.build.revision + ", content " + DATA.build.content_fingerprint + ", source " + DATA.build.source_fingerprint);
    var reviewers = dict();
    notes.concat(inputs).forEach(function (r) { reviewers[r.reviewer] = true; });
    L.push("Reviewers (labels, not authenticated): " + (Object.keys(reviewers).sort().join(", ") || "none"));
    L.push("Notes: " + notes.length + ". Answers and proposals: " + inputs.length + " (pending until the package owner reconciles them).");
    L.push("");
    L.push("== Notes by journey and page ==");
    var groups = dict();
    notes.forEach(function (n) { var k = n.journey || "~"; (groups[k] = groups[k] || []).push(n); });
    Object.keys(groups).sort().forEach(function (k) {
      L.push("-- " + (k === "~" ? "Free exploration (no journey)" : k + ": " + title(k)) + " --");
      groups[k].sort(function (a, b) { return cmp(a.page || "", b.page || "") || cmp(a.target.id, b.target.id) || cmp(a.id, b.id); })
        .forEach(function (n) {
          L.push("  [" + (n.page || "-") + "] " + n.target.label + " (" + n.target.id + ")  " + n.id);
          L.push("    by " + n.reviewer + ", " + n.mode + (n.assistance !== "none" ? ", " + n.assistance : "") +
                 (n.scenario ? ", " + n.scenario + (n.step !== null ? " step " + (n.step + 1) : "") : "") +
                 ", build revision " + n.build.revision + ", anchor " + anchorOf(n) + (remapOf(n.id) ? " (remapped to " + remapOf(n.id).target + ", original " + n.target.id + ")" : "") + (n.category ? ", category " + n.category : ""));
          L.push.apply(L, quote(n.text));
        });
    });
    L.push("");
    L.push("== Answers and proposals by question, rule or decision ==");
    inputs.sort(function (a, b) { return cmp(a.target, b.target) || cmp(a.id, b.id); }).forEach(function (x) {
      L.push("  " + x.target + ": " + title(x.target) + "  " + x.id + " (" + x.type + ", " + x.status + ", by " + x.reviewer + ")");
      L.push.apply(L, quote(x.text));
    });
    if (!inputs.length) L.push("  none");
    L.push("");
    L.push("== Coverage (visiting a scenario is not approval) ==");
    var sessions = allSessions().slice().sort(function (a, b) { return cmp(a.id, b.id); });
    var visited = dict();
    sessions.forEach(function (s) { s.visited.forEach(function (v) { visited[v] = true; }); });
    var scen = (DATA.design.scenarios || []).map(function (s) { return s.id; }).sort();
    L.push("Visited: " + (scen.filter(function (s) { return visited[s]; }).join(", ") || "none"));
    L.push("Not visited: " + (scen.filter(function (s) { return !visited[s]; }).join(", ") || "none"));
    sessions.forEach(function (s) {
      s.tasks.forEach(function (t) {
        L.push("  session " + s.id + ": " + t.scenario + " " + t.mode + " " + t.outcome + ", help requested " + t.help_requested +
               ", assisted steps " + t.assisted_steps + (t.prior_guided ? ", after a guided task in this session" : ""));
      });
    });
    var un = notes.filter(function (n) { return anchorOf(n) === "unresolved"; }).sort(function (a, b) { return cmp(a.id, b.id); });
    L.push("");
    L.push("== Unresolved anchors (kept; the target is not in this build) ==");
    un.forEach(function (n) { L.push("  " + n.id + ": " + n.target.id + " (build revision " + n.build.revision + ")"); });
    if (!un.length) L.push("  none");
    return L.join("\n") + "\n";
  }

  // ------------------------------------------------------------------ views

  var VIEWS = [["overview", "Overview"], ["journeys", "Journeys"], ["experience", "Product experience"],
               ["questions", "Questions and decisions"], ["engineering", "Engineering"], ["feedback", "Feedback"]];

  function go(view) {
    RT.view = view;
    render();
    if (location.hash !== "#" + view) location.hash = "#" + view;   // a history entry: browser back returns to this view
  }
  function badge(text, kind) { return h("span", {"class": "tp-badge tp-" + (kind || "plain")}, text); }
  function card(id, heading, body, extra) {
    return tgt(h("article", {"class": "tp-card" + (extra ? " " + extra : "")}, [h("h4", {}, [heading, " ", h("code", {}, id)]), body]), id, heading);
  }
  function list(items) { return items && items.length ? h("ul", {}, items.map(function (x) { return h("li", {}, x); })) : h("p", {"class": "tp-muted"}, "None."); }

  function viewOverview() {
    var p = DATA.package, d = DATA.design;
    var slices = (d.slices || []).map(function (s) {
      var r = DATA.readiness[s.id] || {};
      return card(s.id, s.title, [h("p", {}, s.outcome), badge(r.effective || "?", r.effective),
        h("p", {}, "Includes:"), list(s.includes), h("p", {}, "Excludes:"), list(s.excludes),
        (r.blockers || []).length ? h("p", {"class": "tp-warn"}, "Blocked: " + r.blockers.map(function (b) { return b.reason; }).join("; ")) : null]);
    });
    var assumptions = (d.claims || []).filter(function (c) { return c.status === "assumption" || c.status === "unresolved"; });
    var claims = (d.claims || []).filter(function (c) { return assumptions.indexOf(c) < 0; });
    var src = byId("sources");
    return h("section", {}, [
      tgt(h("h2", {}, p.title), "view.overview", "Overview"),
      h("p", {}, p.objective),
      h("p", {"class": "tp-muted"}, "Owner: " + p.owner + ". Activity: " + p.activity + "."),
      h("p", {}, ["Next action: ", p.next_action.text, " (", p.next_action.owner, ", set ", p.next_action.set, ")"]),
      h("h3", {}, "Intended users"),
      h("div", {"class": "tp-grid"}, (d.actors || []).map(function (a) { return card(a.id, a.title, h("p", {}, a.description)); })),
      h("h3", {}, "Scope and review status (per slice)"),
      h("div", {"class": "tp-grid"}, slices),
      h("h3", {}, "Assumptions and unresolved claims"),
      h("div", {"class": "tp-grid"}, assumptions.map(function (c) {
        return card(c.id, c.status, [h("p", {}, c.statement), c.note ? h("p", {"class": "tp-muted"}, c.note) : null]);
      })),
      h("h3", {}, "Evidence"),
      h("div", {"class": "tp-grid"}, claims.map(function (c) {
        return card(c.id, c.status, [h("p", {}, c.statement), list((c.sources || []).map(function (sid) {
          var s = src[sid];
          return s ? tgt(h("span", {}, [s.title, ": ", s.locator.path, " (", s.anchor, ")", s.excerpt ? h("q", {}, s.excerpt) : null]), s.id, s.title) : sid;
        }))]);
      })),
      DATA.warnings.length ? h("div", {"class": "tp-panel tp-warn"}, [h("h3", {}, "Build warnings"), list(DATA.warnings)]) : null
    ]);
  }

  function scenarioButtons(sid) {
    return h("span", {"class": "tp-actions"}, [
      button("Guided walkthrough", function () { startTask(sid, "guided"); }, {"data-tp-start": "guided:" + sid}),
      button("Unguided task", function () { startTask(sid, "unguided"); }, {"data-tp-start": "unguided:" + sid})
    ]);
  }
  function viewJourneys() {
    var d = DATA.design;
    return h("section", {}, [
      tgt(h("h2", {}, "Journeys"), "view.journeys", "Journeys"),
      (d.journeys || []).map(function (j) {
        return card(j.id, j.title, [
          h("p", {}, ["Actor: ", title(j.actor), ". Goal: ", j.goal]),
          h("div", {"class": "tp-map"}, j.steps.map(function (st, i) {
            return h("div", {"class": "tp-map-col"}, [h("p", {"class": "tp-step"}, (i + 1) + ". " + st.text),
              (st.scenarios || []).map(function (sid) {
                var sc = record(sid);
                return sc ? tgt(h("div", {"class": "tp-chip"}, [h("strong", {}, sc.title), " ", badge(sc.kind, sc.kind),
                  h("p", {"class": "tp-muted"}, sc.goal), scenarioButtons(sid)]), sid, sc.title) : h("div", {"class": "tp-chip tp-warn"}, "Missing " + sid);
              }),
              (st.rules || []).map(function (rid) { return tgt(h("p", {"class": "tp-rule"}, "Rule: " + title(rid)), rid, title(rid)); })]);
          })),
          h("h4", {}, "Release slices"),
          list((j.slices || []).map(function (sid) {
            var r = DATA.readiness[sid] || {};
            return tgt(h("span", {}, [title(sid), " ", badge(r.effective || "?", r.effective)]), sid, title(sid));
          }))
        ]);
      })
    ]);
  }

  function viewExperience() {
    var wrap = h("section", {"class": "tp-exp"});
    append(wrap, tgt(h("h2", {}, "Product experience"), "view.experience", "Product experience"));
    if (!DATA.adapter) { append(wrap, h("p", {"class": "tp-panel"}, "This package has no prototype adapter (prototype/adapter.json), so there is no interactive prototype in this build.")); return wrap; }
    if (!PROTO) { append(wrap, h("p", {"class": "tp-panel tp-error", "role": "alert"}, "The prototype script did not register (" + (LOAD_ERRORS.join("; ") || "no TPReview.registerPrototype call") + ").")); return wrap; }
    var seeds = h("select", {"aria-label": "Seed", "id": "tp-seed"}, DATA.adapter.seeds.map(function (s) {
      var o = h("option", {"value": s.id}, s.label + " (" + s.id + ")");
      if (S.run && s.id === S.run.seed) o.selected = true;
      return o;
    }));
    seeds.addEventListener("change", function () { reset(seeds.value); });
    var a = activeTask();
    append(wrap, h("div", {"class": "tp-panel tp-controls"}, [
      h("label", {}, ["Seed ", seeds]),
      button("Reset to seed", function () { reset(); }, {"id": "tp-reset"}),
      button("Back one action", back, {"id": "tp-back", "disabled": !(S.run && S.run.actions.length)}),
      h("span", {"class": "tp-muted", "id": "tp-checkpoint"}, "Checkpoint " + (S.run ? S.run.seed + "@" + S.run.actions.length : "-") +
        " (" + RT.events.length + " simulated event" + (RT.events.length === 1 ? "" : "s") + ")"),
      h("span", {}, ["Mode: ", badge(a.mode, a.mode)])
    ]));
    append(wrap, taskPanel());
    if (RT.message) append(wrap, h("p", {"class": "tp-panel tp-" + RT.message.kind, "role": "status", "id": "tp-message"}, RT.message.text));
    var stage = h("div", {"id": "tp-proto", "class": "tp-proto"});
    try {
      append(stage, PROTO.render(protoUi(), clone(RT.state)));
    } catch (e) {
      append(stage, h("p", {"class": "tp-error", "role": "alert"}, "The prototype could not draw this page: " + e.message));
    }
    append(wrap, stage);
    if (RT.events.length) {
      var ev = RT.events[RT.events.length - 1];
      append(wrap, h("p", {"class": "tp-muted"}, ["Last simulated event: ", ev.operation, " ", ev.status, " (", ev.label, "). ",
        button("See it in Engineering", function () { RT.selectedEvent = ev.seq; go("engineering"); })]));
    }
    return wrap;
  }
  function protoUi() {
    return {
      h: h,
      target: function (el, id, label, onActivate) {
        tgt(el, id, label);
        if (onActivate) {
          el.addEventListener("click", function () { onActivate(); });
          if (!/^(BUTTON|INPUT|SELECT|TEXTAREA|A)$/.test(el.tagName)) {
            el.addEventListener("keydown", function (e) { if (e.key === "Enter" && !RT.noteMode) onActivate(); });
          }
        }
        return el;
      },
      act: function (action, target, input) { return function () { dispatch(action, target, input); }; }
    };
  }
  function taskPanel() {
    var g = S.ui.guide, u = S.ui.unguided;
    RT.stepError = null;
    if (g) {
      var sc = record(g.scenario), t = S.session.tasks[g.task];
      if (!sc) return h("p", {"class": "tp-panel tp-error", "role": "alert"}, "Scenario " + g.scenario + " is not in this build.");
      var st = sc.actions[g.step];
      return tgt(h("div", {"class": "tp-panel tp-guide", "id": "tp-guide"}, [
        h("h3", {}, ["Guided: ", sc.title]),
        h("p", {}, sc.goal),
        h("ol", {}, sc.actions.map(function (x, i) {
          return h("li", {"class": i < g.step ? "tp-done" : i === g.step ? "tp-now" : ""}, describe(x));
        })),
        st ? h("p", {"id": "tp-step"}, ["Step " + (g.step + 1) + " of " + sc.actions.length + ": ", describe(st)])
           : h("p", {"id": "tp-step"}, "Steps done. Outcome: " + (t ? t.outcome : "?") + (t && t.assisted_steps ? " (with " + t.assisted_steps + " assisted step" + (t.assisted_steps > 1 ? "s" : "") + ": not unassisted success)" : "")),
        h("p", {"class": "tp-muted"}, "Expected: " + sc.expected.join(" ")),
        h("p", {"class": "tp-error", "role": "alert", "id": "tp-step-error", "hidden": true}, ""),
        h("div", {"class": "tp-actions"}, [
          st ? button("Do this step for me (recorded as assisted)", assist, {"id": "tp-assist"}) : null,
          button("Restart", function () { reset(); }, {"id": "tp-restart"}),
          button("End walkthrough", function () { endTasks("abandoned"); save(); render(); }),
          t && t.outcome === "in-progress" && !st ? button("Mark done (self-reported)", function () { t.outcome = "self-reported"; t.ended = now(); save(); render(); }) : null
        ])
      ]), sc.id, sc.title);
    }
    if (u) {
      var su = record(u.scenario), tu = S.session.tasks[u.task];
      if (!su) return null;
      return tgt(h("div", {"class": "tp-panel tp-guide", "id": "tp-task"}, [
        h("h3", {}, "Unguided task"),
        h("p", {}, ["Goal: ", su.goal]),
        h("p", {"id": "tp-task-outcome"}, "Status: " + tu.outcome + ". Help requested: " + tu.help_requested + "."),
        RT.hint ? h("div", {"class": "tp-hint"}, [h("p", {}, "Hint (counted as help):"), h("ol", {}, su.actions.map(function (x) { return h("li", {}, describe(x)); }))]) : null,
        h("div", {"class": "tp-actions"}, tu.outcome === "in-progress" ? [
          button("I need help", function () { tu.help_requested += 1; RT.hint = true; save(); render(); }, {"id": "tp-help"}),
          button("I'm done", function () {
            var met = condition(su.id);
            if (met === null) { tu.outcome = "self-reported"; tu.ended = now(); }
            else if (met) { tu.outcome = "completed"; tu.ended = now(); }
            else RT.message = {kind: "warn", text: "Not done yet: the goal is not observed in the prototype."};
            save(); render();
          }),
          button("Give up", function () { tu.outcome = "abandoned"; tu.ended = now(); save(); render(); })
        ] : [button("Close task", function () { S.ui.unguided = null; RT.hint = false; save(); render(); })])
      ]), su.id, su.title);
    }
    return h("div", {"class": "tp-panel"}, [h("p", {}, "Free exploration. Start a guided walkthrough or an unguided task from Journeys, or explore and add notes."),
      button("Start a new review session (for a first-time, unguided observation)", newSessionClick, {"id": "tp-new-session"})]);
  }
  function newSessionClick() {
    endTasks("abandoned");
    var cur = sessionRecord(S.session, S.run);
    if (allSessionsHas(cur)) S.sessions.push(cur);
    S.session = newSession(null);
    var seed = DATA.adapter ? DATA.adapter.seeds[0].id : null;
    S.run = seed ? {seed: seed, actions: []} : null;
    replay();
    save();
    render();
  }
  function describe(x) {
    var a = adapterAction(x.action);
    var t = (REG.exact[x.target] !== undefined && REG.exact[x.target]) || x.target;
    var input = x.input && x.input.value !== undefined ? " with \"" + x.input.value + "\"" : x.input && x.input.to ? " to " + x.input.to : "";
    return (a ? a.label : x.action) + ": " + t + input;
  }
  function highlightStep() {
    var g = S.ui.guide;
    if (RT.view !== "experience" || !g) return;
    var sc = record(g.scenario), st = sc && sc.actions[g.step], box = document.getElementById("tp-step-error");
    if (!st || !box) return;
    var stage = document.getElementById("tp-proto");
    var el = stage && Array.prototype.filter.call(stage.querySelectorAll("[data-tp-target]"), function (e) {
      return e.getAttribute("data-tp-target") === st.target;
    })[0];
    if (el) {
      el.classList.add("tp-highlight");
      if (el.scrollIntoView) el.scrollIntoView({block: "nearest"});
      box.hidden = true;
    } else {
      RT.stepError = st.target;
      box.textContent = "Step " + (g.step + 1) + " needs '" + st.target + "' (" + st.action + "), which is not on the current page (" +
        (RT.state ? RT.state.page : "-") + ")" + (knownTarget(st.target) ? "." : ", and the prototype does not declare it.") +
        " Use Back or Restart; if the page is right, the scenario names a target this prototype does not show.";
      box.hidden = false;
    }
  }

  function inputBox(type, targetId, label) {
    var ta = h("textarea", {"rows": 2, "aria-label": label, "data-tp-input-for": targetId});
    var out = h("span", {"class": "tp-muted"}, "");
    return h("div", {"class": "tp-answer"}, [ta, button(label, function () {
      var err = addInput(type, targetId, ta.value);
      if (err) out.textContent = err;
    }, {"data-tp-answer": targetId}), out]);
  }
  function viewQuestions() {
    var d = DATA.design, f = RT.questionFilter;
    var filters = h("div", {"class": "tp-actions", "role": "group", "aria-label": "Filter"}, [["open", "Unanswered"], ["all", "All questions"]].map(function (x) {
      return button(x[1], function () { RT.questionFilter = x[0]; render(); }, {"aria-pressed": f === x[0] ? "true" : "false"});
    }));
    var qs = (d.questions || []).filter(function (q) { return f === "all" || q.status === "open"; });
    var mine = function (id) { return S.inputs.filter(function (x) { return x.target === id; }); };
    return h("section", {}, [
      tgt(h("h2", {}, "Questions and decisions"), "view.questions", "Questions and decisions"),
      h("p", {"class": "tp-muted"}, "Your answers and proposals are suggestions. They stay pending until the package owner records a decision; a label or wording that claims approval changes nothing."),
      filters,
      qs.map(function (q) {
        return card(q.id, q.title, [
          h("p", {}, [h("strong", {}, "Decision needed: "), q.decision_needed]),
          h("p", {}, [h("strong", {}, "Why it matters: "), q.why_it_matters]),
          h("p", {}, "Owner: " + q.owner + ". Status: " + q.status + (q.blocking ? ". Blocks " + q.affected_slices.join(", ") : "")),
          list((q.options || []).map(function (o) { return h("span", {}, [o.choice, h("span", {"class": "tp-muted"}, " Tradeoff: " + o.tradeoff)]); })),
          q.recommendation ? h("p", {}, [h("strong", {}, "Recommended: "), q.recommendation]) : null,
          q.decision ? h("p", {}, ["Recorded decision: ", title(q.decision)]) : null,
          mine(q.id).map(function (x) { return h("p", {"class": "tp-input"}, [badge(x.status, "pending"), " ", x.reviewer, ": ", x.text]); }),
          q.status === "open" ? inputBox("answer", q.id, "Answer (pending)") : null
        ]);
      }),
      h("h3", {}, "Decisions"),
      (d.decisions || []).map(function (x) {
        return card(x.id, x.title, [h("p", {}, x.choice), h("p", {"class": "tp-muted"}, x.rationale),
          h("p", {}, ["Status: ", badge(x.status, x.status), x.acceptance ? " (accepted locally by " + x.acceptance.by + " on " + x.acceptance.date + ")" : ""]),
          mine(x.id).map(function (i) { return h("p", {"class": "tp-input"}, [badge(i.status, "pending"), " ", i.reviewer, ": ", i.text]); }),
          x.status === "proposed" ? inputBox("proposal", x.id, "Propose a change (pending)") : null]);
      }),
      h("h3", {}, "Rules"),
      (d.rules || []).map(function (r) {
        return card(r.id, r.title, [h("p", {}, r.statement), r.explanation ? h("p", {"class": "tp-muted"}, r.explanation) : null,
          h("p", {}, ["Status: ", badge(r.status, r.status)]),
          list((r.examples || []).map(function (e) { return e.situation + " -> " + e.outcome; })),
          mine(r.id).map(function (i) { return h("p", {"class": "tp-input"}, [badge(i.status, "pending"), " ", i.reviewer, ": ", i.text]); }),
          r.status === "proposed" ? inputBox("proposal", r.id, "Propose a choice (pending)") : null]);
      })
    ]);
  }

  function viewEngineering() {
    var d = DATA.design, sel = RT.events.filter(function (e) { return e.seq === RT.selectedEvent; })[0] || null;
    var events = RT.events.map(function (e) {
      var row = button([String(e.seq), ". ", e.operation, " ", e.status, " - ", e.label], function () { RT.selectedEvent = e.seq; render(); },
                       {"class": "tp-event" + (sel === e ? " tp-selected" : ""), "data-tp-event": String(e.seq), "aria-pressed": sel === e ? "true" : "false"});
      return tgt(row, e.contract || "view.engineering", "Event " + e.seq + ": " + e.operation, "Simulated event " + e.seq + " (" + e.action + " on " + e.target + "): " + e.operation + " " + e.status + ". " + e.consequence);
    });
    var detail = sel ? (function () {
      var c = sel.contract ? record(sel.contract) : null;
      var scen = (d.scenarios || []).filter(function (s) { return c && s.contracts.indexOf(c.id) >= 0; });
      var slices = (d.slices || []).filter(function (s) { return c && s.contracts.indexOf(c.id) >= 0; });
      return h("div", {"class": "tp-panel", "id": "tp-event-detail"}, [
        h("h3", {}, ["Event ", String(sel.seq), ": ", sel.operation]),
        h("p", {}, [badge(sel.label, c ? c.status : "local"), " Action ", sel.action, " on ", sel.target, " (action ", String(sel.index + 1), ")"]),
        h("p", {}, "Request: "), h("pre", {}, sel.request),
        h("p", {}, "Response (" + sel.status + "): "), h("pre", {}, sel.response),
        h("p", {}, "Records affected:"), list(sel.records),
        h("p", {}, ["UI consequence: ", sel.consequence]),
        c ? h("div", {}, [h("p", {}, ["Contract: ", c.title, " (", c.status, ")"]),
                          h("p", {}, "Rules:"), list((c.rules || []).map(function (r) { return tgt(h("span", {}, title(r)), r, title(r)); })),
                          h("p", {}, "Scenarios:"), list(scen.map(function (s) { return s.title + " (" + s.id + ")"; })),
                          h("p", {}, "Slices:"), list(slices.map(function (s) { return s.title + " (" + (DATA.readiness[s.id] || {}).effective + ")"; }))])
          : h("p", {"class": "tp-muted"}, "No contract covers this action yet.")
      ]);
    })() : h("p", {"class": "tp-muted"}, "Select an event to see its request, response, affected records and related rules.");
    var scenSel = h("select", {"aria-label": "Scenario"}, [h("option", {"value": ""}, "Choose a scenario...")].concat((d.scenarios || []).map(function (s) {
      var o = h("option", {"value": s.id}, s.title);
      if (s.id === RT.engScenario) o.selected = true;
      return o;
    })));
    scenSel.addEventListener("change", function () { RT.engScenario = scenSel.value || null; render(); });
    var es = RT.engScenario && record(RT.engScenario);
    return h("section", {}, [
      tgt(h("h2", {}, "Engineering"), "view.engineering", "Engineering"),
      h("p", {"class": "tp-muted"}, "Everything here is simulated: no request leaves this page. Proposed contracts need engineering validation."),
      h("h3", {}, "Simulated events in this session"),
      events.length ? h("div", {"class": "tp-events", "id": "tp-events"}, events) : h("p", {"class": "tp-muted"}, "No events yet: act in the Product experience."),
      detail,
      h("h3", {}, "Scenario to calls, rules and records"),
      scenSel,
      es ? h("div", {"class": "tp-panel"}, [list(es.actions.map(function (x) {
        var a = adapterAction(x.action);
        return describe(x) + (a && a.contract ? " -> " + a.operation + " (" + a.contract + ")" : "");
      })), h("p", {}, "Rules: " + es.rules.join(", ") + ". Contracts: " + (es.contracts.join(", ") || "none") + ".")]) : null,
      h("h3", {}, "Contracts"),
      (d.contracts || []).map(function (c) {
        return card(c.id, c.title, [h("p", {}, [badge(c.kind, c.kind), " ", badge(c.status === "existing-verified" ? "existing verified" : "proposed (simulated)", c.status)]),
          h("p", {}, c.summary),
          (c.operations || []).map(function (o) {
            return h("div", {"class": "tp-op"}, [h("strong", {}, o.name), h("pre", {}, o.request + "\n" + o.response),
              list((o.failures || []).map(function (f) { return f.condition + " -> " + f.response; }))]);
          }),
          (c.entities || []).map(function (e) { return h("div", {"class": "tp-op"}, [h("strong", {}, e.name), " owned by ", e.owner, list(e.changes)]); }),
          c.compatibility ? h("p", {"class": "tp-muted"}, c.compatibility) : null]);
      }),
      h("h3", {}, "Dependencies (slice -> scenarios, rules, decisions, contracts)"),
      (d.slices || []).map(function (s) {
        var r = DATA.readiness[s.id] || {};
        return card(s.id, s.title, [badge(r.effective || "?", r.effective),
          h("p", {}, "Scenarios: " + s.scenarios.join(", ")), h("p", {}, "Rules: " + s.rules.join(", ")),
          h("p", {}, "Decisions: " + (s.decisions.join(", ") || "none")), h("p", {}, "Contracts: " + (s.contracts.join(", ") || "none")),
          h("p", {}, "Depends on: " + (s.depends_on.join(", ") || "nothing"))]);
      })
    ]);
  }

  function viewFeedback() {
    var anchors = {current: [], "changed-context": [], remapped: [], unresolved: []};
    S.notes.forEach(function (n) { anchors[anchorOf(n)].push(n); });
    var fileIn = h("input", {"type": "file", "accept": ".json,application/json", "id": "tp-import-file", "aria-label": "Import a feedback bundle"});
    fileIn.addEventListener("change", function () { if (fileIn.files && fileIn.files[0]) importFile(fileIn.files[0]); });
    var rep = RT.importReport;
    return h("section", {}, [
      tgt(h("h2", {}, "Feedback"), "view.feedback", "Feedback"),
      h("p", {}, ["Session ", h("code", {}, S.session.id), " by ", S.session.reviewer || "(no label yet)", ". Notes here: " + S.notes.length +
        ", answers and proposals: " + S.inputs.length + "."]),
      h("div", {"class": "tp-actions"}, [
        button("Export feedback bundle (.json)", doExport, {"id": "tp-export"}),
        button("Summarize feedback", function () { RT.summary = summaryText(); render(); }, {"id": "tp-summarize"}),
        RT.summary ? button("Download summary (.txt)", function () { download(DATA.package.id + "-summary.txt", RT.summary, "text/plain"); }) : null,
        h("label", {}, ["Import a bundle: ", fileIn])
      ]),
      rep ? h("div", {"class": "tp-panel " + (rep.ok ? "" : "tp-error"), "id": "tp-import-report", "role": "status"}, rep.ok ? [
        "Imported ", String(rep.added.notes.length), " note(s), ", String(rep.added.inputs.length), " answer(s)/proposal(s), ",
        String(rep.added.sessions.length), " session(s); ", String(rep.duplicates.length), " already here (unchanged)."
      ] : ["Nothing was imported: ", list(rep.errors.concat(rep.conflicts))]) : null,
      RT.summary ? h("textarea", {"readonly": true, "rows": 14, "id": "tp-summary", "aria-label": "Feedback summary"}, RT.summary) : null,
      RT.exportError ? h("p", {"class": "tp-panel tp-error", "role": "alert", "id": "tp-export-error"}, RT.exportError) : null,
      RT.exportParts ? h("div", {"class": "tp-panel", "id": "tp-export-parts", "role": "status"}, [
        h("p", {}, RT.exportParts.length > 1
          ? "The feedback was larger than one bundle allows, so it was exported as " + RT.exportParts.length + " bundles (part 1 of " +
            RT.exportParts.length + " and so on). Import every part; each is complete on its own. If a download was blocked, save that part again or copy its JSON."
          : "Exported as one bundle. If the download was blocked, save it again or copy its JSON."),
        partsBlock("tp-export", RT.exportParts)]) : null,
      h("h3", {}, "Notes"),
      S.notes.length ? h("div", {"id": "tp-notes"}, S.notes.map(noteCard)) : h("p", {"class": "tp-muted"}, "No notes yet. Use Add note (or press N) and pick any highlighted element."),
      h("h3", {}, "Unresolved anchors"),
      h("p", {"class": "tp-muted"}, "Notes whose target, journey or scenario is not in this build. They are kept, never dropped; the package owner remaps them."),
      anchors.unresolved.length ? h("ul", {"id": "tp-unresolved"}, anchors.unresolved.map(function (n) { return h("li", {}, n.id + ": " + n.target.id + " (" + n.target.label + "), build revision " + n.build.revision); }))
                                : h("p", {"class": "tp-muted", "id": "tp-unresolved"}, "None."),
      h("h3", {}, "Remapped by the package owner"),
      (DATA.remaps || []).length ? h("ul", {"id": "tp-remaps"}, DATA.remaps.map(function (r) {
        return h("li", {}, [r.note, ": now on ", h("code", {}, r.target), " (" + title(r.target) + "), originally on ", h("code", {}, r.original),
                            ". Reason: ", r.reason]);
      })) : h("p", {"class": "tp-muted", "id": "tp-remaps"}, "None."),
      h("h3", {}, "Answers and proposals"),
      S.inputs.length ? list(S.inputs.map(function (x) { return h("span", {"class": "tp-input"}, [badge(x.status, "pending"), " ", x.id, " on ", x.target, " by ", x.reviewer, ": ", x.text]); }))
                      : h("p", {"class": "tp-muted"}, "None."),
      h("h3", {}, "Coverage (visiting is not approval)"),
      list((DATA.design.scenarios || []).map(function (s) {
        var tasks = allSessions().reduce(function (acc, x) { return acc.concat(x.tasks.filter(function (t) { return t.scenario === s.id; })); }, []);
        return s.title + ": " + (tasks.length ? tasks.map(function (t) { return t.mode + " " + t.outcome + (t.help_requested ? ", help " + t.help_requested : "") + (t.assisted_steps ? ", assisted " + t.assisted_steps : ""); }).join("; ") : "not visited");
      })),
      h("h3", {}, "Sessions in this browser"),
      list(S.sessions.map(function (s) {
        return h("span", {}, [s.id + " by " + (s.reviewer || "(no label)") + ", " + s.tasks.length + " task(s) ",
          button("Resume this session", function () { resume(s.id); }, {"data-tp-resume": s.id})]);
      }))
    ]);
  }
  function noteCard(n) {
    var a = anchorOf(n);
    return h("article", {"class": "tp-note", "data-note-id": n.id}, [
      h("p", {"class": "tp-note-text"}, n.text),
      h("p", {"class": "tp-muted"}, [n.id, " by ", n.reviewer, " on ", n.target.label, " (", n.target.id, "), ", n.page || "-",
        n.scenario ? ", " + n.scenario + (n.step !== null ? " step " + (n.step + 1) : "") : "", ", ", n.mode,
        n.assistance !== "none" ? " (" + n.assistance + ")" : "", n.category ? ", " + n.category : ""]),
      h("p", {"class": "tp-muted"}, ["Context: ", n.target.context, ". Checkpoint ", n.checkpoint || "-", ". ",
        badge(a === "current" ? "this build" : a === "changed-context" ? "from build revision " + n.build.revision + ": context may have changed" :
              a === "remapped" ? "remapped to " + remapOf(n.id).target + " (original " + n.target.id + ")" : "unresolved anchor", a)])
    ]);
  }

  // ------------------------------------------------------------------ chrome: toolbar, note panel, render

  function toolbar() {
    var label = h("input", {"type": "text", "value": S.session.reviewer || "", "placeholder": "Your reviewer label", "aria-label": "Reviewer label", "id": "tp-reviewer"});
    label.addEventListener("change", function () { S.session.reviewer = label.value.trim().slice(0, 200) || null; save(); render(); });
    var unsaved = S.exported === exportDigest() ? 0 : S.notes.length + S.inputs.length;
    return h("header", {"class": "tp-toolbar"}, [
      h("div", {"class": "tp-brand"}, [h("strong", {}, DATA.package.title), " ", h("span", {"class": "tp-muted", "id": "tp-build"},
        "revision " + DATA.build.revision + ", source " + short(DATA.build.source_fingerprint))]),
      h("nav", {"class": "tp-tabs"}, VIEWS.map(function (v) {
        return button(v[1], function () { go(v[0]); }, {"aria-current": RT.view === v[0] ? "page" : null, "data-tp-view": v[0]});
      })),
      h("div", {"class": "tp-tools"}, [label,
        button(RT.noteMode ? "Cancel note (Esc)" : "Add note (N)", function () { RT.noteMode = !RT.noteMode; closeNote(); render(); },
               {"id": "tp-note-mode", "aria-pressed": RT.noteMode ? "true" : "false"})]),
      h("div", {"class": "tp-status " + (RT.storage.ok ? "" : "tp-error"), "id": "tp-storage", "role": "status"},
        RT.storage.ok ? "Notes are kept in this browser only (best effort, not shared). Export to keep or share them." +
                        (unsaved ? " Not exported yet: " + unsaved + " record(s)." : "")
                      : "Browser storage is unavailable (" + RT.storage.reason + "). Your notes exist only in this tab: export before you close it." +
                        (unsaved ? " Not exported yet: " + unsaved + " record(s)." : "")),
      RT.storageNotice ? h("div", {"class": "tp-status tp-warn", "role": "alert", "id": "tp-storage-notice"}, [RT.storageNotice,
        RT.savedRefused ? h("div", {"class": "tp-actions"}, [
          button("Download the saved data as it is (.json)", function () { download(DATA.package.id + "-saved-state.json", RT.savedRefused.raw, "application/json"); },
                 {"id": "tp-saved-download"}),
          isType("object", RT.savedRefused.value) ? button("Export its importable notes and answers", salvage, {"id": "tp-saved-export"}) : null
        ]) : null,
        RT.salvage && RT.salvage.error ? h("p", {"class": "tp-error", "id": "tp-saved-error"}, "Export refused: " + RT.salvage.error + ".") : null,
        RT.salvage && RT.salvage.parts ? h("div", {"id": "tp-saved-parts"}, [
          h("p", {}, "The saved records that are importable on their own, as " + RT.salvage.parts.length + " bundle(s); import every part."),
          partsBlock("tp-saved", RT.salvage.parts)]) : null,
        RT.salvage ? h("div", {"id": "tp-saved-excluded"}, RT.salvage.excluded.length
          ? ["Left out (kept in the saved data as it is): ", list(RT.salvage.excluded.slice(0, 50).concat(RT.salvage.excluded.length > 50 ? ["and " + (RT.salvage.excluded.length - 50) + " more"] : []))]
          : "Nothing was left out.") : null]) : null,
      RT.noteMode ? notePicker() : null
    ]);
  }
  function notePicker() {
    var els = Array.prototype.slice.call(document.querySelectorAll("#tp-main [data-tp-target]"));
    var sel = h("select", {"aria-label": "Target for the note", "id": "tp-note-pick"}, els.map(function (e, i) {
      return h("option", {"value": String(i)}, e.getAttribute("data-tp-label") + " (" + e.getAttribute("data-tp-target") + ")");
    }));
    return h("div", {"class": "tp-status tp-note-hint"}, ["Note mode: click, tab to or right-click any outlined element (its action will not run), or choose it here: ",
      sel, button("Note on it", function () { var e = els[+sel.value]; if (e) beginNote(e); })]);
  }
  function notePanel() {
    var p = RT.pending;
    if (!p) return null;
    var draft = p.draft || (p.draft = {text: "", category: ""});   // transient, like the rest of RT.pending
    var ta = h("textarea", {"id": "tp-note-text", "rows": 5, "aria-label": "Note text"});
    var cat = h("select", {"id": "tp-note-category", "aria-label": "Category"}, [["", "No category"], ["question", "Question"], ["confusing", "Confusing"],
      ["problem", "Problem"], ["idea", "Idea"], ["agree", "Looks right"]].map(function (c) { return h("option", {"value": c[0]}, c[1]); }));
    ta.value = draft.text;
    cat.value = draft.category;
    ta.addEventListener("input", function () { draft.text = ta.value; });
    cat.addEventListener("change", function () { draft.category = cat.value; });
    var err = h("p", {"class": "tp-error", "role": "alert"}, "");
    return h("aside", {"class": "tp-note-panel", "role": "dialog", "aria-label": "Add a note", "id": "tp-note-panel"}, [
      h("h3", {}, "Note on: " + p.target.label),
      h("p", {"class": "tp-muted"}, p.target.context),
      h("p", {"class": "tp-muted"}, "Captured at " + (p.checkpoint || "-") + " in " + p.mode + " mode; later actions do not change it."),
      ta, h("label", {}, ["Category ", cat]), err,
      h("div", {"class": "tp-actions"}, [
        button("Save note", function () { var e = saveNote(ta.value, cat.value); if (e) err.textContent = e; }, {"id": "tp-note-save"}),
        button("Cancel", function () { closeNote(); render(); })
      ])
    ]);
  }
  // Drawing is deferred: a handler changes state and asks for a render; the page is redrawn once, after the event
  // that caused it. While a pointer button is down it waits for the release, so a field's change event (fired by the
  // mousedown that blurs it) does not replace the button under the pointer and lose its click.
  var paintQueued = false, settledWaiters = [];
  function render() {
    RT.renderPending = true;
    if (RT.holding || paintQueued) return;
    paintQueued = true;
    setTimeout(flush, 0);
  }
  function flush() {
    paintQueued = false;
    if (!RT.holding && RT.renderPending) paint();
  }
  function release() {
    RT.holding = false;
    if (RT.renderPending) render();
  }
  function focusKey(el) {
    if (!el || el === document.body) return null;
    var names = ["id", "data-tp-view", "data-tp-start", "data-tp-answer", "data-tp-event", "data-tp-resume", "data-tp-target"];
    for (var i = 0; i < names.length; i++) if (el.getAttribute(names[i])) return [names[i], el.getAttribute(names[i])];
    return null;
  }
  function paint() {
    RT.renderPending = false;
    var root = document.getElementById("tp-root");
    var active = focusKey(document.activeElement);
    var pendingText = document.getElementById("tp-note-text"), pendingCat = document.getElementById("tp-note-category");
    if (RT.pending && RT.pending.draft) {   // whatever the fields hold now, even if no event reported it yet
      if (pendingText) RT.pending.draft.text = pendingText.value;
      if (pendingCat) RT.pending.draft.category = pendingCat.value;
    }
    var selStart = pendingText ? pendingText.selectionStart : null, selEnd = pendingText ? pendingText.selectionEnd : null;
    clear(root);
    RT.undeclared = dict();
    var main = h("main", {"id": "tp-main", "class": RT.noteMode ? "tp-note-mode" : ""});
    var body;
    try {
      body = {overview: viewOverview, journeys: viewJourneys, experience: viewExperience, questions: viewQuestions,
              engineering: viewEngineering, feedback: viewFeedback}[RT.view]();
    } catch (e) {
      body = h("p", {"class": "tp-error", "role": "alert"}, "This view failed to draw: " + e.message);
    }
    append(main, body);
    var undeclared = Object.keys(RT.undeclared);
    if (undeclared.length) append(main, h("p", {"class": "tp-panel tp-error", "role": "alert"}, "Undeclared review targets on this page (notes on them cannot be anchored): " + undeclared.join(", ")));
    append(root, main);
    root.insertBefore(toolbar(), main);  // after main exists, so the note picker can list its targets
    var panel = notePanel();
    if (panel) append(root, panel);
    var newText = document.getElementById("tp-note-text");
    if (newText && selStart !== null) { try { newText.setSelectionRange(selStart, selEnd); } catch (e) { /* not focusable yet */ } }
    if (RT.noteMode) Array.prototype.forEach.call(main.querySelectorAll("[data-tp-target]"), function (e) { if (!e.hasAttribute("tabindex")) e.setAttribute("tabindex", "0"); });
    highlightStep();
    var ta = document.getElementById("tp-note-text");
    if (RT.focusNote && ta) { RT.focusNote = false; ta.focus(); }
    else if (active) {
      var again = Array.prototype.filter.call(root.querySelectorAll("[" + active[0] + "]"), function (e) { return e.getAttribute(active[0]) === active[1]; })[0];
      if (again && again.focus) again.focus({preventScroll: true});
    }
    RT.paints += 1;
    var w = settledWaiters;
    settledWaiters = [];
    w.forEach(function (f) { f(); });
  }
  function settled() {   // resolves once every requested render has been drawn
    return new Promise(function (resolve) {
      if (!RT.renderPending && !paintQueued) resolve(); else settledWaiters.push(resolve);
    });
  }

  // ------------------------------------------------------------------ note-mode interception (capture phase)

  function targetOf(e) {
    var t = e.target;
    return t && t.closest ? t.closest("#tp-main [data-tp-target]") : null;
  }
  function inChrome(e) { return e.target && e.target.closest && e.target.closest(".tp-toolbar, .tp-note-panel"); }
  function stop(e) { e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation(); }
  function installInterception() {
    document.addEventListener("pointerdown", function () { RT.holding = true; }, true);
    // pointerup, mouseup and click are dispatched in one task: the release runs after the click's handlers
    ["pointerup", "pointercancel", "blur"].forEach(function (t) { window.addEventListener(t, function () { setTimeout(release, 0); }); });
    ["click", "mousedown", "change", "input", "submit"].forEach(function (type) {
      document.addEventListener(type, function (e) {
        if (!RT.noteMode || inChrome(e)) return;
        var el = targetOf(e);
        stop(e);
        if (type === "click" && el) beginNote(el);
      }, true);
    });
    document.addEventListener("contextmenu", function (e) {
      var el = targetOf(e);
      if (!el || inChrome(e)) return;
      stop(e);
      beginNote(el);
    }, true);
    document.addEventListener("keydown", function (e) {
      var typing = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target && e.target.tagName) || "");
      if (e.key === "Escape" && (RT.noteMode || RT.pending)) { RT.noteMode = false; closeNote(); render(); return; }
      if (RT.noteMode && !inChrome(e) && e.key !== "Tab") {
        var el = targetOf(e);   // in note mode a key never reaches the prototype: Enter or Space picks the target
        if (el || (e.target && e.target.closest && e.target.closest("#tp-main"))) stop(e);
        if (el && (e.key === "Enter" || e.key === " ")) beginNote(el);
        if (el || e.key === "Enter" || e.key === " ") return;
      }
      if (!typing && !e.ctrlKey && !e.metaKey && !e.altKey && (e.key === "n" || e.key === "N")) {
        stop(e);
        RT.noteMode = !RT.noteMode;
        closeNote();
        render();
      }
    }, true);
  }

  // ------------------------------------------------------------------ start

  function registerPrototype(p) {
    if (PROTO) throw new Error("a prototype is already registered");
    if (!p || typeof p.reduce !== "function" || typeof p.render !== "function" || !p.seeds) {
      throw new Error("registerPrototype needs seeds, reduce(state, action) and render(ui, state)");
    }
    PROTO = p;
  }
  function start() {
    var root = document.getElementById("tp-root");
    try {
      DATA = JSON.parse(document.getElementById("tp-review-data").textContent);
    } catch (e) {
      root.textContent = "This review build's data could not be read: " + e.message;
      return;
    }
    KEY = "tp-product-review:v1:" + DATA.package.id;
    buildRegistry();
    try { S = load(); } catch (e) {
      RT.storageNotice = "Review data saved in this browser could not be read (" + e.message + "); it is left untouched and this page starts fresh.";
      RT.storage = {ok: false, reason: "saved data unreadable; not overwriting it"};
      S = freshState();
    }
    if (PROTO && DATA.adapter) {
      DATA.adapter.seeds.forEach(function (s) { if (!own(PROTO.seeds, s.id)) LOAD_ERRORS.push("seed " + s.id + " is declared but the script does not provide it"); });
      if (!S.run) S.run = {seed: DATA.adapter.seeds[0].id, actions: []};
      try { replay(); } catch (e) { RT.message = {kind: "error", text: "Replaying the saved checkpoint failed: " + (e && e.message) + ". Reset to start again."}; }
    }
    var v = location.hash.replace(/^#/, "");
    RT.view = VIEWS.some(function (x) { return x[0] === v; }) ? v : "overview";
    window.addEventListener("hashchange", function () {   // browser back/forward switch views; they never replay actions
      var hv = location.hash.replace(/^#/, "");
      hv = VIEWS.some(function (x) { return x[0] === hv; }) ? hv : "overview";
      if (hv !== RT.view) { RT.view = hv; render(); }
    });
    installInterception();
    paint();
    document.documentElement.setAttribute("data-tp-ready", "1");
    document.dispatchEvent(new CustomEvent("tp-ready"));
  }
  window.addEventListener("error", function (e) { LOAD_ERRORS.push(e.message || "script error"); });
  window.TPReview = Object.freeze({
    registerPrototype: registerPrototype,
    // read-only diagnostics for tests and support: copies, never live objects
    debug: function () {
      return clone({view: RT.view, run: S && S.run, state: RT.state, events: RT.events, notes: S ? S.notes.length : 0,
                    inputs: S ? S.inputs.length : 0, session: S && S.session, sessions: S ? S.sessions.length : 0,
                    storage: RT.storage, blocked: RT.blocked, noteMode: RT.noteMode, pending: RT.pending, stepError: RT.stepError,
                    guide: S && S.ui.guide, unguided: S && S.ui.unguided, errors: LOAD_ERRORS, storageNotice: RT.storageNotice,
                    exportError: RT.exportError, exportParts: RT.exportParts ? RT.exportParts.map(function (x) { return x.bytes; }) : null,
                    salvage: RT.salvage ? {parts: RT.salvage.parts ? RT.salvage.parts.map(function (x) { return x.bytes; }) : null,
                                           error: RT.salvage.error, excluded: RT.salvage.excluded} : null});
    },
    summary: function () { return summaryText(); },
    settled: settled
  });
  document.addEventListener("DOMContentLoaded", start);
})();
