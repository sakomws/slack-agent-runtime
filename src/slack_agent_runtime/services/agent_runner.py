"""Bounded model/tool loop for direct MCP execution."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any, TypeAlias

from slack_agent_runtime.inference.client import (
    InferenceClient,
    InferenceMessage,
    InferenceOptions,
    InferenceResult,
    InferenceRouterError,
    InferenceStreamComplete,
    InferenceStreamDelta,
    InferenceToolCall,
    InferenceUsage,
)
from slack_agent_runtime.mcp.approval import ApprovalBroker, PendingApproval

if TYPE_CHECKING:
    from slack_agent_runtime.domain.models import ThreadKey
    from slack_agent_runtime.mcp.manager import McpManager, McpTool

ProgressCallback: TypeAlias = Callable[[str], Awaitable[None]]
ApprovalCallback: TypeAlias = Callable[[PendingApproval], Awaitable[None]]


class AgentLoopError(RuntimeError):
    """A safe failure raised when a bounded agent run cannot finish."""


class AgentRunner:
    def __init__(
        self,
        inference_client: InferenceClient,
        mcp_manager: McpManager,
        approval_broker: ApprovalBroker,
        *,
        max_steps: int,
    ) -> None:
        self._inference_client = inference_client
        self._mcp_manager = mcp_manager
        self._approval_broker = approval_broker
        self._max_steps = max_steps

    async def run(
        self,
        *,
        messages: Sequence[InferenceMessage],
        model: str,
        web_search: bool,
        streaming: bool,
        options: InferenceOptions,
        thread_key: ThreadKey,
        requester_id: str | None,
        progress: ProgressCallback,
        request_approval: ApprovalCallback,
        allowed_tool_names: frozenset[str] | None = None,
        required_tool_names: frozenset[str] = frozenset(),
    ) -> InferenceResult:
        working_messages = [dict(message) for message in messages]
        tools = tuple(
            tool
            for tool in self._mcp_manager.tools
            if allowed_tool_names is None or tool.function_name in allowed_tool_names
        )
        schemas = tuple(tool.openai_schema() for tool in tools)
        tool_index = {tool.function_name: tool for tool in tools}
        results: list[InferenceResult] = []
        successful_required_tool = False

        for _ in range(self._max_steps):
            result = await self._complete_round(
                messages=working_messages,
                model=model,
                web_search=web_search,
                tools=schemas,
                streaming=streaming,
                options=options,
                progress=progress,
            )
            results.append(result)
            if not result.tool_calls:
                if required_tool_names and not successful_required_tool:
                    raise AgentLoopError("The agent did not retrieve the requested specification")
                return _aggregate_results(results)
            working_messages.append(_assistant_tool_message(result))
            for call in result.tool_calls:
                tool_result, succeeded = await self._execute_tool(
                    call,
                    tool_index=tool_index,
                    thread_key=thread_key,
                    requester_id=requester_id,
                    request_approval=request_approval,
                    progress=progress,
                )
                successful_required_tool = successful_required_tool or (
                    succeeded and call.name in required_tool_names
                )
                working_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": tool_result,
                    }
                )
        raise AgentLoopError("The agent reached its MCP step limit before producing an answer")

    async def _complete_round(
        self,
        *,
        messages: Sequence[InferenceMessage],
        model: str,
        web_search: bool,
        tools: Sequence[dict[str, Any]],
        streaming: bool,
        options: InferenceOptions,
        progress: ProgressCallback,
    ) -> InferenceResult:
        kwargs: dict[str, Any] = {
            "messages": messages,
            "model": model,
            "web_search": web_search,
            **options,
        }
        if tools:
            kwargs["tools"] = tools
        if not streaming or self._inference_client.streaming is not True:
            return await self._inference_client.complete(**kwargs)

        parts: list[str] = []
        final_result: InferenceResult | None = None
        async for event in self._inference_client.stream_complete(**kwargs):
            if isinstance(event, InferenceStreamDelta):
                if final_result is not None:
                    raise InferenceRouterError("Inference Router returned an invalid stream")
                parts.append(event.content)
                await progress("".join(parts))
            elif isinstance(event, InferenceStreamComplete):
                if final_result is not None:
                    raise InferenceRouterError("Inference Router returned an invalid stream")
                final_result = event.result
            else:
                raise InferenceRouterError("Inference Router returned an invalid stream")
        if final_result is None:
            raise InferenceRouterError("Inference Router stream ended unexpectedly")
        streamed_content = "".join(parts)
        return replace(
            final_result,
            content=streamed_content or final_result.content,
        )

    async def _execute_tool(
        self,
        call: InferenceToolCall,
        *,
        tool_index: dict[str, McpTool],
        thread_key: ThreadKey,
        requester_id: str | None,
        request_approval: ApprovalCallback,
        progress: ProgressCallback,
    ) -> tuple[str, bool]:
        tool = tool_index.get(call.name)
        if tool is None:
            return "Tool execution failed: the requested tool is unavailable.", False
        if not tool.auto_approved:
            await progress(f"Waiting for approval: `{tool.server_name}/{tool.tool_name}`…")
            pending = self._approval_broker.create(
                tool,
                requester_id=requester_id,
                thread_key=thread_key,
            )
            await request_approval(pending)
            try:
                approved = await self._approval_broker.wait(pending.id)
            except BaseException:
                self._approval_broker.resolve(
                    pending.id,
                    approved=False,
                    thread_key=thread_key,
                )
                raise
            if not approved:
                return "Tool execution was denied or the approval expired.", False
        await progress(f"Running MCP tool `{tool.server_name}/{tool.tool_name}`…")
        try:
            result = await self._mcp_manager.call_tool(call.name, call.arguments)
        except Exception:
            return "Tool execution failed. The tool server may be unavailable.", False
        return result.content, not result.is_error


def _assistant_tool_message(result: InferenceResult) -> InferenceMessage:
    return {
        "role": "assistant",
        "content": result.content or None,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": _json_arguments(call.arguments),
                },
            }
            for call in result.tool_calls
        ],
    }


def _json_arguments(arguments: dict[str, Any]) -> str:
    import json

    return json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))


def _aggregate_results(results: Sequence[InferenceResult]) -> InferenceResult:
    final = results[-1]
    return replace(
        final,
        duration_ms=sum(result.duration_ms for result in results),
        usage=InferenceUsage(
            prompt_tokens=_sum_known(result.usage.prompt_tokens for result in results),
            completion_tokens=_sum_known(result.usage.completion_tokens for result in results),
            total_tokens=_sum_known(result.usage.total_tokens for result in results),
        ),
    )


def _sum_known(values: Iterable[int | None]) -> int | None:
    materialized = tuple(values)
    known = tuple(value for value in materialized if value is not None)
    return sum(known) if known else None
