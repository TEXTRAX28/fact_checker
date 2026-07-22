import assert from "node:assert/strict";
import test from "node:test";

test("Refresh captures a newly opened active tab and replies after storage is ready", async () => {
  let messageListener;
  const stored = [];
  const queries = [];
  let scriptCall = 0;
  globalThis.chrome = {
    runtime: {
      onInstalled: { addListener() {} },
      onMessage: { addListener(listener) { messageListener = listener; } },
      sendMessage: async () => undefined,
    },
    action: { onClicked: { addListener() {} } },
    sidePanel: {
      setPanelBehavior: async () => undefined,
      open: async () => undefined,
    },
    tabs: {
      async query(value) {
        queries.push(value);
        return [{ id: 73, url: "https://news.example/new", title: "New article" }];
      },
    },
    scripting: {
      async executeScript() {
        scriptCall += 1;
        if (scriptCall === 1) return [];
        return [{ result: {
          title: "New article",
          text: "This is newly captured article text with enough characters.",
          characterCount: 59,
        } }];
      },
    },
    storage: {
      session: { async set(value) { stored.push(value); } },
    },
  };

  try {
    await import(`../service-worker.js?test=${Date.now()}`);
    assert.equal(typeof messageListener, "function");
    const response = await new Promise((resolve) => {
      const keepChannelOpen = messageListener(
        { type: "REFRESH_PAGE_CAPTURE" }, {}, resolve,
      );
      assert.equal(keepChannelOpen, true);
    });

    assert.deepEqual(queries, [{ active: true, lastFocusedWindow: true }]);
    assert.deepEqual(response, { ok: true, tabId: 73 });
    assert.equal(stored[0]["fc:activeTabId"], 73);
    assert.equal(stored[0]["fc:page:73"].status, "collecting");
    assert.equal(stored[1]["fc:page:73"].status, "ready");
    assert.equal(stored[1]["fc:page:73"].url, "https://news.example/new");
  } finally {
    delete globalThis.chrome;
  }
});
