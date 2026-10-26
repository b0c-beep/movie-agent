from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from pydantic_ai.exceptions import ModelRateLimitError

import movie_agent.api.recommendations as recommendations_api
from movie_agent.main import app
from movie_agent.models.clarification import StoredClarificationOption
from movie_agent.models.events import EventOrigin, TraceEvent, TraceEventType, TraceStage
from movie_agent.models.intent import MovieIntent, PersonConstraint
from movie_agent.security.sessions import sign_session_token
from movie_agent.services.persistence import PersistenceClient
from movie_agent.services.tmdb import TmdbClient

SIGNING_KEY = "test-session-signing-key-that-is-at-least-32-bytes"


def _token(session_id) -> str:
    return sign_session_token(
        session_id,
        datetime.now(UTC) + timedelta(hours=1),
        SIGNING_KEY,
    )


def _conversation_row(
    *,
    conversation_id,
    session_id,
    state: dict[str, object],
    version: int,
) -> dict[str, object]:
    now = "2026-10-25T12:00:00+00:00"
    return {
        "id": str(conversation_id),
        "session_id": str(session_id),
        "state": state,
        "version": version,
        "active_run_id": None,
        "active_run_lease": None,
        "lease_expires_at": None,
        "created_at": now,
        "updated_at": now,
    }


def _pending_option(choice_id: str, tmdb_id: int, name: str) -> StoredClarificationOption:
    return StoredClarificationOption(
        choice_id=choice_id,
        kind="person",
        label=name,
        detail="Acting; known for a grounded title",
        tmdb_id=tmdb_id,
        person_name=name,
        person_role="actor",
        replace_name="Chris Evans",
        replace_role="actor",
    )


def test_phase4_filter_edits_are_deterministic_and_validate_values() -> None:
    conversation_id = uuid4()
    body = recommendations_api.RecommendationRunRequest(
        message="Keep it short",
        client_request_id="filter-1",
        conversation_id=conversation_id,
        expected_version=2,
        filter_edit=recommendations_api.FilterEdit(
            field="runtime_max_minutes",
            operation="set",
            value="100",
        ),
    )
    prior = MovieIntent(genres=["Comedy"], requested_count=3)

    intent, reference = recommendations_api._build_deterministic_turn(body, prior, {})

    assert reference is None
    assert intent is not None
    assert intent.genres == ["Comedy"]
    assert intent.requested_count == 3
    assert intent.runtime_max_minutes == 100

    removal = recommendations_api.RecommendationRunRequest(
        message="Remove comedy",
        client_request_id="filter-remove",
        conversation_id=conversation_id,
        expected_version=2,
        filter_edit=recommendations_api.FilterEdit(
            field="genres",
            operation="remove",
            value="Comedy",
        ),
    )
    removed, _ = recommendations_api._build_deterministic_turn(removal, prior, {})
    assert removed is not None
    assert removed.genres == []

    invalid = recommendations_api.RecommendationRunRequest(
        message="Keep it short",
        client_request_id="filter-2",
        conversation_id=conversation_id,
        expected_version=2,
        filter_edit=recommendations_api.FilterEdit(
            field="runtime_max_minutes",
            operation="set",
            value="not-a-number",
        ),
    )
    with pytest.raises(ValueError, match="whole number"):
        recommendations_api._build_deterministic_turn(invalid, prior, {})


def test_confirmed_conflicting_year_bound_replaces_the_prior_range() -> None:
    prior = MovieIntent(
        genres=["Romance"],
        moods=["Dark"],
        release_year_min=1990,
        release_year_max=1999,
        requested_count=3,
    )
    patch = recommendations_api.MovieIntentPatch(
        turn_kind="refine",
        release_year_min=2016,
        clarification="Do you want movies released after 2015 instead of 1990-1999?",
    )

    proposed = recommendations_api._proposed_year_replacement(prior, patch)
    assert proposed is not None
    assert proposed.genres == ["Romance"]
    assert proposed.moods == ["Dark"]
    assert proposed.release_year_min == 2016
    assert proposed.release_year_max is None
    assert proposed.requested_count == 3

    state: dict[str, object] = {}
    recommendations_api._set_pending_clarification(
        state,
        recommendations_api.PendingClarification(
            text=patch.clarification,
            proposed_intent=proposed,
        ),
    )
    confirmation = recommendations_api.RecommendationRunRequest(
        message="yes",
        client_request_id="year-confirm",
        conversation_id=uuid4(),
        expected_version=2,
    )

    confirmed, _ = recommendations_api._build_deterministic_turn(confirmation, prior, state)

    assert confirmed == proposed


