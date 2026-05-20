"""Apply an ExtractedInvoice to Moloni: resolve subcategories (creating any missing),
match-or-create each product line, then create the supplier invoice. Sequential,
deterministic, yields typed progress events for the SSE stream.

All Moloni calls go through MoloniClient (the existing async client). No MCP."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

from app.agent.prompts import parse_supplier_config, validate_supplier_config
from app.agent.schema import ExtractedInvoice
from app.config import Settings
from app.moloni_categories import fetch_categories_level
from app.moloni_client import MoloniAPIError, MoloniClient

log = logging.getLogger("agent.applier")

# Small delay between Moloni writes — polite, avoids burst rate limits.
_INTER_CALL_SLEEP = 0.1


async def _fetch_products_in_category(
    client: MoloniClient, company_id: int, category_id: int
) -> dict[str, Any]:
    """Fetch all products in a single category; return a ref_upper→product dict.
    Moloni's products/getAll filters reliably by category_id."""
    try:
        results = await client.post_all_pages(
            "products/getAll",
            {"company_id": company_id, "category_id": category_id},
        )
    except MoloniAPIError as e:
        log.warning("applier: products/getAll(cat=%s) failed: %s", category_id, e)
        return {}
    by_ref: dict[str, Any] = {}
    for p in results or []:
        ref = str(p.get("reference", "")).strip().upper()
        if ref:
            by_ref[ref] = p
    log.info("applier: fetched %d products from category %s", len(by_ref), category_id)
    return by_ref


def _build_product_insert_body(
    company_id: int,
    *,
    reference: str,
    name: str,
    summary: str,
    price: float,
    category_id: int,
    unit_id: int,
    tax_id: int,
    tax_value: float,
) -> dict[str, Any]:
    """Body for products/insert. Conservative defaults; stock disabled."""
    return {
        "company_id": company_id,
        "category_id": category_id,
        "type": 1,
        "name": name,
        "summary": summary or "",
        "reference": reference,
        "ean": "",
        "price": price,
        "unit_id": unit_id,
        "has_stock": 0,
        "stock": 0,
        "minimum_stock": 0,
        "pos_favorite": 0,
        "at_product_category": "",
        "exemption_reason": "",
        "taxes": [
            {
                "tax_id": tax_id,
                "value": tax_value,
                "order": 1,
                "cumulative": 0,
            }
        ],
        "suppliers": [],
        "properties": [],
    }


def _build_supplier_invoice_insert_body(
    company_id: int,
    extracted: ExtractedInvoice,
    *,
    line_product_ids: dict[str, int],
    defaults: dict[str, Any],
) -> dict[str, Any]:
    """Body for supplierInvoices/insert from the user-reviewed JSON + resolved product_ids."""
    products: list[dict[str, Any]] = []
    for i, line in enumerate(extracted.lines):
        pid = line_product_ids.get(line.reference)
        if not pid:
            continue
        products.append(
            {
                "product_id": pid,
                "name": line.name,
                "summary": line.summary or line.name,
                "qty": float(line.qty),
                "price": float(line.unit_cost),
                "discount": 0,
                "deduction_id": 0,
                "order": i,
                "exemption_reason": defaults.get("exemption_reason", "M10"),
                "warehouse_id": 0,
                "taxes": [],
            }
        )

    body = {
        "company_id": company_id,
        "date": extracted.header.date,
        "expiration_date": extracted.header.expiration_date or extracted.header.date,
        "maturity_date_id": int(defaults.get("maturity_date_id", 0)),
        "document_set_id": int(defaults.get("document_set_id", 0)),
        "supplier_id": int(defaults["supplier_id"]),
        "our_reference": "",
        "your_reference": extracted.header.invoice_number,
        "financial_discount": 0,
        "special_discount": 0,
        "notes": "",
        "status": 0,
        "products": products,
        "delivery_method_id": int(defaults.get("delivery_method_id", 0)),
        "delivery_datetime": f"{extracted.header.date} 00:00:00",
    }
    return body


