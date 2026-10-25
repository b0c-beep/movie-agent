from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest

from movie_agent.api.auth import SESSION_COOKIE_NAME
from movie_agent.main import app
from movie_agent.services.persistence import PersistenceClient

SIGNING_KEY = "test-session-signing-key-that-is-at-least-32-bytes"
AUTH_RPC_PATH = "/rest/v1/rpc/consume_auth_attempt"
SESSIONS_PATH = "/rest/v1/sessions"
Handler = Callable[[httpx.Request], httpx.Response]


def _auth_bucket_key(address: str) -> str:
    return hmac.new(
        SIGNING_KEY.encode("utf-8"),
        address.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _configure_access(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_PASSCODE", "movie-night")
    monkeypatch.setenv("SESSION_SIGNING_KEY", SIGNING_KEY)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("VERCEL", raising=False)


def _install_persistence(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> httpx.AsyncClient:
    persistence_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    persistence = PersistenceClient(
        persistence_http,
        "https://project.supabase.co",
        "test-service-secret",
    )
    monkeypatch.setattr(app.state, "persistence", persistence, raising=False)
    return persistence_http


def _session_row(body: dict[str, str]) -> dict[str, str | None]:
    now = datetime.now(UTC).isoformat()
    return {
        "id": body["id"],
        "created_at": now,
        "last_seen_at": now,
        "expires_at": body["expires_at"],
        "revoked_at": None,
    }


@pytest.mark.asyncio
async def test_unlock_consumes_allowed_attempt_before_creating_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_access(monkeypatch)
    requests: list[httpx.Request] = []
    expected_key = _auth_bucket_key("203.0.113.7")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == AUTH_RPC_PATH:
            body = json.loads(request.content)
            assert body == {"p_bucket_key": expected_key, "p_hourly_limit": 10}
            assert "203.0.113.7" not in request.content.decode()
            return httpx.Response(200, json=[{"allowed": True, "request_count": 1}])
        assert request.url.path == SESSIONS_PATH
        return httpx.Response(201, json=[_session_row(json.loads(request.content))])

    persistence_http = _install_persistence(monkeypatch, handler)
    api_transport = httpx.ASGITransport(app=app, client=("203.0.113.7", 12345))

    try:
        async with httpx.AsyncClient(transport=api_transport, base_url="http://test") as client:
            response = await client.post("/api/auth/unlock", json={"passcode": "movie-night"})
            session = await client.get("/api/auth/session")

        assert response.status_code == 200
        assert response.json()["authenticated"] is True
        assert "session_id" in response.json()
        cookie = response.headers["set-cookie"]
        assert SESSION_COOKIE_NAME in cookie
        assert "httponly" in cookie.lower()
        assert "secure" not in cookie.lower()
        assert session.status_code == 200
        assert session.json()["session_id"] == response.json()["session_id"]
        assert [request.url.path for request in requests] == [AUTH_RPC_PATH, SESSIONS_PATH]
        assert UUID(response.json()["session_id"])
    finally:
        monkeypatch.setattr(app.state, "persistence", None, raising=False)
        await persistence_http.aclose()


@pytest.mark.asyncio
async def test_unlock_rejects_wrong_passcode_without_creating_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_access(monkeypatch)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == AUTH_RPC_PATH
        return httpx.Response(200, json=[{"allowed": True, "request_count": 2}])

    persistence_http = _install_persistence(monkeypatch, handler)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/auth/unlock", json={"passcode": "wrong"})

        assert response.status_code == 401
        assert "set-cookie" not in response.headers
        assert [request.url.path for request in requests] == [AUTH_RPC_PATH]
    finally:
        monkeypatch.setattr(app.state, "persistence", None, raising=False)
        await persistence_http.aclose()


@pytest.mark.asyncio
async def test_unlock_returns_429_when_auth_bucket_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_access(monkeypatch)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == AUTH_RPC_PATH
        assert json.loads(request.content)["p_hourly_limit"] == 10
        return httpx.Response(200, json=[{"allowed": False, "request_count": 10}])

    persistence_http = _install_persistence(monkeypatch, handler)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/auth/unlock", json={"passcode": "movie-night"})

        assert response.status_code == 429
        assert "set-cookie" not in response.headers
        assert [request.url.path for request in requests] == [AUTH_RPC_PATH]
    finally:
        monkeypatch.setattr(app.state, "persistence", None, raising=False)
        await persistence_http.aclose()


@pytest.mark.asyncio
async def test_unlock_uses_vercel_forwarded_for_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_access(monkeypatch)
    monkeypatch.setenv("VERCEL", "1")
    requests: list[httpx.Request] = []
    expected_key = _auth_bucket_key("198.51.100.4")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == AUTH_RPC_PATH
        body = json.loads(request.content)
        assert body["p_bucket_key"] == expected_key
        assert "198.51.100.4" not in request.content.decode()
        return httpx.Response(200, json=[{"allowed": False, "request_count": 10}])

    persistence_http = _install_persistence(monkeypatch, handler)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/auth/unlock",
                json={"passcode": "movie-night"},
                headers={"x-vercel-forwarded-for": "198.51.100.4, 10.0.0.1"},
            )

        assert response.status_code == 429
        assert [request.url.path for request in requests] == [AUTH_RPC_PATH]
    finally:
        monkeypatch.setattr(app.state, "persistence", None, raising=False)
        await persistence_http.aclose()


@pytest.mark.asyncio
async def test_unlock_fails_closed_when_rate_limit_storage_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_access(monkeypatch)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500, json={"code": "XX000"})

    persistence_http = _install_persistence(monkeypatch, handler)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/auth/unlock", json={"passcode": "movie-night"})

        assert response.status_code == 503
        assert "set-cookie" not in response.headers
        assert [request.url.path for request in requests] == [AUTH_RPC_PATH]
    finally:
        monkeypatch.setattr(app.state, "persistence", None, raising=False)
        await persistence_http.aclose()


@pytest.mark.asyncio
async def test_session_endpoint_rejects_missing_or_invalid_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SESSION_SIGNING_KEY", SIGNING_KEY)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        missing = await client.get("/api/auth/session")
        client.cookies.set(SESSION_COOKIE_NAME, "malformed")
        invalid = await client.get("/api/auth/session")

    assert missing.status_code == 401
    assert invalid.status_code == 401
