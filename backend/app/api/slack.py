"""
Slack notify API — Stream A4.

Two endpoints:
- POST /api/v1/slack/webhook?ws=  → user-provided incoming webhook URL
- POST /api/v1/slack/post?ws=     → user-provided bot token + channel (chat.postMessage)

Security:
  - Auth required (JWT or PAT scope notify|full)
  - require_workspace_access(ws, me)
  - audit_push(action="notify.slack")
  - Billing free (user pays Slack for messaging quota)

Tokens / webhook URLs are NEVER persisted by Zeni and only masked-logged.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services.audit import audit_push
from app.services.slack import post_message, send_webhook

log = logging.getLogger("zeni.api.slack")
router = APIRouter(prefix="/slack", tags=["slack"])


def _check_notify_scope(me: CurrentUser) -> None:
    if me.auth_scope and not any(s in me.auth_scope for s in ("notify", "full")):
        raise HTTPException(
            status_code=403,
            detail="Token thiếu scope 'notify' (hoặc 'full')",
        )


# ─── Webhook mode ────────────────────────────────────────
class SlackWebhookIn(BaseModel):
    webhook_url: str = Field(min_length=20, max_length=500,
                             description="Slack incoming webhook URL.")
    text: str = Field(min_length=1, max_length=40000,
                      description="Fallback plain text content.")
    blocks: list[dict] | None = Field(default=None, description="Block Kit blocks.")
    attachments: list[dict] | None = Field(default=None, description="Legacy attachments.")


class SlackWebhookOut(BaseModel):
    ok: bool
    response_status: int


@router.post("/webhook", response_model=SlackWebhookOut)
async def slack_webhook_endpoint(
    ws: str,
    data: SlackWebhookIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SlackWebhookOut:
    """Send a message via a Slack incoming webhook URL."""
    await require_workspace_access(ws, me)
    _check_notify_scope(me)

    try:
        result = await send_webhook(
            webhook_url=data.webhook_url,
            text=data.text,
            blocks=data.blocks,
            attachments=data.attachments,
        )
    except ValueError as e:
        # Bad webhook prefix
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        await audit_push(
            db, actor=me.email, workspace_id=ws, action="notify.slack",
            target="webhook:error", severity="err",
            metadata={"mode": "webhook", "error": str(e)[:200]},
        )
        await db.commit()
        raise HTTPException(status_code=502, detail=str(e))

    await audit_push(
        db, actor=me.email, workspace_id=ws, action="notify.slack",
        target="webhook",
        severity="ok" if result["ok"] else "warn",
        metadata={
            "mode": "webhook",
            "response_status": result["response_status"],
            "len_text": len(data.text),
            "has_blocks": data.blocks is not None,
            "has_attachments": data.attachments is not None,
        },
    )
    await db.commit()
    return SlackWebhookOut(**result)


# ─── Bot API mode (chat.postMessage) ──────────────────────
class SlackPostIn(BaseModel):
    token: str = Field(min_length=10, max_length=400,
                       description="Slack bot token (xoxb-...).")
    channel: str = Field(min_length=1, max_length=100,
                         description="Channel name (#general) or ID (C0123456).")
    text: str = Field(min_length=1, max_length=40000)
    blocks: list[dict] | None = Field(default=None)


class SlackPostOut(BaseModel):
    ok: bool
    ts: str
    channel: str


@router.post("/post", response_model=SlackPostOut)
async def slack_post_endpoint(
    ws: str,
    data: SlackPostIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SlackPostOut:
    """Post a message via Slack Bot API (chat.postMessage)."""
    await require_workspace_access(ws, me)
    _check_notify_scope(me)

    try:
        result = await post_message(
            token=data.token,
            channel=data.channel,
            text=data.text,
            blocks=data.blocks,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        await audit_push(
            db, actor=me.email, workspace_id=ws, action="notify.slack",
            target=f"post:{data.channel}", severity="err",
            metadata={"mode": "post", "error": str(e)[:200], "channel": data.channel},
        )
        await db.commit()
        # Slack auth/channel errors → 502 (upstream)
        raise HTTPException(status_code=502, detail=str(e))

    await audit_push(
        db, actor=me.email, workspace_id=ws, action="notify.slack",
        target=f"post:{result.get('channel', data.channel)}",
        severity="ok",
        metadata={
            "mode": "post",
            "channel": result.get("channel", data.channel),
            "ts": result.get("ts", ""),
            "len_text": len(data.text),
            "has_blocks": data.blocks is not None,
        },
    )
    await db.commit()
    return SlackPostOut(
        ok=bool(result.get("ok")),
        ts=result.get("ts", ""),
        channel=result.get("channel", data.channel),
    )
