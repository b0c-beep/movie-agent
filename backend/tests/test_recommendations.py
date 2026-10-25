from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest
from pydantic import ValidationError

from movie_agent.agent.recommendations import (
    ProviderLookupOutcome,
    assemble_recommendations,
    merge_intent,
)
from movie_agent.models.intent import (
    MovieIntent,
    MovieIntentPatch,
    PersonConstraint,
    StringCollectionPatch,
    TurnKind,
)
from movie_agent.models.movies import RatingSource
from movie_agent.models.tmdb import (
    TmdbCastMember,
    TmdbCrewMember,
    TmdbDiscoverFilters,
    TmdbMovieCandidate,
    TmdbMovieDetails,
    TmdbMoviePage,
    TmdbPerson,
    TmdbPersonPage,
    TmdbWatchLookupStatus,
    TmdbWatchOptions,
    TmdbWatchProvider,
)
from movie_agent.services.tmdb import TmdbError


class FakeTmdbClient:
    def __init__(
        self,
        *,
        pages: dict[int, TmdbMoviePage] | None = None,
        details: dict[int, TmdbMovieDetails | Exception] | None = None,
        providers: dict[int, TmdbWatchOptions | Exception] | None = None,
    ) -> None:
        self.pages = pages or {}
        self.details = details or {}
        self.providers = providers or {}
        self.discover_calls: list[tuple[TmdbDiscoverFilters, int]] = []
        self.detail_calls: list[int] = []
        self.provider_calls: list[tuple[int, str]] = []
        self.people_calls: list[str] = []

    async def search_people(self, name: str, *, page: int = 1) -> TmdbPersonPage:
        self.people_calls.append(name)
        return TmdbPersonPage(
            page=page,
            total_pages=1,
            total_results=1,
            results=[
                TmdbPerson(
                    tmdb_id=9999,
                    name=name,
                    known_for_department="Acting",
                )
            ],
        )

    async def discover_movies(
        self,
        filters: TmdbDiscoverFilters,
        *,
        page: int = 1,
    ) -> TmdbMoviePage:
        self.discover_calls.append((filters, page))
        return self.pages.get(page, _page(page, 0, []))

    async def get_movie_details(self, tmdb_id: int) -> TmdbMovieDetails:
        self.detail_calls.append(tmdb_id)
        value = self.details.get(tmdb_id)
        if value is None:
            raise TmdbError("missing details")
        if isinstance(value, Exception):
            raise value
        return value

    async def get_watch_providers(self, tmdb_id: int, country: str) -> TmdbWatchOptions:
        self.provider_calls.append((tmdb_id, country))
        value = self.providers.get(tmdb_id)
        if value is None:
            return TmdbWatchOptions(
                country=country,
                status=TmdbWatchLookupStatus.NO_DATA,
            )
        if isinstance(value, Exception):
            raise value
        return value


