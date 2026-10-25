from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class RatingSource(StrEnum):
    TMDB = "tmdb"
    IMDB = "imdb"
    ROTTEN_TOMATOES = "rotten_tomatoes"


class Rating(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: RatingSource
    value: float | None = None
    scale: str
    vote_count: int | None = Field(default=None, ge=0)


class WatchProvider(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: int
    name: str
    logo_path: str | None = None


class WatchOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    country: str = Field(pattern=r"^[A-Z]{2}$")
    subscription: list[WatchProvider] = Field(default_factory=list)
    free: list[WatchProvider] = Field(default_factory=list)
    ads: list[WatchProvider] = Field(default_factory=list)
    rent: list[WatchProvider] = Field(default_factory=list)
    buy: list[WatchProvider] = Field(default_factory=list)


class MovieDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tmdb_id: int = Field(gt=0)
    title: str
    release_year: int | None = None
    runtime_minutes: int | None = Field(default=None, ge=1)
    genres: list[str] = Field(default_factory=list)
    overview: str = ""
    poster_url: HttpUrl | None = None
    tmdb_url: HttpUrl
    imdb_url: HttpUrl | None = None
    ratings: list[Rating] = Field(default_factory=list)
    watch_options: WatchOptions | None = None


class Recommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rank: int = Field(ge=1, le=10)
    movie: MovieDetails
    match_reason: str = Field(min_length=1, max_length=400)
    matched_constraints: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


class Alternative(BaseModel):
    """A separately grounded, explicitly relaxed recommendation."""

    model_config = ConfigDict(extra="forbid")

    recommendation: Recommendation
    unmet_filters: list[str] = Field(default_factory=list)


class RecommendationBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recommendations: list[Recommendation] = Field(max_length=10)
    requested_count: int = Field(ge=1, le=10)
    shortfall_reason: str | None = None
    alternatives: list[Alternative] = Field(default_factory=list, max_length=3)
