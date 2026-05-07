"""
Zeni CRM — engine service.

Responsibilities:
  - auto_create_contact_from_lead(...) — landing page form auto-create contact
  - merge_duplicates(...)              — merge duplicate contacts vào primary
  - compute_deal_score(...)            — rule-based lead/deal score
  - process_sequence_step()            — cron worker: send next step cho enrollment
  - evaluate_dynamic_list(...)         — refresh dynamic list members theo filter

Re-uses app.services.email.send_email cho gửi email sequence.
Re-uses app.services.audit.audit_push cho important state changes.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.audit import audit_push
from app.services.email import send_email

log = logging.getLogger("zeni.crm.engine")


# ═════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_\.]*)\s*\}\}")
SEQUENCE_BATCH_SIZE = 100
MAX_SEQUENCE_STEPS = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _serialize_jsonb(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


def _render_template(tmpl: str, ctx: dict[str, Any]) -> str:
    """Mustache-style {{var}} substitution. Supports nested keys via dot."""
    if not tmpl:
        return tmpl

    def _resolve(path: str) -> str:
        cur: Any = ctx
        for p in path.split("."):
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                return ""
        return "" if cur is None else str(cur)

    return _VAR_RE.sub(lambda m: _resolve(m.group(1)), tmpl)


def _flatten_contact(contact: dict[str, Any]) -> dict[str, Any]:
    """Build a flat name → value lookup for {{var}} substitution.

    Supports: email, full_name, first_name, last_name, phone, job_title,
              company_name, plus anything in properties.
    """
    full_name = (contact.get("full_name") or "").strip()
    parts = full_name.split(None, 1) if full_name else ["", ""]
    first = parts[0] if parts else ""
    last = parts[1] if len(parts) > 1 else ""
    out: dict[str, Any] = {
        "email": contact.get("email") or "",
        "full_name": full_name or contact.get("email") or "",
        "name": full_name or contact.get("email") or "",
        "first_name": first,
        "last_name": last,
        "phone": contact.get("phone") or "",
        "job_title": contact.get("job_title") or "",
        "company_name": contact.get("company_name") or "",
    }
    props = contact.get("properties") or {}
    if isinstance(props, str):
        try:
            props = json.loads(props)
        except Exception:
            props = {}
    if isinstance(props, dict):
        for k, v in props.items():
            if k not in out:
                out[k] = v
    return out


# ═════════════════════════════════════════════════════════
# 1. Landing-page lead capture
# ═════════════════════════════════════════════════════════
async def auto_create_contact_from_lead(
    db: AsyncSession,
    *,
    workspace_id: str,
    email: str,
    full_name: str | None = None,
    phone: str | None = None,
    source: str = "website",
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Auto-create contact (lifecycle=lead) hoặc cập nhật nếu đã tồn tại.

    Idempotent: nếu email đã có trong workspace, refresh `last_activity_at` +
    merge properties + return existing record.
    """
    e = _normalize_email(email)
    if not e or not EMAIL_RE.match(e):
        raise ValueError(f"invalid email: {email!r}")

    existing = (await db.execute(text("""
        SELECT id, lifecycle_stage, properties FROM crm_contacts
         WHERE workspace_id = :ws AND email = :email
    """), {"ws": workspace_id, "email": e})).first()

    if existing:
        # Merge new properties vào existing.properties (existing wins)
        merged_props = _serialize_jsonb(existing[2]) or {}
        if isinstance(properties, dict):
            for k, v in properties.items():
                merged_props.setdefault(k, v)
        await db.execute(text("""
            UPDATE crm_contacts
               SET full_name = COALESCE(full_name, :name),
                   phone = COALESCE(phone, :phone),
                   properties = CAST(:props AS JSONB),
                   last_activity_at = NOW(),
                   updated_at = NOW()
             WHERE id = :id
        """), {
            "id": existing[0], "name": full_name, "phone": phone,
            "props": json.dumps(merged_props),
        })
        return {
            "id": int(existing[0]),
            "email": e,
            "created": False,
            "lifecycle_stage": existing[1],
        }

    row = (await db.execute(text("""
        INSERT INTO crm_contacts
          (workspace_id, email, full_name, phone, source, lifecycle_stage,
           properties, last_activity_at)
        VALUES (:ws, :email, :name, :phone, :src, 'lead', CAST(:props AS JSONB), NOW())
        RETURNING id
    """), {
        "ws": workspace_id, "email": e, "name": full_name, "phone": phone,
        "src": source, "props": json.dumps(properties or {}),
    })).first()

    try:
        await audit_push(db, actor=None, workspace_id=workspace_id,
                         action="crm.lead.auto_create",
                         target=f"contact#{int(row[0])}", severity="ok",
                         metadata={"email": e, "source": source})
    except Exception:
        pass

    log.info("Auto-created contact ws=%s email=%s id=%s source=%s",
             workspace_id, e, row[0], source)
    return {
        "id": int(row[0]),
        "email": e,
        "created": True,
        "lifecycle_stage": "lead",
    }


