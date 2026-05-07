"""
Zeni Cloud Core — Cache service (Postgres KV với TTL).

Bảng public.kv_cache là UNLOGGED → throughput cao, không WAL.
Mất khi crash là CHẤP NHẬN ĐƯỢC (đúng semantic của cache).

Public API (async):
  cache_set(db, ws, key, value, ttl_seconds=None) -> None
  cache_get(db, ws, key) -> Any | None              (lazy delete khi expired)
  cache_delete(db, ws, key) -> bool
  cache_list(db, ws, prefix="", limit=100) -> list[dict]   (KHÔNG trả value)
  cache_purge_expired(db) -> int                    (cron-callable)

Validate:
  - key max 256 chars, không cho empty
  - ttl_seconds: 1..86400*30 (max 30 ngày), None = không expire
  - value phải JSON-serializable
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("zeni.services.cache")

MAX_KEY_LEN = 256
MAX_TTL_SECONDS = 86400 * 30   # 30 ngày
MIN_TTL_SECONDS = 1
MAX_VALUE_BYTES = 1_000_000     # 1MB / value (hard cap)


def _validate_key(key: str) -> None:
    if not key or not isinstance(key, str):
        raise HTTPException(status_code=400, detail="cache key không hợp lệ")
    if len(key) > MAX_KEY_LEN:
        raise HTTPException(status_code=400, detail=f"cache key vượt {MAX_KEY_LEN} ký tự")


def _validate_ttl(ttl_seconds: int | None) -> int | None:
    if ttl_seconds is None:
        return None
    if not isinstance(ttl_seconds, int) or ttl_seconds < MIN_TTL_SECONDS:
        raise HTTPException(status_code=400, detail=f"ttl_seconds phải >= {MIN_TTL_SECONDS}")
    if ttl_seconds > MAX_TTL_SECONDS:
        raise HTTPException(status_code=400, detail=f"ttl_seconds vượt giới hạn {MAX_TTL_SECONDS} (30 ngày)")
    return ttl_seconds


def _serialize_value(value: Any) -> str:
    """Convert value sang JSON string. Raise 400 nếu không JSON-serializable."""
    try:
        s = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"value không JSON-serializable: {e}")
    if len(s.encode("utf-8")) > MAX_VALUE_BYTES:
        raise HTTPException(status_code=400, detail=f"value vượt {MAX_VALUE_BYTES} bytes")
    return s


async def cache_set(
    db: AsyncSession,
    workspace_id: str,
    key: str,
    value: Any,
    ttl_seconds: int | None = None,
) -> None:
    """UPSERT vào kv_cache. expires_at = NOW + ttl_seconds nếu ttl > 0, else NULL."""
    _validate_key(key)
    ttl = _validate_ttl(ttl_seconds)
    payload = _serialize_value(value)

    if ttl is None:
        sql = text("""
            INSERT INTO public.kv_cache (workspace_id, key, value, expires_at, created_at)
            VALUES (:ws, :k, CAST(:v AS JSONB), NULL, NOW())
            ON CONFLICT (workspace_id, key) DO UPDATE
              SET value = EXCLUDED.value,
                  expires_at = NULL,
                  created_at = NOW()
        """)
        await db.execute(sql, {"ws": workspace_id, "k": key, "v": payload})
    else:
        sql = text("""
            INSERT INTO public.kv_cache (workspace_id, key, value, expires_at, created_at)
            VALUES (:ws, :k, CAST(:v AS JSONB), NOW() + (:ttl || ' seconds')::interval, NOW())
            ON CONFLICT (workspace_id, key) DO UPDATE
              SET value = EXCLUDED.value,
                  expires_at = EXCLUDED.expires_at,
                  created_at = NOW()
        """)
        await db.execute(sql, {"ws": workspace_id, "k": key, "v": payload, "ttl": str(ttl)})
    await db.commit()


async def cache_get(
    db: AsyncSession,
    workspace_id: str,
    key: str,
) -> Any | None:
    """Get value. Return None nếu missing OR expired (lazy delete expired row)."""
    _validate_key(key)

    row = (await db.execute(text("""
        SELECT value, expires_at
        FROM public.kv_cache
        WHERE workspace_id = :ws AND key = :k
    """), {"ws": workspace_id, "k": key})).first()

    if row is None:
        return None

    value, expires_at = row[0], row[1]

    # Lazy delete: nếu expired thì xoá và trả None
    if expires_at is not None:
        # Compare ở Postgres để tránh sai timezone giữa client và server
        chk = (await db.execute(text("""
            SELECT (expires_at < NOW()) AS expired
            FROM public.kv_cache
            WHERE workspace_id = :ws AND key = :k
        """), {"ws": workspace_id, "k": key})).first()
        if chk is not None and chk[0]:
            await db.execute(text("""
                DELETE FROM public.kv_cache WHERE workspace_id = :ws AND key = :k
            """), {"ws": workspace_id, "k": key})
            await db.commit()
            return None

    return value


async def cache_delete(
    db: AsyncSession,
    workspace_id: str,
    key: str,
) -> bool:
    """DELETE entry. Trả True nếu thực sự xoá được, False nếu không có."""
    _validate_key(key)
    res = await db.execute(text("""
        DELETE FROM public.kv_cache
        WHERE workspace_id = :ws AND key = :k
    """), {"ws": workspace_id, "k": key})
    await db.commit()
    return (res.rowcount or 0) > 0


async def cache_list(
    db: AsyncSession,
    workspace_id: str,
    prefix: str = "",
    limit: int = 100,
) -> list[dict]:
    """List keys (KHÔNG trả value để tránh leak khi list).

    Trả mỗi entry: {key, expires_at, size_bytes, created_at}.
    """
    limit = min(max(1, int(limit or 100)), 1000)
    if prefix and len(prefix) > MAX_KEY_LEN:
        raise HTTPException(status_code=400, detail="prefix quá dài")

    # Lưu ý: dùng LIKE với escape để safe — prefix có thể chứa _ % nên escape.
    safe_prefix = (prefix or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = safe_prefix + "%"

    rows = (await db.execute(text("""
        SELECT key, expires_at, octet_length(value::text) AS size_bytes, created_at
        FROM public.kv_cache
        WHERE workspace_id = :ws
          AND key LIKE :pat ESCAPE '\\'
          AND (expires_at IS NULL OR expires_at >= NOW())
        ORDER BY key ASC
        LIMIT :lim
    """), {"ws": workspace_id, "pat": pattern, "lim": limit})).all()

    return [
        {
            "key": r[0],
            "expires_at": r[1].isoformat() if r[1] else None,
            "size_bytes": int(r[2] or 0),
            "created_at": r[3].isoformat() if r[3] else None,
        }
        for r in rows
    ]


async def cache_purge_expired(db: AsyncSession) -> int:
    """Cron-callable: xoá hết entries hết hạn. Trả số rows bị xoá."""
    res = await db.execute(text("""
        DELETE FROM public.kv_cache
        WHERE expires_at IS NOT NULL AND expires_at < NOW()
    """))
    await db.commit()
    deleted = int(res.rowcount or 0)
    if deleted > 0:
        log.info("[cache] purged %d expired entries", deleted)
    return deleted
