"""Thin wrappers around the Anthropic SDK for Hava's own reasoning calls.

Two shapes:
  * `ask_json` — structured output validated against a Pydantic model.
  * `ask_text` — free text (used to write supplier rulebooks), streamed.

The Claude Sonnet 5.5 server-side refusal fallback is enabled on both, so a
safety-classifier decline is retried on another model inside the same call
instead of failing the invoice.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel

from app.agent.pdf_text import PdfTextUnavailable, extract_positioned_text
from app.config import Settings

log = logging.getLogger("hava.llm")

T = TypeVar("T", bound=BaseModel)

_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(Exception):
    pass


def pdf_block(pdf_bytes: bytes) -> dict[str, Any]:
    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": base64.standard_b64encode(pdf_bytes).decode("ascii"),
        },
        "cache_control": {"type": "ephemeral"},
    }


def positioned_text_block(pdf_bytes: bytes) -> list[dict[str, Any]]:
    """The PDF's text layer with x positions, as used by the extractor. Empty for scans."""
    try:
        positioned = extract_positioned_text(pdf_bytes)
    except PdfTextUnavailable:
        return []
    return [
        {
            "type": "text",
            "text": (
                "AUTHORITATIVE TEXT LAYER — every word of the PDF with its horizontal "
                "position, as `word@x`, grouped into visual rows. Use it, not the "
                "image, to decide which column a value belongs to.\n\n" + positioned
            ),
        }
    ]


def _client(settings: Settings) -> anthropic.AsyncAnthropic:
    if not settings.anthropic_api_key:
        raise LLMError("ANTHROPIC_API_KEY not configured")
    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)


def _check(msg: Any) -> None:
    stop = getattr(msg, "stop_reason", None)
    if stop == "refusal":
        raise LLMError("o modelo recusou o pedido")
    if stop == "max_tokens":
        raise LLMError("a resposta do modelo foi cortada (max_tokens)")


async def ask_json(
    settings: Settings,
    model: str,
    *,
    system: str,
    content: list[dict[str, Any]],
    output: type[T],
    max_tokens: int = 16000,
) -> T:
    client = _client(settings)
    try:
        msg = await client.beta.messages.parse(
            model=model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": content}],
            # SDK 0.125 only takes a Pydantic class through `output_format`
            # (deprecated name; `output_config.format` there needs a raw schema).
            output_format=output,
            betas=[_FALLBACK_BETA],
            extra_body={"fallbacks": "default"},
        )
    except anthropic.APIStatusError as e:
        raise LLMError(f"Anthropic {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise LLMError(f"Anthropic connection error: {e}") from e
    _check(msg)
    parsed = getattr(msg, "parsed_output", None)
    if parsed is None:
        raise LLMError("a resposta do modelo não respeitou o formato pedido")
    usage = getattr(msg, "usage", None)
    log.info(
        "ask_json %s: in=%s out=%s",
        output.__name__,
        getattr(usage, "input_tokens", "?"),
        getattr(usage, "output_tokens", "?"),
    )
    return parsed


async def ask_text(
    settings: Settings,
    model: str,
    *,
    system: str,
    content: list[dict[str, Any]],
    max_tokens: int = 32000,
) -> str:
    client = _client(settings)
    try:
        async with client.beta.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": content}],
            betas=[_FALLBACK_BETA],
            extra_body={"fallbacks": "default"},
        ) as stream:
            msg = await stream.get_final_message()
    except anthropic.APIStatusError as e:
        raise LLMError(f"Anthropic {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise LLMError(f"Anthropic connection error: {e}") from e
    _check(msg)
    return "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text")
