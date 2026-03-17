from app.agent_md import export_agent_md, parse_agent_md


def test_agent_md_roundtrip_contains_expected_sections() -> None:
    profile = {
        "name": "oracle-investigator",
        "role": "You are a careful enterprise support agent.",
        "guidelines": "- Prefer MCP tools over guessing.",
        "output_style": "Be concise.",
        "model_name": "gpt-5-mini",
        "temperature": 0.2,
        "max_iterations": 8,
        "telemetry_json": {
            "langsmith_project": "agent-playground",
            "tags": ["playground", "mcp"],
            "metadata": {"environment": "local"},
            "otel_enabled": True,
            "otel_service_name": "agent-playground",
        },
    }
    mcp_servers = [
        {
            "name": "oracle_docs",
            "label": "Oracle Docs MCP",
            "server_url": "https://example.com/mcp",
            "token_url": "https://example.com/oauth2/token",
            "scope": "scope",
            "allowed_tools": ["search_docs"],
            "approval_mode": "prompt",
            "enabled": True,
        }
    ]

    content = export_agent_md(profile, mcp_servers)
    parsed = parse_agent_md(content)

    assert parsed["frontmatter"]["name"] == "oracle-investigator"
    assert parsed["sections"]["Role"] == "You are a careful enterprise support agent."
    assert parsed["frontmatter"]["mcp_servers"][0]["key"] == "oracle_docs"

