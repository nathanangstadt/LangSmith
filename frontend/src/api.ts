import type { AgentProfile, MCPServer, MCPServerDetail, RunTelemetry, Thread } from "./types";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8001";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}/api${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    return response.json() as Promise<T>;
  }
  return (response.text() as unknown) as T;
}

export const api = {
  getConfig: () => request<{ langsmith_enabled: boolean; langsmith_project: string; otel_enabled: boolean; otel_endpoint: string; otel_export_active: boolean; jaeger_ui_url: string; openai_configured: boolean }>("/config"),
  toggleOtelExport: () => request<{ otel_export_active: boolean }>("/otel/toggle", { method: "POST" }),
  listProfiles: () => request<AgentProfile[]>("/agent-profiles"),
  cloneProfile: (profileId: string) => request<AgentProfile>(`/agent-profiles/${profileId}/clone`, { method: "POST" }),
  deleteProfile: (profileId: string) => request<{ ok: boolean }>(`/agent-profiles/${profileId}`, { method: "DELETE" }),
  createProfile: (body: Record<string, unknown>) =>
    request<AgentProfile>("/agent-profiles", { method: "POST", body: JSON.stringify(body) }),
  updateProfile: (profileId: string, body: Record<string, unknown>) =>
    request<AgentProfile>(`/agent-profiles/${profileId}`, { method: "PATCH", body: JSON.stringify(body) }),
  importAgentMd: (content: string) =>
    request<{ profile: AgentProfile; frontmatter: Record<string, unknown> }>("/agent-profiles/import-agent-md", {
      method: "POST",
      body: JSON.stringify({ content }),
    }),
  exportAgentMd: (profileId: string) =>
    request<string>(`/agent-profiles/${profileId}/export-agent-md`),
  listServers: () => request<MCPServer[]>("/mcp-servers"),
  getServer: (serverId: string) => request<MCPServerDetail>(`/mcp-servers/${serverId}`),
  cloneServer: (serverId: string) => request<MCPServer>(`/mcp-servers/${serverId}/clone`, { method: "POST" }),
  deleteServer: (serverId: string) => request<{ ok: boolean }>(`/mcp-servers/${serverId}`, { method: "DELETE" }),
  createServer: (body: Record<string, unknown>) =>
    request<MCPServer>("/mcp-servers", { method: "POST", body: JSON.stringify(body) }),
  updateServer: (serverId: string, body: Record<string, unknown>) =>
    request<MCPServer>(`/mcp-servers/${serverId}`, { method: "PATCH", body: JSON.stringify(body) }),
  testDraftServer: (body: Record<string, unknown>) =>
    request<Record<string, unknown>>("/mcp-servers/test", { method: "POST", body: JSON.stringify(body) }),
  testServer: (serverId: string) => request<Record<string, unknown>>(`/mcp-servers/${serverId}/test`, { method: "POST" }),
  listThreads: () => request<Thread[]>("/threads"),
  getThread: (threadId: string) => request<Thread>(`/threads/${threadId}`),
  listRuns: (threadId: string) => request<{ id: string; status: string; created_at: string }[]>(`/threads/${threadId}/runs`),
  createThread: (body: Record<string, unknown>) =>
    request<Thread>("/threads", { method: "POST", body: JSON.stringify(body) }),
  updateThread: (threadId: string, body: { title: string }) =>
    request<Thread>(`/threads/${threadId}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteThread: (threadId: string) => request<{ ok: boolean }>(`/threads/${threadId}`, { method: "DELETE" }),
  getTelemetry: (runId: string) => request<RunTelemetry>(`/runs/${runId}/telemetry`),
  downloadOtelExport: async (runId: string) => {
    const data = await request<Record<string, unknown>>(`/runs/${runId}/otel-export`);
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `otel-export-${runId}.json`;
    a.click();
    URL.revokeObjectURL(url);
  },
  resolveApproval: (runId: string, approvalId: string, body: Record<string, unknown>) =>
    request<{ run: { id: string; status: string } }>(
      `/runs/${runId}/approvals/${approvalId}`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  async streamResumedRun(
    runId: string,
    onEvent: (eventName: string, payload: Record<string, unknown>) => void,
  ) {
    const response = await fetch(`${API_BASE_URL}/api/runs/${runId}/resume`, { method: "POST" });
    if (!response.ok || !response.body) {
      throw new Error(await response.text());
    }
    const decoder = new TextDecoder();
    const reader = response.body.getReader();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const chunks = buffer.split("\n\n");
      buffer = chunks.pop() ?? "";
      for (const chunk of chunks) {
        const lines = chunk.split("\n");
        const eventLine = lines.find((line) => line.startsWith("event: "));
        const dataLine = lines.find((line) => line.startsWith("data: "));
        if (!eventLine || !dataLine) continue;
        const eventName = eventLine.slice(7);
        try {
          onEvent(eventName, JSON.parse(dataLine.slice(6)));
        } catch {
          console.warn("SSE: skipped malformed data line", dataLine);
        }
      }
    }
  },
  async streamMessage(
    threadId: string,
    content: string,
    onEvent: (eventName: string, payload: Record<string, unknown>) => void,
  ) {
    const response = await fetch(`${API_BASE_URL}/api/threads/${threadId}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    });
    if (!response.ok || !response.body) {
      throw new Error(await response.text());
    }

    const decoder = new TextDecoder();
    const reader = response.body.getReader();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const chunks = buffer.split("\n\n");
      buffer = chunks.pop() ?? "";
      for (const chunk of chunks) {
        const lines = chunk.split("\n");
        const eventLine = lines.find((line) => line.startsWith("event: "));
        const dataLine = lines.find((line) => line.startsWith("data: "));
        if (!eventLine || !dataLine) continue;
        const eventName = eventLine.slice(7);
        try {
          onEvent(eventName, JSON.parse(dataLine.slice(6)));
        } catch {
          console.warn("SSE: skipped malformed data line", dataLine);
        }
      }
    }
  },
};
