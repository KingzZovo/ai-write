"""Authentication endpoints – single hardcoded user with JWT tokens."""

import hashlib
import os
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.config import settings

router = APIRouter(prefix="/api/auth", tags=["auth"])

# ---------------------------------------------------------------------------
# Hardcoded user
# ---------------------------------------------------------------------------
_USERNAME = os.environ.get("AUTH_USERNAME", "king")
# Password hash — supports bcrypt ($2b$ prefix) or legacy sha256
_PASSWORD_HASH = os.environ.get(
    "AUTH_PASSWORD_HASH",
    # bcrypt hash of default password
    "$2b$12$GGjcFhOAfXd/.fcpugYc4uqy6y7fw7pvDJxk.XA1HnmKR/UNrZAKO",
)

_JWT_ALGORITHM = "HS256"
_JWT_EXPIRY_DAYS = 7

# Legacy sha256 hash for backward compatibility
_LEGACY_SHA256_HASH = "ab7be174ff6743f20255f4f81415eaae7cfb5ca5aaab9238272dcb983437c364"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    username: str


class MeResponse(BaseModel):
    username: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _verify_password(password: str, stored_hash: str) -> bool:
    """Verify password against stored hash (bcrypt or legacy sha256)."""
    if stored_hash.startswith("$2b$") or stored_hash.startswith("$2a$"):
        return bcrypt.checkpw(password.encode(), stored_hash.encode())
    # Legacy sha256 fallback
    return hashlib.sha256(password.encode()).hexdigest() == stored_hash


def _create_token(username: str) -> str:
    payload = {
        "sub": username,
        "exp": datetime.now(timezone.utc) + timedelta(days=_JWT_EXPIRY_DAYS),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=_JWT_ALGORITHM)


def verify_token(token: str) -> str:
    """Decode a JWT token and return the username. Raises HTTPException on failure."""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[_JWT_ALGORITHM])
        username: str | None = payload.get("sub")
        if username is None:
            raise HTTPException(status_code=401, detail="Invalid token payload")
        return username
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest) -> LoginResponse:
    """Authenticate with username/password, receive a JWT token."""
    if body.username != _USERNAME or not _verify_password(body.password, _PASSWORD_HASH):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    token = _create_token(body.username)
    return LoginResponse(token=token, username=body.username)


@router.get("/me", response_model=MeResponse)
async def me(authorization: str = Header(...)) -> MeResponse:
    """Return the current authenticated user."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    token = authorization[7:]
    username = verify_token(token)
    return MeResponse(username=username)
