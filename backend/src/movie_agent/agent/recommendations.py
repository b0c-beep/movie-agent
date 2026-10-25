"""Deterministic intent merging and bounded TMDB recommendation assembly.

This module contains no model inference. It applies typed intent patches,
queries only explicit validated constraints, verifies hard constraints against
TMDB details, and builds recommendations exclusively from observed facts.
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError

from movie_agent.models.intent import (
    MovieIntent,
    MovieIntentPatch,
    PersonConstraint,
    StringCollectionPatch,
    TurnKind,
)
from movie_agent.models.movies import (
    MovieDetails,
    Rating,
    RatingSource,
    Recommendation,
    RecommendationBatch,
    WatchOptions,
    WatchProvider,
)
from movie_agent.models.tmdb import (
    TmdbDiscoverFilters,
    TmdbMovieCandidate,
    TmdbMovieDetails,
    TmdbWatchLookupStatus,
    TmdbWatchOptions,
    TmdbWatchProvider,
)
from movie_agent.services.tmdb import TmdbClient, TmdbError

MAX_DISCOVERY_PAGES: Final = 3
CANDIDATES_PER_REQUESTED_MOVIE: Final = 3
MAX_DETAILS_TO_VERIFY: Final = 20
MAX_CONCURRENT_TMDB_REQUESTS: Final = 4
MAX_GENRE_FILTERS: Final = 20
MAX_OVERVIEW_CHARS: Final = 1200
MAX_REASON_CHARS: Final = 400
MAX_MATCHED_CONSTRAINTS: Final = 20
MAX_CAVEATS: Final = 10
MAX_PROVIDERS_PER_GROUP: Final = 20
TMDB_IMAGE_BASE: Final = "https://image.tmdb.org/t/p/w500"
TMDB_WEB_BASE: Final = "https://www.themoviedb.org/movie"
IMDB_WEB_BASE: Final = "https://www.imdb.com/title"
_IMDB_ID_PATTERN: Final = re.compile(r"tt\d{6,10}")

_GENRE_IDS: Final[dict[str, int]] = {
    "action": 28,
    "adventure": 12,
    "animation": 16,
    "comedy": 35,
    "crime": 80,
    "documentary": 99,
    "drama": 18,
    "family": 10751,
    "fantasy": 14,
    "history": 36,
    "horror": 27,
    "music": 10402,
    "mystery": 9648,
    "romance": 10749,
    "science fiction": 878,
    "tv movie": 10770,
    "thriller": 53,
    "war": 10752,
    "western": 37,
}

_GENRE_ALIASES: Final[dict[str, str]] = {
    "sci-fi": "science fiction",
    "sci fi": "science fiction",
    "science-fiction": "science fiction",
    "scifi": "science fiction",
}

_CAST_ROLES: Final = frozenset({"actor", "actress", "cast", "performer", "star"})
_DIRECTOR_ROLES: Final = frozenset({"director", "directing"})
_WRITER_ROLES: Final = frozenset({"writer", "screenwriter", "screenplay", "writing"})
_PRODUCER_ROLES: Final = frozenset({"producer", "production"})


class IntentIssue(BaseModel):
    """A deterministic merge problem that did not discard prior constraints."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1)
    field: str = Field(min_length=1)
    message: str = Field(min_length=1)


class IntentMergeResult(BaseModel):
    """A validated merged intent plus turn-specific exclusions and issues."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: MovieIntent
    turn_kind: TurnKind
    excluded_tmdb_ids: list[int] = Field(default_factory=list)
    validation_issues: list[IntentIssue] = Field(default_factory=list)
    clarification: str | None = None


class AssemblyIssue(BaseModel):
    """A bounded, public-safe note about candidate assembly."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1)
    field: str | None = None
    message: str = Field(min_length=1)
    tmdb_id: int | None = Field(default=None, gt=0)


class ProviderLookupOutcome(StrEnum):
    OK = "ok"
    NO_DATA = "no_data"
    ERROR = "error"


class ProviderLookup(BaseModel):
    """Observed watch-provider outcome for a recommended movie."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tmdb_id: int = Field(gt=0)
    country: str = Field(pattern=r"^[A-Z]{2}$")
    outcome: ProviderLookupOutcome
    provider_count: int = Field(ge=0)
    detail: str | None = None


class RecommendationAssembly(BaseModel):
    """Final typed recommendations plus bounded assembly observations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    batch: RecommendationBatch
    issues: list[AssemblyIssue] = Field(default_factory=list)
    provider_lookups: list[ProviderLookup] = Field(default_factory=list)


