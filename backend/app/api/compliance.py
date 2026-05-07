"""
Zeni Cloud Core — Compliance Pack API.

Module tự động hoá SOC 2, ISO 27001, GDPR, Nghị định 13/2023 VN cho
enterprise customers. Cho phép self-serve compliance dashboard + auto
evidence collection.

Routes (mounted under /api/v1/compliance trong main.py):

  Frameworks (public):
    GET    /frameworks                       — list 4 frameworks
    GET    /frameworks/{id}/controls         — list controls cho framework

  Assessments:
    POST   /assessments?ws=                  — initialize all controls cho framework
    GET    /assessments?ws=&framework_id=&status=
    PATCH  /assessments/{id}                 — update status, notes, assigned_to
    POST   /assessments/{id}/auto-check      — chạy automated check

  Evidence:
    POST   /evidence?ws=                     — attach evidence
    GET    /evidence?ws=&assessment_id=
    DELETE /evidence/{id}

  Audit Trail:
    GET    /audit-trail?ws=&from=&to=&action=

  Risks:
    POST   /risks?ws=                         — register risk
    GET    /risks?ws=&status=
    PATCH  /risks/{id}                        — update treatment

  Policies:
    POST   /policies?ws=                      — create policy
    GET    /policies?ws=
    PATCH  /policies/{id}/approve             — sign-off

  Reports:
    GET    /reports/readiness?ws=&framework_id=    — % compliance + breakdown
    GET    /reports/audit-pack?ws=&framework_id=   — audit-ready bundle (text placeholder)

Mọi endpoint (trừ /frameworks public) đều yêu cầu workspace access. Hành động
quan trọng (initialize, approve policy, attach evidence) đều ghi vào
audit_log qua audit_push().
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import (
    CurrentUser,
    get_current_user,
    require_workspace_access,
)
from app.db.base import get_db
from app.services.audit import audit_push
from app.services.compliance_checker import run_all_auto_checks

log = logging.getLogger("zeni.api.compliance")
router = APIRouter(prefix="/compliance", tags=["compliance"])


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════
async def _resolve_ws(db: AsyncSession, ws: str, me: CurrentUser) -> str:
    """Accept workspace code hoặc id, return canonical id, ensure access."""
    row = (await db.execute(
        text("SELECT id FROM workspaces WHERE id = :ws OR code = :ws LIMIT 1"),
        {"ws": ws},
    )).first()
    if not row:
        raise HTTPException(404, "Không tìm thấy workspace")
    workspace_id = str(row[0])
    await require_workspace_access(workspace_id, me)
    return workspace_id


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Mapping row → JSON-safe dict."""
    out: dict[str, Any] = {}
    for k, v in dict(row).items():
        if isinstance(v, (date, datetime)):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


async def _audit_compliance_event(
    db: AsyncSession,
    *,
    workspace_id: str,
    actor_email: str | None,
    action: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    request: Request = None,  # type: ignore[assignment]
    request_data: dict[str, Any] | None = None,
    response_status: int = 200,
) -> None:
    """Insert vào compliance_audit_trail (riêng cho compliance evidence chain)."""
    ip = None
    user_agent = None
    if request is not None:
        client = request.client
        ip = client.host if client else None
        user_agent = request.headers.get("user-agent")

    import json
    await db.execute(
        text(
            """
            INSERT INTO compliance_audit_trail
              (workspace_id, actor_email, action, resource_type, resource_id,
               ip_address, user_agent, request_data, response_status)
            VALUES (:ws, :actor, :action, :rtype, :rid,
                    CAST(:ip AS INET), :ua, CAST(:req AS JSONB), :status)
            """
        ),
        {
            "ws": workspace_id,
            "actor": actor_email,
            "action": action,
            "rtype": resource_type,
            "rid": resource_id,
            "ip": ip,
            "ua": user_agent,
            "req": json.dumps(request_data, default=str) if request_data else None,
            "status": response_status,
        },
    )


# ════════════════════════════════════════════════════════════════════════════
# Pydantic schemas
# ════════════════════════════════════════════════════════════════════════════
class AssessmentPatchIn(BaseModel):
    status: str | None = Field(default=None,
                                pattern=r"^(not_started|in_progress|compliant|non_compliant|exempt)$")
    notes: str | None = Field(default=None, max_length=4000)
    assigned_to: str | None = Field(default=None, max_length=200)
    next_review_at: datetime | None = None