def test_phase4_filter_edit_uses_mood_field_and_rejects_unknown_genre() -> None:
    body = recommendations_api.RecommendationRunRequest(
        message="Make it suspenseful",
        client_request_id="filter-3",
        conversation_id=uuid4(),
        expected_version=1,
        filter_edit=recommendations_api.FilterEdit(
            field="moods",
            operation="set",
            value="Suspenseful",
        ),
    )

    intent, _ = recommendations_api._build_deterministic_turn(body, MovieIntent(), {})

    assert intent is not None
    assert intent.genres == []
    assert intent.moods == ["Suspenseful"]

    unsupported = recommendations_api.RecommendationRunRequest(
        message="Make it psychological",
        client_request_id="filter-unsupported-genre",
        conversation_id=uuid4(),
        expected_version=1,
        filter_edit=recommendations_api.FilterEdit(
            field="genres",
            operation="set",
            value="psychological",
        ),
    )
    with pytest.raises(ValueError, match="TMDB does not support that genre"):
        recommendations_api._build_deterministic_turn(unsupported, MovieIntent(), {})


def test_phase4_person_choice_uses_the_owned_grounded_option() -> None:
    options = [
        _pending_option("person:101", 101, "Chris Evans"),
        _pending_option("person:202", 202, "Chris Evans"),
    ]
    state = {
        "last_clarification": "Which Chris Evans did you mean?",
        "last_clarification_options": [option.model_dump(mode="json") for option in options],
    }
    body = recommendations_api.RecommendationRunRequest(
        message="The first one",
        client_request_id="choice-1",
        conversation_id=uuid4(),
        expected_version=1,
        clarification_choice_id="person:202",
    )

    intent, reference = recommendations_api._build_deterministic_turn(
        body,
        MovieIntent(people=[PersonConstraint(name="Chris Evans")]),
        state,
    )

    assert reference is None
    assert intent is not None
    assert len(intent.people) == 1
    assert intent.people[0].name == "Chris Evans"
    assert intent.people[0].tmdb_id == 202
    assert intent.people[0].role == "actor"

    with pytest.raises(ValueError, match="no longer available"):
        recommendations_api._build_deterministic_turn(
            body.model_copy(update={"clarification_choice_id": "person:999"}),
            MovieIntent(people=[PersonConstraint(name="Chris Evans")]),
            state,
        )


def test_phase4_person_choice_preserves_other_unresolved_people() -> None:
    option = _pending_option("person:101", 101, "Chris Evans")
    other_option = _pending_option("person:202", 202, "Chris Evans")
    body = recommendations_api.RecommendationRunRequest(
        message="Chris Evans the actor",
        client_request_id="choice-two-actors",
        conversation_id=uuid4(),
        expected_version=1,
        clarification_choice_id=option.choice_id,
    )

    intent, _ = recommendations_api._build_deterministic_turn(
        body,
        MovieIntent(
            people=[
                PersonConstraint(name="Chris Evans", role="actor"),
                PersonConstraint(name="Riley Keough", role="actor"),
            ]
        ),
        {
            "last_clarification": "Which Chris Evans?",
            "last_clarification_options": [
                option.model_dump(mode="json"),
                other_option.model_dump(mode="json"),
            ],
        },
    )

    assert intent is not None
    assert [(person.name, person.tmdb_id) for person in intent.people] == [
        ("Chris Evans", 101),
        ("Riley Keough", None),
    ]