class ConcurrencyTrackingTmdbClient(FakeTmdbClient):
    """Fake client that records in-flight requests and staggers completions."""

    def __init__(
        self,
        *,
        pages: dict[int, TmdbMoviePage] | None = None,
        details: dict[int, TmdbMovieDetails | Exception] | None = None,
        providers: dict[int, TmdbWatchOptions | Exception] | None = None,
        people_ids: dict[str, int] | None = None,
        delay: float = 0.01,
    ) -> None:
        super().__init__(pages=pages, details=details, providers=providers)
        self.people_ids = people_ids or {}
        self.delay = delay
        self.people_active = 0
        self.people_max_active = 0
        self.details_active = 0
        self.details_max_active = 0
        self.providers_active = 0
        self.providers_max_active = 0

    async def search_people(self, name: str, *, page: int = 1) -> TmdbPersonPage:
        self.people_calls.append(name)
        self.people_active += 1
        self.people_max_active = max(self.people_max_active, self.people_active)
        try:
            await asyncio.sleep(self.delay)
            return TmdbPersonPage(
                page=page,
                total_pages=1,
                total_results=1,
                results=[
                    TmdbPerson(
                        tmdb_id=self.people_ids[name],
                        name=name,
                        known_for_department="Acting",
                    )
                ],
            )
        finally:
            self.people_active -= 1

    async def get_movie_details(self, tmdb_id: int) -> TmdbMovieDetails:
        self.details_active += 1
        self.details_max_active = max(self.details_max_active, self.details_active)
        try:
            details = await super().get_movie_details(tmdb_id)
            await asyncio.sleep(self.delay if tmdb_id % 2 else self.delay / 2)
            return details
        finally:
            self.details_active -= 1

    async def get_watch_providers(self, tmdb_id: int, country: str) -> TmdbWatchOptions:
        self.providers_active += 1
        self.providers_max_active = max(self.providers_max_active, self.providers_active)
        try:
            options = await super().get_watch_providers(tmdb_id, country)
            await asyncio.sleep(self.delay if tmdb_id % 2 else self.delay / 2)
            return options
        finally:
            self.providers_active -= 1


def _page(
    page: int,
    total_pages: int,
    results: Sequence[TmdbMovieCandidate],
) -> TmdbMoviePage:
    return TmdbMoviePage(
        page=page,
        total_pages=total_pages,
        total_results=len(results),
        results=list(results),
    )


def _candidate(tmdb_id: int, *, year: int = 2000) -> TmdbMovieCandidate:
    return TmdbMovieCandidate(
        tmdb_id=tmdb_id,
        title=f"Movie {tmdb_id}",
        release_year=year,
        genre_ids=[35],
    )


def _details(
    tmdb_id: int,
    *,
    year: int = 2000,
    runtime: int = 100,
    genres: Sequence[str] = ("Comedy",),
    cast_names: Sequence[str] = (),
    director: str | None = None,
    rating: float | None = 7.5,
    imdb_id: str | None = None,
    poster_path: str | None = None,
) -> TmdbMovieDetails:
    cast = [
        TmdbCastMember(
            tmdb_id=1000 + index,
            name=name,
            character=None,
            order=index,
        )
        for index, name in enumerate(cast_names)
    ]
    crew = (
        [
            TmdbCrewMember(
                tmdb_id=2000,
                name=director,
                job="Director",
                department="Directing",
            )
        ]
        if director is not None
        else []
    )
    return TmdbMovieDetails(
        tmdb_id=tmdb_id,
        title=f"Movie {tmdb_id}",
        release_year=year,
        runtime_minutes=runtime,
        genres=list(genres),
        overview="A grounded overview.",
        poster_path=poster_path,
        imdb_id=imdb_id,
        tmdb_rating=rating,
        vote_count=100 if rating is not None else 0,
        cast=cast,
        crew=crew,
    )


def _provider(provider_id: int, name: str) -> TmdbWatchProvider:
    return TmdbWatchProvider(
        provider_id=provider_id,
        name=name,
        logo_path=f"/{provider_id}.jpg",
    )


def _watch(
    *,
    country: str = "RO",
    status: TmdbWatchLookupStatus = TmdbWatchLookupStatus.OK,
    subscription: Sequence[TmdbWatchProvider] = (),
) -> TmdbWatchOptions:
    return TmdbWatchOptions(
        country=country,
        status=status,
        subscription=list(subscription),
    )


