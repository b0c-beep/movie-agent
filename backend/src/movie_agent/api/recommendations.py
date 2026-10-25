from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import re
from collections.abc import AsyncIterator, Sequence
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_ai.exceptions import ModelRateLimitError

from movie_agent.agent.intent import extract_intent_patch
from movie_agent.agent.recommendations import (
    AssemblyIssue,
    ProviderLookup,
    ProviderLookupOutcome,
    assemble_recommendations,
    is_supported_tmdb_genre,
    merge_intent,
    refresh_recommendation_availability,
)
from movie_agent.api.auth import require_session_id
from movie_agent.models.clarification import (
    ClarificationOption,
    PendingClarification,
    StoredClarificationOption,
)
from movie_agent.models.events import (
    EventOrigin,
    TraceEvent,
    TraceEventType,
    TraceStage,
)
from movie_agent.models.intent import (
    MovieIntent,
    MovieIntentPatch,
    PersonConstraint,
    TurnKind,
)
from movie_agent.models.movies import RecommendationBatch
from movie_agent.services.omdb import OmdbClient
from movie_agent.services.persistence import (
    ClaimedRun,
    PersistenceClient,
    PersistenceError,
    RunLimits,
    RunRecord,
    RunStatus,
    TerminalRunStatus,
)
from movie_agent.services.tmdb import TmdbClient, TmdbError

router = APIRouter(prefix="/api/recommendations", tags=["recommendations"])
logger = logging.getLogger(__name__)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
MAX_RUN_SECONDS = 90
RUN_LEASE_SECONDS = 120
MAX_SEEN_IDS = 200
MAX_HISTORY_TURNS = 6
MAX_HISTORY_EVENTS = 24
MAX_HISTORY_CHARS = 32_000
FILTER_EDIT_FIELDS = frozenset(
    {
        "genres",
        "moods",
        "people",
        "release_year",
        "release_year_min",
        "release_year_max",
        "original_language",
        "runtime_max_minutes",
        "tmdb_rating_min",
        "country",
        "requested_count",
    }
)


class FilterEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1, max_length=40)
    operation: Literal["set", "remove", "clear"]
    value: str | int | float | None = None

    @field_validator("field")
    @classmethod
    def validate_field(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized not in FILTER_EDIT_FIELDS:
            raise ValueError("filter_edit field is not supported")
        return normalized

    @model_validator(mode="after")
    def value_matches_operation(self) -> FilterEdit:
        if self.operation in {"set", "remove"} and self.value is None:
            raise ValueError(f"filter_edit {self.operation} requires a value")
        if self.operation == "clear" and self.value is not None:
            raise ValueError("filter_edit clear does not accept a value")
        return self


class RecommendationRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=1200)
    client_request_id: str = Field(min_length=1, max_length=128)
    conversation_id: UUID | None = None
    expected_version: int | None = Field(default=None, ge=1)
    clarification_choice_id: str | None = Field(
        default=None,
        pattern=r"^[a-z]+:[0-9]+$",
        max_length=64,
    )
    filter_edit: FilterEdit | None = None

    @field_validator("client_request_id")
    @classmethod
    def validate_request_id(cls, value: str) -> str:
        if _REQUEST_ID.fullmatch(value) is None:
            raise ValueError("client_request_id has unsupported characters")
        return value

    @field_validator("message")
    @classmethod
    def validate_message_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value


class AvailabilityRefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    country: str = Field(pattern=r"^[A-Z]{2}$")
    expected_version: int = Field(ge=1)

    @field_validator("country", mode="before")
    @classmethod
    def normalize_country(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value


class ConversationTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    user_message: str = Field(min_length=1, max_length=1200)
    status: str = Field(min_length=1, max_length=40)
    response: dict[str, Any] | None = None
    answer: str | None = Field(default=None, max_length=1200)
    events: list[TraceEvent] = Field(default_factory=list, max_length=MAX_HISTORY_EVENTS)


class ConversationSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID
    version: int = Field(ge=1)
    intent: MovieIntent
    result: RecommendationBatch | None = None
    clarification: str | None = Field(default=None, max_length=240)
    clarification_options: list[ClarificationOption] = Field(default_factory=list, max_length=4)
    turns: list[ConversationTurn] = Field(default_factory=list, max_length=MAX_HISTORY_TURNS)
    issues: list[AssemblyIssue] = Field(default_factory=list)
    provider_lookups: list[ProviderLookup] = Field(default_factory=list)


class CapabilitiesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    history: bool = True
    structured_clarification: bool = True
    filter_edit: bool = True
    alternatives: bool = True
    run_status: bool = True


class RunStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    conversation_id: UUID
    status: str = Field(min_length=1, max_length=40)
    version: int = Field(ge=1)
    events: list[TraceEvent] = Field(default_factory=list)
    response: dict[str, Any] | None = None


def _persistence(request: Request) -> PersistenceClient:
    value = getattr(request.app.state, "persistence", None)
    if not isinstance(value, PersistenceClient):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Conversation storage is unavailable",
        )
    return value


def _tmdb(request: Request) -> TmdbClient:
    value = getattr(request.app.state, "tmdb", None)
    if not isinstance(value, TmdbClient):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Movie search is not configured",
        )
    return value


def _omdb(request: Request) -> OmdbClient | None:
    value = getattr(request.app.state, "omdb", None)
    return value if isinstance(value, OmdbClient) else None


@router.get("/capabilities", response_model=CapabilitiesResponse)
async def get_capabilities(
    session_id: Annotated[UUID, Depends(require_session_id)],
) -> CapabilitiesResponse:
    del session_id
    return CapabilitiesResponse()


@router.get("/runs/{run_id}", response_model=RunStatusResponse)
async def get_run_status(
    run_id: UUID,
    request: Request,
    session_id: Annotated[UUID, Depends(require_session_id)],
) -> RunStatusResponse:
    persistence = _persistence(request)
    try:
        run = await persistence.get_run(session_id, run_id)
    except PersistenceError as error:
        raise _persistence_http_error(error) from error
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    events = _events_from_run(run)
    response = run.result if isinstance(run.result, dict) else None
    return RunStatusResponse(
        run_id=run.run_id,
        conversation_id=run.conversation_id,
        status=run.status.value,
        version=run.conversation_version,
        events=events,
        response=response,
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationSnapshot)
async def get_conversation_snapshot(
    conversation_id: UUID,
    request: Request,
    session_id: Annotated[UUID, Depends(require_session_id)],
) -> ConversationSnapshot:
    persistence = _persistence(request)
    try:
        conversation = await persistence.get_conversation(session_id, conversation_id)
    except PersistenceError as error:
        raise _persistence_http_error(error) from error
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    snapshot = _snapshot_from_conversation(
        conversation.conversation_id,
        conversation.version,
        conversation.state,
    )
    try:
        runs = await persistence.list_conversation_runs(
            session_id,
            conversation_id,
            limit=MAX_HISTORY_TURNS,
        )
        snapshot.turns = _snapshot_turns_from_runs(runs)
    except PersistenceError as error:
        raise _persistence_http_error(error) from error
    return snapshot


