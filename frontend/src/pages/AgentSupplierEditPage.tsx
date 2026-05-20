import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { apiJson } from "../api";

const SLUG_RE = /^[a-z0-9_-]+$/;

export default function AgentSupplierEditPage() {
  const { slug } = useParams<{ slug: string }>();
  const isNew = slug === "new";
  const navigate = useNavigate();
  const qc = useQueryClient();

  const [newSlug, setNewSlug] = useState("");
  const [content, setContent] = useState("");
  const [slugError, setSlugError] = useState("");

  const q = useQuery({
    queryKey: ["agent-supplier-rules", slug],
    queryFn: () => apiJson<{ slug: string; content: string }>(`/agent/suppliers/${slug}/rules`),
    enabled: !isNew,
  });

  useEffect(() => {
    if (q.data?.content !== undefined) setContent(q.data.content);
  }, [q.data?.content]);

  const saveMut = useMutation({
    mutationFn: ({ targetSlug, body }: { targetSlug: string; body: string }) =>
      apiJson(`/agent/suppliers/${targetSlug}/rules`, {
        method: "PUT",
        body: JSON.stringify({ content: body }),
      }),
    onSuccess: (_data, { targetSlug }) => {
      qc.invalidateQueries({ queryKey: ["agent-suppliers"] });
      qc.invalidateQueries({ queryKey: ["agent-supplier-rules", targetSlug] });
      if (isNew) navigate(`/agent/suppliers/${targetSlug}`);
    },
  });

  const deleteMut = useMutation({
    mutationFn: () => apiJson(`/agent/suppliers/${slug}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["agent-suppliers"] });
      navigate("/agent/suppliers");
    },
  });

  function handleSave() {
    const targetSlug = isNew ? newSlug : slug!;
    if (!SLUG_RE.test(targetSlug)) {
      setSlugError("Slug inválido: apenas letras minúsculas, dígitos, _ e - são permitidos.");
      return;
    }
    setSlugError("");
    saveMut.mutate({ targetSlug, body: content });
  }

  function handleDelete() {
    if (!window.confirm(`Eliminar o fornecedor "${slug}"? Esta acção não pode ser desfeita.`)) return;
    deleteMut.mutate();
  }

  const isBusy = saveMut.isPending || deleteMut.isPending;

  return (
    <div className="supplier-rules-page">
      <Link to="/agent/suppliers" className="supplier-rules-back">← Fornecedores IA</Link>

      <div className="supplier-rules-header">
        <h1>{isNew ? "Novo fornecedor" : slug}</h1>
        <span className="supplier-rules-stamp">{isNew ? "Esboço" : "Regras"}</span>
      </div>

      {q.isLoading && <p className="muted">A carregar…</p>}
      {q.error && <p className="status error">{(q.error as Error).message}</p>}
      {saveMut.isSuccess && !isNew && <p className="status ok">Gravado.</p>}
      {saveMut.error && <p className="status error">{(saveMut.error as Error).message}</p>}
      {deleteMut.error && <p className="status error">{(deleteMut.error as Error).message}</p>}

      <div className="card">
        {isNew && (
          <label className="supplier-rules-field">
            <span className="supplier-rules-field-label">Slug</span>
            <input
              className="supplier-rules-input"
              value={newSlug}
              onChange={(e) => { setNewSlug(e.target.value); setSlugError(""); }}
              placeholder="ex: nova_marca"
            />
            {slugError && <span className="supplier-rules-field-hint is-error">{slugError}</span>}
            <span className="supplier-rules-field-hint">Apenas letras minúsculas, dígitos, _ e -</span>
          </label>
        )}

        <label className="supplier-rules-field">
          <span className="supplier-rules-field-label">Regras (Markdown)</span>
          <textarea
            className="supplier-rules-editor"
            value={content}
            onChange={(e) => setContent(e.target.value)}
          />
        </label>

        <div className="supplier-rules-actions">
          <button type="button" className="btn primary" disabled={isBusy} onClick={handleSave}>
            {saveMut.isPending ? "A guardar…" : "Guardar"}
          </button>
          {!isNew && (
            <button
              type="button"
              className="btn danger"
              disabled={isBusy}
              onClick={handleDelete}
            >
              {deleteMut.isPending ? "A eliminar…" : "Eliminar"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
