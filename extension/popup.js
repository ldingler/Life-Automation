const DEFAULTS = { apiBase: "http://127.0.0.1:8787", apiToken: "", enterToBid: false };

async function restore() {
  const stored = await chrome.storage.sync.get(DEFAULTS);
  document.getElementById("apiBase").value = stored.apiBase;
  document.getElementById("apiToken").value = stored.apiToken;
  document.getElementById("enterToBid").checked = stored.enterToBid;
}

document.getElementById("save").addEventListener("click", async () => {
  await chrome.storage.sync.set({
    apiBase: document.getElementById("apiBase").value.trim() || DEFAULTS.apiBase,
    apiToken: document.getElementById("apiToken").value.trim(),
    enterToBid: document.getElementById("enterToBid").checked,
  });

  const status = document.getElementById("status");
  status.textContent = "Saved. Checking engine…";
  try {
    const base = document.getElementById("apiBase").value.trim() || DEFAULTS.apiBase;
    const response = await fetch(`${base.replace(/\/$/, "")}/healthz`);
    status.textContent = response.ok ? "Saved — engine is reachable." : "Saved, but engine returned an error.";
  } catch (_) {
    status.textContent = "Saved, but the engine isn't reachable. Is `nellis serve` running?";
  }
});

document.getElementById("openQueue").addEventListener("click", () => {
  chrome.tabs.create({ url: chrome.runtime.getURL("queue.html") });
});

restore();
