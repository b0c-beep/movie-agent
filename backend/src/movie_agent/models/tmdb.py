from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator

MAX_PAGE = 500
MAX_RESULTS_PER_PAGE = 20
MAX_FILTER_IDS = 20
MAX_KNOWN_FOR_TITLES = 10
MIN_YEAR = 1888
MAX_YEAR = 2200
COUNTRY_PATTERN = r"^[A-Z]{2}$"
LANGUAGE_PATTERN = r"^[a-z]{2}$"


class TmdbWatchLookupStatus(StrEnum):
    OK = "ok"
    NO_DATA = "no_data"


class TmdbPerson(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tmdb_id: int = Field(gt=0)
    name: str = Field(min_length=1)
    known_for_department: str | None = None
    profile_path: str | None = None
    known_for_titles: list[str] = Field(default_factory=list, max_length=MAX_KNOWN_FOR_TITLES)


class TmdbPersonPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    page: int = Field(ge=1, le=MAX_PAGE)
    total_pages: int = Field(ge=0)
    total_results: int = Field(ge=0)
    results: list[TmdbPerson] = Field(default_factory=list, max_length=MAX_RESULTS_PER_PAGE)


class TmdbMovieCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tmdb_id: int = Field(gt=0)
    title: str = Field(min_length=1)
    original_title: str | None = None
    release_year: int | None = Field(default=None, ge=MIN_YEAR, le=MAX_YEAR)
    overview: str = ""
    poster_path: str | None = None
    tmdb_rating: float | None = Field(default=None, ge=0, le=10)
    vote_count: int | None = Field(default=None, ge=0)
    genre_ids: list[int] = Field(default_factory=list)


class TmdbMoviePage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    page: int = Field(ge=1, le=MAX_PAGE)
    total_pages: int = Field(ge=0)
    total_results: int = Field(ge=0)
    results: list[TmdbMovieCandidate] = Field(default_factory=list, max_length=MAX_RESULTS_PER_PAGE)


class TmdbCastMember(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tmdb_id: int = Field(gt=0)
    name: str = Field(min_length=1)
    character: str | None = None
    order: int | None = Field(default=None, ge=0)


class TmdbCrewMember(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tmdb_id: int = Field(gt=0)
    name: str = Field(min_length=1)
    job: str | None = None
    department: str | None = None


class TmdbMovieDetails(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tmdb_id: int = Field(gt=0)
    title: str = Field(min_length=1)
    original_title: str | None = None
    release_year: int | None = Field(default=None, ge=MIN_YEAR, le=MAX_YEAR)
    runtime_minutes: int | None = Field(default=None, ge=1)
    genres: list[str] = Field(default_factory=list)
    overview: str = ""
    poster_path: str | None = None
    backdrop_path: str | None = None
    imdb_id: str | None = None
    tmdb_rating: float | None = Field(default=None, ge=0, le=10)
    vote_count: int | None = Field(default=None, ge=0)
    keywords: list[str] = Field(default_factory=list)
    cast: list[TmdbCastMember] = Field(default_factory=list)
    crew: list[TmdbCrewMember] = Field(default_factory=list)


class TmdbWatchProvider(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: int = Field(gt=0)
    name: str = Field(min_length=1)
    logo_path: str | None = None


class TmdbWatchOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    country: str = Field(pattern=COUNTRY_PATTERN)
    status: TmdbWatchLookupStatus
    link: str | None = None
    subscription: list[TmdbWatchProvider] = Field(default_factory=list)
    free: list[TmdbWatchProvider] = Field(default_factory=list)
    ads: list[TmdbWatchProvider] = Field(default_factory=list)
    rent: list[TmdbWatchProvider] = Field(default_factory=list)
    buy: list[TmdbWatchProvider] = Field(default_factory=list)


class TmdbDiscoverFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    release_year_min: int | None = Field(default=None, ge=MIN_YEAR, le=MAX_YEAR)
    release_year_max: int | None = Field(default=None, ge=MIN_YEAR, le=MAX_YEAR)
    original_language: str | None = Field(default=None, pattern=LANGUAGE_PATTERN)
    runtime_max_minutes: int | None = Field(default=None, ge=1, le=600)
    genre_ids: list[PositiveInt] = Field(default_factory=list, max_length=MAX_FILTER_IDS)
    people_ids: list[PositiveInt] = Field(default_factory=list, max_length=MAX_FILTER_IDS)
    crew_ids: list[PositiveInt] = Field(default_factory=list, max_length=MAX_FILTER_IDS)
    watch_region: str | None = Field(default=None, pattern=COUNTRY_PATTERN)

    @model_validator(mode="after")
    def years_are_ordered(self) -> TmdbDiscoverFilters:
        if (
            self.release_year_min is not None
            and self.release_year_max is not None
            and self.release_year_min > self.release_year_max
        ):
            raise ValueError("release_year_min must not exceed release_year_max")
        return self
