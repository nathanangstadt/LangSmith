export type AgentProfile = {
  id: string;
  name: string;
  role: string;
  guidelines: string;
  output_style: string;
  model_name: string;
  temperature: number;
  max_iterations: number;
  telemetry_json: {
    langsmith_project?: string;
    tags?: string[];
    metadata?: Record<string, unknown>;
    otel_enabled?: boolean;
    otel_service_name?: string;
  };
  ui_json: Record<string, unknown>;
};

export type MCPServer = {
  id: string;
  name: string;
  label: string;
  server_url: string;
  token_url: string;
  grant_type: string;
  scope: string;
  allowed_tools: string[];
  approval_mode: "prompt" | "auto";
  headers: Record<string, string>;
  timeout_ms: number;
  enabled: boolean;
};

export type Message = {
  id: string;
  thread_id: string;
  role: "user" | "assistant";
  content: string;
  metadata_json: Record<string, unknown>;
  created_at: string;
};

export type Thread = {
  id: string;
  title: string;
  agent_profile_id: string;
  created_at: string;
  updated_at: string;
  messages: Message[];
};

export type RunTelemetry = {
  run: {
    id: string;
    status: string;
    trace_id: string;
    langsmith_run_id?: string | null;
    otel_trace_id?: string | null;
  };
  steps: Array<{
    id: string;
    step_index: number;
    kind: string;
    name: string;
    status: string;
    latency_ms?: number | null;
    token_usage: Record<string, unknown>;
    input_payload: Record<string, unknown>;
    output_payload: Record<string, unknown>;
    metadata_json: Record<string, unknown>;
    span_id: string;
    parent_span_id?: string | null;
    langsmith_run_id?: string | null;
    otel_span_id?: string | null;
    created_at: string;
  }>;
  approvals: Array<{
    id: string;
    mcp_server_id: string;
    status: string;
    rationale?: string | null;
    metadata_json: Record<string, unknown>;
  }>;
  telemetry: Array<{
    id: string;
    event_type: string;
    trace_id: string;
    span_id: string;
    payload: Record<string, unknown>;
    created_at: string;
  }>;
};

export type PendingApproval = {
  run_id: string;
  approval_id: string;
  mcp_server_id: string;
  metadata: Record<string, unknown>;
};

