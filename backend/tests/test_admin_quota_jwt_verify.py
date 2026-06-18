"""Security regression: admin + quota caller-resolution must verify the JWT
signature, not trust an unsigned/forged token.

Before the fix, ``admin_usage._caller_username`` and
``quota._extract_username`` decoded with ``verify_signature=False`` and relied
on AuthMiddleware having already validated the signature. When
``DISABLE_AUTH=1`` (or for any path AuthMiddleware doesn't cover) a forged
``{"sub": "king"}`` token would pass the admin gate. These tests pin the
contract that a bad signature is rejected.
"""

import jwt
import pytest

from app.api.auth import _create_token
from app.api.admin_usage import _caller_username
from app.middlewares.quota import _extract_username
from app.config import settings


def _bearer(token: str):
    class _Req:
        headers = {"authorization": f"Bearer {token}"}
    return _Req()


def test_admin_caller_rejects_forged_signature():
    forged = jwt.encode({"sub": "king"}, "attacker-secret", algorithm="HS256")
    assert _caller_username(_bearer(forged)) is None


def test_admin_caller_rejects_unsigned_token():
    unsigned = jwt.encode({"sub": "king"}, "", algorithm="none")
    assert _caller_username(_bearer(unsigned)) is None


def test_admin_caller_accepts_valid_token():
    valid = _create_token("king")
    assert _caller_username(_bearer(valid)) == "king"


def test_quota_extract_rejects_forged_signature():
    forged = jwt.encode({"sub": "king"}, "attacker-secret", algorithm="HS256")
    assert _extract_username(f"Bearer {forged}") is None


def test_quota_extract_accepts_valid_token():
    valid = _create_token("king")
    assert _extract_username(f"Bearer {valid}") == "king"
