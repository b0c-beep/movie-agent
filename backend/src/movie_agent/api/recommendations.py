from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from movie_agent.agent.intent import extract_intent_patch
from movie_agent.agent.recommendations import (
    AssemblyIssue,
    ProviderLookup,
    ProviderLookupOutcome,
    assemble_recommendations,
    merge_intent,
    refresh_recommendation_availability,
)
from movie_agent.api.auth import require_session_id
from movie_agent.models.events import (
    EventOrigin,
    TraceEvent,
    TraceEventType,
    TraceStage,
)
from movie_agent.models.intent import MovieIntent, TurnKind
from movie_agent.models.movies import RecommendationBatch
from movie_agent.services.omdb import OmdbClient
from movie_agent.services.persistence import (
    ClaimedRun,
    PersistenceClient,
    PersistenceError,
    RunLimits,
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


class RecommendationRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=1200)
    client_request_id: str = Field(min_length=1, max_length=128)
    conversation_id: UUID | None = None
    expected_version: int | None = Field(default=None, ge=1)

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


class ConversationSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID
    version: int = Field(ge=1)
    intent: MovieIntent
    result: RecommendationBatch | None = None
    issues: list[AssemblyIssue] = Field(default_factory=list)
    provider_lookups: list[ProviderLookup] = Field(default_factory=list)


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
    return _snapshot_from_conversation(
        conversation.conversation_id,
        conversation.version,
        conversation.state,
    )


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
        claimed = await persistence.claim_run(
            session_id,
            conversation_id,
            body.client_request_id,
            version,
            {"message": body.message.strip()},
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
                    "Interpreting the latest request against saved preferences",
                )
                patch = await extract_intent_patch(body.message, prior_intent)
                failure_stage = "intent_merge"
                merge_base = (
                    MovieIntent() if patch.turn_kind is TurnKind.NEW_SEARCH else prior_intent
                )
                merged = merge_intent(merge_base, patch, seen_tmdb_ids=current_seen_ids)
                current_intent = merged.intent
                clarification: str | None
                if patch.turn_kind is TurnKind.INSPECT_RESULTS:
                    clarification = "Questions about individual movie cards are not supported yet."
                else:
                    clarification = merged.clarification

                current_state = {
                    **({} if patch.turn_kind is TurnKind.NEW_SEARCH else prior_state),
                    "intent": current_intent.model_dump(mode="json"),
                    "seen_tmdb_ids": (
                        []
                        if patch.turn_kind is TurnKind.NEW_SEARCH
                        else current_seen_ids[-MAX_SEEN_IDS:]
                    ),
                }
                yield emit(
                    TraceStage.DECIDE,
                    TraceEventType.INTENT_UPDATED,
                    "Saved the explicit filters and kept omitted preferences",
                    metadata={
                        "genre_count": len(current_intent.genres),
                        "people_count": len(current_intent.people),
                        "requested_count": current_intent.requested_count,
                        "country": current_intent.country,
                        "turn_kind": patch.turn_kind.value,
                    },
                    data={"intent": current_intent.model_dump(mode="json")},
                )

                result_data: dict[str, Any]
                if patch.turn_kind is TurnKind.INSPECT_RESULTS:
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

                if clarification:
                    result_data = {
                        "intent": current_intent.model_dump(mode="json"),
                        "recommendations": [],
                        "requested_count": current_intent.requested_count,
                        "shortfall_reason": None,
                        "clarification": clarification,
                        "issues": [],
                        "provider_lookups": [],
                    }
                    terminal = emit(
                        TraceStage.COMPLETE,
                        TraceEventType.NEEDS_CLARIFICATION,
                        clarification,
                        data=result_data,
                    )
                    failure_stage = "run_finalization"
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
                    "Resolving people, searching TMDB, and checking movie details",
                    metadata={"provider": "tmdb"},
                )
                failure_stage = "tmdb_recommendations"
                assembly = await assemble_recommendations(
                    tmdb,
                    current_intent,
                    omdb=_omdb(request),
                    excluded_tmdb_ids=(
                        merged.excluded_tmdb_ids if patch.turn_kind is TurnKind.ANOTHER_SET else ()
                    ),
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

                new_ids = [item.movie.tmdb_id for item in assembly.batch.recommendations]
                if patch.turn_kind is TurnKind.ANOTHER_SET:
                    current_seen_ids = list(dict.fromkeys([*current_seen_ids, *new_ids]))[
                        -MAX_SEEN_IDS:
                    ]
                elif patch.turn_kind is TurnKind.NEW_SEARCH:
                    current_seen_ids = new_ids
                else:
                    current_seen_ids = list(dict.fromkeys([*current_seen_ids, *new_ids]))[
                        -MAX_SEEN_IDS:
                    ]
                current_state = {
                    "intent": current_intent.model_dump(mode="json"),
                    "seen_tmdb_ids": current_seen_ids,
                    "last_result": assembly.batch.model_dump(mode="json"),
                    "last_issues": [item.model_dump(mode="json") for item in assembly.issues],
                    "provider_lookups": [
                        item.model_dump(mode="json") for item in assembly.provider_lookups
                    ],
                }
                result_data = {
                    "intent": current_intent.model_dump(mode="json"),
                    "recommendations": [
                        item.model_dump(mode="json") for item in assembly.batch.recommendations
                    ],
                    "requested_count": assembly.batch.requested_count,
                    "shortfall_reason": assembly.batch.shortfall_reason,
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
            terminal = emit(
                TraceStage.COMPLETE,
                TraceEventType.FAILED,
                "The recommendation run could not be completed",
                metadata=_failure_metadata(failure_stage, error, code),
            )
            with contextlib.suppress(PersistenceError):
                await _finalize(
                    persistence,
                    session_id,
                    claimed,
                    TerminalRunStatus.FAILED,
                    error_code=code,
                    result={"error_code": code},
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
    except Exception:
        raise HTTPException(
            status_code=503, detail="Stored conversation data is malformed"
        ) from None
    return ConversationSnapshot(
        conversation_id=conversation_id,
        version=version,
        intent=intent,
        result=result,
        issues=issues,
        provider_lookups=provider_lookups,
    )


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
    if isinstance(error, TimeoutError):
        return "run_timeout"
    if isinstance(error, TmdbError):
        return "tmdb_unavailable"
    return "recommendation_failed"


def _failure_metadata(
    stage: str, error: Exception, code: str
) -> dict[str, str | int | float | bool | None]:
    """Expose bounded diagnostic labels, never provider messages or user input."""
    return {
        "error_code": code,
        "failure_stage": stage,
        "exception_type": type(error).__name__,
    }


def _log_run_failure(stage: str, run_id: UUID, error: Exception, code: str) -> None:
    """Log bounded diagnostics without user input, provider text, or a traceback."""
    logger.warning(
        "recommendation_run_failed stage=%s exception_type=%s run_id=%s error_code=%s",
        stage,
        type(error).__name__,
        run_id,
        code,
    )
