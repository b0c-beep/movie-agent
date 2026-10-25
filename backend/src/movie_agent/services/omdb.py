from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Final

import httpx
from pydantic import BaseModel, ConfigDict, Field

from movie_agent.models.movies import Rating, RatingSource

BASE_URL: Final = "https://www.omdbapi.com/"
DEFAULT_TIMEOUT_SECONDS: Final = 5.0
MAX_IMDB_IDS: Final = 10
MAX_CONCURRENT_REQUESTS: Final = 4
IMDB_ID_PATTERN: Final = r"^tt\d{6,10}$"
IMDB_RATING_SCALE: Final = "10"
ROTTEN_TOMATOES_RATING_SCALE: Final = "100"

_IMDB_ID_RE: Final = re.compile(IMDB_ID_PATTERN)
_IMDB_VALUE_RE: Final = re.compile(r"^(?P<score>\d{1,2}(?:\.\d+)?)\s*(?:/\s*10)?$")
_ROTTEN_TOMATOES_VALUE_RE: Final = re.compile(r"^(?P<score>\d{1,3}(?:\.\d+)?)\s*%?$")
_IMDB_SOURCES: Final = frozenset({"imdb", "internet movie database"})
_ROTTEN_TOMATOES_SOURCES: Final = frozenset({"rotten tomatoes"})


class OmdbLookupStatus(StrEnum):
    OK = "ok"
    NO_RATINGS = "no_ratings"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"


class OmdbRatings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    imdb_id: str = Field(pattern=IMDB_ID_PATTERN)
    status: OmdbLookupStatus
    imdb: Rating | None = None
    rotten_tomatoes: Rating | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)


class OmdbClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        api_key: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_concurrent_requests: int = MAX_CONCURRENT_REQUESTS,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key is required")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive finite number")
        if (
            isinstance(max_concurrent_requests, bool)
            or not isinstance(max_concurrent_requests, int)
            or not 1 <= max_concurrent_requests <= MAX_IMDB_IDS
        ):
            raise ValueError(f"max_concurrent_requests must be between 1 and {MAX_IMDB_IDS}")

        self._http = http
        self._api_key = api_key.strip()
        self._timeout_seconds = float(timeout_seconds)
        self._max_concurrent_requests = max_concurrent_requests

    async def get_ratings(self, imdb_id: str) -> OmdbRatings:
        canonical_id = _require_imdb_id(imdb_id)
        try:
            response = await self._http.get(
                BASE_URL,
                params={"apikey": self._api_key, "i": canonical_id},
                headers={"Accept": "application/json"},
                timeout=self._timeout_seconds,
            )
        except httpx.HTTPError:
            return _unavailable(canonical_id)

        if not 200 <= response.status_code < 300:
            return _unavailable(canonical_id, http_status=response.status_code)

        try:
            payload = response.json()
        except ValueError:
            return _unavailable(canonical_id)
        if not isinstance(payload, Mapping):
            return _unavailable(canonical_id)

        response_flag = payload.get("Response")
        if not isinstance(response_flag, str):
            return _unavailable(canonical_id)

        normalized_flag = response_flag.strip().casefold()
        if normalized_flag == "false":
            error = payload.get("Error")
            if isinstance(error, str) and _is_not_found_error(error):
                return OmdbRatings(imdb_id=canonical_id, status=OmdbLookupStatus.NOT_FOUND)
            return _unavailable(canonical_id)
        if normalized_flag != "true":
            return _unavailable(canonical_id)

        imdb_rating, rotten_tomatoes_rating = _extract_ratings(payload)
        status = (
            OmdbLookupStatus.OK
            if imdb_rating is not None or rotten_tomatoes_rating is not None
            else OmdbLookupStatus.NO_RATINGS
        )
        return OmdbRatings(
            imdb_id=canonical_id,
            status=status,
            imdb=imdb_rating,
            rotten_tomatoes=rotten_tomatoes_rating,
        )

    async def get_ratings_for_ids(self, imdb_ids: Sequence[str]) -> list[OmdbRatings]:
        canonical_ids = _require_imdb_ids(imdb_ids)
        semaphore = asyncio.Semaphore(self._max_concurrent_requests)

        async def lookup(imdb_id: str) -> OmdbRatings:
            async with semaphore:
                return await self.get_ratings(imdb_id)

        return list(await asyncio.gather(*(lookup(imdb_id) for imdb_id in canonical_ids)))


def _extract_ratings(payload: Mapping[object, object]) -> tuple[Rating | None, Rating | None]:
    imdb_rating: Rating | None = None
    rotten_tomatoes_rating: Rating | None = None

    raw_ratings = payload.get("Ratings")
    if isinstance(raw_ratings, list):
        for raw_entry in raw_ratings:
            if not isinstance(raw_entry, Mapping):
                continue
            source = raw_entry.get("Source")
            value = raw_entry.get("Value")
            if not isinstance(source, str) or not isinstance(value, str):
                continue

            normalized_source = " ".join(source.split()).casefold()
            if imdb_rating is None and normalized_source in _IMDB_SOURCES:
                imdb_rating = _parse_imdb_rating(value)
            elif rotten_tomatoes_rating is None and normalized_source in _ROTTEN_TOMATOES_SOURCES:
                rotten_tomatoes_rating = _parse_rotten_tomatoes_rating(value)

    if imdb_rating is None:
        imdb_rating = _parse_imdb_rating(payload.get("imdbRating"))

    return imdb_rating, rotten_tomatoes_rating


def _parse_imdb_rating(value: object) -> Rating | None:
    if not isinstance(value, str):
        return None
    match = _IMDB_VALUE_RE.fullmatch(value.strip())
    if match is None:
        return None
    score = _bounded_score(match.group("score"), maximum=10.0)
    if score is None:
        return None
    return Rating(source=RatingSource.IMDB, value=score, scale=IMDB_RATING_SCALE)


def _parse_rotten_tomatoes_rating(value: object) -> Rating | None:
    if not isinstance(value, str):
        return None
    match = _ROTTEN_TOMATOES_VALUE_RE.fullmatch(value.strip())
    if match is None:
        return None
    score = _bounded_score(match.group("score"), maximum=100.0)
    if score is None:
        return None
    return Rating(
        source=RatingSource.ROTTEN_TOMATOES,
        value=score,
        scale=ROTTEN_TOMATOES_RATING_SCALE,
    )


def _bounded_score(value: str, *, maximum: float) -> float | None:
    try:
        score = float(value)
    except ValueError:
        return None
    if not math.isfinite(score) or not 0.0 <= score <= maximum:
        return None
    return score


def _is_not_found_error(message: str) -> bool:
    normalized = " ".join(message.casefold().split())
    return normalized in {"movie not found!", "incorrect imdb id."} or "not found" in normalized


def _unavailable(imdb_id: str, *, http_status: int | None = None) -> OmdbRatings:
    return OmdbRatings(
        imdb_id=imdb_id,
        status=OmdbLookupStatus.UNAVAILABLE,
        http_status=http_status,
    )


def _require_imdb_id(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("imdb_id must be a string")
    normalized = value.strip().lower()
    if _IMDB_ID_RE.fullmatch(normalized) is None:
        raise ValueError("imdb_id must be tt followed by 6-10 digits")
    return normalized


def _require_imdb_ids(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError("imdb_ids must be a sequence of IMDb IDs")
    if not 1 <= len(values) <= MAX_IMDB_IDS:
        raise ValueError(f"imdb_ids must contain 1-{MAX_IMDB_IDS} IDs")
    return tuple(_require_imdb_id(value) for value in values)
