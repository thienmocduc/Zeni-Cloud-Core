from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


def _derive_key() -> bytes:
    if settings.vault_key:
        return settings.vault_key.encode()
    # Dev fallback — derive deterministic key from jwt_secret (NOT production-safe)
    digest = hashlib.sha256(settings.jwt_secret.encode()).digest()
    return base64.urlsafe_b64encode(digest)


_fernet = Fernet(_derive_key())


def encrypt(plain: str) -> bytes:
    return _fernet.encrypt(plain.encode())


def decrypt(token: bytes) -> str:
    try:
        return _fernet.decrypt(token).decode()
    except InvalidToken:
        raise ValueError("invalid ciphertext — vault key mismatch or data corrupted")


def generate_key() -> str:
    return Fernet.generate_key().decode()


def mask(value: str, show_prefix: int = 4, show_suffix: int = 4) -> str:
    if len(value) <= show_prefix + show_suffix:
        return "*" * len(value)
    return f"{value[:show_prefix]}{'•' * 8}{value[-show_suffix:]}"
