from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

import movie_agent.api.recommendations as recommendations_api
from movie_agent.api.recommendations import MAX_RUN_SECONDS, RUN_LEASE_SECONDS
from movie_agent.main import app
from movie_agent.models.intent import MovieIntentPatch, TurnKind
from movie_agent.security.sessions import sign_session_token
from movie_agent.services.persistence import (
    ClaimedRun,
    PersistenceClient,
    PersistenceError,
    RunStatus,
)
from movie_agent.services.tmdb import TmdbClient

SIGNING_KEY = "test-session-signing-key-that-is-at-least-32-bytes"


def test_run_deadline_expires_before_its_persistence_lease() -> None:
    assert MAX_RUN_SECONDS == 90
    assert MAX_RUN_SECONDS < RUN_LEASE_SECONDS


def test_postgres_usage_limit_error_maps_to_http_429() -> None:
    error = PersistenceError("Persistence request failed", status_code=400, code="P0001")

    response = recommendations_api._persistence_http_error(error)

    assert response.status_code == 429


def test_expired_run_replay_without_trace_returns_a_failed_terminal_event() -> None:
    claimed = ClaimedRun(
        run_id=uuid4(),
        status=RunStatus.FAILED,
        replayed=True,
        base_version=1,
        error_code="lease_expired",
        created_at=datetime.now(UTC),
    )

    events = recommendations_api._load_replay_events(claimed)

    assert len(events) == 1
    assert events[0].type.value == "failed"
    assert events[0].metadata["error_code"] == "lease_expired"


