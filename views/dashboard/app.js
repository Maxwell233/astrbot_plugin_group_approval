/**
 * Frontend script for the AstrBot Group Approval WebUI dashboard.
 * Version: 0.1.2 (Early Development & Debugging)
 *
 * CANONICAL LOCATION: views/dashboard/ (AstrBot 4.26+ / 4.29.4 prefers "views";
 * "pages/" is only a legacy alias and must not carry a second copy).
 *
 * AstrBot 4.29.4 contract:
 *  - Plugin views are served from views/<view_name>/index.html.
 *  - The bridge is window.AstrBotPluginView (alias window.AstrBotPluginPage);
 *    apiGet/apiPost are forwarded by the dashboard to
 *    /api/v1/plugins/extensions/<plugin_name>/<endpoint>.
 *  - The legacy /api/plug/... route does NOT exist in 4.29.4; the fallback used
 *    here is the correct /api/v1/plugins/extensions/... path.
 *
 * All dynamic data is rendered with DOM APIs (textContent / createElement); no
 * user-controlled value is ever interpolated into innerHTML.
 */

const PLUGIN_NAME = "astrbot_plugin_group_approval";
const API_ROUTE_PREFIX = `/api/v1/plugins/extensions/${encodeURIComponent(PLUGIN_NAME)}`;
const REQUEST_TIMEOUT_MS = 30000;
const BRIDGE_READY_TIMEOUT_MS = 1500;

// The bridge only exists in the dashboard-embedded context (restricted iframe).
// A direct/standalone load of the view must not postMessage into the void.
const IS_EMBEDDED = window.parent !== window;
const BRIDGE = IS_EMBEDDED ? (window.AstrBotPluginView || window.AstrBotPluginPage) : null;

const state = { verifiedBots: [], settingsBot: "", readSequence: 0 };

// ---------------------------------------------------------------------------
// Tiny DOM helpers (XSS-safe by construction)
// ---------------------------------------------------------------------------
function $(id) {
  return document.getElementById(id);
}

function el(tag, options = {}) {
  const node = document.createElement(tag);
  if (options.className) node.className = options.className;
  if (options.text !== undefined && options.text !== null) node.textContent = String(options.text);
  if (options.attrs) {
    Object.entries(options.attrs).forEach(([key, value]) => {
      if (value !== undefined && value !== null) node.setAttribute(key, String(value));
    });
  }
  return node;
}

function cell(content, className) {
  const td = document.createElement("td");
  if (className) td.className = className;
  if (content instanceof Node) {
    td.appendChild(content);
  } else {
    td.textContent = content === undefined || content === null ? "" : String(content);
  }
  return td;
}

function renderTableMessage(tbody, colspan, message, className = "text-center text-muted") {
  if (!tbody) return;
  tbody.replaceChildren();
  const tr = document.createElement("tr");
  const td = el("td", { text: message, className });
  td.colSpan = colspan;
  tr.appendChild(td);
  tbody.appendChild(tr);
}

function formatDate(ts) {
  if (ts === null || ts === undefined || ts === "" || ts === 0) return "-";
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return "-";
  return new Date(n * 1000).toLocaleString("zh-CN", { hour12: false });
}

function toast(message, type = "info") {
  let container = $("toast-container");
  if (!container) {
    container = el("div", { attrs: { id: "toast-container", class: "toast-container", "aria-live": "polite" } });
    document.body.appendChild(container);
  }
  const node = el("div", { className: `toast toast-${type}`, text: String(message || "") });
  container.appendChild(node);
  window.setTimeout(() => {
    node.classList.add("toast-hide");
    window.setTimeout(() => node.remove(), 300);
  }, 4200);
}

function toastError(error) {
  const message = error instanceof Error ? error.message : String(error || "操作失败");
  toast(message, "error");
}

