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
  ui_json: {
    detailed_messages_enabled?: boolean;
    [key: string]: unknown;
  };
};

export type MCPServer = {
  id: string;
  name: string;
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

export type MCPServerDetail = MCPServer & {
  client_id: string;
  client_secret: string;
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

export type OtelSpan = {
  id: string;
  run_id: string | null;
  trace_id: string;
  span_id: string;
  parent_span_id: string | null;
  name: string;
  kind: string;
  start_time_unix_nano: number;
  end_time_unix_nano: number;
  duration_ms: number | null;
  status_code: string;
  status_message: string;
  attributes: Record<string, unknown>;
  events: Array<{
    name: string;
    time_unix_nano: number;
    attributes: Record<string, unknown>;
  }>;
  resource_attributes: Record<string, unknown>;
  created_at: string;
};

export type RunTelemetry = {
  run: {
    id: string;
    thread_id?: string;
    agent_profile_id?: string;
    status: string;
    trace_id: string;
    metadata_json?: Record<string, unknown>;
  };
  spans: OtelSpan[];
  approvals: Array<{
    id: string;
    mcp_server_id: string;
    status: string;
    rationale?: string | null;
    metadata_json: Record<string, unknown>;
  }>;
};

export type PendingApproval = {
  run_id: string;
  approval_id: string;
  mcp_server_id: string;
  metadata: Record<string, unknown>;
};
