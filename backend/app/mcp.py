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
    tool = {
        "type": "mcp",
        "server_label": server.name,
        "server_url": server.server_url,
        "authorization": f"Bearer {token}",
        "allowed_tools": server.allowed_tools,
        "require_approval": "always" if server.approval_mode == "prompt" else "never",
        "headers": server.headers or {},
    }
    return tool, token_meta


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

