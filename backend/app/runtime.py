import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from openai import OpenAI
from sqlalchemy.orm import Session

from app.config import get_settings
from app.mcp import build_openai_mcp_tool
from app.models import AgentProfile, AgentRun, ApprovalDecision, MCPServer, Message, Thread
from app.telemetry import ActiveSpan, telemetry_manager


settings = get_settings()


class RuntimeErrorResponse(Exception):
    pass


class AgentRuntime:
    def __init__(self) -> None:
        client = OpenAI(api_key=settings.openai_api_key) if settings.openai_api_key else None
        self.client = client

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
        now = datetime.now(timezone.utc)
        parts.append(f"System time: {now.strftime('%Y-%m-%dT%H:%M:%S.')}{now.microsecond // 1000:03d}Z")
        if profile.role:
            parts.append(f"Role:\n{profile.role}")
        if profile.guidelines:
            parts.append(f"Guidelines:\n{profile.guidelines}")
        if profile.output_style:
            parts.append(f"Output Style:\n{profile.output_style}")
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
        if event_type == "response.completed":
            return {
                **base_payload,
                "response": AgentRuntime._safe_model_dump(getattr(event, "response", {})),
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
        return not (model_name.startswith("gpt-5") or model_name.startswith("o"))

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
                        if getattr(event, "type", "") == "response.completed":
                            break  # all data captured; don't wait for response.done
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
                    break  # we have everything we need; don't wait for __done__
                await on_event(stream_event)
        finally:
            if task.done():
                await task  # propagate any exception from the producer
            else:
                # Stream context-manager cleanup is still running in the background thread.
                # Don't block waiting for it — let it finish on its own.
                task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
        if final_payload is None:
            raise RuntimeErrorResponse("OpenAI returned no final response.")
        return final_payload

    async def _call_openai_with_mcp_fallback(
        self,
        *,
        model_span: ActiveSpan,
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
                otel_span = getattr(model_span, "_otel_span", None)
                if otel_span:
                    otel_span.add_event("mcp.fallback", attributes={
                        "reason": message[:512],
                        "skipped_servers": json.dumps([tool.get("server_label") for tool in tools]),
                    })
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
        )
        model_span: ActiveSpan | None = None
        model_span_ended = False
        assistant_message: Message | None = None
        try:
            approvals = await self.request_approvals_if_needed(db, run, prompt_servers)
            if self.approvals_denied(approvals):
                run.status = "failed"
                root_span._otel_span.add_event("run.failed", attributes={"error": "An MCP approval was denied."})  # type: ignore[attr-defined]
                telemetry_manager.end_span(root_span, status="failed")
                db.commit()
                yield self._event("run.failed", {"run_id": run.id, "span_id": root_span.span_id, "error": "Approval denied"})
                return

            if approvals and not self.approvals_ready(approvals):
                run.status = "waiting_for_approval"
                for approval in approvals:
                    root_span._otel_span.add_event("approval.requested", attributes={  # type: ignore[attr-defined]
                        "approval_id": approval.id,
                        "server_name": str(approval.metadata_json.get("server_name", "")),
                        "server_url": str(approval.metadata_json.get("server_url", "")),
                    })
                    yield self._event(
                        "run.approval.requested",
                        {
                            "run_id": run.id,
                            "approval_id": approval.id,
                            "mcp_server_id": approval.mcp_server_id,
                            "metadata": approval.metadata_json,
                        },
                    )
                telemetry_manager.end_span(root_span, status="waiting_for_approval")
                db.commit()
                return

            messages = list(db.query(Message).filter(Message.thread_id == thread.id).order_by(Message.created_at))

            prepare_span = telemetry_manager.start_span(
                run,
                name="prepare.prompt",
                kind="prepare",
                parent_otel_span=root_span._otel_span,  # type: ignore[attr-defined]
                attributes={"message_count": len(messages)},
            )
            telemetry_manager.end_span(prepare_span)
            yield self._event("run.step.completed", {"run_id": run.id, "kind": "prepare"})

            tools = []
            for server in [*auto_servers, *prompt_servers]:
                tool, _ = await build_openai_mcp_tool(server)
                tools.append(tool)

            instructions = self._prompt(profile)
            input_items = self._conversation_input(messages)

            model_span = telemetry_manager.start_span(
                run,
                name="model.call",
                kind="model",
                parent_otel_span=root_span._otel_span,  # type: ignore[attr-defined]
                attributes={
                    "gen_ai.request.model": profile.model_name,
                    "gen_ai.request.temperature": profile.temperature,
                    "tool_count": len(tools),
                },
            )
            model_otel = model_span._otel_span  # type: ignore[attr-defined]

            # Record the full conversation as OTEL span events (gen_ai semantic conventions).
            model_otel.add_event("gen_ai.system.message", attributes={"gen_ai.prompt": instructions[:8192]})
            for item in input_items:
                role = item.get("role", "user")
                model_otel.add_event(
                    f"gen_ai.{role}.message",
                    attributes={"gen_ai.prompt": json.dumps(item)[:8192]},
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
                    "instructions": instructions,
                    "input_items": input_items,
                },
            )

            streamed_output_text = ""
            queued_events: asyncio.Queue[str] = asyncio.Queue()

            async def on_stream_event(stream_event: dict[str, Any]) -> None:
                nonlocal streamed_output_text
                event_type = str(stream_event.get("type", ""))

                # Record completed output items as OTEL span events.
                if event_type == "response.output_item.done":
                    item = stream_event.get("item") or {}
                    item_attrs: dict[str, str] = {
                        "item.type": str(item.get("type", "")),
                        "item.id": str(item.get("id", "")),
                        "content": json.dumps(item)[:65536],
                    }
                    # Store text separately so the frontend can recover it even if the
                    # full JSON is truncated (long invoice / report responses).
                    if item.get("type") == "message":
                        content_list = item.get("content", [])
                        if isinstance(content_list, list):
                            text = " ".join(
                                str(c.get("text", ""))
                                for c in content_list
                                if isinstance(c, dict) and c.get("type") == "output_text"
                            )
                            if text:
                                item_attrs["text"] = text[:65536]
                    model_otel.add_event("gen_ai.output_item.done", attributes=item_attrs)
                    await queued_events.put(
                        self._event(
                            "run.detail.item",
                            {"run_id": run.id, "kind": "model", **stream_event},
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
                    text = str(stream_event.get("text") or "")
                    if text:
                        streamed_output_text = text
                    if streamed_output_text:
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

            model_call = asyncio.create_task(
                self._call_openai_with_mcp_fallback(
                    model_span=model_span,
                    model_name=profile.model_name,
                    instructions=instructions,
                    temperature=profile.temperature,
                    input_items=input_items,
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

            # Set token usage as OTEL attributes and record the completion event.
            if usage_payload.get("input_tokens") is not None:
                model_otel.set_attribute("gen_ai.usage.input_tokens", int(usage_payload["input_tokens"]))
            if usage_payload.get("output_tokens") is not None:
                model_otel.set_attribute("gen_ai.usage.output_tokens", int(usage_payload["output_tokens"]))
            if usage_payload.get("total_tokens") is not None:
                model_otel.set_attribute("gen_ai.usage.total_tokens", int(usage_payload["total_tokens"]))
            if output_text:
                model_otel.add_event("gen_ai.choice", attributes={
                    "gen_ai.completion": output_text[:65536],
                    "finish_reason": "stop",
                    "used_mcp_fallback": used_mcp_fallback,
                })

            model_span_ended = True
            telemetry_manager.end_span(model_span)
            yield self._event("run.step.completed", {"run_id": run.id, "kind": "model", "span_id": model_span.span_id, "usage": usage_payload})

            assistant_message.content = output_text or "(empty response)"
            assistant_message.metadata_json = {"response_id": response_id}
            run.status = "completed"

            final_span = telemetry_manager.start_span(
                run,
                name="final.answer",
                kind="final",
                parent_otel_span=root_span._otel_span,  # type: ignore[attr-defined]
            )
            telemetry_manager.end_span(final_span)
            telemetry_manager.end_span(root_span)
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
                    "span_id": final_span.span_id,
                    "assistant_message": {
                        "id": assistant_message.id,
                        "thread_id": assistant_message.thread_id,
                        "role": assistant_message.role,
                        "content": assistant_message.content,
                        "metadata_json": assistant_message.metadata_json,
                    },
                },
            )
        except Exception as exc:
            error_message = str(exc)
            run.status = "failed"
            failed_span = (model_span if model_span and not model_span_ended else None) or root_span
            failed_otel = getattr(failed_span, "_otel_span", None)
            if failed_otel:
                failed_otel.add_event("run.failed", attributes={"error": error_message, "error_type": exc.__class__.__name__})
            telemetry_manager.end_span(failed_span, status="failed")
            if failed_span is not root_span:
                telemetry_manager.close_otel_span(root_span, status="failed")

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
            db.commit()
            yield self._event(
                "run.failed",
                {
                    "run_id": run.id,
                    "span_id": failed_span.span_id,
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
