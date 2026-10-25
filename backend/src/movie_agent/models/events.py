from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

type SafeMetadataValue = str | int | float | bool | None


class TraceStage(StrEnum):
    PREDICT = "predict"
    DECIDE = "decide"
    ACT = "act"
    COMPLETE = "complete"


class TraceEventType(StrEnum):
    RUN_STARTED = "run_started"
    INTENT_UPDATED = "intent_updated"
    DECISION = "decision"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    RECOMMENDATIONS = "recommendations"
    COMPLETED = "completed"
    NEEDS_CLARIFICATION = "needs_clarification"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EventOrigin(StrEnum):
    AGENT = "agent"
    APPLICATION = "application"


class TraceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    sequence: int = Field(ge=1)
    stage: TraceStage
    type: TraceEventType
    origin: EventOrigin
    summary: str = Field(min_length=1, max_length=240)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, SafeMetadataValue] = Field(default_factory=dict)
    data: dict[str, Any] | None = None

    @model_validator(mode="after")
    def event_data_is_bounded(self) -> TraceEvent:
        if self.data is not None and len(json.dumps(self.data, separators=(",", ":"))) > 262_144:
            raise ValueError("event data exceeds 256 KiB")
        return self

    def to_sse(self) -> str:
        payload = self.model_dump(mode="json")
        return (
            f"id: {self.sequence}\n"
            f"event: {self.type.value}\n"
            f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
        )
