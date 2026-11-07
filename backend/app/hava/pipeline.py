"""The invoice job state machine.

NEW → (supplier known) → EXTRACTING → (doubts?) → AWAITING_DOUBTS → CORRECTING
    → APPLYING → DONE
NEW → (unsure / new) → AWAITING_SUPPLIER → ONBOARDING → WRITING_RULES → EXTRACTING …

Each step runs in one worker, one job at a time (Moloni and the model are the
bottlenecks, and sequential writes keep Moloni's rate limit happy). Steps that
wait on the group end by asking questions; when the last open question of a
job is answered, the job is queued again and resumes where it stopped.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
from typing import Any

from app.agent.applier import apply_invoice
from app.agent.prompts import load_supplier_rules, save_supplier_rules
from app.agent.runner import ExtractionError, extract_invoice
from app.agent.schema import ExtractedInvoice
from app.config import Settings
from app.hava import onboarding as onb
from app.hava import store as st
from app.hava.config import HavaSettings
from app.hava.detect import Identification, detect_supplier, known_suppliers
from app.hava.doubts import CANCEL, apply_answers, find_doubts, summary_text
from app.hava.llm import LLMError
from app.hava.store import Job, Question, Store
from app.hava.telegram import Telegram, TelegramError, esc
from app.moloni_client import MoloniAPIError, MoloniClient

log = logging.getLogger("hava.pipeline")

NEW_SUPPLIER = "__new__"
MAX_DOUBT_ROUNDS = 3
REPLY_HINT = "\n\n<i>Escolhe um botão ou responde a esta mensagem.</i>"


class Pipeline:
    def __init__(
        self, hs: HavaSettings, settings: Settings, tg: Telegram, store: Store, moloni: MoloniClient
    ) -> None:
        self.hs = hs
        self.settings = settings
        self.tg = tg
        self.store = store
        self.moloni = moloni
        self.company_id = settings.moloni_company_id
        self.queue: asyncio.Queue[int] = asyncio.Queue()

    # ── queue / worker ───────────────────────────────────────────────────────

    def enqueue(self, job_id: int) -> None:
        self.queue.put_nowait(job_id)

    def resume_after_restart(self) -> None:
        for job in self.store.jobs_with_status({st.APPLYING}):
            self.store.update_job(job.id, status=st.FAILED, error="interrupted while applying")
            asyncio.get_running_loop().create_task(
                self.say(
                    job,
                    "⚠️ Fui reiniciada a meio de criar esta fatura no Moloni. "
                    "Verifica na consola o que ficou criado antes de a enviares de novo.",
                )
            )
        for job in self.store.jobs_with_status(st.RUNNABLE):
            self.enqueue(job.id)
        for job in self.store.jobs_with_status({st.AWAITING_SUPPLIER, st.ONBOARDING, st.AWAITING_DOUBTS}):
            if self.store.open_count(job.id) == 0:
                self.enqueue(job.id)

    async def worker(self) -> None:
        while True:
            job_id = await self.queue.get()
            try:
                await self.step(job_id)
            except Exception as e:  # noqa: BLE001 — one bad invoice must not stop the worker
                log.exception("job %s crashed", job_id)
                job = self.store.get_job(job_id)
                if job and job.status not in st.TERMINAL:
                    await self.fail(job, f"{type(e).__name__}: {e}")
            finally:
                self.queue.task_done()

    async def step(self, job_id: int) -> None:
        job = self.store.get_job(job_id)
        if not job or job.status in st.TERMINAL:
            return
        handlers = {
            st.NEW: self.detect,
            st.AWAITING_SUPPLIER: self.on_supplier_answer,
            st.ONBOARDING: self.on_onboarding_answers,
            st.WRITING_RULES: self.write_rules,
            st.EXTRACTING: self.extract,
            st.AWAITING_DOUBTS: self.on_doubt_answers,
            st.CORRECTING: self.correct,
        }
        handler = handlers.get(job.status)
        if handler is None:
            log.warning("job %s: nothing to do in status %s", job.id, job.status)
            return
        if job.status in (st.AWAITING_SUPPLIER, st.ONBOARDING, st.AWAITING_DOUBTS) and self.store.open_count(job.id):
            return  # still waiting on the group
        await handler(job)

    # ── talking to the group ─────────────────────────────────────────────────

    async def say(self, job: Job, text: str) -> None:
        try:
            await self.tg.send(job.chat_id, text, reply_to=job.pdf_message_id)
        except TelegramError as e:
            log.warning("job %s: send failed: %s", job.id, e)

    async def ask(
        self,
        job: Job,
        kind: str,
        text: str,
        options: list[dict[str, Any]],
        payload: dict[str, Any] | None = None,
    ) -> None:
        qid = self.store.add_question(
            job_id=job.id, chat_id=job.chat_id, kind=kind, text=text, options=options, payload=payload
        )
        buttons = [[{"text": o["label"][:60], "callback_data": f"q:{qid}:{i}"}] for i, o in enumerate(options)]
        msg = await self.tg.send(job.chat_id, text + REPLY_HINT, reply_to=job.pdf_message_id, buttons=buttons or None)
        self.store.set_question_message(qid, int(msg["message_id"]))

    async def fail(self, job: Job, reason: str) -> None:
        self.store.update_job(job.id, status=st.FAILED, error=reason[:2000])
        self.store.cancel_open_questions(job.id)
        await self.say(job, f"❌ Não consegui tratar esta fatura.\n<code>{esc(reason[:800])}</code>")

    async def cancel(self, job: Job, by: str) -> None:
        self.store.update_job(job.id, status=st.CANCELLED)
        self.store.cancel_open_questions(job.id)
        await self.say(job, f"🛑 Fatura cancelada por {esc(by)}. Não criei nada no Moloni.")

    def latest(self, job_id: int, kind: str) -> Question | None:
        answered = [q for q in self.store.questions(job_id, kind_prefix=kind) if q.status == "answered" and q.kind == kind]
        return answered[-1] if answered else None

    # ── answers from the bot ─────────────────────────────────────────────────

    async def on_answer(self, q: Question) -> None:
        """Called by the bot after a question was answered."""
        job = self.store.get_job(q.job_id)
        if not job:
            return
        if q.answer_value == "cancel" and job.status not in st.TERMINAL:
            await self.cancel(job, q.answered_by or "alguém")
            return
        if q.kind == "approve_rules":
            await self.on_approve_rules(job, q)
            return
        if q.kind == "rules_update":
            await self.on_rules_update(job, q)
            return
        if job.status not in st.TERMINAL and self.store.open_count(job.id) == 0:
            self.enqueue(job.id)

    # ── step: who is the supplier? ───────────────────────────────────────────

    async def detect(self, job: Job) -> None:
        pdf = job.pdf_bytes()
        det = await detect_supplier(self.settings, self.hs.hava_model, pdf)
        ctx = job.context
        if det.ident:
            ctx["ident"] = det.ident.model_dump()
        self.store.update_job(job.id, context=ctx)

        if det.ident and not det.ident.is_invoice:
            self.store.update_job(job.id, status=st.CANCELLED)
            await self.say(
                job,
                f"🤔 Isto não me parece uma fatura de fornecedor ({esc(det.ident.reason)}). "
                "Não fiz nada. Se for mesmo uma fatura, envia-a outra vez.",
            )
            return

        suppliers = {s.slug: s for s in known_suppliers()}
        if det.slug:
            title = suppliers[det.slug].title if det.slug in suppliers else det.slug
            self.store.update_job(job.id, supplier_slug=det.slug, status=st.EXTRACTING)
            await self.say(job, f"📄 Fornecedor: <b>{esc(title)}</b>. A ler a fatura…")
            self.enqueue(job.id)
            return

        await self.ask_supplier(job, det.candidates, det.ident)

    async def ask_supplier(self, job: Job, candidates: list[str], ident: Identification | None) -> None:
        suppliers = known_suppliers()
        by_slug = {s.slug: s for s in suppliers}
        options: list[dict[str, Any]] = [
            {"label": by_slug[c].title, "value": c} for c in candidates if c in by_slug
        ]
        who = ""
        if ident:
            who = f"Fatura de <b>{esc(ident.supplier_name)}</b>"
            if ident.supplier_vat:
                who += f" (NIF {esc(ident.supplier_vat)})"
            if ident.brand and ident.brand.lower() not in ident.supplier_name.lower():
                who += f", marca <b>{esc(ident.brand)}</b>"
            who += ".\n"
        new = {"label": "Fornecedor novo — criar regras", "value": NEW_SUPPLIER}
        if candidates:
            text = f"{who}Não tenho a certeza de qual é o fornecedor. Qual é?"
            options.append(new)
        else:
            text = (
                f"{who}Não conheço este fornecedor. É novo? Se for, preparo as regras dele "
                "a partir desta fatura (com umas perguntas pelo caminho)."
            )
            options = [new] + [{"label": s.title, "value": s.slug} for s in suppliers][:6]
        options.append(CANCEL)
        self.store.update_job(job.id, status=st.AWAITING_SUPPLIER)
        await self.ask(job, "pick_supplier", text, options)

    async def on_supplier_answer(self, job: Job) -> None:
        q = self.latest(job.id, "pick_supplier")
        if q is None:
            return
        value = q.answer_value
        if value is None:  # typed answer: match it against known suppliers
            value = self._match_supplier_text(q.answer_text or "")
            if value is None:
                await self.ask_supplier(job, [], Identification.model_validate(job.context["ident"]) if job.context.get("ident") else None)
                return
        if value == NEW_SUPPLIER:
            await self.start_onboarding(job)
            return
        title = next((s.title for s in known_suppliers() if s.slug == value), value)
        self.store.update_job(job.id, supplier_slug=value, status=st.EXTRACTING)
        await self.say(job, f"📄 Fornecedor: <b>{esc(title)}</b>. A ler a fatura…")
        self.enqueue(job.id)

    @staticmethod
    def _match_supplier_text(text: str) -> str | None:
        t = text.strip().lower()
        if not t:
            return None
        if any(w in t for w in ("novo", "nova", "new")):
            return NEW_SUPPLIER
        suppliers = known_suppliers()
        names = {s.title.lower(): s.slug for s in suppliers} | {s.slug.replace("_", " "): s.slug for s in suppliers}
        for name, slug in names.items():
            if t in name or name in t:
                return slug
        close = difflib.get_close_matches(t, list(names), n=1, cutoff=0.6)
        return names[close[0]] if close else None

    # ── step: new supplier onboarding ────────────────────────────────────────

    async def start_onboarding(self, job: Job) -> None:
        ident_raw = job.context.get("ident")
        if not ident_raw:
            await self.fail(job, "sem dados do fornecedor para criar regras")
            return
        ident = Identification.model_validate(ident_raw)
        self.store.update_job(job.id, status=st.ONBOARDING)
        await self.say(job, f"🆕 Vou preparar as regras de <b>{esc(ident.brand or ident.supplier_name)}</b>. Um momento…")
        await self.tg.typing(job.chat_id)

        pdf = job.pdf_bytes()
        plan = await onb.plan_onboarding(self.settings, self.hs.hava_model, pdf, ident)
        moloni_suppliers = await onb.find_moloni_suppliers(
            self.moloni, self.company_id, ident.supplier_vat, ident.supplier_name
        )
        mats = await onb.maturity_dates(self.moloni, self.company_id)
        cats = await onb.top_categories(self.moloni, self.company_id)

        state: dict[str, Any] = {"defaults": onb.shared_defaults(), "notes": {}}
        ctx = job.context
        ctx["plan"] = plan.model_dump()
        ctx["onb"] = state
        ctx["maturities"] = mats

        # Moloni supplier
        if len(moloni_suppliers) == 1:
            s = moloni_suppliers[0]
            state["supplier_id"], state["notes"]["supplier_id"] = s["supplier_id"], s["name"]
        else:
            ctx["moloni_suppliers"] = moloni_suppliers
        # Payment terms
        if plan.payment_days is not None:
            m = next((m for m in mats if m["days"] == plan.payment_days), None)
            if m:
                state["maturity_date_id"], state["notes"]["maturity_date_id"] = m["id"], m["name"]
        self.store.update_job(job.id, context=ctx)

        brand = plan.brand or ident.brand or ident.supplier_name
        found = ""
        if "supplier_id" in state:
            found += f"• Fornecedor no Moloni: {esc(state['notes']['supplier_id'])}\n"
        if "maturity_date_id" in state:
            found += f"• Pagamento: {esc(state['notes']['maturity_date_id'])}\n"
        if found:
            await self.say(job, "Já encontrei:\n" + found)

        if "supplier_id" not in state:
            await self.ask_moloni_supplier(job, ident, moloni_suppliers)

        similar = onb.similar_categories(cats, brand, ident.supplier_name)
        cat_options = [{"label": c["name"], "value": {"id": c["id"], "name": c["name"]}} for c in similar]
        cat_options.append({"label": f"Criar categoria «{brand.upper()}»", "value": {"create": brand.upper()}})
        await self.ask(
            job,
            "onb:category",
            f"Em que <b>categoria do Moloni</b> ficam os artigos {esc(brand)}? "
            "(as subcategorias, como T-SHIRT ou CALCAS, ficam dentro dela)",
            cat_options,
        )

        country = (ident.supplier_country or "").upper()
        exempt = {"label": "Isenta — intracomunitária (M10)", "value": "M10"}
        taxed = {"label": "Com IVA a 23%", "value": ""}
        if country == "PT":
            vat_text = "<b>IVA</b>: fornecedor português — a fatura leva IVA a 23%?"
            vat_options = [taxed, exempt]
        else:
            where = f" ({esc(country)})" if country else ""
            vat_text = f"<b>IVA</b>: fornecedor de fora de Portugal{where}. Como tratamos a fatura?"
            vat_options = [exempt, taxed]
        await self.ask(job, "onb:vat", vat_text, vat_options)

        if "maturity_date_id" not in state:
            await self.ask(
                job,
                "onb:maturity",
                "<b>Prazo de pagamento</b> desta fatura?",
                [{"label": m["name"], "value": m["id"]} for m in mats][:6],
            )

        examples = onb.pricing_examples()
        await self.ask(
            job,
            "onb:pricing",
            f"<b>Preço de venda</b>: qual a regra do PVP (com IVA) para {esc(brand)}?"
            + (" Os outros fornecedores usam " + ", ".join(f"custo × {esc(e)}" for e in examples) + "." if examples else ""),
            [{"label": f"custo × {e}", "value": f"PVP com IVA = custo unitário × {e}"} for e in examples],
        )

        await self.ask(
            job,
            "onb:conventions",
            f"<b>Proposta de regras para {esc(brand)}:</b>\n{esc(plan.conventions)}\n\nEstá bem assim?",
            [{"label": "Está bem", "value": "ok"}],
        )
        for oq in plan.questions[:4]:
            await self.ask(
                job,
                f"onb:ask:{oq.key}",
                esc(oq.question),
                [{"label": o, "value": o} for o in oq.options[:4]],
            )

    async def ask_moloni_supplier(self, job: Job, ident: Identification, found: list[dict[str, Any]]) -> None:
        if found:
            await self.ask(
                job,
                "onb:supplier",
                f"Encontrei vários fornecedores no Moloni parecidos com <b>{esc(ident.supplier_name)}</b>. Qual é?",
                [{"label": f"{s['name']} ({s['vat']})", "value": s["supplier_id"]} for s in found],
            )
        else:
            await self.ask(
                job,
                "onb:supplier",
                f"Não encontrei <b>{esc(ident.supplier_name)}</b> (NIF {esc(ident.supplier_vat or '?')}) "
                "no Moloni. Cria-o lá (Compras › Fornecedores) e carrega em «Já criei», "
                "ou responde com o ID do fornecedor no Moloni.",
                [{"label": "Já criei", "value": "recheck"}, CANCEL],
            )

    async def on_onboarding_answers(self, job: Job) -> None:
        ctx = job.context
        state = ctx["onb"]
        ident = Identification.model_validate(ctx["ident"])

        # Moloni supplier
        if "supplier_id" not in state:
            q = self.latest(job.id, "onb:supplier")
            sup = await self._resolve_supplier(q, ident) if q else None
            if sup is None:
                again = await onb.find_moloni_suppliers(self.moloni, self.company_id, ident.supplier_vat, ident.supplier_name)
                if len(again) == 1:
                    sup = again[0]
                else:
                    self.store.update_job(job.id, context=ctx)
                    await self.ask_moloni_supplier(job, ident, again)
                    return
            state["supplier_id"], state["notes"]["supplier_id"] = sup["supplier_id"], sup["name"]

        # Category
        if "parent_category_id" not in state:
            q = self.latest(job.id, "onb:category")
            cat = await self._resolve_category(q) if q else None
            if cat is None:
                await self.fail(job, "não consegui definir a categoria")
                return
            state["parent_category_id"], state["notes"]["parent_category_id"] = cat["id"], cat["name"]

        # VAT
        q = self.latest(job.id, "onb:vat")
        if q is not None:
            v = q.answer_value
            if v is None:
                v = "M10" if re.search(r"isent|intra|m10", (q.answer_text or "").lower()) else ""
            state["exemption_reason"] = v

        # Payment terms
        if "maturity_date_id" not in state:
            q = self.latest(job.id, "onb:maturity")
            mats = ctx.get("maturities", [])
            mid = q.answer_value if q else None
            if mid is None and q is not None:
                days = re.findall(r"\d+", q.answer_text or "")
                m = next((m for m in mats if days and m["days"] == int(days[0])), None)
                mid = m["id"] if m else None
            if mid is None:
                mid = next((m["id"] for m in mats if m["days"] == 0), mats[0]["id"] if mats else 0)
            state["maturity_date_id"] = int(mid)
            state["notes"]["maturity_date_id"] = next((m["name"] for m in mats if m["id"] == int(mid)), "")

        ctx["onb"] = state
        self.store.update_job(job.id, context=ctx, status=st.WRITING_RULES)
        await self.say(job, "✍️ Obrigada! A escrever as regras do fornecedor…")
        self.enqueue(job.id)

    async def _resolve_supplier(self, q: Question, ident: Identification) -> dict[str, Any] | None:
        v = q.answer_value
        if isinstance(v, int):
            name = next((o["label"] for o in q.options if o.get("value") == v), str(v))
            return {"supplier_id": v, "name": name}
        if v == "recheck":
            return None
        text = (q.answer_text or "").strip()
        if text.isdigit():
            try:
                res = await self.moloni.post("suppliers/getOne", {"company_id": self.company_id, "supplier_id": int(text)})
            except MoloniAPIError:
                res = None
            if isinstance(res, dict) and res.get("supplier_id"):
                return {"supplier_id": int(res["supplier_id"]), "name": str(res.get("name", ""))}
            return None
        if text:
            found = await onb.find_moloni_suppliers(self.moloni, self.company_id, "", text)
            return found[0] if len(found) == 1 else None
        return None

    async def _resolve_category(self, q: Question) -> dict[str, Any] | None:
        v = q.answer_value
        if isinstance(v, dict) and v.get("id"):
            return {"id": int(v["id"]), "name": str(v.get("name", ""))}
        name = v.get("create") if isinstance(v, dict) else (q.answer_text or "").strip().upper()
        if not name:
            return None
        cats = await onb.top_categories(self.moloni, self.company_id)
        existing = next((c for c in cats if c["name"].strip().upper() == name.strip().upper()), None)
        if existing:
            return existing
        res = await self.moloni.post(
            "productCategories/insert",
            {"company_id": self.company_id, "parent_id": 0, "name": name, "description": "", "pos_enabled": 1},
        )
        new_id = int(res.get("category_id", 0)) if isinstance(res, dict) else 0
        if not new_id:
            raise MoloniAPIError(f"productCategories/insert não devolveu id: {res!r}")
        return {"id": new_id, "name": name}

    async def write_rules(self, job: Job) -> None:
        ctx = job.context
        state = ctx["onb"]
        ident = Identification.model_validate(ctx["ident"])
        plan = onb.OnboardingPlan.model_validate(ctx["plan"])
        cfg = {**state.get("defaults", {}), **{k: state[k] for k in ("supplier_id", "parent_category_id", "maturity_date_id", "exemption_reason") if k in state}}
        block = onb.render_config_block(cfg, state.get("notes", {}))

        answers: list[tuple[str, str]] = []
        for q in self.store.questions(job.id, kind_prefix="onb:"):
            if q.status != "answered":
                continue
            if q.kind in ("onb:pricing", "onb:conventions", "onb:vat") or q.kind.startswith("onb:ask:"):
                answers.append((re.sub(r"<[^>]+>", "", q.text), q.answer_label))
        await self.tg.typing(job.chat_id)
        markdown = await onb.write_rulebook(
            self.settings, self.hs.hava_model, job.pdf_bytes(),
            ident=ident, plan=plan, config_block=block, answers=answers,
        )
        missing = onb.finalize_and_save(plan.slug, markdown, config_block=block, vat=ident.supplier_vat, name=ident.supplier_name)
        if missing:
            await self.fail(job, f"regras gravadas mas com configuração incompleta: {', '.join(missing)}")
            return
        url = f"{self.hs.console_base_url}/agent/suppliers/{plan.slug}"
        self.store.update_job(job.id, supplier_slug=plan.slug, status=st.EXTRACTING)
        await self.say(
            job,
            f"📘 Regras de <b>{esc(plan.brand)}</b> escritas (rascunho até aprovares a primeira fatura). "
            f'Podes vê-las e editá-las na <a href="{esc(url)}">consola</a>.\n\n📄 A ler a fatura…',
        )
        self.enqueue(job.id)

    # ── step: read the invoice, then ask about doubts ────────────────────────

    async def extract(self, job: Job) -> None:
        slug = job.supplier_slug or ""
        await self.tg.typing(job.chat_id)
        try:
            inv = await extract_invoice(job.pdf_bytes(), job.filename, slug, self.settings)
        except ExtractionError as e:
            await self.fail(job, f"leitura da fatura falhou: {e}")
            return
        self.store.update_job(job.id, extracted_json=inv.model_dump_json())
        await self.ask_doubts(job, inv, round_no=1, hard_only=False)

    async def ask_doubts(self, job: Job, inv: ExtractedInvoice, *, round_no: int, hard_only: bool) -> None:
        slug = job.supplier_slug or ""
        doubts = find_doubts(inv, draft_rules=onb.is_draft(slug) and not hard_only, hard_only=hard_only)
        if not doubts:
            await self.apply(job, inv)
            return
        ctx = job.context
        ctx["doubt_round"] = round_no
        self.store.update_job(job.id, context=ctx, status=st.AWAITING_DOUBTS)
        intro = summary_text(inv) + (f"\nTenho {len(doubts)} dúvida(s):" if round_no == 1 else "\nAinda há isto por resolver:")
        await self.say(job, intro)
        for d in doubts:
            await self.ask(job, f"doubt:{d.kind}", d.text, d.options, payload={"round": round_no})

    async def on_doubt_answers(self, job: Job) -> None:
        round_no = int(job.context.get("doubt_round", 1))
        qs = [
            q for q in self.store.questions(job.id, kind_prefix="doubt:")
            if q.status == "answered" and q.payload.get("round") == round_no
        ]
        qa = [(q.text, q.answer_label) for q in qs if q.answer_value != "ok"]
        inv = ExtractedInvoice.model_validate_json(job.extracted_json or "{}")
        if not qa:
            await self.apply(job, inv)
            return
        ctx = job.context
        ctx["pending_qa"] = qa
        self.store.update_job(job.id, context=ctx, status=st.CORRECTING)
        self.enqueue(job.id)

    async def correct(self, job: Job) -> None:
        ctx = job.context
        qa = [tuple(x) for x in ctx.get("pending_qa", [])]
        inv = ExtractedInvoice.model_validate_json(job.extracted_json or "{}")
        await self.tg.typing(job.chat_id)
        try:
            fixed, corr = await apply_answers(
                self.settings, self.hs.hava_model, job.pdf_bytes(), inv, job.supplier_slug or "", qa
            )
        except LLMError as e:
            await self.fail(job, f"não consegui aplicar as respostas: {e}")
            return
        ctx["rule_changes"] = list(dict.fromkeys(ctx.get("rule_changes", []) + corr.rule_changes))
        ctx.pop("pending_qa", None)
        self.store.update_job(job.id, context=ctx, extracted_json=fixed.model_dump_json())
        if corr.note:
            await self.say(job, f"✏️ {esc(corr.note)}")
        round_no = int(ctx.get("doubt_round", 1)) + 1
        if round_no > MAX_DOUBT_ROUNDS and find_doubts(fixed, draft_rules=False, hard_only=True):
            await self.fail(job, "continuam a faltar dados depois de 3 rondas de perguntas")
            return
        job = self.store.get_job(job.id) or job
        await self.ask_doubts(job, fixed, round_no=round_no, hard_only=True)

    # ── step: write to Moloni ────────────────────────────────────────────────

    async def apply(self, job: Job, inv: ExtractedInvoice) -> None:
        self.store.update_job(job.id, status=st.APPLYING)
        await self.say(job, "⚙️ A criar os artigos e o rascunho da fatura no Moloni…")
        created, matched, line_errors = 0, 0, []
        document_id: int | None = None
        invoice_errors: list[str] = []
        async for ev in apply_invoice(inv, self.moloni, self.settings):
            t = ev.get("type")
            if t == "line_created":
                created += 1
            elif t == "line_matched":
                matched += 1
            elif t == "line_error":
                line_errors.append(f"{ev.get('reference')}: {ev.get('message')}")
            elif t == "invoice_created":
                document_id = int(ev["document_id"])
            elif t == "invoice_error":
                invoice_errors.append(str(ev.get("message")))

        if document_id is None:
            detail = "\n".join(f"• {esc(e)}" for e in (line_errors + invoice_errors)[:10])
            self.store.update_job(job.id, status=st.FAILED, error="; ".join(line_errors + invoice_errors)[:2000])
            await self.say(
                job,
                "❌ O Moloni não aceitou a fatura.\n" + detail
                + (f"\n\nCriei {created} artigo(s) novo(s) antes do erro." if created else ""),
            )
            return

        self.store.update_job(job.id, status=st.DONE, document_id=document_id)
        url = f"{self.hs.console_base_url}/invoices/{document_id}"
        await self.say(
            job,
            f"✅ Rascunho criado no Moloni: <a href=\"{esc(url)}\">fatura {esc(inv.header.invoice_number)}</a>\n"
            f"{created} artigo(s) novo(s), {matched} já existia(m).\n"
            "Revê os preços e os campos na consola e fecha a fatura lá.",
        )

        slug = job.supplier_slug or ""
        job = self.store.get_job(job.id) or job
        changes = job.context.get("rule_changes", [])
        if changes:
            await self.ask(
                job,
                "rules_update",
                "Algumas respostas pareceram-me regras deste fornecedor, não só desta fatura:\n"
                + "\n".join(f"• {esc(c)}" for c in changes)
                + "\n\nAcrescento-as às regras?",
                [{"label": "Sim, acrescenta", "value": "yes"}, {"label": "Não, foi só nesta", "value": "no"}],
            )
        if onb.is_draft(slug):
            await self.ask(
                job,
                "approve_rules",
                "As regras deste fornecedor ainda são <b>rascunho</b>. Quando confirmares esta fatura "
                "na consola e estiver tudo certo, carrega em «Regras OK».",
                [{"label": "Regras OK", "value": "approve"}],
            )

    async def on_approve_rules(self, job: Job, q: Question) -> None:
        slug = job.supplier_slug or ""
        if q.answer_value == "approve" and onb.approve_rulebook(slug):
            await self.say(job, f"👍 Regras de <b>{esc(slug)}</b> aprovadas. As próximas faturas deste fornecedor seguem-nas.")

    async def on_rules_update(self, job: Job, q: Question) -> None:
        slug = job.supplier_slug or ""
        changes = job.context.get("rule_changes", [])
        if q.answer_value != "yes" and not (q.answer_text and q.answer_text.strip().lower().startswith("s")):
            return
        text = load_supplier_rules(slug)
        if text is None or not changes:
            return
        heading = "## Team Corrections"
        addition = "\n".join(f"- {c}" for c in changes)
        if heading in text:
            text = text.rstrip() + "\n" + addition + "\n"
        else:
            text = text.rstrip() + f"\n\n{heading}\n\nRules added from the team's answers in Telegram.\n\n{addition}\n"
        save_supplier_rules(slug, text)
        await self.say(job, "📘 Regras atualizadas.")
