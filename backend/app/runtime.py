import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any, Awaitable, Callable

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
    def _safe_model_dump(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, list):
            return [AgentRuntime._safe_model_dump(item) for item in value]
        if isinstance(value, tuple):
            return [AgentRuntime._safe_model_dump(item) for item in value]
        if isinstance(value, dict):
            return {str(key): AgentRuntime._safe_model_dump(item) for key, item in value.items()}
        if hasattr(value, "model_dump"):
            try:
                return AgentRuntime._safe_model_dump(value.model_dump(mode="json"))
            except TypeError:
                return AgentRuntime._safe_model_dump(value.model_dump())
            except Exception:
                return str(value)
        return str(value)

    @staticmethod
    def _serialize_stream_event(event: Any) -> dict[str, Any] | None:
        event_type = getattr(event, "type", "")
        base_payload = {
            "type": event_type,
            "sequence_number": getattr(event, "sequence_number", None),
            "output_index": getattr(event, "output_index", None),
        }
        if event_type in {"response.output_item.added", "response.output_item.done"}:
            return {
                **base_payload,
                "item": AgentRuntime._safe_model_dump(getattr(event, "item", None)),
            }
        if event_type == "response.output_text.delta":
            return {
                **base_payload,
                "item_id": getattr(event, "item_id", None),
                "content_index": getattr(event, "content_index", None),
                "delta": getattr(event, "delta", ""),
                "snapshot": getattr(event, "snapshot", ""),
            }
        if event_type == "response.output_text.done":
            return {
                **base_payload,
                "item_id": getattr(event, "item_id", None),
                "content_index": getattr(event, "content_index", None),
                "text": getattr(event, "text", ""),
            }
        return None

    @staticmethod
    def _response_text_from_payload(payload: dict[str, Any]) -> str:
        direct_text = payload.get("output_text")
        if isinstance(direct_text, str) and direct_text:
            return direct_text
        output = payload.get("output", [])
        if not isinstance(output, list):
            return ""
        parts: list[str] = []
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content", [])
            if not isinstance(content, list):
                continue
            for entry in content:
                if not isinstance(entry, dict):
                    continue
                text = entry.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
        return "\n".join(parts)

    @staticmethod
    def _response_usage_payload(payload: dict[str, Any]) -> dict[str, Any]:
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return {"input_tokens": None, "output_tokens": None, "total_tokens": None}
        return {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "total_tokens": usage.get("total_tokens"),
        }

    @staticmethod
    def _supports_temperature(model_name: str) -> bool:
        return not model_name.startswith("gpt-5")

    @traceable(run_type="chain", name="agent_playground.react_run")
    async def _call_openai_streaming(
        self,
        *,
        model_name: str,
        instructions: str,
        temperature: float,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_event: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> dict[str, Any]:
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

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def producer() -> None:
            try:
                with self.client.responses.stream(**kwargs) as stream:
                    for event in stream:
                        payload = self._serialize_stream_event(event)
                        if payload is not None:
                            loop.call_soon_threadsafe(queue.put_nowait, payload)
                    final_response = stream.get_final_response()
                    loop.call_soon_threadsafe(
                        queue.put_nowait,
                        {"type": "response.completed", "response": self._safe_model_dump(final_response)},
                    )
            except Exception as exc:
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    {"type": "response.error", "error": str(exc), "error_type": exc.__class__.__name__},
                )
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, {"type": "__done__"})

        task = asyncio.create_task(asyncio.to_thread(producer))
        final_payload: dict[str, Any] | None = None
        try:
            while True:
                stream_event = await queue.get()
                event_type = stream_event.get("type")
                if event_type == "__done__":
                    break
                if event_type == "response.error":
                    raise RuntimeErrorResponse(str(stream_event.get("error", "OpenAI streaming failed")))
                if event_type == "response.completed":
                    response = stream_event.get("response", {})
                    final_payload = response if isinstance(response, dict) else {}
                    continue
                await on_event(stream_event)
        finally:
            await task
        if final_payload is None:
            raise RuntimeErrorResponse("OpenAI returned no final response.")
        return final_payload

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
        on_event: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> tuple[dict[str, Any], bool]:
        try:
            response = await self._call_openai_streaming(
                model_name=model_name,
                instructions=instructions,
                temperature=temperature,
                input_items=input_items,
                tools=tools,
                on_event=on_event,
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
                response = await self._call_openai_streaming(
                    model_name=model_name,
                    instructions=instructions,
                    temperature=temperature,
                    input_items=input_items,
                    tools=[],
                    on_event=on_event,
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
        model_span_ended = False
        assistant_message: Message | None = None
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
                input_payload={
                    "instructions": self._prompt(profile),
                    "input_items": self._conversation_input(messages),
                    "tools": [tool["server_label"] for tool in tools],
                },
            )
            assistant_message = Message(
                thread_id=thread.id,
                role="assistant",
                content="",
                metadata_json={"streaming": True},
            )
            db.add(assistant_message)
            db.flush()
            run.assistant_message_id = assistant_message.id
            yield self._event("run.step.started", {"run_id": run.id, "kind": "model", "span_id": model_span.span_id})
            yield self._event(
                "run.detail.input",
                {
                    "run_id": run.id,
                    "kind": "model",
                    "instructions": self._prompt(profile),
                    "input_items": self._conversation_input(messages),
                },
            )
            streamed_output_text = ""
            queued_events: asyncio.Queue[str] = asyncio.Queue()

            async def on_stream_event(stream_event: dict[str, Any]) -> None:
                nonlocal streamed_output_text
                event_type = str(stream_event.get("type", ""))
                telemetry_manager.record_event(
                    db,
                    run_id=run.id,
                    trace_id=run.trace_id,
                    span_id=model_span.span_id,
                    event_type=event_type,
                    payload=stream_event,
                )
                if event_type in {"response.output_item.added", "response.output_item.done"}:
                    await queued_events.put(
                        self._event(
                            "run.detail.item",
                            {
                                "run_id": run.id,
                                "kind": "model",
                                **stream_event,
                            },
                        )
                    )
                    return
                if event_type == "response.output_text.delta":
                    snapshot = str(stream_event.get("snapshot") or "")
                    delta = str(stream_event.get("delta") or "")
                    streamed_output_text = snapshot or f"{streamed_output_text}{delta}"
                    await queued_events.put(
                        self._event(
                            "message.delta",
                            {
                                "run_id": run.id,
                                "message_id": assistant_message.id,
                                "delta": delta,
                                "snapshot": streamed_output_text,
                            },
                        )
                    )
                    await queued_events.put(
                        self._event(
                            "run.detail.text",
                            {
                                "run_id": run.id,
                                "kind": "model",
                                "item_id": stream_event.get("item_id"),
                                "snapshot": streamed_output_text,
                            },
                        )
                    )
                    return
                if event_type == "response.output_text.done":
                    streamed_output_text = str(stream_event.get("text") or streamed_output_text)

            model_call = asyncio.create_task(
                self._call_openai_with_mcp_fallback(
                    db=db,
                    run=run,
                    model_span=model_span,
                    model_name=profile.model_name,
                    instructions=self._prompt(profile),
                    temperature=profile.temperature,
                    input_items=self._conversation_input(messages),
                    tools=tools,
                    on_event=on_stream_event,
                )
            )
            while not model_call.done() or not queued_events.empty():
                if queued_events.empty():
                    await asyncio.sleep(0.02)
                    continue
                yield await queued_events.get()
            response, used_mcp_fallback = await model_call
            output_text = self._response_text_from_payload(response) or streamed_output_text
            usage_payload = self._response_usage_payload(response)
            response_id = response.get("id")
            response_output_items = response.get("output", [])
            if not isinstance(response_output_items, list):
                response_output_items = []
            model_span_ended = True
            model_step = telemetry_manager.end_span(
                db,
                run,
                model_span,
                step_index=2,
                output_payload={
                    "response_id": response_id,
                    "output_text": output_text,
                    "response_items": response_output_items,
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

            assistant_message.content = output_text or "(empty response)"
            assistant_message.metadata_json = {"response_id": response_id}
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
            if not streamed_output_text and assistant_message.content:
                yield self._event(
                    "message.delta",
                    {
                        "run_id": run.id,
                        "message_id": assistant_message.id,
                        "delta": assistant_message.content,
                        "snapshot": assistant_message.content,
                    },
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
            failed_span = (model_span if model_span and not model_span_ended else None) or root_span
            step = telemetry_manager.end_span(
                db,
                run,
                failed_span,
                step_index=99,
                status="failed",
                output_payload={"error": error_message},
                metadata_json={"error_type": exc.__class__.__name__},
            )
            if assistant_message is None:
                assistant_message = Message(
                    thread_id=thread.id,
                    role="assistant",
                    content=f"Runtime error: {error_message}",
                    metadata_json={"error": True},
                )
                db.add(assistant_message)
                db.flush()
            else:
                assistant_message.content = f"Runtime error: {error_message}"
                assistant_message.metadata_json = {"error": True}
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
