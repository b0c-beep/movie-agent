from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from pydantic import ValidationError

from movie_agent.models.tmdb import TmdbDiscoverFilters, TmdbWatchLookupStatus
from movie_agent.services.tmdb import TmdbClient, TmdbError

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.mark.asyncio
async def test_search_people_normalizes_facts_and_authenticates() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "page": 1,
                "total_pages": 2,
                "total_results": 21,
                "results": [
                    {
                        "id": 206,
                        "name": "Jim Carrey",
                        "known_for_department": "Acting",
                        "profile_path": "/jim.jpg",
                        "known_for": [
                            {"title": "Ace Ventura"},
                            {"name": "In Living Color"},
                        ],
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        page = await TmdbClient(http, " test-token ").search_people(" Jim Carrey ")

    assert page.page == 1
    assert page.total_pages == 2
    assert page.total_results == 21
    assert page.results[0].tmdb_id == 206
    assert page.results[0].name == "Jim Carrey"
    assert page.results[0].known_for_department == "Acting"
    assert page.results[0].profile_path == "/jim.jpg"
    assert page.results[0].known_for_titles == ["Ace Ventura", "In Living Color"]

    request = requests[0]
    assert request.url.path == "/3/search/person"
    assert dict(request.url.params) == {
        "query": "Jim Carrey",
        "page": "1",
        "include_adult": "false",
    }
    assert request.headers["authorization"] == "Bearer test-token"


@pytest.mark.asyncio
async def test_discover_movies_uses_only_whitelisted_params_and_null_ratings() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "page": 2,
                "total_pages": 3,
                "total_results": 45,
                "results": [
                    {
                        "id": 603,
                        "title": "The Matrix",
                        "original_title": "The Matrix",
                        "release_date": "1999-03-31",
                        "overview": "A hacker learns the truth.",
                        "poster_path": "/matrix.jpg",
                        "vote_average": 8.2,
                        "vote_count": 25000,
                        "genre_ids": [28, 878],
                    },
                    {
                        "id": 604,
                        "title": "No Votes",
                        "original_title": "No Votes",
                        "release_date": "1999-01-01",
                        "overview": "",
                        "poster_path": None,
                        "vote_average": 0.0,
                        "vote_count": 0,
                        "genre_ids": [],
                    },
                    {
                        "id": 605,
                        "title": "No Score",
                        "original_title": "No Score",
                        "release_date": "",
                        "overview": "",
                        "poster_path": None,
                        "vote_average": None,
                        "vote_count": 4,
                        "genre_ids": [],
                    },
                ],
            },
        )

    filters = TmdbDiscoverFilters(
        release_year_min=1990,
        release_year_max=1999,
        original_language="en",
        tmdb_rating_min=7.5,
        genre_ids=[35, 18],
        people_ids=[206],
        crew_ids=[525],
        watch_region="RO",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        page = await TmdbClient(http, "token").discover_movies(filters, page=2)

    assert page.results[0].tmdb_id == 603
    assert page.results[0].release_year == 1999
    assert page.results[0].tmdb_rating == 8.2
    assert page.results[0].genre_ids == [28, 878]
    assert page.results[1].release_year == 1999
    assert page.results[1].tmdb_rating is None
    assert page.results[2].release_year is None
    assert page.results[2].tmdb_rating is None

    request = requests[0]
    assert request.url.path == "/3/discover/movie"
    assert dict(request.url.params) == {
        "page": "2",
        "include_adult": "false",
        "primary_release_date.gte": "1990-01-01",
        "primary_release_date.lte": "1999-12-31",
        "with_original_language": "en",
        "vote_average.gte": "7.5",
        "with_genres": "35,18",
        "with_people": "206",
        "with_crew": "525",
        "watch_region": "RO",
    }


@pytest.mark.asyncio
async def test_get_movie_details_normalizes_credits_keywords_and_external_ids() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": 603,
                "title": "The Matrix",
                "original_title": "The Matrix",
                "release_date": "1999-03-31",
                "runtime": 136,
                "genres": [
                    {"id": 28, "name": "Action"},
                    {"id": 878, "name": "Science Fiction"},
                ],
                "overview": "A hacker learns the truth.",
                "poster_path": "/matrix.jpg",
                "backdrop_path": "/matrix-backdrop.jpg",
                "vote_average": 8.2,
                "vote_count": 25000,
                "external_ids": {"imdb_id": "tt0133093"},
                "keywords": {"keywords": [{"id": 4565, "name": "cyberpunk"}]},
                "credits": {
                    "cast": [
                        {
                            "id": 6384,
                            "name": "Keanu Reeves",
                            "character": "Neo",
                            "order": 0,
                        }
                    ],
                    "crew": [
                        {
                            "id": 9339,
                            "name": "Lana Wachowski",
                            "job": "Director",
                            "department": "Directing",
                        }
                    ],
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        details = await TmdbClient(http, "token").get_movie_details(603)

    assert details.tmdb_id == 603
    assert details.release_year == 1999
    assert details.runtime_minutes == 136
    assert details.genres == ["Action", "Science Fiction"]
    assert details.imdb_id == "tt0133093"
    assert details.tmdb_rating == 8.2
    assert details.vote_count == 25000
    assert details.keywords == ["cyberpunk"]
    assert details.cast[0].character == "Neo"
    assert details.crew[0].job == "Director"

    request = requests[0]
    assert request.url.path == "/3/movie/603"
    assert request.url.params["append_to_response"] == ("credits,keywords,external_ids")


@pytest.mark.asyncio
async def test_get_watch_providers_distinguishes_missing_country_data() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": 603,
                "results": {
                    "RO": {
                        "link": "https://www.themoviedb.org/movie/603/watch?locale=RO",
                        "flatrate": [
                            {
                                "provider_id": 8,
                                "provider_name": "Netflix",
                                "logo_path": "/netflix.jpg",
                            }
                        ],
                        "free": [],
                        "ads": [],
                        "rent": [
                            {
                                "provider_id": 2,
                                "provider_name": "Apple TV",
                                "logo_path": "/apple.jpg",
                            }
                        ],
                        "buy": [],
                    }
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TmdbClient(http, "token")
        found = await client.get_watch_providers(603, "RO")
        missing = await client.get_watch_providers(603, "US")

    assert found.status is TmdbWatchLookupStatus.OK
    assert found.country == "RO"
    assert found.link is not None
    assert [provider.name for provider in found.subscription] == ["Netflix"]
    assert [provider.name for provider in found.rent] == ["Apple TV"]
    assert found.free == []

    assert missing.status is TmdbWatchLookupStatus.NO_DATA
    assert missing.country == "US"
    assert missing.subscription == []
    assert missing.rent == []


@pytest.mark.asyncio
async def test_empty_provider_pages_are_not_request_failures() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/3/search/person":
            return httpx.Response(
                200,
                json={
                    "page": 1,
                    "total_pages": 0,
                    "total_results": 0,
                    "results": [],
                },
            )
        return httpx.Response(
            200,
            json={
                "page": 1,
                "total_pages": 0,
                "total_results": 0,
                "results": [],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TmdbClient(http, "token")
        people = await client.search_people("Unknown Person")
        movies = await client.discover_movies()

    assert people.results == []
    assert movies.results == []


@pytest.mark.asyncio
async def test_malformed_json_and_payloads_raise_typed_error() -> None:
    def bad_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    def bad_payload(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "page": 1,
                "total_pages": 1,
                "total_results": 1,
                "results": [{"name": "Missing ID"}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(bad_json)) as http:
        with pytest.raises(TmdbError):
            await TmdbClient(http, "token").search_people("Jim Carrey")

    async with httpx.AsyncClient(transport=httpx.MockTransport(bad_payload)) as http:
        with pytest.raises(TmdbError):
            await TmdbClient(http, "token").search_people("Jim Carrey")


@pytest.mark.asyncio
async def test_error_status_maps_to_typed_error_without_retry() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            429,
            json={"status_message": "Your request count is over the allowed limit."},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(TmdbError) as exc_info:
            await TmdbClient(http, "token").get_movie_details(603)

    assert exc_info.value.status_code == 429
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_local_bounds_are_validated_before_any_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ValueError):
            TmdbClient(http, "  ")
        client = TmdbClient(http, "token")

        with pytest.raises(ValueError):
            await client.search_people("Jim Carrey", page=0)
        with pytest.raises(ValueError):
            await client.discover_movies(page=501)
        with pytest.raises(ValueError):
            await client.get_movie_details(0)
        with pytest.raises(ValueError):
            await client.get_watch_providers(603, "ro")
        with pytest.raises(ValueError):
            await client.get_watch_providers(603, "ROU")

    assert requests == []

    with pytest.raises(ValidationError):
        TmdbDiscoverFilters(release_year_min=2000, release_year_max=1990)
    with pytest.raises(ValidationError):
        TmdbDiscoverFilters(genre_ids=[0])
    with pytest.raises(ValidationError):
        TmdbDiscoverFilters(original_language="ENGLISH")


@pytest.mark.asyncio
async def test_result_page_bounds_reject_oversized_provider_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "page": 1,
                "total_pages": 2,
                "total_results": 21,
                "results": [{"id": index, "name": f"Person {index}"} for index in range(1, 22)],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(TmdbError):
            await TmdbClient(http, "token").search_people("Any Person")
