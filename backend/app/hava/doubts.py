"""Find what is doubtful in an extracted invoice, and apply the group's answers.

Checks are deterministic and grouped by kind, so a 60-line invoice with ten
short-reference problems produces one question, not ten.
"""

from __future__ import annotations

import logging
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agent.applier import MAX_REFERENCE_LEN
from app.agent.prompts import load_supplier_rules
from app.agent.schema import ExtractedHeader, ExtractedInvoice, ExtractedLine
from app.config import Settings
from app.hava.llm import ask_json, pdf_block, positioned_text_block
from app.hava.telegram import esc

log = logging.getLogger("hava.doubts")

OK = {"label": "Está certo, avançar", "value": "ok"}
CANCEL = {"label": "Cancelar fatura", "value": "cancel"}

# Kinds that must be resolved before Moloni will accept the invoice.
HARD_KINDS = {"empty", "reference_long", "reference_missing", "reference_dup", "qty", "cost", "price", "subcategory"}

_MAX_LISTED = 12


@dataclass
class Doubt:
    kind: str
    text: str  # HTML, Portuguese
    options: list[dict[str, Any]] = field(default_factory=lambda: [OK, CANCEL])


def _line_label(i: int, line: ExtractedLine) -> str:
    bits = [line.reference or "(sem ref.)", line.name]
    if line.color:
        bits.append(line.color)
    if line.size:
        bits.append(line.size)
    return f"#{i + 1} " + " · ".join(esc(b) for b in bits if b)


def _listing(items: list[str]) -> str:
    shown = items[:_MAX_LISTED]
    more = len(items) - len(shown)
    out = "\n".join(f"• {s}" for s in shown)
    return out + (f"\n• … e mais {more}" if more > 0 else "")


def _eur(v: float) -> str:
    return f"{v:,.2f} €".replace(",", " ").replace(".", ",")


