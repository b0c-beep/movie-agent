import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from movie_agent.api.auth import router as auth_router
from movie_agent.api.recommendations import router as recommendations_router
from movie_agent.api.routes import router
from movie_agent.services.http import build_http_client
from movie_agent.services.omdb import OmdbClient
from movie_agent.services.persistence import PersistenceClient
from movie_agent.services.tmdb import TmdbClient


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    url = os.getenv("SUPABASE_URL", "").strip()
    secret = os.getenv("SUPABASE_SECRET_KEY", "").strip()
    app.state.persistence = None
    app.state.tmdb = None
    app.state.omdb = None
    client = None
    tmdb_client = None
    omdb_http = None
    omdb_client = None
    if url and secret:
        client = build_http_client()
        app.state.persistence = PersistenceClient(client, url, secret)
    tmdb_token = os.getenv("TMDB_API_TOKEN", "").strip()
    if tmdb_token:
        tmdb_client = build_http_client()
        app.state.tmdb = TmdbClient(tmdb_client, tmdb_token)
    omdb_key = os.getenv("OMDB_API_KEY", "").strip()
    if omdb_key:
        omdb_http = build_http_client()
        omdb_client = OmdbClient(omdb_http, omdb_key)
        app.state.omdb = omdb_client
    try:
        yield
    finally:
        if client is not None:
            await client.aclose()
        if tmdb_client is not None:
            await tmdb_client.aclose()
        if omdb_http is not None:
            await omdb_http.aclose()


app = FastAPI(title="Movie Agent API", version="0.1.0", lifespan=lifespan)
app.include_router(router)
app.include_router(auth_router)
app.include_router(recommendations_router)
