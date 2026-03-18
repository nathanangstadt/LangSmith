from typing import Any

import frontmatter
import yaml


SECTION_HEADERS = {
    "Role": "role",
    "Guidelines": "guidelines",
    "Output Style": "output_style",
}


def parse_agent_md(content: str) -> dict[str, Any]:
    post = frontmatter.loads(content)
    body = post.content
    sections: dict[str, str] = {}
    current_header = None
    current_lines: list[str] = []
    for line in body.splitlines():
        if line.startswith("# "):
            if current_header:
                sections[current_header] = "\n".join(current_lines).strip()
            current_header = line[2:].strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_header:
        sections[current_header] = "\n".join(current_lines).strip()
    return {
        "frontmatter": post.metadata,
        "sections": sections,
        "raw": content,
    }


def export_agent_md(profile: dict[str, Any], mcp_servers: list[dict[str, Any]]) -> str:
    frontmatter_dict = {
        "name": profile["name"],
        "version": 1,
        "model": {
            "provider": "openai",
            "name": profile["model_name"],
            "temperature": profile["temperature"],
        },
        "runtime": {
            "loop": "react",
            "max_iterations": profile["max_iterations"],
        },
        "telemetry": {
            "langsmith_project": profile["telemetry_json"].get("langsmith_project", "agent-playground"),
            "tags": profile["telemetry_json"].get("tags", []),
            "metadata": profile["telemetry_json"].get("metadata", {}),
        },
        "otel": {
            "enabled": profile["telemetry_json"].get("otel_enabled", True),
            "service_name": profile["telemetry_json"].get("otel_service_name", "agent-playground"),
        },
        "mcp_servers": [
            {
                "key": server["name"],
                "server_url": server["server_url"],
                "token_url": server["token_url"],
                "auth": {
                    "grant_type": "client_credentials",
                    "client_id_secret_ref": f"secret://mcp/{server['name']}/client_id",
                    "client_secret_secret_ref": f"secret://mcp/{server['name']}/client_secret",
                    "scope": server["scope"],
                },
                "allowed_tools": server["allowed_tools"],
                "approval_mode": server["approval_mode"],
                "enabled": server["enabled"],
            }
            for server in mcp_servers
        ],
    }

    parts = [
        "---",
        yaml.safe_dump(frontmatter_dict, sort_keys=False).strip(),
        "---",
        "",
        "# Role",
        profile.get("role", "").strip(),
        "",
        "# Guidelines",
        profile.get("guidelines", "").strip(),
        "",
        "# Output Style",
        profile.get("output_style", "").strip(),
        "",
    ]
    return "\n".join(parts)