@router.patch(
    "/conversations/{conversation_id}/availability",
    response_model=ConversationSnapshot,
)
async def refresh_conversation_availability(
    conversation_id: UUID,
    body: AvailabilityRefreshRequest,
    request: Request,
    session_id: Annotated[UUID, Depends(require_session_id)],
) -> ConversationSnapshot:
    persistence = _persistence(request)
    tmdb = _tmdb(request)
    try:
        conversation = await persistence.get_conversation(session_id, conversation_id)
    except PersistenceError as error:
        raise _persistence_http_error(error) from error
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if body.expected_version != conversation.version:
        raise HTTPException(status_code=409, detail="Conversation version changed")

    try:
        intent = MovieIntent.model_validate(conversation.state.get("intent", {}))
        raw_result = conversation.state.get("last_result")
        result = RecommendationBatch.model_validate(raw_result) if raw_result is not None else None
        issues = [
            AssemblyIssue.model_validate(item) for item in conversation.state.get("last_issues", [])
        ]
        pending = _stored_pending(conversation.state)
        clarification = pending.text if pending is not None else None
        clarification_options = pending.public_options() if pending is not None else []
        turns = _snapshot_turns(conversation.state)
    except Exception:
        raise HTTPException(
            status_code=503, detail="Stored conversation data is malformed"
        ) from None

    updated_intent = intent.model_copy(update={"country": body.country})
    provider_lookups: list[ProviderLookup] = []
    if result is not None:
        assembly = await refresh_recommendation_availability(
            tmdb,
            result,
            country=body.country,
        )
        result = assembly.batch
        provider_lookups = assembly.provider_lookups

    next_state = {
        **conversation.state,
        "intent": updated_intent.model_dump(mode="json"),
        "last_result": result.model_dump(mode="json") if result is not None else None,
        "last_issues": [item.model_dump(mode="json") for item in issues],
        "provider_lookups": [item.model_dump(mode="json") for item in provider_lookups],
    }
    try:
        updated = await persistence.update_conversation_state(
            session_id,
            conversation_id,
            body.expected_version,
            next_state,
        )
    except PersistenceError as error:
        raise _persistence_http_error(error) from error

    return ConversationSnapshot(
        conversation_id=conversation_id,
        version=updated.version,
        intent=updated_intent,
        result=result,
        clarification=clarification,
        clarification_options=clarification_options,
        turns=turns,
        issues=issues,
        provider_lookups=provider_lookups,
    )


