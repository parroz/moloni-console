/** Moloni product tax helpers (IVA saft_type === 1). */

export type MoloniTaxRow = {
  tax_id?: number;
  value?: number;
  /** Present on some document line tax rows (flat). */
  saft_type?: number;
  tax?: { saft_type?: number; value?: number };
};

function taxRowSaftType(t: MoloniTaxRow): number {
  const nested = t.tax || {};
  return Number(nested.saft_type ?? t.saft_type ?? 0);
}

export function vatRatePercentFromProduct(product: { taxes?: MoloniTaxRow[] } | null | undefined): number {
  if (!product?.taxes?.length) return 0;
  for (const t of product.taxes) {
    if (taxRowSaftType(t) !== 1) continue;
    const tax = t.tax || {};
    const v = t.value ?? tax.value;
    return Number(v ?? 0);
  }
  return 0;
}

/** PVP (com IVA) para ecrã: 2 casas decimais. */
export function pvpFromPv(pv: number, ratePercent: number): number {
  const pvN = Number(pv);
  if (!Number.isFinite(pvN)) return 0;
  if (ratePercent <= -100) return Math.round(pvN * 100) / 100;
  const gross = pvN * (1 + ratePercent / 100);
  return Math.round(gross * 100) / 100;
}

/** PV sem IVA (Moloni guarda este): PVP arredondado a 2 dp, depois / (1 + tax%). Resultado com 4 dp. */
export function pvFromPvp(pvp: number, ratePercent: number): number {
  const pvpN = Number(pvp);
  if (!Number.isFinite(pvpN)) return 0;
  const pvp2 = Math.round(pvpN * 100) / 100;
  if (ratePercent <= -100) return Math.round(pvp2 * 10000) / 10000;
  const pv = pvp2 / (1 + ratePercent / 100);
  return Math.round(pv * 10000) / 10000;
}

export function vatRateFromLineTaxes(taxes: MoloniTaxRow[] | undefined): number {
  if (!taxes?.length) return 0;
  for (const t of taxes) {
    if (taxRowSaftType(t) !== 1) continue;
    const tax = t.tax || {};
    return Number(t.value ?? tax.value ?? 0);
  }
  return 0;
}
