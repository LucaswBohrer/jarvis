// Behavioral harness for the F3.7 web shell (tests/integration/web_shell_harness.js).
//
// Runs the page's inline <script> under Node with stubbed DOM / fetch /
// sessionStorage and reports what the page did as JSON:
//   { fetchCalls, bubbles, memoryItems, stateLine, storedSession, views,
//     viewHistory, orbState, dataMotion, sessionRows, taskRows, activityRows,
//     auditRows, nexusStatus, nexusProv, homeStatus, panelTask, greeting,
//     bootDone, backendStatus }
// or { error } if the page script itself threw.
//
// Usage: node web_shell_harness.js <index.html> <scenario.json>
//
// Scenario JSON shape:
//   {
//     "storedSession": "sess-1" | null,   // pre-seeded sessionStorage
//     "createSessionId": "new-session",   // id returned by POST /api/v1/sessions
//     "history": [ {role, content}, ... ] // GET .../messages payload
//              | { "__status": 404 }       // GET .../messages fails with 404
//              | { "__fail": true }        // GET .../messages throws (network)
//     "memories": [ {kind, content}, ... ] // GET /api/v1/memory payload
//              | { "__fail": true }        // GET /api/v1/memory throws (network)
//     "sessionList": [ {id, status, ...} ] // GET /api/v1/sessions payload
//     "taskList": [ {id, kind, state, ...} ] // GET /api/v1/tasks payload
//     "auditEvents": [ {event_type, outcome, ...} ] // GET /api/v1/audit payload
//     "healthFail": true,                  // GET /health fails
//     "postMessage": { status, message, source, task_id } // POST .../messages
//              | { "__status": 422, "__detail": "..." }    // POST fails
//     "steps": [                           // simulated user actions, in order
//       { "set": ["input", "hello"] },
//       { "fire": ["composer", "submit"] },
//       { "fire": ["nav-tasks", "click"] },
//       { "fire": ["input", "keydown", { "key": "Enter" }] },
//       { "fireChild": ["session-list", 0, "click"] }
//     ]
//   }

const fs = require("fs");
const vm = require("vm");

const [htmlPath, scenarioPath] = process.argv.slice(2);
const html = fs.readFileSync(htmlPath, "utf8");
const scenario = JSON.parse(fs.readFileSync(scenarioPath, "utf8"));

const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>/);
if (!scriptMatch) {
  console.error("no inline script found");
  process.exit(2);
}
const pageSrc = scriptMatch[1];
if (/innerHTML/.test(pageSrc)) {
  // The page must never parse API text as HTML (XSS boundary, F3.2/F3.3).
  console.error("page uses innerHTML");
  process.exit(3);
}

const fetchCalls = [];
const bubbles = []; // {kind, text, meta}
const memoryItems = []; // F3.5: {title, meta} rendered into #memory-list

function makeEl(id) {
  const el = {
    _id: id || null,
    _text: "",
    _attrs: {},
    _listeners: {},
    _classes: new Set(),
    disabled: false,
    hidden: false,
    checked: false,
    value: "",
    style: {},
    dataset: {},
    children: [],
    classList: {
      add(...cls) {
        cls.forEach((c) => el._classes.add(c));
        el.className = [...el._classes].join(" ");
      },
      remove(...cls) {
        cls.forEach((c) => el._classes.delete(c));
        el.className = [...el._classes].join(" ");
      },
      toggle(c, force) {
        const has = el._classes.has(c);
        const on = force === undefined ? !has : !!force;
        if (on) el._classes.add(c);
        else el._classes.delete(c);
        el.className = [...el._classes].join(" ");
        return on;
      },
      contains(c) {
        return el._classes.has(c);
      },
    },
    setAttribute(name, value) {
      this._attrs[String(name)] = String(value);
    },
    getAttribute(name) {
      const v = this._attrs[String(name)];
      return v === undefined ? null : v;
    },
    appendChild(child) {
      this.children.push(child);
      if (this._id === "conversation") {
        const kind = String(child.className || "").replace(/^msg\s+/, "");
        const meta = child.children.find((c) => c.className === "meta");
        const text = child._text || "";
        if (text || meta) {
          bubbles.push({ kind, text, meta: meta ? meta._text : null });
        }
      }
      if (this._id === "memory-list") {
        // Rows nest title/meta inside .r-main; the empty-state is a
        // structured div.empty whose message lives in .e-title.
        if (String(child.className || "").split(/\s+/).indexOf("empty") >= 0) {
          const et = child.children.find(
            (c) => String(c.className || "").split(/\s+/).indexOf("e-title") >= 0
          );
          memoryItems.push({ title: et ? et._text : child._text, meta: null });
        } else {
          const main = child.children[0];
          const title = main && main.children[0];
          const meta = main && main.children[1];
          memoryItems.push({
            title: title ? title._text : child._text,
            meta: meta ? meta._text : null,
          });
        }
      }
      return child;
    },
    addEventListener(type, fn) {
      if (!this._listeners[type]) this._listeners[type] = [];
      this._listeners[type].push(fn);
    },
    removeEventListener() {},
    remove() {},
    scrollTop: 0,
    scrollHeight: 0,
  };
  Object.defineProperty(el, "textContent", {
    // Real DOM: assigning textContent removes child nodes.
    get() {
      return this._text;
    },
    set(v) {
      this._text = String(v);
      this.children.length = 0;
    },
    enumerable: true,
    configurable: true,
  });
  Object.defineProperty(el, "className", {
    get() {
      return this._className || "";
    },
    set(v) {
      this._className = String(v);
      this._classes = new Set(this._className.split(/\s+/).filter(Boolean));
    },
    enumerable: true,
    configurable: true,
  });
  el.className = "";
  return el;
}