def merge_intent(
    prior: MovieIntent,
    patch: MovieIntentPatch,
    *,
    seen_tmdb_ids: Sequence[int] = (),
) -> IntentMergeResult:
    """Apply a patch without dropping omitted constraints.

    Only explicit patch fields replace prior values. ``another_set`` additionally
    excludes every supplied previously seen movie ID. An ordered year range that
    becomes invalid is reported and the prior range is retained.
    """

    issues: list[IntentIssue] = []
    genres = _merge_collection(prior.genres, patch.genres, "genres", issues)
    moods = _merge_collection(prior.moods, patch.moods, "moods", issues)
    clear_filters = set(patch.clear_filters)

    release_year_min = None if "release_year" in clear_filters else prior.release_year_min
    release_year_max = None if "release_year" in clear_filters else prior.release_year_max
    if patch.release_year_min is not None:
        release_year_min = patch.release_year_min
    if patch.release_year_max is not None:
        release_year_max = patch.release_year_max

    if (
        release_year_min is not None
        and release_year_max is not None
        and release_year_min > release_year_max
    ):
        issues.append(
            IntentIssue(
                code="invalid_year_range",
                field="release_year",
                message=(
                    "The patched release year range was invalid; the previous range was kept."
                ),
            )
        )
        release_year_min = prior.release_year_min
        release_year_max = prior.release_year_max

    intent = MovieIntent(
        genres=genres,
        people=(
            []
            if "people" in clear_filters
            else list(patch.people)
            if patch.people is not None
            else list(prior.people)
        ),
        moods=moods,
        release_year_min=release_year_min,
        release_year_max=release_year_max,
        original_language=(
            None
            if "original_language" in clear_filters
            else patch.original_language
            if patch.original_language is not None
            else prior.original_language
        ),
        runtime_max_minutes=(
            None
            if "runtime_max_minutes" in clear_filters
            else patch.runtime_max_minutes
            if patch.runtime_max_minutes is not None
            else prior.runtime_max_minutes
        ),
        tmdb_rating_min=(
            None
            if "tmdb_rating_min" in clear_filters
            else patch.tmdb_rating_min
            if patch.tmdb_rating_min is not None
            else prior.tmdb_rating_min
        ),
        country=patch.country if patch.country is not None else prior.country,
        requested_count=(
            patch.requested_count if patch.requested_count is not None else prior.requested_count
        ),
    )

    exclusions = (
        _unique_positive_ids(seen_tmdb_ids) if patch.turn_kind is TurnKind.ANOTHER_SET else []
    )
    return IntentMergeResult(
        intent=intent,
        turn_kind=patch.turn_kind,
        excluded_tmdb_ids=exclusions,
        validation_issues=issues,
        clarification=patch.clarification,
    )


