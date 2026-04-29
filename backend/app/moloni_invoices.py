"""Supplier invoice getOne → supplierInvoices/update body."""

from __future__ import annotations

from typing import Any


def _line_taxes_for_update(line: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in line.get("taxes") or []:
        out.append(
            {
                "tax_id": int(t["tax_id"]),
                "value": float(t.get("value", 0)),
                "order": int(t.get("order", 1)),
                "cumulative": int(t.get("cumulative", 0)),
            }
        )
    return out


def _line_for_update(line: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    overrides = overrides or {}
    pid = int(line["product_id"])
    ovr = overrides.get(pid) or {}
    taxes = _line_taxes_for_update(line)
    exemption = str(ovr.get("exemption_reason", line.get("exemption_reason") or ""))
    if not taxes and not exemption:
        exemption = "M99"
    return {
        "product_id": pid,
        "name": str(ovr.get("name", line.get("name", ""))),
        "summary": str(ovr.get("summary", line.get("summary") or line.get("name", ""))),
        "qty": float(ovr.get("qty", line.get("qty", 0))),
        "price": float(ovr.get("price", line.get("price", 0))),
        "discount": float(ovr.get("discount", line.get("discount", 0) or 0)),
        "deduction_id": int(ovr.get("deduction_id", line.get("deduction_id", 0) or 0)),
        "order": int(ovr.get("order", line.get("order", 0) or 0)),
        "exemption_reason": exemption,
        "warehouse_id": int(ovr.get("warehouse_id", line.get("warehouse_id") or 0) or 0),
        "taxes": taxes,
    }


def build_supplier_invoice_update(
    doc: dict[str, Any],
    *,
    header: dict[str, Any] | None = None,
    line_overrides: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build POST body for supplierInvoices/update from getOne document."""
    header = header or {}
    line_overrides = line_overrides or {}
    cid = int(doc["company_id"])
    did = int(doc["document_id"])

    products = [_line_for_update(p, line_overrides) for p in doc.get("products") or []]

    assoc: list[dict[str, Any]] = []
    for a in doc.get("associated_documents") or []:
        assoc.append({"associated_id": int(a["associated_id"]), "value": float(a.get("value", 0))})

    body: dict[str, Any] = {
        "company_id": cid,
        "document_id": did,
        "date": str(header.get("date", doc.get("date", "")))[:10],
        "expiration_date": str(header.get("expiration_date", doc.get("expiration_date", "")))[:10],
        "maturity_date_id": int(header.get("maturity_date_id", doc.get("maturity_date_id", 0)) or 0),
        "document_set_id": int(header.get("document_set_id", doc.get("document_set_id", 0))),
        "supplier_id": int(header.get("supplier_id", doc.get("supplier_id", 0))),
        "our_reference": str(header.get("our_reference", doc.get("our_reference", "") or "")),
        "your_reference": str(header.get("your_reference", doc.get("your_reference", "") or "")),
        "financial_discount": float(header.get("financial_discount", doc.get("financial_discount", 0) or 0)),
        "special_discount": float(header.get("special_discount", doc.get("special_discount", 0) or 0)),
        "related_documents_notes": str(
            header.get("related_documents_notes", doc.get("related_documents_notes", "") or "")
        ),
        "notes": str(header.get("notes", doc.get("notes", "") or "")),
        "status": int(header.get("status", doc.get("status", 0) or 0)),
        "products": products,
        "associated_documents": assoc,
        "delivery_method_id": int(header.get("delivery_method_id", doc.get("delivery_method_id", 0) or 0)),
        "delivery_datetime": str(header.get("delivery_datetime", doc.get("delivery_datetime", "") or ""))[:19],
        "delivery_departure_address": str(
            header.get("delivery_departure_address", doc.get("delivery_departure_address", "") or "")
        ),
        "delivery_departure_city": str(
            header.get("delivery_departure_city", doc.get("delivery_departure_city", "") or "")
        ),
        "delivery_departure_zip_code": str(
            header.get("delivery_departure_zip_code", doc.get("delivery_departure_zip_code", "") or "")
        ),
        "delivery_departure_country": int(
            header.get("delivery_departure_country", doc.get("delivery_departure_country", 0) or 0)
        ),
        "delivery_destination_address": str(
            header.get("delivery_destination_address", doc.get("delivery_destination_address", "") or "")
        ),
        "delivery_destination_city": str(
            header.get("delivery_destination_city", doc.get("delivery_destination_city", "") or "")
        ),
        "delivery_destination_zip_code": str(
            header.get("delivery_destination_zip_code", doc.get("delivery_destination_zip_code", "") or "")
        ),
        "delivery_destination_country": int(
            header.get("delivery_destination_country", doc.get("delivery_destination_country", 0) or 0)
        ),
        "vehicle_id": int(header.get("vehicle_id", doc.get("vehicle_id", 0) or 0)),
        "vehicle_name": str(header.get("vehicle_name", doc.get("vehicle_name", "") or "")),
        "vehicle_number_plate": str(header.get("vehicle_number_plate", doc.get("vehicle_number_plate", "") or "")),
    }
    ex_c = doc.get("exchange_currency_id")
    if ex_c:
        body["exchange_currency_id"] = int(ex_c)
        body["exchange_rate"] = float(doc.get("exchange_rate", 1) or 1)
    return body
