"""Pydantic models for the extracted-invoice JSON returned by the agent
and the apply-progress events streamed from the applier.

The schema is intentionally narrow: only fields the supplier-invoice flow needs.
The agent must return JSON conforming to ExtractedInvoice exactly.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ── Extracted invoice (model output, then user-edited) ─────────────────────────


class ExtractedSupplier(BaseModel):
    slug: str = Field(..., description="Supplier slug matching a .md file in suppliers/.")
    name: str
    vat: str = ""
    moloni_supplier_id: int = Field(..., description="From supplier rules — the Moloni supplier_id to bill against.")


class ExtractedHeader(BaseModel):
    invoice_number: str = Field(..., description="Used as your_reference on the Moloni invoice.")
    date: str = Field(..., description="ISO YYYY-MM-DD.")
    expiration_date: str | None = None
    currency: str = "EUR"
    subtotal: float = Field(0.0, description="Pre-discount line total (before invoice-level discount).")
    tax_total: float = 0.0
    grand_total: float = 0.0
    invoice_discount_pct: float = Field(
        0.0,
        description=(
            "Invoice-level discount percentage applied to the whole document "
            "(e.g. 'Desc PP 3' → 3.0). Maps to Moloni financial_discount."
        ),
    )


class ExtractedLine(BaseModel):
    """One invoice line = one product variant. Quantity must be ≥ 1."""

    reference: str = Field(..., description="Canonical product reference per supplier rules.")
    name: str
    summary: str = ""
    qty: float
    unit_cost: float = Field(..., description="Net unit cost from the supplier invoice (no VAT).")
    pvp_with_vat: float = Field(0.0, description="Retail price with VAT. Backend rounds & recomputes net.")
    moloni_price_no_vat: float = Field(0.0, description="Net retail price for product catalog.")
    subcategory_name: str = Field(..., description="Free-text subcategory name. Backend resolves to category_id.")
    color: str = ""
    size: str = ""
    discount_pct: float = Field(
        0.0,
        description=(
            "Per-line discount percentage (e.g. the 'DC%' column). "
            "Maps to Moloni line discount."
        ),
    )


class Reconciliation(BaseModel):
    calculated_subtotal: float = 0.0
    matches_invoice_total: bool = True
    warnings: list[str] = Field(default_factory=list)


class ExtractedInvoice(BaseModel):
    supplier: ExtractedSupplier
    header: ExtractedHeader
    lines: list[ExtractedLine] = Field(default_factory=list)
    reconciliation: Reconciliation = Field(default_factory=Reconciliation)


# ── Apply-progress SSE events ──────────────────────────────────────────────────


class ApplyStarted(BaseModel):
    type: Literal["started"] = "started"


class SubcategoryLookup(BaseModel):
    type: Literal["subcategory_lookup"] = "subcategory_lookup"
    needs_creation: list[str]


class SubcategoryCreated(BaseModel):
    type: Literal["subcategory_created"] = "subcategory_created"
    name: str
    category_id: int


class ProductsIndexing(BaseModel):
    type: Literal["products_indexing"] = "products_indexing"
    total_existing: int


class LineMatched(BaseModel):
    type: Literal["line_matched"] = "line_matched"
    reference: str
    product_id: int


class LineCreating(BaseModel):
    type: Literal["line_creating"] = "line_creating"
    reference: str


class LineCreated(BaseModel):
    type: Literal["line_created"] = "line_created"
    reference: str
    product_id: int


class LineError(BaseModel):
    type: Literal["line_error"] = "line_error"
    reference: str
    message: str


class InvoiceCreating(BaseModel):
    type: Literal["invoice_creating"] = "invoice_creating"


class InvoiceCreated(BaseModel):
    type: Literal["invoice_created"] = "invoice_created"
    document_id: int


class InvoiceError(BaseModel):
    type: Literal["invoice_error"] = "invoice_error"
    message: str


class ApplyDone(BaseModel):
    type: Literal["done"] = "done"


# Plain dicts cross the SSE boundary, so we don't bother with a discriminated
# Union here — the runner emits dict payloads, the route layer wraps them as SSE,
# and the frontend parses by `type`.
