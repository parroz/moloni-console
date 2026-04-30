"""Build Moloni products/update payloads from products/getOne."""

from __future__ import annotations

from typing import Any


def _tax_row_saft_type(t: dict[str, Any]) -> int:
    """Moloni usually nests saft_type under tax{}; some payloads expose it on the row."""
    tax = t.get("tax") or {}
    st = int(tax.get("saft_type", 0) or 0) or int(t.get("saft_type", 0) or 0)
    return st


def _has_iva_tax(taxes: list[dict[str, Any]] | None) -> bool:
    if not taxes:
        return False
    for t in taxes:
        if _tax_row_saft_type(t) != 1:
            continue
        tax = t.get("tax") or {}
        val = float(t.get("value", tax.get("value", 0)) or 0)
        if val > 0:
            return True
    return False


def build_product_update_body(company_id: int, full: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Merge getOne product with editable fields (ean, price, category_id, name, summary)."""
    product_id = int(full["product_id"])
    category_id = int(patch.get("category_id", full.get("category_id")))
    name = patch.get("name", full.get("name", ""))
    summary = patch.get("summary", full.get("summary", ""))
    reference = patch.get("reference", full.get("reference", ""))
    ean = patch.get("ean", full.get("ean", ""))
    if ean is None:
        ean = ""
    price = float(patch.get("price", full.get("price", 0)))

    taxes_out: list[dict[str, Any]] = []
    for t in full.get("taxes") or []:
        taxes_out.append(
            {
                "tax_id": int(t["tax_id"]),
                "value": float(t.get("value", 0)),
                "order": int(t.get("order", 1)),
                "cumulative": int(t.get("cumulative", 0)),
            }
        )

    exemption_reason = str(full.get("exemption_reason") or "")
    if not _has_iva_tax(taxes_out) and not exemption_reason:
        exemption_reason = str(patch.get("exemption_reason") or "M99")

    suppliers_out: list[dict[str, Any]] = []
    for s in full.get("suppliers") or []:
        row: dict[str, Any] = {
            "supplier_id": int(s["supplier_id"]),
            "cost_price": float(s.get("cost_price", 0)),
        }
        ref = s.get("reference")
        if ref:
            row["reference"] = str(ref)
        suppliers_out.append(row)

    properties_out: list[dict[str, Any]] = []
    for p in full.get("properties") or []:
        properties_out.append(
            {
                "property_id": int(p["property_id"]),
                "value": str(p.get("value", "")),
            }
        )

    body: dict[str, Any] = {
        "company_id": company_id,
        "product_id": product_id,
        "category_id": category_id,
        "type": int(full.get("type", 1)),
        "name": str(name),
        "summary": str(summary or ""),
        "reference": str(reference),
        "ean": str(ean),
        "price": price,
        "unit_id": int(full.get("unit_id", 0)),
        "has_stock": int(full.get("has_stock", 1)),
        "stock": float(full.get("stock", 0) or 0),
        "minimum_stock": float(full.get("minimum_stock", 0) or 0),
        "pos_favorite": int(full.get("pos_favorite", 0)),
        "at_product_category": str(full.get("at_product_category") or ""),
        "exemption_reason": exemption_reason,
        "taxes": taxes_out,
        "suppliers": suppliers_out,
        "properties": properties_out,
    }
    return body


def pvp_from_pv(pv: float, tax_rate_percent: float) -> float:
    """PVP (com IVA) for UI: 2 decimal places."""
    pv_n = float(pv)
    if tax_rate_percent <= -100:
        return round(pv_n, 2)
    gross = pv_n * (1 + tax_rate_percent / 100.0)
    return round(gross, 2)


def pv_from_pvp(pvp: float, tax_rate_percent: float) -> float:
    """Net unit price in Moloni (sem IVA): round PVP to 2 dp first, then PV to 4 dp (PV = PVP / (1 + tax%))."""
    pvp_n = round(float(pvp), 2)
    if tax_rate_percent <= -100:
        return round(pvp_n, 4)
    pv = pvp_n / (1 + tax_rate_percent / 100.0)
    return round(pv, 4)


def effective_retail_vat_percent(_full: dict[str, Any], *, fallback_percent: float) -> float:
    """Legal IVA % used only for PVP ↔ preço sem IVA (``pv_from_pvp`` / ``pvp_from_pv``).

    Moloni's product tax ``value`` is often the **IVA amount in currency** for the current net
    unit price, not the rate (6 / 13 / 23). Using it as a % breaks the reversal. Pricing
    therefore uses **only** the configured rate (``MOLONI_DEFAULT_RETAIL_VAT_PERCENT``, default 23).

    Model: ``pvp = pv + pv * (rate/100)`` ⇒ ``pv = pvp / (1 + rate/100)``.
    """
    f = float(fallback_percent)
    return f if f > 0 else 0.0
