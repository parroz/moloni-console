"""Telegram update loop: PDFs become jobs, button presses and replies become answers."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.hava.config import HavaSettings
from app.hava.pipeline import Pipeline
from app.hava.sales import DailyReport, day_sales, format_report
from app.hava.store import Store
from app.hava.telegram import Telegram, TelegramError, esc

log = logging.getLogger("hava.bot")

MAX_PDF_BYTES = 20 * 1024 * 1024  # Telegram bots can download up to 20 MB


def _sender(m: dict[str, Any]) -> str:
    u = m.get("from") or {}
    return " ".join(p for p in (u.get("first_name"), u.get("last_name")) if p) or u.get("username") or "alguém"


class Bot:
    def __init__(
        self, hs: HavaSettings, tg: Telegram, store: Store, pipeline: Pipeline, daily: DailyReport | None = None
    ) -> None:
        self.hs = hs
        self.tg = tg
        self.store = store
        self.pipeline = pipeline
        self.daily = daily
        self.me: dict[str, Any] = {}

    async def run(self) -> None:
        self.me = await self.tg.get_me()
        log.info("Hava online as @%s; group=%s", self.me.get("username"), self.hs.hava_chat_id or "(setup mode)")
        offset = int(self.store.kv_get("tg_offset", "0") or 0)
        while True:
            try:
                updates = await self.tg.get_updates(offset)
            except TelegramError as e:
                log.warning("getUpdates failed: %s", e)
                await asyncio.sleep(5)
                continue
            for u in updates:
                offset = int(u["update_id"]) + 1
                try:
                    if "message" in u:
                        await self.on_message(u["message"])
                    elif "callback_query" in u:
                        await self.on_callback(u["callback_query"])
                except Exception:  # noqa: BLE001
                    log.exception("update %s failed", u.get("update_id"))
                self.store.kv_set("tg_offset", str(offset))

    # ── messages ─────────────────────────────────────────────────────────────

    async def on_message(self, m: dict[str, Any]) -> None:
        chat_id = int(m["chat"]["id"])
        text = (m.get("text") or m.get("caption") or "").strip()

        if text.startswith("/chatid"):
            if not self.hs.hava_chat_id or chat_id == self.hs.hava_chat_id:
                await self.tg.send(chat_id, f"ID deste chat: <code>{chat_id}</code>", reply_to=m["message_id"])
            return
        if chat_id != self.hs.hava_chat_id:
            return  # only the configured group

        if re.match(r"^/vendas(@\w+)?\b", text):
            await self.on_sales_command(chat_id, m["message_id"], text)
            return

        doc = m.get("document")
        if doc and (doc.get("mime_type") == "application/pdf" or str(doc.get("file_name", "")).lower().endswith(".pdf")):
            await self.on_pdf(m, doc)
            return

        reply = m.get("reply_to_message") or {}
        if reply and (reply.get("from") or {}).get("id") == self.me.get("id"):
            q = self.store.question_for_message(chat_id, int(reply["message_id"]))
            if q is not None:
                if q.status != "open":
                    await self.tg.send(chat_id, "Essa pergunta já está respondida. 🙂", reply_to=m["message_id"])
                elif text:
                    by = _sender(m)
                    self.store.answer(q.id, text=text, by=by)
                    await self._mark_answered(q.chat_id, q.tg_message_id, q.text, text, by)
                    fresh = self.store.get_question(q.id)
                    if fresh:
                        await self.pipeline.on_answer(fresh)
                return

        username = self.me.get("username", "")
        if (username and f"@{username}".lower() in text.lower()) or re.search(r"\bhava\b", text, re.IGNORECASE):
            await self.tg.send(
                chat_id,
                "Para já trato das faturas de fornecedores: envia o PDF aqui no grupo. "
                "Para as vendas do dia, escreve /vendas (ou /vendas ontem, /vendas 03/11). "
                "Em breve também respondo a perguntas sobre vendas, stock e preços.",
                reply_to=m["message_id"],
            )

    async def on_sales_command(self, chat_id: int, message_id: int, text: str) -> None:
        if self.daily is None:
            return
        tz = ZoneInfo(self.hs.hava_timezone)
        today = datetime.now(tz).date()
        arg = text.split(maxsplit=1)[1].strip().lower() if len(text.split(maxsplit=1)) > 1 else ""
        day = today
        if arg == "ontem":
            day = today - timedelta(days=1)
        elif arg:
            m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?", arg) or None
            iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", arg)
            try:
                if iso:
                    day = date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
                elif m:
                    year = int(m.group(3)) if m.group(3) else today.year
                    year = year + 2000 if year < 100 else year
                    day = date(year, int(m.group(2)), int(m.group(1)))
                else:
                    raise ValueError
            except ValueError:
                await self.tg.send(chat_id, "Não percebi a data. Usa /vendas, /vendas ontem ou /vendas 03/11.", reply_to=message_id)
                return
        await self.tg.typing(chat_id)
        s = await day_sales(self.daily.client, self.daily.company_id, day, exclude_refs=self.hs.sales_exclude_refs)
        await self.tg.send(chat_id, format_report(s, today=day == today), reply_to=message_id)

    async def on_pdf(self, m: dict[str, Any], doc: dict[str, Any]) -> None:
        chat_id = int(m["chat"]["id"])
        name = str(doc.get("file_name") or "fatura.pdf")
        if int(doc.get("file_size") or 0) > MAX_PDF_BYTES:
            await self.tg.send(chat_id, "Esse PDF tem mais de 20 MB e o Telegram não me deixa descarregá-lo.", reply_to=m["message_id"])
            return
        data = await self.tg.download(str(doc["file_id"]))
        if not data.startswith(b"%PDF"):
            await self.tg.send(chat_id, "Esse ficheiro não parece ser um PDF.", reply_to=m["message_id"])
            return
        job_id = self.store.create_job(
            chat_id=chat_id, pdf_message_id=int(m["message_id"]), filename=name, pdf_path="", sender=_sender(m)
        )
        pdf_dir = self.hs.data_path / "pdfs"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        path = pdf_dir / f"{job_id}.pdf"
        path.write_bytes(data)
        self.store.set_pdf_path(job_id, str(path))
        await self.tg.send(chat_id, f"📥 Recebi <b>{esc(name)}</b>. Vou ver de que fornecedor é…", reply_to=m["message_id"])
        self.pipeline.enqueue(job_id)

    # ── buttons ──────────────────────────────────────────────────────────────

    async def on_callback(self, cb: dict[str, Any]) -> None:
        msg = cb.get("message") or {}
        chat_id = int((msg.get("chat") or {}).get("id") or 0)
        if chat_id != self.hs.hava_chat_id:
            await self.tg.answer_callback(cb["id"])
            return
        m = re.fullmatch(r"q:(\d+):(\d+)", str(cb.get("data", "")))
        if not m:
            await self.tg.answer_callback(cb["id"])
            return
        qid, idx = int(m.group(1)), int(m.group(2))
        q = self.store.get_question(qid)
        if q is None or q.status != "open" or idx >= len(q.options):
            await self.tg.answer_callback(cb["id"], "Esta pergunta já foi respondida.")
            return
        by = _sender(cb)
        label = str(q.options[idx].get("label", ""))
        self.store.answer(qid, option=idx, by=by)
        await self.tg.answer_callback(cb["id"], f"✔ {label}"[:180])
        await self._mark_answered(q.chat_id, q.tg_message_id, q.text, label, by)
        fresh = self.store.get_question(qid)
        if fresh:
            await self.pipeline.on_answer(fresh)

    async def _mark_answered(self, chat_id: int, message_id: int | None, text: str, answer: str, by: str) -> None:
        if message_id:
            await self.tg.edit_text(chat_id, message_id, f"{text}\n\n✅ <b>{esc(answer[:300])}</b> — {esc(by)}")
