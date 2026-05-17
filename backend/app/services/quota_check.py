"""
Workspace AI Quota Check + Enforcement
======================================

Per migration 075 — workspace_ai_quotas table.

Pattern:
  1. Before AI call: `await check_quota(db, ws, kind, amount)` → raise 429 nếu vượt
  2. After AI call success: `await increment_usage(db, ws, kind, amount)`

Kinds:
  - reasoning   : Gemini Pro text-only tokens
  - vision      : Gemini Pro multimodal tokens
  - image       : Imagen 3 renders (count)
  - storage     : GCS GB-month

Quota = 0 → unlimited (default cho Owner/internal workspaces).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("zeni.quota")


def _current_period() -> str:
    """Return current YYYY-MM."""
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


_FIELD_MAP = {
    "reasoning": ("reasoning_tokens_used", "reasoning_tokens_quota"),
    "vision":    ("vision_tokens_used",    "vision_tokens_quota"),
    "image":     ("image_count_used",      "image_count_quota"),
    "storage":   ("storage_gb_used",       "storage_gb_quota"),
}


async def check_quota(
    db: AsyncSession,
    workspace_id: str,
    kind: str,
    amount: float = 1,
) -> None:
    """Raise HTTPException(429) nếu workspace vượt quota.

    Args:
        db: AsyncSession
        workspace_id: workspace_id (vd 'vietcontech')
        kind: 'reasoning' | 'vision' | 'image' | 'storage'
        amount: số tokens/images/GB cần tiêu (default 1 cho image)

    No-op nếu workspace không có quota row (= unlimited).
    """
    if kind not in _FIELD_MAP:
        raise ValueError(f"Invalid quota kind: {kind}")

    used_field, quota_field = _FIELD_MAP[kind]
    period = _current_period()

    row = (await db.execute(text(f"""
        SELECT {used_field} AS used, {quota_field} AS quota
        FROM workspace_ai_quotas
        WHERE workspace_id = :ws AND period_month = :p
    """), {"ws": workspace_id, "p": period})).mappings().first()

    if row is None:
        # No quota row → unlimited (default Owner / new workspace).
        return

    quota = float(row["quota"] or 0)
    if quota <= 0:
        # Quota = 0 → unlimited.
        return

    used = float(row["used"] or 0)
    if used + amount > quota:
        log.warning(
            "[quota] %s exceeded %s: used=%s + amount=%s > quota=%s",
            workspace_id, kind, used, amount, quota,
        )
        raise HTTPException(
            status_code=429,
            detail={
                "error": "quota_exceeded",
                "kind": kind,
                "used": used,
                "quota": quota,
                "amount_requested": amount,
                "period": period,
                "message": (
                    f"Workspace '{workspace_id}' đã vượt quota {kind} tháng {period}: "
                    f"{used + amount}/{quota}. Upgrade plan hoặc đợi tháng sau."
                ),
            },
        )


async def increment_usage(
    db: AsyncSession,
    workspace_id: str,
    kind: str,
    amount: float = 1,
) -> None:
    """Increment usage counter sau khi AI call success.

    Auto-create row nếu chưa có (with quota=0 = unlimited).
    """
    if kind not in _FIELD_MAP:
        raise ValueError(f"Invalid quota kind: {kind}")

    used_field, _ = _FIELD_MAP[kind]
    period = _current_period()

    await db.execute(text(f"""
        INSERT INTO workspace_ai_quotas (workspace_id, period_month, {used_field})
        VALUES (:ws, :p, :a)
        ON CONFLICT (workspace_id, period_month) DO UPDATE
            SET {used_field} = workspace_ai_quotas.{used_field} + :a,
                updated_at  = NOW()
    """), {"ws": workspace_id, "p": period, "a": amount})


async def get_usage_summary(
    db: AsyncSession,
    workspace_id: str,
) -> dict:
    """Lấy quota state hiện tại của workspace cho dashboard."""
    period = _current_period()
    row = (await db.execute(text("""
        SELECT
            reasoning_tokens_used  AS r_used,  reasoning_tokens_quota AS r_quota,
            vision_tokens_used     AS v_used,  vision_tokens_quota    AS v_quota,
            image_count_used       AS i_used,  image_count_quota      AS i_quota,
            storage_gb_used        AS s_used,  storage_gb_quota       AS s_quota
        FROM workspace_ai_quotas
        WHERE workspace_id = :ws AND period_month = :p
    """), {"ws": workspace_id, "p": period})).mappings().first()

    if row is None:
        return {
            "workspace_id": workspace_id,
            "period": period,
            "reasoning": {"used": 0, "quota": 0, "remaining": -1},
            "vision":    {"used": 0, "quota": 0, "remaining": -1},
            "image":     {"used": 0, "quota": 0, "remaining": -1},
            "storage":   {"used": 0, "quota": 0, "remaining": -1},
            "unlimited": True,
        }

    def _stat(used_val, quota_val):
        used = float(used_val or 0)
        quota = float(quota_val or 0)
        return {
            "used": used,
            "quota": quota,
            "remaining": max(0, quota - used) if quota > 0 else -1,  # -1 = unlimited
        }

    return {
        "workspace_id": workspace_id,
        "period": period,
        "reasoning": _stat(row["r_used"], row["r_quota"]),
        "vision":    _stat(row["v_used"], row["v_quota"]),
        "image":     _stat(row["i_used"], row["i_quota"]),
        "storage":   _stat(row["s_used"], row["s_quota"]),
    }
