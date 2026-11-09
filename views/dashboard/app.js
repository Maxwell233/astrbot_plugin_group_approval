/**
 * Frontend script for AstrBot Group Approval WebUI dashboard.
 */

const PLUGIN_NAME = "astrbot_plugin_group_approval";

// Bridge SDK resolution with graceful HTTP fallback
const bridge = window.AstrBotPluginPage || window.AstrBotPluginView;

async function apiCall(endpoint, method = "GET", payload = null) {
  if (bridge && typeof bridge.ready === "function") {
    try {
      if (method.toUpperCase() === "GET") {
        return await bridge.apiGet(endpoint);
      } else {
        return await bridge.apiPost(endpoint, payload || {});
      }
    } catch (e) {
      console.warn("Bridge SDK call failed, falling back to fetch:", e);
    }
  }

  // Standard fetch fallback
  const url = `/api/plug/${PLUGIN_NAME}/${endpoint}`;
  const options = {
    method,
    headers: { "Content-Type": "application/json" },
  };
  if (payload && method.toUpperCase() !== "GET") {
    options.body = JSON.stringify(payload);
  }

  const res = await fetch(url, options);
  return await res.json();
}

function formatDate(ts) {
  if (!ts) return "-";
  const d = new Date(ts * 1000);
  return d.toLocaleString("zh-CN", { hour12: false });
}

function renderStatusTag(status) {
  const s = String(status || "").toUpperCase();
  const map = {
    PENDING: '<span class="tag tag-pending">待审批</span>',
    APPROVED: '<span class="tag tag-active">已批准</span>',
    ACTIVE: '<span class="tag tag-active">正常使用</span>',
    REJECTED: '<span class="tag tag-rejected">已拒绝</span>',
    REVOKED: '<span class="tag tag-revoked">已撤销</span>',
    SUSPENDED: '<span class="tag tag-suspended">已暂停</span>',
    CANCELLED: '<span class="tag tag-expired">已取消</span>',
    EXPIRED: '<span class="tag tag-expired">已过期</span>',
  };
  return map[s] || `<span class="tag">${s}</span>`;
}

// State
let currentAction = null; // { type: 'approve'|'reject', id: '...' }

// Initialize
document.addEventListener("DOMContentLoaded", async () => {
  if (bridge && typeof bridge.ready === "function") {
    try {
      await bridge.ready();
    } catch (err) {
      console.log("Bridge ready notice:", err);
    }
  }

  initTabs();
  initModals();
  initFilters();

  await loadDashboard();
});

function initTabs() {
  const tabs = document.querySelectorAll(".nav-tab");
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      tabs.forEach((t) => t.classList.remove("active"));
      document.querySelectorAll(".tab-pane").forEach((p) => p.classList.remove("active"));

      tab.classList.add("active");
      const targetPane = document.getElementById(tab.dataset.tab);
      if (targetPane) targetPane.classList.add("active");

      // Load data for specific tab
      if (tab.dataset.tab === "tab-overview") loadDashboard();
      if (tab.dataset.tab === "tab-applications") loadApplications();
      if (tab.dataset.tab === "tab-users") loadUsers();
      if (tab.dataset.tab === "tab-groups") loadGroups();
      if (tab.dataset.tab === "tab-audit") loadAuditLogs();
    });
  });

  document.getElementById("btn-refresh").addEventListener("click", () => {
    const activeTab = document.querySelector(".nav-tab.active");
    if (activeTab) activeTab.click();
  });
}

function initFilters() {
  document.getElementById("btn-filter-apps")?.addEventListener("click", loadApplications);
  document.getElementById("btn-filter-users")?.addEventListener("click", loadUsers);
  document.getElementById("btn-filter-audit")?.addEventListener("click", loadAuditLogs);

  document.getElementById("btn-export-audit-json")?.addEventListener("click", () => exportAudit("json"));
  document.getElementById("btn-export-audit-csv")?.addEventListener("click", () => exportAudit("csv"));
}

