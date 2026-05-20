import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { apiJson } from "../api";

type SupplierOption = { slug: string; label: string };

export default function AgentSuppliersPage() {
  const q = useQuery({
    queryKey: ["agent-suppliers"],
    queryFn: () => apiJson<SupplierOption[]>("/agent/suppliers"),
  });

  return (
    <div className="supplier-rules-page">
      <div className="supplier-rules-header">
        <h1>Fornecedores IA</h1>
        <span className="supplier-rules-stamp">Regras / Markdown</span>
        <span className="header-spacer" />
        <Link to="/agent/suppliers/new" className="btn primary">
          + Novo fornecedor
        </Link>
      </div>

      {q.isLoading && <p className="muted">A carregar…</p>}
      {q.error && <p className="status error">{(q.error as Error).message}</p>}

      {q.data && (
        q.data.length === 0 ? (
          <p className="supplier-rules-empty">Nenhum fornecedor configurado</p>
        ) : (
          <ol className="supplier-rules-list">
            {q.data.map((s, i) => (
              <li key={s.slug}>
                <span className="supplier-rules-index">{String(i + 1).padStart(2, "0")}</span>
                <span className="supplier-rules-slug">{s.slug}</span>
                <span className="supplier-rules-name">{s.label}</span>
                <Link to={`/agent/suppliers/${s.slug}`} className="btn">
                  Editar →
                </Link>
              </li>
            ))}
          </ol>
        )
      )}
    </div>
  );
}