def test_phase4_title_choice_returns_grounded_reference_without_mutating_filters() -> None:
    option = StoredClarificationOption(
        choice_id="title:949",
        kind="title",
        label="Heat (1995)",
        detail="TMDB title match; release year 1995",
        tmdb_id=949,
        title="Heat",
        year=1995,
    )
    state = {
        "last_clarification": "Which Heat did you mean?",
        "last_clarification_options": [
            option.model_dump(mode="json"),
            option.model_copy(update={"choice_id": "title:1124", "year": 2022}).model_dump(
                mode="json"
            ),
        ],
    }
    body = recommendations_api.RecommendationRunRequest(
        message="Heat 1995",
        client_request_id="choice-2",
        conversation_id=uuid4(),
        expected_version=1,
        clarification_choice_id="title:949",
    )

    intent, reference = recommendations_api._build_deterministic_turn(
        body,
        MovieIntent(genres=["Crime"]),
        state,
    )

    assert intent is not None
    assert intent.genres == ["Crime"]
    assert reference == {"tmdb_id": 949, "title": "Heat", "year": 1995}


def test_phase4_snapshot_returns_bounded_turns_and_public_clarification_options() -> None:
    options = [
        _pending_option("person:101", 101, "Chris Evans"),
        _pending_option("person:202", 202, "Chris Evans"),
    ]
    turns = [
        {
            "run_id": str(uuid4()),
            "user_message": f"Turn {index}",
            "status": "completed",
            "response": {"recommendations": [], "requested_count": 1},
            "answer": None,
            "events": [],
        }
        for index in range(8)
    ]
    snapshot = recommendations_api._snapshot_from_conversation(
        uuid4(),
        3,
        {
            "intent": {"country": "RO", "requested_count": 1},
            "last_clarification": "Which Chris Evans did you mean?",
            "last_clarification_options": [option.model_dump(mode="json") for option in options],
            "history": turns,
        },
    )

    assert len(snapshot.turns) == recommendations_api.MAX_HISTORY_TURNS
    assert snapshot.turns[-1].user_message == "Turn 7"
    assert len(snapshot.clarification_options) == 2
    assert "tmdb_id" not in snapshot.clarification_options[0].model_dump()
    assert snapshot.clarification == "Which Chris Evans did you mean?"


def test_phase4_rate_limit_failure_metadata_is_stable_and_safe() -> None:
    error = ModelRateLimitError(
        "groq",
        "provider response contains private prompt details",
        retry_after=12.2,
    )

    code = recommendations_api._failure_code(error)
    metadata = recommendations_api._failure_metadata("intent_extraction", error, code)

    assert code == "model_rate_limited"
    assert metadata["error_code"] == "model_rate_limited"
    assert metadata["retry_after_seconds"] == 13
    assert "private prompt details" not in str(metadata)


@pytest.mark.asyncio
async def test_phase4_capabilities_are_session_owned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSION_SIGNING_KEY", SIGNING_KEY)
    session_id = uuid4()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        unauthorized = await client.get("/api/recommendations/capabilities")
        client.cookies.set("movie_agent_session", _token(session_id))
        response = await client.get("/api/recommendations/capabilities")

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    assert response.json() == {
        "history": True,
        "structured_clarification": True,
        "filter_edit": True,
        "alternatives": True,
        "run_status": True,
    }


