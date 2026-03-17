import { useEffect, useState } from "react";

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
  allowed_tools: "",
  approval_mode: "prompt",
  headers: "{}",
  timeout_ms: 20000,
  enabled: true,
};

type ThreadMessage = Thread["messages"][number];
type MpcFormState = typeof defaultMcpForm;
type MenuState = { section: "profiles" | "threads" | "servers"; id: string } | null;
type ServerTestState = {
  status: "idle" | "success" | "error";
  message: string;
  tools: string[];
};

export default function App() {
  const [profiles, setProfiles] = useState<AgentProfile[]>([]);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [servers, setServers] = useState<MCPServer[]>([]);
  const [selectedProfileId, setSelectedProfileId] = useState<string>("");
  const [selectedThreadId, setSelectedThreadId] = useState<string>("");
  const [selectedServerId, setSelectedServerId] = useState<string>("");
  const [isCreatingServer, setIsCreatingServer] = useState(false);
  const [isCreatingProfile, setIsCreatingProfile] = useState(false);
  const [isProfileEditorOpen, setIsProfileEditorOpen] = useState(false);
  const [isServerEditorOpen, setIsServerEditorOpen] = useState(false);
  const [openMenu, setOpenMenu] = useState<MenuState>(null);
  const [profileForm, setProfileForm] = useState(defaultProfileForm);
  const [mcpForm, setMcpForm] = useState(defaultMcpForm);
  const [chatInput, setChatInput] = useState("");
  const [telemetry, setTelemetry] = useState<RunTelemetry | null>(null);
  const [activeRunId, setActiveRunId] = useState<string>("");
  const [pendingApprovals, setPendingApprovals] = useState<PendingApproval[]>([]);
  const [waitingThreadId, setWaitingThreadId] = useState<string>("");
  const [statusLine, setStatusLine] = useState("Idle");
  const [errorMessage, setErrorMessage] = useState("");
  const [serverTestState, setServerTestState] = useState<ServerTestState>({ status: "idle", message: "", tools: [] });

  useEffect(() => {
    void initializeWorkspace();
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
    return { profileData, threadData, serverData };
  };

  const initializeWorkspace = async () => {
    try {
      const { profileData, threadData } = await refreshAll();
      if (profileData.length === 0) {
        const profile = await api.createProfile(defaultProfileForm);
        setSelectedProfileId(profile.id);
        const thread = await api.createThread({
          agent_profile_id: profile.id,
          title: "Starter Thread",
        });
        setSelectedThreadId(thread.id);
        setStatusLine("Created a starter profile and thread");
        await refreshAll();
        return;
      }
      if (threadData.length === 0) {
        const profileId = profileData[0].id;
        const thread = await api.createThread({
          agent_profile_id: profileId,
          title: "Starter Thread",
        });
        setSelectedThreadId(thread.id);
        setStatusLine("Created a starter thread");
        await refreshAll();
      }
    } catch (error) {
      handleError(error, "Unable to initialize the workspace");
    }
  };

  const refreshTelemetry = async (runId: string) => {
    const data = await api.getTelemetry(runId);
    setTelemetry(data);
  };

  const selectedThread = threads.find((thread) => thread.id === selectedThreadId);
  const selectedProfile = profiles.find((profile) => profile.id === selectedProfileId);
  const selectedServer = servers.find((server) => server.id === selectedServerId);
  const temperatureDisabled = profileForm.model_name.startsWith("gpt-5");

  useEffect(() => {
    if (!selectedProfile) return;
    setProfileForm({
      name: selectedProfile.name,
      role: selectedProfile.role,
      guidelines: selectedProfile.guidelines,
      output_style: selectedProfile.output_style,
      model_name: selectedProfile.model_name,
      temperature: selectedProfile.temperature,
      max_iterations: selectedProfile.max_iterations,
      telemetry_json: {
        metadata: {
          ...((selectedProfile.telemetry_json.metadata as Record<string, unknown> | undefined) ?? {}),
          environment: String(
            ((selectedProfile.telemetry_json.metadata as Record<string, unknown> | undefined)?.environment ?? "local"),
          ),
        },
        langsmith_project: selectedProfile.telemetry_json.langsmith_project ?? "agent-playground",
        tags: selectedProfile.telemetry_json.tags ?? ["playground", "mcp"],
        otel_enabled: selectedProfile.telemetry_json.otel_enabled ?? true,
        otel_service_name: selectedProfile.telemetry_json.otel_service_name ?? "agent-playground",
      },
      ui_json: selectedProfile.ui_json ?? {},
    });
  }, [selectedProfileId, selectedProfile]);

  useEffect(() => {
    if (servers.length === 0) {
      if (selectedServerId) setSelectedServerId("");
      if (!isCreatingServer) setIsCreatingServer(true);
      return;
    }
    if (!selectedServerId && isCreatingServer) {
      return;
    }
    if (!selectedServerId || !servers.some((server) => server.id === selectedServerId)) {
      void onSelectServer(servers[0]);
    }
  }, [servers, selectedServerId, isCreatingServer]);

  const handleError = (error: unknown, fallback: string) => {
    const rawMessage = error instanceof Error ? error.message : fallback;
    let message = rawMessage || fallback;
    try {
      const parsed = JSON.parse(rawMessage);
      if (typeof parsed?.detail === "string") {
        message = parsed.detail;
      } else if (typeof parsed?.error === "string") {
        message = parsed.error;
      }
    } catch {
      message = rawMessage || fallback;
    }
    setErrorMessage(message || fallback);
    setStatusLine("Action failed");
  };

  const upsertThread = (thread: Thread) => {
    setThreads((current) => {
      const existingIndex = current.findIndex((item) => item.id === thread.id);
      if (existingIndex === -1) return [thread, ...current];
      const next = [...current];
      next[existingIndex] = thread;
      return next;
    });
  };

  const appendMessageToThread = (threadId: string, message: ThreadMessage) => {
    setThreads((current) =>
      current.map((thread) => {
        if (thread.id !== threadId) return thread;
        const existingIndex = thread.messages.findIndex((item) => item.id === message.id);
        const messages =
          existingIndex === -1
            ? [...thread.messages, message]
            : thread.messages.map((item) => (item.id === message.id ? message : item));
        return { ...thread, messages, updated_at: new Date().toISOString() };
      }),
    );
  };

  const ensureThreadInState = (threadId: string, agentProfileId: string, title: string) => {
    setThreads((current) => {
      if (current.some((thread) => thread.id === threadId)) return current;
      return [
        {
          id: threadId,
          title,
          agent_profile_id: agentProfileId,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
          messages: [],
        },
        ...current,
      ];
    });
  };

  const onProfileField = (key: string, value: string | number) => {
    setProfileForm((current) => ({ ...current, [key]: value }));
  };

  const toggleMenu = (section: "profiles" | "threads" | "servers", id: string) => {
    setOpenMenu((current) => {
      if (current && current.section === section && current.id === id) return null;
      return { section, id };
    });
  };

  const closeMenu = () => {
    setOpenMenu(null);
  };

  const onResetProfileForm = () => {
    setIsCreatingProfile(true);
    setIsProfileEditorOpen(true);
    setSelectedProfileId("");
    setProfileForm(defaultProfileForm);
    closeMenu();
    setStatusLine("Ready to add a profile");
  };

  const onSelectProfile = (profile: AgentProfile, openEditor = false) => {
    setIsCreatingProfile(false);
    setIsProfileEditorOpen(openEditor);
    setSelectedProfileId(profile.id);
    closeMenu();
    setStatusLine(`Selected profile ${profile.name}`);
  };

  const onCreateProfile = async () => {
    setErrorMessage("");
    try {
      if (selectedProfileId) {
        const updated = await api.updateProfile(selectedProfileId, profileForm);
        setIsCreatingProfile(false);
        setIsProfileEditorOpen(false);
        setSelectedProfileId(updated.id);
        setStatusLine(`Updated profile ${updated.name}`);
      } else {
        const created = await api.createProfile(profileForm);
        setIsCreatingProfile(false);
        setIsProfileEditorOpen(false);
        setSelectedProfileId(created.id);
        setStatusLine(`Created profile ${created.name}`);
      }
      closeMenu();
      await refreshAll();
    } catch (error) {
      handleError(error, "Unable to save the profile");
    }
  };

  const onDeleteProfile = async (profileId: string) => {
    setErrorMessage("");
    try {
      await api.deleteProfile(profileId);
      if (selectedProfileId === profileId) {
        setSelectedProfileId("");
        setSelectedThreadId("");
        setPendingApprovals([]);
        setActiveRunId("");
        setTelemetry(null);
        setIsCreatingProfile(false);
        setIsProfileEditorOpen(false);
      }
      closeMenu();
      setStatusLine("Deleted profile");
      const { profileData } = await refreshAll();
      if (profileData.length === 0) {
        await initializeWorkspace();
      }
    } catch (error) {
      handleError(error, "Unable to delete the profile");
    }
  };

  const ensureProfile = async (): Promise<string> => {
    let profileId = selectedProfileId || profiles[0]?.id || "";
    if (!profileId) {
      const profile = await api.createProfile(profileForm);
      profileId = profile.id;
      setSelectedProfileId(profileId);
    } else {
      await api.updateProfile(profileId, profileForm);
    }
    return profileId;
  };

  const ensureProfileAndThread = async (): Promise<{ profileId: string; threadId: string; threadTitle: string }> => {
    const profileId = await ensureProfile();
    let threadId = selectedThreadId;
    let threadTitle = selectedThread?.title ?? `Thread ${new Date().toLocaleTimeString()}`;
    if (!threadId) {
      const thread = await api.createThread({
        agent_profile_id: profileId,
        title: `Thread ${new Date().toLocaleTimeString()}`,
      });
      threadId = thread.id;
      threadTitle = thread.title;
      setSelectedThreadId(threadId);
    }

    return { profileId, threadId, threadTitle };
  };

  const onCreateThread = async () => {
    setErrorMessage("");
    try {
      const profileId = await ensureProfile();
      const thread = await api.createThread({
        agent_profile_id: profileId,
        title: `Thread ${new Date().toLocaleTimeString()}`,
      });
      setSelectedThreadId(thread.id);
      closeMenu();
      setStatusLine(`Created ${thread.title}`);
      await refreshAll();
    } catch (error) {
      handleError(error, "Unable to create a thread");
    }
  };

  const onDeleteThread = async (threadId: string) => {
    setErrorMessage("");
    try {
      await api.deleteThread(threadId);
      const remainingThreads = threads.filter((thread) => thread.id !== threadId);
      setThreads(remainingThreads);
      if (selectedThreadId === threadId) {
        setSelectedThreadId(remainingThreads[0]?.id ?? "");
        setPendingApprovals([]);
        setActiveRunId("");
        setTelemetry(null);
      }
      closeMenu();
      setStatusLine("Deleted thread");
      await refreshAll();
    } catch (error) {
      handleError(error, "Unable to delete the thread");
    }
  };

  const onMcpField = (key: string, value: string | number | boolean) => {
    setMcpForm((current) => ({ ...current, [key]: value }));
    if (serverTestState.status !== "idle") {
      setServerTestState({ status: "idle", message: "", tools: [] });
    }
  };

  const serverToForm = (server: MCPServer): MpcFormState => ({
    name: server.name,
    label: server.label,
    server_url: server.server_url,
    token_url: server.token_url,
    grant_type: server.grant_type,
    client_id: "",
    client_secret: "",
    scope: server.scope,
    allowed_tools: server.allowed_tools.join(","),
    approval_mode: server.approval_mode,
    headers: JSON.stringify(server.headers ?? {}, null, 2),
    timeout_ms: server.timeout_ms,
    enabled: server.enabled,
  });

  const serializeDraftServer = (form: MpcFormState) => ({
    server_id: selectedServerId || undefined,
    ...form,
    allowed_tools: form.allowed_tools
      .split(",")
      .map((tool) => tool.trim())
      .filter(Boolean),
    headers: JSON.parse(form.headers),
  });

  const buildServerTestMessage = (result: Record<string, unknown>, label: string) => {
    const tokenMeta = (result.token_meta as Record<string, unknown> | undefined) ?? {};
    const cacheState = String(tokenMeta.cache ?? "ok");
    const expiresIn = tokenMeta.expires_in ? `, expires in ${tokenMeta.expires_in}s` : "";
    return `${label} passed (${cacheState}${expiresIn}).`;
  };

  const onSelectServer = async (server: MCPServer, openEditor = false) => {
    setErrorMessage("");
    try {
      const detail = await api.getServer(server.id);
      setIsCreatingServer(false);
      setIsServerEditorOpen(openEditor);
      setSelectedServerId(server.id);
      setMcpForm({
        ...serverToForm(server),
        client_id: detail.client_id,
        client_secret: detail.client_secret,
      });
      closeMenu();
      setServerTestState({ status: "idle", message: "", tools: [] });
      setStatusLine(`Selected ${server.label}`);
    } catch (error) {
      handleError(error, `Unable to load ${server.label}`);
    }
  };

  const onResetServerForm = () => {
    setIsCreatingServer(true);
    setIsServerEditorOpen(true);
    setSelectedServerId("");
    setMcpForm(defaultMcpForm);
    setServerTestState({ status: "idle", message: "", tools: [] });
    closeMenu();
    setStatusLine("Ready to add an MCP server");
  };

  const onCreateServer = async () => {
    setErrorMessage("");
    try {
      const payload = serializeDraftServer(mcpForm);
      const saved = selectedServerId
        ? await api.updateServer(selectedServerId, {
            label: payload.label,
            server_url: payload.server_url,
            token_url: payload.token_url,
            grant_type: payload.grant_type,
            scope: payload.scope,
            approval_mode: payload.approval_mode,
            enabled: payload.enabled,
            ...(mcpForm.client_id ? { client_id: mcpForm.client_id } : {}),
            ...(mcpForm.client_secret ? { client_secret: mcpForm.client_secret } : {}),
          })
        : await api.createServer(payload);
      setIsCreatingServer(false);
      setIsServerEditorOpen(false);
      setSelectedServerId(saved.id);
      const detail = await api.getServer(saved.id);
      setMcpForm({
        ...serverToForm(saved),
        client_id: detail.client_id,
        client_secret: detail.client_secret,
      });
      setStatusLine(`${selectedServerId ? "Updated" : "Saved"} MCP server ${saved.label}`);
      setServerTestState({ status: "idle", message: "", tools: [] });
      closeMenu();
      await refreshAll();
    } catch (error) {
      handleError(error, "Unable to save the MCP server");
    }
  };

  const onDeleteServer = async (serverId: string) => {
    setErrorMessage("");
    try {
      await api.deleteServer(serverId);
      if (selectedServerId === serverId) {
        setSelectedServerId("");
        setIsCreatingServer(false);
        setIsServerEditorOpen(false);
        setMcpForm(defaultMcpForm);
        setServerTestState({ status: "idle", message: "", tools: [] });
      }
      closeMenu();
      setStatusLine("Deleted MCP server");
      await refreshAll();
    } catch (error) {
      handleError(error, "Unable to delete the MCP server");
    }
  };

  const onTestDraftServer = async () => {
    setErrorMessage("");
    try {
      const result = await api.testDraftServer(serializeDraftServer(mcpForm));
      const label = selectedServerId ? mcpForm.label || mcpForm.name : `Draft ${mcpForm.label || mcpForm.name}`;
      const message = buildServerTestMessage(result, label);
      const tools = Array.isArray(result.discovered_tools)
        ? result.discovered_tools.map((tool) => String(tool))
        : [];
      setServerTestState({ status: "success", message, tools });
      setStatusLine("MCP configuration passed");
    } catch (error) {
      const rawMessage = error instanceof Error ? error.message : "Unable to test the draft MCP server";
      let message = rawMessage;
      try {
        const parsed = JSON.parse(rawMessage);
        message = typeof parsed?.detail === "string" ? parsed.detail : rawMessage;
      } catch {
        message = rawMessage;
      }
      setServerTestState({ status: "error", message, tools: [] });
      setErrorMessage(message);
      setStatusLine("MCP test failed");
    }
  };

  const onSendMessage = async () => {
    if (!chatInput.trim()) {
      setStatusLine("Enter a message first");
      return;
    }
    setErrorMessage("");
    try {
      const { profileId, threadId, threadTitle } = await ensureProfileAndThread();
      const content = chatInput;
      setChatInput("");
      setSelectedThreadId(threadId);
      ensureThreadInState(threadId, profileId, threadTitle);
      appendMessageToThread(threadId, {
        id: `pending-user-${Date.now()}`,
        thread_id: threadId,
        role: "user",
        content,
        metadata_json: {},
        created_at: new Date().toISOString(),
      });
      setWaitingThreadId(threadId);
      setStatusLine("Running agent");
      await api.streamMessage(threadId, content, (eventName, payload) => {
        const runId = String(payload.run_id ?? "");
        if (runId) setActiveRunId(runId);
        if (eventName === "run.step.started" || eventName === "run.step.completed") {
          setStatusLine(`Run ${String(payload.kind ?? "step")}`);
          if (runId) void refreshTelemetry(runId);
        }
        if (eventName === "run.approval.requested") {
          setPendingApprovals((current) => [...current, payload as unknown as PendingApproval]);
          setStatusLine("Waiting for MCP approval");
          if (runId) void refreshTelemetry(runId);
        }
        if (eventName === "message.delta") {
          appendMessageToThread(threadId, {
            id: String(payload.message_id),
            thread_id: threadId,
            role: "assistant",
            content: String(payload.delta ?? ""),
            metadata_json: {},
            created_at: new Date().toISOString(),
          });
        }
        if (eventName === "run.completed") {
          setWaitingThreadId("");
          setStatusLine("Run completed");
          const assistantMessage = payload.assistant_message as ThreadMessage | undefined;
          if (assistantMessage) appendMessageToThread(threadId, assistantMessage);
          void api.getThread(threadId).then(upsertThread);
          if (runId) void refreshTelemetry(runId);
        }
        if (eventName === "run.failed") {
          setWaitingThreadId("");
          setStatusLine(String(payload.error ?? "Run failed"));
          setErrorMessage(String(payload.error ?? "Run failed"));
          const assistantMessage = payload.assistant_message as ThreadMessage | undefined;
          if (assistantMessage) appendMessageToThread(threadId, assistantMessage);
          void api.getThread(threadId).then(upsertThread);
          if (runId) void refreshTelemetry(runId);
        }
      });
      const updatedThread = await api.getThread(threadId);
      upsertThread(updatedThread);
      await refreshAll();
    } catch (error) {
      setWaitingThreadId("");
      handleError(error, "Unable to send the message");
    }
  };

  const onResolveApproval = async (approval: PendingApproval, status: "approved" | "denied") => {
    setErrorMessage("");
    try {
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
    } catch (error) {
      handleError(error, "Unable to resolve the approval");
    }
  };

  return (
    <div className="shell">
      <aside
        className={
          openMenu && (openMenu.section === "profiles" || openMenu.section === "servers")
            ? "sidebar has-open-menu"
            : "sidebar"
        }
      >
        <section className="panel">
          <h1>Agent Playground</h1>
          <p className="muted">{statusLine}</p>
          {errorMessage && <p className="error-banner">{errorMessage}</p>}
        </section>

        <section className={openMenu?.section === "profiles" ? "panel has-open-menu" : "panel"}>
          <div className="panel-header">
            <h2>Profiles</h2>
            <button className="icon-button" onClick={onResetProfileForm} aria-label="Add profile">+</button>
          </div>
          <div className="entity-list">
            {profiles.map((profile) => (
              <div key={profile.id} className={profile.id === selectedProfileId ? "entity-row selected" : "entity-row"}>
                <button className="entity-main" onClick={() => onSelectProfile(profile)}>
                  <strong>{profile.name}</strong>
                  <span>{profile.model_name}</span>
                </button>
                <div className="entity-actions">
                  <button
                    className="kebab-button secondary-button"
                    onClick={(event) => {
                      event.stopPropagation();
                      toggleMenu("profiles", profile.id);
                    }}
                    aria-label={`Profile options for ${profile.name}`}
                  >
                    ⋮
                  </button>
                  {openMenu?.section === "profiles" && openMenu.id === profile.id && (
                    <div className="menu-popover">
                      <button
                        className="menu-item"
                        onClick={() => onSelectProfile(profile, true)}
                      >
                        Edit
                      </button>
                      <button className="menu-item danger" onClick={() => void onDeleteProfile(profile.id)}>Delete</button>
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
          {isProfileEditorOpen && <h3>{selectedProfileId ? "Edit Profile" : "New Profile"}</h3>}
          {isProfileEditorOpen && (
            <>
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
            disabled={temperatureDisabled}
          />
          {temperatureDisabled && <p className="helper-text">This model ignores temperature, so the runtime omits it.</p>}
          <label>Max iterations</label>
          <input
            type="number"
            min={1}
            value={profileForm.max_iterations}
            onChange={(e) => onProfileField("max_iterations", Number(e.target.value))}
          />
          <div className="row-actions">
            <div className="action-row">
              <button onClick={onCreateProfile}>{selectedProfileId ? "Save Changes" : "Save Profile"}</button>
            </div>
          </div>
            </>
          )}
        </section>

        <section className={openMenu?.section === "servers" ? "panel has-open-menu" : "panel"}>
          <div className="panel-header">
            <h2>MCP Servers</h2>
            <button className="icon-button" onClick={onResetServerForm} aria-label="Add MCP server">+</button>
          </div>
          <div className="mcp-manager">
            <div className="mcp-server-list">
              {servers.map((server) => (
                <div key={server.id} className={server.id === selectedServerId ? "entity-row selected" : "entity-row"}>
                    <button className="entity-main" onClick={() => void onSelectServer(server)}>
                    <strong>{server.label}</strong>
                    <span>{server.enabled ? "Enabled" : "Disabled"} · {server.approval_mode}</span>
                  </button>
                  <div className="entity-actions">
                    <button
                      className="kebab-button secondary-button"
                      onClick={(event) => {
                        event.stopPropagation();
                        toggleMenu("servers", server.id);
                      }}
                      aria-label={`MCP server options for ${server.label}`}
                    >
                      ⋮
                    </button>
                    {openMenu?.section === "servers" && openMenu.id === server.id && (
                      <div className="menu-popover">
                        <button
                          className="menu-item"
                          onClick={() => void onSelectServer(server, true)}
                        >
                          Edit
                        </button>
                        <button className="menu-item danger" onClick={() => void onDeleteServer(server.id)}>Delete</button>
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>

            <div className="mcp-editor">
              {isServerEditorOpen && <h3>{selectedServer ? selectedServer.label : "New MCP Server"}</h3>}
              {isServerEditorOpen && (
                <>
              <p className="helper-text">
                {selectedServer
                  ? "Update the selected server and test it from this editor."
                  : "Create a new server, test it, then save it."}
              </p>
              <div className="toggle-row">
                <label htmlFor="mcp-enabled">Enabled</label>
                <input
                  id="mcp-enabled"
                  type="checkbox"
                  checked={mcpForm.enabled}
                  onChange={(e) => onMcpField("enabled", e.target.checked)}
                />
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
              <label>Approval</label>
              <select value={mcpForm.approval_mode} onChange={(e) => onMcpField("approval_mode", e.target.value)}>
                <option value="prompt">prompt</option>
                <option value="auto">auto</option>
              </select>
              <div className="action-row">
                <button className="secondary-button" onClick={() => void onTestDraftServer()}>Test</button>
                <button onClick={onCreateServer}>{selectedServerId ? "Save Changes" : "Save Server"}</button>
              </div>
              {serverTestState.status === "success" && <p className="result-banner">{serverTestState.message}</p>}
              {serverTestState.status === "error" && <p className="error-banner">{serverTestState.message}</p>}
              {serverTestState.status === "success" && (
                <div className="mcp-tools-panel">
                  <strong>Discovered Tools</strong>
                  {serverTestState.tools.length > 0 ? (
                    <div className="mcp-tools-list">
                      {serverTestState.tools.map((tool) => (
                        <span key={tool} className="mcp-tool-chip">{tool}</span>
                      ))}
                    </div>
                  ) : (
                    <p className="helper-text">No tools were returned by the server.</p>
                  )}
                </div>
              )}
                </>
              )}
          </div>
          </div>
        </section>

      </aside>

      <main className="workspace">
        <section className={openMenu?.section === "threads" ? "chat-pane panel has-open-menu" : "chat-pane panel"}>
          <div className="panel-header">
            <h2>Threads</h2>
            <button className="icon-button" onClick={onCreateThread} aria-label="Add thread">+</button>
          </div>
          <p className="helper-text">
            Profile: <strong>{selectedProfile?.name ?? "Draft profile"}</strong>
            {" · "}
            Thread: <strong>{selectedThread?.title ?? "No thread selected"}</strong>
          </p>
          <div className="entity-list thread-list">
              {threads.map((thread) => (
                <div key={thread.id} className={thread.id === selectedThreadId ? "entity-row selected" : "entity-row"}>
                <button
                  className="entity-main"
                  onClick={() => {
                    setSelectedThreadId(thread.id);
                    closeMenu();
                    setStatusLine(`Selected ${thread.title}`);
                  }}
                >
                  <strong>{thread.title}</strong>
                  <span>{new Date(thread.updated_at).toLocaleString()}</span>
                </button>
                <div className="entity-actions">
                  <button
                    className="kebab-button secondary-button"
                    onClick={(event) => {
                      event.stopPropagation();
                      toggleMenu("threads", thread.id);
                    }}
                    aria-label={`Thread options for ${thread.title}`}
                  >
                    ⋮
                  </button>
                  {openMenu?.section === "threads" && openMenu.id === thread.id && (
                    <div className="menu-popover">
                      <button className="menu-item danger" onClick={() => void onDeleteThread(thread.id)}>Delete</button>
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>

          <div className="messages">
            {selectedThread?.messages.map((message) => (
              <article key={message.id} className={`message ${message.role}`}>
                <header>{message.role}</header>
                <pre>{message.content}</pre>
              </article>
            ))}
            {waitingThreadId === selectedThreadId && (
              <article className="message assistant waiting">
                <header>assistant</header>
                <pre>Thinking...</pre>
              </article>
            )}
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
            <button onClick={onSendMessage}>
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
                <button className="secondary-button" onClick={() => void refreshTelemetry(activeRunId)}>
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
