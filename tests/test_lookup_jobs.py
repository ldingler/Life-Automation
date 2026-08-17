"""Browser-driven price lookups: classification, pacing, and stopping when told.

Two things are being protected here.

The first is valuation quality: a search page is full of accessories, knock-offs
and multi-packs, and letting any of them through as "the price of this item"
poisons the ceiling logic that decides whether to bid at all.

The second is restraint. The pacing rules are the whole reason this design is
acceptable — one search at a time, minutes apart, a daily ceiling, and a full
stop when a site pushes back. Those are asserted as hard behaviour, not left as
comments, because they're the kind of thing that quietly erodes into "just one
more request" otherwise.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nellis.market.classify import (
    Rejected,
    classify_all,
    classify_candidate,
    multipack_count,
)
from nellis.market.jobs import (
    check_pacing,
    complete,
    enqueue_for_lot,
    lease_next,
    mark_blocked,
    mark_failed,
    priority_for_lot,
    queue_status,
    reclaim_stale,
    search_url,
)
from nellis.models import LookupJob, LookupSite, LookupStatus, Lot, MarketPrice, PriceKind

NOW = datetime.now(UTC)


def make_lot(session, **kwargs):
    defaults = dict(
        nellis_id="L1",
        url="https://www.nellisauction.com/p/L1",
        title="DeWalt DCD777C2 20V Max Cordless Drill Driver Kit",
        brand="DeWalt",
        model="DCD777C2",
        retail_price=179.0,
        current_bid=60.0,
        close_at=NOW + timedelta(hours=10),
    )
    defaults.update(kwargs)
    lot = Lot(**defaults)
    session.add(lot)
    session.flush()
    return lot


def card(title, price, **kwargs):
    return {"title": title, "price": price, **kwargs}


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


class TestClassification:
    LOT = "DeWalt DCD777C2 20V Max Cordless Drill Driver Kit"

    def test_same_model_number_is_an_exact_match(self):
        record = classify_candidate(
            card("DEWALT 20V MAX Cordless Drill Driver Kit, DCD777C2", 169.0),
            lot_title=self.LOT,
            brand="DeWalt",
            model="DCD777C2",
        )
        assert record["kind"] == PriceKind.EXACT_NEW.value

    def test_used_wording_demotes_to_exact_used(self):
        record = classify_candidate(
            card("DEWALT DCD777C2 Drill Kit - Renewed", 119.0),
            lot_title=self.LOT,
            brand="DeWalt",
            model="DCD777C2",
        )
        assert record["kind"] == PriceKind.EXACT_USED.value

    def test_a_different_brand_is_a_substitute_not_a_verification(self):
        """A rival drill is a fair alternative. It does not verify DeWalt's price."""
        record = classify_candidate(
            card("Ryobi 20V Cordless Drill Driver Kit", 89.0, rating=4.4, review_count=900),
            lot_title=self.LOT,
            brand="DeWalt",
        )
        assert record["kind"] == PriceKind.SUBSTITUTE_NEW.value

    def test_accessory_is_rejected_outright(self):
        """The $12 case is the single most damaging false comparable."""
        with pytest.raises(Rejected, match="accessory"):
            classify_candidate(
                card("Hard Carrying Case for DEWALT DCD777C2 Drill", 12.99),
                lot_title=self.LOT,
                brand="DeWalt",
            )

    def test_replacement_battery_is_rejected(self):
        with pytest.raises(Rejected):
            classify_candidate(
                card("Replacement 20V Battery compatible with DeWalt drills", 24.99),
                lot_title=self.LOT,
                brand="DeWalt",
            )

    def test_an_accessory_lot_still_matches_accessories(self):
        """When the lot IS a case, a case listing is exactly right."""
        record = classify_candidate(
            card("Hard Carrying Case for DEWALT DCD777C2 Drill", 12.99),
            lot_title="Hard Carrying Case for DeWalt Drill",
        )
        assert record["price"] == 12.99

    def test_different_product_class_is_rejected(self):
        with pytest.raises(Rejected, match="different product class"):
            classify_candidate(
                card("Ninja Blender 1000W Kitchen Countertop", 79.0),
                lot_title=self.LOT,
                brand="DeWalt",
            )

    def test_multipack_is_priced_per_unit(self):
        """A 4-pack at $80 is $20 each, not an $80 comparable."""
        assert multipack_count("Widget (4-Pack) Heavy Duty") == 4
        record = classify_candidate(
            card("Cordless Drill Driver Kit (2-Pack)", 200.0, rating=4.3, review_count=300),
            lot_title=self.LOT,
        )
        assert record["price"] == 100.0
        assert "2-pack" in record["note"]

    def test_no_price_is_rejected(self):
        with pytest.raises(Rejected, match="price"):
            classify_candidate(card("DEWALT DCD777C2", None), lot_title=self.LOT)

    def test_classify_all_dedupes_and_reports_rejections(self):
        kept, rejects = classify_all(
            [
                card("DEWALT DCD777C2 Drill Kit", 169.0, url="https://a/1"),
                card("DEWALT DCD777C2 Drill Kit", 169.0, url="https://a/1"),  # dupe
                card("Case for DEWALT DCD777C2", 12.0, url="https://a/2"),
            ],
            lot_title=self.LOT,
            brand="DeWalt",
            model="DCD777C2",
        )
        assert len(kept) == 1
        assert any("accessory" in r for r in rejects)


