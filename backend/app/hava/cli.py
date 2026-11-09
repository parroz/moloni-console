"""Hava's tools, as a command line that prints JSON.

Hava herself is a Claude Code session on Pedro's Mac with the Telegram channel
plugin. She reaches Moloni and the invoice pipeline only through this CLI,
run inside the `hava` container over SSH by ~/hava-bot/tools/hava.sh:

    hava.sh <command> [args...]      (JSON input, when needed, on stdin)

Invoices:
    suppliers                         known rulebooks (slug, title, VAT, draft?)
    rules <slug>                      a rulebook's Markdown
    detect <pdf>                      who issued this invoice
    extract <pdf> <slug>              read it with that rulebook → job + doubts
    job <job_id>                      current extraction + doubts of a job
    correct <job_id>                  stdin {"qa": [[question, answer], ...]}
    apply <job_id>                    create products + draft invoice in Moloni
New supplier:
    onboard <pdf>                     facts + proposed conventions + questions
    create-category <name>            new top-level product category (a write!)
    write-rules <pdf>                 stdin {ident, plan, config, answers} → draft rulebook
    approve-rules <slug>              drop the draft mark
    add-rules <slug>                  stdin {"rules": [...]} → append team corrections
Sales and catalogue (read-only):
    sales [YYYY-MM-DD]                day summary (default today, Lisbon)
    sold <from> <to> [filter]         products sold in a date range (max 31 days)
    stock                             whole catalogue: products in stock and negative
    moloni <endpoint>                 stdin JSON body; read-only endpoints only

PDF paths are inside the container: uploads land in /app/hava-data/inbox/.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.agent.applier import apply_invoice
from app.agent.prompts import load_supplier_rules, save_supplier_rules
from app.agent.runner import extract_invoice
from app.agent.schema import ExtractedInvoice
from app.config import get_settings
from app.hava import onboarding as onb
from app.hava.config import get_hava_settings
from app.hava.detect import Identification, detect_supplier, known_suppliers
from app.hava.doubts import apply_answers, find_doubts
from app.hava.sales import SALE_TYPES, day_sales, format_report
from app.hava.store import Store
from app.moloni_client import MoloniClient

# Read-only Moloni endpoints Hava may call freely: anything named get*/count*.
_READ_ENDPOINT = re.compile(r"^[a-zA-Z]+/(get[A-Za-z]*|count[A-Za-z]*)$")
_INBOX = "/app/hava-data/inbox"


class CliError(Exception):
    pass


def _plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def _pdf(path: str) -> tuple[bytes, Path]:
    p = Path(path)
    if not p.is_absolute():
        p = Path(_INBOX) / p
    p = p.resolve()
    if not str(p).startswith("/app/hava-data/"):
        raise CliError("PDF must be under /app/hava-data/")
    if not p.is_file():
        raise CliError(f"PDF not found: {p}")
    data = p.read_bytes()
    if not data.startswith(b"%PDF"):
        raise CliError("file is not a PDF")
    return data, p


def _stdin_json() -> dict[str, Any]:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise CliError(f"stdin is not valid JSON: {e}") from e


def _doubts_out(inv: ExtractedInvoice, slug: str, *, hard_only: bool) -> list[dict[str, Any]]:
    return [
        {"kind": d.kind, "question": _plain(d.text), "suggested_answers": [o["label"] for o in d.options]}
        for d in find_doubts(inv, draft_rules=onb.is_draft(slug) and not hard_only, hard_only=hard_only)
    ]


def _invoice_out(inv: ExtractedInvoice) -> dict[str, Any]:
    h = inv.header
    return {
        "invoice_number": h.invoice_number,
        "date": h.date,
        "lines": len(inv.lines),
        "units": sum(l.qty for l in inv.lines),
        "subtotal": h.subtotal,
        "grand_total": h.grand_total,
        "calculated_subtotal": inv.reconciliation.calculated_subtotal,
        "totals_match": inv.reconciliation.matches_invoice_total,
        "line_table": [
            {
                "i": i,
                "reference": l.reference,
                "name": l.name,
                "color": l.color,
                "size": l.size,
                "qty": l.qty,
                "unit_cost": l.unit_cost,
                "pvp_with_vat": l.pvp_with_vat,
                "subcategory": l.subcategory_name,
                "discount_pct": l.discount_pct,
            }
            for i, l in enumerate(inv.lines)
        ],
    }


class Tools:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.hs = get_hava_settings()
        self.hs.data_path.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.hs.data_path / "hava.db")
        self.model = self.hs.hava_model
        self.company_id = self.settings.moloni_company_id
        self._moloni: MoloniClient | None = None

    @property
    def moloni(self) -> MoloniClient:
        if self._moloni is None:
            s = self.settings
            self._moloni = MoloniClient(
                developer_id=s.moloni_developer_id,
                client_key=s.moloni_client_key,
                username=s.moloni_username,
                password=s.moloni_password,
                company_id=s.moloni_company_id,
            )
        return self._moloni

    async def close(self) -> None:
        if self._moloni is not None:
            await self._moloni.aclose()

    def _job_invoice(self, job_id: str) -> tuple[Any, ExtractedInvoice]:
        job = self.store.get_job(int(job_id))
        if job is None or not job.extracted_json:
            raise CliError(f"job {job_id} not found or not extracted")
        return job, ExtractedInvoice.model_validate_json(job.extracted_json)

    # ── invoices ──────────────────────────────────────────────────────────────

    async def suppliers(self) -> Any:
        return [
            {"slug": s.slug, "title": s.title, "vat": s.vat, "draft": s.is_draft} for s in known_suppliers()
        ]

    async def rules(self, slug: str) -> Any:
        text = load_supplier_rules(slug)
        if text is None:
            raise CliError(f"no rulebook {slug!r}")
        return {"slug": slug, "markdown": text}

    async def detect(self, pdf: str) -> Any:
        data, _ = _pdf(pdf)
        det = await detect_supplier(self.settings, self.model, data)
        return {
            "decided_slug": det.slug,
            "candidates": det.candidates,
            "ident": det.ident.model_dump() if det.ident else None,
            "known": await self.suppliers(),
        }

    async def extract(self, pdf: str, slug: str) -> Any:
        data, path = _pdf(pdf)
        if load_supplier_rules(slug) is None:
            raise CliError(f"no rulebook {slug!r}")
        inv = await extract_invoice(data, path.name, slug, self.settings)
        job_id = self.store.create_job(
            chat_id=0, pdf_message_id=0, filename=path.name, pdf_path=str(path), sender="hava"
        )
        self.store.update_job(job_id, supplier_slug=slug, status="awaiting_doubts", extracted_json=inv.model_dump_json())
        return {
            "job_id": job_id,
            "supplier": slug,
            "draft_rules": onb.is_draft(slug),
            "invoice": _invoice_out(inv),
            "notes": inv.reconciliation.warnings,
            "doubts": _doubts_out(inv, slug, hard_only=False),
        }

    async def job(self, job_id: str) -> Any:
        job, inv = self._job_invoice(job_id)
        slug = job.supplier_slug or ""
        return {
            "job_id": job.id,
            "status": job.status,
            "supplier": slug,
            "document_id": job.document_id,
            "invoice": _invoice_out(inv),
            "doubts": _doubts_out(inv, slug, hard_only=False),
        }

    async def correct(self, job_id: str) -> Any:
        job, inv = self._job_invoice(job_id)
        qa = [tuple(x) for x in _stdin_json().get("qa", [])]
        if not qa:
            raise CliError('stdin must be {"qa": [["question", "answer"], ...]}')
        data = Path(job.pdf_path).read_bytes()
        fixed, corr = await apply_answers(self.settings, self.model, data, inv, job.supplier_slug or "", qa)
        self.store.update_job(job.id, extracted_json=fixed.model_dump_json())
        return {
            "job_id": job.id,
            "note": corr.note,
            "possible_rule_changes": corr.rule_changes,
            "invoice": _invoice_out(fixed),
            "remaining_doubts": _doubts_out(fixed, job.supplier_slug or "", hard_only=True),
        }

    async def apply(self, job_id: str) -> Any:
        job, inv = self._job_invoice(job_id)
        if job.document_id:
            raise CliError(f"job {job.id} was already applied (document {job.document_id})")
        hard = _doubts_out(inv, job.supplier_slug or "", hard_only=True)
        if hard:
            return {"applied": False, "blocking_doubts": hard}
        self.store.update_job(job.id, status="applying")
        created, matched, errors, document_id = [], [], [], None
        async for ev in apply_invoice(inv, self.moloni, self.settings):
            t = ev.get("type")
            if t == "line_created":
                created.append(ev["reference"])
            elif t == "line_matched":
                matched.append(ev["reference"])
            elif t in ("line_error", "invoice_error"):
                errors.append(f"{ev.get('reference', 'fatura')}: {ev.get('message')}")
            elif t == "invoice_created":
                document_id = int(ev["document_id"])
        self.store.update_job(
            job.id, status="done" if document_id else "failed", document_id=document_id, error="; ".join(errors) or None
        )
        return {
            "applied": document_id is not None,
            "document_id": document_id,
            "console_url": f"{self.hs.console_base_url}/invoices/{document_id}" if document_id else None,
            "products_created": len(created),
            "products_existing": len(matched),
            "errors": errors,
        }

    # ── new supplier ──────────────────────────────────────────────────────────

    async def onboard(self, pdf: str) -> Any:
        data, _ = _pdf(pdf)
        det = await detect_supplier(self.settings, self.model, data)
        if det.ident is None:
            raise CliError(f"this invoice matches the existing rulebook {det.slug!r}")
        ident = det.ident
        plan = await onb.plan_onboarding(self.settings, self.model, data, ident)
        cats = await onb.top_categories(self.moloni, self.company_id)
        mats = await onb.maturity_dates(self.moloni, self.company_id)
        return {
            "ident": ident.model_dump(),
            "plan": plan.model_dump(),
            "moloni_suppliers": await onb.find_moloni_suppliers(
                self.moloni, self.company_id, ident.supplier_vat, ident.supplier_name
            ),
            "similar_categories": onb.similar_categories(cats, plan.brand, ident.supplier_name),
            "maturity_dates": mats,
            "shared_defaults": onb.shared_defaults(),
            "pricing_examples": [f"custo × {m}" for m in onb.pricing_examples()],
            "vat_hint": "exemption_reason M10 (intra-community)" if ident.supplier_country.upper() not in ("", "PT") else "taxed at 23% (exemption_reason empty)",
        }

    async def create_category(self, *name_parts: str) -> Any:
        name = " ".join(name_parts).strip().upper()
        if not name:
            raise CliError("usage: create-category <name>")
        cats = await onb.top_categories(self.moloni, self.company_id)
        existing = next((c for c in cats if c["name"].strip().upper() == name), None)
        if existing:
            return {"created": False, **existing}
        res = await self.moloni.post(
            "productCategories/insert",
            {"company_id": self.company_id, "parent_id": 0, "name": name, "description": "", "pos_enabled": 1},
        )
        new_id = int(res.get("category_id", 0)) if isinstance(res, dict) else 0
        if not new_id:
            raise CliError(f"Moloni did not create the category: {res!r}")
        return {"created": True, "id": new_id, "name": name}

    async def write_rules(self, pdf: str) -> Any:
        data, _ = _pdf(pdf)
        body = _stdin_json()
        try:
            ident = Identification.model_validate(body["ident"])
            plan = onb.OnboardingPlan.model_validate(body["plan"])
            cfg_in = body["config"]
            answers = [tuple(x) for x in body.get("answers", [])]
        except (KeyError, ValueError) as e:
            raise CliError(f"stdin needs ident, plan, config, answers: {e}") from e
        required = ("supplier_id", "parent_category_id", "maturity_date_id", "exemption_reason")
        missing = [k for k in required if k not in cfg_in]
        if missing:
            raise CliError(f"config is missing {missing}")
        cfg = {**onb.shared_defaults(), **{k: cfg_in[k] for k in required}}
        notes = {k: str(v) for k, v in (body.get("config_notes") or {}).items()}
        block = onb.render_config_block(cfg, notes)
        markdown = await onb.write_rulebook(
            self.settings, self.model, data, ident=ident, plan=plan, config_block=block, answers=answers
        )
        bad = onb.finalize_and_save(plan.slug, markdown, config_block=block, vat=ident.supplier_vat, name=ident.supplier_name)
        return {
            "slug": plan.slug,
            "saved": True,
            "draft": True,
            "config_problems": bad,
            "console_url": f"{self.hs.console_base_url}/agent/suppliers/{plan.slug}",
        }

    async def approve_rules(self, slug: str) -> Any:
        return {"slug": slug, "approved": onb.approve_rulebook(slug)}

    async def add_rules(self, slug: str) -> Any:
        rules = [str(r).strip() for r in _stdin_json().get("rules", []) if str(r).strip()]
        text = load_supplier_rules(slug)
        if text is None or not rules:
            raise CliError("unknown slug or no rules given")
        heading = "## Team Corrections"
        addition = "\n".join(f"- {r}" for r in rules)
        if heading in text:
            text = text.rstrip() + "\n" + addition + "\n"
        else:
            text = text.rstrip() + f"\n\n{heading}\n\nRules added from the team's answers in Telegram.\n\n{addition}\n"
        save_supplier_rules(slug, text)
        return {"slug": slug, "added": len(rules)}

    # ── sales and catalogue ───────────────────────────────────────────────────

    def _today(self) -> date:
        return datetime.now(ZoneInfo(self.hs.hava_timezone)).date()

    async def sales(self, day: str = "") -> Any:
        d = date.fromisoformat(day) if day else self._today()
        s = await day_sales(self.moloni, self.company_id, d, exclude_refs=self.hs.sales_exclude_refs)
        return {
            "date": d.isoformat(),
            "sales": s.sales,
            "items": s.items,
            "total_with_vat": round(s.with_vat, 2),
            "total_without_vat": round(s.without_vat, 2),
            "vat": round(s.vat, 2),
            "returns": s.returns,
            "returns_with_vat": round(s.returns_with_vat, 2),
            "errors": s.errors,
            "message": _plain(format_report(s, today=d == self._today())),
        }

    async def sold(self, start: str, end: str, *filter_parts: str) -> Any:
        d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
        if d1 < d0 or (d1 - d0).days > 30:
            raise CliError("range must be 1–31 days, start <= end")
        needle = " ".join(filter_parts).strip().lower()
        agg: dict[str, dict[str, Any]] = {}
        docs = 0
        day = d0
        while day <= d1:
            for doc_type in SALE_TYPES:
                rows = await self.moloni.post_all_pages(
                    f"{doc_type}/getAll", {"company_id": self.company_id, "date": day.isoformat()}
                )
                for d in rows or []:
                    if not str(d.get("date", "")).startswith(day.isoformat()) or int(d.get("status") or 0) != 1:
                        continue
                    docs += 1
                    full = await self.moloni.post(
                        f"{doc_type}/getOne", {"company_id": self.company_id, "document_id": d["document_id"]}
                    )
                    for p in (full or {}).get("products", []) if isinstance(full, dict) else []:
                        ref, name = str(p.get("reference", "")), str(p.get("name", ""))
                        if needle and needle not in f"{ref} {name}".lower():
                            continue
                        qty = float(p.get("qty") or 0)
                        net = qty * float(p.get("price") or 0) * (1 - float(p.get("discount") or 0) / 100)
                        row = agg.setdefault(ref + "|" + name, {"reference": ref, "name": name, "qty": 0.0, "net_value": 0.0})
                        row["qty"] += qty
                        row["net_value"] += net
            day += timedelta(days=1)
        items = sorted(agg.values(), key=lambda r: -r["qty"])
        for r in items:
            r["net_value"] = round(r["net_value"], 2)
        return {"from": start, "to": end, "filter": needle or None, "documents": docs, "products": items[:200]}

    async def stock(self) -> Any:
        """Page through the whole catalogue and split stock-tracked products by stock."""
        body = {"company_id": self.company_id}
        expected = await self.moloni.post("products/count", body)
        rows = await self.moloni.post_all_pages("products/getAll", body)
        products = {r["product_id"]: r for r in rows}

        def out(p: dict[str, Any]) -> dict[str, Any]:
            price = float(p.get("price") or 0)
            vat = sum(float(t.get("value") or 0) for t in p.get("taxes") or [])
            return {
                "reference": p.get("reference", ""),
                "name": p.get("name", ""),
                "ean": p.get("ean", ""),
                "stock": float(p.get("stock") or 0),
                "pvp_with_vat": round(price + vat, 2),
            }

        tracked = [out(p) for p in products.values() if int(p.get("has_stock") or 0) == 1]
        return {
            "catalogue_count": int(expected["count"]),
            "products_read": len(products),
            "tracked": len(tracked),
            "in_stock": sorted((p for p in tracked if p["stock"] > 0), key=lambda p: p["reference"]),
            "negative": sorted((p for p in tracked if p["stock"] < 0), key=lambda p: (p["stock"], p["reference"])),
        }

    async def moloni_read(self, endpoint: str) -> Any:
        if not _READ_ENDPOINT.match(endpoint):
            raise CliError("only read endpoints (…/get*, …/count*) are allowed")
        body = _stdin_json()
        body["company_id"] = self.company_id
        return await self.moloni.post(endpoint, body)


COMMANDS = {
    "suppliers": "suppliers",
    "rules": "rules",
    "detect": "detect",
    "extract": "extract",
    "job": "job",
    "correct": "correct",
    "apply": "apply",
    "onboard": "onboard",
    "create-category": "create_category",
    "write-rules": "write_rules",
    "approve-rules": "approve_rules",
    "add-rules": "add_rules",
    "sales": "sales",
    "sold": "sold",
    "stock": "stock",
    "moloni": "moloni_read",
}


async def main(argv: list[str]) -> int:
    if not argv or argv[0] not in COMMANDS:
        print(json.dumps({"error": "unknown command", "commands": sorted(COMMANDS), "help": __doc__}, ensure_ascii=False))
        return 2
    tools = Tools()
    try:
        result = await getattr(tools, COMMANDS[argv[0]])(*argv[1:])
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, default=str))
        return 0
    except TypeError as e:
        print(json.dumps({"ok": False, "error": f"wrong arguments: {e}"}, ensure_ascii=False))
        return 2
    except Exception as e:  # noqa: BLE001 — always answer in JSON
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        return 1
    finally:
        await tools.close()


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    sys.exit(asyncio.run(main(sys.argv[1:])))
