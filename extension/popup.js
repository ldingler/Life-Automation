const DEFAULTS = {
  apiBase: "http://127.0.0.1:8787",
  apiToken: "",
  enterToBid: false,
  autoCapture: false,
  shopperEnabled: false,
};

async function restore() {
  const stored = await chrome.storage.sync.get(DEFAULTS);
  document.getElementById("apiBase").value = stored.apiBase;
  document.getElementById("apiToken").value = stored.apiToken;
  document.getElementById("enterToBid").checked = stored.enterToBid;
  document.getElementById("autoCapture").checked = stored.autoCapture;
  document.getElementById("shopperEnabled").checked = stored.shopperEnabled;
  showLookupStatus(stored);
}

/**
 * Show what price checking has actually been doing — including any site that's
 * currently backed off. A silent feature that quietly stopped working is worse
 * than one that says so.
 */
async function showLookupStatus(stored) {
  const el = document.getElementById("lookupStatus");
  if (!stored.shopperEnabled) {
    el.textContent = "";
    return;
  }
  try {
    const headers = stored.apiToken ? { "X-API-Token": stored.apiToken } : {};
    const base = (stored.apiBase || DEFAULTS.apiBase).replace(/\/$/, "");
    const data = await (await fetch(`${base}/api/market/status`, { headers })).json();
    const blocked = (data.sites || []).filter((s) => s.blocked_until).map((s) => s.site);
    const queued = data.counts?.pending ?? 0;
    el.textContent =
      `${data.searches_last_24h}/${data.daily_limit} searches today, ${queued} queued` +
      (blocked.length ? ` — backed off: ${blocked.join(", ")}` : "");
  } catch {
    el.textContent = "";
  }
}

document.getElementById("save").addEventListener("click", async () => {
  const values = {
    apiBase: document.getElementById("apiBase").value.trim() || DEFAULTS.apiBase,
    apiToken: document.getElementById("apiToken").value.trim(),
    enterToBid: document.getElementById("enterToBid").checked,
    autoCapture: document.getElementById("autoCapture").checked,
    shopperEnabled: document.getElementById("shopperEnabled").checked,
  };
  await chrome.storage.sync.set(values);

  const status = document.getElementById("status");
  status.textContent = "Saved. Checking engine…";
  try {
    const response = await fetch(`${values.apiBase.replace(/\/$/, "")}/healthz`);
    status.textContent = response.ok
      ? "Saved — engine is reachable."
      : "Saved, but engine returned an error.";
  } catch (_) {
    status.textContent = "Saved, but the engine isn't reachable. Is `nellis serve` running?";
  }
  showLookupStatus(values);
});

document.getElementById("capture").addEventListener("click", async () => {
  const status = document.getElementById("status");
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) return;
  status.textContent = "Reading this page…";
  chrome.tabs.sendMessage(tab.id, { type: "nde-capture" }, (result) => {
    if (chrome.runtime.lastError || !result) {
      status.textContent = "Nothing importable on this page.";
      return;
    }
    status.textContent = result.added
      ? `Imported ${result.added} item(s).`
      : "Nothing new found here.";
  });
});

document.getElementById("openQueue").addEventListener("click", () => {
  chrome.tabs.create({ url: chrome.runtime.getURL("queue.html") });
});

restore();
