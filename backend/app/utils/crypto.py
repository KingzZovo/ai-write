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


def decrypt_api_key(ciphertext: str) -> str:
    """Decrypt an API key. If not encrypted (no 'enc:' prefix), returns as-is."""
    if not ciphertext:
        return ""
    if not ciphertext.startswith("enc:"):
        return ciphertext  # Plaintext (legacy, not yet migrated)
    try:
        return _fernet.decrypt(ciphertext[4:].encode()).decode()
    except InvalidToken:
        logger.error("Failed to decrypt API key — SECRET_KEY may have changed")
        return ""


def is_encrypted(value: str) -> bool:
    """Check if a value is already encrypted."""
    return value.startswith("enc:")
