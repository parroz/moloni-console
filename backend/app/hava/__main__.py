"""Entry point: `python -m app.hava`."""

from __future__ import annotations

import asyncio
import logging
import time

from app.config import get_settings
from app.hava.bot import Bot
from app.hava.config import get_hava_settings
from app.hava.pipeline import Pipeline
from app.hava.store import Store
from app.hava.telegram import Telegram
from app.moloni_client import MoloniClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    force=True,
)
log = logging.getLogger("hava")


async def heartbeat(path) -> None:
    """Touch a file every 30 s; the container healthcheck reads its age."""
    while True:
        path.write_text(str(int(time.time())))
        await asyncio.sleep(30)


async def main() -> None:
    settings = get_settings()
    hs = get_hava_settings()
    hs.data_path.mkdir(parents=True, exist_ok=True)

    if not hs.hava_telegram_token:
        # Don't crash-loop the container: stay up (healthy) and say what is missing.
        log.warning("HAVA_TELEGRAM_TOKEN is not set — Hava is idle. Add it to .env and restart.")
        await heartbeat(hs.data_path / "heartbeat")
        return

    store = Store(hs.data_path / "hava.db")
    tg = Telegram(hs.hava_telegram_token)
    moloni = MoloniClient(
        developer_id=settings.moloni_developer_id,
        client_key=settings.moloni_client_key,
        username=settings.moloni_username,
        password=settings.moloni_password,
        company_id=settings.moloni_company_id,
    )
    pipeline = Pipeline(hs, settings, tg, store, moloni)
    daily = DailyReport(
        tg=tg,
        store=store,
        client=moloni,
        company_id=settings.moloni_company_id,
        chat_id=hs.hava_chat_id,
        at=hs.hava_daily_report_at,
        tz=hs.hava_timezone,
        exclude_refs=hs.sales_exclude_refs,
    )
    bot = Bot(hs, tg, store, pipeline, daily)
    try:
        pipeline.resume_after_restart()
        await asyncio.gather(
            bot.run(), pipeline.worker(), daily.run(), heartbeat(hs.data_path / "heartbeat")
        )
    finally:
        await tg.aclose()
        await moloni.aclose()


if __name__ == "__main__":
    asyncio.run(main())
