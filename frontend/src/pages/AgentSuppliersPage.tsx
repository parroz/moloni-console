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
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: "1rem", marginBottom: "1rem" }}>
        <h1 style={{ margin: 0 }}>Fornecedores IA</h1>
        <Link to="/agent/suppliers/new">
          <button type="button" className="btn primary">Novo Fornecedor</button>
        </Link>
      </div>

      {q.isLoading && <p>A carregar…</p>}
      {q.error && <p className="error">{(q.error as Error).message}</p>}

      {q.data && (
        q.data.length === 0 ? (
          <p className="muted">Nenhum fornecedor configurado.</p>
        ) : (
          <div className="card" style={{ padding: 0 }}>
            <table className="data">
              <thead>
                <tr>
                  <th>Slug</th>
                  <th>Nome</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {q.data.map((s) => (
                  <tr key={s.slug}>
                    <td><code>{s.slug}</code></td>
                    <td>{s.label}</td>
                    <td>
                      <div className="row-actions">
                        <Link to={`/agent/suppliers/${s.slug}`}>
                          <button type="button" className="btn">Editar</button>
                        </Link>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      )}
    </div>
  );
}
