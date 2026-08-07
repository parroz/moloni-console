# INDI&COLD Invoice Rules

Supplier is Spanish (NIF/CIF `B20361309`), so the supplier invoice is an
intra-community VAT-exempt document — same handling as RAINS / American
Vintage / Yerse: `exemption_reason: M10`, line `taxes: []`.

---

## Moloni Configuration

This block is parsed by the applier. Keys must be valid Moloni IDs (positive
integers, except `exemption_reason`). Trailing parenthetical comments are
ignored. The applier refuses to run while any required key is missing or
non-numeric.

* parent_category_id: 6549307
* supplier_id: 2047168
* document_set_id: 546933
* maturity_date_id: 1506783
* delivery_method_id: 1733368  (Transportadora)
* unit_id: 2104808
* tax_id: 2537703
* tax_value: 23
* exemption_reason: M10

**Before the first run**, three IDs must be resolved in Moloni:

1. `parent_category_id` — create/find the top-level **Indi&Cold** category.
2. `supplier_id` — create/find the supplier **MAKIEXPORT S.L.U.**
   (not "Indi&Cold" — that is the trading brand; the legal biller is
   Makiexport, and the VAT number belongs to Makiexport).
3. `maturity_date_id` — this supplier is **30 days**, not the 60-day term
   (`1506793`) used by every other supplier in this repo.

---

## Supplier

- name: MAKIEXPORT S.L.U.
- brand: INDI&COLD
- VAT: ESB20361309  *(printed on the invoice as `NIF/CIF: B20361309`;
  prefix with `ES` for the intra-community format)*
- address: Portuetxe 24 Bajo, 20018 San Sebastián, Guipúzcoa, Spain
- bank: Banco Sabadell-Atlántico · IBAN `ES02 0081 5182 57 0001060612` ·
  BIC `BSABESBB`

---

## Brand

* Brand name: INDI&COLD
* Brand prefix: ICD

---

## Product Reference Rule

Format:

ICD-{ARTICLE_CODE}-{COLOR_CODE}-{SIZE}

Rules:

* Always uppercase
* Use hyphens as separators
* Use the **numeric colour code**, not the colour name (see below)
* Keep the article code exactly as printed, including any dot
  (`VI25NJ277S.PATY`, `VI25NJ575.SAM` — the dot is part of the code)
* Keep the size token exactly as printed (`34`, `36`, `S`, `M`, `UNIC`)
* One variant per (article_code + colour_code + size)

Examples:

* `ICD-VI25DD297-721-36`
* `ICD-VI25NJ277S.PATY-603-34`
* `ICD-VI25RH735-900-UNIC`

### Why the colour code and not the colour name

Indi&Cold prints colour as `<code> <name>` — e.g. `721 VERDE SAFARI`,
`900 NEGRO`, `107 CHOCOLATE`. The numeric code is the supplier's own stable
identifier, is unique per article, and never contains spaces. The colour
**name** still goes into the product name and summary, so searching Moloni
for "NEGRO" continues to work.

*(This differs from American Vintage, which has no colour code and therefore
uses the name. Do not "harmonise" the two — each follows its own supplier's
data.)*

---

## Pricing Rule

For each product:

1. PVP with VAT:
   unit_cost × 2.7

2. Moloni product price (without VAT):
   round(round((unit_cost × 2.7), 2) / 1.23, 4)

Example:

* unit_cost: 49.60
* PVP with VAT: 133.92
* Moloni price: 108.8780

Notes:

* The Moloni **product catalog** price is always stored without VAT, and the
  product carries the 23 % tax (`tax_id` / `tax_value` above) for customer
  sales.
* The **supplier invoice line** uses the raw `unit_cost`, never the PVP.

---

## Invoice Layout (three-level hierarchy)

Indi&Cold invoices are **not** a size × colour matrix like American Vintage.
They are a three-level indent tree, and the columns between the colour and
the quantity are `<size> <qty>` pairs laid out on a variable grid:

```
VI25DD297 49.60                                    <- article code + UNIT PRICE
   PANTALÓN                                        <- product type = SUBCATEGORY
      721 VERDE SAFARI   36 1   38 1      2   99.20 EUR
      ^colour code+name  ^size/qty pairs   ^row qty  ^row total
```

