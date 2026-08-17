/**
 * Service worker.
 *
 * It does exactly two things, and it is worth being precise about which:
 *
 *   1. Relays a message when you mark a lot handled. Cosmetic.
 *   2. Runs the price-lookup shopper — opens a retailer search page, lets
 *      `shopper.js` read it, closes the tab, reports back.
 *
 * What it emphatically does NOT do is touch Nellis. There is no alarm, no
 * poller and no scheduled task pointed at nellisauction.com; every bid is still
 * placed by hand. The shopper only ever visits Amazon, Walmart and Facebook
 * Marketplace, and only to read prices.
 *
 * Pacing lives on the engine side, not here. This worker asks "anything to do?"
 * and the engine answers "no, wait 3 minutes" — so making it faster means
 * changing settings deliberately, not editing a constant in a background
 * script. One tab at a time, always.
 *
 * Off by default. Nothing runs until you switch it on in the popup.
 */

const DEFAULTS = {
  apiBase: "http://127.0.0.1:8787",
  apiToken: "",
  shopperEnabled: false,
};

const POLL_ALARM = "nde-shopper-poll";
const TAB_TIMEOUT_MS = 75000;

chrome.runtime.onInstalled.addListener(() => {
  chrome.tabs.create({ url: chrome.runtime.getURL("queue.html") });
  chrome.alarms.create(POLL_ALARM, { periodInMinutes: 1 });
});

chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create(POLL_ALARM, { periodInMinutes: 1 });
});

async function config() {
  return { ...DEFAULTS, ...(await chrome.storage.sync.get(DEFAULTS)) };
}

async function api(path, { method = "GET", body } = {}) {
  const cfg = await config();
  const headers = { "Content-Type": "application/json" };
  if (cfg.apiToken) headers["X-API-Token"] = cfg.apiToken;
  const response = await fetch(`${cfg.apiBase.replace(/\/$/, "")}${path}`, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) throw new Error(`engine returned ${response.status}`);
  return response.json();
}

// State has to survive the worker being torn down between alarms, so it lives
// in storage rather than a module variable.
const state = {
  get: async () => (await chrome.storage.local.get({ active: null, nextPollAt: 0 })),
  set: (patch) => chrome.storage.local.set(patch),
};

async function closeTab(tabId) {
  if (!tabId) return;
  try {
    await chrome.tabs.remove(tabId);
  } catch {
    /* already gone */
  }
}

async function finish(patchStatus) {
  const { active } = await state.get();
  if (active) await closeTab(active.tabId);
  await state.set({ active: null, ...patchStatus });
}

/** Abandon a job whose tab never reported — a closed lid, a hung page. */
async function reapStale() {
  const { active } = await state.get();
  if (!active) return;
  if (Date.now() - active.startedAt < TAB_TIMEOUT_MS) return;

  await api(`/api/market/jobs/${active.job_id}/failed`, {
    method: "POST",
    body: { reason: "tab did not report back in time" },
  }).catch(() => {});
  await finish({});
}

async function poll() {
  const cfg = await config();
  if (!cfg.shopperEnabled) return;

  await reapStale();

  const { active, nextPollAt } = await state.get();
  if (active) return; // one at a time, always
  if (nextPollAt && Date.now() < nextPollAt) return;

  let lease;
  try {
    lease = await api("/api/market/jobs");
  } catch {
    // Engine not running. Back off rather than retrying every minute.
    await state.set({ nextPollAt: Date.now() + 5 * 60_000 });
    return;
  }

  if (!lease.job) {
    const wait = Math.max(30, lease.retry_after_seconds || 60) * 1000;
    await state.set({ nextPollAt: Date.now() + wait });
    return;
  }

  // Background tab: the search happens without stealing focus, and closes
  // itself when it's done.
  const tab = await chrome.tabs.create({ url: lease.job.url, active: false });
  await state.set({
    active: {
      job_id: lease.job.job_id,
      site: lease.job.site,
      tabId: tab.id,
      startedAt: Date.now(),
    },
    nextPollAt: 0,
  });
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === POLL_ALARM) poll();
});

// A tab the user closes mid-search shouldn't leave the job leased forever.
chrome.tabs.onRemoved.addListener(async (tabId) => {
  const { active } = await state.get();
  if (!active || active.tabId !== tabId) return;
  await api(`/api/market/jobs/${active.job_id}/failed`, {
    method: "POST",
    body: { reason: "tab closed before results were read" },
  }).catch(() => {});
  await state.set({ active: null });
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "nde-advance") {
    chrome.tabs.query({ url: chrome.runtime.getURL("queue.html") }, (tabs) => {
      tabs.forEach((tab) => chrome.tabs.reload(tab.id));
    });
    sendResponse({ ok: true });
    return false;
  }

  // The shopper content script asking "was this tab opened for a job?".
  // Any other tab — you, browsing — gets null and does nothing.
  if (message?.type === "nde-shopper-ready") {
    state.get().then(({ active }) => {
      if (active && active.tabId === sender.tab?.id) {
        sendResponse({ job_id: active.job_id, site: active.site });
      } else {
        sendResponse(null);
      }
    });
    return true;
  }

  if (message?.type === "nde-shopper-result") {
    api(`/api/market/jobs/${message.job_id}/result`, {
      method: "POST",
      body: { candidates: message.candidates || [] },
    })
      .catch(() => {})
      .finally(() => finish({}));
    sendResponse({ ok: true });
    return false;
  }

  if (message?.type === "nde-shopper-blocked") {
    // The site asked us to stop. Report it and stand down — the engine puts
    // that whole site to sleep for hours. Nothing here retries or works around
    // it.
    api(`/api/market/jobs/${message.job_id}/blocked`, {
      method: "POST",
      body: { reason: message.reason || "blocked" },
    })
      .catch(() => {})
      .finally(() => finish({ nextPollAt: Date.now() + 10 * 60_000 }));
    sendResponse({ ok: true });
    return false;
  }

  sendResponse({ ok: true });
  return false;
});