function initModals() {
  const modalAction = document.getElementById("modal-action");
  const modalClose = document.getElementById("modal-close");
  const modalCancel = document.getElementById("modal-btn-cancel");
  const modalConfirm = document.getElementById("modal-btn-confirm");

  const closeModal = () => modalAction.classList.remove("show");
  modalClose?.addEventListener("click", closeModal);
  modalCancel?.addEventListener("click", closeModal);

  modalConfirm?.addEventListener("click", async () => {
    if (!currentAction) return;
    const note = document.getElementById("modal-input-note").value;

    if (currentAction.type === "approve") {
      const res = await apiCall("applications/approve", "POST", {
        application_id: currentAction.id,
        note: note || "WebUI管理员批准",
      });
      alert(res.message || (res.status === "ok" ? "批准成功" : "批准失败"));
    } else if (currentAction.type === "reject") {
      const res = await apiCall("applications/reject", "POST", {
        application_id: currentAction.id,
        reason: note || "WebUI管理员拒绝",
      });
      alert(res.message || (res.status === "ok" ? "拒绝成功" : "拒绝失败"));
    }
    closeModal();
    loadApplications();
    loadDashboard();
  });

  // Group Policy Modal
  const modalGroup = document.getElementById("modal-group");
  const groupClose = document.getElementById("modal-group-close");
  const groupCancel = document.getElementById("modal-group-cancel");
  const groupSave = document.getElementById("modal-group-save");

  const closeGroupModal = () => modalGroup.classList.remove("show");
  groupClose?.addEventListener("click", closeGroupModal);
  groupCancel?.addEventListener("click", closeGroupModal);

  document.getElementById("btn-open-add-group")?.addEventListener("click", () => {
    document.getElementById("group-form-id").value = "";
    document.getElementById("group-form-level").value = "10";
    modalGroup.classList.add("show");
  });

  groupSave?.addEventListener("click", async () => {
    const payload = {
      group_id: document.getElementById("group-form-id").value.trim(),
      enabled: document.getElementById("group-form-enabled").value === "true",
      min_level: parseInt(document.getElementById("group-form-level").value, 10),
      approval_required: document.getElementById("group-form-approval").value === "true",
      grant_duration_days: parseInt(document.getElementById("group-form-duration").value, 10),
      recheck_interval_seconds: parseInt(document.getElementById("group-form-recheck").value, 10),
      restore_policy: document.getElementById("group-form-restore").value,
      group_admin_bypass: document.getElementById("group-form-admin-bypass").value === "true",
    };

    if (!payload.group_id) {
      alert("请输入有效的 QQ 群号！");
      return;
    }

    const res = await apiCall("groups/update", "POST", payload);
    if (res.status === "ok") {
      alert("群策略保存成功！");
      closeGroupModal();
      loadGroups();
    } else {
      alert(`保存失败: ${res.message}`);
    }
  });
}

// 1. Dashboard
async function loadDashboard() {
  try {
    const res = await apiCall("dashboard");
    if (!res || res.status !== "ok") return;

    const stats = res.data.stats || {};
    document.getElementById("metric-groups").textContent = stats.enabled_groups || 0;
    document.getElementById("metric-pending").textContent = stats.pending_applications || 0;
    document.getElementById("metric-active").textContent = stats.active_grants || 0;
    document.getElementById("metric-suspended").textContent = stats.suspended_grants || 0;
    document.getElementById("metric-blacklist").textContent = stats.blacklisted_users || 0;

    document.getElementById("nav-pending-badge").textContent = stats.pending_applications || 0;

    const tbody = document.getElementById("overview-recent-table");
    const apps = res.data.recent_applications || [];
    if (apps.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted">暂无申请记录</td></tr>';
      return;
    }

    tbody.innerHTML = apps
      .map(
        (a) => `
      <tr>
        <td><strong>${a.id}</strong></td>
        <td>${a.group_id}</td>
        <td>${a.user_id}</td>
        <td>LV${a.level}</td>
        <td>${formatDate(a.created_at)}</td>
        <td>${renderStatusTag(a.status)}</td>
        <td>
          ${
            a.status === "PENDING"
              ? `<button class="btn btn-sm btn-success btn-action-approve" data-id="${a.id}">批准</button>
                 <button class="btn btn-sm btn-danger btn-action-reject" data-id="${a.id}">拒绝</button>`
              : "-"
          }
        </td>
      </tr>
    `
      )
      .join("");

    bindActionButtons();
  } catch (err) {
    console.error("loadDashboard error:", err);
  }
}

// 2. Applications
async function loadApplications() {
  const gid = document.getElementById("app-filter-group").value.trim();
  const uid = document.getElementById("app-filter-user").value.trim();
  const status = document.getElementById("app-filter-status").value;

  const tbody = document.getElementById("applications-table-body");
  tbody.innerHTML = '<tr><td colspan="9" class="text-center text-muted">加载中...</td></tr>';

  try {
    const query = new URLSearchParams();
    if (gid) query.append("group_id", gid);
    if (uid) query.append("user_id", uid);
    if (status) query.append("status", status);

    const res = await apiCall(`applications?${query.toString()}`);
    if (!res || res.status !== "ok") {
      tbody.innerHTML = `<tr><td colspan="9" class="text-center text-muted">加载失败: ${res?.message}</td></tr>`;
      return;
    }

    const apps = res.data.applications || [];
    if (apps.length === 0) {
      tbody.innerHTML = '<tr><td colspan="9" class="text-center text-muted">无符合条件的申请记录</td></tr>';
      return;
    }

    tbody.innerHTML = apps
      .map(
        (a) => `
      <tr>
        <td><strong>${a.id}</strong></td>
        <td>${a.group_id}</td>
        <td>${a.user_id}</td>
        <td>LV${a.level_at_application}</td>
        <td>${a.reason || "-"}</td>
        <td>${formatDate(a.created_at)}</td>
        <td>${renderStatusTag(a.status)}</td>
        <td>${a.review_note || "-"}</td>
        <td>
          ${
            a.status === "PENDING"
              ? `<button class="btn btn-sm btn-success btn-action-approve" data-id="${a.id}">批准</button>
                 <button class="btn btn-sm btn-danger btn-action-reject" data-id="${a.id}">拒绝</button>`
              : `<span class="text-muted">已处理</span>`
          }
        </td>
      </tr>
    `
      )
      .join("");

    bindActionButtons();
  } catch (err) {
    console.error("loadApplications error:", err);
  }
}

