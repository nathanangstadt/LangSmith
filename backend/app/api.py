from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agent_md import export_agent_md, parse_agent_md
from app.database import get_db
from app.mcp import build_openai_mcp_tool, discover_mcp_tools, serialize_mcp_server
from app.models import (
    AgentProfile,
    AgentRun,
    ApprovalDecision,
    LLMConnection,
    MCPServer,
    Message,
    OtelSpan,
    Thread,
)
from app.runtime import agent_runtime
from app.telemetry import telemetry_manager
from app.schemas import (
    AgentMdImportRequest,
    AgentProfileCreate,
    AgentProfileOut,
    AgentProfileUpdate,
    ApprovalResolve,
    LLMConnectionCreate,
    MCPServerCreate,
    MCPServerDetailOut,
    MCPServerOut,
    MCPServerTestRequest,
    MCPServerUpdate,
    MessageCreate,
    MessageOut,
    OtelSpanOut,
    ThreadCreate,
    ThreadUpdate,
    ThreadOut,
)

from app.security import secret_box


router = APIRouter()


async def _test_server_config(server: MCPServer) -> dict[str, Any]:
    try:
        tool, meta = await build_openai_mcp_tool(server)
        discovered_tools, _ = await discover_mcp_tools(server)
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text.strip() or str(exc)
        raise HTTPException(status_code=400, detail=f"MCP token request failed: {detail}")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=400, detail=f"MCP connection failed: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"MCP tool discovery failed: {exc}")
    redacted_tool = dict(tool)
    if isinstance(redacted_tool.get("headers"), dict):
        redacted_headers = dict(redacted_tool["headers"])
        if "Authorization" in redacted_headers:
            redacted_headers["Authorization"] = "<redacted>"
        redacted_tool["headers"] = redacted_headers
    return {
        "ok": True,
        "tool": redacted_tool,
        "token_meta": meta,
        "discovered_tools": discovered_tools,
    }


def _delete_thread_records(db: Session, thread_id: str) -> None:
    run_ids = [run_id for (run_id,) in db.query(AgentRun.id).filter(AgentRun.thread_id == thread_id).all()]
    if run_ids:
        db.execute(delete(OtelSpan).where(OtelSpan.run_id.in_(run_ids)))
        db.execute(delete(ApprovalDecision).where(ApprovalDecision.run_id.in_(run_ids)))
        db.execute(delete(AgentRun).where(AgentRun.id.in_(run_ids)))
    db.execute(delete(Message).where(Message.thread_id == thread_id))


@router.get("/health")
def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/config")
def get_config() -> dict[str, bool | str]:
    from app.config import get_settings as _get_settings
    s = _get_settings()
    return {
        "langsmith_enabled": s.langsmith_tracing and bool(s.langsmith_api_key),
        "langsmith_project": s.langsmith_project,
        "otel_enabled": bool(s.otel_exporter_otlp_endpoint),
        "otel_endpoint": s.otel_exporter_otlp_endpoint or "",
        "otel_export_active": telemetry_manager.otel_export_active,
        "jaeger_ui_url": s.jaeger_ui_url,
        "openai_configured": bool(s.openai_api_key),
    }


@router.post("/otel/toggle")
def toggle_otel_export() -> dict[str, bool]:
    telemetry_manager.otel_export_active = not telemetry_manager.otel_export_active
    return {"otel_export_active": telemetry_manager.otel_export_active}


@router.post("/llm-connections")
def create_llm_connection(payload: LLMConnectionCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    connection = LLMConnection(**payload.model_dump())
    db.add(connection)
    db.commit()
    db.refresh(connection)
    return {"id": connection.id, "name": connection.name}


@router.post("/agent-profiles", response_model=AgentProfileOut)
def create_agent_profile(payload: AgentProfileCreate, db: Session = Depends(get_db)) -> AgentProfile:
    profile = AgentProfile(**payload.model_dump())
    db.add(profile)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"Agent profile '{payload.name}' already exists")
    db.refresh(profile)
    return profile