def test_merge_preserves_prior_values_and_applies_collection_operations() -> None:
    prior = MovieIntent(
        genres=["Comedy"],
        people=[PersonConstraint(name="Jim Carrey")],
        moods=["Cozy"],
        release_year_min=1990,
        release_year_max=1999,
        original_language="en",
        runtime_max_minutes=120,
        country="US",
        requested_count=4,
    )
    patch = MovieIntentPatch(
        turn_kind=TurnKind.REFINE,
        genres=StringCollectionPatch(add=["Drama", "comedy"]),
        moods=StringCollectionPatch(remove=["cozy"]),
    )

    result = merge_intent(prior, patch)

    assert result.turn_kind is TurnKind.REFINE
    assert result.intent.genres == ["Comedy", "Drama"]
    assert result.intent.moods == []
    assert result.intent.people == prior.people
    assert result.intent.release_year_min == 1990
    assert result.intent.release_year_max == 1999
    assert result.intent.original_language == "en"
    assert result.intent.runtime_max_minutes == 120
    assert result.intent.country == "US"
    assert result.intent.requested_count == 4
    assert result.excluded_tmdb_ids == []
    assert result.validation_issues == []


def test_merge_collection_set_clear_country_and_count() -> None:
    prior = MovieIntent(
        genres=["Comedy"],
        moods=["Cozy"],
        country="US",
        requested_count=4,
    )
    patch = MovieIntentPatch(
        turn_kind=TurnKind.REFINE,
        genres=StringCollectionPatch(set=["Action", "action", " Drama "]),
        moods=StringCollectionPatch(clear=True),
        country="DE",
        requested_count=7,
    )

    result = merge_intent(prior, patch)

    assert result.intent.genres == ["Action", "Drama"]
    assert result.intent.moods == []
    assert result.intent.country == "DE"
    assert result.intent.requested_count == 7


def test_merge_detects_invalid_year_range_and_keeps_prior_bounds() -> None:
    prior = MovieIntent(release_year_min=1990, release_year_max=1999)

    late_min = merge_intent(
        prior,
        MovieIntentPatch(turn_kind=TurnKind.REFINE, release_year_min=2005),
    )
    early_max = merge_intent(
        prior,
        MovieIntentPatch(turn_kind=TurnKind.REFINE, release_year_max=1980),
    )

    for result in (late_min, early_max):
        assert result.intent.release_year_min == 1990
        assert result.intent.release_year_max == 1999
        assert [issue.code for issue in result.validation_issues] == ["invalid_year_range"]


def test_another_set_excludes_all_seen_ids_and_keeps_constraints() -> None:
    prior = MovieIntent(
        genres=["Comedy"],
        moods=["Cozy"],
        release_year_min=1990,
        release_year_max=1999,
        country="RO",
        requested_count=5,
    )
    patch = MovieIntentPatch(turn_kind=TurnKind.ANOTHER_SET, requested_count=3)

    result = merge_intent(prior, patch, seen_tmdb_ids=[603, 604, 603, 0, -1])

    assert result.turn_kind is TurnKind.ANOTHER_SET
    assert result.excluded_tmdb_ids == [603, 604]
    assert result.intent.genres == ["Comedy"]
    assert result.intent.moods == ["Cozy"]
    assert result.intent.release_year_min == 1990
    assert result.intent.release_year_max == 1999
    assert result.intent.requested_count == 3


def test_refine_keeps_constraints_and_does_not_exclude_prior_results() -> None:
    prior = MovieIntent(
        genres=["Comedy"],
        moods=["Cozy"],
        release_year_min=1990,
        release_year_max=1999,
        runtime_max_minutes=120,
        country="RO",
        requested_count=5,
    )
    patch = MovieIntentPatch(turn_kind=TurnKind.REFINE, requested_count=2)

    result = merge_intent(prior, patch, seen_tmdb_ids=[603, 604])

    assert result.intent.genres == ["Comedy"]
    assert result.intent.moods == ["Cozy"]
    assert result.intent.release_year_min == 1990
    assert result.intent.release_year_max == 1999
    assert result.intent.runtime_max_minutes == 120
    assert result.intent.requested_count == 2
    assert result.excluded_tmdb_ids == []


