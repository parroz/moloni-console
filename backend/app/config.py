from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/.env — same folder as the `app` package (not cwd-dependent).
_BACKEND_DIR = Path(__file__).resolve().parent.parent
_ENV_FILE = _BACKEND_DIR / ".env"


class Settings(BaseSettings):
    """Moloni grant uses OAuth-style query names (client_id, client_secret); the panel labels them DEVELOPER_ID and CLIENT_KEY."""

    model_config = SettingsConfigDict(env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    moloni_developer_id: str = Field(
        validation_alias=AliasChoices("MOLONI_DEVELOPER_ID", "MOLONI_CLIENT_ID"),
        description="Painel Moloni: DEVELOPER_ID (no pedido /grant vai no parâmetro client_id).",
    )
    moloni_client_key: str = Field(
        validation_alias=AliasChoices("MOLONI_CLIENT_KEY", "MOLONI_CLIENT_SECRET"),
        description="Painel Moloni: CLIENT_KEY (no /grant vai em client_secret).",
    )
    moloni_username: str
    moloni_password: str
    moloni_company_id: int
    moloni_default_retail_vat_percent: float = Field(
        23.0,
        validation_alias=AliasChoices("MOLONI_DEFAULT_RETAIL_VAT_PERCENT"),
        description="IVA % para PVP→PV quando o artigo no Moloni não devolve taxa IVA normalizada (saft_type 1).",
    )

    session_secret: str = "change-me-in-production-use-openssl-rand-hex-32"
    # True behind HTTPS (nginx TLS): browser only sends session cookie on secure connections.
    session_cookie_secure: bool = False

    console_password: str

    cors_origins: str = "http://localhost:5173"

    # ── Agent (PDF → supplier invoice) ──
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-5"

    @field_validator("moloni_password", mode="after")
    @classmethod
    def moloni_password_plain(cls, v: str) -> str:
        """If the password was copy-pasted URL-encoded (e.g. %26 for &), decode once. httpx encodes again for /grant."""
        s = v.strip().strip("'\"")
        if "%" not in s:
            return s
        decoded = unquote(s)
        return decoded if decoded != s else s

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
