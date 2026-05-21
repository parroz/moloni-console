from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

log = logging.getLogger("moloni.routes")

from app.config import Settings, get_settings
from app.deps import MoloniDep, require_auth
from app.moloni_categories import (
    fetch_all_categories_parallel,
    fetch_categories_level,
    normalize_category_list,
)
from app.moloni_client import MoloniAPIError
from app.moloni_invoices import build_supplier_invoice_update
from app.moloni_products import (
    build_product_update_body,
    effective_retail_vat_percent,
    pv_from_pvp,
)

router = APIRouter(prefix="/api")


class LoginBody(BaseModel):
    password: str


@router.post("/auth/login")
async def login(request: Request, body: LoginBody, settings: Settings = Depends(get_settings)) -> dict[str, bool]:
    if body.password != settings.console_password:
        raise HTTPException(status_code=401, detail="Invalid password")
    request.session["auth"] = True
    return {"ok": True}


@router.post("/auth/logout")
async def logout(request: Request) -> dict[str, bool]:
    request.session.clear()
    return {"ok": True}


@router.get("/auth/me")
async def me(request: Request) -> dict[str, bool]:
    require_auth(request)
    return {"authenticated": True}


@router.get("/config")
async def app_config(request: Request, settings: Settings = Depends(get_settings)) -> dict[str, float]:
    """Non-secret UI settings (authenticated)."""
    require_auth(request)
    return {"retail_vat_percent": float(settings.moloni_default_retail_vat_percent)}


# --- Moloni proxy (authenticated) ---


@router.get("/moloni/supplier-invoices")
async def list_supplier_invoices(
    request: Request,
    moloni: MoloniDep,
    settings: Settings = Depends(get_settings),
    supplier_id: int | None = None,
) -> list[Any]:
    require_auth(request)
    cid = settings.moloni_company_id
    body: dict[str, Any] = {"company_id": cid, "qty": 50, "offset": 0}
    if supplier_id:
        body["supplier_id"] = supplier_id
    return await moloni.post_all_pages("supplierInvoices/getAll", body)


@router.get("/moloni/supplier-invoices/{document_id}")
async def get_supplier_invoice(request: Request, moloni: MoloniDep, document_id: int, settings: Settings = Depends(get_settings)) -> Any:
    require_auth(request)
    return await moloni.post(
        "supplierInvoices/getOne",
        {"company_id": settings.moloni_company_id, "document_id": document_id},
    )