function bindActionButtons() {
  document.querySelectorAll(".btn-action-approve").forEach((btn) => {
    btn.addEventListener("click", () => {
      currentAction = { type: "approve", id: btn.dataset.id };
      document.getElementById("modal-title").textContent = `批准申请：${btn.dataset.id}`;
      document.getElementById("modal-input-label").textContent = "审批备注：";
      document.getElementById("modal-input-note").value = "符合群等级要求，准予授权";
      document.getElementById("modal-action").classList.add("show");
    });
  });

  document.querySelectorAll(".btn-action-reject").forEach((btn) => {
    btn.addEventListener("click", () => {
      currentAction = { type: "reject", id: btn.dataset.id };
      document.getElementById("modal-title").textContent = `拒绝申请：${btn.dataset.id}`;
      document.getElementById("modal-input-label").textContent = "拒绝原因：";
      document.getElementById("modal-input-note").value = "不符合本群准入要求";
      document.getElementById("modal-action").classList.add("show");
    });
  });
}

// 3. Users & Grants
async function loadUsers() {
  const gid = document.getElementById("user-filter-group").value.trim();
  const uid = document.getElementById("user-filter-qq").value.trim();
  const status = document.getElementById("user-filter-status").value;

  const tbody = document.getElementById("users-table-body");
  tbody.innerHTML = '<tr><td colspan="9" class="text-center text-muted">加载中...</td></tr>';

  try {
    const query = new URLSearchParams();
    if (gid) query.append("group_id", gid);
    if (uid) query.append("user_id", uid);
    if (status) query.append("status", status);

    const res = await apiCall(`users?${query.toString()}`);
    if (!res || res.status !== "ok") {
      tbody.innerHTML = `<tr><td colspan="9" class="text-center text-muted">加载失败: ${res?.message}</td></tr>`;
      return;
    }

    const grants = res.data.grants || [];
    if (grants.length === 0) {
      tbody.innerHTML = '<tr><td colspan="9" class="text-center text-muted">未找到用户授权记录</td></tr>';
      return;
    }

    tbody.innerHTML = grants
      .map(
        (g) => `
      <tr>
        <td><code>${g.id}</code></td>
        <td>${g.group_id}</td>
        <td><strong>${g.user_id}</strong></td>
        <td>${renderStatusTag(g.status)}</td>
        <td>LV${g.last_verified_level}</td>
        <td>${g.expires_at === 0 ? "永久" : formatDate(g.expires_at)}</td>
        <td>${g.approved_by || "-"}</td>
        <td>${formatDate(g.last_verified_at)}</td>
        <td>
          ${
            g.status === "ACTIVE"
              ? `<button class="btn btn-sm btn-danger btn-revoke" data-gid="${g.group_id}" data-uid="${g.user_id}">撤销</button>`
              : ""
          }
          <button class="btn btn-sm btn-secondary btn-ban" data-gid="${g.group_id}" data-uid="${g.user_id}">拉黑</button>
        </td>
      </tr>
    `
      )
      .join("");

    document.querySelectorAll(".btn-revoke").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!confirm(`确定撤销 QQ ${btn.dataset.uid} 在群 ${btn.dataset.gid} 的授权吗？`)) return;
        const res = await apiCall("users/revoke", "POST", {
          group_id: btn.dataset.gid,
          user_id: btn.dataset.uid,
          reason: "WebUI手动撤销",
        });
        alert(res.message || "操作完成");
        loadUsers();
      });
    });

    document.querySelectorAll(".btn-ban").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const reason = prompt(`输入将 QQ ${btn.dataset.uid} 加入黑名单的原因：`, "违规使用");
        if (reason === null) return;
        const res = await apiCall("users/ban", "POST", {
          group_id: btn.dataset.gid,
          user_id: btn.dataset.uid,
          reason: reason || "违反规则",
        });
        alert(res.message || "已加入黑名单");
        loadUsers();
      });
    });
  } catch (err) {
    console.error("loadUsers error:", err);
  }
}

