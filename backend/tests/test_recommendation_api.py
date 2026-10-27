from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryFeedbackPart,
    RetryInfo,
    ThinkingPart,
    ToolCallPart,
    UserPromptPart,
)

import movie_agent.api.recommendations as recommendations_api
from movie_agent.api.recommendations import MAX_RUN_SECONDS, RUN_LEASE_SECONDS
from movie_agent.main import app
from movie_agent.models.intent import MovieIntentPatch, StringCollectionPatch, TurnKind
from movie_agent.models.movies import (
    MovieDetails,
    Rating,
    RatingSource,
    Recommendation,
    RecommendationBatch,
    WatchOptions,
)
from movie_agent.models.tmdb import TmdbMovieCandidate, TmdbMoviePage
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


@pytest.mark.parametrize("code", ["40001", "55P03"])
def test_active_run_error_maps_to_http_409(code: str) -> None:
    error = PersistenceError("Persistence request failed", status_code=400, code=code)

    response = recommendations_api._persistence_http_error(error)

    assert response.status_code == 409


def test_run_failure_log_has_safe_diagnostics_only(caplog: pytest.LogCaptureFixture) -> None:
    secret_text = "provider response contains sensitive prompt details"

    recommendations_api._log_run_failure(
        "intent_extraction", uuid4(), RuntimeError(secret_text), "recommendation_failed"
    )

    assert "stage=intent_extraction" in caplog.text
    assert "exception_type=RuntimeError" in caplog.text
    assert "recommendation_failed" in caplog.text
    assert secret_text not in caplog.text


def test_run_failure_metadata_identifies_stage_without_provider_details() -> None:
    secret_text = "provider response contains private prompt details"

    metadata = recommendations_api._failure_metadata(
        "intent_extraction", RuntimeError(secret_text), "recommendation_failed"
    )

    assert metadata == {
        "error_code": "recommendation_failed",
        "failure_stage": "intent_extraction",
        "exception_type": "RuntimeError",
    }
    assert secret_text not in str(metadata)


@pytest.mark.parametrize(
    ("message", "limit"),
    [
        ("The next request would exceed request_limit of 6", "request"),
        ("Exceeded output_tokens_limit of 10000", "output_tokens"),
    ],
)
def test_model_usage_limit_metadata_names_only_the_limit_kind(message: str, limit: str) -> None:
    error = UsageLimitExceeded(message)

    code = recommendations_api._failure_code(error)
    metadata = recommendations_api._failure_metadata("intent_extraction", error, code)

    assert code == "model_usage_limited"
    assert metadata == {
        "error_code": "model_usage_limited",
        "failure_stage": "intent_extraction",
        "exception_type": "UsageLimitExceeded",
        "usage_limit": limit,
    }
    assert message not in str(metadata)


def test_failed_intent_fingerprint_keeps_only_provider_lookup_and_fixed_labels() -> None:
    secret = "private prompt and provider error text"
    messages = [
        ModelRequest(parts=[UserPromptPart(content=secret)]),
        ModelResponse(
            parts=[
                ThinkingPart(content=secret),
                ToolCallPart(tool_name="functions.final_result", args={"private": secret}),
            ],
            provider_response_id="gen-safe-123",
            finish_reason="error",
        ),
        ModelRequest(parts=[RetryFeedbackPart(content=secret, retry=RetryInfo(cause="no_output"))]),
    ]

    assert recommendations_api._last_model_response_failed(messages)
    usage = recommendations_api._intent_failure_usage(messages)

    assert usage == {
        "intent_extraction": {
            "generations": [{"id": "gen-safe-123", "finish_reason": "error"}],
            "retry_causes": ["no_output"],
            "misnamed_output_calls": 1,
        }
    }
    assert secret not in json.dumps(usage)

    messages.append(
        ModelResponse(
            parts=[ThinkingPart(content=secret)],
            provider_response_id="private prompt",
            finish_reason="stop",
        )
    )
    assert not recommendations_api._last_model_response_failed(messages)
    usage = recommendations_api._intent_failure_usage(messages)
    assert usage is not None
    assert usage["intent_extraction"]["generations"][1]["id"] is None


