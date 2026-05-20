"""System prompt + supplier rules loader for the invoice extractor.

Single-pass model: the agent reads a PDF and returns a JSON object that
conforms to the ExtractedInvoice schema. No tools, no MCP, no multi-turn.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_SUPPLIERS_DIR = Path(__file__).resolve().parent / "suppliers"


# ── Moloni Configuration block parser ─────────────────────────────────────────
# Each supplier .md must contain a `## Moloni Configuration` section with the
# IDs the applier needs. The block is the source of truth — the AI extractor
# is told about it via the system prompt but does NOT mediate these values.

_CONFIG_SECTION_HEADER = "moloni configuration"
_CONFIG_LINE_RE = re.compile(r"^[\s*\-]*([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*(.+?)\s*$")
_TRAILING_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*$")

REQUIRED_CONFIG_KEYS: tuple[str, ...] = (
    "parent_category_id",
    "supplier_id",
    "document_set_id",
    "maturity_date_id",
    "delivery_method_id",
    "unit_id",
    "tax_id",
)

OPTIONAL_CONFIG_DEFAULTS: dict[str, Any] = {
    "tax_value": 23,
    "exemption_reason": "M10",
}

_INT_CONFIG_KEYS = set(REQUIRED_CONFIG_KEYS) | {"tax_value"}


BASE_SYSTEM_PROMPT = """\
## Role

You are a supplier-invoice extractor. Read the PDF the user provides and return
a single JSON object that conforms to the ExtractedInvoice schema below. You
will be given supplier-specific rules in a second system block — apply them
faithfully. You DO NOT call any tools. You DO NOT write to Moloni. Your only
output is the JSON.

## Output format (STRICT)

Return ONLY the JSON object. No markdown fences, no commentary, no code blocks.
The very first character of your reply must be `{` and the last must be `}`.
The JSON must conform to this shape:

```
{
  "supplier": {
    "slug": "<supplier_slug from rules>",
    "name": "<supplier name>",
    "vat": "<VAT number>",
    "moloni_supplier_id": <int from supplier rules>
  },
  "header": {
    "invoice_number": "<as printed>",
    "date": "YYYY-MM-DD",
    "expiration_date": "YYYY-MM-DD" | null,
    "currency": "EUR",
    "subtotal": <number>,
    "tax_total": <number>,
    "grand_total": <number>
  },
  "lines": [
    {
      "reference": "<canonical reference per supplier rules>",
      "name": "<product name>",
      "summary": "<helpful summary, e.g. brand | code | colour | size>",
      "qty": <number, must be >= 1>,
      "unit_cost": <net unit cost, no VAT>,
      "pvp_with_vat": <retail price with VAT per supplier rules>,
      "moloni_price_no_vat": <retail net price per supplier rules>,
      "subcategory_name": "<short canonical subcategory string per supplier rules>",
      "color": "<colour or empty>",
      "size": "<size or empty>"
    }
  ],
  "reconciliation": {
    "calculated_subtotal": <sum of qty * unit_cost across all lines>,
    "matches_invoice_total": <true if calculated_subtotal == header.subtotal within 0.05, else false>,
    "warnings": [ "<short string>", ... ]
  }
}
```

## Quantity Rule (CRITICAL)

A product variant exists on this invoice only if its quantity is at least 1.

- Never include a line whose quantity is 0, empty, blank, or missing.
- For matrix layouts (size × colour grids): only emit lines for filled cells.
- The sum of every variant's qty within a single article must equal the article's printed line total. If they don't match, list the discrepancy in `reconciliation.warnings` but still emit the lines you DID find.

## Reference, Name, Pricing

Apply the rules in the supplier-specific block exactly:
- Reference format (prefix, separators, casing) — follow the rule.
- Pricing — compute `pvp_with_vat` and `moloni_price_no_vat` from the rule's formula.
- Subcategory name — match the rule's mapping table.

## Reconciliation

Compute `calculated_subtotal = sum(qty * unit_cost)` across all emitted lines.
Compare to `header.subtotal`. Tolerate a 0.05 EUR rounding difference.
- If they match → `matches_invoice_total: true`, `warnings: []`.
- If not → `matches_invoice_total: false`, list the diff and the most likely cause in `warnings`.

## Failure modes

If you cannot extract a required field, set it to a reasonable default and add a clear warning to `reconciliation.warnings`. Do not invent values to fill gaps.

