import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiJson } from "../api";
import type { AgentEvent, AgentSession, ContentBlock, SupplierOption } from "../agent/types";
import { useAgentStream } from "../agent/useAgentStream";

// ── Local view-model: a flattened transcript of "things to display" ──

type ChatItem =
  | { kind: "user"; text: string; key: string }
  | { kind: "assistant"; text: string; key: string; streaming?: boolean }
  | {
      kind: "tool";
      key: string;
      id: string;
      name: string;
      server: string;
      input: Record<string, unknown>;
      result?: { isError: boolean; text: string };
    };

function flattenSessionMessages(messages: AgentSession["messages"]): ChatItem[] {
  const items: ChatItem[] = [];
  let toolCallById: Record<string, { idx: number }> = {};
  messages.forEach((m, mi) => {
    m.content.forEach((block: ContentBlock, bi) => {
      const key = `${mi}.${bi}`;
      if (block.type === "text" && block.text) {
        items.push({
          kind: m.role === "user" ? "user" : "assistant",
          text: block.text,
          key,
        });
      } else if (block.type === "tool_use" || block.type === "mcp_tool_use") {
        toolCallById[block.id] = { idx: items.length };
        items.push({
          kind: "tool",
          key,
          id: block.id,
          name: block.name,
          server: ("server_name" in block ? block.server_name : "") || "",
          input: block.input || {},
        });
      } else if (block.type === "mcp_tool_result") {
        const target = toolCallById[block.tool_use_id];
        const text =
          (block.content || [])
            .map((c) => (c.type === "text" ? c.text || "" : ""))
            .join("\n")
            .trim() || "(sem conteúdo)";
        if (target && items[target.idx].kind === "tool") {
          (items[target.idx] as Extract<ChatItem, { kind: "tool" }>).result = {
            isError: !!block.is_error,
            text,
          };
        }
      }
    });
  });
  return items;
}

// ── Stored-locally session id (so we don't lose context on tab reload) ──
const SESSION_KEY = "moloni-agent-session-id";

function readStoredSessionId(): string | null {
  try {
    return sessionStorage.getItem(SESSION_KEY);
  } catch {
    return null;
  }
}

function writeStoredSessionId(id: string | null) {
  try {
    if (id) sessionStorage.setItem(SESSION_KEY, id);
    else sessionStorage.removeItem(SESSION_KEY);
  } catch {
    /* noop */
  }
}

// ── Page ──

