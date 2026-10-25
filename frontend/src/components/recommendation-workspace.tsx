"use client";

import { FormEvent, useEffect, useRef, useState } from "react";

import {
  getConversation,
  streamRecommendations,
  type RecommendationCard,
  type RecommendationResult,
  type TraceEvent,
} from "@/lib/api";

const CONVERSATION_STORAGE_KEY = "movie-agent-conversation";

type SavedConversation = { id: string; version: number };

export function RecommendationWorkspace() {
  const [message, setMessage] = useState("");
  const [conversation, setConversation] = useState<SavedConversation | null>(null);
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [result, setResult] = useState<RecommendationResult | null>(null);
  const [status, setStatus] = useState("Ready");
  const [isRunning, setIsRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const conversationId = conversation?.id;

  useEffect(() => {
    const timeout = window.setTimeout(() => {
      try {
        const raw = window.localStorage.getItem(CONVERSATION_STORAGE_KEY);
        if (raw) setConversation(JSON.parse(raw) as SavedConversation);
      } catch {
        window.localStorage.removeItem(CONVERSATION_STORAGE_KEY);
      }
    }, 0);
    return () => window.clearTimeout(timeout);
  }, []);

  useEffect(() => {
    if (!conversationId) return;
    let active = true;
    getConversation(conversationId)
      .then((snapshot) => {
        if (!active) return;
        if (!snapshot) {
          setConversation(null);
          setResult(null);
          window.localStorage.removeItem(CONVERSATION_STORAGE_KEY);
          return;
        }
        const restored = { id: snapshot.conversation_id, version: snapshot.version };
        setConversation(restored);
        window.localStorage.setItem(CONVERSATION_STORAGE_KEY, JSON.stringify(restored));
        if (snapshot.result) {
          setResult({
            ...snapshot.result,
            intent: snapshot.intent,
            issues: [],
            provider_lookups: [],
          });
        }
      })
      .catch(() => {
        if (active) setError("Could not restore this conversation. Start a new search.");
      });
    return () => {
      active = false;
    };
  }, [conversationId]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setEvents([]);
    setResult(null);
    setError(null);
    setStatus("Starting");
    setIsRunning(true);

    try {
      const response = await streamRecommendations(
        {
          message,
          clientRequestId: window.crypto.randomUUID(),
          conversationId: conversation?.id ?? null,
          expectedVersion: conversation?.version ?? null,
        },
        (nextEvent) => {
          setEvents((current) => [...current, nextEvent]);
          setStatus(nextEvent.type.replaceAll("_", " "));
          if (nextEvent.type === "recommendations" && nextEvent.data) {
            setResult(nextEvent.data as unknown as RecommendationResult);
          }
        },
        controller.signal,
      );
      const nextConversation = {
        id: response.conversationId,
        version: (conversation?.version ?? 1) + 1,
      };
      setConversation(nextConversation);
      window.localStorage.setItem(CONVERSATION_STORAGE_KEY, JSON.stringify(nextConversation));
      setMessage("");
      setStatus("Complete");
    } catch (reason) {
      if (controller.signal.aborted) {
        setStatus("Cancelled");
      } else {
        setStatus("Failed");
        setError(reason instanceof Error ? reason.message : "The recommendation request failed.");
      }
    } finally {
      setIsRunning(false);
    }
  }

  function startNewConversation() {
    abortRef.current?.abort();
    setConversation(null);
    setResult(null);
    setEvents([]);
    setMessage("");
    setError(null);
    setStatus("Ready");
    window.localStorage.removeItem(CONVERSATION_STORAGE_KEY);
  }

  return (
    <main className="workspace">
      <section className="hero">
        <p className="eyebrow">Movie Agent · grounded picks</p>
        <div className="workspaceHeading">
          <div>
            <h1>What should we watch tonight?</h1>
            <p className="lede">
              Describe the movie you want. Refine the results by chatting, or ask for another set.
            </p>
          </div>
          {conversation ? (
            <button className="quietButton" onClick={startNewConversation} type="button">
              New search
            </button>
          ) : null}
        </div>

        <form onSubmit={handleSubmit}>
          <label htmlFor="prompt">Tell me what you&apos;re in the mood for</label>
          <textarea
            id="prompt"
            maxLength={1200}
            minLength={1}
            onChange={(event) => setMessage(event.target.value)}
            placeholder="For example: three comedies with Jim Carrey"
            required
            rows={3}
            value={message}
          />
          <div className="formActions">
            <span>{message.length}/1200</span>
            <div className="actionButtons">
              {isRunning ? (
                <button
                  className="quietButton"
                  onClick={() => abortRef.current?.abort()}
                  type="button"
                >
                  Cancel
                </button>
              ) : null}
              <button disabled={isRunning || !message.trim()} type="submit">
                Get recommendations
              </button>
            </div>
          </div>
        </form>
      </section>

      <section className="trace" aria-live="polite" aria-busy={isRunning}>
        <div className="traceHeader">
          <h2>Search progress</h2>
          <span data-testid="status">{status}</span>
        </div>
        {error ? <p role="alert">{error}</p> : null}
        {events.length === 0 ? (
          <p className="empty">Your search steps will appear here.</p>
        ) : (
          <ol>
            {events.map((event) => (
              <li key={event.event_id}>
                <span>{event.stage}</span>
                <p>{event.summary}</p>
              </li>
            ))}
          </ol>
        )}
      </section>

      {result ? <Results result={result} /> : null}
      <footer className="attribution" aria-label="Credits">
        <a href="https://www.themoviedb.org/" rel="noreferrer" target="_blank">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            alt="TMDB"
            height={28}
            loading="lazy"
            src="https://www.themoviedb.org/assets/2/v4/logos/v2/blue_square_2-d537fb228cf3ded904ef09b136fe3fec72548ebc1fea3fbbd1ad9e36364db38b.svg"
            width={28}
          />
        </a>
        <span>
          This product uses TMDB and the TMDB APIs but is not endorsed, certified, or otherwise
          approved by TMDB.
        </span>
        <span>Streaming availability data is provided through JustWatch.</span>
      </footer>
    </main>
  );
}

function Results({ result }: { result: RecommendationResult }) {
  return (
    <section className="results" aria-live="polite">
      <div className="resultsHeading">
        <div>
          <p className="eyebrow">Your shortlist</p>
          <h2>
            {result.recommendations.length} of {result.requested_count} picks
          </h2>
        </div>
      </div>
      {result.shortfall_reason ? <p className="shortfall">{result.shortfall_reason}</p> : null}
      {result.clarification ? <p className="shortfall">{result.clarification}</p> : null}
      {result.recommendations.length ? (
        <div className="movieGrid">
          {result.recommendations.map((recommendation) => (
            <MovieCard key={recommendation.movie.tmdb_id} recommendation={recommendation} />
          ))}
        </div>
      ) : (
        <p className="empty">No verified matches yet. Try loosening one of the filters.</p>
      )}
    </section>
  );
}

function MovieCard({ recommendation }: { recommendation: RecommendationCard }) {
  const movie = recommendation.movie;
  const rating = movie.ratings.find((item) => item.source === "tmdb");
  const providerGroups = movie.watch_options
    ? [
        ["Subscription", movie.watch_options.subscription],
        ["Free", movie.watch_options.free],
        ["With ads", movie.watch_options.ads],
        ["Rent", movie.watch_options.rent],
        ["Buy", movie.watch_options.buy],
      ] as const
    : [];

  return (
    <article className="movieCard">
      <a className="poster" href={movie.tmdb_url} rel="noreferrer" target="_blank">
        {movie.poster_url ? (
          <span
            aria-label={`${movie.title} poster`}
            className="posterImage"
            role="img"
            style={{ backgroundImage: `url("${movie.poster_url}")` }}
          />
        ) : (
          <span className="posterFallback">Poster unavailable</span>
        )}
      </a>
      <div className="movieInfo">
        <p className="movieMeta">
          {movie.release_year ?? "Year unknown"}
          {movie.runtime_minutes ? ` · ${movie.runtime_minutes} min` : ""}
        </p>
        <h3>
          <a href={movie.tmdb_url} rel="noreferrer" target="_blank">
            {movie.title}
          </a>
        </h3>
        <p className="movieRating">
          TMDB · {rating?.value == null ? "Not rated" : `${rating.value.toFixed(1)} / 10`}
          {rating?.vote_count ? ` · ${rating.vote_count.toLocaleString()} votes` : ""}
        </p>
        <p className="movieOverview">{movie.overview || "No overview available."}</p>
        <p className="matchReason">{recommendation.match_reason}</p>
        {providerGroups.map(([label, providers]) =>
          providers.length ? (
            <p className="providers" key={label}>
              <strong>{label}:</strong>
              {providers.map((provider) => (
                <span className="providerBadge" key={provider.provider_id}>
                  {provider.logo_path ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img
                      alt=""
                      height={24}
                      loading="lazy"
                      src={`https://image.tmdb.org/t/p/w92${provider.logo_path}`}
                      width={24}
                    />
                  ) : null}
                  {provider.name}
                </span>
              ))}
            </p>
          ) : null,
        )}
        {recommendation.caveats.map((caveat) => (
          <p className="caveat" key={caveat}>
            {caveat}
          </p>
        ))}
      </div>
    </article>
  );
}
