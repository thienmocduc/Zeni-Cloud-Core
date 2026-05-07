"""
Zeni Cloud Core — Vector DB Premium API.

Routes (all workspace-scoped via ?ws= query param). Mounted at /vector-premium.

Search:
  POST   /vector-premium/search/hybrid?ws=
  POST   /vector-premium/search/rerank?ws=

RAG Pipelines (CRUD):
  POST   /vector-premium/rag/pipelines?ws=
  GET    /vector-premium/rag/pipelines?ws=
  GET    /vector-premium/rag/pipelines/{id}?ws=
  PATCH  /vector-premium/rag/pipelines/{id}?ws=
  DELETE /vector-premium/rag/pipelines/{id}?ws=

RAG Execution:
  POST   /vector-premium/rag/query?ws=
  GET    /vector-premium/rag/queries?ws=&from=&to=&limit=

Premium upsert + stats:
  POST   /vector-premium/upsert/batch?ws=
  GET    /vector-premium/collections/{id}/stats?ws=

Pricing (rough, USD; logged via billing_push):
  - Hybrid search       : $0.0002 / call
  - Rerank              : $0.0005 / call
  - RAG full pipeline   : per-query metered (embed + LLM input/output)
  - Embedding cache hit : $0
  - Embedding miss      : ~$0.000025/1K tokens (text-embedding-004)
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services import rag_engine, vector_search
from app.services.audit import audit_push, billing_push

log = logging.getLogger("zeni.api.vector_premium")
router = APIRouter(prefix="/vector-premium", tags=["vector", "rag", "premium"])


# ════════════════════════════════════════════════════════════════════════════
# Pydantic schemas (v2)
# ════════════════════════════════════════════════════════════════════════════
class HybridSearchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collection: str = Field(min_length=1, max_length=64)
    query: str = Field(min_length=1, max_length=8000)
    top_k: int = Field(default=10, ge=1, le=100)
    alpha: float = Field(default=0.5, ge=0.0, le=1.0)
    namespace: str | None = Field(default=None, max_length=64)
    filter: dict[str, Any] | None = Field(default=None)
    embed_model: str = Field(default="text-embedding-004", max_length=80)


class RerankIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collection: str = Field(min_length=1, max_length=64)
    query: str = Field(min_length=1, max_length=8000)
    candidates_top_k: int = Field(default=20, ge=1, le=100)
    rerank_top_k: int = Field(default=5, ge=1, le=50)
    alpha: float = Field(default=0.5, ge=0.0, le=1.0)
    namespace: str | None = Field(default=None, max_length=64)
    filter: dict[str, Any] | None = Field(default=None)
    rerank_model: str = Field(default="gemini-2.5-flash", max_length=60)


class PipelineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    collection_id: int = Field(ge=1)
    embedding_model: str = Field(default="text-embedding-004", max_length=80)
    rerank_model: str | None = Field(default=None, max_length=80)
    top_k: int = Field(default=5, ge=1, le=100)
    rerank_top_k: int = Field(default=3, ge=1, le=50)
    hybrid_alpha: float = Field(default=0.5, ge=0.0, le=1.0)
    namespace: str | None = Field(default="default", max_length=64)
    system_prompt: str | None = Field(default=None, max_length=4000)
    llm_model: str = Field(default="gemini-2.5-flash", max_length=60)
    temperature: float = Field(default=0.4, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1024, ge=1, le=8192)


class PipelinePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    embedding_model: str | None = Field(default=None, max_length=80)
    rerank_model: str | None = Field(default=None, max_length=80)
    top_k: int | None = Field(default=None, ge=1, le=100)
    rerank_top_k: int | None = Field(default=None, ge=1, le=50)
    hybrid_alpha: float | None = Field(default=None, ge=0.0, le=1.0)
    namespace: str | None = Field(default=None, max_length=64)
    system_prompt: str | None = Field(default=None, max_length=4000)
    llm_model: str | None = Field(default=None, max_length=60)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1, le=8192)


class ConvTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str = Field(pattern=r"^(user|assistant|system)$")
    content: str = Field(min_length=1, max_length=4000)


class RagQueryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pipeline_id: int = Field(ge=1)
    query: str = Field(min_length=1, max_length=8000)
    conversation_history: list[ConvTurn] | None = Field(default=None, max_length=30)


class DocIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=20000)
    id: str | None = Field(default=None, max_length=256)
    metadata: dict[str, Any] | None = Field(default=None)
    namespace: str | None = Field(default="default", max_length=64)


class BatchUpsertIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collection: str = Field(min_length=1, max_length=64)
    documents: list[DocIn] = Field(min_length=1, max_length=200)
    batch_size: int = Field(default=50, ge=1, le=200)
    embed_async: bool = Field(default=True)
    embed_model: str = Field(default="text-embedding-004", max_length=80)

    @field_validator("documents")
    @classmethod
    def _v_docs(cls, v: list[DocIn]) -> list[DocIn]:
        if not v:
            raise ValueError("documents rỗng")
        return v


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════
def _check_scope(me: CurrentUser) -> None:
    """PAT must have scope 'vector', 'rag', or 'full'."""
    if me.auth_scope and not any(
        s in me.auth_scope for s in ("vector", "rag", "data", "full")
    ):
        raise HTTPException(
            status_code=403,
            detail="Token thiếu scope 'vector' hoặc 'rag'",
        )


async def _ensure_workspace(db: AsyncSession, ws: str, me: CurrentUser) -> str:
    """Resolve `ws` (id or code) → canonical workspace id, enforce access."""
    row = (await db.execute(
        text("SELECT id FROM workspaces WHERE id = :ws OR code = :ws LIMIT 1"),
        {"ws": ws},
    )).first()
    if not row:
        raise HTTPException(status_code=404, detail="Không tìm thấy workspace")
    workspace_id = row[0]
    await require_workspace_access(workspace_id, me)
    return workspace_id


def _map_business_error(e: Exception) -> HTTPException:
    msg = str(e)
    if "không tồn tại" in msg.lower() or "không tìm thấy" in msg.lower():
        return HTTPException(status_code=404, detail=msg)
    return HTTPException(status_code=400, detail=msg)


async def _resolve_collection_name(
    db: AsyncSession, workspace_id: str, collection_id: int,
) -> str:
    row = (await db.execute(
        text("""
            SELECT name FROM public.vector_collections
            WHERE id = :id AND workspace_id = :ws
        """),
        {"id": collection_id, "ws": workspace_id},
    )).first()
    if not row:
        raise HTTPException(status_code=404, detail="Collection không tồn tại")
    return row[0]


def _pipeline_row_to_dict(r: Any) -> dict[str, Any]:
    return {
        "id":              r[0],
        "workspace_id":    r[1],
        "name":            r[2],
        "description":     r[3],
        "collection_id":   r[4],
        "embedding_model": r[5],
        "rerank_model":    r[6],
        "top_k":           int(r[7]),
        "rerank_top_k":    int(r[8]),
        "hybrid_alpha":    float(r[9]),
        "namespace":       r[10],
        "system_prompt":   r[11],
        "llm_model":       r[12],
        "temperature":     float(r[13]),
        "max_tokens":      int(r[14]),
        "created_by":      str(r[15]) if r[15] else None,
        "created_at":      r[16].isoformat() if r[16] else None,
        "updated_at":      r[17].isoformat() if r[17] else None,
    }


_PIPELINE_SELECT = (
    "SELECT id, workspace_id, name, description, collection_id, embedding_model, "
    "rerank_model, top_k, rerank_top_k, hybrid_alpha, namespace, system_prompt, "
    "llm_model, temperature, max_tokens, created_by, created_at, updated_at "
    "FROM public.vector_rag_pipelines"
)


# ════════════════════════════════════════════════════════════════════════════
# 1. Hybrid Search
# ════════════════════════════════════════════════════════════════════════════
@router.post("/search/hybrid")
async def hybrid_search_endpoint(
    ws: str = Query(...),
    *,
    body: HybridSearchIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Hybrid search blending BM25 (full-text) + vector cosine similarity.
    `alpha=1.0` → pure vector; `alpha=0.0` → pure BM25; `0.5` = balanced.
    """
    workspace_id = await _ensure_workspace(db, ws, me)
    _check_scope(me)

    try:
        result = await rag_engine.hybrid_search(
            db,
            workspace_id=workspace_id,
            collection=body.collection,
            query_text=body.query,
            top_k=body.top_k,
            alpha=body.alpha,
            namespace=body.namespace,
            filter=body.filter,
            embed_model=body.embed_model,
        )
    except (rag_engine.RagError, vector_search.VectorError) as e:
        raise _map_business_error(e)
    except HTTPException:
        raise
    except Exception as e:
        log.exception("hybrid_search ws=%s coll=%s", ws, body.collection)
        raise HTTPException(status_code=502, detail=f"Hybrid search lỗi: {e}")

    cost = 0.0002 + float(result.get("embed", {}).get("cost_usd") or 0.0)
    await audit_push(
        db, actor=me.email, workspace_id=workspace_id, action="vector.search.hybrid",
        target=body.collection[:80], severity="ok",
        metadata={
            "k": body.top_k, "alpha": body.alpha,
            "matched": result.get("count", 0),
            "namespace": body.namespace,
            "embed_cache_hit": bool(result.get("embed", {}).get("cache_hit")),
        },
    )
    await billing_push(
        db, workspace_id=workspace_id, layer="L2",
        action="vector.search.hybrid", cost_usd=cost,
    )
    await db.commit()
    result["cost_usd"] = round(cost, 6)
    return result


