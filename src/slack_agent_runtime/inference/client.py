"""Provider-neutral OpenAI-compatible inference and SSE streaming client."""

import json
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias, TypedDict

import httpx
from opentelemetry.trace import use_span

from slack_agent_runtime.inference.telemetry import InferenceTelemetry, get_telemetry


class InferenceRouterError(RuntimeError):
    """A safe error raised when the router cannot produce an answer."""


@dataclass(frozen=True)
class InferenceUsage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class InferenceToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


InferenceMessage: TypeAlias = dict[str, Any]
InferenceToolSchema: TypeAlias = dict[str, Any]


@dataclass(frozen=True)
class InferenceResult:
    content: str
    model: str
    request_id: str | None
    duration_ms: float
    usage: InferenceUsage
    tool_calls: tuple[InferenceToolCall, ...] = ()


@dataclass(frozen=True)
class InferenceStreamDelta:
    content: str
    model: str | None = None
    request_id: str | None = None


@dataclass(frozen=True)
class InferenceStreamComplete:
    result: InferenceResult


InferenceStreamEvent: TypeAlias = InferenceStreamDelta | InferenceStreamComplete


class InferenceOptions(TypedDict, total=False):
    temperature: float
    top_p: float
    max_tokens: int
    reasoning_effort: str


