import base64
import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from movie_agent.security.sessions import (
    InvalidSessionToken,
    sign_session_token,
    verify_passcode,
    verify_session_token,
)

SIGNING_KEY = "test-only-session-signing-key-with-32-bytes"


def test_passcode_verification_handles_empty_and_mismatch() -> None:
    assert verify_passcode("movie-night", "movie-night")
    assert not verify_passcode("wrong", "movie-night")
    assert not verify_passcode("", "movie-night")
    assert not verify_passcode("movie-night", "")


def test_session_token_round_trip() -> None:
    session_id = uuid4()
    expires_at = datetime.now(UTC) + timedelta(hours=1)
    token = sign_session_token(session_id, expires_at, SIGNING_KEY)

    claims = verify_session_token(token, SIGNING_KEY)

    assert claims.session_id == session_id
    assert claims.expires_at == datetime.fromtimestamp(int(expires_at.timestamp()), UTC)


def test_session_token_rejects_tampering_and_expiry() -> None:
    token = sign_session_token(uuid4(), datetime.now(UTC) + timedelta(hours=1), SIGNING_KEY)
    payload, signature = token.rsplit(".", 1)
    replacement = "1" if payload[-1] != "1" else "2"
    tampered = f"{payload[:-1]}{replacement}.{signature}"
    with pytest.raises(InvalidSessionToken):
        verify_session_token(tampered, SIGNING_KEY)

    expired = sign_session_token(uuid4(), datetime.now(UTC) - timedelta(seconds=1), SIGNING_KEY)
    with pytest.raises(InvalidSessionToken, match="Expired"):
        verify_session_token(expired, SIGNING_KEY)


def test_signing_key_must_be_at_least_32_bytes() -> None:
    with pytest.raises(ValueError, match="32 UTF-8 bytes"):
        sign_session_token(uuid4(), datetime.now(UTC) + timedelta(hours=1), "too-short")


def test_session_tokens_reject_unbounded_and_out_of_range_expiries() -> None:
    with pytest.raises(InvalidSessionToken, match="Malformed"):
        verify_session_token("x" * 257, SIGNING_KEY)

    expired_payload = f"{uuid4()}.999999999999999999999999999999999999"
    signature = (
        base64.urlsafe_b64encode(
            hmac.new(SIGNING_KEY.encode(), expired_payload.encode(), hashlib.sha256).digest()
        )
        .rstrip(b"=")
        .decode()
    )
    with pytest.raises(InvalidSessionToken, match="Malformed"):
        verify_session_token(f"{expired_payload}.{signature}", SIGNING_KEY)


def test_sign_session_token_requires_timezone_aware_expiry() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        sign_session_token(uuid4(), datetime(2030, 1, 1), SIGNING_KEY)