def find_doubts(inv: ExtractedInvoice, *, draft_rules: bool, hard_only: bool = False) -> list[Doubt]:
    doubts: list[Doubt] = []
    lines = inv.lines

    if not lines:
        return [
            Doubt(
                "empty",
                "Não encontrei nenhuma linha de produto nesta fatura. "
                "Responde a esta mensagem a dizer o que se passa, ou cancela.",
                [CANCEL],
            )
        ]

    # ── per-line problems ────────────────────────────────────────────────────
    long_refs = [i for i, l in enumerate(lines) if len(l.reference) > MAX_REFERENCE_LEN]
    if long_refs:
        doubts.append(
            Doubt(
                "reference_long",
                f"<b>Referências com mais de {MAX_REFERENCE_LEN} caracteres</b> (o Moloni recusa-as):\n"
                + _listing([f"{_line_label(i, lines[i])} ({len(lines[i].reference)})" for i in long_refs])
                + "\n\nQueres que as encurte, abreviando a cor? Ou responde com as referências certas.",
                [{"label": "Encurta, abreviando a cor", "value": "shorten"}, CANCEL],
            )
        )

    missing_ref = [i for i, l in enumerate(lines) if not l.reference.strip()]
    if missing_ref:
        doubts.append(
            Doubt(
                "reference_missing",
                "<b>Linhas sem referência:</b>\n"
                + _listing([_line_label(i, lines[i]) for i in missing_ref])
                + "\n\nResponde com a referência de cada uma.",
                [CANCEL],
            )
        )

    refs = Counter(l.reference.strip().upper() for l in lines if l.reference.strip())
    dups = [r for r, n in refs.items() if n > 1]
    if dups:
        doubts.append(
            Doubt(
                "reference_dup",
                "<b>A mesma referência aparece em várias linhas:</b>\n"
                + _listing([esc(r) for r in dups])
                + "\n\nSão o mesmo artigo (junto as quantidades) ou a referência está errada?",
                [{"label": "Junta as quantidades", "value": "merge"}, CANCEL],
            )
        )

    bad_qty = [i for i, l in enumerate(lines) if l.qty < 1 or l.qty != int(l.qty)]
    if bad_qty:
        doubts.append(
            Doubt(
                "qty",
                "<b>Quantidades estranhas</b> (zero, negativas ou com decimais):\n"
                + _listing([f"{_line_label(i, lines[i])} → qtd {lines[i].qty:g}" for i in bad_qty])
                + "\n\nResponde com as quantidades certas.",
                [CANCEL],
            )
        )

    bad_cost = [i for i, l in enumerate(lines) if l.unit_cost <= 0]
    if bad_cost:
        doubts.append(
            Doubt(
                "cost",
                "<b>Linhas sem custo unitário:</b>\n"
                + _listing([_line_label(i, lines[i]) for i in bad_cost])
                + "\n\nResponde com o custo de cada uma (sem IVA).",
                [CANCEL],
            )
        )

    bad_price = [i for i, l in enumerate(lines) if l.pvp_with_vat <= 0 or l.moloni_price_no_vat <= 0]
    if bad_price:
        doubts.append(
            Doubt(
                "price",
                "<b>Linhas sem preço de venda calculado:</b>\n"
                + _listing([_line_label(i, lines[i]) for i in bad_price])
                + "\n\nAplico a regra de preço do fornecedor, ou indicas o PVP?",
                [{"label": "Aplica a regra de preço", "value": "apply_rule"}, CANCEL],
            )
        )

    no_subcat = [i for i, l in enumerate(lines) if not l.subcategory_name.strip()]
    if no_subcat:
        doubts.append(
            Doubt(
                "subcategory",
                "<b>Não sei a subcategoria destes artigos:</b>\n"
                + _listing([_line_label(i, lines[i]) for i in no_subcat])
                + "\n\nResponde com a subcategoria (ex.: T-SHIRT, CALCAS, VESTIDO).",
                [CANCEL],
            )
        )

    if hard_only:
        return doubts

    sized = sum(1 for l in lines if l.size.strip())
    if sized and sized >= len(lines) / 2:
        no_size = [i for i, l in enumerate(lines) if not l.size.strip()]
        if no_size:
            doubts.append(
                Doubt(
                    "size",
                    "<b>Linhas sem tamanho</b>, quando as outras têm:\n"
                    + _listing([_line_label(i, lines[i]) for i in no_size])
                    + "\n\nSão tamanho único ou falta o tamanho?",
                    [{"label": "Tamanho único", "value": "one_size"}, OK, CANCEL],
                )
            )

    # PVP/cost ratio should be the supplier's fixed multiplier; an outlier means a misread.
    ratios = [(i, l.pvp_with_vat / l.unit_cost) for i, l in enumerate(lines) if l.unit_cost > 0 and l.pvp_with_vat > 0]
    if len(ratios) >= 3:
        med = statistics.median(r for _, r in ratios)
        outliers = [i for i, r in ratios if abs(r - med) / med > 0.03]
        if outliers:
            doubts.append(
                Doubt(
                    "price_ratio",
                    f"<b>Preços fora do padrão</b> (normalmente PVP ≈ custo × {med:.2f}):\n"
                    + _listing(
                        [
                            f"{_line_label(i, lines[i])} → custo {_eur(lines[i].unit_cost)}, "
                            f"PVP {_eur(lines[i].pvp_with_vat)}"
                            for i in outliers
                        ]
                    )
                    + "\n\nEstão certos? Se não, diz-me o que corrigir.",
                )
            )

    # ── header ───────────────────────────────────────────────────────────────
    h = inv.header
    header_issues: list[str] = []
    if not h.invoice_number.strip():
        header_issues.append("não encontrei o número da fatura")
    try:
        d = date.fromisoformat(h.date)
        if d > date.today():
            header_issues.append(f"a data {esc(h.date)} é no futuro")
    except ValueError:
        header_issues.append(f"a data «{esc(h.date)}» não é válida")
    if header_issues:
        doubts.append(
            Doubt(
                "header",
                "<b>Cabeçalho da fatura:</b> " + "; ".join(header_issues) + ".\n\nResponde com os dados certos.",
                [CANCEL],
            )
        )

    rec = inv.reconciliation
    if not rec.matches_invoice_total:
        doubts.append(
            Doubt(
                "totals",
                "<b>Os totais não batem certo.</b>\n"
                f"Soma das linhas: {_eur(rec.calculated_subtotal)}\n"
                f"Subtotal na fatura: {_eur(h.subtotal)}\n"
                f"Total na fatura: {_eur(h.grand_total)}\n\n"
                "Avanço assim, ou há linhas em falta/erradas?",
            )
        )

    warnings = [w for w in rec.warnings if w.strip()]
    if warnings:
        doubts.append(
            Doubt(
                "warnings",
                "<b>Notas da leitura da fatura:</b>\n" + _listing([esc(w) for w in warnings]) + "\n\nAlguma coisa a corrigir?",
            )
        )

    if draft_rules:
        preview = _listing(
            [
                f"{_line_label(i, l)} · {l.qty:g} un · custo {_eur(l.unit_cost)} · PVP {_eur(l.pvp_with_vat)} · {esc(l.subcategory_name)}"
                for i, l in enumerate(lines)
            ]
        )
        doubts.append(
            Doubt(
                "draft_review",
                "<b>Primeira fatura com regras novas — confirma a leitura:</b>\n"
                f"Fatura {esc(h.invoice_number)} de {esc(h.date)}, {len(lines)} linhas, total {_eur(h.grand_total)}\n\n"
                + preview,
                [{"label": "Confirmo, criar no Moloni", "value": "ok"}, CANCEL],
            )
        )

    return doubts


