---
name: langsmith-agent-playground
version: 1
model:
  provider: openai
  name: gpt-4o
  temperature: 0.2
runtime:
  loop: react
  max_iterations: 8
telemetry:
  langsmith_project: agent-playground
  tags: [playground, local]
  metadata:
    environment: local
otel:
  enabled: true
  service_name: agent-playground
---

# How This Project Was Built

This document captures the process and principles used to build this project, so future projects can follow the same approach.

## Planning Before Building

Before any code was written, a structured implementation plan was produced covering:

- **Stack justification**: Every technology choice was tied to a concrete requirement. FastAPI was chosen for async SSE streaming and Python ecosystem fit. The OpenAI Responses API was chosen because it natively supports conversation state, custom tools, and streaming — a direct fit for a server-side ReAct loop. PostgreSQL was chosen for relational persistence of a span tree with foreign keys.
- **Explicit phasing**: The plan named every ReAct loop phase (`prepare`, `model`, `tool selection`, `tool execution`, `observation`, `final`), every Postgres table, and every API endpoint before implementation began. This prevented scope creep during development.
- **Constraint-first API design**: The SSE event vocabulary (`run.step.started`, `run.approval.requested`, etc.) was defined in the plan to lock the frontend/backend contract before either side was built.

## Architecture Principles

### Canonical Internal Model as Source of Truth

Telemetry was modeled once using OTEL concepts (`trace_id`, `span_id`, `parent_span_id`, `kind`, `attributes`, `events`) and persisted locally. LangSmith and OTLP were treated as **export sinks**, not primary storage. This meant:

- The UI telemetry pane is always driven by local data; it never reads back from LangSmith.
- Adding or removing an export target does not change the runtime model.
- OTEL compatibility was treated as a first-class constraint in v1, not a future deferral.

### Secrets Never in Portable Config

`agent.md` is explicitly non-secret. MCP server credentials (`client_id`, `client_secret`) are stored encrypted in Postgres and referenced via `secret://` URIs in `agent.md`. API keys stay in environment variables. This boundary was defined in the plan and enforced in the import/export logic.

### Extensibility Over Generality

Built-in tools were limited to a small, predictable set (`calculator`, `current_time`, `notes_lookup`) to keep telemetry testable. The registry was made extensible, but arbitrary code execution was deferred. This pattern — ship the minimum extensible surface, not the maximum configurable one — applies broadly.

### Port Conflicts Are Discovered at Start, Not End

Published ports were shifted from defaults (`5173`→`5174`, `8000`→`8001`) because the defaults were occupied. Environment conflicts should be surfaced and resolved during initial scaffold, not after full implementation.

## Verification Strategy

Verification was container-level, not host-level:

- `docker compose build` succeeded
- `docker compose up -d` brought all services healthy
- Backend startup log confirmed DB connection and readiness

Host-side HTTP checks were skipped because the sandbox could not reach `localhost`. The principle: use the verification scope that matches the environment you can actually observe. Do not block on checks you cannot run.

## Git Workflow

- The repo was bootstrapped as greenfield: `git init`, initial scaffold commit, then `origin` added and pushed.
- Implementation was committed as a single coherent unit after full scaffold verification.
- The `agent.md` file (this file) was not committed as secret material; it is the portable profile.

## `agent.md` as a Portable Contract

`agent.md` serves two roles:

1. **Runtime profile**: YAML frontmatter is parsed into structured settings (model, temperature, tools, telemetry, OTEL, MCP server references).
2. **System prompt source**: Markdown sections (`# Role`, `# Guidelines`, `# Output Style`, etc.) are assembled into ordered prompt blocks.

Import/export rules:
- Frontmatter fields override UI defaults on import.
- Unknown frontmatter keys are preserved on export for forward compatibility.
- Secret refs (`secret://`) are preserved; raw secrets are rejected on import.

## Approval Flow

MCP servers with `approval_mode: prompt` require an explicit approval decision per run before the model is invoked. The approval gate is:

1. Backend emits `run.approval.requested` SSE event.
2. UI blocks execution and surfaces the approval prompt.
3. User submits decision via `POST /api/runs/:id/approvals/:approvalId`.
4. Backend resumes the run.

This gate is enforced server-side. The frontend approval UI is a display of server state, not the source of truth.

## What to Carry Forward

| Principle | Application |
|---|---|
| Plan every table, endpoint, and event before writing code | Prevents mid-build contract changes |
| Canonical internal model + multiple export sinks | Apply to any observability or logging feature |
| Secrets never in portable config files | Apply to any project with shared or exportable config |
| Ship minimal extensible surface, defer max configurability | Apply to any plugin/tool registry |
| OTEL compatibility is v1, not v2 | Apply to any project that will eventually need distributed tracing |
| Verify at the scope you can observe | Do not write verification steps you cannot run in the target environment |
