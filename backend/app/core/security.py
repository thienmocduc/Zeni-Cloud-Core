import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings

log = logging.getLogger("zeni.security")

# bcrypt has 72-byte input limit. Truncate to ensure consistency between
# hash and verify (passlib used to do this, bcrypt 4.x raises instead).
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__truncate_error=False)

_BCRYPT_MAX_BYTES = 72


def _safe_truncate(raw: str) -> str:
    """Ensure password byte-length ≤ 72 (bcrypt limit). Truncate at byte boundary."""
    enc = raw.encode("utf-8")
    if len(enc) <= _BCRYPT_MAX_BYTES:
        return raw
    # Truncate at byte boundary safely (avoid breaking multi-byte UTF-8 char)
    return enc[:_BCRYPT_MAX_BYTES].decode("utf-8", errors="ignore")


def hash_password(raw: str) -> str:
    return pwd.hash(_safe_truncate(raw))


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return pwd.verify(_safe_truncate(raw), hashed)
    except Exception as e:
        log.warning("[security.verify_password] failed: %s (hash_prefix=%s)",
                    type(e).__name__, (hashed or "")[:7])
        return False


def make_access_token(sub: str, extra: dict[str, Any] | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": sub,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=settings.jwt_access_ttl)).timestamp()),
        "typ": "access",
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_alg)


def make_refresh_token(sub: str) -> tuple[str, str, datetime]:
    jti = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    exp = now + timedelta(seconds=settings.jwt_refresh_ttl)
    payload = {
        "sub": sub,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "typ": "refresh",
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_alg)
    return token, jti, exp


def decode_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_alg])
    except JWTError as e:
        raise ValueError(f"invalid token: {e}")
