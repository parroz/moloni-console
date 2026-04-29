import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { apiJson } from "../api";
import { pvpFromPv, vatRatePercentFromProduct } from "../moloniUtils";

type Cat = { category_id: number; parent_id: number; name: string };
type Prod = {
  product_id: number;
  reference?: string;
  name?: string;
  ean?: string;
  price?: number;
  taxes?: { tax?: { saft_type?: number }; value?: number }[];
};

export default function ProductsPage() {
  const catQ = useQuery({
    queryKey: ["categories", "recursive"],
    queryFn: () => apiJson<Cat[]>("/moloni/categories?recursive=1"),
  });
  const [categoryId, setCategoryId] = useState<number | null>(null);
  const [offset, setOffset] = useState(0);
  const [accum, setAccum] = useState<Prod[]>([]);

  useEffect(() => {
    const cats = catQ.data;
    if (!cats?.length || categoryId != null) return;
    const roots = cats.filter((c) => c.parent_id === 0);
    const pick = roots[0]?.category_id ?? cats[0]?.category_id;
    if (pick != null) setCategoryId(pick);
  }, [catQ.data, categoryId]);

  const prodQ = useQuery({
    queryKey: ["products", categoryId, offset],
    queryFn: () => apiJson<Prod[]>(`/moloni/products?category_id=${categoryId}&offset=${offset}`),
    enabled: categoryId != null,
  });

  useEffect(() => {
    if (!prodQ.data) return;
    if (offset === 0) setAccum(prodQ.data);
    else setAccum((a) => [...a, ...prodQ.data!]);
  }, [prodQ.data, offset]);

  useEffect(() => {
    setOffset(0);
    setAccum([]);
  }, [categoryId]);

  const cats = catQ.data ?? [];
  const sortedCats = [...cats].sort((a, b) => a.name.localeCompare(b.name));

  return (
    <div>
      <h1>Produtos</h1>
      <p className="muted">Moloni exige category_id no getAll; use -1 no Moloni para órfãos.</p>
      <div className="card no-print">
        <label className="muted">
          Categoria
          <select
            value={categoryId ?? ""}
            onChange={(e) => setCategoryId(e.target.value ? Number(e.target.value) : null)}
            style={{ marginLeft: 8, minWidth: 240 }}
          >
            <option value="">—</option>
            {sortedCats.map((c) => (
              <option key={c.category_id} value={c.category_id}>
                {c.name} (#{c.category_id}) [parent {c.parent_id}]
              </option>
            ))}
          </select>
        </label>
      </div>
      {prodQ.isLoading ? <p>A carregar…</p> : null}
      {prodQ.error ? <p className="error">{(prodQ.error as Error).message}</p> : null}
      {accum.length ? (
        <div className="card" style={{ padding: 0, overflow: "auto" }}>
          <table className="data">
            <thead>
              <tr>
                <th>ID</th>
                <th>Ref.</th>
                <th>Nome</th>
                <th>EAN</th>
                <th>PV</th>
                <th>PVP</th>
              </tr>
            </thead>
            <tbody>
              {accum.map((p) => {
                const rate = vatRatePercentFromProduct(p);
                const pv = Number(p.price ?? 0);
                const pvp = pvpFromPv(pv, rate);
                return (
                  <tr key={p.product_id}>
                    <td>{p.product_id}</td>
                    <td>{p.reference}</td>
                    <td>{p.name}</td>
                    <td>{p.ean}</td>
                    <td>{pv.toFixed(4)}</td>
                    <td>{pvp.toFixed(4)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {prodQ.data && prodQ.data.length === 50 ? (
            <div style={{ padding: "0.75rem" }}>
              <button type="button" className="btn" onClick={() => setOffset((o) => o + 50)}>
                Carregar mais
              </button>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
