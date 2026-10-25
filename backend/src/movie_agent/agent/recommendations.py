"""Deterministic intent merging and bounded TMDB recommendation assembly.

This module contains no model inference. It applies typed intent patches,
queries only explicit validated constraints, verifies hard constraints against
TMDB details, and builds recommendations exclusively from observed facts.
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from difflib import SequenceMatcher
from enum import StrEnum
from typing import Any, Final, Protocol

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError

from movie_agent.models.clarification import (
    PendingClarification,
    StoredClarificationOption,
)
from movie_agent.models.intent import (
    MovieIntent,
    MovieIntentPatch,
    PersonConstraint,
    StringCollectionPatch,
    TurnKind,
)
from movie_agent.models.movies import (
    Alternative,
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
    TmdbPerson,
    TmdbWatchLookupStatus,
    TmdbWatchOptions,
    TmdbWatchProvider,
)
from movie_agent.services.omdb import OmdbLookupStatus, OmdbRatings
from movie_agent.services.tmdb import TmdbClient, TmdbError

MAX_DISCOVERY_PAGES: Final = 3
CANDIDATES_PER_REQUESTED_MOVIE: Final = 3
MAX_DETAILS_TO_VERIFY: Final = 20
MAX_CONCURRENT_TMDB_REQUESTS: Final = 4
MAX_ALTERNATIVES: Final = 3
MAX_ALTERNATIVE_DISCOVERY_CALLS: Final = 2
MAX_ALTERNATIVE_DETAILS: Final = 6
MAX_ALTERNATIVE_SECONDS: Final = 15.0
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
_MOOD_LIKE_TERMS: Final = frozenset(
    {
        "bittersweet",
        "chilling",
        "cozy",
        "dark",
        "feel good",
        "feel-good",
        "heartwarming",
        "hopeful",
        "inspiring",
        "lighthearted",
        "melancholic",
        "suspense",
        "suspenseful",
        "tense",
        "thought-provoking",
        "uplifting",
        "whimsical",
    }
)


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


class ResolvedPerson(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tmdb_id: int = Field(gt=0)
    name: str = Field(min_length=1)
    role: str | None = Field(default=None, max_length=60)
    original_name: str | None = Field(default=None, max_length=120)
    original_role: str | None = Field(default=None, max_length=60)


class PeopleResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    resolved: list[ResolvedPerson] = Field(default_factory=list)
    unresolved: list[PersonConstraint] = Field(default_factory=list)
    clarification: PendingClarification | None = None


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
    clarification: PendingClarification | None = None


class ExternalRatingsClient(Protocol):
    async def get_ratings_for_ids(self, imdb_ids: Sequence[str]) -> list[OmdbRatings]: ...


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
    genres, moods, unsupported_genres = _separate_unsupported_genres(genres, moods, issues)
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
    clarification = patch.clarification
    if unsupported_genres:
        names = ", ".join(f"'{_display(value)}'" for value in unsupported_genres[:3])
        clarification = (
            f"TMDB doesn't list {names} as a genre. Did you mean a mood, or another genre?"
        )[:240]
    return IntentMergeResult(
        intent=intent,
        turn_kind=patch.turn_kind,
        excluded_tmdb_ids=exclusions,
        validation_issues=issues,
        clarification=clarification,
    )


async def assemble_recommendations(
    client: TmdbClient,
    intent: MovieIntent,
    *,
    excluded_tmdb_ids: Sequence[int] = (),
    omdb: ExternalRatingsClient | None = None,
    similar_to_tmdb_id: int | None = None,
    similar_to_title: str | None = None,
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
    people_resolution = await _resolve_people(client, intent.people, issues)
    if people_resolution.clarification is not None:
        return RecommendationAssembly(
            batch=RecommendationBatch(
                recommendations=[],
                requested_count=intent.requested_count,
            ),
            issues=issues,
            clarification=people_resolution.clarification,
        )
    filters = _build_discover_filters(
        intent,
        issues,
        people=people_resolution.resolved,
    )
    candidates = await _discover_candidates(
        client,
        filters,
        requested_count=intent.requested_count,
        excluded_ids=exclusions,
        similar_to_tmdb_id=similar_to_tmdb_id,
    )

    recommendations: list[Recommendation] = []
    provider_lookups: list[ProviderLookup] = []
    failures: Counter[str] = Counter()
    accepted_details: list[TmdbMovieDetails] = []
    rejected_details: list[tuple[TmdbMovieDetails, list[str]]] = []
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

            failed_constraints = _hard_constraint_failures(
                details,
                intent,
                resolved_people=people_resolution.resolved,
            )
            if failed_constraints:
                failures.update(failed_constraints)
                rejected_details.append(
                    (
                        details,
                        _unmet_filter_labels(
                            details,
                            intent,
                            resolved_people=people_resolution.resolved,
                        ),
                    )
                )
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
                similar_to_title=similar_to_title,
            )
        )

    shortfall_reason = _shortfall_reason(
        intent=intent,
        discovered=len(candidates),
        accepted=len(recommendations),
        failures=failures,
        excluded_count=len(exclusions),
    )
    alternatives: list[Alternative] = []
    if len(recommendations) < intent.requested_count:
        alternatives, alternative_lookups = await _find_alternatives(
            client,
            intent,
            excluded_ids=exclusions,
            known_ids=[candidate.tmdb_id for candidate in candidates],
            checked_details=rejected_details,
        )
        provider_lookups.extend(alternative_lookups)
    batch = RecommendationBatch(
        recommendations=recommendations,
        requested_count=intent.requested_count,
        shortfall_reason=shortfall_reason,
        alternatives=alternatives,
    )
    if omdb is not None:
        batch = await _enrich_external_ratings(batch, omdb)
    return RecommendationAssembly(
        batch=batch,
        issues=issues,
        provider_lookups=provider_lookups,
    )


async def _enrich_external_ratings(
    batch: RecommendationBatch,
    client: ExternalRatingsClient,
) -> RecommendationBatch:
    imdb_ids_by_movie: dict[int, str] = {}
    for recommendation in batch.recommendations:
        imdb_url = recommendation.movie.imdb_url
        match = _IMDB_ID_PATTERN.search(str(imdb_url)) if imdb_url is not None else None
        if match is not None:
            imdb_ids_by_movie[recommendation.movie.tmdb_id] = match.group(0)

    ordered_ids = list(dict.fromkeys(imdb_ids_by_movie.values()))[:10]
    if not ordered_ids:
        return batch

    try:
        lookups = await client.get_ratings_for_ids(ordered_ids)
    except Exception:
        # OMDb is optional enrichment; it must never fail a grounded TMDB batch.
        return batch
    by_imdb_id = {result.imdb_id: result for result in lookups}

    enriched: list[Recommendation] = []
    for recommendation in batch.recommendations:
        imdb_id = imdb_ids_by_movie.get(recommendation.movie.tmdb_id)
        lookup = by_imdb_id.get(imdb_id) if imdb_id is not None else None
        ratings = [
            rating for rating in recommendation.movie.ratings if rating.source is RatingSource.TMDB
        ]
        if lookup is not None and lookup.status is OmdbLookupStatus.OK:
            if lookup.imdb is not None:
                ratings.append(lookup.imdb)
            if lookup.rotten_tomatoes is not None:
                ratings.append(lookup.rotten_tomatoes)
        enriched.append(
            recommendation.model_copy(
                update={"movie": recommendation.movie.model_copy(update={"ratings": ratings})}
            )
        )
    return batch.model_copy(update={"recommendations": enriched})


async def refresh_recommendation_availability(
    client: TmdbClient,
    batch: RecommendationBatch,
    *,
    country: str,
) -> RecommendationAssembly:
    """Refresh only watch-provider data for existing grounded recommendations.

    This deliberately does not discover titles, fetch movie details, or invoke an
    LLM. Recommendation order, facts, match reasons, and shortfall are preserved.
    """

    normalized_country = country.strip().upper()
    if re.fullmatch(r"[A-Z]{2}", normalized_country) is None:
        raise ValueError("country must be an ISO 3166-1 alpha-2 code")

    async def load_provider(
        recommendation: Recommendation,
    ) -> tuple[WatchOptions | None, ProviderLookup, list[str]]:
        return await _load_provider_data(
            client,
            tmdb_id=recommendation.movie.tmdb_id,
            country=normalized_country,
        )

    alternative_items = [item.recommendation for item in batch.alternatives]
    all_recommendations = [*batch.recommendations, *alternative_items]
    provider_results = await _gather_bounded(all_recommendations, load_provider)

    def refreshed_recommendation(
        recommendation: Recommendation,
        provider_result: tuple[WatchOptions | None, ProviderLookup, list[str]],
    ) -> Recommendation:
        watch_options, lookup, provider_caveats = provider_result
        provider_lookups.append(lookup)
        retained_caveats = [
            caveat for caveat in recommendation.caveats if not _is_provider_caveat(caveat)
        ]
        caveats = _dedupe_texts([*retained_caveats, *provider_caveats])[:MAX_CAVEATS]
        return recommendation.model_copy(
            update={
                "movie": recommendation.movie.model_copy(update={"watch_options": watch_options}),
                "caveats": caveats,
            }
        )

    recommendations: list[Recommendation] = []
    provider_lookups: list[ProviderLookup] = []
    for index, recommendation in enumerate(batch.recommendations):
        recommendations.append(refreshed_recommendation(recommendation, provider_results[index]))
    alternatives = [
        Alternative(
            recommendation=refreshed_recommendation(
                item.recommendation,
                provider_results[len(batch.recommendations) + index],
            ),
            unmet_filters=item.unmet_filters,
        )
        for index, item in enumerate(batch.alternatives)
    ]

    refreshed = RecommendationBatch(
        recommendations=recommendations,
        requested_count=batch.requested_count,
        shortfall_reason=batch.shortfall_reason,
        alternatives=alternatives,
    )
    return RecommendationAssembly(batch=refreshed, provider_lookups=provider_lookups)


def _is_provider_caveat(caveat: str) -> bool:
    normalized = caveat.casefold()
    return (
        "watch-provider" in normalized
        or "watch providers" in normalized
        or "provider data" in normalized
        or "availability is unknown" in normalized
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


def _separate_unsupported_genres(
    genres: Sequence[str],
    moods: Sequence[str],
    issues: list[IntentIssue],
) -> tuple[list[str], list[str], list[str]]:
    """Move known mood terms out of genres; leave other unknown labels for clarification."""

    supported: list[str] = []
    moved_moods: list[str] = []
    unsupported: list[str] = []
    for genre in genres:
        if _canonical_genre(genre) in _GENRE_IDS:
            supported.append(genre)
        elif _collection_key(genre) in _MOOD_LIKE_TERMS:
            moved_moods.append(genre)
        else:
            unsupported.append(genre)

    if not moved_moods:
        return supported + unsupported, list(moods), unsupported

    existing = {_collection_key(value) for value in moods}
    resolved_moods = list(moods)
    for value in moved_moods:
        key = _collection_key(value)
        if key not in existing:
            existing.add(key)
            resolved_moods.append(value)
    issues.append(
        IntentIssue(
            code="mood_moved_from_genre",
            field="genres",
            message=(
                "One or more genre labels have no TMDB genre; they were kept as soft "
                "mood preferences."
            ),
        )
    )
    return supported + unsupported, resolved_moods, unsupported


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
    people: Sequence[ResolvedPerson] = (),
) -> TmdbDiscoverFilters:
    cast_ids: list[int] = []
    crew_ids: list[int] = []
    for person in people:
        role = _collection_key(person.role or "")
        if role in _DIRECTOR_ROLES or role in _WRITER_ROLES or role in _PRODUCER_ROLES:
            if person.tmdb_id not in crew_ids:
                crew_ids.append(person.tmdb_id)
        elif person.tmdb_id not in cast_ids:
            cast_ids.append(person.tmdb_id)
    return TmdbDiscoverFilters(
        release_year_min=intent.release_year_min,
        release_year_max=intent.release_year_max,
        original_language=intent.original_language,
        runtime_max_minutes=intent.runtime_max_minutes,
        tmdb_rating_min=intent.tmdb_rating_min,
        genre_ids=_resolve_genre_ids(intent.genres, issues),
        people_ids=cast_ids,
        crew_ids=crew_ids,
    )


async def _resolve_people(
    client: TmdbClient,
    people: Sequence[PersonConstraint],
    issues: list[AssemblyIssue],
) -> PeopleResolution:
    async def resolve(
        person: PersonConstraint,
    ) -> tuple[ResolvedPerson | None, PendingClarification | None, AssemblyIssue | None]:
        if person.tmdb_id is not None:
            return (
                ResolvedPerson(
                    tmdb_id=person.tmdb_id,
                    name=person.name,
                    role=person.role,
                    original_name=person.name,
                    original_role=person.role,
                ),
                None,
                None,
            )
        try:
            page = await client.search_people(person.name)
        except TmdbError:
            return (
                None,
                None,
                AssemblyIssue(
                    code="person_lookup_failed",
                    field="people",
                    message=f"TMDB could not look up '{_display(person.name)}'.",
                ),
            )

        chosen = _select_person(person, page.results)
        if chosen is not None:
            return (
                ResolvedPerson(
                    tmdb_id=chosen.tmdb_id,
                    name=chosen.name,
                    role=person.role,
                    original_name=person.name,
                    original_role=person.role,
                ),
                None,
                None,
            )

        options = _person_options(person, page.results)
        if options:
            clarification = PendingClarification(
                text=_person_choice_prompt(person, options),
                options=options,
            )
            issue = AssemblyIssue(
                code="person_ambiguous",
                field="people",
                message=(
                    f"'{_display(person.name)}' matched more than one TMDB person; "
                    "the user must choose a grounded identity."
                ),
            )
        else:
            clarification = PendingClarification(
                text=(
                    f"I couldn't ground '{_display(person.name)}' as a TMDB person. "
                    "Check the spelling or choose a different name."
                )[:240],
            )
            issue = AssemblyIssue(
                code="person_not_found",
                field="people",
                message=f"'{_display(person.name)}' could not be found in TMDB.",
            )
        return None, clarification, issue

    outcomes = await _gather_bounded(people[:10], resolve)
    issues.extend(outcome[2] for outcome in outcomes if outcome[2] is not None)
    clarification = next(
        (outcome[1] for outcome in outcomes if outcome[1] is not None),
        None,
    )
    if clarification is not None:
        return PeopleResolution(clarification=clarification)
    return PeopleResolution(
        resolved=[outcome[0] for outcome in outcomes if outcome[0] is not None],
        unresolved=[people[index] for index, outcome in enumerate(outcomes) if outcome[0] is None],
    )


def _select_person(person: PersonConstraint, results: Sequence[TmdbPerson]) -> TmdbPerson | None:
    candidates = list(results)
    if not candidates:
        return None
    wanted = _person_key(person.name)
    exact = [result for result in candidates if _person_key(result.name) == wanted]
    role_department = _role_department(_collection_key(person.role or ""))
    if role_department is not None and exact:
        role_matches = [
            result
            for result in exact
            if _collection_key(result.known_for_department or "") == role_department
        ]
        if len(role_matches) == 1:
            return role_matches[0]
        if role_matches:
            exact = role_matches
        elif any(result.known_for_department for result in exact):
            return None

    if len(exact) == 1:
        return exact[0]
    if exact:
        return None

    close = sorted(
        candidates,
        key=lambda result: SequenceMatcher(None, wanted, _person_key(result.name)).ratio(),
        reverse=True,
    )
    if close:
        best = SequenceMatcher(None, wanted, _person_key(close[0].name)).ratio()
        second = (
            SequenceMatcher(None, wanted, _person_key(close[1].name)).ratio()
            if len(close) > 1
            else 0.0
        )
        role_match_supported = (
            role_department is None
            or not close[0].known_for_department
            or _collection_key(close[0].known_for_department) == role_department
        )
        if best >= 0.88 and best - second >= 0.04 and role_match_supported:
            return close[0]
    return None


def _person_options(
    person: PersonConstraint,
    results: Sequence[TmdbPerson],
) -> list[StoredClarificationOption]:
    wanted = _person_key(person.name)
    candidates = list(results)
    exact = [result for result in candidates if _person_key(result.name) == wanted]
    pool = exact or candidates
    options: list[StoredClarificationOption] = []
    seen: set[int] = set()
    for result in pool:
        if result.tmdb_id in seen:
            continue
        seen.add(result.tmdb_id)
        known_for = ", ".join(result.known_for_titles[:2])
        department = result.known_for_department or "TMDB person"
        label = result.name
        if known_for:
            label = f"{label} - known for {known_for}"
        detail = department + (f"; known for {known_for}" if known_for else "")
        options.append(
            StoredClarificationOption(
                choice_id=f"person:{result.tmdb_id}",
                kind="person",
                label=_truncate(label, 120),
                detail=_truncate(detail, 240),
                tmdb_id=result.tmdb_id,
                person_name=result.name,
                person_role=person.role,
                replace_name=person.name,
                replace_role=person.role,
            )
        )
        if len(options) >= 4:
            break
    return options if len(options) >= 2 else []


def _person_choice_prompt(
    person: PersonConstraint,
    options: Sequence[StoredClarificationOption],
) -> str:
    labels = "; ".join(option.label for option in options)
    return _truncate(
        f"Which '{_display(person.name)}' did you mean? Choose one: {labels}.",
        240,
    )


def _person_key(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_text = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", ascii_text.casefold()))


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
    similar_to_tmdb_id: int | None = None,
) -> list[TmdbMovieCandidate]:
    cap = requested_count * CANDIDATES_PER_REQUESTED_MOVIE
    seen = set(excluded_ids)
    candidates: list[TmdbMovieCandidate] = []
    if similar_to_tmdb_id is not None:
        seen.add(similar_to_tmdb_id)
        page = await client.get_similar_movies(similar_to_tmdb_id, page=1)
        for candidate in page.results:
            if candidate.tmdb_id in seen:
                continue
            seen.add(candidate.tmdb_id)
            candidates.append(candidate)
            if len(candidates) >= min(cap, MAX_DETAILS_TO_VERIFY):
                break
        return candidates

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


async def _find_alternatives(
    client: TmdbClient,
    intent: MovieIntent,
    *,
    excluded_ids: Sequence[int],
    known_ids: Sequence[int] = (),
    checked_details: Sequence[tuple[TmdbMovieDetails, list[str]]] = (),
) -> tuple[list[Alternative], list[ProviderLookup]]:
    """Find at most three grounded nearby matches inside explicit call/time caps."""

    try:
        async with asyncio.timeout(MAX_ALTERNATIVE_SECONDS):
            return await _collect_alternatives(
                client,
                intent,
                excluded_ids=excluded_ids,
                known_ids=known_ids,
                checked_details=checked_details,
            )
    except TimeoutError:
        return [], []


async def _collect_alternatives(
    client: TmdbClient,
    intent: MovieIntent,
    *,
    excluded_ids: Sequence[int],
    known_ids: Sequence[int] = (),
    checked_details: Sequence[tuple[TmdbMovieDetails, list[str]]] = (),
) -> tuple[list[Alternative], list[ProviderLookup]]:
    seen = {*excluded_ids, *known_ids}
    checked: list[tuple[TmdbMovieDetails, list[str]]] = list(checked_details)
    details_checked = 0
    discovery_calls = 0

    for relaxed in _relaxation_plans(intent):
        if len(checked) >= MAX_ALTERNATIVES:
            break
        if discovery_calls >= MAX_ALTERNATIVE_DISCOVERY_CALLS:
            break
        if details_checked >= MAX_ALTERNATIVE_DETAILS:
            break
        filters = _build_discover_filters(relaxed, [])
        discovery_calls += 1
        try:
            page = await client.discover_movies(filters, page=1)
        except (TmdbError, ValueError):
            continue
        candidates = [candidate for candidate in page.results if candidate.tmdb_id not in seen]
        for candidate in candidates:
            if details_checked >= MAX_ALTERNATIVE_DETAILS:
                break
            seen.add(candidate.tmdb_id)
            details_checked += 1
            try:
                details = await client.get_movie_details(candidate.tmdb_id)
            except TmdbError:
                continue
            if details.tmdb_id != candidate.tmdb_id:
                continue
            unmet = _unmet_filter_labels(details, intent)
            if unmet:
                checked.append((details, unmet))

    checked.sort(key=lambda item: len(item[1]))
    selected = checked[:MAX_ALTERNATIVES]
    if not selected:
        return [], []

    async def load_provider(
        details: TmdbMovieDetails,
    ) -> tuple[WatchOptions | None, ProviderLookup, list[str]]:
        return await _load_provider_data(
            client,
            tmdb_id=details.tmdb_id,
            country=intent.country,
        )

    provider_results = await _gather_bounded([details for details, _ in selected], load_provider)
    alternatives: list[Alternative] = []
    provider_lookups: list[ProviderLookup] = []
    for rank, ((details, unmet), provider_result) in enumerate(
        zip(selected, provider_results, strict=True),
        start=1,
    ):
        watch_options, provider_lookup, provider_caveats = provider_result
        provider_lookups.append(provider_lookup)
        alternatives.append(
            Alternative(
                recommendation=_build_recommendation(
                    rank=rank,
                    details=details,
                    intent=intent,
                    watch_options=watch_options,
                    provider_caveats=provider_caveats,
                ),
                unmet_filters=unmet,
            )
        )
    return alternatives, provider_lookups


def _relaxation_plans(intent: MovieIntent) -> list[MovieIntent]:
    fields: list[str] = []
    if intent.people:
        fields.append("people")
    if intent.genres:
        fields.append("genres")
    if intent.release_year_min is not None or intent.release_year_max is not None:
        fields.append("release_year")
    if intent.runtime_max_minutes is not None:
        fields.append("runtime_max_minutes")
    if intent.tmdb_rating_min is not None:
        fields.append("tmdb_rating_min")
    if intent.original_language is not None:
        fields.append("original_language")

    removals: set[str] = set()
    plans: list[MovieIntent] = []
    for field in fields[:MAX_ALTERNATIVE_DISCOVERY_CALLS]:
        removals.add(field)
        updates: dict[str, Any] = {}
        if "people" in removals:
            updates["people"] = []
        if "genres" in removals:
            updates["genres"] = []
        if "release_year" in removals:
            updates["release_year_min"] = None
            updates["release_year_max"] = None
        if "runtime_max_minutes" in removals:
            updates["runtime_max_minutes"] = None
        if "tmdb_rating_min" in removals:
            updates["tmdb_rating_min"] = None
        if "original_language" in removals:
            updates["original_language"] = None
        plans.append(intent.model_copy(update=updates))
    return plans


def _unmet_filter_labels(
    details: TmdbMovieDetails,
    intent: MovieIntent,
    *,
    resolved_people: Sequence[ResolvedPerson] = (),
) -> list[str]:
    labels: list[str] = []
    for person in intent.people:
        if not _person_matches(
            person,
            details,
            resolved=_resolved_for(person, resolved_people),
        ):
            suffix = f" ({_display(person.role)})" if person.role else ""
            labels.append(f"person: {_display(person.name)}{suffix}")

    available = {_canonical_genre(value) for value in details.genres}
    for genre in intent.genres:
        canonical = _canonical_genre(genre)
        if canonical in _GENRE_IDS and canonical not in available:
            labels.append(f"genre: {_display(genre)}")

    if (
        intent.release_year_min is not None
        and (details.release_year is None or details.release_year < intent.release_year_min)
    ) or (
        intent.release_year_max is not None
        and (details.release_year is None or details.release_year > intent.release_year_max)
    ):
        labels.append(_year_label(intent))

    if intent.runtime_max_minutes is not None and (
        details.runtime_minutes is None or details.runtime_minutes > intent.runtime_max_minutes
    ):
        labels.append(f"runtime: <= {intent.runtime_max_minutes} minutes")

    if intent.tmdb_rating_min is not None and (
        details.tmdb_rating is None or details.tmdb_rating < intent.tmdb_rating_min
    ):
        labels.append(f"TMDB rating: >= {intent.tmdb_rating_min:g}/10")

    if intent.original_language is not None and (
        details.original_language is None
        or details.original_language.casefold() != intent.original_language
    ):
        labels.append(f"language: {intent.original_language}")

    return labels


def _year_label(intent: MovieIntent) -> str:
    if intent.release_year_min is not None and intent.release_year_max is not None:
        return f"year: {intent.release_year_min}-{intent.release_year_max}"
    if intent.release_year_min is not None:
        return f"year: >= {intent.release_year_min}"
    if intent.release_year_max is not None:
        return f"year: <= {intent.release_year_max}"
    return "year"


def _hard_constraint_failures(
    details: TmdbMovieDetails,
    intent: MovieIntent,
    *,
    resolved_people: Sequence[ResolvedPerson] = (),
) -> list[str]:
    failures: list[str] = []

    if intent.release_year_min is not None and (
        details.release_year is None or details.release_year < intent.release_year_min
    ):
        failures.append("year")
    if intent.release_year_max is not None and (
        details.release_year is None or details.release_year > intent.release_year_max
    ):
        failures.append("year")
    if intent.original_language is not None and (
        details.original_language is None
        or details.original_language.casefold() != intent.original_language
    ):
        failures.append("language")
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
    if intent.people and not _matches_people(
        intent.people,
        details,
        resolved_people=resolved_people,
    ):
        failures.append("people")

    return list(dict.fromkeys(failures))


def _matches_genres(requested: Sequence[str], details: TmdbMovieDetails) -> bool:
    available = {_canonical_genre(value) for value in details.genres}
    supported = [value for value in requested if _canonical_genre(value) in _GENRE_IDS]
    return all(_canonical_genre(value) in available for value in supported)


def _matches_people(
    people: Sequence[PersonConstraint],
    details: TmdbMovieDetails,
    *,
    resolved_people: Sequence[ResolvedPerson] = (),
) -> bool:
    return all(
        _person_matches(
            person,
            details,
            resolved=_resolved_for(person, resolved_people),
        )
        for person in people
    )


def _resolved_for(
    person: PersonConstraint,
    resolved_people: Sequence[ResolvedPerson],
) -> ResolvedPerson | None:
    wanted_name = _collection_key(person.name)
    wanted_role = _collection_key(person.role or "")
    for resolved in resolved_people:
        if (
            _collection_key(resolved.original_name or resolved.name) == wanted_name
            and _collection_key(resolved.original_role or "") == wanted_role
        ):
            return resolved
    return None


def _person_matches(
    person: PersonConstraint,
    details: TmdbMovieDetails,
    *,
    resolved: ResolvedPerson | None = None,
) -> bool:
    name = _collection_key(person.name)
    role = _collection_key(person.role or "")
    person_id = person.tmdb_id

    grounded_id = resolved.tmdb_id if resolved is not None else None

    def name_matches(member_name: str) -> bool:
        if person_id is not None:
            return False
        return _collection_key(member_name) == name

    def cast_matches(member_id: int, member_name: str) -> bool:
        if person_id is not None:
            return member_id == person_id
        if grounded_id is not None and member_id == grounded_id:
            return True
        return name_matches(member_name)

    def crew_matches(member_id: int, member_name: str) -> bool:
        if person_id is not None:
            return member_id == person_id
        if grounded_id is not None and member_id == grounded_id:
            return True
        return name_matches(member_name)

    if person.role is None:
        return any(cast_matches(member.tmdb_id, member.name) for member in details.cast) or any(
            crew_matches(member.tmdb_id, member.name) for member in details.crew
        )

    if role in _CAST_ROLES:
        return any(cast_matches(member.tmdb_id, member.name) for member in details.cast)
    if role in _DIRECTOR_ROLES:
        return any(
            crew_matches(member.tmdb_id, member.name)
            and _collection_key(member.job or "") == "director"
            for member in details.crew
        )
    if role in _WRITER_ROLES:
        return any(
            crew_matches(member.tmdb_id, member.name)
            and (
                "writ" in _collection_key(member.job or "")
                or "screenplay" in _collection_key(member.job or "")
            )
            for member in details.crew
        )
    if role in _PRODUCER_ROLES:
        return any(
            crew_matches(member.tmdb_id, member.name)
            and "producer" in _collection_key(member.job or "")
            for member in details.crew
        )
    return any(
        crew_matches(member.tmdb_id, member.name)
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
    similar_to_title: str | None = None,
) -> Recommendation:
    caveats = list(provider_caveats)
    if intent.moods:
        caveats.append("Mood preferences are not verified by TMDB metadata.")

    return Recommendation(
        rank=rank,
        movie=_movie_details(details, watch_options),
        match_reason=(
            f"TMDB's similar-titles results for {similar_to_title} included this movie. "
            f"{_match_reason(details, intent)}"
            if similar_to_title is not None
            else _match_reason(details, intent)
        ),
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
    if failures["language"]:
        labels.append("language")
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


def is_supported_tmdb_genre(value: str) -> bool:
    return _canonical_genre(value) in _GENRE_IDS


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
