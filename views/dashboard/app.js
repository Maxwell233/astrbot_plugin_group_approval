/**
 * Frontend script for the AstrBot Group Approval WebUI dashboard.
 * Version: 0.1.1 (Early Development & Debugging)
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

const state = {
  verifiedBots: [],
  groupPolicies: [],
  diagnostics: null,
};

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

function statusTag(status) {
  const map = {
    PENDING: ["待审批", "tag-pending"],
    APPROVED: ["已批准", "tag-active"],
    ACTIVE: ["正常使用", "tag-active"],
    REJECTED: ["已拒绝", "tag-rejected"],
    REVOKED: ["已撤销", "tag-revoked"],
    SUSPENDED: ["已暂停", "tag-suspended"],
    CANCELLED: ["已取消", "tag-expired"],
    EXPIRED: ["已过期", "tag-expired"],
  };
  const key = String(status || "").toUpperCase();
  const entry = map[key];
  if (entry) return el("span", { className: `tag ${entry[1]}`, text: entry[0] });
  return el("span", { className: "tag", text: key || "-" });
}

function actionButton(label, className, onClick, requiresBot = false) {
  const button = el("button", {
    className: `btn btn-sm ${className}${requiresBot ? " requires-bot" : ""}`,
    text: label,
    attrs: { type: "button" },
  });
  button.addEventListener("click", (event) => {
    event.preventDefault();
    if (requiresBot && !getWriteBotId()) return;
    onClick(button);
  });
  return button;
}

// ---------------------------------------------------------------------------
// Toast notifications
// ---------------------------------------------------------------------------
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

function getWriteBotId() {
  const selected = getSelectedBotId();
  if (selected) return selected;
  // A single verified bot is unambiguous; never fall back to "default" or a guess.
  return state.verifiedBots.length === 1 ? state.verifiedBots[0] : "";
}

function requireWriteBot() {
  const botId = getWriteBotId();
  if (!botId) {
    toast(
      state.verifiedBots.length === 0
        ? "未检测到已验证的 Bot 实例"
        : "请先在右上角选择具体的已验证 Bot",
      "error"
    );
    return "";
  }
  return botId;
}

function syncActionAvailability() {
  const writeBot = getWriteBotId();
  const hint = $("bot-hint");
  if (hint) {
    if (state.verifiedBots.length === 0) {
      hint.textContent = "未检测到已验证的 Bot 实例";
      hint.hidden = false;
    } else if (writeBot && !getSelectedBotId()) {
      hint.textContent = `写操作将使用唯一已验证 Bot: ${writeBot}`;
      hint.hidden = false;
    } else if (!writeBot) {
      hint.textContent = "请选择具体 Bot 以启用写操作";
      hint.hidden = false;
    } else {
      hint.hidden = true;
      hint.textContent = "";
    }
  }

  document.querySelectorAll(".requires-bot").forEach((button) => {
    button.disabled = !writeBot;
    if (writeBot) {
      button.removeAttribute("title");
    } else {
      button.setAttribute("title", "请先选择已验证的 Bot 实例");
    }
  });

  const groupSave = $("modal-group-save");
  if (groupSave) groupSave.disabled = !writeBot;
  const grantOpen = $("btn-open-manual-grant");
  if (grantOpen) grantOpen.disabled = !writeBot;
}

async function loadBots() {
  const select = $("global-bot-select");
  if (!select) return;
  try {
    const res = await apiCall("bots");
    const data = res && res.data && typeof res.data === "object" ? res.data : {};
    const rawBots = Array.isArray(data.bots) ? data.bots : [];
    state.verifiedBots = rawBots
      .map((bot) => {
        if (typeof bot === "string") return bot.trim();
        if (bot && typeof bot === "object") {
          if (bot.verified === false) return "";
          const id = bot.bot_self_id || bot.id || bot.self_id;
          return id === undefined || id === null ? "" : String(id).trim();
        }
        return "";
      })
      .filter((bot) => bot && bot !== "default");

    const previous = select.value;
    select.replaceChildren();
    select.appendChild(el("option", { text: "全部 Bot", attrs: { value: "" } }));
    state.verifiedBots.forEach((bot) => {
      select.appendChild(el("option", { text: `Bot: ${bot}`, attrs: { value: bot } }));
    });
    if (state.verifiedBots.includes(previous)) select.value = previous;
    syncActionAvailability();
  } catch (error) {
    state.verifiedBots = [];
    toastError(error);
    syncActionAvailability();
  }
}

// ---------------------------------------------------------------------------
// Diagnostics banner (read-only GroupGuardian precedence status)
// ---------------------------------------------------------------------------
function diagnosticsPrecedence(data) {
  if (!data || typeof data !== "object") return { verified: false, status: "UNKNOWN" };
  if (typeof data.moderation_precedence === "string") {
    return { verified: data.moderation_precedence === "VERIFIED", status: data.moderation_precedence };
  }
  // Legacy shape (older backend): group_guardian_status OPTIMAL/WARNING/NOT_INSTALLED/UNKNOWN.
  const legacy = String(data.group_guardian_status || "UNKNOWN");
  return { verified: legacy === "OPTIMAL", status: legacy };
}

function renderDiagnostics(data) {
  state.diagnostics = data || null;
  const badge = $("handler-diag-badge");
  const banner = $("diag-banner");
  const precedence = diagnosticsPrecedence(data);
  const message = data && data.message ? String(data.message) : "未能获取审核链诊断信息。";
  const recommendations = data && data.recommendations ? String(data.recommendations) : "";

  if (badge) {
    if (precedence.verified) {
      badge.className = "status-badge live";
      badge.textContent = "🛡️ 审核链已验证";
    } else {
      badge.className = "status-badge danger";
      badge.textContent = "⚠️ 审核链未验证";
    }
    badge.title = message;
  }

  if (!banner) return;
  banner.hidden = false;
  banner.className = `diag-banner ${precedence.verified ? "ok" : "danger"}`;
  const icon = $("diag-banner-icon");
  if (icon) icon.textContent = precedence.verified ? "✅" : "⛔";
  const title = $("diag-banner-title");
  if (title) {
    title.textContent = precedence.verified
      ? "审核链已验证：GroupGuardian 优先于准入闸门"
      : `审核链风险（${precedence.status}）：内容审核可能被准入闸门抢先拦截`;
  }
  const messageNode = $("diag-banner-message");
  if (messageNode) messageNode.textContent = message;
  const recommendationNode = $("diag-banner-recommendations");
  if (recommendationNode) {
    recommendationNode.textContent = recommendations;
    recommendationNode.hidden = !recommendations;
  }
}

async function loadDiagnostics() {
  try {
    const res = await apiCall("diagnostics");
    renderDiagnostics(res && res.data ? res.data : {});
  } catch (error) {
    renderDiagnostics({
      moderation_precedence: "UNAVAILABLE",
      message: `无法加载审核链诊断：${error instanceof Error ? error.message : error}`,
      recommendations: "请确认 AstrBot 与插件版本匹配后重试。",
    });
  }
}

// ---------------------------------------------------------------------------
// Tabs / filters / modals
// ---------------------------------------------------------------------------
function loadActiveTab() {
  const active = document.querySelector(".nav-tab.active");
  if (!active) return;
  const tab = active.dataset.tab;
  if (tab === "tab-overview") loadDashboard();
  if (tab === "tab-applications") loadApplications();
  if (tab === "tab-users") loadUsers();
  if (tab === "tab-groups") loadGroups();
  if (tab === "tab-audit") loadAuditLogs();
}

function initTabs() {
  const tabs = document.querySelectorAll(".nav-tab");
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      tabs.forEach((item) => item.classList.remove("active"));
      document.querySelectorAll(".tab-pane").forEach((pane) => pane.classList.remove("active"));
      tab.classList.add("active");
      const pane = document.getElementById(tab.dataset.tab);
      if (pane) pane.classList.add("active");
      loadActiveTab();
    });
  });

  const refresh = $("btn-refresh");
  if (refresh) {
    refresh.addEventListener("click", () => {
      loadActiveTab();
      loadDiagnostics();
    });
  }
}

function initFilters() {
  $("btn-filter-apps")?.addEventListener("click", () => loadApplications());
  $("btn-filter-users")?.addEventListener("click", () => loadUsers());
  $("btn-filter-audit")?.addEventListener("click", () => loadAuditLogs());
  $("btn-export-audit-json")?.addEventListener("click", () => exportAudit("json"));
  $("btn-export-audit-csv")?.addEventListener("click", () => exportAudit("csv"));

  const botSelect = $("global-bot-select");
  if (botSelect) {
    botSelect.addEventListener("change", () => {
      syncActionAvailability();
      loadActiveTab();
    });
  }
}

let currentAction = null; // { type: "approve" | "reject", id: string }

function openApprovalModal(type, applicationId) {
  currentAction = { type, id: String(applicationId || "") };
  const title = $("modal-title");
  const label = $("modal-input-label");
  const note = $("modal-input-note");
  if (title) title.textContent = type === "approve" ? `批准申请：${currentAction.id}` : `拒绝申请：${currentAction.id}`;
  if (label) label.textContent = type === "approve" ? "审批备注：" : "拒绝原因：";
  if (note) note.value = type === "approve" ? "符合群等级要求，准予授权" : "不符合本群准入要求";
  $("modal-action")?.classList.add("show");
}

function initModals() {
  const modalAction = $("modal-action");
  const modalClose = $("modal-close");
  const modalCancel = $("modal-btn-cancel");
  const modalConfirm = $("modal-btn-confirm");

  const closeActionModal = () => modalAction?.classList.remove("show");
  modalClose?.addEventListener("click", closeActionModal);
  modalCancel?.addEventListener("click", closeActionModal);

  modalConfirm?.addEventListener("click", async () => {
    if (!currentAction) return;
    const noteInput = $("modal-input-note");
    const note = noteInput ? noteInput.value.trim() : "";
    modalConfirm.disabled = true;
    try {
      let res;
      if (currentAction.type === "approve") {
        res = await apiCall("applications/approve", "POST", {
          application_id: currentAction.id,
          note: note || "管理员批准",
        });
      } else {
        res = await apiCall("applications/reject", "POST", {
          application_id: currentAction.id,
          reason: note || "管理员拒绝",
        });
      }
      toast(res.message || (currentAction.type === "approve" ? "批准成功" : "拒绝成功"), "success");
      closeActionModal();
      loadApplications();
      loadDashboard();
    } catch (error) {
      toastError(error);
    } finally {
      modalConfirm.disabled = false;
    }
  });

  // Group policy modal
  const modalGroup = $("modal-group");
  const groupClose = $("modal-group-close");
  const groupCancel = $("modal-group-cancel");
  const groupSave = $("modal-group-save");

  const closeGroupModal = () => modalGroup?.classList.remove("show");
  groupClose?.addEventListener("click", closeGroupModal);
  groupCancel?.addEventListener("click", closeGroupModal);

  $("btn-open-add-group")?.addEventListener("click", () => {
    const idInput = $("group-form-id");
    const levelInput = $("group-form-level");
    if (idInput) idInput.value = "";
    if (levelInput) levelInput.value = "10";
    modalGroup?.classList.add("show");
    syncActionAvailability();
  });

  groupSave?.addEventListener("click", async () => {
    const botId = requireWriteBot();
    if (!botId) return;

    const groupId = String($("group-form-id")?.value || "").trim();
    if (!/^\d{1,20}$/.test(groupId)) {
      toast("请输入有效的 QQ 群号（纯数字）", "error");
      return;
    }

    const minLevel = Number.parseInt(String($("group-form-level")?.value || ""), 10);
    if (!Number.isInteger(minLevel) || minLevel < 0 || minLevel > 1000) {
      toast("最低群等级必须是 0 - 1000 之间的整数", "error");
      return;
    }

    const durationDays = Number.parseInt(String($("group-form-duration")?.value || ""), 10);
    if (!Number.isInteger(durationDays) || durationDays < 0 || durationDays > 36500) {
      toast("授权有效期必须是 0 - 36500 之间的整数（天）", "error");
      return;
    }

    const recheckSeconds = Number.parseInt(String($("group-form-recheck")?.value || ""), 10);
    if (!Number.isInteger(recheckSeconds) || recheckSeconds < 10) {
      toast("等级复核间隔必须是不小于 10 的整数（秒）", "error");
      return;
    }

    const restorePolicy = String($("group-form-restore")?.value || "");
    if (!["automatic", "manual"].includes(restorePolicy)) {
      toast("资格恢复策略无效", "error");
      return;
    }

    const payload = {
      bot_self_id: botId,
      group_id: groupId,
      enabled: String($("group-form-enabled")?.value || "true") === "true",
      min_level: minLevel,
      approval_required: String($("group-form-approval")?.value || "true") === "true",
      grant_duration_days: durationDays,
      recheck_interval_seconds: recheckSeconds,
      restore_policy: restorePolicy,
      group_admin_bypass: String($("group-form-admin-bypass")?.value || "false") === "true",
    };

    groupSave.disabled = true;
    try {
      const res = await apiCall("groups/update", "POST", payload);
      toast(res.message || "群策略保存成功", "success");
      closeGroupModal();
      loadGroups();
    } catch (error) {
      toastError(error);
    } finally {
      groupSave.disabled = false;
      syncActionAvailability();
    }
  });

  // Manual grant modal
  const modalGrant = $("modal-grant");
  const grantClose = $("modal-grant-close");
  const grantCancel = $("modal-grant-cancel");
  const grantSave = $("modal-grant-save");

  const closeGrantModal = () => modalGrant?.classList.remove("show");
  grantClose?.addEventListener("click", closeGrantModal);
  grantCancel?.addEventListener("click", closeGrantModal);

  $("btn-open-manual-grant")?.addEventListener("click", () => {
    if (!requireWriteBot()) return;
    ["grant-form-group", "grant-form-user", "grant-form-note"].forEach((id) => {
      const node = $(id);
      if (node) node.value = "";
    });
    const days = $("grant-form-days");
    if (days) days.value = "0";
    modalGrant?.classList.add("show");
  });

  grantSave?.addEventListener("click", async () => {
    const botId = requireWriteBot();
    if (!botId) return;

    const groupId = String($("grant-form-group")?.value || "").trim();
    if (!/^\d{1,20}$/.test(groupId)) {
      toast("请输入有效的 QQ 群号（纯数字）", "error");
      return;
    }
    const userId = String($("grant-form-user")?.value || "").trim();
    if (!/^\d{1,20}$/.test(userId)) {
      toast("请输入有效的用户 QQ 号（纯数字）", "error");
      return;
    }
    const durationDays = Number.parseInt(String($("grant-form-days")?.value || "0"), 10);
    if (!Number.isInteger(durationDays) || durationDays < 0 || durationDays > 36500) {
      toast("授权有效期必须是 0 - 36500 之间的整数（天）", "error");
      return;
    }
    const payload = {
      bot_self_id: botId,
      group_id: groupId,
      user_id: userId,
      duration_days: durationDays,
      note: String($("grant-form-note")?.value || "").trim() || "Dashboard手动授权",
      is_level_exempt: String($("grant-form-exempt")?.value || "false") === "true",
    };
    grantSave.disabled = true;
    try {
      const res = await apiCall("users/grant", "POST", payload);
      toast(res.message || "手动授权成功", "success");
      closeGrantModal();
      loadUsers();
    } catch (error) {
      toastError(error);
    } finally {
      grantSave.disabled = false;
      syncActionAvailability();
    }
  });
}

// ---------------------------------------------------------------------------
// 1. Overview
// ---------------------------------------------------------------------------
async function loadDashboard() {
  try {
    const res = await apiCall("dashboard");
    const data = res.data || {};
    const stats = data.stats || {};
    const setMetric = (id, value) => {
      const node = $(id);
      if (node) node.textContent = String(value === undefined || value === null ? 0 : value);
    };
    setMetric("metric-groups", stats.enabled_groups);
    setMetric("metric-pending", stats.pending_applications);
    setMetric("metric-active", stats.active_grants);
    setMetric("metric-suspended", stats.suspended_grants);
    setMetric("metric-blacklist", stats.blacklisted_users);

    const pendingBadge = $("nav-pending-badge");
    if (pendingBadge) pendingBadge.textContent = String(stats.pending_applications || 0);

    const tbody = $("overview-recent-table");
    const applications = Array.isArray(data.recent_applications) ? data.recent_applications : [];
    if (applications.length === 0) {
      renderTableMessage(tbody, 7, "暂无申请记录");
      return;
    }
    tbody.replaceChildren();
    applications.forEach((application) => {
      const tr = document.createElement("tr");
      tr.appendChild(cell(application.id, "font-strong"));
      tr.appendChild(cell(application.group_id));
      tr.appendChild(cell(application.user_id));
      tr.appendChild(cell(`LV${application.level}`));
      tr.appendChild(cell(formatDate(application.created_at)));
      tr.appendChild(cell(statusTag(application.status)));
      const actions = document.createElement("td");
      if (String(application.status || "").toUpperCase() === "PENDING") {
        actions.appendChild(actionButton("批准", "btn-success", () => openApprovalModal("approve", application.id)));
        actions.appendChild(document.createTextNode(" "));
        actions.appendChild(actionButton("拒绝", "btn-danger", () => openApprovalModal("reject", application.id)));
      } else {
        actions.appendChild(el("span", { className: "text-muted", text: "-" }));
      }
      tr.appendChild(actions);
      tbody.appendChild(tr);
    });
  } catch (error) {
    toastError(error);
    renderTableMessage($("overview-recent-table"), 7, "加载失败");
  }
}

// ---------------------------------------------------------------------------
// 2. Applications
// ---------------------------------------------------------------------------
async function loadApplications() {
  const tbody = $("applications-table-body");
  renderTableMessage(tbody, 9, "加载中...");
  try {
    const params = {};
    const gid = String($("app-filter-group")?.value || "").trim();
    const uid = String($("app-filter-user")?.value || "").trim();
    const status = String($("app-filter-status")?.value || "");
    if (gid) params.group_id = gid;
    if (uid) params.user_id = uid;
    if (status) params.status = status;

    const res = await apiCall("applications", "GET", null, params);
    const data = res.data || {};
    const applications = Array.isArray(data.applications) ? data.applications : [];
    if (applications.length === 0) {
      renderTableMessage(tbody, 9, "无符合条件的申请记录");
      return;
    }
    tbody.replaceChildren();
    applications.forEach((application) => {
      const tr = document.createElement("tr");
      tr.appendChild(cell(application.id, "font-strong"));
      tr.appendChild(cell(application.group_id));
      tr.appendChild(cell(application.user_id));
      tr.appendChild(cell(`LV${application.level_at_application}`));
      tr.appendChild(cell(application.reason || "-"));
      tr.appendChild(cell(formatDate(application.created_at)));
      tr.appendChild(cell(statusTag(application.status)));
      tr.appendChild(cell(application.review_note || "-"));
      const actions = document.createElement("td");
      if (String(application.status || "").toUpperCase() === "PENDING") {
        actions.appendChild(actionButton("批准", "btn-success", () => openApprovalModal("approve", application.id)));
        actions.appendChild(document.createTextNode(" "));
        actions.appendChild(actionButton("拒绝", "btn-danger", () => openApprovalModal("reject", application.id)));
      } else {
        actions.appendChild(el("span", { className: "text-muted", text: "已处理" }));
      }
      tr.appendChild(actions);
      tbody.appendChild(tr);
    });
  } catch (error) {
    toastError(error);
    renderTableMessage(tbody, 9, "加载失败");
  }
}

// ---------------------------------------------------------------------------
// 3. Users & grants
// ---------------------------------------------------------------------------
async function loadUsers() {
  const tbody = $("users-table-body");
  renderTableMessage(tbody, 9, "加载中...");
  try {
    const params = {};
    const gid = String($("user-filter-group")?.value || "").trim();
    const uid = String($("user-filter-qq")?.value || "").trim();
    const status = String($("user-filter-status")?.value || "");
    if (gid) params.group_id = gid;
    if (uid) params.user_id = uid;
    if (status) params.status = status;

    const res = await apiCall("users", "GET", null, params);
    const data = res.data || {};
    const grants = Array.isArray(data.grants) ? data.grants : [];
    if (grants.length === 0) {
      renderTableMessage(tbody, 9, "未找到用户授权记录");
      return;
    }
    tbody.replaceChildren();
    grants.forEach((grant) => {
      const tr = document.createElement("tr");
      tr.appendChild(cell(grant.id, "font-mono"));
      tr.appendChild(cell(grant.group_id));
      tr.appendChild(cell(grant.user_id, "font-strong"));
      tr.appendChild(cell(statusTag(grant.status)));
      tr.appendChild(cell(`LV${grant.last_verified_level}`));
      tr.appendChild(cell(Number(grant.expires_at) === 0 ? "永久" : formatDate(grant.expires_at)));
      tr.appendChild(cell(grant.approved_by || "-"));
      tr.appendChild(cell(formatDate(grant.last_verified_at)));
      const actions = document.createElement("td");
      if (String(grant.status || "").toUpperCase() === "ACTIVE") {
        actions.appendChild(
          actionButton(
            "撤销",
            "btn-danger",
            () => revokeGrant(grant),
            true
          )
        );
        actions.appendChild(document.createTextNode(" "));
      }
      actions.appendChild(actionButton("拉黑", "btn-secondary requires-bot", () => banUser(grant), true));
      tr.appendChild(actions);
      tbody.appendChild(tr);
    });
    syncActionAvailability();
  } catch (error) {
    toastError(error);
    renderTableMessage(tbody, 9, "加载失败");
  }
}

async function revokeGrant(grant) {
  const botId = requireWriteBot();
  if (!botId) return;
  const confirmed = window.confirm(`确定撤销 QQ ${grant.user_id} 在群 ${grant.group_id} 的授权吗？`);
  if (!confirmed) return;
  try {
    const res = await apiCall("users/revoke", "POST", {
      bot_self_id: botId,
      group_id: grant.group_id,
      user_id: grant.user_id,
      reason: "管理员手动撤销",
    });
    toast(res.message || "操作完成", "success");
    loadUsers();
  } catch (error) {
    toastError(error);
  }
}

async function banUser(grant) {
  const botId = requireWriteBot();
  if (!botId) return;
  const reason = window.prompt(`输入将 QQ ${grant.user_id} 加入黑名单的原因：`, "违规使用");
  if (reason === null) return;
  try {
    const res = await apiCall("users/ban", "POST", {
      bot_self_id: botId,
      group_id: grant.group_id,
      user_id: grant.user_id,
      reason: reason || "违反规则",
    });
    toast(res.message || "已加入黑名单", "success");
    loadUsers();
  } catch (error) {
    toastError(error);
  }
}

// ---------------------------------------------------------------------------
// 4. Group policies
// ---------------------------------------------------------------------------
async function loadGroups() {
  const tbody = $("groups-table-body");
  renderTableMessage(tbody, 9, "加载中...");
  try {
    const res = await apiCall("groups");
    const data = res.data || {};
    state.groupPolicies = Array.isArray(data.groups) ? data.groups : [];
    if (state.groupPolicies.length === 0) {
      renderTableMessage(tbody, 9, "尚未配置专属群策略");
      return;
    }
    tbody.replaceChildren();
    state.groupPolicies.forEach((policy, index) => {
      const tr = document.createElement("tr");
      tr.appendChild(cell(policy.group_id, "font-strong"));
      tr.appendChild(cell(policy.enabled ? el("span", { className: "tag tag-active", text: "开启" }) : el("span", { className: "tag tag-expired", text: "关闭" })));
      tr.appendChild(cell(`LV${policy.min_level}`));
      tr.appendChild(cell(policy.approval_required ? "是" : "否"));
      const durationSeconds = Number(policy.grant_duration_seconds);
      tr.appendChild(cell(durationSeconds === 0 ? "永久" : `${durationSeconds / 86400} 天`));
      tr.appendChild(cell(`${policy.recheck_interval_seconds}s`));
      tr.appendChild(cell(policy.restore_policy === "automatic" ? "自动重新激活" : "手动重新审批"));
      tr.appendChild(cell(policy.group_admin_bypass ? "豁免" : "不豁免"));
      const actions = document.createElement("td");
      actions.appendChild(
        actionButton(
          "编辑",
          "btn-secondary",
          () => openGroupModal(index),
          true
        )
      );
      tr.appendChild(actions);
      tbody.appendChild(tr);
    });
    syncActionAvailability();
  } catch (error) {
    toastError(error);
    renderTableMessage(tbody, 9, "加载失败");
  }
}

function openGroupModal(index) {
  const policy = state.groupPolicies[index];
  if (!policy) return;
  if (!requireWriteBot()) return;
  const set = (id, value) => {
    const node = $(id);
    if (node) node.value = value === undefined || value === null ? "" : String(value);
  };
  set("group-form-id", policy.group_id);
  set("group-form-enabled", policy.enabled ? "true" : "false");
  set("group-form-level", policy.min_level);
  set("group-form-approval", policy.approval_required ? "true" : "false");
  set("group-form-duration", Number(policy.grant_duration_seconds) / 86400);
  set("group-form-recheck", policy.recheck_interval_seconds);
  set("group-form-restore", policy.restore_policy);
  set("group-form-admin-bypass", policy.group_admin_bypass ? "true" : "false");
  $("modal-group")?.classList.add("show");
}

// ---------------------------------------------------------------------------
// 5. Audit logs + real file export
// ---------------------------------------------------------------------------
async function loadAuditLogs() {
  const tbody = $("audit-table-body");
  renderTableMessage(tbody, 8, "加载中...");
  try {
    const params = {};
    const gid = String($("audit-filter-group")?.value || "").trim();
    const uid = String($("audit-filter-user")?.value || "").trim();
    const action = String($("audit-filter-action")?.value || "");
    if (gid) params.group_id = gid;
    if (uid) params.user_id = uid;
    if (action) params.action = action;

    const res = await apiCall("audit_logs", "GET", null, params);
    const data = res.data || {};
    const logs = Array.isArray(data.audit_logs) ? data.audit_logs : [];
    if (logs.length === 0) {
      renderTableMessage(tbody, 8, "无符合条件的审计日志");
      return;
    }
    tbody.replaceChildren();
    logs.forEach((log) => {
      const tr = document.createElement("tr");
      tr.appendChild(cell(formatDate(log.timestamp)));
      tr.appendChild(cell(log.actor_id, "font-strong"));
      tr.appendChild(cell(log.actor_type || "-"));
      tr.appendChild(cell(log.group_id || "-"));
      tr.appendChild(cell(log.target_user_id || "-"));
      tr.appendChild(cell(log.action || "-", "font-mono"));
      tr.appendChild(
        cell(
          log.old_state
            ? `${log.old_state} ➔ ${log.new_state}`
            : log.new_state || "-"
        )
      );
      tr.appendChild(cell(log.details_json || "{}"));
      tbody.appendChild(tr);
    });
  } catch (error) {
    toastError(error);
    renderTableMessage(tbody, 8, "加载失败");
  }
}

function buildExportFilename(format) {
  const now = new Date();
  const pad = (value) => String(value).padStart(2, "0");
  const stamp =
    `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}` +
    `_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  return `audit_logs_${stamp}.${format === "csv" ? "csv" : "json"}`;
}

function triggerBlobDownload(content, mime, filename) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.style.display = "none";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

async function exportAudit(format) {
  const normalized = format === "csv" ? "csv" : "json";
  const mime = normalized === "csv" ? "text/csv;charset=utf-8" : "application/json";
  const params = { format: normalized };
  const gid = String($("audit-filter-group")?.value || "").trim();
  const action = String($("audit-filter-action")?.value || "");
  const botId = getSelectedBotId();
  if (gid) params.group_id = gid;
  if (action) params.action = action;
  if (botId) params.bot_self_id = botId;

  try {
    const res = await apiCall("audit_logs/export", "GET", null, params);
    const payload = res.data;
    let content;
    if (typeof payload === "string") {
      content = payload;
    } else if (payload !== undefined && payload !== null) {
      content = JSON.stringify(payload, null, 2);
    } else {
      throw new Error("导出接口返回为空");
    }
    triggerBlobDownload(content, mime, buildExportFilename(normalized));
    toast(`已导出 ${normalized.toUpperCase()} 文件`, "success");
  } catch (error) {
    toastError(error);
  }
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
async function waitForBridgeReady() {
  if (!BRIDGE || typeof BRIDGE.ready !== "function") return null;
  try {
    return await Promise.race([
      BRIDGE.ready(),
      new Promise((resolve) => window.setTimeout(() => resolve(null), BRIDGE_READY_TIMEOUT_MS)),
    ]);
  } catch (error) {
    console.warn("Bridge ready notice:", error);
    return null;
  }
}

async function initApp() {
  initTabs();
  initModals();
  initFilters();
  syncActionAvailability();

  await waitForBridgeReady();
  await loadBots();
  await Promise.allSettled([loadDashboard(), loadDiagnostics()]);
}

document.addEventListener("DOMContentLoaded", () => {
  initApp().catch((error) => toastError(error));
});

// Expose a tiny read-only surface for debugging without leaking internals.
window.GroupApprovalDashboard = Object.freeze({
  apiRoutePrefix: API_ROUTE_PREFIX,
  isEmbedded: IS_EMBEDDED,
  hasBridge: Boolean(BRIDGE),
  timeoutMs: REQUEST_TIMEOUT_MS,
});
