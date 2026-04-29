import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { apiJson } from "../api";

type Row = { supplier_id: number; name?: string; number?: string; vat?: string };

export default function SuppliersPage() {
  const q = useQuery({
    queryKey: ["suppliers"],
    queryFn: () => apiJson<Row[]>("/moloni/suppliers"),
  });

  return (
    <div>
      <h1>Fornecedores</h1>
      {q.isLoading ? <p>A carregar…</p> : null}
      {q.error ? <p className="error">{(q.error as Error).message}</p> : null}
      {q.data ? (
        <div className="card" style={{ padding: 0, overflow: "auto" }}>
          <table className="data">
            <thead>
              <tr>
                <th>ID</th>
                <th>N.º</th>
                <th>Nome</th>
                <th>NIF</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {q.data.map((r) => (
                <tr key={r.supplier_id}>
                  <td>{r.supplier_id}</td>
                  <td>{r.number}</td>
                  <td>{r.name}</td>
                  <td>{r.vat}</td>
                  <td>
                    <Link className="btn" to={`/suppliers/${r.supplier_id}`}>
                      Editar
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
