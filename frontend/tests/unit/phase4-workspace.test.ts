import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RecommendationWorkspace } from "@/components/recommendation-workspace";
import { normalizeResult } from "@/lib/workspace";

const mocks = vi.hoisted(() => ({
  getConversation: vi.fn(),
  getRecommendationCapabilities: vi.fn(),
  getRunStatus: vi.fn(),
  streamRecommendations: vi.fn(),
}));

describe("recommendation defaults", () => {
  it("uses four results when a normalized response omits its requested count", () => {
    expect(normalizeResult({ recommendations: [] })?.requested_count).toBe(4);
  });
});

vi.mock("@/lib/api", () => ({
  getConversation: mocks.getConversation,
  getRecommendationCapabilities: mocks.getRecommendationCapabilities,
  getRunStatus: mocks.getRunStatus,
  streamRecommendations: mocks.streamRecommendations,
}));

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
  mocks.getConversation.mockImplementation(async (conversationId: string) => ({
    version,
    intent: {},
    result: null,
    issues: [],
    provider_lookups: [],
    ...snapshot,
    conversation_id: conversationId,
  }));
}

async function startConversation(prompt = "seed conversation") {
  fireEvent.change(screen.getByLabelText("Message"), { target: { value: prompt } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await screen.findByRole("button", { name: "New chat" });
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
      const runId = `run-${calls.length}`;
      onStart?.({ conversationId, runId });
      for (const event of events(input)) {
        onEvent({ ...event, run_id: runId, event_id: `${runId}-${event.sequence}` });
      }
      if (options.failAfterStart) throw options.failAfterStart;
      return { conversationId, runId };
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

  it("starts with a blank workspace instead of restoring browser conversation state", async () => {
    window.sessionStorage.setItem("movie-agent-conversation", JSON.stringify({ id: "old", version: 4 }));
    window.sessionStorage.setItem("movie-agent-thread:old", "private old conversation text");
    render(createElement(RecommendationWorkspace));

    expect(await screen.findByRole("heading", { name: "What should we watch tonight?" })).toBeInTheDocument();
    expect(within(document.getElementById("example-prompts")!).getAllByRole("option")).toHaveLength(9);
    expect(mocks.getConversation).not.toHaveBeenCalled();
    expect(screen.queryByText("private old conversation text")).not.toBeInTheDocument();
  });

  it("keeps observed turns in the current page", async () => {
    const calls = mockStream(() => [
      traceEvent("run_started", 1),
      traceEvent("recommendations", 2, { data: result() }),
      traceEvent("completed", 3),
    ]);

    render(createElement(RecommendationWorkspace));
    fireEvent.change(screen.getByLabelText("Message"), {
      target: { value: "one comedy" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByText("Complete")).toBeInTheDocument();
    expect(screen.getByText("one comedy")).toBeInTheDocument();
    expect(screen.getByText("run_started summary")).toBeInTheDocument();
    expect(calls).toHaveLength(1);
    expect(window.sessionStorage.getItem("movie-agent-thread:conv-new")).toBeNull();
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
    let run = 0;
    const calls = mockStream(() => {
      run += 1;
      return run === 1
        ? [
            traceEvent("run_started", 1),
            traceEvent("needs_clarification", 2, {
              data: {
                ...result(),
                clarification: "Which Jim Carrey do you mean?",
                clarification_options: [
                  { choice_id: "person-1", kind: "person", label: "Jim Carrey", detail: "Actor" },
                  { choice_id: "person-2", kind: "person", label: "Jim Carrey", detail: "Producer" },
                ],
              },
            }),
          ]
        : [
            traceEvent("run_started", 1),
            traceEvent("recommendations", 2, { data: result() }),
            traceEvent("completed", 3),
          ];
    });

    render(createElement(RecommendationWorkspace));
    await startConversation("Who do you mean by Jim Carrey?");
    fireEvent.click(await screen.findByRole("button", { name: "Use this choice" }));

    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1].clarificationChoiceId).toBe("person-1");
    expect(calls[1].message).toContain("I meant Jim Carrey");
  });

  it("falls back to a descriptive message when clarification choices are unsupported", async () => {
    useConversation("conv-choice", 2, {
      clarification: "Which Jim Carrey do you mean?",
      clarification_options: [
        { choice_id: "person-1", kind: "person", label: "Jim Carrey", detail: "Actor" },
        { choice_id: "person-2", kind: "person", label: "Jim Carrey", detail: "Producer" },
      ],
    });
    let run = 0;
    const calls = mockStream(() => {
      run += 1;
      return run === 1
        ? [
            traceEvent("run_started", 1),
            traceEvent("needs_clarification", 2, {
              data: {
                ...result(),
                clarification: "Which Jim Carrey do you mean?",
                clarification_options: [
                  { choice_id: "person-1", kind: "person", label: "Jim Carrey", detail: "Actor" },
                  { choice_id: "person-2", kind: "person", label: "Jim Carrey", detail: "Producer" },
                ],
              },
            }),
          ]
        : [
            traceEvent("run_started", 1),
            traceEvent("recommendations", 2, { data: result() }),
            traceEvent("completed", 3),
          ];
    });

    render(createElement(RecommendationWorkspace));
    await startConversation("Who do you mean by Jim Carrey?");
    fireEvent.click(await screen.findByRole("button", { name: "Use this choice" }));

    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1].clarificationChoiceId).toBeNull();
    expect(calls[1].message).toContain("I meant Jim Carrey");
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
      traceEvent("recommendations", 2, { data: result() }),
      traceEvent("completed", 3),
    ]);

    render(createElement(RecommendationWorkspace));
    await startConversation("Show me one comedy");
    fireEvent.click(await screen.findByRole("button", { name: "Remove Genre Comedy filter" }));
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1].filterEdit).toEqual({
      field: "genres",
      operation: "remove",
      value: "Comedy",
    });

    cleanup();
    mocks.streamRecommendations.mockReset();
    mocks.getRecommendationCapabilities.mockResolvedValue(null);
    useConversation("conv-edit", 2, {
      intent: { genres: ["Comedy"], requested_count: 1, country: "RO" },
      result: result(),
    });
    calls = mockStream(() => [
      traceEvent("run_started", 1),
      traceEvent("recommendations", 2, { data: result() }),
      traceEvent("completed", 3),
    ]);

    render(createElement(RecommendationWorkspace));
    await startConversation("Show me one comedy");
    fireEvent.click(await screen.findByRole("button", { name: "Remove Genre Comedy filter" }));
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1].filterEdit).toBeNull();
    expect(calls[1].message).toBe("Remove Comedy from the genre filter.");
  });

  it("shows exact results and closest alternatives with every unmet filter", async () => {
    mocks.getRecommendationCapabilities.mockResolvedValue({
      history: true,
      structured_clarification: true,
      filter_edit: true,
      alternatives: true,
      run_status: true,
    });
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
    const alternatives = result({
      alternatives: [
        {
          recommendation: recommendation("Arrival", 329865, 2016),
          unmet_filters: ["Genre: Comedy", "Year: 1990s"],
        },
        { recommendation: recommendation("Moon", 17431, 2009), unmet_filters: ["Genre: Comedy"] },
      ],
    });
    const calls = mockStream(() => [
      traceEvent("run_started", 1),
      traceEvent("recommendations", 2, { data: alternatives }),
      traceEvent("completed", 3),
    ]);

    render(createElement(RecommendationWorkspace));
    await startConversation("Show me one comedy");

    expect(
      await screen.findByRole("heading", { name: "Closest alternatives" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "1 of 1 exact picks" })).toBeInTheDocument();
    expect(screen.getAllByText("Genre: Comedy").length).toBeGreaterThan(0);
    expect(screen.getByText("Year: 1990s")).toBeInTheDocument();
    expect(screen.getAllByText("Closest alternative")).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: /Remove filter:/ })).toHaveLength(3);
    fireEvent.click(screen.getAllByRole("button", { name: "Remove filter: Genre: Comedy" })[0]);
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1].filterEdit).toEqual({
      field: "genres",
      operation: "remove",
      value: "Comedy",
    });
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
    fireEvent.change(screen.getByLabelText("Message"), {
      target: { value: "three comedies" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The recommendation model is temporarily rate limited",
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Wait about 20 seconds");
    expect(screen.getByText("Failed")).toBeInTheDocument();
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
    fireEvent.change(screen.getByLabelText("Message"), {
      target: { value: "one comedy" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(
      await screen.findByRole("button", { name: "Show details for The Matrix" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Complete")).toBeInTheDocument();
    expect(mocks.getRunStatus).toHaveBeenCalledWith("run-1");
    expect(calls).toHaveLength(1);
    expect(window.sessionStorage.getItem("movie-agent-conversation")).toBeNull();
  });

  it("shows a safe interruption without querying run status on an older backend", async () => {
    useConversation("conv-interrupted", 2, { intent: {}, result: null });
    const calls = mockStream(
      () => [traceEvent("run_started", 1)],
      { failAfterStart: new Error("network error while reading stream") },
    );

    render(createElement(RecommendationWorkspace));
    fireEvent.change(screen.getByLabelText("Message"), {
      target: { value: "one comedy" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The live connection was interrupted",
    );
    expect(screen.getByRole("alert")).toHaveTextContent("No new request was sent");
    expect(mocks.getRunStatus).not.toHaveBeenCalled();
    expect(calls).toHaveLength(1);
  });
});
