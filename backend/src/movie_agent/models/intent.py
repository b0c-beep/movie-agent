from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TurnKind(StrEnum):
    NEW_SEARCH = "new_search"
    REFINE = "refine"
    ANOTHER_SET = "another_set"
    INSPECT_RESULTS = "inspect_results"
    OUT_OF_SCOPE = "out_of_scope"


class PersonConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=120)
    role: str | None = Field(default=None, max_length=60)
    tmdb_id: int | None = Field(default=None, gt=0)


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
    tmdb_rating_min: float | None = Field(default=None, ge=0, le=10)
    country: str = Field(default="RO", pattern=r"^[A-Z]{2}$")
    requested_count: int = Field(default=4, ge=1, le=10)

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
    original_language: str | None = Field(default=None, pattern=r"^[a-z]{2}$")
    runtime_max_minutes: int | None = Field(default=None, ge=1, le=600)
    tmdb_rating_min: float | None = Field(default=None, ge=0, le=10)
    people: list[PersonConstraint] | None = Field(default=None, max_length=10)
    clear_filters: list[
        Literal[
            "people",
            "release_year",
            "original_language",
            "runtime_max_minutes",
            "tmdb_rating_min",
        ]
    ] = Field(default_factory=list, max_length=5)
    requested_count: int | None = Field(default=None, ge=1, le=10)
    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    similar_to_title: str | None = Field(default=None, min_length=1, max_length=120)
    similar_to_year: int | None = Field(default=None, ge=1888, le=2200)
    clarification: str | None = Field(default=None, max_length=240)

    @model_validator(mode="after")
    def clear_does_not_conflict_with_updates(self) -> MovieIntentPatch:
        if self.similar_to_year is not None and self.similar_to_title is None:
            raise ValueError("similar_to_year requires similar_to_title")
        conflicts = {
            "people": self.people is not None,
            "release_year": self.release_year_min is not None or self.release_year_max is not None,
            "original_language": self.original_language is not None,
            "runtime_max_minutes": self.runtime_max_minutes is not None,
            "tmdb_rating_min": self.tmdb_rating_min is not None,
        }
        if any(field in self.clear_filters and changed for field, changed in conflicts.items()):
            raise ValueError("clear_filters cannot conflict with an update to the same field")
        if len(set(self.clear_filters)) != len(self.clear_filters):
            raise ValueError("clear_filters entries must be unique")
        return self