async def assemble_recommendations(
    client: TmdbClient,
    intent: MovieIntent,
    *,
    excluded_tmdb_ids: Sequence[int] = (),
) -> RecommendationAssembly:
    """Assemble at most ``requested_count`` verified recommendations.

    Discovery uses only explicit validated intent filters. At most three pages
    are requested and at most ``requested_count * 3`` unique candidates are
    retained before details are fetched. Hard constraints are verified from
    TMDB details. Provider failures degrade to a caveat, while discovery errors
    propagate as ``TmdbError``.
    """

    issues: list[AssemblyIssue] = []
    exclusions = _unique_positive_ids(excluded_tmdb_ids)
    people_ids = await _resolve_people(client, intent.people, issues)
    filters = _build_discover_filters(intent, issues, people_ids=people_ids)
    candidates = await _discover_candidates(
        client,
        filters,
        requested_count=intent.requested_count,
        excluded_ids=exclusions,
    )

    recommendations: list[Recommendation] = []
    provider_lookups: list[ProviderLookup] = []
    failures: Counter[str] = Counter()
    accepted_details: list[TmdbMovieDetails] = []
    details_checked = 0
    next_candidate = 0

    async def fetch_details(
        candidate: TmdbMovieCandidate,
    ) -> tuple[TmdbMovieDetails | None, TmdbError | None]:
        try:
            return await client.get_movie_details(candidate.tmdb_id), None
        except TmdbError as exc:
            return None, exc

    while (
        next_candidate < len(candidates)
        and len(accepted_details) < intent.requested_count
        and details_checked < MAX_DETAILS_TO_VERIFY
    ):
        needed = intent.requested_count - len(accepted_details)
        batch_size = min(
            MAX_CONCURRENT_TMDB_REQUESTS,
            needed,
            MAX_DETAILS_TO_VERIFY - details_checked,
            len(candidates) - next_candidate,
        )
        detail_batch = candidates[next_candidate : next_candidate + batch_size]
        next_candidate += batch_size
        details_checked += batch_size

        fetched = await asyncio.gather(*(fetch_details(candidate) for candidate in detail_batch))
        for candidate, (details, error) in zip(detail_batch, fetched, strict=True):
            if error is not None:
                failures["details"] += 1
                issues.append(
                    AssemblyIssue(
                        code="details_unavailable",
                        field="details",
                        tmdb_id=candidate.tmdb_id,
                        message="TMDB details were unavailable for this candidate.",
                    )
                )
                continue

            assert details is not None
            if details.tmdb_id != candidate.tmdb_id:
                failures["details"] += 1
                issues.append(
                    AssemblyIssue(
                        code="details_mismatch",
                        field="details",
                        tmdb_id=candidate.tmdb_id,
                        message="TMDB returned details for a different movie ID.",
                    )
                )
                continue

            failed_constraints = _hard_constraint_failures(details, intent)
            if failed_constraints:
                failures.update(failed_constraints)
                issues.append(
                    AssemblyIssue(
                        code="candidate_rejected",
                        field="hard_constraints",
                        tmdb_id=candidate.tmdb_id,
                        message=(f"Candidate did not satisfy: {_human_join(failed_constraints)}."),
                    )
                )
                continue

            accepted_details.append(details)

    async def load_provider(
        details: TmdbMovieDetails,
    ) -> tuple[WatchOptions | None, ProviderLookup, list[str]]:
        return await _load_provider_data(
            client,
            tmdb_id=details.tmdb_id,
            country=intent.country,
        )

    provider_results = await _gather_bounded(accepted_details, load_provider)
    for rank, (details, provider_result) in enumerate(
        zip(accepted_details, provider_results, strict=True),
        start=1,
    ):
        watch_options, provider_lookup, provider_caveats = provider_result
        provider_lookups.append(provider_lookup)
        recommendations.append(
            _build_recommendation(
                rank=rank,
                details=details,
                intent=intent,
                watch_options=watch_options,
                provider_caveats=provider_caveats,
            )
        )

    shortfall_reason = _shortfall_reason(
        intent=intent,
        discovered=len(candidates),
        accepted=len(recommendations),
        failures=failures,
        excluded_count=len(exclusions),
    )
    batch = RecommendationBatch(
        recommendations=recommendations,
        requested_count=intent.requested_count,
        shortfall_reason=shortfall_reason,
    )
    return RecommendationAssembly(
        batch=batch,
        issues=issues,
        provider_lookups=provider_lookups,
    )


async def _gather_bounded[ItemT, ResultT](
    items: Sequence[ItemT],
    operation: Callable[[ItemT], Awaitable[ResultT]],
) -> list[ResultT]:
    """Run operations in fixed-size batches while preserving input order."""

    results: list[ResultT] = []
    for start in range(0, len(items), MAX_CONCURRENT_TMDB_REQUESTS):
        batch = items[start : start + MAX_CONCURRENT_TMDB_REQUESTS]
        results.extend(await asyncio.gather(*(operation(item) for item in batch)))
    return results


def _merge_collection(
    prior: Sequence[str],
    patch: StringCollectionPatch | None,
    field: str,
    issues: list[IntentIssue],
) -> list[str]:
    if patch is None:
        return list(prior)
    if patch.set is not None:
        return _normalize_collection(patch.set, field, issues)
    if patch.clear:
        return []

    merged = list(prior)
    if patch.add:
        existing = {_collection_key(value) for value in merged}
        for value in _normalize_collection(patch.add, field, issues):
            key = _collection_key(value)
            if key not in existing:
                existing.add(key)
                merged.append(value)
    if patch.remove:
        removals = {
            _collection_key(cleaned) for value in patch.remove if (cleaned := _clean_text(value))
        }
        merged = [value for value in merged if _collection_key(value) not in removals]
    return merged


