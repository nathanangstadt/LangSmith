import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import SpanKind, StatusCode

from app.config import get_settings
from app.database import db_context
from app.models import OtelSpan

logger = logging.getLogger(__name__)
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


class PostgresSpanExporter(SpanExporter):
    """Writes completed OTEL spans verbatim to the otel_spans table."""

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            with db_context() as db:
                for span in spans:
                    attrs = dict(span.attributes or {})
                    run_id = attrs.get("agent.run_id")
                    trace_id = format(span.context.trace_id, "032x")
                    span_id = format(span.context.span_id, "016x")
                    parent_span_id = (
                        format(span.parent.span_id, "016x")
                        if span.parent and span.parent.is_valid
                        else None
                    )
                    duration_ms = None
                    if span.start_time and span.end_time:
                        duration_ms = int((span.end_time - span.start_time) / 1_000_000)
                    db.add(
                        OtelSpan(
                            id=str(uuid.uuid4()),
                            run_id=run_id,
                            trace_id=trace_id,
                            span_id=span_id,
                            parent_span_id=parent_span_id,
                            name=span.name,
                            kind=span.kind.name,
                            start_time_unix_nano=span.start_time or 0,
                            end_time_unix_nano=span.end_time or 0,
                            duration_ms=duration_ms,
                            status_code=span.status.status_code.name,
                            status_message=span.status.description or "",
                            attributes=attrs,
                            events=[
                                {
                                    "name": e.name,
                                    "time_unix_nano": e.timestamp,
                                    "attributes": dict(e.attributes or {}),
                                }
                                for e in (span.events or [])
                            ],
                            resource_attributes=dict(span.resource.attributes or {}),
                        )
                    )
                db.commit()
            return SpanExportResult.SUCCESS
        except Exception:
            logger.exception("PostgresSpanExporter: failed to write spans")
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        pass


class GatedOTLPExporter(SpanExporter):
    """Wraps OTLPSpanExporter and only forwards spans when active=True."""

    def __init__(self, inner: OTLPSpanExporter) -> None:
        self._inner = inner
        self.active: bool = False

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if not self.active:
            return SpanExportResult.SUCCESS
        return self._inner.export(spans)

    def shutdown(self) -> None:
        self._inner.shutdown()


# Always create a TracerProvider. Register the Postgres exporter with a
# SimpleSpanProcessor (synchronous) so spans land in the DB before the next
# SSE event is emitted. Add the OTLP BatchSpanProcessor only when an endpoint
# is configured — gated so export can be toggled at runtime without restart.
_resource = Resource.create({"service.name": settings.otel_service_name})
_provider = TracerProvider(resource=_resource)
_provider.add_span_processor(SimpleSpanProcessor(PostgresSpanExporter()))

_gated_exporter: GatedOTLPExporter | None = None
if settings.otel_exporter_otlp_endpoint:
    _gated_exporter = GatedOTLPExporter(
        OTLPSpanExporter(
            endpoint=settings.otel_exporter_otlp_endpoint,
            headers=_parse_headers(settings.otel_exporter_otlp_headers),
        )
    )
    _provider.add_span_processor(BatchSpanProcessor(_gated_exporter))
trace.set_tracer_provider(_provider)

otel_tracer = trace.get_tracer("agent_playground")


@dataclass
class ActiveSpan:
    kind: str
    span_id: str  # OTEL hex span ID — stored for convenience in SSE events
    # _otel_span is set as a plain attribute after construction so asdict() ignores it.


class TelemetryManager:
    @property
    def otel_export_active(self) -> bool:
        return _gated_exporter.active if _gated_exporter is not None else False

    @otel_export_active.setter
    def otel_export_active(self, value: bool) -> None:
        if _gated_exporter is not None:
            _gated_exporter.active = value

    def start_span(
        self,
        run: Any,
        name: str,
        kind: str,
        parent_otel_span: Any | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> ActiveSpan:
        otel_kind = SpanKind.CLIENT if kind == "model" else SpanKind.INTERNAL
        parent_ctx = trace.set_span_in_context(parent_otel_span) if parent_otel_span else None
        otel_span = otel_tracer.start_span(name, context=parent_ctx, kind=otel_kind)

        otel_span.set_attribute("agent.run_id", run.id)
        otel_span.set_attribute("agent.kind", kind)
        for key, value in (attributes or {}).items():
            attr_name = key if "." in key else f"agent.{key}"
            if isinstance(value, (str, int, float, bool)):
                otel_span.set_attribute(attr_name, value)

        span = ActiveSpan(
            kind=kind,
            span_id=format(otel_span.get_span_context().span_id, "016x"),
        )
        span._otel_span = otel_span  # type: ignore[attr-defined]
        return span

    def end_span(self, span: ActiveSpan, *, status: str = "completed") -> None:
        otel_span = getattr(span, "_otel_span", None)
        if otel_span is None:
            return
        if span.kind == "model":
            model_name = str(otel_span.attributes.get("gen_ai.request.model", ""))  # type: ignore[union-attr]
            gen_ai_system = "anthropic" if "claude" in model_name else "openai"
            otel_span.set_attribute("gen_ai.system", gen_ai_system)
            otel_span.set_attribute("gen_ai.operation.name", "chat")
        otel_span.set_status(
            trace.Status(StatusCode.ERROR if status == "failed" else StatusCode.OK)
        )
        otel_span.end()  # triggers PostgresSpanExporter synchronously

    def close_otel_span(self, span: ActiveSpan, *, status: str = "failed") -> None:
        """End the OTEL span without a DB span record (for parent cleanup on error)."""
        otel_span = getattr(span, "_otel_span", None)
        if otel_span is not None:
            otel_span.set_status(
                trace.Status(StatusCode.ERROR if status == "failed" else StatusCode.OK)
            )
            otel_span.end()


telemetry_manager = TelemetryManager()
