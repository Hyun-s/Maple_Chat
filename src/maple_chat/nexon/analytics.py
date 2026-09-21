"""Validation and static-site injection for NEXON Open API Analytics."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlparse

_START_MARKER = "    <!-- nexon-open-api-analytics:start -->"
_END_MARKER = "    <!-- nexon-open-api-analytics:end -->"
_ALLOWED_PATHS = frozenset({"/analytics.js", "/js/analytics.js"})
_APP_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class _ScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.has_data = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.has_data = True


def canonical_analytics_script(value: str) -> str:
    """Return a canonical public tag after enforcing the official analytics boundary."""
    parser = _ScriptParser()
    parser.feed(value.strip())
    if parser.has_data or len(parser.tags) != 1 or parser.tags[0][0] != "script":
        raise ValueError("NEXON_ANALYTICS_SCRIPT must contain exactly one script tag")
    attrs = parser.tags[0][1]
    source = attrs.get("src") or ""
    parsed = urlparse(source)
    query = parse_qs(parsed.query, keep_blank_values=True)
    app_ids = query.get("app_id", [])
    if (
        parsed.scheme != "https"
        or parsed.hostname != "openapi.nexon.com"
        or parsed.path not in _ALLOWED_PATHS
        or parsed.fragment
        or set(query) != {"app_id"}
        or len(app_ids) != 1
        or _APP_ID.fullmatch(app_ids[0]) is None
        or "async" not in attrs
    ):
        raise ValueError("NEXON_ANALYTICS_SCRIPT is outside the official analytics boundary")
    canonical_url = f"https://openapi.nexon.com{parsed.path}?{urlencode({'app_id': app_ids[0]})}"
    return f'<script type="text/javascript" src="{canonical_url}" async></script>'


def inject_analytics_script(document: str, script: str) -> str:
    """Idempotently place a validated analytics script inside the document head."""
    block = f"{_START_MARKER}\n    {script}\n{_END_MARKER}"
    if _START_MARKER in document or _END_MARKER in document:
        if document.count(_START_MARKER) != 1 or document.count(_END_MARKER) != 1:
            raise ValueError("analytics markers are inconsistent")
        start = document.index(_START_MARKER)
        end = document.index(_END_MARKER, start) + len(_END_MARKER)
        return document[:start] + block + document[end:]
    head_end = document.find("  </head>")
    if head_end < 0:
        raise ValueError("HTML document does not contain a supported head boundary")
    return document[:head_end] + block + "\n" + document[head_end:]
