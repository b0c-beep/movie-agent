import type {
  ClarificationOption,
  ConversationSnapshot,
  ConversationTurn,
  FilterEdit,
  RecommendationResult,
  TraceEvent,
} from "@/lib/api";

export type ThreadTurn = {
  id: string;
  runId: string | null;
  userMessage: string;
  status: string;
  result: RecommendationResult | null;
  answer: string | null;
  errorMessage: string | null;
  events: TraceEvent[];
  restored: boolean;
};

export type ActiveFilter = {
  id: string;
  field: string;
  group: string;
  display: string;
  value: string | number;
  remove?: FilterEdit;
  editable?: boolean;
  inputType?: "text" | "number";
  inputStep?: string;
};

const THREAD_STORAGE_PREFIX = "movie-agent-thread:";
const MAX_LOCAL_TURNS = 20;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

export function statusLabel(value: string): string {
  if (value === "completed") return "Complete";
  if (value === "needs_clarification") return "Needs clarification";
  if (value === "failed") return "Failed";
  if (value === "cancelled") return "Cancelled";
  if (value === "running") return "Running";
  return value
    .replaceAll("_", " ")
    .replace(/^./, (character) => character.toUpperCase());
}

export function normalizeClarificationOptions(value: unknown): ClarificationOption[] {
  if (!Array.isArray(value)) return [];
  const options = value.flatMap((item): ClarificationOption[] => {
    if (!isRecord(item)) return [];
    const choiceId = stringValue(item.choice_id);
    const label = stringValue(item.label);
    const kind = item.kind === "person" || item.kind === "title" ? item.kind : null;
    if (!choiceId || !label || !kind) return [];
    return [
      {
        choice_id: choiceId,
        kind,
        label,
        detail: stringValue(item.detail) ?? "",
      },
    ];
  });
  return options.length >= 2 && options.length <= 4 ? options : [];
}

export function normalizeResult(
  value: unknown,
  fallbackIntent: Record<string, unknown> = {},
): RecommendationResult | null {
  if (!isRecord(value)) return null;
  const recommendations = Array.isArray(value.recommendations) ? value.recommendations : null;
  const clarification = stringValue(value.clarification);
  if (!recommendations && !clarification) return null;
  const intent = isRecord(value.intent) ? value.intent : fallbackIntent;
  return {
    intent,
    recommendations: (recommendations ?? []) as RecommendationResult["recommendations"],
    requested_count:
      typeof value.requested_count === "number" ? value.requested_count : 5,
    shortfall_reason: stringValue(value.shortfall_reason),
    clarification,
    issues: Array.isArray(value.issues) ? (value.issues as RecommendationResult["issues"]) : [],
    provider_lookups: Array.isArray(value.provider_lookups)
      ? (value.provider_lookups as RecommendationResult["provider_lookups"])
      : [],
    alternatives: Array.isArray(value.alternatives)
      ? (value.alternatives as NonNullable<RecommendationResult["alternatives"]>).filter(
          (item) =>
            isRecord(item) &&
            isRecord(item.recommendation) &&
            Array.isArray(item.unmet_filters),
        )
      : [],
    clarification_options: normalizeClarificationOptions(value.clarification_options),
  };
}

export function failureMessage(event: TraceEvent): string {
  const errorCode = typeof event.metadata?.error_code === "string" ? event.metadata.error_code : null;
  const failureStage =
    typeof event.metadata?.failure_stage === "string" ? event.metadata.failure_stage : null;
  if (errorCode === "model_rate_limited") {
    const retryAfter = event.metadata?.retry_after_seconds;
    const hint =
      typeof retryAfter === "number" && retryAfter > 0
        ? `Wait about ${Math.ceil(retryAfter)} seconds, then try your request again.`
        : "Wait a moment, then try your request again.";
    return `The recommendation model is temporarily rate limited. Your conversation was not changed. ${hint}`;
  }
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

export function requestErrorMessage(reason: unknown, fallback: string): string {
  if (!(reason instanceof Error)) return fallback;
  if (/session expired|status 401/i.test(reason.message)) {
    return "Your session expired. Reload this tab to enter the passcode again.";
  }
  if (/status 429|rate limit|usage limit/i.test(reason.message)) {
    return "The recommendation usage limit was reached. Wait a little while and try again; no new request was started automatically.";
  }
  if (/status 409|version changed|active run/i.test(reason.message)) {
    return "This conversation changed while the request was running. Reload the conversation and try again.";
  }
  return reason.message;
}

export function interruptionMessage(runRecovered: boolean): string {
  if (runRecovered) {
    return "The live connection was interrupted. The saved run status is shown below and no new request was sent.";
  }
  return "The live connection was interrupted before a saved result was available. No new request was sent; try the request again when you are ready.";
}

export function isBudgetExhausted(result: RecommendationResult): boolean {
  if (/budget|exhaust|search limit/i.test(result.shortfall_reason ?? "")) return true;
  return result.issues.some((issue) => /budget|exhaust|limit/i.test(issue.code));
}

export function choiceMessage(option: ClarificationOption): string {
  return option.detail
    ? `I meant ${option.label} (${option.detail}).`
    : `I meant ${option.label}.`;
}

export function filterEditMessage(edit: FilterEdit): string {
  const labels: Record<string, string> = {
    genres: "genre",
    moods: "mood",
    people: "person",
    release_year_min: "earliest release year",
    release_year_max: "latest release year",
    original_language: "language",
    runtime_max_minutes: "maximum runtime",
    tmdb_rating_min: "minimum TMDB rating",
    requested_count: "requested result count",
  };
  const label = labels[edit.field] ?? edit.field.replaceAll("_", " ");
  if (edit.operation === "remove") {
    return `Remove ${String(edit.value)} from the ${label} filter.`;
  }
  if (edit.operation === "clear") {
    return `Clear the ${label} filter.`;
  }
  return `Change the ${label} filter to ${String(edit.value)}.`;
}

function intentStringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string" && item.trim().length > 0)
    : [];
}

