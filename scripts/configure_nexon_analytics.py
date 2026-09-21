#!/usr/bin/env python3
"""Inject the configured public NEXON Analytics tag into the static project page."""

from __future__ import annotations

import os
from pathlib import Path

from maple_chat.nexon.analytics import canonical_analytics_script, inject_analytics_script

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "Maple_chat_site" / "index.html"


def main() -> None:
    configured = os.environ.get("NEXON_ANALYTICS_SCRIPT", "")
    if not configured:
        raise SystemExit("NEXON_ANALYTICS_SCRIPT is not configured")
    script = canonical_analytics_script(configured)
    current = INDEX.read_text(encoding="utf-8")
    updated = inject_analytics_script(current, script)
    INDEX.write_text(updated, encoding="utf-8")
    print("NEXON Open API Analytics configured in Maple_chat_site/index.html")


if __name__ == "__main__":
    main()