@pytest.mark.asyncio
async def test_protected_stream_assembles_and_persists_grounded_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid4()
    conversation_id = uuid4()
    run_id = uuid4()
    lease_token = uuid4()
    persisted: dict[str, object] = {}

    def persistence_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        if request.url.path.endswith("/rpc/create_conversation"):
            return httpx.Response(
                200,
                json=[
                    {
                        "conversation_id": str(conversation_id),
                        "version": 1,
                        "created_at": "2026-10-25T12:00:00+00:00",
                    }
                ],
            )
        if request.url.path.endswith("/rpc/claim_run"):
            return httpx.Response(
                200,
                json=[
                    {
                        "run_id": str(run_id),
                        "status": "running",
                        "replayed": False,
                        "lease_token": str(lease_token),
                        "lease_expires_at": "2026-10-25T12:03:00+00:00",
                        "base_version": 1,
                        "result": None,
                        "trace": None,
                        "error_code": None,
                        "usage": None,
                        "created_at": "2026-10-25T12:00:00+00:00",
                        "completed_at": None,
                    }
                ],
            )
        if request.url.path.endswith("/rpc/finalize_run"):
            persisted.update(body)
            return httpx.Response(
                200,
                json=[
                    {
                        "run_id": str(run_id),
                        "conversation_id": str(conversation_id),
                        "version": 2,
                        "status": "completed",
                        "completed_at": "2026-10-25T12:00:01+00:00",
                    }
                ],
            )
        raise AssertionError(f"Unexpected persistence path: {request.url.path}")

    def tmdb_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/3/discover/movie":
            return httpx.Response(
                200,
                json={
                    "page": 1,
                    "total_pages": 1,
                    "total_results": 1,
                    "results": [
                        {
                            "id": 603,
                            "title": "The Matrix",
                            "original_title": "The Matrix",
                            "release_date": "1999-03-31",
                            "overview": "A hacker discovers a simulated world.",
                            "poster_path": "/matrix.jpg",
                            "vote_average": 8.2,
                            "vote_count": 100,
                            "genre_ids": [878],
                        }
                    ],
                },
            )
        if request.url.path == "/3/movie/603":
            return httpx.Response(
                200,
                json={
                    "id": 603,
                    "title": "The Matrix",
                    "original_title": "The Matrix",
                    "release_date": "1999-03-31",
                    "runtime": 136,
                    "genres": [{"id": 878, "name": "Science Fiction"}],
                    "overview": "A hacker discovers a simulated world.",
                    "poster_path": "/matrix.jpg",
                    "backdrop_path": None,
                    "vote_average": 8.2,
                    "vote_count": 100,
                    "external_ids": {"imdb_id": "tt0133093"},
                    "keywords": {"keywords": []},
                    "credits": {"cast": [], "crew": []},
                },
            )
        if request.url.path == "/3/movie/603/watch/providers":
            return httpx.Response(200, json={"id": 603, "results": {}})
        raise AssertionError(f"Unexpected TMDB path: {request.url.path}")

    persistence_http = httpx.AsyncClient(transport=httpx.MockTransport(persistence_handler))
    tmdb_http = httpx.AsyncClient(transport=httpx.MockTransport(tmdb_handler))
    monkeypatch.setattr(
        app.state,
        "persistence",
        PersistenceClient(persistence_http, "https://project.supabase.co", "test-secret"),
        raising=False,
    )
    monkeypatch.setattr(app.state, "tmdb", TmdbClient(tmdb_http, "tmdb-test"), raising=False)
    monkeypatch.setenv("SESSION_SIGNING_KEY", SIGNING_KEY)
    monkeypatch.setenv("GROQ_MODEL", "openai/gpt-oss-20b")

    async def extract_patch(message: str, prior: object) -> MovieIntentPatch:
        assert message == "One science fiction movie"
        return MovieIntentPatch(
            turn_kind=TurnKind.NEW_SEARCH,
            genres={"set": ["Science Fiction"]},
            requested_count=1,
        )

    monkeypatch.setattr(recommendations_api, "extract_intent_patch", extract_patch)
    token = sign_session_token(session_id, datetime.now(UTC) + timedelta(hours=1), SIGNING_KEY)

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            client.cookies.set("movie_agent_session", token)
            response = await client.post(
                "/api/recommendations/stream",
                json={"message": "One science fiction movie", "client_request_id": "req-1"},
            )

        assert response.status_code == 200
        assert response.headers["x-conversation-id"] == str(conversation_id)
        frames = [frame for frame in response.text.split("\n\n") if frame]
        event_types = [frame.splitlines()[1].removeprefix("event: ") for frame in frames]
        assert event_types[-2:] == ["recommendations", "completed"]
        recommendation_frame = json.loads(frames[-2].split("data: ", 1)[1])
        assert recommendation_frame["data"]["recommendations"][0]["movie"]["tmdb_id"] == 603
        assert persisted["p_status"] == "completed"
        assert persisted["p_state"]["seen_tmdb_ids"] == [603]  # type: ignore[index]
        stored_trace = persisted["p_trace"]
        assert isinstance(stored_trace, list)
        assert stored_trace[-1]["type"] == "completed"
    finally:
        monkeypatch.setattr(app.state, "persistence", None, raising=False)
        monkeypatch.setattr(app.state, "tmdb", None, raising=False)
        await persistence_http.aclose()
        await tmdb_http.aclose()


@pytest.mark.asyncio
async def test_conversation_snapshot_is_scoped_to_the_signed_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid4()
    conversation_id = uuid4()
    requests: list[httpx.Request] = []
    state = {"intent": {"country": "RO", "requested_count": 5}}
    row = {
        "id": str(conversation_id),
        "session_id": str(session_id),
        "state": state,
        "version": 2,
        "active_run_id": None,
        "active_run_lease": None,
        "lease_expires_at": None,
        "created_at": "2026-10-25T12:00:00+00:00",
        "updated_at": "2026-10-25T12:01:00+00:00",
    }
    responses = [httpx.Response(200, json=[row]), httpx.Response(200, json=[])]

    def persistence_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    persistence_http = httpx.AsyncClient(transport=httpx.MockTransport(persistence_handler))
    monkeypatch.setattr(
        app.state,
        "persistence",
        PersistenceClient(persistence_http, "https://project.supabase.co", "test-secret"),
        raising=False,
    )
    monkeypatch.setenv("SESSION_SIGNING_KEY", SIGNING_KEY)
    token = sign_session_token(session_id, datetime.now(UTC) + timedelta(hours=1), SIGNING_KEY)

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            client.cookies.set("movie_agent_session", token)
            owned = await client.get(f"/api/recommendations/conversations/{conversation_id}")
            foreign = await client.get(f"/api/recommendations/conversations/{uuid4()}")

        assert owned.status_code == 200
        assert owned.json()["conversation_id"] == str(conversation_id)
        assert owned.json()["version"] == 2
        assert owned.json()["intent"]["country"] == "RO"
        assert owned.json()["result"] is None
        assert foreign.status_code == 404
        assert all(request.url.params["session_id"] == f"eq.{session_id}" for request in requests)
    finally:
        monkeypatch.setattr(app.state, "persistence", None, raising=False)
        await persistence_http.aclose()


