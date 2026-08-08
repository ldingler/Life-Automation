# Nellis Deal Engine

Finds Nellis Auction lots worth buying, works out what they're actually worth,
and tells you the highest price you can bid and still make money.

**It does not place bids.** That is a design decision, not a missing feature —
see [Why it doesn't bid for you](#why-it-doesnt-bid-for-you). The short version:
Nellis already auto-bids for you, so the only thing that was ever hard is
knowing what number to put in. This works out that number.

---

## What it does

| | |
|---|---|
| **Finds** | Keyword, brand, category, condition, location, retail range, current-bid range, min discount %, time-to-close — saved as watches that run on a schedule |
| **Prices** | Builds your own database of what things *actually close for on Nellis*, plus eBay comps and your own CSV price sheets |
| **Costs** | True landed cost: hammer + 15% buyer's premium + 6.625% NJ tax. Every $1 you bid really costs $1.23 |
| **Projects** | Net resale after marketplace fees, shipping and packaging, by channel (eBay / local / Mercari / OfferUp) |
| **Repairs** | Detects damage and missing parts, prices the replacements, and computes whether fixing it pays — flags "easy fix" arbitrage |
| **Decides** | One number: your **walk-away max bid**, with a confidence grade |
| **Protects** | Exposure caps so bidding on 20 lots at once can't leave you owing for 20 lots |
| **Tells you** | HTML email digests, closing-soon alerts, a dashboard, and a browser extension that pre-fills the bid box |
| **Learns** | Tracks projected vs realized profit on everything you win, so you can see when the model is being optimistic |

---

## Quick start

```bash
cp .env.example .env          # then edit it
uv venv && uv pip install -e ".[dev]"

nellis init                   # create the database
nellis doctor                 # ← RUN THIS FIRST, see below
nellis watch add "Power tools" --keywords "dewalt,milwaukee,makita" \
                               --min-discount 60 --margin 40
nellis scan                   # find, value, queue
nellis queue                  # what to go bid on
nellis serve                  # dashboard at http://127.0.0.1:8787
```

Or with Docker:

```bash
cp .env.example .env
docker compose up -d
```

### Run `nellis doctor` first

The ingestion layer was built and tested against recorded fixtures, because
Nellis is not reachable from the environment this was written in. `nellis doctor`
is what verifies it against the live site:

```
                       Adapter health
┌───────────────┬──────────┬────────┬────────────────────────────┐
│ strategy      │ search   │ detail │ notes                      │
├───────────────┼──────────┼────────┼────────────────────────────┤
│ embedded-json │ 48 lots  │ ok     │ all critical fields present│
│ remix-data    │ 48 lots  │ ok     │                            │
│ html-dom      │ 24 lots  │ ok     │ missing: close_at          │
└───────────────┴──────────┴────────┴────────────────────────────┘
```

If every row shows 0, the site structure changed. The fix is usually one edit:
add the new field names to `ALIASES` in `src/nellis/ingest/adapter.py`. The
extraction searches for *field names* anywhere in the payload rather than
following fixed paths, so it survives most restructuring on its own.

---

## The math

For a hammer price **H**:

```
buyer's premium = H × 0.15
sales tax       = (H + premium) × 0.06625        ← tax applies to the premium too
landed cost     = H + premium + tax + pickup
```

Net resale proceeds:

```
net = comp_value × (1 − marketplace_fee) − shipping − packaging − parts − labor
```

Your walk-away max bid solves the inverse — the highest **H** that still clears
your target margin **m**:

```
H_max = ( net × (1 − m) − pickup ) / ( (1 + 0.15) × (1 + 0.06625) )
```

rounded **down** to a legal bid increment. Rounding up would quietly breach the
margin you asked for.

**Worked example.** A drill with a $186 comp, sold on eBay, at a 40% target
margin: eBay takes ~$25 in fees and you eat ~$18 shipping, so net is ~$140. That
supports a landed cost of ~$84, which after premium and tax means a **max bid of
$68**. The lot showing "$41, retail $299" is a good deal — but only up to $68,
and the number that matters is $68, not $299.

Confidence is scored from comp count, price dispersion, recency and source
agreement. Low confidence doesn't block a deal — it *raises the required margin*,
so uncertainty costs money instead of being ignored.

---

## Exposure control

Proxy bidding means every open max bid is a live commitment. Put maxes on twenty
lots and you can win twenty lots. Before anything enters the queue:

- total open exposure stays under `MAX_OPEN_EXPOSURE`
- per-category and per-lot-count caps apply
- lots closing within 30 minutes of each other are flagged as correlated risk
- when a cap binds, the queue ranks by **profit per dollar of exposure** and cuts
  the tail — keeping the deals that compound, not the biggest headline numbers

This is the failure mode that actually costs money, and no amount of clever
bidding addresses it.

---

## Why it doesn't bid for you

Three reasons, in order of how much they should matter to you.

**1. It would gain you nothing.** Nellis runs server-side proxy bidding: you set
a max, their system auto-outbids to your ceiling for free. And [bid extensions](https://nellisauction.freshdesk.com/support/solutions/articles/157000204077-what-are-bid-extensions-)
add 30 seconds to any bid placed in the final 30 seconds, so sniping is
structurally impossible. A bot bidding at the last instant and you entering the
same max six hours earlier produce **identical outcomes**. There is no timing
edge on this platform to capture.

**2. It's against the rules, with a real penalty.** Nellis' [Terms of Service](https://nellisauction.com/terms)
prohibit "use of automated tools or bots to place bids," with account suspension
or deactivation as the stated consequence. A banned account loses access to the
entire edge this system builds.

**3. So the edge is valuation, not speed.** Which is what this is.

What you get instead: the queue and the extension get entering a max bid down to
a couple of seconds per lot, so bidding on 50 lots is a few minutes of clicking,
not an argument with a CAPTCHA.

**Ingestion is polite, too.** Respects `robots.txt`, one connection, ~2.5s
between requests, ordinary User-Agent, and a hard stop on 403/429. There is no
fingerprint spoofing, proxy rotation, or CAPTCHA solving anywhere in this
codebase. If it gets rate-limited, the fix is to slow it down.

---

## The browser extension

`chrome://extensions` → Developer mode → **Load unpacked** → select `extension/`.

On any Nellis lot page it shows your valuation and pre-fills the max-bid field.
The queue page (`Open bid queue` in the popup) turns "50 lots to bid on" into an
ordered worklist. You click Place Bid; it never does.

Set the engine address to `http://127.0.0.1:8787` and paste your `API_TOKEN` if
you set one.

---

## Comps sources

| Source | Automated | Notes |
|---|---|---|
| **Nellis close history** | yes | Free, unlimited, highest signal. Empty on day one, compounds from there. Leave `nellis serve` running. |
| **eBay Browse API** | yes | Free dev account. Returns *active* listings — asking prices, so they're discounted and down-weighted. |
| eBay Marketplace Insights | no | True sold comps, but **partner approval only**. Set `EBAY_USE_INSIGHTS=true` if you're granted it. |
| **CSV import** | manual | `nellis comps import prices.csv`. The supported path for Facebook Marketplace, which has no public API and blocks scraping. |
| Local feed | optional | Craigslist discontinued native RSS; point `LOCAL_COMPS_FEED_URL` at a feed service if you want one. |

Day one leans on eBay and reports LOW confidence honestly. After a few weeks of
harvesting, Nellis close prices carry the estimates.

---

## Layout

```
src/nellis/
  ingest/      polite client + 4 extraction strategies behind one adapter
  search/      watches and matching
  valuation/   cost · comps · condition · repair · confidence · exposure · engine
  notify/      Jinja2 HTML emails + dedupe
  scheduler/   APScheduler jobs
  web/         FastAPI + HTMX dashboard
  api/         JSON API for the extension
extension/     Chrome MV3
tests/         118 tests, fully offline
```

Three interfaces absorb all the volatility: `NellisAdapter` (site changes),
`CompsProvider` (pricing sources), `PartsProvider` (parts lookup).

---

## Tuning

Everything lives in `.env`. The ones that matter:

| Setting | Default | |
|---|---|---|
| `TARGET_MARGIN` | `0.40` | Required net margin. Raise it if realized profit trails projected. |
| `MIN_PROFIT_DOLLARS` | `20` | Ignore deals thinner than this. |
| `MAX_OPEN_EXPOSURE` | `1500` | The most you can owe across all live bids. |
| `LABOR_RATE_PER_HOUR` | `25` | What your repair time is worth. |
| `REQUEST_DELAY_SECONDS` | `2.5` | Raising the rate is how you get IP-blocked. |

The Portfolio page compares projected vs realized profit on everything you've
sold. If actual consistently trails projected, the model is optimistic — raise
`TARGET_MARGIN`.

---

## Tests

```bash
.venv/bin/python -m pytest -q      # 118 tests, no network
```

Ingestion is fixture-driven; valuation is pure functions. The invariant worth
knowing about: bidding exactly the recommended max always clears the target
margin, and one increment above it always breaches — the answer is provably
maximal, not merely safe.
