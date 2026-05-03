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
    <div>
      <p><Link to="/agent/suppliers">← Fornecedores IA</Link></p>
      <h1>{isNew ? "Novo Fornecedor" : slug}</h1>

      {q.isLoading && <p>A carregar…</p>}
      {q.error && <p className="error">{(q.error as Error).message}</p>}
      {saveMut.isSuccess && !isNew && <p className="success">Gravado.</p>}
      {saveMut.error && <p className="error">{(saveMut.error as Error).message}</p>}
      {deleteMut.error && <p className="error">{(deleteMut.error as Error).message}</p>}

      <div className="card">
        {isNew && (
          <label style={{ display: "block", marginBottom: "1rem" }}>
            Slug
            <input
              value={newSlug}
              onChange={(e) => { setNewSlug(e.target.value); setSlugError(""); }}
              placeholder="ex: nova_marca"
              style={{ display: "block", width: "100%", marginTop: "0.25rem" }}
            />
            {slugError && <span className="error" style={{ fontSize: "0.8rem" }}>{slugError}</span>}
            <span className="muted" style={{ fontSize: "0.8rem" }}>Apenas letras minúsculas, dígitos, _ e -</span>
          </label>
        )}

        <label style={{ display: "block" }}>
          Regras (Markdown)
          <textarea
            value={content}
            onChange={(e) => setContent(e.target.value)}
            style={{
              display: "block",
              width: "100%",
              minHeight: "70vh",
              marginTop: "0.25rem",
              fontFamily: "monospace",
              fontSize: "0.85rem",
              resize: "vertical",
              boxSizing: "border-box",
            }}
          />
        </label>

        <div className="row-actions" style={{ marginTop: "1rem" }}>
          <button type="button" className="btn primary" disabled={isBusy} onClick={handleSave}>
            {saveMut.isPending ? "A guardar…" : "Guardar"}
          </button>
          {!isNew && (
            <button
              type="button"
              className="btn"
              style={{ color: "var(--danger)" }}
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
