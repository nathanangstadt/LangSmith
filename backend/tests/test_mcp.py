import asyncio
from types import SimpleNamespace

from app import mcp


def test_build_openai_mcp_tool_omits_empty_optional_fields(monkeypatch) -> None:
    async def fake_fetch_access_token(server: object) -> tuple[str, dict[str, str]]:
        return "token-123", {"cache": "miss"}

    monkeypatch.setattr(mcp, "fetch_access_token", fake_fetch_access_token)

    server = SimpleNamespace(
        id="server-1",
        name="oracle_docs",
        server_url="https://example.com/mcp",
        approval_mode="auto",
        allowed_tools=[],
        headers={},
    )

    tool, token_meta = asyncio.run(mcp.build_openai_mcp_tool(server))

    assert tool == {
        "type": "mcp",
        "server_label": "oracle_docs",
        "server_url": "https://example.com/mcp",
        "require_approval": "never",
        "headers": {"Authorization": "Bearer token-123"},
    }
    assert token_meta == {"cache": "miss"}


def test_build_openai_mcp_tool_includes_optional_fields_when_present(monkeypatch) -> None:
    async def fake_fetch_access_token(server: object) -> tuple[str, dict[str, str]]:
        return "token-123", {"cache": "hit"}

    monkeypatch.setattr(mcp, "fetch_access_token", fake_fetch_access_token)

    server = SimpleNamespace(
        id="server-2",
        name="oracle_docs",
        server_url="https://example.com/mcp",
        approval_mode="prompt",
        allowed_tools=["SEARCH_DOCS", "GET_DOC"],
        headers={"x-tenant": "acme"},
    )

    tool, token_meta = asyncio.run(mcp.build_openai_mcp_tool(server))

    assert tool == {
        "type": "mcp",
        "server_label": "oracle_docs",
        "server_url": "https://example.com/mcp",
        "allowed_tools": ["SEARCH_DOCS", "GET_DOC"],
        "require_approval": "always",
        "headers": {"x-tenant": "acme", "Authorization": "Bearer token-123"},
    }
    assert token_meta == {"cache": "hit"}


def test_build_openai_mcp_tool_require_approval_override(monkeypatch) -> None:
    """Passing require_approval="never" must override a prompt-mode server's default
    of "always". This is used after the user approves the server in the playground UI
    so the OpenAI Responses API does not re-prompt at the individual tool-call level."""
    async def fake_fetch_access_token(server: object) -> tuple[str, dict[str, str]]:
        return "token-xyz", {"cache": "miss"}

    monkeypatch.setattr(mcp, "fetch_access_token", fake_fetch_access_token)

    server = SimpleNamespace(
        id="server-3",
        name="invoice_server",
        server_url="https://mcp.example.com/v1",
        approval_mode="prompt",   # would normally produce require_approval="always"
        allowed_tools=[],
        headers={},
    )

    tool, _ = asyncio.run(mcp.build_openai_mcp_tool(server, require_approval="never"))

    assert tool["require_approval"] == "never"
