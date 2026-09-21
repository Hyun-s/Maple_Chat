from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from maple_chat.nexon.client import NexonOpenAPIClient, NexonOpenAPIError


@pytest.mark.asyncio
async def test_official_client_sends_key_only_in_header_and_encodes_query() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["x-nxopen-api-key"] == "secret-canary"
        assert "secret-canary" not in str(request.url)
        return httpx.Response(200, json={"ocid": "character-id"})

    async with NexonOpenAPIClient(
        api_key="secret-canary",  # pragma: allowlist secret
        transport=httpx.MockTransport(handler),
    ) as client:
        payload = await client.resolve_character("테스트캐릭터")
        assert "secret-canary" not in repr(client)

    assert payload == {"ocid": "character-id"}
    assert seen[0].url.path == "/maplestory/v1/id"
    assert seen[0].url.params["character_name"] == "테스트캐릭터"


@pytest.mark.asyncio
async def test_retryable_status_is_bounded_and_recovers() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, request=request)
        return httpx.Response(200, json={"date": "2026-08-30T00:00+09:00"})

    async with NexonOpenAPIClient(
        api_key="secret-canary",  # pragma: allowlist secret
        retries=1,
        transport=httpx.MockTransport(handler),
    ) as client:
        payload = await client.get_character_stat("ocid")

    assert payload["date"].startswith("2026-08-30")
    assert calls == 2


@pytest.mark.asyncio
async def test_bad_request_fails_without_exposing_response_or_key() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            content=json.dumps({"error": {"message": "secret remote detail"}}),
        )

    async with NexonOpenAPIClient(
        api_key="secret-canary",  # pragma: allowlist secret
        retries=2,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(NexonOpenAPIError) as raised:
            await client.resolve_character("없는캐릭터")

    rendered = str(raised.value)
    assert "secret-canary" not in rendered
    assert "secret remote detail" not in rendered


def test_non_official_or_credentialed_endpoint_is_rejected() -> None:
    for endpoint in (
        "http://open.api.nexon.com",
        "https://example.com",
        "https://user:pass@open.api.nexon.com",  # pragma: allowlist secret
    ):
        with pytest.raises(ValueError, match="official HTTPS"):
            NexonOpenAPIClient(api_key="key", base_url=endpoint)  # pragma: allowlist secret

    with pytest.raises(ValueError, match="must not be empty"):
        NexonOpenAPIClient(api_key="")
    with pytest.raises(ValueError, match="between zero and three"):
        NexonOpenAPIClient(api_key="key", retries=4)  # pragma: allowlist secret


@pytest.mark.asyncio
async def test_all_profile_routes_and_historical_date_parameter() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.url.params["ocid"] == "ocid"
        assert request.url.params["date"] == "2026-08-30"
        return httpx.Response(200, json={"ok": True})

    async with NexonOpenAPIClient(
        api_key="secret-canary",  # pragma: allowlist secret
        transport=httpx.MockTransport(handler),
    ) as client:
        at = date(2026, 8, 30)
        await client.get_character_basic("ocid", at=at)
        await client.get_character_equipment("ocid", at=at)
        await client.get_union("ocid", at=at)

    assert paths == [
        "/maplestory/v1/character/basic",
        "/maplestory/v1/character/item-equipment",
        "/maplestory/v1/user/union",
    ]


@pytest.mark.asyncio
async def test_non_object_and_exhausted_server_failures_are_safe() -> None:
    def non_object(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["unexpected"])

    async with NexonOpenAPIClient(
        api_key="secret-canary",  # pragma: allowlist secret
        retries=0,
        transport=httpx.MockTransport(non_object),
    ) as client:
        with pytest.raises(NexonOpenAPIError, match="일시적으로"):
            await client.resolve_character("테스트캐릭터")

    def forbidden(_: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    async with NexonOpenAPIClient(
        api_key="secret-canary",  # pragma: allowlist secret
        transport=httpx.MockTransport(forbidden),
    ) as client:
        with pytest.raises(NexonOpenAPIError, match="접근 권한"):
            await client.resolve_character("테스트캐릭터")