# ═════════════════════════════════════════════════════════
# 2. Merge duplicate contacts
# ═════════════════════════════════════════════════════════
async def merge_duplicates(
    db: AsyncSession,
    *,
    workspace_id: str,
    primary_id: int,
    duplicate_ids: list[int],
) -> dict[str, Any]:
    """Merge duplicate contacts vào primary. Reassigns deals/activities/tickets,
    moves list memberships, then deletes duplicates.
    """
    if not duplicate_ids:
        return {"merged": 0, "primary_id": primary_id}

    primary = (await db.execute(text("""
        SELECT id, email, properties, tags FROM crm_contacts
         WHERE id = :id AND workspace_id = :ws
    """), {"id": primary_id, "ws": workspace_id})).first()
    if not primary:
        raise ValueError(f"primary contact #{primary_id} không tồn tại")

    dups = (await db.execute(text("""
        SELECT id, email, properties, tags FROM crm_contacts
         WHERE id = ANY(:ids) AND workspace_id = :ws AND id != :pid
    """), {"ids": duplicate_ids, "ws": workspace_id, "pid": primary_id})).all()
    if not dups:
        return {"merged": 0, "primary_id": primary_id}

    # Merge properties (primary wins) + union tags
    p_props = _serialize_jsonb(primary[2]) or {}
    p_tags: set[str] = set(primary[3] or [])
    for d in dups:
        d_props = _serialize_jsonb(d[2]) or {}
        for k, v in d_props.items():
            p_props.setdefault(k, v)
        for t in (d[3] or []):
            p_tags.add(str(t))

    dup_ids = [int(d[0]) for d in dups]

    # Reassign FK references — set company FK separately because activities don't have contact-fk-cascade-style logic.
    await db.execute(text("""
        UPDATE crm_deals SET contact_id = :pid
         WHERE contact_id = ANY(:ids) AND workspace_id = :ws
    """), {"pid": primary_id, "ids": dup_ids, "ws": workspace_id})
    await db.execute(text("""
        UPDATE crm_activities SET contact_id = :pid
         WHERE contact_id = ANY(:ids) AND workspace_id = :ws
    """), {"pid": primary_id, "ids": dup_ids, "ws": workspace_id})
    await db.execute(text("""
        UPDATE crm_tickets SET contact_id = :pid
         WHERE contact_id = ANY(:ids) AND workspace_id = :ws
    """), {"pid": primary_id, "ids": dup_ids, "ws": workspace_id})

    # List memberships: move members to primary (avoid PK conflict) then drop dup rows
    await db.execute(text("""
        INSERT INTO crm_list_members (list_id, contact_id, added_at)
        SELECT list_id, :pid, MIN(added_at) FROM crm_list_members
         WHERE contact_id = ANY(:ids)
         GROUP BY list_id
        ON CONFLICT DO NOTHING
    """), {"pid": primary_id, "ids": dup_ids})
    await db.execute(text("""
        DELETE FROM crm_list_members WHERE contact_id = ANY(:ids)
    """), {"ids": dup_ids})

    # Sequence enrollments: move to primary, skip if already enrolled
    await db.execute(text("""
        INSERT INTO crm_sequence_enrollments
          (sequence_id, contact_id, workspace_id, current_step, status,
           next_run_at, enrolled_at)
        SELECT sequence_id, :pid, workspace_id, current_step, status,
               next_run_at, enrolled_at
          FROM crm_sequence_enrollments
         WHERE contact_id = ANY(:ids) AND workspace_id = :ws
        ON CONFLICT (sequence_id, contact_id) DO NOTHING
    """), {"pid": primary_id, "ids": dup_ids, "ws": workspace_id})

    # Persist merged props/tags + delete dups
    await db.execute(text("""
        UPDATE crm_contacts
           SET properties = CAST(:props AS JSONB),
               tags = :tags,
               updated_at = NOW()
         WHERE id = :pid
    """), {"props": json.dumps(p_props), "tags": list(p_tags), "pid": primary_id})

    await db.execute(text("""
        DELETE FROM crm_contacts
         WHERE id = ANY(:ids) AND workspace_id = :ws
    """), {"ids": dup_ids, "ws": workspace_id})

    log.info("Merged %d duplicate contacts → primary=%s ws=%s",
             len(dup_ids), primary_id, workspace_id)
    return {"merged": len(dup_ids), "primary_id": primary_id, "merged_ids": dup_ids}