# --------------------------------------------------------------------------
# Queueing
# --------------------------------------------------------------------------


class TestEnqueue:
    def test_enqueues_one_job_per_site(self, db):
        lot = make_lot(db)
        jobs = enqueue_for_lot(db, lot)
        assert {j.site for j in jobs} == {
            LookupSite.AMAZON,
            LookupSite.WALMART,
            LookupSite.FACEBOOK,
        }

    def test_is_idempotent(self, db):
        lot = make_lot(db)
        enqueue_for_lot(db, lot)
        assert enqueue_for_lot(db, lot) == []

    def test_skips_when_prices_are_already_fresh(self, db):
        """Re-checking yesterday's answer wastes the day's search budget."""
        lot = make_lot(db)
        from nellis.normalize import normalize_item_key

        key = normalize_item_key(lot.title, brand=lot.brand, model=lot.model, upc=lot.upc)
        db.add(
            MarketPrice(
                query_key=key,
                kind=PriceKind.EXACT_NEW,
                source="amazon",
                title="DeWalt DCD777C2",
                price=169.0,
                observed_at=NOW - timedelta(days=1),
            )
        )
        db.flush()
        assert enqueue_for_lot(db, lot) == []

    def test_stale_prices_do_not_block_a_recheck(self, db):
        lot = make_lot(db)
        from nellis.normalize import normalize_item_key

        key = normalize_item_key(lot.title, brand=lot.brand, model=lot.model, upc=lot.upc)
        db.add(
            MarketPrice(
                query_key=key,
                kind=PriceKind.EXACT_NEW,
                source="amazon",
                title="DeWalt DCD777C2",
                price=169.0,
                observed_at=NOW - timedelta(days=90),
            )
        )
        db.flush()
        assert enqueue_for_lot(db, lot)

    def test_closing_soon_outranks_everything(self, db):
        soon = make_lot(db, nellis_id="A", current_bid=100.0, close_at=NOW + timedelta(minutes=30))
        later = make_lot(
            db, nellis_id="B", current_bid=100.0, close_at=NOW + timedelta(days=2)
        )
        assert priority_for_lot(soon, now=NOW) > priority_for_lot(later, now=NOW)

    def test_search_url_encodes_the_query(self, settings):
        url = search_url(LookupSite.AMAZON, "dewalt drill 20v", settings)
        assert "dewalt+drill+20v" in url


# --------------------------------------------------------------------------
# Pacing — the part that makes this acceptable at all
# --------------------------------------------------------------------------


class TestPacing:
    def test_only_one_job_is_ever_handed_out(self, db):
        lot = make_lot(db)
        enqueue_for_lot(db, lot)

        first, _ = lease_next(db, now=NOW)
        assert first is not None

        # Immediately asking again gets nothing, regardless of queue depth.
        second, pacing = lease_next(db, now=NOW)
        assert second is None
        assert not pacing.allowed
        assert pacing.retry_after_seconds > 0

    def test_same_site_waits_much_longer_than_a_different_site(self, db, settings):
        lot = make_lot(db)
        enqueue_for_lot(db, lot)

        first, _ = lease_next(db, now=NOW)
        assert first is not None

        # Past the global gap, but not the per-site one.
        later = NOW + timedelta(seconds=settings.lookup_min_seconds_between + 1)
        second, _ = lease_next(db, now=later)
        assert second is not None
        assert second.site != first.site

    def test_daily_ceiling_stops_everything(self, db, monkeypatch, settings):
        monkeypatch.setattr(settings, "lookup_max_per_day", 2)
        for i in range(2):
            db.add(
                LookupJob(
                    query_key=f"k{i}",
                    query="q",
                    site=LookupSite.AMAZON,
                    status=LookupStatus.DONE,
                    leased_at=NOW - timedelta(hours=1),
                )
            )
        db.flush()

        pacing = check_pacing(db, now=NOW, settings=settings)
        assert not pacing.allowed
        assert "daily search ceiling" in pacing.reason

    def test_a_block_takes_the_whole_site_offline(self, db, settings):
        lot = make_lot(db)
        jobs = enqueue_for_lot(db, lot)
        amazon = next(j for j in jobs if j.site == LookupSite.AMAZON)

        mark_blocked(db, amazon, "CAPTCHA shown", now=NOW)

        # Not just this job — every future Amazon lookup, for hours.
        pacing = check_pacing(db, LookupSite.AMAZON, now=NOW + timedelta(minutes=5))
        assert not pacing.allowed
        assert "asked us to stop" in pacing.reason

        # And other sites are unaffected; we stop where we were told to stop.
        assert check_pacing(db, LookupSite.WALMART, now=NOW + timedelta(minutes=5)).allowed

    def test_the_block_cooldown_does_eventually_lift(self, db, settings):
        lot = make_lot(db)
        jobs = enqueue_for_lot(db, lot)
        mark_blocked(db, jobs[0], "CAPTCHA shown", now=NOW)

        after = NOW + timedelta(minutes=settings.lookup_block_cooldown_minutes + 1)
        assert check_pacing(db, jobs[0].site, now=after).allowed

    def test_lookups_can_be_switched_off_entirely(self, db, monkeypatch, settings):
        monkeypatch.setattr(settings, "lookup_enabled", False)
        pacing = check_pacing(db, now=NOW, settings=settings)
        assert not pacing.allowed
        assert "switched off" in pacing.reason


