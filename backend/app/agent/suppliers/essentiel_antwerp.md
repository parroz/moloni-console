# ESSENTIEL ANTWERP Invoice Rules

ESSENTIEL ANTWERP is billed in Portugal by the distributor
**Andre Costa, S.A.** — the same distributor that bills OSKLEN. The Moloni
`supplier_id` refers to Andre Costa and is **shared with `osklen.md`**;
only the brand-specific parsing rules and the parent category differ.

The brand is identified by the **`Marca`** field in the invoice header
(`Marca  ESSENTIEL ANTWERP`). Always read that field to decide which
supplier ruleset applies — the biller, VAT number, layout and totals block
are identical across Andre Costa's brands.

---

## Moloni Configuration

This block is parsed by the applier. Keys must be valid Moloni IDs
(positive integers, except `exemption_reason`). Trailing parenthetical
comments are ignored. The applier refuses to run while any required key
is missing or non-numeric.

* parent_category_id: 10522572
* supplier_id: 3405723  (Andre Costa, S.A. — same as OSKLEN)
* document_set_id: 546933
* maturity_date_id: 1506783
* delivery_method_id: 1733368  (Transportadora)
* unit_id: 2104808
* tax_id: 2537703
* tax_value: 23
* exemption_reason:

`exemption_reason` is intentionally **empty** — see the VAT rule below.

Before the first run, resolve `parent_category_id` (create/find the
top-level **Essentiel Antwerp** category). Do **not** reuse OSKLEN's
`10360624`; the brands need separate trees.

---

## Supplier

- name: Andre Costa, S.A.
- brand billed: ESSENTIEL ANTWERP
- VAT: PT503560251
- address: R. Eng. Ferreira Dias 938, AZ2-1, 4100-246 Porto
- phone: 226199050
- email: info@andrecosta.pt
- bank: Millenium BCP · IBAN `PT50 0033 0000 0004 6425 5454 7` ·
  SWIFT `BCOMPTPL`

Our account with them: `Cliente 2261` · sales rep `Vendedor: Barbara`.

---

## Brand

* Brand name: ESSENTIEL ANTWERP
* Brand prefix: ESS

Unlike OSKLEN, the printed `Referência` does **not** carry a brand prefix
— it is a bare style name (`KAGU`, `KBEAUTY`, `KRIMSON`, `KURTIS`).
There is nothing to strip; prepend `ESS-` as-is.

---

## Invoice Layout Rule

Single product table with the column order:

`Referência | Descrição | Cor | XXS XS S M L XL XXL | Qtd | Preço | DC% | Valor`

Interpretation:

- **Referência** — the style name, used verbatim as the article code.
  A style may appear on **several consecutive rows**, one per colour
  (e.g. `KRIMSON` appears twice: `CC05 Camel` and `DA25 Dusty`).
- **Descrição** — garment type + fabric composition, run together with no
  separator (`CASACO 70%La30%Poliamida`). Strip the composition to get the
  subcategory.
- **Cor** — colour code + colour name (`K1BA Combo`, `BA08 Bear`,
  `CC05 Camel`, `DA25 Dusty`, `KM26 Khaki`).
- **size matrix** — one cell per size; a number means that many units of
  that size. Empty cells mean **not ordered** — skip them.
