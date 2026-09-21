from __future__ import annotations

import json

import httpx
import pytest

from maple_chat.llm.client import ChatMessage, LLMUnavailable, LocalLLMClient


@pytest.mark.asyncio
async def test_local_readiness_and_generation_contract() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "local-coder"}]})
        calls += 1
        body = json.loads(request.content)
        assert body["model"] == "local-coder"
        assert body["chat_template_kwargs"] == {"enable_thinking": False}
        assert body["max_tokens"] == 1024
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}
        stream = "\n\n".join(
            (
                'data: {"choices":[{"delta":{"content":" 근거"}}]}',
                'data: {"choices":[{"delta":{"content":" 기반 답변 "}}]}',
                (
                    'data: {"choices":[],"usage":{"prompt_tokens":12,'
                    '"completion_tokens":5,"total_tokens":17}}'
                ),
                "data: [DONE]",
            )
        )
        return httpx.Response(
            200,
            content=stream,
            headers={"content-type": "text/event-stream"},
        )

    async with LocalLLMClient(
        base_url="http://127.0.0.1:8100/v1",
        model="local-coder",
        transport=httpx.MockTransport(handler),
    ) as client:
        assert await client.ready() is True
        result = await client.generate((ChatMessage("user", "질문"),))
        assert result.text == "근거 기반 답변"
        assert result.metrics.prompt_tokens == 12
        assert result.metrics.completion_tokens == 5
        assert result.metrics.total_tokens == 17
        assert result.metrics.input_tokens_per_second is not None
        assert result.metrics.output_tokens_per_second is not None
        assert result.metrics.request_seconds > 0
    assert calls == 1


@pytest.mark.asyncio
async def test_generation_fails_closed_when_stream_usage_is_missing() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=('data: {"choices":[{"delta":{"content":"답변"}}]}\n\ndata: [DONE]\n\n'),
            headers={"content-type": "text/event-stream"},
        )

    async with LocalLLMClient(
        base_url="http://localhost:8100/v1",
        model="local-coder",
        retries=0,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(LLMUnavailable, match="unavailable"):
            await client.generate((ChatMessage("user", "질문"),))


@pytest.mark.asyncio
async def test_local_server_errors_retry_then_fall_back() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, request=request)

    async with LocalLLMClient(
        base_url="http://localhost:8100/v1",
        model="local-coder",
        retries=1,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(LLMUnavailable, match="unavailable"):
            await client.generate((ChatMessage("user", "질문"),))
    assert calls == 2


def test_cloud_or_credentialed_llm_endpoints_are_rejected() -> None:
    with pytest.raises(ValueError, match="local boundary"):
        LocalLLMClient(base_url="https://api.openai.com/v1", model="local-coder")
    with pytest.raises(ValueError, match="local boundary"):
        LocalLLMClient(
            base_url="http://user:pass@localhost:8100/v1",  # pragma: allowlist secret
            model="local-coder",
        )
