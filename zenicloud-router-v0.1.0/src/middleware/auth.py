"""
ZeniCloud Router - Auth Middleware.
Validates X-Zeni-API-Key header. In production, lookup tenant in DB.
For dev: accept any key matching pattern zk_<env>_<32hex>.
"""
import re

from fastapi import Header, HTTPException, status

from src.core.config import settings
from src.core.logging import get_logger

logger = get_logger(__name__)

# Dev-mode key pattern. Production uses DB lookup.
DEV_KEY_PATTERN = re.compile(r"^zk_(dev|stg|prod)_[a-f0-9]{32}$")


async def verify_api_key(
    x_zeni_api_key: str | None = Header(default=None, alias="X-Zeni-API-Key"),
) -> dict:
    """Returns tenant context dict on success, raises 401 on failure."""
    if not x_zeni_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Zeni-API-Key header",
        )

    if not DEV_KEY_PATTERN.match(x_zeni_api_key):
        logger.warning("invalid_api_key_format")  # never log actual key
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key format",
        )

    # Extract env from key (zk_dev_xxx → dev)
    env = x_zeni_api_key.split("_")[1]
    if env != settings.ENV and not (env == "dev" and settings.ENV == "dev"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Key environment mismatch (key={env}, server={settings.ENV})",
        )

    # In production: SELECT FROM tenants WHERE api_key_hash = sha256(...)
    # Mock tenant context:
    tenant_id = f"tenant_{x_zeni_api_key[-8:]}"
    return {
        "tenant_id": tenant_id,
        "env": env,
        "scopes": ["complete", "route", "models:read"],
    }