@router.post("/stream")
async def stream_recommendations(
    body: RecommendationRunRequest,
    request: Request,
    session_id: Annotated[UUID, Depends(require_session_id)],
) -> StreamingResponse:
    persistence = _persistence(request)
    tmdb = _tmdb(request)
    seen_ids: list[int] = []
    prior_state: dict[str, Any] = {}
    if _REQUEST_ID.fullmatch(body.client_request_id) is None:
        raise HTTPException(status_code=422, detail="Invalid client_request_id")
    if body.clarification_choice_id is not None and body.conversation_id is None:
        raise HTTPException(status_code=422, detail="Clarification choice requires a conversation")
    if body.filter_edit is not None and body.conversation_id is None:
        raise HTTPException(status_code=422, detail="Filter edit requires a conversation")

    try:
        if body.conversation_id is None:
            created = await persistence.create_conversation(session_id, {})
            conversation_id = created.conversation_id
            version = created.version
            prior_intent = MovieIntent()
            prior_state = {}
        else:
            conversation = await persistence.get_conversation(session_id, body.conversation_id)
            if conversation is None:
                raise HTTPException(status_code=404, detail="Conversation not found")
            if body.expected_version is None:
                raise HTTPException(status_code=422, detail="expected_version is required")
            if body.expected_version != conversation.version:
                raise HTTPException(status_code=409, detail="Conversation version changed")
            conversation_id = conversation.conversation_id
            version = conversation.version
            state = conversation.state
            prior_state = state
            prior_intent = MovieIntent.model_validate(state.get("intent", {}))
            raw_seen = state.get("seen_tmdb_ids", [])
            seen_ids = [value for value in raw_seen if isinstance(value, int) and value > 0]
    except PersistenceError as error:
        raise _persistence_http_error(error) from error

    try:
        deterministic_intent, direct_reference = _build_deterministic_turn(
            body,
            prior_intent,
            prior_state,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None

    claim_input: dict[str, Any] = {"message": body.message.strip()}
    if body.clarification_choice_id is not None:
        claim_input["clarification_choice_id"] = body.clarification_choice_id
    if body.filter_edit is not None:
        claim_input["filter_edit"] = body.filter_edit.model_dump(mode="json")

    try:
        claimed = await persistence.claim_run(
            session_id,
            conversation_id,
            body.client_request_id,
            version,
            claim_input,
            RunLimits(lease_seconds=RUN_LEASE_SECONDS),
        )
    except PersistenceError as error:
        raise _persistence_http_error(error) from error

    if claimed.replayed:
        if claimed.status is RunStatus.RUNNING:
            raise HTTPException(status_code=409, detail="This request is still running")
        replay_events = _load_replay_events(claimed)
        return _stream_response(replay_events, conversation_id, claimed.run_id)
    if claimed.lease_token is None:
        raise HTTPException(status_code=503, detail="Could not claim recommendation run")

    async def events() -> AsyncIterator[str]:
        sequence = 0
        trace: list[dict[str, Any]] = []
        current_seen_ids = list(seen_ids)

        def emit(
            stage: TraceStage,
            event_type: TraceEventType,
            summary: str,
            *,
            metadata: dict[str, str | int | float | bool | None] | None = None,
            data: dict[str, Any] | None = None,
        ) -> str:
            nonlocal sequence
            sequence += 1
            event = TraceEvent(
                run_id=claimed.run_id,
                sequence=sequence,
                stage=stage,
                type=event_type,
                origin=EventOrigin.APPLICATION,
                summary=summary,
                metadata=metadata or {},
                data=data,
            )
            trace.append(event.model_dump(mode="json"))
            return event.to_sse()

        current_intent = prior_intent
        current_state: dict[str, Any] = {
            **prior_state,
            "intent": prior_intent.model_dump(mode="json"),
        }
        failure_stage = "intent_extraction"
        try:
            yield emit(
                TraceStage.PREDICT,
                TraceEventType.RUN_STARTED,
                "Starting a protected movie search",
                metadata={"conversation_id": str(conversation_id), "version": version},
            )
            async with asyncio.timeout(MAX_RUN_SECONDS):
                yield emit(
                    TraceStage.PREDICT,
                    TraceEventType.DECISION,
                    (
                        "Applying the selected clarification or filter edit"
                        if deterministic_intent is not None
                        else "Interpreting the latest request against saved preferences"
                    ),
                )
                similar_reference: dict[str, Any] | None = None
                title_clarification: PendingClarification | None = None
                excluded_tmdb_ids: Sequence[int] = ()
                if deterministic_intent is not None:
                    turn_kind = TurnKind.REFINE
                    current_intent = deterministic_intent
                    similar_reference = direct_reference or _saved_similar_reference(prior_state)
                    clarification = None
                else:
                    patch = await extract_intent_patch(body.message, prior_intent)
                    turn_kind = patch.turn_kind
                    if turn_kind is not TurnKind.INSPECT_RESULTS:
                        failure_stage = "tmdb_title_lookup"
                        if patch.similar_to_title is not None:
                            yield emit(
                                TraceStage.ACT,
                                TraceEventType.TOOL_STARTED,
                                "Checking the named reference title in TMDB",
                                metadata={"provider": "tmdb", "operation": "search_movies"},
                            )
                            (
                                similar_reference,
                                title_clarification,
                            ) = await _resolve_similar_reference_turn(
                                tmdb,
                                patch.similar_to_title,
                                patch.similar_to_year,
                            )
                            yield emit(
                                TraceStage.ACT,
                                TraceEventType.TOOL_FINISHED,
                                (
                                    "Reference title was resolved"
                                    if similar_reference
                                    else "Reference title needs clarification"
                                ),
                                metadata={"provider": "tmdb", "operation": "search_movies"},
                            )
                        elif turn_kind is not TurnKind.NEW_SEARCH:
                            similar_reference = _saved_similar_reference(prior_state)
                    else:
                        similar_reference = _saved_similar_reference(prior_state)
                    failure_stage = "intent_merge"
                    merge_base = MovieIntent() if turn_kind is TurnKind.NEW_SEARCH else prior_intent
                    merged = merge_intent(merge_base, patch, seen_tmdb_ids=current_seen_ids)
                    current_intent = merged.intent
                    excluded_tmdb_ids = (
                        merged.excluded_tmdb_ids if turn_kind is TurnKind.ANOTHER_SET else ()
                    )
                    clarification = (
                        None if turn_kind is TurnKind.INSPECT_RESULTS else merged.clarification
                    )

                pending = title_clarification
                if clarification is not None and pending is None:
                    pending = PendingClarification(text=clarification)
                elif clarification is not None and pending is not None:
                    pending = PendingClarification(
                        text=clarification[:240],
                        options=pending.options,
                    )

                if turn_kind is TurnKind.NEW_SEARCH:
                    current_state = {
                        "intent": current_intent.model_dump(mode="json"),
                        "seen_tmdb_ids": [],
                        "history": _stored_history(prior_state),
                    }
                else:
                    current_state = {
                        **prior_state,
                        "intent": current_intent.model_dump(mode="json"),
                        "seen_tmdb_ids": (
                            current_seen_ids[-MAX_SEEN_IDS:] if current_seen_ids else []
                        ),
                    }
                if similar_reference is not None:
                    current_state["similar_reference"] = similar_reference
                else:
                    current_state.pop("similar_reference", None)
                yield emit(
                    TraceStage.DECIDE,
                    TraceEventType.INTENT_UPDATED,
                    "Saved the explicit filters and kept omitted preferences",
                    metadata={
                        "genre_count": len(current_intent.genres),
                        "people_count": len(current_intent.people),
                        "requested_count": current_intent.requested_count,
                        "country": current_intent.country,
                        "turn_kind": turn_kind.value,
                    },
                    data={"intent": current_intent.model_dump(mode="json")},
                )

                result_data: dict[str, Any]
                if turn_kind is TurnKind.INSPECT_RESULTS:
                    _clear_pending_clarification(current_state)
                    answer = _answer_from_saved_results(
                        body.message,
                        prior_state.get("last_result"),
                        prior_state.get("provider_lookups"),
                    )
                    result_data = {"answer": answer}
                    terminal = emit(
                        TraceStage.COMPLETE,
                        TraceEventType.COMPLETED,
                        "Answered from this conversation's saved movie cards",
                        data=result_data,
                    )
                    failure_stage = "run_finalization"
                    current_state = _record_turn(
                        current_state,
                        run_id=claimed.run_id,
                        user_message=body.message,
                        status=TerminalRunStatus.COMPLETED,
                        result_data=result_data,
                        trace=trace,
                    )
                    await _finalize(
                        persistence,
                        session_id,
                        claimed,
                        TerminalRunStatus.COMPLETED,
                        state=current_state,
                        result=result_data,
                        trace=trace,
                    )
                    yield terminal
                    return

                if pending is not None:
                    _set_pending_clarification(current_state, pending)
                    result_data = {
                        "intent": current_intent.model_dump(mode="json"),
                        "recommendations": [],
                        "requested_count": current_intent.requested_count,
                        "shortfall_reason": None,
                        "clarification": pending.text,
                        "clarification_options": [
                            option.model_dump(mode="json") for option in pending.public_options()
                        ],
                        "issues": [],
                        "provider_lookups": [],
                    }
                    terminal = emit(
                        TraceStage.COMPLETE,
                        TraceEventType.NEEDS_CLARIFICATION,
                        pending.text,
                        data=result_data,
                    )
                    failure_stage = "run_finalization"
                    current_state = _record_turn(
                        current_state,
                        run_id=claimed.run_id,
                        user_message=body.message,
                        status=TerminalRunStatus.NEEDS_CLARIFICATION,
                        result_data=result_data,
                        trace=trace,
                    )
                    await _finalize(
                        persistence,
                        session_id,
                        claimed,
                        TerminalRunStatus.NEEDS_CLARIFICATION,
                        state=current_state,
                        result=result_data,
                        trace=trace,
                    )
                    yield terminal
                    return

                yield emit(
                    TraceStage.ACT,
                    TraceEventType.TOOL_STARTED,
                    (
                        f"Finding verified movies similar to {similar_reference['title']}"
                        if similar_reference is not None
                        else "Resolving people, searching TMDB, and checking movie details"
                    ),
                    metadata={"provider": "tmdb"},
                )
                failure_stage = "tmdb_recommendations"
                assembly = await assemble_recommendations(
                    tmdb,
                    current_intent,
                    omdb=_omdb(request),
                    similar_to_tmdb_id=(
                        similar_reference["tmdb_id"] if similar_reference is not None else None
                    ),
                    similar_to_title=(
                        similar_reference["title"] if similar_reference is not None else None
                    ),
                    excluded_tmdb_ids=excluded_tmdb_ids,
                )
                yield emit(
                    TraceStage.ACT,
                    TraceEventType.TOOL_FINISHED,
                    f"TMDB verification produced {len(assembly.batch.recommendations)} cards",
                    metadata={
                        "candidate_issue_count": len(assembly.issues),
                        "provider_lookup_count": len(assembly.provider_lookups),
                    },
                )

                if assembly.clarification is not None:
                    _set_pending_clarification(current_state, assembly.clarification)
                    current_state["last_issues"] = [
                        item.model_dump(mode="json") for item in assembly.issues
                    ]
                    result_data = {
                        "intent": current_intent.model_dump(mode="json"),
                        "recommendations": [],
                        "requested_count": current_intent.requested_count,
                        "shortfall_reason": None,
                        "clarification": assembly.clarification.text,
                        "clarification_options": [
                            option.model_dump(mode="json")
                            for option in assembly.clarification.public_options()
                        ],
                        "issues": [item.model_dump(mode="json") for item in assembly.issues],
                        "provider_lookups": [],
                    }
                    terminal = emit(
                        TraceStage.COMPLETE,
                        TraceEventType.NEEDS_CLARIFICATION,
                        assembly.clarification.text,
                        data=result_data,
                    )
                    failure_stage = "run_finalization"
                    current_state = _record_turn(
                        current_state,
                        run_id=claimed.run_id,
                        user_message=body.message,
                        status=TerminalRunStatus.NEEDS_CLARIFICATION,
                        result_data=result_data,
                        trace=trace,
                    )
                    await _finalize(
                        persistence,
                        session_id,
                        claimed,
                        TerminalRunStatus.NEEDS_CLARIFICATION,
                        state=current_state,
                        result=result_data,
                        trace=trace,
                    )
                    yield terminal
                    return

                new_ids = [item.movie.tmdb_id for item in assembly.batch.recommendations]
                if turn_kind is TurnKind.NEW_SEARCH:
                    current_seen_ids = new_ids
                else:
                    current_seen_ids = list(dict.fromkeys([*current_seen_ids, *new_ids]))[
                        -MAX_SEEN_IDS:
                    ]
                current_state = {
                    **(
                        {
                            "history": _stored_history(prior_state),
                        }
                        if turn_kind is TurnKind.NEW_SEARCH
                        else prior_state
                    ),
                    "intent": current_intent.model_dump(mode="json"),
                    "seen_tmdb_ids": current_seen_ids,
                    "last_result": assembly.batch.model_dump(mode="json"),
                    **({"similar_reference": similar_reference} if similar_reference else {}),
                    "last_issues": [item.model_dump(mode="json") for item in assembly.issues],
                    "provider_lookups": [
                        item.model_dump(mode="json") for item in assembly.provider_lookups
                    ],
                }
                _clear_pending_clarification(current_state)
                result_data = {
                    "intent": current_intent.model_dump(mode="json"),
                    "recommendations": [
                        item.model_dump(mode="json") for item in assembly.batch.recommendations
                    ],
                    "requested_count": assembly.batch.requested_count,
                    "shortfall_reason": assembly.batch.shortfall_reason,
                    "alternatives": [
                        item.model_dump(mode="json") for item in assembly.batch.alternatives
                    ],
                    "issues": [item.model_dump(mode="json") for item in assembly.issues],
                    "provider_lookups": [
                        item.model_dump(mode="json") for item in assembly.provider_lookups
                    ],
                }
                recommendations_frame = emit(
                    TraceStage.COMPLETE,
                    TraceEventType.RECOMMENDATIONS,
                    f"Prepared {len(assembly.batch.recommendations)} grounded recommendations",
                    metadata={
                        "result_count": len(assembly.batch.recommendations),
                        "alternative_count": len(assembly.batch.alternatives),
                        "requested_count": assembly.batch.requested_count,
                        "has_shortfall": assembly.batch.shortfall_reason is not None,
                    },
                    data=result_data,
                )
                completed_frame = emit(
                    TraceStage.COMPLETE,
                    TraceEventType.COMPLETED,
                    "Recommendation run completed",
                    metadata={"conversation_id": str(conversation_id)},
                )
                failure_stage = "run_finalization"
                current_state = _record_turn(
                    current_state,
                    run_id=claimed.run_id,
                    user_message=body.message,
                    status=TerminalRunStatus.COMPLETED,
                    result_data=result_data,
                    trace=trace,
                )
                await _finalize(
                    persistence,
                    session_id,
                    claimed,
                    TerminalRunStatus.COMPLETED,
                    state=current_state,
                    result=result_data,
                    trace=trace,
                )
                yield recommendations_frame
                yield completed_frame
        except asyncio.CancelledError:
            await _finish_cancelled(
                persistence,
                session_id,
                claimed,
                trace=trace,
            )
            raise
        except Exception as error:
            code = _failure_code(error)
            _log_run_failure(failure_stage, claimed.run_id, error, code)
            failure_result: dict[str, Any] = {"error_code": code}
            retry_after = _retry_after_seconds(error) if code == "model_rate_limited" else None
            if retry_after is not None:
                failure_result["retry_after_seconds"] = retry_after
            terminal = emit(
                TraceStage.COMPLETE,
                TraceEventType.FAILED,
                (
                    "The model provider is rate limiting requests. Try again shortly."
                    if code == "model_rate_limited"
                    else "The recommendation run could not be completed"
                ),
                metadata=_failure_metadata(failure_stage, error, code),
            )
            with contextlib.suppress(PersistenceError):
                await _finalize(
                    persistence,
                    session_id,
                    claimed,
                    TerminalRunStatus.FAILED,
                    error_code=code,
                    result=failure_result,
                    trace=trace,
                )
            yield terminal

    return _stream_response(events(), conversation_id, claimed.run_id)


async def _finalize(
    persistence: PersistenceClient,
    session_id: UUID,
    claimed: ClaimedRun,
    status_value: TerminalRunStatus,
    *,
    state: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    trace: list[dict[str, Any]] | None = None,
    error_code: str | None = None,
) -> None:
    if claimed.lease_token is None:
        return
    await persistence.finalize_run(
        session_id,
        claimed.run_id,
        claimed.lease_token,
        status_value,
        state=state,
        result=result,
        trace=trace,
        error_code=error_code,
        model=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
    )


async def _finish_cancelled(
    persistence: PersistenceClient,
    session_id: UUID,
    claimed: ClaimedRun,
    *,
    trace: list[dict[str, Any]],
) -> None:
    if claimed.lease_token is None:
        return
    try:
        await asyncio.shield(
            _finalize(
                persistence,
                session_id,
                claimed,
                TerminalRunStatus.CANCELLED,
                state=None,
                result={"error_code": "client_cancelled"},
                trace=trace,
                error_code="client_cancelled",
            )
        )
    except PersistenceError:
        return


def _load_replay_events(claimed: ClaimedRun) -> list[TraceEvent]:
    if claimed.trace is None:
        terminal_types = {
            RunStatus.FAILED: (
                TraceEventType.FAILED,
                "This recommendation run ended before a result was saved",
            ),
            RunStatus.CANCELLED: (
                TraceEventType.CANCELLED,
                "This recommendation run was cancelled before a result was saved",
            ),
        }
        fallback = terminal_types.get(claimed.status)
        if fallback is None:
            raise HTTPException(status_code=503, detail="Stored run trace is unavailable")
        event_type, summary = fallback
        return [
            TraceEvent(
                run_id=claimed.run_id,
                sequence=1,
                stage=TraceStage.COMPLETE,
                type=event_type,
                origin=EventOrigin.APPLICATION,
                summary=summary,
                metadata={"error_code": claimed.error_code or "run_ended"},
            )
        ]
    try:
        return [TraceEvent.model_validate(item) for item in claimed.trace]
    except Exception:
        raise HTTPException(status_code=503, detail="Stored run trace is malformed") from None


def _stream_response(
    events: AsyncIterator[str] | list[TraceEvent],
    conversation_id: UUID,
    run_id: UUID,
) -> StreamingResponse:
    async def body() -> AsyncIterator[str]:
        if isinstance(events, list):
            for event in events:
                yield event.to_sse()
            return
        async for item in events:
            yield item

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "X-Conversation-ID": str(conversation_id),
            "X-Run-ID": str(run_id),
        },
    )


