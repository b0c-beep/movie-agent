from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from movie_agent.models.events import EventOrigin, TraceEvent, TraceEventType, TraceStage

router = APIRouter(prefix="/api")


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: Literal["movie-agent-api"] = "movie-agent-api"


class FixtureStreamRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=500)


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse()


@router.post("/fixture-stream")
async def fixture_stream(request: FixtureStreamRequest) -> StreamingResponse:
    run_id = uuid4()
    summaries = (
        (TraceStage.PREDICT, TraceEventType.RUN_STARTED, "Interpreting the request"),
        (TraceStage.DECIDE, TraceEventType.DECISION, "Choosing a fixture movie lookup"),
        (TraceStage.ACT, TraceEventType.TOOL_STARTED, "Searching deterministic fixtures"),
        (TraceStage.ACT, TraceEventType.TOOL_FINISHED, "Found two fixture candidates"),
        (TraceStage.COMPLETE, TraceEventType.COMPLETED, "Fixture stream completed"),
    )

    async def events() -> AsyncIterator[str]:
        for sequence, (stage, event_type, summary) in enumerate(summaries, start=1):
            event = TraceEvent(
                run_id=run_id,
                sequence=sequence,
                stage=stage,
                type=event_type,
                origin=EventOrigin.APPLICATION,
                summary=summary,
                metadata={"fixture": True, "prompt_length": len(request.prompt)},
            )
            yield event.to_sse()
            await asyncio.sleep(0.08)

    response = StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
    response.set_cookie(
        "fixture_transport",
        "verified",
        httponly=True,
        samesite="lax",
        secure=False,
        max_age=300,
    )
    return response
