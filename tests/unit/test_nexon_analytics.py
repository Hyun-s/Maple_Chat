from __future__ import annotations

import pytest

from maple_chat.nexon.analytics import canonical_analytics_script, inject_analytics_script


def test_official_analytics_script_is_canonicalized_and_injected_idempotently() -> None:
    configured = (
        '<script type="text/javascript" '
        'src="https://openapi.nexon.com/js/analytics.js?app_id=abc_123" async></script>'
    )
    script = canonical_analytics_script(configured)
    document = "<!doctype html>\n<html>\n  <head>\n  </head>\n</html>\n"

    first = inject_analytics_script(document, script)
    second = inject_analytics_script(first, script)

    assert first == second
    assert first.count("nexon-open-api-analytics:start") == 1
    assert 'app_id=abc_123" async' in first


@pytest.mark.parametrize(
    "configured",
    [
        '<script src="http://openapi.nexon.com/js/analytics.js?app_id=abc" async></script>',
        '<script src="https://example.com/js/analytics.js?app_id=abc" async></script>',
        '<script src="https://openapi.nexon.com/other.js?app_id=abc" async></script>',
        '<script src="https://openapi.nexon.com/js/analytics.js?app_id=abc"></script>',
        '<script src="https://openapi.nexon.com/js/analytics.js?app_id=abc&x=1" async></script>',
        '<script src="https://openapi.nexon.com/js/analytics.js?app_id=%22bad" async></script>',
        '<script src="https://openapi.nexon.com/js/analytics.js?app_id=abc" async></script>bad',
    ],
)
def test_analytics_script_rejects_untrusted_boundaries(configured: str) -> None:
    with pytest.raises(ValueError, match="NEXON_ANALYTICS_SCRIPT"):
        canonical_analytics_script(configured)


def test_injection_rejects_missing_head_or_inconsistent_markers() -> None:
    script = '<script src="https://openapi.nexon.com/js/analytics.js?app_id=abc" async></script>'
    with pytest.raises(ValueError, match="head"):
        inject_analytics_script("<html></html>", script)
    with pytest.raises(ValueError, match="markers"):
        inject_analytics_script("    <!-- nexon-open-api-analytics:start -->", script)
