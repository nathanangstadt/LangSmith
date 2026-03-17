import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AgentRun, RunStep, TelemetryEvent


settings = get_settings()


def _parse_headers(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    headers: dict[str, str] = {}
    for part in value.split(","):
        if "=" in part:
            key, raw = part.split("=", 1)
            headers[key.strip()] = raw.strip()
    return headers


if settings.otel_exporter_otlp_endpoint:
    provider = TracerProvider(resource=Resource.create({"service.name": settings.otel_service_name}))
    exporter = OTLPSpanExporter(
        endpoint=settings.otel_exporter_otlp_endpoint,
        headers=_parse_headers(settings.otel_exporter_otlp_headers),
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)


otel_tracer = trace.get_tracer("agent_playground")


@dataclass
class CanonicalSpan:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    kind: str
    status: str
    start_time: str
    end_time: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)
    token_usage: dict[str, Any] = field(default_factory=dict)
    input_payload: dict[str, Any] = field(default_factory=dict)
    output_payload: dict[str, Any] = field(default_factory=dict)
    langsmith_run_id: str | None = None
    otel_span_id: str | None = None
    local_exported: bool = True
    langsmith_exported: bool = False
    otel_exported: bool = False


class TelemetryManager:
    def start_span(
        self,
        run: AgentRun,
        name: str,
        kind: str,
        parent_span_id: str | None = None,
        attributes: dict[str, Any] | None = None,
        input_payload: dict[str, Any] | None = None,
    ) -> CanonicalSpan:
        return CanonicalSpan(
            trace_id=run.trace_id,
            span_id=uuid.uuid4().hex,
            parent_span_id=parent_span_id,
            name=name,
            kind=kind,
            status="in_progress",
            start_time=datetime.now(timezone.utc).isoformat(),
            attributes=attributes or {},
            input_payload=input_payload or {},
        )

    def end_span(
        self,
        db: Session,
        run: AgentRun,
        span: CanonicalSpan,
        *,
        step_index: int,
        status: str = "completed",
        output_payload: dict[str, Any] | None = None,
        token_usage: dict[str, Any] | None = None,
        metadata_json: dict[str, Any] | None = None,
    ) -> RunStep:
        span.status = status
        span.end_time = datetime.now(timezone.utc).isoformat()
        span.output_payload = output_payload or {}
        span.token_usage = token_usage or {}
        span.otel_exported = bool(settings.otel_exporter_otlp_endpoint)
        event_payload = asdict(span)
        step = RunStep(
            run_id=run.id,
            step_index=step_index,
            kind=span.kind,
            name=span.name,
            status=status,
            latency_ms=self._latency_ms(span.start_time, span.end_time),
            token_usage=span.token_usage,
            input_payload=span.input_payload,
            output_payload=span.output_payload,
            metadata_json=metadata_json or {},
            span_id=span.span_id,
            parent_span_id=span.parent_span_id,
            langsmith_run_id=span.langsmith_run_id,
            otel_span_id=span.otel_span_id,
        )
        db.add(step)
        db.flush()
        db.add(
            TelemetryEvent(
                run_id=run.id,
                step_id=step.id,
                event_type="span.completed",
                trace_id=span.trace_id,
                span_id=span.span_id,
                payload=event_payload,
            )
        )
        self._emit_otel(span)
        return step

    def record_event(
        self,
        db: Session,
        *,
        run_id: str,
        trace_id: str,
        span_id: str,
        event_type: str,
        payload: dict[str, Any],
        step_id: str | None = None,
    ) -> None:
        db.add(
            TelemetryEvent(
                run_id=run_id,
                step_id=step_id,
                event_type=event_type,
                trace_id=trace_id,
                span_id=span_id,
                payload=payload,
            )
        )

    @staticmethod
    def _latency_ms(start_time: str, end_time: str | None) -> int | None:
        if not end_time:
            return None
        start_dt = datetime.fromisoformat(start_time)
        end_dt = datetime.fromisoformat(end_time)
        return int((end_dt - start_dt).total_seconds() * 1000)

    def _emit_otel(self, span: CanonicalSpan) -> None:
        if not settings.otel_exporter_otlp_endpoint:
            return
        with otel_tracer.start_as_current_span(span.name) as otel_span:
            for key, value in span.attributes.items():
                otel_span.set_attribute(key, json.dumps(value) if isinstance(value, (dict, list)) else value)
            otel_span.set_attribute("agent.trace_id", span.trace_id)
            otel_span.set_attribute("agent.span_id", span.span_id)
            otel_span.set_attribute("agent.kind", span.kind)
            otel_span.set_attribute("agent.status", span.status)
            span.otel_span_id = format(otel_span.get_span_context().span_id, "016x")


telemetry_manager = TelemetryManager()

