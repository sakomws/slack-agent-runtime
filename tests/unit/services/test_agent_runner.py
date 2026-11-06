from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from slack_agent_runtime.inference.client import (
    InferenceResult,
    InferenceStreamComplete,
    InferenceStreamDelta,
    InferenceToolCall,
    InferenceUsage,
)
from slack_agent_runtime.mcp.approval import ApprovalBroker
from slack_agent_runtime.mcp.manager import McpTool, McpToolResult
from slack_agent_runtime.services.agent_runner import AgentLoopError, AgentRunner


def _result(
    content: str,
    *,
    calls: tuple[InferenceToolCall, ...] = (),
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
) -> InferenceResult:
    return InferenceResult(
        content=content,
        model="model",
        request_id="request",
        duration_ms=10,
        usage=InferenceUsage(prompt_tokens, completion_tokens, total_tokens),
        tool_calls=calls,
    )


def _tool(*, auto_approved: bool) -> McpTool:
    return McpTool(
        "mcp__server__lookup",
        "server",
        "lookup",
        "Lookup",
        {"type": "object"},
        read_only=True,
        auto_approved=auto_approved,
    )


def _runner(client, tool: McpTool, *, max_steps: int = 3):
    manager = SimpleNamespace(
        tools=(tool,),
        call_tool=AsyncMock(return_value=McpToolResult('{"answer":42}', False)),
    )
    broker = ApprovalBroker(timeout_seconds=1)
    return AgentRunner(client, manager, broker, max_steps=max_steps), manager, broker


@pytest.mark.asyncio
async def test_auto_approved_tool_runs_without_approval_and_aggregates_usage() -> None:
    call = InferenceToolCall("call-1", "mcp__server__lookup", {"q": "answer"})
    tool = _tool(auto_approved=True)
    client = AsyncMock()
    client.streaming = False
    client.complete.side_effect = [
        _result("", calls=(call,), prompt_tokens=3, completion_tokens=2, total_tokens=5),
        _result("Final answer", prompt_tokens=8, completion_tokens=4, total_tokens=12),
    ]
    runner, manager, _ = _runner(client, tool)
    progress = AsyncMock()
    request_approval = AsyncMock()

    result = await runner.run(
        messages=[{"role": "user", "content": "question"}],
        model="model",
        web_search=False,
        streaming=True,
        options={"temperature": 0.2},
        thread_key=("C1", "T1"),
        requester_id="U1",
        progress=progress,
        request_approval=request_approval,
        required_tool_names=frozenset({tool.function_name}),
    )

    assert result.content == "Final answer"
    assert result.duration_ms == 20
    assert result.usage == InferenceUsage(11, 6, 17)
    request_approval.assert_not_awaited()
    manager.call_tool.assert_awaited_once_with(
        "mcp__server__lookup",
        {"q": "answer"},
    )
    second_messages = client.complete.await_args_list[1].kwargs["messages"]
    assert second_messages[-2]["tool_calls"][0]["function"]["arguments"] == '{"q":"answer"}'
    assert second_messages[-1] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": '{"answer":42}',
    }
    assert client.complete.await_args_list[0].kwargs["tools"] == (tool.openai_schema(),)


@pytest.mark.asyncio
async def test_approval_path_runs_tool_after_matching_approval() -> None:
    call = InferenceToolCall("call-1", "mcp__server__lookup", {})
    client = AsyncMock()
    client.streaming = False
    client.complete.side_effect = [_result("", calls=(call,)), _result("done")]
    runner, manager, broker = _runner(client, _tool(auto_approved=False))
    progress = AsyncMock()

    async def approve(pending) -> None:
        assert pending.requester_id == "U1"
        assert broker.resolve(
            pending.id,
            approved=True,
            thread_key=("C1", "T1"),
        )

    await runner.run(
        messages=[{"role": "user", "content": "question"}],
        model="model",
        web_search=False,
        streaming=False,
        options={},
        thread_key=("C1", "T1"),
        requester_id="U1",
        progress=progress,
        request_approval=approve,
    )

    assert progress.await_args_list[0].args[0].startswith("Waiting for approval")
    assert progress.await_args_list[1].args[0].startswith("Running MCP tool")
    manager.call_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_denied_tool_returns_safe_result_to_model_without_execution() -> None:
    call = InferenceToolCall("call-1", "mcp__server__lookup", {"secret": "value"})
    client = AsyncMock()
    client.streaming = False
    client.complete.side_effect = [_result("", calls=(call,)), _result("denied answer")]
    runner, manager, broker = _runner(client, _tool(auto_approved=False))

    async def deny(pending) -> None:
        assert broker.resolve(
            pending.id,
            approved=False,
            thread_key=("C1", "T1"),
        )

    result = await runner.run(
        messages=[{"role": "user", "content": "question"}],
        model="model",
        web_search=False,
        streaming=False,
        options={},
        thread_key=("C1", "T1"),
        requester_id=None,
        progress=AsyncMock(),
        request_approval=deny,
    )

    assert result.content == "denied answer"
    manager.call_tool.assert_not_awaited()
    assert client.complete.await_args_list[1].kwargs["messages"][-1]["content"] == (
        "Tool execution was denied or the approval expired."
    )


