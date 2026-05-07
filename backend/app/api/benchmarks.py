"""
Zeni Cloud Core — AI Benchmark Tracker (P1#10 ClawWits Professor Wits AI CIO).

Track real-time leaderboard scores for SWE-bench, HumanEval, GPQA, AIME, MMLU, LMSYS Arena, BIG-Bench, AgentBench.

Endpoints (prefix /benchmarks):
  GET    /sources                            — List 8 tracked benchmark sources
  GET    /                                   — List all benchmark scores (filtered)
  GET    /{benchmark_name}                   — Latest scores for one benchmark (e.g. swe-bench)
  GET    /{benchmark_name}/history           — Historical scores over time
  GET    /models/{model_name}                — All scores for a specific model
  POST   /scores                             — Manual score entry (admin) — used by crawler
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user
from app.db.base import get_db

log = logging.getLogger("zeni.benchmarks")

router = APIRouter(prefix="/benchmarks", tags=["benchmarks"])


# ===== Schemas =====

class BenchmarkSource(BaseModel):
    id: str
    display_name: str
    description: Optional[str] = None
    source_url: Optional[str] = None
    is_active: bool
    last_scraped_at: Optional[str] = None


class BenchmarkScore(BaseModel):
    benchmark_name: str
    model_name: str
    model_provider: Optional[str] = None
    model_version: Optional[str] = None
    score_value: float
    score_unit: str
    rank: Optional[int] = None
    source_url: Optional[str] = None
    measured_at: str
    metadata: dict = Field(default_factory=dict)


class ScoreCreate(BaseModel):
    benchmark_name: str
    model_name: str
    model_provider: Optional[str] = None
    model_version: Optional[str] = None
    score_value: float
    score_unit: str = "percent"
    rank: Optional[int] = None
    source_url: Optional[str] = None
    measured_at: Optional[str] = Field(None, description="YYYY-MM-DD; defaults to today")
    metadata: dict = Field(default_factory=dict)


# ===== Endpoints =====

@router.get("/sources", response_model=list[BenchmarkSource])
async def list_sources(db: AsyncSession = Depends(get_db)):
    """List all tracked benchmark sources. Public."""
    rows = (await db.execute(text(
        "SELECT id, display_name, description, source_url, is_active, last_scraped_at "
        "FROM benchmark_sources WHERE is_active = TRUE ORDER BY display_name"
    ))).mappings().all()
    return [
        BenchmarkSource(
            id=r["id"],
            display_name=r["display_name"],
            description=r["description"],
            source_url=r["source_url"],
            is_active=r["is_active"],
            last_scraped_at=r["last_scraped_at"].isoformat() if r["last_scraped_at"] else None,
        )
        for r in rows
    ]


@router.get("/{benchmark_name}", response_model=list[BenchmarkScore])
async def get_benchmark(
    benchmark_name: str,
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Latest scores for one benchmark (e.g. /benchmarks/swe-bench). Public."""
    rows = (await db.execute(text(
        "WITH latest AS ("
        "  SELECT DISTINCT ON (model_name) model_name, model_provider, model_version, "
        "    score_value, score_unit, rank, source_url, measured_at, metadata "
        "  FROM benchmark_models WHERE benchmark_name = :bn "
        "  ORDER BY model_name, measured_at DESC"
        ") SELECT * FROM latest ORDER BY score_value DESC LIMIT :lim"
    ), {"bn": benchmark_name, "lim": limit})).mappings().all()
    return [
        BenchmarkScore(
            benchmark_name=benchmark_name,
            model_name=r["model_name"],
            model_provider=r["model_provider"],
            model_version=r["model_version"],
            score_value=float(r["score_value"]),
            score_unit=r["score_unit"],
            rank=r["rank"],
            source_url=r["source_url"],
            measured_at=r["measured_at"].isoformat() if r["measured_at"] else "",
            metadata=r["metadata"] if isinstance(r["metadata"], dict) else {},
        )
        for r in rows
    ]


