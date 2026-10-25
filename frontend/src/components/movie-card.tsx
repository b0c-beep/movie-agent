"use client";

import type { RecommendationCard, RecommendationResult } from "@/lib/api";

type ProviderLookup = RecommendationResult["provider_lookups"][number];

const RATING_LABELS: Record<string, string> = {
  tmdb: "TMDB",
  imdb: "IMDb",
  rotten_tomatoes: "Rotten Tomatoes",
};

function ratingLabel(source: string): string {
  return RATING_LABELS[source] ?? source.replaceAll("_", " ");
}

function ratingValue(value: number, scale: string): string {
  const formatted = scale === "10" || !Number.isInteger(value) ? value.toFixed(1) : String(value);
  return `${formatted} / ${scale}`;
}

export function MovieCard({
  recommendation,
  providerLookup,
  isAlternative = false,
}: {
  recommendation: RecommendationCard;
  providerLookup?: ProviderLookup;
  isAlternative?: boolean;
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
    <article className="movieCard" data-alternative={isAlternative ? "true" : undefined}>
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
        {isAlternative ? <p className="alternativeBadge">Closest alternative</p> : null}
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