@pytest.mark.asyncio
async def test_phase4_run_status_is_session_owned_and_terminal_consistent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid4()
    conversation_id = uuid4()
    foreign_conversation_id = uuid4()
    run_id = uuid4()
    foreign_run_id = uuid4()
    result = {
        "intent": {"country": "RO", "requested_count": 1},
        "recommendations": [],
        "requested_count": 1,
        "shortfall_reason": "No grounded candidates.",
        "alternatives": [],
        "issues": [],
        "provider_lookups": [],
    }
    now = "2026-10-25T12:00:00+00:00"
    runs = {
        str(run_id): {
            "id": str(run_id),
            "conversation_id": str(conversation_id),
            "status": "completed",
            "base_version": 4,
            "result": result,
            "trace": [],
            "error_code": None,
            "created_at": now,
            "completed_at": now,
        },
        str(foreign_run_id): {
            "id": str(foreign_run_id),
            "conversation_id": str(foreign_conversation_id),
            "status": "failed",
            "base_version": 2,
            "result": {"error_code": "recommendation_failed"},
            "trace": [],
            "error_code": "recommendation_failed",
            "created_at": now,
            "completed_at": now,
        },
    }
    conversations = {
        str(conversation_id): _conversation_row(
            conversation_id=conversation_id,
            session_id=session_id,
            state={},
            version=4,
        )
    }
    requests: list[httpx.Request] = []

    def persistence_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        if request.url.path == "/rest/v1/runs":
            wanted = str(request.url.params["id"]).removeprefix("eq.")
            return httpx.Response(200, json=[runs[wanted]])
        if request.url.path == "/rest/v1/conversations":
            wanted = str(request.url.params["id"]).removeprefix("eq.")
            row = conversations.get(wanted)
            return httpx.Response(200, json=[row] if row is not None else [])
        raise AssertionError(f"Unexpected persistence path: {request.url.path}")

    persistence_http = httpx.AsyncClient(transport=httpx.MockTransport(persistence_handler))
    monkeypatch.setattr(
        app.state,
        "persistence",
        PersistenceClient(persistence_http, "https://project.supabase.co", "test-secret"),
        raising=False,
    )
    monkeypatch.setenv("SESSION_SIGNING_KEY", SIGNING_KEY)

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            client.cookies.set("movie_agent_session", _token(session_id))
            owned = await client.get(f"/api/recommendations/runs/{run_id}")
            foreign = await client.get(f"/api/recommendations/runs/{foreign_run_id}")

        assert owned.status_code == 200
        payload = owned.json()
        assert payload["run_id"] == str(run_id)
        assert payload["conversation_id"] == str(conversation_id)
        assert payload["status"] == "completed"
        assert payload["version"] == 4
        assert payload["response"] == result
        assert payload["events"][-1]["type"] == "completed"
        assert payload["events"][-1]["sequence"] == 1
        assert foreign.status_code == 404
        assert all(request.method == "GET" for request in requests)
        assert not any("/rpc/" in request.url.path for request in requests)
    finally:
        monkeypatch.setattr(app.state, "persistence", None, raising=False)
        await persistence_http.aclose()


