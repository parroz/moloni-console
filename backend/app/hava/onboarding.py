"""New supplier: read the invoice, work out what can be worked out, ask the group
the rest, then write a rulebook in the same format as the existing ones.

Where each piece of the `## Moloni Configuration` block comes from:
  * document_set_id, delivery_method_id, unit_id, tax_id, tax_value —
    the value every existing rulebook already uses.
  * supplier_id — Moloni lookup by VAT, then by name; the group confirms.
  * parent_category_id — the group picks an existing top-level category or
    says to create one for the brand.
  * exemption_reason — from the supplier's country, confirmed by the group.
  * maturity_date_id — from the payment terms on the invoice; asked if unclear.
Pricing is a business rule, so it is always asked.
"""

from __future__ import annotations

import difflib
import logging
import re
from collections import Counter
from typing import Any

from pydantic import BaseModel, Field

from app.agent.prompts import (
    list_suppliers,
    load_supplier_rules,
    parse_supplier_config,
    save_supplier_rules,
    validate_supplier_config,
)
from app.config import Settings
from app.hava.detect import DRAFT_MARKER, Identification, normalize_vat, vat_core
from app.hava.llm import ask_json, ask_text, pdf_block, positioned_text_block
from app.moloni_categories import fetch_categories_level
from app.moloni_client import MoloniAPIError, MoloniClient

log = logging.getLogger("hava.onboarding")

EU_COUNTRIES = {
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "EL", "HR",
    "HU", "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "RO", "SE", "SI", "SK",
}

_SHARED_KEYS = ("document_set_id", "delivery_method_id", "unit_id", "tax_id", "tax_value")


# ── facts from Moloni and the existing rulebooks ─────────────────────────────


def shared_defaults() -> dict[str, Any]:
    """Most common value of each shared config key across existing rulebooks."""
    counters: dict[str, Counter] = {k: Counter() for k in _SHARED_KEYS}
    for item in list_suppliers():
        try:
            cfg = parse_supplier_config(item["slug"])
        except FileNotFoundError:
            continue
        for k in _SHARED_KEYS:
            v = cfg.get(k)
            if isinstance(v, int) and v > 0:
                counters[k][v] += 1
    return {k: c.most_common(1)[0][0] for k, c in counters.items() if c}


def pricing_examples() -> list[str]:
    """Distinct 'custo × N' multipliers used by existing rulebooks."""
    seen: list[str] = []
    for item in list_suppliers():
        text = load_supplier_rules(item["slug"]) or ""
        for m in re.finditer(r"unit_cost\s*[×x*]\s*([0-9]+(?:[.,][0-9]+)?)", text):
            mult = m.group(1).replace(",", ".")
            if mult not in seen:
                seen.append(mult)
    return seen[:4]


async def find_moloni_suppliers(
    client: MoloniClient, company_id: int, vat: str, name: str
) -> list[dict[str, Any]]:
    found: dict[int, dict[str, Any]] = {}
    variants = [v for v in dict.fromkeys([normalize_vat(vat), vat_core(vat)]) if v]
    for v in variants:
        try:
            res = await client.post("suppliers/getByVat", {"company_id": company_id, "vat": v})
        except MoloniAPIError as e:
            log.warning("suppliers/getByVat(%s) failed: %s", v, e)
            continue
        for s in res or []:
            if isinstance(s, dict) and s.get("supplier_id"):
                found[int(s["supplier_id"])] = s
    if not found and name:
        # Search by the first distinctive word of the name.
        word = max(re.findall(r"[A-Za-zÀ-ÿ0-9]{3,}", name) or [name], key=len)
        try:
            res = await client.post("suppliers/getBySearch", {"company_id": company_id, "search": word})
        except MoloniAPIError as e:
            log.warning("suppliers/getBySearch(%s) failed: %s", word, e)
            res = []
        for s in res or []:
            if isinstance(s, dict) and s.get("supplier_id"):
                found[int(s["supplier_id"])] = s
    return [
        {"supplier_id": sid, "name": str(s.get("name", "")), "vat": str(s.get("vat", ""))}
        for sid, s in found.items()
    ][:5]


async def maturity_dates(client: MoloniClient, company_id: int) -> list[dict[str, Any]]:
    res = await client.post("maturityDates/getAll", {"company_id": company_id})
    return [
        {"id": int(m["maturity_date_id"]), "name": str(m.get("name", "")), "days": int(m.get("days") or 0)}
        for m in res or []
        if isinstance(m, dict) and m.get("maturity_date_id")
    ]


async def top_categories(client: MoloniClient, company_id: int) -> list[dict[str, Any]]:
    res = await fetch_categories_level(client, company_id, 0)
    return [
        {"id": int(c["category_id"]), "name": str(c.get("name", ""))}
        for c in res or []
        if isinstance(c, dict) and c.get("category_id")
    ]


