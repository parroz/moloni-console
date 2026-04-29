"""Build Moloni products/update payloads from products/getOne."""

from __future__ import annotations

from typing import Any


def _has_iva_tax(taxes: list[dict[str, Any]] | None) -> bool:
    if not taxes:
        return False
    for t in taxes:
        tax = t.get("tax") or {}
        if int(tax.get("saft_type", 0) or 0) == 1:
            val = float(t.get("value", 0) or 0)
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
    return round(pv * (1 + tax_rate_percent / 100.0), 4)


def pv_from_pvp(pvp: float, tax_rate_percent: float) -> float:
    if tax_rate_percent <= -100:
        return round(pvp, 4)
    return round(pvp / (1 + tax_rate_percent / 100.0), 4)


def primary_tax_rate_percent(full: dict[str, Any]) -> float:
    for t in full.get("taxes") or []:
        tax = t.get("tax") or {}
        if int(tax.get("saft_type", 0) or 0) == 1:
            return float(t.get("value", tax.get("value", 0)) or 0)
    return 0.0