If the PDF appears not to be an invoice, return:
```
{ "supplier": {...best guess...}, "header": {...empty...}, "lines": [], "reconciliation": {"calculated_subtotal": 0, "matches_invoke_total": false, "warnings": ["PDF does not look like a supplier invoice"] } }
```

Remember: ONLY the JSON. No explanation. No prose. Just the object.
"""


def list_suppliers() -> list[dict[str, str]]:
    """Available supplier slugs (for the frontend dropdown — currently single-supplier v1)."""
    items: list[dict[str, str]] = []
    if not _SUPPLIERS_DIR.is_dir():
        return items
    for path in sorted(_SUPPLIERS_DIR.glob("*.md")):
        slug = path.stem
        label = slug.replace("_", " ").replace("-", " ").title()
        items.append({"slug": slug, "label": label})
    return items


def load_supplier_rules(slug: str) -> str | None:
    """Read a supplier .md by slug. Returns None for unknown slug."""
    if not slug:
        return None
    safe = slug.replace("/", "").replace("..", "")
    path = _SUPPLIERS_DIR / f"{safe}.md"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


_SLUG_RE = __import__("re").compile(r"^[a-z0-9_-]+$")


def _validate_slug(slug: str) -> str:
    """Return sanitised slug or raise ValueError."""
    if not slug or not _SLUG_RE.match(slug):
        raise ValueError(f"Invalid slug {slug!r}: only lowercase letters, digits, _ and - allowed")
    return slug


def save_supplier_rules(slug: str, content: str) -> None:
    """Write content to {slug}.md, creating the file if necessary."""
    _validate_slug(slug)
    _SUPPLIERS_DIR.mkdir(parents=True, exist_ok=True)
    (_SUPPLIERS_DIR / f"{slug}.md").write_text(content, encoding="utf-8")


def delete_supplier(slug: str) -> bool:
    """Delete {slug}.md. Returns True if deleted, False if it didn't exist."""
    _validate_slug(slug)
    path = _SUPPLIERS_DIR / f"{slug}.md"
    if path.is_file():
        path.unlink()
        return True
    return False


def parse_supplier_config(slug: str) -> dict[str, Any]:
    """Parse `## Moloni Configuration` block from the supplier .md.

    Returns the merged dict (parsed values + defaults for known optional keys).
    Raises FileNotFoundError if the supplier .md doesn't exist. Missing or
    malformed keys are NOT raised here — call validate_supplier_config to
    surface them with a user-friendly message.
    """
    text = load_supplier_rules(slug)
    if text is None:
        raise FileNotFoundError(f"No supplier rules for slug {slug!r}")

    in_section = False
    raw: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("##"):
            heading = stripped.lstrip("#").strip().lower()
            in_section = heading == _CONFIG_SECTION_HEADER
            continue
        if not in_section or not stripped:
            continue
        m = _CONFIG_LINE_RE.match(stripped)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        # Strip trailing parenthetical comments like "1506793 (60 Dias)"
        value = _TRAILING_PAREN_RE.sub("", value).strip()
        raw[key] = value

    config: dict[str, Any] = dict(OPTIONAL_CONFIG_DEFAULTS)
    for key, value in raw.items():
        if key in _INT_CONFIG_KEYS:
            try:
                config[key] = int(value)
            except ValueError:
                config[key] = value  # keep raw so validator can flag it
        else:
            config[key] = value
    return config


def validate_supplier_config(config: dict[str, Any]) -> list[str]:
    """Return a list of required keys that are missing or invalid (empty = OK)."""
    bad: list[str] = []
    for key in REQUIRED_CONFIG_KEYS:
        v = config.get(key)
        if v is None or v == "":
            bad.append(key)
            continue
        if key in _INT_CONFIG_KEYS:
            if not isinstance(v, int) or v <= 0:
                bad.append(key)
    return bad


def build_system_blocks(supplier_slug: str) -> list[dict]:
    """Anthropic system as a list so each section can be cached independently.
    Cache_control on the base prompt + supplier rules — both are stable per supplier."""
    blocks: list[dict] = [
        {
            "type": "text",
            "text": BASE_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    rules = load_supplier_rules(supplier_slug)
    if rules:
        blocks.append(
            {
                "type": "text",
                "text": f"# Supplier-specific rules ({supplier_slug})\n\n{rules}",
                "cache_control": {"type": "ephemeral"},
            }
        )
    return blocks