# ════════════════════════════════════════════════════════════════════════════
# 2. Rerank
# ════════════════════════════════════════════════════════════════════════════
@router.post("/search/rerank")
async def rerank_endpoint(
    ws: str = Query(...),
    *,
    body: RerankIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Hybrid retrieve → LLM cross-rerank → top rerank_top_k."""
    workspace_id = await _ensure_workspace(db, ws, me)
    _check_scope(me)

    # 1. Hybrid retrieve
    try:
        retrieved = await rag_engine.hybrid_search(
            db,
            workspace_id=workspace_id,
            collection=body.collection,
            query_text=body.query,
            top_k=body.candidates_top_k,
            alpha=body.alpha,
            namespace=body.namespace,
            filter=body.filter,
        )
    except (rag_engine.RagError, vector_search.VectorError) as e:
        raise _map_business_error(e)
    except Exception as e:
        log.exception("rerank: hybrid retrieve failed")
        raise HTTPException(status_code=502, detail=f"Retrieve lỗi: {e}")

    candidates = retrieved.get("matches", [])

    # 2. Rerank
    try:
        rr = await rag_engine.rerank_documents(
            query=body.query,
            candidates=candidates,
            model=body.rerank_model,
            max_docs=body.candidates_top_k,
        )
    except Exception as e:
        log.exception("rerank failed")
        raise HTTPException(status_code=502, detail=f"Rerank lỗi: {e}")

    reranked = rr["reranked"][:body.rerank_top_k]
    cost = 0.0005 + float(retrieved.get("embed", {}).get("cost_usd") or 0.0)

    await audit_push(
        db, actor=me.email, workspace_id=workspace_id, action="vector.search.rerank",
        target=body.collection[:80], severity="ok",
        metadata={
            "candidates": len(candidates),
            "rerank_top_k": body.rerank_top_k,
            "rerank_model": body.rerank_model,
        },
    )
    await billing_push(
        db, workspace_id=workspace_id, layer="L2",
        action="vector.search.rerank", cost_usd=cost,
    )
    await db.commit()

    return {
        "matches": reranked,
        "count": len(reranked),
        "candidates_evaluated": len(candidates),
        "rerank_latency_ms": rr.get("rerank_latency_ms"),
        "search_latency_ms": retrieved.get("search_latency_ms"),
        "cost_usd": round(cost, 6),
    }


# ════════════════════════════════════════════════════════════════════════════
# 3. RAG Pipelines (CRUD)
# ════════════════════════════════════════════════════════════════════════════
@router.post("/rag/pipelines", status_code=201)
async def create_pipeline(
    ws: str = Query(...),
    *,
    body: PipelineIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Create RAG pipeline. Validates collection ownership."""
    workspace_id = await _ensure_workspace(db, ws, me)
    _check_scope(me)

    # Verify collection belongs to workspace
    coll = (await db.execute(
        text("""
            SELECT id, name FROM public.vector_collections
            WHERE id = :id AND workspace_id = :ws
        """),
        {"id": body.collection_id, "ws": workspace_id},
    )).first()
    if not coll:
        raise HTTPException(404, "Collection không tồn tại trong workspace")

    try:
        row = (await db.execute(
            text(f"""
                INSERT INTO public.vector_rag_pipelines
                    (workspace_id, name, description, collection_id, embedding_model,
                     rerank_model, top_k, rerank_top_k, hybrid_alpha, namespace,
                     system_prompt, llm_model, temperature, max_tokens, created_by)
                VALUES
                    (:ws, :name, :desc, :cid, :emb,
                     :rer, :tk, :rtk, :alpha, :ns,
                     :sys, :llm, :temp, :mt, :uid)
                RETURNING id, workspace_id, name, description, collection_id,
                          embedding_model, rerank_model, top_k, rerank_top_k,
                          hybrid_alpha, namespace, system_prompt, llm_model,
                          temperature, max_tokens, created_by, created_at, updated_at
            """),
            {
                "ws": workspace_id, "name": body.name, "desc": body.description,
                "cid": body.collection_id, "emb": body.embedding_model,
                "rer": body.rerank_model, "tk": body.top_k, "rtk": body.rerank_top_k,
                "alpha": body.hybrid_alpha, "ns": body.namespace,
                "sys": body.system_prompt, "llm": body.llm_model,
                "temp": body.temperature, "mt": body.max_tokens,
                "uid": me.id,
            },
        )).first()
    except Exception as e:
        await db.rollback()
        msg = str(e).lower()
        if "duplicate" in msg or "unique" in msg:
            raise HTTPException(409, "Pipeline đã tồn tại (trùng name trong workspace)")
        log.exception("create_pipeline failed")
        raise HTTPException(500, f"Lỗi tạo pipeline: {e}")

    await audit_push(
        db, actor=me.email, workspace_id=workspace_id, action="rag.pipeline.create",
        target=body.name[:80], severity="ok",
        metadata={"collection_id": body.collection_id, "alpha": body.hybrid_alpha},
    )
    await db.commit()
    return _pipeline_row_to_dict(row)


@router.get("/rag/pipelines")
async def list_pipelines(
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    workspace_id = await _ensure_workspace(db, ws, me)
    _check_scope(me)
    rows = (await db.execute(
        text(_PIPELINE_SELECT + " WHERE workspace_id = :ws ORDER BY created_at DESC"),
        {"ws": workspace_id},
    )).all()
    items = [_pipeline_row_to_dict(r) for r in rows]
    return {"workspace_id": workspace_id, "pipelines": items, "count": len(items)}


@router.get("/rag/pipelines/{pipeline_id}")
async def get_pipeline(
    pipeline_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    workspace_id = await _ensure_workspace(db, ws, me)
    _check_scope(me)
    row = (await db.execute(
        text(_PIPELINE_SELECT + " WHERE id = :id AND workspace_id = :ws"),
        {"id": pipeline_id, "ws": workspace_id},
    )).first()
    if not row:
        raise HTTPException(404, "Pipeline không tồn tại")
    return _pipeline_row_to_dict(row)


@router.patch("/rag/pipelines/{pipeline_id}")
async def update_pipeline(
    pipeline_id: int,
    ws: str = Query(...),
    *,
    body: PipelinePatch,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    workspace_id = await _ensure_workspace(db, ws, me)
    _check_scope(me)

    updates: dict[str, Any] = {}
    field_map = {
        "name": "name", "description": "description",
        "embedding_model": "embedding_model", "rerank_model": "rerank_model",
        "top_k": "top_k", "rerank_top_k": "rerank_top_k",
        "hybrid_alpha": "hybrid_alpha", "namespace": "namespace",
        "system_prompt": "system_prompt", "llm_model": "llm_model",
        "temperature": "temperature", "max_tokens": "max_tokens",
    }
    for key, col in field_map.items():
        v = getattr(body, key)
        if v is not None:
            updates[col] = v
    if not updates:
        raise HTTPException(400, "Không có field nào để cập nhật")

    set_clauses = [f"{c} = :{c}" for c in updates]
    set_clauses.append("updated_at = NOW()")
    sql = f"""
        UPDATE public.vector_rag_pipelines
        SET {", ".join(set_clauses)}
        WHERE id = :id AND workspace_id = :ws
        RETURNING id, workspace_id, name, description, collection_id,
                  embedding_model, rerank_model, top_k, rerank_top_k,
                  hybrid_alpha, namespace, system_prompt, llm_model,
                  temperature, max_tokens, created_by, created_at, updated_at
    """
    params = {"id": pipeline_id, "ws": workspace_id, **updates}
    try:
        row = (await db.execute(text(sql), params)).first()
    except Exception as e:
        await db.rollback()
        log.exception("update_pipeline failed")
        raise HTTPException(500, f"Lỗi cập nhật pipeline: {e}")
    if not row:
        raise HTTPException(404, "Pipeline không tồn tại")

    await audit_push(
        db, actor=me.email, workspace_id=workspace_id, action="rag.pipeline.update",
        target=f"pipeline:{pipeline_id}", severity="ok",
        metadata={"fields": list(updates.keys())},
    )
    await db.commit()
    return _pipeline_row_to_dict(row)


@router.delete("/rag/pipelines/{pipeline_id}")
async def delete_pipeline(
    pipeline_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    workspace_id = await _ensure_workspace(db, ws, me)
    _check_scope(me)
    res = await db.execute(
        text("""
            DELETE FROM public.vector_rag_pipelines
            WHERE id = :id AND workspace_id = :ws
        """),
        {"id": pipeline_id, "ws": workspace_id},
    )
    if (res.rowcount or 0) == 0:
        raise HTTPException(404, "Pipeline không tồn tại")
    await audit_push(
        db, actor=me.email, workspace_id=workspace_id, action="rag.pipeline.delete",
        target=f"pipeline:{pipeline_id}", severity="warn", metadata={},
    )
    await db.commit()
    return {"ok": True, "deleted_id": pipeline_id}


# ════════════════════════════════════════════════════════════════════════════
# 4. RAG Query Execution
# ════════════════════════════════════════════════════════════════════════════
@router.post("/rag/query")
async def rag_query(
    ws: str = Query(...),
    *,
    body: RagQueryIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Execute pipeline: retrieve → rerank → LLM answer with [doc_id] citations."""
    workspace_id = await _ensure_workspace(db, ws, me)
    _check_scope(me)

    pipeline = (await db.execute(
        text(_PIPELINE_SELECT + " WHERE id = :id AND workspace_id = :ws"),
        {"id": body.pipeline_id, "ws": workspace_id},
    )).first()
    if not pipeline:
        raise HTTPException(404, "Pipeline không tồn tại trong workspace")
    pipe_dict = _pipeline_row_to_dict(pipeline)

    history = None
    if body.conversation_history:
        history = [{"role": t.role, "content": t.content} for t in body.conversation_history]

    try:
        result = await rag_engine.run_rag_pipeline(
            db,
            pipeline=pipe_dict,
            query=body.query,
            conversation_history=history,
            workspace_id=workspace_id,
            actor=me.email,
        )
    except rag_engine.RagError as e:
        raise _map_business_error(e)
    except HTTPException:
        raise
    except Exception as e:
        log.exception("rag_query failed pipeline=%s", body.pipeline_id)
        raise HTTPException(status_code=502, detail=f"RAG query lỗi: {e}")

    cost = float(result.get("cost_usd") or 0.0)
    await audit_push(
        db, actor=me.email, workspace_id=workspace_id, action="rag.query",
        target=f"pipeline:{body.pipeline_id}", severity="ok",
        metadata={
            "latency_ms": result.get("latency_ms"),
            "chunks": len(result.get("chunks", [])),
            "citations": len(result.get("citations", [])),
        },
    )
    await billing_push(
        db, workspace_id=workspace_id, layer="L2",
        action="rag.query", cost_usd=cost,
    )
    await db.commit()

    result["pipeline_id"] = body.pipeline_id
    return result


@router.get("/rag/queries")
async def list_rag_queries(
    ws: str = Query(...),
    pipeline_id: int | None = Query(default=None, ge=1),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Recent RAG queries for analytics. Filter by pipeline + time window."""
    workspace_id = await _ensure_workspace(db, ws, me)
    _check_scope(me)

    where = ["workspace_id = :ws"]
    params: dict[str, Any] = {"ws": workspace_id, "limit": limit}
    if pipeline_id is not None:
        where.append("pipeline_id = :pid")
        params["pid"] = pipeline_id
    if from_:
        try:
            params["from"] = datetime.fromisoformat(from_)
        except ValueError:
            raise HTTPException(400, "from phải là ISO 8601 datetime")
        where.append("created_at >= :from")
    if to:
        try:
            params["to"] = datetime.fromisoformat(to)
        except ValueError:
            raise HTTPException(400, "to phải là ISO 8601 datetime")
        where.append("created_at <= :to")

    sql = f"""
        SELECT id, pipeline_id, collection_id, actor,
               query_text, retrieved_chunks, rerank_scores,
               final_answer, citations,
               latency_ms, embed_latency_ms, retrieve_latency_ms,
               rerank_latency_ms, llm_latency_ms,
               cost_usd, error, created_at
        FROM public.vector_rag_queries
        WHERE {" AND ".join(where)}
        ORDER BY created_at DESC
        LIMIT :limit
    """
    rows = (await db.execute(text(sql), params)).all()
    items = [
        {
            "id":                  r[0],
            "pipeline_id":         r[1],
            "collection_id":       r[2],
            "actor":               r[3],
            "query_text":          r[4],
            "retrieved_chunks":    r[5] or [],
            "rerank_scores":       r[6] or [],
            "final_answer":        r[7] or "",
            "citations":           r[8] or [],
            "latency_ms":          int(r[9] or 0),
            "embed_latency_ms":    int(r[10] or 0),
            "retrieve_latency_ms": int(r[11] or 0),
            "rerank_latency_ms":   int(r[12] or 0),
            "llm_latency_ms":      int(r[13] or 0),
            "cost_usd":            float(r[14] or 0),
            "error":               r[15],
            "created_at":          r[16].isoformat() if r[16] else None,
        }
        for r in rows
    ]

    # Aggregate stats (same WHERE; LIMIT not part of where clauses)
    agg_params = {k: v for k, v in params.items() if k != "limit"}
    agg_row = (await db.execute(
        text(f"""
            SELECT COUNT(*), COALESCE(AVG(latency_ms), 0)::INT,
                   COALESCE(SUM(cost_usd), 0)::NUMERIC(10,6),
                   COUNT(*) FILTER (WHERE error IS NOT NULL)
            FROM public.vector_rag_queries
            WHERE {" AND ".join(where)}
        """),
        agg_params,
    )).first()
    stats = {
        "total":           int(agg_row[0]) if agg_row else 0,
        "avg_latency_ms":  int(agg_row[1]) if agg_row else 0,
        "total_cost_usd":  float(agg_row[2]) if agg_row else 0.0,
        "errors":          int(agg_row[3]) if agg_row else 0,
    }
    return {"items": items, "count": len(items), "stats": stats}


# ════════════════════════════════════════════════════════════════════════════
# 5. Premium Batch Upsert
# ════════════════════════════════════════════════════════════════════════════
@router.post("/upsert/batch")
async def upsert_batch(
    ws: str = Query(...),
    *,
    body: BatchUpsertIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Async batch upsert with embedding cache. Each doc may include `text`, `id?`,
    `metadata?`, `namespace?`. Embeddings auto-generated via Vertex AI (cached).
    """
    workspace_id = await _ensure_workspace(db, ws, me)
    _check_scope(me)

    documents = [
        {
            "text": d.text,
            "id": d.id,
            "metadata": d.metadata or {},
            "namespace": d.namespace or "default",
        }
        for d in body.documents
    ]
    try:
        result = await rag_engine.upsert_documents(
            db,
            workspace_id=workspace_id,
            collection=body.collection,
            documents=documents,
            batch_size=body.batch_size,
            embed_async=body.embed_async,
            embed_model=body.embed_model,
        )
    except (rag_engine.RagError, vector_search.VectorError) as e:
        raise _map_business_error(e)
    except HTTPException:
        raise
    except Exception as e:
        log.exception("upsert_batch ws=%s coll=%s", ws, body.collection)
        raise HTTPException(status_code=502, detail=f"Batch upsert lỗi: {e}")

    n = result.get("upserted", 0)
    embed_cost = float(result.get("embed", {}).get("cost_usd") or 0.0)
    storage_cost = 0.0001 * n  # consistent with vector module pricing
    cost = round(embed_cost + storage_cost, 6)

    await audit_push(
        db, actor=me.email, workspace_id=workspace_id, action="vector.upsert.batch",
        target=body.collection[:80], severity="ok",
        metadata={
            "count": n,
            "row_count": result.get("row_count"),
            "cache_hits": result.get("embed", {}).get("cache_hits"),
        },
    )
    await billing_push(
        db, workspace_id=workspace_id, layer="L2",
        action="vector.upsert.batch", cost_usd=cost,
    )
    await db.commit()
    result["cost_usd"] = cost
    return result


# ════════════════════════════════════════════════════════════════════════════
# 6. Collection Stats
# ════════════════════════════════════════════════════════════════════════════
@router.get("/collections/{collection_id}/stats")
async def collection_stats_endpoint(
    collection_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Per-namespace breakdown, doc count, last update, premium status."""
    workspace_id = await _ensure_workspace(db, ws, me)
    _check_scope(me)
    name = await _resolve_collection_name(db, workspace_id, collection_id)

    try:
        stats = await rag_engine.collection_stats(
            db, workspace_id=workspace_id, collection=name,
        )
    except (rag_engine.RagError, vector_search.VectorError) as e:
        raise _map_business_error(e)
    except Exception as e:
        log.exception("collection_stats failed")
        raise HTTPException(502, f"Stats lỗi: {e}")

    # Recent RAG query stats for this collection (last 30 days)
    rag_agg = (await db.execute(
        text("""
            SELECT COUNT(*) AS qcount,
                   COALESCE(AVG(latency_ms), 0)::INT AS avg_lat,
                   COALESCE(SUM(cost_usd), 0)::NUMERIC(10,6) AS cost
            FROM public.vector_rag_queries
            WHERE workspace_id = :ws
              AND collection_id = :cid
              AND created_at > NOW() - INTERVAL '30 days'
        """),
        {"ws": workspace_id, "cid": collection_id},
    )).first()
    stats["rag_30d"] = {
        "queries":        int(rag_agg[0]) if rag_agg else 0,
        "avg_latency_ms": int(rag_agg[1]) if rag_agg else 0,
        "cost_usd":       float(rag_agg[2]) if rag_agg else 0.0,
    }
    stats["collection_id"] = collection_id
    return stats


# ════════════════════════════════════════════════════════════════════════════
# 7. Embedding Cache Stats (utility)
# ════════════════════════════════════════════════════════════════════════════
@router.get("/cache/stats")
async def cache_stats(
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Embedding cache hit-rate analytics (global, not workspace-scoped)."""
    await _ensure_workspace(db, ws, me)
    _check_scope(me)
    row = (await db.execute(
        text("""
            SELECT COUNT(*) AS total_entries,
                   COALESCE(SUM(hit_count), 0) AS total_hits,
                   COUNT(*) FILTER (WHERE last_hit_at > NOW() - INTERVAL '7 days') AS active_7d,
                   COALESCE(MAX(last_hit_at), NULL) AS last_hit
            FROM public.vector_embedding_cache
        """)
    )).first()
    return {
        "entries":      int(row[0]) if row else 0,
        "total_hits":   int(row[1]) if row else 0,
        "active_7d":    int(row[2]) if row else 0,
        "last_hit_at":  row[3].isoformat() if row and row[3] else None,
    }
