"""
Zeni Cloud Core — Vector Search service (pgvector backed).

Pattern:
  - Registry table `public.vector_collections` lưu metadata (workspace, name, dim, metric).
  - Mỗi collection có 1 table riêng `public.vec_<ws>_<name>` chứa rows
    (id TEXT PK, vector VECTOR(dim), metadata JSONB, created_at TIMESTAMPTZ).
  - DDL dùng `text()` + identifier đã được sanitize bằng `_sanitize_name()`
    (regex `^[a-z][a-z0-9_]{0,30}$`). KHÔNG f-string user input vào WHERE/VALUES.
  - DML dùng named params (`:id`, `:vector`, …).
  - HNSW index tạo lúc create collection để search nhanh.

Metric → operator:
  cosine → `<=>`   (vector_cosine_ops)
  l2     → `<->`   (vector_l2_ops)
  ip     → `<#>`   (vector_ip_ops, distance = -inner_product)
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("zeni.vector")

# ─── Constants ──────────────────────────────────────────────
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
_MAX_TABLE_NAME_LEN = 60                # tổng độ dài identifier table
_MAX_DIM = 4096
_MAX_K = 100
_MAX_POINTS_PER_UPSERT = 1000

_METRIC_TO_OPERATOR = {
    "cosine": "<=>",
    "l2":     "<->",
    "ip":     "<#>",
}
_METRIC_TO_OPCLASS = {
    "cosine": "vector_cosine_ops",
    "l2":     "vector_l2_ops",
    "ip":     "vector_ip_ops",
}

# Trả về exception class nội bộ — caller tầng API map sang HTTPException.
class VectorError(ValueError):
    """Lỗi business cho vector service (caller convert thành HTTP 400/404)."""


# ─── Helpers ────────────────────────────────────────────────
def _sanitize_name(s: str) -> str:
    """Chuẩn hoá identifier cho workspace_id hoặc collection name.

    Yêu cầu: bắt đầu bằng chữ thường, chỉ chứa [a-z0-9_], dài 1-31 ký tự.
    Raises ValueError nếu không hợp lệ.
    """
    if not isinstance(s, str):
        raise VectorError("Tên collection không hợp lệ")
    if not _NAME_RE.match(s):
        raise VectorError(
            "Tên không hợp lệ. Chỉ chấp nhận [a-z0-9_], bắt đầu bằng chữ, dài 1-31 ký tự"
        )
    return s


def _table_name(workspace_id: str, name: str) -> str:
    """Build identifier `vec_<ws>_<name>` đã được verify safe."""
    ws = _sanitize_name(workspace_id)
    nm = _sanitize_name(name)
    table = f"vec_{ws}_{nm}"
    if len(table) > _MAX_TABLE_NAME_LEN:
        raise VectorError(
            f"Tên table quá dài (max {_MAX_TABLE_NAME_LEN} ký tự): {table}"
        )
    return table


def _validate_dim(dim: int) -> None:
    if not isinstance(dim, int) or dim < 1 or dim > _MAX_DIM:
        raise VectorError(f"Số chiều phải từ 1-{_MAX_DIM}")


def _validate_metric(metric: str) -> None:
    if metric not in _METRIC_TO_OPERATOR:
        raise VectorError("Metric không hợp lệ. Chấp nhận: cosine, l2, ip")


def _vector_literal(vec: list[float]) -> str:
    """Serialize python list → pgvector literal '[v1,v2,...]'."""
    if not isinstance(vec, list) or not vec:
        raise VectorError("Vector không hợp lệ (rỗng hoặc sai kiểu)")
    parts: list[str] = []
    for v in vec:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise VectorError("Vector phải là list số (int/float)")
        # Loại NaN/Inf để Postgres không reject
        f = float(v)
        if f != f or f in (float("inf"), float("-inf")):
            raise VectorError("Vector chứa giá trị NaN/Inf không hợp lệ")
        parts.append(repr(f))
    return "[" + ",".join(parts) + "]"


async def _get_collection_meta(
    db: AsyncSession, workspace_id: str, name: str
) -> dict[str, Any]:
    row = (await db.execute(
        text("""
            SELECT id, workspace_id, name, dim, metric, row_count, table_name, created_at
            FROM public.vector_collections
            WHERE workspace_id = :ws AND name = :nm
        """),
        {"ws": workspace_id, "nm": name},
    )).first()
    if row is None:
        raise VectorError("Collection không tồn tại")
    return {
        "id":           row[0],
        "workspace_id": row[1],
        "name":         row[2],
        "dim":          row[3],
        "metric":       row[4],
        "row_count":    int(row[5] or 0),
        "table_name":   row[6],
        "created_at":   row[7].isoformat() if row[7] else None,
    }


# ─── Public API ─────────────────────────────────────────────
async def create_collection(
    db: AsyncSession,
    workspace_id: str,
    name: str,
    dim: int,
    metric: str = "cosine",
) -> dict[str, Any]:
    """Tạo collection mới: register row + tạo per-collection table + HNSW index.

    Idempotent: nếu collection cùng (workspace_id, name) đã tồn tại → raise
    VectorError "Collection đã tồn tại".
    """
    _validate_dim(dim)
    _validate_metric(metric)
    table = _table_name(workspace_id, name)        # đã sanitize
    nm = _sanitize_name(name)
    ws = _sanitize_name(workspace_id)

    opclass = _METRIC_TO_OPCLASS[metric]

    # 1. Insert vào registry trước (giữ uniqueness)
    try:
        await db.execute(
            text("""
                INSERT INTO public.vector_collections
                    (workspace_id, name, dim, metric, table_name)
                VALUES (:ws, :nm, :dim, :metric, :tbl)
            """),
            {"ws": ws, "nm": nm, "dim": dim, "metric": metric, "tbl": table},
        )
    except IntegrityError as e:
        await db.rollback()
        log.info("create_collection conflict ws=%s name=%s", ws, nm)
        raise VectorError("Collection đã tồn tại") from e

    # 2. Tạo per-collection table (identifier đã sanitize, safe để format)
    ddl_table = (
        f'CREATE TABLE IF NOT EXISTS public."{table}" ('
        f'  id          TEXT PRIMARY KEY,'
        f'  vector      VECTOR({dim}) NOT NULL,'
        f'  metadata    JSONB NOT NULL DEFAULT \'{{}}\'::jsonb,'
        f'  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()'
        f')'
    )
    ddl_index = (
        f'CREATE INDEX IF NOT EXISTS "idx_{table}_hnsw" '
        f'ON public."{table}" USING hnsw (vector {opclass})'
    )
    ddl_meta = (
        f'CREATE INDEX IF NOT EXISTS "idx_{table}_metadata" '
        f'ON public."{table}" USING GIN (metadata)'
    )
    ddl_grant = f'GRANT ALL PRIVILEGES ON public."{table}" TO zeni_app'

    try:
        await db.execute(text(ddl_table))
        await db.execute(text(ddl_index))
        await db.execute(text(ddl_meta))
    except Exception:
        await db.rollback()
        log.exception("create_collection DDL failed ws=%s name=%s", ws, nm)
        raise

    # GRANT là best-effort — môi trường dev có thể chưa có role zeni_app.
    # Phải commit trước, vì bất kỳ lỗi nào trong transaction Postgres đều abort
    # toàn bộ transaction → rollback sẽ huỷ luôn registry+CREATE TABLE phía trên.
    await db.commit()
    try:
        await db.execute(text(ddl_grant))
        await db.commit()
    except ProgrammingError:
        await db.rollback()
        log.warning("GRANT skipped (role zeni_app missing) for %s", table)
    except Exception:
        await db.rollback()
        log.warning("GRANT failed (non-fatal) for %s", table)
    return {
        "ok":           True,
        "workspace_id": ws,
        "name":         nm,
        "dim":          dim,
        "metric":       metric,
        "table_name":   table,
    }


async def list_collections(
    db: AsyncSession, workspace_id: str
) -> list[dict[str, Any]]:
    """Liệt kê tất cả collection trong workspace."""
    ws = _sanitize_name(workspace_id)
    rows = (await db.execute(
        text("""
            SELECT name, dim, metric, row_count, table_name, created_at
            FROM public.vector_collections
            WHERE workspace_id = :ws
            ORDER BY name
        """),
        {"ws": ws},
    )).all()
    return [
        {
            "name":       r[0],
            "dim":        r[1],
            "metric":     r[2],
            "row_count":  int(r[3] or 0),
            "table_name": r[4],
            "created_at": r[5].isoformat() if r[5] else None,
        }
        for r in rows
    ]


async def upsert_points(
    db: AsyncSession,
    workspace_id: str,
    collection: str,
    points: list[dict[str, Any]],
) -> dict[str, Any]:
    """Bulk upsert vào collection. Mỗi point: {id, vector, metadata?}.

    Trả về `{ok, upserted}`.
    """
    if not isinstance(points, list) or len(points) == 0:
        raise VectorError("Danh sách points rỗng")
    if len(points) > _MAX_POINTS_PER_UPSERT:
        raise VectorError(
            f"Quá nhiều points (max {_MAX_POINTS_PER_UPSERT} mỗi request)"
        )

    meta = await _get_collection_meta(db, workspace_id, collection)
    table = meta["table_name"]
    expected_dim = int(meta["dim"])

    # Validate + serialize từng point
    rows: list[dict[str, Any]] = []
    for idx, p in enumerate(points):
        if not isinstance(p, dict):
            raise VectorError(f"Point #{idx} không phải object")
        pid = p.get("id")
        vec = p.get("vector")
        md = p.get("metadata") or {}
        if not isinstance(pid, str) or not pid:
            raise VectorError(f"Point #{idx} thiếu id (string)")
        if len(pid) > 256:
            raise VectorError(f"Point #{idx} id quá dài (max 256)")
        if not isinstance(vec, list):
            raise VectorError(f"Point #{idx} thiếu vector (list số)")
        if len(vec) != expected_dim:
            raise VectorError(
                f"Point #{idx} có {len(vec)} chiều, expected {expected_dim}"
            )
        if not isinstance(md, dict):
            raise VectorError(f"Point #{idx} metadata phải là object")
        rows.append({
            "id":       pid,
            "vector":   _vector_literal(vec),
            "metadata": json.dumps(md, ensure_ascii=False),
        })

    # Identifier table đã sanitize trong _get_collection_meta (set bởi create)
    sql = (
        f'INSERT INTO public."{table}" (id, vector, metadata) '
        f'VALUES (:id, CAST(:vector AS vector), CAST(:metadata AS jsonb)) '
        f'ON CONFLICT (id) DO UPDATE SET '
        f'  vector = EXCLUDED.vector, '
        f'  metadata = EXCLUDED.metadata'
    )

    try:
        await db.execute(text(sql), rows)
    except Exception:
        await db.rollback()
        log.exception(
            "upsert_points failed ws=%s collection=%s n=%d",
            workspace_id, collection, len(rows),
        )
        raise

    # Refresh row_count = SELECT COUNT(*)
    cnt = (await db.execute(
        text(f'SELECT COUNT(*) FROM public."{table}"')
    )).scalar() or 0
    await db.execute(
        text("""
            UPDATE public.vector_collections
            SET row_count = :cnt
            WHERE workspace_id = :ws AND name = :nm
        """),
        {"cnt": int(cnt), "ws": meta["workspace_id"], "nm": meta["name"]},
    )
    await db.commit()
    return {"ok": True, "upserted": len(rows), "row_count": int(cnt)}


async def search(
    db: AsyncSession,
    workspace_id: str,
    collection: str,
    vector: list[float],
    k: int = 10,
    filter: dict | None = None,
) -> list[dict[str, Any]]:
    """Top-k similarity search. Trả về list `{id, distance, metadata}` đã sort tăng dần."""
    if not isinstance(k, int) or k < 1 or k > _MAX_K:
        raise VectorError(f"k phải từ 1-{_MAX_K}")

    meta = await _get_collection_meta(db, workspace_id, collection)
    table = meta["table_name"]
    expected_dim = int(meta["dim"])
    metric = meta["metric"]
    op = _METRIC_TO_OPERATOR[metric]

    if not isinstance(vector, list) or len(vector) != expected_dim:
        raise VectorError(
            f"Vector phải có {expected_dim} chiều"
        )
    vec_lit = _vector_literal(vector)

    # Build optional metadata filter (JSONB containment)
    params: dict[str, Any] = {"qvec": vec_lit, "k": k}
    where_sql = ""
    if filter is not None:
        if not isinstance(filter, dict):
            raise VectorError("filter phải là object JSON")
        if filter:
            where_sql = "WHERE metadata @> CAST(:flt AS jsonb)"
            params["flt"] = json.dumps(filter, ensure_ascii=False)

    sql = (
        f'SELECT id, vector {op} CAST(:qvec AS vector) AS distance, metadata '
        f'FROM public."{table}" '
        f'{where_sql} '
        f'ORDER BY vector {op} CAST(:qvec AS vector) '
        f'LIMIT :k'
    )

    try:
        rows = (await db.execute(text(sql), params)).all()
    except Exception:
        await db.rollback()
        log.exception(
            "search failed ws=%s collection=%s k=%d", workspace_id, collection, k
        )
        raise

    return [
        {"id": r[0], "distance": float(r[1]), "metadata": r[2] or {}}
        for r in rows
    ]


async def delete_collection(
    db: AsyncSession, workspace_id: str, name: str
) -> None:
    """Drop per-collection table + xoá row trong registry. Idempotent: nếu không tồn tại → raise."""
    meta = await _get_collection_meta(db, workspace_id, name)
    table = meta["table_name"]

    try:
        await db.execute(text(f'DROP TABLE IF EXISTS public."{table}" CASCADE'))
        await db.execute(
            text("""
                DELETE FROM public.vector_collections
                WHERE workspace_id = :ws AND name = :nm
            """),
            {"ws": meta["workspace_id"], "nm": meta["name"]},
        )
    except Exception:
        await db.rollback()
        log.exception(
            "delete_collection failed ws=%s name=%s", workspace_id, name
        )
        raise

    await db.commit()
