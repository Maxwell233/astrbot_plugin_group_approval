const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");

function dashboard() {
  const calls = [];
  const window = {
    parent: {},
    AstrBotPluginView: {
      async apiPost(path, payload) {
        calls.push({ path, ...payload });
        return { message: "已保存" };
      },
    },
  };
  const inputs = new Map();
  const document = {
    addEventListener() {},
    getElementById(id) {
      if (!inputs.has(id)) inputs.set(id, { value: "", checked: false });
      return inputs.get(id);
    },
  };
  const context = vm.createContext({ window, document });
  vm.runInContext(fs.readFileSync("views/dashboard/app.js", "utf8"), context);
  vm.runInContext(`
    state.verifiedBots = ["10001", "20002"];
    $("global-bot-select").value = "10001";
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

test("rejection works in the Dashboard iframe and keeps the row's Bot", async () => {
  const { calls, run } = dashboard();
  run(`
    const dialog = $("reject-dialog");
    dialog.showModal = () => {};
    dialog.addEventListener = (name, callback) => { dialog.onClose = callback; };
    const application = { id: "L123456789abc", bot_self_id: "20002" };
    function closeRejection(value) {
      dialog.returnValue = value;
      dialog.onClose();
    }
  `);
  const cancelled = run('rejectApplication(application)');
  run('closeRejection("cancel")');
  await cancelled;
  assert.equal(calls.length, 0);

  const confirmed = run('rejectApplication(application)');
  run('$("reject-note").value = "资格不符"; closeRejection("reject")');
  await confirmed;
  assert.deepEqual(calls.map(c => [c.action, c.bot_self_id, c.application_id, c.note]), [
    ["reject", "20002", "L123456789abc", "资格不符"],
  ]);
});
