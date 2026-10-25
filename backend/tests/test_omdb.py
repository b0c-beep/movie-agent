from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from movie_agent.models.movies import RatingSource
from movie_agent.services.omdb import MAX_IMDB_IDS, OmdbClient, OmdbLookupStatus

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.mark.asyncio
async def test_get_ratings_normalizes_imdb_and_rotten_tomatoes() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "Response": "True",
                "imdbID": "tt0133093",
                "imdbRating": "8.7",
                "imdbVotes": "2,000,000",
                "Ratings": [
                    {"Source": "Internet Movie Database", "Value": "8.7/10"},
                    {"Source": "Rotten Tomatoes", "Value": "83%"},
                    {"Source": "Metacritic", "Value": "73/100"},
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await OmdbClient(http, " test-key ").get_ratings(" TT0133093 ")

    assert result.imdb_id == "tt0133093"
    assert result.status is OmdbLookupStatus.OK
    assert result.http_status is None

    assert result.imdb is not None
    assert result.imdb.source is RatingSource.IMDB
    assert result.imdb.value == pytest.approx(8.7)
    assert result.imdb.scale == "10"

    assert result.rotten_tomatoes is not None
    assert result.rotten_tomatoes.source is RatingSource.ROTTEN_TOMATOES
    assert result.rotten_tomatoes.value == pytest.approx(83.0)
    assert result.rotten_tomatoes.scale == "100"

    request = requests[0]
    assert request.method == "GET"
    assert request.url.scheme == "https"
    assert request.url.host == "www.omdbapi.com"
    assert request.url.path == "/"
    assert dict(request.url.params) == {"apikey": "test-key", "i": "tt0133093"}
    assert request.headers["accept"] == "application/json"
    assert request.extensions["timeout"] == {
        "connect": 5.0,
        "read": 5.0,
        "write": 5.0,
        "pool": 5.0,
    }


@pytest.mark.asyncio
async def test_missing_and_malformed_rating_fields_are_ignored() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "Response": "true",
                "imdbRating": "7.8",
                "Ratings": [
                    {"Source": "Internet Movie Database", "Value": "N/A"},
                    {"Source": "Rotten Tomatoes", "Value": "certified fresh"},
                    {"Source": "Rotten Tomatoes"},
                    {"Value": "90%"},
                    {"Source": "IMDb", "Value": 8.1},
                    "not-a-mapping",
                    {"Source": "Metacritic", "Value": "73/100"},
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await OmdbClient(http, "test-key").get_ratings("tt0133093")

    assert result.status is OmdbLookupStatus.OK
    assert result.imdb is not None
    assert result.imdb.value == pytest.approx(7.8)
    assert result.imdb.scale == "10"
    assert result.rotten_tomatoes is None


@pytest.mark.asyncio
async def test_out_of_range_and_unparseable_scores_are_dropped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "Response": "True",
                "imdbRating": "N/A",
                "Ratings": [
                    {"Source": "IMDb", "Value": "11/10"},
                    {"Source": "Internet Movie Database", "Value": "eight"},
                    {"Source": "ROTTEN TOMATOES", "Value": "101%"},
                    {"Source": "Rotten Tomatoes", "Value": "8.3/10"},
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await OmdbClient(http, "test-key").get_ratings("tt0133093")

    assert result.status is OmdbLookupStatus.NO_RATINGS
    assert result.imdb is None
    assert result.rotten_tomatoes is None


@pytest.mark.asyncio
async def test_not_found_is_typed_without_ratings() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"Response": "False", "Error": "Incorrect IMDb ID."},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await OmdbClient(http, "test-key").get_ratings("tt0133093")

    assert result.status is OmdbLookupStatus.NOT_FOUND
    assert result.imdb is None
    assert result.rotten_tomatoes is None
    assert result.http_status is None
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_omdb_api_limit_error_is_unavailable_not_a_missing_movie() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"Response": "False", "Error": "Request limit reached!"},
            )
        )
    ) as http:
        result = await OmdbClient(http, "test-key").get_ratings("tt0133093")

    assert result.status is OmdbLookupStatus.UNAVAILABLE
    assert result.imdb is None
    assert result.rotten_tomatoes is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 429, 500])
async def test_http_failures_return_unavailable_without_retry(status_code: int) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, text="provider failure")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await OmdbClient(http, "test-key").get_ratings("tt0133093")

    assert result.status is OmdbLookupStatus.UNAVAILABLE
    assert result.http_status == status_code
    assert result.imdb is None
    assert result.rotten_tomatoes is None
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_transport_timeout_is_non_fatal() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ReadTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await OmdbClient(http, "test-key", timeout_seconds=1.5).get_ratings("tt0133093")

    assert result.status is OmdbLookupStatus.UNAVAILABLE
    assert result.http_status is None
    assert result.imdb is None
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"imdbRating": "8.0"}),
        httpx.Response(200, json={"Response": True, "imdbRating": "8.0"}),
        httpx.Response(200, json={"Response": "Unknown"}),
    ],
)
async def test_malformed_responses_are_unavailable(response: httpx.Response) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as http:
        result = await OmdbClient(http, "test-key").get_ratings("tt0133093")

    assert result.status is OmdbLookupStatus.UNAVAILABLE
    assert result.http_status is None
    assert result.imdb is None
    assert result.rotten_tomatoes is None


@pytest.mark.asyncio
async def test_batch_lookups_are_bounded_and_preserve_input_order() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        imdb_id = dict(request.url.params)["i"]
        if imdb_id == "tt0000002":
            return httpx.Response(
                200,
                json={"Response": "False", "Error": "Incorrect IMDb ID."},
            )
        return httpx.Response(
            200,
            json={
                "Response": "True",
                "Ratings": [
                    {"Source": "Internet Movie Database", "Value": "8.1/10"},
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        results = await OmdbClient(
            http,
            "test-key",
            max_concurrent_requests=2,
        ).get_ratings_for_ids(["tt0000001", "tt0000002"])

    assert [result.imdb_id for result in results] == ["tt0000001", "tt0000002"]
    assert results[0].status is OmdbLookupStatus.OK
    assert results[0].imdb is not None
    assert results[0].imdb.value == pytest.approx(8.1)
    assert results[1].status is OmdbLookupStatus.NOT_FOUND
    assert results[1].imdb is None


@pytest.mark.asyncio
async def test_local_input_bounds_are_validated_before_any_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ValueError):
            OmdbClient(http, "   ")
        with pytest.raises(ValueError):
            OmdbClient(http, "test-key", timeout_seconds=0)
        with pytest.raises(ValueError):
            OmdbClient(http, "test-key", timeout_seconds=-1)
        with pytest.raises(ValueError):
            OmdbClient(http, "test-key", timeout_seconds=float("inf"))
        with pytest.raises(ValueError):
            OmdbClient(http, "test-key", timeout_seconds=float("nan"))
        with pytest.raises(ValueError):
            OmdbClient(http, "test-key", max_concurrent_requests=0)
        with pytest.raises(ValueError):
            OmdbClient(http, "test-key", max_concurrent_requests=MAX_IMDB_IDS + 1)

        client = OmdbClient(http, "test-key")
        for invalid_id in ("", "tt", "tt12345", "tt12345678901", "nm1234567"):
            with pytest.raises(ValueError):
                await client.get_ratings(invalid_id)

        with pytest.raises(ValueError):
            await client.get_ratings_for_ids([])
        with pytest.raises(ValueError):
            await client.get_ratings_for_ids(["tt1234567"] * (MAX_IMDB_IDS + 1))
        with pytest.raises(ValueError):
            await client.get_ratings_for_ids("tt1234567")

    assert requests == []