def _persistence_http_error(error: PersistenceError) -> HTTPException:
    if error.code in {"40001", "55P03", "version_conflict", "run_in_progress", "lease_mismatch"}:
        return HTTPException(status_code=409, detail="Conversation changed or has an active run")
    if error.code in {"P0001", "session_rate_limited", "global_rate_limited"}:
        return HTTPException(status_code=429, detail="Recommendation usage limit reached")
    if error.code in {"28000", "session_invalid"}:
        return HTTPException(status_code=401, detail="App session expired")
    return HTTPException(status_code=503, detail="Conversation storage is unavailable")


def _build_deterministic_turn(
    body: RecommendationRunRequest,
    prior_intent: MovieIntent,
    prior_state: dict[str, Any],
) -> tuple[MovieIntent | None, dict[str, Any] | None]:
    """Apply an owned clarification choice and/or a deterministic filter edit."""
    if body.clarification_choice_id is None and body.filter_edit is None:
        return None, None

    intent = prior_intent
    reference: dict[str, Any] | None = None
    if body.clarification_choice_id is not None:
        pending = _stored_pending(prior_state)
        if pending is None:
            raise ValueError("There is no pending clarification for this conversation")
        option = next(
            (item for item in pending.options if item.choice_id == body.clarification_choice_id),
            None,
        )
        if option is None:
            raise ValueError("That clarification choice is no longer available")
        if option.kind == "title":
            if option.title is None:
                raise ValueError("That clarification choice is no longer available")
            reference = {
                "tmdb_id": option.tmdb_id,
                "title": option.title,
                "year": option.year,
            }
        elif option.kind == "person":
            selected = PersonConstraint(
                name=option.person_name or option.label,
                role=option.person_role,
                tmdb_id=option.tmdb_id,
            )
            people: list[PersonConstraint] = []
            replaced = False
            for person in intent.people:
                if _person_choice_replaces(person, option):
                    if not replaced:
                        people.append(selected)
                        replaced = True
                    continue
                people.append(person)
            if not replaced:
                people.append(selected)
            intent = intent.model_copy(update={"people": people})
        else:
            raise ValueError("That clarification choice is not supported")

    if body.filter_edit is not None:
        intent = _apply_filter_edit(intent, body.filter_edit)

    # Preserve the same mood/genre correction used by model-authored edits.
    normalized = merge_intent(
        intent,
        MovieIntentPatch(turn_kind=TurnKind.REFINE),
    ).intent
    return normalized, reference


