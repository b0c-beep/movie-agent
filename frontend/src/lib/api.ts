import { SseParser } from "@/lib/sse";

export type AppSession = {
  authenticated: true;
  session_id: string;
  expires_at: string;
};

export async function getCurrentSession(): Promise<AppSession | null> {
  const response = await fetch("/api/auth/session", { cache: "no-store" });
  if (response.status === 401) return null;
  if (!response.ok) throw new Error(`Session lookup failed with status ${response.status}`);
  return (await response.json()) as AppSession;
}

export async function unlockApp(passcode: string): Promise<AppSession> {
  const response = await fetch("/api/auth/unlock", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ passcode }),
  });
  if (response.status === 401) throw new Error("That passcode didn’t match. Try again.");
  if (!response.ok) throw new Error(`Could not unlock the app (status ${response.status})`);
  return (await response.json()) as AppSession;
}

export type TraceEvent = {
  event_id: string;
  run_id: string;
  sequence: number;
  stage: "predict" | "decide" | "act" | "complete";
  type: string;
  origin: "agent" | "application";
  summary: string;
  timestamp: string;
  metadata: Record<string, string | number | boolean | null>;
  data?: Record<string, unknown> | null;
};

export type RecommendationCard = {
  rank: number;
  match_reason: string;
  caveats: string[];
  movie: {
    tmdb_id: number;
    title: string;
    release_year: number | null;
    runtime_minutes: number | null;
    genres: string[];
    overview: string;
    poster_url: string | null;
    tmdb_url: string;
    imdb_url: string | null;
    ratings: { source: string; value: number | null; scale: string; vote_count: number | null }[];
    watch_options: {
      country: string;
      subscription: { provider_id: number; name: string; logo_path: string | null }[];
      free: { provider_id: number; name: string; logo_path: string | null }[];
      ads: { provider_id: number; name: string; logo_path: string | null }[];
      rent: { provider_id: number; name: string; logo_path: string | null }[];
      buy: { provider_id: number; name: string; logo_path: string | null }[];
    } | null;
  };
};

export type ClarificationOption = {
  choice_id: string;
  kind: "person" | "title";
  label: string;
  detail: string;
};

export type AlternativeRecommendation = {
  recommendation: RecommendationCard;
  unmet_filters: string[];
};

export type FilterEdit = {
  field: string;
  operation: "set" | "remove" | "clear";
  value?: string | number;
};

export type RecommendationCapabilities = {
  history: boolean;
  structured_clarification: boolean;
  filter_edit: boolean;
  alternatives: boolean;
  run_status: boolean;
};

export type RecommendationResult = {
  intent: Record<string, unknown>;
  recommendations: RecommendationCard[];
  requested_count: number;
  shortfall_reason: string | null;
  clarification?: string | null;
  issues: { code: string; message: string; field: string | null }[];
  provider_lookups: {
    tmdb_id: number;
    country: string;
    outcome: string;
    provider_count: number;
    detail?: string | null;
  }[];
  alternatives?: AlternativeRecommendation[];
  clarification_options?: ClarificationOption[];
};

export type ConversationTurn = {
  run_id: string;
  user_message: string;
  status: string;
  response: RecommendationResult | null;
  answer: string | null;
  events: TraceEvent[];
};

export type ConversationSnapshot = {
  conversation_id: string;
  version: number;
  intent: Record<string, unknown>;
  clarification?: string | null;
  clarification_options?: ClarificationOption[];
  turns?: ConversationTurn[];
  result: {
    recommendations: RecommendationCard[];
    requested_count: number;
    shortfall_reason: string | null;
    alternatives?: AlternativeRecommendation[];
    clarification_options?: ClarificationOption[];
  } | null;
  issues: { code: string; message: string; field: string | null; tmdb_id: number | null }[];
  provider_lookups: RecommendationResult["provider_lookups"];
};

export type RunStatusSnapshot = {
  run_id: string;
  conversation_id: string;
  status: string;
  version: number;
  events: TraceEvent[];
  response: RecommendationResult | null;
};

