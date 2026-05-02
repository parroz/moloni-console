"""Agent loop: Anthropic streaming + remote MCP. Yields typed events for the SSE layer.

The agent loop itself runs inside Anthropic's infra (we use the `mcp_servers` parameter,
so Claude calls MCP tools directly through Anthropic). Our job is:
  1. Build the request (system + cached supplier rules + cached PDF + history + MCP).
  2. Stream the response.
  3. Translate Anthropic stream events into our wire format.
  4. Persist the final assistant message into the session.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any, AsyncIterator

import anthropic

from app.agent.prompts import build_system_blocks
from app.agent.state import AgentSession
from app.config import Settings

log = logging.getLogger("agent.runner")


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


def _user_message_with_optional_pdf(text: str, pdf_bytes: bytes | None, attach_pdf: bool) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    if attach_pdf and pdf_bytes:
        content.append(_pdf_block(pdf_bytes))
    content.append({"type": "text", "text": text})
    return {"role": "user", "content": content}


def _mcp_servers(settings: Settings) -> list[dict[str, Any]]:
    if not settings.moloni_mcp_url:
        return []
    srv: dict[str, Any] = {
        "type": "url",
        "url": settings.moloni_mcp_url,
        "name": "moloni",
    }
    if settings.moloni_mcp_auth_token:
        srv["authorization_token"] = settings.moloni_mcp_auth_token
    return [srv]


async def run_turn(
    session: AgentSession,
    user_text: str,
    settings: Settings,
) -> AsyncIterator[dict[str, Any]]:
    """
    Append the user message (with PDF on first turn) and stream the assistant response.
    Yields wire events:
      {"type": "user_message_persisted"}                  — emitted after we append user msg
      {"type": "text_delta", "text": "..."}               — incremental assistant text
      {"type": "tool_use", "id": "...", "name": "...", "input": {...}, "server_name": "..."}
      {"type": "tool_result", "tool_use_id": "...", "content": [...], "is_error": bool}
      {"type": "message_complete", "usage": {...}, "stop_reason": "..."}
      {"type": "error", "message": "..."}
    """
    if not settings.anthropic_api_key:
        yield {"type": "error", "message": "ANTHROPIC_API_KEY not configured."}
        return

    # Decide whether to attach the PDF: only on the very first user turn, when there's a PDF.
    has_history = bool(session.messages)
    attach_pdf = (not has_history) and bool(session.pdf_bytes)
    user_msg = _user_message_with_optional_pdf(user_text, session.pdf_bytes, attach_pdf)
    session.messages.append(user_msg)
    yield {"type": "user_message_persisted"}

    log.info(
        "agent run start: session=%s supplier=%s test_mode=%s attach_pdf=%s pdf_size=%d turn=%d",
        session.session_id,
        session.supplier_slug,
        session.test_mode,
        attach_pdf,
        len(session.pdf_bytes) if session.pdf_bytes else 0,
        len(session.messages),
    )

    system_blocks = build_system_blocks(session.supplier_slug, session.test_mode)
    mcp_servers = _mcp_servers(settings)

    if not mcp_servers:
        log.warning("agent run: MOLONI_MCP_URL not set — model will run without Moloni tools")

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    t0 = time.monotonic()
    first_event_at: float | None = None

    # Track tool_use blocks so we can emit a clean `tool_use` event with full input
    # once the corresponding content_block_stop arrives. The stream gives us partial
    # JSON deltas which we accumulate per block index.
    tool_use_buffers: dict[int, dict[str, Any]] = {}

    final_message_content: list[Any] = []

    try:
        # MCP-as-tools is exposed under `client.beta.messages.*` on the Python SDK.
        # We pass `betas=[...]` to enable the MCP client beta feature.
        log.info("agent run: opening stream (model=%s, max_tokens=8192)", settings.anthropic_model)
        yield {"type": "stream_started"}
        async with client.beta.messages.stream(
            model=settings.anthropic_model,
            max_tokens=8192,
            system=system_blocks,
            messages=session.messages,
            mcp_servers=mcp_servers,
            betas=["mcp-client-2025-04-04"],
        ) as stream:
            async for event in stream:
                etype = getattr(event, "type", None)
                if first_event_at is None:
                    first_event_at = time.monotonic()
                    log.info("agent run: first event after %.1fs (event_type=%s)", first_event_at - t0, etype)

                if etype == "content_block_start":
                    block = getattr(event, "content_block", None)
                    block_type = getattr(block, "type", None)
                    idx = getattr(event, "index", -1)
                    if block_type == "mcp_tool_use" or block_type == "tool_use":
                        tool_use_buffers[idx] = {
                            "id": getattr(block, "id", ""),
                            "name": getattr(block, "name", ""),
                            "server_name": getattr(block, "server_name", ""),
                            "input_json": "",
                        }

                elif etype == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    dtype = getattr(delta, "type", None)
                    idx = getattr(event, "index", -1)
                    if dtype == "text_delta":
                        text = getattr(delta, "text", "") or ""
                        if text:
                            yield {"type": "text_delta", "text": text}
                    elif dtype == "input_json_delta":
                        partial = getattr(delta, "partial_json", "") or ""
                        if idx in tool_use_buffers:
                            tool_use_buffers[idx]["input_json"] += partial

                elif etype == "content_block_stop":
                    idx = getattr(event, "index", -1)
                    if idx in tool_use_buffers:
                        buf = tool_use_buffers.pop(idx)
                        try:
                            parsed_input = json.loads(buf["input_json"]) if buf["input_json"] else {}
                        except json.JSONDecodeError:
                            parsed_input = {"_raw": buf["input_json"]}
                        log.info(
                            "agent run: tool_use name=%s server=%s id=%s",
                            buf["name"],
                            buf["server_name"],
                            buf["id"],
                        )
                        yield {
                            "type": "tool_use",
                            "id": buf["id"],
                            "name": buf["name"],
                            "server_name": buf["server_name"],
                            "input": parsed_input,
                        }

                # MCP tool RESULTS arrive as their own content blocks in the same stream
                # (Anthropic injects them after forwarding the call). We catch them via
                # the final message rather than per-event because the SDK exposes them
                # cleanly as content blocks.

            final = await stream.get_final_message()
            final_message_content = final.content

            # Walk the final content and emit any tool_result blocks we missed
            for block in final_message_content:
                btype = getattr(block, "type", None)
                if btype == "mcp_tool_result":
                    is_error = bool(getattr(block, "is_error", False))
                    log.info(
                        "agent run: tool_result tool_use_id=%s error=%s",
                        getattr(block, "tool_use_id", ""),
                        is_error,
                    )
                    yield {
                        "type": "tool_result",
                        "tool_use_id": getattr(block, "tool_use_id", ""),
                        "content": _serialise_result_content(getattr(block, "content", [])),
                        "is_error": is_error,
                    }

            # Persist assistant message in conversation history (Anthropic expects content
            # as the same JSON shape it returned).
            session.messages.append(
                {"role": "assistant", "content": _serialise_assistant_content(final_message_content)}
            )

            usage = getattr(final, "usage", None)
            usage_payload = _serialise_usage(usage)
            elapsed = time.monotonic() - t0
            log.info(
                "agent run complete: elapsed=%.1fs stop_reason=%s usage=%s",
                elapsed,
                getattr(final, "stop_reason", None),
                usage_payload,
            )
            yield {
                "type": "message_complete",
                "stop_reason": getattr(final, "stop_reason", None),
                "usage": usage_payload,
            }
    except anthropic.APIStatusError as e:
        log.exception("agent run: Anthropic API error")
        yield {"type": "error", "message": f"Anthropic API error ({e.status_code}): {e.message}"}
    except anthropic.APIError as e:
        log.exception("agent run: Anthropic SDK error")
        yield {"type": "error", "message": f"Anthropic error: {str(e)}"}
    except Exception as e:  # noqa: BLE001 — surface anything else to the UI
        log.exception("agent run: unexpected error")
        yield {"type": "error", "message": f"Internal error: {type(e).__name__}: {e}"}


def _serialise_result_content(content: Any) -> list[dict[str, Any]]:
    """MCP tool result content can be SDK objects or already dicts; reduce to a JSON-safe list."""
    if not content:
        return []
    out: list[dict[str, Any]] = []
    for item in content:
        if isinstance(item, dict):
            out.append(item)
            continue
        itype = getattr(item, "type", None)
        if itype == "text":
            out.append({"type": "text", "text": getattr(item, "text", "")})
        else:
            # Best-effort fallback: dump everything readable
            out.append({"type": itype or "unknown", "raw": str(item)})
    return out


def _serialise_assistant_content(content: Any) -> list[dict[str, Any]]:
    """Convert the SDK's content blocks into the JSON dict form the API also accepts on input."""
    out: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, dict):
            out.append(block)
            continue
        btype = getattr(block, "type", None)
        if btype == "text":
            out.append({"type": "text", "text": getattr(block, "text", "")})
        elif btype == "tool_use":
            out.append(
                {
                    "type": "tool_use",
                    "id": getattr(block, "id", ""),
                    "name": getattr(block, "name", ""),
                    "input": getattr(block, "input", {}) or {},
                }
            )
        elif btype == "mcp_tool_use":
            out.append(
                {
                    "type": "mcp_tool_use",
                    "id": getattr(block, "id", ""),
                    "name": getattr(block, "name", ""),
                    "server_name": getattr(block, "server_name", ""),
                    "input": getattr(block, "input", {}) or {},
                }
            )
        elif btype == "mcp_tool_result":
            out.append(
                {
                    "type": "mcp_tool_result",
                    "tool_use_id": getattr(block, "tool_use_id", ""),
                    "is_error": bool(getattr(block, "is_error", False)),
                    "content": _serialise_result_content(getattr(block, "content", [])),
                }
            )
        else:
            # Drop unknown block types from history rather than corrupt the payload
            continue
    return out


def _serialise_usage(usage: Any) -> dict[str, int]:
    if usage is None:
        return {}
    return {
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "cache_creation_input_tokens": int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
        "cache_read_input_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
    }
