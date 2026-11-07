"""Daily store sales from Moloni, and the 19:30 report Hava posts to the group.

What counts as a sale: closed (status 1) invoice-receipts, simplified invoices
and invoices dated that day. Credit notes of the same day are returns: they are
listed and subtracted from the totals.

Moloni's list fields, checked against a real document's lines:
  net_value   = document total WITH VAT
  taxes_value = VAT
  gross_value = pre-discount value, not used here
so "sem IVA" = net_value − taxes_value.

Items are summed from each document's lines, leaving out the paper bags sold
at the till (references starting GC-SACO by default).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.hava.telegram import esc
from app.moloni_client import MoloniAPIError, MoloniClient

log = logging.getLogger("hava.sales")

SALE_TYPES = ("invoiceReceipts", "simplifiedInvoices", "invoices")
RETURN_TYPE = "creditNotes"
CLOSED = 1

_WEEKDAYS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
_MONTHS = [
    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
]


@dataclass
class DaySales:
    day: date
    sales: int = 0
    items: float = 0.0
    with_vat: float = 0.0
    vat: float = 0.0
    returns: int = 0
    returns_with_vat: float = 0.0
    returns_items: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def without_vat(self) -> float:
        return self.with_vat - self.vat


def _eur(v: float) -> str:
    s = f"{v:,.2f}".replace(",", " ").replace(".", ",")
    return f"{s} €"


def _qty(v: float) -> str:
    return f"{v:g}".replace(".", ",")


def long_date(d: date) -> str:
    return f"{_WEEKDAYS[d.weekday()]}, {d.day} de {_MONTHS[d.month - 1]}"


async def _documents(client: MoloniClient, company_id: int, doc_type: str, day: date) -> list[dict[str, Any]]:
    rows = await client.post_all_pages(
        f"{doc_type}/getAll", {"company_id": company_id, "date": day.isoformat()}
    )
    return [
        d for d in rows or []
        if isinstance(d, dict)
        and str(d.get("date", "")).startswith(day.isoformat())
        and int(d.get("status") or 0) == CLOSED
    ]


async def _items(
    client: MoloniClient, company_id: int, doc_type: str, document_id: int, exclude: tuple[str, ...]
) -> float:
    doc = await client.post(f"{doc_type}/getOne", {"company_id": company_id, "document_id": document_id})
    total = 0.0
    for p in (doc or {}).get("products", []) if isinstance(doc, dict) else []:
        ref = str(p.get("reference", "")).upper()
        if any(ref.startswith(x) for x in exclude):
            continue
        total += float(p.get("qty") or 0)
    return total


async def day_sales(
    client: MoloniClient, company_id: int, day: date, *, exclude_refs: tuple[str, ...] = ("GC-SACO",)
) -> DaySales:
    out = DaySales(day=day)
    exclude = tuple(x.upper() for x in exclude_refs if x)

    for doc_type in SALE_TYPES:
        try:
            docs = await _documents(client, company_id, doc_type, day)
        except MoloniAPIError as e:
            out.errors.append(f"{doc_type}: {e}")
            continue
        for d in docs:
            out.sales += 1
            out.with_vat += float(d.get("net_value") or 0)
            out.vat += float(d.get("taxes_value") or 0)
        counts = await asyncio.gather(
            *(_items(client, company_id, doc_type, int(d["document_id"]), exclude) for d in docs),
            return_exceptions=True,
        )
        for c in counts:
            if isinstance(c, Exception):
                out.errors.append(f"{doc_type} linhas: {c}")
            else:
                out.items += c

    try:
        notes = await _documents(client, company_id, RETURN_TYPE, day)
    except MoloniAPIError as e:
        out.errors.append(f"{RETURN_TYPE}: {e}")
        notes = []
    for d in notes:
        out.returns += 1
        out.returns_with_vat += float(d.get("net_value") or 0)
        out.with_vat -= float(d.get("net_value") or 0)
        out.vat -= float(d.get("taxes_value") or 0)
    counts = await asyncio.gather(
        *(_items(client, company_id, RETURN_TYPE, int(d["document_id"]), exclude) for d in notes),
        return_exceptions=True,
    )
    for c in counts:
        if not isinstance(c, Exception):
            out.returns_items += c
    out.items -= out.returns_items
    return out


def format_report(s: DaySales, *, today: bool) -> str:
    when = "de hoje" if today else f"de {long_date(s.day)}"
    head = f"🧾 <b>Vendas {when}</b>" + (f" — {long_date(s.day)}" if today else "")
    if s.sales == 0 and s.returns == 0:
        body = "Não houve vendas." if not s.errors else "Não consegui ler as vendas no Moloni."
    else:
        body = (
            f"Total com IVA: <b>{_eur(s.with_vat)}</b>\n"
            f"Total sem IVA: {_eur(s.without_vat)}\n"
            f"IVA: {_eur(s.vat)}\n"
            f"Artigos vendidos: <b>{_qty(s.items)}</b>\n"
            f"Vendas: <b>{s.sales}</b>"
        )
        if s.returns:
            body += (
                f"\nDevoluções: {s.returns} ({_eur(s.returns_with_vat)} com IVA, "
                f"{_qty(s.returns_items)} artigo(s)) — já descontadas acima"
            )
    if s.errors:
        body += "\n\n⚠️ Parte dos dados falhou: " + esc("; ".join(s.errors)[:300])
    return f"{head}\n\n{body}"


class DailyReport:
    """Posts the day's sales to the group at a fixed local time, once per day.

    The last day sent is stored, so a restart neither repeats the report nor
    skips it: if Hava comes back up after the hour but before midnight, she
    sends it then.
    """

    def __init__(self, *, tg, store, client: MoloniClient, company_id: int, chat_id: int,
                 at: str, tz: str, exclude_refs: tuple[str, ...]) -> None:
        self.tg = tg
        self.store = store
        self.client = client
        self.company_id = company_id
        self.chat_id = chat_id
        hh, mm = (int(x) for x in at.split(":"))
        self.at = (hh, mm)
        self.tz = ZoneInfo(tz)
        self.exclude_refs = exclude_refs

    def _next_run(self, now: datetime) -> datetime:
        target = now.replace(hour=self.at[0], minute=self.at[1], second=0, microsecond=0)
        sent = self.store.kv_get("daily_report_sent", "")
        if now >= target and sent != now.date().isoformat():
            return now  # missed today's slot (restart/downtime): send now
        if now >= target:
            target += timedelta(days=1)
        return target

    async def run(self) -> None:
        if not self.chat_id:
            log.info("daily report disabled: no HAVA_CHAT_ID")
            return
        while True:
            now = datetime.now(self.tz)
            nxt = self._next_run(now)
            wait = (nxt - now).total_seconds()
            log.info("daily report: next at %s (in %.0f s)", nxt.isoformat(timespec="minutes"), wait)
            await asyncio.sleep(max(wait, 0))
            day = datetime.now(self.tz).date()
            if self.store.kv_get("daily_report_sent", "") == day.isoformat():
                continue
            try:
                s = await day_sales(self.client, self.company_id, day, exclude_refs=self.exclude_refs)
                await self.tg.send(self.chat_id, format_report(s, today=True))
                self.store.kv_set("daily_report_sent", day.isoformat())
            except Exception:  # noqa: BLE001 — retry in 10 minutes rather than lose the day
                log.exception("daily report failed")
                await asyncio.sleep(600)
