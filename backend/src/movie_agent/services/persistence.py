from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

_CONVERSATION_SELECT = (
    "id,session_id,state,version,active_run_id,active_run_lease,"
    "lease_expires_at,created_at,updated_at"
)
_MAX_LEASE_SECONDS = 300
_MIN_LEASE_SECONDS = 10
_MAX_HOURLY_LIMIT = 100_000
AUTH_HOURLY_LIMIT = 10


class PersistenceError(Exception):
    """A failed or malformed persistence request, never a domain outcome.

    ``code`` keeps the raw PostgREST/PostgreSQL error code so callers can map
    database outcomes without parsing error messages or response bodies.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    NEEDS_CLARIFICATION = "needs_clarification"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TerminalRunStatus(StrEnum):
    COMPLETED = "completed"
    NEEDS_CLARIFICATION = "needs_clarification"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lease_seconds: int = Field(
        default=120,
        ge=_MIN_LEASE_SECONDS,
        le=_MAX_LEASE_SECONDS,
    )
    session_hourly_limit: int = Field(default=60, ge=1, le=_MAX_HOURLY_LIMIT)
    global_hourly_limit: int = Field(default=600, ge=1, le=_MAX_HOURLY_LIMIT)


class SessionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: UUID = Field(alias="id")
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None


class AuthAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    request_count: int = Field(ge=0)


class ConversationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    conversation_id: UUID = Field(alias="id")
    session_id: UUID
    state: dict[str, Any]
    version: int = Field(ge=1)
    active_run_id: UUID | None = None
    active_run_lease: UUID | None = None
    lease_expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class CreatedConversation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    conversation_id: UUID
    version: int = Field(ge=1)
    created_at: datetime


class ClaimedRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    status: RunStatus
    replayed: bool
    lease_token: UUID | None = None
    lease_expires_at: datetime | None = None
    base_version: int = Field(ge=1)
    result: dict[str, Any] | list[Any] | None = None
    trace: list[Any] | None = None
    error_code: str | None = None
    usage: dict[str, Any] | None = None
    created_at: datetime
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def running_replay_has_no_lease(self) -> ClaimedRun:
        if self.replayed and self.lease_token is not None:
            raise ValueError("replayed runs cannot carry a new lease")
        return self


class FinalizedRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    conversation_id: UUID
    version: int = Field(ge=1)
    status: TerminalRunStatus
    completed_at: datetime


class PersistenceClient:
    """Minimal Supabase Data API adapter over an injected async HTTP client.

    The client uses the backend-only service secret, fixed table/RPC paths and
    no retries. Callers own the transport lifetime and must enforce session
    ownership through the session-scoped arguments on every operation.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        service_secret: str,
    ) -> None:
        normalized_url = base_url.strip().rstrip("/")
        normalized_secret = service_secret.strip()
        if not normalized_url:
            raise ValueError("base_url is required")
        if not normalized_secret:
            raise ValueError("service_secret is required")
        self._http = http
        self._base_url = normalized_url
        self._service_secret = normalized_secret

    async def create_session(
        self,
        session_id: UUID,
        expires_at: datetime,
    ) -> SessionRecord:
        _require_aware(expires_at)
        response = await self._request(
            "POST",
            "/rest/v1/sessions",
            json={"id": str(session_id), "expires_at": expires_at.isoformat()},
            prefer="return=representation",
        )
        return _validate(
            SessionRecord,
            _single_row(response, "session"),
            "session",
        )

    async def consume_auth_attempt(
        self,
        bucket_key: str,
        hourly_limit: int = AUTH_HOURLY_LIMIT,
    ) -> AuthAttempt:
        response = await self._rpc(
            "consume_auth_attempt",
            {
                "p_bucket_key": bucket_key,
                "p_hourly_limit": hourly_limit,
            },
        )
        return _validate(
            AuthAttempt,
            _single_row(response, "auth attempt"),
            "auth attempt",
        )

    async def get_conversation(
        self,
        session_id: UUID,
        conversation_id: UUID,
    ) -> ConversationRecord | None:
        response = await self._request(
            "GET",
            "/rest/v1/conversations",
            params={
                "select": _CONVERSATION_SELECT,
                "id": f"eq.{conversation_id}",
                "session_id": f"eq.{session_id}",
                "limit": "1",
            },
        )
        rows = _rows(response, "conversation")
        if not rows:
            return None
        if len(rows) > 1:
            raise PersistenceError("Persistence returned an ambiguous conversation")
        return _validate(ConversationRecord, rows[0], "conversation")

    async def create_conversation(
        self,
        session_id: UUID,
        state: Mapping[str, Any],
    ) -> CreatedConversation:
        response = await self._rpc(
            "create_conversation",
            {
                "p_session_id": str(session_id),
                "p_state": dict(state),
            },
        )
        return _validate(
            CreatedConversation,
            _single_row(response, "created conversation"),
            "created conversation",
        )

    async def claim_run(
        self,
        session_id: UUID,
        conversation_id: UUID,
        client_request_id: str,
        expected_version: int,
        input: Mapping[str, Any],
        limits: RunLimits | None = None,
    ) -> ClaimedRun:
        resolved_limits = limits or RunLimits()
        response = await self._rpc(
            "claim_run",
            {
                "p_session_id": str(session_id),
                "p_conversation_id": str(conversation_id),
                "p_client_request_id": client_request_id,
                "p_expected_version": expected_version,
                "p_input": dict(input),
                "p_lease_seconds": resolved_limits.lease_seconds,
                "p_session_hourly_limit": resolved_limits.session_hourly_limit,
                "p_global_hourly_limit": resolved_limits.global_hourly_limit,
            },
        )
        return _validate(
            ClaimedRun,
            _single_row(response, "claimed run"),
            "claimed run",
        )

    async def finalize_run(
        self,
        session_id: UUID,
        run_id: UUID,
        lease_token: UUID,
        status: TerminalRunStatus,
        state: Mapping[str, Any] | None = None,
        result: dict[str, Any] | list[Any] | None = None,
        trace: list[Any] | None = None,
        error_code: str | None = None,
        usage: dict[str, Any] | None = None,
        model: str | None = None,
    ) -> FinalizedRun:
        response = await self._rpc(
            "finalize_run",
            {
                "p_session_id": str(session_id),
                "p_run_id": str(run_id),
                "p_lease_token": str(lease_token),
                "p_status": status.value,
                "p_state": dict(state) if state is not None else None,
                "p_result": result,
                "p_trace": trace,
                "p_error_code": error_code,
                "p_usage": usage,
                "p_model": model,
            },
        )
        return _validate(
            FinalizedRun,
            _single_row(response, "finalized run"),
            "finalized run",
        )

    async def _rpc(
        self,
        name: str,
        payload: Mapping[str, Any],
    ) -> httpx.Response:
        return await self._request(
            "POST",
            f"/rest/v1/rpc/{name}",
            json=payload,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Mapping[str, Any] | None = None,
        prefer: str | None = None,
    ) -> httpx.Response:
        headers = {
            "apikey": self._service_secret,
            "Authorization": f"Bearer {self._service_secret}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if prefer is not None:
            headers["Prefer"] = prefer

        try:
            response = await self._http.request(
                method,
                f"{self._base_url}{path}",
                params=params,
                json=json,
                headers=headers,
            )
        except httpx.HTTPError:
            raise PersistenceError("Persistence request failed") from None

        if not 200 <= response.status_code < 300:
            raise PersistenceError(
                f"Persistence request failed with status {response.status_code}",
                status_code=response.status_code,
                code=_error_code(response),
            )
        return response


def _error_code(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, Mapping):
        return None
    code = payload.get("code")
    return code if isinstance(code, str) and code else None


def _rows(response: httpx.Response, label: str) -> list[Mapping[str, Any]]:
    try:
        payload = response.json()
    except ValueError:
        raise PersistenceError(f"Persistence returned malformed {label} JSON") from None
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
        raise PersistenceError(f"Persistence returned malformed {label} data")

    rows: list[Mapping[str, Any]] = []
    for item in payload:
        if not isinstance(item, Mapping):
            raise PersistenceError(f"Persistence returned malformed {label} data")
        rows.append(item)
    return rows


def _single_row(response: httpx.Response, label: str) -> Mapping[str, Any]:
    rows = _rows(response, label)
    if len(rows) != 1:
        raise PersistenceError(f"Persistence returned malformed {label} data")
    return rows[0]


def _validate[ModelT: BaseModel](
    model: type[ModelT],
    data: Mapping[str, Any],
    label: str,
) -> ModelT:
    try:
        return model.model_validate(data)
    except ValidationError:
        raise PersistenceError(f"Persistence returned malformed {label} data") from None


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("expires_at must be timezone-aware")
