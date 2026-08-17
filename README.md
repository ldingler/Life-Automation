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
| **Wants** | A second engine for "do I want this?" — want list, plus satiation so buying a shed quiets sheds for a while but buying screws never quiets screws |
| **Verifies** | Confirms stated retail against real listings for the exact item, and finds cheaper *equally-rated* substitutes — then refuses any bid that lands above the best real alternative |
| **Books** | Categorises purchases as MSS Company Expense (inventory/supplies/fixtures/office/other), Resell or Personal; records sales; totals savings against a *defensible* reference |

---

## See it work first (no setup, no network)

**macOS — one command:**

```bash
git clone https://github.com/ldingler/Life-Automation && cd Life-Automation
git checkout claude/nellis-auction-auto-bidder-vkty08
./scripts/bootstrap-mac.sh
```

It checks your Python, sets up the venv, installs, runs the tests and seeds the
demo. It **detects rather than installs** — if Python 3.11+ or `uv` is missing it
prints the exact command and stops, rather than installing Homebrew on your
machine unasked.

> macOS ships Python 3.9, which is too old. If the script says so:
> `brew install python@3.11`. Every dependency has prebuilt wheels for both
> Apple Silicon and Intel, so nothing needs Xcode to compile.

**Any platform, manually:**

```bash
uv venv && uv pip install -e ".[dev]"
nellis check                  # verify the environment first
nellis demo --reset
nellis serve                  # http://127.0.0.1:8787
```

No credentials, no scraping, nothing to configure. This seeds ~106 closed sales
across 8 product families — which become **real comps** — plus 14 open lots, and
runs them through the **real valuation engine**. Nothing is pre-baked; if the
valuation logic is wrong, the demo shows it being wrong.

The dataset deliberately includes lots that must be **rejected**, so you can see
the guardrails rather than a curated happy path:

```
Closed lots (became comps)  106      Recommended            6
Comps in database           106      Rejected (guardrails)  8
Open lots valued             14      Repair plays flagged   3
```

| Lot | Verdict |
|---|---|
| Dyson V8, complete | bid up to **$47** — projected +$39.43, HIGH confidence |
| Dyson V8, missing filter | bid up to **$39** — $14 part, **easy-fix 3.2×**, +$33.90 |
| Weber grill (local pickup, no fees) | bid up to **$97** — +$80.96 |
| Sony headphones at $214 | rejected — walk-away is $59 |
| Milwaukee, missing battery | rejected — $19.79 profit misses the $20 floor |
| Fridge, dead compressor | rejected — fatal damage, **$0 bid** |
| Craft supplies, no comps | rejected — cannot value it |

Then `nellis queue`, `nellis digest --dry-run`, and the Portfolio and Analytics
tabs all have data to look at.

## Going live

```bash
cp .env.example .env          # then edit it
nellis init                   # create the database
nellis doctor                 # ← RUN THIS FIRST, see below
nellis watch add "Power tools" --keywords "dewalt,milwaukee,makita" \
                               --min-discount 60 --margin 40
nellis scan                   # find, value, queue
nellis queue                  # what to go bid on
nellis serve                  # dashboard + scheduler
```

`nellis demo` writes to the same database, so run `nellis demo --reset` or delete
`data/nellis.db` before switching to real data.

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

Doctor also writes `fixtures/live/doctor.json`, which is easier to hand to
someone than a copied terminal table.

### If the parsers need repairing: `nellis record`

```bash
nellis record            # or: nellis record 1234567
```

Captures the search page and one lot page through the same polite client, pulls
out every JSON payload and Remix route ID, reports which fields actually
resolved, redacts anything credential-shaped, and writes a single
`fixtures/live/capture.zip`.

That one file is the whole handoff — it contains what's needed to calibrate the
parsers against real payloads instead of synthetic fixtures. It reads only public
pages, nothing requiring a login, and `fixtures/live/` is gitignored so a capture
is never committed by accident.

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

### The alternative ceiling

Comps answer "what did this sell for?" — but that assumes winning the auction is
the only way to get the thing. It isn't. Before recommending any bid, the engine
checks what it costs to just **buy one instead**:

```
Nellis:  Widget A, retail $500, bid at $350
Reality: Widget B, 4.5★ / 800 reviews, $300 NEW at Amazon
Verdict: don't bid — landed $429 vs $300 new
```

A substitute only counts if it's genuinely comparable (≥4.0★, ≥25 reviews),
otherwise the cheapest junk on the internet would veto every good lot. And buying
used at auction has to beat buying new by 30%, not a rounding error — there's a
pickup trip, no warranty, no returns.

Confidence is scored from comp count, price dispersion, recency and source
agreement. Low confidence doesn't block a deal — it *raises the required margin*,
so uncertainty costs money instead of being ignored.

### Where those prices come from: your browser

Stated retail on a liquidation listing is right maybe half the time, so every
lot gets checked against real listings. None of the sites that matter will give
out a price API — Amazon's Product Advertising API needs an Associate account
with qualifying sales, Walmart's needs partner approval, Facebook Marketplace
has no public API at all — so the check runs the way a person would run it.

