"""Bounded local vLLM readiness and chat-completion client."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "host.docker.internal"})


class LLMUnavailable(RuntimeError):
    """Local generation is unavailable after bounded retries."""


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class GenerationMetrics:
    """Measured usage and effective throughput for one or more local model calls."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    input_seconds: float
    output_seconds: float
    request_seconds: float
    model_calls: int = 1

    @property
    def input_tokens_per_second(self) -> float | None:
        if self.prompt_tokens <= 0 or self.input_seconds <= 0:
            return None
        return self.prompt_tokens / self.input_seconds

    @property
    def output_tokens_per_second(self) -> float | None:
        if self.completion_tokens <= 0 or self.output_seconds <= 0:
            return None
        return self.completion_tokens / self.output_seconds

    @classmethod
    def combine(cls, metrics: tuple[GenerationMetrics, ...]) -> GenerationMetrics | None:
        if not metrics:
            return None
        return cls(
            prompt_tokens=sum(item.prompt_tokens for item in metrics),
            completion_tokens=sum(item.completion_tokens for item in metrics),
            total_tokens=sum(item.total_tokens for item in metrics),
            input_seconds=sum(item.input_seconds for item in metrics),
            output_seconds=sum(item.output_seconds for item in metrics),
            request_seconds=sum(item.request_seconds for item in metrics),
            model_calls=sum(item.model_calls for item in metrics),
        )


@dataclass(frozen=True, slots=True)
class GenerationResult:
    text: str
    metrics: GenerationMetrics


class LocalLLMClient:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_seconds: float = 60.0,
        retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in _LOCAL_HOSTS
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("LLM endpoint must remain inside the approved local boundary")
        if retries < 0 or retries > 3:
            raise ValueError("LLM retry count must be between zero and three")
        self.model = model
        self.retries = retries
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )

    async def __aenter__(self) -> LocalLLMClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def ready(self) -> bool:
        try:
            response = await self._client.get("models")
            response.raise_for_status()
            payload = response.json()
            return any(item.get("id") == self.model for item in payload.get("data", []))
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            return False

    async def generate(
        self,
        messages: tuple[ChatMessage, ...],
        *,
        max_tokens: int = 1024,
    ) -> GenerationResult:
        if max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": message.role, "content": message.content} for message in messages
            ],
            "max_tokens": max_tokens,
            "temperature": 0.1,
            "chat_template_kwargs": {"enable_thinking": False},
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                started_at = time.perf_counter()
                first_content_at: float | None = None
                content_parts: list[str] = []
                usage: dict[str, Any] | None = None
                async with self._client.stream("POST", "chat/completions", json=body) as response:
                    if response.status_code >= 500:
                        raise httpx.HTTPStatusError(
                            "local generation returned a server error",
                            request=response.request,
                            response=response,
                        )
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line or not line.startswith("data:"):
                            continue
                        data = line.removeprefix("data:").strip()
                        if data == "[DONE]":
                            break
                        payload = json.loads(data)
                        current_usage = payload.get("usage")
                        if isinstance(current_usage, dict):
                            usage = current_usage
                        choices = payload.get("choices")
                        if not isinstance(choices, list) or not choices:
                            continue
                        delta = choices[0].get("delta", {})
                        content = delta.get("content")
                        if isinstance(content, str) and content:
                            if first_content_at is None:
                                first_content_at = time.perf_counter()
                            content_parts.append(content)
                finished_at = time.perf_counter()
                text = "".join(content_parts).strip()
                if not text:
                    raise ValueError("empty local generation response")
                if usage is None:
                    raise ValueError("local generation did not return token usage")
                prompt_tokens = _usage_token_count(usage, "prompt_tokens")
                completion_tokens = _usage_token_count(usage, "completion_tokens")
                total_tokens = _usage_token_count(usage, "total_tokens")
                if total_tokens < prompt_tokens + completion_tokens:
                    raise ValueError("local generation returned inconsistent token usage")
                if first_content_at is None:
                    raise ValueError("local generation did not stream assistant content")
                return GenerationResult(
                    text=text,
                    metrics=GenerationMetrics(
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_tokens=total_tokens,
                        input_seconds=max(first_content_at - started_at, 1e-9),
                        output_seconds=max(finished_at - first_content_at, 1e-9),
                        request_seconds=max(finished_at - started_at, 1e-9),
                    ),
                )
            except (
                httpx.TransportError,
                httpx.TimeoutException,
                httpx.HTTPStatusError,
                ValueError,
                KeyError,
                TypeError,
            ) as exc:
                last_error = exc
                retryable = isinstance(exc, (httpx.TransportError, httpx.TimeoutException)) or (
                    isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500
                )
                if not retryable or attempt >= self.retries:
                    break
                await asyncio.sleep(min(0.25 * (2**attempt), 1.0))
        raise LLMUnavailable("local generation unavailable") from last_error


def _usage_token_count(usage: dict[str, Any], key: str) -> int:
    value = usage.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"local generation returned invalid {key}")
    return value
