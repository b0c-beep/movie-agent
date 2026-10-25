"use client";

import { useEffect, useRef } from "react";

import { ClarificationChoices } from "@/components/clarification-choices";
import { MovieCard } from "@/components/movie-card";
import type { ClarificationOption, RecommendationResult } from "@/lib/api";
import { COUNTRY_OPTIONS } from "@/lib/countries";
import { isBudgetExhausted } from "@/lib/workspace";

export function RecommendationResults({
  result,
  error,
  isRefreshing,
  isRunning,
  notice,
  onCountryChange,
  onChooseClarification,
  onEditFilters,
  onRelaxFilter,
}: {
  result: RecommendationResult;
  error: string | null;
  isRefreshing: boolean;
  isRunning: boolean;
  notice: string | null;
  onCountryChange: (country: string) => Promise<boolean>;
  onChooseClarification: (option: ClarificationOption) => void;
  onEditFilters: () => void;
  onRelaxFilter: (unmetFilter: string) => void;
}) {
  const savedCountry =
    typeof result.intent?.country === "string" && /^[A-Z]{2}$/.test(result.intent.country)
      ? result.intent.country
      : "RO";
  const countryPickerRef = useRef<HTMLSelectElement>(null);
  const alternatives = (result.alternatives ?? []).slice(0, 3);
  const clarificationOptions = result.clarification
    ? result.clarification_options ?? []
    : [];
  const hasMatches = result.recommendations.length > 0 || alternatives.length > 0;

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

  const heading = result.clarification
    ? "A little more detail needed"
    : result.recommendations.length
      ? `${result.recommendations.length} of ${result.requested_count} exact picks`
      : alternatives.length
        ? "No exact matches"
        : "No verified matches";

  return (
    <section className="results" aria-labelledby="results-heading">
      <div className="resultsHeading">
        <div>
          <p className="eyebrow">Your shortlist</p>
          <h2 id="results-heading">{heading}</h2>
        </div>
        <div className="resultsActions">
          <button className="quietButton" onClick={onEditFilters} type="button">
            Edit active filters
          </button>
          {hasMatches ? (
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
      </div>
      {error ? <p className="availabilityError" role="alert">{error}</p> : null}
      {result.shortfall_reason ? (
        <p className="shortfall" role="status">
          {result.shortfall_reason}
        </p>
      ) : null}
      {result.clarification ? (
        <div className="clarificationPanel">
          <p className="clarification shortfall" role="status">
            {result.clarification}
          </p>
          {clarificationOptions.length ? (
            <ClarificationChoices
              disabled={isRunning}
              key={clarificationOptions.map((option) => option.choice_id).join("|")}
              onChoose={onChooseClarification}
              options={clarificationOptions}
            />
          ) : (
            <p className="empty">Reply in the prompt below with the person, title, or detail you meant.</p>
          )}
        </div>
      ) : null}
      {result.recommendations.length ? (
        <section aria-labelledby="exact-heading" className="exactResults">
          <h3 id="exact-heading">Exact matches</h3>
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
        </section>
      ) : null}
      {alternatives.length ? (
        <section aria-labelledby="alternatives-heading" className="alternatives">
          <h3 id="alternatives-heading">Closest alternatives</h3>
          <p className="muted">
            These are grounded TMDB results that miss at least one explicit filter. They are not
            exact matches.
          </p>
          <div className="alternativeList">
            {alternatives.map((alternative) => (
              <div
                className="alternativeItem"
                key={`${alternative.recommendation.movie.tmdb_id}-${alternative.unmet_filters.join("|")}`}
              >
                <MovieCard
                  isAlternative
                  providerLookup={(result.provider_lookups ?? []).find(
                    (lookup) => lookup.tmdb_id === alternative.recommendation.movie.tmdb_id,
                  )}
                  recommendation={alternative.recommendation}
                />
                <div className="unmetFilters">
                  <strong>Unmet filters</strong>
                  <ul>
                    {alternative.unmet_filters.map((unmet) => (
                      <li key={unmet}>
                        <span>{unmet}</span>
                        <button
                          aria-label={`Relax or edit filter: ${unmet}`}
                          className="filterButton"
                          disabled={isRunning}
                          onClick={() => onRelaxFilter(unmet)}
                          type="button"
                        >
                          Relax
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
            ))}
          </div>
        </section>
      ) : null}
      {!result.clarification && !hasMatches ? (
        <div className="emptyState">
          {isBudgetExhausted(result) ? (
            <p className="empty">
              The search reached its provider budget before finding verified matches. No movie was
              invented, and no result was silently relaxed. Edit a filter to try a narrower search.
            </p>
          ) : (
            <p className="empty">
              No verified matches yet. Edit a filter or loosen one requirement to try again.
            </p>
          )}
          <button className="quietButton" onClick={onEditFilters} type="button">
            Adjust filters
          </button>
        </div>
      ) : null}
    </section>
  );
}