class TestLeaseRecovery:
    def test_an_abandoned_lease_goes_back_in_the_queue(self, db, settings):
        lot = make_lot(db)
        enqueue_for_lot(db, lot)
        leased, _ = lease_next(db, now=NOW)
        assert leased is not None

        later = NOW + timedelta(seconds=settings.lookup_lease_timeout_seconds + 1)
        assert reclaim_stale(db, now=later) == 1

        job = db.get(LookupJob, leased.job_id)
        assert job.status == LookupStatus.PENDING

    def test_repeatedly_abandoned_jobs_give_up(self, db, monkeypatch, settings):
        monkeypatch.setattr(settings, "lookup_max_attempts", 1)
        lot = make_lot(db)
        enqueue_for_lot(db, lot)
        leased, _ = lease_next(db, now=NOW, settings=settings)

        later = NOW + timedelta(seconds=settings.lookup_lease_timeout_seconds + 1)
        reclaim_stale(db, now=later, settings=settings)

        job = db.get(LookupJob, leased.job_id)
        assert job.status == LookupStatus.FAILED

    def test_a_failure_retries_once_then_stops(self, db, settings):
        lot = make_lot(db)
        enqueue_for_lot(db, lot)
        leased, _ = lease_next(db, now=NOW)
        job = db.get(LookupJob, leased.job_id)

        mark_failed(db, job, "page didn't load")
        assert job.status == LookupStatus.PENDING

        job.attempts = settings.lookup_max_attempts
        mark_failed(db, job, "page didn't load again")
        assert job.status == LookupStatus.FAILED


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------


class TestCompletion:
    def test_results_become_prices_the_ceiling_can_use(self, db):
        from nellis.market.lookup import market_view_for_lot

        lot = make_lot(db)
        enqueue_for_lot(db, lot)
        leased, _ = lease_next(db, now=NOW)
        job = db.get(LookupJob, leased.job_id)

        result = complete(
            db,
            job,
            [
                card("DEWALT DCD777C2 20V MAX Cordless Drill Kit", 169.0, url="https://a/1",
                     rating=4.8, review_count=32000),
                card("Ryobi 20V Cordless Drill Driver Kit", 79.0, url="https://a/2",
                     rating=4.4, review_count=5200),
                card("Carrying Case for DEWALT DCD777C2", 14.99, url="https://a/3"),
            ],
        )

        assert result.recorded == 2  # the case is thrown out
        assert job.status == LookupStatus.DONE

        view = market_view_for_lot(db, lot)
        assert view.has_exact
        assert view.exact_new_price == 169.0
        # The Ryobi is a real, well-reviewed alternative, so it sets the ceiling.
        assert view.best_alternative is not None
        assert view.best_alternative.price == 79.0

    def test_a_search_with_nothing_usable_is_recorded_as_empty(self, db):
        lot = make_lot(db)
        enqueue_for_lot(db, lot)
        leased, _ = lease_next(db, now=NOW)
        job = db.get(LookupJob, leased.job_id)

        result = complete(db, job, [card("Ninja Blender 1000W", 79.0)])
        assert result.recorded == 0
        assert job.status == LookupStatus.EMPTY
        assert "none usable" in job.note

    def test_status_reports_what_is_backed_off(self, db):
        lot = make_lot(db)
        jobs = enqueue_for_lot(db, lot)
        mark_blocked(db, jobs[0], "CAPTCHA shown", now=NOW)

        status = queue_status(db, now=NOW)
        blocked = [s for s in status["sites"] if s["blocked_until"]]
        assert len(blocked) == 1
        assert status["counts"]["blocked"] == 1