- **Qtd** — row total (sum of that row's size cells).
- **Preço** — unit price **without VAT**, before any discount.
- **DC%** — per-line discount %. Blank on the observed invoice, but the
  column exists and may be populated.
- **Valor** — `Qtd × Preço × (1 − DC%/100)`, without VAT.

### Reading the size matrix (IMPORTANT)

Only the apparel vocabulary appears on this brand:
`XXS · XS · S · M · L · XL · XXL`

The quantity digits are **column-aligned with the size headers**, and the
gaps are wide enough that eyeballing the rendered PDF is unreliable — on
the reference invoice a visual read put the first two rows in `XXS/XS`
when they are in fact `XS/S`. Align each digit to its header column by
horizontal position, not by counting spaces or guessing from the render.

---

## Product Reference Rule

Format:

ESS-{REFERENCIA}-{COLOR_NAME}-{SIZE}

Rules:

* Always uppercase
* Use hyphens as separators; normalize spaces inside a colour name to
  hyphens
* `REFERENCIA` is the printed style name verbatim — no prefix to strip
* Use the **colour name**, not the numeric/alpha colour code — this
  matches the OSKLEN convention for the same distributor
* Sizes are the printed letter codes (`XXS`…`XXL`)
* One variant per (referencia + colour + size)

Examples:

* `ESS-KAGU-COMBO-XS`
* `ESS-KBEAUTY-BEAR-S`
* `ESS-KRIMSON-CAMEL-S`
* `ESS-KURTIS-KHAKI-M`

### Colour-name caveat

Colour names on this invoice look **truncated** — `DA25 Dusty` is almost
certainly "Dusty Pink"/"Dusty Rose" cut to fit the column. Follow the
house rule: **preserve the printed text, never invent the missing ending.**

If truncation turns out to make references unstable across invoices (the
same colour printing differently on different documents), switch this rule
to use the **colour code** (`DA25`) instead of the name — the code is
short, unique and immune to truncation. That is what `indi_and_cold.md`
does. Do not mix the two conventions within a brand.

---

## Product Naming Rule

Use the garment description stripped of fabric composition, then
distinguish by colour and size:

{GARMENT} - {COLOR} - {SIZE}

Examples:

* `CASACO - COMBO - XS`
* `CASACO - CAMEL - S`
* `CAMISOLA - KHAKI - M`

Keep the fabric composition out of the variant title; it belongs in the
long description / summary.

---

## Pricing Rule

For each product:

1. PVP with VAT:
   unit_cost × 2.7    *(TODO: confirm ESSENTIEL ANTWERP markup — 2.7 is the
   house default; replace if this brand uses a different multiplier)*

2. Moloni product price (without VAT):
   round(round((unit_cost × 2.7), 2) / 1.23, 4)

Example:

* unit_cost: 173.00
* PVP with VAT: 467.10
* Moloni price: 379.7561

Notes:

* `unit_cost` = the **Preço** column — the per-unit price **before** the
  header discount and **without** VAT. Never use `Valor`, and never use a
  discount-reduced price.
* The 3 % header discount is **not** folded into the catalog price. It goes
  on the supplier invoice as `financial_discount` (see below).
* If a line ever has a `DC%`, apply it as the Moloni per-line `discount`,
  not by reducing the catalog `Preço`.

---

## Category Rules

Parent category:

* name: Essentiel Antwerp
* category_id: FILL_ME_IN  (also declared in `## Moloni Configuration`)

Subcategory logic:

* Derive from the leading word(s) of **Descrição**, with the fabric
  composition stripped
* The composition runs straight on from the garment word with no
  separator — cut at the first digit or `%`
* Keep names short and uppercase; preserve Portuguese spelling/accents

Examples observed on this invoice:

| Descrição (raw)                            | Subcategory |
| ------------------------------------------ | ----------- |
| CASACO 70%La30%Poliamida                   | CASACO      |
| CASACO 100%Poliester                       | CASACO      |
| CAMISOLA 47%La46%Acrilico5%Poliamida2%El   | CAMISOLA    |

Workflow:

1. List existing categories under `parent_category_id`
2. If the subcategory exists → use it
3. If not → create it under the parent

---

## Invoice Identification

Header block:

```
Número      5 SFTN/49274
Data        11/08/2026
Marca       ESSENTIEL ANTWERP
ATCUD       JFKVMD6Z-49274
Vencimento  11/08/2026
```

* `your_reference` = the **Número** value verbatim → `"5 SFTN/49274"`
  *(matches the OSKLEN convention, which uses `"5 SFTN/48100"`)*
* **Dates are `DD/MM/YYYY`.** `11/08/2026` → `2026-08-11`. Never read the
  first field as a month.
* `expiration_date` = the printed **Vencimento**. On this invoice
  Vencimento **equals** Data (`11/08/2026`) — i.e. due on issue, unlike
  OSKLEN's 60-day term. Prefer the printed date over computing one, and
  make sure `maturity_date_id` reflects the real term rather than being
  copied from `osklen.md`.

Other useful header fields:

- `S/Referência` — their order reference (`2EssentOI26-1cx`; `OI26` =
  Outono/Inverno 2026). Useful in `our_reference` or the invoice notes.
- `Vendedor` — sales rep (`Barbara`)
- `Cliente` — our account number (`2261`)
- Delivery: `Data Carga 11/08/2026 14:17`, `Local Carga Porto`,
  `Descarga Morada de Cliente`, `Transporte Chronopost`

---

## Supplier Invoice VAT Rule

- Andre Costa is a **Portuguese domestic supplier** → **23 % VAT applies on
  every line**. This is the opposite of the intra-community suppliers
  (American Vintage, RAINS, Yerse, Indi&Cold) — do **not** apply `M10` here.
- Use `taxes: [{ tax_id: <Moloni 23% VAT id>, value: 23, order: 1, cumulative: 0 }]`
  on each supplier-invoice line.
- Use `exemption_reason: ""` (empty) — there is no exemption.

The applier branches on `exemption_reason` in `## Moloni Configuration`:
non-empty → exempt mode (no line taxes); empty → taxed mode (applies
`tax_id` at `tax_value` % to every line).

---

## Invoice-Level Discount Rule

Andre Costa applies a header-level discount in the **`Desc.`** column of
the second header row.

**This is the `Desc.` column, not `Desc PP`.** The two are adjacent and
easy to confuse. On the reference invoice `Desc. = 3` and `Desc PP` is
empty (verified by column position: the `3` sits inside the `Desc.` column
bounds). `Desc PP` is the *pronto-pagamento* (early-settlement) discount
and is a different field — read them separately and never substitute one
for the other.

> Note: `osklen.md` currently attributes the same 3 % to "Desc PP". That
> label is wrong for this layout; the value lives in `Desc.`. The
> arithmetic is unaffected.

Totals block on the reference invoice:

```
Total Mercadoria   1 030,00
Descontos             30,90   ← 3 % of 1 030,00  (Desc.)
Desconto Linha            0   ← sum of per-line DC%
Valor Iliquido       999,10
Total IVA            229,79   ← 23 % of 999,10
Total a Pagar EUR  1 228,89
```

Extraction:

- Read the **`Desc.`** cell → `header.invoice_discount_pct` (e.g. `3` →
  `3.0`). Absent/empty → `0`.
- Read each row's `DC%` → `line.discount_pct`. Empty / dash / `0` → `0`.
- The two compose: per-line first, then header-level on the resulting
  subtotal.

The applier maps `invoice_discount_pct` → Moloni `financial_discount` and
`line.discount_pct` → Moloni per-line `discount`.

---

## Quantity Rule (CRITICAL)

A variant exists only if its size-matrix cell holds a positive number.

* Skip every cell that is empty, blank, dash, or `0`
* Never invent sizes that are not printed on the row
* The sum of a row's size cells **must equal** that row's printed `Qtd`
* `Qtd × Preço` **must equal** that row's printed `Valor` (when `DC%` is
  empty)