@router.get("/{benchmark_name}/history", response_model=list[BenchmarkScore])
async def get_benchmark_history(
    benchmark_name: str,
    model_name: Optional[str] = Query(None, description="Filter to one model"),
    days: int = Query(180, ge=1, le=730, description="History window (days)"),
    db: AsyncSession = Depends(get_db),
):
    """Historical scores over time for trend charts. Public."""
    sql = (
        "SELECT model_name, model_provider, model_version, score_value, score_unit, "
        "rank, source_url, measured_at, metadata FROM benchmark_models "
        "WHERE benchmark_name = :bn AND measured_at >= CURRENT_DATE - INTERVAL ':d days'"
    )
    params: dict[str, Any] = {"bn": benchmark_name, "d": days}
    if model_name:
        sql += " AND model_name = :mn"
        params["mn"] = model_name
    sql += " ORDER BY measured_at ASC, score_value DESC LIMIT 500"
    rows = (await db.execute(text(sql), params)).mappings().all()
    return [
        BenchmarkScore(
            benchmark_name=benchmark_name,
            model_name=r["model_name"],
            model_provider=r["model_provider"],
            model_version=r["model_version"],
            score_value=float(r["score_value"]),
            score_unit=r["score_unit"],
            rank=r["rank"],
            source_url=r["source_url"],
            measured_at=r["measured_at"].isoformat() if r["measured_at"] else "",
            metadata=r["metadata"] if isinstance(r["metadata"], dict) else {},
        )
        for r in rows
    ]


@router.get("/models/{model_name}", response_model=list[BenchmarkScore])
async def get_model_scores(
    model_name: str,
    db: AsyncSession = Depends(get_db),
):
    """All benchmark scores for a specific model (e.g. /benchmarks/models/claude-opus-4.7)."""
    rows = (await db.execute(text(
        "SELECT benchmark_name, model_provider, model_version, score_value, score_unit, "
        "rank, source_url, measured_at, metadata FROM benchmark_models "
        "WHERE model_name = :mn ORDER BY measured_at DESC LIMIT 200"
    ), {"mn": model_name})).mappings().all()
    return [
        BenchmarkScore(
            benchmark_name=r["benchmark_name"],
            model_name=model_name,
            model_provider=r["model_provider"],
            model_version=r["model_version"],
            score_value=float(r["score_value"]),
            score_unit=r["score_unit"],
            rank=r["rank"],
            source_url=r["source_url"],
            measured_at=r["measured_at"].isoformat() if r["measured_at"] else "",
            metadata=r["metadata"] if isinstance(r["metadata"], dict) else {},
        )
        for r in rows
    ]


@router.post("/scores", status_code=201)
async def add_score(
    data: ScoreCreate,
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    """Manually add benchmark score (admin/crawler use). Requires Admin or Owner role."""
    if me.role not in ("Admin", "Owner"):
        raise HTTPException(403, "Only Admin/Owner can add benchmark scores. Use crawler for automation.")
    measured = date.today() if not data.measured_at else date.fromisoformat(data.measured_at)
    import json
    await db.execute(text(
        "INSERT INTO benchmark_models (benchmark_name, model_name, model_provider, model_version, "
        "score_value, score_unit, rank, source_url, measured_at, metadata) "
        "VALUES (:bn, :mn, :mp, :mv, :sv, :su, :rk, :url, :ma, CAST(:meta AS jsonb)) "
        "ON CONFLICT (benchmark_name, model_name, measured_at) DO UPDATE SET "
        "score_value = EXCLUDED.score_value, rank = EXCLUDED.rank, "
        "source_url = EXCLUDED.source_url, metadata = EXCLUDED.metadata"
    ), {
        "bn": data.benchmark_name,
        "mn": data.model_name,
        "mp": data.model_provider,
        "mv": data.model_version,
        "sv": data.score_value,
        "su": data.score_unit,
        "rk": data.rank,
        "url": data.source_url,
        "ma": measured,
        "meta": json.dumps(data.metadata),
    })
    await db.commit()
    return {"status": "recorded", "benchmark": data.benchmark_name, "model": data.model_name}
