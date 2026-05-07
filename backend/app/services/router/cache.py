"""
ZeniRouter cache layer — DB-backed (router_cache table).

Exact-match key: SHA256(tenant_id + messages + model + temperature).
TTL: 5 minutes default (configurable per task_type by the caller).
Tenant scoping via workspace_id is enforced at the key level so two tenants
never collide even on identical prompts.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def make_cache_key(
    workspace_id: str,
    messages: list[dict],
    model_id: str,
    temperature: float,
) -> str:
    """Cache key includes workspace_id to prevent cross-tenant leak."""
    payload = json.dumps(
        {"ws": workspace_id, "msgs": messages, "model": model_id, "temp": temperature},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


async def cache_get(db: AsyncSession, key: str) -> dict | None:
    """Return cached entry if present + not expired; bumps hit_count atomically."""
    row = (await db.execute(text("""
        UPDATE router_cache
        SET hit_count = hit_count + 1
        WHERE cache_key = :k AND expires_at > NOW()
        RETURNING response_text, model_id, input_tokens, output_tokens
    """), {"k": key})).mappings().first()
    if row:
        await db.commit()
        return dict(row)
    return None


async def cache_set(
    db: AsyncSession,
    key: str,
    workspace_id: str,
    response_text: str,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    ttl_seconds: int = 300,
) -> None:
    """Insert or refresh a cache entry. ON CONFLICT bumps hit_count."""
    expires = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    await db.execute(text("""
        INSERT INTO router_cache
            (cache_key, workspace_id, response_text, model_id,
             input_tokens, output_tokens, expires_at)
        VALUES
            (:k, :ws, :rt, :m, :it, :ot, :exp)
        ON CONFLICT (cache_key) DO UPDATE
            SET hit_count = router_cache.hit_count + 1
    """), {
        "k": key, "ws": workspace_id, "rt": response_text, "m": model_id,
        "it": input_tokens, "ot": output_tokens, "exp": expires,
    })
    await db.commit()


async def cache_purge_expired(db: AsyncSession) -> int:
    """Optional housekeeping helper — deletes rows past expires_at.
    Returns the number of rows removed."""
    res = await db.execute(text("DELETE FROM router_cache WHERE expires_at <= NOW()"))
    await db.commit()
    return res.rowcount or 0
