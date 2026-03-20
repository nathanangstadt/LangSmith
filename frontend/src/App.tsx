import { useEffect, useRef, useState } from "react";

import { api } from "./api";
import type { AgentProfile, MCPServer, OtelSpan, PendingApproval, RunTelemetry, Thread } from "./types";

type SpanNode = OtelSpan & { children: SpanNode[] };

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
  ui_json: {
    detailed_messages_enabled: false,
  },
};

const defaultMcpForm = {
  name: "oracle_docs",
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

type DetailedActivityItem = {
  key: string;
  role: "system" | "user" | "assistant" | "assistant_context" | "tool_request" | "tool_response";
  label: string;
  body: string;
  raw: Record<string, unknown>;
  order?: number;
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
  const [renamingThreadId, setRenamingThreadId] = useState<string>("");
  const [renamingTitle, setRenamingTitle] = useState<string>("");
  const [statusLine, setStatusLine] = useState("Idle");
  const [errorMessage, setErrorMessage] = useState("");
  const [serverTestState, setServerTestState] = useState<ServerTestState>({ status: "idle", message: "", tools: [] });
  const [liveDetailedActivity, setLiveDetailedActivity] = useState<DetailedActivityItem[]>([]);
  const [traceModalOpen, setTraceModalOpen] = useState(false);
  const [backendConfig, setBackendConfig] = useState<{ langsmith_available: boolean; langsmith_url: string; otel_available: boolean; otel_endpoint: string; export_mode: "none" | "langsmith" | "otel"; jaeger_ui_url: string; openai_configured: boolean } | null>(null);

  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    void initializeWorkspace();
    api.getConfig().then(setBackendConfig).catch(() => {});
  }, []);

  const refreshAll = async () => {
    const [profileData, threadData, serverData] = await Promise.all([
      api.listProfiles(),
      api.listThreads(),
      api.listServers(),
    ]);
    setProfiles(profileData);
    // Preserve in-memory messages: listThreads returns threads with messages=[].
    // Merging keeps messages loaded during this session visible after refresh.
    setThreads((current) => {
      const messageMap = new Map(current.map((t) => [t.id, t.messages]));
      return threadData.map((thread) => ({
        ...thread,
        messages: messageMap.get(thread.id) ?? [],
      }));
    });
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
  const temperatureDisabled = profileForm.model_name.startsWith("gpt-5") || profileForm.model_name.startsWith("o");
  const detailedMessagesEnabled = Boolean(selectedProfile?.ui_json?.detailed_messages_enabled);

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
      ui_json: {
        detailed_messages_enabled: Boolean(selectedProfile.ui_json?.detailed_messages_enabled),
        ...(selectedProfile.ui_json ?? {}),
      },
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

  useEffect(() => {
    if (!selectedThreadId || !selectedThread) {
      setLiveDetailedActivity([]);
      return;
    }
    if (waitingThreadId && waitingThreadId !== selectedThreadId) {
      setLiveDetailedActivity([]);
    }
  }, [selectedThreadId, selectedThread, waitingThreadId]);

  useEffect(() => {
    if (!selectedThreadId || waitingThreadId === selectedThreadId) return;
    // Load full thread messages when switching threads.
    api.getThread(selectedThreadId).then(upsertThread).catch(() => {});
    api.listRuns(selectedThreadId).then((runs) => {
      if (runs.length > 0) {
        setActiveRunId(runs[0].id);
        void refreshTelemetry(runs[0].id);
      } else {
        setActiveRunId("");
        setTelemetry(null);
      }
    }).catch(() => {});
  }, [selectedThreadId]);

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

  const upsertStreamingAssistantMessage = (threadId: string, messageId: string, snapshot: string) => {
    appendMessageToThread(threadId, {
      id: messageId,
      thread_id: threadId,
      role: "assistant",
      content: snapshot,
      metadata_json: { streaming: true },
      created_at: new Date().toISOString(),
    });
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

  const onProfileUiField = (key: string, value: boolean) => {
    setProfileForm((current) => ({
      ...current,
      ui_json: {
        ...(current.ui_json ?? {}),
        [key]: value,
      },
    }));
  };

  const contentText = (content: unknown, textKey: "text" | "input_text" = "text") => {
    if (!Array.isArray(content)) return "";
    return content
      .map((entry) => {
        if (!entry || typeof entry !== "object") return "";
        if (textKey in entry) return String(entry[textKey as keyof typeof entry] ?? "");
        if ("text" in entry) return String(entry.text ?? "");
        return "";
      })
      .filter(Boolean)
      .join("\n");
  };

  const buildInputDetailedItems = (
    instructions: string,
    inputItems: Record<string, unknown>[],
    orderBase = 0,
  ): DetailedActivityItem[] => {
    const items: DetailedActivityItem[] = [];
    if (instructions.trim()) {
      items.push({
        key: "system-instructions",
        role: "system",
        label: "System",
        body: instructions,
        raw: { type: "instructions", instructions },
        order: orderBase,
      });
    }
    inputItems.forEach((item, index) => {
      const role = String(item.role ?? "user");
      const body = contentText(item.content, "text");
      if (!body) return;
      items.push({
        key: `input-${index}`,
        role: role === "assistant" ? "assistant" : "user",
        label: role === "assistant" ? "Assistant Context" : "User",
        body,
        raw: item,
        order: orderBase + index + 1,
      });
    });
    return items;
  };

  const buildOutputDetailedItems = (
    item: Record<string, unknown>,
    fallbackKey: string,
    order?: number,
  ): DetailedActivityItem[] => {
    const type = String(item.type ?? "event");
    const itemKey = String(item.id ?? fallbackKey);
    if (type === "reasoning") {
      const summaries = Array.isArray(item.summary) ? item.summary : [];
      const summaryText = summaries
        .map((entry) => {
          if (typeof entry === "string") return entry;
          if (entry && typeof entry === "object" && "text" in entry) return String(entry.text ?? "");
          return "";
        })
        .filter(Boolean)
        .join("\n");
      if (!summaryText) return [];
      return [{
        key: itemKey,
        role: "assistant_context",
        label: "Assistant Context",
        body: summaryText,
        raw: item,
        order,
      }];
    }
    if (type === "mcp_call") {
      const toolName = String(item.name ?? "unknown");
      const serverLabel = String(item.server_label ?? "MCP");
      const argumentsText = typeof item.arguments === "string" ? item.arguments : "";
      const output = typeof item.output === "string" ? item.output : JSON.stringify(item.output ?? {}, null, 2);
      const entries: DetailedActivityItem[] = [{
        key: `${itemKey}:request`,
        role: "tool_request",
        label: `Tool Request · ${toolName}`,
        body: argumentsText || "Calling tool...",
        raw: item,
        order,
      }];
      if (output) {
        entries.push({
          key: `${itemKey}:response`,
          role: "tool_response",
          label: `Tool Response · ${toolName}`,
          body: output,
          raw: item,
          order: typeof order === "number" ? order + 0.1 : order,
        });
      } else if (String(item.status ?? "") === "completed") {
        entries.push({
          key: `${itemKey}:response`,
          role: "tool_response",
          label: `Tool Response · ${toolName}`,
          body: "Tool completed with no returned content.",
          raw: item,
          order: typeof order === "number" ? order + 0.1 : order,
        });
      }
      return entries.map((entry) => ({ ...entry, raw: { ...item, server_label: serverLabel } }));
    }
    if (type === "message") {
      const body = contentText(item.content);
      return [{
        key: itemKey,
        role: "assistant",
        label: "Assistant",
        body: body || "",
        raw: item,
        order,
      }];
    }
    return [];
  };

  const buildSpanTree = (spans: OtelSpan[]): SpanNode | null => {
    const nodes = new Map(spans.map((s) => ({ ...s, children: [] as SpanNode[] })).map((n) => [n.span_id, n]));
    let root: SpanNode | null = null;
    for (const node of nodes.values()) {
      if (node.parent_span_id && nodes.has(node.parent_span_id)) {
        nodes.get(node.parent_span_id)!.children.push(node);
      } else {
        root = node;
      }
    }
    return root;
  };

  const spanVisual = (name: string, attrs: Record<string, unknown>, durationMs: number | null) => {
    const dur = durationMs != null ? (durationMs >= 1000 ? `~${(durationMs / 1000).toFixed(1)}s` : `~${durationMs}ms`) : "";
    if (name === "gen_ai.agent.invoke") return { bg: "#3b2d7e", subtitle: `Root span · ${dur} total` };
    if (name === "gen_ai.chat") return { bg: "#7a4a18", subtitle: `${String(attrs["gen_ai.request.model"] ?? "")} · ${dur}` };
    if (name === "gen_ai.tool.call") return { bg: "#4a3a6a", subtitle: `${String(attrs["gen_ai.tool.name"] ?? "")} · ${dur}` };
    if (name === "gen_ai.approval.wait") return { bg: "#5c3a1a", subtitle: `${String(attrs["approval.server"] ?? "")} · ${dur}` };
    if (name === "prepare.prompt") return { bg: "#1a5c4a", subtitle: `${String(attrs["agent.message_count"] ?? "?")} message` };
    if (name === "final.answer") return { bg: "#1a5c4a", subtitle: "Summary output" };
    return { bg: "#2a4258", subtitle: name };
  };

  type TraceEventRow = { key: string; bg: string; title: string; subtitle: string };

  const parseTraceEvents = (span: OtelSpan): TraceEventRow[] => {
    const rows: TraceEventRow[] = [];
    let i = 0;
    while (i < span.events.length) {
      const ev = span.events[i];
      const attrs = ev.attributes;
      if (ev.name === "gen_ai.system.message") {
        const text = String(attrs["gen_ai.prompt"] ?? "");
        rows.push({ key: `${i}`, bg: "#3d4852", title: "System prompt", subtitle: text.slice(0, 90) + (text.length > 90 ? "…" : "") });
        i++; continue;
      }
      if (ev.name === "gen_ai.user.message" || ev.name === "gen_ai.assistant.message") {
        const role = ev.name === "gen_ai.user.message" ? "User" : "Assistant context";
        const raw = String(attrs["gen_ai.prompt"] ?? "");
        let preview = raw;
        try {
          const p = JSON.parse(raw) as Record<string, unknown>;
          const c = p.content;
          if (Array.isArray(c)) preview = c.map((x: unknown) => (x && typeof x === "object" && "text" in x ? String((x as { text: string }).text) : "")).filter(Boolean).join(" ");
        } catch { /* use raw */ }
        rows.push({ key: `${i}`, bg: "#3d4852", title: `${role} message`, subtitle: `"${preview.slice(0, 90)}${preview.length > 90 ? "…" : ""}"` });
        i++; continue;
      }
      if (ev.name === "gen_ai.output_item.done") {
        let item: Record<string, unknown> = {};
        try { item = JSON.parse(String(attrs["content"] ?? "{}")); } catch { /* */ }
        const type = String(item.type ?? "");
        if (type === "mcp_call") {
          const toolName = String(item.name ?? "tool");
          let count = 1;
          const subtitles: string[] = [];
          const fmt = (it: Record<string, unknown>) => {
            const args = typeof it.arguments === "string" ? it.arguments : JSON.stringify(it.arguments ?? "");
            const out = String(it.output ?? "").trim().slice(0, 50);
            return `${args.slice(0, 30)}${out ? ` → ${out}` : ""}`;
          };
          subtitles.push(fmt(item));
          while (i + count < span.events.length) {
            const nev = span.events[i + count];
            if (nev.name !== "gen_ai.output_item.done") break;
            let nit: Record<string, unknown> = {};
            try { nit = JSON.parse(String(nev.attributes["content"] ?? "{}")); } catch { /* */ }
            if (String(nit.type ?? "") !== "mcp_call" || String(nit.name ?? "") !== toolName) break;
            subtitles.push(fmt(nit));
            count++;
          }
          const bg = toolName.startsWith("LIST") ? "#1e3a5f" : toolName.startsWith("CLASSIFY") ? "#5c2a1a" : "#1e3a5f";
          rows.push({ key: `${i}`, bg, title: count > 1 ? `Tool call: ${toolName} × ${count}` : `Tool call: ${toolName}`, subtitle: subtitles.slice(0, 3).join(" · ") });
          i += count; continue;
        }
        if (type === "message") {
          const content = Array.isArray(item.content) ? item.content : [];
          const text = content.map((c: unknown) => (c && typeof c === "object" && "text" in c ? String((c as { text: string }).text) : "")).filter(Boolean).join(" ");
          rows.push({ key: `${i}`, bg: "#3d4852", title: "Model output", subtitle: text.slice(0, 120) + (text.length > 120 ? "…" : "") });
          i++; continue;
        }
        i++; continue;
      }
      if (ev.name === "gen_ai.choice") {
        const completion = String(attrs["gen_ai.completion"] ?? "");
        const reason = String(attrs["finish_reason"] ?? "stop");
        rows.push({ key: `${i}`, bg: "#1a5c2a", title: `gen_ai.choice (finish_reason: ${reason})`, subtitle: completion.slice(0, 100) + (completion.length > 100 ? "…" : "") });
        i++; continue;
      }
      i++;
    }
    return rows;
  };

  const upsertLiveDetailedItems = (items: DetailedActivityItem[]) => {
    setLiveDetailedActivity((current) => {
      const next = [...current];
      for (const item of items) {
        const index = next.findIndex((entry) => entry.key === item.key);
        if (index === -1) {
          next.push(item);
          continue;
        }
        next[index] = item;
      }
      return next.sort((left, right) => (left.order ?? Number.MAX_SAFE_INTEGER) - (right.order ?? Number.MAX_SAFE_INTEGER));
    });
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

  const onCloneProfile = async (profileId: string) => {
    setErrorMessage("");
    try {
      const cloned = await api.cloneProfile(profileId);
      setProfiles((current) => [cloned, ...current]);
      closeMenu();
      setStatusLine(`Cloned profile as "${cloned.name}"`);
    } catch (error) {
      handleError(error, "Unable to clone profile");
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
        setWaitingThreadId("");
      }
      closeMenu();
      setStatusLine("Deleted thread");
      await refreshAll();
    } catch (error) {
      handleError(error, "Unable to delete the thread");
    }
  };

  const onCommitRename = async (threadId: string) => {
    const title = renamingTitle.trim();
    setRenamingThreadId("");
    setRenamingTitle("");
    if (!title) return;
    try {
      const updated = await api.updateThread(threadId, { title });
      upsertThread(updated);
    } catch (error) {
      handleError(error, "Unable to rename thread");
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

  const parseHeadersField = (raw: string): Record<string, string> => {
    try {
      return JSON.parse(raw);
    } catch {
      throw new Error('Headers must be valid JSON (e.g. {"X-Custom": "value"} or {})');
    }
  };

  const serializeDraftServer = (form: MpcFormState) => ({
    server_id: selectedServerId || undefined,
    ...form,
    allowed_tools: form.allowed_tools
      .split(",")
      .map((tool) => tool.trim())
      .filter(Boolean),
    headers: parseHeadersField(form.headers),
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
      setStatusLine(`Selected ${server.name}`);
    } catch (error) {
      handleError(error, `Unable to load ${server.name}`);
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
            name: payload.name,
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
      setStatusLine(`${selectedServerId ? "Updated" : "Saved"} MCP server ${saved.name}`);
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

  const onCloneServer = async (serverId: string) => {
    setErrorMessage("");
    try {
      const cloned = await api.cloneServer(serverId);
      setServers((current) => [cloned, ...current]);
      closeMenu();
      setStatusLine(`Cloned server as "${cloned.name}"`);
    } catch (error) {
      handleError(error, "Unable to clone MCP server");
    }
  };

  const onTestDraftServer = async () => {
    setErrorMessage("");
    try {
      const result = await api.testDraftServer(serializeDraftServer(mcpForm));
      const label = selectedServerId ? mcpForm.name : `Draft ${mcpForm.name}`;
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
    setLiveDetailedActivity([]);
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
        // NOTE: run.approval.requested closes the stream without run.completed/run.failed,
        // so waitingThreadId is cleared unconditionally after the stream below.
        const runId = String(payload.run_id ?? "");
        if (runId) setActiveRunId(runId);
        if (eventName === "run.step.started" || eventName === "run.step.completed") {
          setStatusLine(`Run ${String(payload.kind ?? "step")}`);
          if (runId) void refreshTelemetry(runId);
        }
        if (eventName === "run.detail.input") {
          const instructions = String(payload.instructions ?? "");
          const inputItems = Array.isArray(payload.input_items)
            ? (payload.input_items as Record<string, unknown>[])
            : [];
          upsertLiveDetailedItems(buildInputDetailedItems(instructions, inputItems));
        }
        if (eventName === "run.detail.item") {
          const item = payload.item;
          if (item && typeof item === "object") {
            upsertLiveDetailedItems(
              buildOutputDetailedItems(
                item as Record<string, unknown>,
                `${String(payload.output_index ?? "item")}`,
                Number(payload.sequence_number ?? Number.MAX_SAFE_INTEGER),
              ),
            );
          }
        }
        if (eventName === "run.detail.text") {
          const snapshot = String(payload.snapshot ?? "");
          const itemId = String(payload.item_id ?? `message-${runId}`);
          if (snapshot) {
            upsertLiveDetailedItems([{
              key: itemId,
              role: "assistant",
              label: "Assistant",
              body: snapshot,
              raw: {
                type: "message",
                id: itemId,
                status: "in_progress",
                content: [{ type: "output_text", text: snapshot }],
              },
              order: Number.MAX_SAFE_INTEGER - 1,
            }]);
          }
        }
        if (eventName === "run.approval.requested") {
          setPendingApprovals((current) => [...current, payload as unknown as PendingApproval]);
          setStatusLine("Waiting for MCP approval");
          if (runId) void refreshTelemetry(runId);
        }
        if (eventName === "message.delta") {
          upsertStreamingAssistantMessage(
            threadId,
            String(payload.message_id),
            String(payload.snapshot ?? payload.delta ?? ""),
          );
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
      setWaitingThreadId("");  // always clear — no-op if run.completed/run.failed already cleared it
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
    setPendingApprovals((current) => current.filter((item) => item.approval_id !== approval.approval_id));
    const threadId = selectedThreadId;
    try {
      const result = await api.resolveApproval(approval.run_id, approval.approval_id, {
        status,
        rationale: status === "approved" ? "Approved in playground UI" : "Denied in playground UI",
      });
      setActiveRunId(result.run.id);
      if (result.run.status === "failed") {
        setStatusLine("Run failed");
        setErrorMessage("Approval was denied.");
        await refreshAll();
        await refreshTelemetry(result.run.id);
        return;
      }
      // Approved — stream the resumed run in real-time.
      setWaitingThreadId(threadId);
      setLiveDetailedActivity([]);
      setStatusLine("Resuming agent after approval…");
      const runId = result.run.id;
      await api.streamResumedRun(runId, (eventName, payload) => {
        if (eventName === "run.step.started" || eventName === "run.step.completed") {
          setStatusLine(`Run ${String(payload.kind ?? "step")}`);
          void refreshTelemetry(runId);
        }
        if (eventName === "run.detail.input") {
          const instructions = String(payload.instructions ?? "");
          const inputItems = Array.isArray(payload.input_items)
            ? (payload.input_items as Record<string, unknown>[])
            : [];
          upsertLiveDetailedItems(buildInputDetailedItems(instructions, inputItems));
        }
        if (eventName === "run.detail.item") {
          const item = payload.item;
          if (item && typeof item === "object") {
            upsertLiveDetailedItems(
              buildOutputDetailedItems(
                item as Record<string, unknown>,
                `${String(payload.output_index ?? "item")}`,
                Number(payload.sequence_number ?? Number.MAX_SAFE_INTEGER),
              ),
            );
          }
        }
        if (eventName === "run.detail.text") {
          const snapshot = String(payload.snapshot ?? "");
          const itemId = String(payload.item_id ?? `message-${runId}`);
          if (snapshot) {
            upsertLiveDetailedItems([{
              key: itemId,
              role: "assistant",
              label: "Assistant",
              body: snapshot,
              raw: {
                type: "message",
                id: itemId,
                status: "in_progress",
                content: [{ type: "output_text", text: snapshot }],
              },
              order: Number.MAX_SAFE_INTEGER - 1,
            }]);
          }
        }
        if (eventName === "run.approval.requested") {
          setPendingApprovals((current) => [...current, payload as unknown as PendingApproval]);
          setStatusLine("Waiting for MCP approval");
          void refreshTelemetry(runId);
        }
        if (eventName === "message.delta") {
          upsertStreamingAssistantMessage(
            threadId,
            String(payload.message_id),
            String(payload.snapshot ?? payload.delta ?? ""),
          );
        }
        if (eventName === "run.completed") {
          setWaitingThreadId("");
          setStatusLine("Run completed");
          const assistantMessage = payload.assistant_message as ThreadMessage | undefined;
          if (assistantMessage) appendMessageToThread(threadId, assistantMessage);
          void api.getThread(threadId).then(upsertThread);
          void refreshTelemetry(runId);
        }
        if (eventName === "run.failed") {
          setWaitingThreadId("");
          setStatusLine(String(payload.error ?? "Run failed"));
          setErrorMessage(String(payload.error ?? "Run failed"));
          const assistantMessage = payload.assistant_message as ThreadMessage | undefined;
          if (assistantMessage) appendMessageToThread(threadId, assistantMessage);
          void api.getThread(threadId).then(upsertThread);
          void refreshTelemetry(runId);
        }
      });
      setWaitingThreadId("");  // always clear after stream ends
      const updatedThread = await api.getThread(threadId);
      upsertThread(updatedThread);
      await refreshAll();
    } catch (error) {
      setWaitingThreadId("");
      handleError(error, "Unable to resolve the approval");
    }
  };

  const persistedDetailedActivity: DetailedActivityItem[] =
    detailedMessagesEnabled && telemetry?.run.thread_id === selectedThreadId
      ? telemetry.spans
          .slice()
          .sort((a, b) => a.start_time_unix_nano - b.start_time_unix_nano)
          .flatMap<DetailedActivityItem>((span, spanIndex) => {
            const items: DetailedActivityItem[] = [];
            const orderBase = spanIndex * 1000;
            span.events.forEach((event, eventIndex) => {
              const attrs = event.attributes;
              if (event.name === "gen_ai.system.message") {
                const prompt = String(attrs["gen_ai.prompt"] ?? "");
                if (prompt.trim()) {
                  items.push({ key: `${span.span_id}-system`, role: "system", label: "System", body: prompt, raw: attrs, order: orderBase + eventIndex });
                }
              } else if (event.name === "gen_ai.user.message" || event.name === "gen_ai.assistant.message") {
                const role = event.name === "gen_ai.user.message" ? "user" : "assistant_context";
                const label = role === "user" ? "User" : "Assistant Context";
                const prompt = String(attrs["gen_ai.prompt"] ?? "");
                try {
                  const parsed = JSON.parse(prompt) as Record<string, unknown>;
                  const body = contentText(parsed.content, "text");
                  if (body) items.push({ key: `${span.span_id}-${event.name}-${eventIndex}`, role: role as DetailedActivityItem["role"], label, body, raw: parsed, order: orderBase + eventIndex });
                } catch {
                  if (prompt.trim()) items.push({ key: `${span.span_id}-${event.name}-${eventIndex}`, role: role as DetailedActivityItem["role"], label, body: prompt, raw: attrs, order: orderBase + eventIndex });
                }
              } else if (event.name === "gen_ai.output_item.done") {
                try {
                  const item = JSON.parse(String(attrs["content"] ?? "{}")) as Record<string, unknown>;
                  items.push(...buildOutputDetailedItems(item, `${span.span_id}-output-${eventIndex}`, orderBase + eventIndex));
                } catch {
                  // JSON truncated — recover text from the `text` attribute (stored separately by new backend).
                  const text = String(attrs["text"] ?? "");
                  const itemType = String(attrs["item.type"] ?? "");
                  const itemId = String(attrs["item.id"] ?? `${span.span_id}-output-${eventIndex}`);
                  if (itemType === "message" && text) {
                    items.push({
                      key: itemId,
                      role: "assistant",
                      label: "Assistant",
                      body: text,
                      raw: attrs,
                      order: orderBase + eventIndex,
                    });
                  }
                }
              } else if (event.name === "gen_ai.choice") {
                // gen_ai.choice carries the full completion text at end-of-loop (finish_reason: stop),
                // regardless of when in the stream the text was emitted. Use it to fill in the assistant
                // message if no message item was successfully parsed from gen_ai.output_item.done.
                const completion = String(attrs["gen_ai.completion"] ?? "");
                if (completion && !items.some((i) => i.role === "assistant")) {
                  items.push({
                    key: `${span.span_id}-choice`,
                    role: "assistant",
                    label: "Assistant",
                    body: completion,
                    raw: attrs,
                    order: orderBase + eventIndex,
                  });
                }
              }
            });
            return items;
          })
      : [];

  const visibleLiveDetailedActivity =
    selectedThreadId && waitingThreadId === selectedThreadId ? liveDetailedActivity : [];

  // If persisted activity has other items (input, tool calls) but no assistant message,
  // the OTEL content was likely truncated. Fall back to the finalized message from thread state,
  // which is always fetched from the DB via api.getThread().
  const persistedHasItems = persistedDetailedActivity.length > 0;
  const persistedHasAssistant = persistedDetailedActivity.some((item) => item.role === "assistant");
  const dbAssistantFallback: DetailedActivityItem[] =
    detailedMessagesEnabled &&
    persistedHasItems &&
    !persistedHasAssistant &&
    waitingThreadId !== selectedThreadId
      ? (() => {
          const msg = selectedThread?.messages
            .slice()
            .reverse()
            .find((m) => m.role === "assistant" && !m.metadata_json?.streaming);
          if (!msg?.content || msg.content === "(empty response)") return [];
          return [{
            key: `db-assistant-${msg.id}`,
            role: "assistant" as const,
            label: "Assistant",
            body: msg.content,
            raw: { id: msg.id, content: msg.content },
            order: Number.MAX_SAFE_INTEGER - 50,
          }];
        })()
      : [];

  const detailedActivity: DetailedActivityItem[] = detailedMessagesEnabled && selectedThread
    ? Array.from(new Map([...visibleLiveDetailedActivity, ...persistedDetailedActivity, ...dbAssistantFallback].map((item) => [item.key, item])).values())
        .sort((left, right) => (left.order ?? Number.MAX_SAFE_INTEGER) - (right.order ?? Number.MAX_SAFE_INTEGER))
    : [];

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [detailedActivity.length, selectedThread?.messages.length]);

  return (
    <div className="shell">
      <aside
        className={
          openMenu && (openMenu.section === "profiles" || openMenu.section === "servers" || openMenu.section === "threads")
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
                      <button className="menu-item" onClick={() => void onCloneProfile(profile.id)}>Clone</button>
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
            <optgroup label="GPT-4o">
              <option value="gpt-4o-mini">gpt-4o-mini</option>
              <option value="gpt-4o">gpt-4o</option>
            </optgroup>
            <optgroup label="GPT-4.1">
              <option value="gpt-4.1-mini">gpt-4.1-mini</option>
              <option value="gpt-4.1-nano">gpt-4.1-nano</option>
              <option value="gpt-4.1">gpt-4.1</option>
            </optgroup>
            <optgroup label="GPT-5 (MCP native)">
              <option value="gpt-5-mini">gpt-5-mini</option>
              <option value="gpt-5-chat-latest">gpt-5-chat-latest</option>
              <option value="gpt-5.4">gpt-5.4</option>
            </optgroup>
            <optgroup label="Reasoning (o-series)">
              <option value="o4-mini">o4-mini</option>
              <option value="o3-mini">o3-mini</option>
              <option value="o3">o3</option>
            </optgroup>
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
          <div className="toggle-row">
            <label htmlFor="profile-detailed-messages">Detailed Messages</label>
            <input
              id="profile-detailed-messages"
              type="checkbox"
              checked={Boolean(profileForm.ui_json?.detailed_messages_enabled)}
              onChange={(e) => onProfileUiField("detailed_messages_enabled", e.target.checked)}
            />
          </div>
          <p className="helper-text">Show the full LLM exchange for this profile instead of only the final assistant reply.</p>
          <div className="row-actions">
            <div className="action-row">
              <button onClick={onCreateProfile}>{selectedProfileId ? "Save Changes" : "Save Profile"}</button>
            </div>
          </div>
            </>
          )}
        </section>

        <section className={openMenu?.section === "threads" ? "panel has-open-menu" : "panel"}>
          <div className="panel-header">
            <h2>Threads</h2>
            <button className="icon-button" onClick={onCreateThread} aria-label="Add thread">+</button>
          </div>
          <div className="entity-list">
            {threads.map((thread) => {
              const isRunning = waitingThreadId === thread.id;
              const isRenaming = renamingThreadId === thread.id;
              return (
                <div key={thread.id} className={[
                  "entity-row",
                  thread.id === selectedThreadId ? "selected" : "",
                  isRunning ? "running" : "",
                ].filter(Boolean).join(" ")}>
                  {isRenaming ? (
                    <input
                      className="rename-input"
                      value={renamingTitle}
                      autoFocus
                      onChange={(e) => setRenamingTitle(e.target.value)}
                      onBlur={() => void onCommitRename(thread.id)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") void onCommitRename(thread.id);
                        if (e.key === "Escape") { setRenamingThreadId(""); setRenamingTitle(""); }
                      }}
                    />
                  ) : (
                    <button
                      className="entity-main"
                      onClick={() => {
                        setSelectedThreadId(thread.id);
                        closeMenu();
                        setStatusLine(`Selected ${thread.title}`);
                      }}
                    >
                      <strong>{thread.title}{isRunning && <span className="thread-running-dot" aria-label="Running" />}</strong>
                      <span>{isRunning ? "Running…" : new Date(thread.updated_at).toLocaleString()}</span>
                    </button>
                  )}
                  {!isRenaming && (
                    <div className="entity-actions">
                      <button
                        className="kebab-button secondary-button"
                        onClick={(e) => { e.stopPropagation(); toggleMenu("threads", thread.id); }}
                        aria-label={`Thread options for ${thread.title}`}
                      >⋮</button>
                      {openMenu?.section === "threads" && openMenu.id === thread.id && (
                        <div className="menu-popover">
                          <button className="menu-item" onClick={() => { setRenamingThreadId(thread.id); setRenamingTitle(thread.title); closeMenu(); }}>Rename</button>
                          <button className="menu-item danger" onClick={() => void onDeleteThread(thread.id)}>Delete</button>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
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
                    <strong>{server.name}</strong>
                    <span>{server.enabled ? "Enabled" : "Disabled"} · {server.approval_mode}</span>
                  </button>
                  <div className="entity-actions">
                    <button
                      className="kebab-button secondary-button"
                      onClick={(event) => {
                        event.stopPropagation();
                        toggleMenu("servers", server.id);
                      }}
                      aria-label={`MCP server options for ${server.name}`}
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
                        <button className="menu-item" onClick={() => void onCloneServer(server.id)}>Clone</button>
                        <button className="menu-item danger" onClick={() => void onDeleteServer(server.id)}>Delete</button>
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>

            <div className="mcp-editor">
              {isServerEditorOpen && <h3>{selectedServer ? selectedServer.name : "New MCP Server"}</h3>}
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
        <section className="chat-pane panel">
          <div className="panel-header">
            <h2>{selectedThread?.title ?? "No thread selected"}</h2>
          </div>
          <p className="helper-text">
            Profile: <strong>{selectedProfile?.name ?? "Draft profile"}</strong>
          </p>

          <div className="messages">
            {detailedMessagesEnabled
              ? detailedActivity.map((item) => (
                  <article key={item.key} className={`message exchange ${item.role}`}>
                    <header>{item.label}</header>
                    <pre>{item.body}</pre>
                    <details>
                      <summary>Raw</summary>
                      <pre>{JSON.stringify(item.raw, null, 2)}</pre>
                    </details>
                  </article>
                ))
              : selectedThread?.messages.map((message) => (
                  <article key={message.id} className={`message ${message.role}`}>
                    <header>{message.role}</header>
                    <pre>{message.content}</pre>
                  </article>
                ))}
            {waitingThreadId && waitingThreadId === selectedThreadId && (
              <div className="agent-working-indicator">
                <span className="thread-running-dot" />
                <span>Agent working…</span>
              </div>
            )}
            <div ref={messagesEndRef} />
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
          <div className="panel-header telemetry-header">
            <h2>Telemetry</h2>
            <div className="row-actions telemetry-actions">
              <span className="badge badge--otel-on" title="Telemetry is always stored in this app's local Postgres database.">Local<span className="otel-indicator otel-indicator--on" /></span>
              {(() => {
                if (!backendConfig) return null;
                const { langsmith_available, otel_available, export_mode } = backendConfig;
                const available: ("none" | "langsmith" | "otel")[] = [
                  "none",
                  ...(langsmith_available ? ["langsmith" as const] : []),
                  ...(otel_available ? ["otel" as const] : []),
                ];
                const nextMode = available[(available.indexOf(export_mode) + 1) % available.length];
                const isExporting = export_mode !== "none";

                const label = export_mode === "langsmith" ? "LangSmith"
                  : export_mode === "otel" ? "OTEL"
                  : "Export: Off";
                const title = export_mode === "langsmith"
                  ? `Exporting to LangSmith — click to change`
                  : export_mode === "otel"
                    ? `Exporting to ${backendConfig.otel_endpoint} — click to change`
                    : available.length > 1
                      ? "Export off — click to enable"
                      : "No export destinations configured. Set LANGSMITH_TRACING or OTEL_EXPORTER_OTLP_ENDPOINT in .env.";

                return (
                  <button
                    className={isExporting ? "badge badge--button badge--otel-on" : "badge badge--inactive badge--button"}
                    disabled={available.length === 1}
                    title={title}
                    onClick={() => {
                      api.setExportMode(nextMode).then((res) => {
                        setBackendConfig((c) => c ? { ...c, export_mode: res.export_mode } : c);
                      }).catch(() => {});
                    }}
                  >
                    {label}
                    {isExporting && <span className="otel-indicator otel-indicator--on" />}
                  </button>
                );
              })()}
              {backendConfig?.langsmith_available && backendConfig.langsmith_url && (
                <a
                  href={backendConfig.langsmith_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="badge badge--button"
                  title="Open LangSmith"
                >LangSmith ↗</a>
              )}
              {backendConfig?.otel_available && backendConfig.jaeger_ui_url && (
                <a
                  href={backendConfig.jaeger_ui_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="badge badge--button"
                  title="Open Jaeger UI"
                >Jaeger ↗</a>
              )}
              {telemetry && telemetry.spans.length > 0 && (
                <button className="secondary-button" onClick={() => setTraceModalOpen(true)}>
                  View Trace
                </button>
              )}
              {activeRunId && (
                <>
                  <button
                    className="secondary-button"
                    title="Download OTLP JSON export for this run."
                    onClick={() => void api.downloadOtelExport(activeRunId)}
                  >
                    Download
                  </button>
                  <button
                    className="secondary-button"
                    title="Reload the current run's telemetry from the backend."
                    onClick={() => void refreshTelemetry(activeRunId)}
                  >
                    Refresh
                  </button>
                </>
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
                {telemetry.spans.map((span) => (
                  <article key={span.id} className="timeline-card">
                    <header>
                      <strong>{span.name}</strong>
                      <span className="timeline-kind">{span.kind}</span>
                    </header>
                    <p>Status: {span.status_code}{span.status_message ? ` — ${span.status_message}` : ""}</p>
                    <p>Latency: {span.duration_ms ?? "n/a"} ms</p>
                    {span.events.length > 0 && (
                      <details>
                        <summary>Events ({span.events.length})</summary>
                        <pre>{JSON.stringify(span.events, null, 2)}</pre>
                      </details>
                    )}
                    {Object.keys(span.attributes).length > 0 && (
                      <details>
                        <summary>Attributes</summary>
                        <pre>{JSON.stringify(span.attributes, null, 2)}</pre>
                      </details>
                    )}
                    <details>
                      <summary>Raw</summary>
                      <pre>{JSON.stringify(span, null, 2)}</pre>
                    </details>
                  </article>
                ))}
              </div>

            </>
          ) : (
            <p className="muted">Run a thread to populate telemetry.</p>
          )}
        </section>
      </main>

      {traceModalOpen && telemetry && (() => {
        const root = buildSpanTree(telemetry.spans);
        const modelSpan = telemetry.spans.find((s) => s.name === "gen_ai.chat");
        const traceEvents = modelSpan ? parseTraceEvents(modelSpan) : [];
        const inTok = modelSpan?.attributes["gen_ai.usage.input_tokens"];
        const outTok = modelSpan?.attributes["gen_ai.usage.output_tokens"];
        const totTok = modelSpan?.attributes["gen_ai.usage.total_tokens"];
        const temp = modelSpan?.attributes["gen_ai.request.temperature"];
        return (
          <div className="modal-overlay" onClick={() => setTraceModalOpen(false)}>
            <div className="modal-panel" onClick={(e) => e.stopPropagation()}>
              <div className="modal-header">
                <h2>Trace — {telemetry.run.id.slice(0, 8)}</h2>
                <button className="secondary-button" onClick={() => setTraceModalOpen(false)}>✕</button>
              </div>
              <div className="modal-body">
                {root && (() => {
                  const rv = spanVisual(root.name, root.attributes, root.duration_ms);
                  return (
                    <div className="trace-tree">
                      <div className="trace-root-row">
                        <div className="trace-span-card" style={{ background: rv.bg }}>
                          <strong>{root.name}</strong>
                          <span>{rv.subtitle}</span>
                        </div>
                      </div>
                      {root.children.length > 0 && (
                        <div className="trace-children-row">
                          {root.children
                            .slice()
                            .sort((a, b) => a.start_time_unix_nano - b.start_time_unix_nano)
                            .map((child) => {
                              const cv = spanVisual(child.name, child.attributes, child.duration_ms);
                              return (
                                <div key={child.id} className="trace-span-card" style={{ background: cv.bg }}>
                                  <strong>{child.name}</strong>
                                  <span>{cv.subtitle}</span>
                                </div>
                              );
                            })}
                        </div>
                      )}
                    </div>
                  );
                })()}

                {traceEvents.length > 0 && (
                  <div className="trace-events">
                    <p className="trace-events-label">Events (in order)</p>
                    {traceEvents.map((row) => (
                      <div key={row.key} className="trace-event-card" style={{ borderLeftColor: row.bg }}>
                        <strong>{row.title}</strong>
                        {row.subtitle && <span>{row.subtitle}</span>}
                      </div>
                    ))}
                  </div>
                )}

                {(inTok != null || outTok != null) && (
                  <p className="trace-usage">
                    Token usage: {String(inTok ?? "?")} input + {String(outTok ?? "?")} output = {String(totTok ?? "?")} total
                    {temp != null ? ` · temp ${String(temp)}` : ""}
                  </p>
                )}
              </div>
            </div>
          </div>
        );
      })()}
    </div>
  );
}
