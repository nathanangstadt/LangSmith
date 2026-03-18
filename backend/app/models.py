import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.database import Base


def json_type():
    return JSONB().with_variant(JSON, "sqlite")


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class LLMConnection(Base, TimestampMixin):
    __tablename__ = "llm_connections"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(120), unique=True)
    provider: Mapped[str] = mapped_column(String(32), default="openai")
    model_name: Mapped[str] = mapped_column(String(64), default="gpt-5-mini")
    temperature: Mapped[float] = mapped_column(default=0.2)
    metadata_json: Mapped[dict] = mapped_column(json_type(), default=dict)


class AgentProfile(Base, TimestampMixin):
    __tablename__ = "agent_profiles"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(120), unique=True)
    role: Mapped[str] = mapped_column(Text, default="")
    guidelines: Mapped[str] = mapped_column(Text, default="")
    output_style: Mapped[str] = mapped_column(Text, default="")
    model_name: Mapped[str] = mapped_column(String(64), default="gpt-5-mini")
    temperature: Mapped[float] = mapped_column(default=0.2)
    max_iterations: Mapped[int] = mapped_column(Integer, default=8)
    telemetry_json: Mapped[dict] = mapped_column(json_type(), default=dict)
    ui_json: Mapped[dict] = mapped_column(json_type(), default=dict)
    imported_agent_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_connection_id: Mapped[str | None] = mapped_column(ForeignKey("llm_connections.id"), nullable=True)

    llm_connection: Mapped[LLMConnection | None] = relationship()


class MCPServer(Base, TimestampMixin):
    __tablename__ = "mcp_servers"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(120), unique=True)
    label: Mapped[str] = mapped_column(String(120))
    server_url: Mapped[str] = mapped_column(Text)
    token_url: Mapped[str] = mapped_column(Text)
    grant_type: Mapped[str] = mapped_column(String(64), default="client_credentials")
    client_id_encrypted: Mapped[str] = mapped_column(Text)
    client_secret_encrypted: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(Text, default="")
    allowed_tools: Mapped[list] = mapped_column(json_type(), default=list)
    approval_mode: Mapped[str] = mapped_column(String(16), default="prompt")
    headers: Mapped[dict] = mapped_column(json_type(), default=dict)
    timeout_ms: Mapped[int] = mapped_column(Integer, default=20000)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Thread(Base, TimestampMixin):
    __tablename__ = "threads"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    title: Mapped[str] = mapped_column(String(200), default="New Thread")
    agent_profile_id: Mapped[str] = mapped_column(ForeignKey("agent_profiles.id"))

    agent_profile: Mapped[AgentProfile] = relationship()


class Message(Base, TimestampMixin):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    thread_id: Mapped[str] = mapped_column(ForeignKey("threads.id"))
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column(json_type(), default=dict)


class AgentRun(Base, TimestampMixin):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    thread_id: Mapped[str] = mapped_column(ForeignKey("threads.id"))
    agent_profile_id: Mapped[str] = mapped_column(ForeignKey("agent_profiles.id"))
    status: Mapped[str] = mapped_column(String(32), default="queued")
    user_message_id: Mapped[str | None] = mapped_column(ForeignKey("messages.id"), nullable=True)
    assistant_message_id: Mapped[str | None] = mapped_column(ForeignKey("messages.id"), nullable=True)
    trace_id: Mapped[str] = mapped_column(String(64), default=lambda: uuid.uuid4().hex)
    langsmith_run_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    otel_trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(json_type(), default=dict)


class RunStep(Base, TimestampMixin):
    __tablename__ = "run_steps"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"))
    step_index: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(32), default="completed")
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_usage: Mapped[dict] = mapped_column(json_type(), default=dict)
    input_payload: Mapped[dict] = mapped_column(json_type(), default=dict)
    output_payload: Mapped[dict] = mapped_column(json_type(), default=dict)
    metadata_json: Mapped[dict] = mapped_column(json_type(), default=dict)
    span_id: Mapped[str] = mapped_column(String(64))
    parent_span_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    langsmith_run_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    otel_span_id: Mapped[str | None] = mapped_column(String(128), nullable=True)


class TelemetryEvent(Base, TimestampMixin):
    __tablename__ = "telemetry_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"))
    step_id: Mapped[str | None] = mapped_column(ForeignKey("run_steps.id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64))
    trace_id: Mapped[str] = mapped_column(String(64))
    span_id: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(json_type(), default=dict)


class ApprovalDecision(Base, TimestampMixin):
    __tablename__ = "approval_decisions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"))
    mcp_server_id: Mapped[str] = mapped_column(ForeignKey("mcp_servers.id"))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(json_type(), default=dict)