def test_explicit_clear_filters_remove_people_year_language_and_runtime() -> None:
    prior = MovieIntent(
        people=[PersonConstraint(name="Jim Carrey")],
        release_year_min=1990,
        release_year_max=1999,
        original_language="en",
        runtime_max_minutes=120,
    )
    patch = MovieIntentPatch(
        turn_kind=TurnKind.REFINE,
        clear_filters=["people", "release_year", "original_language", "runtime_max_minutes"],
    )

    result = merge_intent(prior, patch)

    assert result.intent.people == []
    assert result.intent.release_year_min is None
    assert result.intent.release_year_max is None
    assert result.intent.original_language is None
    assert result.intent.runtime_max_minutes is None


def test_clear_filter_cannot_conflict_with_an_update() -> None:
    with pytest.raises(ValidationError, match="cannot conflict"):
        MovieIntentPatch(
            turn_kind=TurnKind.REFINE,
            original_language="en",
            clear_filters=["original_language"],
        )


@pytest.mark.asyncio
async def test_discovery_uses_only_explicit_filters_and_caps_candidates() -> None:
    intent = MovieIntent(
        genres=["Comedy", "sci-fi"],
        release_year_min=2000,
        release_year_max=2010,
        original_language="en",
        runtime_max_minutes=120,
        requested_count=2,
    )
    candidates = [_candidate(index, year=2005) for index in range(1, 11)]
    details = {
        index: _details(index, year=2005, runtime=200, genres=["Comedy"]) for index in range(1, 11)
    }
    client = FakeTmdbClient(pages={1: _page(1, 3, candidates)}, details=details)

    result = await assemble_recommendations(client, intent)

    assert result.batch.recommendations == []
    assert client.detail_calls == [1, 2, 3, 4, 5, 6]
    assert len(client.discover_calls) == 1
    filters, page_number = client.discover_calls[0]
    assert page_number == 1
    assert filters.release_year_min == 2000
    assert filters.release_year_max == 2010
    assert filters.original_language == "en"
    assert filters.runtime_max_minutes == 120
    assert filters.genre_ids == [35, 878]
    assert filters.people_ids == []
    assert filters.watch_region is None
    assert client.people_calls == []
    assert result.batch.shortfall_reason is not None
    assert "runtime" in result.batch.shortfall_reason


@pytest.mark.asyncio
async def test_discovery_requests_at_most_three_pages() -> None:
    intent = MovieIntent(requested_count=10)
    pages = {page: _page(page, 10, [_candidate(page)]) for page in (1, 2, 3, 4)}
    details = {page: _details(page) for page in (1, 2, 3, 4)}
    client = FakeTmdbClient(pages=pages, details=details)

    result = await assemble_recommendations(client, intent)

    assert [page for _, page in client.discover_calls] == [1, 2, 3]
    assert [recommendation.movie.tmdb_id for recommendation in result.batch.recommendations] == [
        1,
        2,
        3,
    ]
    assert result.batch.shortfall_reason is not None
    assert "3" in result.batch.shortfall_reason