Reading rules:

* **Level 1 (bold)** — `ARTICLE_CODE UNIT_PRICE`. The trailing number on the
  article line is the **unit cost**, not a total.
* **Level 2** — the product type. This is the subcategory verbatim; no
  derivation needed (see Category Rules).
* **Level 3** — one row per colour. A single article may have several colour
  rows (e.g. `VI25NJ575.SAM` has both `107 CHOCOLATE` and `900 NEGRO`).
  Each colour row ends with its own total quantity and total EUR.

Within a colour row, the middle columns are `<size> <qty>` pairs. The size
column positions shift between rows — **read the pairs, not the column
offsets.**

Size tokens seen on real invoices:

| Kind         | Examples              |
| ------------ | --------------------- |
| Numeric      | `34` `36` `38` `40` `42` |
| Letter       | `S` `M`               |
| One-size     | `UNIC` (bags, accessories) |
| Non-product  | `UNIDAD` (shipping — see below) |

---

## Quantity Rule (CRITICAL)

* Emit a variant **only** for `<size> <qty>` pairs with qty ≥ 1.
* Never invent sizes that are not printed on the row.
* The sum of a colour row's size quantities **must equal that row's printed
  total quantity** column.
* `row_qty × unit_price` **must equal** the row's printed EUR total.
* The sum of every row total (including PORTES) must equal
  **Total Units** / **Total EUR** in the footer.

If any of these checks fails, still emit the lines you did find, and record
the discrepancy in `reconciliation.warnings`.

---

## PORTES (shipping) — do NOT create a product

The first block of every invoice is the freight charge:

```
PORTES 10.00
   OTROS
      UNI UNIDAD                          2    20.00 EUR
```

Rules:

* **Never** create a Moloni product for `PORTES`.
* Exclude it from `lines[]` entirely.
* Its value **is** part of the invoice total, so subtract it before
  reconciling the goods subtotal, and note it in
  `reconciliation.warnings` as e.g. `"PORTES 20.00 EUR excluded from lines"`.
* Handle the freight amount on the Moloni supplier invoice separately
  (manual line or shipping product), per the operator's workflow.

`OTROS` and `UNIDAD` appear **only** on the PORTES block; if you see them
elsewhere, flag it rather than guessing.

---

## Category Rules

Parent category:

* name: Indi&Cold
* category_id: FILL_ME_IN  (also declared in `## Moloni Configuration`)

Subcategory logic:

* The subcategory is the **level-2 line printed directly under the article
  code** — take it verbatim. Do **not** derive it from the article code or
  from the colour.
* Keep the Spanish spelling and accents exactly as printed.
* Check whether the subcategory exists under the parent before creating it.

Subcategories seen on real invoices:

| Printed    | Subcategory |
| ---------- | ----------- |
| PANTALÓN   | PANTALÓN    |
| FALDA      | FALDA       |
| VESTIDO    | VESTIDO     |
| BOLSOS     | BOLSOS      |
| OTROS      | *(PORTES only — never becomes a product)* |

Workflow:

1. List existing categories under `parent_category_id`
2. If the subcategory exists → use it
3. If not → create it under the parent

---

## Invoice Identification

The header block is:

```
Doc.    225FB2B-001325
Date:   25-09-2025
D.N.:   225AB2B-001309 / 225AB2B-001372
```

* `your_reference` = the **Doc.** value → `"225FB2B-001325"`
* **Dates are `DD-MM-YYYY`.** `25-09-2025` → `2025-09-25`. Never read the
  first field as a month.
* `D.N.` are the delivery-note numbers. They are *not* the invoice number —
  do not use them as `your_reference`. Put them in the invoice notes if the
  operator wants the traceability.

Payment terms: `GIRO VENCIMIENTO A 30 DIAS`, with the due date printed
below the totals (`636.800 EUR 27-10-2025` → `expiration_date: 2025-10-27`).
Prefer the printed due date over computing date + 30 days.

---

## Supplier Invoice VAT Rule

