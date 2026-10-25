# Movie Agent

Movie Agent helps answer the deceptively difficult question: **what should I watch tonight?** It turns conversational requests such as “give me three 1990s dark romances” or “an action movie with Sylvester Stallone” into grounded, country-aware recommendations.

> Status: Phase 2 is complete. Phase 3 is in progress on `main` at `5f8a332`, including per-tab conversations, new-tab passcode prompts, richer cards and trace labels, and a 4,500-token intent extraction budget. A post-merge production smoke at this revision completed a synthetic recommendation request. Current Phase 3 work adds country-only availability refresh, optional OMDb ratings, and answers about saved cards; local checks pass and the changes are pending owner review.

## Planned Product Experience

- Understand genre, mood, era, cast, runtime, language, rating, and result-count preferences.
- Preserve context across follow-ups such as “make them newer” or “give me another three.”
- Stream a safe, visible **predict → decide → act** trace while the agent works.
- Return responsive movie cards with posters, match explanations, ratings, and external links.
- Show country-specific streaming providers and their logos.
- Protect the demo behind a shared passcode to control API usage; the current Phase 3 work asks again on each new tab while retaining the HttpOnly cookie for API ownership checks.

New searches default to five recommendations and can request a different count, capped at ten. If too few verified matches exist, the app will explain the shortfall. Follow-ups can refine preferences, request another set, or ask about a displayed movie.

## Planned Architecture

```text
Next.js UI on Vercel
        │ streamed HTTP / SSE
        ▼
FastAPI + Pydantic AI
        ├── Groq + Pydantic AI: bounded typed intent extraction
        ├── TMDB: movies, people, posters, ratings, providers
        ├── OMDb: optional external rating enrichment
        └── Supabase: anonymous sessions, conversations, runs, usage limits
```

The model extracts a typed intent patch; TMDB supplies movie facts; application code verifies hard constraints and assembles the cards. The portable backend and protected recommendation flow are deployed on Vercel. A production smoke at `5f8a332` completed a synthetic request with the updated intent-extraction budget; broader recommendation quality remains under evaluation.

## Data Sources

- **TMDB** is the required source for movie metadata, artwork, TMDB ratings, external IDs, and regional watch-provider data.
- A requested rating minimum uses TMDB's 0–10 score and is enforced during discovery and detail verification. IMDb and Rotten Tomatoes thresholds are not supported yet; the app asks for a TMDB threshold instead of substituting a different source.
- **OMDb** optionally supplies IMDb and Rotten Tomatoes values. Recommendations continue with TMDB data if OMDb is unavailable.
- Watch-provider data is powered by JustWatch. Direct provider deep links are outside the MVP.

