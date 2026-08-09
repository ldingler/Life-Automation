"""Browser tests for the Chrome extension.

The extension is the one component that can't be tested by importing it — it
only means anything inside a browser, injected into a page, talking to a running
engine. Everything else here has unit coverage; without this, ~400 lines of JS
ship completely unverified.

What's exercised:
  * the MV3 extension loads at all (valid manifest, service worker starts)
  * the content script injects its panel into a lot page
  * it fetches the valuation from the engine and renders real numbers
  * it finds the max-bid field by *heuristics*, against markup that is
    deliberately NOT a copy of Nellis' own — proving it doesn't secretly depend
    on their class names
  * pre-filling fires input events, so framework-tracked inputs actually
    register the value instead of silently ignoring it
  * it never submits a bid on its own

Opt-in (see conftest.py):

    NELLIS_BROWSER_TESTS=1 pytest tests/browser -o asyncio_mode=strict
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EXTENSION_DIR = REPO_ROOT / "extension"
MOCK_PAGE = Path(__file__).parent / "mock_lot_page.html"
CHROMIUM = Path("/opt/pw-browsers/chromium")

pytest.importorskip("playwright.sync_api", reason="playwright not installed")
pytestmark = pytest.mark.skipif(not CHROMIUM.exists(), reason="no chromium binary")

LOT_ID = "700105"

VALUATION = {
    "queue_id": 7,
    "nellis_id": LOT_ID,
    "title": "DeWalt DCD999B 20V MAX XR Hammer Drill (Tool Only)",
    "url": f"https://www.nellisauction.com/p/{LOT_ID}",
    "current_bid": 12.0,
    "retail_price": 299.0,
    "suggested_max_bid": 24.0,
    "projected_profit": 20.59,
    "projected_margin": 0.41,
    "comp_value": 96.5,
    "comp_count": 14,
    "confidence": "high",
    "condition": "Open Box",
    "close_at": None,
    "exposure_if_won": 29.43,
    "rank_score": 0.7,
    "status": "pending",
    "block_reason": None,
    "repair_notes": None,
    "missing_parts": [],
    "reason": "Bid up to $24.00. Projected profit $20.59 (high confidence).",
}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class MockSite:
    """Serves the fake lot page over real HTTP at /p/<id>.

    Playwright's request interception does not inject content scripts into the
    main frame, so routing a fake response at `nellisauction.com` produces a page
    the extension never touches — which looks like a passing load and tests
    nothing. Serving over real HTTP makes Chrome perform a genuine navigation,
    which is what actually triggers injection.
    """

    def __init__(self, html: str):
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.port = _free_port()
        self.server = HTTPServer(("127.0.0.1", self.port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def lot_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/p/{LOT_ID}"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()


class FakeEngine:
    """Stands in for `nellis serve`.

    The contract under test is the extension's, not the engine's — the engine
    API has its own tests — so this keeps the browser test independent of how
    the database happens to be seeded.
    """

    def __init__(self, payload: dict):
        from http.server import BaseHTTPRequestHandler, HTTPServer

        self.payload = payload
        self.requests: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code: int, body: dict):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "*")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_OPTIONS(self):
                self._send(200, {})

            def do_GET(self):
                outer.requests.append(self.path)
                if self.path.startswith(f"/api/lot/{LOT_ID}"):
                    self._send(200, outer.payload)
                elif self.path == "/healthz":
                    self._send(200, {"ok": True})
                else:
                    self._send(404, {"detail": "not found"})

            def do_POST(self):
                outer.requests.append("POST " + self.path)
                self._send(200, {"total_exposure": 0.0, "open_lots": 0,
                                 "max_exposure": 1500.0, "headroom": 1500.0,
                                 "by_category": {}})

        self.port = _free_port()
        self.server = HTTPServer(("127.0.0.1", self.port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()


@pytest.fixture
def engine():
    server = FakeEngine(VALUATION).start()
    yield server
    server.stop()


@pytest.fixture(scope="module")
def site():
    server = MockSite(MOCK_PAGE.read_text()).start()
    yield server
    server.stop()


@pytest.fixture(scope="module")
def loadable_extension(tmp_path_factory):
    """A copy of the real extension with the local test origin added to matches.

    Only `content_scripts[].matches` is widened — every line of JS under test is
    the shipped file, byte for byte. The production match patterns are asserted
    separately in TestExtensionLoads, so nothing about the real targeting goes
    unchecked.
    """
    import shutil

    target = tmp_path_factory.mktemp("ext") / "extension"
    shutil.copytree(EXTENSION_DIR, target)

    manifest = json.loads((target / "manifest.json").read_text())
    for entry in manifest["content_scripts"]:
        entry["matches"] = list(entry["matches"]) + ["http://127.0.0.1/*", "http://localhost/*"]
    manifest["host_permissions"] = list(manifest.get("host_permissions", [])) + [
        "http://127.0.0.1/*", "http://localhost/*"
    ]
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return target


@pytest.fixture(scope="module")
def context(tmp_path_factory, loadable_extension):
    """Chromium with the unpacked extension loaded.

    Extensions require a persistent context; a plain launch() can't load them.
    Module-scoped because a browser launch costs several seconds.
    """
    from playwright.sync_api import sync_playwright

    profile = tmp_path_factory.mktemp("chrome-profile")
    playwright = sync_playwright().start()
    ctx = playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile),
        executable_path=str(CHROMIUM),
        headless=True,
        args=[
            f"--disable-extensions-except={loadable_extension}",
            f"--load-extension={loadable_extension}",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
        timeout=45_000,
    )
    yield ctx
    ctx.close()
    playwright.stop()


def _worker(ctx, timeout: float = 20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ctx.service_workers:
            return ctx.service_workers[0]
        time.sleep(0.2)
    return None


def _configure(ctx, base_url: str) -> None:
    """Write the engine address into chrome.storage, as the popup would."""
    worker = _worker(ctx)
    assert worker is not None, "extension service worker never started"
    worker.evaluate(
        "([base]) => chrome.storage.sync.set({apiBase: base, apiToken: '', enterToBid: false})",
        [base_url],
    )


def _open_lot(ctx, site):
    """Real navigation to the mock lot page, so content scripts actually inject."""
    page = ctx.new_page()
    page.goto(site.lot_url, wait_until="load")
    return page


class TestExtensionLoads:
    def test_manifest_valid_and_worker_starts(self, context):
        """A malformed manifest means the extension silently never runs."""
        manifest = json.loads((EXTENSION_DIR / "manifest.json").read_text())
        assert manifest["manifest_version"] == 3

        worker = _worker(context)
        assert worker is not None, "service worker did not start — manifest is broken"
        assert worker.url.startswith("chrome-extension://")

    def test_production_match_patterns_target_nellis_lot_pages(self):
        """The loaded copy widens `matches` for testing; the shipped file must not."""
        manifest = json.loads((EXTENSION_DIR / "manifest.json").read_text())
        patterns = [m for cs in manifest["content_scripts"] for m in cs["matches"]]
        assert "https://www.nellisauction.com/p/*" in patterns
        assert all("nellisauction.com" in p for p in patterns), \
            f"content script must only run on Nellis lot pages, got {patterns}"


class TestPanelOnLotPage:
    def test_panel_injects_with_real_numbers(self, context, engine, site):
        _configure(context, engine.base)
        page = _open_lot(context, site)
        try:
            panel = page.locator("#nde-panel")
            panel.wait_for(state="visible", timeout=20_000)
            text = panel.inner_text()

            assert "$24.00" in text, f"max bid missing; panel showed: {text!r}"
            assert "HIGH" in text.upper()
            assert "14" in text  # comp count
            assert "You place every bid" in text
            assert any(f"/api/lot/{LOT_ID}" in r for r in engine.requests), \
                "extension never called the engine"
        finally:
            page.close()

    def test_prefills_bid_field_found_by_heuristic(self, context, engine, site):
        """The field is located by shape, not by Nellis' class names."""
        _configure(context, engine.base)
        page = _open_lot(context, site)
        try:
            page.locator("#nde-panel").wait_for(state="visible", timeout=20_000)
            page.locator("#maxBid").wait_for(state="attached", timeout=20_000)
            page.locator('#nde-panel [data-nde="fill"]').click()
            page.wait_for_function(
                "() => document.getElementById('maxBid')?.value === '24'", timeout=10_000
            )
            assert page.locator("#maxBid").input_value() == "24"
        finally:
            page.close()

    def test_prefill_notifies_framework_state(self, context, engine, site):
        """Setting .value alone updates the DOM but not React/Remix state.

        content.js uses the native setter plus synthetic events. If that
        regresses, the box looks filled while the site ignores it — the worst
        failure mode, because it fails silently at bid time.
        """
        _configure(context, engine.base)
        page = _open_lot(context, site)
        try:
            page.locator("#nde-panel").wait_for(state="visible", timeout=20_000)
            page.locator("#maxBid").wait_for(state="attached", timeout=20_000)
            page.locator('#nde-panel [data-nde="fill"]').click()
            page.wait_for_function(
                "() => window.__trackedValue?.() === '24'", timeout=10_000
            )
        finally:
            page.close()

    def test_never_submits_a_bid_on_its_own(self, context, engine, site):
        """The single most important assertion in this file."""
        _configure(context, engine.base)
        page = _open_lot(context, site)
        try:
            page.locator("#nde-panel").wait_for(state="visible", timeout=20_000)
            page.locator("#maxBid").wait_for(state="attached", timeout=20_000)
            page.locator('#nde-panel [data-nde="fill"]').click()
            page.wait_for_timeout(1500)

            assert page.locator("#placed").inner_text() == "", \
                "the extension submitted a bid with no human action — must never happen"
            assert not any(r.startswith("POST") for r in engine.requests), \
                "extension posted to the engine without a click"
        finally:
            page.close()

    def test_marking_placed_records_the_commitment(self, context, engine, site):
        _configure(context, engine.base)
        page = _open_lot(context, site)
        try:
            page.locator("#nde-panel").wait_for(state="visible", timeout=20_000)
            page.locator('#nde-panel [data-nde="placed"]').click()
            page.wait_for_timeout(1500)
            assert any("confirm" in r for r in engine.requests)
        finally:
            page.close()

    def test_degrades_quietly_when_engine_is_down(self, context, engine, site):
        """Engine offline must not break the Nellis page itself."""
        engine.stop()
        _configure(context, engine.base)
        page = _open_lot(context, site)
        try:
            page.wait_for_timeout(3000)
            assert page.locator("h1").is_visible(), "page broke when engine was down"
            assert page.locator("#nde-panel").count() == 0
        finally:
            page.close()
