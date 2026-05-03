"""Apply an ExtractedInvoice to Moloni: resolve subcategories (creating any missing),
match-or-create each product line, then create the supplier invoice. Sequential,
deterministic, yields typed progress events for the SSE stream.

All Moloni calls go through MoloniClient (the existing async client). No MCP."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

from app.agent.schema import ExtractedInvoice
from app.config import Settings
from app.moloni_categories import fetch_categories_level
from app.moloni_client import MoloniAPIError, MoloniClient

log = logging.getLogger("agent.applier")

# Small delay between Moloni writes — polite, avoids burst rate limits.
_INTER_CALL_SLEEP = 0.1


async def _list_products_under(
    client: MoloniClient, company_id: int, root_category_id: int
) -> list[dict[str, Any]]:
    """All products under a category and every descendant subcategory.
    BFS so we visit each subcategory once. Bounded depth+count to avoid runaway."""
    seen_cats: set[int] = set()
    seen_pids: set[int] = set()
    products: list[dict[str, Any]] = []
    queue: list[int] = [int(root_category_id)]
    visited = 0
    MAX_CATS = 100
    MAX_PRODUCTS = 10_000

    while queue and visited < MAX_CATS and len(products) < MAX_PRODUCTS:
        cid = queue.pop(0)
        if cid in seen_cats:
            continue
        seen_cats.add(cid)
        visited += 1

        # Children of this category (so we recurse)
        try:
            children = await fetch_categories_level(client, company_id, cid)
        except MoloniAPIError as e:
            log.warning("applier: fetch_categories_level(%s) failed: %s", cid, e)
            children = []
        for c in children or []:
            try:
                child_id = int(c["category_id"])
            except (KeyError, TypeError, ValueError):
                continue
            if child_id not in seen_cats:
                queue.append(child_id)

        # Products directly in this category
        try:
            page = await client.post_all_pages(
                "products/getAll",
                {"company_id": company_id, "category_id": cid},
            )
        except MoloniAPIError as e:
            log.warning("applier: products/getAll(%s) failed: %s", cid, e)
            continue
        for p in page or []:
            try:
                pid = int(p["product_id"])
            except (KeyError, TypeError, ValueError):
                continue
            if pid in seen_pids:
                continue
            seen_pids.add(pid)
            products.append(p)
            if len(products) >= MAX_PRODUCTS:
                break

    log.info(
        "applier: indexed %d products across %d categories under root=%s",
        len(products),
        visited,
        root_category_id,
    )
    return products


def _supplier_parent_category_id(supplier_slug: str) -> int:
    """Map supplier slug → parent category_id. Read from supplier rules in v2;
    for v1 we support American Vintage hardcoded."""
    # The parent category lives in the supplier .md but parsing markdown is overkill;
    # keep a mapping here, fall back to a sensible default.
    mapping = {
        "american_vintage": 6549313,
    }
    return mapping.get(supplier_slug, 0)


def _supplier_defaults(supplier_slug: str) -> dict[str, Any]:
    """Hardcoded supplier defaults for invoice creation. Will move into the .md
    parsing or a sidecar JSON in v2."""
    if supplier_slug == "american_vintage":
        return {
            "supplier_id": 2032922,
            "document_set_id": 546933,
            "maturity_date_id": 1506793,
            "delivery_method_id": 1733368,
            "unit_id": 2104808,
            "tax_id": 2537703,
            "tax_value": 23,
            "exemption_reason": "M10",
        }
    return {}


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
        "supplier_id": int(extracted.supplier.moloni_supplier_id or defaults.get("supplier_id", 0)),
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
    parent_cat = _supplier_parent_category_id(supplier_slug)
    defaults = _supplier_defaults(supplier_slug)

    yield {"type": "started"}

    if parent_cat <= 0:
        yield {
            "type": "invoice_error",
            "message": f"No supplier parent category configured for slug {supplier_slug!r}",
        }
        yield {"type": "done"}
        return

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

    # ── Step 2: index every product under the supplier's parent (recursive).
    existing = await _list_products_under(client, company_id, parent_cat)
    yield {"type": "products_indexing", "total_existing": len(existing)}

    by_ref = {str(p.get("reference", "")).strip().upper(): p for p in existing if p.get("reference")}

    # ── Step 3: per line — match or create. Sequential.
    line_product_ids: dict[str, int] = {}
    for line in extracted.lines:
        ref_key = line.reference.strip().upper()
        match = by_ref.get(ref_key)

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
