/* Access tokens live only in this page's memory; refresh tokens use HttpOnly cookies. */
(() => {
  const nativeFetch = window.fetch.bind(window);
  let access = "", user = null, refreshing = null;
  const loginPage = location.pathname === "/login";
  const grants = new Map();
  const safeNext = () => location.pathname + location.search;
  function goLogin() {
    if (!loginPage) location.replace("/login?next=" + encodeURIComponent(safeNext()));
  }
  async function refreshAccess() {
    if (refreshing) return refreshing;
    const perform = async () => {
      const response = await nativeFetch("/api/auth/refresh", {
        method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }
      });
      if (!response.ok) { access = ""; return false; }
      access = (await response.json()).access_token;
      return true;
    };
    refreshing = (navigator.locks ? navigator.locks.request("commerceflow-refresh", perform) : perform())
      .finally(() => { refreshing = null; });
    return refreshing;
  }
  async function identify() {
    const response = await nativeFetch("/api/auth/me", { headers: { Authorization: "Bearer " + access } });
    if (!response.ok) throw new Error("登录状态无效");
    user = await response.json();
    const old = localStorage.getItem("commerceflow_last_user");
    if (old !== user.user_id) localStorage.removeItem("commerceflow_conversation_id");
    localStorage.setItem("commerceflow_last_user", user.user_id);
    localStorage.removeItem("commerceflow_session_id");
    return user;
  }
  const ready = loginPage ? Promise.resolve() : (async () => {
    try {
      if (!await refreshAccess()) { goLogin(); return false; }
      await identify();
      return true;
    } catch { goLogin(); return false; }
  })();

  async function authorizedFetch(input, init = {}) {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    // Never attach credentials to a remote origin.
    if (url.origin !== location.origin || !url.pathname.startsWith("/api/") ||
        url.pathname.startsWith("/api/auth/")) return nativeFetch(input, init);
    if (!await ready) throw new Error("请先登录");
    let requestInit = { ...init, credentials: "same-origin", headers: new Headers(init.headers || {}) };
    // Explicit server preview for HTTP forms; retry the same grant after an uncertain response.
    if (["/api/actions/create-ticket", "/api/actions/create-refund"].includes(url.pathname) && init.body) {
      const body = JSON.parse(init.body);
      if (!body.approval_id) {
        const kind = url.pathname.endsWith("create-refund") ? "create_refund" : "create_ticket";
        const args = kind === "create_refund" ? { order_id: body.order_id, reason: body.reason } :
          { description: body.description, ticket_type: body.ticket_type };
        const key = JSON.stringify([user.user_id, body.conversation_id, kind, args]);
        let grant = grants.get(key);
        if (!grant) {
          const response = await authorizedFetch("/api/actions/prepare", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ conversation_id: body.conversation_id, tool_name: kind, arguments: args })
          });
          if (!response.ok) return response;
          grant = await response.json();
          const preview = kind === "create_refund" ?
            "订单：" + grant.preview.order_id + "\n原因：" + grant.preview.reason :
            "类别：" + grant.preview.ticket_type + "\n描述：" + grant.preview.description;
          if (!window.confirm(preview + "\n\n确认提交申请工单？")) {
            return new Response(JSON.stringify({ detail: "已取消提交" }), {
              status: 409, headers: { "Content-Type": "application/json" }
            });
          }
          grants.set(key, grant);
        }
        body.approval_id = grant.approval_id;
        requestInit.body = JSON.stringify(body);
      }
    }
    const perform = () => {
      requestInit.headers.set("Authorization", "Bearer " + access);
      return nativeFetch(input, requestInit);
    };
    let response = await perform();
    if (response.status === 401) {
      if (!await refreshAccess()) { goLogin(); return response; }
      response = await perform();
    }
    return response;
  }
  window.fetch = authorizedFetch;
  window.commerceflowAuth = {
    ready,
    async login(username, password) {
      const response = await nativeFetch("/api/auth/login", {
        method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password })
      });
      if (!response.ok) throw new Error(response.status === 429 ? "操作过于频繁，请稍后再试" : "登录失败，请检查凭证或联系管理员");
      access = (await response.json()).access_token;
      return identify();
    },
    async register(username, password) {
      const response = await nativeFetch("/api/auth/register", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password })
      });
      if (!response.ok) throw new Error("注册未开放，或用户名、密码不符合要求");
    },
    async logout() {
      const response = await nativeFetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
      if (!response.ok) throw new Error("退出失败，请重试");
      access = ""; grants.clear();
      localStorage.removeItem("commerceflow_conversation_id");
      location.replace("/login");
    }
  };
  document.addEventListener("DOMContentLoaded", async () => {
    if (loginPage || !await ready) return;
    const bar = document.createElement("div");
    bar.style.cssText = "position:fixed;right:12px;bottom:8px;z-index:1000;background:#fff6e6;border:2px solid #2b2632;padding:6px;font:13px sans-serif";
    const label = document.createElement("span"); label.textContent = user.username + " ";
    const button = document.createElement("button"); button.textContent = "退出登录";
    button.onclick = () => window.commerceflowAuth.logout().catch(e => alert(e.message));
    bar.append(label, button); document.body.appendChild(bar);
  });
})();