class EvidenceIn(BaseModel):
    assessment_id: int = Field(gt=0)
    evidence_type: str = Field(max_length=40,
                                pattern=r"^(audit_log|screenshot|policy_doc|attestation|test_result)$")
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    storage_url: str | None = Field(default=None, max_length=2000)
    metadata: dict[str, Any] | None = None
    expires_at: datetime | None = None


class RiskIn(BaseModel):
    title: str = Field(min_length=2, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    likelihood: str = Field(pattern=r"^(rare|unlikely|possible|likely|almost_certain)$")
    impact: str = Field(pattern=r"^(insignificant|minor|moderate|major|catastrophic)$")
    treatment_plan: str | None = Field(default=None, max_length=4000)
    owner_email: str | None = Field(default=None, max_length=200)
    review_date: date | None = None


class RiskPatchIn(BaseModel):
    status: str | None = Field(default=None, pattern=r"^(open|accepted|mitigated|closed)$")
    likelihood: str | None = Field(default=None,
                                    pattern=r"^(rare|unlikely|possible|likely|almost_certain)$")
    impact: str | None = Field(default=None,
                                pattern=r"^(insignificant|minor|moderate|major|catastrophic)$")
    treatment_plan: str | None = Field(default=None, max_length=4000)
    owner_email: str | None = Field(default=None, max_length=200)
    review_date: date | None = None


class PolicyIn(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    category: str = Field(pattern=r"^(security|privacy|hr|ops)$")
    version: str | None = Field(default="1.0", max_length=20)
    content_md: str | None = Field(default=None)
    next_review_date: date | None = None


# ════════════════════════════════════════════════════════════════════════════
# Risk-score helper
# ════════════════════════════════════════════════════════════════════════════
LIKELIHOOD_SCORE = {
    "rare": 1, "unlikely": 2, "possible": 3, "likely": 4, "almost_certain": 5,
}
IMPACT_SCORE = {
    "insignificant": 1, "minor": 2, "moderate": 3, "major": 4, "catastrophic": 5,
}


def _calc_risk_score(likelihood: str, impact: str) -> int:
    return LIKELIHOOD_SCORE.get(likelihood, 1) * IMPACT_SCORE.get(impact, 1)


# ════════════════════════════════════════════════════════════════════════════
# 1. Frameworks (public — list 4 frameworks + their controls)
# ════════════════════════════════════════════════════════════════════════════
@router.get("/frameworks")
async def list_frameworks(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(
        text(
            """
            SELECT f.id, f.name, f.description, f.version, f.categories, f.is_active,
                   COUNT(c.id) AS controls_count
            FROM compliance_frameworks f
            LEFT JOIN compliance_controls c ON c.framework_id = f.id
            WHERE f.is_active = TRUE
            GROUP BY f.id, f.name, f.description, f.version, f.categories, f.is_active
            ORDER BY f.id
            """
        )
    )).mappings().all()
    return [dict(r) for r in rows]


@router.get("/frameworks/{framework_id}/controls")
async def get_framework_controls(
    framework_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    fw = (await db.execute(
        text("SELECT id, name, description, version, categories FROM compliance_frameworks WHERE id = :id"),
        {"id": framework_id},
    )).mappings().first()
    if not fw:
        raise HTTPException(404, "Framework không tồn tại")
    rows = (await db.execute(
        text(
            """
            SELECT id, control_code, title, description, category, severity, automation_type
            FROM compliance_controls
            WHERE framework_id = :fw
            ORDER BY control_code
            """
        ),
        {"fw": framework_id},
    )).mappings().all()
    return {
        "framework": dict(fw),
        "controls": [dict(r) for r in rows],
        "total": len(rows),
    }


# ════════════════════════════════════════════════════════════════════════════
# 2. Assessments
# ════════════════════════════════════════════════════════════════════════════
@router.post("/assessments")
async def initialize_assessments(
    framework_id: str = Query(..., description="ví dụ: 'soc2', 'iso27001', 'gdpr', 'nd13'"),
    ws: str = Query(..., description="workspace code or id"),
    request: Request = None,  # type: ignore[assignment]
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Initialize tất cả controls của framework cho workspace với status='not_started'."""
    workspace_id = await _resolve_ws(db, ws, me)
    fw = (await db.execute(
        text("SELECT id FROM compliance_frameworks WHERE id = :id"), {"id": framework_id}
    )).first()
    if not fw:
        raise HTTPException(404, "Framework không tồn tại")

    inserted = (await db.execute(
        text(
            """
            INSERT INTO compliance_assessments (workspace_id, framework_id, control_id, status)
            SELECT :ws, :fw, c.id, 'not_started'
            FROM compliance_controls c
            WHERE c.framework_id = :fw
            ON CONFLICT (workspace_id, control_id) DO NOTHING
            RETURNING id
            """
        ),
        {"ws": workspace_id, "fw": framework_id},
    )).all()

    await audit_push(
        db,
        actor=me.email,
        workspace_id=workspace_id,
        action="compliance.assessments.initialize",
        target=framework_id,
        metadata={"framework_id": framework_id, "inserted": len(inserted)},
    )
    await _audit_compliance_event(
        db,
        workspace_id=workspace_id,
        actor_email=me.email,
        action="assessments.initialize",
        resource_type="framework",
        resource_id=framework_id,
        request=request,
        request_data={"framework_id": framework_id},
    )
    await db.commit()
    return {
        "framework_id": framework_id,
        "workspace_id": workspace_id,
        "controls_initialized": len(inserted),
    }


@router.get("/assessments")
async def list_assessments(
    ws: str = Query(...),
    framework_id: str | None = None,
    status: str | None = Query(default=None,
                                pattern=r"^(not_started|in_progress|compliant|non_compliant|exempt)$"),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    workspace_id = await _resolve_ws(db, ws, me)
    sql = """
        SELECT a.id, a.workspace_id, a.framework_id, a.control_id,
               c.control_code, c.title AS control_title, c.category, c.severity,
               c.automation_type, a.status, a.evidence_count,
               a.last_check_at, a.next_review_at, a.assigned_to,
               a.notes, a.auto_check_passed
        FROM compliance_assessments a
        JOIN compliance_controls c ON c.id = a.control_id
        WHERE a.workspace_id = :ws
    """
    params: dict[str, Any] = {"ws": workspace_id}
    if framework_id:
        sql += " AND a.framework_id = :fw"
        params["fw"] = framework_id
    if status:
        sql += " AND a.status = :status"
        params["status"] = status
    sql += " ORDER BY c.framework_id, c.control_code"

    rows = (await db.execute(text(sql), params)).mappings().all()
    return [_row_to_dict(r) for r in rows]


@router.patch("/assessments/{assessment_id}")
async def patch_assessment(
    assessment_id: int,
    body: AssessmentPatchIn,
    request: Request = None,  # type: ignore[assignment]
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = (await db.execute(
        text("SELECT workspace_id, framework_id FROM compliance_assessments WHERE id = :id"),
        {"id": assessment_id},
    )).first()
    if not row:
        raise HTTPException(404, "Assessment không tồn tại")
    workspace_id = str(row[0])
    await require_workspace_access(workspace_id, me)

    sets: list[str] = []
    params: dict[str, Any] = {"id": assessment_id}
    for k, v in body.model_dump(exclude_unset=True).items():
        sets.append(f"{k} = :{k}")
        params[k] = v
    if not sets:
        raise HTTPException(400, "Không có thay đổi")
    sql = f"UPDATE compliance_assessments SET {', '.join(sets)} WHERE id = :id RETURNING *"
    updated = (await db.execute(text(sql), params)).mappings().first()

    await audit_push(
        db,
        actor=me.email,
        workspace_id=workspace_id,
        action="compliance.assessment.update",
        target=str(assessment_id),
        metadata=body.model_dump(exclude_unset=True),
    )
    await _audit_compliance_event(
        db,
        workspace_id=workspace_id,
        actor_email=me.email,
        action="assessment.update",
        resource_type="assessment",
        resource_id=str(assessment_id),
        request=request,
        request_data=body.model_dump(exclude_unset=True),
    )
    await db.commit()
    return _row_to_dict(updated) if updated else {}


@router.post("/assessments/{assessment_id}/auto-check")
async def run_auto_check_one(
    assessment_id: int,
    request: Request = None,  # type: ignore[assignment]
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Chạy automated checks cho workspace (workspace-scope, không chỉ 1 control).

    Tham số ``assessment_id`` chỉ dùng để xác định workspace + log target.
    Tất cả checks chạy đồng thời (1 batch) → cập nhật mọi assessment nào áp dụng.
    """
    row = (await db.execute(
        text("SELECT workspace_id FROM compliance_assessments WHERE id = :id"),
        {"id": assessment_id},
    )).first()
    if not row:
        raise HTTPException(404, "Assessment không tồn tại")
    workspace_id = str(row[0])
    await require_workspace_access(workspace_id, me)

    summary = await run_all_auto_checks(db, workspace_id, actor_email=me.email)

    await audit_push(
        db,
        actor=me.email,
        workspace_id=workspace_id,
        action="compliance.auto_check.run",
        target=str(assessment_id),
        metadata={
            "checks_run": summary["checks_run"],
            "passed": summary["passed"],
            "failed": summary["failed"],
        },
    )
    await _audit_compliance_event(
        db,
        workspace_id=workspace_id,
        actor_email=me.email,
        action="auto_check.run",
        resource_type="assessment",
        resource_id=str(assessment_id),
        request=request,
        request_data={"trigger": "manual"},
    )
    await db.commit()
    return summary


# ════════════════════════════════════════════════════════════════════════════
# 3. Evidence
# ════════════════════════════════════════════════════════════════════════════
@router.post("/evidence")
async def attach_evidence(
    body: EvidenceIn,
    ws: str = Query(...),
    request: Request = None,  # type: ignore[assignment]
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    workspace_id = await _resolve_ws(db, ws, me)

    # Verify assessment thuộc workspace
    a = (await db.execute(
        text("SELECT workspace_id FROM compliance_assessments WHERE id = :id"),
        {"id": body.assessment_id},
    )).first()
    if not a or str(a[0]) != workspace_id:
        raise HTTPException(404, "Assessment không tồn tại trong workspace này")

    import json
    inserted = (await db.execute(
        text(
            """
            INSERT INTO compliance_evidence
              (assessment_id, workspace_id, evidence_type, title, description,
               storage_url, metadata, collected_at, collected_by, expires_at)
            VALUES (:aid, :ws, :etype, :title, :desc, :url,
                    CAST(:meta AS JSONB), NOW(), :by, :exp)
            RETURNING id, collected_at
            """
        ),
        {
            "aid": body.assessment_id,
            "ws": workspace_id,
            "etype": body.evidence_type,
            "title": body.title,
            "desc": body.description,
            "url": body.storage_url,
            "meta": json.dumps(body.metadata, default=str) if body.metadata else None,
            "by": me.email,
            "exp": body.expires_at,
        },
    )).first()
    if not inserted:
        raise HTTPException(500, "Không thể tạo evidence")
    evidence_id = int(inserted[0])

    # Increment evidence_count trên assessment
    await db.execute(
        text(
            """
            UPDATE compliance_assessments
            SET evidence_count = evidence_count + 1,
                last_check_at = NOW()
            WHERE id = :id
            """
        ),
        {"id": body.assessment_id},
    )

    await audit_push(
        db,
        actor=me.email,
        workspace_id=workspace_id,
        action="compliance.evidence.attach",
        target=str(evidence_id),
        metadata={"assessment_id": body.assessment_id, "type": body.evidence_type},
    )
    await _audit_compliance_event(
        db,
        workspace_id=workspace_id,
        actor_email=me.email,
        action="evidence.attach",
        resource_type="evidence",
        resource_id=str(evidence_id),
        request=request,
        request_data=body.model_dump(),
    )
    await db.commit()
    return {
        "id": evidence_id,
        "assessment_id": body.assessment_id,
        "evidence_type": body.evidence_type,
        "title": body.title,
        "collected_at": inserted[1].isoformat() if inserted[1] else None,
    }


@router.get("/evidence")
async def list_evidence(
    ws: str = Query(...),
    assessment_id: int | None = None,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    workspace_id = await _resolve_ws(db, ws, me)
    sql = """
        SELECT id, assessment_id, workspace_id, evidence_type, title, description,
               storage_url, metadata, collected_at, collected_by, expires_at
        FROM compliance_evidence
        WHERE workspace_id = :ws
    """
    params: dict[str, Any] = {"ws": workspace_id}
    if assessment_id:
        sql += " AND assessment_id = :aid"
        params["aid"] = assessment_id
    sql += " ORDER BY collected_at DESC LIMIT 500"
    rows = (await db.execute(text(sql), params)).mappings().all()
    return [_row_to_dict(r) for r in rows]


@router.delete("/evidence/{evidence_id}")
async def delete_evidence(
    evidence_id: int,
    request: Request = None,  # type: ignore[assignment]
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = (await db.execute(
        text("SELECT workspace_id, assessment_id FROM compliance_evidence WHERE id = :id"),
        {"id": evidence_id},
    )).first()
    if not row:
        raise HTTPException(404, "Evidence không tồn tại")
    workspace_id = str(row[0])
    assessment_id = int(row[1])
    await require_workspace_access(workspace_id, me)

    await db.execute(text("DELETE FROM compliance_evidence WHERE id = :id"), {"id": evidence_id})
    await db.execute(
        text(
            """
            UPDATE compliance_assessments
            SET evidence_count = GREATEST(evidence_count - 1, 0)
            WHERE id = :aid
            """
        ),
        {"aid": assessment_id},
    )

    await audit_push(
        db,
        actor=me.email,
        workspace_id=workspace_id,
        action="compliance.evidence.delete",
        target=str(evidence_id),
        severity="warning",
    )
    await _audit_compliance_event(
        db,
        workspace_id=workspace_id,
        actor_email=me.email,
        action="evidence.delete",
        resource_type="evidence",
        resource_id=str(evidence_id),
        request=request,
    )
    await db.commit()
    return {"deleted": True, "id": evidence_id}


# ════════════════════════════════════════════════════════════════════════════
# 4. Audit Trail
# ════════════════════════════════════════════════════════════════════════════
@router.get("/audit-trail")
async def query_audit_trail(
    ws: str = Query(...),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
    action: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    workspace_id = await _resolve_ws(db, ws, me)
    sql = """
        SELECT id, workspace_id, actor_email, action, resource_type, resource_id,
               ip_address::text AS ip_address, user_agent, request_data,
               response_status, occurred_at
        FROM compliance_audit_trail
        WHERE workspace_id = :ws
    """
    params: dict[str, Any] = {"ws": workspace_id}
    if from_:
        sql += " AND occurred_at >= :from_"
        params["from_"] = from_
    if to:
        sql += " AND occurred_at <= :to"
        params["to"] = to
    if action:
        sql += " AND action = :action"
        params["action"] = action
    sql += " ORDER BY occurred_at DESC LIMIT :lim"
    params["lim"] = limit
    rows = (await db.execute(text(sql), params)).mappings().all()
    return [_row_to_dict(r) for r in rows]


# ════════════════════════════════════════════════════════════════════════════
# 5. Risks
# ════════════════════════════════════════════════════════════════════════════
@router.post("/risks")
async def create_risk(
    body: RiskIn,
    ws: str = Query(...),
    request: Request = None,  # type: ignore[assignment]
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    workspace_id = await _resolve_ws(db, ws, me)
    risk_score = _calc_risk_score(body.likelihood, body.impact)
    inserted = (await db.execute(
        text(
            """
            INSERT INTO compliance_risks
              (workspace_id, title, description, likelihood, impact, risk_score,
               status, treatment_plan, owner_email, review_date)
            VALUES (:ws, :title, :desc, :lik, :imp, :score, 'open',
                    :tp, :owner, :rev)
            RETURNING *
            """
        ),
        {
            "ws": workspace_id,
            "title": body.title,
            "desc": body.description,
            "lik": body.likelihood,
            "imp": body.impact,
            "score": risk_score,
            "tp": body.treatment_plan,
            "owner": body.owner_email,
            "rev": body.review_date,
        },
    )).mappings().first()

    await audit_push(
        db,
        actor=me.email,
        workspace_id=workspace_id,
        action="compliance.risk.create",
        target=str(inserted["id"]) if inserted else None,
        metadata={"title": body.title, "risk_score": risk_score},
    )
    await _audit_compliance_event(
        db,
        workspace_id=workspace_id,
        actor_email=me.email,
        action="risk.create",
        resource_type="risk",
        resource_id=str(inserted["id"]) if inserted else None,
        request=request,
        request_data=body.model_dump(),
    )
    await db.commit()
    return _row_to_dict(inserted) if inserted else {}


@router.get("/risks")
async def list_risks(
    ws: str = Query(...),
    status: str | None = Query(default=None, pattern=r"^(open|accepted|mitigated|closed)$"),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    workspace_id = await _resolve_ws(db, ws, me)
    sql = """
        SELECT id, workspace_id, title, description, likelihood, impact, risk_score,
               status, treatment_plan, owner_email, review_date, created_at
        FROM compliance_risks
        WHERE workspace_id = :ws
    """
    params: dict[str, Any] = {"ws": workspace_id}
    if status:
        sql += " AND status = :status"
        params["status"] = status
    sql += " ORDER BY risk_score DESC, created_at DESC"
    rows = (await db.execute(text(sql), params)).mappings().all()
    return [_row_to_dict(r) for r in rows]


@router.patch("/risks/{risk_id}")
async def patch_risk(
    risk_id: int,
    body: RiskPatchIn,
    request: Request = None,  # type: ignore[assignment]
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = (await db.execute(
        text("SELECT workspace_id, likelihood, impact FROM compliance_risks WHERE id = :id"),
        {"id": risk_id},
    )).first()
    if not row:
        raise HTTPException(404, "Risk không tồn tại")
    workspace_id = str(row[0])
    await require_workspace_access(workspace_id, me)

    payload = body.model_dump(exclude_unset=True)
    if not payload:
        raise HTTPException(400, "Không có thay đổi")

    # Recalc risk_score nếu likelihood / impact thay đổi
    new_lik = payload.get("likelihood", row[1])
    new_imp = payload.get("impact", row[2])
    if "likelihood" in payload or "impact" in payload:
        payload["risk_score"] = _calc_risk_score(new_lik, new_imp)

    sets: list[str] = []
    params: dict[str, Any] = {"id": risk_id}
    for k, v in payload.items():
        sets.append(f"{k} = :{k}")
        params[k] = v
    sql = f"UPDATE compliance_risks SET {', '.join(sets)} WHERE id = :id RETURNING *"
    updated = (await db.execute(text(sql), params)).mappings().first()

    await audit_push(
        db,
        actor=me.email,
        workspace_id=workspace_id,
        action="compliance.risk.update",
        target=str(risk_id),
        metadata=payload,
    )
    await _audit_compliance_event(
        db,
        workspace_id=workspace_id,
        actor_email=me.email,
        action="risk.update",
        resource_type="risk",
        resource_id=str(risk_id),
        request=request,
        request_data=payload,
    )
    await db.commit()
    return _row_to_dict(updated) if updated else {}


# ════════════════════════════════════════════════════════════════════════════
# 6. Policies
# ════════════════════════════════════════════════════════════════════════════
@router.post("/policies")
async def create_policy(
    body: PolicyIn,
    ws: str = Query(...),
    request: Request = None,  # type: ignore[assignment]
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    workspace_id = await _resolve_ws(db, ws, me)
    inserted = (await db.execute(
        text(
            """
            INSERT INTO compliance_policies
              (workspace_id, name, category, version, content_md, next_review_date, status)
            VALUES (:ws, :name, :cat, :ver, :content, :rev, 'draft')
            RETURNING *
            """
        ),
        {
            "ws": workspace_id,
            "name": body.name,
            "cat": body.category,
            "ver": body.version,
            "content": body.content_md,
            "rev": body.next_review_date,
        },
    )).mappings().first()

    await audit_push(
        db,
        actor=me.email,
        workspace_id=workspace_id,
        action="compliance.policy.create",
        target=str(inserted["id"]) if inserted else None,
        metadata={"name": body.name, "category": body.category},
    )
    await _audit_compliance_event(
        db,
        workspace_id=workspace_id,
        actor_email=me.email,
        action="policy.create",
        resource_type="policy",
        resource_id=str(inserted["id"]) if inserted else None,
        request=request,
        request_data=body.model_dump(),
    )
    await db.commit()
    return _row_to_dict(inserted) if inserted else {}


@router.get("/policies")
async def list_policies(
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    workspace_id = await _resolve_ws(db, ws, me)
    rows = (await db.execute(
        text(
            """
            SELECT id, workspace_id, name, category, version, content_md,
                   approved_by, approved_at, next_review_date, status, created_at
            FROM compliance_policies
            WHERE workspace_id = :ws
            ORDER BY created_at DESC
            """
        ),
        {"ws": workspace_id},
    )).mappings().all()
    return [_row_to_dict(r) for r in rows]


@router.patch("/policies/{policy_id}/approve")
async def approve_policy(
    policy_id: int,
    request: Request = None,  # type: ignore[assignment]
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = (await db.execute(
        text("SELECT workspace_id, name, status FROM compliance_policies WHERE id = :id"),
        {"id": policy_id},
    )).first()
    if not row:
        raise HTTPException(404, "Policy không tồn tại")
    workspace_id = str(row[0])
    await require_workspace_access(workspace_id, me)

    if row[2] == "approved":
        raise HTTPException(409, "Policy đã được duyệt rồi")

    updated = (await db.execute(
        text(
            """
            UPDATE compliance_policies
            SET status = 'approved',
                approved_by = :by,
                approved_at = NOW()
            WHERE id = :id
            RETURNING *
            """
        ),
        {"id": policy_id, "by": me.email},
    )).mappings().first()

    await audit_push(
        db,
        actor=me.email,
        workspace_id=workspace_id,
        action="compliance.policy.approve",
        target=str(policy_id),
        severity="warning",
        metadata={"policy_name": row[1]},
    )
    await _audit_compliance_event(
        db,
        workspace_id=workspace_id,
        actor_email=me.email,
        action="policy.approve",
        resource_type="policy",
        resource_id=str(policy_id),
        request=request,
    )
    await db.commit()
    return _row_to_dict(updated) if updated else {}


# ════════════════════════════════════════════════════════════════════════════
# 7. Reports — readiness + audit-pack
# ════════════════════════════════════════════════════════════════════════════
@router.get("/reports/readiness")
async def readiness_report(
    ws: str = Query(...),
    framework_id: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """% compliance + breakdown by category + missing controls."""
    workspace_id = await _resolve_ws(db, ws, me)

    fw = (await db.execute(
        text("SELECT id, name FROM compliance_frameworks WHERE id = :id"),
        {"id": framework_id},
    )).mappings().first()
    if not fw:
        raise HTTPException(404, "Framework không tồn tại")

    # Status counts
    status_rows = (await db.execute(
        text(
            """
            SELECT a.status, COUNT(*) AS n
            FROM compliance_assessments a
            WHERE a.workspace_id = :ws AND a.framework_id = :fw
            GROUP BY a.status
            """
        ),
        {"ws": workspace_id, "fw": framework_id},
    )).mappings().all()
    by_status = {r["status"]: int(r["n"]) for r in status_rows}

    total_controls = (await db.execute(
        text("SELECT COUNT(*) FROM compliance_controls WHERE framework_id = :fw"),
        {"fw": framework_id},
    )).scalar() or 0
    total_controls = int(total_controls)

    # Tỷ lệ compliant
    n_compliant = by_status.get("compliant", 0)
    n_in_progress = by_status.get("in_progress", 0)
    n_non = by_status.get("non_compliant", 0)
    n_exempt = by_status.get("exempt", 0)
    n_explicit_not_started = by_status.get("not_started", 0)
    n_assessed = sum(by_status.values())
    # Controls chưa từng được tạo assessment vẫn count là "not_started"
    n_not_started = n_explicit_not_started + max(total_controls - n_assessed, 0)

    pct = round((n_compliant + n_exempt) * 100.0 / total_controls, 2) if total_controls else 0.0

    # Breakdown by category
    cat_rows = (await db.execute(
        text(
            """
            SELECT c.category,
                   COUNT(*) FILTER (WHERE a.status = 'compliant')      AS compliant,
                   COUNT(*) FILTER (WHERE a.status = 'non_compliant')  AS non_compliant,
                   COUNT(*) FILTER (WHERE a.status = 'in_progress')    AS in_progress,
                   COUNT(*) FILTER (WHERE a.status = 'exempt')         AS exempt,
                   COUNT(*) FILTER (WHERE a.status = 'not_started' OR a.status IS NULL) AS not_started,
                   COUNT(c.id) AS total
            FROM compliance_controls c
            LEFT JOIN compliance_assessments a
              ON a.control_id = c.id AND a.workspace_id = :ws
            WHERE c.framework_id = :fw
            GROUP BY c.category
            ORDER BY c.category
            """
        ),
        {"ws": workspace_id, "fw": framework_id},
    )).mappings().all()

    breakdown = []
    for r in cat_rows:
        d = dict(r)
        d["compliance_pct"] = round(
            int(d["compliant"]) * 100.0 / max(int(d["total"]), 1), 1
        )
        breakdown.append(d)

    # Missing controls (chưa có assessment hoặc not_started / non_compliant)
    missing = (await db.execute(
        text(
            """
            SELECT c.id, c.control_code, c.title, c.category, c.severity,
                   c.automation_type, COALESCE(a.status, 'not_started') AS status
            FROM compliance_controls c
            LEFT JOIN compliance_assessments a
              ON a.control_id = c.id AND a.workspace_id = :ws
            WHERE c.framework_id = :fw
              AND (a.id IS NULL OR a.status IN ('not_started', 'non_compliant', 'in_progress'))
            ORDER BY c.control_code
            """
        ),
        {"ws": workspace_id, "fw": framework_id},
    )).mappings().all()

    # Open risks
    open_risks_n = (await db.execute(
        text(
            "SELECT COUNT(*) FROM compliance_risks "
            "WHERE workspace_id = :ws AND status = 'open'"
        ),
        {"ws": workspace_id},
    )).scalar() or 0

    return {
        "workspace_id": workspace_id,
        "framework": dict(fw),
        "summary": {
            "total_controls": total_controls,
            "compliant": n_compliant,
            "in_progress": n_in_progress,
            "non_compliant": n_non,
            "exempt": n_exempt,
            "not_started": n_not_started,
            "compliance_pct": pct,
            "open_risks": int(open_risks_n),
        },
        "by_category": breakdown,
        "missing_controls": [_row_to_dict(r) for r in missing],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/reports/audit-pack")
async def audit_pack_report(
    ws: str = Query(...),
    framework_id: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Audit-ready bundle (text placeholder).

    V1 trả về JSON manifest mô tả nội dung sẽ có trong ZIP/PDF thực tế.
    Khi xuất ra file binary sẽ dùng GCS signed URL.
    """
    workspace_id = await _resolve_ws(db, ws, me)
    fw = (await db.execute(
        text("SELECT id, name, version FROM compliance_frameworks WHERE id = :id"),
        {"id": framework_id},
    )).mappings().first()
    if not fw:
        raise HTTPException(404, "Framework không tồn tại")

    # Đếm assessments + evidence
    assess_n = (await db.execute(
        text(
            "SELECT COUNT(*) FROM compliance_assessments "
            "WHERE workspace_id = :ws AND framework_id = :fw"
        ),
        {"ws": workspace_id, "fw": framework_id},
    )).scalar() or 0

    evid_n = (await db.execute(
        text(
            """
            SELECT COUNT(e.id)
            FROM compliance_evidence e
            JOIN compliance_assessments a ON a.id = e.assessment_id
            WHERE e.workspace_id = :ws AND a.framework_id = :fw
            """
        ),
        {"ws": workspace_id, "fw": framework_id},
    )).scalar() or 0

    policies_n = (await db.execute(
        text(
            "SELECT COUNT(*) FROM compliance_policies "
            "WHERE workspace_id = :ws AND status = 'approved'"
        ),
        {"ws": workspace_id},
    )).scalar() or 0

    risks_n = (await db.execute(
        text("SELECT COUNT(*) FROM compliance_risks WHERE workspace_id = :ws"),
        {"ws": workspace_id},
    )).scalar() or 0

    audit_n = (await db.execute(
        text(
            "SELECT COUNT(*) FROM compliance_audit_trail "
            "WHERE workspace_id = :ws AND occurred_at >= NOW() - INTERVAL '90 days'"
        ),
        {"ws": workspace_id},
    )).scalar() or 0

    # Audit-pack tự ghi log để chứng minh đã trích xuất
    await audit_push(
        db,
        actor=me.email,
        workspace_id=workspace_id,
        action="compliance.audit_pack.generate",
        target=framework_id,
        metadata={"framework_id": framework_id},
    )
    await _audit_compliance_event(
        db,
        workspace_id=workspace_id,
        actor_email=me.email,
        action="audit_pack.generate",
        resource_type="framework",
        resource_id=framework_id,
    )
    await db.commit()

    return {
        "workspace_id": workspace_id,
        "framework": dict(fw),
        "manifest": {
            "assessments_included": int(assess_n),
            "evidence_artifacts": int(evid_n),
            "approved_policies": int(policies_n),
            "risk_register_items": int(risks_n),
            "audit_trail_entries_90d": int(audit_n),
        },
        "format": "json_manifest_v1",
        "note": (
            "Đây là bản placeholder. Phiên bản tiếp theo sẽ trả về GCS "
            "signed URL trỏ đến ZIP gồm: PDF assessment summary, evidence "
            "files, policy PDFs, risk register CSV, audit trail CSV."
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
