"use client";

import { FormEvent, useEffect, useRef, useState } from "react";

import {
  getConversation,
  refreshConversationAvailability,
  streamRecommendations,
  type RecommendationCard,
  type RecommendationResult,
  type TraceEvent,
} from "@/lib/api";

const CONVERSATION_STORAGE_KEY = "movie-agent-conversation";

const EVENT_TYPE_LABELS: Record<string, string> = {
  run_started: "Run started",
  intent_updated: "Preferences updated",
  decision: "Decision",
  tool_started: "Tool started",
  tool_finished: "Tool finished",
  recommendations: "Recommendations",
  completed: "Completed",
  needs_clarification: "Needs clarification",
  failed: "Run failed",
  cancelled: "Run cancelled",
};

const RATING_LABELS: Record<string, string> = {
  tmdb: "TMDB",
  imdb: "IMDb",
  rotten_tomatoes: "Rotten Tomatoes",
};

type SavedConversation = { id: string; version: number };
type ProviderLookup = RecommendationResult["provider_lookups"][number];

function readSavedConversation(): SavedConversation | null {
  try {
    const raw = window.sessionStorage.getItem(CONVERSATION_STORAGE_KEY);
    if (!raw) return null;
    const value: unknown = JSON.parse(raw);
    if (
      typeof value === "object" &&
      value !== null &&
      "id" in value &&
      typeof value.id === "string" &&
      "version" in value &&
      typeof value.version === "number"
    ) {
      return { id: value.id, version: value.version };
    }
  } catch {
    // A fresh conversation is safer than restoring a malformed tab pointer.
  }
  saveConversation(null);
  return null;
}

function saveConversation(conversation: SavedConversation | null) {
  try {
    if (conversation) {
      window.sessionStorage.setItem(CONVERSATION_STORAGE_KEY, JSON.stringify(conversation));
    } else {
      window.sessionStorage.removeItem(CONVERSATION_STORAGE_KEY);
    }
  } catch {
    // The current tab continues in memory if browser storage is unavailable.
  }
}

function eventTypeLabel(type: string): string {
  return (
    EVENT_TYPE_LABELS[type] ??
    type.replaceAll("_", " ").replace(/^./, (character) => character.toUpperCase())
  );
}

function ratingLabel(source: string): string {
  return RATING_LABELS[source] ?? source.replaceAll("_", " ");
}

function ratingValue(value: number, scale: string): string {
  const formatted = scale === "10" || !Number.isInteger(value) ? value.toFixed(1) : String(value);
  return `${formatted} / ${scale}`;
}

function failureMessage(event: TraceEvent): string {
  const errorCode = typeof event.metadata?.error_code === "string" ? event.metadata.error_code : null;
  const failureStage =
    typeof event.metadata?.failure_stage === "string" ? event.metadata.failure_stage : null;
  if (errorCode === "run_timeout") {
    return "The search took too long to finish. Your conversation was not changed—please try again.";
  }
  if (errorCode === "tmdb_unavailable") {
    return "Movie data is temporarily unavailable. Your conversation was not changed—please try again.";
  }
  if (failureStage === "intent_extraction") {
    return "I couldn’t interpret that request. Try a shorter prompt with a genre, actor, or rating.";
  }
  if (failureStage === "run_finalization") {
    return "The results could not be saved. Your conversation was not changed—please try again.";
  }
  return "The recommendation run could not be completed. Your conversation was not changed—please try again.";
}

function requestErrorMessage(reason: unknown, fallback: string): string {
  if (!(reason instanceof Error)) return fallback;
  if (/session expired|status 401/i.test(reason.message)) {
    return "Your session expired. Reload this tab to enter the passcode again.";
  }
  return reason.message;
}

function countryName(code: string): string {
  const option = COUNTRY_OPTIONS.find(([value]) => value === code);
  return option ? option[1] : code;
}

