"""Minimal Telegram Bot API client (long polling) on httpx — no extra dependency."""

from __future__ import annotations

import html
import logging
from typing import Any

import httpx

log = logging.getLogger("hava.telegram")

# httpx logs every request URL at INFO, and the URL contains the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)

MAX_TEXT = 4000  # Telegram's hard limit is 4096; leave room for HTML entities.


class TelegramError(Exception):
    pass


def esc(text: Any) -> str:
    return html.escape(str(text), quote=False)


def split_text(text: str, limit: int = MAX_TEXT) -> list[str]:
    """Split on line boundaries so no chunk exceeds the Telegram limit."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


class Telegram:
    def __init__(self, token: str) -> None:
        if not token:
            raise TelegramError("HAVA_TELEGRAM_TOKEN is not set")
        self._base = f"https://api.telegram.org/bot{token}"
        self._file_base = f"https://api.telegram.org/file/bot{token}"
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(75.0, connect=10.0))

    async def aclose(self) -> None:
        await self._http.aclose()

    async def call(self, method: str, **params: Any) -> Any:
        payload = {k: v for k, v in params.items() if v is not None}
        try:
            r = await self._http.post(f"{self._base}/{method}", json=payload)
        except httpx.HTTPError as e:
            # Never include the URL: it carries the token.
            raise TelegramError(f"{method}: {type(e).__name__}") from None
        try:
            data = r.json()
        except ValueError:
            raise TelegramError(f"{method}: HTTP {r.status_code}") from None
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description', r.status_code)}")
        return data["result"]

    async def get_me(self) -> dict[str, Any]:
        return await self.call("getMe")

    async def get_updates(self, offset: int, timeout: int = 50) -> list[dict[str, Any]]:
        return await self.call(
            "getUpdates",
            offset=offset,
            timeout=timeout,
            allowed_updates=["message", "callback_query"],
        )

    async def send(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to: int | None = None,
        buttons: list[list[dict[str, str]]] | None = None,
    ) -> dict[str, Any]:
        """Send HTML text. Long text is split; buttons go on the last chunk.
        Returns the last message sent."""
        chunks = split_text(text)
        sent: dict[str, Any] = {}
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            sent = await self.call(
                "sendMessage",
                chat_id=chat_id,
                text=chunk,
                parse_mode="HTML",
                link_preview_options={"is_disabled": True},
                reply_parameters=(
                    {"message_id": reply_to, "allow_sending_without_reply": True}
                    if reply_to and i == 0
                    else None
                ),
                reply_markup={"inline_keyboard": buttons} if (buttons and last) else None,
            )
        return sent

    async def edit_text(self, chat_id: int, message_id: int, text: str) -> None:
        try:
            await self.call(
                "editMessageText",
                chat_id=chat_id,
                message_id=message_id,
                text=text[:MAX_TEXT],
                parse_mode="HTML",
                link_preview_options={"is_disabled": True},
            )
        except TelegramError as e:
            log.warning("edit_text failed: %s", e)

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        try:
            await self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except TelegramError as e:
            log.warning("answerCallbackQuery failed: %s", e)

    async def typing(self, chat_id: int) -> None:
        try:
            await self.call("sendChatAction", chat_id=chat_id, action="typing")
        except TelegramError:
            pass

    async def download(self, file_id: str) -> bytes:
        info = await self.call("getFile", file_id=file_id)
        path = info.get("file_path")
        if not path:
            raise TelegramError("getFile: no file_path")
        try:
            r = await self._http.get(f"{self._file_base}/{path}")
        except httpx.HTTPError as e:
            raise TelegramError(f"download: {type(e).__name__}") from None
        if r.status_code != 200:
            raise TelegramError(f"download: HTTP {r.status_code}")
        return r.content