The extension opens a search page **in your own browser, in your own session**,
reads the results, and closes the tab. Turn it on in the popup ("Check retail
prices for me in the background"); it's off until you do.

The pacing is the whole design, and it lives on the engine side so loosening it
means editing `.env` on purpose:

| Rule | Default | Why |
|---|---|---|
| One search at a time | always | Never concurrent tabs, never a burst |
| Gap between any two searches | 45s | A person comparison-shopping, not a crawler |
| Gap between two searches at the *same* site | 3 min | Per-site is what sites actually measure |
| Daily ceiling | 120 | A hard stop, not a target |
| A site shows a CAPTCHA | that site sleeps 6h | **See below** |

That last row is not a backoff-and-retry. A CAPTCHA is a site saying *stop*, and
the response is to stop — there is no solver here, no retry loop, no second path
in. If verification coverage suffers, thinner coverage is the honest outcome,
and `nellis lookups` shows you exactly which site is quiet and why.

What comes back is raw: a title, a price, maybe a rating. Everything that
decides what a card *means* happens server-side in `market/classify.py`, where
it's tested without a browser — because a search page is mostly accessories,
knock-offs and multi-packs, and a $12 "case for DeWalt DCD777C2" masquerading as
the price of the drill would wreck every valuation downstream. Accessories are
rejected, multi-packs are divided down to unit price, a rival brand becomes a
*substitute* rather than a verification, and anything from a different product
class is thrown out.

```bash
nellis lookups     # what's been checked, what's queued, what's backed off
```

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

## Troubleshooting

`nellis check` is the first thing to run when anything looks wrong — it verifies
Python version, dependencies, writable paths and the port, and tells you what to
do about each failure. Missing email/eBay config is reported as a *warning*: the
demo works without either.

Every path it reports is absolute and anchored to the repo, so `nellis` behaves
identically no matter which directory you run it from. Output always lands in
`data/` and `fixtures/` inside the repo — never in your current folder.

| Symptom | Cause |
|---|---|
| Can't log in to nellisauction.com, "Error Code: 500-NLS" | Nellis rate-limits by network and reports it as a 500. Turn off any VPN, close other tabs/devices hitting Nellis, wait ~15 min. Not a ban — the mobile app will still work. |
| `SyntaxError` deep in a dependency | Python 3.9 (the macOS system one). Use 3.11+. |
| `Address already in use` | Something's on 8787 — `nellis serve --port 8788` |
| Dashboard is empty | Run `nellis demo --reset`, or `nellis scan` for live data |
| `nellis doctor` shows all zeros | Site structure changed — run `nellis record` and send the zip |
| Extension shows nothing | Engine not running (`nellis serve`), or wrong address in the popup |

## The browser extension

`chrome://extensions` → Developer mode → **Load unpacked** → select `extension/`.
On macOS the repo is wherever you cloned it; the folder to pick is `extension/`
inside it.

On any Nellis lot page it shows your valuation and pre-fills the max-bid field.
The queue page (`Open bid queue` in the popup) turns "50 lots to bid on" into an
ordered worklist. You click Place Bid; it never does.

Set the engine address to `http://127.0.0.1:8787` and paste your `API_TOKEN` if
you set one.

Three optional toggles, all off by default:

- **Enter key places the bid** — a shortcut for the click you'd make anyway.
- **Auto-import history / cart / list pages** — otherwise an Import button
  appears and nothing is sent until you click it.
- **Check retail prices in the background** — the price lookups described
  [above](#where-those-prices-come-from-your-browser). This is the only thing in
  the extension that opens a page by itself, and it only ever opens Amazon,
  Walmart and Facebook Marketplace searches. Nothing automated ever touches
  Nellis.

---

## Comps sources

| Source | Automated | Notes |
|---|---|---|
| **Nellis close history** | yes | Free, unlimited, highest signal. Empty on day one, compounds from there. Leave `nellis serve` running. |
| **eBay Browse API** | yes | Free dev account. Returns *active* listings — asking prices, so they're discounted and down-weighted. |
| eBay Marketplace Insights | no | True sold comps, but **partner approval only**. Set `EBAY_USE_INSIGHTS=true` if you're granted it. |
| **Browser lookups** | yes | Amazon / Walmart / Facebook Marketplace read through your own session, paced like a person. See [above](#where-those-prices-come-from-your-browser). |
| **CSV import** | manual | `nellis comps import prices.csv`. |
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
  market/      what it costs to buy elsewhere: verify · classify · lookup queue
  books/       purpose, savings and sold tracking
  wants/       personal need matching and satiation
  api/         JSON API for the extension
extension/     Chrome MV3 — panel, capture, price shopper
tests/         300 tests, fully offline
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
.venv/bin/python -m pytest -q      # 264 tests, no network
```

Browser tests for the extension are opt-in, since they need a real Chromium and
take about 80 seconds:

```bash
uv pip install -e ".[browser]"
NELLIS_BROWSER_TESTS=1 pytest tests/browser -o asyncio_mode=strict
```

These load the actual unpacked extension in Chromium and verify it injects its
panel, finds the max-bid field by heuristics on markup that is *not* a copy of
Nellis' own, fires the input events that framework-tracked fields require, and —
most importantly — **never submits a bid on its own**.

Ingestion is fixture-driven; valuation is pure functions. The invariant worth
knowing about: bidding exactly the recommended max always clears the target
margin, and one increment above it always breaches — the answer is provably
maximal, not merely safe.