def _person_choice_replaces(
    person: PersonConstraint,
    option: StoredClarificationOption,
) -> bool:
    if option.replace_name is not None:
        wanted = " ".join(re.findall(r"[a-z0-9]+", option.replace_name.casefold()))
        current = " ".join(re.findall(r"[a-z0-9]+", person.name.casefold()))
        if wanted and wanted != current:
            return False
    if option.replace_role is not None and person.role is not None:
        return person.role.casefold() == option.replace_role.casefold()
    return True


def _apply_filter_edit(intent: MovieIntent, edit: FilterEdit) -> MovieIntent:
    field = edit.field
    operation = edit.operation
    updates: dict[str, Any] = {}

    if field in {"genres", "moods"}:
        current = getattr(intent, field)
        if operation == "clear":
            updates[field] = []
        else:
            value = _filter_text(edit.value, field)
            if operation == "set":
                if field == "genres" and not is_supported_tmdb_genre(value):
                    raise ValueError(
                        "TMDB does not support that genre; choose a catalog genre "
                        "or edit it as a mood"
                    )
                updates[field] = [value]
            else:
                updates[field] = [item for item in current if item.casefold() != value.casefold()]
    elif field == "people":
        if operation == "clear":
            updates["people"] = []
        elif operation == "set":
            name = _filter_text(edit.value, field)
            updates["people"] = [PersonConstraint(name=name)]
        else:
            name = _filter_text(edit.value, field)
            updates["people"] = [
                person for person in intent.people if person.name.casefold() != name.casefold()
            ]
    elif field == "release_year":
        if operation == "clear":
            updates["release_year_min"] = None
            updates["release_year_max"] = None
        elif operation == "set":
            year = _filter_int(edit.value, field, minimum=1888, maximum=2200)
            updates["release_year_min"] = year
            updates["release_year_max"] = year
        else:
            raise ValueError("filter_edit release_year supports set or clear")
    elif field in {"release_year_min", "release_year_max"}:
        if operation == "clear":
            updates[field] = None
        elif operation == "set":
            updates[field] = _filter_int(edit.value, field, minimum=1888, maximum=2200)
        else:
            raise ValueError(f"filter_edit {field} supports set or clear")
    elif field == "original_language":
        if operation == "clear":
            updates[field] = None
        elif operation == "set":
            value = _filter_text(edit.value, field).casefold()
            if re.fullmatch(r"[a-z]{2}", value) is None:
                raise ValueError("filter_edit original_language must be a two-letter code")
            updates[field] = value
        else:
            raise ValueError("filter_edit original_language supports set or clear")
    elif field == "runtime_max_minutes":
        if operation == "clear":
            updates[field] = None
        elif operation == "set":
            updates[field] = _filter_int(edit.value, field, minimum=1, maximum=600)
        else:
            raise ValueError(f"filter_edit {field} supports set or clear")
    elif field == "tmdb_rating_min":
        if operation == "clear":
            updates[field] = None
        elif operation == "set":
            rating = _filter_float(edit.value, field)
            if not 0 <= rating <= 10:
                raise ValueError("filter_edit tmdb_rating_min must be between 0 and 10")
            updates[field] = rating
        else:
            raise ValueError(f"filter_edit {field} supports set or clear")
    elif field == "country":
        if operation != "set":
            raise ValueError("filter_edit country supports set only")
        value = _filter_text(edit.value, field).upper()
        if re.fullmatch(r"[A-Z]{2}", value) is None:
            raise ValueError("filter_edit country must be a two-letter code")
        updates[field] = value
    elif field == "requested_count":
        if operation != "set":
            raise ValueError("filter_edit requested_count supports set only")
        updates[field] = _filter_int(edit.value, field, minimum=1, maximum=10)
    else:
        raise ValueError("filter_edit field is not supported")

    try:
        return MovieIntent.model_validate({**intent.model_dump(), **updates})
    except Exception as error:
        raise ValueError("filter_edit produces an invalid filter state") from error