@pytest.mark.asyncio
async def test_assembly_deduplicates_preserves_rank_and_builds_observed_facts() -> None:
    intent = MovieIntent(
        genres=["Comedy"],
        release_year_min=1990,
        release_year_max=1999,
        requested_count=2,
    )
    pages = {
        1: _page(1, 2, [_candidate(603, year=1999), _candidate(604, year=1995)]),
        2: _page(2, 2, [_candidate(603, year=1999), _candidate(605, year=1994)]),
    }
    details = {
        603: _details(
            603,
            year=1999,
            runtime=136,
            genres=["Comedy"],
            rating=8.2,
            imdb_id="tt0133093",
            poster_path="/matrix.jpg",
        ),
        604: _details(604, year=1995, runtime=90, genres=["Comedy"], rating=None),
    }
    providers = {
        603: _watch(subscription=[_provider(8, "Netflix")]),
        604: _watch(status=TmdbWatchLookupStatus.NO_DATA),
    }
    client = FakeTmdbClient(pages=pages, details=details, providers=providers)

    result = await assemble_recommendations(client, intent)

    recommendations = result.batch.recommendations
    assert [recommendation.rank for recommendation in recommendations] == [1, 2]
    assert [recommendation.movie.tmdb_id for recommendation in recommendations] == [603, 604]
    assert client.detail_calls == [603, 604]

    first = recommendations[0]
    assert first.movie.title == "Movie 603"
    assert first.movie.release_year == 1999
    assert first.movie.runtime_minutes == 136
    assert first.movie.genres == ["Comedy"]
    assert str(first.movie.tmdb_url) == "https://www.themoviedb.org/movie/603"
    assert str(first.movie.imdb_url) == "https://www.imdb.com/title/tt0133093/"
    assert str(first.movie.poster_url) == "https://image.tmdb.org/t/p/w500/matrix.jpg"
    assert first.movie.ratings[0].source is RatingSource.TMDB
    assert first.movie.ratings[0].value == 8.2
    assert first.movie.watch_options is not None
    assert [provider.name for provider in first.movie.watch_options.subscription] == ["Netflix"]
    assert "Comedy" in first.match_reason
    assert "1999" in first.match_reason
    assert "genre:Comedy" in first.matched_constraints

    second = recommendations[1]
    assert second.movie.watch_options is None
    assert second.movie.ratings[0].value is None
    assert any("no watch-provider data" in caveat for caveat in second.caveats)
    assert result.batch.shortfall_reason is None
    assert [lookup.outcome for lookup in result.provider_lookups] == [
        ProviderLookupOutcome.OK,
        ProviderLookupOutcome.NO_DATA,
    ]


@pytest.mark.asyncio
async def test_hard_constraint_shortfalls_use_details_and_report_constraints() -> None:
    intent = MovieIntent(
        genres=["Comedy"],
        release_year_min=1990,
        release_year_max=1999,
        runtime_max_minutes=120,
        people=[PersonConstraint(name="Jim Carrey")],
        requested_count=5,
    )
    candidates = [_candidate(index) for index in range(1, 6)]
    details = {
        1: _details(1, year=2005, genres=["Comedy"], cast_names=["Jim Carrey"]),
        2: _details(2, year=1995, genres=["Drama"], cast_names=["Jim Carrey"]),
        3: _details(3, year=1995, genres=["Comedy"], runtime=150, cast_names=["Jim Carrey"]),
        4: _details(4, year=1995, genres=["Comedy"], cast_names=["Someone Else"]),
        5: _details(5, year=1995, genres=["Comedy"], cast_names=["Jim Carrey"]),
    }
    client = FakeTmdbClient(pages={1: _page(1, 1, candidates)}, details=details)

    result = await assemble_recommendations(client, intent)

    assert [recommendation.movie.tmdb_id for recommendation in result.batch.recommendations] == [5]
    assert client.detail_calls == [1, 2, 3, 4, 5]
    assert client.provider_calls == [(5, "RO")]
    filters, _ = client.discover_calls[0]
    assert filters.people_ids == [9999]
    assert client.people_calls == ["Jim Carrey"]
    assert result.batch.shortfall_reason is not None
    for expected in ("1 of 5", "year", "genre", "runtime", "people"):
        assert expected in result.batch.shortfall_reason
    rejected = {issue.tmdb_id for issue in result.issues if issue.code == "candidate_rejected"}
    assert rejected == {1, 2, 3, 4}


@pytest.mark.asyncio
async def test_assembly_excludes_previously_seen_ids_before_details() -> None:
    intent = MovieIntent(requested_count=2)
    pages = {1: _page(1, 1, [_candidate(1), _candidate(2), _candidate(3)])}
    details = {3: _details(3)}
    client = FakeTmdbClient(pages=pages, details=details)

    result = await assemble_recommendations(client, intent, excluded_tmdb_ids=[1, 2])

    assert [recommendation.movie.tmdb_id for recommendation in result.batch.recommendations] == [3]
    assert client.detail_calls == [3]
    assert client.provider_calls == [(3, "RO")]
    assert result.batch.shortfall_reason is not None
    assert "Previously shown movies were excluded" in result.batch.shortfall_reason


