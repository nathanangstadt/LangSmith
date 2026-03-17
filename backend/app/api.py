from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agent_md import export_agent_md, parse_agent_md
from app.database import get_db
from app.mcp import build_openai_mcp_tool, serialize_mcp_server
from app.models import (
    AgentProfile,
    AgentRun,
    ApprovalDecision,
    LLMConnection,
    MCPServer,
    Message,
    RunStep,
    TelemetryEvent,
    Thread,
)
from app.runtime import agent_runtime
from app.schemas import (
    AgentMdImportRequest,
    AgentProfileCreate,
    AgentProfileOut,
    AgentProfileUpdate,
    AgentRunOut,
    ApprovalResolve,
    LLMConnectionCreate,
    MCPServerCreate,
    MCPServerOut,
    MCPServerUpdate,
    MessageCreate,
    MessageOut,
    RunResumeResponse,
    ThreadCreate,
    ThreadOut,
)
from app.security import secret_box


router = APIRouter()


@router.get("/health")
def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


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
        key = server.get("key") or server.get("label", "mcp-server").lower().replace(" ", "_")
        existing = db.query(MCPServer).filter(MCPServer.name == key).one_or_none()
        if existing:
            continue
        db.add(
            MCPServer(
                name=key,
                label=server.get("label", key),
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
        label=payload.label,
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


@router.post("/mcp-servers/{server_id}/test")
async def test_mcp_server(server_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    server = db.query(MCPServer).filter(MCPServer.id == server_id).one_or_none()
    if not server:
        raise HTTPException(status_code=404, detail="MCP server not found")
    tool, meta = await build_openai_mcp_tool(server)
    return {"ok": True, "tool": tool | {"authorization": "<redacted>"}, "token_meta": meta}


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
    db.refresh(run)
    auto_servers = list(db.query(MCPServer).filter(MCPServer.enabled.is_(True), MCPServer.approval_mode == "auto"))
    prompt_servers = list(
        db.query(MCPServer).filter(MCPServer.enabled.is_(True), MCPServer.approval_mode == "prompt")
    )
    generator = agent_runtime.stream_run(db, thread, profile, run, user_message, auto_servers, prompt_servers)
    return StreamingResponse(generator, media_type="text/event-stream")


@router.post("/runs/{run_id}/approvals/{approval_id}", response_model=RunResumeResponse)
async def resolve_approval(
    run_id: str,
    approval_id: str,
    payload: ApprovalResolve,
    db: Session = Depends(get_db),
) -> RunResumeResponse:
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
    approval.status = payload.status
    approval.rationale = payload.rationale
    if payload.status == "denied":
        run.status = "failed"
    db.commit()
    db.refresh(run)
    assistant_message = None
    if payload.status == "approved":
        pending = (
            db.query(ApprovalDecision)
            .filter(ApprovalDecision.run_id == run_id, ApprovalDecision.status == "pending")
            .count()
        )
        if pending == 0:
            assistant_message = await agent_runtime.resume_run(db, run)
            db.refresh(run)
    telemetry = list(db.query(TelemetryEvent).filter(TelemetryEvent.run_id == run.id).order_by(TelemetryEvent.created_at))
    return RunResumeResponse(
        run=AgentRunOut(
            id=run.id,
            thread_id=run.thread_id,
            agent_profile_id=run.agent_profile_id,
            status=run.status,
            user_message_id=run.user_message_id,
            assistant_message_id=run.assistant_message_id,
            trace_id=run.trace_id,
            langsmith_run_id=run.langsmith_run_id,
            otel_trace_id=run.otel_trace_id,
            metadata_json=run.metadata_json,
            created_at=run.created_at,
            updated_at=run.updated_at,
            steps=[],
            approvals=[],
            telemetry=[],
        ),
        assistant_message=MessageOut.model_validate(assistant_message) if assistant_message else None,
    )


@router.get("/runs/{run_id}/telemetry")
def get_run_telemetry(run_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    run = db.query(AgentRun).filter(AgentRun.id == run_id).one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    steps = list(db.query(RunStep).filter_by(run_id=run_id).order_by(RunStep.step_index))
    approvals = list(db.query(ApprovalDecision).filter_by(run_id=run_id).order_by(ApprovalDecision.created_at))
    telemetry = list(db.query(TelemetryEvent).filter_by(run_id=run_id).order_by(TelemetryEvent.created_at))
    return {
        "run": {
            "id": run.id,
            "thread_id": run.thread_id,
            "agent_profile_id": run.agent_profile_id,
            "status": run.status,
            "trace_id": run.trace_id,
            "langsmith_run_id": run.langsmith_run_id,
            "otel_trace_id": run.otel_trace_id,
            "metadata_json": run.metadata_json,
        },
        "steps": [
            {
                "id": step.id,
                "step_index": step.step_index,
                "kind": step.kind,
                "name": step.name,
                "status": step.status,
                "latency_ms": step.latency_ms,
                "token_usage": step.token_usage,
                "input_payload": step.input_payload,
                "output_payload": step.output_payload,
                "metadata_json": step.metadata_json,
                "span_id": step.span_id,
                "parent_span_id": step.parent_span_id,
                "langsmith_run_id": step.langsmith_run_id,
                "otel_span_id": step.otel_span_id,
                "created_at": step.created_at.isoformat(),
            }
            for step in steps
        ],
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
        "telemetry": [
            {
                "id": event.id,
                "event_type": event.event_type,
                "trace_id": event.trace_id,
                "span_id": event.span_id,
                "payload": event.payload,
                "created_at": event.created_at.isoformat(),
            }
            for event in telemetry
        ],
    }
