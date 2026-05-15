"""
Zeni Cloud Core — L2 Data API (REAL multi-tenant SQL).

Per-workspace Postgres schema isolation:
  ws=anima → SET search_path = ws_anima, public; <query>

Vector + Object queries still mock for MVP (will hook to pgvector + Cloud Storage
in next milestones).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import SessionLocal, get_db
from app.db.models import Database
from app.schemas.resources import DatabaseOut, QueryIn
from app.services.audit import audit_push, billing_push

log = logging.getLogger("zeni.api.data")
router = APIRouter(prefix="/data", tags=["data"])


# Allowed workspace IDs (matches workspaces seed)
_VALID_WS = {"holdings", "anima", "zeniipo", "digital", "wellkoc", "nexbuild", "bthome", "capital"}

# Forbidden patterns — prevent abuse even when running in isolated schema
_FORBIDDEN_PATTERNS = [
    r"\bpg_catalog\b",           # block introspection of system catalogs
    r"\bpg_authid\b",
    r"\bpg_shadow\b",
    r"\binformation_schema\.[a-z_]*\b(?!.*\bws_)",  # allow if scoped to ws_
    r"\bcopy\s+\w+\s+(?:to|from)\b",  # COPY filesystem
    r"\blo_(?:import|export|create)\b",
    r"\bdblink\b",
    r"--\s*rm\s+-rf",
    r"xp_cmdshell",
    r";\s*(?:drop|truncate)\s+(?:database|user|role)\b",
]
_FORBIDDEN_RE = [re.compile(p, re.IGNORECASE) for p in _FORBIDDEN_PATTERNS]

# Hard caps
MAX_ROWS_RETURN = 1000
QUERY_TIMEOUT_SEC = 30


def _validate_query(q: str) -> None:
    if len(q) > 4000:
        raise HTTPException(status_code=400, detail="Query quá dài (>4000 ký tự)")
    for pattern in _FORBIDDEN_RE:
        if pattern.search(q):
            raise HTTPException(status_code=400, detail=f"Query chứa pattern bị chặn: {pattern.pattern}")


def _validate_query_request(ws: str, qtype: str, query: str | None) -> str:
    """Pre-flight validation — fail-fast 422 with clear hint instead of cryptic 400 later.

    Returns trimmed query string ready for execution.
    """
    if ws not in _VALID_WS:
        raise HTTPException(
            status_code=404,
            detail=f"Workspace '{ws}' không hợp lệ. Dùng: {', '.join(sorted(_VALID_WS))}.",
        )
    if qtype not in {"sql", "vector", "object"}:
        raise HTTPException(
            status_code=422,
            detail=f"qtype '{qtype}' không hỗ trợ. Dùng: sql, vector, object.",
        )
    if query is None or not query.strip():
        raise HTTPException(
            status_code=422,
            detail="Query rỗng — vui lòng nhập SQL/vector keyword/object prefix cụ thể.",
        )
    trimmed = query.strip()
    if len(trimmed) > 4000:
        raise HTTPException(status_code=422, detail="Query quá dài (>4000 ký tự).")
    if qtype == "sql":
        # Light syntax sanity: must start with a known keyword
        first_token = trimmed.split(None, 1)[0].lower()
        allowed_starts = {
            "select", "with", "explain", "show", "insert", "update",
            "delete", "create", "alter", "drop", "truncate", "begin",
            "commit", "rollback",
        }
        if first_token not in allowed_starts:
            raise HTTPException(
                status_code=422,
                detail=f"SQL phải bắt đầu bằng từ khóa hợp lệ (SELECT, WITH, INSERT, ...). Got: '{first_token}'.",
            )
    return trimmed


@router.get("/databases", response_model=list[DatabaseOut])
async def list_databases(
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[DatabaseOut]:
    await require_workspace_access(ws, me)
    rows = (await db.execute(
        select(Database).where(Database.workspace_id == ws).order_by(Database.kind, Database.name)
    )).scalars().all()
    return [DatabaseOut.model_validate(r) for r in rows]


@router.get("/tables")
async def list_tables(
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """List tables in this workspace's schema (real introspection)."""
    await require_workspace_access(ws, me)
    if ws not in _VALID_WS:
        raise HTTPException(status_code=404, detail="workspace không hợp lệ")
    schema = f"ws_{ws}"
    sql = """
        SELECT table_name,
               (SELECT count(*) FROM information_schema.columns c
                WHERE c.table_schema = t.table_schema AND c.table_name = t.table_name) AS column_count
        FROM information_schema.tables t
        WHERE t.table_schema = :schema AND t.table_type = 'BASE TABLE'
        ORDER BY table_name
    """
    rows = (await db.execute(text(sql), {"schema": schema})).all()
    return {
        "schema": schema,
        "tables": [{"name": r[0], "columns": r[1]} for r in rows],
    }