export function activeFilters(intent: Record<string, unknown>): ActiveFilter[] {
  const filters: ActiveFilter[] = [];
  for (const genre of intentStringList(intent.genres)) {
    filters.push({
      id: `genre-${genre}`,
      field: "genres",
      group: "Genre",
      display: genre,
      value: genre,
      remove: { field: "genres", operation: "remove", value: genre },
    });
  }
  for (const mood of intentStringList(intent.moods)) {
    filters.push({
      id: `mood-${mood}`,
      field: "moods",
      group: "Mood",
      display: mood,
      value: mood,
      remove: { field: "moods", operation: "remove", value: mood },
    });
  }
  if (Array.isArray(intent.people)) {
    for (const person of intent.people) {
      if (!isRecord(person)) continue;
      const name = stringValue(person.name);
      if (!name) continue;
      const role = stringValue(person.role);
      filters.push({
        id: `person-${name}`,
        field: "people",
        group: role ? `Person (${role})` : "Person",
        display: name,
        value: name,
        remove: { field: "people", operation: "remove", value: name },
      });
    }
  }
  if (typeof intent.release_year_min === "number") {
    filters.push({
      id: "release-year-min",
      field: "release_year_min",
      group: "From year",
      display: String(intent.release_year_min),
      value: intent.release_year_min,
      remove: { field: "release_year_min", operation: "clear" },
      editable: true,
      inputType: "number",
    });
  }
  if (typeof intent.release_year_max === "number") {
    filters.push({
      id: "release-year-max",
      field: "release_year_max",
      group: "Through year",
      display: String(intent.release_year_max),
      value: intent.release_year_max,
      remove: { field: "release_year_max", operation: "clear" },
      editable: true,
      inputType: "number",
    });
  }
  const language = stringValue(intent.original_language);
  if (language) {
    filters.push({
      id: "original-language",
      field: "original_language",
      group: "Language",
      display: language.toUpperCase(),
      value: language,
      remove: { field: "original_language", operation: "clear" },
      editable: true,
      inputType: "text",
    });
  }
  if (typeof intent.runtime_max_minutes === "number") {
    filters.push({
      id: "runtime-max",
      field: "runtime_max_minutes",
      group: "Max runtime",
      display: `${intent.runtime_max_minutes} min`,
      value: intent.runtime_max_minutes,
      remove: { field: "runtime_max_minutes", operation: "clear" },
      editable: true,
      inputType: "number",
    });
  }
  if (typeof intent.tmdb_rating_min === "number") {
    filters.push({
      id: "tmdb-rating-min",
      field: "tmdb_rating_min",
      group: "TMDB rating",
      display: `${intent.tmdb_rating_min} or higher`,
      value: intent.tmdb_rating_min,
      remove: { field: "tmdb_rating_min", operation: "clear" },
      editable: true,
      inputType: "number",
      inputStep: "0.1",
    });
  }
  if (typeof intent.requested_count === "number") {
    filters.push({
      id: "requested-count",
      field: "requested_count",
      group: "Results requested",
      display: String(intent.requested_count),
      value: intent.requested_count,
      editable: true,
      inputType: "number",
    });
  }
  const country = stringValue(intent.country);
  if (country) {
    filters.push({
      id: "country",
      field: "country",
      group: "Availability country",
      display: country,
      value: country,
    });
  }
  return filters;
}

export function editValueForFilter(
  filter: ActiveFilter,
  rawValue: string,
): FilterEdit | null {
  const trimmed = rawValue.trim();
  if (!trimmed && filter.remove) {
    return filter.remove;
  }
  if (!trimmed) return null;
  if (filter.inputType === "number") {
    const value = Number(trimmed);
    if (!Number.isFinite(value) || value <= 0) return null;
    return { field: filter.field, operation: "set", value };
  }
  if (filter.field === "original_language") {
    if (!/^[a-z]{2}$/i.test(trimmed)) return null;
    return { field: filter.field, operation: "set", value: trimmed.toLowerCase() };
  }
  return { field: filter.field, operation: "set", value: trimmed };
}

