/**
 * Reads your own data off pages you're already looking at.
 *
 * Nellis purchase history, Amazon cart and saved-for-later, and Alexa lists have
 * no usable API between them — Nellis needs a login (and automating that login is
 * exactly what their ToS forbids), Amazon publishes nothing, and Amazon shut the
 * Alexa List Management REST API down on 1 July 2024 with no replacement.
 *
 * So this doesn't fetch anything. It reads the DOM of a page YOU opened, in a
 * session YOU logged into, and posts the result to your own local engine. No
 * credentials are stored, no login is automated, and nothing runs unless you're
 * looking at the page.
 *
 * It never runs on its own: `autoCapture` defaults to off, and even switched on
 * it only reads a page you navigated to yourself.
 *
 * Selectors are heuristic and grouped per site so they can be repaired in one
 * place. Amazon in particular reshuffles its DOM constantly, so nothing here
 * depends on a single fragile path — each extractor tries several shapes and
 * falls back to reading text.
 */

(() => {
  "use strict";

  const DEFAULTS = { apiBase: "http://127.0.0.1:8787", apiToken: "", autoCapture: false };

  const MONEY = /\$\s?([0-9][0-9,]*(?:\.[0-9]{2})?)/;

  const money = (text) => {
    const match = MONEY.exec(text || "");
    return match ? parseFloat(match[1].replace(/,/g, "")) : null;
  };

  const clean = (text) => (text || "").replace(/\s+/g, " ").trim();

  /** Which importable source, if any, is this page? */
  function detectSource() {
    const { host, pathname } = location;
    if (/nellisauction\.com$/.test(host)) {
      if (/invoice|purchase|order|won/i.test(pathname)) return "nellis_purchase";
      if (/return/i.test(pathname)) return "nellis_return";
      if (/watch|favorite/i.test(pathname)) return "nellis_watchlist";
      return null;
    }
    if (/amazon\.(com|co\.uk|ca)$/.test(host)) {
      if (/\/cart|\/gp\/cart/i.test(pathname)) return "amazon_cart";
      if (/saved|wishlist|registry|hz\/wishlist/i.test(pathname)) return "amazon_saved";
      if (/order-history|your-orders|\/gp\/css\/order/i.test(pathname)) return "amazon_order";
      return null;
    }
    if (/alexa\.amazon\.|amazon\.com$/.test(host) && /list/i.test(pathname)) {
      return "alexa_list";
    }
    return null;
  }

  /**
   * Generic row extractor.
   *
   * Rather than a brittle selector per site, this finds repeated containers that
   * each hold a title-ish link and optionally a price, which is what all of these
   * pages look like structurally regardless of their class names.
   */
  function extractRows() {
    const candidates = [
      "[data-item-id]", "[data-asin]", "[data-itemid]",
      ".sc-list-item", ".a-list-item", ".order-card", ".item-row",
      "li[class*='item']", "tr[class*='item']", "div[class*='card']",
      "div[class*='item']", "article",
    ];

    const seen = new Set();
    const rows = [];

    for (const selector of candidates) {
      let nodes;
      try {
        nodes = document.querySelectorAll(selector);
      } catch (_) {
        continue;
      }
      if (!nodes.length || nodes.length > 400) continue;

      for (const node of nodes) {
        const text = clean(node.innerText);
        if (!text || text.length < 8 || text.length > 900) continue;

        const link =
          node.querySelector("a[href*='/dp/'], a[href*='/p/'], a[href*='/product']") ||
          node.querySelector("a[href]");
        const heading = node.querySelector("h1,h2,h3,h4,[class*='title'],[class*='name']");

        let title = clean(heading?.innerText) || clean(link?.innerText);
        if (!title || title.length < 4) continue;
        title = title.slice(0, 300);

        const key = title.toLowerCase();
        if (seen.has(key)) continue;
        seen.add(key);

        const qtyMatch = /(?:qty|quantity)\s*[:.]?\s*(\d{1,3})/i.exec(text);
        const dateMatch =
          /(?:ordered|placed|purchased|won|date)[^\n]{0,20}?([A-Z][a-z]{2,9}\s+\d{1,2},?\s+\d{4})/i.exec(text) ||
          /(\d{1,2}\/\d{1,2}\/\d{2,4})/.exec(text);

        rows.push({
          title,
          price: money(text),
          quantity: qtyMatch ? parseInt(qtyMatch[1], 10) : 1,
          date: dateMatch ? dateMatch[1] : null,
          url: link?.href || null,
          id: node.getAttribute("data-asin") || node.getAttribute("data-item-id") || null,
        });
      }
      // Enough structure found; deeper selectors would only duplicate.
      if (rows.length >= 3) break;
    }
    return rows;
  }

  /** Alexa/shopping lists are plain checklists, not product cards. */
  function extractListItems() {
    const rows = [];
    const seen = new Set();
    for (const node of document.querySelectorAll(
      "li, [role='listitem'], [class*='list-item'], [class*='ListItem']"
    )) {
      const text = clean(node.innerText);
      if (!text || text.length < 2 || text.length > 120) continue;
      if (/^(sign in|menu|settings|help|home|lists?)$/i.test(text)) continue;
      const key = text.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      rows.push({ title: text, quantity: 1, price: money(text) });
    }
    return rows;
  }

  async function settings() {
    return { ...DEFAULTS, ...(await chrome.storage.sync.get(DEFAULTS)) };
  }

  async function send(source, records) {
    const config = await settings();
    const headers = { "Content-Type": "application/json" };
    if (config.apiToken) headers["X-API-Token"] = config.apiToken;

    const response = await fetch(
      `${config.apiBase.replace(/\/$/, "")}/api/signals/${source}`,
      { method: "POST", headers, body: JSON.stringify({ records }) }
    );
    if (!response.ok) throw new Error(`engine returned ${response.status}`);
    return response.json();
  }

  function toast(message, ok = true) {
    document.getElementById("nde-capture-toast")?.remove();
    const el = document.createElement("div");
    el.id = "nde-capture-toast";
    el.textContent = message;
    el.style.cssText = `
      position:fixed;bottom:18px;left:18px;z-index:2147483000;
      background:${ok ? "#0d3b2e" : "#5a1f1f"};color:#fff;
      padding:11px 15px;border-radius:9px;font:13px/1.4 -apple-system,sans-serif;
      box-shadow:0 8px 26px rgba(0,0,0,.28);max-width:340px`;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 6000);
  }

  async function capture(interactive = true) {
    const source = detectSource();
    if (!source) {
      if (interactive) toast("Nothing importable on this page.", false);
      return { captured: 0 };
    }

    const records = source === "alexa_list" ? extractListItems() : extractRows();
    if (!records.length) {
      if (interactive) {
        toast("Couldn't read any items here — the page layout may have changed.", false);
      }
      return { captured: 0 };
    }

    try {
      const result = await send(source, records);
      if (interactive) {
        toast(
          `Imported ${result.added} new item(s) from ${source.replace(/_/g, " ")}` +
            (result.skipped ? ` (${result.skipped} already known)` : "")
        );
      }
      return result;
    } catch (error) {
      if (interactive) toast(`Import failed: ${error.message}`, false);
      throw error;
    }
  }

  // Exposed for the popup's "Capture this page" button.
  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type === "nde-capture") {
      capture(true).then(sendResponse).catch(() => sendResponse({ captured: 0 }));
      return true; // async response
    }
    if (message?.type === "nde-detect") {
      sendResponse({ source: detectSource() });
      return false;
    }
    return false;
  });

  // Offer, don't act. A button appears on importable pages; nothing is sent
  // until it's clicked, unless auto-capture was explicitly switched on.
  (async () => {
    const source = detectSource();
    if (!source) return;
    const config = await settings();

    if (config.autoCapture) {
      capture(false).catch(() => {});
      return;
    }

    if (document.getElementById("nde-capture-btn")) return;
    const button = document.createElement("button");
    button.id = "nde-capture-btn";
    button.textContent = `Import ${source.replace(/_/g, " ")} → Deal Engine`;
    button.style.cssText = `
      position:fixed;bottom:18px;left:18px;z-index:2147483000;
      background:#12253d;color:#fff;border:0;border-radius:9px;
      padding:11px 15px;font:13px/1 -apple-system,sans-serif;font-weight:650;
      cursor:pointer;box-shadow:0 8px 26px rgba(0,0,0,.28)`;
    button.addEventListener("click", () => {
      button.disabled = true;
      button.textContent = "Importing…";
      capture(true).finally(() => button.remove());
    });
    document.body.appendChild(button);
  })();
})();