// ---------------------------------------------------------------------------
// API layer: ONE helper, bridge first, correct 4.26+/4.29.4 HTTP fallback.
// ---------------------------------------------------------------------------
function normalizeEndpoint(endpoint) {
  const raw = String(endpoint || "").replace(/^\/+/, "").replace(/\/+$/, "");
  if (!raw) throw new Error("API endpoint is empty");
  const parts = raw.split("/");
  if (parts.some((part) => !part || part === "." || part === "..")) {
    throw new Error("API endpoint is invalid");
  }
  return { path: parts.join("/"), encoded: parts.map((part) => encodeURIComponent(part)).join("/") };
}

function readDashboardToken() {
  // The view is a same-origin iframe of the AstrBot dashboard. Only in the embedded
  // (bridge) context may the dashboard's own bearer token be forwarded.
  if (!IS_EMBEDDED) return null;
  try {
    return window.localStorage.getItem("token");
  } catch (error) {
    return null;
  }
}

function unwrapApiPayload(payload) {
  if (
    payload &&
    typeof payload === "object" &&
    !Array.isArray(payload) &&
    (Object.prototype.hasOwnProperty.call(payload, "status") ||
      Object.prototype.hasOwnProperty.call(payload, "retcode"))
  ) {
    if (payload.status === "error") {
      const error = new Error(payload.message || payload.error || "接口返回错误");
      error.retcode = payload.retcode;
      error.payload = payload;
      throw error;
    }
    const nestedMessage =
      payload.data && typeof payload.data === "object" && typeof payload.data.message === "string"
        ? payload.data.message
        : "";
    return {
      status: "ok",
      retcode: payload.retcode === undefined || payload.retcode === null ? 0 : payload.retcode,
      message: payload.message || nestedMessage || "",
      data: payload.data === undefined ? {} : payload.data,
      raw: payload,
    };
  }
  const message =
    payload && typeof payload === "object" && typeof payload.message === "string"
      ? payload.message
      : "";
  return {
    status: "ok",
    retcode: 0,
    message,
    data: payload === undefined ? {} : payload,
    raw: payload,
  };
}

async function apiFetch(encodedPath, method, payload, queryParams) {
  const url = new URL(`${API_ROUTE_PREFIX}/${encodedPath}`, window.location.origin);
  Object.entries(queryParams || {}).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") return;
    url.searchParams.set(key, String(value));
  });

  const headers = {};
  if (method !== "GET") headers["Content-Type"] = "application/json";
  const token = readDashboardToken();
  if (token) headers.Authorization = `Bearer ${token}`;

  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  let response;
  try {
    response = await fetch(url.toString(), {
      method,
      credentials: "include",
      headers,
      signal: controller.signal,
      body: method === "GET" ? undefined : JSON.stringify(payload || {}),
    });
  } catch (error) {
    if (error && error.name === "AbortError") throw new Error("请求超时，请重试");
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }

  let data = null;
  try {
    data = await response.json();
  } catch (error) {
    data = null;
  }
  if (!response.ok) {
    const message =
      (data && (data.message || data.error)) || `请求失败 (HTTP ${response.status})`;
    const error = new Error(message);
    error.status = response.status;
    error.payload = data;
    throw error;
  }
  return data;
}

/**
 * Central API helper.
 *
 * - Bridge present (dashboard iframe): window.AstrBotPluginView.apiGet/apiPost.
 *   The dashboard unwraps the plugin's {"status":"ok","data":...} envelope, so
 *   both wrapped and unwrapped payloads are normalised here.
 * - No bridge (standalone/older host): same-origin HTTP to the real 4.26+ route
 *   /api/v1/plugins/extensions/<plugin>/<endpoint>, with cookies and, only in the
 *   embedded dashboard context, the dashboard bearer token.
 */