@router.get("/moloni/supplier-invoices/{document_id}/detail")
async def get_supplier_invoice_detail(
    request: Request,
    moloni: MoloniDep,
    document_id: int,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Invoice getOne + products/getOne (parallel) + productCategories/getOne per product category."""
    require_auth(request)
    cid = settings.moloni_company_id
    doc = await moloni.post("supplierInvoices/getOne", {"company_id": cid, "document_id": document_id})
    if not isinstance(doc, dict) or "products" not in doc:
        raise HTTPException(404, "Invoice not found")

    # Unique product IDs preserving first-seen order
    seen: set[int] = set()
    unique_pids: list[int] = []
    for p in doc.get("products") or []:
        pid = int(p["product_id"])
        if pid not in seen:
            seen.add(pid)
            unique_pids.append(pid)

    # Fetch all products in parallel
    product_results = await asyncio.gather(
        *[moloni.post("products/getOne", {"company_id": cid, "product_id": pid}) for pid in unique_pids]
    )
    products: dict[str, Any] = {str(pid): res for pid, res in zip(unique_pids, product_results)}

    # Collect unique category IDs from the fetched products
    cat_ids: list[int] = list(
        dict.fromkeys(
            int(p["category_id"])
            for p in products.values()
            if isinstance(p, dict) and p.get("category_id")
        )
    )

    # Fetch each category in parallel to resolve parent_id (for dropdown pre-fill)
    categories: dict[str, Any] = {}
    if cat_ids:
        cat_results = await asyncio.gather(
            *[moloni.post("productCategories/getOne", {"company_id": cid, "category_id": cid_val}) for cid_val in cat_ids],
            return_exceptions=True,
        )
        for cid_val, res in zip(cat_ids, cat_results):
            if isinstance(res, dict) and "category_id" in res:
                categories[str(cid_val)] = {
                    "category_id": int(res["category_id"]),
                    "parent_id": int(res.get("parent_id", 0) or 0),
                    "name": str(res.get("name", "")),
                }

    return {
        "document": doc,
        "products": products,
        "categories": categories,
        "retail_vat_percent": float(settings.moloni_default_retail_vat_percent),
    }


class SupplierInvoiceHeaderUpdate(BaseModel):
    date: str | None = None
    expiration_date: str | None = None
    maturity_date_id: int | None = None
    document_set_id: int | None = None
    supplier_id: int | None = None
    our_reference: str | None = None
    your_reference: str | None = None
    financial_discount: float | None = None
    special_discount: float | None = None
    related_documents_notes: str | None = None
    notes: str | None = None
    status: int | None = None


class SupplierInvoiceLinePatch(BaseModel):
    product_id: int
    qty: float | None = None
    price: float | None = None
    discount: float | None = None
    name: str | None = None
    summary: str | None = None
    exemption_reason: str | None = None


class SupplierInvoiceUpdateBody(BaseModel):
    header: SupplierInvoiceHeaderUpdate | None = None
    lines: list[SupplierInvoiceLinePatch] | None = None


@router.put("/moloni/supplier-invoices/{document_id}")
async def update_supplier_invoice(
    request: Request,
    moloni: MoloniDep,
    document_id: int,
    body: SupplierInvoiceUpdateBody,
    settings: Settings = Depends(get_settings),
) -> Any:
    require_auth(request)
    doc = await moloni.post(
        "supplierInvoices/getOne",
        {"company_id": settings.moloni_company_id, "document_id": document_id},
    )
    if not isinstance(doc, dict) or "document_id" not in doc:
        raise HTTPException(404, "Invoice not found")
    header = body.header.model_dump(exclude_none=True) if body.header else {}
    line_overrides: dict[int, dict[str, Any]] = {}
    if body.lines:
        for ln in body.lines:
            line_overrides[ln.product_id] = ln.model_dump(exclude_none=True)
    payload = build_supplier_invoice_update(doc, header=header or None, line_overrides=line_overrides or None)
    return await moloni.post("supplierInvoices/update", payload)


@router.get("/moloni/suppliers")
async def list_suppliers(request: Request, moloni: MoloniDep, settings: Settings = Depends(get_settings)) -> list[Any]:
    require_auth(request)
    return await moloni.post_all_pages(
        "suppliers/getAll",
        {"company_id": settings.moloni_company_id},
    )


@router.get("/moloni/suppliers/{supplier_id}")
async def get_supplier(request: Request, moloni: MoloniDep, supplier_id: int, settings: Settings = Depends(get_settings)) -> Any:
    require_auth(request)
    return await moloni.post(
        "suppliers/getOne",
        {"company_id": settings.moloni_company_id, "supplier_id": supplier_id},
    )


class SupplierUpdateBody(BaseModel):
    vat: str | int | None = None
    number: str | None = None
    name: str | None = None
    language_id: int | None = None
    address: str | None = None
    zip_code: str | None = None
    city: str | None = None
    country_id: int | None = None
    email: str | None = None
    website: str | None = None
    phone: str | None = None
    fax: str | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    notes: str | None = None
    maturity_date_id: int | None = None
    discount: float | None = None
    credit_limit: float | None = None
    qty_copies_document: int | None = None
    payment_method_id: int | None = None
    delivery_method_id: int | None = None
    field_notes: str | None = None


@router.put("/moloni/suppliers/{supplier_id}")
async def update_supplier(
    request: Request,
    moloni: MoloniDep,
    supplier_id: int,
    body: SupplierUpdateBody,
    settings: Settings = Depends(get_settings),
) -> Any:
    require_auth(request)
    cur = await moloni.post(
        "suppliers/getOne",
        {"company_id": settings.moloni_company_id, "supplier_id": supplier_id},
    )
    if not isinstance(cur, dict) or "supplier_id" not in cur:
        raise HTTPException(404, "Supplier not found")
    patch = body.model_dump(exclude_none=True)
    merged = {**cur, **patch}
    payload = {
        "company_id": settings.moloni_company_id,
        "supplier_id": supplier_id,
        "vat": merged.get("vat"),
        "number": merged.get("number"),
        "name": merged.get("name"),
        "language_id": int(merged.get("language_id", 0)),
        "address": merged.get("address", "") or "",
        "zip_code": merged.get("zip_code", "") or "",
        "city": merged.get("city", "") or "",
        "country_id": int(merged.get("country_id", 0)),
        "email": merged.get("email", "") or "",
        "website": merged.get("website", "") or "",
        "phone": merged.get("phone", "") or "",
        "fax": merged.get("fax", "") or "",
        "contact_name": merged.get("contact_name", "") or "",
        "contact_email": merged.get("contact_email", "") or "",
        "contact_phone": merged.get("contact_phone", "") or "",
        "notes": merged.get("notes", "") or "",
        "maturity_date_id": int(merged.get("maturity_date_id", 0)),
        "discount": float(merged.get("discount", 0) or 0),
        "credit_limit": float(merged.get("credit_limit", 0) or 0),
        "qty_copies_document": int(merged.get("qty_copies_document", 1) or 1),
        "payment_method_id": int(merged.get("payment_method_id", 0)),
        "field_notes": merged.get("field_notes", "") or "",
    }
    dm = merged.get("delivery_method_id")
    if dm is not None and int(dm) > 0:
        payload["delivery_method_id"] = int(dm)
    return await moloni.post("suppliers/update", payload)


@router.get("/moloni/categories")
async def list_categories(
    request: Request,
    moloni: MoloniDep,
    parent_id: int = 0,
    recursive: int = 0,
    settings: Settings = Depends(get_settings),
) -> list[Any]:
    """
    Default: one Moloni level (``parent_id``, usually ``0`` for roots) — fast for the Categories UI.
    ``recursive=1`` (or any non-zero): full tree via parallel BFS (dropdowns). Uses int so ``?recursive=1`` always works.
    """
    require_auth(request)
    if recursive != 0:
        raw = await fetch_all_categories_parallel(moloni, settings.moloni_company_id)
        return normalize_category_list(raw if isinstance(raw, list) else [])
    raw_level = await fetch_categories_level(moloni, settings.moloni_company_id, parent_id)
    return normalize_category_list(raw_level if isinstance(raw_level, list) else [])


@router.get("/moloni/categories/{category_id}")
async def get_category(request: Request, moloni: MoloniDep, category_id: int, settings: Settings = Depends(get_settings)) -> Any:
    require_auth(request)
    return await moloni.post(
        "productCategories/getOne",
        {"company_id": settings.moloni_company_id, "category_id": category_id},
    )


class CategoryCreate(BaseModel):
    parent_id: int = 0
    name: str
    description: str = ""
    pos_enabled: int = 1


@router.post("/moloni/categories")
async def create_category(request: Request, moloni: MoloniDep, body: CategoryCreate, settings: Settings = Depends(get_settings)) -> Any:
    require_auth(request)
    return await moloni.post(
        "productCategories/insert",
        {
            "company_id": settings.moloni_company_id,
            "parent_id": body.parent_id,
            "name": body.name,
            "description": body.description,
            "pos_enabled": body.pos_enabled,
        },
    )


class CategoryUpdate(BaseModel):
    parent_id: int
    name: str
    description: str = ""
    pos_enabled: int = 1


@router.put("/moloni/categories/{category_id}")
async def update_category(
    request: Request,
    moloni: MoloniDep,
    category_id: int,
    body: CategoryUpdate,
    settings: Settings = Depends(get_settings),
) -> Any:
    require_auth(request)
    return await moloni.post(
        "productCategories/update",
        {
            "company_id": settings.moloni_company_id,
            "category_id": category_id,
            "parent_id": body.parent_id,
            "name": body.name,
            "description": body.description,
            "pos_enabled": body.pos_enabled,
        },
    )


@router.delete("/moloni/categories/{category_id}")
async def delete_category(request: Request, moloni: MoloniDep, category_id: int, settings: Settings = Depends(get_settings)) -> Any:
    require_auth(request)
    return await moloni.post(
        "productCategories/delete",
        {"company_id": settings.moloni_company_id, "category_id": category_id},
    )


@router.get("/moloni/products")
async def list_products(
    request: Request,
    moloni: MoloniDep,
    category_id: int,
    offset: int = 0,
    settings: Settings = Depends(get_settings),
) -> list[Any]:
    require_auth(request)
    return await moloni.post(
        "products/getAll",
        {
            "company_id": settings.moloni_company_id,
            "category_id": category_id,
            "qty": 50,
            "offset": offset,
        },
    )


@router.get("/moloni/products/{product_id}")
async def get_product(request: Request, moloni: MoloniDep, product_id: int, settings: Settings = Depends(get_settings)) -> Any:
    require_auth(request)
    return await moloni.post(
        "products/getOne",
        {"company_id": settings.moloni_company_id, "product_id": product_id},
    )


class ProductPatch(BaseModel):
    ean: str | None = None
    price: float | None = None
    """Preço de venda com IVA (PVP); convertido para price sem IVA antes de gravar no Moloni."""
    pvp: float | None = None
    category_id: int | None = None
    name: str | None = None
    summary: str | None = None
    reference: str | None = None


@router.put("/moloni/products/{product_id}")
async def update_product(
    request: Request,
    moloni: MoloniDep,
    product_id: int,
    body: ProductPatch,
    settings: Settings = Depends(get_settings),
) -> Any:
    require_auth(request)
    full = await moloni.post(
        "products/getOne",
        {"company_id": settings.moloni_company_id, "product_id": product_id},
    )
    if not isinstance(full, dict) or "product_id" not in full:
        raise HTTPException(404, "Product not found")
    patch = body.model_dump(exclude_none=True)
    pvp = patch.pop("pvp", None)
    if pvp is not None:
        rate = effective_retail_vat_percent(full, fallback_percent=settings.moloni_default_retail_vat_percent)
        patch["price"] = pv_from_pvp(float(pvp), rate)
    payload = build_product_update_body(settings.moloni_company_id, full, patch)
    return await moloni.post("products/update", payload)


class BulkProductRow(BaseModel):
    product_id: int
    ean: str | None = None
    price: float | None = None
    pvp: float | None = None
    category_id: int | None = None
    name: str | None = None
    summary: str | None = None


class BulkProductUpdateBody(BaseModel):
    items: list[BulkProductRow]


@router.post("/moloni/products/bulk-update")
async def bulk_update_products(
    request: Request,
    moloni: MoloniDep,
    body: BulkProductUpdateBody,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    require_auth(request)
    results: list[dict[str, Any]] = []
    for row in body.items:
        try:
            full = await moloni.post(
                "products/getOne",
                {"company_id": settings.moloni_company_id, "product_id": row.product_id},
            )
            if not isinstance(full, dict):
                results.append({"product_id": row.product_id, "ok": False, "error": "getOne failed"})
                continue
            patch: dict[str, Any] = {}
            if row.ean is not None:
                patch["ean"] = row.ean
            if row.name is not None:
                patch["name"] = row.name
            if row.summary is not None:
                patch["summary"] = row.summary
            if row.category_id is not None:
                patch["category_id"] = row.category_id
            # PVP (com IVA) wins over raw net price so accidental price:0 never skips the retail update.
            if row.pvp is not None:
                rate = effective_retail_vat_percent(full, fallback_percent=settings.moloni_default_retail_vat_percent)
                patch["price"] = pv_from_pvp(float(row.pvp), rate)
            elif row.price is not None:
                patch["price"] = row.price
            payload = build_product_update_body(settings.moloni_company_id, full, patch)
            # Diagnostic log: compare the incoming patch against the existing
            # product so we can see which fields are actually changing. If a
            # field looks correct in the patch but doesn't update in Moloni,
            # this trail tells us whether it's our code or Moloni's quirk.
            log.info(
                "bulk_update product_id=%s patch_keys=%s "
                "name: %r → %r | ean: %r → %r | price: %r → %r | category_id: %r → %r",
                row.product_id,
                sorted(patch.keys()),
                full.get("name"), payload.get("name"),
                full.get("ean"), payload.get("ean"),
                full.get("price"), payload.get("price"),
                full.get("category_id"), payload.get("category_id"),
            )
            res = await moloni.post("products/update", payload)
            log.info("bulk_update product_id=%s response=%r", row.product_id, res)
            # Did Moloni *actually* apply the name change? Re-fetch and compare.
            # Variant children are known to accept name updates with valid=1
            # but keep the old value — surfacing that case here gives the UI
            # a real error instead of a silent lie.
            sent_name = payload.get("name")
            existing_name = full.get("name")
            if sent_name and sent_name != existing_name:
                after = await moloni.post(
                    "products/getOne",
                    {"company_id": settings.moloni_company_id, "product_id": row.product_id},
                )
                if isinstance(after, dict) and after.get("name") != sent_name:
                    log.warning(
                        "bulk_update product_id=%s: Moloni accepted but kept name. "
                        "sent=%r still got %r after update.",
                        row.product_id, sent_name, after.get("name"),
                    )
                    # Diagnostic dump: full before/after product + full update
                    # payload. Lets us spot any field Moloni is using as the
                    # source of truth that we might be (re)writing with the
                    # old value (composition, properties, at_product_category…).
                    import json as _json
                    log.warning(
                        "bulk_update product_id=%s FULL pre-update product:\n%s",
                        row.product_id,
                        _json.dumps(full, indent=2, ensure_ascii=False, default=str),
                    )
                    log.warning(
                        "bulk_update product_id=%s FULL payload we sent:\n%s",
                        row.product_id,
                        _json.dumps(payload, indent=2, ensure_ascii=False, default=str),
                    )
                    log.warning(
                        "bulk_update product_id=%s FULL post-update product:\n%s",
                        row.product_id,
                        _json.dumps(after, indent=2, ensure_ascii=False, default=str),
                    )
                    results.append({
                        "product_id": row.product_id,
                        "ok": False,
                        "error": (
                            f"Moloni accepted the update (valid=1) but kept the old name "
                            f"({after.get('name')!r}). This product is likely a variant "
                            "child — the name must be set on its parent product."
                        ),
                        "result": res,
                    })
                    continue
            # Moloni's "200 OK + {valid: 0}" is an application-level failure;
            # we used to mark these as ok=True and silently mislead the UI.
            if isinstance(res, dict) and res.get("valid") in (0, "0"):
                results.append({
                    "product_id": row.product_id,
                    "ok": False,
                    "error": f"Moloni rejected update (valid=0): {res!r}",
                    "result": res,
                })
                continue
            results.append({"product_id": row.product_id, "ok": True, "result": res})
        except MoloniAPIError as e:
            log.warning("bulk_update product_id=%s MoloniAPIError: %s", row.product_id, e)
            results.append({"product_id": row.product_id, "ok": False, "error": str(e), "body": e.body})
    return {"results": results}

