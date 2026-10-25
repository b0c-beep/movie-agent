from __future__ import annotations

import hashlib
import hmac
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from movie_agent.security.sessions import (
    InvalidSessionToken,
    SessionClaims,
    sign_session_token,
    verify_passcode,
    verify_session_token,
)
from movie_agent.services.persistence import (
    AUTH_HOURLY_LIMIT,
    PersistenceClient,
    PersistenceError,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])
SESSION_COOKIE_NAME = "movie_agent_session"
SESSION_TTL = timedelta(days=30)
_MAX_CLIENT_ADDRESS_LENGTH = 64
_FALLBACK_CLIENT_ADDRESS = "unknown"


class UnlockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passcode: str = Field(min_length=1, max_length=256)


class SessionResponse(BaseModel):
    authenticated: bool = True
    session_id: UUID
    expires_at: datetime


@router.post("/unlock", response_model=SessionResponse)
async def unlock(request: Request, response: Response, body: UnlockRequest) -> SessionResponse:
    configured_passcode = os.getenv("APP_PASSCODE", "")
    signing_key = os.getenv("SESSION_SIGNING_KEY", "")
    persistence = _persistence_or_503(request)

    if not configured_passcode or len(signing_key.encode("utf-8")) < 32:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="App access is not configured",
        )
    try:
        attempt = await persistence.consume_auth_attempt(
            _auth_bucket_key(request, signing_key),
            AUTH_HOURLY_LIMIT,
        )
    except PersistenceError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session storage is unavailable",
        ) from error
    if not attempt.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many unlock attempts",
        )
    if not verify_passcode(body.passcode, configured_passcode):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid passcode",
        )

    existing_session = _existing_session(request, signing_key)
    if existing_session is not None:
        return SessionResponse(
            session_id=existing_session.session_id,
            expires_at=existing_session.expires_at,
        )

    now = datetime.now(UTC)
    session_id = uuid4()
    expires_at = now + SESSION_TTL
    try:
        await persistence.create_session(session_id, expires_at)
    except PersistenceError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session storage is unavailable",
        ) from error

    token = sign_session_token(session_id, expires_at, signing_key)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        secure=os.getenv("APP_ENV", "production").lower() != "development",
        samesite="lax",
        path="/api",
    )
    return SessionResponse(session_id=session_id, expires_at=expires_at)


@router.get("/session", response_model=SessionResponse)
async def current_session(request: Request) -> SessionResponse:
    signing_key = os.getenv("SESSION_SIGNING_KEY", "")
    if len(signing_key.encode("utf-8")) < 32:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="App access is not configured",
        )
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token is None:
        raise _unauthorized()
    try:
        claims = verify_session_token(token, signing_key)
    except (InvalidSessionToken, ValueError):
        raise _unauthorized() from None
    return SessionResponse(session_id=claims.session_id, expires_at=claims.expires_at)


async def require_session_id(request: Request) -> UUID:
    signing_key = os.getenv("SESSION_SIGNING_KEY", "")
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if len(signing_key.encode("utf-8")) < 32 or token is None:
        raise _unauthorized()
    try:
        return verify_session_token(token, signing_key).session_id
    except (InvalidSessionToken, ValueError):
        raise _unauthorized() from None


def _auth_bucket_key(request: Request, signing_key: str) -> str:
    address = _client_address(request)
    return hmac.new(
        signing_key.encode("utf-8"),
        address.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _existing_session(request: Request, signing_key: str) -> SessionClaims | None:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token is None:
        return None
    try:
        return verify_session_token(token, signing_key)
    except (InvalidSessionToken, ValueError):
        return None


def _client_address(request: Request) -> str:
    if os.getenv("VERCEL") == "1":
        forwarded = request.headers.get("x-vercel-forwarded-for", "")
        candidate = forwarded.split(",", 1)[0].strip()
        if candidate and len(candidate) <= _MAX_CLIENT_ADDRESS_LENGTH:
            return candidate
    host = request.client.host if request.client is not None else ""
    if host and len(host) <= _MAX_CLIENT_ADDRESS_LENGTH:
        return host
    return _FALLBACK_CLIENT_ADDRESS


def _persistence_or_503(request: Request) -> PersistenceClient:
    persistence = getattr(request.app.state, "persistence", None)
    if not isinstance(persistence, PersistenceClient):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session storage is unavailable",
        )
    return persistence


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="A valid app session is required",
    )