// 4. Groups
async function loadGroups() {
  const tbody = document.getElementById("groups-table-body");
  tbody.innerHTML = '<tr><td colspan="9" class="text-center text-muted">加载中...</td></tr>';

  try {
    const res = await apiCall("groups");
    if (!res || res.status !== "ok") {
      tbody.innerHTML = `<tr><td colspan="9" class="text-center text-muted">加载失败: ${res?.message}</td></tr>`;
      return;
    }

    const groups = res.data.groups || [];
    if (groups.length === 0) {
      tbody.innerHTML = '<tr><td colspan="9" class="text-center text-muted">尚未配置专属群策略</td></tr>';
      return;
    }

    tbody.innerHTML = groups
      .map(
        (g) => `
      <tr>
        <td><strong>${g.group_id}</strong></td>
        <td>${g.enabled ? '<span class="tag tag-active">开启</span>' : '<span class="tag tag-expired">关闭</span>'}</td>
        <td><strong style="color: var(--primary)">LV${g.min_level}</strong></td>
        <td>${g.approval_required ? "是" : "否"}</td>
        <td>${g.grant_duration_seconds === 0 ? "永久" : g.grant_duration_seconds / 86400 + " 天"}</td>
        <td>${g.recheck_interval_seconds}s</td>
        <td>${g.restore_policy === "automatic" ? "自动重新激活" : "手动重新审批"}</td>
        <td>${g.group_admin_bypass ? "豁免" : "不豁免"}</td>
        <td>
          <button class="btn btn-sm btn-secondary btn-edit-group" data-group='${JSON.stringify(g)}'>编辑</button>
        </td>
      </tr>
    `
      )
      .join("");

    document.querySelectorAll(".btn-edit-group").forEach((btn) => {
      btn.addEventListener("click", () => {
        const g = JSON.parse(btn.dataset.group);
        document.getElementById("group-form-id").value = g.group_id;
        document.getElementById("group-form-enabled").value = g.enabled ? "true" : "false";
        document.getElementById("group-form-level").value = g.min_level;
        document.getElementById("group-form-approval").value = g.approval_required ? "true" : "false";
        document.getElementById("group-form-duration").value = g.grant_duration_seconds / 86400;
        document.getElementById("group-form-recheck").value = g.recheck_interval_seconds;
        document.getElementById("group-form-restore").value = g.restore_policy;
        document.getElementById("group-form-admin-bypass").value = g.group_admin_bypass ? "true" : "false";
        document.getElementById("modal-group").classList.add("show");
      });
    });
  } catch (err) {
    console.error("loadGroups error:", err);
  }
}

// 5. Audit Logs
async function loadAuditLogs() {
  const gid = document.getElementById("audit-filter-group").value.trim();
  const uid = document.getElementById("audit-filter-user").value.trim();
  const action = document.getElementById("audit-filter-action").value;

  const tbody = document.getElementById("audit-table-body");
  tbody.innerHTML = '<tr><td colspan="8" class="text-center text-muted">加载中...</td></tr>';

  try {
    const query = new URLSearchParams();
    if (gid) query.append("group_id", gid);
    if (uid) query.append("user_id", uid);
    if (action) query.append("action", action);

    const res = await apiCall(`audit_logs?${query.toString()}`);
    if (!res || res.status !== "ok") {
      tbody.innerHTML = `<tr><td colspan="8" class="text-center text-muted">加载失败: ${res?.message}</td></tr>`;
      return;
    }

    const logs = res.data.audit_logs || [];
    if (logs.length === 0) {
      tbody.innerHTML = '<tr><td colspan="8" class="text-center text-muted">无符合条件的审计日志</td></tr>';
      return;
    }

    tbody.innerHTML = logs
      .map(
        (l) => `
      <tr>
        <td>${formatDate(l.timestamp)}</td>
        <td><strong>${l.actor_id}</strong></td>
        <td><span class="tag">${l.actor_type}</span></td>
        <td>${l.group_id || "-"}</td>
        <td>${l.target_user_id || "-"}</td>
        <td><code>${l.action}</code></td>
        <td>${l.old_state ? `${l.old_state} ➔ ${l.new_state}` : l.new_state || "-"}</td>
        <td><small style="color:var(--text-muted)">${l.details_json || "{}"}</small></td>
      </tr>
    `
      )
      .join("");
  } catch (err) {
    console.error("loadAuditLogs error:", err);
  }
}

function exportAudit(format) {
  window.open(`/api/plug/${PLUGIN_NAME}/audit_logs/export?format=${format}`, "_blank");
}
