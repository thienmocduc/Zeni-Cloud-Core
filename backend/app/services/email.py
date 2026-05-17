"""
Zeni Cloud Core — SMTP email sender (Gmail).

Used by member invite, password reset, alerts. Gmail credentials read from
Secret Manager (GMAIL_SMTP_USER + GMAIL_SMTP_PASSWORD env vars).
"""
from __future__ import annotations

import logging
import os
from email.message import EmailMessage

import aiosmtplib

log = logging.getLogger("zeni.email")


SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
# Fallback chain: support cả 2 tên env (GMAIL_SMTP_* legacy + SMTP_* mới Cloud Run đã wire).
# Bug Viet Contech 2026-05-17: Cloud Run wire SMTP_USER/SMTP_PASSWORD nhưng code đọc
# GMAIL_SMTP_USER/GMAIL_SMTP_PASSWORD → empty → email skip → /register/zalo-otp/start upstream_error.
SMTP_USER = os.environ.get("GMAIL_SMTP_USER") or os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("GMAIL_SMTP_PASSWORD") or os.environ.get("SMTP_PASSWORD", "")
FROM_NAME = "Zeni Cloud"


def is_configured() -> bool:
    return bool(SMTP_USER and SMTP_PASS)


async def send_email(*, to: str, subject: str, body_html: str, body_text: str | None = None) -> bool:
    """Send email via Gmail SMTP. Returns True on success."""
    if not is_configured():
        log.warning("SMTP not configured (GMAIL_SMTP_USER/PASSWORD missing) — skipping email to %s", to)
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{FROM_NAME} <{SMTP_USER}>"
    msg["To"] = to
    msg.set_content(body_text or _strip_html(body_html))
    msg.add_alternative(body_html, subtype="html")

    try:
        await aiosmtplib.send(
            msg, hostname=SMTP_HOST, port=SMTP_PORT, start_tls=True,
            username=SMTP_USER, password=SMTP_PASS, timeout=30,
        )
        log.info("Email sent to %s — subject=%r", to, subject)
        return True
    except Exception as e:
        log.exception("Email send failed to %s: %s", to, e)
        return False


def _strip_html(html: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", html)


# ─── Email templates ─────────────────────────────────────
def render_invite_email(*, inviter_name: str, workspace_name: str, accept_url: str) -> tuple[str, str]:
    subject = f"[Zeni Cloud] {inviter_name} mời bạn vào workspace {workspace_name}"
    html = f"""
<!DOCTYPE html>
<html><body style="font-family: -apple-system, system-ui, sans-serif; max-width: 600px; margin: 0 auto; padding: 24px; background: #fafafa; color: #1a1a1a;">
  <div style="background: #08051F; padding: 28px; border-radius: 12px; text-align: center;">
    <div style="display: inline-block; width: 60px; height: 60px; border-radius: 14px; background: linear-gradient(135deg, #FDE68A, #A855F7); color: #1a0938; font-weight: 900; font-size: 32px; line-height: 60px;">Z</div>
    <h1 style="color: #FAF5FF; margin: 16px 0 4px; font-size: 24px;">Zeni Cloud</h1>
    <p style="color: #C4B5FD; margin: 0; font-size: 12px; letter-spacing: 0.1em;">UNIFIED CLOUD OS</p>
  </div>
  <div style="background: white; padding: 32px; border-radius: 12px; margin-top: 16px;">
    <h2 style="color: #1a0938; margin: 0 0 16px;">Bạn được mời vào <em style="color: #A855F7;">{workspace_name}</em></h2>
    <p>Xin chào,</p>
    <p><strong>{inviter_name}</strong> đã mời bạn tham gia workspace <strong>{workspace_name}</strong> trên Zeni Cloud — nền tảng cloud thống nhất cho doanh nghiệp Việt Nam.</p>
    <p>Click nút bên dưới để chấp nhận lời mời:</p>
    <div style="text-align: center; margin: 24px 0;">
      <a href="{accept_url}" style="display: inline-block; padding: 14px 32px; background: linear-gradient(135deg, #FDE68A, #F59E0B); color: #1a0938; text-decoration: none; font-weight: 700; border-radius: 8px;">Chấp nhận lời mời</a>
    </div>
    <p style="color: #666; font-size: 13px;">Hoặc copy link: <a href="{accept_url}">{accept_url}</a></p>
    <p style="color: #999; font-size: 12px; margin-top: 24px;">Lời mời này hết hạn sau 7 ngày. Nếu không phải bạn yêu cầu, bỏ qua email này.</p>
  </div>
  <div style="text-align: center; margin-top: 24px; color: #999; font-size: 11px;">
    Sản phẩm của <strong>Zeni Holdings</strong> · zenicloud.io
  </div>
</body></html>
"""
    return subject, html


def render_alert_email(*, kind: str, summary: str, details_html: str = "") -> tuple[str, str]:
    subject = f"[Zeni Cloud · ALERT] {kind}: {summary[:80]}"
    html = f"""
<!DOCTYPE html>
<html><body style="font-family: system-ui, sans-serif;">
  <h2 style="color:#dc2626;">⚠ Zeni Cloud Alert</h2>
  <p><strong>Kind:</strong> {kind}<br/>
     <strong>Summary:</strong> {summary}</p>
  {details_html}
  <p style="color:#999;font-size:11px;margin-top:24px;">zenicloud.io · alert engine</p>
</body></html>
"""
    return subject, html
