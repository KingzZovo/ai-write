"""Self-signed JWT helper for smoke + pytest (v1.2.0 / chunk-27).

The smoke suite and the GitHub Actions CI job both need an admin-scoped
bearer token without having to reach a real auth server. This module centralises
that logic so the same signing path is used by:

* ``scripts/smoke_v1.sh`` (via ``docker compose exec backend python -m
  tests.fixtures.self_sign_jwt``)
* ``pytest`` (via the ``admin_jwt`` fixture below)

It reads ``app.config.settings.SECRET_KEY`` so the token round-trips through the
same validation path real requests use.
"""
from __future__ import annotations

import datetime as _dt
import os
import sys
from typing import Optional


def sign_smoke_jwt(
    subject: str = "admin",
    ttl_seconds: int = 3600,
    secret: Optional[str] = None,
    algorithm: str = "HS256",
) -> str:
    """Return a signed JWT string for the given subject.

    Parameters
    ----------
    subject:
        ``sub`` claim. Defaults to ``admin`` (the backend's default admin
        username). Override when testing a non-admin path.
    ttl_seconds:
        Token lifetime. Default 1 hour is plenty for a smoke run; keep it
        short so the fixture can't become a persistent credential.
    secret:
        Optional override for the signing secret. When ``None`` (the default)
        we load ``app.config.settings.SECRET_KEY`` so the token is accepted
        by the running backend. Override in unit tests where the settings
        module isn't importable.
    algorithm:
        JWT algorithm. ``HS256`` matches the backend AuthMiddleware.
    """
    import jwt  # deferred: keeps this module importable in envs without PyJWT

    if secret is None:
        # Prefer the settings singleton so we stay in sync with the backend.
        # Fall back to env var for bare-metal test runs.
        try:
            from app.config import settings  # type: ignore

            secret = settings.SECRET_KEY
        except Exception:
            secret = os.environ.get("SECRET_KEY", "dev-secret")

    now = _dt.datetime.now(_dt.timezone.utc)
    payload = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int((now + _dt.timedelta(seconds=ttl_seconds)).timestamp()),
    }
    token = jwt.encode(payload, secret, algorithm=algorithm)
    # PyJWT < 2 returns bytes; PyJWT >= 2 returns str. Normalise.
    if isinstance(token, bytes):
        token = token.decode("utf-8")
    return token


# ---------------------------------------------------------------------------
# pytest fixture (optional: only active when pytest is importing this module).
# ---------------------------------------------------------------------------
try:
    import pytest

    @pytest.fixture
    def admin_jwt() -> str:
        """pytest fixture yielding an admin-scoped bearer token."""
        return sign_smoke_jwt(subject="admin")

except ImportError:  # pragma: no cover -- pytest not installed in runtime env
    pass


if __name__ == "__main__":
    # CLI entry for scripts/smoke_v1.sh:
    #   docker compose exec -T backend python -m tests.fixtures.self_sign_jwt
    sys.stdout.write(sign_smoke_jwt())
    sys.stdout.write("\n")