* The sum of all `Qtd` **must equal** the footer `Quantidade`

If any check fails, still emit the lines you did find and record the
discrepancy in `reconciliation.warnings`.

---

## Invoice-Level Checks

Validate before creating anything:

* `Σ(Qtd × Preço)` = **Total Mercadoria**
* `Total Mercadoria × Desc./100` = **Descontos**
* `Total Mercadoria − Descontos − Desconto Linha` = **Valor Ilíquido**
* `Valor Ilíquido × tax_value/100` = **Total IVA**
* `Valor Ilíquido + Total IVA` = **Total a Pagar EUR**
* `Σ Qtd` = footer **Quantidade**

---

## Worked Example (invoice 5 SFTN/49274, 11/08/2026)

| Referência | Descrição                                | Cor        | Sizes      | Qtd | Preço  | Valor  |
| ---------- | ---------------------------------------- | ---------- | ---------- | --- | ------ | ------ |
| KAGU       | CASACO 70%La30%Poliamida                 | K1BA Combo | XS:1, S:1  | 2   | 173,00 | 346,00 |
| KBEAUTY    | CASACO 100%Poliester                     | BA08 Bear  | XS:1, S:1  | 2   | 129,00 | 258,00 |
| KRIMSON    | CASACO 100%Poliester                     | CC05 Camel | S:1        | 1   | 121,00 | 121,00 |
| KRIMSON    | CASACO 100%Poliester                     | DA25 Dusty | S:1        | 1   | 121,00 | 121,00 |
| KURTIS     | CAMISOLA 47%La46%Acrilico5%Poliamida2%El | KM26 Khaki | S:1, M:1   | 2   |  92,00 | 184,00 |

Produces **8 variants / 8 units**:

1. `ESS-KAGU-COMBO-XS` — qty 1 — unit_cost 173,00 — CASACO
2. `ESS-KAGU-COMBO-S` — qty 1 — unit_cost 173,00 — CASACO
3. `ESS-KBEAUTY-BEAR-XS` — qty 1 — unit_cost 129,00 — CASACO
4. `ESS-KBEAUTY-BEAR-S` — qty 1 — unit_cost 129,00 — CASACO
5. `ESS-KRIMSON-CAMEL-S` — qty 1 — unit_cost 121,00 — CASACO
6. `ESS-KRIMSON-DUSTY-S` — qty 1 — unit_cost 121,00 — CASACO
7. `ESS-KURTIS-KHAKI-S` — qty 1 — unit_cost 92,00 — CAMISOLA
8. `ESS-KURTIS-KHAKI-M` — qty 1 — unit_cost 92,00 — CAMISOLA

Reconciliation:

* Σ qty = 2+2+1+1+2 = **8** ✓ matches footer `Quantidade 8`
* Σ (Qtd × Preço) = 346,00 + 258,00 + 121,00 + 121,00 + 184,00 =
  **1 030,00** ✓ matches `Total Mercadoria`
* Discount 3 % of 1 030,00 = **30,90** ✓ matches `Descontos`
* 1 030,00 − 30,90 = **999,10** ✓ matches `Valor Ilíquido`
* 999,10 × 23 % = **229,79** ✓ matches `Total IVA`
* 999,10 + 229,79 = **1 228,89** ✓ matches `Total a Pagar EUR`

---

## Notes

* Moloni product price is ALWAYS stored without VAT
* Supplier invoice line price uses the unit cost (`Preço`), not the PVP
* One `Referência` may span several rows — one per colour. Treat each row
  as a distinct (style × colour) combination.
* Only the apparel size vocabulary appears for this brand. If a footwear
  table (EU numeric 34–42) ever shows up, follow `osklen.md` and keep the
  two size vocabularies strictly apart.
* This brand shares a `supplier_id` with OSKLEN but must have its own
  parent category — never merge the two product trees.
