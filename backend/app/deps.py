from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from app.config import Settings, get_settings
from app.moloni_client import MoloniClient


def get_moloni(request: Request) -> MoloniClient:
    client = getattr(request.app.state, "moloni", None)
    if not client:
        raise HTTPException(503, "Moloni client not initialized")
    return client


def require_auth(request: Request) -> None:
    if not request.session.get("auth"):
        raise HTTPException(401, "Not authenticated")


MoloniDep = Annotated[MoloniClient, Depends(get_moloni)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