const elements = {};
const viewHistory = []; // every time a view section becomes visible (hidden -> false)
// Layout-regression support: the F3.7 overlap bug was CSS (.view{display:flex}
// defeating the hidden attribute), invisible to the stub DOM. Tracking the
// hidden-flag transitions proves the JS view machine shows exactly one view
// at a time; a companion static test asserts the CSS guard exists.
function trackViewVisibility(el, id) {
  if (!id || id.indexOf("view-") !== 0) return;
  let hv = false;
  Object.defineProperty(el, "hidden", {
    get() { return hv; },
    set(v) {
      hv = !!v;
      if (!hv) viewHistory.push(id.slice(5));
    },
    enumerable: true,
    configurable: true,
  });
}
const documentStub = {
  getElementById(id) {
    if (!elements[id]) {
      elements[id] = makeEl(id);
      trackViewVisibility(elements[id], id);
    }
    return elements[id];
  },
  createElement() {
    return makeEl(null);
  },
};

function fire(id, type, extra) {
  const el = documentStub.getElementById(id);
  const listeners = el._listeners[type] || [];
  const ev = Object.assign(
    { preventDefault() {}, target: el },
    extra || {}
  );
  listeners.forEach((fn) => fn(ev));
}

const store = {};
if (scenario.storedSession) store["jarvis.session.id"] = scenario.storedSession;
const sessionStorageStub = {
  getItem(k) {
    return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null;
  },
  setItem(k, v) {
    store[k] = String(v);
  },
  removeItem(k) {
    delete store[k];
  },
};

function makeRes({ ok, status, jsonBody }) {
  return {
    ok,
    status,
    json: async () => jsonBody,
  };
}

function listOrFail(key, failMsg) {
  const v = scenario[key];
  if (v && v.__fail) throw new Error(failMsg);
  return v || [];
}

async function fetchStub(url, opts) {
  const method = (opts && opts.method) || "GET";
  const call = { url, method };
  if (opts && opts.body) {
    try {
      call.body = JSON.parse(opts.body);
    } catch (e) {
      call.body = String(opts.body);
    }
  }
  fetchCalls.push(call);
  if (url === "/health") {
    if (scenario.healthFail) {
      return makeRes({ ok: false, status: 503, jsonBody: { status: "error" } });
    }
    return makeRes({ ok: true, status: 200, jsonBody: { status: "ok", version: "0.1.0" } });
  }
  if (url === "/ready") {
    return makeRes({ ok: true, status: 200, jsonBody: { ready: true, reason: "ok" } });
  }
  if (url === "/version") {
    return makeRes({
      ok: true,
      status: 200,
      jsonBody: { service: "jarvis", version: "0.1.0", database_schema: "0004", policy_version: "1", memory_schema: "v1" },
    });
  }
  const m = url.match(/^\/api\/v1\/sessions\/([^/]+)\/messages$/);
  if (m && method === "GET") {
    const h = scenario.history;
    if (h && h.__fail) throw new Error("network down");
    if (h && h.__status) {
      return makeRes({
        ok: false,
        status: h.__status,
        jsonBody: { error: { message_key: "session.not_found" } },
      });
    }
    return makeRes({ ok: true, status: 200, jsonBody: { messages: h || [] } });
  }
  if (m && method === "POST") {
    const p = scenario.postMessage;
    if (p && p.__status) {
      return makeRes({ ok: false, status: p.__status, jsonBody: { detail: p.__detail || "erro" } });
    }
    const payload = p || { status: "ok", message: "resposta do backend", source: "fake", task_id: "task-1" };
    return makeRes({ ok: true, status: 200, jsonBody: payload });
  }
  if (url === "/api/v1/sessions" && method === "POST") {
    return makeRes({
      ok: true,
      status: 201,
      jsonBody: { id: scenario.createSessionId || "new-session" },
    });
  }
  // NOTE: the list routes below must come AFTER the /messages routes above,
  // otherwise the "/api/v1/sessions" prefix would swallow message URLs.
  if (url.startsWith("/api/v1/sessions") && method === "GET") {
    return makeRes({ ok: true, status: 200, jsonBody: { items: listOrFail("sessionList", "network down") } });
  }
  if (url.startsWith("/api/v1/tasks") && method === "GET") {
    return makeRes({ ok: true, status: 200, jsonBody: { items: listOrFail("taskList", "network down") } });
  }
  if (url.startsWith("/api/v1/audit") && method === "GET") {
    return makeRes({ ok: true, status: 200, jsonBody: { items: listOrFail("auditEvents", "network down") } });
  }
  // F3.5: read-only memory list.
  if (url.startsWith("/api/v1/memory") && method === "GET") {
    const mem = scenario.memories;
    if (mem && mem.__fail) throw new Error("network down");
    return makeRes({ ok: true, status: 200, jsonBody: { items: mem || [] } });
  }
  throw new Error("unexpected fetch: " + method + " " + url);
}

