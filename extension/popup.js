const DEFAULTS = { apiBase: "http://127.0.0.1:8787", apiToken: "", enterToBid: false, autoCapture: false };

async function restore() {
  const stored = await chrome.storage.sync.get(DEFAULTS);
  document.getElementById("apiBase").value = stored.apiBase;
  document.getElementById("apiToken").value = stored.apiToken;
  document.getElementById("enterToBid").checked = stored.enterToBid;
  document.getElementById("autoCapture").checked = stored.autoCapture;
}

document.getElementById("save").addEventListener("click", async () => {
  await chrome.storage.sync.set({
    apiBase: document.getElementById("apiBase").value.trim() || DEFAULTS.apiBase,
    apiToken: document.getElementById("apiToken").value.trim(),
    enterToBid: document.getElementById("enterToBid").checked,
    autoCapture: document.getElementById("autoCapture").checked,
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