@pytest.mark.asyncio
async def test_provider_stream_error_is_classified_and_persisted_without_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id, conversation_id, run_id, lease_token = (uuid4() for _ in range(4))
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
                        "created_at": "2026-10-27T12:00:00+00:00",
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
                        "lease_expires_at": "2026-10-27T12:03:00+00:00",
                        "base_version": 1,
                        "result": None,
                        "trace": None,
                        "error_code": None,
                        "usage": None,
                        "created_at": "2026-10-27T12:00:00+00:00",
                        "completed_at": None,
                    }
                ],
            )
        assert request.url.path.endswith("/rpc/finalize_run")
        persisted.update(body)
        return httpx.Response(
            200,
            json=[
                {
                    "run_id": str(run_id),
                    "conversation_id": str(conversation_id),
                    "version": 1,
                    "status": "failed",
                    "completed_at": "2026-10-27T12:00:01+00:00",
                }
            ],
        )

    secret = "private model response"
    messages = [
        ModelResponse(
            parts=[ThinkingPart(content=secret)],
            provider_response_id="gen-123",
            finish_reason="error",
        )
    ]

    @contextmanager
    def capture_messages():
        yield messages

    async def failing_extract(_message: str, _prior: object) -> MovieIntentPatch:
        raise RuntimeError(secret)

    monkeypatch.setattr(recommendations_api, "capture_run_messages", capture_messages)
    monkeypatch.setattr(recommendations_api, "extract_intent_patch", failing_extract)
    monkeypatch.setattr(
        app.state,
        "persistence",
        PersistenceClient(
            httpx.AsyncClient(transport=httpx.MockTransport(persistence_handler)),
            "https://project.supabase.co",
            "test-secret",
        ),
        raising=False,
    )
    monkeypatch.setattr(
        app.state, "tmdb", TmdbClient(httpx.AsyncClient(), "test-token"), raising=False
    )
    monkeypatch.setenv("SESSION_SIGNING_KEY", SIGNING_KEY)
    token = sign_session_token(session_id, datetime.now(UTC) + timedelta(hours=1), SIGNING_KEY)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("movie_agent_session", token)
        response = await client.post(
            "/api/recommendations/stream",
            json={"message": "recommend a movie", "client_request_id": "req-provider-error"},
        )

    assert response.status_code == 200
    assert persisted["p_status"] == "failed"
    assert persisted["p_error_code"] == "model_provider_failed"
    assert persisted["p_usage"] == {
        "intent_extraction": {
            "generations": [{"id": "gen-123", "finish_reason": "error"}],
            "retry_causes": [],
            "misnamed_output_calls": 0,
        }
    }
    assert "model_provider_failed" in response.text
    assert secret not in json.dumps(persisted)
    assert secret not in response.text


def test_recommendation_request_rejects_whitespace_only_message() -> None:
    with pytest.raises(ValueError, match="message must not be blank"):
        recommendations_api.RecommendationRunRequest(message=" \t ", client_request_id="request-1")


