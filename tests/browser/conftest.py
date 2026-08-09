"""Browser tests are opt-in.

They need a real Chromium and take tens of seconds, so the default `pytest` run
must never collect them — an ordinary run stays fast and fully offline.

Enable with:

    NELLIS_BROWSER_TESTS=1 pytest tests/browser -o asyncio_mode=strict

`asyncio_mode=strict` matters: the project default is `auto`, and the sync
Playwright API cannot run inside pytest-asyncio's event loop.
"""

from __future__ import annotations

import os

collect_ignore_glob = []

if os.environ.get("NELLIS_BROWSER_TESTS") != "1":
    collect_ignore_glob = ["test_*.py"]
