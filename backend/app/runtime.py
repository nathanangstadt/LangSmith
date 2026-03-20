import asyncio
import json
import logging
import re
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from openai import OpenAI
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import db_context
from app.mcp import build_openai_mcp_tool
from app.models import AgentProfile, AgentRun, ApprovalDecision, MCPServer, Message, Thread
from app.telemetry import ActiveSpan, telemetry_manager


settings = get_settings()
logger = logging.getLogger(__name__)


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
        # Always parse the full output list — do not short-circuit on the SDK's
        # `output_text` convenience field, which only contains text from the
        # first message item and omits text produced after tool calls.
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
                        if settings.log_llm_traffic:
                            logger.debug("[LLM RAW] type=%s payload=%s", getattr(event, "type", "?"), repr(event)[:2048])
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
                    final_payload = stream_event.get("response") or {}
                    # Fix #2: continue draining until __done__ so the producer thread
                    # finishes before we return and mutate shared state (spans, messages).
                    continue
                await on_event(stream_event)
        finally:
            # Fix #2/#3: always cancel the task if it is still running (e.g. on
            # CancelledError or early exit), then await to suppress any residual exception.
            if not task.done():
                task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
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
        run_id: str,
        root_span_name: str = "gen_ai.agent.invoke",
        parent_otel_span: Any | None = None,
    ) -> AsyncGenerator[str, None]:
        with db_context() as db:
            run = db.query(AgentRun).filter(AgentRun.id == run_id).one()
            thread = db.query(Thread).filter(Thread.id == run.thread_id).one()
            profile = db.query(AgentProfile).filter(AgentProfile.id == run.agent_profile_id).one()
            auto_servers = list(db.query(MCPServer).filter(MCPServer.enabled.is_(True), MCPServer.approval_mode == "auto"))
            prompt_servers = list(db.query(MCPServer).filter(MCPServer.enabled.is_(True), MCPServer.approval_mode == "prompt"))
            root_span = telemetry_manager.start_span(
                run,
                name=root_span_name,
                kind="run",
                parent_otel_span=parent_otel_span,
                attributes={
                    "thread_id": thread.id,
                    "agent_profile_id": profile.id,
                    "gen_ai.agent.name": profile.name,
                    "gen_ai.thread.id": thread.id,
                    "gen_ai.run.id": run.id,
                },
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

                user_input = next((m.content for m in reversed(messages) if m.role == "user"), "")
                if user_input:
                    root_span._otel_span.set_attribute("input.value", user_input[:8192])  # type: ignore[attr-defined]

                prepare_span = telemetry_manager.start_span(
                    run,
                    name="prepare.prompt",
                    kind="prepare",
                    parent_otel_span=root_span._otel_span,  # type: ignore[attr-defined]
                    attributes={"message_count": len(messages)},
                )
                telemetry_manager.end_span(prepare_span)
                yield self._event("run.step.completed", {"run_id": run.id, "kind": "prepare"})

                # Servers the user approved in our UI must get require_approval="never"
                # so the OpenAI Responses API does not re-prompt at the tool-call level.
                approved_server_ids = {a.mcp_server_id for a in approvals if a.status == "approved"}
                tools = []
                for server in [*auto_servers, *prompt_servers]:
                    override = "never" if server.id in approved_server_ids else None
                    tool, _ = await build_openai_mcp_tool(server, require_approval=override)
                    tools.append(tool)

                instructions = self._prompt(profile)
                input_items = self._conversation_input(messages)

                # Build a label→server map for peer.service attribution on tool spans.
                server_by_label: dict[str, MCPServer] = {
                    re.sub(r"[^a-zA-Z0-9_-]", "_", s.name)[:64]: s
                    for s in [*auto_servers, *prompt_servers]
                }

                model_span = telemetry_manager.start_span(
                    run,
                    name="gen_ai.chat",
                    kind="model",
                    parent_otel_span=root_span._otel_span,  # type: ignore[attr-defined]
                    attributes={
                        "gen_ai.request.model": profile.model_name,
                        "gen_ai.request.temperature": profile.temperature,
                        "tool_count": len(tools),
                    },
                )
                model_otel = model_span._otel_span  # type: ignore[attr-defined]

                model_otel.set_attribute("input.value", json.dumps({
                    "system": instructions,
                    "messages": input_items,
                })[:8192])

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
                had_tool_calls = False
                active_tool_spans: dict[str, ActiveSpan] = {}
                queued_events: asyncio.Queue[str] = asyncio.Queue()

                async def on_stream_event(stream_event: dict[str, Any]) -> None:
                    nonlocal streamed_output_text, had_tool_calls
                    event_type = str(stream_event.get("type", ""))

                    # Pattern A: start a child span when a tool call item begins.
                    if event_type == "response.output_item.added":
                        item = stream_event.get("item") or {}
                        item_type = item.get("type", "")
                        item_id = str(item.get("id", ""))
                        if item_type in ("mcp_call", "function_call") and item_id:
                            had_tool_calls = True
                            server_label = str(item.get("server_label", ""))
                            tool_name = str(item.get("name", "unknown"))
                            call_id = str(item.get("call_id") or item.get("id", ""))
                            server = server_by_label.get(server_label)
                            tool_attrs: dict[str, Any] = {
                                "gen_ai.tool.name": tool_name,
                                "gen_ai.tool.call.id": call_id,
                                "tool.name": tool_name,
                                "tool.server": server_label,
                                "peer.service": server_label,
                            }
                            if server and server.server_url:
                                parsed = urlparse(server.server_url)
                                if parsed.hostname:
                                    tool_attrs["server.address"] = parsed.hostname
                                if parsed.port:
                                    tool_attrs["server.port"] = parsed.port
                            tool_span = telemetry_manager.start_span(
                                run,
                                name="gen_ai.tool.call",
                                kind="tool",
                                parent_otel_span=model_otel,
                                attributes=tool_attrs,
                            )
                            active_tool_spans[item_id] = tool_span
                        return

                    # Record completed output items; end tool.call child span if one is active.
                    if event_type == "response.output_item.done":
                        item = stream_event.get("item") or {}
                        item_type = item.get("type", "")
                        item_id = str(item.get("id", ""))

                        if item_id in active_tool_spans:
                            tool_span = active_tool_spans.pop(item_id)
                            tool_otel = tool_span._otel_span  # type: ignore[attr-defined]
                            tool_input = item.get("input") or item.get("arguments")
                            if tool_input:
                                raw = tool_input if isinstance(tool_input, str) else json.dumps(tool_input)
                                tool_otel.set_attribute("gen_ai.tool.args", raw[:8192])
                                tool_otel.set_attribute("input.value", raw[:8192])
                            tool_output = item.get("output")
                            if tool_output:
                                raw = tool_output if isinstance(tool_output, str) else json.dumps(tool_output)
                                tool_otel.set_attribute("gen_ai.tool.result", raw[:8192])
                                tool_otel.set_attribute("output.value", raw[:8192])
                            telemetry_manager.end_span(tool_span)

                        # Always record the output_item event on the parent span.
                        # Tool call items also have dedicated child spans (Pattern A)
                        # for OTEL consumers; the event here serves the frontend
                        # persistence layer (persistedDetailedActivity).
                        item_attrs: dict[str, str] = {
                            "item.type": str(item.get("type", "")),
                            "item.id": item_id,
                            "content": json.dumps(item)[:65536],
                        }
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
                try:
                    while not model_call.done() or not queued_events.empty():
                        if queued_events.empty():
                            await asyncio.sleep(0.02)
                            continue
                        yield await queued_events.get()
                    response, used_mcp_fallback = await model_call
                finally:
                    # Fix #3: cancel the background task on client disconnect (GeneratorExit)
                    # or any other early exit so the OpenAI HTTP connection is not orphaned.
                    if not model_call.done():
                        model_call.cancel()

                output_text = self._response_text_from_payload(response) or streamed_output_text
                # Always record the raw output items so we can compare against what
                # _response_text_from_payload extracted. Stored as a span event so it
                # lands in Postgres alongside the processed data.
                raw_output = response.get("output", [])
                model_otel.add_event("llm.raw_output", attributes={
                    "output_item_count": len(raw_output) if isinstance(raw_output, list) else 0,
                    "output_item_types": json.dumps([i.get("type") for i in raw_output if isinstance(i, dict)])[:1024],
                    "raw_output": json.dumps(raw_output)[:65536],
                })
                if settings.log_llm_traffic:
                    logger.debug("[LLM PARSED] extracted_text=%r output_item_count=%d item_types=%s",
                                 output_text[:500] if output_text else None,
                                 len(raw_output) if isinstance(raw_output, list) else 0,
                                 [i.get("type") for i in raw_output if isinstance(i, dict)])
                if output_text:
                    model_otel.set_attribute("output.value", output_text[:8192])
                    root_span._otel_span.set_attribute("output.value", output_text[:8192])  # type: ignore[attr-defined]
                usage_payload = self._response_usage_payload(response)
                response_id = response.get("id")
                response_model = response.get("model", profile.model_name)
                finish_reason = "tool_calls" if had_tool_calls else "stop"

                # Set token usage and response metadata as OTEL attributes.
                if usage_payload.get("input_tokens") is not None:
                    model_otel.set_attribute("gen_ai.usage.input_tokens", int(usage_payload["input_tokens"]))
                if usage_payload.get("output_tokens") is not None:
                    model_otel.set_attribute("gen_ai.usage.output_tokens", int(usage_payload["output_tokens"]))
                if usage_payload.get("total_tokens") is not None:
                    model_otel.set_attribute("gen_ai.usage.total_tokens", int(usage_payload["total_tokens"]))
                if response_id:
                    model_otel.set_attribute("gen_ai.response.id", response_id)
                model_otel.set_attribute("gen_ai.response.model", response_model)
                model_otel.set_attribute("gen_ai.response.finish_reasons", finish_reason)
                model_otel.add_event("gen_ai.choice", attributes={
                    "index": 0,
                    "finish_reason": finish_reason,
                    "message.role": "assistant",
                    "message.content": output_text[:65536] if output_text else "",
                    "gen_ai.completion": output_text[:65536] if output_text else "",
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
                    failed_otel.set_attribute("error.type", exc.__class__.__name__)
                    failed_otel.add_event("gen_ai.error", attributes={
                        "error.type": exc.__class__.__name__,
                        "error.message": error_message[:2048],
                    })
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

    @staticmethod
    def _event(name: str, payload: dict[str, Any]) -> str:
        return f"event: {name}\ndata: {json.dumps(payload)}\n\n"


agent_runtime = AgentRuntime()
