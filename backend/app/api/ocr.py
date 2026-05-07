"""
Zeni Cloud Core — L3 OCR API (Stream A3).

Endpoints:
  POST /ocr/image?ws=  — OCR 1 ảnh (GCS URI / public URL / base64)
  POST /ocr/pdf?ws=    — OCR PDF (sync, max 5 pages)

Pricing: $1.50 / 1K page (image = 1 page, PDF = N pages)
Audit:   ai.ocr
Billing: layer="L3", action="ai.ocr"
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services import ocr as ocr_svc
from app.services.audit import audit_push, billing_push

log = logging.getLogger("zeni.api.ocr")
router = APIRouter(prefix="/ocr", tags=["ai", "ocr"])

# Cloud Vision DOCUMENT_TEXT_DETECTION public list price: $1.50 per 1000 pages
COST_PER_PAGE_USD = 1.50 / 1000  # = 0.0015


class OcrImageIn(BaseModel):
    gcs_uri: str | None = Field(default=None, max_length=2048)
    image_url: str | None = Field(default=None, pattern=r"^https?://.+", max_length=2048)
    image_base64: str | None = Field(default=None, max_length=20_000_000)


class OcrPdfIn(BaseModel):
    gcs_uri: str = Field(min_length=5, max_length=2048)
    max_pages: int = Field(default=5, ge=1, le=5)


def _check_scope(me: CurrentUser) -> None:
    """PAT (workspace token) phải có scope 'ai' hoặc 'full'. JWT user thì bỏ qua."""
    if me.auth_scope and not any(s in me.auth_scope for s in ("ai", "full")):
        raise HTTPException(status_code=403, detail="Token thiếu scope 'ai'")


@router.post("/image")
async def ocr_image(
    ws: str,
    data: OcrImageIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """OCR 1 image — Cloud Vision DOCUMENT_TEXT_DETECTION."""
    await require_workspace_access(ws, me)
    _check_scope(me)

    # Đúng 1 trong 3 phải có
    provided = [bool(data.gcs_uri), bool(data.image_url), bool(data.image_base64)]
    if sum(provided) != 1:
        raise HTTPException(
            status_code=400,
            detail="Cần đúng 1 trong 3: gcs_uri, image_url, image_base64",
        )

    image_source = data.gcs_uri or data.image_url or data.image_base64

    try:
        result = await ocr_svc.ocr_image(image_source)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        log.warning("ocr_image runtime: %s", e)
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        log.exception("ocr_image unexpected")
        raise HTTPException(status_code=502, detail="Không thể OCR ảnh")

    # 1 image = 1 page
    pages_count = max(1, len(result.get("pages") or []))
    cost = pages_count * COST_PER_PAGE_USD

    await audit_push(
        db,
        actor=me.email,
        workspace_id=ws,
        action="ai.ocr",
        target=(data.gcs_uri or data.image_url or "base64-image")[:80],
        severity="ok",
        metadata={
            "kind": "image",
            "pages": pages_count,
            "chars": result.get("raw_text_length", 0),
            "language": result.get("language"),
        },
    )
    await billing_push(db, workspace_id=ws, layer="L3", action="ai.ocr", cost_usd=cost)
    await db.commit()

    result["cost_usd"] = cost
    result["pages_charged"] = pages_count
    return result


@router.post("/pdf")
async def ocr_pdf(
    ws: str,
    data: OcrPdfIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """OCR PDF (GCS URI), sync mode max 5 pages."""
    await require_workspace_access(ws, me)
    _check_scope(me)

    try:
        result = await ocr_svc.ocr_pdf(data.gcs_uri, max_pages=data.max_pages)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        log.warning("ocr_pdf runtime: %s", e)
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        log.exception("ocr_pdf unexpected")
        raise HTTPException(status_code=502, detail="Không thể OCR PDF")

    pages_count = max(1, result.get("total_pages") or 0)
    cost = pages_count * COST_PER_PAGE_USD

    await audit_push(
        db,
        actor=me.email,
        workspace_id=ws,
        action="ai.ocr",
        target=data.gcs_uri[:80],
        severity="ok",
        metadata={
            "kind": "pdf",
            "pages": pages_count,
            "chars": result.get("total_chars", 0),
        },
    )
    await billing_push(db, workspace_id=ws, layer="L3", action="ai.ocr", cost_usd=cost)
    await db.commit()

    result["cost_usd"] = cost
    result["pages_charged"] = pages_count
    return result
