"""
Zeni Mail — engine service.

Responsibilities:
  - enqueue_campaign(campaign_id) → expand list/segment into mail_sends rows
  - send_pending_emails()        → cron worker: dequeue + deliver via SMTP
  - process_automation_enrollments() → cron worker: advance drip steps
  - render_template(html, subscriber) → Mustache-style {{var}} substitution
  - inject_tracking(html, message_id, base_url) → open pixel + click rewrite

Re-uses app.services.email.send_email for actual SMTP delivery (Gmail).
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote_plus

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.email import send_email

log = logging.getLogger("zeni.mail.engine")


# ─── Config ─────────────────────────────────────────────
TRACK_BASE_URL = os.environ.get(
    "ZENI_MAIL_TRACK_BASE",
    "https://api.zenicloud.io/api/v1/mail/track",
)
SEND_BATCH_SIZE = int(os.environ.get("ZENI_MAIL_SEND_BATCH", "100"))
MAX_HTML_BYTES = 1_000_000   # 1 MB hard cap per email


# ─── Variable rendering ────────────────────────────────
_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_\.]*)\s*\}\}")


def _flatten_subscriber_fields(sub: dict) -> dict[str, Any]:
    """Build a flat name → value lookup for {{var}} substitution.

    Supports:
      first_name, last_name, email, full_name (computed)
      tags (joined with ", ")
      anything in custom_fields JSON (top-level keys)
    """
    out: dict[str, Any] = {
        "email": sub.get("email") or "",
        "first_name": sub.get("first_name") or "",
        "last_name": sub.get("last_name") or "",
    }
    fn = (sub.get("first_name") or "").strip()
    ln = (sub.get("last_name") or "").strip()
    out["full_name"] = (f"{fn} {ln}").strip() or out["email"]
    out["name"] = out["full_name"]   # alias

    tags = sub.get("tags") or []
    if isinstance(tags, list):
        out["tags"] = ", ".join(str(t) for t in tags)

    cf = sub.get("custom_fields") or {}
    if isinstance(cf, str):
        try:
            cf = json.loads(cf)
        except Exception:
            cf = {}
    if isinstance(cf, dict):
        for k, v in cf.items():
            # Don't allow custom_fields to override core identity keys
            if k not in out:
                out[k] = v
    return out


def render_template(body: str, subscriber: dict | None) -> str:
    """Mustache-like {{var}} substitution. Missing vars → empty string."""
    if not body:
        return ""
    if not subscriber:
        return _VAR_RE.sub("", body)

    flat = _flatten_subscriber_fields(subscriber)

    def _replace(match: re.Match) -> str:
        key = match.group(1)
        # Support dotted custom_fields (e.g. {{custom_fields.company}})
        if "." in key:
            parts = key.split(".")
            cur: Any = flat
            for p in parts:
                if isinstance(cur, dict):
                    cur = cur.get(p, "")
                else:
                    cur = ""
                    break
            return str(cur) if cur is not None else ""
        val = flat.get(key, "")
        return str(val) if val is not None else ""

    return _VAR_RE.sub(_replace, body)


# ─── Tracking injection ────────────────────────────────
class _LinkRewriter(HTMLParser):
    """Rewrite href attributes on <a> tags to the click-tracking endpoint."""

    def __init__(self, message_id: str, base_url: str):
        super().__init__(convert_charrefs=False)
        self.message_id = message_id
        self.base = base_url
        self.parts: list[str] = []

    def _attrs_to_str(self, attrs: list[tuple[str, str | None]]) -> str:
        out = []
        for name, val in attrs:
            if val is None:
                out.append(name)
            else:
                escaped = (val.replace("&", "&amp;")
                              .replace('"', "&quot;")
                              .replace("<", "&lt;")
                              .replace(">", "&gt;"))
                out.append(f'{name}="{escaped}"')
        return (" " + " ".join(out)) if out else ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            new_attrs: list[tuple[str, str | None]] = []
            for name, val in attrs:
                if name.lower() == "href" and val:
                    # Skip mailto:, tel:, anchor, and unsubscribe links
                    low = val.strip().lower()
                    if (low.startswith("mailto:") or low.startswith("tel:")
                            or low.startswith("#")
                            or "/unsubscribe" in low):
                        new_attrs.append((name, val))
                    else:
                        wrapped = (f"{self.base}/click/{self.message_id}"
                                   f"?url={quote_plus(val)}")
                        new_attrs.append((name, wrapped))
                else:
                    new_attrs.append((name, val))
            self.parts.append(f"<{tag}{self._attrs_to_str(new_attrs)}>")
        else:
            self.parts.append(f"<{tag}{self._attrs_to_str(attrs)}>")

    def handle_endtag(self, tag: str) -> None:
        self.parts.append(f"</{tag}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # Self-closing: rewrite same as starttag, but with /
        if tag.lower() == "a":
            self.handle_starttag(tag, attrs)
            return
        self.parts.append(f"<{tag}{self._attrs_to_str(attrs)}/>")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self.parts.append(f"<!--{data}-->")

    def render(self) -> str:
        return "".join(self.parts)


def inject_tracking(html: str, message_id: str, base_url: str | None = None) -> str:
    """Inject 1×1 open-tracking pixel and rewrite anchor hrefs.

    Idempotent best-effort: if HTML parsing fails we fall back to original.
    """
    if not html or not message_id:
        return html or ""
    base = base_url or TRACK_BASE_URL

    # 1. Rewrite <a href> to click-tracking
    try:
        rw = _LinkRewriter(message_id, base)
        rw.feed(html)
        rw.close()
        html_rewritten = rw.render()
    except Exception as e:
        log.warning("inject_tracking link-rewrite failed: %s", e)
        html_rewritten = html

    # 2. Append tracking pixel just before </body>, or at end if no body
    pixel = (
        f'<img src="{base}/open/{message_id}.gif" '
        f'width="1" height="1" alt="" '
        f'style="display:block;width:1px;height:1px;border:0;" />'
    )
    body_close = re.search(r"</body\s*>", html_rewritten, re.IGNORECASE)
    if body_close:
        idx = body_close.start()
        html_rewritten = html_rewritten[:idx] + pixel + html_rewritten[idx:]
    else:
        html_rewritten = html_rewritten + pixel

    return html_rewritten


# ─── Campaign expansion ────────────────────────────────
async def _select_subscribers_for_campaign(
    db: AsyncSession,
    *,
    workspace_id: str,
    list_id: int | None,
    segment_filter: dict | None,
) -> list[dict]:
    """Build the recipient list for a campaign.

    segment_filter accepts {"tags":[...], "status":"active|all"}.
    Defaults to status='active'.
    """
    sf = segment_filter or {}
    status = sf.get("status") or "active"
    tags_filter = sf.get("tags") or []

    sql = """
        SELECT id, list_id, workspace_id, email, first_name, last_name,
               custom_fields, tags, status
          FROM mail_subscribers
         WHERE workspace_id = :ws
    """
    params: dict[str, Any] = {"ws": workspace_id}

    if list_id is not None:
        sql += " AND list_id = :lid"
        params["lid"] = list_id

    if status != "all":
        sql += " AND status = :status"
        params["status"] = status

    if tags_filter and isinstance(tags_filter, list):
        sql += " AND tags && CAST(:tags AS TEXT[])"
        params["tags"] = tags_filter

    rows = (await db.execute(text(sql), params)).mappings().all()
    return [dict(r) for r in rows]


def _make_message_id(campaign_id: int | None, automation_id: int | None,
                     subscriber_id: int) -> str:
    suffix = secrets.token_urlsafe(12)
    if campaign_id:
        return f"c{campaign_id}-s{subscriber_id}-{suffix}"
    if automation_id:
        return f"a{automation_id}-s{subscriber_id}-{suffix}"
    return f"x{subscriber_id}-{suffix}"


async def enqueue_campaign(db: AsyncSession, campaign_id: int) -> dict:
    """Expand campaign into per-recipient mail_sends rows; mark campaign 'sending'.

    Returns {queued: N}. Does NOT actually send — that's send_pending_emails.
    """
    crow = (await db.execute(text("""
        SELECT id, workspace_id, name, subject, from_email, from_name, reply_to,
               body_html, body_text, list_id, segment_filter, status
          FROM mail_campaigns
         WHERE id = :cid
    """), {"cid": campaign_id})).mappings().first()
    if not crow:
        raise ValueError(f"campaign {campaign_id} not found")

    if crow["status"] in ("sending", "sent"):
        return {"queued": 0, "already_running": True, "status": crow["status"]}

    seg = crow["segment_filter"]
    if isinstance(seg, str):
        try:
            seg = json.loads(seg)
        except Exception:
            seg = None

    subs = await _select_subscribers_for_campaign(
        db,
        workspace_id=crow["workspace_id"],
        list_id=crow["list_id"],
        segment_filter=seg,
    )

    queued = 0
    now = datetime.now(timezone.utc)
    subject = crow["subject"]
    for sub in subs:
        if not sub.get("email"):
            continue
        msg_id = _make_message_id(campaign_id, None, sub["id"])
        # Render personalized subject too
        rendered_subject = render_template(subject, sub)
        try:
            await db.execute(text("""
                INSERT INTO mail_sends
                  (campaign_id, automation_id, workspace_id, subscriber_id,
                   to_email, subject, message_id, status)
                VALUES
                  (:cid, NULL, :ws, :sid, :email, :subj, :mid, 'queued')
                ON CONFLICT (message_id) DO NOTHING
            """), {
                "cid": campaign_id, "ws": crow["workspace_id"],
                "sid": sub["id"], "email": sub["email"],
                "subj": rendered_subject[:500], "mid": msg_id,
            })
            queued += 1
        except Exception as e:
            log.warning("enqueue_campaign insert failed for sub=%s: %s", sub.get("id"), e)

    await db.execute(text("""
        UPDATE mail_campaigns
           SET status = 'sending',
               total_recipients = :n,
               started_at = :now
         WHERE id = :cid
    """), {"cid": campaign_id, "n": queued, "now": now})
    await db.commit()
    log.info("[mail_engine] campaign=%s queued=%s", campaign_id, queued)
    return {"queued": queued, "campaign_id": campaign_id}


# ─── Send worker ───────────────────────────────────────
async def _record_send_result(
    db: AsyncSession,
    *,
    send_id: int,
    ok: bool,
    error: str | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    if ok:
        await db.execute(text("""
            UPDATE mail_sends
               SET status = 'sent', sent_at = :now, error_message = NULL
             WHERE id = :id
        """), {"id": send_id, "now": now})
    else:
        await db.execute(text("""
            UPDATE mail_sends
               SET status = 'bounced', error_message = :err, bounce_reason = :err
             WHERE id = :id
        """), {"id": send_id, "err": (error or "unknown")[:1000]})


async def _bump_campaign_counter(
    db: AsyncSession, campaign_id: int | None, *, sent: int = 0, bounced: int = 0
) -> None:
    if not campaign_id:
        return
    await db.execute(text("""
        UPDATE mail_campaigns
           SET sent_count = sent_count + :s,
               bounce_count = bounce_count + :b
         WHERE id = :cid
    """), {"cid": campaign_id, "s": sent, "b": bounced})


async def _maybe_finalize_campaign(db: AsyncSession, campaign_id: int) -> None:
    """If no more queued sends remain, mark campaign 'sent' and stamp completed_at."""
    remaining = (await db.execute(text("""
        SELECT COUNT(*)::INT FROM mail_sends
         WHERE campaign_id = :cid AND status = 'queued'
    """), {"cid": campaign_id})).scalar() or 0
    if remaining == 0:
        await db.execute(text("""
            UPDATE mail_campaigns
               SET status = 'sent', completed_at = NOW()
             WHERE id = :cid AND status = 'sending'
        """), {"cid": campaign_id})


async def send_pending_emails(db: AsyncSession, *, batch_size: int | None = None) -> dict:
    """Cron worker. Pull queued sends, deliver via SMTP, update statuses.

    Returns {sent, failed, processed}.
    """
    n = batch_size or SEND_BATCH_SIZE
    rows = (await db.execute(text("""
        SELECT s.id, s.campaign_id, s.automation_id, s.workspace_id,
               s.subscriber_id, s.to_email, s.message_id,
               COALESCE(c.subject, s.subject) AS subject,
               c.from_email, c.from_name, c.reply_to,
               c.body_html, c.body_text
          FROM mail_sends s
          LEFT JOIN mail_campaigns c ON c.id = s.campaign_id
         WHERE s.status = 'queued'
         ORDER BY s.id ASC
         LIMIT :n
    """), {"n": n})).mappings().all()

    if not rows:
        return {"sent": 0, "failed": 0, "processed": 0}

    # Fetch subscribers in bulk for personalization
    sub_ids = [r["subscriber_id"] for r in rows if r["subscriber_id"]]
    sub_map: dict[int, dict] = {}
    if sub_ids:
        sub_rows = (await db.execute(text("""
            SELECT id, email, first_name, last_name, custom_fields, tags
              FROM mail_subscribers
             WHERE id = ANY(:ids)
        """), {"ids": sub_ids})).mappings().all()
        for s in sub_rows:
            sub_map[s["id"]] = dict(s)

    sent_total = 0
    failed_total = 0
    campaigns_touched: dict[int, dict[str, int]] = {}

    for r in rows:
        try:
            sub = sub_map.get(r["subscriber_id"]) if r["subscriber_id"] else None
            html = r["body_html"] or ""
            text_body = r["body_text"]
            subject = r["subject"] or "(no subject)"

            # Personalize
            html = render_template(html, sub)
            if text_body:
                text_body = render_template(text_body, sub)
            subject = render_template(subject, sub)

            # Tracking
            if len(html.encode("utf-8")) > MAX_HTML_BYTES:
                raise ValueError("HTML body exceeds size limit")
            html = inject_tracking(html, r["message_id"])

            ok = await send_email(
                to=r["to_email"],
                subject=subject,
                body_html=html,
                body_text=text_body,
            )
            await _record_send_result(db, send_id=r["id"], ok=ok,
                                       error=None if ok else "smtp_send_returned_false")
            if ok:
                sent_total += 1
                if r["campaign_id"]:
                    cs = campaigns_touched.setdefault(int(r["campaign_id"]), {"s": 0, "b": 0})
                    cs["s"] += 1
            else:
                failed_total += 1
                if r["campaign_id"]:
                    cs = campaigns_touched.setdefault(int(r["campaign_id"]), {"s": 0, "b": 0})
                    cs["b"] += 1
        except Exception as e:
            log.exception("send_pending_emails failed for send=%s: %s", r["id"], e)
            try:
                await _record_send_result(db, send_id=r["id"], ok=False,
                                           error=str(e)[:500])
                failed_total += 1
                if r["campaign_id"]:
                    cs = campaigns_touched.setdefault(int(r["campaign_id"]), {"s": 0, "b": 0})
                    cs["b"] += 1
            except Exception:
                pass

    # Bump campaign counters
    for cid, cnt in campaigns_touched.items():
        await _bump_campaign_counter(db, cid, sent=cnt["s"], bounced=cnt["b"])
        await _maybe_finalize_campaign(db, cid)

    await db.commit()
    log.info("[mail_engine] send_pending sent=%s failed=%s", sent_total, failed_total)
    return {"sent": sent_total, "failed": failed_total, "processed": len(rows)}


# ─── Automation worker ────────────────────────────────
async def enroll_subscriber(
    db: AsyncSession,
    *,
    automation_id: int,
    subscriber_id: int,
    workspace_id: str,
) -> dict:
    """Add a subscriber to an automation (idempotent). Sets next_step_at = NOW so
    the worker picks them up on the next tick.
    """
    now = datetime.now(timezone.utc)
    await db.execute(text("""
        INSERT INTO mail_enrollments
          (automation_id, subscriber_id, workspace_id, current_step,
           next_step_at, status)
        VALUES (:aid, :sid, :ws, 0, :now, 'active')
        ON CONFLICT (automation_id, subscriber_id) DO NOTHING
    """), {"aid": automation_id, "sid": subscriber_id, "ws": workspace_id, "now": now})
    await db.commit()
    return {"automation_id": automation_id, "subscriber_id": subscriber_id, "enrolled": True}


async def process_automation_enrollments(
    db: AsyncSession, *, batch_size: int = 200
) -> dict:
    """Cron worker. Advance every active enrollment whose next_step_at <= now.

    For each: render the step's template (if any), enqueue a mail_send row,
    advance current_step (or mark completed when no more steps).
    """
    now = datetime.now(timezone.utc)
    rows = (await db.execute(text("""
        SELECT e.id, e.automation_id, e.subscriber_id, e.workspace_id,
               e.current_step, a.steps, a.is_active
          FROM mail_enrollments e
          JOIN mail_automations a ON a.id = e.automation_id
         WHERE e.status = 'active'
           AND a.is_active = TRUE
           AND e.next_step_at IS NOT NULL
           AND e.next_step_at <= :now
         ORDER BY e.next_step_at ASC
         LIMIT :n
    """), {"now": now, "n": batch_size})).mappings().all()

    advanced = 0
    completed = 0
    queued = 0

    for r in rows:
        steps = r["steps"]
        if isinstance(steps, str):
            try:
                steps = json.loads(steps)
            except Exception:
                steps = []
        if not isinstance(steps, list):
            steps = []

        cur_idx = int(r["current_step"] or 0)
        if cur_idx >= len(steps):
            # No more steps → completed
            await db.execute(text("""
                UPDATE mail_enrollments
                   SET status = 'completed', next_step_at = NULL
                 WHERE id = :eid
            """), {"eid": r["id"]})
            completed += 1
            continue

        step = steps[cur_idx] or {}
        template_id = step.get("template_id")

        # Load subscriber
        sub_row = (await db.execute(text("""
            SELECT id, email, first_name, last_name, custom_fields, tags, status
              FROM mail_subscribers WHERE id = :sid
        """), {"sid": r["subscriber_id"]})).mappings().first()

        if not sub_row or sub_row["status"] in ("unsubscribed", "complained", "bounced"):
            # Exit automation
            await db.execute(text("""
                UPDATE mail_enrollments
                   SET status = 'exited', next_step_at = NULL
                 WHERE id = :eid
            """), {"eid": r["id"]})
            continue

        if template_id:
            tpl = (await db.execute(text("""
                SELECT subject, body_html, body_text
                  FROM mail_templates WHERE id = :tid
            """), {"tid": template_id})).mappings().first()
            if tpl:
                msg_id = _make_message_id(None, r["automation_id"], r["subscriber_id"])
                rendered_subject = render_template(tpl["subject"], dict(sub_row))
                # Automations have no parent campaign, so we render + send inline
                # right here rather than going through the queued-by-campaign path.
                html = render_template(tpl["body_html"] or "", dict(sub_row))
                text_body = render_template(tpl["body_text"] or "", dict(sub_row)) if tpl["body_text"] else None
                html = inject_tracking(html, msg_id)
                # Insert send row first so the tracking pixel resolves on open
                await db.execute(text("""
                    INSERT INTO mail_sends
                      (campaign_id, automation_id, workspace_id, subscriber_id,
                       to_email, subject, message_id, status, sent_at)
                    VALUES
                      (NULL, :aid, :ws, :sid, :email, :subj, :mid, 'queued', NULL)
                """), {
                    "aid": r["automation_id"], "ws": r["workspace_id"],
                    "sid": r["subscriber_id"], "email": sub_row["email"],
                    "subj": rendered_subject[:500], "mid": msg_id,
                })
                # Find the inserted send id
                sid_row = (await db.execute(text(
                    "SELECT id FROM mail_sends WHERE message_id = :mid"
                ), {"mid": msg_id})).first()
                send_id = int(sid_row[0]) if sid_row else 0

                ok = False
                try:
                    ok = await send_email(
                        to=sub_row["email"], subject=rendered_subject,
                        body_html=html, body_text=text_body,
                    )
                except Exception as e:
                    log.exception("automation send failed: %s", e)
                if send_id:
                    await _record_send_result(db, send_id=send_id, ok=ok,
                                               error=None if ok else "automation_send_failed")
                queued += 1

        # Advance to next step
        next_idx = cur_idx + 1
        if next_idx >= len(steps):
            await db.execute(text("""
                UPDATE mail_enrollments
                   SET current_step = :ns, status = 'completed', next_step_at = NULL
                 WHERE id = :eid
            """), {"ns": next_idx, "eid": r["id"]})
            completed += 1
        else:
            next_step = steps[next_idx] or {}
            wait_h = float(next_step.get("wait_hours") or 0)
            next_at = datetime.now(timezone.utc) + timedelta(hours=wait_h)
            await db.execute(text("""
                UPDATE mail_enrollments
                   SET current_step = :ns, next_step_at = :nat
                 WHERE id = :eid
            """), {"ns": next_idx, "nat": next_at, "eid": r["id"]})
            advanced += 1

    await db.commit()
    log.info("[mail_engine] automation processed=%s advanced=%s completed=%s queued=%s",
             len(rows), advanced, completed, queued)
    return {
        "processed": len(rows),
        "advanced": advanced,
        "completed": completed,
        "queued": queued,
    }


# ─── Trigger helpers ──────────────────────────────────
async def trigger_subscribe_automations(
    db: AsyncSession, *, list_id: int, subscriber_id: int, workspace_id: str
) -> int:
    """When a subscriber confirms or is added 'active' to a list, enroll them
    into all automations whose trigger_type='subscribe' and list_id matches.
    """
    rows = (await db.execute(text("""
        SELECT id FROM mail_automations
         WHERE workspace_id = :ws
           AND is_active = TRUE
           AND trigger_type = 'subscribe'
           AND (list_id = :lid OR list_id IS NULL)
    """), {"ws": workspace_id, "lid": list_id})).all()
    n = 0
    for (aid,) in rows:
        await enroll_subscriber(
            db, automation_id=int(aid), subscriber_id=subscriber_id,
            workspace_id=workspace_id,
        )
        n += 1
    return n
