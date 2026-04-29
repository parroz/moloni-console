/** Moloni product tax helpers (IVA saft_type === 1). */

export type MoloniTaxRow = {
  tax_id?: number;
  value?: number;
  tax?: { saft_type?: number; value?: number };
};

export function vatRatePercentFromProduct(product: { taxes?: MoloniTaxRow[] } | null | undefined): number {
  if (!product?.taxes?.length) return 0;
  for (const t of product.taxes) {
    const tax = t.tax || {};
    if (Number(tax.saft_type) === 1) {
      const v = t.value ?? tax.value;
      return Number(v ?? 0);
    }
  }
  return 0;
}

export function pvpFromPv(pv: number, ratePercent: number): number {
  return Math.round(pv * (1 + ratePercent / 100) * 10000) / 10000;
}

export function pvFromPvp(pvp: number, ratePercent: number): number {
  if (ratePercent <= -100) return pvp;
  return Math.round((pvp / (1 + ratePercent / 100)) * 10000) / 10000;
}

export function vatRateFromLineTaxes(taxes: MoloniTaxRow[] | undefined): number {
  if (!taxes?.length) return 0;
  for (const t of taxes) {
    const tax = t.tax || {};
    if (Number(tax.saft_type) === 1) {
      return Number(t.value ?? tax.value ?? 0);
    }
  }
  return 0;
}
