import json
from collections.abc import AsyncIterator

import httpx
import pytest

from slack_agent_runtime.inference.client import (
    InferenceRouterClient,
    InferenceRouterError,
    InferenceStreamComplete,
)


class ChunkedStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        pass


def _client(handler) -> tuple[InferenceRouterClient, httpx.AsyncClient]:
    http_client = httpx.AsyncClient(
        base_url="https://api.example.test",
        transport=httpx.MockTransport(handler),
    )
    return (
        InferenceRouterClient(
            base_url="https://api.example.test",
            api_key="key",
            model="default",
            http_client=http_client,
        ),
        http_client,
    )


@pytest.mark.asyncio
async def test_nonstream_parses_tool_calls_and_sends_messages_and_tools() -> None:
    schema = {
        "type": "function",
        "function": {
            "name": "mcp__server__lookup",
            "description": "Lookup",
            "parameters": {"type": "object"},
        },
    }

    async def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body == {
            "model": "selected",
            "messages": [{"role": "user", "content": "question"}],
            "stream": False,
            "tools": [{"type": "web_search"}, schema],
        }
        return httpx.Response(
            200,
            json={
                "model": "selected",
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "mcp__server__lookup",
                                        "arguments": '{"query":"value","limit":2}',
                                    },
                                }
                            ],
                        }
                    }
                ],
            },
        )

    client, http_client = _client(handle)
    result = await client.complete(
        messages=[{"role": "user", "content": "question"}],
        model="selected",
        web_search=True,
        tools=[schema],
    )

    assert result.content == ""
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].id == "call-1"
    assert result.tool_calls[0].name == "mcp__server__lookup"
    assert result.tool_calls[0].arguments == {"query": "value", "limit": 2}
    await http_client.aclose()


@pytest.mark.parametrize(
    "tool_calls",
    [
        {},
        [{"id": "", "function": {"name": "tool", "arguments": "{}"}}],
        [{"id": "call", "function": {"name": "", "arguments": "{}"}}],
        [{"id": "call", "function": {"name": "tool", "arguments": "[]"}}],
        [{"id": "call", "function": {"name": "tool", "arguments": "not-json"}}],
    ],
)
@pytest.mark.asyncio
async def test_nonstream_rejects_malformed_tool_calls(tool_calls) -> None:
    async def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": None, "tool_calls": tool_calls}}]},
        )

    client, http_client = _client(handle)
    with pytest.raises(InferenceRouterError, match="invalid response"):
        await client.complete("question")
    await http_client.aclose()


@pytest.mark.asyncio
async def test_stream_assembles_interleaved_tool_call_fragments_and_usage() -> None:
    raw = (
        b'data: {"model":"stream-model","choices":[{"delta":{"tool_calls":['
        b'{"index":1,"id":"call-b","function":{"name":"mcp__b","arguments":"{\\"b\\":"}},'
        b'{"index":0,"id":"call-a","function":{"name":"mcp__a","arguments":"{\\"a\\":"}}]}}]}\n\n'
        b'data: {"choices":[{"delta":{"tool_calls":['
        b'{"index":0,"function":{"arguments":"1}"}},'
        b'{"index":1,"function":{"arguments":"2}"}}]}}]}\n\n'
        b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":4,'
        b'"total_tokens":7}}\n\n'
        b"data: [DONE]\n\n"
    )
    stream = ChunkedStream([raw[:31], raw[31:113], raw[113:]])

    async def handle(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["tools"][0]["function"]["name"] == "mcp__a"
        return httpx.Response(200, stream=stream)

    client, http_client = _client(handle)
    events = [
        event
        async for event in client.stream_complete(
            "question",
            tools=[
                {
                    "type": "function",
                    "function": {"name": "mcp__a", "parameters": {"type": "object"}},
                }
            ],
        )
    ]

    assert len(events) == 1
    complete = events[0]
    assert isinstance(complete, InferenceStreamComplete)
    assert [(call.id, call.name, call.arguments) for call in complete.result.tool_calls] == [
        ("call-a", "mcp__a", {"a": 1}),
        ("call-b", "mcp__b", {"b": 2}),
    ]
    assert complete.result.usage.total_tokens == 7
    await http_client.aclose()


@pytest.mark.asyncio
async def test_stream_rejects_invalid_tool_argument_fragments_safely() -> None:
    stream = ChunkedStream(
        [
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call",'
            b'"function":{"name":"tool","arguments":"provider-secret"}}]}}]}\n\n'
            b"data: [DONE]\n\n"
        ]
    )
    client, http_client = _client(lambda _: httpx.Response(200, stream=stream))

    with pytest.raises(InferenceRouterError, match="invalid stream") as exc_info:
        _ = [event async for event in client.stream_complete("question")]
    assert "provider-secret" not in str(exc_info.value)
    await http_client.aclose()


@pytest.mark.asyncio
async def test_no_tools_preserves_legacy_request_shape() -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {
            "model": "default",
            "messages": [{"role": "user", "content": "question"}],
            "stream": False,
        }
        return httpx.Response(200, json={"choices": [{"message": {"content": "answer"}}]})

    client, http_client = _client(handle)
    assert (await client.complete("question", tools=[])).content == "answer"
    await http_client.aclose()