def _normalize_collection(
    values: Sequence[str],
    field: str,
    issues: list[IntentIssue],
) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    skipped = False
    for raw in values:
        value = _clean_text(raw)
        if not value:
            skipped = True
            continue
        key = _collection_key(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    if skipped:
        issues.append(
            IntentIssue(
                code="empty_collection_value",
                field=field,
                message=f"Empty {field} values were ignored.",
            )
        )
    return result


def _collection_key(value: str) -> str:
    return _clean_text(value).casefold()


def _clean_text(value: str) -> str:
    return " ".join(value.split())


def _unique_positive_ids(values: Sequence[int]) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            continue
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _build_discover_filters(
    intent: MovieIntent,
    issues: list[AssemblyIssue],
    *,
    people_ids: Sequence[int] = (),
) -> TmdbDiscoverFilters:
    return TmdbDiscoverFilters(
        release_year_min=intent.release_year_min,
        release_year_max=intent.release_year_max,
        original_language=intent.original_language,
        runtime_max_minutes=intent.runtime_max_minutes,
        tmdb_rating_min=intent.tmdb_rating_min,
        genre_ids=_resolve_genre_ids(intent.genres, issues),
        people_ids=list(people_ids),
    )


async def _resolve_people(
    client: TmdbClient,
    people: Sequence[PersonConstraint],
    issues: list[AssemblyIssue],
) -> list[int]:
    async def resolve(
        person: PersonConstraint,
    ) -> tuple[int | None, AssemblyIssue | None]:
        try:
            page = await client.search_people(person.name)
        except TmdbError:
            return None, AssemblyIssue(
                code="person_lookup_failed",
                field="people",
                message=f"TMDB could not resolve '{_display(person.name)}'.",
            )

        exact = [
            result
            for result in page.results
            if _collection_key(result.name) == _collection_key(person.name)
        ]
        role = _collection_key(person.role or "")
        department = _role_department(role)
        if department is not None:
            role_matches = [
                result
                for result in exact
                if _collection_key(result.known_for_department or "") == department
            ]
            if role_matches:
                exact = role_matches

        ids = _unique_positive_ids([result.tmdb_id for result in exact])
        if len(ids) == 1:
            return ids[0], None

        code = "person_not_found" if not ids else "person_ambiguous"
        note = "could not be found" if not ids else "matched more than one TMDB person"
        return None, AssemblyIssue(
            code=code,
            field="people",
            message=f"'{_display(person.name)}' {note}; details will still be checked.",
        )

    outcomes = await _gather_bounded(people[:10], resolve)
    issues.extend(issue for _, issue in outcomes if issue is not None)
    return _unique_positive_ids([tmdb_id for tmdb_id, _ in outcomes if tmdb_id is not None])


def _role_department(role: str) -> str | None:
    if role in _CAST_ROLES:
        return "acting"
    if role in _DIRECTOR_ROLES:
        return "directing"
    if role in _WRITER_ROLES:
        return "writing"
    if role in _PRODUCER_ROLES:
        return "production"
    return None


def _resolve_genre_ids(genres: Sequence[str], issues: list[AssemblyIssue]) -> list[int]:
    resolved: list[int] = []
    unknown: set[str] = set()
    for raw in genres:
        cleaned = _clean_text(raw)
        if not cleaned:
            continue
        canonical = _canonical_genre(cleaned)
        genre_id = _GENRE_IDS.get(canonical)
        if genre_id is None:
            if canonical not in unknown:
                unknown.add(canonical)
                issues.append(
                    AssemblyIssue(
                        code="unsupported_genre",
                        field="genres",
                        message=(
                            f"TMDB has no stable genre ID for '{_display(cleaned)}'; "
                            "it will only be checked against returned details."
                        ),
                    )
                )
            continue
        if genre_id not in resolved:
            resolved.append(genre_id)

    if len(resolved) > MAX_GENRE_FILTERS:
        issues.append(
            AssemblyIssue(
                code="genre_filter_truncated",
                field="genres",
                message=(
                    f"Only the first {MAX_GENRE_FILTERS} genre filters are sent to TMDB; "
                    "all requested genres are still verified."
                ),
            )
        )
        resolved = resolved[:MAX_GENRE_FILTERS]
    return resolved


async def _discover_candidates(
    client: TmdbClient,
    filters: TmdbDiscoverFilters,
    *,
    requested_count: int,
    excluded_ids: Sequence[int],
) -> list[TmdbMovieCandidate]:
    cap = requested_count * CANDIDATES_PER_REQUESTED_MOVIE
    seen = set(excluded_ids)
    candidates: list[TmdbMovieCandidate] = []
    page_number = 1

    while page_number <= MAX_DISCOVERY_PAGES and len(candidates) < cap:
        page = await client.discover_movies(filters, page=page_number)
        for candidate in page.results:
            if candidate.tmdb_id in seen:
                continue
            seen.add(candidate.tmdb_id)
            candidates.append(candidate)
            if len(candidates) >= cap:
                break
        if page.total_pages == 0 or page_number >= page.total_pages:
            break
        page_number += 1

    return candidates


def _hard_constraint_failures(details: TmdbMovieDetails, intent: MovieIntent) -> list[str]:
    failures: list[str] = []

    if intent.release_year_min is not None and (
        details.release_year is None or details.release_year < intent.release_year_min
    ):
        failures.append("year")
    if intent.release_year_max is not None and (
        details.release_year is None or details.release_year > intent.release_year_max
    ):
        failures.append("year")
    if intent.genres and not _matches_genres(intent.genres, details):
        failures.append("genres")
    if intent.runtime_max_minutes is not None and (
        details.runtime_minutes is None or details.runtime_minutes > intent.runtime_max_minutes
    ):
        failures.append("runtime")
    if intent.tmdb_rating_min is not None and (
        details.tmdb_rating is None or details.tmdb_rating < intent.tmdb_rating_min
    ):
        failures.append("rating")
    if intent.people and not _matches_people(intent.people, details):
        failures.append("people")

    return list(dict.fromkeys(failures))


def _matches_genres(requested: Sequence[str], details: TmdbMovieDetails) -> bool:
    available = {_canonical_genre(value) for value in details.genres}
    return all(_canonical_genre(value) in available for value in requested)


def _matches_people(people: Sequence[PersonConstraint], details: TmdbMovieDetails) -> bool:
    return all(_person_matches(person, details) for person in people)


def _person_matches(person: PersonConstraint, details: TmdbMovieDetails) -> bool:
    name = _collection_key(person.name)
    if person.role is None:
        return any(_collection_key(member.name) == name for member in details.cast) or any(
            _collection_key(member.name) == name for member in details.crew
        )

    role = _collection_key(person.role)
    if role in _CAST_ROLES:
        return any(_collection_key(member.name) == name for member in details.cast)
    if role in _DIRECTOR_ROLES:
        return any(
            _collection_key(member.name) == name and _collection_key(member.job or "") == "director"
            for member in details.crew
        )
    if role in _WRITER_ROLES:
        return any(
            _collection_key(member.name) == name
            and (
                "writ" in _collection_key(member.job or "")
                or "screenplay" in _collection_key(member.job or "")
            )
            for member in details.crew
        )
    if role in _PRODUCER_ROLES:
        return any(
            _collection_key(member.name) == name and "producer" in _collection_key(member.job or "")
            for member in details.crew
        )
    return any(
        _collection_key(member.name) == name
        and (
            role in _collection_key(member.job or "")
            or role in _collection_key(member.department or "")
        )
        for member in details.crew
    )


async def _load_provider_data(
    client: TmdbClient,
    *,
    tmdb_id: int,
    country: str,
) -> tuple[WatchOptions | None, ProviderLookup, list[str]]:
    try:
        raw = await client.get_watch_providers(tmdb_id, country)
    except TmdbError:
        return (
            None,
            ProviderLookup(
                tmdb_id=tmdb_id,
                country=country,
                outcome=ProviderLookupOutcome.ERROR,
                provider_count=0,
                detail="Watch-provider lookup failed.",
            ),
            ["Watch-provider lookup failed; availability is unknown."],
        )

    if raw.status is TmdbWatchLookupStatus.NO_DATA:
        return (
            None,
            ProviderLookup(
                tmdb_id=tmdb_id,
                country=country,
                outcome=ProviderLookupOutcome.NO_DATA,
                provider_count=0,
                detail="TMDB returned no watch-provider data.",
            ),
            [f"TMDB returned no watch-provider data for {country}."],
        )

    options = _watch_options(raw)
    count = _unique_provider_count(raw)
    caveats = [f"TMDB lists no watch providers for {country}."] if count == 0 else []
    return (
        options,
        ProviderLookup(
            tmdb_id=tmdb_id,
            country=country,
            outcome=ProviderLookupOutcome.OK,
            provider_count=count,
        ),
        caveats,
    )


def _watch_options(raw: TmdbWatchOptions) -> WatchOptions:
    return WatchOptions(
        country=raw.country,
        subscription=_map_providers(raw.subscription),
        free=_map_providers(raw.free),
        ads=_map_providers(raw.ads),
        rent=_map_providers(raw.rent),
        buy=_map_providers(raw.buy),
    )


def _map_providers(providers: Sequence[TmdbWatchProvider]) -> list[WatchProvider]:
    result: list[WatchProvider] = []
    seen: set[int] = set()
    for provider in providers:
        if provider.provider_id in seen:
            continue
        seen.add(provider.provider_id)
        result.append(
            WatchProvider(
                provider_id=provider.provider_id,
                name=provider.name,
                logo_path=provider.logo_path,
            )
        )
        if len(result) >= MAX_PROVIDERS_PER_GROUP:
            break
    return result


def _unique_provider_count(raw: TmdbWatchOptions) -> int:
    return len(
        {
            provider.provider_id
            for providers in (
                raw.subscription,
                raw.free,
                raw.ads,
                raw.rent,
                raw.buy,
            )
            for provider in providers
        }
    )


def _build_recommendation(
    *,
    rank: int,
    details: TmdbMovieDetails,
    intent: MovieIntent,
    watch_options: WatchOptions | None,
    provider_caveats: Sequence[str],
) -> Recommendation:
    caveats = list(provider_caveats)
    if intent.moods:
        caveats.append("Mood preferences are not verified by TMDB metadata.")

    return Recommendation(
        rank=rank,
        movie=_movie_details(details, watch_options),
        match_reason=_match_reason(details, intent),
        matched_constraints=_matched_constraints(details, intent)[:MAX_MATCHED_CONSTRAINTS],
        caveats=_dedupe_texts(caveats)[:MAX_CAVEATS],
    )


def _movie_details(
    details: TmdbMovieDetails,
    watch_options: WatchOptions | None,
) -> MovieDetails:
    return MovieDetails(
        tmdb_id=details.tmdb_id,
        title=details.title,
        release_year=details.release_year,
        runtime_minutes=details.runtime_minutes,
        genres=list(details.genres),
        overview=_truncate(details.overview.strip(), MAX_OVERVIEW_CHARS),
        poster_url=_image_url(details.poster_path),
        tmdb_url=HttpUrl(f"{TMDB_WEB_BASE}/{details.tmdb_id}"),
        imdb_url=_imdb_url(details.imdb_id),
        ratings=[
            Rating(
                source=RatingSource.TMDB,
                value=details.tmdb_rating,
                scale="10",
                vote_count=details.vote_count,
            )
        ],
        watch_options=watch_options,
    )


def _match_reason(details: TmdbMovieDetails, intent: MovieIntent) -> str:
    available = {_canonical_genre(value) for value in details.genres}
    parts: list[str] = []

    matched_genres = [genre for genre in intent.genres if _canonical_genre(genre) in available]
    if matched_genres:
        parts.append(f"TMDB genre match: {_display_join(matched_genres)}.")

    if (
        intent.release_year_min is not None or intent.release_year_max is not None
    ) and details.release_year is not None:
        parts.append(f"Released in {details.release_year}.")

    if intent.runtime_max_minutes is not None and details.runtime_minutes is not None:
        parts.append(
            f"Runtime {details.runtime_minutes} minutes is within the "
            f"{intent.runtime_max_minutes}-minute limit."
        )
    if intent.tmdb_rating_min is not None and details.tmdb_rating is not None:
        parts.append(
            f"TMDB rating {details.tmdb_rating:.1f} meets the "
            f"{intent.tmdb_rating_min:g}/10 minimum."
        )

    matched_people = [person.name for person in intent.people if _person_matches(person, details)]
    if matched_people:
        parts.append(f"Includes {_display_join(matched_people)} as requested.")

    if not parts:
        if details.genres:
            parts.append(f"TMDB lists {_display_join(details.genres[:3])}.")
        else:
            parts.append("TMDB details match the active request.")

    return _truncate(" ".join(parts), MAX_REASON_CHARS)


def _matched_constraints(details: TmdbMovieDetails, intent: MovieIntent) -> list[str]:
    constraints: list[str] = []
    available = {_canonical_genre(value) for value in details.genres}

    for genre in intent.genres:
        if _canonical_genre(genre) in available:
            constraints.append(f"genre:{_display(genre)}")

    if (
        intent.release_year_min is not None
        and intent.release_year_max is not None
        and details.release_year is not None
    ):
        constraints.append(f"year:{intent.release_year_min}-{intent.release_year_max}")
    elif intent.release_year_min is not None and details.release_year is not None:
        constraints.append(f"year>={intent.release_year_min}")
    elif intent.release_year_max is not None and details.release_year is not None:
        constraints.append(f"year<={intent.release_year_max}")

    if intent.runtime_max_minutes is not None and details.runtime_minutes is not None:
        constraints.append(f"runtime<={intent.runtime_max_minutes}")
    if intent.tmdb_rating_min is not None and details.tmdb_rating is not None:
        constraints.append(f"tmdb_rating>={intent.tmdb_rating_min:g}")

    for person in intent.people:
        if _person_matches(person, details):
            suffix = f" ({_display(person.role)})" if person.role else ""
            constraints.append(f"person:{_display(person.name)}{suffix}")

    return constraints


def _shortfall_reason(
    *,
    intent: MovieIntent,
    discovered: int,
    accepted: int,
    failures: Counter[str],
    excluded_count: int,
) -> str | None:
    if accepted >= intent.requested_count:
        return None

    excluded_note = " Previously shown movies were excluded." if excluded_count else ""
    if discovered == 0:
        return f"No TMDB movies matched the explicit filters.{excluded_note}"
    if accepted == 0:
        labels = _constraint_labels(failures)
        if labels:
            return (
                "TMDB returned candidates, but none satisfied the requested "
                f"{_human_join(labels)} constraints.{excluded_note}"
            )
        if failures["details"]:
            return (
                "TMDB returned candidates, but their details were unavailable for "
                f"verification.{excluded_note}"
            )
        return f"TMDB returned candidates, but none could be used.{excluded_note}"
    if accepted == discovered:
        movie_word = "movie" if accepted == 1 else "movies"
        return (
            f"TMDB supplied only {accepted} eligible {movie_word} for the explicit "
            f"filters.{excluded_note}"
        )

    labels = _constraint_labels(failures)
    if labels:
        return (
            f"Only {accepted} of {intent.requested_count} requested movies met the "
            f"requested {_human_join(labels)} constraints after checking TMDB "
            f"details.{excluded_note}"
        )
    return (
        f"Only {accepted} of {intent.requested_count} requested movies remained after "
        f"checking TMDB details.{excluded_note}"
    )


def _constraint_labels(failures: Counter[str]) -> list[str]:
    labels: list[str] = []
    if failures["year"]:
        labels.append("year")
    if failures["genres"]:
        labels.append("genre")
    if failures["runtime"]:
        labels.append("runtime")
    if failures["rating"]:
        labels.append("TMDB rating")
    if failures["people"]:
        labels.append("people")
    return labels


def _canonical_genre(value: str) -> str:
    normalized = _collection_key(value)
    return _GENRE_ALIASES.get(normalized, normalized)


def _image_url(path: str | None) -> HttpUrl | None:
    if path is None:
        return None
    cleaned = path.strip()
    if not cleaned.startswith("/") or len(cleaned) > 300:
        return None
    try:
        return HttpUrl(f"{TMDB_IMAGE_BASE}{cleaned}")
    except ValidationError:
        return None


def _imdb_url(imdb_id: str | None) -> HttpUrl | None:
    if imdb_id is None or _IMDB_ID_PATTERN.fullmatch(imdb_id) is None:
        return None
    return HttpUrl(f"{IMDB_WEB_BASE}/{imdb_id}/")


def _display(value: str | None, limit: int = 60) -> str:
    if value is None:
        return ""
    return _truncate(_clean_text(value), limit)


def _display_join(values: Sequence[str], limit: int = 80) -> str:
    return ", ".join(_display(value, limit) for value in values)


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def _dedupe_texts(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _human_join(values: Sequence[str]) -> str:
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    return f"{', '.join(values[:-1])} or {values[-1]}"
