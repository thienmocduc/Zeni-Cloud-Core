"""
Zeni Cloud — CTO Auto-Lock Service.

Đếm số lần vi phạm trong cửa sổ thời gian, tự khóa workspace+IP khi vượt threshold.

Policy mặc định (configurable qua env):
  - 3 critical violations / 5 phút → lock 30 phút
  - 5 high+ violations / 10 phút → lock 60 phút
  - 10 warn+ violations / 30 phút → lock 15 phút

Lock storage: bảng `cto_workspace_locks` (workspace_id, ip_address, locked_at,
unlock_at, reason). Check trước mỗi request — nếu locked, từ chối ngay.

Unlock:
  - Auto khi `unlock_at < NOW()`
  - Manual: Chairman gọi /api/v1/cto/admin/unlock (Owner only)

Notify:
  - Mọi lock đều ghi audit_push severity=critical
  - Lock 60+ phút → email Chairman (best-effort)
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text as _sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.audit import audit_push

log = logging.getLogger("zeni.cto.auto_lock")


# ─────────────────────────────────────────────────────────────
# Threshold policy — tweak qua env nếu cần
# ─────────────────────────────────────────────────────────────
def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default


THRESH_CRITICAL_COUNT = _env_int("CTO_LOCK_CRITICAL_COUNT", 3)
THRESH_CRITICAL_WINDOW_MIN = _env_int("CTO_LOCK_CRITICAL_WINDOW_MIN", 5)
LOCK_CRITICAL_DURATION_MIN = _env_int("CTO_LOCK_CRITICAL_DURATION_MIN", 30)

THRESH_HIGH_COUNT = _env_int("CTO_LOCK_HIGH_COUNT", 5)
THRESH_HIGH_WINDOW_MIN = _env_int("CTO_LOCK_HIGH_WINDOW_MIN", 10)
LOCK_HIGH_DURATION_MIN = _env_int("CTO_LOCK_HIGH_DURATION_MIN", 60)

THRESH_WARN_COUNT = _env_int("CTO_LOCK_WARN_COUNT", 10)
THRESH_WARN_WINDOW_MIN = _env_int("CTO_LOCK_WARN_WINDOW_MIN", 30)
LOCK_WARN_DURATION_MIN = _env_int("CTO_LOCK_WARN_DURATION_MIN", 15)


@dataclass
class LockStatus:
    locked: bool
    workspace_id: str
    ip_address: Optional[str] = None
    unlock_at: Optional[datetime] = None
    reason: Optional[str] = None
    severity: Optional[str] = None


class CtoAutoLock:
    """
    Service kiểm tra + áp lock cho CTO endpoint customer.

    Cách dùng trong handler:
        lock = await CtoAutoLock.check(db, ws_id, ip)
        if lock.locked:
            raise HTTPException(423, f"Locked: {lock.reason}")
        ...
        await CtoAutoLock.evaluate_and_lock(db, ws_id, ip)
    """

    # ────────────────────────────────────────
    # Check current lock status
    # ────────────────────────────────────────
    @staticmethod
    async def check(
        db: AsyncSession,
        workspace_id: str,
        ip_address: Optional[str] = None,
    ) -> LockStatus:
        """
        Trả về LockStatus(locked=True/False).
        Tìm lock active (unlock_at > NOW()) cho workspace HOẶC ip.
        """
        try:
            row = (await db.execute(_sql_text("""
                SELECT workspace_id, ip_address, unlock_at, reason, severity
                FROM cto_workspace_locks
                WHERE unlock_at > NOW()
                  AND (workspace_id = :ws OR (ip_address IS NOT NULL AND ip_address = :ip))
                ORDER BY unlock_at DESC
                LIMIT 1
            """), {"ws": workspace_id, "ip": ip_address or ""})).mappings().first()
        except Exception as e:
            log.warning("[auto_lock] check failed (DB?): %s", e)
            return LockStatus(locked=False, workspace_id=workspace_id, ip_address=ip_address)

        if not row:
            return LockStatus(locked=False, workspace_id=workspace_id, ip_address=ip_address)

        return LockStatus(
            locked=True,
            workspace_id=row["workspace_id"],
            ip_address=row["ip_address"],
            unlock_at=row["unlock_at"],
            reason=row["reason"],
            severity=row["severity"],
        )

    # ────────────────────────────────────────
    # Evaluate violation counts → maybe lock
    # ────────────────────────────────────────
    @staticmethod
    async def evaluate_and_lock(
        db: AsyncSession,
        workspace_id: str,
        ip_address: Optional[str] = None,
    ) -> Optional[LockStatus]:
        """
        Đếm violations trong các cửa sổ → quyết định có lock không.

        Trả LockStatus(locked=True, ...) nếu vừa áp lock, None nếu không.
        """
        try:
            # Count critical in window
            r_crit = (await db.execute(_sql_text("""
                SELECT COUNT(*) AS c FROM cto_security_violations
                WHERE workspace_id = :ws
                  AND severity = 'critical'
                  AND created_at > NOW() - (:mins || ' minutes')::interval
            """), {"ws": workspace_id, "mins": str(THRESH_CRITICAL_WINDOW_MIN)})).mappings().first()
            n_crit = int(r_crit["c"]) if r_crit else 0

            if n_crit >= THRESH_CRITICAL_COUNT:
                return await CtoAutoLock._apply_lock(
                    db, workspace_id, ip_address,
                    duration_min=LOCK_CRITICAL_DURATION_MIN,
                    severity="critical",
                    reason=f"{n_crit} critical violations trong {THRESH_CRITICAL_WINDOW_MIN} phút",
                )

            # Count high (high + critical) in window
            r_high = (await db.execute(_sql_text("""
                SELECT COUNT(*) AS c FROM cto_security_violations
                WHERE workspace_id = :ws
                  AND severity IN ('high', 'critical')
                  AND created_at > NOW() - (:mins || ' minutes')::interval
            """), {"ws": workspace_id, "mins": str(THRESH_HIGH_WINDOW_MIN)})).mappings().first()
            n_high = int(r_high["c"]) if r_high else 0

            if n_high >= THRESH_HIGH_COUNT:
                return await CtoAutoLock._apply_lock(
                    db, workspace_id, ip_address,
                    duration_min=LOCK_HIGH_DURATION_MIN,
                    severity="high",
                    reason=f"{n_high} high+ violations trong {THRESH_HIGH_WINDOW_MIN} phút",
                )

            # Count warn+ in window
            r_warn = (await db.execute(_sql_text("""
                SELECT COUNT(*) AS c FROM cto_security_violations
                WHERE workspace_id = :ws
                  AND severity IN ('warn', 'high', 'critical')
                  AND created_at > NOW() - (:mins || ' minutes')::interval
            """), {"ws": workspace_id, "mins": str(THRESH_WARN_WINDOW_MIN)})).mappings().first()
            n_warn = int(r_warn["c"]) if r_warn else 0

            if n_warn >= THRESH_WARN_COUNT:
                return await CtoAutoLock._apply_lock(
                    db, workspace_id, ip_address,
                    duration_min=LOCK_WARN_DURATION_MIN,
                    severity="warn",
                    reason=f"{n_warn} warn+ violations trong {THRESH_WARN_WINDOW_MIN} phút",
                )
        except Exception as e:
            log.exception("[auto_lock] evaluate failed: %s", e)

        return None

    # ────────────────────────────────────────
    # Apply a lock
    # ────────────────────────────────────────
    @staticmethod
    async def _apply_lock(
        db: AsyncSession,
        workspace_id: str,
        ip_address: Optional[str],
        duration_min: int,
        severity: str,
        reason: str,
    ) -> LockStatus:
        unlock_at = datetime.now(timezone.utc) + timedelta(minutes=duration_min)
        lock_id = uuid.uuid4()
        try:
            # Upsert — nếu đã có active lock thì extend
            await db.execute(_sql_text("""
                INSERT INTO cto_workspace_locks
                    (id, workspace_id, ip_address, unlock_at, severity, reason)
                VALUES
                    (:id, :ws, :ip, :until, :sev, :r)
                ON CONFLICT (workspace_id)
                DO UPDATE SET
                    unlock_at = GREATEST(EXCLUDED.unlock_at, cto_workspace_locks.unlock_at),
                    severity = EXCLUDED.severity,
                    reason = EXCLUDED.reason,
                    ip_address = COALESCE(EXCLUDED.ip_address, cto_workspace_locks.ip_address)
            """), {
                "id": str(lock_id),
                "ws": workspace_id,
                "ip": ip_address or None,
                "until": unlock_at,
                "sev": severity,
                "r": reason[:500],
            })
            await db.commit()
        except Exception as e:
            log.exception("[auto_lock] apply_lock failed: %s", e)
            try:
                await db.rollback()
            except Exception:
                pass
            return LockStatus(locked=False, workspace_id=workspace_id, ip_address=ip_address)

        log.critical(
            "[auto_lock] LOCKED workspace=%s ip=%s until=%s severity=%s reason=%s",
            workspace_id, ip_address, unlock_at.isoformat(), severity, reason,
        )

        # Audit push (best-effort)
        try:
            await audit_push(
                db,
                actor="system:cto_auto_lock",
                workspace_id=workspace_id,
                action="cto.workspace_lock",
                target=workspace_id,
                severity="critical",
                metadata={
                    "severity": severity, "duration_min": duration_min,
                    "unlock_at": unlock_at.isoformat(), "reason": reason,
                    "ip_address": ip_address,
                },
            )
        except Exception as e:
            log.warning("[auto_lock] audit_push failed: %s", e)

        return LockStatus(
            locked=True,
            workspace_id=workspace_id,
            ip_address=ip_address,
            unlock_at=unlock_at,
            reason=reason,
            severity=severity,
        )

    # ────────────────────────────────────────
    # Manual unlock (Chairman)
    # ────────────────────────────────────────
    @staticmethod
    async def manual_unlock(
        db: AsyncSession,
        workspace_id: str,
        actor_id: str,
        reason: str = "Chairman manual unlock",
    ) -> bool:
        try:
            await db.execute(_sql_text("""
                DELETE FROM cto_workspace_locks WHERE workspace_id = :ws
            """), {"ws": workspace_id})
            await db.commit()
            await audit_push(
                db,
                actor=f"user:{actor_id}",
                workspace_id=workspace_id,
                action="cto.workspace_unlock",
                target=workspace_id,
                severity="warning",
                metadata={"reason": reason},
            )
            return True
        except Exception as e:
            log.exception("[auto_lock] manual_unlock failed: %s", e)
            try:
                await db.rollback()
            except Exception:
                pass
            return False


__all__ = ["CtoAutoLock", "LockStatus"]
