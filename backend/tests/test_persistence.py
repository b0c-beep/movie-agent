from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from movie_agent.services.persistence import (
    PersistenceClient,
    PersistenceError,
    RunLimits,
    RunStatus,
    TerminalRunStatus,
)

BASE_URL = "https://project.supabase.co/"
SECRET = "service-role-secret-value"
CONVERSATION_SELECT = (
    "id,session_id,state,version,active_run_id,active_run_lease,"
    "lease_expires_at,created_at,updated_at"
)
Handler = Callable[[httpx.Request], httpx.Response]


def _body(request: httpx.Request) -> Any:
    return json.loads(request.content)


@pytest.mark.asyncio
async def test_create_session_posts_expected_row_and_authenticates() -> None:
    requests: list[httpx.Request] = []
    session_id = uuid4()
    expires_at = datetime(2026, 10, 26, 18, 30, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            201,
            json=[
                {
                    "id": str(session_id),
                    "created_at": "2026-10-25T12:00:00+00:00",
                    "last_seen_at": "2026-10-25T12:00:00+00:00",
                    "expires_at": expires_at.isoformat(),
                    "revoked_at": None,
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PersistenceClient(http, BASE_URL, f" {SECRET} ")
        session = await client.create_session(session_id, expires_at)

    assert session.session_id == session_id
    assert session.expires_at == expires_at
    assert session.revoked_at is None

    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"{BASE_URL.rstrip('/')}/rest/v1/sessions"
    assert request.headers["apikey"] == SECRET
    assert request.headers["authorization"] == f"Bearer {SECRET}"
    assert request.headers["accept"] == "application/json"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["prefer"] == "return=representation"
    assert _body(request) == {
        "id": str(session_id),
        "expires_at": expires_at.isoformat(),
    }


@pytest.mark.asyncio
async def test_consume_auth_attempt_calls_rpc_with_hashed_key_and_returns_outcome() -> None:
    requests: list[httpx.Request] = []
    responses = [
        httpx.Response(200, json=[{"allowed": True, "request_count": 1}]),
        httpx.Response(200, json=[{"allowed": False, "request_count": 10}]),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    bucket_key = "a" * 64
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PersistenceClient(http, BASE_URL, SECRET)
        allowed = await client.consume_auth_attempt(bucket_key)
        denied = await client.consume_auth_attempt(bucket_key)

    assert allowed.allowed is True
    assert allowed.request_count == 1
    assert denied.allowed is False
    assert denied.request_count == 10

    assert len(requests) == 2
    for request in requests:
        assert request.method == "POST"
        assert request.url.path == "/rest/v1/rpc/consume_auth_attempt"
        assert _body(request) == {
            "p_bucket_key": bucket_key,
            "p_hourly_limit": 10,
        }


@pytest.mark.asyncio
async def test_get_conversation_scopes_by_session_and_returns_none_when_missing() -> None:
    requests: list[httpx.Request] = []
    session_id = uuid4()
    conversation_id = uuid4()
    responses = [
        httpx.Response(
            200,
            json=[
                {
                    "id": str(conversation_id),
                    "session_id": str(session_id),
                    "state": {"intent": {"genre": "comedy"}},
                    "version": 3,
                    "active_run_id": None,
                    "active_run_lease": None,
                    "lease_expires_at": None,
                    "created_at": "2026-10-25T12:00:00+00:00",
                    "updated_at": "2026-10-25T12:01:00+00:00",
                }
            ],
        ),
        httpx.Response(200, json=[]),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PersistenceClient(http, BASE_URL, SECRET)
        found = await client.get_conversation(session_id, conversation_id)
        missing = await client.get_conversation(session_id, conversation_id)

    assert found is not None
    assert found.conversation_id == conversation_id
    assert found.session_id == session_id
    assert found.state == {"intent": {"genre": "comedy"}}
    assert found.version == 3
    assert missing is None
    assert len(requests) == 2

    request = requests[0]
    assert request.method == "GET"
    assert request.url.path == "/rest/v1/conversations"
    assert dict(request.url.params) == {
        "select": CONVERSATION_SELECT,
        "id": f"eq.{conversation_id}",
        "session_id": f"eq.{session_id}",
        "limit": "1",
    }
    assert request.headers["apikey"] == SECRET


@pytest.mark.asyncio
async def test_create_conversation_calls_rpc_with_state() -> None:
    requests: list[httpx.Request] = []
    session_id = uuid4()
    conversation_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
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

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PersistenceClient(http, BASE_URL, SECRET)
        created = await client.create_conversation(session_id, {"country": "RO"})

    assert created.conversation_id == conversation_id
    assert created.version == 1

    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/rest/v1/rpc/create_conversation"
    assert _body(request) == {
        "p_session_id": str(session_id),
        "p_state": {"country": "RO"},
    }


@pytest.mark.asyncio
async def test_update_conversation_state_calls_rpc_with_expected_version() -> None:
    requests: list[httpx.Request] = []
    session_id = uuid4()
    conversation_id = uuid4()
    state = {
        "intent": {"country": "DE"},
        "last_result": {"recommendations": []},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "conversation_id": str(conversation_id),
                    "version": 5,
                    "updated_at": "2026-10-25T12:05:00+00:00",
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PersistenceClient(http, BASE_URL, SECRET)
        updated = await client.update_conversation_state(
            session_id,
            conversation_id,
            4,
            state,
        )

    assert updated.conversation_id == conversation_id
    assert updated.version == 5

    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/rest/v1/rpc/update_conversation_state"
    assert _body(request) == {
        "p_session_id": str(session_id),
        "p_conversation_id": str(conversation_id),
        "p_expected_version": 4,
        "p_state": state,
    }


@pytest.mark.asyncio
async def test_update_conversation_state_preserves_conflict_markers() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            409,
            json={"code": "55P03", "message": "run_in_progress"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PersistenceClient(http, BASE_URL, SECRET)
        with pytest.raises(PersistenceError) as exc_info:
            await client.update_conversation_state(
                uuid4(),
                uuid4(),
                1,
                {"intent": {"country": "RO"}},
            )

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "55P03"
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_claim_run_calls_rpc_with_limits_and_normalizes_result() -> None:
    requests: list[httpx.Request] = []
    session_id = uuid4()
    conversation_id = uuid4()
    run_id = uuid4()
    lease_token = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "run_id": str(run_id),
                    "status": "running",
                    "replayed": False,
                    "lease_token": str(lease_token),
                    "lease_expires_at": "2026-10-25T12:03:00+00:00",
                    "base_version": 3,
                    "result": None,
                    "trace": None,
                    "error_code": None,
                    "usage": None,
                    "created_at": "2026-10-25T12:01:00+00:00",
                    "completed_at": None,
                }
            ],
        )

    limits = RunLimits(
        lease_seconds=90,
        session_hourly_limit=5,
        global_hourly_limit=50,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PersistenceClient(http, BASE_URL, SECRET)
        claimed = await client.claim_run(
            session_id,
            conversation_id,
            "request-1",
            3,
            {"message": "dark comedy"},
            limits,
        )

    assert claimed.run_id == run_id
    assert claimed.status is RunStatus.RUNNING
    assert claimed.replayed is False
    assert claimed.lease_token == lease_token
    assert claimed.base_version == 3
    assert claimed.completed_at is None

    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/rest/v1/rpc/claim_run"
    assert _body(request) == {
        "p_session_id": str(session_id),
        "p_conversation_id": str(conversation_id),
        "p_client_request_id": "request-1",
        "p_expected_version": 3,
        "p_input": {"message": "dark comedy"},
        "p_lease_seconds": 90,
        "p_session_hourly_limit": 5,
        "p_global_hourly_limit": 50,
    }


@pytest.mark.asyncio
async def test_finalize_run_sends_exact_rpc_arguments() -> None:
    requests: list[httpx.Request] = []
    session_id = uuid4()
    run_id = uuid4()
    conversation_id = uuid4()
    lease_token = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "run_id": str(run_id),
                    "conversation_id": str(conversation_id),
                    "version": 4,
                    "status": "completed",
                    "completed_at": "2026-10-25T12:02:00+00:00",
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PersistenceClient(http, BASE_URL, SECRET)
        finalized = await client.finalize_run(
            session_id,
            run_id,
            lease_token,
            TerminalRunStatus.COMPLETED,
            state={"intent": {"genre": "comedy"}},
            result={"recommendations": []},
            trace=[{"type": "completed"}],
            error_code=None,
            usage={"model_responses": 2},
            model="openai/gpt-oss-20b",
        )

    assert finalized.run_id == run_id
    assert finalized.conversation_id == conversation_id
    assert finalized.version == 4
    assert finalized.status is TerminalRunStatus.COMPLETED

    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/rest/v1/rpc/finalize_run"
    assert _body(request) == {
        "p_session_id": str(session_id),
        "p_run_id": str(run_id),
        "p_lease_token": str(lease_token),
        "p_status": "completed",
        "p_state": {"intent": {"genre": "comedy"}},
        "p_result": {"recommendations": []},
        "p_trace": [{"type": "completed"}],
        "p_error_code": None,
        "p_usage": {"model_responses": 2},
        "p_model": "openai/gpt-oss-20b",
    }


@pytest.mark.asyncio
async def test_http_error_preserves_db_code_without_leaking_body_or_secret() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            409,
            json={
                "code": "40001",
                "message": "private database detail in response body",
                "details": "private detail",
                "hint": "private hint",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PersistenceClient(http, BASE_URL, SECRET)
        with pytest.raises(PersistenceError) as exc_info:
            await client.claim_run(
                uuid4(),
                uuid4(),
                "request-1",
                1,
                {"message": "hello"},
            )

    error = exc_info.value
    assert error.status_code == 409
    assert error.code == "40001"
    assert SECRET not in str(error)
    assert "private database detail" not in str(error)
    assert error.__cause__ is None
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_transport_error_maps_to_typed_error_without_retry() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ConnectError("transport included private detail", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PersistenceClient(http, BASE_URL, SECRET)
        with pytest.raises(PersistenceError) as exc_info:
            await client.create_session(
                uuid4(),
                datetime(2026, 10, 26, 18, 30, tzinfo=UTC),
            )

    error = exc_info.value
    assert error.status_code is None
    assert error.code is None
    assert SECRET not in str(error)
    assert "private detail" not in str(error)
    assert error.__cause__ is None
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_malformed_success_responses_raise_typed_errors() -> None:
    session_id = uuid4()
    expires_at = datetime(2026, 10, 26, 18, 30, tzinfo=UTC)

    async def run_case(response: httpx.Response) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return response

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = PersistenceClient(http, BASE_URL, SECRET)
            with pytest.raises(PersistenceError):
                await client.create_session(session_id, expires_at)

    await run_case(httpx.Response(200, text="not json"))
    await run_case(httpx.Response(200, json={"id": str(session_id)}))
    await run_case(httpx.Response(200, json=[{"id": str(session_id)}]))
    await run_case(
        httpx.Response(
            200,
            json=[
                {
                    "id": str(session_id),
                    "created_at": "2026-10-25T12:00:00+00:00",
                    "last_seen_at": "2026-10-25T12:00:00+00:00",
                    "expires_at": expires_at.isoformat(),
                    "revoked_at": None,
                    "unexpected": True,
                }
            ],
        )
    )


@pytest.mark.asyncio
async def test_replayed_run_cannot_carry_a_new_lease() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "run_id": str(uuid4()),
                    "status": "completed",
                    "replayed": True,
                    "lease_token": str(uuid4()),
                    "lease_expires_at": "2026-10-25T12:03:00+00:00",
                    "base_version": 1,
                    "result": {"recommendations": []},
                    "trace": [],
                    "error_code": None,
                    "usage": None,
                    "created_at": "2026-10-25T12:01:00+00:00",
                    "completed_at": "2026-10-25T12:02:00+00:00",
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PersistenceClient(http, BASE_URL, SECRET)
        with pytest.raises(PersistenceError):
            await client.claim_run(
                uuid4(),
                uuid4(),
                "request-1",
                1,
                {},
            )


@pytest.mark.asyncio
async def test_local_validation_happens_before_any_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ValueError, match="base_url"):
            PersistenceClient(http, "  ", SECRET)
        with pytest.raises(ValueError, match="service_secret"):
            PersistenceClient(http, BASE_URL, "  ")

        client = PersistenceClient(http, BASE_URL, SECRET)
        with pytest.raises(ValueError, match="timezone-aware"):
            await client.create_session(uuid4(), datetime(2026, 10, 26, 18, 30))

    assert requests == []

    with pytest.raises(ValidationError):
        RunLimits(lease_seconds=9)
    with pytest.raises(ValidationError):
        RunLimits(session_hourly_limit=0)