def _filter_text(value: str | int | float | None, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"filter_edit {field} requires a non-empty text value")
    return " ".join(value.split())


def _filter_int(
    value: str | int | float | None,
    field: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool):
        raise ValueError(f"filter_edit {field} requires a whole number")
    if isinstance(value, str):
        if re.fullmatch(r"\s*[+-]?\d+\s*", value) is None:
            raise ValueError(f"filter_edit {field} requires a whole number")
        number = int(value)
    elif isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    else:
        raise ValueError(f"filter_edit {field} requires a whole number")
    if not minimum <= number <= maximum:
        raise ValueError(f"filter_edit {field} is outside the allowed range")
    return number


def _filter_float(value: str | int | float | None, field: str) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"filter_edit {field} requires a number")
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            raise ValueError(f"filter_edit {field} requires a number") from None
    if isinstance(value, (int, float)):
        return float(value)
    raise ValueError(f"filter_edit {field} requires a number")


def _stored_pending(state: dict[str, Any]) -> PendingClarification | None:
    text = _stored_clarification(state)
    if text is None:
        return None
    raw_options = state.get("last_clarification_options")
    if raw_options is None:
        return PendingClarification(text=text)
    if not isinstance(raw_options, list):
        raise ValueError("Stored clarification options are malformed")
    try:
        options = [StoredClarificationOption.model_validate(item) for item in raw_options]
        return PendingClarification(text=text, options=options if len(options) >= 2 else [])
    except Exception as error:
        raise ValueError("Stored clarification options are malformed") from error


def _set_pending_clarification(
    state: dict[str, Any],
    pending: PendingClarification,
) -> None:
    state["last_clarification"] = pending.text
    if 2 <= len(pending.options) <= 4:
        state["last_clarification_options"] = [
            option.model_dump(mode="json") for option in pending.options
        ]
    else:
        state.pop("last_clarification_options", None)


def _clear_pending_clarification(state: dict[str, Any]) -> None:
    state.pop("last_clarification", None)
    state.pop("last_clarification_options", None)


def _stored_history(state: dict[str, Any]) -> list[dict[str, Any]]:
    raw = state.get("history")
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, dict)][-MAX_HISTORY_TURNS:]


def _record_turn(
    state: dict[str, Any],
    *,
    run_id: UUID,
    user_message: str,
    status: TerminalRunStatus,
    result_data: dict[str, Any],
    trace: list[dict[str, Any]],
) -> dict[str, Any]:
    response: dict[str, Any] | None = None
    answer: str | None = None
    raw_answer = result_data.get("answer")
    if isinstance(raw_answer, str) and "recommendations" not in result_data:
        answer = raw_answer[:1200]
    else:
        response = json.loads(json.dumps(result_data))
    turn: dict[str, Any] = {
        "run_id": str(run_id),
        "user_message": user_message.strip()[:1200],
        "status": status.value,
        "response": response,
        "answer": answer,
        "events": _stored_events(trace),
    }
    return {**state, "history": _bounded_history([*_stored_history(state), turn])}


