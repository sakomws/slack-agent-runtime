import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest

from slack_agent_runtime.inference.client import (
    InferenceRouterClient,
    InferenceRouterError,
    InferenceStreamComplete,
    InferenceStreamDelta,
)


class ChunkedStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class BlockingStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started.set()
        await asyncio.Event().wait()
        yield b"unreachable"

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_complete_calls_inference_router_with_bearer_token() -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert json.loads(request.content) == {
            "model": "default-model",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        }
        return httpx.Response(
            200,
            headers={"x-request-id": "req-123"},
            json={
                "model": "default-model",
                "choices": [{"message": {"content": "Hello"}}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            },
        )

    http_client = httpx.AsyncClient(
        base_url="https://api.example.com",
        transport=httpx.MockTransport(handle),
    )
    client = InferenceRouterClient(
        base_url="https://api.example.com",
        api_key="test-key",
        model="default-model",
        http_client=http_client,
    )

    result = await client.complete("hello")
    assert result.content == "Hello"
    assert result.model == "default-model"
    assert result.request_id == "req-123"
    assert result.duration_ms >= 0
    assert result.usage.prompt_tokens == 10
    assert result.usage.completion_tokens == 5
    assert result.usage.total_tokens == 15
    await http_client.aclose()


@pytest.mark.asyncio
async def test_complete_hides_provider_error_body() -> None:
    async def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="provider secret details")

    http_client = httpx.AsyncClient(
        base_url="https://api.example.com",
        transport=httpx.MockTransport(handle),
    )
    client = InferenceRouterClient(
        base_url="https://api.example.com",
        api_key="test-key",
        model="default-model",
        http_client=http_client,
    )

    with pytest.raises(InferenceRouterError, match="HTTP 503") as exc_info:
        await client.complete("hello")
    assert "provider secret details" not in str(exc_info.value)
    await http_client.aclose()


@pytest.mark.asyncio
async def test_complete_can_override_model() -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["model"] == "alternate-model"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Alternate answer"}}]},
        )

    http_client = httpx.AsyncClient(
        base_url="https://api.example.com",
        transport=httpx.MockTransport(handle),
    )
    client = InferenceRouterClient(
        base_url="https://api.example.com",
        api_key="test-key",
        model="default-model",
        http_client=http_client,
    )

    result = await client.complete("hello", model="alternate-model")
    assert result.content == "Alternate answer"
    assert result.model == "alternate-model"
    await http_client.aclose()


@pytest.mark.asyncio
async def test_complete_can_enable_server_side_web_search() -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["tools"] == [{"type": "web_search"}]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Grounded answer"}}]},
        )

    http_client = httpx.AsyncClient(
        base_url="https://api.example.com",
        transport=httpx.MockTransport(handle),
    )
    client = InferenceRouterClient(
        base_url="https://api.example.com",
        api_key="test-key",
        model="default-model",
        http_client=http_client,
    )

    result = await client.complete("latest news", web_search=True)
    assert result.content == "Grounded answer"
    await http_client.aclose()


@pytest.mark.asyncio
async def test_complete_sends_generation_parameters() -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["temperature"] == 0.7
        assert body["top_p"] == 0.9
        assert body["max_tokens"] == 2048
        assert body["reasoning_effort"] == "high"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Tuned answer"}}]},
        )

    http_client = httpx.AsyncClient(
        base_url="https://api.example.com",
        transport=httpx.MockTransport(handle),
    )
    client = InferenceRouterClient(
        base_url="https://api.example.com",
        api_key="test-key",
        model="default-model",
        http_client=http_client,
    )

    result = await client.complete(
        "hello",
        temperature=0.7,
        top_p=0.9,
        max_tokens=2048,
        reasoning_effort="high",
    )
    assert result.content == "Tuned answer"
    await http_client.aclose()


@pytest.mark.asyncio
async def test_stream_complete_parses_fragmented_sse_and_final_usage() -> None:
    raw = (
        b'data: {"model":"stream-model","choices":[{"delta":{"content":"Hel"}}]}\r\n\r\n'
        b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
        b'data: {"choices":[],"usage":{"prompt_tokens":4,'
        b'"completion_tokens":2,"total_tokens":6}}\n\n'
        b"data: [DONE]\n\n"
    )
    stream = ChunkedStream([raw[:7], raw[7:39], raw[39:101], raw[101:]])

    async def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}
        return httpx.Response(
            200,
            headers={"x-request-id": "req-stream"},
            stream=stream,
        )

    http_client = httpx.AsyncClient(
        base_url="https://api.example.com",
        transport=httpx.MockTransport(handle),
    )
    client = InferenceRouterClient(
        base_url="https://api.example.com",
        api_key="test-key",
        model="default-model",
        http_client=http_client,
    )

    events = [event async for event in client.stream_complete("hello")]

    assert [event.content for event in events if isinstance(event, InferenceStreamDelta)] == [
        "Hel",
        "lo",
    ]
    complete = events[-1]
    assert isinstance(complete, InferenceStreamComplete)
    assert complete.result.content == "Hello"
    assert complete.result.model == "stream-model"
    assert complete.result.request_id == "req-stream"
    assert complete.result.usage.total_tokens == 6
    assert stream.closed
    await http_client.aclose()