# ═════════════════════════════════════════════════════════
# 3. Deal scoring (rule-based)
# ═════════════════════════════════════════════════════════
async def compute_deal_score(
    db: AsyncSession,
    *,
    deal_id: int,
) -> int:
    """Rule-based deal score (0-100):
      + amount_vnd >= 100M           : +25
      + amount_vnd >= 1B             : +40 (cumulative w/ 100M tier)
      + has contact + email valid    : +10
      + has company                  : +15
      + recent activity (≤7 ngày)    : +20
      + multiple activities (≥3)     : +10
      + status='won'                 : 100 hard-set
      + status='lost'                : 0 hard-set
    """
    row = (await db.execute(text("""
        SELECT d.amount_vnd, d.contact_id, d.company_id, d.status,
               c.email,
               (SELECT COUNT(*) FROM crm_activities a
                  WHERE a.deal_id = d.id) AS act_count,
               (SELECT MAX(a.created_at) FROM crm_activities a
                  WHERE a.deal_id = d.id) AS last_act
          FROM crm_deals d
          LEFT JOIN crm_contacts c ON c.id = d.contact_id
         WHERE d.id = :id
    """), {"id": deal_id})).first()
    if not row:
        return 0

    if row[3] == "won":
        score = 100
    elif row[3] == "lost":
        score = 0
    else:
        score = 0
        amt = float(row[0] or 0)
        if amt >= 1_000_000_000:
            score += 40
        elif amt >= 100_000_000:
            score += 25
        elif amt >= 10_000_000:
            score += 10

        if row[1] is not None and row[4] and EMAIL_RE.match(row[4]):
            score += 10
        if row[2] is not None:
            score += 15

        act_count = int(row[5] or 0)
        if act_count >= 3:
            score += 10

        last_act = row[6]
        if last_act:
            age_days = (_now() - last_act).total_seconds() / 86400
            if age_days <= 7:
                score += 20
            elif age_days <= 30:
                score += 10

        score = max(0, min(100, score))

    await db.execute(text(
        "UPDATE crm_deals SET score = :s, updated_at = NOW() WHERE id = :id"
    ), {"s": score, "id": deal_id})
    return score


