const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");

function dashboard() {
  const window = {};
  window.parent = window;
  const inputs = new Map();
  const document = {
    addEventListener() {},
    getElementById(id) {
      if (!inputs.has(id)) inputs.set(id, { value: "", checked: false });
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
      calls.push({ path, method, ...payload });
      return {};
    };
  `, context);
  return { calls, run: (code) => vm.runInContext(code, context) };
}

test("review and grant actions keep the row's Bot and scope", async () => {
  const { calls, run } = dashboard();
  await run(`(async () => {
    const application = { id: "L123456789abc", bot_self_id: "20002" };
    await reviewApplication(application, "approve");
    await reviewApplication(application, "reject");
    await changeGrant({ bot_self_id: "20002", user_id: "30001", scope: "PRIVATE" }, "revoke");
  })()`);
  await assert.rejects(run(`changeGrant({ bot_self_id: "99999", scope: "GROUP" }, "revoke")`), /已核验/);
  assert.deepEqual(calls.map(c => [c.path, c.action, c.bot_self_id]), [
    ["scope/action", "approve", "20002"],
    ["scope/action", "reject", "20002"],
    ["scope/action", "revoke", "20002"],
  ]);
  assert.equal(calls[2].scope, "PRIVATE");
});

test("policy save stays bound to the Bot whose settings were loaded", async () => {
  const { calls, run } = dashboard();
  await run(`(async () => {
    $("global-bot-select").value = "20002";
    state.settingsBot = "20002";
    $("level-PRIVATE").value = "20";
    $("approval-GROUP").checked = true;
    $("global-bot-select").value = "10001";
    await saveSettings();
  })()`);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].bot_self_id, "20002");
  assert.equal(calls[0].private.min_level, "20");
  assert.equal(calls[0].group.approval_required, true);
});