async def apply_invoice(
    extracted: ExtractedInvoice,
    client: MoloniClient,
    settings: Settings,
) -> AsyncIterator[dict[str, Any]]:
    """Yield a stream of dict events. Caller wraps as SSE."""
    company_id = settings.moloni_company_id
    supplier_slug = extracted.supplier.slug

    yield {"type": "started"}

    try:
        config = parse_supplier_config(supplier_slug)
    except FileNotFoundError:
        yield {
            "type": "invoice_error",
            "message": (
                f"No supplier rules found for slug {supplier_slug!r}. "
                "Save them via Fornecedores IA first."
            ),
        }
        yield {"type": "done"}
        return

    missing = validate_supplier_config(config)
    if missing:
        yield {
            "type": "invoice_error",
            "message": (
                f"Supplier rules ({supplier_slug}) missing or invalid in the "
                f"'## Moloni Configuration' section: {', '.join(missing)}."
            ),
        }
        yield {"type": "done"}
        return

    parent_cat: int = config["parent_category_id"]
    defaults: dict[str, Any] = config

    # ── Step 1: resolve subcategories. Build name→id map, create missing.
    try:
        subcats = await fetch_categories_level(client, company_id, parent_cat)
    except MoloniAPIError as e:
        yield {"type": "invoice_error", "message": f"Failed to list subcategories: {e}"}
        yield {"type": "done"}
        return

    by_name = {str(c.get("name", "")).strip().upper(): int(c["category_id"]) for c in subcats or []}

    needed = sorted({line.subcategory_name.strip().upper() for line in extracted.lines if line.subcategory_name})
    missing = [name for name in needed if name not in by_name]

    yield {"type": "subcategory_lookup", "needs_creation": missing}

    for name in missing:
        try:
            res = await client.post(
                "productCategories/insert",
                {
                    "company_id": company_id,
                    "parent_id": parent_cat,
                    "name": name,
                    "description": "",
                    "pos_enabled": 1,
                },
            )
            new_id = int(res.get("category_id", 0)) if isinstance(res, dict) else 0
            if not new_id:
                yield {"type": "invoice_error", "message": f"Subcategory create returned no id: {name}"}
                yield {"type": "done"}
                return
            by_name[name] = new_id
            yield {"type": "subcategory_created", "name": name, "category_id": new_id}
            await asyncio.sleep(_INTER_CALL_SLEEP)
        except MoloniAPIError as e:
            yield {"type": "invoice_error", "message": f"Subcategory {name} create failed: {e}"}
            yield {"type": "done"}
            return

    # ── Step 2 & 3: per line — look up by reference (lazy per-subcategory cache), create if missing.
    # Moloni's products/getAll filters reliably by category_id but not by reference.
    # We fetch each subcategory once on demand and cache the results for the run.
    cat_cache: dict[int, dict[str, Any]] = {}  # category_id → {ref_upper: product}
    line_product_ids: dict[str, int] = {}

    for line in extracted.lines:
        ref_key = line.reference.strip().upper()
        cat_id = by_name.get(line.subcategory_name.strip().upper(), parent_cat)

        if cat_id not in cat_cache:
            cat_cache[cat_id] = await _fetch_products_in_category(client, company_id, cat_id)
            await asyncio.sleep(_INTER_CALL_SLEEP)

        match = cat_cache[cat_id].get(ref_key)

        if match:
            pid = int(match["product_id"])
            line_product_ids[line.reference] = pid
            yield {"type": "line_matched", "reference": line.reference, "product_id": pid}
            continue

        yield {"type": "line_creating", "reference": line.reference}
        cat_id = by_name.get(line.subcategory_name.strip().upper(), parent_cat)
        body = _build_product_insert_body(
            company_id,
            reference=line.reference,
            name=line.name,
            summary=line.summary or line.name,
            price=float(line.moloni_price_no_vat or 0.0),
            category_id=cat_id,
            unit_id=int(defaults.get("unit_id", 0)),
            tax_id=int(defaults.get("tax_id", 0)),
            tax_value=float(defaults.get("tax_value", 23)),
        )
        try:
            res = await client.post("products/insert", body)
            new_pid = int(res.get("product_id", 0)) if isinstance(res, dict) else 0
            if not new_pid:
                yield {"type": "line_error", "reference": line.reference, "message": "insert returned no product_id"}
                continue
            line_product_ids[line.reference] = new_pid
            # Update cache so a duplicate reference later in the same invoice hits the match path
            cat_cache.setdefault(cat_id, {})[ref_key] = res
            yield {"type": "line_created", "reference": line.reference, "product_id": new_pid}
            await asyncio.sleep(_INTER_CALL_SLEEP)
        except MoloniAPIError as e:
            yield {"type": "line_error", "reference": line.reference, "message": str(e)}

    # ── Step 4: create supplier invoice if all lines have a product_id.
    failed_lines = [l for l in extracted.lines if l.reference not in line_product_ids]
    if failed_lines:
        yield {
            "type": "invoice_error",
            "message": f"{len(failed_lines)} line(s) without product_id; not creating invoice.",
        }
        yield {"type": "done"}
        return

    yield {"type": "invoice_creating"}
    body = _build_supplier_invoice_insert_body(
        company_id, extracted, line_product_ids=line_product_ids, defaults=defaults
    )
    try:
        res = await client.post("supplierInvoices/insert", body)
        if isinstance(res, dict) and res.get("valid") and res.get("document_id"):
            yield {"type": "invoice_created", "document_id": int(res["document_id"])}
        else:
            yield {"type": "invoice_error", "message": f"Moloni response: {res!r}"}
    except MoloniAPIError as e:
        yield {"type": "invoice_error", "message": str(e)}

    yield {"type": "done"}
