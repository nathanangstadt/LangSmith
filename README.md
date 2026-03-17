# Agent Playground with LangSmith, MCP, and OTEL

Local playground for experimenting with an OpenAI-backed ReAct-style agent, LangSmith-compatible tracing, remote MCP servers, and OTLP export.

## What is included

- FastAPI backend with PostgreSQL persistence
- React/Vite frontend
- Agent profile editor with `agent.md` import/export
- MCP server management with client-credentials auth and encrypted secrets
- ReAct-style runtime with per-turn approvals for prompt-mode MCP servers
- Canonical local span model persisted to Postgres and exported to LangSmith and OTEL when configured

## Quick start

1. Copy `.env.example` to `.env`.
2. Set `OPENAI_API_KEY`.
3. Set `APP_ENCRYPTION_KEY` to a Fernet-compatible key.
4. Run `docker compose up --build`.
5. Open `http://localhost:5174`.

## Notes

- The runtime uses native remote MCP tool definitions for OpenAI requests.
- Prompt-mode MCP approvals are enforced per server per run before model execution.
- LangSmith and OTEL exports are optional sinks; the local persisted telemetry is the source of truth for the UI.