# ═════════════════════════════════════════════════════════
# 4. Sequence step processor (cron worker)
# ═════════════════════════════════════════════════════════
async def process_sequence_step(
    db: AsyncSession,
    *,
    batch_size: int = SEQUENCE_BATCH_SIZE,
) -> dict[str, Any]:
    """Cron worker — advance active enrollments có next_run_at <= now.

    For each ready enrollment:
      - Load sequence + steps
      - If current_step < len(steps): render + send email, advance step
      - Else: mark completed
      - On send failure: mark status='failed' + last_error
    """
    now = _now()
    rows = (await db.execute(text("""
        SELECT e.id, e.sequence_id, e.contact_id, e.workspace_id, e.current_step,
               s.steps, s.active, s.sender_email, s.name,
               c.email, c.full_name, c.phone, c.job_title, c.properties
          FROM crm_sequence_enrollments e
          JOIN crm_sequences s ON s.id = e.sequence_id
          JOIN crm_contacts c   ON c.id = e.contact_id
         WHERE e.status = 'active'
           AND (e.next_run_at IS NULL OR e.next_run_at <= :now)
           AND s.active = TRUE
         ORDER BY e.next_run_at NULLS FIRST
         LIMIT :lim
    """), {"now": now, "lim": batch_size})).all()

    sent = 0
    completed = 0
    failed = 0
    skipped = 0

    for r in rows:
        enroll_id = int(r[0])
        cur_step = int(r[4] or 0)
        steps = _serialize_jsonb(r[5]) or []
        if not isinstance(steps, list) or not steps:
            skipped += 1
            continue
        if cur_step >= len(steps):
            await db.execute(text("""
                UPDATE crm_sequence_enrollments
                   SET status = 'completed', completed_at = :now, next_run_at = NULL
                 WHERE id = :id
            """), {"id": enroll_id, "now": now})
            completed += 1
            continue

        step = steps[cur_step] if isinstance(steps[cur_step], dict) else {}
        contact_ctx = _flatten_contact({
            "email": r[9],
            "full_name": r[10],
            "phone": r[11],
            "job_title": r[12],
            "properties": _serialize_jsonb(r[13]),
        })
        subject = _render_template(str(step.get("subject") or ""), contact_ctx)[:240]
        body_html = _render_template(str(step.get("body_html") or ""), contact_ctx)
        body_text = _render_template(str(step.get("body_text") or ""), contact_ctx)

        if not r[9] or not subject or not body_html:
            await db.execute(text("""
                UPDATE crm_sequence_enrollments
                   SET status = 'failed', last_error = :err, next_run_at = NULL
                 WHERE id = :id
            """), {"id": enroll_id,
                   "err": "missing email/subject/body"[:1000]})
            failed += 1
            continue

        ok = False
        try:
            ok = await send_email(
                to=r[9], subject=subject, body_html=body_html,
                body_text=body_text or None,
            )
        except Exception as e:
            log.warning("sequence send error enroll=%s: %s", enroll_id, e)
            ok = False

        if not ok:
            await db.execute(text("""
                UPDATE crm_sequence_enrollments
                   SET status = 'failed',
                       last_error = :err,
                       next_run_at = NULL
                 WHERE id = :id
            """), {"id": enroll_id, "err": "smtp_failed"[:1000]})
            failed += 1
            continue

        # Advance step
        next_step_idx = cur_step + 1
        if next_step_idx >= len(steps):
            new_status = "completed"
            new_next = None
            completed_at = now
        else:
            new_status = "active"
            wait_days = int((steps[next_step_idx] or {}).get("wait_days") or 0)
            new_next = now + timedelta(days=wait_days)
            completed_at = None

        await db.execute(text("""
            UPDATE crm_sequence_enrollments
               SET current_step = :step,
                   status = :st,
                   next_run_at = :nrun,
                   completed_at = :cat,
                   last_error = NULL
             WHERE id = :id
        """), {
            "id": enroll_id, "step": next_step_idx, "st": new_status,
            "nrun": new_next, "cat": completed_at,
        })

        # Log activity
        await db.execute(text("""
            INSERT INTO crm_activities
              (workspace_id, contact_id, type, subject, description,
               completed, completed_at, created_by, metadata)
            VALUES (:ws, :cid, 'email', :sub, :desc, TRUE, :now,
                    'sequence@zenicloud', CAST(:meta AS JSONB))
        """), {
            "ws": r[3], "cid": int(r[2]), "sub": f"[Sequence] {subject}"[:240],
            "desc": f"Sequence '{r[8]}' step {cur_step + 1}/{len(steps)}",
            "now": now,
            "meta": json.dumps({"sequence_id": int(r[1]), "step": cur_step}),
        })
        await db.execute(text(
            "UPDATE crm_contacts SET last_activity_at = :now WHERE id = :id"
        ), {"now": now, "id": int(r[2])})
        sent += 1

    await db.commit()
    log.info("process_sequence_step: sent=%d completed=%d failed=%d skipped=%d",
             sent, completed, failed, skipped)
    return {"sent": sent, "completed": completed,
            "failed": failed, "skipped": skipped}


