const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");

function dashboard() {
  const window = { confirm: () => true, prompt: () => "test" };
  window.parent = window;
  const inputs = new Map();
  const document = {
    addEventListener() {},
    querySelectorAll: () => [],
    getElementById(id) {
      if (!inputs.has(id)) {
        inputs.set(id, {
          value: "",
          classList: { add() {}, remove() {} },
          addEventListener(type, fn) { this[type] = fn; },
        });
      }
      return inputs.get(id);
    },
  };
  const calls = [];
  const context = vm.createContext({ window, document, calls });
  vm.runInContext(fs.readFileSync("views/dashboard/app.js", "utf8"), context);
  vm.runInContext(`
    state.verifiedBots = ["10001", "20002"];
    $("global-bot-select").value = "10001";
    apiCall = async (path, method, payload) => {
      calls.push({ path, method, bot: payload.bot_self_id });
      return {};
    };
    toast = () => {};
    toastError = (error) => { throw error; };
    loadApplications = loadDashboard = loadUsers = loadGroups = async () => {};
    initModals();
  `, context);
  return { calls, run: (code) => vm.runInContext(code, context) };
}

test("review and grant actions keep the row's Bot identity", async () => {
  const { calls, run } = dashboard();
  await run(`(async () => {
    const application = { id: "A1", bot_self_id: "20002" };
    openApprovalModal("approve", application);
    await $("modal-btn-confirm").click();
    openApprovalModal("reject", application);
    await $("modal-btn-confirm").click();
    await revokeGrant({ bot_self_id: "20002", user_id: "30001", group_id: "40001" });
    await banUser({ bot_self_id: "99999", user_id: "30001", group_id: "40001" });
  })()`);
  assert.deepEqual(calls.map(({ path, bot }) => [path, bot]), [
    ["applications/approve", "20002"],
    ["applications/reject", "20002"],
    ["users/revoke", "20002"],
  ]);
});

test("editing a policy keeps its Bot when the dashboard selection changes", async () => {
  const { calls, run } = dashboard();
  await run(`(async () => {
    state.groupPolicies = [{ bot_self_id: "20002", group_id: "40001", enabled: true,
      min_level: 10, grant_duration_seconds: 0, recheck_interval_seconds: 30,
      restore_policy: "automatic" }];
    $("global-bot-select").value = "20002";
    openGroupModal(0);
    $("global-bot-select").value = "10001";
    await $("modal-group-save").click();
  })()`);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].bot, "20002");
});
