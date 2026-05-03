import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { apiJson } from "../api";
import type { AgentSession, ApplyEvent, ExtractedLine, SupplierOption } from "../agent/types";
import { useSSEPost } from "../agent/useAgentStream";

// ── session storage ──────────────────────────────────────────────────────────

const SESSION_KEY = "moloni-agent-session-id-v2";

function readStoredId(): string | null {
  try { return sessionStorage.getItem(SESSION_KEY); } catch { return null; }
}
function writeStoredId(id: string | null) {
  try {
    if (id) sessionStorage.setItem(SESSION_KEY, id);
    else sessionStorage.removeItem(SESSION_KEY);
  } catch { /* noop */ }
}

// ── page ─────────────────────────────────────────────────────────────────────

export default function AgentPage() {
  const qc = useQueryClient();
  const [sessionId, setSessionId] = useState<string | null>(readStoredId);

  // Local editable copy of lines (kept in sync from session.extracted)
  const [editLines, setEditLines] = useState<ExtractedLine[]>([]);
  const lastExtractedRef = useRef<string | null>(null); // prevent overwriting user edits

  // Apply stream state
  const [applyLog, setApplyLog] = useState<ApplyEvent[]>([]);
  const [applyDone, setApplyDone] = useState(false);
  const [applyError, setApplyError] = useState<string | null>(null);

  const { stream: streamApply, streaming: applying } = useSSEPost<ApplyEvent>();

  // ── suppliers ──
  const suppliersQ = useQuery({
    queryKey: ["agent", "suppliers"],
    queryFn: () => apiJson<SupplierOption[]>("/agent/suppliers"),
  });

  // ── session ──
  const sessionQ = useQuery({
    queryKey: ["agent", "session", sessionId],
    queryFn: async () => {
      if (!sessionId) return null;
      try {
        return await apiJson<AgentSession>(`/agent/sessions/${sessionId}`);
      } catch {
        writeStoredId(null);
        setSessionId(null);
        return null;
      }
    },
    enabled: !!sessionId,
    refetchOnWindowFocus: false,
  });

  const session = sessionQ.data ?? null;

  // Sync editLines when a new extraction arrives
  useEffect(() => {
    const lines = session?.extracted?.lines;
    if (!lines) return;
    const key = JSON.stringify(lines);
    if (key === lastExtractedRef.current) return; // already loaded
    lastExtractedRef.current = key;
    setEditLines(lines.map((l) => ({ ...l })));
    // Reset apply state for a fresh extraction
    setApplyLog([]);
    setApplyDone(false);
    setApplyError(null);
  }, [session?.extracted?.lines]);

  // ── create session ──
  const createSessionMut = useMutation({
    mutationFn: (supplierSlug?: string) =>
      apiJson<AgentSession>("/agent/sessions", {
        method: "POST",
        body: JSON.stringify({ supplier_slug: supplierSlug ?? "american_vintage" }),
      }),
    onSuccess: (s) => {
      writeStoredId(s.session_id);
      setSessionId(s.session_id);
      qc.setQueryData(["agent", "session", s.session_id], s);
    },
  });

  useEffect(() => {
    if (!sessionId && !createSessionMut.isPending) {
      createSessionMut.mutate(undefined);
    }
  }, [sessionId, createSessionMut]);

  // ── upload PDF ──
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
      lastExtractedRef.current = null; // allow sync on next extraction
      setEditLines([]);
      setApplyLog([]);
      setApplyDone(false);
      setApplyError(null);
      qc.setQueryData(["agent", "session", sessionId], s);
    },
  });

  // ── extract ──
  const extractMut = useMutation({
    mutationFn: () =>
      apiJson<AgentSession>(`/agent/sessions/${sessionId}/extract`, { method: "POST" }),
    onSuccess: (s) => {
      qc.setQueryData(["agent", "session", sessionId], s);
    },
  });

  // ── apply ──
  const handleApply = useCallback(async () => {
    if (!sessionId || applying) return;
    setApplyLog([]);
    setApplyDone(false);
    setApplyError(null);

    // Patch edits to backend first, then stream apply
    try {
      await apiJson(`/agent/sessions/${sessionId}/data`, {
        method: "PATCH",
        body: JSON.stringify({ lines: editLines }),
      });
    } catch (e) {
      setApplyError(`Erro ao guardar edições: ${e instanceof Error ? e.message : String(e)}`);
      return;
    }

    try {
      await streamApply(`/api/agent/sessions/${sessionId}/apply`, (ev) => {
        setApplyLog((prev) => [...prev, ev]);
        if (ev.type === "done") setApplyDone(true);
        if (ev.type === "invoice_error") setApplyError(ev.message);
      });
    } catch (e) {
      setApplyError(e instanceof Error ? e.message : String(e));
    }
  }, [sessionId, applying, editLines, streamApply]);

  // ── line edits ──
  function updateLine(idx: number, field: keyof ExtractedLine, value: string | number) {
    setEditLines((prev) =>
      prev.map((l, i) => (i === idx ? { ...l, [field]: value } : l)),
    );
  }
  function removeLine(idx: number) {
    setEditLines((prev) => prev.filter((_, i) => i !== idx));
  }

  // ── render ──

  if (!sessionId || (!session && sessionQ.isLoading)) {
    return (
      <div>
        <h1>Agente</h1>
        <p className="muted">A iniciar sessão…</p>
      </div>
    );
  }

  const extracted = session?.extracted ?? null;
  const hasPdf = session?.has_pdf ?? false;
  const busy = applying || extractMut.isPending || uploadMut.isPending;

  return (
    <div className="agent-page">
      <h1>Agente · Faturas de fornecedor</h1>

      {/* ── Card 1: Upload ── */}
      <div className="card">
        <div className="agent-upload-row">
          <label className="agent-upload-label">
            Fornecedor
            <select
              value={session?.supplier_slug ?? "american_vintage"}
              onChange={(e) => {
                // Create a fresh session with the new supplier
                writeStoredId(null);
                setSessionId(null);
                createSessionMut.mutate(e.target.value);
              }}
              disabled={busy}
            >
              {(suppliersQ.data ?? [{ slug: "american_vintage", label: "American Vintage" }]).map(
                (s) => (
                  <option key={s.slug} value={s.slug}>
                    {s.label}
                  </option>
                ),
              )}
            </select>
          </label>

          <Dropzone
            hasPdf={hasPdf}
            filename={session?.pdf_filename ?? null}
            uploading={uploadMut.isPending}
            disabled={busy}
            onFile={(f) => uploadMut.mutate(f)}
          />

          {hasPdf && !extracted && (
            <button
              type="button"
              className="btn primary"
              onClick={() => extractMut.mutate()}
              disabled={busy}
            >
              {extractMut.isPending ? <><Spinner /> A extrair…</> : "▶ Extrair fatura"}
            </button>
          )}

          {hasPdf && extracted && !applying && (
            <span className="agent-extracted-badge">✓ Extraído</span>
          )}
        </div>

        {uploadMut.error && (
          <p className="error" style={{ marginTop: "0.5rem" }}>
            {(uploadMut.error as Error).message}
          </p>
        )}
        {extractMut.error && (
          <p className="error" style={{ marginTop: "0.5rem" }}>
            {(extractMut.error as Error).message}
          </p>
        )}

        {extractMut.isPending && (
          <div className="agent-phase" style={{ marginTop: "0.75rem" }}>
            <Spinner />
            <span className="agent-phase-label">A ler PDF com Claude…</span>
            <span className="agent-phase-detail muted">pode demorar 10–20 s</span>
          </div>
        )}
      </div>

      {/* ── Card 2: Review table ── */}
      {extracted && (
        <div className="card">
          <div className="agent-review-header">
            <div>
              <h2 style={{ margin: 0, fontSize: "1.05rem" }}>Revisão da fatura</h2>
              <p className="muted" style={{ margin: "0.2rem 0 0" }}>
                {extracted.supplier.name} · Fatura {extracted.header.invoice_number} ·{" "}
                {extracted.header.date} · {extracted.header.grand_total.toFixed(2)} EUR
              </p>
            </div>
            <button
              type="button"
              className="btn primary"
              onClick={handleApply}
              disabled={busy || editLines.length === 0}
            >
              {applying ? <><Spinner /> A aplicar…</> : "Aplicar no Moloni"}
            </button>
          </div>

          {extracted.reconciliation.warnings.length > 0 && (
            <div className="agent-warnings">
              {extracted.reconciliation.warnings.map((w, i) => (
                <p key={i} className="agent-warning">
                  ⚠ {w}
                </p>
              ))}
            </div>
          )}

          <div className="agent-table-wrap">
            <table className="agent-review-table">
              <thead>
                <tr>
                  <th>Ref.</th>
                  <th>Nome</th>
                  <th style={{ textAlign: "right" }}>Qtd</th>
                  <th style={{ textAlign: "right" }}>Custo unit.</th>
                  <th style={{ textAlign: "right" }}>PVP c/IVA</th>
                  <th>Subcategoria</th>
                  <th>Cor</th>
                  <th>Tam</th>
                  <th style={{ textAlign: "right" }}>Total</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {editLines.map((line, idx) => (
                  <tr key={idx}>
                    <td>
                      <input
                        className="agent-cell-input"
                        value={line.reference}
                        onChange={(e) => updateLine(idx, "reference", e.target.value)}
                      />
                    </td>
                    <td>
                      <input
                        className="agent-cell-input agent-cell-name"
                        value={line.name}
                        onChange={(e) => updateLine(idx, "name", e.target.value)}
                      />
                    </td>
                    <td>
                      <input
                        className="agent-cell-input agent-cell-num"
                        type="number"
                        min={1}
                        step={1}
                        value={line.qty}
                        onChange={(e) => updateLine(idx, "qty", Number(e.target.value))}
                      />
                    </td>
                    <td>
                      <input
                        className="agent-cell-input agent-cell-num"
                        type="number"
                        step={0.01}
                        value={line.unit_cost}
                        onChange={(e) => updateLine(idx, "unit_cost", Number(e.target.value))}
                      />
                    </td>
                    <td>
                      <input
                        className="agent-cell-input agent-cell-num"
                        type="number"
                        step={0.01}
                        value={line.pvp_with_vat}
                        onChange={(e) => updateLine(idx, "pvp_with_vat", Number(e.target.value))}
                      />
                    </td>
                    <td>
                      <input
                        className="agent-cell-input"
                        value={line.subcategory_name}
                        onChange={(e) => updateLine(idx, "subcategory_name", e.target.value)}
                      />
                    </td>
                    <td className="agent-cell-ro">{line.color || "—"}</td>
                    <td className="agent-cell-ro">{line.size || "—"}</td>
                    <td className="agent-cell-ro" style={{ textAlign: "right" }}>
                      {(line.qty * line.unit_cost).toFixed(2)}
                    </td>
                    <td>
                      <button
                        type="button"
                        className="agent-remove-btn"
                        title="Remover linha"
                        onClick={() => removeLine(idx)}
                        disabled={applying}
                      >
                        ×
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr>
                  <td colSpan={8} style={{ textAlign: "right", fontWeight: 600, paddingTop: "0.5rem" }}>
                    Subtotal (linhas):
                  </td>
                  <td style={{ textAlign: "right", fontWeight: 600, paddingTop: "0.5rem" }}>
                    {editLines.reduce((s, l) => s + l.qty * l.unit_cost, 0).toFixed(2)}
                  </td>
                  <td />
                </tr>
                <tr>
                  <td colSpan={8} style={{ textAlign: "right", color: "var(--muted)", fontSize: "0.82rem" }}>
                    Subtotal fatura (Claude):
                  </td>
                  <td
                    style={{
                      textAlign: "right",
                      color: extracted.reconciliation.matches_invoice_total
                        ? "var(--muted)"
                        : "var(--danger)",
                      fontSize: "0.82rem",
                    }}
                  >
                    {extracted.header.subtotal.toFixed(2)}
                    {!extracted.reconciliation.matches_invoice_total && " ⚠"}
                  </td>
                  <td />
                </tr>
              </tfoot>
            </table>
          </div>

          <p className="muted" style={{ marginTop: "0.5rem", fontSize: "0.8rem" }}>
            {editLines.length} linha(s) · edita diretamente na tabela antes de aplicar
          </p>
        </div>
      )}

      {/* ── Card 3: Apply progress ── */}
      {(applyLog.length > 0 || applying) && (
        <div className="card">
          <h2 style={{ fontSize: "1.05rem", marginBottom: "0.75rem" }}>
            {applyDone
              ? applyError
                ? "Aplicação concluída com erros"
                : "Aplicação concluída"
              : "A aplicar no Moloni…"}
          </h2>

          {applyError && <p className="error">{applyError}</p>}

          <div className="agent-apply-log">
            {applyLog.map((ev, i) => (
              <ApplyEventRow key={i} event={ev} />
            ))}
            {applying && !applyDone && (
              <div className="agent-apply-row agent-apply-pending">
                <Spinner /> <span>A processar…</span>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Dropzone ─────────────────────────────────────────────────────────────────

function Dropzone(props: {
  hasPdf: boolean;
  filename: string | null;
  uploading: boolean;
  disabled: boolean;
  onFile: (f: File) => void;
}) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [dragOver, setDragOver] = useState(false);

  const handleFiles = (files: FileList | null) => {
    const f = files && Array.from(files).find((x) => x.type === "application/pdf");
    if (f) props.onFile(f);
  };

  return (
    <div
      className={`agent-dropzone agent-dropzone-inline${dragOver ? " is-dragover" : ""}${props.disabled ? " is-disabled" : ""}`}
      onDragOver={(e) => { e.preventDefault(); if (!props.disabled) setDragOver(true); }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e) => { e.preventDefault(); setDragOver(false); if (!props.disabled) handleFiles(e.dataTransfer.files); }}
      onClick={() => !props.disabled && inputRef.current?.click()}
      role="button"
      tabIndex={props.disabled ? -1 : 0}
    >
      <input
        ref={inputRef}
        type="file"
        accept="application/pdf"
        style={{ display: "none" }}
        onChange={(e) => handleFiles(e.target.files)}
      />
      {props.uploading ? (
        <span className="muted"><Spinner /> A carregar…</span>
      ) : props.hasPdf ? (
        <span>📄 <strong>{props.filename || "fatura.pdf"}</strong> <span className="muted">(clica para substituir)</span></span>
      ) : (
        <span>📥 Larga ou clica para carregar o PDF da fatura</span>
      )}
    </div>
  );
}

// ── Apply event row ───────────────────────────────────────────────────────────

function ApplyEventRow({ event }: { event: ApplyEvent }) {
  switch (event.type) {
    case "started":
      return <div className="agent-apply-row">🚀 Início da aplicação</div>;
    case "subcategory_lookup":
      return (
        <div className="agent-apply-row">
          🗂 Subcategorias a criar:{" "}
          {event.needs_creation.length === 0
            ? "nenhuma"
            : event.needs_creation.join(", ")}
        </div>
      );
    case "subcategory_created":
      return (
        <div className="agent-apply-row agent-apply-ok">
          ✅ Subcategoria criada: <strong>{event.name}</strong> (id {event.category_id})
        </div>
      );
    case "line_matched":
      return (
        <div className="agent-apply-row agent-apply-ok">
          ✓ <code>{event.reference}</code> → produto existente #{event.product_id}
        </div>
      );
    case "line_creating":
      return (
        <div className="agent-apply-row">
          ＋ <code>{event.reference}</code> → a criar produto…
        </div>
      );
    case "line_created":
      return (
        <div className="agent-apply-row agent-apply-ok">
          ✅ <code>{event.reference}</code> → produto criado #{event.product_id}
        </div>
      );
    case "line_error":
      return (
        <div className="agent-apply-row agent-apply-err">
          ❌ <code>{event.reference}</code>: {event.message}
        </div>
      );
    case "invoice_creating":
      return <div className="agent-apply-row">📄 A criar fatura de fornecedor…</div>;
    case "invoice_created":
      return (
        <div className="agent-apply-row agent-apply-ok" style={{ fontWeight: 600 }}>
          🎉 Fatura criada no Moloni — documento #{event.document_id}
        </div>
      );
    case "invoice_error":
      return (
        <div className="agent-apply-row agent-apply-err">
          ❌ Erro na fatura: {event.message}
        </div>
      );
    case "done":
      return <div className="agent-apply-row agent-apply-done">— Concluído —</div>;
    default:
      return null;
  }
}

// ── Spinner ───────────────────────────────────────────────────────────────────

function Spinner() {
  return <span className="agent-spinner" aria-hidden="true" />;
}
