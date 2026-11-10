// AstrBot 4.29.4 serves this view in a sandboxed Dashboard iframe.
const bridge = window.AstrBotPluginView;

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

function renderTableMessage(tbody, colspan, message) {
  const tr = document.createElement("tr");
  const td = el("td", { text: message, className: "text-center text-muted" });
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

function toast(message, type) {
  const node = el("div", { className: `toast toast-${type}`, text: message });
  $("toast-container").appendChild(node);
  window.setTimeout(() => {
    node.classList.add("toast-hide");
    window.setTimeout(() => node.remove(), 300);
  }, 4200);
}

function toastError(error) {
  const message = error instanceof Error ? error.message : String(error || "操作失败");
  toast(message, "error");
}

function getSelectedBotId() {
  const value = $("global-bot-select").value;
  return state.verifiedBots.includes(value) ? value : "";
}

async function mutate(bot, action, payload = {}) {
  if (!state.verifiedBots.includes(String(bot))) {
    throw new Error("请选择已核验的 Bot。");
  }
  const { message } = await bridge.apiPost("scope/action", { ...payload, bot_self_id: bot, action });
  return message;
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
    if (message) {
      toast(message, "success");
      await loadState();
    }
  } catch (error) {
    toastError(error);
  } finally {
    node.disabled = false;
  }
}

async function reviewApplication(row, action, note = "") {
  return mutate(row.bot_self_id, action, { application_id: row.id, note });
}

async function rejectApplication(row) {
  const dialog = $("reject-dialog");
  const note = $("reject-note");
  dialog.returnValue = "";
  note.value = "";
  dialog.showModal();
  await new Promise(resolve => dialog.addEventListener("close", resolve, { once: true }));
  if (dialog.returnValue === "reject") {
    return reviewApplication(row, "reject", note.value);
  }
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
    for (const column of columns) tr.appendChild(el("td", { text: column(row) }));
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
  const data = await bridge.apiGet("bots");
  state.verifiedBots = data.verified_bots;
  const select = $("global-bot-select");
  const previous = select.value;
  select.replaceChildren(el("option", { text: "选择 QQ Bot", attrs: { value: "" } }));
  for (const bot of state.verifiedBots) {
    select.appendChild(el("option", { text: bot, attrs: { value: bot } }));
  }
  select.value = state.verifiedBots.includes(previous) ? previous :
    (state.verifiedBots.length === 1 ? state.verifiedBots[0] : "");
  const status = data.guard_status;
  $("guard-status").textContent = data.guard_active ? "原生聊天准入已启用" :
    (status && status !== "INACTIVE" ? `原生聊天准入异常（${status}）` : "原生聊天准入未启用");
}

async function loadState() {
  const bot = getSelectedBotId();
  const seq = ++state.readSequence;
  $("bot-panel").hidden = true;
  state.settingsBot = "";
  if (!bot) return;
  const user = $("user-filter").value.trim();
  const data = await bridge.apiGet("scope/state", { bot_self_id: bot, user_id: user });
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
    button("拒绝", () => rejectApplication(row)),
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
  if (window.parent === window || !bridge) {
    $("guard-status").textContent = "请在 AstrBot Dashboard 中打开此页面。";
    return;
  }
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
