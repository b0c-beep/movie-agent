from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from movie_agent.models.intent import MovieIntent

ChoiceKind = Literal["person", "title"]


class ClarificationOption(BaseModel):
    """Public, grounded choice for a pending clarification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    choice_id: str = Field(pattern=r"^[a-z]+:[0-9]+$", max_length=64)
    kind: ChoiceKind
    label: str = Field(min_length=1, max_length=120)
    detail: str = Field(min_length=1, max_length=240)


class StoredClarificationOption(ClarificationOption):
    """Persisted option with provider IDs; never exposed as-is."""

    tmdb_id: int = Field(gt=0)
    person_name: str | None = Field(default=None, max_length=120)
    person_role: str | None = Field(default=None, max_length=60)
    replace_name: str | None = Field(default=None, max_length=120)
    replace_role: str | None = Field(default=None, max_length=60)
    title: str | None = Field(default=None, max_length=200)
    year: int | None = Field(default=None, ge=1888, le=2200)

    def public(self) -> ClarificationOption:
        return ClarificationOption(
            choice_id=self.choice_id,
            kind=self.kind,
            label=self.label,
            detail=self.detail,
        )


class PendingClarification(BaseModel):
    """A bounded clarification with optional choices or a proposed filter update."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, max_length=240)
    options: list[StoredClarificationOption] = Field(default_factory=list, max_length=4)
    proposed_intent: MovieIntent | None = None

    def public_options(self) -> list[ClarificationOption]:
        return [option.public() for option in self.options]
