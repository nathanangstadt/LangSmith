from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


SUPPORTED_MODELS = ["gpt-5-mini", "gpt-5-chat-latest", "gpt-5.4"]


class LLMConnectionCreate(BaseModel):
    name: str
    provider: str = "openai"
    model_name: str = "gpt-5-mini"
    temperature: float = 0.2
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class TelemetryConfig(BaseModel):
    langsmith_project: str = "agent-playground"
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    otel_enabled: bool = True
    otel_service_name: str = "agent-playground"


class AgentProfileCreate(BaseModel):
    name: str
    role: str = ""
    guidelines: str = ""
    output_style: str = ""
    model_name: str = "gpt-5-mini"
    temperature: float = 0.2
    max_iterations: int = 8
    telemetry_json: dict[str, Any] = Field(default_factory=dict)
    ui_json: dict[str, Any] = Field(default_factory=dict)
    llm_connection_id: str | None = None


class AgentProfileUpdate(BaseModel):
    name: str | None = None
    role: str | None = None
    guidelines: str | None = None
    output_style: str | None = None
    model_name: str | None = None
    temperature: float | None = None
    max_iterations: int | None = None
    telemetry_json: dict[str, Any] | None = None
    ui_json: dict[str, Any] | None = None
    llm_connection_id: str | None = None


class MCPServerCreate(BaseModel):
    name: str
    server_url: str
    token_url: str
    grant_type: str = "client_credentials"
    client_id: str
    client_secret: str
    scope: str = ""
    allowed_tools: list[str] = Field(default_factory=list)
    approval_mode: Literal["prompt", "auto"] = "prompt"
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_ms: int = 20000
    enabled: bool = True


class MCPServerTestRequest(BaseModel):
    server_id: str | None = None
    name: str
    server_url: str
    token_url: str
    grant_type: str = "client_credentials"
    client_id: str = ""
    client_secret: str = ""
    scope: str = ""
    allowed_tools: list[str] = Field(default_factory=list)
    approval_mode: Literal["prompt", "auto"] = "prompt"
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_ms: int = 20000
    enabled: bool = True


class MCPServerUpdate(BaseModel):
    name: str | None = None
    server_url: str | None = None
    token_url: str | None = None
    grant_type: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    scope: str | None = None
    allowed_tools: list[str] | None = None
    approval_mode: Literal["prompt", "auto"] | None = None
    headers: dict[str, str] | None = None
    timeout_ms: int | None = None
    enabled: bool | None = None


class ThreadCreate(BaseModel):
    agent_profile_id: str
    title: str = "New Thread"


class ThreadUpdate(BaseModel):
    title: str


class MessageCreate(BaseModel):
    content: str


class ApprovalResolve(BaseModel):
    status: Literal["approved", "denied"]
    rationale: str | None = None


class AgentMdImportRequest(BaseModel):
    content: str


class AgentProfileOut(BaseModel):
    id: str
    name: str
    role: str
    guidelines: str
    output_style: str
    model_name: str
    temperature: float
    max_iterations: int
    telemetry_json: dict[str, Any]
    ui_json: dict[str, Any]
    llm_connection_id: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MCPServerOut(BaseModel):
    id: str
    name: str
    server_url: str
    token_url: str
    grant_type: str
    scope: str
    allowed_tools: list[str]
    approval_mode: str
    headers: dict[str, str]
    timeout_ms: int
    enabled: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MCPServerDetailOut(MCPServerOut):
    client_id: str
    client_secret: str


class MessageOut(BaseModel):
    id: str
    thread_id: str
    role: str
    content: str
    metadata_json: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class ThreadOut(BaseModel):
    id: str
    title: str
    agent_profile_id: str
    created_at: datetime
    updated_at: datetime
    messages: list[MessageOut] = Field(default_factory=list)


class OtelSpanOut(BaseModel):
    id: str
    run_id: str | None
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    kind: str
    start_time_unix_nano: int
    end_time_unix_nano: int
    duration_ms: int | None
    status_code: str
    status_message: str
    attributes: dict[str, Any]
    events: list[dict[str, Any]]
    resource_attributes: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class ApprovalDecisionOut(BaseModel):
    id: str
    run_id: str
    mcp_server_id: str
    status: str
    rationale: str | None
    metadata_json: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}