def test_inspect_results_answers_from_the_selected_saved_card() -> None:
    batch = RecommendationBatch(
        recommendations=[
            Recommendation(
                rank=1,
                movie=MovieDetails(
                    tmdb_id=1,
                    title="Heat",
                    release_year=1995,
                    runtime_minutes=170,
                    genres=["Crime", "Drama"],
                    tmdb_url="https://www.themoviedb.org/movie/1",
                    ratings=[Rating(source=RatingSource.TMDB, value=8.3, scale="10")],
                ),
                match_reason="TMDB genre match: Crime.",
            ),
            Recommendation(
                rank=2,
                movie=MovieDetails(
                    tmdb_id=2,
                    title="The Thing",
                    release_year=1982,
                    runtime_minutes=109,
                    genres=["Horror", "Science Fiction"],
                    tmdb_url="https://www.themoviedb.org/movie/2",
                    ratings=[Rating(source=RatingSource.TMDB, value=8.1, scale="10")],
                ),
                match_reason="TMDB genre match: Horror.",
            ),
        ],
        requested_count=2,
    )

    answer = recommendations_api._answer_from_saved_results(
        "Tell me about the second one",
        batch.model_dump(mode="json"),
        [
            {
                "tmdb_id": 2,
                "country": "US",
                "outcome": "no_data",
                "provider_count": 0,
                "detail": "TMDB returned no watch-provider data.",
            }
        ],
    )

    assert answer.startswith("The Thing (1982)")
    assert "runtime 109 minutes" in answer
    assert "TMDB rating 8.1/10" in answer
    assert "TMDB has no provider data for US." in answer
    assert "Heat" not in answer


def test_inspect_results_asks_when_a_saved_movie_reference_is_ambiguous() -> None:
    answer = recommendations_api._answer_from_saved_results("What about the second one?", None)

    assert "There are no saved movie cards" in answer


def test_inspect_results_does_not_answer_unrelated_questions_from_empty_history() -> None:
    answer = recommendations_api._answer_from_saved_results("What is the weather tomorrow?", None)

    assert answer == (
        "There are no saved movie cards in this conversation yet. Ask for recommendations first."
    )


@pytest.mark.parametrize(
    "message",
    ["What is the weather tomorrow?", "What about the weather?", "What was Heat's budget?"],
)
def test_inspect_results_does_not_treat_a_single_saved_card_as_an_off_topic_answer(
    message: str,
) -> None:
    batch = RecommendationBatch(
        recommendations=[
            Recommendation(
                rank=1,
                movie=MovieDetails(
                    tmdb_id=1,
                    title="Heat",
                    release_year=1995,
                    tmdb_url="https://www.themoviedb.org/movie/1",
                ),
                match_reason="Crime drama match.",
            )
        ],
        requested_count=1,
    )

    answer = recommendations_api._answer_from_saved_results(message, batch.model_dump(mode="json"))
    movie_answer = recommendations_api._answer_from_saved_results(
        "What rating does it have?", batch.model_dump(mode="json")
    )

    assert answer == recommendations_api.OUT_OF_SCOPE_ANSWER
    assert movie_answer.startswith("Heat (1995)")


@pytest.mark.asyncio
async def test_similar_title_resolution_requires_a_grounded_exact_match() -> None:
    class SearchClient:
        async def search_movies(self, title: str, *, year: int | None = None):
            assert title == "Heat"
            return TmdbMoviePage(
                page=1,
                total_pages=1,
                total_results=2,
                results=[
                    TmdbMovieCandidate(tmdb_id=949, title="Heat", release_year=1995),
                    TmdbMovieCandidate(tmdb_id=1124, title="Heat", release_year=2022),
                ],
            )

    unresolved, question = await recommendations_api._resolve_similar_reference(
        SearchClient(), "Heat", None
    )
    assert unresolved is None
    assert question is not None
    assert "Heat (1995)" in question
    assert "Heat (2022)" in question

    resolved, no_question = await recommendations_api._resolve_similar_reference(
        SearchClient(), "Heat", 1995
    )
    assert no_question is None
    assert resolved == {"tmdb_id": 949, "title": "Heat", "year": 1995}


