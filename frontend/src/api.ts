import type { AgentProfile, MCPServer, RunTelemetry, Thread } from "./types";

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
  listProfiles: () => request<AgentProfile[]>("/agent-profiles"),
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
    request<string>(`/agent-profiles/${profileId}/export-agent-md`, { headers: {} }),
  listServers: () => request<MCPServer[]>("/mcp-servers"),
  createServer: (body: Record<string, unknown>) =>
    request<MCPServer>("/mcp-servers", { method: "POST", body: JSON.stringify(body) }),
  updateServer: (serverId: string, body: Record<string, unknown>) =>
    request<MCPServer>(`/mcp-servers/${serverId}`, { method: "PATCH", body: JSON.stringify(body) }),
  testDraftServer: (body: Record<string, unknown>) =>
    request<Record<string, unknown>>("/mcp-servers/test", { method: "POST", body: JSON.stringify(body) }),
  testServer: (serverId: string) => request<Record<string, unknown>>(`/mcp-servers/${serverId}/test`, { method: "POST" }),
  listThreads: () => request<Thread[]>("/threads"),
  getThread: (threadId: string) => request<Thread>(`/threads/${threadId}`),
  createThread: (body: Record<string, unknown>) =>
    request<Thread>("/threads", { method: "POST", body: JSON.stringify(body) }),
  deleteThread: (threadId: string) => request<{ ok: boolean }>(`/threads/${threadId}`, { method: "DELETE" }),
  getTelemetry: (runId: string) => request<RunTelemetry>(`/runs/${runId}/telemetry`),
  resolveApproval: (runId: string, approvalId: string, body: Record<string, unknown>) =>
    request<{ run: { id: string; status: string }; assistant_message?: Thread["messages"][number] }>(
      `/runs/${runId}/approvals/${approvalId}`,
      { method: "POST", body: JSON.stringify(body) },
    ),
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
        onEvent(eventName, JSON.parse(dataLine.slice(6)));
      }
    }
  },
};