@router.get("/agent-profiles")
def list_agent_profiles(db: Session = Depends(get_db)) -> list[AgentProfileOut]:
    profiles = db.query(AgentProfile).order_by(AgentProfile.created_at.desc()).all()
    return [AgentProfileOut.model_validate(profile) for profile in profiles]


@router.get("/agent-profiles/{profile_id}", response_model=AgentProfileOut)
def get_agent_profile(profile_id: str, db: Session = Depends(get_db)) -> AgentProfile:
    profile = db.query(AgentProfile).filter(AgentProfile.id == profile_id).one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Agent profile not found")
    return profile


@router.patch("/agent-profiles/{profile_id}", response_model=AgentProfileOut)
def update_agent_profile(
    profile_id: str,
    payload: AgentProfileUpdate,
    db: Session = Depends(get_db),
) -> AgentProfile:
    profile = db.query(AgentProfile).filter(AgentProfile.id == profile_id).one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Agent profile not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(profile, key, value)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Agent profile update conflicts with an existing name")
    db.refresh(profile)
    return profile


@router.post("/agent-profiles/{profile_id}/clone", response_model=AgentProfileOut)
def clone_agent_profile(profile_id: str, db: Session = Depends(get_db)) -> AgentProfile:
    source = db.query(AgentProfile).filter(AgentProfile.id == profile_id).one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Agent profile not found")
    clone = AgentProfile(
        name=f"Copy of {source.name}",
        role=source.role,
        guidelines=source.guidelines,
        output_style=source.output_style,
        model_name=source.model_name,
        temperature=source.temperature,
        max_iterations=source.max_iterations,
        telemetry_json=dict(source.telemetry_json or {}),
        ui_json=dict(source.ui_json or {}),
        llm_connection_id=source.llm_connection_id,
    )
    db.add(clone)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="A profile with that name already exists")
    db.refresh(clone)
    return clone


@router.delete("/agent-profiles/{profile_id}")
def delete_agent_profile(profile_id: str, db: Session = Depends(get_db)) -> dict[str, bool]:
    profile = db.query(AgentProfile).filter(AgentProfile.id == profile_id).one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Agent profile not found")
    thread_ids = [thread_id for (thread_id,) in db.query(Thread.id).filter(Thread.agent_profile_id == profile_id).all()]
    for thread_id in thread_ids:
        _delete_thread_records(db, thread_id)
        thread = db.query(Thread).filter(Thread.id == thread_id).one_or_none()
        if thread:
            db.delete(thread)
    db.delete(profile)
    db.commit()
    return {"ok": True}