def summary_text(inv: ExtractedInvoice) -> str:
    h = inv.header
    units = sum(l.qty for l in inv.lines)
    return (
        f"Fatura <b>{esc(h.invoice_number)}</b> de {esc(h.date)} · {len(inv.lines)} linhas · "
        f"{units:g} unidades · total {_eur(h.grand_total)}"
    )


# ── applying the group's answers ─────────────────────────────────────────────


class LineEdit(BaseModel):
    index: int = Field(..., description="0-based line index (line #1 is index 0).")
    field: Literal[
        "reference", "name", "summary", "qty", "unit_cost", "pvp_with_vat",
        "moloni_price_no_vat", "subcategory_name", "color", "size", "discount_pct",
    ]
    value: str = Field(..., description="New value, as text (numbers with a dot decimal).")


class HeaderEdit(BaseModel):
    field: Literal[
        "invoice_number", "date", "expiration_date", "subtotal", "tax_total",
        "grand_total", "invoice_discount_pct",
    ]
    value: str


class Corrections(BaseModel):
    line_edits: list[LineEdit] = Field(default_factory=list)
    header_edits: list[HeaderEdit] = Field(default_factory=list)
    remove_lines: list[int] = Field(default_factory=list, description="0-based indices of lines to delete.")
    add_lines: list[ExtractedLine] = Field(default_factory=list)
    rule_changes: list[str] = Field(
        default_factory=list,
        description=(
            "Answers that state a lasting rule for this supplier (not a one-off fix), "
            "rephrased as one short English rule each. Empty if none."
        ),
    )
    note: str = Field("", description="One short sentence in European Portuguese summarising the changes.")


_CORRECT_SYSTEM = """\
You apply a team's answers to an extracted supplier invoice. You get the
invoice PDF, the supplier rulebook, the extraction as JSON (lines numbered from
0), and the questions Hava asked with the team's answers (in Portuguese).

Return the minimal set of edits that makes the extraction agree with the
answers and with the rulebook: line edits, header edits, lines to remove,
lines to add. Recompute dependent values when you change one (e.g. changing
unit_cost changes pvp_with_vat and moloni_price_no_vat per the pricing rule;
merging duplicate references sums qty and removes the extra line). Keep every
reference ≤ 30 characters. Do not change anything the answers do not ask for.
"""


async def apply_answers(
    settings: Settings,
    model: str,
    pdf_bytes: bytes,
    inv: ExtractedInvoice,
    slug: str,
    qa: list[tuple[str, str]],
) -> tuple[ExtractedInvoice, Corrections]:
    rules = load_supplier_rules(slug) or ""
    numbered = inv.model_dump()
    qa_text = "\n\n".join(f"Question:\n{_plain(q)}\nAnswer: {a}" for q, a in qa)
    content = [pdf_block(pdf_bytes), *positioned_text_block(pdf_bytes)]
    content.append(
        {
            "type": "text",
            "text": (
                f"===== SUPPLIER RULEBOOK ({slug}) =====\n{rules}\n\n"
                f"===== EXTRACTION (JSON) =====\n{_json(numbered)}\n\n"
                f"===== QUESTIONS AND ANSWERS =====\n{qa_text}"
            ),
        }
    )
    corr = await ask_json(settings, model, system=_CORRECT_SYSTEM, content=content, output=Corrections)
    return _apply(inv, corr), corr


def _apply(inv: ExtractedInvoice, corr: Corrections) -> ExtractedInvoice:
    lines = [l.model_dump() for l in inv.lines]
    for e in corr.line_edits:
        if 0 <= e.index < len(lines):
            lines[e.index][e.field] = _coerce(ExtractedLine, e.field, e.value)
    header = inv.header.model_dump()
    for e in corr.header_edits:
        header[e.field] = _coerce(ExtractedHeader, e.field, e.value)
    drop = set(corr.remove_lines)
    kept = [l for i, l in enumerate(lines) if i not in drop]
    kept += [l.model_dump() for l in corr.add_lines]
    new_lines = [ExtractedLine.model_validate(l) for l in kept]
    subtotal = round(sum(l.qty * l.unit_cost for l in new_lines), 2)
    rec = inv.reconciliation.model_copy(update={"calculated_subtotal": subtotal})
    return inv.model_copy(
        update={
            "lines": new_lines,
            "header": ExtractedHeader.model_validate(header),
            "reconciliation": rec,
        }
    )


def _coerce(model: type[BaseModel], field_name: str, value: str) -> Any:
    annotation = model.model_fields[field_name].annotation
    if annotation is float:
        return float(value.replace(",", "."))
    return value


def _plain(html_text: str) -> str:
    return re.sub(r"<[^>]+>", "", html_text)


def _json(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False, indent=1)
