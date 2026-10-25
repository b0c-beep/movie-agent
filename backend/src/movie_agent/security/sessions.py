from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID


class InvalidSessionToken(ValueError):
    """The session token is malformed, expired, or has an invalid signature."""


@dataclass(frozen=True, slots=True)
class SessionClaims:
    session_id: UUID
    expires_at: datetime


def verify_passcode(candidate: str, configured_passcode: str) -> bool:
    if not candidate or not configured_passcode:
        return False
    candidate_digest = hashlib.sha256(candidate.encode("utf-8")).digest()
    configured_digest = hashlib.sha256(configured_passcode.encode("utf-8")).digest()
    return hmac.compare_digest(candidate_digest, configured_digest)


def sign_session_token(
    session_id: UUID,
    expires_at: datetime,
    signing_key: str,
) -> str:
    key = _validate_signing_key(signing_key)
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise ValueError("expires_at must be timezone-aware")
    expiry = int(expires_at.timestamp())
    payload = f"{session_id}.{expiry}"
    signature = hmac.new(key, payload.encode("ascii"), hashlib.sha256).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return f"{payload}.{encoded_signature}"


def verify_session_token(
    token: str,
    signing_key: str,
    *,
    now: datetime | None = None,
) -> SessionClaims:
    key = _validate_signing_key(signing_key)
    if not isinstance(token, str) or len(token) > 256:
        raise InvalidSessionToken("Malformed session token")
    parts = token.split(".")
    if len(parts) != 3:
        raise InvalidSessionToken("Malformed session token")

    session_id_raw, expiry_raw, supplied_signature = parts
    payload = f"{session_id_raw}.{expiry_raw}"
    expected = hmac.new(key, payload.encode("ascii", errors="ignore"), hashlib.sha256).digest()
    try:
        supplied = base64.urlsafe_b64decode(
            supplied_signature + "=" * (-len(supplied_signature) % 4)
        )
        session_id = UUID(session_id_raw)
        expiry = int(expiry_raw)
    except (ValueError, TypeError) as error:
        raise InvalidSessionToken("Malformed session token") from error

    if not hmac.compare_digest(expected, supplied):
        raise InvalidSessionToken("Invalid session signature")

    try:
        expires_at = datetime.fromtimestamp(expiry, tz=UTC)
    except (OverflowError, OSError, ValueError) as error:
        raise InvalidSessionToken("Malformed session token") from error
    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if expires_at <= current_time:
        raise InvalidSessionToken("Expired session token")
    return SessionClaims(session_id=session_id, expires_at=expires_at)


def _validate_signing_key(signing_key: str) -> bytes:
    key = signing_key.encode("utf-8")
    if len(key) < 32:
        raise ValueError("SESSION_SIGNING_KEY must contain at least 32 UTF-8 bytes")
    return key
