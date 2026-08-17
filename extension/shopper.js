/**
 * Reads a search results page, when — and only when — the engine asked for one.
 *
 * This is how retail verification happens without an API. Amazon's Product
 * Advertising API needs an Associate account with qualifying sales, Walmart's
 * needs partner approval, and Facebook Marketplace has no public API at all. So
 * the check runs the way a person would run it: open the search page, look at
 * the results, close the tab.
 *
 * Two rules this file exists to enforce:
 *
 *   1. **It never acts on its own.** The script loads on retailer search pages,
 *      but does nothing at all unless the background worker confirms this exact
 *      tab was opened for a queued job. Browsing Amazon yourself triggers
 *      nothing and sends nothing.
 *
 *   2. **A block means stop.** If the page is a CAPTCHA, an
 *      "unusual activity" wall or a login gate, it reports that and the engine
 *      takes the whole site offline for hours. There is no solver here, no
 *      retry loop, no second path in. Thinner verification coverage is the
 *      honest outcome and the dashboard shows it.
 *
 * Selectors rot — retailers reshuffle markup constantly. Each parser tries
 * structured data first (JSON-LD is far more stable than CSS classes), then
 * site-specific selectors, then a generic text scrape. Nothing depends on a
 * single fragile path.
 */

(() => {
  "use strict";

  const MONEY = /\$\s?([0-9][0-9,]*(?:\.[0-9]{2})?)/;
  const RATING = /([0-5](?:\.[0-9])?)\s*out of\s*5/i;
  const COUNT = /([0-9][0-9,]*)/;

  const clean = (text) => (text || "").replace(/\s+/g, " ").trim();
  const money = (text) => {
    const m = MONEY.exec(text || "");
    return m ? parseFloat(m[1].replace(/,/g, "")) : null;
  };
  const abs = (href) => {
    try {
      return new URL(href, location.origin).href;
    } catch {
      return null;
    }
  };

  // ---------------------------------------------------------------- blocks

  /**
   * Does this page want us to go away?
   *
   * Returns a human-readable reason, or null. Deliberately generous: a false
   * positive costs a few hours of one site's coverage, a false negative means
   * hammering a site that already asked us to stop.
   */
  function blockReason() {
    const text = clean(document.body?.innerText || "").slice(0, 4000).toLowerCase();
    const href = location.href.toLowerCase();

    if (/\/errors\/validatecaptcha|captcha|challenge|checkpoint/.test(href)) {
      return "redirected to a challenge page";
    }
    if (document.querySelector("form[action*='validateCaptcha'], #captchacharacters")) {
      return "CAPTCHA shown";
    }
    if (document.querySelector("#px-captcha, [id*='px-captcha'], iframe[src*='captcha']")) {
      return "CAPTCHA shown";
    }
    const phrases = [
      "enter the characters you see below",
      "type the characters you see",
      "not a robot",
      "robot or human",
      "unusual traffic",
      "unusual activity",
      "verify your identity",
      "access denied",
      "you're temporarily blocked",
      "temporarily blocked",
      "rate limit",
    ];
    for (const phrase of phrases) {
      if (text.includes(phrase)) return `page says "${phrase}"`;
    }
    if (
      location.hostname.includes("facebook") &&
      (document.querySelector("#login_form, form[action*='login']") ||
        text.includes("log in to facebook") ||
        text.includes("log in or sign up for facebook"))
    ) {
      return "not signed in to Facebook — sign in and this resumes";
    }
    return null;
  }

  // ------------------------------------------------------------- structured

  /** JSON-LD survives redesigns that break every CSS selector. */
  function fromStructuredData() {
    const out = [];
    for (const node of document.querySelectorAll("script[type='application/ld+json']")) {
      let data;
      try {
        data = JSON.parse(node.textContent);
      } catch {
        continue;
      }
      const lists = Array.isArray(data) ? data : [data];
      for (const entry of lists) {
        const items = entry?.itemListElement || [];
        for (const wrapper of items) {
          const item = wrapper?.item || wrapper;
          if (!item || !item.name) continue;
          const offer = Array.isArray(item.offers) ? item.offers[0] : item.offers;
          const price = parseFloat(offer?.price ?? offer?.lowPrice ?? NaN);
          if (!isFinite(price) || price <= 0) continue;
          out.push({
            title: clean(item.name),
            price,
            url: abs(item.url || offer?.url || ""),
            brand: clean(item.brand?.name || item.brand || "") || null,
            rating: parseFloat(item.aggregateRating?.ratingValue ?? NaN) || null,
            review_count: parseInt(item.aggregateRating?.reviewCount ?? "", 10) || null,
            in_stock: !/outofstock|soldout/i.test(offer?.availability || ""),
            condition: /used|refurb/i.test(offer?.itemCondition || "") ? "used" : null,
          });
        }
      }
    }
    return out;
  }

  // ------------------------------------------------------------ per-site

  function parseAmazon() {
    const out = [];
    for (const card of document.querySelectorAll("div[data-component-type='s-search-result']")) {
      const title = clean(card.querySelector("h2 span, h2 a span, h2")?.innerText);
      const price =
        money(card.querySelector(".a-price .a-offscreen")?.textContent) ??
        money(card.querySelector(".a-price")?.innerText);
      if (!title || !price) continue;

      const ratingText =
        card.querySelector("[aria-label*='out of 5 stars']")?.getAttribute("aria-label") ||
        card.querySelector(".a-icon-alt")?.textContent ||
        "";
      const reviewsEl =
        card.querySelector("[aria-label*='ratings'], [aria-label*='reviews']") ||
        card.querySelector("span.a-size-base.s-underline-text");
      const reviewsText =
        reviewsEl?.getAttribute?.("aria-label") || reviewsEl?.textContent || "";

      out.push({
        title,
        price,
        url: abs(card.querySelector("h2 a, a.a-link-normal")?.getAttribute("href") || ""),
        rating: parseFloat(RATING.exec(ratingText)?.[1] ?? NaN) || null,
        review_count: parseInt((COUNT.exec(reviewsText)?.[1] || "").replace(/,/g, ""), 10) || null,
        in_stock: !/currently unavailable/i.test(card.innerText),
        condition: /\b(used|renewed|refurbished)\b/i.test(card.innerText) ? "used" : null,
      });
    }
    return out;
  }

  function parseWalmart() {
    const out = [];
    const cards = document.querySelectorAll(
      "div[data-item-id], div[data-testid='list-view'] > div, [data-testid='item-stack'] > div"
    );
    for (const card of cards) {
      const title = clean(
        card.querySelector("span[data-automation-id='product-title'], a span")?.innerText
      );
      const price =
        money(card.querySelector("div[data-automation-id='product-price']")?.innerText) ??
        money(card.innerText);
      if (!title || !price) continue;

      const ratingText = card.querySelector("[aria-label*='out of 5']")?.getAttribute("aria-label") || "";
      const reviewsText = card.querySelector("[data-testid='review-count']")?.textContent || "";

      out.push({
        title,
        price,
        url: abs(card.querySelector("a")?.getAttribute("href") || ""),
        rating: parseFloat(RATING.exec(ratingText)?.[1] ?? NaN) || null,
        review_count: parseInt((COUNT.exec(reviewsText)?.[1] || "").replace(/,/g, ""), 10) || null,
        in_stock: !/out of stock/i.test(card.innerText),
        condition: /\b(restored|refurbished|pre-owned)\b/i.test(card.innerText) ? "used" : null,
      });
    }
    return out;
  }

  /**
   * Facebook Marketplace.
   *
   * Class names are generated and change constantly, so this anchors on the one
   * stable thing: item links. Everything is second-hand by default, which is
   * the right assumption — a Marketplace listing sets a *used* ceiling, and the
   * engine requires a much bigger discount before a used alternative can veto a
   * lot.
   */
  function parseFacebook() {
    const out = [];
    const seen = new Set();
    for (const link of document.querySelectorAll("a[href*='/marketplace/item/']")) {
      const href = abs(link.getAttribute("href") || "")?.split("?")[0];
      if (!href || seen.has(href)) continue;

      const lines = clean(link.innerText).split(/\s*(?=\$)/);
      const text = clean(link.innerText);
      const price = money(text);
      if (!price) continue;

      // The title is whatever follows the price, minus the location line.
      const after = text.replace(MONEY, "").trim();
      const title = clean(after.split(/\s{2,}|·/)[0]).slice(0, 200) || clean(lines[1] || "");
      if (!title) continue;

      seen.add(href);
      out.push({
        title,
        price,
        url: href,
        rating: null,
        review_count: null,
        in_stock: true,
        condition: "used",
      });
    }
    return out;
  }

  const PARSERS = { amazon: parseAmazon, walmart: parseWalmart, facebook: parseFacebook };

  function harvest(site) {
    const parser = PARSERS[site];
    const primary = parser ? parser() : [];
    if (primary.length) return primary;
    // Structured data is the fallback, not the first choice: on search pages it
    // is often a partial list, but a partial list beats nothing.
    return fromStructuredData();
  }

  // ------------------------------------------------------------ lifecycle

  /**
   * Results load lazily on every one of these sites. Wait for the list to stop
   * growing rather than guessing a fixed delay.
   */
  function waitForResults(site, { timeout = 12000, quiet = 900 } = {}) {
    return new Promise((resolve) => {
      const started = Date.now();
      let last = 0;
      let stableSince = Date.now();

      const tick = () => {
        const found = harvest(site);
        if (found.length !== last) {
          last = found.length;
          stableSince = Date.now();
        }
        const settled = found.length > 0 && Date.now() - stableSince > quiet;
        if (settled || Date.now() - started > timeout) {
          resolve(found);
          return;
        }
        setTimeout(tick, 300);
      };
      tick();
    });
  }

  async function run() {
    // Ask the background worker whether this tab is a job. If it isn't — which
    // is the case any time you're just shopping — we stop here and do nothing.
    let assignment = null;
    try {
      assignment = await chrome.runtime.sendMessage({
        type: "nde-shopper-ready",
        url: location.href,
      });
    } catch {
      return;
    }
    if (!assignment || !assignment.job_id || !assignment.site) return;

    const blocked = blockReason();
    if (blocked) {
      chrome.runtime.sendMessage({
        type: "nde-shopper-blocked",
        job_id: assignment.job_id,
        reason: blocked,
      });
      return;
    }

    const candidates = await waitForResults(assignment.site);

    // Re-check: challenge pages sometimes swap in after the first paint.
    const late = blockReason();
    if (late) {
      chrome.runtime.sendMessage({
        type: "nde-shopper-blocked",
        job_id: assignment.job_id,
        reason: late,
      });
      return;
    }

    chrome.runtime.sendMessage({
      type: "nde-shopper-result",
      job_id: assignment.job_id,
      candidates: candidates.slice(0, 40),
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", run, { once: true });
  } else {
    run();
  }
})();
