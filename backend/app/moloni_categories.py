"""Moloni productCategories/getAll — one parent_id per request (max 50 rows per page)."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, TYPE_CHECKING

from app.moloni_client import MoloniAPIError

if TYPE_CHECKING:
    from app.moloni_client import MoloniClient


async def fetch_categories_level(client: MoloniClient, company_id: int, parent_id: int) -> list[dict[str, Any]]:
    """All categories whose direct parent is ``parent_id`` (use ``0`` for roots)."""
    return await client.post_all_pages(
        "productCategories/getAll",
        {"company_id": company_id, "parent_id": parent_id},
    )


def normalize_category_list(items: list[Any]) -> list[dict[str, Any]]:
    """Moloni rows as {category_id, parent_id, name} with ints (API may return strings)."""
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            cid = item.get("category_id")
            if cid is None:
                continue
            out.append(
                {
                    "category_id": int(cid),
                    "parent_id": int(item.get("parent_id", 0) or 0),
                    "name": str(item.get("name", "")),
                }
            )
        except (TypeError, ValueError):
            continue
    return out


async def fetch_all_categories_parallel(
    client: MoloniClient,
    company_id: int,
    *,
    concurrency: int = 6,
    max_categories: int = 8000,
    max_waves: int = 500,
) -> list[dict[str, Any]]:
    """
    Full tree: BFS in waves, many ``getAll`` calls in parallel per wave.
    Used for dropdowns (products / invoice). Avoids one giant sequential chain that times out the browser.
    ``max_waves`` caps Moloni misbehaviour (non-empty pages forever) so the server cannot hang indefinitely.
    """
    out: list[dict[str, Any]] = []
    seen_cat: set[int] = set()
    fetched_parent: set[int] = set()
    queue: deque[int] = deque([0])
    waves = 0

    while queue and len(seen_cat) < max_categories and waves < max_waves:
        waves += 1
        batch: list[int] = []
        while queue and len(batch) < concurrency:
            p = queue.popleft()
            if p in fetched_parent:
                continue
            fetched_parent.add(p)
            batch.append(p)
        if not batch:
            break
        chunks = await asyncio.gather(*[fetch_categories_level(client, company_id, pid) for pid in batch])
        for cats in chunks:
            if not isinstance(cats, list):
                raise MoloniAPIError(
                    "Moloni productCategories/getAll: resposta inválida (esperada lista).",
                    body=cats,
                )
            for c in cats:
                if len(seen_cat) >= max_categories:
                    break
                cid = int(c["category_id"])
                if cid not in seen_cat:
                    seen_cat.add(cid)
                    out.append(c)
                if cid not in fetched_parent:
                    queue.append(cid)
    return out