@router.post("/agent-profiles/import-agent-md")
def import_agent_md(payload: AgentMdImportRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    parsed = parse_agent_md(payload.content)
    fm = parsed["frontmatter"]
    profile = AgentProfile(
        name=fm.get("name", "imported-agent"),
        role=parsed["sections"].get("Role", ""),
        guidelines=parsed["sections"].get("Guidelines", ""),
        output_style=parsed["sections"].get("Output Style", ""),
        model_name=fm.get("model", {}).get("name", "gpt-5-mini"),
        temperature=fm.get("model", {}).get("temperature", 0.2),
        max_iterations=fm.get("runtime", {}).get("max_iterations", 8),
        telemetry_json={
            "langsmith_project": fm.get("telemetry", {}).get("langsmith_project", "agent-playground"),
            "tags": fm.get("telemetry", {}).get("tags", []),
            "metadata": fm.get("telemetry", {}).get("metadata", {}),
            "otel_enabled": fm.get("otel", {}).get("enabled", True),
            "otel_service_name": fm.get("otel", {}).get("service_name", "agent-playground"),
        },
        imported_agent_md=payload.content,
    )
    db.add(profile)
    for server in fm.get("mcp_servers", []):
        key = server.get("key") or server.get("name", "mcp-server").lower().replace(" ", "_")
        existing = db.query(MCPServer).filter(MCPServer.name == key).one_or_none()
        if existing:
            continue
        db.add(
            MCPServer(
                name=key,
                server_url=server.get("server_url", ""),
                token_url=server.get("token_url", ""),
                grant_type=server.get("auth", {}).get("grant_type", "client_credentials"),
                client_id_encrypted=secret_box.encrypt(server.get("auth", {}).get("client_id_secret_ref", "")),
                client_secret_encrypted=secret_box.encrypt(server.get("auth", {}).get("client_secret_secret_ref", "")),
                scope=server.get("auth", {}).get("scope", ""),
                allowed_tools=server.get("allowed_tools", []),
                approval_mode=server.get("approval_mode", "prompt"),
                enabled=server.get("enabled", True),
            )
        )
    db.commit()
    db.refresh(profile)
    return {"profile": AgentProfileOut.model_validate(profile), "frontmatter": parsed["frontmatter"]}


@router.get("/agent-profiles/{profile_id}/export-agent-md", response_class=PlainTextResponse)
def export_agent_md_endpoint(profile_id: str, db: Session = Depends(get_db)) -> str:
    profile = db.query(AgentProfile).filter(AgentProfile.id == profile_id).one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Agent profile not found")
    servers = [serialize_mcp_server(server) for server in db.query(MCPServer).order_by(MCPServer.name)]
    return export_agent_md(profile.__dict__, servers)


@router.post("/mcp-servers", response_model=MCPServerOut)
def create_mcp_server(payload: MCPServerCreate, db: Session = Depends(get_db)) -> MCPServer:
    server = MCPServer(
        name=payload.name,
        server_url=payload.server_url,
        token_url=payload.token_url,
        grant_type=payload.grant_type,
        client_id_encrypted=secret_box.encrypt(payload.client_id),
        client_secret_encrypted=secret_box.encrypt(payload.client_secret),
        scope=payload.scope,
        allowed_tools=payload.allowed_tools,
        approval_mode=payload.approval_mode,
        headers=payload.headers,
        timeout_ms=payload.timeout_ms,
        enabled=payload.enabled,
    )
    db.add(server)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"MCP server '{payload.name}' already exists")
    db.refresh(server)
    return server


@router.get("/mcp-servers")
def list_mcp_servers(db: Session = Depends(get_db)) -> list[MCPServerOut]:
    servers = db.query(MCPServer).order_by(MCPServer.created_at.desc()).all()
    return [MCPServerOut.model_validate(server) for server in servers]


@router.get("/mcp-servers/{server_id}", response_model=MCPServerDetailOut)
def get_mcp_server(server_id: str, db: Session = Depends(get_db)) -> MCPServerDetailOut:
    server = db.query(MCPServer).filter(MCPServer.id == server_id).one_or_none()
    if not server:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return MCPServerDetailOut(
        **MCPServerOut.model_validate(server).model_dump(),
        client_id=secret_box.decrypt(server.client_id_encrypted),
        client_secret=secret_box.decrypt(server.client_secret_encrypted),
    )


@router.patch("/mcp-servers/{server_id}", response_model=MCPServerOut)
def update_mcp_server(server_id: str, payload: MCPServerUpdate, db: Session = Depends(get_db)) -> MCPServer:
    server = db.query(MCPServer).filter(MCPServer.id == server_id).one_or_none()
    if not server:
        raise HTTPException(status_code=404, detail="MCP server not found")
    data = payload.model_dump(exclude_unset=True)
    if "client_id" in data:
        server.client_id_encrypted = secret_box.encrypt(data.pop("client_id"))
    if "client_secret" in data:
        server.client_secret_encrypted = secret_box.encrypt(data.pop("client_secret"))
    for key, value in data.items():
        setattr(server, key, value)
    db.commit()
    db.refresh(server)
    return server


@router.post("/mcp-servers/{server_id}/clone", response_model=MCPServerOut)
def clone_mcp_server(server_id: str, db: Session = Depends(get_db)) -> MCPServer:
    source = db.query(MCPServer).filter(MCPServer.id == server_id).one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="MCP server not found")
    clone = MCPServer(
        name=f"Copy of {source.name}",
        server_url=source.server_url,
        token_url=source.token_url,
        grant_type=source.grant_type,
        client_id_encrypted=secret_box.encrypt(secret_box.decrypt(source.client_id_encrypted)),
        client_secret_encrypted=secret_box.encrypt(secret_box.decrypt(source.client_secret_encrypted)),
        scope=source.scope,
        allowed_tools=list(source.allowed_tools or []),
        approval_mode=source.approval_mode,
        headers=dict(source.headers or {}),
        timeout_ms=source.timeout_ms,
        enabled=source.enabled,
    )
    db.add(clone)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="An MCP server with that name already exists")
    db.refresh(clone)
    return clone


