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
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import SessionLocal, get_db
from app.db.models import Database
from app.schemas.resources import DatabaseOut, QueryIn
from app.services.audit import audit_push, billing_push


class DatabaseCreateIn(BaseModel):
    """Schema for creating a new logical database/collection in a workspace.

    `kind` controls which storage backend is materialized:
      - postgres → row in databases table + Postgres schema-scoped to ws_{id} (no
        physical new DB for MVP, single Cloud SQL instance)
      - vector → row representing a pgvector collection (dim required)
      - object → row representing a Cloud Storage prefix bucket
    """
    name: str = Field(min_length=1, max_length=128)
    kind: str = Field(default="postgres")  # postgres / vector / object
    description: str | None = Field(default=None, max_length=500)
    dim: int | None = Field(default=None, ge=1, le=4096)  # for vector only

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


@router.post("/databases", response_model=DatabaseOut, status_code=201)
async def create_database(
    ws: str,
    payload: DatabaseCreateIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DatabaseOut:
    """Create a logical database row + materialize physical resource if needed."""
    await require_workspace_access(ws, me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Cần Developer trở lên để tạo database")

    if payload.kind not in ("postgres", "vector", "object"):
        raise HTTPException(status_code=422, detail="kind phải là: postgres / vector / object")

    # Sanitize name: lowercase + alphanumeric + underscore only
    safe_name = re.sub(r"[^a-z0-9_]", "_", payload.name.lower()).strip("_")
    if not safe_name:
        raise HTTPException(status_code=422, detail="Tên không hợp lệ sau sanitize")

    # Check duplicate
    existing = (await db.execute(
        select(Database).where(Database.workspace_id == ws, Database.name == safe_name)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail=f"Database '{safe_name}' đã tồn tại trong workspace")

    if payload.kind == "vector" and not payload.dim:
        raise HTTPException(status_code=422, detail="kind=vector cần `dim` (chiều embedding, vd 1536)")

    # For postgres kind: also create real schema-scoped table set if needed (MVP: row only)
    new_db = Database(
        workspace_id=ws, name=safe_name, kind=payload.kind,
        description=payload.description, dim=payload.dim,
        row_count=0, size_bytes=0,
    )
    db.add(new_db)
    await db.commit()
    await db.refresh(new_db)

    await audit_push(
        db, actor=me.email, workspace_id=ws, action="data.database.create",
        target=f"{payload.kind}/{safe_name}", severity="ok",
        metadata={"kind": payload.kind, "name": safe_name, "dim": payload.dim},
    )
    await billing_push(db, workspace_id=ws, layer="L2", action="data.database.create", cost_usd=0.0)
    await db.commit()
    return DatabaseOut.model_validate(new_db)


@router.delete("/databases/{db_id}", status_code=204)
async def delete_database(
    ws: str,
    db_id: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Soft-delete a logical database row. Real schema NOT dropped automatically."""
    await require_workspace_access(ws, me)
    if me.role in ("Viewer", "Developer"):
        raise HTTPException(status_code=403, detail="Cần Admin trở lên để xóa database")

    import uuid as _uuid
    try:
        uid = _uuid.UUID(db_id)
    except Exception:
        raise HTTPException(status_code=422, detail="db_id phải là UUID")

    row = (await db.execute(
        select(Database).where(Database.id == uid, Database.workspace_id == ws)
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Database không tồn tại trong workspace này")

    name = row.name
    kind = row.kind
    await db.delete(row)
    await db.commit()

    await audit_push(
        db, actor=me.email, workspace_id=ws, action="data.database.delete",
        target=f"{kind}/{name}", severity="warn",
    )
    await db.commit()


@router.get("/tables/{table_name}/schema")
async def get_table_schema(
    ws: str,
    table_name: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Inspect columns + indexes + constraints of a table in workspace schema."""
    await require_workspace_access(ws, me)
    if ws not in _VALID_WS:
        raise HTTPException(status_code=404, detail="workspace không hợp lệ")
    # Sanitize table_name (alphanum + underscore only)
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$", table_name):
        raise HTTPException(status_code=422, detail="Tên table không hợp lệ")

    schema = f"ws_{ws}"
    # Columns
    cols_sql = """
        SELECT column_name, data_type, is_nullable, column_default, character_maximum_length
        FROM information_schema.columns
        WHERE table_schema = :schema AND table_name = :tname
        ORDER BY ordinal_position
    """
    cols = (await db.execute(text(cols_sql), {"schema": schema, "tname": table_name})).all()
    if not cols:
        raise HTTPException(status_code=404, detail=f"Table '{table_name}' không có trong schema {schema}")

    # Indexes
    idx_sql = """
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = :schema AND tablename = :tname
        ORDER BY indexname
    """
    idx = (await db.execute(text(idx_sql), {"schema": schema, "tname": table_name})).all()

    # Row count estimate (fast, from stats)
    cnt_sql = """
        SELECT n_live_tup, n_dead_tup, last_vacuum, last_autovacuum
        FROM pg_stat_user_tables
        WHERE schemaname = :schema AND relname = :tname
    """
    cnt_row = (await db.execute(text(cnt_sql), {"schema": schema, "tname": table_name})).first()

    return {
        "schema": schema,
        "table": table_name,
        "columns": [
            {
                "name": r[0],
                "type": r[1],
                "nullable": r[2] == "YES",
                "default": r[3],
                "max_length": r[4],
            }
            for r in cols
        ],
        "indexes": [{"name": r[0], "definition": r[1]} for r in idx],
        "row_count_estimate": cnt_row[0] if cnt_row else 0,
        "dead_rows": cnt_row[1] if cnt_row else 0,
        "last_vacuum": cnt_row[2].isoformat() if cnt_row and cnt_row[2] else None,
        "last_autovacuum": cnt_row[3].isoformat() if cnt_row and cnt_row[3] else None,
    }


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