@pytest.mark.asyncio
async def test_unknown_tool_call_is_reported_to_model() -> None:
    call = InferenceToolCall("call-1", "missing", {})
    client = AsyncMock()
    client.streaming = False
    client.complete.side_effect = [_result("", calls=(call,)), _result("recovered")]
    runner, manager, _ = _runner(client, _tool(auto_approved=True))

    result = await runner.run(
        messages=[],
        model="model",
        web_search=False,
        streaming=False,
        options={},
        thread_key=("C1", "T1"),
        requester_id=None,
        progress=AsyncMock(),
        request_approval=AsyncMock(),
    )

    assert result.content == "recovered"
    manager.call_tool.assert_not_awaited()
    assert "unavailable" in client.complete.await_args_list[1].kwargs["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_tool_failure_is_safely_returned_to_model() -> None:
    call = InferenceToolCall("call-1", "mcp__server__lookup", {})
    client = AsyncMock()
    client.streaming = False
    client.complete.side_effect = [_result("", calls=(call,)), _result("fallback")]
    runner, manager, _ = _runner(client, _tool(auto_approved=True))
    manager.call_tool.side_effect = RuntimeError("provider secret")

    result = await runner.run(
        messages=[],
        model="model",
        web_search=False,
        streaming=False,
        options={},
        thread_key=("C1", "T1"),
        requester_id=None,
        progress=AsyncMock(),
        request_approval=AsyncMock(),
    )

    assert result.content == "fallback"
    tool_message = client.complete.await_args_list[1].kwargs["messages"][-1]["content"]
    assert tool_message == "Tool execution failed. The tool server may be unavailable."
    assert "provider secret" not in tool_message


@pytest.mark.asyncio
async def test_max_step_limit_stops_repeated_tool_calls() -> None:
    call = InferenceToolCall("call-1", "mcp__server__lookup", {})
    client = AsyncMock()
    client.streaming = False
    client.complete.return_value = _result("", calls=(call,))
    runner, manager, _ = _runner(client, _tool(auto_approved=True), max_steps=2)

    with pytest.raises(AgentLoopError, match="step limit"):
        await runner.run(
            messages=[],
            model="model",
            web_search=False,
            streaming=False,
            options={},
            thread_key=("C1", "T1"),
            requester_id=None,
            progress=AsyncMock(),
            request_approval=AsyncMock(),
        )

    assert client.complete.await_count == 2
    assert manager.call_tool.await_count == 2


@pytest.mark.asyncio
async def test_streaming_round_reports_deltas_and_retains_tool_calls() -> None:
    call = InferenceToolCall("call-1", "mcp__server__lookup", {})

    class StreamingClient:
        streaming = True

        def __init__(self) -> None:
            self.round = 0

        async def stream_complete(self, **_):
            self.round += 1
            if self.round == 1:
                yield InferenceStreamDelta("Thinking")
                yield InferenceStreamComplete(_result("provider copy", calls=(call,)))
            else:
                yield InferenceStreamDelta("Final")
                yield InferenceStreamComplete(_result("provider final"))

    client = StreamingClient()
    runner, manager, _ = _runner(client, _tool(auto_approved=True))
    progress = AsyncMock()

    result = await runner.run(
        messages=[],
        model="model",
        web_search=False,
        streaming=True,
        options={},
        thread_key=("C1", "T1"),
        requester_id=None,
        progress=progress,
        request_approval=AsyncMock(),
    )

    assert result.content == "Final"
    assert progress.await_args_list[0].args == ("Thinking",)
    assert progress.await_args_list[-1].args == ("Final",)
    manager.call_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_required_tool_must_complete_successfully() -> None:
    tool = _tool(auto_approved=True)
    client = AsyncMock()
    client.streaming = False
    client.complete.return_value = _result("Ungrounded answer")
    runner, manager, _ = _runner(client, tool)

    with pytest.raises(AgentLoopError, match="did not retrieve"):
        await runner.run(
            messages=[],
            model="model",
            web_search=False,
            streaming=False,
            options={},
            thread_key=("C1", "T1"),
            requester_id=None,
            progress=AsyncMock(),
            request_approval=AsyncMock(),
            allowed_tool_names=frozenset({tool.function_name}),
            required_tool_names=frozenset({tool.function_name}),
        )

    manager.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_allowed_tool_filter_excludes_other_tools() -> None:
    allowed = _tool(auto_approved=True)
    excluded = McpTool(
        "mcp__other__write",
        "other",
        "write",
        "Write",
        {},
        read_only=False,
        auto_approved=False,
    )
    client = AsyncMock()
    client.streaming = False
    client.complete.return_value = _result("Done")
    manager = SimpleNamespace(tools=(allowed, excluded), call_tool=AsyncMock())
    runner = AgentRunner(client, manager, ApprovalBroker(timeout_seconds=1), max_steps=2)

    await runner.run(
        messages=[],
        model="model",
        web_search=False,
        streaming=False,
        options={},
        thread_key=("C1", "T1"),
        requester_id=None,
        progress=AsyncMock(),
        request_approval=AsyncMock(),
        allowed_tool_names=frozenset({allowed.function_name}),
    )

    schemas = client.complete.await_args.kwargs["tools"]
    assert [schema["function"]["name"] for schema in schemas] == [allowed.function_name]
