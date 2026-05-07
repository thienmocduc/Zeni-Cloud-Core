"""
Zeni Cloud Core — Platform Admin role gate.

Tách biệt rõ giữa "Owner" (chủ workspace của khách) và "PlatformAdmin"
(super-admin của nền tảng Zeni Cloud).

  Owner          : chỉ có quyền trong workspace của họ.
  PlatformAdmin  : super-admin Zeni — xem aggregate stats toàn platform,
                   không truy cập raw data của khách (phải qua CAAA flow).

Cách xác định Platform Admin:
  1. user.role == 'PlatformAdmin'  (set qua DB)
  2. user.email == settings.admin_email  (fallback cho bootstrap CEO)

Sử dụng:

    @router.get("/dashboard")
    async def dashboard(me = Depends(require_platform_admin)):
        ...
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, status

from app.core.config import settings
from app.core.deps import CurrentUser, get_current_user

# Whitelist email được phép truy cập admin platform — cần khớp với
# settings.admin_email. Có thể mở rộng sau khi mời thêm CTO/Engineering Lead.
PLATFORM_ADMIN_EMAILS: list[str] = [
    settings.admin_email.lower(),
    "caotuanphat581@gmail.com",  # CEO super-admin
]


def is_platform_admin(user: CurrentUser) -> bool:
    """Boolean check: user có phải Platform Admin không."""
    if user is None:
        return False
    if user.role == "PlatformAdmin":
        return True
    if user.email and user.email.lower() in PLATFORM_ADMIN_EMAILS:
        return True
    return False


async def require_platform_admin(
    me: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """FastAPI dependency — strict gate cho /admin/platform/* endpoints."""
    if not is_platform_admin(me):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Platform admin only",
        )
    return me


__all__ = [
    "PLATFORM_ADMIN_EMAILS",
    "is_platform_admin",
    "require_platform_admin",
]
