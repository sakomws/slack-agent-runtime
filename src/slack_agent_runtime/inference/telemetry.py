"""Redacted JSON logging and opt-in OpenTelemetry instrumentation."""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Status, StatusCode, Tracer

if TYPE_CHECKING:
    from opentelemetry.metrics import Counter, Histogram, Meter

SERVICE_NAME: Final = "slack-agent-runtime"
REDACTED: Final = "[REDACTED]"
SENSITIVE_FIELDS: Final = frozenset(
    {"text", "content", "prompt", "messages", "token", "api_key", "authorization"}
)
_STANDARD_LOG_RECORD_FIELDS: Final = frozenset(logging.makeLogRecord({}).__dict__)


@dataclass(frozen=True)
class ModelPricing:
    input_usd_per_million_tokens: Decimal
    output_usd_per_million_tokens: Decimal


def parse_pricing_json(raw: str) -> dict[str, ModelPricing]:
    if not raw.strip():
        return {}
    try:
        payload: Any = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError("INFERENCE_ROUTER_MODEL_PRICING_JSON must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("INFERENCE_ROUTER_MODEL_PRICING_JSON must be a JSON object")

    pricing: dict[str, ModelPricing] = {}
    required = {"input_usd_per_million_tokens", "output_usd_per_million_tokens"}
    for model, rates in payload.items():
        if not isinstance(model, str) or not model or not isinstance(rates, dict):
            raise RuntimeError("Model pricing entries must map model IDs to rate objects")
        if set(rates) != required:
            raise RuntimeError(
                f"Model pricing for {model!r} must contain exactly {sorted(required)}"
            )
        try:
            input_rate = Decimal(str(rates["input_usd_per_million_tokens"]))
            output_rate = Decimal(str(rates["output_usd_per_million_tokens"]))
        except Exception as exc:
            raise RuntimeError(f"Model pricing for {model!r} must contain numeric rates") from exc
        if not input_rate.is_finite() or not output_rate.is_finite():
            raise RuntimeError(f"Model pricing for {model!r} must contain finite rates")
        if input_rate < 0 or output_rate < 0:
            raise RuntimeError(f"Model pricing for {model!r} cannot be negative")
        pricing[model] = ModelPricing(input_rate, output_rate)
    return pricing


def estimate_cost_usd(
    pricing: Mapping[str, ModelPricing],
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
) -> Decimal | None:
    rates = pricing.get(model)
    if rates is None or input_tokens is None or output_tokens is None:
        return None
    million = Decimal(1_000_000)
    return (
        Decimal(input_tokens) * rates.input_usd_per_million_tokens
        + Decimal(output_tokens) * rates.output_usd_per_million_tokens
    ) / million


class InferenceTelemetry:
    def __init__(
        self,
        *,
        tracer: Tracer | None = None,
        meter: Meter | None = None,
        pricing: Mapping[str, ModelPricing] | None = None,
    ) -> None:
        self.tracer = tracer or trace.get_tracer(__name__)
        active_meter = meter or metrics.get_meter(__name__)
        self.pricing = dict(pricing or {})
        self.requests: Counter = active_meter.create_counter(
            "inference.requests", description="Inference requests"
        )
        self.errors: Counter = active_meter.create_counter(
            "inference.errors", description="Failed inference requests"
        )
        self.latency: Histogram = active_meter.create_histogram(
            "inference.duration", unit="ms", description="Inference request latency"
        )
        self.input_tokens: Counter = active_meter.create_counter(
            "inference.input_tokens", unit="{token}", description="Inference input tokens"
        )
        self.output_tokens: Counter = active_meter.create_counter(
            "inference.output_tokens", unit="{token}", description="Inference output tokens"
        )
        self.total_tokens: Counter = active_meter.create_counter(
            "inference.total_tokens", unit="{token}", description="Inference total tokens"
        )
        self.cost: Counter = active_meter.create_counter(
            "inference.estimated_cost", unit="USD", description="Estimated inference cost"
        )

    @staticmethod
    def attributes(model: str, web_search: bool) -> dict[str, str | bool]:
        return {"inference.model": model, "inference.web_search": web_search}

    def start_span(self, model: str, web_search: bool) -> Span:
        return self.tracer.start_span(
            "inference.request",
            attributes=self.attributes(model, web_search),
        )

    def record_success(
        self,
        span: Span,
        *,
        model: str,
        web_search: bool,
        duration_ms: float,
        input_tokens: int | None,
        output_tokens: int | None,
        total_tokens: int | None,
    ) -> None:
        attributes = self.attributes(model, web_search)
        self.requests.add(1, attributes)
        self.latency.record(duration_ms, attributes)
        for token_type, instrument, value in (
            ("input", self.input_tokens, input_tokens),
            ("output", self.output_tokens, output_tokens),
            ("total", self.total_tokens, total_tokens),
        ):
            if value is not None:
                instrument.add(value, attributes)
                span.set_attribute(f"inference.tokens.{token_type}", value)
        estimated_cost = estimate_cost_usd(self.pricing, model, input_tokens, output_tokens)
        if estimated_cost is not None:
            cost = float(estimated_cost)
            self.cost.add(cost, attributes)
            span.set_attribute("inference.estimated_cost_usd", cost)
        span.set_attribute("inference.response.model", model)
        span.set_status(Status(StatusCode.OK))

    def record_error(
        self,
        span: Span,
        *,
        model: str,
        web_search: bool,
        duration_ms: float,
        error_type: str,
    ) -> None:
        attributes = self.attributes(model, web_search)
        error_attributes = {**attributes, "error.type": error_type}
        self.requests.add(1, attributes)
        self.errors.add(1, error_attributes)
        self.latency.record(duration_ms, attributes)
        span.set_attribute("error.type", error_type)
        span.set_status(Status(StatusCode.ERROR))


class TelemetryRuntime:
    def __init__(
        self,
        telemetry: InferenceTelemetry,
        *,
        tracer_provider: TracerProvider | None = None,
        meter_provider: MeterProvider | None = None,
    ) -> None:
        self.telemetry = telemetry
        self._tracer_provider = tracer_provider
        self._meter_provider = meter_provider
        self._shutdown = False

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        if self._meter_provider is not None:
            self._meter_provider.shutdown()
        if self._tracer_provider is not None:
            self._tracer_provider.shutdown()


_runtime: TelemetryRuntime | None = None
_runtime_lock = threading.Lock()


def configure_telemetry() -> TelemetryRuntime:
    global _runtime
    with _runtime_lock:
        if _runtime is not None:
            return _runtime
        pricing = parse_pricing_json(os.getenv("INFERENCE_ROUTER_MODEL_PRICING_JSON", ""))
        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
        if not endpoint:
            _runtime = TelemetryRuntime(InferenceTelemetry(pricing=pricing))
            return _runtime

        resource = Resource.create({"service.name": SERVICE_NAME})
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        _runtime = TelemetryRuntime(
            InferenceTelemetry(
                tracer=tracer_provider.get_tracer(__name__),
                meter=meter_provider.get_meter(__name__),
                pricing=pricing,
            ),
            tracer_provider=tracer_provider,
            meter_provider=meter_provider,
        )
        return _runtime


def get_telemetry() -> InferenceTelemetry:
    return configure_telemetry().telemetry


def shutdown_telemetry() -> None:
    global _runtime
    with _runtime_lock:
        runtime = _runtime
        _runtime = None
    if runtime is not None:
        runtime.shutdown()


def _redact(value: Any, secret_values: Sequence[str], *, field_name: str | None = None) -> Any:
    if field_name is not None and field_name.lower() in SENSITIVE_FIELDS:
        return REDACTED
    if isinstance(value, Mapping):
        return {
            str(key): _redact(item, secret_values, field_name=str(key))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item, secret_values) for item in value]
    if isinstance(value, str):
        redacted = value
        for secret in secret_values:
            if secret:
                redacted = redacted.replace(secret, REDACTED)
        return redacted
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact(str(value), secret_values)


