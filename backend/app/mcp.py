import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.models import MCPServer
from app.security import secret_box


class MCPTokenCache:
    def __init__(self) -> None:
        self._cache: dict[str, dict[str, Any]] = {}

    def get(self, server_id: str) -> str | None:
        entry = self._cache.get(server_id)
        if not entry:
            return None
        if entry["expires_at"] <= datetime.now(timezone.utc):
            self._cache.pop(server_id, None)
            return None
        return entry["token"]

    def set(self, server_id: str, token: str, expires_in: int) -> None:
        self._cache[server_id] = {
            "token": token,
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=max(expires_in - 30, 30)),
        }


token_cache = MCPTokenCache()


async def fetch_access_token(server: MCPServer) -> tuple[str, dict[str, Any]]:
    cached = token_cache.get(server.id)
    if cached:
        return cached, {"cache": "hit"}

    payload = {
        "grant_type": server.grant_type,
        "client_id": secret_box.decrypt(server.client_id_encrypted),
        "client_secret": secret_box.decrypt(server.client_secret_encrypted),
        "scope": server.scope,
    }
    timeout = httpx.Timeout(server.timeout_ms / 1000)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(server.token_url, data=payload, headers=server.headers or None)
        response.raise_for_status()
        data = response.json()
        token = data["access_token"]
        expires_in = int(data.get("expires_in", 3600))
        token_cache.set(server.id, token, expires_in)
        return token, {"cache": "miss", "expires_in": expires_in}


async def build_openai_mcp_tool(server: MCPServer) -> tuple[dict[str, Any], dict[str, Any]]:
    token, token_meta = await fetch_access_token(server)
    headers = dict(server.headers or {})
    headers["Authorization"] = f"Bearer {token}"
    tool = {
        "type": "mcp",
        "server_label": server.name,
        "server_url": server.server_url,
        "require_approval": "always" if server.approval_mode == "prompt" else "never",
        "headers": headers,
    }
    if server.allowed_tools:
        tool["allowed_tools"] = server.allowed_tools
    return tool, token_meta


def _session_header_name(headers: httpx.Headers) -> str | None:
    for key in headers.keys():
        if key.lower() == "mcp-session-id":
            return key
    return None


def _extract_json_payload(response: httpx.Response) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        return response.json()

    text = response.text.strip()
    if text.startswith("data:"):
        payload_lines = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        if payload_lines:
            return json.loads("".join(payload_lines))
    return response.json()


async def discover_mcp_tools(server: MCPServer) -> tuple[list[str], dict[str, Any]]:
    token, token_meta = await fetch_access_token(server)
    timeout = httpx.Timeout(server.timeout_ms / 1000)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **(server.headers or {}),
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        initialize_response = await client.post(
            server.server_url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": "initialize",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "agent-playground", "version": "0.1.0"},
                },
            },
        )
        initialize_response.raise_for_status()
        session_header = _session_header_name(initialize_response.headers)
        session_value = initialize_response.headers.get(session_header) if session_header else None
        session_headers = headers | ({session_header: session_value} if session_header and session_value else {})

        await client.post(
            server.server_url,
            headers=session_headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        )

        tools_response = await client.post(
            server.server_url,
            headers=session_headers,
            json={"jsonrpc": "2.0", "id": "tools-list", "method": "tools/list", "params": {}},
        )
        tools_response.raise_for_status()
        payload = _extract_json_payload(tools_response)
        tools = payload.get("result", {}).get("tools", [])
        tool_names = [tool.get("name", "unknown") for tool in tools if isinstance(tool, dict)]
        return tool_names, token_meta


def serialize_mcp_server(server: MCPServer) -> dict[str, Any]:
    return {
        "id": server.id,
        "name": server.name,
        "label": server.label,
        "server_url": server.server_url,
        "token_url": server.token_url,
        "scope": server.scope,
        "allowed_tools": server.allowed_tools,
        "approval_mode": server.approval_mode,
        "headers": server.headers,
        "timeout_ms": server.timeout_ms,
        "enabled": server.enabled,
    }


def prompt_mode_servers(db: Session) -> list[MCPServer]:
    return list(db.query(MCPServer).filter(MCPServer.enabled.is_(True), MCPServer.approval_mode == "prompt"))
