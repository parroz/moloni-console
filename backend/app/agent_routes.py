"""Agent endpoints. Mounted under /api/agent.

Authentication uses the same shared session cookie as the rest of the console.
Sessions here are agent-conversation sessions, not browser auth sessions.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.agent.prompts import list_suppliers
from app.agent.runner import run_turn
from app.agent.state import store
from app.config import Settings, get_settings
from app.deps import require_auth

router = APIRouter(prefix="/api/agent")


class SessionCreate(BaseModel):
    supplier_slug: str = "auto"
    test_mode: bool = True


class SessionSettingsPatch(BaseModel):
    supplier_slug: str | None = None
    test_mode: bool | None = None


class MessageBody(BaseModel):
    text: str


def _public_session(session) -> dict[str, Any]:
    """JSON-friendly snapshot of the session (without raw PDF bytes)."""
    return {
        "session_id": session.session_id,
        "supplier_slug": session.supplier_slug,
        "test_mode": session.test_mode,
        "pdf_filename": session.pdf_filename,
        "has_pdf": session.pdf_bytes is not None,
        "messages": session.messages,
        "created_at": session.created_at,
    }


@router.get("/suppliers")
async def get_suppliers(request: Request) -> list[dict[str, str]]:
    require_auth(request)
    return list_suppliers()


@router.post("/sessions")
async def create_session(request: Request, body: SessionCreate) -> dict[str, Any]:
    require_auth(request)
    s = store.create()
    s.supplier_slug = body.supplier_slug or "auto"
    s.test_mode = bool(body.test_mode)
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


@router.patch("/sessions/{session_id}/settings")
async def patch_settings(request: Request, session_id: str, body: SessionSettingsPatch) -> dict[str, Any]:
    require_auth(request)
    s = store.get(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    if body.supplier_slug is not None:
        s.supplier_slug = body.supplier_slug
    if body.test_mode is not None:
        s.test_mode = bool(body.test_mode)
    return _public_session(s)


@router.post("/sessions/{session_id}/upload")
async def upload_pdf(
    request: Request,
    session_id: str,
    file: UploadFile = File(...),
    supplier_slug: str | None = Form(None),
) -> dict[str, Any]:
    """Upload a single PDF for this session. Replaces any previous PDF."""
    require_auth(request)
    s = store.get(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    if file.content_type and file.content_type != "application/pdf":
        raise HTTPException(400, f"Expected application/pdf, got {file.content_type}")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    if len(data) > 25 * 1024 * 1024:  # 25 MB hard cap
        raise HTTPException(413, "PDF too large (max 25 MB)")
    s.pdf_bytes = data
    s.pdf_filename = file.filename or "invoice.pdf"
    if supplier_slug:
        s.supplier_slug = supplier_slug
    # Uploading a new PDF resets the conversation — otherwise the model would still
    # be answering about the old one.
    s.messages = []
    return _public_session(s)


@router.post("/sessions/{session_id}/messages")
async def post_message(
    request: Request,
    session_id: str,
    body: MessageBody,
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """Send a user message; response is an SSE stream of agent events."""
    require_auth(request)
    s = store.get(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    if not body.text.strip():
        raise HTTPException(400, "Empty message")

    async def event_stream() -> AsyncIterator[bytes]:
        # Single concurrent run per session: prevents two messages from interleaving.
        if s.lock.locked():
            yield _sse({"type": "error", "message": "Session busy: another message is in flight."})
            return
        async with s.lock:
            try:
                async for event in run_turn(s, body.text, settings):
                    yield _sse(event)
            except Exception as e:  # noqa: BLE001
                yield _sse({"type": "error", "message": f"Stream crashed: {type(e).__name__}: {e}"})
            finally:
                yield _sse({"type": "done"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # disable nginx buffering
            "Connection": "keep-alive",
        },
    )


def _sse(payload: dict[str, Any]) -> bytes:
    """Format one SSE message. We use a single 'data:' line with JSON body."""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
