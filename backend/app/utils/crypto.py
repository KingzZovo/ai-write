"""Symmetric encryption for sensitive data (API keys) using Fernet.

Key is derived from the application SECRET_KEY via PBKDF2.
"""

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

logger = logging.getLogger(__name__)

# Derive a Fernet-compatible key from SECRET_KEY
_raw = hashlib.pbkdf2_hmac("sha256", settings.SECRET_KEY.encode(), b"ai-write-api-key-salt", 100_000)
_FERNET_KEY = base64.urlsafe_b64encode(_raw)
_fernet = Fernet(_FERNET_KEY)


def encrypt_api_key(plaintext: str) -> str:
    """Encrypt an API key. Returns a base64-encoded encrypted string prefixed with 'enc:'."""
    if not plaintext:
        return ""
    if plaintext.startswith("enc:"):
        return plaintext  # Already encrypted
    return "enc:" + _fernet.encrypt(plaintext.encode()).decode()


class ApiKeyDecryptError(RuntimeError):
    """密钥解不开(通常是 SECRET_KEY 变了或跨环境拷贝了密文)。

    2026-11-17:原实现返回空串,上层会把空 key 注册进 provider,
    表现为"厂商 401"而不是"配置错误"。
    """


def decrypt_api_key(ciphertext: str) -> str:
    """Decrypt an API key. If not encrypted (no 'enc:' prefix), returns as-is."""
    if not ciphertext:
        return ""
    if not ciphertext.startswith("enc:"):
        return ciphertext  # Plaintext (legacy, not yet migrated)
    try:
        return _fernet.decrypt(ciphertext[4:].encode()).decode()
    except InvalidToken as exc:
        logger.error("Failed to decrypt API key — SECRET_KEY may have changed")
        raise ApiKeyDecryptError("api_key_decrypt_failed") from exc


def is_encrypted(value: str) -> bool:
    """Check if a value is already encrypted."""
    return value.startswith("enc:")
