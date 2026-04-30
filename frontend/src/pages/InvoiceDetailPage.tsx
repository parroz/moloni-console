import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { apiJson } from "../api";
import {
  DEFAULT_RETAIL_VAT_PERCENT,
  pvFromPvp,
  pvpFromPv,
  vatRateFromLineTaxes,
  type MoloniTaxRow,
} from "../moloniUtils";

type ProductOne = {
  product_id: number;
  reference?: string;
  name?: string;
  ean?: string;
  price?: number;
  category_id?: number;
  taxes?: MoloniTaxRow[];
};

type InvLine = {
  product_id: number;
  reference?: string;
  name?: string;
  qty?: number;
  price?: number;
  discount?: number;
  taxes?: MoloniTaxRow[];
  exemption_reason?: string;
};

type Doc = {
  document_id: number;
  entity_name?: string;
  date?: string;
  expiration_date?: string;
  your_reference?: string;
  our_reference?: string;
  notes?: string;
  status?: number;
  financial_discount?: number;
  special_discount?: number;
  /** Moloni document totals (reference). */
  net_value?: number;
  taxes_value?: number;
  gross_value?: number;
  products: InvLine[];
};

type Detail = { document: Doc; products: Record<string, ProductOne | null>; retail_vat_percent?: number };

type CatRow = { category_id: number; parent_id: number; name: string };

function initialCategoryParentChild(cid: number, cats: CatRow[]): { parentId: number; leafId: number } {
  if (!cid || !cats.length) return { parentId: 0, leafId: 0 };
  const byId = new Map(cats.map((c) => [c.category_id, c]));
  const node = byId.get(cid);
  if (!node) return { parentId: 0, leafId: cid };
  if (node.parent_id === 0) return { parentId: cid, leafId: cid };
  const parent = byId.get(node.parent_id);
  if (parent && parent.parent_id === 0) return { parentId: parent.category_id, leafId: cid };
  let cur = node;
  while (cur.parent_id !== 0) {
    const p = byId.get(cur.parent_id);
    if (!p) return { parentId: 0, leafId: cid };
    cur = p;
  }
  return { parentId: cur.category_id, leafId: cid };
}

type RowState = {
  productId: number;
  reference: string;
  lineName: string;
  qty: number;
  lineUnitPrice: number;
  lineVatPercent: number;
  discount: number;
  retailPv: number;
  retailPvp: number;
  productVatPercent: number;
  ean: string;
  /** Moloni root category (parent_id === 0) for the first dropdown. */
  categoryParentId: number;
  /** Moloni category_id to save (subcategory or root). */
  categoryId: number;
};

