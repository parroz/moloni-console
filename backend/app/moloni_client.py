"""Async Moloni API v1 client (password grant)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any

import httpx

log = logging.getLogger("moloni.client")

MOLONI_BASE = "https://api.moloni.pt/v1"


class MoloniAPIError(Exception):
    def __init__(self, message: str, status_code: int | None = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class MoloniClient:
    def __init__(
        self,
        *,
        developer_id: str,
        client_key: str,
        username: str,
        password: str,
        company_id: int,
    ):
        # Moloni /grant query uses OAuth names; panel calls them DEVELOPER_ID and CLIENT_KEY.
        self._grant_client_id = developer_id
        self._grant_client_secret = client_key
        self.username = username
        self.password = password
        self.company_id = company_id
        self._access_token: str | None = None
        self._token_deadline: float = 0.0
        self._token_lock = asyncio.Lock()
        self._http = httpx.AsyncClient(timeout=120.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    def _grant_error_message(self, response: httpx.Response) -> str:
        try:
            data = response.json()
        except (json.JSONDecodeError, ValueError):
            return f"Moloni grant failed (HTTP {response.status_code})"
        if not isinstance(data, dict):
            return f"Moloni grant failed (HTTP {response.status_code})"
        err = data.get("error")
        desc = data.get("error_description")
        if err == "too_many_login_attempts":
            return (
                "Moloni bloqueou o login por demasiadas tentativas falhadas. "
                "Confirme utilizador/palavra-passe e client_secret no .env, aguarde o desbloqueio "
                "(geralmente alguns minutos a horas) ou contacte o suporte Moloni. "
                f"Detalhe: {desc or err}"
            )
        if err or desc:
            return f"Moloni grant: {err or 'error'} — {desc or ''}".strip(" —")
        return f"Moloni grant failed (HTTP {response.status_code})"

    async def _ensure_token(self) -> str:
        """Single-flight token refresh: parallel API calls must not call /grant concurrently."""
        async with self._token_lock:
            now = time.time()
            if self._access_token and now < self._token_deadline - 30:
                return self._access_token
            r = await self._http.get(
                f"{MOLONI_BASE}/grant/",
                params={
                    "grant_type": "password",
                    "client_id": self._grant_client_id,
                    "client_secret": self._grant_client_secret,
                    "username": self.username,
                    "password": self.password,
                },
            )
            if r.status_code != 200:
                raise MoloniAPIError(self._grant_error_message(r), r.status_code, r.text)
            data = r.json()
            if isinstance(data, dict) and data.get("error"):
                raise MoloniAPIError(self._grant_error_message(r), r.status_code, data)
            token = data.get("access_token") if isinstance(data, dict) else None
            if not token:
                raise MoloniAPIError("Moloni grant response missing access_token", r.status_code, data)
            expires_in = int(data.get("expires_in", 3600))
            self._access_token = token
            self._token_deadline = now + expires_in
            return token

    async def post(self, path: str, body: dict[str, Any]) -> Any:
        token = await self._ensure_token()
        url = f"{MOLONI_BASE}/{path}/?access_token={token}&json=true"
        r = await self._http.post(url, json=body)
        if r.status_code != 200:
            raise MoloniAPIError(f"Moloni POST {path} failed", r.status_code, r.text)
        try:
            return r.json()
        except Exception:
            return r.text

    async def post_all_pages(
        self,
        path: str,
        body: dict[str, Any],
        *,
        qty_key: str = "qty",
        offset_key: str = "offset",
        page_size: int = 50,
        max_pages: int = 200,
    ) -> list[Any]:
        """Paginate a Moloni list endpoint until no more rows or page < page_size.

        Defends against runaway loops two ways:
        - A duplicate-chunk guard: if Moloni returns the same chunk twice in a
          row (some filters silently ignore offset), abort with a clear error.
        - A page-count ceiling: 200 pages × 50 rows = 10K results, enough for
          any sane single-parent listing. Set max_pages= higher per-call if you
          really need to paginate millions.
        """
        out: list[Any] = []
        offset = 0
        pages = 0
        prev_sig: str | None = None
        while True:
            pages += 1
            if pages > max_pages:
                raise MoloniAPIError(
                    f"Moloni {path}: pagination exceeded {max_pages} pages "
                    f"(collected {len(out)} rows). Likely cause: the filter "
                    "matches an unexpectedly large set (wrong parent_id?) or "
                    "Moloni is ignoring offset.",
                    body={"offset": offset, "collected": len(out), "body": body},
                )
            payload = {**body, qty_key: page_size, offset_key: offset}
            chunk = await self.post(path, payload)
            if not isinstance(chunk, list):
                raise MoloniAPIError(f"Expected list from {path}", body=chunk)
            if not chunk:
                break
            # Detect Moloni returning the same page twice → offset ignored.
            sig = hashlib.sha1(
                json.dumps(chunk, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
            if sig == prev_sig:
                # Dump enough context to the server log to diagnose without
                # needing to re-run the call manually. Most common cause:
                # parent_id is invalid and Moloni silently falls back to
                # returning top-level categories (or similar) ignoring offset.
                log.error(
                    "Moloni %s: duplicate page at offset %d. Request body=%r. "
                    "First row of duplicate page: %r",
                    path, offset, body, chunk[0] if chunk else None,
                )
                raise MoloniAPIError(
                    f"Moloni {path}: identical page returned twice at offset "
                    f"{offset} — endpoint is ignoring pagination. Check that "
                    f"the filter is valid (e.g. parent_id exists).",
                    body={"offset": offset, "collected": len(out), "body": body},
                )
            prev_sig = sig
            out.extend(chunk)
            # Moloni docs say max qty == page_size, but some endpoints
            # (notably productCategories/getAll) ignore qty and return the
            # full result set in one shot. Treat that as "all data" and stop
            # — otherwise the next request returns the same rows and the
            # duplicate-page guard fires for what is really not an error.
            if len(chunk) > page_size:
                log.warning(
                    "Moloni %s returned %d rows for qty=%d at offset %d; "
                    "endpoint ignored qty, treating as final page.",
                    path, len(chunk), page_size, offset,
                )
                break
            if len(chunk) < page_size:
                break
            offset += page_size
        return out