- Do not apply VAT on supplier invoice lines
- Use `taxes: []`
- Use `exemption_reason: M10`

The invoice carries no VAT line — `Total 636.80 EUR` equals
`NET TO PAY 636.80 EUR`, confirming the intra-community exemption. The
`Discount` field (`0,0`) uses a comma decimal separator; treat it as 0.

Product catalog tax remains 23 % for customer sales.

---

## Worked Example (invoice 225FB2B-001325, 25-09-2025)

| Article           | Subcat.  | Colour           | Sizes      | Unit  | Qty | Row EUR |
| ----------------- | -------- | ---------------- | ---------- | ----- | --- | ------- |
| *PORTES*          | *OTROS*  | *UNI UNIDAD*     | —          | 10.00 | 2   | 20.00   |
| VI25DD297         | PANTALÓN | 721 VERDE SAFARI | 36:1, 38:1 | 49.60 | 2   | 99.20   |
| VI25NJ273         | FALDA    | 900 NEGRO        | S:1        | 49.00 | 1   | 49.00   |
| VI25NJ277S.PATY   | PANTALÓN | 603 MARINO       | 34:1, 38:1 | 33.90 | 2   | 67.80   |
| VI25NJ278S.NOE    | PANTALÓN | 603 MARINO       | 40:1       | 33.90 | 1   | 33.90   |
| VI25NJ300         | VESTIDO  | 901 GRIS         | M:1        | 48.70 | 1   | 48.70   |
| VI25NJ301         | PANTALÓN | 901 GRIS         | 42:1       | 33.90 | 1   | 33.90   |
| VI25NJ575.SAM     | PANTALÓN | 107 CHOCOLATE    | 40:1       | 33.90 | 1   | 33.90   |
| VI25NJ575.SAM     | PANTALÓN | 900 NEGRO        | 40:2       | 33.90 | 2   | 67.80   |
| VI25RH735         | BOLSOS   | 900 NEGRO        | UNIC:1     | 45.50 | 1   | 45.50   |
| VI25VF256         | PANTALÓN | 800 TEJANO       | 34:1       | 45.70 | 1   | 45.70   |
| VI25VF261         | PANTALÓN | 707 OLIVA        | 36:1, 42:1 | 45.70 | 2   | 91.40   |

Produces **14 product variants / 15 units**:

1. `ICD-VI25DD297-721-36` — qty 1 — cost 49.60
2. `ICD-VI25DD297-721-38` — qty 1 — cost 49.60
3. `ICD-VI25NJ273-900-S` — qty 1 — cost 49.00
4. `ICD-VI25NJ277S.PATY-603-34` — qty 1 — cost 33.90
5. `ICD-VI25NJ277S.PATY-603-38` — qty 1 — cost 33.90
6. `ICD-VI25NJ278S.NOE-603-40` — qty 1 — cost 33.90
7. `ICD-VI25NJ300-901-M` — qty 1 — cost 48.70
8. `ICD-VI25NJ301-901-42` — qty 1 — cost 33.90
9. `ICD-VI25NJ575.SAM-107-40` — qty 1 — cost 33.90
10. `ICD-VI25NJ575.SAM-900-40` — qty **2** — cost 33.90
11. `ICD-VI25RH735-900-UNIC` — qty 1 — cost 45.50
12. `ICD-VI25VF256-800-34` — qty 1 — cost 45.70
13. `ICD-VI25VF261-707-36` — qty 1 — cost 45.70
14. `ICD-VI25VF261-707-42` — qty 1 — cost 45.70

Reconciliation:

* Goods subtotal: **616.80 EUR** (15 units)
* PORTES: **20.00 EUR** (2 units, excluded from `lines[]`)
* Invoice total: 616.80 + 20.00 = **636.80 EUR** ✓ matches footer
* Footer units: 15 + 2 = **17** ✓ matches `Total Units 17`

---

## Notes

* Moloni product price is ALWAYS stored without VAT
* Supplier invoice price uses unit cost (not PVP)
* Product names should carry the colour name for searchability, e.g.
  `PANTALÓN VI25DD297 VERDE SAFARI 36`
* All logic is supplier-specific and must not be generalized incorrectly