class RedactingJsonFormatter(logging.Formatter):
    def __init__(self, secret_values: Sequence[str] = ()) -> None:
        super().__init__()
        self.secret_values = tuple(value for value in secret_values if value)

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _redact(record.getMessage(), self.secret_values),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_LOG_RECORD_FIELDS and key not in {"message", "asctime"}:
                payload[key] = _redact(value, self.secret_values, field_name=key)
        if record.exc_info:
            payload["exception"] = _redact(
                self.formatException(record.exc_info), self.secret_values
            )
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)


_logging_lock = threading.Lock()


class _JsonStreamHandler(logging.StreamHandler[Any]):
    pass


def configure_logging(level: int = logging.INFO, *, secret_values: Sequence[str] = ()) -> None:
    with _logging_lock:
        root = logging.getLogger()
        handler = next(
            (item for item in root.handlers if isinstance(item, _JsonStreamHandler)), None
        )
        if handler is None:
            handler = _JsonStreamHandler()
            root.handlers.clear()
            root.addHandler(handler)
        handler.setFormatter(RedactingJsonFormatter(secret_values))
        root.setLevel(level)


def configured_secret_values() -> tuple[str, ...]:
    names = (
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "INFERENCE_ROUTER_API_KEY",
        "SLACK_AGENT_RUNTIME_GITHUB_TOKEN",
        "THREAD_STATE_URL",
        "OTEL_EXPORTER_OTLP_HEADERS",
    )
    values = [value for name in names if (value := os.getenv(name, ""))]
    mcp_path = os.getenv("SLACK_AGENT_RUNTIME_MCP_CONFIG", "").strip()
    if mcp_path:
        try:
            from slack_agent_runtime.mcp.config import load_mcp_config

            for server in load_mcp_config(mcp_path).servers:
                values.extend(value for _, value in (*server.headers, *server.env) if value)
        except RuntimeError:
            # Settings validation reports the actionable configuration error.
            pass
    return tuple(values)