def similar_categories(cats: list[dict[str, Any]], *names: str, n: int = 3) -> list[dict[str, Any]]:
    by_upper = {c["name"].upper(): c for c in cats}
    picked: list[dict[str, Any]] = []
    for name in names:
        if not name:
            continue
        for match in difflib.get_close_matches(name.upper(), list(by_upper), n=n, cutoff=0.5):
            if by_upper[match] not in picked:
                picked.append(by_upper[match])
    return picked[:n]


# ── the model's reading of the new supplier ──────────────────────────────────


class OpenQuestion(BaseModel):
    key: str = Field(..., description="Short snake_case id, e.g. 'size_column' or 'naming'.")
    question: str = Field(..., description="The question, in European Portuguese, quoting the invoice where useful.")
    options: list[str] = Field(default_factory=list, description="0-4 short likely answers, in Portuguese.")


class OnboardingPlan(BaseModel):
    slug: str = Field(..., description="Rulebook slug: lowercase brand name, words joined by _, e.g. 'maison_kitsune'.")
    brand: str = Field(..., description="Brand name as it should appear in product names and the category, e.g. 'MAISON KITSUNE'.")
    payment_days: int | None = Field(None, description="Payment term in days printed on the invoice, if any.")
    conventions: str = Field(
        ...,
        description=(
            "European Portuguese, max ~8 short lines: the reference format, product naming "
            "pattern and subcategory mapping you propose, each with one example from this invoice."
        ),
    )
    questions: list[OpenQuestion] = Field(
        default_factory=list,
        description="Only real ambiguities in reading this layout. Max 4. Empty if none.",
    )


_PLAN_SYSTEM = """\
You are preparing a parsing rulebook for a NEW supplier of a Portuguese clothing
retailer (the buyer, MACHADO & HIGINO LDA, NIF 506241173). Later, an extractor
will read every invoice from this supplier using that rulebook and produce one
line per product variant (reference, name, colour, size, qty, unit cost...).

You get the invoice, its positioned text layer, and existing rulebooks from
other suppliers as examples of the conventions the retailer uses. Follow those
conventions (brand-prefixed references, size as the last reference segment,
upper-case names, short subcategory names such as T-SHIRT, CALCAS, VESTIDO...).

Return:
- `slug`, `brand`, `payment_days`.
- `conventions`: what you propose for reference format, product naming and
  subcategory mapping, each with an example taken from THIS invoice.
- `questions`: only genuine ambiguities in reading this layout that the team
  must settle (e.g. which column is the quantity, what an unlabelled code means,
  whether a line is a product or a fee). Do NOT ask about pricing, VAT,
  payment terms, the Moloni category or the Moloni supplier — those are asked
  separately. Max 4, in European Portuguese, each with a few likely answers.
"""


def _example_rulebooks(max_books: int = 2) -> str:
    """Two existing rulebooks as format examples: prefer one VAT-exempt and one domestic."""
    exempt, domestic = None, None
    for item in list_suppliers():
        text = load_supplier_rules(item["slug"]) or ""
        if not text or DRAFT_MARKER in text:
            continue
        try:
            cfg = parse_supplier_config(item["slug"])
        except FileNotFoundError:
            continue
        if cfg.get("exemption_reason") and exempt is None:
            exempt = (item["slug"], text)
        elif not cfg.get("exemption_reason") and domestic is None:
            domestic = (item["slug"], text)
    books = [b for b in (exempt, domestic) if b][:max_books]
    return "\n\n".join(f"===== EXAMPLE RULEBOOK: {slug}.md =====\n{text}" for slug, text in books)


async def plan_onboarding(
    settings: Settings, model: str, pdf_bytes: bytes, ident: Identification
) -> OnboardingPlan:
    content = [pdf_block(pdf_bytes), *positioned_text_block(pdf_bytes)]
    content.append(
        {
            "type": "text",
            "text": (
                f"{_example_rulebooks()}\n\n"
                f"New supplier read from the invoice: {ident.supplier_name} "
                f"(VAT {ident.supplier_vat or '?'}, country {ident.supplier_country or '?'}, "
                f"brand {ident.brand or '?'}).\n\nPrepare the onboarding plan."
            ),
        }
    )
    plan = await ask_json(settings, model, system=_PLAN_SYSTEM, content=content, output=OnboardingPlan)
    plan.slug = _safe_slug(plan.slug or ident.brand or ident.supplier_name)
    return plan


def _safe_slug(raw: str) -> str:
    import unicodedata

    s = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_") or "fornecedor"
    existing = {i["slug"] for i in list_suppliers()}
    base, n = s, 2
    while s in existing:
        s = f"{base}_{n}"
        n += 1
    return s


# ── writing the rulebook ─────────────────────────────────────────────────────


def render_config_block(cfg: dict[str, Any], notes: dict[str, str]) -> str:
    def line(key: str) -> str:
        value = cfg.get(key, "")
        note = notes.get(key)
        return f"* {key}: {value}" + (f"  ({note})" if note else "")

    keys = [
        "parent_category_id",
        "supplier_id",
        "document_set_id",
        "maturity_date_id",
        "delivery_method_id",
        "unit_id",
        "tax_id",
        "tax_value",
        "exemption_reason",
    ]
    return (
        "## Moloni Configuration\n\n"
        "This block is parsed by the applier. Keys must be valid Moloni IDs (positive\n"
        "integers, except `exemption_reason`). Trailing parenthetical comments are\n"
        "ignored. The applier refuses to run while any required key is missing or\n"
        "non-numeric. Written by Hava from the first invoice and the team's answers.\n\n"
        + "\n".join(line(k) for k in keys)
        + "\n"
    )