const sandbox = {
  console,
  document: documentStub,
  sessionStorage: sessionStorageStub,
  fetch: fetchStub,
  AbortController,
  setTimeout,
  clearTimeout,
  JSON,
  Promise,
  Array,
  Object,
  Error,
  encodeURIComponent,
  Math,
  Date,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

function rowParts(id) {
  // Rows are list > row > (main > title/meta, side > badges, leaf...):
  // flatten two levels so tests see the visible texts.
  const el = elements[id];
  if (!el) return [];
  return el.children.map((row) => {
    const parts = [];
    row.children.forEach((c) => {
      if (c.children.length) c.children.forEach((g) => parts.push(g._text));
      else parts.push(c._text);
    });
    return parts;
  });
}

function textOf(id) {
  const el = elements[id];
  return el ? el._text : null;
}

function attrOf(id, name) {
  const el = elements[id];
  if (!el) return null;
  const v = el._attrs[String(name)];
  return v === undefined ? null : v;
}

const VIEWS = ["home", "sessions", "tasks", "memory", "activity", "audit", "nexus", "system"];

(async () => {
  try {
    vm.runInContext(pageSrc, sandbox, { filename: "index-inline.js" });
  } catch (e) {
    console.log(JSON.stringify({ error: "script threw: " + (e && e.message) }));
    process.exit(0);
  }
  // Let the promise chains (ensureSession -> loadHistory -> render) settle.
  await new Promise((r) => setTimeout(r, 300));
  // Simulated user actions.
  const steps = scenario.steps || [];
  for (const st of steps) {
    try {
      if (st.fire) fire(st.fire[0], st.fire[1], st.fire[2] || {});
      else if (st.fireChild) {
        // click a child node inside a container (e.g. a session row)
        const cont = documentStub.getElementById(st.fireChild[0]);
        const child = cont && cont.children[st.fireChild[1]];
        const listeners = (child && child._listeners[st.fireChild[2]]) || [];
        const ev = Object.assign({ preventDefault() {}, target: child }, st.fireChild[3] || {});
        listeners.forEach((fn) => fn(ev));
      }
      else if (st.set) {
        const el = documentStub.getElementById(st.set[0]);
        if (el) el.value = st.set[1];
      }
    } catch (e) {
      console.log(JSON.stringify({ error: "step threw: " + (e && e.message) }));
      process.exit(0);
    }
    await new Promise((r) => setTimeout(r, 150));
  }
  await new Promise((r) => setTimeout(r, 300));
  console.log(
    JSON.stringify({
      fetchCalls,
      bubbles,
      memoryItems,
      stateLine: textOf("state-line"),
      storedSession: sessionStorageStub.getItem("jarvis.session.id"),
      views: VIEWS.filter((v) => elements["view-" + v] && !elements["view-" + v].hidden),
      viewHistory,
      orbState: attrOf("orb-wrap", "data-orb"),
      dataMotion: attrOf("app", "data-motion"),
      sessionRows: rowParts("session-list"),
      taskRows: rowParts("task-list"),
      activityRows: rowParts("activity-list"),
      auditRows: rowParts("audit-list"),
      nexusStatus: textOf("nexus-status-text"),
      nexusProv: textOf("nexus-prov"),
      homeStatus: {
        backend: textOf("hs-backend"),
        session: textOf("hs-session"),
        turns: textOf("hs-turns"),
        task: textOf("hs-task"),
      },
      panelTask: textOf("panel-task"),
      greeting: textOf("greeting-title"),
      bootDone: elements["boot"] ? elements["boot"].classList.contains("done") : null,
      backendStatus: textOf("backend-status"),
    })
  );
  process.exit(0);
})();
