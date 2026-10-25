# Movie Agent

Movie Agent helps answer the deceptively difficult question: **what should I watch tonight?** It turns conversational requests such as “give me three 1990s dark romances” or “an action movie with Sylvester Stallone” into grounded, country-aware recommendations.

> Status: foundation implementation. The first working release is targeted for Tuesday, October 27, 2026.

## Planned Product Experience

- Understand genre, mood, era, cast, runtime, language, rating, and result-count preferences.
- Preserve context across follow-ups such as “make them newer” or “give me another three.”
- Stream a safe, visible **predict → decide → act** trace while the agent works.
- Return responsive movie cards with posters, match explanations, ratings, and external links.
- Show country-specific streaming providers and their logos.
- Protect the demo behind a shared passcode to control API usage.

New searches default to five recommendations and can request a different count, with a planned cap of ten. If too few verified matches exist, the app will explain the shortfall. Follow-ups can refine preferences, request another set, or ask about a displayed movie.

## Planned Architecture

```text
Next.js UI on Vercel
        │ streamed HTTP / SSE
        ▼
FastAPI + Pydantic AI
        ├── Groq: intent, tool decisions, ranking, explanations
        ├── TMDB: movies, people, posters, ratings, providers
        ├── OMDb: optional external rating enrichment
        └── Supabase: conversations, runs, traces, cache
```

The LLM plans and ranks; provider APIs supply movie facts. Pydantic models validate every tool boundary, and application code rechecks hard constraints before presenting results. Each visitor gets a distinct anonymous session. The portable backend's initial hosting and streaming integration will be verified during scaffolding.

## Data Sources

- **TMDB** is the required source for movie metadata, artwork, TMDB ratings, external IDs, and regional watch-provider data.
- **OMDb** optionally supplies IMDb and Rotten Tomatoes values. Recommendations continue with TMDB data if OMDb is unavailable.
- Watch-provider data is powered by JustWatch. Direct provider deep links are outside the MVP.

Required TMDB notice: *This product uses the TMDB API but is not endorsed or certified by TMDB.*

## Repository Layout

```text
backend/   FastAPI, Pydantic contracts, Pydantic AI, provider adapters and tests
frontend/  Next.js application, streamed-event client and browser tests
supabase/  Database migrations and database tests
```

Detailed planning and agent working memory remain local to the development workspace.

## Development

Prerequisites: Python 3.13, [uv](https://docs.astral.sh/uv/), Node.js 20.9 or newer, and npm. Install pinned dependencies from their lockfiles:

```bash
make install
```

Run the API and web application in separate terminals:

```bash
make dev-backend
make dev-frontend
```

Open `http://localhost:3000`. The current UI exercises a deterministic SSE fixture through the frontend's same-origin `/api` rewrite. It does not call paid APIs.

Run local checks with:

```bash
make check
make test-e2e
```

The end-to-end command starts both applications and requires a Playwright Chromium installation (`cd frontend && npx playwright install chromium`). Copy the environment examples when live integrations are introduced; never commit populated environment files.

The opt-in Groq compatibility smoke requires `GROQ_API_KEY` and a supported `GROQ_MODEL`. `openai/gpt-oss-20b` passed the live tool and typed-output smoke. Ordinary tests use Pydantic AI's deterministic test model and do not consume API quota.

### Environment

Create the server-only local environment file:

```bash
cp backend/.env.example backend/.env
```

Set `SUPABASE_URL` to the project's API URL and `SUPABASE_SECRET_KEY` to a current `sb_secret_...` backend key. The secret key bypasses Row Level Security: never place it in `frontend/`, prefix it with `NEXT_PUBLIC_`, paste it into tickets or chat, or commit it. The browser does not connect to Supabase directly, so no publishable key is currently required.

The other required live-integration variables are `GROQ_API_KEY`, `GROQ_MODEL`, and `TMDB_API_TOKEN`; `OMDB_API_KEY` is optional. `frontend/.env.local` only needs `BACKEND_ORIGIN` when the backend is not available at the default local address.

Development follows one branch and pull request per phase or independently reviewable part. Tasks become one or more focused commits. Local agents follow the workflow in the ignored `AGENTS.md`; the README will gain setup commands, screenshots, demo instructions, tradeoffs, and known limitations as implementation progresses.

### Preview deployment proof

Import this repository into two Vercel projects, selecting `backend` and `frontend` as their respective Root Directories. Set `BACKEND_ORIGIN` on the frontend project to the backend deployment origin, without a trailing `/api`. Deploy the backend first, then the frontend. The current fixture endpoint needs no live provider secrets in Vercel. Keep preview environment variables separate from production values; changing a Vercel variable requires a new deployment.

The app roots include `.vercelignore` files so a later CLI deployment does not upload local `.env` files. Git imports use the tracked repository and its `.gitignore` rules.

Verify `GET /api/health` on the backend deployment, then run the browser fixture check through the frontend deployment:

```bash
cd frontend
PREVIEW_BASE_URL=https://your-frontend-preview.example npm run test:e2e
```

`PREVIEW_BASE_URL` skips local server startup. The browser test checks the frontend's `/api` forwarding, the completed stream, and the forwarded HttpOnly fixture cookie. Use the actual reachable deployment URL; a protected preview may require a browser session with Vercel access.

## Current Limitations

- Only the typed contracts, health endpoint, fixture stream, and transport-proof UI exist; real recommendation behavior is not implemented yet.
- Vercel preview streaming and cookies still require deployed verification. Groq tool/typed-output compatibility and API credential authentication have been checked; real recommendation behavior and Supabase tables have not.
- Availability is provider-level and country-specific; it may be incomplete or stale.
- Subjective qualities such as “dark” are interpretations, not objective catalog fields.
- Rotten Tomatoes enrichment is optional and may be absent for individual titles.
- Unrated titles will show “Not rated”; missing availability data will be distinguished from a failed lookup.

## Attribution

Movie metadata and imagery are supplied by TMDB. Watch-provider data requires JustWatch attribution. Optional external rating data is supplied through OMDb, which is not affiliated with IMDb.