def test_conversation_snapshot_restores_saved_clarification() -> None:
    snapshot = recommendations_api._snapshot_from_conversation(
        uuid4(),
        2,
        {
            "intent": {"country": "RO", "requested_count": 3},
            "last_clarification": "Which decade do you prefer?",
        },
    )

    assert snapshot.clarification == "Which decade do you prefer?"
    assert snapshot.result is None
    assert snapshot.intent.requested_count == 3


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
    monkeypatch.setenv("MODEL_NAME", "openai/gpt-oss-20b")

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
    responses = [
        httpx.Response(200, json=[row]),
        httpx.Response(200, json=[row]),
        httpx.Response(200, json=[]),
        httpx.Response(200, json=[]),
    ]

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
        assert all(
            request.url.params["session_id"] == f"eq.{session_id}"
            for request in requests
            if request.url.path.endswith("/conversations")
        )
        assert any(request.url.path.endswith("/runs") for request in requests)
    finally:
        monkeypatch.setattr(app.state, "persistence", None, raising=False)
        await persistence_http.aclose()


@pytest.mark.asyncio
async def test_country_availability_refresh_updates_only_providers_and_saved_country(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid4()
    conversation_id = uuid4()
    now = "2026-10-25T12:01:00+00:00"
    batch = RecommendationBatch(
        recommendations=[
            Recommendation(
                rank=1,
                movie=MovieDetails(
                    tmdb_id=603,
                    title="The Matrix",
                    release_year=1999,
                    tmdb_url="https://www.themoviedb.org/movie/603",
                    ratings=[Rating(source=RatingSource.TMDB, value=8.2, scale="10")],
                    watch_options=WatchOptions(country="RO"),
                ),
                match_reason="TMDB genre match: Science Fiction.",
            )
        ],
        requested_count=1,
    )
    state = {
        "intent": {"country": "RO", "genres": ["Science Fiction"], "requested_count": 1},
        "seen_tmdb_ids": [603],
        "last_result": batch.model_dump(mode="json"),
        "last_issues": [],
        "provider_lookups": [],
    }
    row = {
        "id": str(conversation_id),
        "session_id": str(session_id),
        "state": state,
        "version": 3,
        "active_run_id": None,
        "active_run_lease": None,
        "lease_expires_at": None,
        "created_at": now,
        "updated_at": now,
    }
    persistence_requests: list[httpx.Request] = []

    def persistence_handler(request: httpx.Request) -> httpx.Response:
        persistence_requests.append(request)
        if request.method == "GET":
            if request.url.path.endswith("/runs"):
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[row])
        assert request.url.path.endswith("/rpc/update_conversation_state")
        update = json.loads(request.content)
        assert update["p_expected_version"] == 3
        assert update["p_state"]["intent"]["country"] == "US"
        assert update["p_state"]["seen_tmdb_ids"] == [603]
        row["state"] = update["p_state"]
        row["version"] = 4
        row["updated_at"] = "2026-10-25T12:02:00+00:00"
        return httpx.Response(
            200,
            json=[
                {
                    "conversation_id": str(conversation_id),
                    "version": 4,
                    "updated_at": "2026-10-25T12:02:00+00:00",
                }
            ],
        )

    def tmdb_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/3/movie/603/watch/providers"
        return httpx.Response(
            200,
            json={
                "id": 603,
                "results": {
                    "US": {
                        "link": "https://www.themoviedb.org/movie/603/watch?locale=US",
                        "flatrate": [
                            {"provider_id": 8, "provider_name": "Netflix", "logo_path": "/n.jpg"}
                        ],
                    }
                },
            },
        )

    persistence_http = httpx.AsyncClient(transport=httpx.MockTransport(persistence_handler))
    tmdb_http = httpx.AsyncClient(transport=httpx.MockTransport(tmdb_handler))
    monkeypatch.setattr(
        app.state,
        "persistence",
        PersistenceClient(persistence_http, "https://project.supabase.co", "test-secret"),
        raising=False,
    )
    monkeypatch.setattr(app.state, "tmdb", TmdbClient(tmdb_http, "test-tmdb-token"), raising=False)
    monkeypatch.setenv("SESSION_SIGNING_KEY", SIGNING_KEY)
    token = sign_session_token(session_id, datetime.now(UTC) + timedelta(hours=1), SIGNING_KEY)

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            client.cookies.set("movie_agent_session", token)
            response = await client.patch(
                f"/api/recommendations/conversations/{conversation_id}/availability",
                json={"country": "us", "expected_version": 3},
            )
            restored = await client.get(f"/api/recommendations/conversations/{conversation_id}")

        assert response.status_code == 200
        payload = response.json()
        assert payload["version"] == 4
        assert payload["intent"]["country"] == "US"
        assert payload["result"]["recommendations"][0]["movie"]["tmdb_id"] == 603
        movie = payload["result"]["recommendations"][0]["movie"]
        assert movie["ratings"][0]["value"] == 8.2
        assert movie["watch_options"]["country"] == "US"
        assert movie["watch_options"]["subscription"][0]["name"] == "Netflix"
        assert payload["provider_lookups"] == [
            {
                "tmdb_id": 603,
                "country": "US",
                "outcome": "ok",
                "provider_count": 1,
                "detail": None,
            }
        ]
        assert restored.status_code == 200
        assert restored.json()["version"] == 4
        assert restored.json()["provider_lookups"][0]["country"] == "US"
        assert [item.method for item in persistence_requests] == [
            "GET",
            "POST",
            "GET",
            "GET",
            "GET",
        ]
    finally:
        monkeypatch.setattr(app.state, "persistence", None, raising=False)
        monkeypatch.setattr(app.state, "tmdb", None, raising=False)
        await persistence_http.aclose()
        await tmdb_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("turn_kind", "message"),
    [
        (TurnKind.INSPECT_RESULTS, "Tell me about the second one"),
        (TurnKind.OUT_OF_SCOPE, "What is the weather tomorrow?"),
    ],
)
async def test_non_discovery_turns_complete_without_tmdb_and_preserve_saved_cards(
    monkeypatch: pytest.MonkeyPatch,
    turn_kind: TurnKind,
    message: str,
) -> None:
    session_id = uuid4()
    conversation_id = uuid4()
    run_id = uuid4()
    lease_token = uuid4()
    now = "2026-10-25T12:01:00+00:00"
    batch = RecommendationBatch(
        recommendations=[
            Recommendation(
                rank=1,
                movie=MovieDetails(
                    tmdb_id=1,
                    title="Heat",
                    release_year=1995,
                    tmdb_url="https://www.themoviedb.org/movie/1",
                ),
                match_reason="TMDB genre match: Crime.",
            ),
            Recommendation(
                rank=2,
                movie=MovieDetails(
                    tmdb_id=2,
                    title="The Thing",
                    release_year=1982,
                    runtime_minutes=109,
                    genres=["Horror", "Science Fiction"],
                    tmdb_url="https://www.themoviedb.org/movie/2",
                    ratings=[Rating(source=RatingSource.TMDB, value=8.1, scale="10")],
                ),
                match_reason="TMDB genre match: Horror.",
            ),
        ],
        requested_count=2,
    )
    state = {
        "intent": {"country": "US", "requested_count": 2},
        "seen_tmdb_ids": [1, 2],
        "last_result": batch.model_dump(mode="json"),
        "last_issues": [],
        "provider_lookups": [
            {
                "tmdb_id": 2,
                "country": "US",
                "outcome": "no_data",
                "provider_count": 0,
                "detail": "TMDB returned no watch-provider data.",
            }
        ],
    }
    if turn_kind is TurnKind.OUT_OF_SCOPE:
        state["last_clarification"] = "Which actor did you mean?"
    row = {
        "id": str(conversation_id),
        "session_id": str(session_id),
        "state": state,
        "version": 2,
        "active_run_id": None,
        "active_run_lease": None,
        "lease_expires_at": None,
        "created_at": now,
        "updated_at": now,
    }
    persisted: dict[str, object] = {}

    def persistence_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        if request.method == "GET":
            return httpx.Response(200, json=[row])
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
                        "base_version": 2,
                        "result": None,
                        "trace": None,
                        "error_code": None,
                        "usage": None,
                        "created_at": now,
                        "completed_at": None,
                    }
                ],
            )
        assert request.url.path.endswith("/rpc/finalize_run")
        persisted.update(body)
        return httpx.Response(
            200,
            json=[
                {
                    "run_id": str(run_id),
                    "conversation_id": str(conversation_id),
                    "version": 3,
                    "status": "completed",
                    "completed_at": "2026-10-25T12:02:00+00:00",
                }
            ],
        )

    async def inspect_patch(message, prior, *, model=None):
        return MovieIntentPatch(
            turn_kind=turn_kind,
            genres=(
                StringCollectionPatch(set=["Horror"])
                if turn_kind is TurnKind.OUT_OF_SCOPE
                else None
            ),
        )

    monkeypatch.setattr(recommendations_api, "extract_intent_patch", inspect_patch)
    persistence_http = httpx.AsyncClient(transport=httpx.MockTransport(persistence_handler))
    tmdb_requests: list[httpx.Request] = []

    def tmdb_handler(request: httpx.Request) -> httpx.Response:
        tmdb_requests.append(request)
        raise AssertionError("card questions must not call TMDB")

    tmdb_http = httpx.AsyncClient(transport=httpx.MockTransport(tmdb_handler))
    monkeypatch.setattr(
        app.state,
        "persistence",
        PersistenceClient(persistence_http, "https://project.supabase.co", "test-secret"),
        raising=False,
    )
    monkeypatch.setattr(app.state, "tmdb", TmdbClient(tmdb_http, "test-tmdb-token"), raising=False)
    monkeypatch.setenv("SESSION_SIGNING_KEY", SIGNING_KEY)
    token = sign_session_token(session_id, datetime.now(UTC) + timedelta(hours=1), SIGNING_KEY)

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            client.cookies.set("movie_agent_session", token)
            response = await client.post(
                "/api/recommendations/stream",
                json={
                    "message": message,
                    "client_request_id": "inspect-card-1",
                    "conversation_id": str(conversation_id),
                    "expected_version": 2,
                },
            )

        assert response.status_code == 200
        frames = [frame for frame in response.text.split("\n\n") if frame]
        answer_event = json.loads(frames[-1].split("data: ", 1)[1])
        assert answer_event["type"] == "completed"
        if turn_kind is TurnKind.OUT_OF_SCOPE:
            assert answer_event["data"]["answer"] == recommendations_api.OUT_OF_SCOPE_ANSWER
        else:
            assert "The Thing (1982)" in answer_event["data"]["answer"]
            assert "TMDB has no provider data for US" in answer_event["data"]["answer"]
        assert tmdb_requests == []
        assert persisted["p_status"] == "completed"
        saved_state = persisted["p_state"]
        assert saved_state["last_result"] == state["last_result"]  # type: ignore[index]
        assert saved_state["provider_lookups"] == state["provider_lookups"]  # type: ignore[index]
        if turn_kind is TurnKind.OUT_OF_SCOPE:
            assert saved_state["intent"]["genres"] == []  # type: ignore[index]
            assert saved_state["last_clarification"] == "Which actor did you mean?"  # type: ignore[index]
        assert persisted["p_result"] == {"answer": answer_event["data"]["answer"]}
    finally:
        monkeypatch.setattr(app.state, "persistence", None, raising=False)
        monkeypatch.setattr(app.state, "tmdb", None, raising=False)
        await persistence_http.aclose()
        await tmdb_http.aclose()


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
        assert event["data"]["requested_count"] == 4
        assert event["data"]["clarification"] == "Which decade do you prefer?"
        assert event["data"]["issues"] == []
        assert event["data"]["provider_lookups"] == []
        assert persisted["p_result"] == event["data"]
        assert persisted["p_state"]["last_clarification"] == "Which decade do you prefer?"
    finally:
        monkeypatch.setattr(app.state, "persistence", None, raising=False)
        monkeypatch.setattr(app.state, "tmdb", None, raising=False)
        await persistence_http.aclose()
        await tmdb_http.aclose()
