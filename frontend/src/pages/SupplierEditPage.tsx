import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { apiJson } from "../api";

type Supplier = Record<string, string | number | null | undefined>;

export default function SupplierEditPage() {
  const { supplierId } = useParams();
  const id = Number(supplierId);
  const qc = useQueryClient();
  const [form, setForm] = useState<Supplier>({});

  const q = useQuery({
    queryKey: ["supplier", id],
    queryFn: () => apiJson<Supplier>(`/moloni/suppliers/${id}`),
    enabled: Number.isFinite(id),
  });

  useEffect(() => {
    if (q.data) setForm(q.data);
  }, [q.data]);

  const mut = useMutation({
    mutationFn: (payload: Supplier) =>
      apiJson(`/moloni/suppliers/${id}`, { method: "PUT", body: JSON.stringify(payload) }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["suppliers"] });
      qc.invalidateQueries({ queryKey: ["supplier", id] });
    },
  });

  if (!Number.isFinite(id)) return <p className="error">ID inválido</p>;

  function set<K extends string>(key: K, value: string) {
    setForm((f) => ({ ...f, [key]: value }));
  }

  return (
    <div>
      <p>
        <Link to="/suppliers">← Fornecedores</Link>
      </p>
      <h1>Fornecedor #{id}</h1>
      {q.isLoading ? <p>A carregar…</p> : null}
      {q.error ? <p className="error">{(q.error as Error).message}</p> : null}
      {mut.isSuccess ? <p className="success">Gravado.</p> : null}
      {mut.error ? <p className="error">{(mut.error as Error).message}</p> : null}
      {Object.keys(form).length ? (
        <div className="card">
          <div className="form-grid">
            {(
              [
                ["name", "Nome"],
                ["number", "N.º fornecedor"],
                ["vat", "NIF / VAT"],
                ["address", "Morada"],
                ["zip_code", "Código postal"],
                ["city", "Cidade"],
                ["country_id", "country_id"],
                ["language_id", "language_id"],
                ["email", "Email"],
                ["phone", "Telefone"],
                ["notes", "Notas"],
                ["maturity_date_id", "maturity_date_id"],
                ["payment_method_id", "payment_method_id"],
                ["delivery_method_id", "delivery_method_id"],
                ["qty_copies_document", "qty_copies_document"],
                ["discount", "Desconto"],
                ["credit_limit", "Limite crédito"],
              ] as const
            ).map(([key, label]) => (
              <label key={key}>
                {label}
                <input
                  value={form[key] == null ? "" : String(form[key])}
                  onChange={(e) => set(key, e.target.value)}
                />
              </label>
            ))}
          </div>
          <div className="row-actions">
            <button type="button" className="btn primary" disabled={mut.isPending} onClick={() => mut.mutate(form)}>
              Guardar
            </button>
          </div>
          <p className="muted">
            Campos *_id devem corresponder a valores válidos no Moloni (prazos, métodos de pagamento, etc.).
          </p>
        </div>
      ) : null}
    </div>
  );
}