function turnFromSnapshot(
  turn: ConversationTurn,
  index: number,
  fallbackIntent: Record<string, unknown>,
): ThreadTurn | null {
  if (!isRecord(turn)) return null;
  const userMessage = stringValue(turn.user_message);
  if (!userMessage) return null;
  const events = Array.isArray(turn.events) ? (turn.events as TraceEvent[]) : [];
  const result = normalizeResult(turn.response, fallbackIntent);
  const failedEvent = [...events].reverse().find((event) => event.type === "failed");
  const rawStatus = stringValue(turn.status) ?? "completed";
  const runId = stringValue(turn.run_id);
  return {
    id: runId ?? `restored-${index}`,
    runId,
    userMessage,
    status: statusLabel(rawStatus),
    result,
    answer: stringValue(turn.answer),
    errorMessage:
      rawStatus === "failed"
        ? failedEvent
          ? failureMessage(failedEvent)
          : "The recommendation run could not be completed."
        : null,
    events,
    restored: true,
  };
}

export function turnsFromSnapshot(snapshot: ConversationSnapshot): ThreadTurn[] {
  if (!Array.isArray(snapshot.turns)) return [];
  return snapshot.turns
    .map((turn, index) => turnFromSnapshot(turn, index, snapshot.intent))
    .filter((turn): turn is ThreadTurn => turn !== null);
}

export function latestResultFromTurns(turns: ThreadTurn[]): RecommendationResult | null {
  for (let index = turns.length - 1; index >= 0; index -= 1) {
    if (turns[index].result) return turns[index].result;
  }
  return null;
}

export function latestAnswerFromTurns(turns: ThreadTurn[]): string | null {
  for (let index = turns.length - 1; index >= 0; index -= 1) {
    if (turns[index].answer) return turns[index].answer;
  }
  return null;
}

export function eventsFromTurns(turns: ThreadTurn[]): TraceEvent[] {
  const seen = new Set<string>();
  const events: TraceEvent[] = [];
  for (const turn of turns) {
    for (const event of [...turn.events].sort((left, right) => left.sequence - right.sequence)) {
      const key = event.event_id || `${event.run_id}-${event.sequence}`;
      if (seen.has(key)) continue;
      seen.add(key);
      events.push(event);
    }
  }
  return events;
}

export function mergeRunEvents(
  existing: TraceEvent[],
  runId: string,
  incoming: TraceEvent[],
): TraceEvent[] {
  const ordered = [...incoming].sort((left, right) => left.sequence - right.sequence);
  const merged: TraceEvent[] = [];
  let inserted = false;
  for (const event of existing) {
    if (event.run_id === runId) {
      if (!inserted) {
        merged.push(...ordered);
        inserted = true;
      }
      continue;
    }
    merged.push(event);
  }
  if (!inserted) merged.push(...ordered);
  return merged;
}

export function readLocalTurns(conversationId: string): ThreadTurn[] {
  try {
    const raw = window.sessionStorage.getItem(`${THREAD_STORAGE_PREFIX}${conversationId}`);
    if (!raw) return [];
    const value: unknown = JSON.parse(raw);
    if (!Array.isArray(value)) return [];
    return value.flatMap((item, index): ThreadTurn[] => {
      if (!isRecord(item)) return [];
      const userMessage = stringValue(item.user_message) ?? stringValue(item.userMessage);
      if (!userMessage) return [];
      const result = normalizeResult(item.result);
      return [
        {
          id: stringValue(item.id) ?? `local-${index}`,
          runId: stringValue(item.runId) ?? stringValue(item.run_id),
          userMessage,
          status: stringValue(item.status) ?? "Complete",
          result,
          answer: stringValue(item.answer),
          errorMessage: stringValue(item.errorMessage),
          events: Array.isArray(item.events) ? (item.events as TraceEvent[]) : [],
          restored: false,
        },
      ];
    });
  } catch {
    return [];
  }
}

export function saveLocalTurns(conversationId: string, turns: ThreadTurn[]): void {
  try {
    const observed = turns.filter((turn) => !turn.restored).slice(-MAX_LOCAL_TURNS);
    if (observed.length === 0) {
      window.sessionStorage.removeItem(`${THREAD_STORAGE_PREFIX}${conversationId}`);
      return;
    }
    window.sessionStorage.setItem(
      `${THREAD_STORAGE_PREFIX}${conversationId}`,
      JSON.stringify(observed),
    );
  } catch {
    // The in-memory thread remains available when tab storage is full or blocked.
  }
}

export function clearLocalTurns(conversationId: string): void {
  try {
    window.sessionStorage.removeItem(`${THREAD_STORAGE_PREFIX}${conversationId}`);
  } catch {
    // Clearing tab-local history is best effort.
  }
}
