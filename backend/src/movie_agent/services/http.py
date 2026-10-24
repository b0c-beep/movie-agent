from dataclasses import dataclass

import httpx


@dataclass(frozen=True, slots=True)
class HttpClientConfig:
    timeout_seconds: float = 5.0
    max_connections: int = 20
    max_keepalive_connections: int = 10


def build_http_client(config: HttpClientConfig | None = None) -> httpx.AsyncClient:
    resolved = config or HttpClientConfig()
    return httpx.AsyncClient(
        timeout=httpx.Timeout(resolved.timeout_seconds),
        limits=httpx.Limits(
            max_connections=resolved.max_connections,
            max_keepalive_connections=resolved.max_keepalive_connections,
        ),
        follow_redirects=False,
    )
