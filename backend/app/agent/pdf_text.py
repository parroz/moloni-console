"""Positioned text extraction from invoice PDFs.

Why this exists
---------------
Supplier invoices encode the most important field — *which size was ordered* —
purely as horizontal position: a bare `1` sitting under the `XS` column header.
Asking a vision model to recover that from a rendered page is asking it to do
sub-pixel column alignment, and it gets it wrong. Observed failure: quantities
belonging to `XS`/`S` were read as `S`/`M`, silently producing the wrong
variants.

So we extract the PDF's own text layer with coordinates and hand it to the
model alongside the image. Column alignment stops being a perception problem
and becomes exact numeric matching: the header reads `XS@288.0`, the cell reads
`1@288.0`, so the cell is an XS.

This is supplier-agnostic — no table detection, no per-brand heuristics. We
just report what is on the page and where.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("agent.pdf_text")

# Words closer than this vertically are treated as the same visual row.
_ROW_TOLERANCE = 3.0

# Guard rails: a pathological PDF shouldn't blow up the prompt.
_MAX_PAGES = 20
_MAX_WORDS_PER_PAGE = 1200


class PdfTextUnavailable(Exception):
    """Raised when the text layer can't be read (scanned/image-only PDF)."""


def _group_into_rows(words: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Cluster words into visual rows by their vertical position."""
    rows: list[list[dict[str, Any]]] = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if rows and abs(rows[-1][0]["top"] - w["top"]) <= _ROW_TOLERANCE:
            rows[-1].append(w)
        else:
            rows.append([w])
    for row in rows:
        row.sort(key=lambda w: w["x0"])
    return rows


def extract_positioned_text(pdf_bytes: bytes) -> str:
    """Return the PDF text layer as `word@x` tuples, one line per visual row.

    Raises PdfTextUnavailable if the PDF has no usable text layer, so the
    caller can fall back to image-only extraction rather than failing hard.
    """
    try:
        import pdfplumber
    except ImportError as e:  # pragma: no cover - dependency is declared
        raise PdfTextUnavailable(f"pdfplumber not installed: {e}") from e

    import io

    out: list[str] = []
    total_words = 0

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page_no, page in enumerate(pdf.pages[:_MAX_PAGES], start=1):
                words = page.extract_words() or []
                total_words += len(words)
                if len(words) > _MAX_WORDS_PER_PAGE:
                    words = words[:_MAX_WORDS_PER_PAGE]
                    log.warning(
                        "pdf_text: page %d truncated to %d words",
                        page_no,
                        _MAX_WORDS_PER_PAGE,
                    )

                out.append(f"--- page {page_no} ---")
                for row in _group_into_rows(words):
                    cells = "  ".join(f"{w['text']}@{w['x0']:.1f}" for w in row)
                    out.append(f"y={row[0]['top']:.0f} | {cells}")
    except PdfTextUnavailable:
        raise
    except Exception as e:  # noqa: BLE001 - any parse failure is a soft failure
        raise PdfTextUnavailable(f"{type(e).__name__}: {e}") from e

    if total_words == 0:
        raise PdfTextUnavailable("PDF has no text layer (probably a scan)")

    log.info("pdf_text: extracted %d words across %d page(s)", total_words, page_no)
    return "\n".join(out)
