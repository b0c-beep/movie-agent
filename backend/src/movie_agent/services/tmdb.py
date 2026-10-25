from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from movie_agent.models.tmdb import (
    COUNTRY_PATTERN,
    MAX_KNOWN_FOR_TITLES,
    MAX_PAGE,
    MAX_YEAR,
    MIN_YEAR,
    TmdbDiscoverFilters,
    TmdbMovieDetails,
    TmdbMoviePage,
    TmdbPersonPage,
    TmdbWatchLookupStatus,
    TmdbWatchOptions,
)

BASE_URL = "https://api.themoviedb.org/3"
MAX_PERSON_QUERY_LENGTH = 200
MAX_TITLE_QUERY_LENGTH = 200
_APPENDED_DETAILS = "credits,keywords,external_ids"


class TmdbError(Exception):
    """A failed or malformed TMDB response, never a no-data outcome."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class TmdbClient:
    """Read-only TMDB adapter over an injected async HTTP client.

    The client performs no retries and no caching. Callers own the transport
    lifetime and configuration; this adapter only adds authentication.
    """

    def __init__(self, http: httpx.AsyncClient, bearer_token: str) -> None:
        token = bearer_token.strip()
        if not token:
            raise ValueError("bearer_token is required")
        self._http = http
        self._bearer_token = token

    async def search_people(self, name: str, *, page: int = 1) -> TmdbPersonPage:
        query = name.strip()
        if not query or len(query) > MAX_PERSON_QUERY_LENGTH:
            raise ValueError(f"person name must contain 1-{MAX_PERSON_QUERY_LENGTH} chars")
        _require_page(page)

        payload = await self._get(
            "/search/person",
            {"query": query, "page": page, "include_adult": "false"},
        )
        raw_results = _as_sequence(payload.get("results"), "person results")
        return _validate(
            TmdbPersonPage,
            {
                "page": payload.get("page"),
                "total_pages": payload.get("total_pages"),
                "total_results": payload.get("total_results"),
                "results": [_person(item) for item in raw_results],
            },
            "person search",
        )

    async def search_movies(
        self,
        title: str,
        *,
        year: int | None = None,
        page: int = 1,
    ) -> TmdbMoviePage:
        query = title.strip()
        if not query or len(query) > MAX_TITLE_QUERY_LENGTH:
            raise ValueError(f"title must contain 1-{MAX_TITLE_QUERY_LENGTH} chars")
        _require_page(page)
        if year is not None:
            _require_year(year)

        params: dict[str, str | int] = {
            "query": query,
            "page": page,
            "include_adult": "false",
        }
        if year is not None:
            params["year"] = year

        payload = await self._get("/search/movie", params)
        return _movie_page(payload, "movie title search")

    async def get_similar_movies(self, tmdb_id: int, *, page: int = 1) -> TmdbMoviePage:
        _require_tmdb_id(tmdb_id)
        _require_page(page)
        payload = await self._get(f"/movie/{tmdb_id}/similar", {"page": page})
        return _movie_page(payload, "similar movies")

    async def discover_movies(
        self,
        filters: TmdbDiscoverFilters | None = None,
        *,
        page: int = 1,
    ) -> TmdbMoviePage:
        _require_page(page)
        resolved = filters or TmdbDiscoverFilters()
        params: dict[str, str | int] = {"page": page, "include_adult": "false"}

        if resolved.release_year_min is not None:
            params["primary_release_date.gte"] = f"{resolved.release_year_min}-01-01"
        if resolved.release_year_max is not None:
            params["primary_release_date.lte"] = f"{resolved.release_year_max}-12-31"
        if resolved.original_language is not None:
            params["with_original_language"] = resolved.original_language
        if resolved.runtime_max_minutes is not None:
            params["with_runtime.lte"] = resolved.runtime_max_minutes
        if resolved.tmdb_rating_min is not None:
            params["vote_average.gte"] = str(resolved.tmdb_rating_min)
        if resolved.genre_ids:
            params["with_genres"] = ",".join(str(value) for value in resolved.genre_ids)
        if resolved.people_ids:
            params["with_people"] = ",".join(str(value) for value in resolved.people_ids)
        if resolved.crew_ids:
            params["with_crew"] = ",".join(str(value) for value in resolved.crew_ids)
        if resolved.watch_region is not None:
            params["watch_region"] = resolved.watch_region

        payload = await self._get("/discover/movie", params)
        return _movie_page(payload, "movie discovery")

    async def get_movie_details(self, tmdb_id: int) -> TmdbMovieDetails:
        _require_tmdb_id(tmdb_id)
        payload = await self._get(
            f"/movie/{tmdb_id}",
            {"append_to_response": _APPENDED_DETAILS},
        )

        credits = _optional_mapping(payload.get("credits"), "movie credits")
        cast = _as_sequence(credits.get("cast"), "movie cast")
        crew = _as_sequence(credits.get("crew"), "movie crew")
        keywords = _optional_mapping(payload.get("keywords"), "movie keywords")
        external_ids = _optional_mapping(payload.get("external_ids"), "external ids")

        return _validate(
            TmdbMovieDetails,
            {
                "tmdb_id": payload.get("id"),
                "title": payload.get("title"),
                "original_title": payload.get("original_title"),
                "release_year": _release_year(payload.get("release_date")),
                "runtime_minutes": _runtime(payload.get("runtime")),
                "original_language": _optional_text(
                    payload.get("original_language"), "movie original language"
                ),
                "genres": _named_values(payload.get("genres"), "movie genres"),
                "overview": _optional_text(payload.get("overview"), "movie overview") or "",
                "poster_path": payload.get("poster_path"),
                "backdrop_path": payload.get("backdrop_path"),
                "imdb_id": external_ids.get("imdb_id") or payload.get("imdb_id"),
                "tmdb_rating": _tmdb_rating(payload.get("vote_average"), payload.get("vote_count")),
                "vote_count": payload.get("vote_count"),
                "keywords": _named_values(keywords.get("keywords"), "movie keywords", field="name"),
                "cast": [_cast_member(item) for item in cast],
                "crew": [_crew_member(item) for item in crew],
            },
            "movie details",
        )

    async def get_watch_providers(self, tmdb_id: int, country: str) -> TmdbWatchOptions:
        _require_tmdb_id(tmdb_id)
        _require_country(country)
        payload = await self._get(f"/movie/{tmdb_id}/watch/providers")

        results = _optional_mapping(payload.get("results"), "watch provider results")
        raw_options = results.get(country)
        if raw_options is None:
            return TmdbWatchOptions(
                country=country,
                status=TmdbWatchLookupStatus.NO_DATA,
            )

        options = _as_mapping(raw_options, "country watch options")
        return _validate(
            TmdbWatchOptions,
            {
                "country": country,
                "status": TmdbWatchLookupStatus.OK,
                "link": _optional_text(options.get("link"), "watch link"),
                "subscription": _providers(options.get("flatrate"), "subscription"),
                "free": _providers(options.get("free"), "free"),
                "ads": _providers(options.get("ads"), "ads"),
                "rent": _providers(options.get("rent"), "rent"),
                "buy": _providers(options.get("buy"), "buy"),
            },
            "watch providers",
        )

    async def _get(self, path: str, params: dict[str, str | int] | None = None) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._bearer_token}",
            "Accept": "application/json",
        }
        try:
            response = await self._http.get(f"{BASE_URL}{path}", params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise TmdbError("TMDB request failed") from exc

        if not 200 <= response.status_code < 300:
            raise TmdbError(
                f"TMDB request failed with status {response.status_code}",
                status_code=response.status_code,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise TmdbError("TMDB returned malformed JSON") from exc
        if not isinstance(payload, dict):
            raise TmdbError("TMDB returned a malformed response")
        return payload


def _validate[ModelT: BaseModel](model: type[ModelT], data: Any, label: str) -> ModelT:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise TmdbError(f"TMDB returned malformed {label} data") from exc


def _as_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TmdbError(f"TMDB returned malformed {label}")
    return value


def _optional_mapping(value: object, label: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    return _as_mapping(value, label)


def _as_sequence(value: object, label: str) -> Sequence[Any]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TmdbError(f"TMDB returned malformed {label}")
    return value


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TmdbError(f"TMDB returned malformed {label}")
    return value


def _person(value: object) -> dict[str, object]:
    person = _as_mapping(value, "person result")
    titles: list[str] = []
    for raw_entry in _as_sequence(person.get("known_for"), "person known-for"):
        entry = _as_mapping(raw_entry, "person known-for entry")
        title = entry.get("title") or entry.get("name")
        if isinstance(title, str) and title:
            titles.append(title)

    return {
        "tmdb_id": person.get("id"),
        "name": person.get("name"),
        "known_for_department": person.get("known_for_department"),
        "profile_path": person.get("profile_path"),
        "known_for_titles": titles[:MAX_KNOWN_FOR_TITLES],
    }


def _movie_page(payload: Mapping[str, Any], label: str) -> TmdbMoviePage:
    raw_results = _as_sequence(payload.get("results"), "movie results")
    return _validate(
        TmdbMoviePage,
        {
            "page": payload.get("page"),
            "total_pages": payload.get("total_pages"),
            "total_results": payload.get("total_results"),
            "results": [_candidate(item) for item in raw_results],
        },
        label,
    )


def _candidate(value: object) -> dict[str, object]:
    movie = _as_mapping(value, "movie result")
    return {
        "tmdb_id": movie.get("id"),
        "title": movie.get("title"),
        "original_title": movie.get("original_title"),
        "release_year": _release_year(movie.get("release_date")),
        "overview": _optional_text(movie.get("overview"), "movie overview") or "",
        "poster_path": movie.get("poster_path"),
        "tmdb_rating": _tmdb_rating(movie.get("vote_average"), movie.get("vote_count")),
        "vote_count": movie.get("vote_count"),
        "genre_ids": list(_as_sequence(movie.get("genre_ids"), "movie genre ids")),
    }


def _cast_member(value: object) -> dict[str, object]:
    member = _as_mapping(value, "cast member")
    return {
        "tmdb_id": member.get("id"),
        "name": member.get("name"),
        "character": member.get("character"),
        "order": member.get("order"),
    }


def _crew_member(value: object) -> dict[str, object]:
    member = _as_mapping(value, "crew member")
    return {
        "tmdb_id": member.get("id"),
        "name": member.get("name"),
        "job": member.get("job"),
        "department": member.get("department"),
    }


def _named_values(value: object, label: str, *, field: str = "name") -> list[str]:
    names: list[str] = []
    for item in _as_sequence(value, label):
        entry = _as_mapping(item, label)
        name = entry.get(field)
        if not isinstance(name, str):
            raise TmdbError(f"TMDB returned malformed {label}")
        names.append(name)
    return names


def _providers(value: object, label: str) -> list[dict[str, object]]:
    providers: list[dict[str, object]] = []
    for item in _as_sequence(value, f"{label} providers"):
        provider = _as_mapping(item, f"{label} provider")
        providers.append(
            {
                "provider_id": provider.get("provider_id"),
                "name": provider.get("provider_name"),
                "logo_path": provider.get("logo_path"),
            }
        )
    return providers


def _release_year(value: object) -> int | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) < 4 or not value[:4].isdigit():
        raise TmdbError("TMDB returned malformed release date")
    year = int(value[:4])
    if not MIN_YEAR <= year <= MAX_YEAR:
        raise TmdbError("TMDB returned an out-of-range release year")
    return year


def _runtime(value: object) -> object:
    if value is None or value == 0:
        return None
    return value


def _tmdb_rating(average: object, vote_count: object) -> object:
    if average is None:
        return None
    if isinstance(vote_count, int) and vote_count <= 0:
        return None
    return average


def _require_page(page: int) -> None:
    if not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= MAX_PAGE:
        raise ValueError(f"page must be between 1 and {MAX_PAGE}")


def _require_year(year: int) -> None:
    if not isinstance(year, int) or isinstance(year, bool) or not MIN_YEAR <= year <= MAX_YEAR:
        raise ValueError(f"year must be between {MIN_YEAR} and {MAX_YEAR}")


def _require_tmdb_id(tmdb_id: int) -> None:
    if not isinstance(tmdb_id, int) or isinstance(tmdb_id, bool) or tmdb_id <= 0:
        raise ValueError("tmdb_id must be a positive integer")


def _require_country(country: str) -> None:
    if not isinstance(country, str) or re.fullmatch(COUNTRY_PATTERN, country) is None:
        raise ValueError("country must be an uppercase ISO 3166-1 alpha-2 code")