# ═════════════════════════════════════════════════════════
# 5. Dynamic list evaluator
# ═════════════════════════════════════════════════════════
def _build_dynamic_filter_sql(filt: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Convert filter JSON → SQL WHERE clauses.

    Supported keys:
      lifecycle_stage : str
      source          : str
      owner_email     : str
      tags_any        : list[str]   (contact has at least one)
      tags_all        : list[str]   (contact has all)
      created_after   : ISO date
      created_before  : ISO date
      activity_within_days : int    (last_activity_at within last N days)
      no_activity_days : int        (no activity for at least N days)
      has_company     : bool
      industry        : str (joined via crm_companies.industry — only if matches)
    """
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if not isinstance(filt, dict):
        return "TRUE", params

    if v := filt.get("lifecycle_stage"):
        clauses.append("c.lifecycle_stage = :flt_stage")
        params["flt_stage"] = str(v)
    if v := filt.get("source"):
        clauses.append("c.source = :flt_src")
        params["flt_src"] = str(v)
    if v := filt.get("owner_email"):
        clauses.append("c.owner_email = :flt_owner")
        params["flt_owner"] = str(v).lower()
    if isinstance(filt.get("tags_any"), list) and filt["tags_any"]:
        clauses.append("c.tags && CAST(:flt_tags_any AS TEXT[])")
        params["flt_tags_any"] = [str(t) for t in filt["tags_any"]]
    if isinstance(filt.get("tags_all"), list) and filt["tags_all"]:
        clauses.append("c.tags @> CAST(:flt_tags_all AS TEXT[])")
        params["flt_tags_all"] = [str(t) for t in filt["tags_all"]]
    if v := filt.get("created_after"):
        clauses.append("c.created_at >= CAST(:flt_after AS TIMESTAMPTZ)")
        params["flt_after"] = str(v)
    if v := filt.get("created_before"):
        clauses.append("c.created_at < CAST(:flt_before AS TIMESTAMPTZ)")
        params["flt_before"] = str(v)
    if isinstance(filt.get("activity_within_days"), int):
        clauses.append(
            "c.last_activity_at >= NOW() - (:flt_act_days || ' days')::INTERVAL"
        )
        params["flt_act_days"] = str(int(filt["activity_within_days"]))
    if isinstance(filt.get("no_activity_days"), int):
        clauses.append(
            "(c.last_activity_at IS NULL "
            "OR c.last_activity_at < NOW() - (:flt_no_act_days || ' days')::INTERVAL)"
        )
        params["flt_no_act_days"] = str(int(filt["no_activity_days"]))
    if filt.get("has_company") is True:
        clauses.append("c.company_id IS NOT NULL")
    if filt.get("has_company") is False:
        clauses.append("c.company_id IS NULL")
    if v := filt.get("industry"):
        clauses.append(
            "c.company_id IN (SELECT id FROM crm_companies "
            " WHERE workspace_id = :ws AND industry = :flt_industry)"
        )
        params["flt_industry"] = str(v)

    where = " AND ".join(clauses) if clauses else "TRUE"
    return where, params


async def evaluate_dynamic_list(
    db: AsyncSession,
    *,
    list_id: int,
    workspace_id: str,
) -> dict[str, Any]:
    """Refresh dynamic list members based on filter JSON.

    Strategy: clear list_members for this list, recompute matching contacts,
    insert. Update member_count + last_refreshed_at.
    """
    lst = (await db.execute(text("""
        SELECT id, type, filter FROM crm_lists
         WHERE id = :id AND workspace_id = :ws
    """), {"id": list_id, "ws": workspace_id})).first()
    if not lst:
        raise ValueError(f"list #{list_id} không tồn tại")
    if lst[1] != "dynamic":
        raise ValueError("chỉ refresh được dynamic list")

    filt = _serialize_jsonb(lst[2]) or {}
    where, params = _build_dynamic_filter_sql(filt)
    params["ws"] = workspace_id
    params["lid"] = list_id

    # Wipe + repopulate
    await db.execute(text("""
        DELETE FROM crm_list_members WHERE list_id = :lid
    """), {"lid": list_id})
    res = await db.execute(text(f"""
        INSERT INTO crm_list_members (list_id, contact_id, added_at)
        SELECT :lid, c.id, NOW()
          FROM crm_contacts c
         WHERE c.workspace_id = :ws AND ({where})
        ON CONFLICT DO NOTHING
    """), params)

    cnt = (await db.execute(text(
        "SELECT COUNT(*) FROM crm_list_members WHERE list_id = :lid"
    ), {"lid": list_id})).scalar_one()

    await db.execute(text("""
        UPDATE crm_lists
           SET member_count = :c, last_refreshed_at = NOW(), updated_at = NOW()
         WHERE id = :lid
    """), {"c": int(cnt or 0), "lid": list_id})

    log.info("evaluate_dynamic_list lid=%s ws=%s matched=%s",
             list_id, workspace_id, cnt)
    return {
        "list_id": list_id,
        "matched": int(cnt or 0),
        "inserted": int(res.rowcount or 0),
        "filter": filt,
    }


# ═════════════════════════════════════════════════════════
# 6. Bulk recompute deal scores (cron helper)
# ═════════════════════════════════════════════════════════
async def recompute_open_deal_scores(
    db: AsyncSession,
    *,
    workspace_id: str | None = None,
    limit: int = 500,
) -> int:
    """Recompute scores cho all open deals. Optionally scoped to a workspace.
    Use case: nightly cron để update lead scoring cho dashboard fresh."""
    where = "status = 'open'"
    params: dict[str, Any] = {"lim": limit}
    if workspace_id:
        where += " AND workspace_id = :ws"
        params["ws"] = workspace_id
    rows = (await db.execute(text(f"""
        SELECT id FROM crm_deals
         WHERE {where}
         ORDER BY updated_at ASC
         LIMIT :lim
    """), params)).all()
    n = 0
    for r in rows:
        try:
            await compute_deal_score(db, deal_id=int(r[0]))
            n += 1
        except Exception as e:
            log.warning("score recompute failed deal=%s: %s", r[0], e)
    await db.commit()
    return n