export async function getConversation(conversationId: string): Promise<ConversationSnapshot | null> {
  const response = await fetch(`/api/recommendations/conversations/${conversationId}`, {
    cache: "no-store",
  });
  if (response.status === 404) return null;
  if (!response.ok) throw new Error(`Conversation lookup failed with status ${response.status}`);
  return (await response.json()) as ConversationSnapshot;
}

export async function getRecommendationCapabilities(): Promise<RecommendationCapabilities | null> {
  try {
    const response = await fetch("/api/recommendations/capabilities", { cache: "no-store" });
    if (!response.ok) return null;
    const raw = (await response.json()) as Partial<RecommendationCapabilities> | null;
    if (!raw) return null;
    return {
      history: raw.history === true,
      structured_clarification: raw.structured_clarification === true,
      filter_edit: raw.filter_edit === true,
      alternatives: raw.alternatives === true,
      run_status: raw.run_status === true,
    };
  } catch {
    return null;
  }
}

export async function getRunStatus(runId: string): Promise<RunStatusSnapshot | null> {
  const response = await fetch(`/api/recommendations/runs/${encodeURIComponent(runId)}`, {
    cache: "no-store",
  });
  if (response.status === 404) return null;
  if (!response.ok) throw new Error(`Run status lookup failed with status ${response.status}`);
  return (await response.json()) as RunStatusSnapshot;
}

export async function refreshConversationAvailability(
  conversationId: string,
  country: string,
  expectedVersion: number,
): Promise<ConversationSnapshot> {
  const response = await fetch(`/api/recommendations/conversations/${conversationId}/availability`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ country, expected_version: expectedVersion }),
  });
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(payload?.detail ?? `Availability refresh failed with status ${response.status}`);
  }
  return (await response.json()) as ConversationSnapshot;
}

export async function streamRecommendations(
  input: {
    message: string;
    clientRequestId: string;
    conversationId: string | null;
    expectedVersion: number | null;
    clarificationChoiceId?: string | null;
    filterEdit?: FilterEdit | null;
  },
  onEvent: (event: TraceEvent) => void,
  signal?: AbortSignal,
  onStart?: (ids: { conversationId: string; runId: string }) => void,
): Promise<{ conversationId: string; runId: string }> {
  const response = await fetch("/api/recommendations/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({
      message: input.message,
      client_request_id: input.clientRequestId,
      conversation_id: input.conversationId,
      expected_version: input.expectedVersion,
      ...(input.clarificationChoiceId
        ? { clarification_choice_id: input.clarificationChoiceId }
        : {}),
      ...(input.filterEdit ? { filter_edit: input.filterEdit } : {}),
    }),
    signal,
  });

  if (!response.ok || !response.body) {
    const payload = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(payload?.detail ?? `Recommendation request failed with status ${response.status}`);
  }

  const conversationId = response.headers.get("x-conversation-id");
  const runId = response.headers.get("x-run-id");
  if (!conversationId || !runId) throw new Error("Recommendation response omitted run identifiers");
  onStart?.({ conversationId, runId });

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const parser = new SseParser();
  const consume = (text: string) => {
    for (const message of parser.feed(text)) onEvent(JSON.parse(message.data) as TraceEvent);
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    consume(decoder.decode(value, { stream: true }));
  }
  consume(decoder.decode());
  for (const message of parser.finish()) onEvent(JSON.parse(message.data) as TraceEvent);
  return { conversationId, runId };
}

export async function streamFixture(
  prompt: string,
  onEvent: (event: TraceEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch("/api/fixture-stream", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ prompt }),
    signal,
  });

  if (!response.ok || !response.body) {
    throw new Error(`Fixture stream failed with status ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const parser = new SseParser();

  const consume = (text: string) => {
    for (const message of parser.feed(text)) {
      onEvent(JSON.parse(message.data) as TraceEvent);
    }
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    consume(decoder.decode(value, { stream: true }));
  }

  consume(decoder.decode());
  for (const message of parser.finish()) {
    onEvent(JSON.parse(message.data) as TraceEvent);
  }
}