@router.delete("/mcp-servers/{server_id}")
def delete_mcp_server(server_id: str, db: Session = Depends(get_db)) -> dict[str, bool]:
    server = db.query(MCPServer).filter(MCPServer.id == server_id).one_or_none()
    if not server:
        raise HTTPException(status_code=404, detail="MCP server not found")
    db.execute(delete(ApprovalDecision).where(ApprovalDecision.mcp_server_id == server_id))
    db.delete(server)
    db.commit()
    return {"ok": True}


@router.post("/mcp-servers/test")
async def test_mcp_server_draft(payload: MCPServerTestRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    existing_server: MCPServer | None = None
    if payload.server_id:
        existing_server = db.query(MCPServer).filter(MCPServer.id == payload.server_id).one_or_none()
        if not existing_server:
            raise HTTPException(status_code=404, detail="Saved MCP server not found")

    client_id = payload.client_id or (
        secret_box.decrypt(existing_server.client_id_encrypted) if existing_server else ""
    )
    client_secret = payload.client_secret or (
        secret_box.decrypt(existing_server.client_secret_encrypted) if existing_server else ""
    )
    if not client_id or not client_secret:
        raise HTTPException(status_code=400, detail="Client ID and client secret are required to test this server")

    server = MCPServer(
        id="draft",
        name=payload.name,
        server_url=payload.server_url,
        token_url=payload.token_url,
        grant_type=payload.grant_type,
        client_id_encrypted=secret_box.encrypt(client_id),
        client_secret_encrypted=secret_box.encrypt(client_secret),
        scope=payload.scope,
        allowed_tools=payload.allowed_tools,
        approval_mode=payload.approval_mode,
        headers=payload.headers,
        timeout_ms=payload.timeout_ms,
        enabled=payload.enabled,
    )
    return await _test_server_config(server)


@router.post("/mcp-servers/{server_id}/test")
async def test_mcp_server(server_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    server = db.query(MCPServer).filter(MCPServer.id == server_id).one_or_none()
    if not server:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return await _test_server_config(server)


@router.post("/threads", response_model=ThreadOut)
def create_thread(payload: ThreadCreate, db: Session = Depends(get_db)) -> ThreadOut:
    thread = Thread(agent_profile_id=payload.agent_profile_id, title=payload.title)
    db.add(thread)
    db.commit()
    db.refresh(thread)
    return ThreadOut(id=thread.id, title=thread.title, agent_profile_id=thread.agent_profile_id, created_at=thread.created_at, updated_at=thread.updated_at, messages=[])


@router.get("/threads")
def list_threads(db: Session = Depends(get_db)) -> list[ThreadOut]:
    threads = db.query(Thread).order_by(Thread.updated_at.desc()).all()
    result = []
    for thread in threads:
        messages = list(db.query(Message).filter(Message.thread_id == thread.id).order_by(Message.created_at))
        result.append(
            ThreadOut(
                id=thread.id,
                title=thread.title,
                agent_profile_id=thread.agent_profile_id,
                created_at=thread.created_at,
                updated_at=thread.updated_at,
                messages=[MessageOut.model_validate(message) for message in messages],
            )
        )
    return result


@router.get("/threads/{thread_id}", response_model=ThreadOut)
def get_thread(thread_id: str, db: Session = Depends(get_db)) -> ThreadOut:
    thread = db.query(Thread).filter(Thread.id == thread_id).one_or_none()
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    messages = list(db.query(Message).filter(Message.thread_id == thread.id).order_by(Message.created_at))
    return ThreadOut(
        id=thread.id,
        title=thread.title,
        agent_profile_id=thread.agent_profile_id,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
        messages=[MessageOut.model_validate(message) for message in messages],
    )


@router.patch("/threads/{thread_id}", response_model=ThreadOut)
def update_thread(thread_id: str, payload: ThreadUpdate, db: Session = Depends(get_db)) -> ThreadOut:
    thread = db.query(Thread).filter(Thread.id == thread_id).one_or_none()
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    thread.title = payload.title.strip() or thread.title
    db.commit()
    db.refresh(thread)
    messages = list(db.query(Message).filter(Message.thread_id == thread.id).order_by(Message.created_at))
    return ThreadOut(id=thread.id, title=thread.title, agent_profile_id=thread.agent_profile_id, created_at=thread.created_at, updated_at=thread.updated_at, messages=[MessageOut.model_validate(m) for m in messages])


@router.delete("/threads/{thread_id}")
def delete_thread(thread_id: str, db: Session = Depends(get_db)) -> dict[str, bool]:
    thread = db.query(Thread).filter(Thread.id == thread_id).one_or_none()
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    _delete_thread_records(db, thread_id)
    db.delete(thread)
    db.commit()
    return {"ok": True}


@router.get("/threads/{thread_id}/runs")
def list_thread_runs(thread_id: str, db: Session = Depends(get_db)) -> list[dict]:
    thread = db.query(Thread).filter(Thread.id == thread_id).one_or_none()
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    runs = (
        db.query(AgentRun)
        .filter(AgentRun.thread_id == thread_id)
        .order_by(AgentRun.created_at.desc())
        .all()
    )
    return [{"id": run.id, "status": run.status, "created_at": run.created_at.isoformat()} for run in runs]


@router.post("/threads/{thread_id}/messages")
async def create_message_and_run(
    thread_id: str,
    payload: MessageCreate,
    db: Session = Depends(get_db),
) -> StreamingResponse:
    thread = db.query(Thread).filter(Thread.id == thread_id).one_or_none()
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    profile = db.query(AgentProfile).filter(AgentProfile.id == thread.agent_profile_id).one()
    user_message = Message(thread_id=thread.id, role="user", content=payload.content, metadata_json={})
    db.add(user_message)
    db.flush()
    run = AgentRun(
        thread_id=thread.id,
        agent_profile_id=profile.id,
        status="running",
        user_message_id=user_message.id,
        metadata_json={"thread_title": thread.title},
    )
    db.add(run)
    db.commit()
    return StreamingResponse(agent_runtime.stream_run(run.id), media_type="text/event-stream")


@router.post("/runs/{run_id}/approvals/{approval_id}")
def resolve_approval(
    run_id: str,
    approval_id: str,
    payload: ApprovalResolve,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    run = db.query(AgentRun).filter(AgentRun.id == run_id).one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    approval = (
        db.query(ApprovalDecision)
        .filter(ApprovalDecision.id == approval_id, ApprovalDecision.run_id == run_id)
        .one_or_none()
    )
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found")
    if approval.status != "pending":
        raise HTTPException(status_code=409, detail=f"Approval already resolved (status: {approval.status})")
    if run.status != "waiting_for_approval":
        raise HTTPException(status_code=409, detail=f"Run is not waiting for approval (status: {run.status})")
    approval.status = payload.status
    approval.rationale = payload.rationale
    if payload.status == "denied":
        run.status = "failed"
    db.commit()
    db.refresh(run)
    return {"run": {"id": run.id, "status": run.status}}


@router.post("/runs/{run_id}/resume")
def resume_run_stream(run_id: str, db: Session = Depends(get_db)) -> StreamingResponse:
    # Verify the run exists and all approvals are resolved before streaming.
    run = db.query(AgentRun).filter(AgentRun.id == run_id).one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    pending = (
        db.query(ApprovalDecision)
        .filter(ApprovalDecision.run_id == run_id, ApprovalDecision.status == "pending")
        .count()
    )
    if pending > 0:
        raise HTTPException(status_code=409, detail=f"Run has {pending} pending approval(s)")

    # Reconstruct the original react.run span as a non-recording parent so that
    # react.resume appears as a child in the same Jaeger trace.
    parent_otel_span = None
    original_span = (
        db.query(OtelSpan)
        .filter(OtelSpan.run_id == run_id, OtelSpan.name == "react.run")
        .order_by(OtelSpan.start_time_unix_nano)
        .first()
    )
    if original_span:
        try:
            span_ctx = SpanContext(
                trace_id=int(original_span.trace_id, 16),
                span_id=int(original_span.span_id, 16),
                is_remote=True,
                trace_flags=TraceFlags(TraceFlags.SAMPLED),
            )
            parent_otel_span = NonRecordingSpan(span_ctx)
        except (ValueError, TypeError):
            pass

    return StreamingResponse(
        agent_runtime.stream_run(run_id, root_span_name="react.resume", parent_otel_span=parent_otel_span),
        media_type="text/event-stream",
    )


@router.get("/runs/{run_id}/telemetry")
def get_run_telemetry(run_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    run = db.query(AgentRun).filter(AgentRun.id == run_id).one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    spans = list(db.query(OtelSpan).filter_by(run_id=run_id).order_by(OtelSpan.start_time_unix_nano))
    approvals = list(db.query(ApprovalDecision).filter_by(run_id=run_id).order_by(ApprovalDecision.created_at))
    return {
        "run": {
            "id": run.id,
            "thread_id": run.thread_id,
            "agent_profile_id": run.agent_profile_id,
            "status": run.status,
            "trace_id": run.trace_id,
            "metadata_json": run.metadata_json,
        },
        "spans": [OtelSpanOut.model_validate(span).model_dump() for span in spans],
        "approvals": [
            {
                "id": approval.id,
                "mcp_server_id": approval.mcp_server_id,
                "status": approval.status,
                "rationale": approval.rationale,
                "metadata_json": approval.metadata_json,
            }
            for approval in approvals
        ],
    }


def _otlp_value(v: Any) -> dict[str, Any]:
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": v}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, list):
        return {"arrayValue": {"values": [_otlp_value(i) for i in v]}}
    return {"stringValue": str(v)}


def _otlp_attrs(attrs: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"key": k, "value": _otlp_value(v)} for k, v in attrs.items()]


_SPAN_KIND_MAP = {"INTERNAL": 1, "SERVER": 2, "CLIENT": 3, "PRODUCER": 4, "CONSUMER": 5}
_STATUS_CODE_MAP = {"UNSET": 0, "OK": 1, "ERROR": 2}


@router.get("/runs/{run_id}/otel-export")
def export_run_otel(run_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    run = db.query(AgentRun).filter(AgentRun.id == run_id).one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    spans = list(db.query(OtelSpan).filter_by(run_id=run_id).order_by(OtelSpan.start_time_unix_nano))
    otlp_spans = []
    for span in spans:
        otlp_span: dict[str, Any] = {
            "traceId": span.trace_id,
            "spanId": span.span_id,
            "name": span.name,
            "kind": _SPAN_KIND_MAP.get(span.kind, 1),
            "startTimeUnixNano": str(span.start_time_unix_nano),
            "endTimeUnixNano": str(span.end_time_unix_nano),
            "attributes": _otlp_attrs(span.attributes),
            "events": [
                {
                    "timeUnixNano": str(e.get("time_unix_nano", 0)),
                    "name": e.get("name", ""),
                    "attributes": _otlp_attrs(e.get("attributes", {})),
                }
                for e in (span.events or [])
            ],
            "status": {"code": _STATUS_CODE_MAP.get(span.status_code, 0), "message": span.status_message},
        }
        if span.parent_span_id:
            otlp_span["parentSpanId"] = span.parent_span_id
        otlp_spans.append(otlp_span)

    resource_attrs = spans[0].resource_attributes if spans else {"service.name": "agent_playground"}
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": _otlp_attrs(resource_attrs)},
                "scopeSpans": [
                    {
                        "scope": {"name": "agent_playground"},
                        "spans": otlp_spans,
                    }
                ],
            }
        ]
    }
