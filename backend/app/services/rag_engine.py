"""
Zeni Cloud Core — RAG Engine (Vector DB Premium service layer).

Builds on Sprint A2 (`app/services/vector_search.py` per-collection tables) and
adds:
  - embed_with_cache()       — sha256-keyed embedding cache to avoid recompute
  - hybrid_search()          — BM25 (ts_rank) ⊕ vector cosine, alpha-blended
  - rerank_documents()       — LLM-as-reranker (Gemini multi-doc relevance scoring)
  - run_rag_pipeline()       — full pipeline: retrieve → rerank → LLM answer + citations
  - upsert_documents()       — premium-aware upsert with embedding cache + content_tsv

All methods are async, use SQLAlchemy `text()` with named params for DML,
sanitize identifiers via vector_search._table_name() (whitelisted in Sprint A2),
and avoid touching `main.py`.

Cosine distance via pgvector `<=>` operator (smaller = closer; sim = 1 - dist).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import ai_core, vector_search

log = logging.getLogger("zeni.rag_engine")

# ─── Constants ──────────────────────────────────────────────────────────────
_DEFAULT_EMBED_MODEL = "text-embedding-004"
_DEFAULT_EMBED_DIM = 768
_MAX_BATCH_DOCS = 200            # cap docs per batch (Vertex AI limit ~250)
_MAX_HYBRID_K = 100
_MAX_RERANK_K = 50
_NAMESPACE_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")

# Pricing (rough USD)
_COST_EMBED_PER_1K_TOKENS = 0.000025   # text-embedding-004
_COST_LLM_INPUT_PER_1K = 0.0001        # gemini-2.5-flash input
_COST_LLM_OUTPUT_PER_1K = 0.0004       # gemini-2.5-flash output
_COST_RERANK_FIXED = 0.0002            # per rerank call


class RagError(ValueError):
    """Business error for RAG engine — caller maps to HTTPException 400/404."""


# ─── Helpers ────────────────────────────────────────────────────────────────
def _sha256_hex(text_in: str, model: str) -> str:
    """Stable hash for cache key. Normalize whitespace, include model id."""
    norm = " ".join((text_in or "").split()).lower()
    h = hashlib.sha256(f"{model}::{norm}".encode("utf-8")).hexdigest()
    return h


def _validate_namespace(ns: str) -> str:
    if not isinstance(ns, str) or not _NAMESPACE_RE.match(ns):
        raise RagError("Namespace không hợp lệ (1-64 ký tự, [a-zA-Z0-9_-])")
    return ns


def _vector_literal(vec: list[float]) -> str:
    """Reuse same serialization as vector_search to avoid format drift."""
    return vector_search._vector_literal(vec)  # type: ignore[attr-defined]


async def _ensure_premium(db: AsyncSession, workspace_id: str, collection: str) -> dict[str, Any]:
    """Fetch collection meta and ensure premium hybrid columns exist (idempotent)."""
    meta = await vector_search._get_collection_meta(db, workspace_id, collection)  # type: ignore[attr-defined]
    table = meta["table_name"]
    if not meta.get("table_name"):
        raise RagError("Collection thiếu table_name (corrupt registry)")

    # Check premium flag on registry; if false, call helper function (commits inside).
    flag = (await db.execute(
        text("SELECT premium_enabled FROM public.vector_collections WHERE id = :id"),
        {"id": meta["id"]},
    )).scalar()
    if not flag:
        try:
            await db.execute(
                text("SELECT public.vector_enable_hybrid(:t)"),
                {"t": table},
            )
            await db.commit()
        except Exception:
            await db.rollback()
            log.exception("vector_enable_hybrid failed for %s", table)
            raise
    return meta


# ─── 1. EMBEDDING CACHE ─────────────────────────────────────────────────────
async def embed_with_cache(
    db: AsyncSession,
    *,
    text_in: str,
    model: str = _DEFAULT_EMBED_MODEL,
    task_type: str = "RETRIEVAL_DOCUMENT",
    save_cache: bool = True,
) -> dict[str, Any]:
    """
    Embed text using cache. Returns {vector, dim, cache_hit, cost_usd, latency_ms}.

    Cache key = sha256(lower(normalized_text) + '::' + model). Misses call
    ai_core.embed_text() and upsert into vector_embedding_cache. Hit count is
    incremented atomically for analytics.
    """
    if not isinstance(text_in, str) or not text_in.strip():
        raise RagError("text rỗng")

    h = _sha256_hex(text_in, model)
    started = time.perf_counter()

    # 1. Try cache hit
    row = (await db.execute(
        text("""
            UPDATE public.vector_embedding_cache
            SET hit_count = hit_count + 1, last_hit_at = NOW()
            WHERE text_hash = :h AND embedding_model = :m
            RETURNING embedding, dim
        """),
        {"h": h, "m": model},
    )).first()
    if row is not None:
        emb_str, dim = row[0], row[1]
        # pgvector returns string '[v1,v2,...]'; parse to list
        if isinstance(emb_str, str):
            try:
                vec = json.loads(emb_str)
            except json.JSONDecodeError:
                # pgvector format `[v1,v2]` is JSON-compatible; failure means corrupt
                vec = None
        else:
            vec = list(emb_str) if emb_str is not None else None
        if vec and len(vec) == int(dim):
            await db.commit()
            return {
                "vector": [float(x) for x in vec],
                "dim": int(dim),
                "cache_hit": True,
                "cost_usd": 0.0,
                "latency_ms": int((time.perf_counter() - started) * 1000),
            }

    # 2. Cache miss → call Vertex AI
    try:
        result = await ai_core.embed_text(
            texts=[text_in], model=model, task_type=task_type,
        )
    except Exception as e:
        log.exception("embed_with_cache: ai_core.embed_text failed")
        raise RagError(f"Embedding lỗi: {e}") from e

    if not result.get("embeddings"):
        raise RagError("Embedding rỗng từ Vertex AI")
    item = result["embeddings"][0]
    vec = list(item["vector"])
    dim = int(result.get("dimensions") or len(vec))
    tokens = int(item.get("tokens") or 0)
    cost = (tokens / 1000.0) * _COST_EMBED_PER_1K_TOKENS

    # 3. Upsert cache (best-effort; failure shouldn't block caller)
    if save_cache:
        try:
            await db.execute(
                text("""
                    INSERT INTO public.vector_embedding_cache
                        (text_hash, embedding_model, dim, embedding, text_preview, hit_count, last_hit_at)
                    VALUES (:h, :m, :d, CAST(:v AS vector), :pv, 0, NOW())
                    ON CONFLICT (text_hash) DO NOTHING
                """),
                {
                    "h": h, "m": model, "d": dim,
                    "v": _vector_literal(vec),
                    "pv": text_in[:200],
                },
            )
            await db.commit()
        except Exception:
            await db.rollback()
            log.warning("embedding cache upsert skipped (non-fatal)")

    return {
        "vector": vec,
        "dim": dim,
        "cache_hit": False,
        "cost_usd": cost,
        "tokens": tokens,
        "latency_ms": int((time.perf_counter() - started) * 1000),
    }


# ─── 2. HYBRID SEARCH (BM25 + vector) ───────────────────────────────────────
async def hybrid_search(
    db: AsyncSession,
    *,
    workspace_id: str,
    collection: str,
    query_text: str,
    query_vector: list[float] | None = None,
    top_k: int = 10,
    alpha: float = 0.5,
    namespace: str | None = None,
    filter: dict[str, Any] | None = None,
    embed_model: str = _DEFAULT_EMBED_MODEL,
) -> dict[str, Any]:
    """
    Hybrid search combining BM25 (ts_rank) and vector cosine similarity.

    score = alpha * vec_sim + (1 - alpha) * bm25_norm
      vec_sim   = 1 - (embedding <=> query_vec)
      bm25_norm = ts_rank(content_tsv, plainto_tsquery(query_text))

    If query_vector is None, embeds query_text via embed_with_cache().
    """
    if not (1 <= int(top_k) <= _MAX_HYBRID_K):
        raise RagError(f"top_k phải từ 1-{_MAX_HYBRID_K}")
    if not (0.0 <= float(alpha) <= 1.0):
        raise RagError("alpha phải nằm trong [0,1]")
    if not isinstance(query_text, str) or not query_text.strip():
        raise RagError("query_text rỗng")

    meta = await _ensure_premium(db, workspace_id, collection)
    table = meta["table_name"]
    expected_dim = int(meta["dim"])

    # Embed query (cache-aware) if vector not provided.
    embed_info: dict[str, Any] = {"cache_hit": False, "cost_usd": 0.0, "latency_ms": 0}
    if query_vector is None:
        emb = await embed_with_cache(
            db, text_in=query_text, model=embed_model, task_type="RETRIEVAL_QUERY",
        )
        query_vector = emb["vector"]
        embed_info = {
            "cache_hit": emb["cache_hit"],
            "cost_usd": emb["cost_usd"],
            "latency_ms": emb["latency_ms"],
        }
    if len(query_vector) != expected_dim:
        raise RagError(f"Query vector phải có {expected_dim} chiều")
    qvec_lit = _vector_literal(query_vector)

    # Build WHERE clause (namespace + JSONB filter)
    where_clauses: list[str] = []
    params: dict[str, Any] = {
        "qvec": qvec_lit,
        "qtext": query_text,
        "alpha": float(alpha),
        "k": int(top_k),
    }
    if namespace:
        _validate_namespace(namespace)
        where_clauses.append("namespace = :ns")
        params["ns"] = namespace
    if filter:
        if not isinstance(filter, dict):
            raise RagError("filter phải là object JSON")
        if filter:
            where_clauses.append("metadata @> CAST(:flt AS jsonb)")
            params["flt"] = json.dumps(filter, ensure_ascii=False)
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    # Hybrid SQL: compute both scores per row, blend by alpha.
    # ts_rank returns 0..~1; cosine sim = 1 - distance.
    sql = f'''
        WITH scored AS (
            SELECT
                id,
                COALESCE(content, '') AS content,
                COALESCE(metadata, '{{}}'::jsonb) AS metadata,
                COALESCE(namespace, 'default') AS namespace,
                1 - (vector <=> CAST(:qvec AS vector)) AS vec_sim,
                CASE
                    WHEN content_tsv IS NULL THEN 0
                    ELSE ts_rank(content_tsv, plainto_tsquery('simple', :qtext))
                END AS bm25_score
            FROM public."{table}"
            {where_sql}
        )
        SELECT
            id,
            content,
            metadata,
            namespace,
            vec_sim,
            bm25_score,
            (:alpha * vec_sim + (1 - :alpha) * bm25_score) AS hybrid_score
        FROM scored
        ORDER BY hybrid_score DESC
        LIMIT :k
    '''

    started = time.perf_counter()
    try:
        rows = (await db.execute(text(sql), params)).all()
    except Exception:
        await db.rollback()
        log.exception("hybrid_search SQL failed ws=%s coll=%s", workspace_id, collection)
        raise

    matches = []
    for r in rows:
        matches.append({
            "id":            r[0],
            "content":       r[1] or "",
            "metadata":      r[2] or {},
            "namespace":     r[3],
            "vec_sim":       float(r[4] or 0),
            "bm25_score":    float(r[5] or 0),
            "hybrid_score":  float(r[6] or 0),
        })

    return {
        "matches": matches,
        "count": len(matches),
        "alpha": float(alpha),
        "embed": embed_info,
        "search_latency_ms": int((time.perf_counter() - started) * 1000),
    }


# ─── 3. RERANK (LLM-as-reranker) ────────────────────────────────────────────
_RERANK_PROMPT = (
    "You are a relevance scorer. Given a query and a list of documents, "
    "rate each document's relevance to the query on a scale 0-10 (10 = perfect "
    "answer, 0 = completely irrelevant). Return ONLY a JSON array of integers "
    "matching the input order, no prose, no markdown.\n\n"
    "Query: {query}\n\nDocuments:\n{docs}\n\n"
    "Return JSON array now (e.g. [8,3,9,...]):"
)


async def rerank_documents(
    *,
    query: str,
    candidates: list[dict[str, Any]],
    model: str = "gemini-2.5-flash",
    max_docs: int = _MAX_RERANK_K,
) -> dict[str, Any]:
    """
    Rerank candidates via LLM cross-scoring. Returns candidates sorted desc by
    rerank_score plus latency/cost.

    Each candidate must have at least {"id", "content"} keys.
    """
    if not candidates:
        return {"reranked": [], "rerank_latency_ms": 0, "cost_usd": 0.0}

    cands = candidates[:max_docs]
    docs_block = "\n".join(
        f"[{i}] {((c.get('content') or '')[:600]).replace(chr(10), ' ')}"
        for i, c in enumerate(cands)
    )
    prompt = _RERANK_PROMPT.format(query=query, docs=docs_block)

    started = time.perf_counter()
    # Call Gemini via existing ai_core stream_complete; we just need the joined text.
    chunks: list[str] = []
    try:
        async for piece in ai_core.stream_complete(
            prompt=prompt, model=model, temperature=0.0, max_tokens=256,
        ):
            chunks.append(piece)
    except Exception as e:
        log.exception("rerank LLM call failed")
        # Graceful fallback: keep original order with neutral scores.
        return {
            "reranked": [{**c, "rerank_score": 5.0} for c in cands],
            "rerank_latency_ms": int((time.perf_counter() - started) * 1000),
            "cost_usd": 0.0,
            "error": f"Rerank fallback (LLM error): {e}",
        }
    raw = "".join(chunks).strip()
    # Extract JSON array (model may pad with whitespace/markdown despite prompt).
    m = re.search(r"\[\s*[\d\.,\s\-]+\]", raw)
    scores: list[float] = []
    if m:
        try:
            parsed = json.loads(m.group(0))
            if isinstance(parsed, list):
                scores = [float(x) for x in parsed]
        except (ValueError, TypeError):
            scores = []
    # If parsing failed or length mismatch → neutral fallback.
    if len(scores) != len(cands):
        log.warning("rerank parse mismatch (got %d, expected %d) raw=%s",
                    len(scores), len(cands), raw[:200])
        scores = [5.0] * len(cands)

    reranked = [
        {**c, "rerank_score": float(scores[i])}
        for i, c in enumerate(cands)
    ]
    reranked.sort(key=lambda x: x["rerank_score"], reverse=True)

    return {
        "reranked": reranked,
        "rerank_latency_ms": int((time.perf_counter() - started) * 1000),
        "cost_usd": _COST_RERANK_FIXED,
    }


# ─── 4. RAG PIPELINE EXECUTION ──────────────────────────────────────────────
_RAG_DEFAULT_SYSTEM = (
    "You are a precise RAG assistant. Answer the user's question using ONLY "
    "the provided context. Cite sources inline as [doc_id]. If the context "
    "is insufficient, say so plainly. Never fabricate citations."
)


def _format_context(chunks: list[dict[str, Any]]) -> str:
    """Format retrieved chunks as numbered context blocks with [doc_id] tags."""
    out = []
    for c in chunks:
        cid = c.get("id", "?")
        body = (c.get("content") or "")[:1500]
        out.append(f"[{cid}]\n{body}")
    return "\n\n---\n\n".join(out)


async def run_rag_pipeline(
    db: AsyncSession,
    *,
    pipeline: dict[str, Any],
    query: str,
    conversation_history: list[dict[str, str]] | None = None,
    workspace_id: str,
    actor: str | None = None,
) -> dict[str, Any]:
    """
    Execute full RAG pipeline:
      1. Hybrid retrieve (top_k from pipeline.top_k)
      2. Rerank (LLM, narrow to pipeline.rerank_top_k)
      3. Format context with [doc_id] citations
      4. Call Gemini for final answer
      5. Log to vector_rag_queries

    `pipeline` is a dict shaped like vector_rag_pipelines row.
    Returns {answer, citations, chunks, latency_ms, cost_usd, ...}.
    """
    if not isinstance(query, str) or not query.strip():
        raise RagError("query rỗng")

    pipeline_id = pipeline.get("id")
    collection_id = pipeline.get("collection_id")
    if not collection_id:
        raise RagError("Pipeline thiếu collection_id")

    # Resolve collection name from id (registry table is whitelisted by sanitization)
    meta_row = (await db.execute(
        text("SELECT name FROM public.vector_collections WHERE id = :id AND workspace_id = :ws"),
        {"id": collection_id, "ws": workspace_id},
    )).first()
    if not meta_row:
        raise RagError("Collection của pipeline không tồn tại trong workspace")
    coll_name = meta_row[0]

    top_k = int(pipeline.get("top_k") or 5)
    rerank_top_k = int(pipeline.get("rerank_top_k") or 3)
    alpha = float(pipeline.get("hybrid_alpha") or 0.5)
    embed_model = pipeline.get("embedding_model") or _DEFAULT_EMBED_MODEL
    llm_model = pipeline.get("llm_model") or "gemini-2.5-flash"
    rerank_model = pipeline.get("rerank_model") or llm_model
    namespace = pipeline.get("namespace") or None
    sys_prompt = pipeline.get("system_prompt") or _RAG_DEFAULT_SYSTEM
    temperature = float(pipeline.get("temperature") or 0.4)
    max_tokens = int(pipeline.get("max_tokens") or 1024)

    overall_start = time.perf_counter()
    cost_total = 0.0
    error: str | None = None
    chunks: list[dict[str, Any]] = []
    reranked: list[dict[str, Any]] = []
    answer = ""
    citations: list[dict[str, Any]] = []
    # Pre-init latency counters so failure path always has them.
    embed_latency_ms = 0
    retrieve_latency_ms = 0
    rerank_latency_ms = 0
    llm_latency_ms = 0

    try:
        # --- 1. Retrieve ---
        retrieve_start = time.perf_counter()
        retrieved = await hybrid_search(
            db,
            workspace_id=workspace_id,
            collection=coll_name,
            query_text=query,
            top_k=top_k,
            alpha=alpha,
            namespace=namespace,
            embed_model=embed_model,
        )
        chunks = retrieved.get("matches", [])
        cost_total += float(retrieved.get("embed", {}).get("cost_usd") or 0.0)
        retrieve_latency_ms = int((time.perf_counter() - retrieve_start) * 1000)
        embed_latency_ms = int(retrieved.get("embed", {}).get("latency_ms") or 0)

        # --- 2. Rerank (only if rerank_model set or rerank_top_k < top_k) ---
        if pipeline.get("rerank_model") or rerank_top_k < top_k:
            rr = await rerank_documents(
                query=query,
                candidates=chunks,
                model=rerank_model,
                max_docs=top_k,
            )
            reranked = rr["reranked"][:rerank_top_k]
            cost_total += float(rr.get("cost_usd") or 0.0)
            rerank_latency_ms = int(rr.get("rerank_latency_ms") or 0)
        else:
            reranked = chunks[:rerank_top_k]
            rerank_latency_ms = 0

        # --- 3. Build context + call LLM ---
        context_block = _format_context(reranked)

        # Inject conversation history if provided.
        history_block = ""
        if conversation_history:
            for turn in conversation_history[-6:]:  # last 6 turns max
                role = (turn.get("role") or "").strip()
                content = (turn.get("content") or "").strip()
                if not role or not content:
                    continue
                history_block += f"\n{role.upper()}: {content[:500]}"

        full_prompt = (
            f"CONTEXT:\n{context_block}\n\n"
            f"{('CONVERSATION HISTORY:' + history_block) if history_block else ''}\n\n"
            f"USER QUESTION: {query}\n\n"
            f"Answer using only the context. Cite [doc_id] inline."
        )

        llm_start = time.perf_counter()
        chunks_out: list[str] = []
        async for piece in ai_core.stream_complete(
            prompt=full_prompt,
            model=llm_model,
            system=sys_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        ):
            chunks_out.append(piece)
        answer = "".join(chunks_out).strip()
        llm_latency_ms = int((time.perf_counter() - llm_start) * 1000)

        # Cost approximation: input ~ prompt+context (~ char/4), output ~ answer
        in_tokens_est = max(1, len(full_prompt) // 4)
        out_tokens_est = max(1, len(answer) // 4)
        cost_total += (in_tokens_est / 1000.0) * _COST_LLM_INPUT_PER_1K
        cost_total += (out_tokens_est / 1000.0) * _COST_LLM_OUTPUT_PER_1K

        # --- 4. Extract citations actually mentioned in answer ---
        cited_ids = set(re.findall(r"\[([A-Za-z0-9_\-:.]{1,256})\]", answer))
        for c in reranked:
            cid = str(c.get("id"))
            if cid in cited_ids:
                citations.append({
                    "doc_id": cid,
                    "snippet": (c.get("content") or "")[:240],
                    "score": c.get("rerank_score") or c.get("hybrid_score"),
                })

    except RagError:
        raise
    except Exception as e:
        error = str(e)
        log.exception("run_rag_pipeline failed pipeline_id=%s", pipeline_id)

    total_latency_ms = int((time.perf_counter() - overall_start) * 1000)

    # --- 5. Audit log ---
    try:
        await db.execute(
            text("""
                INSERT INTO public.vector_rag_queries
                    (workspace_id, pipeline_id, collection_id, actor,
                     query_text, retrieved_chunks, rerank_scores,
                     final_answer, citations, latency_ms,
                     embed_latency_ms, retrieve_latency_ms,
                     rerank_latency_ms, llm_latency_ms,
                     cost_usd, error)
                VALUES
                    (:ws, :pid, :cid, :actor,
                     :q, CAST(:retrieved AS jsonb), CAST(:rerank AS jsonb),
                     :answer, CAST(:cite AS jsonb), :lat,
                     :emb_lat, :ret_lat, :rer_lat, :llm_lat,
                     :cost, :err)
            """),
            {
                "ws": workspace_id,
                "pid": pipeline_id,
                "cid": collection_id,
                "actor": (actor or "")[:255] or None,
                "q": query[:8000],
                "retrieved": json.dumps(
                    [
                        {
                            "id": c.get("id"),
                            "score": c.get("hybrid_score"),
                            "namespace": c.get("namespace"),
                            "excerpt": (c.get("content") or "")[:240],
                        }
                        for c in chunks
                    ],
                    ensure_ascii=False,
                ),
                "rerank": json.dumps(
                    [{"id": c.get("id"), "score": c.get("rerank_score")} for c in reranked],
                    ensure_ascii=False,
                ),
                "answer": (answer or "")[:8000],
                "cite": json.dumps(citations, ensure_ascii=False),
                "lat": total_latency_ms,
                "emb_lat": embed_latency_ms,
                "ret_lat": retrieve_latency_ms,
                "rer_lat": rerank_latency_ms,
                "llm_lat": llm_latency_ms,
                "cost": round(cost_total, 6),
                "err": error,
            },
        )
        await db.commit()
    except Exception:
        await db.rollback()
        log.exception("rag query log insert failed (non-fatal)")

    if error:
        raise RagError(f"RAG pipeline failed: {error}")

    return {
        "answer": answer,
        "citations": citations,
        "chunks": [
            {
                "id": c.get("id"),
                "score": c.get("rerank_score") or c.get("hybrid_score"),
                "namespace": c.get("namespace"),
                "excerpt": (c.get("content") or "")[:240],
            }
            for c in reranked
        ],
        "latency_ms": total_latency_ms,
        "cost_usd": round(cost_total, 6),
    }


# ─── 5. PREMIUM-AWARE BATCH UPSERT ──────────────────────────────────────────
async def upsert_documents(
    db: AsyncSession,
    *,
    workspace_id: str,
    collection: str,
    documents: list[dict[str, Any]],
    batch_size: int = 50,
    embed_async: bool = True,
    embed_model: str = _DEFAULT_EMBED_MODEL,
) -> dict[str, Any]:
    """
    Premium upsert with embedding + content_tsv (BM25). Each document:
        {text, id?, metadata?, namespace?}
    If embedding not provided, we embed via cache (Vertex AI). If `id` not
    provided, we hash content+namespace to get deterministic id.
    """
    if not isinstance(documents, list) or not documents:
        raise RagError("documents rỗng")
    if len(documents) > _MAX_BATCH_DOCS:
        raise RagError(f"Quá nhiều docs (max {_MAX_BATCH_DOCS}/request)")

    meta = await _ensure_premium(db, workspace_id, collection)
    table = meta["table_name"]
    expected_dim = int(meta["dim"])

    # Step 1. Resolve embeddings (with cache).
    started = time.perf_counter()
    cost_embed = 0.0
    cache_hits = 0
    pending: list[tuple[int, str]] = []   # (idx, text) for cache miss
    embeddings: dict[int, list[float]] = {}

    for i, d in enumerate(documents):
        if not isinstance(d, dict):
            raise RagError(f"Doc #{i} không phải object")
        txt = d.get("text") or d.get("content")
        if not isinstance(txt, str) or not txt.strip():
            raise RagError(f"Doc #{i} thiếu text")
        if d.get("vector"):
            v = d["vector"]
            if len(v) != expected_dim:
                raise RagError(f"Doc #{i} vector sai chiều (expected {expected_dim})")
            embeddings[i] = [float(x) for x in v]
            continue
        pending.append((i, txt))

    # Try cache first (single round-trip).
    if pending:
        hashes = [_sha256_hex(t, embed_model) for _, t in pending]
        if hashes:
            cache_rows = (await db.execute(
                text("""
                    UPDATE public.vector_embedding_cache
                    SET hit_count = hit_count + 1, last_hit_at = NOW()
                    WHERE text_hash = ANY(:hs) AND embedding_model = :m
                    RETURNING text_hash, embedding, dim
                """),
                {"hs": hashes, "m": embed_model},
            )).all()
            cache_map: dict[str, list[float]] = {}
            for r in cache_rows:
                emb_str, dim = r[1], r[2]
                if isinstance(emb_str, str):
                    try:
                        cache_map[r[0]] = list(json.loads(emb_str))
                    except json.JSONDecodeError:
                        pass
                elif emb_str is not None:
                    cache_map[r[0]] = list(emb_str)
            await db.commit()

            still_pending: list[tuple[int, str]] = []
            for (idx, txt), h in zip(pending, hashes):
                if h in cache_map and len(cache_map[h]) == expected_dim:
                    embeddings[idx] = cache_map[h]
                    cache_hits += 1
                else:
                    still_pending.append((idx, txt))
            pending = still_pending

    # Cache miss → batch embed via Vertex AI.
    if pending:
        # Vertex caps ~250/request — chunk just in case.
        chunk = 100
        for start in range(0, len(pending), chunk):
            sub = pending[start:start + chunk]
            try:
                result = await ai_core.embed_text(
                    texts=[t for _, t in sub],
                    model=embed_model,
                    task_type="RETRIEVAL_DOCUMENT",
                )
            except Exception as e:
                log.exception("upsert embed batch failed")
                raise RagError(f"Embedding batch lỗi: {e}") from e
            embs = result.get("embeddings", [])
            for (idx, txt), item in zip(sub, embs):
                vec = list(item["vector"])
                if len(vec) != expected_dim:
                    raise RagError(
                        f"Vertex AI trả vector dim={len(vec)} (expected {expected_dim}). "
                        "Collection dim khác model dim?"
                    )
                embeddings[idx] = vec
                tokens = int(item.get("tokens") or 0)
                cost_embed += (tokens / 1000.0) * _COST_EMBED_PER_1K_TOKENS
                # Best-effort cache upsert
                try:
                    await db.execute(
                        text("""
                            INSERT INTO public.vector_embedding_cache
                                (text_hash, embedding_model, dim, embedding, text_preview, hit_count)
                            VALUES (:h, :m, :d, CAST(:v AS vector), :pv, 0)
                            ON CONFLICT (text_hash) DO NOTHING
                        """),
                        {
                            "h": _sha256_hex(txt, embed_model),
                            "m": embed_model,
                            "d": expected_dim,
                            "v": _vector_literal(vec),
                            "pv": txt[:200],
                        },
                    )
                except Exception:
                    pass
        try:
            await db.commit()
        except Exception:
            await db.rollback()

    # Step 2. Insert into per-collection table in batches.
    rows: list[dict[str, Any]] = []
    for i, d in enumerate(documents):
        ns = d.get("namespace") or "default"
        _validate_namespace(ns)
        txt = d.get("text") or d.get("content") or ""
        md = d.get("metadata") or {}
        if not isinstance(md, dict):
            raise RagError(f"Doc #{i} metadata phải là object")
        pid = d.get("id") or hashlib.sha256(
            f"{ns}::{txt}".encode("utf-8")
        ).hexdigest()[:32]
        rows.append({
            "id": str(pid)[:256],
            "vector": _vector_literal(embeddings[i]),
            "metadata": json.dumps(md, ensure_ascii=False),
            "metadata_indexed": json.dumps(md, ensure_ascii=False),
            "content": txt,
            "namespace": ns,
        })

    sql = (
        f'INSERT INTO public."{table}" (id, vector, metadata, metadata_indexed, content, namespace) '
        f'VALUES (:id, CAST(:vector AS vector), CAST(:metadata AS jsonb), '
        f'        CAST(:metadata_indexed AS jsonb), :content, :namespace) '
        f'ON CONFLICT (id) DO UPDATE SET '
        f'  vector = EXCLUDED.vector, '
        f'  metadata = EXCLUDED.metadata, '
        f'  metadata_indexed = EXCLUDED.metadata_indexed, '
        f'  content = EXCLUDED.content, '
        f'  namespace = EXCLUDED.namespace'
    )
    upserted = 0
    bs = max(1, min(int(batch_size or 50), 200))
    try:
        for start in range(0, len(rows), bs):
            sub = rows[start:start + bs]
            await db.execute(text(sql), sub)
            upserted += len(sub)
    except Exception:
        await db.rollback()
        log.exception("upsert_documents failed ws=%s coll=%s", workspace_id, collection)
        raise

    # Refresh row_count
    cnt = (await db.execute(
        text(f'SELECT COUNT(*) FROM public."{table}"')
    )).scalar() or 0
    await db.execute(
        text("""
            UPDATE public.vector_collections
            SET row_count = :cnt
            WHERE id = :id
        """),
        {"cnt": int(cnt), "id": meta["id"]},
    )
    await db.commit()

    return {
        "ok": True,
        "upserted": upserted,
        "row_count": int(cnt),
        "embed": {
            "cache_hits": cache_hits,
            "cost_usd": round(cost_embed, 6),
            "model": embed_model,
        },
        "latency_ms": int((time.perf_counter() - started) * 1000),
    }


# ─── 6. COLLECTION STATS (premium-aware) ────────────────────────────────────
async def collection_stats(
    db: AsyncSession,
    *,
    workspace_id: str,
    collection: str,
) -> dict[str, Any]:
    """Premium-aware: namespace breakdown, doc count, last update, premium flag."""
    meta = await vector_search._get_collection_meta(db, workspace_id, collection)  # type: ignore[attr-defined]
    table = meta["table_name"]

    # Top-level stats (always available)
    total = (await db.execute(
        text(f'SELECT COUNT(*) FROM public."{table}"')
    )).scalar() or 0

    # Premium columns may not exist — guard with column probe.
    has_namespace = (await db.execute(
        text("""
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = :t
              AND column_name = 'namespace'
            LIMIT 1
        """),
        {"t": table},
    )).first() is not None

    by_namespace: list[dict[str, Any]] = []
    last_update = None
    if has_namespace:
        rows = (await db.execute(
            text(f'''
                SELECT namespace, COUNT(*) AS n, MAX(created_at) AS last_updated
                FROM public."{table}"
                GROUP BY namespace
                ORDER BY n DESC
                LIMIT 50
            '''),
        )).all()
        by_namespace = [
            {
                "namespace": r[0],
                "count": int(r[1]),
                "last_updated": r[2].isoformat() if r[2] else None,
            }
            for r in rows
        ]
        last_update = (await db.execute(
            text(f'SELECT MAX(created_at) FROM public."{table}"')
        )).scalar()

    return {
        "name": meta["name"],
        "dim": meta["dim"],
        "metric": meta["metric"],
        "total_count": int(total),
        "namespaces": by_namespace,
        "last_updated": last_update.isoformat() if last_update else None,
        "premium_enabled": bool(meta.get("premium_enabled")) if isinstance(meta.get("premium_enabled"), bool) else has_namespace,
    }
