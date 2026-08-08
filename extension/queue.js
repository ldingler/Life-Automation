/**
 * Bulk bid queue.
 *
 * Turns "50 lots to bid on" into a short, ordered worklist. Each row opens the
 * real lot page with the max bid pre-filled; you place the bid there. This page
 * only tracks which ones you've handled and keeps your exposure honest.
 */

const DEFAULTS = { apiBase: "http://127.0.0.1:8787", apiToken: "" };
let settings = DEFAULTS;

const money = (n) => (n == null ? "—" : `$${Number(n).toFixed(2)}`);

function closesIn(iso) {
  if (!iso) return null;
  const seconds = (new Date(iso) - new Date()) / 1000;
  if (seconds < 0) return "closed";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
  return `${Math.floor(seconds / 86400)}d`;
}

function headers(json = false) {
  const h = {};
  if (json) h["Content-Type"] = "application/json";
  if (settings.apiToken) h["X-API-Token"] = settings.apiToken;
  return h;
}

const api = (path) => `${settings.apiBase.replace(/\/$/, "")}${path}`;

async function loadExposure() {
  try {
    const response = await fetch(api("/api/exposure"), { headers: headers() });
    if (!response.ok) return;
    const data = await response.json();
    const pct = data.max_exposure ? Math.min(100, (data.total_exposure / data.max_exposure) * 100) : 0;
    document.getElementById("exposure").innerHTML = `
      Open exposure <b>${money(data.total_exposure)}</b> / ${money(data.max_exposure)}
      · ${data.open_lots} live
      <div class="bar"><span style="width:${pct}%"></span></div>`;
  } catch (_) {
    /* engine offline — the list render will surface it */
  }
}

async function act(queueId, action, element) {
  await fetch(api(`/api/queue/${queueId}/${action}`), {
    method: "POST",
    headers: headers(true),
    body: JSON.stringify({}),
  });
  element.closest(".card").remove();
  loadExposure();
  updateCount();
}

function updateCount() {
  const remaining = document.querySelectorAll("#list .card").length;
  document.getElementById("count").textContent = remaining
    ? `${remaining} lot${remaining === 1 ? "" : "s"} to work`
    : "";
}

function row(item) {
  const card = document.createElement("div");
  card.className = "card";
  const time = closesIn(item.close_at);

  card.innerHTML = `
    <div class="grow">
      <div class="t">${item.title}</div>
      <div class="m">
        ${item.retail_price ? `Retail ${money(item.retail_price)} · ` : ""}
        Current ${money(item.current_bid)}
        ${time ? ` · closes in ${time}` : ""}
        ${item.condition ? ` · ${item.condition}` : ""}
      </div>
      <div class="m" style="margin-top:5px">
        <span class="tag ${item.confidence}">${item.confidence}</span>
        comps ${money(item.comp_value)} (n=${item.comp_count})
        ${item.missing_parts?.length ? ` · missing ${item.missing_parts.join(", ")}` : ""}
      </div>
      ${item.block_reason ? `<div class="m" style="color:#b3261e;margin-top:5px">${item.block_reason}</div>` : ""}
      <div class="acts">
        <button data-act="open">Open with ${money(item.suggested_max_bid)} filled</button>
        <button class="ghost" data-act="confirm">Mark placed</button>
        <button class="ghost" data-act="skip">Skip</button>
      </div>
    </div>
    <div class="bid">
      <div class="l">Max bid</div>
      <div class="a">${money(item.suggested_max_bid)}</div>
      <div class="s">+${money(item.projected_profit)}</div>
    </div>`;

  card.addEventListener("click", (event) => {
    const action = event.target.getAttribute("data-act");
    if (!action) return;
    if (action === "open") {
      chrome.tabs.create({ url: item.url, active: true });
      return;
    }
    act(item.queue_id, action, event.target);
  });

  return card;
}

async function load() {
  settings = { ...DEFAULTS, ...(await chrome.storage.sync.get(DEFAULTS)) };
  const list = document.getElementById("list");

  let items;
  try {
    const response = await fetch(api("/api/queue?include_blocked=true"), { headers: headers() });
    if (!response.ok) throw new Error(`engine returned ${response.status}`);
    items = await response.json();
  } catch (error) {
    list.innerHTML = `<div class="empty">
      Can't reach your engine at <code>${settings.apiBase}</code>.<br>
      Start it with <code>nellis serve</code>, or fix the address in the extension popup.
      <div style="margin-top:8px;font-size:12px">${error.message}</div>
    </div>`;
    return;
  }

  if (!items.length) {
    list.innerHTML = `<div class="empty">
      Queue is empty. Run <code>nellis scan</code> to find deals.
    </div>`;
    return;
  }

  items.forEach((item) => list.appendChild(row(item)));
  updateCount();
  loadExposure();
}

load();
