import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
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
  summary?: string;
  ean?: string;
  price?: number;
  category_id?: number;
  taxes?: MoloniTaxRow[];
};

type InvLine = {
  product_id: number;
  reference?: string;
  name?: string;
  summary?: string;
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
  net_value?: number;
  taxes_value?: number;
  gross_value?: number;
  products: InvLine[];
};

type CatRow = { category_id: number; parent_id: number; name: string };

type Detail = {
  document: Doc;
  products: Record<string, ProductOne | null>;
  /** category_id → {category_id, parent_id, name} resolved via productCategories/getOne */
  categories?: Record<string, CatRow>;
  retail_vat_percent?: number;
};

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
  /** Kept in state so it's still sent to Moloni on save, but not shown as an editable field. */
  summary: string;
  categoryParentId: number;
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

  /** Root categories (parent_id === 0) — one fast call, used for the Category dropdown. */
  const rootsQ = useQuery({
    queryKey: ["categories", 0],
    queryFn: () => apiJson<CatRow[]>("/moloni/categories"),
    staleTime: 5 * 60 * 1000,
    enabled: detailQ.isSuccess,
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
    const retailRate =
      typeof d.retail_vat_percent === "number" && Number.isFinite(d.retail_vat_percent)
        ? d.retail_vat_percent
        : DEFAULT_RETAIL_VAT_PERCENT;
    const catMap = d.categories ?? {};
    const next: RowState[] = [];
    for (const line of doc.products || []) {
      const pid = Number(line.product_id);
      const p = d.products[String(pid)] as ProductOne | null | undefined;
      const leafCat = Number(p?.category_id ?? 0);
      // Resolve parent/leaf from the pre-fetched category info
      const catInfo = catMap[String(leafCat)];
      let parentId = 0;
      let leafId = leafCat;
      if (catInfo) {
        if (catInfo.parent_id === 0) {
          // It is itself a root category
          parentId = catInfo.category_id;
          leafId = catInfo.category_id;
        } else {
          parentId = catInfo.parent_id;
          leafId = catInfo.category_id;
        }
      }
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
          summary: String(line.summary ?? ""),
          categoryParentId: parentId,
          categoryId: leafId,
        });
        continue;
      }
      const pv = Number(p.price ?? 0);
      next.push({
        productId: pid,
        reference: String(line.reference ?? p.reference ?? ""),
        lineName: String(p.name ?? line.name ?? ""),
        qty: Number(line.qty ?? 0),
        lineUnitPrice: Number(line.price ?? 0),
        lineVatPercent: vatRateFromLineTaxes(line.taxes),
        discount: Number(line.discount ?? 0),
        retailPv: pv,
        retailPvp: pvpFromPv(pv, retailRate),
        productVatPercent: retailRate,
        ean: String(p.ean ?? ""),
        summary: String(line.summary ?? p.summary ?? ""),
        categoryParentId: parentId,
        categoryId: leafId,
      });
    }
    setRows(next);
  }, [detailQ.data]);

  /** Unique non-zero parent IDs from current rows → drives subcategory loading. */
  const parentIds = useMemo(
    () => [...new Set(rows.map((r) => r.categoryParentId).filter((pid) => pid > 0))],
    [rows],
  );

  /** One query per parent: loads its children on demand, cached 5 min. */
  const childrenQueries = useQueries({
    queries: parentIds.map((pid) => ({
      queryKey: ["categories", pid],
      queryFn: () => apiJson<CatRow[]>(`/moloni/categories?parent_id=${pid}`),
      staleTime: 5 * 60 * 1000,
    })),
  });

  const childrenByParent = useMemo(() => {
    const map = new Map<number, CatRow[]>();
    parentIds.forEach((pid, i) => {
      const data = childrenQueries[i]?.data;
      if (data) map.set(pid, [...data].sort((a, b) => a.name.localeCompare(b.name)));
    });
    return map;
  }, [parentIds, childrenQueries]);

  const rootCategories = useMemo(
    () =>
      (rootsQ.data ?? [])
        .map((c) => ({ category_id: Number(c.category_id), parent_id: 0, name: String(c.name) }))
        .sort((a, b) => a.name.localeCompare(b.name)),
    [rootsQ.data],
  );

  const bulkMut = useMutation({
    mutationFn: (
      items: { product_id: number; ean?: string; pvp?: number; category_id?: number; name?: string; summary?: string }[],
    ) =>
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

  const purchaseTotals = useMemo(() => {
    let net = 0;
    let vat = 0;
    for (const r of rows) {
      const disc = Math.min(100, Math.max(0, Number(r.discount) || 0));
      const lineNet = r.qty * r.lineUnitPrice * (1 - disc / 100);
      net += lineNet;
      vat += lineNet * ((Number(r.lineVatPercent) || 0) / 100);
    }
    return { net, vat, gross: net + vat };
  }, [rows]);

  const retailTotals = useMemo(() => {
    let pv = 0;
    let pvp = 0;
    for (const r of rows) {
      pv += r.qty * r.retailPv;
      pvp += r.qty * r.retailPvp;
    }
    return { pv, pvp };
  }, [rows]);

  function updateRow(i: number, patch: Partial<RowState>) {
    setRows((prev) => {
      const copy = [...prev];
      const cur = { ...copy[i], ...patch };
      if (patch.categoryParentId != null) {
        cur.categoryParentId = Number(patch.categoryParentId);
        cur.categoryId = 0; // reset; subcategory dropdown re-populates once children load
      }
      if (patch.categoryId != null) {
        cur.categoryId = Number(patch.categoryId);
      }
      if (patch.retailPvp != null) {
        const raw = Number(patch.retailPvp);
        const pvp = Number.isFinite(raw) ? Math.round(raw * 100) / 100 : 0;
        cur.retailPvp = pvp;
        cur.retailPv = pvFromPvp(pvp, cur.productVatPercent);
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

  function pushProducts() {
    setMsg(null);
    setErr(null);
    const bad = rows.filter((r) => !Number.isFinite(r.retailPvp));
    if (bad.length) {
      setErr(`PVP inválido nas linhas: ${bad.map((r) => r.reference || r.productId).join(", ")}`);
      return;
    }
    const items = rows.map((r) => {
      const item: {
        product_id: number;
        ean: string;
        pvp: number;
        name: string;
        summary: string;
        category_id?: number;
      } = {
        product_id: r.productId,
        ean: r.ean,
        pvp: Math.round(r.retailPvp * 100) / 100,
        name: r.lineName,
        summary: r.summary,
      };
      if (r.categoryId > 0) item.category_id = r.categoryId;
      return item;
    });
    bulkMut.mutate(items);
  }

  function pushDocument() {
    setMsg(null);
    setErr(null);
    docMut.mutate({
      header: {
        date: header.date || undefined,
        expiration_date: header.expiration_date || undefined,
        your_reference: header.your_reference,
        our_reference: header.our_reference,
        notes: header.notes,
        status: header.status,
      },
      lines: rows.map((r) => ({
        product_id: r.productId,
        qty: r.qty,
        price: r.lineUnitPrice,
        discount: r.discount,
        name: r.lineName,
        summary: r.summary,
      })),
    });
  }

  if (!Number.isFinite(id)) return <p className="error">ID inválido</p>;

  const catsLoading = rootsQ.isFetching || childrenQueries.some((q) => q.isFetching);

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
            <button type="button" className="btn primary" disabled={bulkMut.isPending} onClick={pushProducts}>
              Atualizar produtos no Moloni
            </button>
            <button type="button" className="btn" disabled={docMut.isPending} onClick={pushDocument}>
              Atualizar documento
            </button>
            <button type="button" className="btn" onClick={exportLabelsCsv}>
              Exportar CSV etiquetas
            </button>
            <button type="button" className="btn" onClick={() => window.print()}>
              Imprimir
            </button>
          </div>

          <div className="card" id="invoice-report">
            <div style={{ marginBottom: "0.75rem" }}>
              <h2 style={{ margin: 0, fontSize: "1.15rem" }}>Relatório · Fatura #{id}</h2>
              <p className="muted" style={{ margin: "0.25rem 0 0" }}>
                {detailQ.data.document.entity_name ?? "Fornecedor"} · {header.date || "—"}
              </p>
            </div>
            {catsLoading ? <p className="muted no-print" style={{ margin: "0 0 0.5rem" }}>A carregar categorias…</p> : null}

            <div style={{ overflow: "auto" }}>
              <table className="data invoice-lines-table">
                <thead>
                  <tr>
                    <th className="invoice-th-ref">Ref.</th>
                    <th className="invoice-th-qty">Qtd</th>
                    <th className="invoice-th-cost">Custo unit.</th>
                    <th className="invoice-th-total">Total</th>
                    <th className="invoice-th-vat">IVA%</th>
                    <th className="invoice-th-pvp">PVP (c/ IVA)</th>
                    <th className="invoice-th-cat">Categoria</th>
                    <th className="invoice-th-subcat">Subcategoria</th>
                  </tr>
                </thead>
                {rows.map((r, i) => {
                    // Subcategory options = children of the selected category (parent).
                    // Until those children load, fall back to the resolved category from
                    // detail.categories so the dropdown still shows the correct name.
                    const detailCats = detailQ.data?.categories ?? {};
                    const loadedChildren = childrenByParent.get(r.categoryParentId) ?? [];
                    const subOpts: CatRow[] = [...loadedChildren];
                    if (r.categoryId > 0 && !subOpts.some((c) => c.category_id === r.categoryId)) {
                      const resolved = detailCats[String(r.categoryId)];
                      if (resolved) subOpts.unshift(resolved);
                    }
                    return (
                      <tbody key={r.productId} className="product-block">
                        {/* Row 1: Ref | Qty | Cost | Total | VAT | PVP | Category | Subcategory */}
                        <tr className="invoice-line-r1">
                          <td rowSpan={2} className="invoice-td-ref">
                            <input
                              className="invoice-input-ref"
                              value={r.reference}
                              onChange={(e) => updateRow(i, { reference: e.target.value })}
                            />
                          </td>
                          <td className="invoice-td-qty">
                            <input
                              type="number"
                              value={r.qty}
                              onChange={(e) => updateRow(i, { qty: Number(e.target.value) })}
                            />
                          </td>
                          <td className="invoice-td-cost">
                            <input
                              type="number"
                              step="0.0001"
                              value={r.lineUnitPrice}
                              onChange={(e) => updateRow(i, { lineUnitPrice: Number(e.target.value) })}
                            />
                          </td>
                          <td className="invoice-td-total">
                            {(r.qty * r.lineUnitPrice).toFixed(2)}
                          </td>
                          <td className="invoice-td-vat">{r.lineVatPercent}%</td>
                          <td className="invoice-td-pvp">
                            <div className="pvp-cell">
                              <input
                                type="number"
                                step="0.01"
                                min={0}
                                value={r.retailPvp}
                                onChange={(e) => updateRow(i, { retailPvp: Number(e.target.value) })}
                              />
                              {r.retailPv > 0 ? <span className="pv-hint">PV {r.retailPv.toFixed(4)} €</span> : null}
                            </div>
                          </td>
                          <td className="invoice-td-cat">
                            <select
                              value={r.categoryParentId}
                              onChange={(e) => updateRow(i, { categoryParentId: Number(e.target.value) })}
                            >
                              <option value={0}>—</option>
                              {rootCategories.map((c) => (
                                <option key={c.category_id} value={c.category_id}>
                                  {c.name}
                                </option>
                              ))}
                            </select>
                          </td>
                          <td className="invoice-td-subcat">
                            <select
                              value={r.categoryId}
                              onChange={(e) => updateRow(i, { categoryId: Number(e.target.value) })}
                              disabled={subOpts.length === 0}
                            >
                              <option value={0}>—</option>
                              {subOpts.map((c) => (
                                <option key={c.category_id} value={c.category_id}>
                                  {c.name}
                                </option>
                              ))}
                            </select>
                          </td>
                        </tr>
                        {/* Row 2: Name (wide) | EAN */}
                        <tr className="invoice-line-r2">
                          <td colSpan={5} className="invoice-td-name">
                            <label className="field-label">
                              Nome
                              <input
                                className="invoice-input-name"
                                value={r.lineName}
                                onChange={(e) => updateRow(i, { lineName: e.target.value })}
                              />
                            </label>
                          </td>
                          <td colSpan={2} className="invoice-td-ean">
                            <label className="field-label">
                              EAN
                              <input
                                className="invoice-input-ean"
                                value={r.ean}
                                maxLength={20}
                                onChange={(e) => updateRow(i, { ean: e.target.value })}
                                placeholder="—"
                              />
                            </label>
                          </td>
                        </tr>
                      </tbody>
                    );
                  })}
              </table>
            </div>

            <div className="invoice-totals-block">
              <h3 className="invoice-totals-title">Totais — custo na fatura</h3>
              <table className="data invoice-totals-table">
                <tbody>
                  <tr>
                    <th scope="row">Total s/ IVA</th>
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
                Totais — retalho
              </h3>
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
                  Moloni: líquido <strong>{Number(detailQ.data.document.net_value).toFixed(2)} €</strong> · IVA{" "}
                  <strong>{Number(detailQ.data.document.taxes_value).toFixed(2)} €</strong>
                  {detailQ.data.document.gross_value != null ? (
                    <> · bruto <strong>{Number(detailQ.data.document.gross_value).toFixed(2)} €</strong></>
                  ) : null}
                </p>
              ) : null}
            </div>
          </div>
        </>
      ) : null}
    </div>
  );
}
