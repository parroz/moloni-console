"""Agent endpoints — extract-then-apply architecture.

Flow:
  1. POST /sessions                  → create session
  2. POST /sessions/{id}/upload      → attach PDF
  3. POST /sessions/{id}/extract     → run Anthropic extraction (sync, ~10-20 s)
  4. PATCH /sessions/{id}/data       → user edits (header / lines)
  5. POST /sessions/{id}/apply       → SSE stream of Moloni writes
  6. DELETE /sessions/{id}           → cleanup

Auth: same require_auth cookie check as the rest of the console.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.agent.applier import apply_invoice
from app.agent.prompts import delete_supplier, list_suppliers, load_supplier_rules, save_supplier_rules
from app.agent.runner import ExtractionError, extract_invoice
from app.agent.schema import ExtractedHeader, ExtractedInvoice, ExtractedLine
from app.agent.state import store
from app.config import Settings, get_settings
from app.deps import require_auth

log = logging.getLogger("agent.routes")

router = APIRouter(prefix="/api/agent")


# ── helpers ────────────────────────────────────────────────────────────────────


def _public_session(session) -> dict[str, Any]:
    """JSON-friendly snapshot of an AgentSession (no raw bytes)."""
    extracted: ExtractedInvoice | None = session.extracted
    return {
        "session_id": session.session_id,
        "supplier_slug": session.supplier_slug,
        "pdf_filename": session.pdf_filename,
        "has_pdf": session.pdf_bytes is not None,
        "extracted": extracted.model_dump() if extracted else None,
        "apply_log": session.apply_log,
        "created_at": session.created_at,
    }


def _sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


# ── suppliers list ──────────────────────────────────────────────────────────────


@router.get("/suppliers")
async def get_suppliers(request: Request) -> list[dict[str, str]]:
    require_auth(request)
    return list_suppliers()


@router.get("/suppliers/{slug}/rules")
async def get_supplier_rules(request: Request, slug: str) -> dict[str, str]:
    require_auth(request)
    content = load_supplier_rules(slug)
    if content is None:
        raise HTTPException(404, f"Supplier {slug!r} not found")
    return {"slug": slug, "content": content}


class SupplierRulesBody(BaseModel):
    content: str


@router.put("/suppliers/{slug}/rules")
async def put_supplier_rules(request: Request, slug: str, body: SupplierRulesBody) -> dict[str, str]:
    require_auth(request)
    try:
        save_supplier_rules(slug, body.content)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"slug": slug}


@router.delete("/suppliers/{slug}")
async def delete_supplier_route(request: Request, slug: str) -> dict[str, bool]:
    require_auth(request)
    try:
        found = delete_supplier(slug)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if not found:
        raise HTTPException(404, f"Supplier {slug!r} not found")
    return {"ok": True}


# ── session CRUD ────────────────────────────────────────────────────────────────


class SessionCreate(BaseModel):
    supplier_slug: str = "american_vintage"


@router.post("/sessions")
async def create_session(request: Request, body: SessionCreate) -> dict[str, Any]:
    require_auth(request)
    s = store.create()
    s.supplier_slug = body.supplier_slug or "american_vintage"
    return _public_session(s)


@router.get("/sessions/{session_id}")
async def get_session(request: Request, session_id: str) -> dict[str, Any]:
    require_auth(request)
    s = store.get(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    return _public_session(s)


@router.delete("/sessions/{session_id}")
async def delete_session(request: Request, session_id: str) -> dict[str, bool]:
    require_auth(request)
    store.delete(session_id)
    return {"ok": True}


# ── upload ──────────────────────────────────────────────────────────────────────


@router.post("/sessions/{session_id}/upload")
async def upload_pdf(
    request: Request,
    session_id: str,
    file: UploadFile = File(...),
    supplier_slug: str | None = Form(None),
) -> dict[str, Any]:
    """Attach a PDF to the session. Replaces any previous PDF and resets extraction."""
    require_auth(request)
    s = store.get(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    if file.content_type and file.content_type not in ("application/pdf", "application/octet-stream"):
        raise HTTPException(400, f"Expected application/pdf, got {file.content_type!r}")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(413, "PDF too large (max 25 MB)")
    s.pdf_bytes = data
    s.pdf_filename = file.filename or "invoice.pdf"
    if supplier_slug:
        s.supplier_slug = supplier_slug
    # Uploading a new PDF resets any prior extraction
    s.extracted = None
    s.apply_log = []
    return _public_session(s)


# ── extract ──────────────────────────────────────────────────────────────────────


@router.post("/sessions/{session_id}/extract")
async def run_extract(
    request: Request,
    session_id: str,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Run PDF → JSON extraction via Anthropic. Synchronous (~10-20 s).
    Returns the updated session (with extracted field populated)."""
    require_auth(request)
    s = store.get(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    if not s.pdf_bytes:
        raise HTTPException(400, "No PDF uploaded for this session")

    if s.lock.locked():
        raise HTTPException(409, "Session busy: extraction or apply already running")

    async with s.lock:
        try:
            extracted = await extract_invoice(
                s.pdf_bytes,
                s.pdf_filename or "invoice.pdf",
                s.supplier_slug,
                settings,
            )
        except ExtractionError as e:
            raise HTTPException(422, f"Extraction failed: {e}") from e
        except Exception as e:
            log.exception("extract_invoice unexpected error: %s", e)
            raise HTTPException(500, f"Internal extraction error: {type(e).__name__}: {e}") from e

        s.extracted = extracted
        s.apply_log = []
        return _public_session(s)


# ── user edits ──────────────────────────────────────────────────────────────────


class DataPatch(BaseModel):
    """Partial update of the extracted invoice. Omit fields you don't want to change."""
    header: dict[str, Any] | None = None
    lines: list[dict[str, Any]] | None = None


@router.patch("/sessions/{session_id}/data")
async def patch_data(request: Request, session_id: str, body: DataPatch) -> dict[str, Any]:
    """Apply user edits to the extracted invoice before apply."""
    require_auth(request)
    s = store.get(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    if not s.extracted:
        raise HTTPException(400, "No extracted data for this session. Run /extract first.")

    # Merge header fields
    if body.header is not None:
        merged = s.extracted.header.model_dump()
        merged.update(body.header)
        s.extracted = s.extracted.model_copy(
            update={"header": ExtractedHeader.model_validate(merged)}
        )

    # Replace lines entirely (front-end sends the full edited list)
    if body.lines is not None:
        validated_lines = [ExtractedLine.model_validate(l) for l in body.lines]
        s.extracted = s.extracted.model_copy(update={"lines": validated_lines})

    return _public_session(s)


# ── apply ────────────────────────────────────────────────────────────────────────


@router.post("/sessions/{session_id}/apply")
async def run_apply(
    request: Request,
    session_id: str,
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """Stream Moloni write progress as SSE events.

    Event shapes (all have a `type` field):
      started | subcategory_lookup | subcategory_created | products_indexing
      line_matched | line_creating | line_created | line_error
      invoice_creating | invoice_created | invoice_error | done
    """
    require_auth(request)
    s = store.get(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    if not s.extracted:
        raise HTTPException(400, "No extracted data. Run /extract (and optionally /data) first.")

    extracted_snapshot = s.extracted  # capture to avoid mid-stream mutation
    client = request.app.state.moloni  # singleton from lifespan

    async def event_stream() -> AsyncIterator[bytes]:
        if s.lock.locked():
            yield _sse({"type": "invoice_error", "message": "Session busy: extraction or apply already running."})
            yield _sse({"type": "done"})
            return

        async with s.lock:
            s.apply_log = []
            try:
                async for event in apply_invoice(extracted_snapshot, client, settings):
                    s.apply_log.append(event)
                    yield _sse(event)
            except Exception as e:  # noqa: BLE001
                log.exception("apply_invoice stream error: %s", e)
                err = {"type": "invoice_error", "message": f"Stream crashed: {type(e).__name__}: {e}"}
                s.apply_log.append(err)
                yield _sse(err)
                yield _sse({"type": "done"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
