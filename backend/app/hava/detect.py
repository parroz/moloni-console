"""Work out which supplier an invoice PDF comes from.

1. Match VAT numbers: every rulebook lists its supplier's VAT; look for it in
   the PDF's text layer (tolerating spaces/dots and a missing country prefix).
2. Several rulebooks share one biller (André Costa bills Essentiel Antwerp and
   Osklen) — then let the model pick among those candidates by brand/layout.
3. No VAT match (new supplier, or a scan with no text layer) — the model
   compares the invoice with every known supplier and either names one or says
   it is a new supplier, returning what it can read about it.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from app.agent.prompts import list_suppliers, load_supplier_rules
from app.config import Settings
from app.hava.llm import ask_json, pdf_block

log = logging.getLogger("hava.detect")

DRAFT_MARKER = "<!-- hava:rascunho -->"

_VAT_LINE_RE = re.compile(r"^\s*[-*]?\s*(?:VAT|NIF|CIF|TVA)\s*:\s*`?([A-Za-z0-9 .\-]+)", re.MULTILINE)


def normalize_vat(vat: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", vat.upper())


def vat_core(vat: str) -> str:
    """VAT without its two-letter country prefix (ESB20361309 → B20361309)."""
    v = normalize_vat(vat)
    if len(v) > 8 and v[:2].isalpha():
        return v[2:]
    return v


def vat_in_text(vat: str, text: str) -> bool:
    core = vat_core(vat)
    if len(core) < 7:
        return False
    # Allow separators between characters, but not other letters/digits around it.
    pattern = r"(?<![A-Z0-9])" + r"[\s.\-]?".join(re.escape(c) for c in core) + r"(?![A-Z0-9])"
    return re.search(pattern, text.upper()) is not None


@dataclass
class KnownSupplier:
    slug: str
    title: str
    vat: str
    is_draft: bool
    summary: str  # opening of the rulebook, for the model to compare against


def known_suppliers() -> list[KnownSupplier]:
    out: list[KnownSupplier] = []
    for item in list_suppliers():
        text = load_supplier_rules(item["slug"]) or ""
        m = _VAT_LINE_RE.search(text)
        title = next((l.lstrip("# ").strip() for l in text.splitlines() if l.startswith("# ")), item["label"])
        title = re.sub(r"\s*(supplier\s+)?invoice\s+rules\s*$", "", title, flags=re.IGNORECASE) or item["label"]
        out.append(
            KnownSupplier(
                slug=item["slug"],
                title=title,
                vat=normalize_vat(m.group(1)) if m else "",
                is_draft=DRAFT_MARKER in text,
                summary=_summary(text),
            )
        )
    return out


def _summary(rules: str, limit: int = 1800) -> str:
    """Title + Supplier/Brand/Invoice Identification sections, trimmed."""
    keep = ("supplier", "brand", "invoice identification", "fornecedor", "marca")
    lines = rules.splitlines()
    out: list[str] = []
    take = False
    for line in lines:
        if line.startswith("# "):
            out.append(line)
            continue
        if line.startswith("## "):
            take = line[3:].strip().lower().startswith(keep)
        if take:
            out.append(line)
    text = "\n".join(out).strip() or rules[:limit]
    return text[:limit]


def pdf_plain_text(pdf_bytes: bytes) -> str:
    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            return "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception as e:  # noqa: BLE001 — a broken text layer just means "no text"
        log.warning("pdf_plain_text failed: %s", e)
        return ""


class Identification(BaseModel):
    """What the model can tell about the invoice's supplier."""

    slug: str | None = Field(
        None, description="Slug of the matching known supplier, or null if it is a new supplier."
    )
    confidence: Literal["high", "low"]
    supplier_name: str = Field(..., description="Legal name of the company that issued the invoice.")
    supplier_vat: str = Field("", description="Issuer VAT number with country prefix, e.g. ESB20361309.")
    supplier_country: str = Field("", description="ISO 3166-1 alpha-2 country of the issuer, e.g. ES.")
    brand: str = Field("", description="Brand of the goods, if different from the issuer name.")
    is_invoice: bool = Field(True, description="False if the PDF is not a supplier invoice at all.")
    reason: str = Field("", description="One short sentence, in Portuguese, explaining the decision.")


@dataclass
class Detection:
    slug: str | None              # decided supplier, or None
    candidates: list[str]         # slugs to offer when undecided
    ident: Identification | None  # model's reading (always set unless one VAT match decided it)


_IDENTIFY_SYSTEM = """\
You identify which supplier issued a supplier invoice for a Portuguese clothing
retailer. The retailer itself is MACHADO & HIGINO LDA (NIF 506241173): it is the
BUYER and appears on every invoice — it is never the supplier.

You get the invoice PDF and a list of known suppliers (slug + the opening of
each supplier's rulebook). One legal issuer can bill several brands, each with
its own rulebook; tell them apart by brand names, product codes and layout.

Return `slug` only when the invoice clearly belongs to that rulebook's brand;
`confidence: high` when you are sure, `low` when it is a guess. Return
`slug: null` for a supplier/brand with no rulebook. Always fill the issuer's
name, VAT, country and brand from the invoice. `reason` in European Portuguese.
"""


async def identify(
    settings: Settings, model: str, pdf_bytes: bytes, suppliers: list[KnownSupplier]
) -> Identification:
    listing = "\n\n".join(
        f"### slug: {s.slug}\nVAT: {s.vat or '(none listed)'}\n{s.summary}" for s in suppliers
    ) or "(no known suppliers yet)"
    return await ask_json(
        settings,
        model,
        system=_IDENTIFY_SYSTEM,
        content=[
            pdf_block(pdf_bytes),
            {"type": "text", "text": f"Known suppliers:\n\n{listing}\n\nWho issued this invoice?"},
        ],
        output=Identification,
        max_tokens=4000,
    )


async def detect_supplier(settings: Settings, model: str, pdf_bytes: bytes) -> Detection:
    suppliers = known_suppliers()
    text = pdf_plain_text(pdf_bytes)
    vat_hits = [s for s in suppliers if s.vat and vat_in_text(s.vat, text)]
    log.info("detect: text=%d chars, vat matches=%s", len(text), [s.slug for s in vat_hits])

    if len(vat_hits) == 1:
        return Detection(slug=vat_hits[0].slug, candidates=[vat_hits[0].slug], ident=None)

    pool = vat_hits or suppliers
    ident = await identify(settings, model, pdf_bytes, pool)
    slugs = {s.slug for s in pool}
    chosen = ident.slug if ident.slug in slugs else None
    log.info("detect: model says slug=%s confidence=%s (%s)", ident.slug, ident.confidence, ident.reason)

    if vat_hits:
        # Same biller, several brands: trust a confident pick, otherwise ask among them.
        if chosen and ident.confidence == "high":
            return Detection(slug=chosen, candidates=[s.slug for s in vat_hits], ident=ident)
        return Detection(slug=None, candidates=[s.slug for s in vat_hits], ident=ident)

    if chosen and ident.confidence == "high":
        return Detection(slug=chosen, candidates=[chosen], ident=ident)
    # Unsure, or looks new: let the group decide (known suppliers + "new").
    candidates = [chosen] if chosen else []
    return Detection(slug=None, candidates=candidates, ident=ident)
