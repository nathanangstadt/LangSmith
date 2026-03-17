import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

from langsmith import traceable
from langsmith.wrappers import wrap_openai
from openai import OpenAI
from sqlalchemy.orm import Session

from app.config import get_settings
from app.mcp import build_openai_mcp_tool
from app.models import AgentProfile, AgentRun, ApprovalDecision, MCPServer, Message, RunStep, Thread
from app.telemetry import CanonicalSpan, telemetry_manager


settings = get_settings()


class RuntimeErrorResponse(Exception):
    pass


class AgentRuntime:
    def __init__(self) -> None:
        client = OpenAI(api_key=settings.openai_api_key) if settings.openai_api_key else None
        self.client = wrap_openai(client) if client else None

    async def request_approvals_if_needed(
        self,
        db: Session,
        run: AgentRun,
        prompt_servers: list[MCPServer],
    ) -> list[ApprovalDecision]:
        approvals: list[ApprovalDecision] = []
        for server in prompt_servers:
            existing = (
                db.query(ApprovalDecision)
                .filter(ApprovalDecision.run_id == run.id, ApprovalDecision.mcp_server_id == server.id)
                .one_or_none()
            )
            if existing:
                approvals.append(existing)
                continue
            approval = ApprovalDecision(
                run_id=run.id,
                mcp_server_id=server.id,
                status="pending",
                metadata_json={
                    "server_name": server.name,
                    "server_url": server.server_url,
                    "allowed_tools": server.allowed_tools,
                    "approval_mode": server.approval_mode,
                },
            )
            db.add(approval)
            approvals.append(approval)
        db.flush()
        return approvals

    def approvals_ready(self, approvals: list[ApprovalDecision]) -> bool:
        return bool(approvals) and all(approval.status == "approved" for approval in approvals)

    def approvals_denied(self, approvals: list[ApprovalDecision]) -> bool:
        return any(approval.status == "denied" for approval in approvals)

    def _prompt(self, profile: AgentProfile) -> str:
        parts = []
        if profile.role:
            parts.append(f"Role:\n{profile.role}")
        if profile.guidelines:
            parts.append(f"Guidelines:\n{profile.guidelines}")
        if profile.output_style:
            parts.append(f"Output Style:\n{profile.output_style}")
        parts.append(
            "Use tools when appropriate. Keep intermediate reasoning private and summarize tool-backed claims explicitly."
        )
        return "\n\n".join(parts)

    def _conversation_input(self, messages: list[Message]) -> list[dict[str, Any]]:
        input_items = []
        for message in messages:
            content_type = "output_text" if message.role == "assistant" else "input_text"
            input_items.append(
                {
                    "role": message.role,
                    "content": [{"type": content_type, "text": message.content}],
                }
            )
        return input_items

    @staticmethod
    def _supports_temperature(model_name: str) -> bool:
        return not model_name.startswith("gpt-5")

    @traceable(run_type="chain", name="agent_playground.react_run")
    async def _call_openai(
        self,
        *,
        model_name: str,
        instructions: str,
        temperature: float,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Any:
        if not self.client:
            raise RuntimeErrorResponse("OPENAI_API_KEY is not configured.")
        kwargs = {
            "model": model_name,
            "instructions": instructions,
            "input": input_items,
        }
        if self._supports_temperature(model_name):
            kwargs["temperature"] = temperature
        if tools:
            kwargs["tools"] = tools
        return await asyncio.to_thread(self.client.responses.create, **kwargs)

    async def _call_openai_with_mcp_fallback(
        self,
        *,
        db: Session,
        run: AgentRun,
        model_span: CanonicalSpan,
        model_name: str,
        instructions: str,
        temperature: float,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> tuple[Any, bool]:
        try:
            response = await self._call_openai(
                model_name=model_name,
                instructions=instructions,
                temperature=temperature,
                input_items=input_items,
                tools=tools,
            )
            return response, False
        except Exception as exc:
            message = str(exc)
            if tools and ("Error retrieving tool list from MCP server" in message or "external_connector_error" in message):
                telemetry_manager.record_event(
                    db,
                    run_id=run.id,
                    trace_id=run.trace_id,
                    span_id=model_span.span_id,
                    event_type="mcp.fallback",
                    payload={"reason": message, "skipped_servers": [tool.get("server_label") for tool in tools]},
                )
                response = await self._call_openai(
                    model_name=model_name,
                    instructions=instructions,
                    temperature=temperature,
                    input_items=input_items,
                    tools=[],
                )
                return response, True
            raise

    async def stream_run(
        self,
        db: Session,
        thread: Thread,
        profile: AgentProfile,
        run: AgentRun,
        user_message: Message,
        auto_servers: list[MCPServer],
        prompt_servers: list[MCPServer],
        ) -> AsyncGenerator[str, None]:
        run = db.merge(run)
        root_span = telemetry_manager.start_span(
            run,
            name="react.run",
            kind="run",
            attributes={"thread_id": thread.id, "agent_profile_id": profile.id},
            input_payload={"message_id": user_message.id, "content": user_message.content},
        )
        model_span: CanonicalSpan | None = None
        try:
            approvals = await self.request_approvals_if_needed(db, run, prompt_servers)
            if self.approvals_denied(approvals):
                run.status = "failed"
                step = telemetry_manager.end_span(
                    db,
                    run,
                    root_span,
                    step_index=0,
                    status="failed",
                    output_payload={"error": "An MCP approval was denied."},
                )
                db.commit()
                yield self._event("run.failed", {"run_id": run.id, "step_id": step.id, "error": "Approval denied"})
                return
            if approvals and not self.approvals_ready(approvals):
                run.status = "waiting_for_approval"
                for approval in approvals:
                    telemetry_manager.record_event(
                        db,
                        run_id=run.id,
                        trace_id=run.trace_id,
                        span_id=root_span.span_id,
                        event_type="approval.requested",
                        payload={
                            "approval_id": approval.id,
                            "server_name": approval.metadata_json.get("server_name"),
                            "server_url": approval.metadata_json.get("server_url"),
                            "allowed_tools": approval.metadata_json.get("allowed_tools"),
                        },
                    )
                    yield self._event(
                        "run.approval.requested",
                        {
                            "run_id": run.id,
                            "approval_id": approval.id,
                            "mcp_server_id": approval.mcp_server_id,
                            "metadata": approval.metadata_json,
                        },
                    )
                telemetry_manager.end_span(
                    db,
                    run,
                    root_span,
                    step_index=0,
                    status="waiting_for_approval",
                    output_payload={"status": "waiting_for_approval"},
                )
                db.commit()
                return

            messages = list(db.query(Message).filter(Message.thread_id == thread.id).order_by(Message.created_at))
            prepare_span = telemetry_manager.start_span(
                run,
                name="prepare.prompt",
                kind="prepare",
                parent_span_id=root_span.span_id,
                attributes={"message_count": len(messages)},
            )
            telemetry_manager.end_span(
                db,
                run,
                prepare_span,
                step_index=1,
                output_payload={"instructions": self._prompt(profile)},
            )
            yield self._event("run.step.completed", {"run_id": run.id, "kind": "prepare"})

            tools = []
            token_meta: list[dict[str, Any]] = []
            for server in [*auto_servers, *prompt_servers]:
                tool, meta = await build_openai_mcp_tool(server)
                tools.append(tool)
                token_meta.append({"server_name": server.name, **meta})

            model_span = telemetry_manager.start_span(
                run,
                name="model.call",
                kind="model",
                parent_span_id=root_span.span_id,
                attributes={"model": profile.model_name, "tool_count": len(tools)},
                input_payload={"tools": [tool["server_label"] for tool in tools]},
            )
            yield self._event("run.step.started", {"run_id": run.id, "kind": "model", "span_id": model_span.span_id})
            response, used_mcp_fallback = await self._call_openai_with_mcp_fallback(
                db=db,
                run=run,
                model_span=model_span,
                model_name=profile.model_name,
                instructions=self._prompt(profile),
                temperature=profile.temperature,
                input_items=self._conversation_input(messages),
                tools=tools,
            )
            output_text = getattr(response, "output_text", "") or ""
            usage = getattr(response, "usage", None)
            usage_payload = {
                "input_tokens": getattr(usage, "input_tokens", None) if usage else None,
                "output_tokens": getattr(usage, "output_tokens", None) if usage else None,
                "total_tokens": getattr(usage, "total_tokens", None) if usage else None,
            }
            response_id = getattr(response, "id", None)
            model_step = telemetry_manager.end_span(
                db,
                run,
                model_span,
                step_index=2,
                output_payload={
                    "response_id": response_id,
                    "output_text": output_text,
                    "token_meta": token_meta,
                    "used_mcp_fallback": used_mcp_fallback,
                },
                token_usage=usage_payload,
                metadata_json={"response_id": response_id, "used_mcp_fallback": used_mcp_fallback},
            )
            yield self._event(
                "run.step.completed",
                {"run_id": run.id, "kind": "model", "step_id": model_step.id, "usage": usage_payload},
            )

            assistant_message = Message(
                thread_id=thread.id,
                role="assistant",
                content=output_text or "(empty response)",
                metadata_json={"response_id": response_id},
            )
            db.add(assistant_message)
            db.flush()
            run.assistant_message_id = assistant_message.id
            run.status = "completed"

            final_span = telemetry_manager.start_span(
                run,
                name="final.answer",
                kind="final",
                parent_span_id=root_span.span_id,
            )
            final_step = telemetry_manager.end_span(
                db,
                run,
                final_span,
                step_index=3,
                output_payload={"assistant_message_id": assistant_message.id, "content": assistant_message.content},
            )
            telemetry_manager.end_span(
                db,
                run,
                root_span,
                step_index=4,
                output_payload={"status": "completed", "assistant_message_id": assistant_message.id},
            )
            db.commit()
            yield self._event(
                "message.delta",
                {"run_id": run.id, "message_id": assistant_message.id, "delta": assistant_message.content},
            )
            yield self._event(
                "run.completed",
                {
                    "run_id": run.id,
                    "assistant_message": {
                        "id": assistant_message.id,
                        "thread_id": assistant_message.thread_id,
                        "role": assistant_message.role,
                        "content": assistant_message.content,
                        "metadata_json": assistant_message.metadata_json,
                    },
                    "final_step_id": final_step.id,
                },
            )
        except Exception as exc:
            error_message = str(exc)
            run.status = "failed"
            failed_span = model_span or root_span
            step = telemetry_manager.end_span(
                db,
                run,
                failed_span,
                step_index=99,
                status="failed",
                output_payload={"error": error_message},
                metadata_json={"error_type": exc.__class__.__name__},
            )
            assistant_message = Message(
                thread_id=thread.id,
                role="assistant",
                content=f"Runtime error: {error_message}",
                metadata_json={"error": True},
            )
            db.add(assistant_message)
            db.flush()
            run.assistant_message_id = assistant_message.id
            telemetry_manager.record_event(
                db,
                run_id=run.id,
                trace_id=run.trace_id,
                span_id=root_span.span_id,
                event_type="run.failed",
                payload={"error": error_message},
            )
            db.commit()
            yield self._event(
                "run.failed",
                {
                    "run_id": run.id,
                    "step_id": step.id,
                    "error": error_message,
                    "assistant_message": {
                        "id": assistant_message.id,
                        "thread_id": assistant_message.thread_id,
                        "role": assistant_message.role,
                        "content": assistant_message.content,
                    },
                },
            )

    async def resume_run(self, db: Session, run: AgentRun) -> Message | None:
        thread = db.query(Thread).filter(Thread.id == run.thread_id).one()
        profile = db.query(AgentProfile).filter(AgentProfile.id == run.agent_profile_id).one()
        user_message = db.query(Message).filter(Message.id == run.user_message_id).one()
        auto_servers = list(db.query(MCPServer).filter(MCPServer.enabled.is_(True), MCPServer.approval_mode == "auto"))
        prompt_servers = list(
            db.query(MCPServer).filter(MCPServer.enabled.is_(True), MCPServer.approval_mode == "prompt")
        )
        async for _ in self.stream_run(db, thread, profile, run, user_message, auto_servers, prompt_servers):
            pass
        if run.assistant_message_id:
            return db.query(Message).filter(Message.id == run.assistant_message_id).one()
        return None

    @staticmethod
    def _event(name: str, payload: dict[str, Any]) -> str:
        return f"event: {name}\ndata: {json.dumps(payload)}\n\n"


agent_runtime = AgentRuntime()
