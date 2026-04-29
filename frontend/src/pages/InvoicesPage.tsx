import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { apiJson } from "../api";

type Row = {
  document_id: number;
  entity_name?: string;
  number?: number;
  date?: string;
  net_value?: number;
  status?: number;
};

export default function InvoicesPage() {
  const q = useQuery({
    queryKey: ["supplier-invoices"],
    queryFn: () => apiJson<Row[]>("/moloni/supplier-invoices"),
  });

  return (
    <div>
      <h1>Faturas de fornecedor</h1>
      <p className="muted">Dados em direto do Moloni (getAll).</p>
      {q.isLoading ? <p>A carregar…</p> : null}
      {q.error ? <p className="error">{(q.error as Error).message}</p> : null}
      {q.data ? (
        <div className="card" style={{ padding: 0, overflow: "auto" }}>
          <table className="data">
            <thead>
              <tr>
                <th>ID</th>
                <th>N.º</th>
                <th>Fornecedor</th>
                <th>Data</th>
                <th>Líquido</th>
                <th>Estado</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {q.data.map((r) => (
                <tr key={r.document_id}>
                  <td>{r.document_id}</td>
                  <td>{r.number ?? "—"}</td>
                  <td>{r.entity_name ?? "—"}</td>
                  <td>{r.date ? String(r.date).slice(0, 10) : "—"}</td>
                  <td>{r.net_value != null ? Number(r.net_value).toFixed(2) : "—"}</td>
                  <td>{r.status === 1 ? "Fechado" : "Rascunho"}</td>
                  <td>
                    <Link className="btn" to={`/invoices/${r.document_id}`}>
                      Abrir
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}
