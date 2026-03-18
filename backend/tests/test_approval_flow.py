"""
Tests for the MCP server prompt-approval flow in AgentRuntime.

Run inside Docker:
    docker compose exec backend pytest tests/test_approval_flow.py -v
"""

import asyncio
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import AgentProfile, AgentRun, ApprovalDecision, MCPServer, Message, Thread
from app.runtime import AgentRuntime


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture()
def fx(db):
    """Minimal set of model instances needed to call stream_run."""
    profile = AgentProfile(name="test-profile", model_name="gpt-4o-mini")
    db.add(profile)
    db.flush()

    thread = Thread(title="Test Thread", agent_profile_id=profile.id)
    db.add(thread)
    db.flush()

    user_msg = Message(thread_id=thread.id, role="user", content="list invoices")
    db.add(user_msg)
    db.flush()

    run = AgentRun(
        thread_id=thread.id,
        agent_profile_id=profile.id,
        status="queued",
        user_message_id=user_msg.id,
    )
    db.add(run)
    db.flush()

    prompt_server = MCPServer(
        name="invoice-server",
        server_url="https://mcp.example.com/v1",
        token_url="",
        client_id_encrypted="",
        client_secret_encrypted="",
        approval_mode="prompt",
        enabled=True,
    )
    db.add(prompt_server)
    db.flush()

    # Patch db_context so stream_run uses the in-memory test session instead of
    # opening a new connection to the production database.
    @contextmanager
    def _fake_db_context():
        yield db

    with patch("app.runtime.db_context", _fake_db_context):
        yield SimpleNamespace(
            profile=profile,
            thread=thread,
            user_msg=user_msg,
            run=run,
            prompt_server=prompt_server,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_stub_span(kind: str = "run") -> MagicMock:
    """
    Minimal ActiveSpan stub that satisfies stream_run's requirements.
    The span_id must be a hex string; _otel_span must support add_event
    and set_attribute.
    """
    otel_span = MagicMock()
    span = MagicMock()
    span.span_id = "abcd1234abcd1234"
    span.kind = kind
    span._otel_span = otel_span
    return span


def stub_start_span(*args, **kwargs) -> MagicMock:
    """Side-effect for telemetry_manager.start_span — always returns a fresh stub."""
    return make_stub_span(kind=kwargs.get("kind", "run"))


def parse_events(raw_events: list[str]) -> list[tuple[str, dict]]:
    """Parse raw SSE strings into (event_name, payload) pairs."""
    result = []
    for raw in raw_events:
        lines = [line for line in raw.strip().split("\n") if line]
        name_line = next((l for l in lines if l.startswith("event: ")), None)
        data_line = next((l for l in lines if l.startswith("data: ")), None)
        if name_line and data_line:
            result.append((name_line[7:], json.loads(data_line[6:])))
    return result


def collect_sse(coro) -> list[tuple[str, dict]]:
    """Drain an async generator and return parsed (name, payload) pairs."""
    async def _drain():
        raw = []
        async for chunk in coro:
            raw.append(chunk)
        return raw
    return parse_events(asyncio.run(_drain()))


# Fake MCP tool — returned whenever build_openai_mcp_tool is mocked.
FAKE_MCP_TOOL = ({"type": "mcp", "server_label": "invoice-server", "server_url": "https://mcp.example.com/v1"}, {})


# ---------------------------------------------------------------------------
# Unit tests: approval helper methods
# ---------------------------------------------------------------------------

def test_approvals_ready_false_when_pending():
    runtime = AgentRuntime.__new__(AgentRuntime)
    pending = MagicMock(status="pending")
    assert runtime.approvals_ready([pending]) is False


def test_approvals_ready_true_when_all_approved():
    runtime = AgentRuntime.__new__(AgentRuntime)
    approved = MagicMock(status="approved")
    assert runtime.approvals_ready([approved]) is True


def test_approvals_ready_false_for_empty_list():
    runtime = AgentRuntime.__new__(AgentRuntime)
    assert runtime.approvals_ready([]) is False


def test_approvals_denied_true_when_any_denied():
    runtime = AgentRuntime.__new__(AgentRuntime)
    approved = MagicMock(status="approved")
    denied = MagicMock(status="denied")
    assert runtime.approvals_denied([approved, denied]) is True


def test_approvals_denied_false_when_none_denied():
    runtime = AgentRuntime.__new__(AgentRuntime)
    pending = MagicMock(status="pending")
    approved = MagicMock(status="approved")
    assert runtime.approvals_denied([pending, approved]) is False


# ---------------------------------------------------------------------------
# Integration tests: stream_run approval gate
# ---------------------------------------------------------------------------

@patch("app.runtime.telemetry_manager")
def test_prompt_server_emits_approval_requested(mock_tm, fx, db):
    """stream_run with a prompt server that has no prior approval
    must emit run.approval.requested and halt without run.completed."""
    mock_tm.start_span.side_effect = stub_start_span

    runtime = AgentRuntime.__new__(AgentRuntime)
    events = collect_sse(runtime.stream_run(fx.run.id))

    names = [e[0] for e in events]
    assert "run.approval.requested" in names
    assert "run.completed" not in names
    assert "run.failed" not in names

    # Payload must carry the right IDs.
    payload = next(p for n, p in events if n == "run.approval.requested")
    assert payload["run_id"] == fx.run.id
    assert payload["mcp_server_id"] == fx.prompt_server.id

    # Run status must be updated in the DB.
    db.refresh(fx.run)
    assert fx.run.status == "waiting_for_approval"


@patch("app.runtime.telemetry_manager")
def test_approval_creation_is_idempotent(mock_tm, fx, db):
    """Calling stream_run twice for the same run/server must not duplicate
    ApprovalDecision rows."""
    mock_tm.start_span.side_effect = stub_start_span

    runtime = AgentRuntime.__new__(AgentRuntime)
    for _ in range(2):
        collect_sse(runtime.stream_run(fx.run.id))

    count = db.query(ApprovalDecision).filter_by(run_id=fx.run.id).count()
    assert count == 1


@patch("app.runtime.telemetry_manager")
def test_denied_approval_emits_run_failed(mock_tm, fx, db):
    """A pre-existing denied ApprovalDecision must cause run.failed without
    emitting run.approval.requested."""
    mock_tm.start_span.side_effect = stub_start_span

    db.add(ApprovalDecision(
        run_id=fx.run.id,
        mcp_server_id=fx.prompt_server.id,
        status="denied",
        metadata_json={},
    ))
    db.flush()

    runtime = AgentRuntime.__new__(AgentRuntime)
    events = collect_sse(runtime.stream_run(fx.run.id))

    names = [e[0] for e in events]
    assert "run.failed" in names
    assert "run.approval.requested" not in names

    db.refresh(fx.run)
    assert fx.run.status == "failed"


@patch("app.runtime.build_openai_mcp_tool", new_callable=AsyncMock, return_value=FAKE_MCP_TOOL)
@patch("app.runtime.telemetry_manager")
def test_approved_approval_bypasses_gate(mock_tm, _mock_tool, fx, db):
    """A pre-approved ApprovalDecision must bypass the halt and proceed to
    the OpenAI call (mocked to raise a sentinel so we don't need real creds)."""
    mock_tm.start_span.side_effect = stub_start_span

    db.add(ApprovalDecision(
        run_id=fx.run.id,
        mcp_server_id=fx.prompt_server.id,
        status="approved",
        metadata_json={},
    ))
    db.flush()

    runtime = AgentRuntime.__new__(AgentRuntime)

    async def raise_after_gate(*args, **kwargs):
        raise RuntimeError("SENTINEL: reached OpenAI call")

    with patch.object(runtime, "_call_openai_with_mcp_fallback", side_effect=raise_after_gate):
        events = collect_sse(runtime.stream_run(fx.run.id))

    names = [e[0] for e in events]
    # Gate bypassed — approval event must NOT be re-emitted.
    assert "run.approval.requested" not in names
    # Sentinel exception is caught → run.failed
    assert "run.failed" in names
    failed_payload = next(p for n, p in events if n == "run.failed")
    assert "SENTINEL" in failed_payload["error"]


@patch("app.runtime.build_openai_mcp_tool", new_callable=AsyncMock, return_value=FAKE_MCP_TOOL)
@patch("app.runtime.telemetry_manager")
def test_auto_server_skips_approval_gate(mock_tm, _mock_tool, fx, db):
    """An auto-mode server must not trigger any approval logic."""
    mock_tm.start_span.side_effect = stub_start_span

    # Change the server to auto mode so stream_run finds it in auto_servers.
    fx.prompt_server.approval_mode = "auto"
    db.flush()

    runtime = AgentRuntime.__new__(AgentRuntime)

    async def raise_sentinel(*args, **kwargs):
        raise RuntimeError("SENTINEL: reached OpenAI call")

    with patch.object(runtime, "_call_openai_with_mcp_fallback", side_effect=raise_sentinel):
        events = collect_sse(runtime.stream_run(fx.run.id))

    names = [e[0] for e in events]
    assert "run.approval.requested" not in names
    assert "run.failed" in names  # from sentinel


# ---------------------------------------------------------------------------
# Integration tests: approved server uses require_approval="never" and
# the resume path uses the react.resume span name
# ---------------------------------------------------------------------------

@patch("app.runtime.build_openai_mcp_tool", new_callable=AsyncMock, return_value=FAKE_MCP_TOOL)
@patch("app.runtime.telemetry_manager")
def test_approved_server_built_with_require_approval_never(mock_tm, mock_tool, fx, db):
    """After the user approves a prompt-mode server, stream_run must call
    build_openai_mcp_tool with require_approval='never' so the OpenAI Responses API
    does not re-prompt at the individual tool-call level."""
    mock_tm.start_span.side_effect = stub_start_span

    db.add(ApprovalDecision(
        run_id=fx.run.id,
        mcp_server_id=fx.prompt_server.id,
        status="approved",
        metadata_json={},
    ))
    db.flush()

    runtime = AgentRuntime.__new__(AgentRuntime)

    async def raise_sentinel(*args, **kwargs):
        raise RuntimeError("SENTINEL")

    with patch.object(runtime, "_call_openai_with_mcp_fallback", side_effect=raise_sentinel):
        collect_sse(runtime.stream_run(fx.run.id))

    mock_tool.assert_called_once()
    _, kwargs = mock_tool.call_args
    assert kwargs.get("require_approval") == "never", (
        "Approved prompt-mode server must be built with require_approval='never'; "
        f"got {kwargs.get('require_approval')!r}"
    )


@patch("app.runtime.build_openai_mcp_tool", new_callable=AsyncMock, return_value=FAKE_MCP_TOOL)
@patch("app.runtime.telemetry_manager")
def test_resume_uses_react_resume_span(mock_tm, _mock_tool, fx, db):
    """stream_run called with root_span_name='react.resume' must use that name
    for the root span, so the telemetry does not show a second 'react.run' node."""
    mock_tm.start_span.side_effect = stub_start_span

    db.add(ApprovalDecision(
        run_id=fx.run.id,
        mcp_server_id=fx.prompt_server.id,
        status="approved",
        metadata_json={},
    ))
    db.flush()

    runtime = AgentRuntime.__new__(AgentRuntime)

    async def raise_sentinel(*args, **kwargs):
        raise RuntimeError("SENTINEL")

    with patch.object(runtime, "_call_openai_with_mcp_fallback", side_effect=raise_sentinel):
        collect_sse(runtime.stream_run(fx.run.id, root_span_name="react.resume"))

    # First start_span call is the root span.
    first_call = mock_tm.start_span.call_args_list[0]
    span_name = first_call.kwargs.get("name") or first_call.args[1]
    assert span_name == "react.resume", (
        f"Expected root span name 'react.resume', got '{span_name}'."
    )
