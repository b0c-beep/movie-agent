# Movie Agent

Movie Agent helps answer the deceptively difficult question: **what should I watch tonight?** It turns conversational requests such as “give me three 1990s dark romances” or “an action movie with Sylvester Stallone” into grounded, country-aware recommendations.

> Status: architecture and implementation planning. The first working release is targeted for Tuesday, October 27, 2026.

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

The implementation will use `backend/` for the portable FastAPI service, `frontend/` for the Next.js application, and `supabase/` for migrations and database tests. Detailed planning and agent working memory remain local to the development workspace.

## Development

The application has not been scaffolded yet, so build and test commands are not available. This section must be updated as soon as the backend and frontend toolchains are introduced. Secrets belong in local environment files; a sanitized `.env.example` will document required variable names.

Development follows one branch and pull request per phase or independently reviewable part. Tasks become one or more focused commits. Local agents follow the workflow in the ignored `AGENTS.md`; the README will gain setup commands, screenshots, demo instructions, tradeoffs, and known limitations as implementation progresses.

## Current Limitations

- No application code or deployment exists yet.
- Availability is provider-level and country-specific; it may be incomplete or stale.
- Subjective qualities such as “dark” are interpretations, not objective catalog fields.
- Rotten Tomatoes enrichment is optional and may be absent for individual titles.
- Unrated titles will show “Not rated”; missing availability data will be distinguished from a failed lookup.

## Attribution

Movie metadata and imagery are supplied by TMDB. Watch-provider data requires JustWatch attribution. Optional external rating data is supplied through OMDb, which is not affiliated with IMDb.