export default function InvoiceDetailPage() {
  const { documentId } = useParams();
  const id = Number(documentId);
  const qc = useQueryClient();

  const detailQ = useQuery({
    queryKey: ["invoice-detail", id],
    queryFn: () => apiJson<Detail>(`/moloni/supplier-invoices/${id}/detail`),
    enabled: Number.isFinite(id),
  });

  const catQ = useQuery({
    queryKey: ["categories", "recursive"],
    queryFn: () => apiJson<CatRow[]>("/moloni/categories?recursive=true"),
  });

  const [rows, setRows] = useState<RowState[]>([]);
  const [header, setHeader] = useState({
    date: "",
    expiration_date: "",
    your_reference: "",
    our_reference: "",
    notes: "",
    status: 0,
  });
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    const d = detailQ.data;
    const cats = catQ.data ?? [];
    if (!d?.document) return;
    const doc = d.document;
    setHeader({
      date: doc.date ? String(doc.date).slice(0, 10) : "",
      expiration_date: doc.expiration_date ? String(doc.expiration_date).slice(0, 10) : "",
      your_reference: String(doc.your_reference ?? ""),
      our_reference: String(doc.our_reference ?? ""),
      notes: String(doc.notes ?? ""),
      status: Number(doc.status ?? 0),
    });
    const next: RowState[] = [];
    const retailRate =
      typeof d.retail_vat_percent === "number" && Number.isFinite(d.retail_vat_percent)
        ? d.retail_vat_percent
        : DEFAULT_RETAIL_VAT_PERCENT;
    for (const line of doc.products || []) {
      const pid = Number(line.product_id);
      const p = d.products[String(pid)] as ProductOne | null | undefined;
      if (!p || typeof p !== "object" || !("product_id" in p)) {
        next.push({
          productId: pid,
          reference: String(line.reference ?? ""),
          lineName: String(line.name ?? ""),
          qty: Number(line.qty ?? 0),
          lineUnitPrice: Number(line.price ?? 0),
          lineVatPercent: vatRateFromLineTaxes(line.taxes),
          discount: Number(line.discount ?? 0),
          retailPv: 0,
          retailPvp: 0,
          productVatPercent: retailRate,
          ean: "",
          categoryParentId: 0,
          categoryId: 0,
        });
        continue;
      }
      const rate = retailRate;
      const pv = Number(p.price ?? 0);
      const leafCat = Number(p.category_id ?? 0);
      const { parentId, leafId } = initialCategoryParentChild(leafCat, cats);
      next.push({
        productId: pid,
        reference: String(line.reference ?? p.reference ?? ""),
        lineName: String(p.name ?? line.name ?? ""),
        qty: Number(line.qty ?? 0),
        lineUnitPrice: Number(line.price ?? 0),
        lineVatPercent: vatRateFromLineTaxes(line.taxes),
        discount: Number(line.discount ?? 0),
        retailPv: pv,
        retailPvp: pvpFromPv(pv, rate),
        productVatPercent: rate,
        ean: String(p.ean ?? ""),
        categoryParentId: parentId,
        categoryId: leafId,
      });
    }
    setRows(next);
  }, [detailQ.data, catQ.data]);

  const bulkMut = useMutation({
    mutationFn: (items: { product_id: number; ean?: string; pvp?: number; category_id?: number; name?: string }[]) =>
      apiJson<{ results: { product_id: number; ok: boolean; error?: string }[] }>("/moloni/products/bulk-update", {
        method: "POST",
        body: JSON.stringify({ items }),
      }),
    onSuccess: (res) => {
      const failed = res.results.filter((r) => !r.ok);
      if (failed.length) {
        setErr(failed.map((f) => `${f.product_id}: ${f.error || "erro"}`).join("\n"));
      } else {
        setErr(null);
      }
      setMsg(`Produtos atualizados: ${res.results.filter((r) => r.ok).length}`);
      qc.invalidateQueries({ queryKey: ["invoice-detail", id] });
    },
    onError: (e: Error) => {
      setMsg(null);
      setErr(e.message);
    },
  });

  const docMut = useMutation({
    mutationFn: (body: unknown) =>
      apiJson(`/moloni/supplier-invoices/${id}`, {
        method: "PUT",
        body: JSON.stringify(body),
      }),
    onSuccess: () => {
      setErr(null);
      setMsg("Documento gravado no Moloni.");
      qc.invalidateQueries({ queryKey: ["invoice-detail", id] });
      qc.invalidateQueries({ queryKey: ["supplier-invoices"] });
    },
    onError: (e: Error) => {
      setMsg(null);
      setErr(e.message);
    },
  });

  /** Purchase-side totals from current rows (custo na fatura: qty × preço × (1 − desconto%), + IVA linha). */
  const purchaseTotals = useMemo(() => {
    let net = 0;
    let vat = 0;
    for (const r of rows) {
      const disc = Math.min(100, Math.max(0, Number(r.discount) || 0));
      const lineNet = r.qty * r.lineUnitPrice * (1 - disc / 100);
      net += lineNet;
      vat += lineNet * ((Number(r.lineVatPercent) || 0) / 100);
    }
    const gross = net + vat;
    return { net, vat, gross };
  }, [rows]);

  /** Retail reference: sum of qty × PV / PVP (not necessarily igual ao documento Moloni). */
  const retailTotals = useMemo(() => {
    let pv = 0;
    let pvp = 0;
    for (const r of rows) {
      pv += r.qty * r.retailPv;
      pvp += r.qty * r.retailPvp;
    }
    return { pv, pvp };
  }, [rows]);

  const categories = catQ.data ?? [];
  const rootCategories = useMemo(
    () => [...categories].filter((c) => c.parent_id === 0).sort((a, b) => a.name.localeCompare(b.name)),
    [categories],
  );
  const childrenOf = useMemo(() => {
    const byParent = new Map<number, CatRow[]>();
    for (const c of categories) {
      const k = c.parent_id;
      if (!byParent.has(k)) byParent.set(k, []);
      byParent.get(k)!.push(c);
    }
    for (const arr of byParent.values()) arr.sort((a, b) => a.name.localeCompare(b.name));
    return (parentId: number) => byParent.get(parentId) ?? [];
  }, [categories]);

  function updateRow(i: number, patch: Partial<RowState>) {
    setRows((prev) => {
      const copy = [...prev];
      const base = copy[i];
      const cur = { ...base, ...patch };
      const rate = cur.productVatPercent;
      if (patch.categoryParentId != null) {
        const pid = Number(patch.categoryParentId);
        cur.categoryParentId = pid;
        const kids = childrenOf(pid);
        cur.categoryId = kids.length ? kids[0].category_id : pid;
      }
      if (patch.categoryId != null) {
        cur.categoryId = Number(patch.categoryId);
      }
      if (patch.retailPvp != null) {
        const raw = Number(patch.retailPvp);
        const pvp = Number.isFinite(raw) ? Math.round(raw * 100) / 100 : 0;
        cur.retailPvp = pvp;
        cur.retailPv = pvFromPvp(pvp, rate);
      }
      copy[i] = cur;
      return copy;
    });
  }

  function exportLabelsCsv() {
    const lines: string[] = ["ref,name,ean,price,IntPart,DecimalPart"];
    for (const r of rows) {
      const pvp = r.retailPvp.toFixed(2);
      const [intPart, decPart] = pvp.split(".");
      let n = Math.max(0, Math.floor(r.qty));
      while (n > 0) {
        lines.push([r.reference, r.lineName.replaceAll(",", " "), r.ean || "", pvp, intPart, decPart].join(","));
        n -= 1;
      }
    }
    const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `labels-${id}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  function printReport() {
    window.print();
  }

  function pushProducts() {
    setMsg(null);
    setErr(null);
    const bad = rows.filter((r) => !Number.isFinite(r.retailPvp));
    if (bad.length) {
      setErr(`PVP inválido (número) nas linhas: ${bad.map((r) => r.reference || r.productId).join(", ")}`);
      return;
    }
    const items = rows.map((r) => {
      const item: {
        product_id: number;
        ean: string;
        pvp: number;
        name: string;
        category_id?: number;
      } = {
        product_id: r.productId,
        ean: r.ean,
        pvp: Math.round(r.retailPvp * 100) / 100,
        name: r.lineName,
      };
      if (r.categoryId > 0) item.category_id = r.categoryId;
      return item;
    });
    bulkMut.mutate(items);
  }

  function pushDocument() {
    setMsg(null);
    setErr(null);
    const linePatches = rows.map((r) => ({
      product_id: r.productId,
      qty: r.qty,
      price: r.lineUnitPrice,
      discount: r.discount,
      name: r.lineName,
    }));
    docMut.mutate({
      header: {
        date: header.date || undefined,
        expiration_date: header.expiration_date || undefined,
        your_reference: header.your_reference,
        our_reference: header.our_reference,
        notes: header.notes,
        status: header.status,
      },
      lines: linePatches,
    });
  }

  if (!Number.isFinite(id)) {
    return <p className="error">ID inválido</p>;
  }

  return (
    <div>
      <p className="no-print">
        <Link to="/invoices">← Faturas</Link>
      </p>
      <h1>Fatura #{id}</h1>
      {detailQ.isLoading ? <p>A carregar…</p> : null}
      {detailQ.error ? <p className="error">{(detailQ.error as Error).message}</p> : null}
      {msg ? <p className="success">{msg}</p> : null}
      {err ? <p className="error">{err}</p> : null}

      {detailQ.data ? (
        <>
          <div className="card no-print">
            <h2>Cabeçalho</h2>
            <div className="form-grid">
              <label>
                Data
                <input type="date" value={header.date} onChange={(e) => setHeader((h) => ({ ...h, date: e.target.value }))} />
              </label>
              <label>
                Vencimento
                <input
                  type="date"
                  value={header.expiration_date}
                  onChange={(e) => setHeader((h) => ({ ...h, expiration_date: e.target.value }))}
                />
              </label>
              <label>
                Nossa ref.
                <input value={header.our_reference} onChange={(e) => setHeader((h) => ({ ...h, our_reference: e.target.value }))} />
              </label>
              <label>
                Vossa ref.
                <input value={header.your_reference} onChange={(e) => setHeader((h) => ({ ...h, your_reference: e.target.value }))} />
              </label>
              <label>
                Estado
                <select value={header.status} onChange={(e) => setHeader((h) => ({ ...h, status: Number(e.target.value) }))}>
                  <option value={0}>Rascunho</option>
                  <option value={1}>Fechado</option>
                </select>
              </label>
            </div>
            <label className="muted" style={{ display: "block", marginTop: "0.5rem" }}>
              Notas
              <textarea
                value={header.notes}
                onChange={(e) => setHeader((h) => ({ ...h, notes: e.target.value }))}
                rows={2}
                style={{ width: "100%", marginTop: 4 }}
              />
            </label>
          </div>

          <div className="row-actions no-print">
            <button type="button" className="btn primary" disabled={bulkMut.isPending} onClick={() => pushProducts()}>
              Atualizar produtos no Moloni
            </button>
            <button type="button" className="btn" disabled={docMut.isPending} onClick={() => pushDocument()}>
              Atualizar documento (linhas + cabeçalho)
            </button>
            <button type="button" className="btn" onClick={() => exportLabelsCsv()}>
              Exportar CSV etiquetas
            </button>
            <button type="button" className="btn" onClick={() => printReport()}>
              Imprimir relatório
            </button>
          </div>

          <div className="card" id="invoice-report">
            <div style={{ marginBottom: "1rem" }}>
              <h2 style={{ margin: 0, fontSize: "1.15rem" }}>Relatório · Fatura #{id}</h2>
              <p className="muted" style={{ margin: "0.25rem 0 0" }}>
                {detailQ.data.document.entity_name ?? "Fornecedor"} · {header.date || "—"}
              </p>
            </div>
            <h2 className="no-print" style={{ fontSize: "1.05rem" }}>
              Linhas
            </h2>
            <p className="muted no-print" style={{ margin: "0.25rem 0 0.75rem", maxWidth: "52rem" }}>
              Retalho: edite só o <strong>PVP</strong> (€ com IVA, 2 decimais). O preço líquido no Moloni segue{" "}
              <code>PVP / (1 + IVA/100)</code>. «IVA linha» é o IVA da fatura de compra; «IVA venda» é a taxa configurada
              no servidor. Cada linha tem duas linhas na grelha: ref. ampla + dados; abaixo EAN ampla.
            </p>
            {catQ.isLoading ? <p className="muted no-print">A carregar categorias Moloni…</p> : null}
            {catQ.error ? <p className="error no-print">{(catQ.error as Error).message}</p> : null}
            <div style={{ overflow: "auto" }}>
              <table className="data invoice-lines-table">
                <thead>
                  <tr>
                    <th>Ref.</th>
                    <th>Nome</th>
                    <th>Qtd</th>
                    <th>Custo unit.</th>
                    <th>Custo total</th>
                    <th>IVA linha %</th>
                    <th>IVA venda %</th>
                    <th>PVP (c/ IVA)</th>
                    <th>Categoria</th>
                    <th>Subcategoria</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r, i) => {
                    let subOpts = childrenOf(r.categoryParentId);
                    if (subOpts.length === 0 && r.categoryParentId > 0) {
                      const root = rootCategories.find((c) => c.category_id === r.categoryParentId);
                      if (root) subOpts = [root];
                    }
                    const leafOk = subOpts.some((k) => k.category_id === r.categoryId);
                    const leafExtra = !leafOk ? categories.find((c) => c.category_id === r.categoryId) : undefined;
                    if (leafExtra) subOpts = [...subOpts, leafExtra];
                    return (
                      <Fragment key={r.productId}>
                        <tr className="invoice-line-r1">
                          <td rowSpan={2} className="invoice-td-ref">
                            <input
                              className="invoice-input-ref"
                              value={r.reference}
                              onChange={(e) => updateRow(i, { reference: e.target.value })}
                            />
                          </td>
                          <td>
                            <input value={r.lineName} onChange={(e) => updateRow(i, { lineName: e.target.value })} />
                          </td>
                          <td>
                            <input
                              type="number"
                              value={r.qty}
                              onChange={(e) => updateRow(i, { qty: Number(e.target.value) })}
                            />
                          </td>
                          <td>
                            <input
                              type="number"
                              step="0.0001"
                              value={r.lineUnitPrice}
                              onChange={(e) => updateRow(i, { lineUnitPrice: Number(e.target.value) })}
                            />
                          </td>
                          <td>{(r.qty * r.lineUnitPrice).toFixed(2)}</td>
                          <td>{r.lineVatPercent}%</td>
                          <td>{r.productVatPercent}%</td>
                          <td>
                            <input
                              type="number"
                              step="0.01"
                              min={0}
                              value={r.retailPvp}
                              onChange={(e) => updateRow(i, { retailPvp: Number(e.target.value) })}
                            />
                          </td>
                          <td>
                            <select
                              value={r.categoryParentId}
                              onChange={(e) => updateRow(i, { categoryParentId: Number(e.target.value) })}
                            >
                              <option value={0}>—</option>
                              {rootCategories.map((c) => (
                                <option key={c.category_id} value={c.category_id}>
                                  {c.name} (#{c.category_id})
                                </option>
                              ))}
                            </select>
                          </td>
                          <td>
                            <select
                              value={r.categoryId}
                              onChange={(e) => updateRow(i, { categoryId: Number(e.target.value) })}
                            >
                              <option value={0}>—</option>
                              {subOpts.map((c) => (
                                <option key={c.category_id} value={c.category_id}>
                                  {c.name} (#{c.category_id})
                                </option>
                              ))}
                            </select>
                          </td>
                        </tr>
                        <tr className="invoice-line-r2">
                          <td colSpan={6} className="invoice-td-ean">
                            <label className="invoice-ean-label">
                              <span className="muted">EAN</span>
                              <input
                                className="invoice-input-ean"
                                value={r.ean}
                                maxLength={20}
                                onChange={(e) => updateRow(i, { ean: e.target.value })}
                                placeholder="13–20 caracteres"
                              />
                            </label>
                          </td>
                          <td colSpan={3} className="invoice-td-r2-spacer muted no-print" />
                        </tr>
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>

            <div className="invoice-totals-block">
              <h3 className="invoice-totals-title">Totais — custo na fatura (linhas)</h3>
              <p className="muted invoice-totals-hint">
                Base sem IVA, IVA calculado pela taxa de cada linha, total com IVA. Desconto de linha tratado como % (0–100),
                como na API Moloni.
              </p>
              <table className="data invoice-totals-table">
                <tbody>
                  <tr>
                    <th scope="row">Total s/ IVA (base)</th>
                    <td>{purchaseTotals.net.toFixed(2)} €</td>
                  </tr>
                  <tr>
                    <th scope="row">IVA</th>
                    <td>{purchaseTotals.vat.toFixed(2)} €</td>
                  </tr>
                  <tr className="invoice-totals-strong">
                    <th scope="row">Total c/ IVA</th>
                    <td>{purchaseTotals.gross.toFixed(2)} €</td>
                  </tr>
                </tbody>
              </table>

              <h3 className="invoice-totals-title" style={{ marginTop: "1.25rem" }}>
                Totais — retalho (referência PVP)
              </h3>
              <p className="muted invoice-totals-hint">Soma de quantidade × PV e × PVP (preços de venda no artigo).</p>
              <table className="data invoice-totals-table">
                <tbody>
                  <tr>
                    <th scope="row">Total PV (s/ IVA)</th>
                    <td>{retailTotals.pv.toLocaleString("pt-PT", { minimumFractionDigits: 2, maximumFractionDigits: 4 })} €</td>
                  </tr>
                  <tr className="invoice-totals-strong">
                    <th scope="row">Total PVP (c/ IVA)</th>
                    <td>{retailTotals.pvp.toFixed(2)} €</td>
                  </tr>
                </tbody>
              </table>

              {detailQ.data.document.net_value != null && detailQ.data.document.taxes_value != null ? (
                <p className="muted invoice-totals-hint" style={{ marginTop: "1rem" }}>
                  Valores no documento Moloni (referência): líquido{" "}
                  <strong>{Number(detailQ.data.document.net_value).toFixed(2)} €</strong> · IVA{" "}
                  <strong>{Number(detailQ.data.document.taxes_value).toFixed(2)} €</strong>
                  {detailQ.data.document.gross_value != null ? (
                    <>
                      {" "}
                      · bruto <strong>{Number(detailQ.data.document.gross_value).toFixed(2)} €</strong>
                    </>
                  ) : null}
                  . Podem diferir ligeiramente dos totais calculados se houver arredondamentos ou descontos globais.
                </p>
              ) : null}
            </div>
          </div>

        </>
      ) : null}
    </div>
  );
}