class InferenceClient(Protocol):
    streaming: bool

    async def complete(
        self,
        prompt: str | None = None,
        *,
        messages: Sequence[InferenceMessage] | None = None,
        model: str | None = None,
        web_search: bool = False,
        tools: Sequence[InferenceToolSchema] | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> InferenceResult: ...

    def stream_complete(
        self,
        prompt: str | None = None,
        *,
        messages: Sequence[InferenceMessage] | None = None,
        model: str | None = None,
        web_search: bool = False,
        tools: Sequence[InferenceToolSchema] | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> AsyncIterator[InferenceStreamEvent]: ...

    async def list_models(self) -> tuple[str, ...]: ...

    async def close(self) -> None: ...


class InferenceRouterClient:
    streaming = True

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        http_client: httpx.AsyncClient | None = None,
        telemetry: InferenceTelemetry | None = None,
    ) -> None:
        self._model = model
        self._telemetry = telemetry or get_telemetry()
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(120.0),
        )
        self._headers = {
            "Authorization": f"Bearer {api_key}",
        }

    async def complete(
        self,
        prompt: str | None = None,
        *,
        messages: Sequence[InferenceMessage] | None = None,
        model: str | None = None,
        web_search: bool = False,
        tools: Sequence[InferenceToolSchema] | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> InferenceResult:
        requested_model = model or self._model
        started_at = time.perf_counter()
        span = self._telemetry.start_span(requested_model, web_search)
        try:
            with use_span(span, end_on_exit=False):
                result = await self._complete_uninstrumented(
                    prompt,
                    messages=messages,
                    model=model,
                    web_search=web_search,
                    tools=tools,
                    temperature=temperature,
                    top_p=top_p,
                    max_tokens=max_tokens,
                    reasoning_effort=reasoning_effort,
                )
            self._telemetry.record_success(
                span,
                model=result.model,
                web_search=web_search,
                duration_ms=result.duration_ms,
                input_tokens=result.usage.prompt_tokens,
                output_tokens=result.usage.completion_tokens,
                total_tokens=result.usage.total_tokens,
            )
            return result
        except BaseException as exc:
            self._telemetry.record_error(
                span,
                model=requested_model,
                web_search=web_search,
                duration_ms=(time.perf_counter() - started_at) * 1_000,
                error_type=type(exc).__name__,
            )
            raise
        finally:
            span.end()

    async def _complete_uninstrumented(
        self,
        prompt: str | None = None,
        *,
        messages: Sequence[InferenceMessage] | None = None,
        model: str | None = None,
        web_search: bool = False,
        tools: Sequence[InferenceToolSchema] | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> InferenceResult:
        requested_model = model or self._model
        body = self._request_body(
            prompt,
            messages=messages,
            model=requested_model,
            stream=False,
            web_search=web_search,
            tools=tools,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )

        started_at = time.perf_counter()
        try:
            response = await self._http_client.post(
                "/v1/chat/completions",
                headers=self._headers,
                json=body,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise InferenceRouterError(
                f"Inference Router returned HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise InferenceRouterError("Inference Router request failed") from exc
        elapsed_ms = (time.perf_counter() - started_at) * 1_000

        try:
            payload: dict[str, Any] = response.json()
            message = payload["choices"][0]["message"]
            content_value = message.get("content")
            content = content_value if isinstance(content_value, str) else ""
            tool_calls = _tool_calls(message.get("tool_calls"))
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise InferenceRouterError("Inference Router returned an invalid response") from exc

        if not content.strip() and not tool_calls:
            raise InferenceRouterError("Inference Router returned an empty response")
        usage_payload = payload.get("usage")
        usage = (
            InferenceUsage(
                prompt_tokens=_optional_int(usage_payload.get("prompt_tokens")),
                completion_tokens=_optional_int(usage_payload.get("completion_tokens")),
                total_tokens=_optional_int(usage_payload.get("total_tokens")),
            )
            if isinstance(usage_payload, dict)
            else InferenceUsage()
        )
        response_model = payload.get("model")
        return InferenceResult(
            content=content,
            model=(
                response_model
                if isinstance(response_model, str) and response_model
                else requested_model
            ),
            request_id=response.headers.get("x-request-id"),
            duration_ms=elapsed_ms,
            usage=usage,
            tool_calls=tool_calls,
        )

    async def stream_complete(
        self,
        prompt: str | None = None,
        *,
        messages: Sequence[InferenceMessage] | None = None,
        model: str | None = None,
        web_search: bool = False,
        tools: Sequence[InferenceToolSchema] | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> AsyncIterator[InferenceStreamEvent]:
        requested_model = model or self._model
        started_at = time.perf_counter()
        span = self._telemetry.start_span(requested_model, web_search)
        final_result: InferenceResult | None = None
        try:
            with use_span(span, end_on_exit=False):
                async for event in self._stream_complete_uninstrumented(
                    prompt,
                    messages=messages,
                    model=model,
                    web_search=web_search,
                    tools=tools,
                    temperature=temperature,
                    top_p=top_p,
                    max_tokens=max_tokens,
                    reasoning_effort=reasoning_effort,
                ):
                    if isinstance(event, InferenceStreamComplete):
                        final_result = event.result
                    yield event
            if final_result is None:
                raise InferenceRouterError("Inference Router stream ended unexpectedly")
            self._telemetry.record_success(
                span,
                model=final_result.model,
                web_search=web_search,
                duration_ms=final_result.duration_ms,
                input_tokens=final_result.usage.prompt_tokens,
                output_tokens=final_result.usage.completion_tokens,
                total_tokens=final_result.usage.total_tokens,
            )
        except BaseException as exc:
            self._telemetry.record_error(
                span,
                model=requested_model,
                web_search=web_search,
                duration_ms=(time.perf_counter() - started_at) * 1_000,
                error_type=type(exc).__name__,
            )
            raise
        finally:
            span.end()

    async def _stream_complete_uninstrumented(
        self,
        prompt: str | None = None,
        *,
        messages: Sequence[InferenceMessage] | None = None,
        model: str | None = None,
        web_search: bool = False,
        tools: Sequence[InferenceToolSchema] | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> AsyncIterator[InferenceStreamEvent]:
        requested_model = model or self._model
        body = self._request_body(
            prompt,
            messages=messages,
            model=requested_model,
            stream=True,
            web_search=web_search,
            tools=tools,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )
        body["stream_options"] = {"include_usage": True}
        started_at = time.perf_counter()
        content_parts: list[str] = []
        response_model = requested_model
        usage = InferenceUsage()
        request_id: str | None = None
        saw_done = False
        tool_call_parts: dict[int, dict[str, str]] = {}

        try:
            async with self._http_client.stream(
                "POST",
                "/v1/chat/completions",
                headers=self._headers,
                json=body,
            ) as response:
                response.raise_for_status()
                request_id = response.headers.get("x-request-id") or response.headers.get(
                    "request-id"
                )
                async for data in _iter_sse_data(response):
                    if data.strip() == "[DONE]":
                        saw_done = True
                        break
                    try:
                        payload: Any = json.loads(data)
                    except (TypeError, ValueError) as exc:
                        raise InferenceRouterError(
                            "Inference Router returned an invalid stream"
                        ) from exc
                    if not isinstance(payload, dict):
                        raise InferenceRouterError("Inference Router returned an invalid stream")

                    payload_model = payload.get("model")
                    if isinstance(payload_model, str) and payload_model:
                        response_model = payload_model
                    payload_request_id = payload.get("request_id")
                    if request_id is None and isinstance(payload_request_id, str):
                        request_id = payload_request_id or None
                    usage_payload = payload.get("usage")
                    if isinstance(usage_payload, dict):
                        usage = _usage(usage_payload)

                    choices = payload.get("choices")
                    if choices is None:
                        continue
                    if not isinstance(choices, list):
                        raise InferenceRouterError("Inference Router returned an invalid stream")
                    for choice in choices:
                        if not isinstance(choice, dict):
                            raise InferenceRouterError(
                                "Inference Router returned an invalid stream"
                            )
                        delta = choice.get("delta")
                        if not isinstance(delta, dict):
                            continue
                        content = delta.get("content")
                        if content is not None and not isinstance(content, str):
                            raise InferenceRouterError(
                                "Inference Router returned an invalid stream"
                            )
                        if content:
                            content_parts.append(content)
                            yield InferenceStreamDelta(
                                content=content,
                                model=response_model,
                                request_id=request_id,
                            )
                        _collect_tool_call_deltas(delta.get("tool_calls"), tool_call_parts)
        except httpx.HTTPStatusError as exc:
            raise InferenceRouterError(
                f"Inference Router returned HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise InferenceRouterError("Inference Router request failed") from exc

        if not saw_done:
            raise InferenceRouterError("Inference Router stream ended unexpectedly")
        content = "".join(content_parts)
        tool_calls = _assembled_tool_calls(tool_call_parts)
        if not content.strip() and not tool_calls:
            raise InferenceRouterError("Inference Router returned an empty response")
        yield InferenceStreamComplete(
            InferenceResult(
                content=content,
                model=response_model,
                request_id=request_id,
                duration_ms=(time.perf_counter() - started_at) * 1_000,
                usage=usage,
                tool_calls=tool_calls,
            )
        )

    async def list_models(self) -> tuple[str, ...]:
        try:
            response = await self._http_client.get(
                "/v1/models",
                headers=self._headers,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise InferenceRouterError(
                f"Inference Router returned HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise InferenceRouterError("Inference Router request failed") from exc

        try:
            payload: dict[str, Any] = response.json()
            models = {
                item["id"]
                for item in payload["data"]
                if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise InferenceRouterError("Inference Router returned an invalid model list") from exc

        if not models:
            raise InferenceRouterError("Inference Router returned an empty model list")
        return tuple(sorted(models))

    async def close(self) -> None:
        if self._owns_http_client:
            await self._http_client.aclose()

    @staticmethod
    def _request_body(
        prompt: str | None,
        *,
        messages: Sequence[InferenceMessage] | None,
        model: str,
        stream: bool,
        web_search: bool,
        tools: Sequence[InferenceToolSchema] | None,
        temperature: float | None,
        top_p: float | None,
        max_tokens: int | None,
        reasoning_effort: str | None,
    ) -> dict[str, Any]:
        if messages is None:
            if prompt is None:
                raise ValueError("prompt or messages is required")
            request_messages = [{"role": "user", "content": prompt}]
        else:
            request_messages = [dict(message) for message in messages]
        body: dict[str, Any] = {
            "model": model,
            "messages": request_messages,
            "stream": stream,
        }
        request_tools = [dict(tool) for tool in tools or ()]
        if web_search:
            request_tools.insert(0, {"type": "web_search"})
        if request_tools:
            body["tools"] = request_tools
        optional_params = {
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
            "reasoning_effort": reasoning_effort,
        }
        body.update({name: value for name, value in optional_params.items() if value is not None})
        return body


async def _iter_sse_data(response: httpx.Response) -> AsyncIterator[str]:
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if not line:
            if data_lines:
                yield "\n".join(data_lines)
                data_lines.clear()
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if field != "data":
            continue
        if separator and value.startswith(" "):
            value = value[1:]
        data_lines.append(value)
    if data_lines:
        yield "\n".join(data_lines)


def _usage(payload: dict[str, Any]) -> InferenceUsage:
    return InferenceUsage(
        prompt_tokens=_optional_int(payload.get("prompt_tokens")),
        completion_tokens=_optional_int(payload.get("completion_tokens")),
        total_tokens=_optional_int(payload.get("total_tokens")),
    )


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _tool_calls(value: Any) -> tuple[InferenceToolCall, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("tool_calls must be a list")
    calls: list[InferenceToolCall] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("tool call must be an object")
        identifier = item.get("id")
        function = item.get("function")
        if not isinstance(identifier, str) or not identifier or not isinstance(function, dict):
            raise ValueError("invalid tool call")
        name = function.get("name")
        arguments = function.get("arguments")
        if not isinstance(name, str) or not name or not isinstance(arguments, str):
            raise ValueError("invalid tool function")
        parsed = json.loads(arguments)
        if not isinstance(parsed, dict):
            raise ValueError("tool arguments must be an object")
        calls.append(InferenceToolCall(identifier, name, parsed))
    return tuple(calls)


def _collect_tool_call_deltas(value: Any, parts: dict[int, dict[str, str]]) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        raise InferenceRouterError("Inference Router returned an invalid stream")
    for item in value:
        if not isinstance(item, dict):
            raise InferenceRouterError("Inference Router returned an invalid stream")
        index = item.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise InferenceRouterError("Inference Router returned an invalid stream")
        target = parts.setdefault(index, {"id": "", "name": "", "arguments": ""})
        identifier = item.get("id")
        if identifier is not None:
            if not isinstance(identifier, str):
                raise InferenceRouterError("Inference Router returned an invalid stream")
            target["id"] += identifier
        function = item.get("function")
        if function is None:
            continue
        if not isinstance(function, dict):
            raise InferenceRouterError("Inference Router returned an invalid stream")
        for key in ("name", "arguments"):
            fragment = function.get(key)
            if fragment is not None:
                if not isinstance(fragment, str):
                    raise InferenceRouterError("Inference Router returned an invalid stream")
                target[key] += fragment


def _assembled_tool_calls(parts: dict[int, dict[str, str]]) -> tuple[InferenceToolCall, ...]:
    if not parts:
        return ()
    raw_calls = [
        {
            "id": part["id"],
            "function": {
                "name": part["name"],
                "arguments": part["arguments"],
            },
        }
        for _, part in sorted(parts.items())
    ]
    try:
        return _tool_calls(raw_calls)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise InferenceRouterError("Inference Router returned an invalid stream") from exc
