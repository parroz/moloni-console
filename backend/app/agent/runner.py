"""PDF → ExtractedInvoice via a single Anthropic call. No tools, no MCP.

Replaces the previous streaming agent loop. The model gets the system prompt
(base + supplier rules, cached), the PDF as a document block (cached), and
returns a JSON object that we parse into ExtractedInvoice.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from typing import Any

import anthropic

from app.agent.prompts import build_system_blocks
from app.agent.schema import ExtractedInvoice
from app.config import Settings

log = logging.getLogger("agent.runner")


class ExtractionError(Exception):
    """Raised when the model output can't be parsed into ExtractedInvoice."""


def _pdf_block(pdf_bytes: bytes) -> dict[str, Any]:
    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": base64.standard_b64encode(pdf_bytes).decode("ascii"),
        },
        "cache_control": {"type": "ephemeral"},
    }


def _extract_json_object(text: str) -> str:
    """Pick the largest top-level {...} from the model output, in case it
    wrapped the response in markdown fences or added stray prose despite the
    instruction. Defensive — the prompt explicitly demands raw JSON only."""
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        return text
    # Strip ```json ... ``` fences
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        return fence.group(1)
    # Last-resort: greedy {...} match
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if m:
        return m.group(0)
    raise ExtractionError(f"No JSON object found in model output (first 200 chars): {text[:200]!r}")


async def extract_invoice(
    pdf_bytes: bytes,
    pdf_filename: str,
    supplier_slug: str,
    settings: Settings,
) -> ExtractedInvoice:
    """One-shot PDF → ExtractedInvoice. Raises ExtractionError on parse failure
    or anthropic.APIError on transport failure."""
    if not settings.anthropic_api_key:
        raise ExtractionError("ANTHROPIC_API_KEY not configured.")
    if not pdf_bytes:
        raise ExtractionError("No PDF bytes provided.")

    system_blocks = build_system_blocks(supplier_slug)
    user_msg: dict[str, Any] = {
        "role": "user",
        "content": [
            _pdf_block(pdf_bytes),
            {
                "type": "text",
                "text": (
                    f"Extract the supplier invoice from this PDF ({pdf_filename}). "
                    f"Apply the supplier rules for slug `{supplier_slug}`. "
                    "Return ONLY the JSON object — nothing before, nothing after."
                ),
            },
        ],
    }

    log.info(
        "extract: pdf=%s supplier=%s pdf_size=%d model=%s",
        pdf_filename,
        supplier_slug,
        len(pdf_bytes),
        settings.anthropic_model,
    )
    t0 = time.monotonic()
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    # Stream the response: at large max_tokens the SDK refuses non-streaming
    # calls whose worst-case runtime would exceed the 10-minute server cap.
    # get_final_message() returns the same assembled Message we'd get back
    # from .create(), so the parsing code below is unchanged.
    async with client.messages.stream(
        model=settings.anthropic_model,
        max_tokens=settings.anthropic_max_tokens,
        system=system_blocks,
        messages=[user_msg],
    ) as stream:
        msg = await stream.get_final_message()

    elapsed = time.monotonic() - t0
    raw_text = ""
    for block in msg.content:
        btype = getattr(block, "type", None)
        if btype == "text":
            raw_text += getattr(block, "text", "") or ""

    stop_reason = getattr(msg, "stop_reason", None)
    usage = getattr(msg, "usage", None)
    out_tokens = getattr(usage, "output_tokens", 0) or 0
    log.info(
        "extract: complete in %.1fs stop=%s in=%d out=%d cache_read=%d cache_create=%d",
        elapsed,
        stop_reason,
        getattr(usage, "input_tokens", 0) or 0,
        out_tokens,
        getattr(usage, "cache_read_input_tokens", 0) or 0,
        getattr(usage, "cache_creation_input_tokens", 0) or 0,
    )

    if stop_reason == "max_tokens":
        raise ExtractionError(
            f"Model output truncated at the {settings.anthropic_max_tokens}-token cap "
            f"({out_tokens} tokens emitted). The invoice has more lines than the current "
            "budget allows. Raise ANTHROPIC_MAX_TOKENS in backend/.env (max 64000 for "
            "Sonnet 4.5) or split the invoice."
        )

    raw_json = _extract_json_object(raw_text)
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as e:
        raise ExtractionError(f"Model output was not valid JSON: {e}") from e

    try:
        return ExtractedInvoice.model_validate(data)
    except Exception as e:
        raise ExtractionError(f"Model JSON did not match schema: {e}") from e
