"""
Daily quota check + email alerts.
Triggered by Cloud Scheduler (cron) tới /internal/cron/quota-check.
"""
from __future__ import annotations

import logging
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User
from app.services.email import send_email, render_alert_email

log = logging.getLogger("zeni.quota_alerts")


async def check_and_alert(db: AsyncSession) -> dict:
    """Find workspaces with quota > 80% used or wallet < 50.000đ → email Owner."""
    # 1. Subscription quota near limit
    subs = (await db.execute(text("""
        SELECT s.workspace_id, w.name, s.tier,
               s.used_agent_runs, s.quota_agent_runs,
               s.used_image_renders, s.quota_image_renders,
               s.used_text_tokens_out, s.quota_text_tokens_out,
               s.period_end
        FROM subscriptions s
        JOIN workspaces w ON w.id = s.workspace_id
        WHERE s.status = 'active' AND s.tier != 'free'
    """))).all()

    near_quota: list[dict] = []
    for s in subs:
        runs_pct = (s[3] / max(1, s[4])) * 100
        renders_pct = (s[5] / max(1, s[6])) * 100
        tokens_pct = (s[7] / max(1, s[8])) * 100
        if max(runs_pct, renders_pct, tokens_pct) >= 80:
            near_quota.append({
                "workspace_id": s[0], "workspace_name": s[1], "tier": s[2],
                "runs_pct": round(runs_pct, 1),
                "renders_pct": round(renders_pct, 1),
                "tokens_pct": round(tokens_pct, 1),
                "period_end": s[9].isoformat() if s[9] else None,
            })

    # 2. Wallet near zero
    low_wallets = (await db.execute(text("""
        SELECT wb.workspace_id, w.name, wb.balance_vnd
        FROM wallet_balances wb
        JOIN workspaces w ON w.id = wb.workspace_id
        WHERE wb.balance_vnd < 50000 AND wb.total_topped_up > 0
    """))).all()
    low_wallet_list = [
        {"workspace_id": r[0], "workspace_name": r[1], "balance_vnd": float(r[2])}
        for r in low_wallets
    ]

    # 3. Send email to Owners (each workspace's first Owner user)
    sent_count = 0
    for entry in near_quota:
        owner_email = await _find_workspace_owner_email(db, entry["workspace_id"])
        if not owner_email:
            continue
        subject, html = render_alert_email(
            kind="QUOTA_NEAR_LIMIT",
            summary=f"Workspace {entry['workspace_name']} tier {entry['tier']} — quota >80% used",
            details_html=f"""
              <h3>Sử dụng quota tháng này:</h3>
              <ul>
                <li>Agent runs: <strong>{entry['runs_pct']}%</strong></li>
                <li>Image renders: <strong>{entry['renders_pct']}%</strong></li>
                <li>Text tokens: <strong>{entry['tokens_pct']}%</strong></li>
              </ul>
              <p>Period end: {entry['period_end']}</p>
              <p>Cân nhắc upgrade tier hoặc top-up ví để tránh gián đoạn.</p>
            """
        )
        ok = await send_email(to=owner_email, subject=subject, body_html=html)
        if ok:
            sent_count += 1

    for entry in low_wallet_list:
        owner_email = await _find_workspace_owner_email(db, entry["workspace_id"])
        if not owner_email:
            continue
        subject, html = render_alert_email(
            kind="WALLET_LOW",
            summary=f"Số dư workspace {entry['workspace_name']} chỉ còn {int(entry['balance_vnd']):,}đ",
            details_html=f"""
              <p>Workspace <strong>{entry['workspace_name']}</strong> sắp hết tiền.</p>
              <p>Số dư hiện tại: <strong>{int(entry['balance_vnd']):,}đ</strong></p>
              <p>Top-up tại <a href='https://zenicloud.io/app/billing'>zenicloud.io/app/billing</a></p>
            """
        )
        ok = await send_email(to=owner_email, subject=subject, body_html=html)
        if ok:
            sent_count += 1

    return {
        "near_quota_count": len(near_quota),
        "low_wallet_count": len(low_wallet_list),
        "emails_sent": sent_count,
        "near_quota": near_quota,
        "low_wallet": low_wallet_list,
    }


async def _find_workspace_owner_email(db: AsyncSession, workspace_id: str) -> str | None:
    row = (await db.execute(text("""
        SELECT u.email FROM users u
        JOIN user_workspaces uw ON uw.user_id = u.id
        WHERE uw.workspace_id = :w AND (u.role = 'Owner' OR uw.role = 'Owner')
        ORDER BY u.created_at ASC LIMIT 1
    """), {"w": workspace_id})).first()
    return row[0] if row else None