@pytest.mark.asyncio
async def test_phase4_filter_edit_stream_skips_model_and_persists_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid4()
    conversation_id = uuid4()
    run_id = uuid4()
    lease_token = uuid4()
    now = "2026-10-25T12:00:00+00:00"
    finalized: dict[str, object] = {}
    claimed_input: dict[str, object] = {}
    state = {
        "intent": {"country": "RO", "genres": ["Comedy"], "requested_count": 1},
        "seen_tmdb_ids": [],
    }
    row = _conversation_row(
        conversation_id=conversation_id,
        session_id=session_id,
        state=state,
        version=2,
    )

    def persistence_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        if request.method == "GET" and request.url.path == "/rest/v1/conversations":
            return httpx.Response(200, json=[row])
        if request.url.path.endswith("/rpc/claim_run"):
            claimed_input.update(body)
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
        if request.url.path.endswith("/rpc/finalize_run"):
            finalized.update(body)
            return httpx.Response(
                200,
                json=[
                    {
                        "run_id": str(run_id),
                        "conversation_id": str(conversation_id),
                        "version": 3,
                        "status": "completed",
                        "completed_at": now,
                    }
                ],
            )
        raise AssertionError(f"Unexpected persistence path: {request.url.path}")

    def tmdb_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/3/discover/movie":
            assert request.url.params["with_genres"] == "878"
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
                    "original_language": "en",
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

    async def unexpected_model_call(message: str, prior: object) -> object:
        raise AssertionError(f"model extraction should not run for {message} / {prior}")

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
    monkeypatch.setattr(recommendations_api, "extract_intent_patch", unexpected_model_call)

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            client.cookies.set("movie_agent_session", _token(session_id))
            response = await client.post(
                "/api/recommendations/stream",
                json={
                    "message": "Make it science fiction",
                    "client_request_id": "phase4-filter-1",
                    "conversation_id": str(conversation_id),
                    "expected_version": 2,
                    "filter_edit": {
                        "field": "genres",
                        "operation": "set",
                        "value": "Science Fiction",
                    },
                },
            )

        assert response.status_code == 200
        frames = [frame for frame in response.text.split("\n\n") if frame]
        recommendation_frame = json.loads(frames[-2].split("data: ", 1)[1])
        assert recommendation_frame["data"]["recommendations"][0]["movie"]["tmdb_id"] == 603
        assert recommendation_frame["data"]["alternatives"] == []
        assert claimed_input["p_input"]["filter_edit"] == {
            "field": "genres",
            "operation": "set",
            "value": "Science Fiction",
        }
        persisted_state = finalized["p_state"]
        assert isinstance(persisted_state, dict)
        assert persisted_state["intent"]["genres"] == ["Science Fiction"]
        assert persisted_state["intent"]["moods"] == []
        history = persisted_state["history"]
        assert len(history) == 1
        assert history[0]["run_id"] == str(run_id)
        assert history[0]["status"] == "completed"
        assert history[0]["user_message"] == "Make it science fiction"
        assert history[0]["response"]["recommendations"][0]["movie"]["tmdb_id"] == 603
        assert history[0]["events"][-1]["type"] == "completed"
        assert history[0]["events"][-1]["data"] is None
    finally:
        monkeypatch.setattr(app.state, "persistence", None, raising=False)
        monkeypatch.setattr(app.state, "tmdb", None, raising=False)
        await persistence_http.aclose()
        await tmdb_http.aclose()


def test_phase4_run_events_use_safe_synthetic_terminal_metadata() -> None:
    run_id = uuid4()
    conversation_id = uuid4()
    run = recommendations_api.RunRecord(
        id=run_id,
        conversation_id=conversation_id,
        status=recommendations_api.RunStatus.FAILED,
        base_version=1,
        result={"error_code": "model_rate_limited"},
        trace=[
            TraceEvent(
                run_id=run_id,
                sequence=1,
                stage=TraceStage.PREDICT,
                type=TraceEventType.RUN_STARTED,
                origin=EventOrigin.APPLICATION,
                summary="Started",
            ).model_dump(mode="json")
        ],
        error_code="model_rate_limited",
        created_at=datetime.now(UTC),
    )

    events = recommendations_api._events_from_run(run)

    assert events[-1].type is TraceEventType.FAILED
    assert events[-1].metadata["error_code"] == "model_rate_limited"
    assert events[-1].sequence == 2


def test_phase4_run_turn_restores_failed_user_message_and_events() -> None:
    run_id = uuid4()
    run = recommendations_api.RunRecord(
        id=run_id,
        conversation_id=uuid4(),
        status=recommendations_api.RunStatus.FAILED,
        base_version=2,
        input={"message": "Find a quiet mystery"},
        trace=[],
        error_code="model_provider_unavailable",
        created_at=datetime.now(UTC),
    )

    turns = recommendations_api._snapshot_turns_from_runs([run])

    assert len(turns) == 1
    assert turns[0].run_id == run_id
    assert turns[0].user_message == "Find a quiet mystery"
    assert turns[0].status == "failed"
    assert turns[0].events[-1].type is TraceEventType.FAILED


def test_run_status_version_uses_latest_owned_conversation_version() -> None:
    run = recommendations_api.RunRecord(
        id=uuid4(),
        conversation_id=uuid4(),
        status=recommendations_api.RunStatus.COMPLETED,
        base_version=2,
        current_conversation_version=3,
        created_at=datetime.now(UTC),
    )

    assert run.base_version == 2
    assert run.conversation_version == 3