@pytest.mark.asyncio
async def test_provider_no_data_and_failure_are_distinguished() -> None:
    intent = MovieIntent(requested_count=2)
    pages = {1: _page(1, 1, [_candidate(1), _candidate(2)])}
    details = {1: _details(1), 2: _details(2)}
    providers = {
        1: _watch(status=TmdbWatchLookupStatus.NO_DATA),
        2: TmdbError("provider lookup failed"),
    }
    client = FakeTmdbClient(pages=pages, details=details, providers=providers)

    result = await assemble_recommendations(client, intent)

    recommendations = result.batch.recommendations
    assert [recommendation.movie.tmdb_id for recommendation in recommendations] == [1, 2]
    assert all(recommendation.movie.watch_options is None for recommendation in recommendations)
    assert [lookup.outcome for lookup in result.provider_lookups] == [
        ProviderLookupOutcome.NO_DATA,
        ProviderLookupOutcome.ERROR,
    ]
    assert any("no watch-provider data" in caveat for caveat in recommendations[0].caveats)
    assert any("lookup failed" in caveat for caveat in recommendations[1].caveats)
    assert result.batch.shortfall_reason is None


@pytest.mark.asyncio
async def test_person_searches_are_bounded_and_preserve_order() -> None:
    people = [PersonConstraint(name=f"Actor {index}") for index in range(1, 11)]
    intent = MovieIntent(people=people, requested_count=1)
    client = ConcurrencyTrackingTmdbClient(
        pages={1: _page(1, 1, [_candidate(1)])},
        details={1: _details(1)},
        people_ids={f"Actor {index}": 1000 + index for index in range(1, 11)},
    )

    await assemble_recommendations(client, intent)

    assert 1 < client.people_max_active <= 4
    assert client.people_calls == [f"Actor {index}" for index in range(1, 11)]
    filters, _ = client.discover_calls[0]
    assert filters.people_ids == list(range(1001, 1011))


@pytest.mark.asyncio
async def test_detail_fetches_are_batched_and_preserve_candidate_order() -> None:
    intent = MovieIntent(requested_count=8)
    candidates = [_candidate(index) for index in range(1, 9)]
    details = {index: _details(index) for index in range(1, 9)}
    client = ConcurrencyTrackingTmdbClient(
        pages={1: _page(1, 1, candidates)},
        details=details,
    )

    result = await assemble_recommendations(client, intent)

    assert 1 < client.details_max_active <= 4
    assert client.detail_calls == list(range(1, 9))
    assert [recommendation.rank for recommendation in result.batch.recommendations] == list(
        range(1, 9)
    )
    assert [recommendation.movie.tmdb_id for recommendation in result.batch.recommendations] == (
        list(range(1, 9))
    )


@pytest.mark.asyncio
async def test_provider_fetches_are_bounded_and_preserve_recommendation_order() -> None:
    intent = MovieIntent(requested_count=8)
    candidates = [_candidate(index) for index in range(1, 9)]
    details = {index: _details(index) for index in range(1, 9)}
    providers = {
        index: _watch(subscription=[_provider(index, f"Provider {index}")]) for index in range(1, 9)
    }
    client = ConcurrencyTrackingTmdbClient(
        pages={1: _page(1, 1, candidates)},
        details=details,
        providers=providers,
    )

    result = await assemble_recommendations(client, intent)

    assert 1 < client.providers_max_active <= 4
    assert client.provider_calls == [(index, "RO") for index in range(1, 9)]
    assert [lookup.tmdb_id for lookup in result.provider_lookups] == list(range(1, 9))
    assert [recommendation.movie.tmdb_id for recommendation in result.batch.recommendations] == (
        list(range(1, 9))
    )