async function apiCall(endpoint, method = "GET", payload = null, queryParams = null) {
  const verb = String(method || "GET").toUpperCase();
  const target = normalizeEndpoint(endpoint);
  const params = { ...(queryParams || {}) };
  const explicitBot = getSelectedBotId();
  if (verb === "GET" && explicitBot && params.bot_self_id === undefined) {
    params.bot_self_id = explicitBot;
  }

  if (BRIDGE) {
    if (verb === "GET") {
      if (typeof BRIDGE.apiGet !== "function") throw new Error("AstrBot bridge apiGet 不可用");
      return unwrapApiPayload(await BRIDGE.apiGet(target.path, params));
    }
    if (verb === "POST") {
      if (typeof BRIDGE.apiPost !== "function") throw new Error("AstrBot bridge apiPost 不可用");
      return unwrapApiPayload(await BRIDGE.apiPost(target.path, payload || {}));
    }
    throw new Error(`Unsupported API method: ${verb}`);
  }

  if (verb !== "GET" && verb !== "POST") throw new Error(`Unsupported API method: ${verb}`);
  return unwrapApiPayload(await apiFetch(target.encoded, verb, payload, params));
}

// ---------------------------------------------------------------------------
// Bot selection / write-action safety
// ---------------------------------------------------------------------------
function getSelectedBotId() {
  const select = $("global-bot-select");
  if (!select) return "";
  const value = String(select.value || "").trim();
  return state.verifiedBots.includes(value) ? value : "";
}

async function mutate(bot, action, payload = {}) {
  if (!state.verifiedBots.includes(String(bot))) {
    throw new Error("请选择已核验的 Bot。");
  }
  const response = await apiCall("scope/action", "POST", { ...payload, bot_self_id: bot, action });
  return response.data?.message || response.message || "已保存";
}

function button(label, work) {
  const node = el("button", { text: label, className: "btn btn-secondary btn-sm", attrs: { type: "button" } });
  node.addEventListener("click", () => runAction(work, node));
  return node;
}

async function runAction(work, node) {
  node.disabled = true;
  try {
    const message = await work();
    if (message) toast(message, "success");
    await loadState();
  } catch (error) {
    toastError(error);
  } finally {
    node.disabled = false;
  }
}

async function reviewApplication(row, action, note = "") {
  return mutate(row.bot_self_id, action, { application_id: row.id, note });
}

async function changeGrant(row, action) {
  return mutate(row.bot_self_id, action, { user_id: row.user_id, scope: row.scope });
}

function renderRows(id, rows, columns, actions) {
  const body = $(id);
  body.replaceChildren();
  if (!rows.length) {
    renderTableMessage(body, columns.length + Number(Boolean(actions)), "暂无记录");
    return;
  }
  for (const row of rows) {
    const tr = el("tr");
    for (const column of columns) tr.appendChild(cell(column(row)));
    if (actions) {
      const td = el("td");
      for (const action of actions(row)) td.appendChild(action);
      tr.appendChild(td);
    }
    body.appendChild(tr);
  }
}

function renderSettings(bot, data) {
  state.settingsBot = bot;
  const settings = data.settings;
  $("primary-group").value = settings.primary_group_id;
  $("cache-ttl").value = settings.cache_ttl;
  for (const key of ["group_to_private", "private_to_group", "owner_bypass"]) {
    $(key).checked = Boolean(settings[key]);
  }
  for (const scope of ["GROUP", "PRIVATE"]) {
    $(`level-${scope}`).value = data.policies[scope].min_level;
    $(`approval-${scope}`).checked = Boolean(data.policies[scope].approval_required);
  }
}

async function saveSettings() {
  const bot = state.settingsBot;
  const payload = {
    cache_ttl: $("cache-ttl").value,
    group_to_private: $("group_to_private").checked,
    private_to_group: $("private_to_group").checked,
    owner_bypass: $("owner_bypass").checked,
    group: { min_level: $("level-GROUP").value, approval_required: $("approval-GROUP").checked },
    private: { min_level: $("level-PRIVATE").value, approval_required: $("approval-PRIVATE").checked },
  };
  const primary = $("primary-group").value.trim();
  if (primary) payload.primary_group_id = primary;
  return mutate(bot, "configure", payload);
}

