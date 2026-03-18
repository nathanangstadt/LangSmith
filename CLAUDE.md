# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Stack

```bash
# Full stack (recommended for development)
docker compose up --build

# Start individual services
docker compose up --build db backend frontend
```

Ports: frontend → `5174`, backend → `8001`, postgres → `5432` (internal: `db:5432`).

## Backend Commands

```bash
cd backend

# Run tests
pytest
pytest tests/test_security.py          # single file
pytest tests/test_mcp.py::test_name    # single test

# Dev server (outside Docker, requires local Postgres)
uvicorn app.main:app --reload --port 8001
```

Dependencies are in `requirements.txt`. There is no `pyproject.toml`.

## Frontend Commands

```bash
cd frontend
npm run dev      # dev server on :5174
npm run build    # TypeScript + Vite bundle to dist/
```

## Environment

Copy `.env.example` to `.env`. Required keys:
- `OPENAI_API_KEY` — agent execution
- `APP_ENCRYPTION_KEY` — Fernet key for MCP credential encryption (omitting it logs a warning and uses a hardcoded insecure fallback)

Optional export sinks (both default to off):
- `LANGSMITH_TRACING=true` + `LANGSMITH_API_KEY`
- `OTEL_EXPORTER_OTLP_ENDPOINT`

## Architecture

### Request → Response flow

```
POST /api/threads/{id}/messages
  → api.py: create Message (user) + AgentRun
  → runtime.AgentRuntime.stream_run()   ← StreamingResponse (SSE)
      ├─ check prompt-mode MCP approvals (emit run.approval.requested)
      ├─ build OpenAI tool definitions from enabled MCPServers
      ├─ _call_openai_with_mcp_fallback()
      │     └─ _call_openai_streaming()   ← wrap_openai + @traceable (LangSmith)
      │           emits: message.delta, run.detail.text, run.detail.item
      ├─ telemetry_manager.end_span()     ← persists RunStep to Postgres
      ├─ telemetry_manager._emit_otel()   ← OTLP export if configured
      └─ emits: run.completed / run.failed
```

### Backend modules (`backend/app/`)

| Module | Responsibility |
|---|---|
| `main.py` | FastAPI app, CORS, DB schema creation on startup |
| `api.py` | All route handlers; register static routes *before* parameterized ones |
| `runtime.py` | `AgentRuntime` — ReAct loop, OpenAI streaming, MCP fallback, SSE emission |
| `telemetry.py` | `TelemetryManager` — canonical span model, Postgres persistence, OTEL export |
| `models.py` | SQLAlchemy ORM (all timestamps use `datetime.now(timezone.utc)`) |
| `schemas.py` | Pydantic request/response shapes |
| `mcp.py` | OAuth2 token cache (in-process, single-worker only), tool discovery, token fetch |
| `security.py` | `SecretBox` — Fernet encryption for MCP credentials stored in Postgres |
| `agent_md.py` | `agent.md` YAML frontmatter + Markdown section parse/export |
| `database.py` | Engine, `get_db()` FastAPI dependency, `db_context()` context manager |
| `config.py` | `pydantic_settings.BaseSettings` — all env vars with defaults |

### Telemetry model

Local Postgres is the **source of truth** for the telemetry pane — the UI never reads back from LangSmith. The canonical span tree (`run_steps`, `telemetry_events`) uses OTEL concepts (`trace_id`, `span_id`, `parent_span_id`). LangSmith and OTLP are export sinks only.

### MCP approval flow

MCP servers have `approval_mode: prompt | auto`. Prompt-mode servers block execution: the backend emits `run.approval.requested` SSE, sets `run.status = waiting_for_approval`, and halts. The frontend surfaces the approval UI. Once `POST /api/runs/{id}/approvals/{approvalId}` is called, `resume_run()` replays `stream_run()`.

### SSE event vocabulary

`run.step.started` · `run.step.completed` · `message.delta` · `run.detail.input` · `run.detail.text` · `run.detail.item` · `run.approval.requested` · `run.approval.resolved` · `run.completed` · `run.failed`

### Frontend (`frontend/src/`)

The entire UI is one component: `App.tsx`. `api.ts` is the HTTP/SSE client. `types.ts` holds interfaces. There is no router — sections are toggled by state. The telemetry pane rebuilds from `persistedDetailedActivity` (from stored `run_steps`) merged with `liveDetailedActivity` (from live SSE events), deduplicated by item key.

### `agent.md` contract

YAML frontmatter → runtime settings; Markdown sections (`# Role`, `# Guidelines`, `# Output Style`) → ordered system prompt blocks. Secrets are never stored in `agent.md` — MCP credentials use `secret://` refs and stay encrypted in Postgres.
