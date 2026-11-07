from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"


class HavaSettings(BaseSettings):
    """Hava-only settings. Moloni and Anthropic credentials come from app.config.Settings."""

    model_config = SettingsConfigDict(env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    hava_telegram_token: str = ""
    # The one group Hava listens to. 0 = setup mode: Hava only answers /chatid.
    hava_chat_id: int = 0
    # Model for Hava's own reasoning (supplier detection, onboarding, corrections).
    # Invoice extraction keeps using ANTHROPIC_MODEL from the console settings.
    hava_model: str = "claude-sonnet-5-5"
    hava_data_dir: str = "/app/hava-data"
    console_base_url: str = "https://console.gluglu.app"
    # Daily sales report posted to the group (local time, HH:MM).
    hava_daily_report_at: str = "19:30"
    hava_timezone: str = "Europe/Lisbon"
    # Comma-separated reference prefixes not counted as items sold (till bags).
    hava_sales_exclude_refs: str = "GC-SACO"

    @property
    def sales_exclude_refs(self) -> tuple[str, ...]:
        return tuple(x.strip() for x in self.hava_sales_exclude_refs.split(",") if x.strip())

    @property
    def data_path(self) -> Path:
        return Path(self.hava_data_dir)


@lru_cache
def get_hava_settings() -> HavaSettings:
    return HavaSettings()
