import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { apiJson } from "../api";
import {
  pvFromPvp,
  pvpFromPv,
  vatRateFromLineTaxes,
  vatRatePercentFromProduct,
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

type Detail = { document: Doc; products: Record<string, ProductOne | null> };

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
    queryFn: () =>
      apiJson<{ category_id: number; parent_id: number; name: string }[]>("/moloni/categories?recursive=1"),
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
    const next: RowState[] = [];
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
          productVatPercent: 0,
          ean: "",
          categoryId: 0,
        });
        continue;
      }
      const rate = vatRatePercentFromProduct(p);
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
        retailPvp: pvpFromPv(pv, rate),
        productVatPercent: rate,
        ean: String(p.ean ?? ""),
        categoryId: Number(p.category_id ?? 0),
      });
    }
    setRows(next);
  }, [detailQ.data]);

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
  const catOptions = useMemo(
    () =>
      [...categories]
        .sort((a, b) => a.name.localeCompare(b.name))
        .map((c) => ({ id: c.category_id, label: `${c.name} (#${c.category_id})` })),
    [categories],
  );

  function updateRow(i: number, patch: Partial<RowState>) {
    setRows((prev) => {
      const copy = [...prev];
      const base = copy[i];
      const cur = { ...base, ...patch };
      const rate = cur.productVatPercent;
      if (patch.retailPvp != null) {
        const raw = Number(patch.retailPvp);
        const pvp = Number.isFinite(raw) ? Math.round(raw * 100) / 100 : 0;
        cur.retailPvp = pvp;
        cur.retailPv = pvFromPvp(pvp, rate);
      } else if (patch.retailPv != null) {
        const pv = Number(patch.retailPv);
        cur.retailPv = pv;
        cur.retailPvp = pvpFromPv(pv, rate);
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
    const items = rows.map((r) => ({
      product_id: r.productId,
      ean: r.ean,
      pvp: Math.round(r.retailPvp * 100) / 100,
      category_id: r.categoryId,
      name: r.lineName,
    }));
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
            <div style={{ overflow: "auto" }}>
              <table className="data">
                <thead>
                  <tr>
                    <th>Ref.</th>
                    <th>Nome</th>
                    <th>Qtd</th>
                    <th>Custo unit.</th>
                    <th>Custo total</th>
                    <th>IVA linha %</th>
                    <th>PV (s/ IVA)</th>
                    <th>PVP (c/ IVA)</th>
                    <th>EAN</th>
                    <th>Categoria</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r, i) => (
                    <tr key={r.productId}>
                      <td>{r.reference}</td>
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
                      <td>
                        <input
                          type="number"
                          step="0.0001"
                          value={r.retailPv}
                          onChange={(e) => updateRow(i, { retailPv: Number(e.target.value) })}
                        />
                      </td>
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
                        <input value={r.ean} onChange={(e) => updateRow(i, { ean: e.target.value })} />
                      </td>
                      <td>
                        <select value={r.categoryId} onChange={(e) => updateRow(i, { categoryId: Number(e.target.value) })}>
                          <option value={0}>—</option>
                          {catOptions.map((c) => (
                            <option key={c.id} value={c.id}>
                              {c.label}
                            </option>
                          ))}
                        </select>
                      </td>
                    </tr>
                  ))}
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
                    <td>{retailTotals.pv.toFixed(2)} €</td>
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
