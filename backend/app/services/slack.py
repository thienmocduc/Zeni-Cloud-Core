"""
Zeni Cloud Core — Slack notify wrapper (Stream A4).

Two modes:
1. Incoming Webhook  (user provides webhook_url starting `https://hooks.slack.com/...`)
2. Bot API           (user provides token + channel; calls chat.postMessage)

User pays Slack for messaging quota — Zeni doesn't bill the call itself.

Tokens / webhook URLs are MASKED in logs (show first 8 chars + "...").
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("zeni.slack")

HTTP_TIMEOUT = 30.0
SLACK_WEBHOOK_PREFIX = "https://hooks.slack.com/"
SLACK_API_POST_MESSAGE = "https://slack.com/api/chat.postMessage"


def _mask(secret: str, show: int = 8) -> str:
    """Mask a secret/url for safe logging — show first N chars only."""
    if not secret:
        return "<empty>"
    if len(secret) <= show:
        return f"{secret[:2]}..."
    return f"{secret[:show]}..."


async def send_webhook(
    webhook_url: str,
    text: str,
    blocks: list[dict] | None = None,
    attachments: list[dict] | None = None,
) -> dict[str, Any]:
    """
    Post to a Slack incoming webhook URL.

    Returns:
        {ok: bool, response_status: int}

    Raises:
        ValueError: if webhook_url does not start with https://hooks.slack.com/
        RuntimeError: upstream call failed.
    """
    if not webhook_url or not webhook_url.startswith(SLACK_WEBHOOK_PREFIX):
        raise ValueError("Slack webhook URL phải bắt đầu https://hooks.slack.com/")

    payload: dict[str, Any] = {"text": text}
    if blocks is not None:
        payload["blocks"] = blocks
    if attachments is not None:
        payload["attachments"] = attachments

    log.info(
        "[slack] webhook → url=%s len_text=%d blocks=%s attachments=%s",
        _mask(webhook_url, 30),  # safe to show host portion
        len(text),
        len(blocks) if blocks else 0,
        len(attachments) if attachments else 0,
    )

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.post(
                webhook_url,
                json=payload,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "User-Agent": "ZeniCloud/1.0",
                },
            )
        ok = 200 <= r.status_code < 300
        if not ok:
            # Avoid logging response body in case it echoes the payload
            log.warning("[slack] webhook non-2xx status=%s", r.status_code)
        return {"ok": ok, "response_status": r.status_code}
    except httpx.TimeoutException:
        log.warning("[slack] webhook timeout url=%s", _mask(webhook_url, 30))
        raise RuntimeError("Slack webhook timeout (>30s)")
    except httpx.HTTPError as e:
        log.warning("[slack] webhook http error: %s", e)
        raise RuntimeError(f"Slack webhook lỗi: {type(e).__name__}")


async def post_message(
    token: str,
    channel: str,
    text: str,
    blocks: list[dict] | None = None,
) -> dict[str, Any]:
    """
    Call Slack Bot API chat.postMessage.

    Args:
        token: xoxb-... bot token (provided by user, never persisted by Zeni).
        channel: e.g. "#general" or "C0123456".
        text: fallback text.
        blocks: optional Block Kit blocks.

    Returns:
        {ok: bool, ts: str, channel: str}

    Raises:
        ValueError: token / channel missing.
        RuntimeError: Slack API returned ok=false or HTTP error.
    """
    if not token:
        raise ValueError("Token Slack bị thiếu")
    if not channel:
        raise ValueError("Channel Slack bị thiếu")

    payload: dict[str, Any] = {"channel": channel, "text": text}
    if blocks is not None:
        payload["blocks"] = blocks

    log.info(
        "[slack] post_message → channel=%s token=%s len_text=%d",
        channel, _mask(token), len(text),
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "ZeniCloud/1.0",
    }

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.post(SLACK_API_POST_MESSAGE, json=payload, headers=headers)
        try:
            body = r.json()
        except Exception:
            body = {}
        if r.status_code >= 400:
            log.warning("[slack] post_message HTTP %s", r.status_code)
            raise RuntimeError(f"Slack API lỗi HTTP {r.status_code}")
        if not body.get("ok"):
            err = body.get("error", "unknown_error")
            # `error` is a Slack-defined enum (invalid_auth, channel_not_found, ...) — safe to log
            log.warning("[slack] post_message ok=false error=%s", err)
            raise RuntimeError(f"Slack API trả lỗi: {err}")
        return {
            "ok": True,
            "ts": body.get("ts", ""),
            "channel": body.get("channel", channel),
        }
    except httpx.TimeoutException:
        log.warning("[slack] post_message timeout channel=%s", channel)
        raise RuntimeError("Slack API timeout (>30s)")
    except httpx.HTTPError as e:
        log.warning("[slack] post_message http error: %s", e)
        raise RuntimeError(f"Slack API lỗi: {type(e).__name__}")
