/**
 * Service worker.
 *
 * Intentionally almost empty. There is no alarm, no polling loop, no scheduled
 * task — nothing that could act on Nellis while you aren't looking. It exists
 * only to open the queue page and relay a message when you mark a lot handled.
 */

chrome.runtime.onInstalled.addListener(() => {
  chrome.tabs.create({ url: chrome.runtime.getURL("queue.html") });
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "nde-advance") {
    // The content script tells us a lot was handled so any open queue tab can
    // refresh. Purely cosmetic.
    chrome.tabs.query({ url: chrome.runtime.getURL("queue.html") }, (tabs) => {
      tabs.forEach((tab) => chrome.tabs.reload(tab.id));
    });
  }
  sendResponse({ ok: true });
  return false;
});
