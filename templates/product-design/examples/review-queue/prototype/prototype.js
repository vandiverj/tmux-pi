/* Review queue prototype (synthetic). Implements the tp-product review adapter (docs/product-design.md, "The prototype
 * adapter"): named seeds, one reducer that is the only way state changes, a renderer that draws the current page from
 * the state and registers review targets, and observable completion conditions per scenario.
 * The reducer is pure: it gets a copy of the state and returns the next one; it reads no clock and no storage, so the
 * runtime can replay a seed and an action list to the same state (reset, back, reload and restore all replay). */
(function () {
  "use strict";
  var CURRENT = "reviewer-1";

  function base(page) {
    return {page: page, clock: 0, user: CURRENT, speed: "normal", sending: null, selected: null, flash: null,
            form: {title: "", category: "", errors: {}}, submissions: [], next: 1};
  }
  function withS1(status, draft) {
    var s = base("page.queue");
    s.clock = 1;
    s.next = 2;
    s.submissions.push({id: "S-1", title: "Laptop request", category: "Hardware", status: status, assignee: CURRENT,
                        created: 1, draft: draft || null});
    return s;
  }
  var seeds = {
    "empty-queue": function () { return base("page.submit"); },
    "one-assigned": function () { return withS1("in-review"); },
    "one-in-review": function () { return withS1("in-review", "Checked the specification; waiting on budget."); }
  };

  function find(state, rowTarget) {
    var id = String(rowTarget || "").replace(/^queue\.row\./, "").toUpperCase();
    for (var i = 0; i < state.submissions.length; i++) if (state.submissions[i].id === id) return state.submissions[i];
    return null;
  }
  function selected(state) {
    for (var i = 0; i < state.submissions.length; i++) if (state.submissions[i].id === state.selected) return state.submissions[i];
    return null;
  }
  function create(state, title, category) {
    state.clock += 1;
    var rec = {id: "S-" + state.next, title: title, category: category, status: "waiting", assignee: null,
               created: state.clock, draft: null};
    state.next += 1;
    state.submissions.push(rec);
    state.form = {title: "", category: "", errors: {}};
    state.flash = "Submitted " + rec.id + ". It is in the queue as Waiting.";
    return {op: "Create submission", request: "POST /submissions " + JSON.stringify({title: title, category: category}),
            response: "201 " + JSON.stringify({id: rec.id, status: "waiting", created: rec.created}), status: "201",
            records: ["submissions " + rec.id + " (created, status waiting)"],
            consequence: "The queue and the summary list " + rec.id + " as Waiting."};
  }

  function reduce(state, act) {
    var a = act.action, input = act.input || {};
    state.flash = null;
    if (a === "open-page") {
      state.page = act.target;
      return {state: state, ok: true};
    }
    if (a === "fill") {
      var field = act.target === "submit.title" ? "title" : act.target === "submit.category" ? "category" : null;
      if (!field) return {state: state, ok: false, message: "No field " + act.target};
      state.form[field] = String(input.value == null ? "" : input.value);
      delete state.form.errors[field];
      return {state: state, ok: true};
    }
    if (a === "set-speed") {
      state.speed = input.value === "slow" ? "slow" : "normal";
      return {state: state, ok: true};
    }
    if (a === "submit") {
      if (state.sending) {
        state.flash = "Still sending: the first submission has not been answered yet. Nothing was sent again.";
        return {state: state, ok: false, message: "already sending"};
      }
      var errors = {}, title = state.form.title.trim(), category = state.form.category.trim();
      if (!title) errors.title = "Title is required";
      if (!category) errors.category = "Category is required";
      var body = JSON.stringify({title: title, category: category});
      if (errors.title || errors.category) {
        state.form.errors = errors;
        var list = [];
        if (errors.title) list.push({field: "title", message: errors.title});
        if (errors.category) list.push({field: "category", message: errors.category});
        return {state: state, ok: false, message: "rejected",
                events: [{operation: "Create submission", request: "POST /submissions " + body,
                          response: "422 " + JSON.stringify({errors: list}), status: "422", records: [],
                          consequence: "The form names each missing field; the queue and the summary are unchanged."}]};
      }
      if (state.speed === "slow") {
        state.sending = {title: title, category: category};
        state.flash = "Still sending...";
        return {state: state, ok: true,
                events: [{operation: "Create submission", request: "POST /submissions " + body,
                          response: "(no response yet: simulated delay)", status: "pending", records: [],
                          consequence: "The form shows 'Still sending' and refuses a second submit."}]};
      }
      var ev = create(state, title, category);
      return {state: state, ok: true, events: [{operation: ev.op, request: ev.request, response: ev.response,
                                                status: ev.status, records: ev.records, consequence: ev.consequence}]};
    }
    if (a === "receive-response") {
      if (!state.sending) return {state: state, ok: false, message: "nothing is being sent"};
      var s = state.sending;
      state.sending = null;
      var done = create(state, s.title, s.category);
      return {state: state, ok: true, events: [{operation: done.op, request: "(response to the pending request)",
                                                response: done.response, status: done.status, records: done.records,
                                                consequence: done.consequence}]};
    }
    if (a === "open-record") {
      var rec = find(state, act.target);
      if (!rec) return {state: state, ok: false, message: "No record " + act.target};
      state.selected = rec.id;
      state.page = "page.record";
      return {state: state, ok: true};
    }
    var cur = selected(state);
    if (a === "claim") {
      if (!cur) return {state: state, ok: false, message: "Open a record first"};
      cur.assignee = state.user;
      cur.status = "in-review";
      return {state: state, ok: true, events: [{operation: "Assign to me", request: "(no operation proposed yet)",
              response: "(simulated locally)", status: "local", records: ["submissions " + cur.id + " assignee"],
              consequence: cur.id + " is In review, assigned to " + state.user + "."}]};
    }
    if (a === "resolve") {
      if (!cur) return {state: state, ok: false, message: "Open a record first"};
      var outcome = act.target === "record.return" ? "returned" : "approved";
      var req = "POST /submissions/" + cur.id + "/resolve " + JSON.stringify({outcome: outcome});
      if (cur.assignee !== state.user) {
        state.flash = "Assigned to another reviewer.";
        return {state: state, ok: false, message: "forbidden",
                events: [{operation: "Resolve submission", request: req,
                          response: "403 " + JSON.stringify({error: "assigned to another reviewer"}), status: "403",
                          records: [], consequence: cur.id + " stays " + cur.status + "."}]};
      }
      cur.status = outcome;
      state.flash = cur.id + " is " + outcome + ".";
      return {state: state, ok: true,
              events: [{operation: "Resolve submission", request: req,
                        response: "200 " + JSON.stringify({id: cur.id, status: outcome}), status: "200",
                        records: ["submissions " + cur.id + " (status " + outcome + ")"],
                        consequence: cur.id + " leaves the waiting count and shows " + outcome + "."}]};
    }
    if (a === "reassign") {
      if (!cur) return {state: state, ok: false, message: "Open a record first"};
      var to = String(input.to || "reviewer-2");
      cur.assignee = to;
      state.flash = cur.id + " is assigned to " + to + ". Not decided: what happens to the previous reviewer's draft.";
      return {state: state, ok: true,
              events: [{operation: "Reassign submission", request: "(no operation proposed yet: question open)",
                        response: "(simulated locally)", status: "local", records: ["submissions " + cur.id + " assignee"],
                        consequence: cur.id + " is assigned to " + to + "; the draft's fate is undecided."}]};
    }
    return {state: state, ok: false, message: "Unknown action " + a};
  }

  function counts(state) {
    var c = {waiting: 0, review: 0, resolved: 0};
    state.submissions.forEach(function (s) {
      if (s.status === "waiting") c.waiting += 1;
      else if (s.status === "in-review") c.review += 1;
      else c.resolved += 1;
    });
    return c;
  }
  var STATUS = {"waiting": "Waiting", "in-review": "In review", "approved": "Approved", "returned": "Returned"};

  function render(ui, state) {
    var h = ui.h;
    var nav = h("nav", {"class": "rq-nav"},
      [["page.submit", "Submit"], ["page.queue", "Queue"], ["page.summary", "Summary"]].map(function (p) {
        return ui.target(h("button", {"type": "button", "class": state.page === p[0] ? "rq-on" : ""}, p[1]), p[0],
                         p[1] + " page link", function () { ui.act("open-page", p[0])(); });
      }));
    var body;
    var c = counts(state);
    if (state.page === "page.submit") {
      var title = h("input", {"type": "text", "value": state.form.title, "aria-label": "Title"});
      ui.target(title, "submit.title", "Title field");
      title.addEventListener("change", function () { ui.act("fill", "submit.title", {value: title.value})(); });
      var cat = h("select", {"aria-label": "Category"},
        ["", "Hardware", "Software", "Access"].map(function (v) {
          var o = h("option", {"value": v}, v || "Choose...");
          if (v === state.form.category) o.selected = true;
          return o;
        }));
      ui.target(cat, "submit.category", "Category field");
      cat.addEventListener("change", function () { ui.act("fill", "submit.category", {value: cat.value})(); });
      var speed = h("select", {"aria-label": "Simulated service speed"},
        [["normal", "Normal response"], ["slow", "Slow response (simulated delay)"]].map(function (v) {
          var o = h("option", {"value": v[0]}, v[1]);
          if (v[0] === state.speed) o.selected = true;
          return o;
        }));
      ui.target(speed, "submit.speed", "Simulated service speed");
      speed.addEventListener("change", function () { ui.act("set-speed", "submit.speed", {value: speed.value})(); });
      var send = ui.target(h("button", {"type": "button", "class": "rq-primary"}, state.sending ? "Still sending..." : "Submit"),
                           "submit.send", "Submit button", ui.act("submit", "submit.send"));
      body = h("section", {"class": "rq-page"}, [
        h("h3", {}, "Submit a record"),
        h("label", {}, ["Title ", title]),
        state.form.errors.title ? h("p", {"class": "rq-error"}, state.form.errors.title) : null,
        h("label", {}, ["Category ", cat]),
        state.form.errors.category ? h("p", {"class": "rq-error"}, state.form.errors.category) : null,
        h("label", {"class": "rq-sim"}, ["Service (simulated) ", speed]),
        send,
        state.sending ? ui.target(h("button", {"type": "button"}, "Deliver the delayed response"), "submit.response",
                                  "Deliver the delayed response", ui.act("receive-response", "submit.response")) : null
      ]);
    } else if (state.page === "page.queue") {
      var rows = state.submissions.slice().sort(function (a, b) { return a.created - b.created; }).map(function (s) {
        var tid = "queue.row." + s.id.toLowerCase();
        return ui.target(h("tr", {"class": "rq-row", "tabindex": "0"}, [h("td", {}, s.id), h("td", {}, s.title),
                         h("td", {}, s.category), h("td", {}, STATUS[s.status] || s.status), h("td", {}, s.assignee || "-")]),
                         tid, "Queue row " + s.id, ui.act("open-record", tid));
      });
      body = h("section", {"class": "rq-page"}, [
        h("h3", {}, "Review queue"),
        ui.target(h("p", {"class": "rq-count"}, "Waiting: " + c.waiting), "queue.waiting-count", "Waiting count"),
        rows.length ? h("table", {"class": "rq-table"}, [h("thead", {}, h("tr", {}, ["Id", "Title", "Category", "Status",
                        "Assignee"].map(function (x) { return h("th", {}, x); }))), h("tbody", {}, rows)])
                    : h("p", {"class": "rq-empty"}, "The queue is empty.")
      ]);
    } else if (state.page === "page.record") {
      var r = selected(state);
      body = h("section", {"class": "rq-page"}, r ? [
        h("h3", {}, r.id + ": " + r.title),
        h("p", {}, "Category " + r.category + " - " + (STATUS[r.status] || r.status) + " - assigned to " + (r.assignee || "nobody")),
        r.draft ? h("p", {"class": "rq-draft"}, "Your draft note: " + r.draft) : null,
        h("div", {"class": "rq-actions"}, [
          ui.target(h("button", {"type": "button"}, "Assign to me"), "record.claim", "Assign to me button", ui.act("claim", "record.claim")),
          ui.target(h("button", {"type": "button", "class": "rq-primary"}, "Approve"), "record.approve", "Approve button",
                    ui.act("resolve", "record.approve")),
          ui.target(h("button", {"type": "button"}, "Return"), "record.return", "Return button", ui.act("resolve", "record.return")),
          ui.target(h("button", {"type": "button"}, "Reassign to reviewer-2"), "record.reassign", "Reassign button",
                    ui.act("reassign", "record.reassign", {to: "reviewer-2"}))
        ])
      ] : [h("p", {}, "No record is open. Open one from the queue.")]);
    } else {
      body = h("section", {"class": "rq-page"}, [
        h("h3", {}, "Summary"),
        ui.target(h("p", {"class": "rq-count"}, "Waiting: " + c.waiting), "summary.waiting", "Waiting total"),
        h("p", {}, "In review: " + c.review),
        ui.target(h("p", {}, "Resolved: " + c.resolved), "summary.resolved", "Resolved total")
      ]);
    }
    return h("div", {"class": "rq-app"}, [h("p", {"class": "rq-user"}, "Signed in (simulated) as " + state.user), nav,
                                          state.flash ? h("p", {"class": "rq-flash", "role": "status"}, state.flash) : null, body]);
  }

  function s1(state) { return state.submissions.filter(function (s) { return s.id === "S-1"; })[0] || null; }
  var conditions = {
    "scenario.submit-complete-record": function (state) {
      return state.page === "page.queue" && state.submissions.some(function (s) { return s.status === "waiting" && s.title && s.category; });
    },
    "scenario.submit-missing-category": function (state) {
      return !!state.form.errors.category && state.submissions.length === 0;
    },
    "scenario.resolve-assigned-submission": function (state) { var s = s1(state); return !!s && s.status === "approved"; },
    "scenario.reassign-during-review": function (state) { var s = s1(state); return !!s && s.assignee === "reviewer-2"; }
  };

  TPReview.registerPrototype({id: "review-queue", seeds: seeds, reduce: reduce, render: render, conditions: conditions});
})();
