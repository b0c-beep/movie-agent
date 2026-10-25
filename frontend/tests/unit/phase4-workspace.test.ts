import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RecommendationWorkspace } from "@/components/recommendation-workspace";

const mocks = vi.hoisted(() => ({
  getConversation: vi.fn(),
  getRecommendationCapabilities: vi.fn(),
  getRunStatus: vi.fn(),
  streamRecommendations: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  getConversation: mocks.getConversation,
  getRecommendationCapabilities: mocks.getRecommendationCapabilities,
  getRunStatus: mocks.getRunStatus,
  streamRecommendations: mocks.streamRecommendations,
}));

const STORAGE_KEY = "movie-agent-conversation";

type StreamInput = {
  message: string;
  conversationId: string | null;
  expectedVersion: number | null;
  clarificationChoiceId?: string | null;
  filterEdit?: { field: string; operation: string; value?: string | number } | null;
};

function recommendation(title: string, tmdbId: number, year = 1999) {
  return {
    rank: 1,
    match_reason: "TMDB lists this as science fiction.",
    caveats: [],
    movie: {
      tmdb_id: tmdbId,
      title,
      release_year: year,
      runtime_minutes: 136,
      genres: ["Science Fiction"],
      overview: "A grounded movie.",
      poster_url: null,
      tmdb_url: `https://www.themoviedb.org/movie/${tmdbId}`,
      imdb_url: null,
      ratings: [{ source: "tmdb", value: 8.2, scale: "10", vote_count: 100 }],
      watch_options: {
        country: "RO",
        subscription: [],
        free: [],
        ads: [],
        rent: [],
        buy: [],
      },
    },
  };
}

function result(overrides: Record<string, unknown> = {}) {
  return {
    intent: { genres: ["Comedy"], requested_count: 1, country: "RO" },
    recommendations: [recommendation("The Matrix", 603)],
    requested_count: 1,
    shortfall_reason: null,
    issues: [],
    provider_lookups: [],
    ...overrides,
  };
}

function traceEvent(
  type: string,
  sequence: number,
  overrides: Record<string, unknown> = {},
) {
  return {
    event_id: `${type}-${sequence}`,
    run_id: "run-1",
    sequence,
    stage: "complete",
    type,
    origin: "application",
    summary: `${type} summary`,
    timestamp: "2026-10-25T12:00:00Z",
    metadata: {},
    data: null,
    ...overrides,
  };
}

function useConversation(
  id: string,
  version: number,
  snapshot: Record<string, unknown>,
) {
  window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ id, version }));
  mocks.getConversation.mockResolvedValue({
    conversation_id: id,
    version,
    intent: {},
    result: null,
    issues: [],
    provider_lookups: [],
    ...snapshot,
  });
}

function mockStream(
  events: (input: StreamInput) => Record<string, unknown>[],
  options: { failAfterStart?: Error } = {},
) {
  const calls: StreamInput[] = [];
  mocks.streamRecommendations.mockImplementation(
    async (
      input: StreamInput,
      onEvent: (event: Record<string, unknown>) => void,
      _signal: AbortSignal,
      onStart?: (ids: { conversationId: string; runId: string }) => void,
    ) => {
      calls.push(input);
      const conversationId = input.conversationId ?? "conv-new";
      onStart?.({ conversationId, runId: "run-1" });
      for (const event of events(input)) onEvent(event);
      if (options.failAfterStart) throw options.failAfterStart;
      return { conversationId, runId: "run-1" };
    },
  );
  return calls;
}