@pytest.mark.asyncio
async def test_clarification_result_uses_the_complete_result_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid4()
    conversation_id = uuid4()
    run_id = uuid4()
    lease_token = uuid4()
    persisted: dict[str, object] = {}

    def persistence_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        if request.url.path.endswith("/rpc/create_conversation"):
            return httpx.Response(
                200,
                json=[
                    {
                        "conversation_id": str(conversation_id),
                        "version": 1,
                        "created_at": "2026-10-25T12:00:00+00:00",
                    }
                ],
            )
        if request.url.path.endswith("/rpc/claim_run"):
            return httpx.Response(
                200,
                json=[
                    {
                        "run_id": str(run_id),
                        "status": "running",
                        "replayed": False,
                        "lease_token": str(lease_token),
                        "lease_expires_at": "2026-10-25T12:03:00+00:00",
                        "base_version": 1,
                        "result": None,
                        "trace": None,
                        "error_code": None,
                        "usage": None,
                        "created_at": "2026-10-25T12:00:00+00:00",
                        "completed_at": None,
                    }
                ],
            )
        if request.url.path.endswith("/rpc/finalize_run"):
            persisted.update(body)
            return httpx.Response(
                200,
                json=[
                    {
                        "run_id": str(run_id),
                        "conversation_id": str(conversation_id),
                        "version": 2,
                        "status": "needs_clarification",
                        "completed_at": "2026-10-25T12:00:01+00:00",
                    }
                ],
            )
        raise AssertionError(f"Unexpected persistence path: {request.url.path}")

    persistence_http = httpx.AsyncClient(transport=httpx.MockTransport(persistence_handler))
    tmdb_http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(
                AssertionError(f"TMDB should not be called for clarification: {request.url}")
            )
        )
    )
    monkeypatch.setattr(
        app.state,
        "persistence",
        PersistenceClient(persistence_http, "https://project.supabase.co", "test-secret"),
        raising=False,
    )
    monkeypatch.setattr(app.state, "tmdb", TmdbClient(tmdb_http, "tmdb-test"), raising=False)
    monkeypatch.setenv("SESSION_SIGNING_KEY", SIGNING_KEY)

    async def extract_patch(message: str, prior: object) -> MovieIntentPatch:
        assert message == "Help me narrow this down"
        return MovieIntentPatch(
            turn_kind=TurnKind.NEW_SEARCH,
            clarification="Which decade do you prefer?",
        )

    monkeypatch.setattr(recommendations_api, "extract_intent_patch", extract_patch)
    token = sign_session_token(session_id, datetime.now(UTC) + timedelta(hours=1), SIGNING_KEY)

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            client.cookies.set("movie_agent_session", token)
            response = await client.post(
                "/api/recommendations/stream",
                json={"message": "Help me narrow this down", "client_request_id": "req-clarify"},
            )

        assert response.status_code == 200
        frame = next(
            part for part in response.text.split("\n\n") if "event: needs_clarification" in part
        )
        event = json.loads(frame.split("data: ", 1)[1])
        assert event["data"]["requested_count"] == 5
        assert event["data"]["clarification"] == "Which decade do you prefer?"
        assert event["data"]["issues"] == []
        assert event["data"]["provider_lookups"] == []
        assert persisted["p_result"] == event["data"]
    finally:
        monkeypatch.setattr(app.state, "persistence", None, raising=False)
        monkeypatch.setattr(app.state, "tmdb", None, raising=False)
        await persistence_http.aclose()
        await tmdb_http.aclose()
