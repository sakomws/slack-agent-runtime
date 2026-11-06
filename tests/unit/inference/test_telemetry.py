import json
import logging
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from slack_agent_runtime.inference.client import (
    InferenceRouterClient,
    InferenceRouterError,
)
from slack_agent_runtime.inference.telemetry import (
    REDACTED,
    InferenceTelemetry,
    RedactingJsonFormatter,
    configured_secret_values,
    estimate_cost_usd,
    parse_pricing_json,
)


def test_cost_math_is_opt_in_and_uses_configured_rates() -> None:
    pricing = parse_pricing_json(
        '{"model-a":{"input_usd_per_million_tokens":2.5,"output_usd_per_million_tokens":"10"}}'
    )

    assert estimate_cost_usd(pricing, "model-a", 2_000, 500) == Decimal("0.010")
    assert estimate_cost_usd(pricing, "unknown", 2_000, 500) is None
    assert estimate_cost_usd(pricing, "model-a", None, 500) is None


def test_pricing_json_rejects_invalid_schema() -> None:
    with pytest.raises(RuntimeError, match="exactly"):
        parse_pricing_json('{"model-a":{"input_usd_per_million_tokens":1}}')


def _telemetry() -> tuple[InferenceTelemetry, InMemorySpanExporter, InMemoryMetricReader]:
    span_exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    metric_reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[metric_reader])
    telemetry = InferenceTelemetry(
        tracer=tracer_provider.get_tracer("test"),
        meter=meter_provider.get_meter("test"),
        pricing=parse_pricing_json(
            '{"model-a":{"input_usd_per_million_tokens":1,"output_usd_per_million_tokens":2}}'
        ),
    )
    return telemetry, span_exporter, metric_reader


def _metric_values(reader: InMemoryMetricReader) -> dict[str, list[float | int]]:
    values: dict[str, list[float | int]] = {}
    data = reader.get_metrics_data()
    assert data is not None
    for resource_metric in data.resource_metrics:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                values[metric.name] = [
                    point.value if hasattr(point, "value") else point.sum
                    for point in metric.data.data_points
                ]
    return values


@pytest.mark.asyncio
async def test_success_records_provider_neutral_trace_and_metrics() -> None:
    telemetry, span_exporter, metric_reader = _telemetry()

    async def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "model-a",
                "choices": [{"message": {"content": "private completion"}}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 4,
                    "total_tokens": 14,
                },
            },
        )

    http_client = httpx.AsyncClient(
        base_url="https://api.example.com", transport=httpx.MockTransport(handle)
    )
    client = InferenceRouterClient(
        base_url="https://api.example.com",
        api_key="secret-key",
        model="model-a",
        http_client=http_client,
        telemetry=telemetry,
    )

    await client.complete("private prompt", web_search=True)

    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "inference.request"
    assert spans[0].attributes["inference.model"] == "model-a"
    assert spans[0].attributes["inference.web_search"] is True
    serialized_span = repr(spans[0].attributes)
    assert "private prompt" not in serialized_span
    assert "private completion" not in serialized_span

    metrics = _metric_values(metric_reader)
    assert metrics["inference.requests"] == [1]
    assert metrics["inference.input_tokens"] == [10]
    assert metrics["inference.output_tokens"] == [4]
    assert metrics["inference.total_tokens"] == [14]
    assert metrics["inference.estimated_cost"] == [0.000018]
    assert metrics["inference.duration"][0] >= 0
    await http_client.aclose()


@pytest.mark.asyncio
async def test_failure_records_error_without_message_content() -> None:
    telemetry, span_exporter, metric_reader = _telemetry()

    async def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="private upstream body")

    http_client = httpx.AsyncClient(
        base_url="https://api.example.com", transport=httpx.MockTransport(handle)
    )
    client = InferenceRouterClient(
        base_url="https://api.example.com",
        api_key="secret-key",
        model="model-a",
        http_client=http_client,
        telemetry=telemetry,
    )

    with pytest.raises(InferenceRouterError):
        await client.complete("private prompt")

    span = span_exporter.get_finished_spans()[0]
    assert span.attributes["error.type"] == "InferenceRouterError"
    assert "private prompt" not in repr(span.attributes)
    metrics = _metric_values(metric_reader)
    assert metrics["inference.requests"] == [1]
    assert metrics["inference.errors"] == [1]
    await http_client.aclose()


def test_json_logging_schema_and_recursive_redaction() -> None:
    formatter = RedactingJsonFormatter(["literal-secret"])
    record = logging.LogRecord(
        "test.logger",
        logging.INFO,
        __file__,
        1,
        "request literal-secret",
        (),
        None,
    )
    record.payload = {
        "safe": "visible",
        "messages": [{"content": "private message"}],
        "nested": {"authorization": "Bearer literal-secret"},
    }
    record.api_key = "literal-secret"

    payload = json.loads(formatter.format(record))

    assert {"timestamp", "level", "logger", "message"} <= payload.keys()
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.logger"
    assert payload["message"] == f"request {REDACTED}"
    assert payload["api_key"] == REDACTED
    assert payload["payload"]["messages"] == REDACTED
    assert payload["payload"]["nested"]["authorization"] == REDACTED
    rendered = json.dumps(payload)
    assert "literal-secret" not in rendered
    assert "private message" not in rendered


def test_mcp_environment_and_header_values_are_registered_as_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "example": {
                        "transport": "streamable-http",
                        "url": "https://mcp.example.com",
                        "headers": {"Authorization": "Bearer ${MCP_TOKEN}"},
                        "autoApprove": [],
                    }
                }
            }
        )
    )
    monkeypatch.setenv("SLACK_AGENT_RUNTIME_MCP_CONFIG", str(config))
    monkeypatch.setenv("MCP_TOKEN", "mcp-secret")

    values = configured_secret_values()

    assert "Bearer mcp-secret" in values
