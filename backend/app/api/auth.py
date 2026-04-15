"""Authentication endpoints – single hardcoded user with JWT tokens."""

import hashlib
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.config import settings

router = APIRouter(prefix="/api/auth", tags=["auth"])

# ---------------------------------------------------------------------------
# Hardcoded user
# ---------------------------------------------------------------------------
_USERNAME = "king"
_PASSWORD_HASH = hashlib.sha256("Wt991125".encode()).hexdigest()

_JWT_ALGORITHM = "HS256"
_JWT_EXPIRY_DAYS = 7


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
    password_hash = hashlib.sha256(body.password.encode()).hexdigest()
    if body.username != _USERNAME or password_hash != _PASSWORD_HASH:
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