export function RecommendationWorkspace() {
  const [message, setMessage] = useState("");
  const [conversation, setConversation] = useState<SavedConversation | null>(null);
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [result, setResult] = useState<RecommendationResult | null>(null);
  const [answer, setAnswer] = useState<string | null>(null);
  const [status, setStatus] = useState("Ready");
  const [isRunning, setIsRunning] = useState(false);
  const [isRefreshingAvailability, setIsRefreshingAvailability] = useState(false);
  const [availabilityError, setAvailabilityError] = useState<string | null>(null);
  const [availabilityNotice, setAvailabilityNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const conversationId = conversation?.id;

  useEffect(() => {
    const timeout = window.setTimeout(() => {
      const saved = readSavedConversation();
      if (saved) setConversation(saved);
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
          saveConversation(null);
          return;
        }
        const restored = { id: snapshot.conversation_id, version: snapshot.version };
        setConversation(restored);
        saveConversation(restored);
        if (snapshot.result) {
          setResult({
            ...snapshot.result,
            intent: snapshot.intent,
            clarification: snapshot.clarification ?? undefined,
            issues: snapshot.issues ?? [],
            provider_lookups: snapshot.provider_lookups ?? [],
          });
        } else if (snapshot.clarification) {
          setResult({
            intent: snapshot.intent,
            recommendations: [],
            requested_count:
              typeof snapshot.intent.requested_count === "number"
                ? snapshot.intent.requested_count
                : 5,
            shortfall_reason: null,
            clarification: snapshot.clarification,
            issues: snapshot.issues ?? [],
            provider_lookups: snapshot.provider_lookups ?? [],
          });
        } else {
          setResult(null);
        }
      })
      .catch((reason) => {
        if (active) {
          setError(
            requestErrorMessage(reason, "Could not restore this conversation. Start a new search."),
          );
        }
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
    setError(null);
    setAvailabilityError(null);
    setAvailabilityNotice(null);
    setAnswer(null);
    setStatus("Starting");
    setIsRunning(true);
    const terminal: { event: TraceEvent | null } = { event: null };

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
          if (nextEvent.type === "failed") {
            terminal.event = nextEvent;
            setStatus("Failed");
            setError(failureMessage(nextEvent));
            return;
          }
          if (nextEvent.type === "cancelled") {
            terminal.event = nextEvent;
            setStatus("Cancelled");
            return;
          }
          if (nextEvent.type === "needs_clarification") {
            terminal.event = nextEvent;
            setStatus("Needs clarification");
            if (nextEvent.data) {
              setResult(nextEvent.data as unknown as RecommendationResult);
            }
            return;
          }
          setStatus(nextEvent.type.replaceAll("_", " "));
          if (nextEvent.type === "recommendations" && nextEvent.data) {
            setResult(nextEvent.data as unknown as RecommendationResult);
          }
          if (
            nextEvent.type === "completed" &&
            typeof nextEvent.data?.answer === "string"
          ) {
            setAnswer(nextEvent.data.answer);
          }
        },
        controller.signal,
      );
      if (terminal.event?.type === "failed" || terminal.event?.type === "cancelled") return;
      const nextConversation = {
        id: response.conversationId,
        version: (conversation?.version ?? 1) + 1,
      };
      setConversation(nextConversation);
      saveConversation(nextConversation);
      setMessage("");
      setStatus(
        terminal.event?.type === "needs_clarification" ? "Needs clarification" : "Complete",
      );
    } catch (reason) {
      if (terminal.event) return;
      if (controller.signal.aborted) {
        setStatus("Cancelled");
      } else {
        setStatus("Failed");
        setError(requestErrorMessage(reason, "The recommendation request failed."));
      }
    } finally {
      setIsRunning(false);
    }
  }

  function startNewConversation() {
    abortRef.current?.abort();
    setConversation(null);
    setResult(null);
    setAnswer(null);
    setEvents([]);
    setMessage("");
    setError(null);
    setAvailabilityError(null);
    setAvailabilityNotice(null);
    setStatus("Ready");
    saveConversation(null);
  }

  async function changeAvailabilityCountry(country: string): Promise<boolean> {
    if (!conversation || !result || isRunning || isRefreshingAvailability) return false;
    setIsRefreshingAvailability(true);
    setAvailabilityError(null);
    setAvailabilityNotice(null);
    try {
      const snapshot = await refreshConversationAvailability(
        conversation.id,
        country,
        conversation.version,
      );
      const nextConversation = { id: snapshot.conversation_id, version: snapshot.version };
      setConversation(nextConversation);
      saveConversation(nextConversation);
      if (snapshot.result) {
        setResult({
          ...snapshot.result,
          intent: snapshot.intent,
          issues: snapshot.issues ?? [],
          provider_lookups: snapshot.provider_lookups ?? [],
        });
      }
      setAvailabilityNotice(`Streaming availability updated for ${countryName(country)}.`);
      return true;
    } catch (reason) {
      setAvailabilityError(
        requestErrorMessage(
          reason,
          "Could not update country availability. Your saved preferences were not changed.",
        ),
      );
      return false;
    } finally {
      setIsRefreshingAvailability(false);
    }
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

      <section className="trace" aria-labelledby="trace-heading">
        <div className="traceHeader">
          <h2 id="trace-heading">Search progress</h2>
          <span aria-live="polite" data-testid="status" role="status">
            {status}
          </span>
        </div>
        {error ? <p role="alert">{error}</p> : null}
        {answer ? <p className="conversationAnswer" role="status">{answer}</p> : null}
        {events.length === 0 ? (
          <p className="empty">Your search steps will appear here.</p>
        ) : (
          <ol aria-live="polite" aria-relevant="additions">
            {events.map((event) => (
              <li key={event.event_id}>
                <div className="traceLabels">
                  <span className="traceStage">{event.stage}</span>
                  <span className="traceOrigin" data-origin={event.origin}>
                    {event.origin === "agent" ? "Agent" : "Application"}
                  </span>
                  <span className="traceType">{eventTypeLabel(event.type)}</span>
                </div>
                <p>{event.summary}</p>
              </li>
            ))}
          </ol>
        )}
      </section>

      {result ? (
        <Results
          error={availabilityError}
          isRefreshing={isRefreshingAvailability}
          isRunning={isRunning}
          notice={availabilityNotice}
          onCountryChange={changeAvailabilityCountry}
          result={result}
        />
      ) : null}
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

const COUNTRY_OPTIONS = [
  ["RO", "Romania"],
  ["US", "United States"],
  ["GB", "United Kingdom"],
  ["CA", "Canada"],
  ["AU", "Australia"],
  ["NZ", "New Zealand"],
  ["DE", "Germany"],
  ["FR", "France"],
  ["ES", "Spain"],
  ["IT", "Italy"],
  ["NL", "Netherlands"],
  ["IE", "Ireland"],
  ["JP", "Japan"],
  ["KR", "South Korea"],
  ["IN", "India"],
  ["BR", "Brazil"],
] as const;

function Results({
  result,
  error,
  isRefreshing,
  isRunning,
  notice,
  onCountryChange,
}: {
  result: RecommendationResult;
  error: string | null;
  isRefreshing: boolean;
  isRunning: boolean;
  notice: string | null;
  onCountryChange: (country: string) => Promise<boolean>;
}) {
  const savedCountry =
    typeof result.intent?.country === "string" && /^[A-Z]{2}$/.test(result.intent.country)
      ? result.intent.country
      : "RO";
  const countryPickerRef = useRef<HTMLSelectElement>(null);

  useEffect(() => {
    if (countryPickerRef.current) countryPickerRef.current.value = savedCountry;
  }, [savedCountry]);

  async function selectCountry(country: string) {
    if (isRefreshing || isRunning) {
      if (countryPickerRef.current) countryPickerRef.current.value = savedCountry;
      return;
    }
    const updated = await onCountryChange(country);
    if (!updated && countryPickerRef.current) countryPickerRef.current.value = savedCountry;
  }

  return (
    <section className="results" aria-labelledby="results-heading">
      <div className="resultsHeading">
        <div>
          <p className="eyebrow">Your shortlist</p>
          <h2 id="results-heading">
            {result.clarification
              ? "A little more detail needed"
              : `${result.recommendations.length} of ${result.requested_count} picks`}
          </h2>
        </div>
        {result.recommendations.length ? (
          <div className="countryPicker">
            <label htmlFor="availability-country">Streaming availability country</label>
            <select
              aria-disabled={isRefreshing || isRunning}
              defaultValue={savedCountry}
              disabled={isRunning}
              id="availability-country"
              onChange={(event) => void selectCountry(event.target.value)}
              ref={countryPickerRef}
            >
              {!COUNTRY_OPTIONS.some(([code]) => code === savedCountry) ? (
                <option value={savedCountry}>{savedCountry}</option>
              ) : null}
              {COUNTRY_OPTIONS.map(([code, label]) => (
                <option key={code} value={code}>
                  {label} ({code})
                </option>
              ))}
            </select>
            <span aria-live="polite" className="countryStatus">
              {isRefreshing
                ? "Refreshing providers…"
                : isRunning
                  ? "Wait for the current search to finish."
                  : notice ?? "Provider lookup only; movie picks stay the same."}
            </span>
          </div>
        ) : null}
      </div>
      {error ? <p className="availabilityError" role="alert">{error}</p> : null}
      {result.shortfall_reason ? (
        <p className="shortfall" role="status">
          {result.shortfall_reason}
        </p>
      ) : null}
      {result.clarification ? (
        <p className="clarification shortfall" role="status">
          {result.clarification}
        </p>
      ) : null}
      {result.recommendations.length ? (
        <div className="movieGrid">
          {result.recommendations.map((recommendation) => (
            <MovieCard
              key={recommendation.movie.tmdb_id}
              providerLookup={(result.provider_lookups ?? []).find(
                (lookup) => lookup.tmdb_id === recommendation.movie.tmdb_id,
              )}
              recommendation={recommendation}
            />
          ))}
        </div>
      ) : result.clarification ? null : (
        <p className="empty">No verified matches yet. Try loosening one of the filters.</p>
      )}
    </section>
  );
}

function MovieCard({
  recommendation,
  providerLookup,
}: {
  recommendation: RecommendationCard;
  providerLookup?: ProviderLookup;
}) {
  const movie = recommendation.movie;
  const ratings = movie.ratings ?? [];
  const tmdbRating = ratings.find((item) => item.source === "tmdb");
  const externalRatings = ratings.filter(
    (item) => item.source !== "tmdb" && item.value != null,
  );
  const genres = movie.genres ?? [];
  const providerGroups = movie.watch_options
    ? [
        ["Subscription", movie.watch_options.subscription],
        ["Free", movie.watch_options.free],
        ["With ads", movie.watch_options.ads],
        ["Rent", movie.watch_options.rent],
        ["Buy", movie.watch_options.buy],
      ] as const
    : [];
  const caveats = providerLookup
    ? recommendation.caveats.filter((caveat) => !/provider|availability/i.test(caveat))
    : recommendation.caveats;

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
        <p className="movieGenres">
          {genres.length ? genres.join(" · ") : "Genres not listed."}
        </p>
        <div className="movieRatings">
          <p className="movieRating">
            TMDB ·{" "}
            {tmdbRating?.value == null
              ? "Not rated"
              : ratingValue(tmdbRating.value, tmdbRating.scale)}
            {tmdbRating?.vote_count
              ? ` · ${tmdbRating.vote_count.toLocaleString()} votes`
              : ""}
          </p>
          {externalRatings.map((rating) => (
            <p className="externalRating" key={rating.source}>
              {ratingLabel(rating.source)} · {ratingValue(rating.value as number, rating.scale)}
              {rating.vote_count ? ` · ${rating.vote_count.toLocaleString()} votes` : ""}
            </p>
          ))}
        </div>
        <p className="movieOverview">{movie.overview || "No overview available."}</p>
        <p className="matchReason">{recommendation.match_reason}</p>
        <p className="movieLinks">
          <a href={movie.tmdb_url} rel="noreferrer" target="_blank">
            TMDB page
          </a>
          {movie.imdb_url ? (
            <a href={movie.imdb_url} rel="noreferrer" target="_blank">
              IMDb page
            </a>
          ) : null}
        </p>
        <ProviderAvailability lookup={providerLookup} />
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
        {caveats.map((caveat) => (
          <p className="caveat" key={caveat}>
            {caveat}
          </p>
        ))}
      </div>
    </article>
  );
}

function ProviderAvailability({ lookup }: { lookup?: ProviderLookup }) {
  if (!lookup) return null;
  if (lookup.outcome === "error") {
    return (
      <p className="availability availabilityError">
        Availability lookup failed for {lookup.country}. Streaming options are unknown.
      </p>
    );
  }
  if (lookup.outcome === "no_data") {
    return (
      <p className="availability">
        No provider data for {lookup.country}. This does not mean the movie is unavailable.
      </p>
    );
  }
  if (lookup.provider_count === 0) {
    return (
      <p className="availability">No streaming providers listed for {lookup.country}.</p>
    );
  }
  return null;
}