@router.post("/query")
async def run_query(
    ws: str,
    data: QueryIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Execute query against workspace schema. SQL = real, vector/object = mock."""
    await require_workspace_access(ws, me)

    # Fail-fast pre-flight: validate ws, qtype, query format BEFORE any DB write/exec
    trimmed_query = _validate_query_request(ws, data.qtype, data.query)
    data.query = trimmed_query

    start = time.perf_counter()
    _validate_query(data.query)
    schema = f"ws_{ws}"

    if data.qtype == "sql":
        rows: list[dict] = []
        columns: list[str] = []
        try:
            # Use a fresh connection to set search_path safely
            async with SessionLocal() as conn_session:
                # Set search_path scoped to this transaction only
                await conn_session.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
                await conn_session.execute(text(f'SET LOCAL statement_timeout = {QUERY_TIMEOUT_SEC * 1000}'))

                # Execute user query (read-only enforced for Viewer; full for others)
                if me.role == "Viewer":
                    if not data.query.strip().lower().startswith(("select", "with", "explain", "show")):
                        raise HTTPException(status_code=403, detail="Viewer chỉ được SELECT/WITH/EXPLAIN")

                result = await conn_session.execute(text(data.query))

                if result.returns_rows:
                    columns = list(result.keys())
                    fetched = result.fetchmany(MAX_ROWS_RETURN)
                    rows = [dict(zip(columns, [_serialize(v) for v in r])) for r in fetched]
                else:
                    columns = ["affected_rows"]
                    rows = [{"affected_rows": result.rowcount}]
                # Auto-commit if write operation
                await conn_session.commit()

        except HTTPException:
            raise
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail=f"Query vượt quá {QUERY_TIMEOUT_SEC}s")
        except Exception as e:
            err_msg = str(e)
            log.exception("Query failed for ws=%s: %s", ws, err_msg)
            raise HTTPException(status_code=400, detail=f"SQL error: {err_msg[:300]}")

    elif data.qtype == "vector":
        # TODO M2.2: real pgvector with embeddings
        rows = [
            {"doc_id": f"doc_{4800 - i*50}",
             "preview": "(L2 vector search chưa hoàn thiện - sẽ có pgvector trong M2.2)",
             "similarity": round(0.95 - i*0.03, 3)}
            for i in range(5)
        ]
        columns = ["doc_id", "preview", "similarity"]

    else:  # object
        # TODO M2.3: real Cloud Storage list
        rows = [
            {"key": f"{ws}/(L2 object storage chưa hoàn thiện - hook Cloud Storage trong M2.3)",
             "size": "—", "modified": "—"}
        ]
        columns = ["key", "size", "modified"]

    latency_ms = int((time.perf_counter() - start) * 1000)
    cost = 0.000012 * max(1, len(rows))

    await audit_push(
        db, actor=me.email, workspace_id=ws, action="data.query", target=schema, severity="ok",
        metadata={"qtype": data.qtype, "rows": len(rows), "latency_ms": latency_ms},
    )
    await billing_push(db, workspace_id=ws, layer="L2", action=f"data.query.{data.qtype}", cost_usd=cost)
    await db.commit()

    return {
        "qtype": data.qtype,
        "schema": schema,
        "latency_ms": latency_ms,
        "columns": columns,
        "rows": rows,
        "cost_usd": cost,
        "row_count": len(rows),
        "truncated": len(rows) >= MAX_ROWS_RETURN,
    }


def _serialize(v: Any) -> Any:
    """Convert non-JSON types to JSON-friendly representation."""
    if v is None or isinstance(v, (str, int, float, bool, list, dict)):
        return v
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    return str(v)
