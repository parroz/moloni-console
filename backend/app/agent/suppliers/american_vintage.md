# American Vintage Invoice Rules

## Supplier

- name: AMERICAN VINTAGE
- supplier_id: 2620364
- VAT: FR36439224544

---

## Brand

* Brand name: AMERICAN VINTAGE
* Brand prefix: AMV

---

## Moloni Defaults

* unit_id: 2104808
* product VAT tax_id: 2537703
* product VAT value: 23

---

## Product Reference Rule

Format:

AMV-{ARTICLE_CODE}-{COLOR}-{SIZE}

Rules:

* Always uppercase
* Use hyphens as separators
* One variant per (article_code + color + size)

Examples:

* AMV-BOBY03FE26-TURQUOISE-S
* AMV-BOBY03FE26-TURQUOISE-M

---

## Pricing Rule

For each product:

1. PVP with VAT:
   unit_cost × 2.7

2. Moloni product price (without VAT):
   round(round((unit_cost × 2.7),2) / 1.23, 4)

Example:

* unit_cost: 55.80
* PVP with VAT: 150.66
* Moloni price: 122.49

---

## Product Lookup (do this ONCE, at the start)

Before generating references or checking existence, call:

```
list_products_by_category(category_id=6549313)
```

This returns every existing product under American Vintage in a single
batched call (~250 products today). Build a `reference → product` map
from the result.

Then, for each variant you generate, check the map:

* match → store the existing `product_id`
* no match → it's new; queue it for `create_product_in_moloni`

**Never call `search_product_by_reference` in a loop for these checks.**
It is a single-reference fallback only. Calling it in parallel for a
batch caused a 5-minute wall-clock wait in production and is forbidden.

---

## Category Rules

Parent category:

* name: American Vintage
* category_id: 6549313

Subcategory logic:

* Derive subcategory from product description
* Use the **main commercial product type**, not full sentence
* Keep names short and standardized
* Check if category exists before creating a new one

Examples:

| Description               | Subcategory |
| ------------------------- | ----------- |
| SWEAT ZIPPE ML CAPUCHE    | SWEAT ZIPPE |
| SWEAT ML COL ROND         | SWEAT       |
| T-SHIRT AMPLE MC COL ROND | T-SHIRT     |
| GILET COURT ML COL ROND   | GILET       |
| PANTALON                  | PANTALON    |
| SHORT COURT               | SHORT       |

Workflow:

1. Search existing categories under parent_id 6549313
2. If subcategory exists → use it
3. If not, create category under parent

---

## Supplier Invoice Defaults

* document_set_id: 546933
* supplier_id: 2032922
* maturity_date_id: 1506793  (60 Dias)
* delivery_method_id: 1733368 (Transportadora)
* status: 0

---

## Invoice Identification

Extract the invoice number from the document:

Example:
Facture N° 26021460

Use:
your_reference = "26021460"

---

## Supplier Invoice VAT Rule

- Do not apply VAT on supplier invoice lines
- Use taxes: []
- Use exemption_reason: M10

Important:
- Product catalog tax remains 23% for customer sales
- Supplier invoice line tax is 0% because this is an intra-community VAT-exempt supplier invoice

---

## Color rule

* Use the exact printed invoice color unless the user has approved normalization.
* Do not invent missing endings for truncated colors.

---

## Quantity Rule (CRITICAL)

American Vintage invoices use a **size × colour matrix** under each article.
A cell is filled (with a number) only when that variant is being shipped.
Empty cells, blank cells, or cells with `0` mean **NOT ordered** — those
variants do **not** exist on this invoice.

Rules:

* **Only create / reference / list product variants for cells with a quantity ≥ 1.**
* **Skip every (color × size) combination whose cell is empty, blank, dash, or 0.**
* Do not "fill in" missing sizes for completeness.
* Do not create products for colours that have no quantities at all.
* The total of the matrix cells must equal the line total quantity printed
  on the right of the article header. If they don't match, flag it and stop.

Examples (using the matrix structure in the PDF):

| Article    | Colour    | S | M | L | line total |
|------------|-----------|---|---|---|------------|
| BOBY09AE26 | ECRU      |   |   |   | 0          |
| BOBY09AE26 | TURQUOISE | 1 | 1 |   | 2          |

→ Produces ONLY:
1. `AMV-BOBY09AE26-TURQUOISE-S` qty 1
2. `AMV-BOBY09AE26-TURQUOISE-M` qty 1

→ Does NOT produce: ECRU-S, ECRU-M, ECRU-L, TURQUOISE-L (all empty cells).

---

## Example Expansion

Invoice line:

* Article: BOBY03FE26
* Description: SWEAT ZIPPE ML CAPUCHE
* Colour matrix:
  * TURQUOISE — S: 1, M: 1, L: (empty)
* Unit cost: 55.80

Produces (only the cells with qty ≥ 1):

1. AMV-BOBY03FE26-TURQUOISE-S

   * qty: 1
   * cost: 55.80
   * PVP with VAT: 150.66

2. AMV-BOBY03FE26-TURQUOISE-M

   * qty: 1
   * cost: 55.80
   * PVP with VAT: 150.66

---

## Notes

* Moloni product price is ALWAYS stored without VAT
* Supplier invoice price uses unit cost (not PVP)
* All logic is supplier-specific and must not be generalized incorrectly
