"""Capture live page artifacts for parser calibration.

The ingestion adapters were written against synthetic fixtures because the build
environment cannot reach nellisauction.com. This command closes that gap: run it
once on a machine that *can* reach the site, and it produces a single zip
containing everything needed to calibrate the parsers against real payloads.

It uses the ordinary `PoliteClient`, so it inherits robots.txt handling, rate
limiting and the hard stop on 403/429 — no new network behavior is introduced
here. It reads the same public pages a browser would, and captures nothing that
requires logging in.

    nellis record            # search page + one lot found on it
    nellis record 1234567    # search page + that specific lot
"""

from __future__ import annotations

import json
import logging
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .adapter import LotRecord
from .client import PoliteClient
from .html_parse import parse_lot_cards
from .remix_json import discover_route_ids, extract_json_blobs, lots_from_blobs

log = logging.getLogger(__name__)

DEFAULT_OUT = Path("fixtures/live")

# Anything that looks like a credential is stripped before writing. These pages
# are public and shouldn't contain any, but a capture is meant to be shareable,
# so we don't rely on that assumption.
SENSITIVE_KEYS = {
    "token", "accesstoken", "refreshtoken", "authorization", "auth", "cookie",
    "session", "sessionid", "password", "secret", "apikey", "api_key",
    "email", "phone", "address", "customerid", "userid", "user",
}


def scrub(node: Any, depth: int = 0) -> Any:
    """Recursively redact credential-ish keys from a payload."""
    if depth > 14:
        return node
    if isinstance(node, dict):
        cleaned = {}
        for key, value in node.items():
            flat = str(key).lower().replace("_", "")
            cleaned[key] = "<redacted>" if flat in SENSITIVE_KEYS else scrub(value, depth + 1)
        return cleaned
    if isinstance(node, list):
        return [scrub(item, depth + 1) for item in node[:200]]
    return node


@dataclass
class StrategyProbe:
    strategy: str
    lots_found: int
    sample_id: str | None = None
    resolved_fields: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class CaptureReport:
    captured_at: str
    base_url: str
    search_lots: int
    route_ids: list[str]
    json_blob_count: int
    probes: list[StrategyProbe]
    notes: list[str] = field(default_factory=list)


# Fields that must resolve for valuation to be meaningful. If these come back
# empty on the live site, that's precisely what needs fixing in ALIASES.
CRITICAL = (
    "nellis_id", "title", "retail_price", "current_bid", "close_at",
    "condition_name", "category", "brand", "buyers_premium_rate", "description",
)


def _probe(name: str, records: list[LotRecord]) -> StrategyProbe:
    if not records:
        return StrategyProbe(strategy=name, lots_found=0)
    sample = max(records, key=lambda r: sum(1 for f in CRITICAL if getattr(r, f, None)))
    resolved, missing = [], []
    for attr in CRITICAL:
        value = getattr(sample, attr, None)
        (resolved if value not in (None, "", 0, []) else missing).append(attr)
    return StrategyProbe(
        strategy=name,
        lots_found=len(records),
        sample_id=sample.nellis_id,
        resolved_fields=resolved,
        missing_fields=missing,
    )


async def capture(
    client: PoliteClient,
    lot_id: str | None = None,
    out_dir: Path | None = None,
    *,
    query: str = "tool",
) -> tuple[Path, CaptureReport]:
    """Fetch the search page and one lot page; write everything to a zip."""
    out_dir = out_dir or DEFAULT_OUT
    out_dir.mkdir(parents=True, exist_ok=True)
    base = client.settings.nellis_base_url
    files: dict[str, str] = {}
    notes: list[str] = []

    # ---- search page ----------------------------------------------------
    search = await client.get("/search", params={"query": query}, use_cache=False)
    files["search.html"] = search.text

    blobs = extract_json_blobs(search.text)
    for index, blob in enumerate(blobs[:12]):
        files[f"search_blob_{index}.json"] = json.dumps(scrub(blob), indent=2, default=str)

    route_ids = discover_route_ids(search.text)
    json_records = lots_from_blobs(blobs, base)
    dom_records = parse_lot_cards(search.text, base)

    probes = [
        _probe("embedded-json", json_records),
        _probe("html-dom", dom_records),
    ]

    # ---- one lot detail page --------------------------------------------
    target = lot_id or (json_records[0].nellis_id if json_records else None)
    if target is None and dom_records:
        target = dom_records[0].nellis_id

    if target is None:
        notes.append(
            "No lot ID could be discovered from the search page — both strategies "
            "returned nothing. search.html is the file to look at."
        )
    else:
        detail = await client.get(f"/p/{target}", use_cache=False)
        files["lot.html"] = detail.text
        detail_blobs = extract_json_blobs(detail.text)
        for index, blob in enumerate(detail_blobs[:12]):
            files[f"lot_blob_{index}.json"] = json.dumps(scrub(blob), indent=2, default=str)
        probes.append(_probe("lot-detail", lots_from_blobs(detail_blobs, base)))

        # The ?_data= loader endpoint, if the route IDs we found are usable.
        for route in [r for r in route_ids if "p." in r][:2]:
            try:
                loader = await client.get(
                    f"/p/{target}", params={"_data": route},
                    accept_json=True, use_cache=False,
                )
                files[f"loader_{route.replace('/', '_')}.json"] = loader.text[:400_000]
            except Exception as exc:
                notes.append(f"loader route {route} unusable: {exc}")

    if not route_ids:
        notes.append(
            "No Remix route IDs found — the site may no longer be a Remix app. "
            "The remix-data strategy will not work; embedded-json or DOM parsing "
            "will have to carry."
        )

    report = CaptureReport(
        captured_at=datetime.now(UTC).isoformat(),
        base_url=base,
        search_lots=len(json_records) or len(dom_records),
        route_ids=route_ids[:40],
        json_blob_count=len(blobs),
        probes=probes,
        notes=notes,
    )
    files["report.json"] = json.dumps(asdict(report), indent=2)

    archive = out_dir / "capture.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)

    log.info("capture written to %s (%d files)", archive, len(files))
    return archive, report