describe("phase 4 conversation workspace", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    mocks.getConversation.mockReset();
    mocks.getConversation.mockResolvedValue(null);
    mocks.getRecommendationCapabilities.mockReset();
    mocks.getRecommendationCapabilities.mockResolvedValue(null);
    mocks.getRunStatus.mockReset();
    mocks.streamRecommendations.mockReset();
  });

  afterEach(() => {
    cleanup();
  });

  it("restores the ordered server thread and retained events", async () => {
    const firstResult = result();
    useConversation("conv-history", 4, {
      intent: { genres: ["Comedy"], requested_count: 1, country: "RO" },
      result: firstResult,
      turns: [
        {
          run_id: "run-1",
          user_message: "one comedy",
          status: "completed",
          response: firstResult,
          answer: null,
          events: [traceEvent("run_started", 1), traceEvent("completed", 2)],
        },
        {
          run_id: "run-2",
          user_message: "tell me about the first one",
          status: "completed",
          response: null,
          answer: "The Matrix (1999); genres Science Fiction.",
          events: [
            traceEvent("run_started", 1, { run_id: "run-2", event_id: "run-2-start" }),
            traceEvent("completed", 2, { run_id: "run-2", event_id: "run-2-complete" }),
          ],
        },
      ],
    });

    render(createElement(RecommendationWorkspace));

    expect(await screen.findByText("Restored from this conversation.")).toBeInTheDocument();
    expect(screen.getByText("one comedy")).toBeInTheDocument();
    expect(screen.getByText("tell me about the first one")).toBeInTheDocument();
    expect(screen.getAllByText(/The Matrix \(1999\)/).length).toBeGreaterThan(0);
    expect(screen.getByRole("heading", { name: "The Matrix" })).toBeInTheDocument();
    expect(screen.getByText("Comedy")).toBeInTheDocument();
    expect(screen.getByText(/Turn 1/)).toBeInTheDocument();
    expect(screen.getByText(/Turn 2/)).toBeInTheDocument();
  });

  it("keeps observed turns in this tab without claiming restored history", async () => {
    useConversation("conv-local", 1, { intent: { requested_count: 1 }, result: null });
    const calls = mockStream(() => [
      traceEvent("run_started", 1),
      traceEvent("recommendations", 2, { data: result() }),
      traceEvent("completed", 3),
    ]);

    render(createElement(RecommendationWorkspace));
    await screen.findByRole("button", { name: "New search" });
    fireEvent.change(screen.getByLabelText(/in the mood/i), {
      target: { value: "one comedy" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Get recommendations" }));

    expect(await screen.findByTestId("status")).toHaveTextContent("Complete");
    expect(screen.getByText("one comedy")).toBeInTheDocument();
    expect(
      screen.getByText("Kept in this tab; older history is not available from this backend."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Restored from this conversation.")).not.toBeInTheDocument();
    expect(calls).toHaveLength(1);
    expect(
      window.sessionStorage.getItem("movie-agent-thread:conv-local"),
    ).toContain("one comedy");
  });

  it("sends a structured clarification choice when capabilities support it", async () => {
    mocks.getRecommendationCapabilities.mockResolvedValue({
      history: true,
      structured_clarification: true,
      filter_edit: true,
      alternatives: true,
      run_status: true,
    });
    useConversation("conv-choice", 2, {
      intent: { requested_count: 5, country: "RO" },
      clarification: "Which Jim Carrey do you mean?",
      clarification_options: [
        { choice_id: "person-1", kind: "person", label: "Jim Carrey", detail: "Actor" },
        { choice_id: "person-2", kind: "person", label: "Jim Carrey", detail: "Producer" },
      ],
    });
    const calls = mockStream(() => [
      traceEvent("run_started", 1),
      traceEvent("recommendations", 2, { data: result() }),
      traceEvent("completed", 3),
    ]);

    render(createElement(RecommendationWorkspace));
    fireEvent.click(await screen.findByRole("button", { name: "Use this choice" }));

    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].clarificationChoiceId).toBe("person-1");
    expect(calls[0].message).toContain("I meant Jim Carrey");
  });

  it("falls back to a descriptive message when clarification choices are unsupported", async () => {
    useConversation("conv-choice", 2, {
      clarification: "Which Jim Carrey do you mean?",
      clarification_options: [
        { choice_id: "person-1", kind: "person", label: "Jim Carrey", detail: "Actor" },
        { choice_id: "person-2", kind: "person", label: "Jim Carrey", detail: "Producer" },
      ],
    });
    const calls = mockStream(() => [
      traceEvent("run_started", 1),
      traceEvent("recommendations", 2, { data: result() }),
      traceEvent("completed", 3),
    ]);

    render(createElement(RecommendationWorkspace));
    fireEvent.click(await screen.findByRole("button", { name: "Use this choice" }));

    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].clarificationChoiceId).toBeNull();
    expect(calls[0].message).toContain("I meant Jim Carrey");
  });

  it("uses a typed filter edit when supported and a natural-language fallback otherwise", async () => {
    useConversation("conv-edit", 2, {
      intent: { genres: ["Comedy"], requested_count: 1, country: "RO" },
      result: result(),
    });
    mocks.getRecommendationCapabilities.mockResolvedValue({
      history: true,
      structured_clarification: true,
      filter_edit: true,
      alternatives: true,
      run_status: true,
    });
    let calls = mockStream(() => [
      traceEvent("run_started", 1),
      traceEvent("recommendations", 2, { data: result({ intent: { requested_count: 1 } }) }),
      traceEvent("completed", 3),
    ]);

    render(createElement(RecommendationWorkspace));
    fireEvent.click(await screen.findByRole("button", { name: "Remove Genre Comedy filter" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].filterEdit).toEqual({
      field: "genres",
      operation: "remove",
      value: "Comedy",
    });

    cleanup();
    window.sessionStorage.clear();
    mocks.streamRecommendations.mockReset();
    mocks.getRecommendationCapabilities.mockResolvedValue(null);
    useConversation("conv-edit", 2, {
      intent: { genres: ["Comedy"], requested_count: 1, country: "RO" },
      result: result(),
    });
    calls = mockStream(() => [
      traceEvent("run_started", 1),
      traceEvent("recommendations", 2, { data: result({ intent: { requested_count: 1 } }) }),
      traceEvent("completed", 3),
    ]);

    render(createElement(RecommendationWorkspace));
    fireEvent.click(await screen.findByRole("button", { name: "Remove Genre Comedy filter" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].filterEdit).toBeNull();
    expect(calls[0].message).toBe("Remove Comedy from the genre filter.");
  });

  it("shows exact results and closest alternatives with every unmet filter", async () => {
    useConversation("conv-alternatives", 2, {
      result: result({
        alternatives: [
          {
            recommendation: recommendation("Arrival", 329865, 2016),
            unmet_filters: ["Genre: Comedy", "Year: 1990s"],
          },
          {
            recommendation: recommendation("Moon", 17431, 2009),
            unmet_filters: ["Genre: Comedy"],
          },
        ],
      }),
    });

    render(createElement(RecommendationWorkspace));

    expect(
      await screen.findByRole("heading", { name: "Closest alternatives" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Exact matches" })).toBeInTheDocument();
    expect(screen.getAllByText("Genre: Comedy").length).toBeGreaterThan(0);
    expect(screen.getByText("Year: 1990s")).toBeInTheDocument();
    expect(screen.getAllByText("Closest alternative")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Relax or edit filter: Year: 1990s" })).toBeInTheDocument();
  });

  it("reports model rate limiting with a safe retry hint", async () => {
    mockStream(() => [
      traceEvent("run_started", 1),
      traceEvent("failed", 2, {
        metadata: {
          error_code: "model_rate_limited",
          retry_after_seconds: 20,
        },
      }),
    ]);

    render(createElement(RecommendationWorkspace));
    fireEvent.change(screen.getByLabelText(/in the mood/i), {
      target: { value: "three comedies" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Get recommendations" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The recommendation model is temporarily rate limited",
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Wait about 20 seconds");
    expect(screen.getByTestId("status")).toHaveTextContent("Failed");
  });

  it("recovers an interrupted stream from the owned run status without resubmitting", async () => {
    useConversation("conv-recover", 4, { intent: { requested_count: 1 }, result: null });
    mocks.getRecommendationCapabilities.mockResolvedValue({
      history: true,
      structured_clarification: true,
      filter_edit: true,
      alternatives: true,
      run_status: true,
    });
    mocks.getConversation.mockImplementation(async (id: string) => ({
      conversation_id: id,
      version: 5,
      intent: { requested_count: 1, country: "RO" },
      result: result(),
      issues: [],
      provider_lookups: [],
    }));
    const calls = mockStream(
      () => [traceEvent("run_started", 1)],
      { failAfterStart: new Error("network error while reading stream") },
    );
    mocks.getRunStatus.mockResolvedValue({
      run_id: "run-1",
      conversation_id: "conv-recover",
      status: "completed",
      version: 5,
      events: [traceEvent("run_started", 1), traceEvent("completed", 2)],
      response: result(),
    });

    render(createElement(RecommendationWorkspace));
    await screen.findByRole("button", { name: "New search" });
    fireEvent.change(screen.getByLabelText(/in the mood/i), {
      target: { value: "one comedy" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Get recommendations" }));

    expect(await screen.findByRole("heading", { name: "The Matrix" })).toBeInTheDocument();
    expect(screen.getByTestId("status")).toHaveTextContent("Complete");
    expect(mocks.getRunStatus).toHaveBeenCalledWith("run-1");
    expect(calls).toHaveLength(1);
    await waitFor(() =>
      expect(window.sessionStorage.getItem(STORAGE_KEY)).toBe(
        JSON.stringify({ id: "conv-recover", version: 5 }),
      ),
    );
  });

  it("shows a safe interruption without querying run status on an older backend", async () => {
    useConversation("conv-interrupted", 2, { intent: {}, result: null });
    const calls = mockStream(
      () => [traceEvent("run_started", 1)],
      { failAfterStart: new Error("network error while reading stream") },
    );

    render(createElement(RecommendationWorkspace));
    await screen.findByRole("button", { name: "New search" });
    fireEvent.change(screen.getByLabelText(/in the mood/i), {
      target: { value: "one comedy" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Get recommendations" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The live connection was interrupted",
    );
    expect(screen.getByRole("alert")).toHaveTextContent("No new request was sent");
    expect(mocks.getRunStatus).not.toHaveBeenCalled();
    expect(calls).toHaveLength(1);
  });
});
