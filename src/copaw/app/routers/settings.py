# -*- coding: utf-8 -*-
from fastapi import APIRouter, Body, HTTPException

from ...settings import get_language, set_language

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get(
    "/language",
    summary="Get language setting",
    description="Get global language preference",
)
async def get_settings_language() -> dict:
    return {"language": get_language()}


@router.put(
    "/language",
    summary="Update language setting",
    description="Update global language preference",
)
async def put_settings_language(
    body: dict = Body(..., description='Body with "language" key'),
) -> dict:
    language = (body.get("language") or "").strip().lower()
    if not language:
        raise HTTPException(status_code=400, detail="language is required")
    try:
        updated = set_language(language)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"language": updated}
