import { ChangeEvent, useEffect, useState } from "react";

import { api } from "./api";
import type { AgentProfile, MCPServer, PendingApproval, RunTelemetry, Thread } from "./types";

const defaultProfileForm = {
  name: "oracle-investigator",
  role: "You are a careful enterprise support agent.",
  guidelines: "Prefer MCP tools over guessing.\nState when a claim comes from MCP observations.",
  output_style: "Be concise and explicit about assumptions.",
  model_name: "gpt-5-mini",
  temperature: 0.2,
  max_iterations: 8,
  telemetry_json: {
    langsmith_project: "agent-playground",
    tags: ["playground", "mcp"],
    metadata: { environment: "local" },
    otel_enabled: true,
    otel_service_name: "agent-playground",
  },
  ui_json: {},
};

const defaultMcpForm = {
  name: "oracle_docs",
  label: "Oracle Docs MCP",
  server_url: "",
  token_url: "",
  grant_type: "client_credentials",
  client_id: "",
  client_secret: "",
  scope: "",
  allowed_tools: "search_docs,get_doc",
  approval_mode: "prompt",
  headers: "{}",
  timeout_ms: 20000,
  enabled: true,
};

export default function App() {
  const [profiles, setProfiles] = useState<AgentProfile[]>([]);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [servers, setServers] = useState<MCPServer[]>([]);
  const [selectedProfileId, setSelectedProfileId] = useState<string>("");
  const [selectedThreadId, setSelectedThreadId] = useState<string>("");
  const [profileForm, setProfileForm] = useState(defaultProfileForm);
  const [mcpForm, setMcpForm] = useState(defaultMcpForm);
  const [chatInput, setChatInput] = useState("");
  const [telemetry, setTelemetry] = useState<RunTelemetry | null>(null);
  const [activeRunId, setActiveRunId] = useState<string>("");
  const [pendingApprovals, setPendingApprovals] = useState<PendingApproval[]>([]);
  const [agentMd, setAgentMd] = useState("");
  const [statusLine, setStatusLine] = useState("Idle");

  useEffect(() => {
    void refreshAll();
  }, []);

  const refreshAll = async () => {
    const [profileData, threadData, serverData] = await Promise.all([
      api.listProfiles(),
      api.listThreads(),
      api.listServers(),
    ]);
    setProfiles(profileData);
    setThreads(threadData);
    setServers(serverData);
    if (!selectedProfileId && profileData[0]) setSelectedProfileId(profileData[0].id);
    if (!selectedThreadId && threadData[0]) setSelectedThreadId(threadData[0].id);
  };

  const refreshTelemetry = async (runId: string) => {
    const data = await api.getTelemetry(runId);
    setTelemetry(data);
  };

  const selectedThread = threads.find((thread) => thread.id === selectedThreadId);

  const onProfileField = (key: string, value: string | number) => {
    setProfileForm((current) => ({ ...current, [key]: value }));
  };

  const onCreateProfile = async () => {
    const created = await api.createProfile(profileForm);
    setSelectedProfileId(created.id);
    setProfileForm(defaultProfileForm);
    await refreshAll();
  };

  const onCreateThread = async () => {
    if (!selectedProfileId) return;
    const thread = await api.createThread({
      agent_profile_id: selectedProfileId,
      title: `Thread ${new Date().toLocaleTimeString()}`,
    });
    setSelectedThreadId(thread.id);
    await refreshAll();
  };

  const onMcpField = (key: string, value: string | number | boolean) => {
    setMcpForm((current) => ({ ...current, [key]: value }));
  };

  const onCreateServer = async () => {
    await api.createServer({
      ...mcpForm,
      allowed_tools: mcpForm.allowed_tools
        .split(",")
        .map((tool) => tool.trim())
        .filter(Boolean),
      headers: JSON.parse(mcpForm.headers),
    });
    setMcpForm(defaultMcpForm);
    await refreshAll();
  };

  const onImportAgentMd = async () => {
    if (!agentMd.trim()) return;
    const result = await api.importAgentMd(agentMd);
    setSelectedProfileId(result.profile.id);
    setStatusLine(`Imported agent.md into profile ${result.profile.name}`);
    await refreshAll();
  };

  const onExportAgentMd = async () => {
    if (!selectedProfileId) return;
    const content = await api.exportAgentMd(selectedProfileId);
    setAgentMd(content);
    setStatusLine("Exported agent.md for the selected profile");
  };

  const onSendMessage = async () => {
    if (!selectedThreadId || !chatInput.trim()) return;
    const content = chatInput;
    setChatInput("");
    setStatusLine("Running agent");
    await api.streamMessage(selectedThreadId, content, (eventName, payload) => {
      if (eventName === "run.approval.requested") {
        setPendingApprovals((current) => [...current, payload as unknown as PendingApproval]);
        setActiveRunId(String(payload.run_id));
        setStatusLine("Waiting for MCP approval");
      }
      if (eventName === "message.delta") {
        void api.getThread(selectedThreadId).then((thread) => {
          setThreads((current) => current.map((item) => (item.id === thread.id ? thread : item)));
        });
      }
      if (eventName === "run.completed") {
        setActiveRunId(String(payload.run_id));
        setStatusLine("Run completed");
        void refreshAll();
        void refreshTelemetry(String(payload.run_id));
      }
      if (eventName === "run.failed") {
        setStatusLine(String(payload.error ?? "Run failed"));
      }
    });
    await refreshAll();
  };

  const onResolveApproval = async (approval: PendingApproval, status: "approved" | "denied") => {
    const result = await api.resolveApproval(approval.run_id, approval.approval_id, {
      status,
      rationale: status === "approved" ? "Approved in playground UI" : "Denied in playground UI",
    });
    setPendingApprovals((current) => current.filter((item) => item.approval_id !== approval.approval_id));
    setActiveRunId(result.run.id);
    setStatusLine(`Approval ${status}`);
    if (result.assistant_message) {
      await refreshAll();
      await refreshTelemetry(result.run.id);
    }
  };

  return (
    <div className="shell">
      <aside className="sidebar">
        <section className="panel">
          <h1>Agent Playground</h1>
          <p className="muted">{statusLine}</p>
        </section>

        <section className="panel">
          <div className="panel-header">
            <h2>Profiles</h2>
            <button onClick={onCreateProfile}>Save</button>
          </div>
          <label>Name</label>
          <input value={profileForm.name} onChange={(e) => onProfileField("name", e.target.value)} />
          <label>Role</label>
          <textarea value={profileForm.role} onChange={(e) => onProfileField("role", e.target.value)} rows={4} />
          <label>Guidelines</label>
          <textarea value={profileForm.guidelines} onChange={(e) => onProfileField("guidelines", e.target.value)} rows={5} />
          <label>Output Style</label>
          <textarea value={profileForm.output_style} onChange={(e) => onProfileField("output_style", e.target.value)} rows={3} />
          <label>Model</label>
          <select value={profileForm.model_name} onChange={(e) => onProfileField("model_name", e.target.value)}>
            <option value="gpt-5-mini">gpt-5-mini</option>
            <option value="gpt-5-chat-latest">gpt-5-chat-latest</option>
            <option value="gpt-5.4">gpt-5.4</option>
          </select>
          <label>Temperature</label>
          <input
            type="number"
            min={0}
            max={2}
            step={0.1}
            value={profileForm.temperature}
            onChange={(e) => onProfileField("temperature", Number(e.target.value))}
          />
          <label>Max iterations</label>
          <input
            type="number"
            min={1}
            value={profileForm.max_iterations}
            onChange={(e) => onProfileField("max_iterations", Number(e.target.value))}
          />
          <div className="list">
            {profiles.map((profile) => (
              <button
                key={profile.id}
                className={profile.id === selectedProfileId ? "list-item selected" : "list-item"}
                onClick={() => setSelectedProfileId(profile.id)}
              >
                <strong>{profile.name}</strong>
                <span>{profile.model_name}</span>
              </button>
            ))}
          </div>
        </section>

        <section className="panel">
          <div className="panel-header">
            <h2>MCP Servers</h2>
            <button onClick={onCreateServer}>Add</button>
          </div>
          <label>Name</label>
          <input value={mcpForm.name} onChange={(e) => onMcpField("name", e.target.value)} />
          <label>Label</label>
          <input value={mcpForm.label} onChange={(e) => onMcpField("label", e.target.value)} />
          <label>Server URL</label>
          <input value={mcpForm.server_url} onChange={(e) => onMcpField("server_url", e.target.value)} />
          <label>Token URL</label>
          <input value={mcpForm.token_url} onChange={(e) => onMcpField("token_url", e.target.value)} />
          <label>Client ID</label>
          <input value={mcpForm.client_id} onChange={(e) => onMcpField("client_id", e.target.value)} />
          <label>Client Secret</label>
          <input type="password" value={mcpForm.client_secret} onChange={(e) => onMcpField("client_secret", e.target.value)} />
          <label>Scope</label>
          <input value={mcpForm.scope} onChange={(e) => onMcpField("scope", e.target.value)} />
          <label>Allowed Tools</label>
          <input value={mcpForm.allowed_tools} onChange={(e) => onMcpField("allowed_tools", e.target.value)} />
          <label>Approval</label>
          <select value={mcpForm.approval_mode} onChange={(e) => onMcpField("approval_mode", e.target.value)}>
            <option value="prompt">prompt</option>
            <option value="auto">auto</option>
          </select>
          <div className="list compact">
            {servers.map((server) => (
              <div key={server.id} className="list-card">
                <strong>{server.label}</strong>
                <span>{server.server_url}</span>
                <span>{server.approval_mode} / {server.enabled ? "enabled" : "disabled"}</span>
                <button onClick={() => void api.testServer(server.id).then(() => setStatusLine(`Tested ${server.name}`))}>
                  Test
                </button>
              </div>
            ))}
          </div>
        </section>

        <section className="panel">
          <div className="panel-header">
            <h2>agent.md</h2>
            <div className="row-actions">
              <button onClick={onImportAgentMd}>Import</button>
              <button onClick={onExportAgentMd}>Export</button>
            </div>
          </div>
          <textarea value={agentMd} onChange={(e) => setAgentMd(e.target.value)} rows={12} />
        </section>
      </aside>

      <main className="workspace">
        <section className="chat-pane panel">
          <div className="panel-header">
            <h2>Threads</h2>
            <button onClick={onCreateThread} disabled={!selectedProfileId}>
              New Thread
            </button>
          </div>
          <div className="thread-strip">
            {threads.map((thread) => (
              <button
                key={thread.id}
                className={thread.id === selectedThreadId ? "thread-chip selected" : "thread-chip"}
                onClick={() => setSelectedThreadId(thread.id)}
              >
                {thread.title}
              </button>
            ))}
          </div>

          <div className="messages">
            {selectedThread?.messages.map((message) => (
              <article key={message.id} className={`message ${message.role}`}>
                <header>{message.role}</header>
                <pre>{message.content}</pre>
              </article>
            ))}
          </div>

          {pendingApprovals.length > 0 && (
            <section className="approval-box">
              <h3>Pending Approvals</h3>
              {pendingApprovals.map((approval) => (
                <div key={approval.approval_id} className="approval-card">
                  <strong>{String(approval.metadata.server_name ?? approval.mcp_server_id)}</strong>
                  <p>{String(approval.metadata.server_url ?? "")}</p>
                  <div className="row-actions">
                    <button onClick={() => void onResolveApproval(approval, "approved")}>Approve</button>
                    <button className="ghost" onClick={() => void onResolveApproval(approval, "denied")}>
                      Deny
                    </button>
                  </div>
                </div>
              ))}
            </section>
          )}

          <div className="composer">
            <textarea value={chatInput} onChange={(e) => setChatInput(e.target.value)} rows={4} placeholder="Chat with the agent" />
            <button onClick={onSendMessage} disabled={!selectedThreadId}>
              Send
            </button>
          </div>
        </section>

        <section className="telemetry-pane panel">
          <div className="panel-header">
            <h2>Telemetry</h2>
            <div className="row-actions">
              <span className="badge">local</span>
              <span className="badge">LangSmith</span>
              <span className="badge">OTEL</span>
              {activeRunId && (
                <button onClick={() => void refreshTelemetry(activeRunId)}>
                  Refresh
                </button>
              )}
            </div>
          </div>
          {telemetry ? (
            <>
              <div className="telemetry-summary">
                <div><strong>Run</strong> {telemetry.run.id}</div>
                <div><strong>Status</strong> {telemetry.run.status}</div>
                <div><strong>Trace</strong> {telemetry.run.trace_id}</div>
              </div>

              <div className="timeline">
                {telemetry.steps.map((step) => (
                  <article key={step.id} className="timeline-card">
                    <header>
                      <strong>{step.step_index}. {step.name}</strong>
                      <span>{step.kind}</span>
                    </header>
                    <p>Status: {step.status}</p>
                    <p>Latency: {step.latency_ms ?? "n/a"} ms</p>
                    <details>
                      <summary>Formatted</summary>
                      <pre>{JSON.stringify(step.output_payload, null, 2)}</pre>
                    </details>
                    <details>
                      <summary>Raw</summary>
                      <pre>{JSON.stringify(step, null, 2)}</pre>
                    </details>
                  </article>
                ))}
              </div>

              <div className="timeline">
                {telemetry.approvals.map((approval) => (
                  <article key={approval.id} className="timeline-card warning">
                    <header>
                      <strong>Approval</strong>
                      <span>{approval.status}</span>
                    </header>
                    <pre>{JSON.stringify(approval.metadata_json, null, 2)}</pre>
                  </article>
                ))}
              </div>
            </>
          ) : (
            <p className="muted">Run a thread to populate telemetry.</p>
          )}
        </section>
      </main>
    </div>
  );
}