def _stored_events(trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for item in trace[-MAX_HISTORY_EVENTS:]:
        event = dict(item)
        event["data"] = None
        events.append(event)
    return events


def _bounded_history(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bounded = [dict(item) for item in turns if isinstance(item, dict)][-MAX_HISTORY_TURNS:]
    for turn in bounded:
        events = turn.get("events")
        if isinstance(events, list):
            turn["events"] = [
                {**dict(event), "data": None}
                for event in events[-MAX_HISTORY_EVENTS:]
                if isinstance(event, dict)
            ]
        else:
            turn["events"] = []
    while len(bounded) > 1 and _history_size(bounded) > MAX_HISTORY_CHARS:
        bounded.pop(0)
    if bounded and _history_size(bounded) > MAX_HISTORY_CHARS:
        bounded[-1]["events"] = []
        response = bounded[-1].get("response")
        if isinstance(response, dict):
            trimmed = dict(response)
            recommendations = trimmed.get("recommendations")
            if isinstance(recommendations, list):
                trimmed["recommendations"] = recommendations[:3]
            alternatives = trimmed.get("alternatives")
            if isinstance(alternatives, list):
                trimmed["alternatives"] = alternatives[:1]
            bounded[-1]["response"] = trimmed
        if _history_size(bounded) > MAX_HISTORY_CHARS:
            bounded[-1]["response"] = None
    return bounded


def _history_size(turns: list[dict[str, Any]]) -> int:
    return len(json.dumps(turns, separators=(",", ":"), default=str))


def _snapshot_turns(state: dict[str, Any]) -> list[ConversationTurn]:
    turns: list[ConversationTurn] = []
    for item in _stored_history(state):
        try:
            turn = ConversationTurn.model_validate(item)
        except Exception:
            continue
        if len(turn.events) > MAX_HISTORY_EVENTS:
            turn = turn.model_copy(update={"events": turn.events[-MAX_HISTORY_EVENTS:]})
        turns.append(turn)
    return turns


def _snapshot_turns_from_runs(runs: list[RunRecord]) -> list[ConversationTurn]:
    turns: list[ConversationTurn] = []
    for run in runs[-MAX_HISTORY_TURNS:]:
        message = run.input.get("message")
        if not isinstance(message, str) or not message.strip():
            continue
        result = run.result if isinstance(run.result, dict) else None
        answer = result.get("answer") if result is not None else None
        if not isinstance(answer, str):
            answer = None
        response = result if result is not None and answer is None else None
        turns.append(
            ConversationTurn(
                run_id=run.run_id,
                user_message=message.strip()[:1200],
                status=run.status.value,
                response=response,
                answer=answer[:1200] if answer is not None else None,
                events=_events_from_run(run)[-MAX_HISTORY_EVENTS:],
            )
        )
    return turns


def _events_from_run(run: RunRecord) -> list[TraceEvent]:
    raw_trace = run.trace if isinstance(run.trace, list) else []
    try:
        events = [TraceEvent.model_validate(item) for item in raw_trace]
    except Exception:
        raise HTTPException(status_code=503, detail="Stored run trace is malformed") from None

    expected = {
        RunStatus.COMPLETED: (TraceEventType.COMPLETED, "Recommendation run completed"),
        RunStatus.FAILED: (
            TraceEventType.FAILED,
            "The recommendation run could not be completed",
        ),
        RunStatus.CANCELLED: (
            TraceEventType.CANCELLED,
            "The recommendation run was cancelled",
        ),
        RunStatus.NEEDS_CLARIFICATION: (
            TraceEventType.NEEDS_CLARIFICATION,
            "More information is needed before recommending movies",
        ),
    }.get(run.status)
    if expected is None:
        return events
    event_type, summary = expected
    if events and events[-1].type is event_type:
        return events
    sequence = max((event.sequence for event in events), default=0) + 1
    if run.status is RunStatus.NEEDS_CLARIFICATION:
        clarification = run.result.get("clarification") if isinstance(run.result, dict) else None
        if isinstance(clarification, str) and clarification.strip():
            summary = clarification[:240]
    metadata: dict[str, str | int | float | bool | None] = {}
    if run.error_code:
        metadata["error_code"] = run.error_code
    events.append(
        TraceEvent(
            run_id=run.run_id,
            sequence=sequence,
            stage=TraceStage.COMPLETE,
            type=event_type,
            origin=EventOrigin.APPLICATION,
            summary=summary,
            metadata=metadata,
        )
    )
    return events


def _retry_after_seconds(error: Exception) -> int | None:
    retry_after = getattr(error, "retry_after", None)
    if (
        isinstance(retry_after, (int, float))
        and not isinstance(retry_after, bool)
        and math.isfinite(retry_after)
        and retry_after > 0
    ):
        return min(max(math.ceil(retry_after), 1), 3600)
    text = str(error).casefold()
    match = re.search(
        r"(?:retry[-_ ]after|try again in|retry in)[^0-9]{0,16}"
        r"(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m)\b",
        text,
    )
    if match is None:
        return None
    value = float(match.group(1))
    if match.group(2).startswith("m"):
        value *= 60
    seconds = math.ceil(value)
    if seconds < 1:
        return 1
    return min(seconds, 3600)


async def _resolve_similar_reference_turn(
    client: TmdbClient,
    title: str,
    year: int | None,
) -> tuple[dict[str, Any] | None, PendingClarification | None]:
    try:
        page = await client.search_movies(title, year=year)
    except (TmdbError, ValueError):
        return None, PendingClarification(
            text="I couldn't verify that reference title with TMDB. Please try again."
        )

    wanted = _normalize_title(title)
    exact = [
        movie
        for movie in page.results
        if wanted
        in {
            _normalize_title(movie.title),
            _normalize_title(movie.original_title or ""),
        }
        and (year is None or movie.release_year == year)
    ]
    if len(exact) == 1:
        movie = exact[0]
        return {
            "tmdb_id": movie.tmdb_id,
            "title": movie.title,
            "year": movie.release_year,
        }, None

    choices = exact or page.results[:4]
    if not choices:
        return None, PendingClarification(
            text=(
                f"I couldn't find {title!r} in TMDB. Try another title or include its release year."
            )[:240]
        )

    labels = [
        f"{movie.title} ({movie.release_year})" if movie.release_year else movie.title
        for movie in choices
    ]
    prompt = (
        "Which reference movie did you mean? Choose one: "
        + "; ".join(f"{index}. {label}" for index, label in enumerate(labels, start=1))
        + ". Reply with 'like Title (Year)' so I can search TMDB's similar titles."
    )
    options: list[StoredClarificationOption] = []
    for movie in choices:
        label = f"{movie.title} ({movie.release_year})" if movie.release_year else movie.title
        detail = "TMDB title match"
        if movie.release_year is not None:
            detail += f"; release year {movie.release_year}"
        options.append(
            StoredClarificationOption(
                choice_id=f"title:{movie.tmdb_id}",
                kind="title",
                label=label[:120],
                detail=detail[:240],
                tmdb_id=movie.tmdb_id,
                title=movie.title[:200],
                year=movie.release_year,
            )
        )
    return None, PendingClarification(text=prompt[:240], options=options)


async def _resolve_similar_reference(
    client: TmdbClient,
    title: str,
    year: int | None,
) -> tuple[dict[str, Any] | None, str | None]:
    reference, pending = await _resolve_similar_reference_turn(client, title, year)
    return reference, pending.text if pending is not None else None


def _saved_similar_reference(state: dict[str, Any]) -> dict[str, Any] | None:
    value = state.get("similar_reference")
    if not isinstance(value, dict):
        return None
    tmdb_id = value.get("tmdb_id")
    title = value.get("title")
    year = value.get("year")
    if (
        isinstance(tmdb_id, bool)
        or not isinstance(tmdb_id, int)
        or tmdb_id <= 0
        or not isinstance(title, str)
        or not title.strip()
    ):
        return None
    return {"tmdb_id": tmdb_id, "title": title, "year": year}


def _normalize_title(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _snapshot_from_conversation(
    conversation_id: UUID,
    version: int,
    state: dict[str, Any],
) -> ConversationSnapshot:
    try:
        intent = MovieIntent.model_validate(state.get("intent", {}))
        raw_result = state.get("last_result")
        result = RecommendationBatch.model_validate(raw_result) if raw_result is not None else None
        issues = [AssemblyIssue.model_validate(item) for item in state.get("last_issues", [])]
        provider_lookups = [
            ProviderLookup.model_validate(item) for item in state.get("provider_lookups", [])
        ]
        pending = _stored_pending(state)
        clarification = pending.text if pending is not None else None
        clarification_options = pending.public_options() if pending is not None else []
        turns = _snapshot_turns(state)
    except Exception:
        raise HTTPException(
            status_code=503, detail="Stored conversation data is malformed"
        ) from None
    return ConversationSnapshot(
        conversation_id=conversation_id,
        version=version,
        intent=intent,
        result=result,
        clarification=clarification,
        clarification_options=clarification_options,
        turns=turns,
        issues=issues,
        provider_lookups=provider_lookups,
    )


def _stored_clarification(state: dict[str, Any]) -> str | None:
    value = state.get("last_clarification")
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 240:
        raise ValueError("Stored clarification is malformed")
    return value


def _answer_from_saved_results(
    message: str,
    raw_result: Any,
    raw_provider_lookups: Any = None,
) -> str:
    try:
        batch = RecommendationBatch.model_validate(raw_result)
        lookups = [
            ProviderLookup.model_validate(item)
            for item in (raw_provider_lookups if isinstance(raw_provider_lookups, list) else [])
        ]
    except Exception:
        return (
            "There are no saved movie cards in this conversation yet. "
            "Ask for recommendations first."
        )
    if not batch.recommendations:
        return (
            "There are no saved movie cards in this conversation yet. "
            "Ask for recommendations first."
        )

    lower_message = message.casefold()
    ordinal_positions = {
        1: r"\b(?:first|1st|one)\b",
        2: r"\b(?:second|2nd|two)\b",
        3: r"\b(?:third|3rd|three)\b",
        4: r"\b(?:fourth|4th|four)\b",
        5: r"\b(?:fifth|5th|five)\b",
        6: r"\b(?:sixth|6th|six)\b",
        7: r"\b(?:seventh|7th|seven)\b",
        8: r"\b(?:eighth|8th|eight)\b",
        9: r"\b(?:ninth|9th|nine)\b",
        10: r"\b(?:tenth|10th|ten)\b",
    }
    ordinal_matches = [
        (match.start(), index)
        for index, pattern in ordinal_positions.items()
        if (match := re.search(pattern, lower_message)) is not None
    ]
    selected_index = min(ordinal_matches)[1] if ordinal_matches else None
    selected = next(
        (item for item in batch.recommendations if item.movie.title.casefold() in lower_message),
        None,
    )
    if selected is None and selected_index is not None:
        selected = next(
            (item for item in batch.recommendations if item.rank == selected_index),
            None,
        )
    if selected is None and selected_index is None and len(batch.recommendations) == 1:
        selected = batch.recommendations[0]
    if selected is None:
        titles = ", ".join(item.movie.title for item in batch.recommendations)
        return f"Which movie do you mean? The saved picks in this conversation are: {titles}."

    movie = selected.movie
    facts = [movie.title]
    if movie.release_year is not None:
        facts[-1] += f" ({movie.release_year})"
    if movie.runtime_minutes is not None:
        facts.append(f"runtime {movie.runtime_minutes} minutes")
    if movie.genres:
        facts.append(f"genres {', '.join(movie.genres)}")
    tmdb_rating = next(
        (rating for rating in movie.ratings if rating.source.value == "tmdb"),
        None,
    )
    if tmdb_rating is not None:
        facts.append(
            "TMDB rating "
            + (
                f"{tmdb_rating.value:.1f}/{tmdb_rating.scale}"
                if tmdb_rating.value is not None
                else "not rated"
            )
        )
    parts = ["; ".join(facts) + f". Match reason: {selected.match_reason}"]

    lookup = next((item for item in lookups if item.tmdb_id == movie.tmdb_id), None)
    if lookup is not None and lookup.outcome is ProviderLookupOutcome.ERROR:
        parts.append(f"Availability in {lookup.country} is unknown because its lookup failed.")
    elif lookup is not None and lookup.outcome is ProviderLookupOutcome.NO_DATA:
        parts.append(f"TMDB has no provider data for {lookup.country}.")
    elif movie.watch_options is not None:
        options = movie.watch_options
        providers = list(
            dict.fromkeys(
                provider.name
                for group in (
                    options.subscription,
                    options.free,
                    options.ads,
                    options.rent,
                    options.buy,
                )
                for provider in group
            )
        )
        parts.append(
            f"Providers listed for {options.country}: {', '.join(providers)}."
            if providers
            else f"TMDB lists no providers for {options.country}."
        )
    return " ".join(parts)


def _failure_code(error: Exception) -> str:
    if isinstance(error, ModelRateLimitError):
        return "model_rate_limited"
    if isinstance(error, TimeoutError):
        return "run_timeout"
    if isinstance(error, TmdbError):
        return "tmdb_unavailable"
    return "recommendation_failed"


def _failure_metadata(
    stage: str, error: Exception, code: str
) -> dict[str, str | int | float | bool | None]:
    """Expose bounded diagnostic labels, never provider messages or user input."""
    metadata: dict[str, str | int | float | bool | None] = {
        "error_code": code,
        "failure_stage": stage,
        "exception_type": type(error).__name__,
    }
    if code == "model_rate_limited":
        retry_after = _retry_after_seconds(error)
        if retry_after is not None:
            metadata["retry_after_seconds"] = retry_after
    return metadata


def _log_run_failure(stage: str, run_id: UUID, error: Exception, code: str) -> None:
    """Log bounded diagnostics without user input, provider text, or a traceback."""
    logger.warning(
        "recommendation_run_failed stage=%s exception_type=%s run_id=%s error_code=%s",
        stage,
        type(error).__name__,
        run_id,
        code,
    )
