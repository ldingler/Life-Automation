"""Application configuration.

Every tunable lives here and is overridable via .env. Nothing is hardcoded in
business logic — the valuation model in particular is meant to be tuned by the
operator as real results come in.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_prefix=""
    )

    # ---- storage -------------------------------------------------------
    # All of these are absolute and anchored to the repo, never to the current
    # working directory. Otherwise `nellis record` run from your home folder
    # writes the capture to ~/fixtures/live and you can't find the file you were
    # told to send.
    database_url: str = f"sqlite+pysqlite:///{REPO_ROOT / 'data' / 'nellis.db'}"
    cache_dir: Path = REPO_ROOT / "cache"
    output_dir: Path = REPO_ROOT / "fixtures" / "live"
    preview_dir: Path = REPO_ROOT / "data" / "previews"

    # ---- ingestion (politeness) ----------------------------------------
    # These defaults are deliberately conservative. We identify as an ordinary
    # browser, obey robots.txt, use a single connection, and hard-stop on 429/403.
    # We do not spoof fingerprints, rotate proxies, or solve CAPTCHAs.
    nellis_base_url: str = "https://www.nellisauction.com"
    request_delay_seconds: float = 2.5
    request_jitter_seconds: float = 1.0
    request_timeout_seconds: float = 25.0
    max_requests_per_run: int = 400
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    respect_robots_txt: bool = True
    # Nellis returns 500 (not 429) when rate limiting. Stop the run after this
    # many consecutive server errors rather than pushing through a throttle that
    # is network-wide and will lock the browser out too.
    server_error_stop_threshold: int = 3
    cache_ttl_seconds: int = 300
    enable_browser_fallback: bool = False

    # ---- locale / fees --------------------------------------------------
    home_zip: str = "08055"  # Medford, NJ (Burlington County)
    sales_tax_rate: float = 0.06625  # NJ statewide
    default_buyers_premium: float = 0.15  # ~15%, overridden per-lot when listed
    pickup_cost: float = 0.0  # your cost per pickup trip (gas/time), amortized

    # ---- valuation ------------------------------------------------------
    target_margin: float = 0.40  # required net margin to recommend a bid
    labor_rate_per_hour: float = 25.0
    min_profit_dollars: float = 20.0  # ignore deals thinner than this
    low_confidence_margin_bump: float = 0.15  # widen margin when comps are thin
    med_confidence_margin_bump: float = 0.07

    # ---- exposure control -----------------------------------------------
    max_open_exposure: float = 1500.0  # total $ you can be on the hook for
    max_open_lots: int = 25
    max_per_category_exposure: float = 500.0
    overlap_window_minutes: int = 30  # lots closing this close together = correlated risk

    # ---- comps providers -------------------------------------------------
    ebay_client_id: str | None = None
    ebay_client_secret: str | None = None
    ebay_marketplace: str = "EBAY_US"
    ebay_fee_rate: float = 0.1325
    # Marketplace Insights (true SOLD comps) is partner-approval-only. Flip this
    # on only once eBay grants it; otherwise Browse/active listings are used.
    ebay_use_insights: bool = False
    local_fee_rate: float = 0.0  # FB Marketplace / Craigslist / local pickup
    local_comps_feed_url: str | None = None  # optional 3rd-party feed (CL RSS is dead)
    comps_max_age_days: int = 120
    comps_min_sample: int = 3

    # ---- browser price lookups -------------------------------------------
    # Retail verification runs through the operator's own browser, because none
    # of these sites will hand out an API: Amazon's PA-API needs an Associate
    # account with qualifying sales, Walmart's needs partner approval, Facebook
    # Marketplace has no public API at all. So the extension opens a search page
    # the operator could have opened by hand, reads it, and closes it.
    #
    # These defaults are pedestrian on purpose. One tab at a time, minutes
    # between searches, a hard daily ceiling. The point is to look like someone
    # comparison-shopping, because that is exactly what this is — not to look
    # like something else.
    lookup_enabled: bool = True
    lookup_sites: str = "amazon,walmart,facebook"
    lookup_min_seconds_between: float = 45.0     # any two searches, any site
    lookup_site_min_seconds: float = 180.0       # two searches at the SAME site
    lookup_jitter_seconds: float = 30.0
    lookup_max_per_day: int = 120
    lookup_max_attempts: int = 2
    lookup_lease_timeout_seconds: float = 300.0  # tab died? put the job back
    # When a site shows a CAPTCHA or a "we noticed unusual activity" wall we
    # back off entirely for this long. We do not solve it and do not retry
    # around it.
    lookup_block_cooldown_minutes: int = 360
    market_price_ttl_days: int = 21              # re-check a price older than this
    lookup_max_results_per_search: int = 12
    # Overridable so a site changing its URL scheme is a .env edit, not a patch.
    # {q} is the URL-encoded query.
    amazon_search_template: str = "https://www.amazon.com/s?k={q}"
    walmart_search_template: str = "https://www.walmart.com/search?q={q}"
    facebook_search_template: str = (
        "https://www.facebook.com/marketplace/search/?query={q}&radius_km=24"
    )

    # ---- notifications ---------------------------------------------------
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_use_tls: bool = True
    email_from: str | None = None
    email_to: str | None = None
    notify_min_profit: float = 25.0
    digest_hour_local: int = 7

    # ---- scheduler --------------------------------------------------------
    watch_sweep_minutes: int = 20
    snapshot_poll_minutes: int = 15
    closing_soon_minutes: int = 90  # poll tracked lots faster inside this window
    harvest_interval_minutes: int = 30

    # ---- web ---------------------------------------------------------------
    web_host: str = "127.0.0.1"
    web_port: int = 8787
    api_token: str | None = None  # shared secret for the browser extension

    timezone: str = "America/New_York"

    @field_validator("target_margin", "sales_tax_rate", "default_buyers_premium")
    @classmethod
    def _sane_rate(cls, v: float) -> float:
        if not 0.0 <= v < 1.0:
            raise ValueError(f"rate must be in [0, 1), got {v}")
        return v

    @property
    def enabled_lookup_sites(self) -> list[str]:
        return [s.strip().lower() for s in self.lookup_sites.split(",") if s.strip()]

    @property
    def email_recipients(self) -> list[str]:
        if not self.email_to:
            return []
        return [addr.strip() for addr in self.email_to.split(",") if addr.strip()]

    @property
    def email_configured(self) -> bool:
        return bool(self.smtp_host and self.email_from and self.email_recipients)


@lru_cache
def get_settings() -> Settings:
    return Settings()