TMDB requires its approved logo and this notice: *This product uses TMDB and the TMDB APIs but is not endorsed, certified, or otherwise approved by TMDB.* See [TMDB's logo and attribution guidelines](https://www.themoviedb.org/about/logos-attribution).

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

Open `http://localhost:3000`. Phase 2 adds the protected recommendation flow; live requests require the backend settings below. The fixture stream remains available for deterministic transport checks.

Run local checks with:

```bash
make check
make test-e2e
```

The end-to-end command starts both applications and requires a Playwright Chromium installation (`cd frontend && npx playwright install chromium`). It covers a deterministic unlock/recommendation browser flow plus the fixture stream transport; it does not call paid services. Never commit populated environment files.

The opt-in Groq compatibility smoke requires `GROQ_API_KEY` and a supported `GROQ_MODEL`. `openai/gpt-oss-20b` passed the live tool and typed-output smoke. Ordinary tests use Pydantic AI's deterministic test model and do not consume API quota.

### Environment

Create the server-only local environment file:

```bash
cp backend/.env.example backend/.env
```

Set `SUPABASE_URL` to the project's API URL and `SUPABASE_SECRET_KEY` to a current `sb_secret_...` backend key. The secret key bypasses Row Level Security: never place it in `frontend/`, prefix it with `NEXT_PUBLIC_`, paste it into tickets or chat, or commit it. The browser does not connect to Supabase directly, so no publishable key is currently required.

Before using live recommendations on a new database, apply the files in `supabase/migrations/` in filename order in the Supabase SQL Editor. They create the private session/conversation/run/usage tables and their atomic run RPCs. The project database has all current migrations applied; live checks cover unlock throttling, conversation ownership, run claims, replay, and finalization. Keep later forward fixes applied when upgrading an existing database.

Set `APP_PASSCODE` to the shared passcode shown at app entry. Set `SESSION_SIGNING_KEY` to a private random value of at least 32 bytes; generate one with `openssl rand -hex 32`. The passcode is checked without storing it in the database, and signed session cookies are HttpOnly. Unlock attempts are limited to ten per client IP per UTC hour; Supabase stores only a keyed HMAC bucket value, not the IP address. Keep both secrets private. The local `make dev-backend` command loads `backend/.env`; Vercel deployments must set these as backend project environment variables and redeploy after changes.

The other required live-integration variables are `GROQ_API_KEY`, `GROQ_MODEL`, and `TMDB_API_TOKEN`; `OMDB_API_KEY` is optional. `frontend/.env.local` only needs `BACKEND_ORIGIN` when the backend is not available at the default local address.

The `APP_PASSCODE` and `SESSION_SIGNING_KEY` values are backend-only in Vercel too. Also configure `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `GROQ_API_KEY`, `GROQ_MODEL`, and `TMDB_API_TOKEN` on the backend project; set `APP_ENV=production` so the session cookie is marked Secure. The frontend project only needs `BACKEND_ORIGIN`. Redeploy after changing a Vercel environment variable.

Development follows one branch and pull request per phase or independently reviewable part. Tasks become one or more focused commits. Local agents follow the workflow in the ignored `AGENTS.md`; detailed plans, deployment notes, and remaining limitations stay in `.brain/`.

### Preview deployment proof

Import this repository into two Vercel projects, selecting `backend` and `frontend` as their respective Root Directories. The backend exports its app from the standard `backend/main.py` entrypoint; use Vercel's detected FastAPI settings without a custom Build Command or Output Directory. Configure the backend variables above, apply the database migration, deploy the backend, then set `BACKEND_ORIGIN` on the frontend project to the backend deployment origin, without a trailing `/api`. Use the detected Next.js settings for the frontend. Keep preview environment variables separate from production values; changing a Vercel variable requires a new deployment.

The app roots include `.vercelignore` files so a later CLI deployment does not upload local `.env` files. Git imports use the tracked repository and its `.gitignore` rules.

Verify `GET /api/health` on the backend deployment, then run the browser transport check through the frontend deployment:

```bash
cd frontend
PREVIEW_BASE_URL=https://your-frontend-preview.example npm run test:e2e
```

`PREVIEW_BASE_URL` skips local server startup. The first browser test stubs the access and recommendation APIs; it checks unlock state, streamed recommendation parsing, and card rendering. The second uses the fixture endpoint through the same-origin `/api` rewrite and checks incremental SSE delivery. Use the actual reachable deployment URL; a protected preview may require a browser session with Vercel access.

The Phase 1 deployment proof passed on October 25, 2026: [frontend](https://movie-agent-frontend-iota.vercel.app/) forwarded `/api/health` to the [backend](https://movie-agent-backend.vercel.app/api/health), and deployed Playwright fixture tests passed, including incremental SSE delivery and HttpOnly cookie forwarding. The backend's direct fixture stream delivered five ordered events over about 0.31 seconds. Phase 2 health, passcode unlock, rating filtering, and active-claim conflict behavior were verified in production. A synthetic production request at `5f8a332` passed through intent update, recommendations, and completion with the 4,500-token budget; its temporary session was removed afterward. This smoke does not establish broad recommendation quality.

## Current Limitations

- The protected passcode/session flow, TMDB adapter, recommendation API and cards are merged to `main` and deployed. The Supabase schema and core persistence RPCs have been exercised against the project database.
- The browser suite uses deterministic fixtures. A hosted rating-threshold request passed at production revision `f536fba`; the live active-claim loser returned HTTP 409 in 1.027 seconds after the Phase 2 migration. The 4,500-token extraction budget passed one synthetic production smoke at `5f8a332`.
- The per-tab conversation pointer uses `sessionStorage`; recommendations receive only the active conversation's preferences. Cross-conversation taste inference is not implemented. Phase 3 changes to country refresh, optional OMDb enrichment, and saved-card questions are under review and are not yet deployed.
- Availability is provider-level and country-specific; it may be incomplete or stale.
- Subjective qualities such as “dark” are interpretations, not objective catalog fields.
- Rotten Tomatoes enrichment is optional and may be absent for individual titles.
- Unrated titles will show “Not rated”; missing availability data will be distinguished from a failed lookup.

## Attribution

Movie metadata and imagery are supplied by TMDB. Watch-provider data is provided through TMDB's JustWatch partnership and needs visible JustWatch attribution. Optional external rating data is supplied through OMDb, which is not affiliated with IMDb.
