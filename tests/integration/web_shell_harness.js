// Behavioral harness for the F3.3 web shell (tests/integration/web_shell_harness.js).
//
// Runs the page's inline <script> under Node with stubbed DOM / fetch /
// sessionStorage and reports what the page did as JSON:
//   { fetchCalls, bubbles, stateLine, storedSession }
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

function makeEl(id) {
  return {
    _id: id || null,
    textContent: "",
    className: "",
    disabled: false,
    hidden: false,
    value: "",
    children: [],
    classList: {
      add() {},
      remove() {},
      contains() {
        return false;
      },
    },
    appendChild(child) {
      this.children.push(child);
      if (this._id === "conversation") {
        const kind = String(child.className || "").replace(/^msg\s+/, "");
        const body = child.children[0];
        const meta = child.children[1];
        bubbles.push({
          kind,
          text: body ? body.textContent : "",
          meta: meta ? meta.textContent : null,
        });
      }
      return child;
    },
    addEventListener() {},
    removeEventListener() {},
    scrollTop: 0,
    scrollHeight: 0,
  };
}

const elements = {};
[
  "conversation",
  "state-line",
  "composer",
  "input",
  "send",
  "cancel",
  "backend-status",
  "status-dot",
].forEach((id) => {
  elements[id] = makeEl(id);
});

const documentStub = {
  getElementById(id) {
    return elements[id] || null;
  },
  createElement() {
    return makeEl(null);
  },
};

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

async function fetchStub(url, opts) {
  const method = (opts && opts.method) || "GET";
  fetchCalls.push({ url, method });
  if (url === "/health") {
    return makeRes({ ok: true, status: 200, jsonBody: { status: "ok", version: "0.1.0" } });
  }
  if (url === "/api/v1/sessions" && method === "POST") {
    return makeRes({
      ok: true,
      status: 201,
      jsonBody: { id: scenario.createSessionId || "new-session" },
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
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

(async () => {
  try {
    vm.runInContext(pageSrc, sandbox, { filename: "index-inline.js" });
  } catch (e) {
    console.log(JSON.stringify({ error: "script threw: " + e.message }));
    process.exit(0);
  }
  // Let the promise chains (ensureSession -> loadHistory -> render) settle.
  await new Promise((r) => setTimeout(r, 300));
  console.log(
    JSON.stringify({
      fetchCalls,
      bubbles,
      stateLine: elements["state-line"].textContent,
      storedSession: sessionStorageStub.getItem("jarvis.session.id"),
    })
  );
})();