# --------------------------------------------------------------------------
# Matching regressions
# --------------------------------------------------------------------------


class TestSpecsAreNotModelNumbers:
    """A voltage is not an identifier, and a pack count is not either.

    Both were being read as model numbers, in opposite and equally wrong
    directions: "20V" made every 20V tool look like the same product, and
    "(2-Pack)" made a valid comparable look like a different one.
    """

    def test_voltage_alone_does_not_make_two_tools_the_same_product(self):
        from nellis.normalize import title_similarity

        assert (
            title_similarity(
                "DeWalt DCD777C2 20V Max Cordless Drill",
                "Ryobi 20V Cordless Drill Driver Kit",
            )
            < 0.9
        )

    def test_a_pack_count_does_not_disqualify_a_comparable(self):
        from nellis.normalize import extract_model_numbers

        assert extract_model_numbers("Cordless Drill Driver Kit (2-Pack)") == []

    def test_real_model_numbers_still_survive(self):
        from nellis.normalize import extract_model_numbers

        assert "dcd777c2" in extract_model_numbers("DeWalt DCD777C2 20V Max Drill")
        assert "m18" in extract_model_numbers("Milwaukee M18 Fuel 5.0Ah")


# --------------------------------------------------------------------------
# The HTTP surface the extension actually talks to
# --------------------------------------------------------------------------


class TestLookupApi:
    @pytest.fixture
    def client(self, db):
        from fastapi.testclient import TestClient

        from nellis.web.app import create_app

        with TestClient(create_app()) as test_client:
            yield test_client

    def test_lease_returns_a_job_with_a_ready_made_url(self, db, client):
        lot = make_lot(db)
        enqueue_for_lot(db, lot)
        db.commit()

        body = client.get("/api/market/jobs").json()
        assert body["job"]["url"].startswith("https://")
        assert body["job"]["lot_id"] == lot.nellis_id

    def test_an_empty_lease_explains_itself(self, db, client):
        """Silence would just make the extension poll harder."""
        body = client.get("/api/market/jobs").json()
        assert body["job"] is None
        assert body["reason"]
        assert body["retry_after_seconds"] >= 30

    def test_posting_results_records_prices(self, db, client):
        lot = make_lot(db)
        enqueue_for_lot(db, lot)
        db.commit()
        job_id = client.get("/api/market/jobs").json()["job"]["job_id"]

        body = client.post(
            f"/api/market/jobs/{job_id}/result",
            json={
                "candidates": [
                    {
                        "title": "DEWALT DCD777C2 20V MAX Drill Kit",
                        "price": 169.0,
                        "url": "https://a/1",
                        "rating": 4.8,
                        "review_count": 32000,
                    }
                ]
            },
        ).json()
        assert body["recorded"] == 1
        assert body["status"] == "done"

    def test_reporting_a_block_backs_the_site_off(self, db, client):
        lot = make_lot(db)
        enqueue_for_lot(db, lot)
        db.commit()
        job = client.get("/api/market/jobs").json()["job"]

        body = client.post(
            f"/api/market/jobs/{job['job_id']}/blocked",
            json={"reason": "CAPTCHA shown"},
        ).json()
        assert body["status"] == "blocked"
        assert body["cooldown_minutes"] > 0

        status = client.get("/api/market/status").json()
        assert any(s["blocked_until"] for s in status["sites"])

    def test_unknown_job_is_a_404(self, client):
        assert client.post("/api/market/jobs/9999/result", json={"candidates": []}).status_code == 404


class TestSchemaUpgrade:
    """An older database must keep working after a pull.

    `create_all` adds missing tables but silently skips missing columns, so a
    database from an earlier version fails at write time with
    "table valuations has no column named ..." — an error that tells the
    operator nothing about what to do next.
    """

    def test_a_column_added_since_the_db_was_created_is_backfilled(self, tmp_path, monkeypatch):
        from sqlalchemy import inspect, text

        from nellis.db import get_engine, init_db, reset_engine

        monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'old.db'}")
        from nellis.config import get_settings

        get_settings.cache_clear()
        reset_engine()

        init_db()
        engine = get_engine()
        # Simulate a database created before the column existed.
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE valuations DROP COLUMN market_verification"))
        assert "market_verification" not in {
            c["name"] for c in inspect(engine).get_columns("valuations")
        }

        init_db()
        assert "market_verification" in {
            c["name"] for c in inspect(engine).get_columns("valuations")
        }
