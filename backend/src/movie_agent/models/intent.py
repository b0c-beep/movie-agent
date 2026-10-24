from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TurnKind(StrEnum):
    NEW_SEARCH = "new_search"
    REFINE = "refine"
    ANOTHER_SET = "another_set"
    INSPECT_RESULTS = "inspect_results"


class PersonConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=120)
    role: str | None = Field(default=None, max_length=60)


class StringCollectionPatch(BaseModel):
    """Explicit collection changes; omitted values preserve existing state."""

    model_config = ConfigDict(extra="forbid")

    set: list[str] | None = None
    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)
    clear: bool = False

    @model_validator(mode="after")
    def operations_do_not_conflict(self) -> StringCollectionPatch:
        if self.set is not None and (self.add or self.remove or self.clear):
            raise ValueError("set cannot be combined with add, remove, or clear")
        if self.clear and (self.add or self.remove):
            raise ValueError("clear cannot be combined with add or remove")
        return self


class MovieIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    genres: list[str] = Field(default_factory=list)
    people: list[PersonConstraint] = Field(default_factory=list)
    moods: list[str] = Field(default_factory=list)
    release_year_min: int | None = Field(default=None, ge=1888, le=2200)
    release_year_max: int | None = Field(default=None, ge=1888, le=2200)
    original_language: str | None = Field(default=None, pattern=r"^[a-z]{2}$")
    runtime_max_minutes: int | None = Field(default=None, ge=1, le=600)
    country: str = Field(default="RO", pattern=r"^[A-Z]{2}$")
    requested_count: int = Field(default=5, ge=1, le=10)

    @model_validator(mode="after")
    def years_are_ordered(self) -> MovieIntent:
        if (
            self.release_year_min is not None
            and self.release_year_max is not None
            and self.release_year_min > self.release_year_max
        ):
            raise ValueError("release_year_min must not exceed release_year_max")
        return self


class MovieIntentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn_kind: TurnKind
    genres: StringCollectionPatch | None = None
    moods: StringCollectionPatch | None = None
    release_year_min: int | None = Field(default=None, ge=1888, le=2200)
    release_year_max: int | None = Field(default=None, ge=1888, le=2200)
    requested_count: int | None = Field(default=None, ge=1, le=10)
    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    clarification: str | None = Field(default=None, max_length=240)