async function loadBots() {
  const { data } = await apiCall("bots");
  state.verifiedBots = Array.isArray(data.verified_bots) ? data.verified_bots.map(String) : [];
  const select = $("global-bot-select");
  const previous = select.value;
  select.replaceChildren(el("option", { text: "选择 QQ Bot", attrs: { value: "" } }));
  for (const bot of state.verifiedBots) {
    select.appendChild(el("option", { text: bot, attrs: { value: bot } }));
  }
  select.value = state.verifiedBots.includes(previous) ? previous :
    (state.verifiedBots.length === 1 ? state.verifiedBots[0] : "");
  $("guard-status").textContent = data.guard_active ? "原生聊天准入已启用" : "原生聊天准入未启用";
}

async function loadState() {
  const bot = getSelectedBotId();
  const seq = ++state.readSequence;
  $("bot-panel").hidden = true;
  state.settingsBot = "";
  if (!bot) return;
  const user = $("user-filter").value.trim();
  const { data } = await apiCall("scope/state", "GET", null, { bot_self_id: bot, user_id: user });
  if (seq !== state.readSequence || bot !== getSelectedBotId()) return;
  renderSettings(bot, data);
  const time = (value) => value ? formatDate(value) : "—";
  const pendingScope = $("pending-scope").value;
  const pendingOnly = $("pending-only").checked;
  renderRows("applications", data.applications.filter(row =>
    (!pendingScope || row.scope === pendingScope) && (!pendingOnly || row.status === "PENDING")
  ), [
    row => row.id, row => row.user_id, row => row.scope, row => row.status,
    row => row.reason, row => row.reviewer_id, row => row.review_note,
  ], row => row.status === "PENDING" ? [
    button("批准", () => reviewApplication(row, "approve")),
    button("拒绝", () => {
      const note = window.prompt("拒绝原因", "");
      return note === null ? undefined : reviewApplication(row, "reject", note);
    }),
  ] : []);
  renderRows("grants", data.grants, [
    row => row.user_id, row => row.scope, row => row.status, row => row.source,
    row => [row.level_exempt && "等级", row.membership_exempt && "成员"].filter(Boolean).join("、"),
    row => row.actor_id,
  ], row => [
    ...(row.status === "SUSPENDED" ? [button("恢复", () => changeGrant(row, "restore"))] : []),
    ...(row.status !== "REVOKED" ? [button("撤销", () => changeGrant(row, "revoke"))] : []),
  ]);
  renderRows("levels", data.levels, [row => row.user_id, row => row.level, row => time(row.verified_at)]);
  renderRows("audit", data.audit, [
    row => time(row.timestamp), row => row.actor_id, row => row.target_user_id, row => row.action,
    row => row.request_id, row => row.details_json,
  ]);
  $("bot-panel").hidden = false;
}

document.addEventListener("DOMContentLoaded", () => {
  $("global-bot-select").addEventListener("change", () => loadState().catch(toastError));
  $("pending-scope").addEventListener("change", () => loadState().catch(toastError));
  $("pending-only").addEventListener("change", () => loadState().catch(toastError));
  $("refresh").addEventListener("click", () => loadBots().then(loadState).catch(toastError));
  $("search").addEventListener("click", () => loadState().catch(toastError));
  $("settings").addEventListener("submit", (event) => {
    event.preventDefault();
    runAction(saveSettings, $("save-settings"));
  });
  $("manual-grant").addEventListener("submit", (event) => {
    event.preventDefault();
    const bot = state.settingsBot;
    const payload = {
      user_id: $("grant-user").value.trim(), scope: $("grant-scope").value,
      level_exempt: $("level-exempt").checked, membership_exempt: $("membership-exempt").checked,
    };
    const submitter = event.submitter || $("save-grant");
    const action = submitter.id === "save-exemptions" ? "exemptions" : "grant";
    runAction(() => mutate(bot, action, payload), submitter);
  });
  loadBots().then(loadState).catch(toastError);
});
