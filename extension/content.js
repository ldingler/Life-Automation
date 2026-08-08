/**
 * Nellis Deal Engine — lot page content script.
 *
 * WHAT THIS DOES
 *   - reads your local engine's valuation for the lot you're looking at
 *   - shows the breakdown and your walk-away max bid
 *   - pre-fills the site's own max-bid input with that number
 *
 * WHAT THIS DOES NOT DO
 *   - it never submits a bid on its own, on any timer, or in the background
 *   - there is no scheduling, no randomized timing, no attempt to look like
 *     anything other than what it is: a calculator in your own browser
 *
 * Every bid is placed by you, one keystroke or click at a time, on the lot in
 * front of you. That is the whole design, not a limitation to work around.
 */

(() => {
  "use strict";

  const DEFAULTS = { apiBase: "http://127.0.0.1:8787", apiToken: "", enterToBid: false };

  const lotId = (location.pathname.match(/\/p\/(\d+)/) || [])[1];
  if (!lotId) return;

  let settings = DEFAULTS;
  let valuation = null;
  let bidInput = null;

  const money = (n) =>
    n === null || n === undefined ? "—" : `$${Number(n).toFixed(2)}`;

  async function loadSettings() {
    const stored = await chrome.storage.sync.get(DEFAULTS);
    settings = { ...DEFAULTS, ...stored };
  }

  async function fetchValuation() {
    const url = `${settings.apiBase.replace(/\/$/, "")}/api/lot/${lotId}`;
    const headers = settings.apiToken ? { "X-API-Token": settings.apiToken } : {};
    const response = await fetch(url, { headers });
    if (response.status === 404) return { notTracked: true };
    if (!response.ok) throw new Error(`engine returned ${response.status}`);
    return response.json();
  }

  /**
   * Find the site's max-bid field without depending on class names, which
   * change constantly. Scored heuristics, best match wins.
   */
  function findBidInput() {
    const inputs = [...document.querySelectorAll("input")].filter((el) => {
      if (el.type === "hidden" || el.disabled || el.readOnly) return false;
      const rect = el.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0;
    });

    let best = null;
    let bestScore = 0;
    for (const el of inputs) {
      const hay = [
        el.name, el.id, el.placeholder, el.getAttribute("aria-label"),
        el.className, el.closest("label")?.textContent,
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();

      let score = 0;
      if (/\bmax\b/.test(hay)) score += 4;
      if (/\bbid\b/.test(hay)) score += 4;
      if (/amount|offer|price/.test(hay)) score += 2;
      if (el.type === "number") score += 2;
      if (el.inputMode === "decimal" || el.inputMode === "numeric") score += 1;
      if (score > bestScore) {
        bestScore = score;
        best = el;
      }
    }
    return bestScore >= 4 ? best : null;
  }

  function setNativeValue(el, value) {
    // React/Remix track value via a native setter; assigning .value directly
    // updates the DOM but not component state, and the app then ignores it.
    const proto = Object.getPrototypeOf(el);
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(el, value);
    else el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function prefill() {
    if (!valuation || !valuation.suggested_max_bid) return false;
    bidInput = bidInput || findBidInput();
    if (!bidInput) return false;
    setNativeValue(bidInput, String(Math.floor(valuation.suggested_max_bid)));
    bidInput.classList.add("nde-filled");
    bidInput.focus();
    bidInput.select?.();
    return true;
  }

  function findBidButton() {
    const candidates = [...document.querySelectorAll('button, input[type="submit"]')];
    return candidates.find((el) => {
      const text = (el.textContent || el.value || "").trim().toLowerCase();
      return /place bid|submit bid|^bid$|set max|confirm bid/.test(text);
    });
  }

  async function post(path) {
    const headers = { "Content-Type": "application/json" };
    if (settings.apiToken) headers["X-API-Token"] = settings.apiToken;
    return fetch(`${settings.apiBase.replace(/\/$/, "")}${path}`, {
      method: "POST",
      headers,
      body: JSON.stringify({}),
    });
  }

  function render() {
    document.getElementById("nde-panel")?.remove();

    const panel = document.createElement("div");
    panel.id = "nde-panel";

    if (!valuation || valuation.notTracked) {
      panel.innerHTML = `
        <div class="nde-head"><span>Nellis Deal Engine</span>
          <button data-nde="close" title="Close">&times;</button></div>
        <div class="nde-body">
          <div class="nde-warn">
            This lot isn't in your engine yet. Run
            <code>nellis value ${lotId}</code> to price it.
          </div>
        </div>`;
      wire(panel);
      document.body.appendChild(panel);
      return;
    }

    const v = valuation;
    const profitable = (v.projected_profit || 0) > 0 && v.suggested_max_bid > 0;
    const overBid = v.current_bid >= v.suggested_max_bid;

    panel.innerHTML = `
      <div class="nde-head">
        <span>Nellis Deal Engine</span>
        <button data-nde="close" title="Close">&times;</button>
      </div>
      <div class="nde-body">
        <div class="nde-bid" style="${profitable && !overBid ? "" : "background:#5a1f1f"}">
          <div class="lbl">Your walk-away max bid</div>
          <div class="amt">${money(v.suggested_max_bid)}</div>
          <div class="sub">
            landed ${money(v.exposure_if_won)} ·
            profit ${money(v.projected_profit)}
            ${v.projected_margin ? `(${Math.round(v.projected_margin * 100)}%)` : ""}
          </div>
        </div>

        <table>
          <tr><td class="k">Current bid</td><td>${money(v.current_bid)}</td></tr>
          <tr><td class="k">Comps</td><td>${money(v.comp_value)} <span style="color:#8a94a0">(n=${v.comp_count})</span></td></tr>
          <tr><td class="k">Confidence</td><td><span class="tag ${v.confidence}">${v.confidence}</span></td></tr>
          ${v.missing_parts?.length ? `<tr><td class="k">Missing</td><td>${v.missing_parts.join(", ")}</td></tr>` : ""}
        </table>

        ${v.repair_notes ? `<div class="nde-warn">${v.repair_notes}</div>` : ""}
        ${overBid ? `<div class="nde-bad">Bidding is already at or above your max. Walk away.</div>` : ""}
        ${v.block_reason ? `<div class="nde-bad">${v.block_reason}</div>` : ""}

        <div class="nde-actions">
          <button class="nde-primary" data-nde="fill">Pre-fill ${money(v.suggested_max_bid)}</button>
          ${v.queue_id ? `<button class="nde-ghost" data-nde="placed">I placed it</button>` : ""}
          ${v.queue_id ? `<button class="nde-ghost" data-nde="skip">Skip</button>` : ""}
        </div>

        <div class="nde-foot">
          ${v.reason || ""}
          <br><b>You place every bid.</b> This panel only fills in the number.
        </div>
      </div>`;

    wire(panel);
    document.body.appendChild(panel);
  }

  function wire(panel) {
    panel.addEventListener("click", async (event) => {
      const action = event.target.getAttribute("data-nde");
      if (!action) return;

      if (action === "close") panel.remove();

      if (action === "fill") {
        const ok = prefill();
        event.target.textContent = ok ? "Filled — now click Place Bid" : "Couldn't find the bid field";
        if (!ok) {
          navigator.clipboard
            ?.writeText(String(Math.floor(valuation.suggested_max_bid)))
            .then(() => (event.target.textContent = "Copied to clipboard — paste it in"))
            .catch(() => {});
        }
      }

      if (action === "placed" && valuation.queue_id) {
        await post(`/api/queue/${valuation.queue_id}/confirm`);
        event.target.textContent = "Recorded";
        chrome.runtime.sendMessage({ type: "nde-advance" }).catch(() => {});
      }

      if (action === "skip" && valuation.queue_id) {
        await post(`/api/queue/${valuation.queue_id}/skip`);
        event.target.textContent = "Skipped";
        chrome.runtime.sendMessage({ type: "nde-advance" }).catch(() => {});
      }
    });
  }

  /**
   * Optional keyboard shortcuts for working a queue quickly.
   * Enter still requires YOU to press it, once, per lot, with the lot on
   * screen — it is a shortcut for the click you were going to make anyway.
   * Off by default; enable it in the popup.
   */
  function bindKeys() {
    document.addEventListener("keydown", (event) => {
      if (event.target.matches("input, textarea, select")) return;
      if (event.key === "Escape" && valuation?.queue_id) {
        post(`/api/queue/${valuation.queue_id}/skip`).then(() =>
          chrome.runtime.sendMessage({ type: "nde-advance" }).catch(() => {})
        );
      }
      if (event.key === "Enter" && settings.enterToBid) {
        if (!prefill()) return;
        const button = findBidButton();
        if (button) button.click();
      }
    });
  }

  async function start() {
    await loadSettings();
    try {
      valuation = await fetchValuation();
    } catch (error) {
      valuation = null;
      console.warn("[nde] engine unreachable:", error.message);
      return;
    }
    render();
    // The page is client-rendered; the bid field may mount after we do.
    setTimeout(prefill, 900);
    bindKeys();
  }

  start();
})();
