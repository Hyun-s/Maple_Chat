"""Bounded client for the official MapleStory NEXON Open API."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any
from urllib.parse import urlparse

import httpx

_OFFICIAL_HOST = "open.api.nexon.com"
_BASE_URL = f"https://{_OFFICIAL_HOST}"


class NexonOpenAPIError(RuntimeError):
    """A safe, credential-free NEXON Open API failure."""


class NexonOpenAPIClient:
    """Read-only MapleStory API client with bounded retries and a fixed trust boundary."""

    def __init__(
        self,
        *,
        api_key: str,
        timeout_seconds: float = 10.0,
        retries: int = 2,
        base_url: str = _BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != _OFFICIAL_HOST
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("NEXON API endpoint must use the official HTTPS boundary")
        if not api_key:
            raise ValueError("NEXON API key must not be empty")
        if retries < 0 or retries > 3:
            raise ValueError("NEXON API retry count must be between zero and three")
        self.retries = retries
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
            headers={"x-nxopen-api-key": api_key, "accept": "application/json"},
        )

    def __repr__(self) -> str:
        return "NexonOpenAPIClient(api_key='<redacted>')"

    async def __aenter__(self) -> NexonOpenAPIClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def resolve_character(self, character_name: str) -> dict[str, Any]:
        return await self._get("maplestory/v1/id", {"character_name": character_name})

    async def get_character_basic(self, ocid: str, *, at: date | None = None) -> dict[str, Any]:
        return await self._get("maplestory/v1/character/basic", _dated_params(ocid, at))

    async def get_character_stat(self, ocid: str, *, at: date | None = None) -> dict[str, Any]:
        return await self._get("maplestory/v1/character/stat", _dated_params(ocid, at))

    async def get_character_equipment(self, ocid: str, *, at: date | None = None) -> dict[str, Any]:
        return await self._get("maplestory/v1/character/item-equipment", _dated_params(ocid, at))

    async def get_union(self, ocid: str, *, at: date | None = None) -> dict[str, Any]:
        return await self._get("maplestory/v1/user/union", _dated_params(ocid, at))

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = await self._client.get(path, params=params)
                if response.status_code == 429 or response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        "NEXON API returned a retryable status",
                        request=response.request,
                        response=response,
                    )
                if response.status_code >= 400:
                    raise NexonOpenAPIError(_safe_error_message(response.status_code))
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("NEXON API returned a non-object response")
                return payload
            except NexonOpenAPIError:
                raise
            except (
                httpx.TransportError,
                httpx.TimeoutException,
                httpx.HTTPStatusError,
                ValueError,
                TypeError,
            ) as exc:
                last_error = exc
                if attempt >= self.retries:
                    break
                await asyncio.sleep(min(0.25 * (2**attempt), 1.0))
        raise NexonOpenAPIError("NEXON Open API를 일시적으로 사용할 수 없습니다.") from last_error


def _dated_params(ocid: str, at: date | None) -> dict[str, str]:
    params = {"ocid": ocid}
    if at is not None:
        params["date"] = at.isoformat()
    return params


def _safe_error_message(status_code: int) -> str:
    if status_code == 400:
        return "캐릭터 또는 조회 조건을 확인할 수 없습니다."
    if status_code == 403:
        return "NEXON Open API 접근 권한을 확인해 주세요."
    return "NEXON Open API 요청에 실패했습니다."
