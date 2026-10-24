import json

import httpx
import pytest

from movie_agent.main import app


@pytest.fixture
def transport() -> httpx.ASGITransport:
    return httpx.ASGITransport(app=app)


@pytest.mark.asyncio
async def test_health(transport: httpx.ASGITransport) -> None:
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "movie-agent-api"}


@pytest.mark.asyncio
async def test_fixture_stream_is_ordered_and_terminal(transport: httpx.ASGITransport) -> None:
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/fixture-stream", json={"prompt": "dark romance"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "fixture_transport=verified" in response.headers["set-cookie"]

    frames = [frame for frame in response.text.split("\n\n") if frame]
    payloads = [json.loads(frame.split("data: ", 1)[1]) for frame in frames]
    assert [payload["sequence"] for payload in payloads] == [1, 2, 3, 4, 5]
    assert payloads[-1]["type"] == "completed"
    assert all(payload["metadata"]["fixture"] is True for payload in payloads)
