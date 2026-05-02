from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from app.agent_routes import router as agent_router
from app.config import get_settings
from app.moloni_client import MoloniAPIError, MoloniClient
from app.routes import router

# Surface agent.* logs at INFO so `docker compose logs api` shows progress.
logging.getLogger("agent").setLevel(logging.INFO)
logging.getLogger("agent.runner").setLevel(logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    client = MoloniClient(
        developer_id=settings.moloni_developer_id,
        client_key=settings.moloni_client_key,
        username=settings.moloni_username,
        password=settings.moloni_password,
        company_id=settings.moloni_company_id,
    )
    app.state.moloni = client
    yield
    await client.aclose()


app = FastAPI(title="Moloni console", lifespan=lifespan)
settings = get_settings()

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    session_cookie="moloni_console",
    same_site="lax",
    https_only=settings.session_cookie_secure,
    max_age=86400 * 14,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
app.include_router(agent_router)


@app.exception_handler(MoloniAPIError)
async def moloni_api_error_handler(_: Any, exc: MoloniAPIError) -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content={"detail": str(exc), "moloni_body": exc.body},
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