@pytest.mark.asyncio
async def test_stream_complete_rejects_malformed_payload_safely() -> None:
    stream = ChunkedStream([b"data: provider-secret-{bad}\n\ndata: [DONE]\n\n"])
    http_client = httpx.AsyncClient(
        base_url="https://api.example.com",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
    )
    client = InferenceRouterClient(
        base_url="https://api.example.com",
        api_key="test-key",
        model="default-model",
        http_client=http_client,
    )

    with pytest.raises(InferenceRouterError, match="invalid stream") as exc_info:
        _ = [event async for event in client.stream_complete("hello")]
    assert "provider-secret" not in str(exc_info.value)
    assert stream.closed
    await http_client.aclose()


@pytest.mark.asyncio
async def test_stream_complete_ignores_sse_comments_and_non_data_fields() -> None:
    stream = ChunkedStream(
        [
            b": keepalive\n"
            b"event: message\n"
            b"id: 1\n"
            b'data:{"request_id":"payload-req","choices":[{"delta":{"content":"ok"}}]}\n\n'
            b'data: {"choices":[],"usage":{"total_tokens":2}}\n\n'
            b"data: [DONE]\n"
        ]
    )
    http_client = httpx.AsyncClient(
        base_url="https://api.example.com",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
    )
    client = InferenceRouterClient(
        base_url="https://api.example.com",
        api_key="test-key",
        model="default-model",
        http_client=http_client,
    )

    events = [event async for event in client.stream_complete("hello")]

    complete = events[-1]
    assert isinstance(complete, InferenceStreamComplete)
    assert complete.result.content == "ok"
    assert complete.result.request_id == "payload-req"
    assert complete.result.usage.total_tokens == 2
    await http_client.aclose()


@pytest.mark.asyncio
async def test_stream_complete_rejects_eof_without_done_marker() -> None:
    stream = ChunkedStream([b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'])
    http_client = httpx.AsyncClient(
        base_url="https://api.example.com",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
    )
    client = InferenceRouterClient(
        base_url="https://api.example.com",
        api_key="test-key",
        model="default-model",
        http_client=http_client,
    )

    with pytest.raises(InferenceRouterError, match="ended unexpectedly"):
        _ = [event async for event in client.stream_complete("hello")]

    assert stream.closed
    await http_client.aclose()


@pytest.mark.asyncio
async def test_stream_complete_rejects_done_without_content() -> None:
    stream = ChunkedStream([b'data: {"choices":[],"usage":{"total_tokens":0}}\n\ndata: [DONE]\n\n'])
    http_client = httpx.AsyncClient(
        base_url="https://api.example.com",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
    )
    client = InferenceRouterClient(
        base_url="https://api.example.com",
        api_key="test-key",
        model="default-model",
        http_client=http_client,
    )

    with pytest.raises(InferenceRouterError, match="empty response"):
        _ = [event async for event in client.stream_complete("hello")]

    await http_client.aclose()


@pytest.mark.asyncio
async def test_stream_complete_rejects_non_list_choices() -> None:
    stream = ChunkedStream([b'data: {"choices":{}}\n\ndata: [DONE]\n\n'])
    http_client = httpx.AsyncClient(
        base_url="https://api.example.com",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
    )
    client = InferenceRouterClient(
        base_url="https://api.example.com",
        api_key="test-key",
        model="default-model",
        http_client=http_client,
    )

    with pytest.raises(InferenceRouterError, match="invalid stream"):
        _ = [event async for event in client.stream_complete("hello")]

    await http_client.aclose()


@pytest.mark.asyncio
async def test_stream_complete_cancellation_closes_response() -> None:
    stream = BlockingStream()
    http_client = httpx.AsyncClient(
        base_url="https://api.example.com",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
    )
    client = InferenceRouterClient(
        base_url="https://api.example.com",
        api_key="test-key",
        model="default-model",
        http_client=http_client,
    )

    async def consume() -> None:
        async for _ in client.stream_complete("hello"):
            pass

    task = asyncio.create_task(consume())
    await stream.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed
    await http_client.aclose()


@pytest.mark.asyncio
async def test_list_models_returns_available_models() -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "default-model", "object": "model"},
                    {"id": "alternate-model", "object": "model"},
                ]
            },
        )

    http_client = httpx.AsyncClient(
        base_url="https://api.example.com",
        transport=httpx.MockTransport(handle),
    )
    client = InferenceRouterClient(
        base_url="https://api.example.com",
        api_key="test-key",
        model="default-model",
        http_client=http_client,
    )

    assert await client.list_models() == ("alternate-model", "default-model")
    await http_client.aclose()
