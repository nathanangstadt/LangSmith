import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Sequence

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
from opentelemetry.trace import Link, SpanContext, SpanKind, StatusCode, TraceFlags

from app.config import get_settings
from app.database import db_context
from app.models import OtelSpan

logger = logging.getLogger(__name__)
settings = get_settings()

ExportMode = Literal["none", "langsmith", "otel"]

_LANGSMITH_OTLP_ENDPOINT = "https://api.smith.langchain.com/otel/v1/traces"


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
    """Wraps OTLPSpanExporter and only forwards spans when active=True.

    active is read by the BatchSpanProcessor background thread and written by
    the toggle endpoint request thread, so access is guarded by a lock.
    """

    def __init__(self, inner: OTLPSpanExporter, initial_active: bool = False) -> None:
        self._inner = inner
        self._lock = threading.Lock()
        self._active: bool = initial_active

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    @active.setter
    def active(self, value: bool) -> None:
        with self._lock:
            self._active = value

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if not self.active:
            return SpanExportResult.SUCCESS
        return self._inner.export(spans)

    def shutdown(self) -> None:
        self._inner.shutdown()


# Always create a TracerProvider. Register the Postgres exporter with a
# SimpleSpanProcessor (synchronous) so spans land in the DB before the next
# SSE event is emitted. Register gated OTLP exporters for each configured
# destination — only one will be active at a time, controlled by export_mode.
_resource = Resource.create({"service.name": settings.otel_service_name})
_provider = TracerProvider(resource=_resource)
_provider.add_span_processor(SimpleSpanProcessor(PostgresSpanExporter()))

_langsmith_exporter: GatedOTLPExporter | None = None
if settings.langsmith_enabled:
    _headers = f"x-api-key={settings.langsmith_api_key}"
    if settings.langsmith_project:
        _headers += f",x-langsmith-project={settings.langsmith_project}"
    _langsmith_exporter = GatedOTLPExporter(
        OTLPSpanExporter(
            endpoint=_LANGSMITH_OTLP_ENDPOINT,
            headers=_parse_headers(_headers),
        )
    )
    _provider.add_span_processor(BatchSpanProcessor(_langsmith_exporter))

_otel_exporter: GatedOTLPExporter | None = None
if settings.otel_exporter_otlp_endpoint:
    _otel_exporter = GatedOTLPExporter(
        OTLPSpanExporter(
            endpoint=settings.otel_exporter_otlp_endpoint,
            headers=_parse_headers(settings.otel_exporter_otlp_headers),
        )
    )
    _provider.add_span_processor(BatchSpanProcessor(_otel_exporter))

# Set initial export mode: prefer langsmith, then otel, then none.
if _langsmith_exporter is not None:
    _langsmith_exporter.active = True
elif _otel_exporter is not None:
    _otel_exporter.active = True

trace.set_tracer_provider(_provider)

otel_tracer = trace.get_tracer("agent_playground")


@dataclass
class ActiveSpan:
    kind: str
    span_id: str  # OTEL hex span ID — stored for convenience in SSE events
    # _otel_span is set as a plain attribute after construction so asdict() ignores it.


class TelemetryManager:
    @property
    def export_mode(self) -> ExportMode:
        if _langsmith_exporter is not None and _langsmith_exporter.active:
            return "langsmith"
        if _otel_exporter is not None and _otel_exporter.active:
            return "otel"
        return "none"

    @export_mode.setter
    def export_mode(self, mode: ExportMode) -> None:
        if _langsmith_exporter is not None:
            _langsmith_exporter.active = (mode == "langsmith")
        if _otel_exporter is not None:
            _otel_exporter.active = (mode == "otel")

    def start_span(
        self,
        run: Any,
        name: str,
        kind: str,
        parent_otel_span: Any | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> ActiveSpan:
        otel_kind = SpanKind.CLIENT if kind in ("model", "tool") else SpanKind.INTERNAL
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

    def record_approval_span(
        self,
        run_id: str,
        approval_id: str,
        server_name: str,
        outcome: str,
        requested_at: datetime,
        resolved_at: datetime,
        link_trace_id: str | None = None,
        link_span_id: str | None = None,
    ) -> None:
        """Create a retroactive gen_ai.approval.wait span with accurate timestamps.

        Called at resolution time so no span stays open during the wait period.
        A span link back to gen_ai.agent.invoke preserves the causal relationship
        without requiring the root span to remain open across HTTP requests.
        """
        start_ns = int(requested_at.timestamp() * 1_000_000_000)
        end_ns = int(resolved_at.timestamp() * 1_000_000_000)
        links: list[Link] = []
        if link_trace_id and link_span_id:
            try:
                link_ctx = SpanContext(
                    trace_id=int(link_trace_id, 16),
                    span_id=int(link_span_id, 16),
                    is_remote=True,
                    trace_flags=TraceFlags(TraceFlags.SAMPLED),
                )
                links.append(Link(link_ctx))
            except ValueError:
                pass
        span = otel_tracer.start_span(
            "gen_ai.approval.wait",
            start_time=start_ns,
            links=links,
            kind=SpanKind.INTERNAL,
        )
        span.set_attribute("agent.run_id", run_id)
        span.set_attribute("approval.id", approval_id)
        span.set_attribute("approval.server", server_name)
        span.set_attribute("approval.outcome", outcome)
        span.set_attribute("approval.wait_ms", max(0, (end_ns - start_ns) // 1_000_000))
        span.set_status(trace.Status(StatusCode.ERROR if outcome == "denied" else StatusCode.OK))
        span.end(end_time=end_ns)

    def close_otel_span(self, span: ActiveSpan, *, status: str = "failed") -> None:
        """End the OTEL span (writes to DB) without the model-specific gen_ai
        attributes that end_span adds. Used to close the root span when a child
        span is the designated failure span."""
        otel_span = getattr(span, "_otel_span", None)
        if otel_span is not None:
            otel_span.set_status(
                trace.Status(StatusCode.ERROR if status == "failed" else StatusCode.OK)
            )
            otel_span.end()


telemetry_manager = TelemetryManager()
