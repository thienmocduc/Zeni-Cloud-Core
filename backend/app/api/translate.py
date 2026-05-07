"""
Zeni Cloud Core — L3 Translation API (Stream A3).

Endpoints:
  POST /translate?ws=  — Cloud Translation v3, target_lang ISO-639 (vi/en/ja/zh-CN/ko/fr/...)

Pricing: $20 / 1M chars
Audit:   ai.translate
Billing: layer="L3", action="ai.translate"
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services import translate as translate_svc
from app.services.audit import audit_push, billing_push

log = logging.getLogger("zeni.api.translate")
router = APIRouter(prefix="/translate", tags=["ai", "translate"])

# Cloud Translation v3 standard pricing: $20 / 1M chars
COST_PER_CHAR_USD = 20.0 / 1_000_000  # = 0.00002


class TranslateIn(BaseModel):
    text: str = Field(min_length=1, max_length=30_000)
    target_lang: str = Field(min_length=2, max_length=10)
    source_lang: str | None = Field(default=None, min_length=2, max_length=10)


def _check_scope(me: CurrentUser) -> None:
    """PAT phải có scope 'ai' hoặc 'full'."""
    if me.auth_scope and not any(s in me.auth_scope for s in ("ai", "full")):
        raise HTTPException(status_code=403, detail="Token thiếu scope 'ai'")


@router.post("")
async def translate_text(
    ws: str,
    data: TranslateIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Translate text qua Cloud Translation v3 (project endpoint = SA auth)."""
    await require_workspace_access(ws, me)
    _check_scope(me)

    # Pre-validate ngôn ngữ trước khi gọi service (để trả 400 thay vì 502)
    if not translate_svc._validate_lang_code(data.target_lang):
        raise HTTPException(status_code=400, detail="Ngôn ngữ đích không hợp lệ")
    if data.source_lang and not translate_svc._validate_lang_code(data.source_lang):
        raise HTTPException(status_code=400, detail="Ngôn ngữ nguồn không hợp lệ")

    try:
        result = await translate_svc.translate_text(
            text=data.text,
            target_lang=data.target_lang,
            source_lang=data.source_lang,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        log.warning("translate runtime: %s", e)
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        log.exception("translate unexpected")
        raise HTTPException(status_code=502, detail="Không thể dịch văn bản")

    char_count = result.get("char_count", len(data.text))
    cost = char_count * COST_PER_CHAR_USD

    await audit_push(
        db,
        actor=me.email,
        workspace_id=ws,
        action="ai.translate",
        target=f"{data.source_lang or 'auto'}->{data.target_lang}"[:80],
        severity="ok",
        metadata={
            "char_count": char_count,
            "target_lang": data.target_lang,
            "source_lang": data.source_lang,
            "source_lang_detected": result.get("source_lang_detected"),
        },
    )
    await billing_push(db, workspace_id=ws, layer="L3", action="ai.translate", cost_usd=cost)
    await db.commit()

    result["cost_usd"] = cost
    return result