export default function AgentPage() {
  const qc = useQueryClient();
  const [sessionId, setSessionId] = useState<string | null>(readStoredSessionId());

  // Live event buffer (text deltas + tool events arriving over SSE for the *current* in-flight turn)
  const [liveAssistant, setLiveAssistant] = useState("");
  const [liveTools, setLiveTools] = useState<Extract<ChatItem, { kind: "tool" }>[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [lastUsage, setLastUsage] = useState<{
    input?: number;
    output?: number;
    cache_read?: number;
    cache_create?: number;
  } | null>(null);

  // Suppliers dropdown
  const suppliersQ = useQuery({
    queryKey: ["agent", "suppliers"],
    queryFn: () => apiJson<SupplierOption[]>("/agent/suppliers"),
  });

  // Session bootstrap: fetch existing or create new
  const sessionQ = useQuery({
    queryKey: ["agent", "session", sessionId],
    queryFn: async () => {
      if (!sessionId) return null;
      try {
        return await apiJson<AgentSession>(`/agent/sessions/${sessionId}`);
      } catch (e) {
        // Session likely expired/restarted — drop and recreate
        writeStoredSessionId(null);
        setSessionId(null);
        throw e;
      }
    },
    enabled: !!sessionId,
    refetchOnWindowFocus: false,
  });

  const createSessionMut = useMutation({
    mutationFn: () =>
      apiJson<AgentSession>("/agent/sessions", {
        method: "POST",
        body: JSON.stringify({ supplier_slug: "auto", test_mode: true }),
      }),
    onSuccess: (s) => {
      writeStoredSessionId(s.session_id);
      setSessionId(s.session_id);
      qc.setQueryData(["agent", "session", s.session_id], s);
    },
  });

  // Auto-create session on first visit if there's none
  useEffect(() => {
    if (!sessionId && !createSessionMut.isPending) {
      createSessionMut.mutate();
    }
  }, [sessionId, createSessionMut]);

  const session = sessionQ.data ?? null;

  // Settings (supplier slug, test mode) — patch on change
  const patchSettingsMut = useMutation({
    mutationFn: (body: { supplier_slug?: string; test_mode?: boolean }) =>
      apiJson<AgentSession>(`/agent/sessions/${sessionId}/settings`, {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    onSuccess: (s) => qc.setQueryData(["agent", "session", sessionId], s),
  });

  // PDF upload
  const uploadMut = useMutation({
    mutationFn: async (file: File) => {
      const fd = new FormData();
      fd.append("file", file);
      const res = await fetch(`/api/agent/sessions/${sessionId}/upload`, {
        method: "POST",
        credentials: "include",
        body: fd,
      });
      if (!res.ok) throw new Error(await res.text().catch(() => res.statusText));
      return (await res.json()) as AgentSession;
    },
    onSuccess: (s) => {
      qc.setQueryData(["agent", "session", sessionId], s);
      setLiveAssistant("");
      setLiveTools([]);
      setError(null);
    },
    onError: (e: Error) => setError(e.message),
  });

  const { send, streaming } = useAgentStream(sessionId);

  const [draft, setDraft] = useState("");
  const handleSend = useCallback(async () => {
    const text = draft.trim();
    if (!text || !sessionId || streaming) return;
    setDraft("");
    setError(null);
    setLiveAssistant("");
    setLiveTools([]);

    // Optimistically refetch to add the user message to the UI right away
    // (the runner persists it before streaming completes; we'll refetch at end)

    try {
      await send(text, (ev: AgentEvent) => {
        switch (ev.type) {
          case "text_delta":
            setLiveAssistant((prev) => prev + ev.text);
            break;
          case "tool_use":
            setLiveTools((prev) => [
              ...prev,
              {
                kind: "tool",
                key: `live-${ev.id}`,
                id: ev.id,
                name: ev.name,
                server: ev.server_name,
                input: ev.input,
              },
            ]);
            break;
          case "tool_result":
            setLiveTools((prev) =>
              prev.map((t) =>
                t.id === ev.tool_use_id
                  ? {
                      ...t,
                      result: {
                        isError: ev.is_error,
                        text:
                          (ev.content || [])
                            .map((c) => (c.type === "text" ? c.text || "" : ""))
                            .join("\n")
                            .trim() || "(sem conteúdo)",
                      },
                    }
                  : t,
              ),
            );
            break;
          case "message_complete":
            setLastUsage({
              input: ev.usage.input_tokens,
              output: ev.usage.output_tokens,
              cache_read: ev.usage.cache_read_input_tokens,
              cache_create: ev.usage.cache_creation_input_tokens,
            });
            break;
          case "error":
            setError(ev.message);
            break;
          case "done":
            // refetch the session so its persisted history replaces our live buffer
            qc.invalidateQueries({ queryKey: ["agent", "session", sessionId] });
            setLiveAssistant("");
            setLiveTools([]);
            break;
        }
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [draft, sessionId, streaming, send, qc]);

  // ── Render ──

  const transcript = useMemo(
    () => (session ? flattenSessionMessages(session.messages) : []),
    [session],
  );

  if (!sessionId || !session) {
    return (
      <div>
        <h1>Agente</h1>
        <p className="muted">A iniciar sessão…</p>
        {createSessionMut.error ? (
          <p className="error">{(createSessionMut.error as Error).message}</p>
        ) : null}
      </div>
    );
  }

  return (
    <div>
      <h1>Agente · Faturas de fornecedor</h1>

      <div className="card no-print">
        <div className="form-grid" style={{ gridTemplateColumns: "1fr 1fr auto" }}>
          <label>
            Fornecedor
            <select
              value={session.supplier_slug}
              onChange={(e) =>
                patchSettingsMut.mutate({ supplier_slug: e.target.value })
              }
            >
              {(suppliersQ.data ?? []).map((s) => (
                <option key={s.slug} value={s.slug}>
                  {s.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Modo
            <select
              value={session.test_mode ? "test" : "live"}
              onChange={(e) =>
                patchSettingsMut.mutate({ test_mode: e.target.value === "test" })
              }
            >
              <option value="test">Modo de teste (não escreve no Moloni)</option>
              <option value="live">Modo real (cria produtos e faturas)</option>
            </select>
          </label>
          <div style={{ alignSelf: "end" }}>
            <button
              type="button"
              className="btn"
              onClick={async () => {
                await apiJson(`/agent/sessions/${sessionId}`, { method: "DELETE" });
                writeStoredSessionId(null);
                setSessionId(null);
                setLiveAssistant("");
                setLiveTools([]);
                setError(null);
                createSessionMut.mutate();
              }}
              disabled={streaming}
            >
              Nova sessão
            </button>
          </div>
        </div>
        {!session.test_mode ? (
          <p className="muted" style={{ marginTop: "0.5rem" }}>
            ⚠ Modo real: o agente vai criar produtos e faturas no Moloni sem perguntar.
          </p>
        ) : null}
      </div>

      <Dropzone
        hasPdf={session.has_pdf}
        filename={session.pdf_filename}
        uploading={uploadMut.isPending}
        onFile={(f) => uploadMut.mutate(f)}
      />

      <div className="card">
        <h2 style={{ fontSize: "1.05rem" }}>Conversa</h2>
        {transcript.length === 0 && !liveAssistant && liveTools.length === 0 ? (
          <p className="muted">
            Carrega um PDF e escreve uma instrução, por exemplo:{" "}
            <em>"Processa esta fatura."</em>
          </p>
        ) : null}
        <div className="agent-thread">
          {transcript.map((it) => (
            <ChatItemView key={it.key} item={it} />
          ))}
          {liveTools.map((t) => (
            <ChatItemView key={t.key} item={t} />
          ))}
          {liveAssistant ? (
            <ChatItemView
              key="live-text"
              item={{ kind: "assistant", text: liveAssistant, key: "live-text", streaming: true }}
            />
          ) : null}
        </div>

        {error ? <p className="error">{error}</p> : null}

        <div style={{ marginTop: "0.75rem", display: "flex", gap: "0.5rem" }}>
          <textarea
            rows={2}
            value={draft}
            placeholder={
              session.has_pdf
                ? "Instrução para o agente…"
                : "Carrega um PDF primeiro, depois escreve aqui."
            }
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                handleSend();
              }
            }}
            style={{ flex: 1, resize: "vertical", minHeight: "2.5rem", padding: "0.5rem" }}
            disabled={streaming || !session.has_pdf}
          />
          <button
            type="button"
            className="btn primary"
            onClick={handleSend}
            disabled={streaming || !draft.trim() || !session.has_pdf}
            style={{ alignSelf: "flex-start" }}
          >
            {streaming ? "A pensar…" : "Enviar"}
          </button>
        </div>

        {lastUsage ? (
          <p className="muted" style={{ marginTop: "0.4rem", fontSize: "0.75rem" }}>
            Última resposta: {lastUsage.input ?? 0} in · {lastUsage.output ?? 0} out · cache hit{" "}
            {lastUsage.cache_read ?? 0}
          </p>
        ) : null}
      </div>
    </div>
  );
}

// ── Subcomponents ──

function Dropzone(props: {
  hasPdf: boolean;
  filename: string | null;
  uploading: boolean;
  onFile: (f: File) => void;
}) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const handleFiles = (files: FileList | null) => {
    if (!files || files.length === 0) return;
    const f = Array.from(files).find((x) => x.type === "application/pdf");
    if (f) props.onFile(f);
  };
  return (
    <div
      className={`agent-dropzone${dragOver ? " is-dragover" : ""}`}
      onDragOver={(e) => {
        e.preventDefault();
        setDragOver(true);
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragOver(false);
        handleFiles(e.dataTransfer.files);
      }}
      onClick={() => inputRef.current?.click()}
      role="button"
      tabIndex={0}
    >
      <input
        ref={inputRef}
        type="file"
        accept="application/pdf"
        style={{ display: "none" }}
        onChange={(e) => handleFiles(e.target.files)}
      />
      {props.uploading ? (
        <p className="muted">A carregar…</p>
      ) : props.hasPdf ? (
        <>
          <p style={{ margin: 0 }}>
            <strong>📄 {props.filename || "fatura.pdf"}</strong>
          </p>
          <p className="muted" style={{ margin: "0.25rem 0 0" }}>
            Clica ou larga outro PDF para substituir (a conversa será reiniciada).
          </p>
        </>
      ) : (
        <>
          <p style={{ margin: 0, fontSize: "1rem" }}>📥 Larga aqui o PDF da fatura</p>
          <p className="muted" style={{ margin: "0.25rem 0 0" }}>
            ou clica para escolher um ficheiro (.pdf, máx. 25 MB)
          </p>
        </>
      )}
    </div>
  );
}

function ChatItemView({ item }: { item: ChatItem }) {
  if (item.kind === "user") {
    return (
      <div className="agent-msg agent-msg-user">
        <div className="agent-msg-role">Tu</div>
        <div className="agent-msg-body">{item.text}</div>
      </div>
    );
  }
  if (item.kind === "assistant") {
    return (
      <div className={`agent-msg agent-msg-assistant${item.streaming ? " is-streaming" : ""}`}>
        <div className="agent-msg-role">Agente</div>
        <div className="agent-msg-body" style={{ whiteSpace: "pre-wrap" }}>
          {item.text}
          {item.streaming ? <span className="agent-cursor">▍</span> : null}
        </div>
      </div>
    );
  }
  // tool
  return (
    <details className={`agent-tool${item.result?.isError ? " is-error" : ""}`}>
      <summary>
        <code>{item.name}</code>
        <span className="muted" style={{ marginLeft: "0.5rem" }}>
          {item.server ? `${item.server} · ` : ""}
          {item.result ? (item.result.isError ? "erro" : "ok") : "a executar…"}
        </span>
      </summary>
      <div className="agent-tool-body">
        <div>
          <div className="muted" style={{ fontSize: "0.7rem", textTransform: "uppercase" }}>
            input
          </div>
          <pre>{JSON.stringify(item.input, null, 2)}</pre>
        </div>
        {item.result ? (
          <div>
            <div className="muted" style={{ fontSize: "0.7rem", textTransform: "uppercase" }}>
              {item.result.isError ? "erro" : "resultado"}
            </div>
            <pre>{item.result.text}</pre>
          </div>
        ) : null}
      </div>
    </details>
  );
}