def _replace_config_section(markdown: str, block: str) -> str:
    lines = markdown.strip().splitlines()
    start = next(
        (i for i, l in enumerate(lines) if l.strip().lower().lstrip("#").strip() == "moloni configuration"),
        None,
    )
    if start is not None:
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        lines[start:end] = block.rstrip().splitlines() + [""]
    else:
        title = next((i for i, l in enumerate(lines) if l.startswith("# ")), -1)
        lines[title + 1:title + 1] = [""] + block.rstrip().splitlines() + [""]
    return "\n".join(lines).strip() + "\n"


def _ensure_vat_line(markdown: str, vat: str, name: str) -> str:
    if not vat or normalize_vat(vat) in normalize_vat(markdown):
        return markdown
    lines = markdown.splitlines()
    sup = next((i for i, l in enumerate(lines) if l.strip().lower() in ("## supplier", "## fornecedor")), None)
    if sup is not None:
        lines.insert(sup + 1, f"\n- name: {name}\n- VAT: {vat}")
        return "\n".join(lines) + "\n"
    return markdown.rstrip() + f"\n\n## Supplier\n\n- name: {name}\n- VAT: {vat}\n"


_WRITE_SYSTEM = """\
You write the parsing rulebook (Markdown) for a NEW supplier of a Portuguese
clothing retailer. An extractor model will later read every invoice from this
supplier with your rulebook as its only supplier-specific guidance, so it must
be precise, explicit and self-contained.

Follow the structure and tone of the example rulebooks exactly: a `# <BRAND>
Invoice Rules` title, then sections such as Supplier, Brand, Invoice Layout
Rule, Size Column Alignment, Product Reference Rule, Product Naming Rule,
Pricing Rule (with the exact formulas for `pvp_with_vat` and
`moloni_price_no_vat`, rounding included, and a worked example), Category
Rules (subcategory mapping table), Invoice Identification, Supplier Invoice VAT
Rule, Invoice-Level Discount Rule, Quantity Rule, Important Parsing Rules,
Example Expansion (using real lines from this invoice), Invoice-Level Checks.

Include a `## Moloni Configuration` section copied VERBATIM from the block you
are given. Every rule must agree with the team's answers; where the team
corrected a proposal, the correction wins. References must stay ≤ 30
characters. Write the rulebook in English, like the examples. Output only the
Markdown document.
"""


async def write_rulebook(
    settings: Settings,
    model: str,
    pdf_bytes: bytes,
    *,
    ident: Identification,
    plan: OnboardingPlan,
    config_block: str,
    answers: list[tuple[str, str]],
) -> str:
    qa = "\n".join(f"- Q: {q}\n  A: {a}" for q, a in answers) or "(none)"
    content = [pdf_block(pdf_bytes), *positioned_text_block(pdf_bytes)]
    content.append(
        {
            "type": "text",
            "text": (
                f"{_example_rulebooks()}\n\n"
                f"===== NEW SUPPLIER =====\n"
                f"Issuer: {ident.supplier_name}\nVAT: {ident.supplier_vat}\n"
                f"Country: {ident.supplier_country}\nBrand: {plan.brand}\nSlug: {plan.slug}\n\n"
                f"Proposed conventions:\n{plan.conventions}\n\n"
                f"Team's answers (authoritative):\n{qa}\n\n"
                f"Moloni Configuration block to copy verbatim:\n{config_block}\n\n"
                f"Write the rulebook for {plan.brand}."
            ),
        }
    )
    return await ask_text(settings, model, system=_WRITE_SYSTEM, content=content)


def finalize_and_save(
    slug: str, markdown: str, *, config_block: str, vat: str, name: str
) -> list[str]:
    """Pin our config block, make sure the VAT is listed, mark as draft, save.
    Returns missing/invalid config keys (empty = OK)."""
    text = markdown.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n|\n```$", "", text).strip()
    text = _replace_config_section(text, config_block)
    text = _ensure_vat_line(text, vat, name)
    if DRAFT_MARKER not in text:
        text = f"{DRAFT_MARKER}\n{text}"
    save_supplier_rules(slug, text)
    return validate_supplier_config(parse_supplier_config(slug))


def approve_rulebook(slug: str) -> bool:
    text = load_supplier_rules(slug)
    if text is None or DRAFT_MARKER not in text:
        return False
    save_supplier_rules(slug, text.replace(f"{DRAFT_MARKER}\n", "").replace(DRAFT_MARKER, ""))
    return True


def is_draft(slug: str) -> bool:
    return DRAFT_MARKER in (load_supplier_rules(slug) or "")
