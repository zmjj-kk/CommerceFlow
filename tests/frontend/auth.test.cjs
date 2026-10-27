const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const code = fs.readFileSync(path.join(__dirname, "../../app/static/auth.js"), "utf8");

function harness({ loggedIn = true, retry = false, loginPage = false, confirm = true } = {}) {
  const storage = new Map([["commerceflow_conversation_id", "99"], ["commerceflow_last_user", "previous"]]);
  const calls = [], redirects = [];
  let refreshes = 0, apiCalls = 0;
  const json = (data, status = 200) => new Response(JSON.stringify(data), {
    status, headers: { "Content-Type": "application/json" }
  });
  const nativeFetch = async (input, init = {}) => {
    const url = typeof input === "string" ? input : input.url;
    calls.push({ url, init });
    if (url === "/api/auth/refresh") {
      refreshes++;
      return loggedIn ? json({ access_token: "access-" + refreshes }) : json({}, 401);
    }
    if (url === "/api/auth/me") return json({ user_id: "alice", username: "Alice", role: "user" });
    if (url === "/api/actions/prepare") {
      const body = JSON.parse(init.body);
      return json({ approval_id: "a".repeat(32), preview: body.arguments });
    }
    if (url === "/api/auth/login") return json({ access_token: "login-access" });
    if (url === "/api/auth/logout") return new Response(null, { status: 204 });
    if (url.startsWith("/api/")) {
      apiCalls++;
      if (retry && apiCalls === 1) return json({}, 401);
    }
    return json({ ok: true });
  };
  const location = { pathname: loginPage ? "/login" : "/", search: "", origin: "https://test.local",
    href: "https://test.local" + (loginPage ? "/login" : "/"), replace: u => redirects.push(u) };
  const window = { fetch: nativeFetch, confirm: () => confirm };
  const localStorage = { getItem: k => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, v),
                         removeItem: k => storage.delete(k) };
  vm.runInNewContext(code, {
    window, location, navigator: { locks: { request: (name, fn) => fn() } },
    document: { addEventListener() {} }, localStorage, URL, Headers, Response, Promise, console, alert() {}
  });
  return { window, calls, redirects, storage, refreshes: () => refreshes };
}

test("boot refreshes before API calls; tokens are never persisted", async () => {
  const h = harness();
  await h.window.fetch("/api/conversations");
  const request = h.calls.find(c => c.url === "/api/conversations");
  assert.equal(request.init.headers.get("Authorization"), "Bearer access-1");
  assert.equal(h.storage.get("commerceflow_conversation_id"), undefined);
  assert(![...h.storage.values()].some(v => String(v).includes("access-")));
});

test("401 refresh retries once with a fresh access token", async () => {
  const h = harness({ retry: true });
  const r = await h.window.fetch("/api/conversations");
  assert.equal(r.status, 200);
  assert.equal(h.refreshes(), 2);
  assert.equal(h.calls.filter(c => c.url === "/api/conversations").length, 2);
});

test("remote destinations never receive credentials", async () => {
  const h = harness();
  await h.window.commerceflowAuth.ready;
  await h.window.fetch("https://elsewhere.example/api/orders");
  const request = h.calls.at(-1);
  assert.equal(request.init.headers, undefined);
  assert.equal(request.init.credentials, undefined);
});

test("expired browser session redirects to login", async () => {
  const h = harness({ loggedIn: false });
  await assert.rejects(h.window.fetch("/api/conversations"));
  assert(h.redirects[0].startsWith("/login"));
});

test("write forms obtain exact preview and reuse the grant on retry", async () => {
  const h = harness();
  const request = { method: "POST", body: JSON.stringify({
    conversation_id: 1, description: "Needs support", ticket_type: "咨询"
  }) };
  await h.window.fetch("/api/actions/create-ticket", request);
  await h.window.fetch("/api/actions/create-ticket", request);
  assert.equal(h.calls.filter(c => c.url === "/api/actions/prepare").length, 1);
  const writes = h.calls.filter(c => c.url === "/api/actions/create-ticket");
  assert.equal(JSON.parse(writes[0].init.body).approval_id, "a".repeat(32));
  assert.equal(writes[0].init.body, writes[1].init.body);
});

test("canceling preview has no business POST", async () => {
  const h = harness({ confirm: false });
  const response = await h.window.fetch("/api/actions/create-refund", {
    method: "POST", body: JSON.stringify({ conversation_id: 1, order_id: "1001", reason: "其他" })
  });
  assert.equal(response.status, 409);
  assert.equal(h.calls.filter(c => c.url === "/api/actions/create-refund").length, 0);
});

test("logout revokes server session and clears local conversation", async () => {
  const h = harness();
  await h.window.commerceflowAuth.ready;
  await h.window.commerceflowAuth.logout();
  assert(h.calls.some(c => c.url === "/api/auth/logout" && c.init.method === "POST"));
  assert.equal(h.storage.get("commerceflow_conversation_id"), undefined);
  assert.equal(h.redirects.at(-1), "/login");
});


test("chat markup escapes quotes from untrusted model or order content", () => {
  const source = fs.readFileSync(path.join(__dirname, "../../app/static/index.html"), "utf8");
  const match = source.match(/function escapeHtml\(s\) \{[\s\S]*?\n    \}/);
  assert(match);
  const result = vm.runInNewContext(match[0] + "; escapeHtml(input)", {
    input: 'https://example.test/"onmouseover="alert(1)<img>'
  });
  assert(!result.includes('"') && !result.includes("<img>"));
  assert(result.includes("&quot;") && result.includes("&lt;"));
});


